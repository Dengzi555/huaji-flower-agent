"""推广建议：复用同店已核验账本，读取本地设置，模型只选择白名单建议类型。

建议不要求新日期，也不授权改预算。数值与时段由宿主从冻结证据生成；
模型不返回预算金额、SQL或事实结论。模型不可用时仍能输出有依据的基础建议。
"""
import copy
import re
from .cancellation import checkpoint, CancelledError

ADVICE_PATTERN=r'建议|设置|怎么|如何|优化|调整|要不要|怎么办|应该|改进|合适|意见|策略'
ACTIONS={
    'review_cost_hours':'先核对高费用时段的投放方式与点击成本；若多个可比日期持续低效，再人工评估是否降低这些时段的出价或预算，避免仅凭一天的费用率直接停投。',
    'compare_modes':'手动与自动推广分开观察扣费、点击和CPC；现有数据没有区分两种模式带来的成交，不能仅凭花费较多就认定某种模式更差，也不建议直接套用固定分配比例。',
    'review_night_schedule':'夜间投放应结合营业和接单能力：全天营业店可考虑低价保留曝光；非全天店按实际接单时间安排投放。关闭广告不代表自然流量或订单必须为零。',
    'check_conversion_delay':'有扣费而同期无成交的小时先检查支付延迟及后续成交，再判断是否持续低效；不要把单个小时的零成交直接认定为广告无效。',
    'collect_attribution':'具体预算和出价还需要毛利、可承受获客成本、归因成交与取消情况。先补充这些依据，观察多个可比日期，逐项人工调整，避免承诺提高预算一定带来订单。',
}


def wants_advice(question):
    """识别问设置/讨论方案的动作；明确执行修改仍留给写动作边界拒绝。"""
    if re.search(r'(?:把|将).{0,25}(?:调成|改成|设为|修改为)|(?:直接|立即|执行|确认修改).{0,16}(?:调|改|设置)|(?:调成|改成|设为)\s*\d',question):return False
    return bool(re.search(ADVICE_PATTERN,question))


def prior_evidence(doc,ids):
    """只在同一业务话题里找同店证据；新店、新调查不能偷偷沿用其他店的数值。"""
    wanted=set(ids)
    for index in range(len(doc['turns'])-1,-1,-1):
        turn=doc['turns'][index]
        if turn.get('task_id') or turn.get('comparison'):break
        receipt=turn.get('advertising')
        if receipt and receipt.get('status') in ('completed','partial'):
            reports=[r for r in receipt.get('reports',[]) if r['store_id'] in wanted]
            if reports:return index,receipt,reports
            if receipt.get('reports'):break
    return None,None,[]


def has_context(doc):
    """允许省略“推广”的建议追问；旧失败的日期提示不应切断已确认话题。"""
    if doc.get('pending_advertising'):return True
    for turn in reversed(doc['turns']):
        if turn.get('task_id') or turn.get('comparison'):return False
        if turn.get('advertising'):return True
    return False


def reply(manager,doc,payload,key,digest,resolution,question,on_delta):
    """生成建议回执；设置来源是本项目本地配置，不冒充平台实时推广配置。"""
    from .advertising_query import save_reply,money,AdvertisingToolRegistry
    ids=list(dict.fromkeys(resolution['reference_store_ids']+resolution['store_ids']))
    index,previous,reports=prior_evidence(doc,ids)
    snapshots=[];calls=[]
    for sid in ids:
        checkpoint()
        service=manager.active(sid)
        registry=AdvertisingToolRegistry(None,None,high=None,low=None,mode='all',settings_service=service.operations)
        value=registry.execute('get_advertising_settings',{})
        snapshots.append({'store_id':sid,'source':'local_operating_settings',
            **{k:value[k] for k in ('revision','open_time','close_time','high_pct','low_pct')}})
        calls.append({'name':'get_advertising_settings','store_id':sid,'status':'ok','source':'local_operating_settings'})
    receipt={'version':'advertising-query-v1','operation':'advertising_advice','status':'completed',
        'store_ids':ids,'start_date':min((r['date'] for r in reports),default=None),
        'end_date':max((r['date'] for r in reports),default=None),'mode':previous.get('mode','all') if previous else 'all',
        'reports':copy.deepcopy(reports),'missing':[],'tool_calls':calls,'settings_snapshot':snapshots,
        'model_calls':0,'read_only':True,'sample_data':True,'store_resolution':resolution,
        'based_on':{'conversation_id':doc['id'],'turn':index+1} if index is not None else None,
        'advice_planning':{'status':'rules','model_calls':0}}
    choices=list(ACTIONS)[:4]
    context={'actions':ACTIONS,'settings':snapshots,'evidence':[
        {'store_id':r['store_id'],'date':r['date'],'totals':r['totals'],'by_mode':r['by_mode'],'attention':r['attention']}
        for r in reports[-16:]],'evidence_complete':len(reports)<=16,
        'policy':'只建议，不修改设置。只从actions选择；不生成数值、预算或事实。没有账本不推断门店经营表现。'}
    adviser=getattr(manager,'advertising_adviser',None)
    settings=getattr(manager.active(ids[0]),'model_settings',None)
    status=settings.public() if settings else {}
    if adviser or status.get('enabled') and status.get('configured'):
        receipt['model_calls']=1
        try:
            checkpoint()
            proposal=(adviser or settings.plan_advertising_advice)(question,context)
            checkpoint()
            if not isinstance(proposal,dict) or set(proposal)-{'actions','model','usage'}:raise ValueError('建议计划字段无效。')
            selected=proposal.get('actions')
            if not isinstance(selected,list) or not 1<=len(selected)<=4 or any(not isinstance(a,str) or a not in ACTIONS for a in selected) or len(set(selected))!=len(selected):raise ValueError('建议超出白名单。')
            choices=selected
            receipt['advice_planning']={'status':'validated','model_calls':1,**{k:proposal[k] for k in ('model','usage') if k in proposal}}
        except CancelledError:raise
        except Exception as exc:
            from .errors import from_exception
            receipt['advice_planning']={'status':'fallback','model_calls':1,'error_info':from_exception(exc,phase='planning',fallback='model_response_invalid').public()}
    receipt['advice_actions']=choices
    names={s['id']:s['name'] for s in manager.shops()}
    lines=['推广设置建议｜仅提供方案，未修改预算、出价或投放状态。']
    for sid in ids:
        own=[r for r in reports if r['store_id']==sid]
        lines.append(names[sid]+'：')
        if own:
            spend=sum(r['totals']['spend_cents'] for r in own);revenue=sum(r['totals']['revenue_cents'] for r in own)
            ratio=f'{spend/revenue*100:.2f}%' if revenue else '无法计算（同期营业额为0）'
            lines.append(f'沿用上一轮 {min(r["date"] for r in own)} 至 {max(r["date"] for r in own)} 的账本证据：扣费 {money(spend)}，费用率 {ratio}。这不是当前实时数据。')
            attention=sorted([(r,h) for r in own for h in r['hours'] if h['flag'] in ('high','no_revenue')],key=lambda item:item[1]['spend_cents'],reverse=True)
            if attention:
                r,h=attention[0];lines.append(f'优先核查 {r["date"]} {h["label"]}：扣费 {money(h["spend_cents"])}，店铺营业额 {money(h["revenue_cents"])}。此处用于定位检查顺序，不能证明该时段应停投。')
        else:lines.append('当前没有可沿用的同店推广账本，以下是通用配置建议，不代表已判断本店投放表现；不需要为了获取建议重新补日期。')
        setting=next(s for s in snapshots if s['store_id']==sid)
        lines.append(f'本地配置的费用关注线为高于 {setting["high_pct"]}%、低于 {setting["low_pct"]}%；这不是平台预算或出价设置，也不是盈利标准。')
    for i,action in enumerate(choices,1):lines.append(f'{i}. {ACTIONS[action]}')
    lines.append('当前系统不能读取平台实时推广配置或直接修改投放。费用率不是ROAS/利润；已有账本为学习用合成数据。')
    if receipt['advice_planning']['status']=='fallback':lines.append('本轮模型建议规划未成功，以上保留基于现有证据的基础建议。')
    doc.pop('pending_advertising',None)
    return save_reply(manager,doc,payload['question'],'\n'.join(lines),receipt,key,digest,on_delta)
