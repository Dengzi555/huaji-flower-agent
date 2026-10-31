"""门店解析与对话的连接层：组装最小上下文、保存待确认问题、验证确认，不查业务数据。"""
import re
from .store_resolution import StoreDirectory,resolve_stores,_result,normalize


def context_for(doc,comparison=None):
    """只提供门店身份与版本；从旧对话迁移时不复制历史指标或推测权限。"""
    previous=doc.get('query_store_ids')
    if previous is None:
        last=next((t for t in reversed(doc['turns']) if t.get('task_id') or t.get('comparison')),None)
        previous=[c['store_id'] for c in last['comparison']['children']] if last and last.get('comparison') else [last['store_id']] if last else [doc['focus_store']]
    return {'focus_store':doc['focus_store'],'query_store_ids':previous,'revision':doc['revision'],
        'recent_store_ids':list(doc['stores']),
        'comparison_store_ids':[c['store_id'] for c in comparison['children']] if comparison else [],
        'anchor_store':comparison['anchor_store'] if comparison else doc['focus_store']}


def business_text(question,shops):
    """门店实体不能污染商品/日期/意图解析；只改解析副本，原话与识别依据单独持久化。"""
    directory=StoreDirectory(shops);text=normalize(question)
    for mention in reversed(directory.mentions(text)):
        text=text[:mention['start']]+' '+text[mention['end']:]
    return text.strip() or '查看情况'


def resolve_turn(manager,doc,payload,comparison):
    """返回(解析、原查询、条件参数)。仅明确确认时重放原查询，其他输入作为新问题。"""
    directory=StoreDirectory(manager.shops());context=context_for(doc,comparison)
    context['default_query_scope']=manager.default_query_scope
    question=payload['question'];pending=doc.get('pending_store');choice=payload.get('store_choice')
    response=normalize(question).strip('。?？')
    if pending and choice is None:
        candidates=pending['candidate_ids']
        ordinal=re.fullmatch(r'(?:第)?([一二三四五六七八九十1-9])(?:家|个|项)(?:店)?(?:吧|就行)?',response)
        if ordinal:
            n=int(ordinal[1]) if ordinal[1].isdigit() else '一二三四五六七八九十'.index(ordinal[1])+1
            if 0<n<=len(candidates):choice=candidates[n-1]
        else:
            mentions=directory.mentions(response)
            ids={sid for m in mentions for sid in m['candidate_ids']}
            rest=response
            for mention in reversed(mentions):rest=rest[:mention['start']]+' '+rest[mention['end']:]
            # “就南岸店吧”是确认；“南岸店前天查33乌梅”是新查询，不重放旧问题。
            if len(ids)==1 and next(iter(ids)) in candidates and re.fullmatch(r'(?:就|选|选择|确认|是|用|查|看|这家|那家|那个|这个|吧|就行|没错|对|[，,\s])*',rest):choice=next(iter(ids))
    if choice is not None:
        if not pending or choice not in pending['candidate_ids']:raise ValueError('所选门店不在当前待确认候选中。')
        if pending['catalog_version']!=directory.version:raise ValueError('门店目录已更新，请重新提交原问题。')
        # 再检查当前可用状态；目录中的inactive店不能靠旧确认回执重新激活。
        if directory.stores[choice]['status']!='active':raise ValueError('所选门店已停用。')
        ids=list(dict.fromkeys(pending.get('fixed_store_ids',[])+[choice]))
        action='compare' if len(ids)>1 and pending.get('compare_requested') else 'switch' if pending.get('switch_requested') else 'query'
        if len(ids)>1 and action!='compare':
            ids=[choice]
        result=_result(directory,context,store_ids=ids,action=action,source='user_confirmed',
            evidence=[{'text':question,'role':'confirmation','candidate_ids':[choice]}])
        result['confirmed_from']=pending['question']
        result['business_today']=pending['business_today']
        return result,pending['question'],{**pending['conditions'],**{k:v for k,v in payload.items() if k in ('sku','date','intent') and v}}
    settings=getattr(manager.active(doc['focus_store']),'model_settings',None)
    planner=manager.store_planner
    if planner is None and settings:
        status=settings.public()
        if status.get('enabled') and status.get('configured'):planner=getattr(settings,'resolve_stores',None)
    result=resolve_stores(question,directory,context,planner=planner,selected_store=payload.get('store_id'))
    result['business_today']=manager.today().isoformat()
    return result,question,{k:payload[k] for k in ('sku','date','intent') if payload.get(k)}


def ask_store(manager,doc,payload,key,digest,resolution):
    """门店不确定时只记录澄清；保存原话/用户槽位，防止确认后要求重新输入整句。"""
    candidates=resolution['candidate_ids']
    failure=resolution['status'] in ('failed','forbidden')
    if not failure:
        fixed=list(dict.fromkeys(sid for e in resolution['evidence'] if e['role']=='mentioned' and len(e['candidate_ids'])==1 for sid in e['candidate_ids']))
        doc['pending_store']={'question':payload['question'],'candidate_ids':candidates,
            'business_today':resolution['business_today'],
            'catalog_version':resolution['catalog_version'],
            'conditions':{k:payload[k] for k in ('sku','date','intent') if payload.get(k)},
            'fixed_store_ids':fixed,'compare_requested':bool(re.search(r'对比|比较|分别|各自',payload['question'])),
            'switch_requested':bool(re.search(r'切到|切回|回到|返回|切换',payload['question']))}
    else:doc.pop('pending_store',None)
    previous_comparison=doc.get('active_comparison')
    result=manager.wait(doc,payload['question'],resolution['message'],[] if failure else ['store'],key,digest,choices=candidates)
    if previous_comparison:doc['active_comparison']=previous_comparison
    turn=doc['turns'][-1];turn['store_resolution']=resolution
    if resolution.get('error_info'):turn['chat']={'error_info':resolution['error_info']}
    manager.save(doc);result['conversation']=manager.public(doc)
    return result
