"""经营决策辅助：只把已观察事实组织为排查顺序，不生成未经验证的根因或经营指令。"""

DOMAIN_LABELS={'sales':'销量','traffic':'访客','inventory':'库存监测','promotions':'活动','order_cohorts':'支付订单批次'}
SIGNAL_CHECKS={
    'stockout_signal':('先核对缺货与少卖是否发生在同一时段','小时级可售库存、支付订单及营业时间','缺货增加只是伴随线索；若少卖发生在缺货前，需要另查原因。'),
    'traffic_signal':('先定位减少的流量来源','同口径自然搜索、推荐和付费渠道访客','先确认渠道变化，再查曝光、点击及成交；不能直接据此调整预算。'),
    'promotion_ended_signal':('换用活动前的同星期日期比较','活动起止、价格及活动前同星期销量','若回到活动前正常水平，可能是峰值回落，而非持续经营恶化。'),
}


def build_guidance(analysis,observations,task=None):
    """输入宿主分析、实际工具观察和冻结任务，输出四段结构化回答；每个事实保留来源ID。"""
    task=task or {};observed=[];signals=[];unknown=[];actions=[]
    valid_refs={o['evidence_id'] for o in observations.values() if o.get('ok')}

    def entry(text,refs=()):
        return {'text':text,'evidence_ids':[ref for ref in refs if ref in valid_refs]}

    def action(key,title,why,required,refs=(),available=False):
        # 建议是可检查的任务，不是已经执行的动作；缺少小时/平台源时公开标注待补数据。
        if any(item['id']==key for item in actions):return
        actions.append({'id':key,'priority':len(actions)+1,'title':title,'reason':why,'required_data':required,
            'availability':'available_evidence' if available else 'needs_additional_data',**entry(title,refs)})

    facts=analysis.get('facts',{})
    for key,label,unit in [('units_sold','成品支付件数','束/桶'),('visitors','商品访客','人')]:
        fact=facts.get(key)
        if fact:
            rate='基线为0，不计算增长率' if fact.get('relative_pct') is None else f"变化{fact['relative_pct']:+}%"
            observed.append(entry(f"{label}：{fact['before']} → {fact['after']}{unit}，增减{fact['absolute']:+}，{rate}。",fact.get('evidence_ids',[])))
    conversion=facts.get('payment_conversion_pct')
    if conversion:observed.append(entry(f"支付买家转化率：{conversion['before']}% → {conversion['after']}%，变化{conversion['percentage_point_change']:+}个百分点。",conversion.get('evidence_ids',[])))
    if analysis.get('kind')=='lookup':
        for metric in analysis.get('display_metrics',[]):
            if metric.get('value') in (None,'—'):continue
            observed.append(entry(f"{metric['label']}：{metric['value']}{metric.get('unit','')}。",analysis.get('evidence_ids',[])))
        selected=analysis.get('selected_comparison',{})
        for row in selected.get('rows',[])[:3]:
            if row.get('absolute_change') is not None:
                observed.append(entry(f"{row['name']}：{row['date']} {row['baseline_units']} → {row['target_date']} {row['target_units']}束/桶，增减{row['absolute_change']:+}。",analysis.get('evidence_ids',[])))
        if analysis.get('data_quality')=='partial':
            unknown.append(entry('本轮数据覆盖不完整，展示的可用记录不能代表完整期间或全部商品。',analysis.get('evidence_ids',[])))
            action('complete_lookup','先补齐当前查询范围的数据','当前可用部分不能替代完整范围。','所选商品与日期的缺失来源记录',analysis.get('evidence_ids',[]))
        if analysis.get('ranking',{}).get('coverage_note'):
            unknown.append(entry(analysis['ranking']['coverage_note'],analysis.get('evidence_ids',[])))
    conflicts=analysis.get('conflicts',[])
    if conflicts:
        descriptions={'full_day_stockout_with_sales':'整日缺货记录与成交记录冲突','buyers_exceed_visitors':'支付买家超过同口径访客','paid_cohort_sales_mismatch':'订单批次与销售表支付订单数不一致'}
        for conflict in conflicts:
            text=descriptions.get(conflict['kind'],'来源记录存在口径冲突')
            unknown.append(entry(text+'；当前不据此判断经营原因。',conflict.get('evidence_ids',[])))
        action('reconcile_sources','先核对冲突记录和统计口径','来源冲突会使转化率或归因失真，排查原因前先确认数据。',
            '同一门店/SKU/日期的原始报表、归因时点和监测范围',[r for c in conflicts for r in c.get('evidence_ids',[])])
    for gap in analysis.get('gaps',[]):
        label=DOMAIN_LABELS.get(gap.get('table'),gap.get('table','数据'))
        text=label+'缺失、延迟或覆盖不足；不能按零处理。'
        refs=[gap['evidence_id']] if gap.get('evidence_id') else []
        unknown.append(entry(text,refs))
        action('complete_'+gap.get('table','source'),'补齐'+label+'的可比记录',text,'目标日及选定基线的'+label+'报表和更新时间',refs)
    if not conflicts:
        for hypothesis in analysis.get('hypotheses',[]):
            refs=hypothesis.get('evidence_ids',[])
            # 未实际观察的引用不作为“有证据的线索”发布。
            if not refs or not set(refs)<=valid_refs:continue
            signals.append(entry(hypothesis['text'],refs))
            check=SIGNAL_CHECKS.get(hypothesis['id'])
            if check:
                title,required,why=check
                if hypothesis['id']=='traffic_signal' and observations.get('traffic_channels',{}).get('ok'):
                    title='对齐已查渠道的小时级流量与成交';required='渠道小时级曝光、点击、访客和支付订单'
                action(hypothesis['id'],title,why,required,refs)
        cohort=analysis.get('order_funnel',{});changes=cohort.get('changes')
        if changes and changes.get('cancellation_pp') is not None:
            refs=[cohort.get('evidence_id')];pp=changes['cancellation_pp']
            observed.append(entry(f"同支付日批次取消率变化{pp:+}个百分点；批次观察截止时间见订单表。",refs))
            if pp>0 and changes['cancelled_orders']['absolute']>0:
                signals.append(entry('取消率和取消单数同时增加，可能影响完成订单；尚不能解释支付件数变化。',refs))
                action('check_cancellations','复核增加的取消订单及原因标签','先对齐批次观察时长，再核查等待、缺货等标签对应订单。','同批订单明细、取消时点、原因标签、观察截止时间',refs)
    trend=observations.get('sales_trend',{}).get('data')
    if not trend:
        trend=observations.get('product_group_sales',{}).get('data',{}).get('trend')
    if trend:
        primary=next((row for row in trend['comparisons'] if row['is_primary']),None)
        if primary and primary.get('context_flags'):
            unknown.append(entry('对比背景：'+'、'.join(primary['context_flags'])+'；不能仅凭一次变化判断原因。'))
        if primary and primary['status']!='ok':
            action('complete_baseline','补齐选定历史基线','基线缺失时不能计算可靠的跨日变化。','所选商品'+primary['date']+'的销售记录')
    if analysis.get('kind')!='lookup':
        unknown.append(entry('当前未建立因果根因；配送范围、预计送达、差评、竞争等因素仍缺少对应证据。'))
        if not actions:
            assessment=analysis.get('assessment',{})
            if assessment.get('status')=='normal_fluctuation' or assessment.get('label')=='正常波动':
                action('observe_next_period','先观察下一可比经营日','当前证据不足以支持立即改变经营策略。','下一同星期日期的销量及活动、库存背景')
            else:
                action('check_comparable_history','先核查同星期历史与商品生命周期','没有候选信号不等于没有问题，先确认回落是否超出正常波动。','已取回的多日销量、日历与商品上架信息',available=bool(trend))
    elif not actions:
        ranking=analysis.get('ranking')
        if ranking and not ranking.get('complete_scope'):
            action('complete_ranking','补齐未参与排名商品的数据','当前排名只覆盖完整记录，不能认定全店最好或最差。','缺失商品在所选日期/窗口的销售报表')
        else:
            text='销量排名不能直接解释利润、投放效率或变化原因。' if ranking else {
                'query_inventory':'缺货分钟来自已监测时段，不能据此回答实时库存件数或实际少卖数量。',
                'query_promotions':'活动开关记录不能证明优惠力度或活动带来的销量。',
            }.get(task.get('intent'),'这是表现查询，不能仅凭销售记录解释经营变化原因。')
            unknown.append(entry(text))
    return {'version':'operating-guidance-v1','status':'data_conflict' if conflicts else 'evidence_gap' if analysis.get('gaps') or analysis.get('data_quality')=='partial' else 'observed_only',
        'observed':observed,'supported_signals':signals,'uncertainties':unknown,'next_checks':actions,
        'causal_status':'not_established'}


def guidance_markdown(guidance):
    """报告与网页消费同一结构，避免两处分别编写经营结论。"""
    lines=[]
    for key,title in [('observed','发生了什么'),('supported_signals','证据支持什么'),('uncertainties','还不能确定什么')]:
        lines+=['','## '+title]
        lines += ['- '+item['text']+('（证据 '+', '.join(item['evidence_ids'])+'）' if item['evidence_ids'] else '') for item in guidance[key]] or ['- 暂无充分的已观察证据。']
    lines+=['','## 建议先查什么']
    for item in guidance['next_checks']:
        lines.append(f"{item['priority']}. {item['title']}：{item['reason']} 所需数据：{item['required_data']}。")
    if not guidance['next_checks']:lines.append('- 当前请求仅查询表现，未据此建议经营调整。')
    return lines
