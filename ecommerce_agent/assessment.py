"""经营关注分级：可解释业务策略，不是统计显著性检验，也不推断根因。"""
from datetime import date
from statistics import median
from .business_calendar import day_context

POLICY_VERSION='attention-v1'
# 初始学习策略，必须随真实门店回测校准；不宣称鲜花行业通用阈值。
POLICIES={'爆款':{'min_loss':8,'min_drop_pct':20},'常规款':{'min_loss':4,'min_drop_pct':25},
          '长尾款':{'min_loss':3,'min_drop_pct':40}}
LABELS={'normal':'正常波动','attention':'值得关注','insufficient':'证据不足','conflict':'数据冲突'}


def assess(analysis, observations, product=None):
    """综合质量、损失、同星期可比历史与生命周期；返回可审计判定及排除的历史日期。"""
    product=product or {};tags=product.get('business_tags') or []
    tier=next((tag for tag in ('爆款','长尾款') if tag in tags),'常规款')
    policy=dict(POLICIES[tier]);facts=analysis.get('facts',{});fact=facts.get('units_sold')
    trend=observations.get('sales_trend',{});rows=trend.get('data',{}).get('records',[])
    sales=observations.get('sales',{});pair=sales.get('data',{}).get('records',[])
    target_day=sales.get('data',{}).get('target_date')
    target=next((r for r in rows if r['date']==target_day),None)
    refs=[o['evidence_id'] for o in observations.values() if o.get('ok')]
    result={'policy_version':POLICY_VERSION,'scope':'销量关注分级，不代表取消率、推广或整店健康。','tier':tier,'thresholds':policy,'new_product_days':14,
        'required_history_days':3,'reference':None,'history_used':[],'history_excluded':[],
        'evidence_ids':refs,'reasons':[],'caveat':'初始业务关注策略，需门店数据回测校准；不是因果或统计显著性结论。'}
    def finish(code,reason):
        result.update(code=code,label=LABELS[code]);result['reasons'].append(reason);return result
    if analysis.get('conflicts'):return finish('conflict','来源指标或统计口径存在冲突，先核对数据。')
    if not fact or analysis.get('gaps'):return finish('insufficient','基础来源存在缺失、延迟或监测不足，不能可靠分级。')
    loss=max(0,fact['before']-fact['after']);drop=-fact['relative_pct'] if fact['relative_pct'] is not None else None
    result['primary']={'loss_units':loss,'drop_pct':drop,'baseline_units':fact['before'],'target_units':fact['after']}
    if not target_day:return finish('insufficient','缺少明确的目标日期。')
    age=(date.fromisoformat(target_day)-date.fromisoformat(product['listed_at'])).days if product.get('listed_at') else None
    result['product_age_days']=age
    if age is not None and age<0:return finish('conflict','目标日在目录上架时间之前却有销售记录，需核对商品目录与销售日期。')
    if age is not None and age<14:
        return finish('insufficient',f'上架仅{age+1}个自然日，按新品观察，暂不套用成熟商品历史基线。')
    if age is None:result['reasons'].append('上架日期未提供，生命周期判断受限。')
    tcal=day_context(target_day);target_signature=(target.get('campaign_active'),target.get('campaign_name')) if target else None
    for row in rows:
        distance=(date.fromisoformat(target_day)-date.fromisoformat(row['date'])).days
        if distance<=0 or distance>35 or distance%7:continue
        cal=day_context(row['date']);reason=None
        if row['status']!='ok':reason='销量记录不可用或未上架'
        elif sales.get('data',{}).get('sample_data') and bool(target and target.get('records',{}).get('sales',{}).get('source_id','').startswith('import:')) != bool(row.get('records',{}).get('sales',{}).get('source_id','').startswith('import:')):reason='学习样例与用户导入记录不混作历史基线'
        elif not cal['schedule_known'] or not tcal['schedule_known']:reason='年度假期安排未核实'
        elif (cal['day_type'],cal['holiday_name'])!=(tcal['day_type'],tcal['holiday_name']):reason='假期或调休背景不同'
        elif target_signature is None or target_signature[0] is None or row.get('campaign_active') is None:reason='活动记录不完整'
        elif (row['campaign_active'],row['campaign_name'])!=target_signature:reason='活动状态或名称不同'
        if reason:result['history_excluded'].append({'date':row['date'],'reason':reason})
        else:result['history_used'].append({'date':row['date'],'units_sold':row['units_sold']})
    changed_context=False
    if len(pair)==2:
        bcal=day_context(pair[0]['date'])
        promo=observations.get('promotions',{}).get('data',{}).get('records',[])
        changed_context=(not tcal['schedule_known'] or not bcal['schedule_known'] or
            (bcal['day_type'],bcal['holiday_name'])!=(tcal['day_type'],tcal['holiday_name']) or
            len(promo)!=2 or any(r['status']!='ok' for r in promo) or
            (promo[0].get('active'),promo[0].get('campaign_name'))!=(promo[1].get('active'),promo[1].get('campaign_name')))
    result['primary_context_differs']=changed_context
    enough=len(result['history_used'])>=3
    if enough:
        values=[r['units_sold'] for r in result['history_used']];center=median(values)
        mad=median(abs(value-center) for value in values)
        # 同时满足绝对损失、比例和历史波动带；MAD用于稳健参考，不声称概率置信度。
        boundary=center-max(policy['min_loss'],center*policy['min_drop_pct']/100,3*mad)
        result['reference']={'median_units':center,'mad_units':mad,'lower_attention_boundary':round(boundary,2),
            'loss_units':max(0,center-fact['after']),'drop_pct':round((center-fact['after'])/center*100,2) if center else None}
        if fact['after']<=boundary and center>fact['after']:
            return finish('attention','销量同时低于可比同星期历史水平与波动参考线，且达到本类型商品的绝对损失要求，建议核查。')
        return finish('normal','当前未超出可比同星期历史的业务关注边界；该标签不保证经营没有问题。')
    if changed_context:return finish('insufficient','主基线存在假期、调休或活动差异，可比同星期历史不足3天，暂不把回落当异常。')
    if loss<policy['min_loss']:
        return finish('normal',f'较基线减少{loss}件，未达到{tier}的绝对损失关注线；避免低基数百分比放大。')
    return finish('insufficient','可比同星期历史不足3天，只能确认单次变化，建议补充历史记录后判断是否偏离常态。')
