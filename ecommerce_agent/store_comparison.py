"""跨店比较：相同SKU、日期和基线分别取证，缺失/失败不补零，不让模型编造排名。"""
import hashlib
import uuid
from datetime import date
from .routing import ROUTES,route_question
from .task_parser import parse_dates,parse_task
from .time_scope import has_window
from .query_plan import ranking,global_scope
from .query_context import QueryContext,snapshot_scope,changes_baseline


def start_comparison(manager,doc,question,payload,key,digest,stores,*,store_resolution=None,business_question=None):
    """准入后每店单独排队；最多8店、每店独立幂等键与数据快照，不复制任务结果。"""
    active=next((t['comparison'] for t in doc['turns'] if t.get('comparison',{}).get('id')==doc.get('active_comparison') and t.get('comparison')),None)
    base=active['scope'] if active else doc['stores'].get(doc['focus_store'],{}).get('scope',{})
    business_question=business_question or question
    decision=route_question(business_question)
    if decision['status']=='unsupported':return manager.wait(doc,question,decision['reason'],['intent'],key,digest)
    store_rank=None
    if store_resolution and store_resolution['action']=='rank':
        from .store_ranking import compile_request
        from .capabilities import question_boundaries
        boundaries=question_boundaries(business_question,True)
        if boundaries:return manager.wait(doc,question,'；'.join(b['message'] for b in boundaries),[],key,digest)
        try:business_question,store_rank=compile_request(business_question)
        except ValueError as exc:return manager.wait(doc,question,str(exc),[],key,digest)
        decision=route_question(business_question)
    if decision['status']=='needs_clarification' and len(decision['matched_rules'])>1:
        return manager.wait(doc,question,'请先确定一种比较事项，例如销量或缺货，不能同时混合不同指标。',['intent'],key,digest)
    targets=list(dict.fromkeys(stores))
    # 门店解析只是建议范围，批量创建前再次验证整个集合，禁止执行一半才发现越界。
    for sid in targets:manager.active(sid)
    if len(targets)<2 and not store_rank:return manager.wait(doc,question,'跨店比较至少需要两家门店。',['store'],key,digest)
    if len(targets)>8:return manager.wait(doc,question,'一次最多比较8家门店，请明确本次店名。',['store'],key,digest,choices=targets)
    anchor=doc['focus_store'] if doc['focus_store'] in targets else targets[0]
    business_today=date.fromisoformat(store_resolution['business_today']) if store_resolution and store_resolution.get('business_today') else manager.today()
    days,_=parse_dates(business_question,business_today)
    service=manager.active(anchor);identity=service.catalog.resolve(business_question,today=business_today)
    # 未命中的业务表达统一进入一次结构化规划；共享计划只生成一次，子店不重复调模型。
    planner=getattr(manager,'query_planner',None)
    settings=getattr(service,'model_settings',None)
    if planner is None and settings:
        configured=settings.public()
        if configured.get('enabled') and configured.get('configured'):planner=getattr(settings,'plan_sales_query',None)
    fresh_product_scope=bool(global_scope(business_question) or identity['candidates'])
    baseline_patch=changes_baseline(business_question,base)
    inherited_time=bool(base.get('date') and not payload.get('date') and (baseline_patch or not days and not has_window(business_question)))
    update=QueryContext.followup(business_question,base,identity,
        inherit_product=base.get('sku')=='__all_matched__' and not fresh_product_scope,
        inherit_time=inherited_time,baseline_patch=baseline_patch)
    # 首句已给齐条件或本次改了条件时，只解析共享范围，不先创建“参照店任务”。
    # 解析与各子任务仍走同一准入；真正商品歧义时停止，不能批量猜款。
    if not all(base.get(k) for k in ('sku','date','intent')) or payload.get('sku') or payload.get('date') or fresh_product_scope or ranking(business_question) or baseline_patch or redefines_scope(business_question,days) or has_window(business_question):
        parsed=parse_task(business_question,service.catalog,service.store,today=business_today,
            sku_override=payload.get('sku') or (base.get('sku') if not fresh_product_scope and not identity['issues'] and not identity.get('suggested_correction') else None),
            date_override=payload.get('date') or (base.get('date') if inherited_time else None),
            intent_override=payload.get('intent') or (decision['intent'] if decision['status']=='selected' else base.get('intent')),
            query_planner=planner,default_product_scope='all_if_omitted',query_context=update)
        if parsed['status'] not in ('ready','needs_data'):
            if parsed.get('error_info') and parsed['status']=='failed':
                response=manager.wait(doc,question,parsed['error_info']['message'],[],key,digest)
                doc['turns'][-1]['chat']={'error_info':parsed['error_info']};manager.save(doc)
                response['conversation']=manager.public(doc);return response
            return manager.wait(doc,question,'；'.join(parsed['issues']) or '请补充本次商品范围、日期或查询事项。',
                [k for k,v in (('sku',parsed.get('sku_id')),('date',parsed.get('target_date')),('intent',parsed.get('intent') if parsed.get('intent') in ROUTES else None)) if not v] or ['sku'],key,digest)
        base=snapshot_scope(parsed,confirmed_sku=parsed['sku_id'])
    scope={**base,'intent':decision['intent'] if decision['status']=='selected' else base['intent']}
    if payload.get('intent'):
        if payload['intent'] not in ROUTES:raise ValueError('比较事项不受支持。')
        scope['intent']=payload['intent']
    job={'id':uuid.uuid4().hex,'scope':scope,'anchor_store':anchor,'children':[],'question':question,'store_resolution':store_resolution}
    if store_rank:job['store_ranking']=store_rank
    context=QueryContext.replay(scope)
    for sid in targets:
        service=manager.active(sid)
        # 同一筛选/排序口径交给各店自己的目录重新准入，不把参照店SKU成员复制到别店。
        product=scope.get('product_scope')
        meta={'conversation_id':doc['id'],'turn':len(doc['turns'])+1,'store_id':sid,'comparison':True,
            'resolved_question':business_question,'query_context':context.document(),
            'inherited':['sku','date','intent'],'previous':{'store_id':doc['focus_store']},'comparison_id':job['id'],'store_resolution':store_resolution}
        token='cmp-'+hashlib.sha256((doc['id']+key+sid).encode()).hexdigest()
        try:
            body={'question':question,'date':scope['date'],'intent':scope['intent']}
            if not product:body['sku']=scope['sku']
            result=service.create(body,token,_conversation=meta,_today=business_today)
            job['children'].append({'store_id':sid,'task_id':result['task']['id']})
        except ValueError:
            job['children'].append({'store_id':sid,'task_id':None,'error':'该店任务未通过准入或当前队列不可用。'})
    entry={'kind':'comparison','question':question,'store_id':anchor,'scope':scope,'task_id':None,'comparison':job,'inherited':['sku','date','intent']}
    doc['turns'].append(entry);doc.update(focus_store=anchor,revision=doc['revision']+1,active_comparison=job['id'],waiting=[],notice='跨店比较已逐店排队；查询条件统一，指标和证据各自独立。')
    doc['query_store_ids']=targets
    doc.pop('pending_product_patch',None);manager.record(doc,key,digest,entry);manager.save(doc)
    return {'conversation':manager.public(doc),'task':None,'reused':False}


def redefines_scope(question,days):
    """当前比较入口仅承接已确认条件，避免在批量请求里猜测局部修改。"""
    import re
    return bool(days or re.search(r'\d+(?:朵|枝)|换成|白色|红色|粉色|紫色|上周|近\d+天',question))


def comparison_view(manager,job):
    """从各店冻结任务结果生成对比，不调用模型，不跨店复用证据ID。"""
    view={**job,'rows':[]};names={s['id']:s['name'] for s in manager.shops()}
    for child in job['children']:
        row={**child,'store_name':names[child['store_id']],'status':'unavailable','comparable':False,'metrics':{},'evidence_ids':[]}
        task=manager.active(child['store_id']).get(child['task_id']) if child.get('task_id') else None
        if task:
            row['status']=task['status'];row['fingerprint']=task['parsed']['dataset_fingerprint']
            state=task.get('result') or {};analysis=state.get('analysis') or {}
            row['evidence_ids']=analysis.get('evidence_ids') or []
            if task['parsed']['intent']=='query_combined':
                row['sections']=analysis.get('sections',[])
                row['business_answer']=analysis.get('business_answer')
                row['comparable']=task['status']=='completed'
                row['note']=analysis.get('overview') or ('等待各事项查询。' if task['status'] in ('queued','running') else '未生成完整结果，已取得部分结果可查看本店证据。')
                view['rows'].append(row)
                continue
            if not row['evidence_ids']:
                row['evidence_ids']=[e['payload']['result']['evidence_id'] for e in task.get('events',[]) if e['type']=='tool_completed' and e.get('payload',{}).get('result',{}).get('ok')]
            if task['status']=='completed':
                scope=job['scope'];intent=scope['intent'];records=analysis.get('records',[])
                target=next((r for r in records if r.get('date')==scope['date']),None)
                if job.get('store_ranking'):
                    trend=state.get('sales_trend') or {}
                    day=next((r for r in trend.get('records',[]) if r['date']==scope['date']),None)
                    if scope.get('time',{}).get('is_window'):
                        summary=trend.get('summary',{});value=summary.get('units_sum')
                        complete=summary.get('complete_window') is True
                    elif day:value=day.get('units_sold');complete=day.get('status')=='ok'
                    else:
                        value=target.get('units_sold') if target else None;complete=bool(target and target.get('status')=='ok')
                    row['metrics']={'units':value};row['comparable']=complete and value is not None and analysis.get('data_quality')!='conflicting'
                elif scope.get('product_scope',{}).get('ranking'):
                    rank=analysis.get('ranking') or {}
                    row['ranking']={k:rank.get(k) for k in ('leaders','rows','partitions','eligible_count','candidate_count','inactive_skus','group_by','complete_scope','coverage_note','period_scope','effective_dates','requested_dates')}
                    row['comparable']=bool(rank.get('leaders')) and rank.get('complete_scope') is True
                    if not row['comparable']:row['note']='排名只覆盖可比记录，或本店无可用销售记录；不能认定全范围冠军。'
                elif intent in ('query_sales_performance','investigate_sales_change'):
                    trend=state.get('sales_trend') or {}
                    primary=next((c for c in trend.get('comparisons',[]) if c.get('is_primary')),None)
                    fact=analysis.get('facts',{}).get('units_sold',{})
                    before=fact.get('before');after=fact.get('after');rate=fact.get('relative_pct')
                    if primary:before,after,rate=primary['baseline_units'],primary['target_units'],primary['relative_pct']
                    elif target:
                        prior=next((r for r in records if r.get('date')==task['parsed']['comparison_date'] and r.get('status')=='ok'),None)
                        after=target.get('units_sold') if target.get('status')=='ok' else None
                        before=prior.get('units_sold') if prior else None
                        rate=round((after-before)/before*100,2) if before and after is not None else None
                    row['metrics']={'units':after,'baseline_units':before,'change_pct':rate,'baseline_date':task['parsed']['comparison_date']}
                    valid=after is not None and before is not None
                    if scope.get('time',{}).get('is_window'):
                        summary=trend.get('summary',{});row['metrics']['window_units']=summary.get('units_sum')
                        valid=valid and summary.get('complete_window') is True
                    row['comparable']=valid and analysis.get('data_quality') not in ('partial','conflicting')
                elif target and target.get('status')=='ok':
                    row['metrics']={k:target.get(k) for k in ('out_of_stock_minutes','monitored_minutes','active','campaign_name')}
                    row['comparable']=analysis.get('data_quality') not in ('partial','conflicting')
                if not row['comparable'] and not row.get('note'):row['note']='目标/基线数据不足、窗口不完整或来源冲突，不参与结论。'
            else:row['note']='等待查询完成。' if task['status'] in ('running','queued','cancelling') else '该店未完成查询或缺少数据，不参与结论。'
        else:row['note']=child.get('error','任务记录不可用。')
        view['rows'].append(row)
    view['pending']=any(r['status'] in ('queued','running','cancelling') for r in view['rows'])
    valid=[r for r in view['rows'] if r['comparable']];view['comparable_count']=len(valid)
    anchor=next((r for r in valid if r['store_id']==job['anchor_store']),None)
    view['summary']=f"{len(valid)}/{len(view['rows'])} 家数据可比较。"
    if job['scope']['intent']=='query_combined':
        view['summary']=f"{len(valid)}/{len(view['rows'])} 家门店的全部事项数据完整；各店保留独立事项结果，不将失败或缺数当作零。"
        return view
    if job.get('store_ranking'):
        from .store_ranking import summarize
        return summarize(view,job['store_ranking'])
    if job['scope'].get('product_scope',{}).get('ranking'):
        view['summary']=f"{len(valid)}/{len(view['rows'])} 家门店排名覆盖完整。按成品支付件数分别统计各店，保留并列；不是将所有门店合并后的商品总排名。"
        return view
    if anchor and 'units' in anchor['metrics']:
        delta=anchor['metrics']['units']-anchor['metrics']['baseline_units'];direction='下降' if delta<0 else '上升' if delta>0 else '持平'
        others=[r for r in valid if r is not anchor]
        same=sum((r['metrics']['units']-r['metrics']['baseline_units']<0) if delta<0 else (r['metrics']['units']-r['metrics']['baseline_units']>0) if delta>0 else r['metrics']['units']==r['metrics']['baseline_units'] for r in others)
        view['summary']+=f"参照店末日销量{direction}；其他{len(others)}家可比较门店中，{same}家方向相同。相同方向不代表相同原因。"
    elif not anchor:view['summary']+='参照店数据不足，暂不判断其他店是否同样变化。'
    return view
