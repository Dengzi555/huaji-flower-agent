"""对话推广查询：复用页面账本，冻结门店/日期后执行只读工具，不借销量规划器代答。

这是独立业务能力的入口，不要求SKU。每次返回原始小时报告及调用回执，
失败门店/日期保留缺口，不补零、不回退到最近有数的一天，不进行广告效果归因。
"""
from datetime import date, timedelta
import re
from .natural_dates import normalize_dates
from .time_scope import resolve_scope, DATE_PATTERN, WINDOW_PATTERN
from .routing import route_question
from .tools import ADVERTISING_SCHEMA,ADVERTISING_SETTINGS_SCHEMA
from .cancellation import checkpoint

AD_PATTERN=r'推广|广告|投放|点击成本|CPC|ROAS|ROI|投产比'
ROUTES={'query_advertising':{'label':'推广账本与费用分析','required_table':'advertising_hourly_ledger','entrypoint':'conversation'}}
SCHEMA=ADVERTISING_SCHEMA


def date_followup(text,today):
    """仅纯日期追问继承推广意图；新商品、销量或闲聊不能被旧推广对话吞掉。"""
    normalized,_,_=normalize_dates(text,today)
    remainder=re.sub(DATE_PATTERN,'',normalized)
    remainder=re.sub(WINDOW_PATTERN,'',remainder)
    remainder=re.sub(r'昨天|前天|大前天|上周|上个月|上月|本月|这个月|[到至~～]|那|再|呢|看看|看下|查下|改成|换成|[，。？?\s]','',remainder)
    return not remainder and bool(text.strip())


class AdvertisingToolRegistry:
    """一次对话的最小工具注册表：参数由宿主冻结，调用方不能传路径或改门店。"""
    def __init__(self,book,day,*,high,low,mode,settings_service=None):
        self.book=book;self.day=day;self.high=high;self.low=low;self.mode=mode
        self.settings_service=settings_service
        self.schemas=[ADVERTISING_SETTINGS_SCHEMA] if settings_service else [SCHEMA]

    def execute(self,name,arguments):
        checkpoint()
        if arguments!={}:raise ValueError('推广工具参数超出授权范围。')
        if name=='get_advertising_settings' and self.settings_service:
            value=self.settings_service.settings()
            result={k:value[k] for k in ('revision','open_time','close_time','high_pct','low_pct')}
        elif name=='get_advertising_report' and self.book and not self.settings_service:
            result=self.book.report(self.day,high=self.high,low=self.low,mode=self.mode)
        else:raise ValueError('推广工具参数超出授权范围。')
        checkpoint()
        return result


def reply(manager,doc,payload,key,digest,resolution,question,on_delta=None):
    """命中推广能力或纯日期追问时返回持久化回复，否则返回None给现有主链路。"""
    last=doc['turns'][-1] if doc['turns'] else {}
    pending=doc.get('pending_advertising')
    # 动作先于槽位：建议/设置讨论不要求查数日期，不能套用历史报表的必填条件。
    from .advertising_advice import wants_advice,has_context,reply as advice_reply
    explicit_ad=bool(re.search(AD_PATTERN,question,re.I))
    context_advice=has_context(doc) and not re.search(r'销量|库存|缺货|订单|取消|促销|花语|养护|保鲜|怎么养|如何养',question)
    if context_advice and not explicit_ad:
        context_advice=not manager.active(doc['focus_store']).catalog.resolve(question,today=manager.today())['candidates']
    if wants_advice(question) and (explicit_ad or context_advice):
        return advice_reply(manager,doc,payload,key,digest,resolution,question,on_delta)
    followup=date_followup(question,manager.today()) and (pending or last.get('advertising',{}).get('status') in ('completed','needs_data','partial'))
    if not re.search(AD_PATTERN,question,re.I) and not followup:
        doc.pop('pending_advertising',None)
        return None
    from .task_parser import parse_dates
    ids=list(dict.fromkeys(resolution['reference_store_ids']+resolution['store_ids']))
    if pending and followup:
        # 待补日期仍绑定原查询门店；界面已切店或明确新店时不重放旧范围。
        switched=pending.get('focus_store')!=doc['focus_store'] or payload.get('store_id') and payload['store_id']!=doc['focus_store']
        ids=ids if switched else pending['store_ids']
    source_question=(pending['question']+' '+question) if pending and followup else question
    previous=last.get('advertising',{}) if followup else {}
    normalized,_,date_errors=normalize_dates(source_question,manager.today())
    scope=resolve_scope(normalized,manager.today(),parse_dates,payload.get('date'))
    mode='manual' if '手动' in source_question and '自动' not in source_question else 'automatic' if '自动' in source_question and '手动' not in source_question else previous.get('mode','all')
    receipt={'version':'advertising-query-v1','operation':'advertising_summary','status':'completed',
        'store_ids':ids,'start_date':scope['trend_start'] if scope['is_window'] else scope['target_date'],
        'end_date':scope['target_date'],'mode':mode,'reports':[],'missing':[],'tool_calls':[],
        'model_calls':0,'read_only':True,'sample_data':True,'store_resolution':resolution}
    unsupported=[]
    route=route_question(question)
    if route.get('intent')=='unsupported_action':unsupported.append('仅支持推广账本分析，不修改预算、出价或投放状态。')
    if re.search(r'ROI|ROAS|投产比|归因|利润|推广.*(?:带来|带动).*(?:成交|销量)|推广效果',question,re.I):
        unsupported.append('现有账本可以分析扣费、点击和费用率；没有广告归因成交数据，不能计算ROI/ROAS或证明投放效果。')
    if re.search(r'相比|对比|比较|同比|环比|比昨天|比前天|排行|排名|最高的店|最低的店',question):
        unsupported.append('当前推广对话支持单日或日期范围的账本分析；跨期变化及门店推广效率排名尚未接入，不能用销量排名代答。')
    for sid in ids:
        if manager.active(sid).catalog.resolve(question,today=manager.today())['candidates']:
            unsupported.append('当前推广账本是门店级数据，不能拆成某款花的推广费用；请查询门店整体推广。');break
    if re.search(r'库存|缺货|断货|促销|销量|取消|转化率',question):
        unsupported.append('这句话包含推广之外的独立查询。当前请分开查询，我不会只回答其中一部分。')
    if unsupported:
        receipt['status']='unsupported'
        return save_reply(manager,doc,payload['question'],'\n'.join(dict.fromkeys(unsupported)),receipt,key,digest,on_delta)
    errors=date_errors+scope['errors']
    if errors:
        doc['pending_advertising']={'question':question,'store_ids':ids,'focus_store':doc['focus_store']}
        doc.pop('pending_store',None);doc.pop('store_choices',None)
        result=manager.wait(doc,payload['question'],'请补充推广分析日期或时间段，例如：前天、近7天，或10月24日至25日。\n'+'\n'.join(errors),['date'],key,digest)
        return result
    doc.pop('pending_advertising',None)
    start=date.fromisoformat(receipt['start_date']);end=date.fromisoformat(receipt['end_date'])
    if len(ids)>8 or not 0<=(end-start).days<60:raise ValueError('推广查询范围超出限制。')
    for sid in ids:
        service=manager.active(sid);settings=service.operations.settings()
        book=getattr(service.operations,'book',None)
        for offset in range((end-start).days+1):
            checkpoint()
            day=(start+timedelta(days=offset)).isoformat()
            call={'name':'get_advertising_report','store_id':sid,'date':day,'mode':mode,'status':'ok'}
            receipt['tool_calls'].append(call)
            if not book or day not in book.dates:
                call['status']='missing';receipt['missing'].append({'store_id':sid,'date':day});continue
            # 明确给出的百分数只作为本次关注线；不修改门店设置。
            high=settings['high_pct']
            threshold=re.search(r'(?:超过|高于|超出|费用率|费比)\D{0,6}(\d+(?:\.\d+)?)\s*[%％]',question)
            if threshold:high=threshold[1]
            try:
                registry=AdvertisingToolRegistry(book,day,high=high,low=settings['low_pct'],mode=mode)
                report=registry.execute('get_advertising_report',{})
            except ValueError as exc:
                receipt['status']='unsupported';receipt['reports']=[]
                return save_reply(manager,doc,payload['question'],str(exc),receipt,key,digest,on_delta)
            receipt['reports'].append(report)
    if receipt['missing']:receipt['status']='partial' if receipt['reports'] else 'needs_data'
    text=summary(manager,receipt)
    return save_reply(manager,doc,payload['question'],text,receipt,key,digest,on_delta)


def money(cents):return f'¥{cents/100:,.2f}'


def summary(manager,receipt):
    """所有数字由同一份冻结工具结果计算；无成交分母为未知，不能写成0%。"""
    names={s['id']:s['name'] for s in manager.shops()}
    mode={'all':'手动＋自动','manual':'手动','automatic':'自动'}[receipt['mode']]
    lines=[f'推广账本分析｜{receipt["start_date"]}'+(f' 至 {receipt["end_date"]}' if receipt['start_date']!=receipt['end_date'] else '')+f'｜{mode}']
    for sid in receipt['store_ids']:
        reports=[r for r in receipt['reports'] if r['store_id']==sid]
        if not reports:continue
        spend=sum(r['totals']['spend_cents'] for r in reports);revenue=sum(r['totals']['revenue_cents'] for r in reports);clicks=sum(r['totals']['clicks'] for r in reports)
        ratio=f'{spend/revenue*100:.2f}%' if revenue else '无法计算（营业额为0）'
        cpc=money(spend/clicks) if clicks else '无法计算（无广告点击）'
        qualifier='已有数据部分合计' if any(m['store_id']==sid for m in receipt['missing']) else '合计'
        lines.append(f'{names[sid]}：{qualifier}扣费 {money(spend)}，店铺营业额 {money(revenue)}，费用率 {ratio}；广告点击 {clicks} 次，平均点击成本 {cpc}。')
        for kind in ('manual','automatic'):
            if kind in reports[0]['by_mode']:
                lines.append(f'{"手动" if kind=="manual" else "自动"}推广扣费 {money(sum(r["by_mode"][kind]["spend_cents"] for r in reports))}。')
        high=[(r,h) for r in reports for h in r['hours'] if h['flag']=='high']
        zero=[(r,h) for r in reports for h in r['hours'] if h['flag']=='no_revenue']
        low=[(r,h) for r in reports for h in r['hours'] if h['flag']=='low']
        lines.append(f'高费用时段 {len(high)} 个；有扣费但同期无成交时段 {len(zero)} 个；低于低投入关注线时段 {len(low)} 个。')
        for r,h in sorted(high+zero,key=lambda pair:pair[1]['spend_cents'],reverse=True)[:3]:
            ratio=f'{h["spend_ratio_pct"]}%' if h['spend_ratio_pct'] is not None else '无法计算（同期无成交）'
            lines.append(f'优先核对 {r["date"]} {h["label"]}：扣费 {money(h["spend_cents"])}，营业额 {money(h["revenue_cents"])}，费用率 {ratio}。')
        lines.append('建议先核对高扣费时段的点击成本、投放状态及成交延迟；低投入时段仅供观察，不能直接据此加预算。')
    for item in receipt['missing']:
        lines.append(f'{names[item["store_id"]]} {item["date"]}：缺少推广账本，不计为零，不替换查询日期。')
    lines.append('费用率＝所选推广扣费÷店铺营业额，不是广告ROAS或利润；小时成交可能滞后于点击。来源为学习用合成小时账本，原始小时数据与账本版本保存在本轮证据中。')
    return '\n'.join(lines)


def save_reply(manager,doc,question,message,receipt,key,digest,on_delta):
    """业务结果单独保存为带工具回执的对话轮；不写入普通聊天记忆或伪造SKU任务。"""
    sid=receipt['store_ids'][0]
    entry={'kind':'chat','question':question,'message':message,'store_id':sid,'task_id':None,
        'scope':{'intent':'query_advertising','date':receipt['end_date'],
            'product_scope':{'label':'门店整体（无需SKU）'},
            'time':{'is_window':receipt['start_date']!=receipt['end_date'],'trend_start':receipt['start_date'],'trend_end':receipt['end_date']}},'inherited':[],
        'chat':{'mode':'advertising','model_calls':receipt['model_calls']},'advertising':receipt}
    doc.update(focus_store=sid,query_store_ids=receipt['store_ids'],waiting=[],notice='',revision=doc['revision']+1)
    doc.pop('active_comparison',None)
    doc.pop('pending_store',None);doc.pop('store_choices',None);doc.pop('pending_product_patch',None)
    doc['turns'].append(entry);manager.record(doc,key,digest,entry);manager.save(doc)
    if on_delta:
        for line in message.splitlines():checkpoint();on_delta(line+'\n')
    return {'conversation':manager.public(doc),'task':None,'reused':False}
