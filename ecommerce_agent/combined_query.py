"""多事项只读计划：共享冻结范围，最多三个独立工具并发，主线程统一保存事件与证据。

不启动多个Agent，不对歧义问题穷举分支。每个工作线程持有独立注册表，
只读同一数据快照；结果按请求顺序汇总，部分失败不覆盖成功证据。
"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextvars import copy_context
from copy import deepcopy
from datetime import date,datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from .cancellation import CancelToken,CancelledError,current_token,cancel_scope,checkpoint,cancel_state
from .model_deadlines import INVESTIGATION_SECONDS
from .group_sales import group_sales
from .model_runtime import current_ledger
from .capabilities import CONTRACT_VERSION,LIMITS

DOMAINS={
    'sales':{'tool':'get_product_group_sales','label':'销量表现与变化'},
    'inventory':{'tool':'get_inventory_range','label':'库存监测与缺货'},
    'promotions':{'tool':'get_promotion_range','label':'促销活动状态'},
}
MAX_CONCURRENCY=3


def parse_combined(question,catalog,store,*,today,sku_override,date_override,query_context,routing):
    """复用统一商品/日期解析，仅在服务端把确定的多事项编译成只读计划。"""
    from .task_parser import _parse_rules_task
    operations=routing.get('operations',[])
    if not operations and query_context:operations=(query_context.operations or {}).get('composite_operations',[])
    # 原文完整保留给实体、日期和能力边界检查；只替换内部选路，避免重写问句丢条件。
    selected={**routing,'intent':'query_sales_performance','status':'selected'}
    base=_parse_rules_task(question,catalog,store,today=today,sku_override=sku_override,
        date_override=date_override,query_context=query_context,routing_decision=selected)
    # 条件不全时也保留已识别事项；仅供澄清视图展示，不意味着任务已经准入。
    base['composite_operations']=list(dict.fromkeys(operations))
    if base['status'] not in ('ready','needs_data'):
        base['intent']='query_combined';base['routing']=routing
        return base
    # 数据缺失留给每个所需工具独立判断，不拿销量缺失阻止库存/活动查询。
    task=deepcopy(base);task.update(intent='query_combined',routing=routing,status='ready',issues=[],
        composite_operations=list(dict.fromkeys(operations)),include_trend='sales' in operations)
    task['query_plan']={'version':'combined-query-v1','contract_version':CONTRACT_VERSION,
        'operation':'parallel_reads','start_date':task['trend_start'] if task['is_window'] else task['target_date'],
        'end_date':task['target_date'],'max_concurrency':MAX_CONCURRENCY,
        'steps':[{'id':domain,'operation':domain,'depends_on':[]} for domain in task['composite_operations']]}
    if task.get('product_scope'):
        task['product_scope']['compare_selected']=False
        if 'sales' not in operations:task['product_scope']['ranking']=None
    task['assumptions']=[*task.get('date_resolution',[]),'各事项使用相同门店、商品集合与日期范围；独立取数并发，取数后统一汇总。',
        '库存按SKU日监测，活动状态不等于付费推广；相关变化不直接认定为销量原因。']
    task['capability_contract']={'version':CONTRACT_VERSION,'scope':'group' if task.get('product_scope') else 'single',
        'operations':task['composite_operations'],'read_only':True,'failure_policy':'preserve_partial_results'}
    try:task['capability_contract']['tools']=validate_combined(task,store)
    except ValueError as exc:task.update(status='needs_clarification',issues=[str(exc)])
    return task


def validate_combined(task,store=None):
    """执行前复核操作白名单、冻结窗口、成员与并发上限；模型不得添加任意工具。"""
    operations=task.get('composite_operations');plan=task.get('query_plan',{})
    if not isinstance(operations,list) or not 2<=len(operations)<=3 or len(set(operations))!=len(operations) or any(op not in DOMAINS for op in operations):
        raise ValueError('组合查询需明确至少两个不同的销量、库存或活动事项。')
    expected={'version','contract_version','operation','start_date','end_date','max_concurrency','steps'}
    if set(plan)!=expected or plan.get('version')!='combined-query-v1' or plan.get('contract_version')!=CONTRACT_VERSION or plan.get('operation')!='parallel_reads':
        raise ValueError('组合计划字段无效。')
    if plan['max_concurrency']!=MAX_CONCURRENCY or plan['steps']!=[{'id':d,'operation':d,'depends_on':[]} for d in operations]:
        raise ValueError('组合计划步骤或并发权限被修改。')
    if task.get('capability_contract',{}).get('read_only') is not True:raise ValueError('组合查询必须保持只读。')
    start=task['trend_start'] if task['is_window'] else task['target_date'];end=task['target_date']
    if (plan['start_date'],plan['end_date'])!=(start,end) or not 0<=(date.fromisoformat(end)-date.fromisoformat(start)).days<LIMITS['max_days']:
        raise ValueError('组合查询日期超出冻结范围。')
    if task.get('business_today') and end>=task['business_today']:raise ValueError('只查询已结束自然日。')
    ids=task.get('product_scope',{}).get('sku_ids') or [task.get('sku_id')]
    if not 1<=len(ids)<=LIMITS['max_products'] or len(set(ids))!=len(ids) or any(not isinstance(sku,str) or store and sku not in store.skus for sku in ids):
        raise ValueError('组合商品集合无效。')
    scope=task.get('product_scope') or {}
    if scope and scope.get('summary_start')!=start:raise ValueError('组合汇总日期与冻结窗口不一致。')
    rank=scope.get('ranking')
    if rank and ('sales' not in operations or rank.get('metric')!='units_sold' or rank.get('direction') not in ('asc','desc','both') or type(rank.get('limit')) is not int or not 1<=rank['limit']<=LIMITS['max_rank_limit'] or rank.get('group_by','sku') not in ('sku','variety','product_type','series')):
        raise ValueError('组合销量排名超出允许口径。')
    return [DOMAINS[op]['tool'] for op in operations]


class CombinedToolRegistry:
    """每个分支独占一个注册表；工具不接收参数，范围全部由已校验任务冻结。"""
    def __init__(self,store,task,domain):
        validate_combined(task,store)
        if domain not in task['composite_operations']:raise ValueError('该数据域不在本次组合授权中。')
        self.store=store;self.task=deepcopy(task);self.domain=domain
        from .tools import SCHEMAS,INVENTORY_RANGE_SCHEMA,PROMOTION_RANGE_SCHEMA
        schema=(next(s for s in SCHEMAS if s['function']['name']=='get_product_group_sales') if domain=='sales' else INVENTORY_RANGE_SCHEMA if domain=='inventory' else PROMOTION_RANGE_SCHEMA)
        self.schemas=[schema]

    def execute(self,name,arguments):
        checkpoint()
        if name!=DOMAINS[self.domain]['tool'] or arguments!={}:raise ValueError('工具不属于本分支授权，或参数超出冻结范围。')
        task=self.task;start=task['query_plan']['start_date'];end=task['target_date']
        ids=task.get('product_scope',{}).get('sku_ids') or [task['sku_id']]
        if self.domain=='sales':
            scope={**task.get('product_scope',{}),'sku_ids':ids,'compare_selected':False,'summary_start':start}
            data=group_sales(self.store,scope,start,end,task.get('comparison_date'),task['candidates'])
            checkpoint();return data
        products={p['sku_id']:p for p in task['candidates']};records=[]
        for sku in ids:
            product=products.get(sku,{})
            for offset in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1):
                checkpoint();day=(date.fromisoformat(start)+timedelta(days=offset)).isoformat()
                if product.get('listed_at') and day<product['listed_at']:row={'date':day,'status':'not_listed'}
                elif product.get('retired_at') and day>=product['retired_at']:row={'date':day,'status':'retired'}
                else:row=dict(self.store.daily_record(self.domain,sku,day))
                records.append({**row,'sku_id':sku})
        return {'table':self.domain,'records':records,'start_date':start,'end_date':end,
            'sku_ids':ids,'sample_data':self.store.meta['sample_data']}


def summarize(domain,data,evidence_id):
    """只按同一数据域聚合；缺失不补零，SKU日累计不冒充店铺自然日时长。"""
    if domain=='sales':
        trend=data['trend'];summary=trend['summary'];complete=summary['complete_window']
        # 趋势还包含对比所需的历史日期，本期总量只能累计请求窗口内的数据。
        units=summary.get('units_sum');available=sum(r['units_sold'] for r in trend['records'] if r['status']=='ok' and trend['start_date']<=r['date']<=trend['end_date'])
        overview=f'所选期间成品销量{units}束/桶。' if complete else f'销量记录覆盖不足，可用日期合计{available}束/桶，不代表完整期间。'
        rank=data.get('ranking')
        if rank:
            for part in rank.get('partitions') or [rank]:
                leaders=part.get('leaders',[])
                if leaders:overview+=('销量最低：' if part['direction']=='asc' else '销量最高：')+'、'.join(f'{r["name"]}（{r["units_sold"]}束/桶）' for r in leaders[:5])+'。'
        return {'domain':domain,'title':DOMAINS[domain]['label'],'status':'completed' if complete else 'partial',
            'overview':overview,'ranking':rank,
            'display_metrics':[{'label':'期间销量' if complete else '可用日期销量','value':units if complete else available,'unit':'束/桶'}],
            'records':data.get('records',[]),'group_items':data['items'],'trend':trend,'evidence_id':evidence_id,
            'limitations':['汇总成品件数；不相加跨商品访客、买家或订单数。','销量变化与缺货/活动同时出现，仅构成核查线索，不证明因果。']}
    records=data['records'];active=[r for r in records if r['status'] not in ('not_listed','retired')]
    valid=[r for r in active if r['status']=='ok'];missing=len(active)-len(valid);complete=bool(active) and not missing
    metrics=[{'label':'可用SKU日','value':len(valid),'unit':f'/{len(active)}'}]
    if domain=='inventory':
        stockout=sum(r['out_of_stock_minutes']>0 for r in valid)
        metrics.append({'label':'可用记录中缺货SKU日','value':stockout if valid else None,'unit':''})
        overview=f'{len(valid)}/{len(active)}个SKU日有库存监测，其中{stockout}个SKU日记录了缺货。'
        notes=['库存指日级可售监测/缺货记录，不是实时库存件数。','同一自然日多个商品分别计为多个SKU日，不能把累计分钟称为店铺缺货时长。']
    else:
        campaigns=sorted({r['campaign_name'] for r in valid if r.get('active') and r.get('campaign_name')})
        metrics.append({'label':'可用记录中活动开启SKU日','value':sum(bool(r['active']) for r in valid) if valid else None,'unit':''})
        overview=f'{len(valid)}/{len(active)}个SKU日有活动记录；活动名称：'+('、'.join(campaigns) if campaigns else '可用记录未见已开启活动' if valid else '没有可用活动记录，状态未知')+'。'
        notes=['这里是促销活动/优惠状态，不代表手动或自动广告投放。','未提供促销效果归因，不能据此判断活动带来多少成交。']
    if missing:notes.append(f'{missing}个SKU日缺失或延迟，保留未知，不按0或未开启处理。')
    if not active:notes.append('所选期间没有已在售且可观察的商品日，不能判定零销量或零缺货。')
    return {'domain':domain,'title':DOMAINS[domain]['label'],'status':'completed' if complete else 'partial',
        'overview':overview,'display_metrics':metrics,'records':records,'evidence_id':evidence_id,'limitations':notes,
        'coverage':{'available':len(valid),'expected':len(active),'missing':missing,'excluded':len(records)-len(active)}}


def execute_combined(store,task,output,*,on_event=None,timeout_seconds=INVESTIGATION_SECONDS):
    """主线程调度/记录，工作线程仅读取；并发度固定3、按原计划顺序交付，可部分失败。"""
    validate_combined(task,store)
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    directory=Path(output).resolve()/run_id;directory.mkdir(parents=True)
    from .runner import write_json
    started=time.monotonic();parent=current_token();token=CancelToken(parent)
    deadline=started+min(timeout_seconds,INVESTIGATION_SECONDS)
    state={'run_id':run_id,'run_dir':str(directory),'sku_id':task['sku_id'],'target_date':task['target_date'],
        'intent':'query_combined','mode':'deterministic_parallel_lookup','status':'running','stop_reason':None,
        'task_context':task,'model_calls':0,'tool_calls':0,**store.meta,
        'execution':{'strategy':'parallel_independent_reads','max_concurrency':MAX_CONCURRENCY,'operations':task['composite_operations']}}
    observations={};sections={};sequence=0;pending={};pool=ThreadPoolExecutor(max_workers=MAX_CONCURRENCY,thread_name_prefix='combined-read')
    def event(kind,**values):
        nonlocal sequence
        sequence+=1;entry={'sequence':sequence,'run_id':run_id,'type':kind,'at':datetime.now(timezone.utc).isoformat(),**values}
        with (directory/'events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
        if on_event:on_event(entry)
    def work(domain):
        checkpoint();tool=DOMAINS[domain]['tool'];data=CombinedToolRegistry(store,task,domain).execute(tool,{})
        checkpoint();return {'ok':True,'tool':tool,'evidence_id':'combined-'+domain,'retrieved_at':datetime.now(timezone.utc).isoformat(),'data':data}
    write_json(directory/'request.json',task)
    try:
        with cancel_scope(token):
            checkpoint();event('run_started',mode=state['mode']);event('workflow_selected',intent='query_combined',reason='明确多事项，共享冻结范围，三个独立只读工具有限并发。')
            for domain in task['composite_operations']:
                checkpoint();event('tool_started',tool=DOMAINS[domain]['tool'],call_id='combined-'+domain)
                checkpoint();context=copy_context();pending[pool.submit(context.run,work,domain)]=domain;state['tool_calls']+=1
            while pending:
                checkpoint()
                if time.monotonic()>=deadline:
                    state['stop_reason']='time_budget';token.cancel();break
                done,_=wait(pending,timeout=min(.05,max(0,deadline-time.monotonic())),return_when=FIRST_COMPLETED)
                for future in done:
                    checkpoint();domain=pending.pop(future);tool=DOMAINS[domain]['tool']
                    try:
                        observation=future.result();observations[domain]=observation
                        sections[domain]=summarize(domain,observation['data'],observation['evidence_id'])
                        event('tool_completed',tool=tool,call_id=observation['evidence_id'],result=observation)
                    except CancelledError:raise
                    except Exception as exc:
                        code='tool_timeout' if isinstance(exc,TimeoutError) else 'tool_execution_failed'
                        problem={'code':code,'phase':'tool','title':'工具查询超时' if code=='tool_timeout' else '工具查询失败','retryable':True,
                            'message':'该事项查询未完成，其他已完成事项仍保留。'}
                        sections[domain]={'domain':domain,'title':DOMAINS[domain]['label'],'status':'failed','overview':problem['message'],'error_info':problem,'display_metrics':[],'records':[],'limitations':[]}
                        event('tool_failed',tool=tool,call_id='combined-'+domain,error_info=problem)
            state['status']='completed' if len(sections)==len(task['composite_operations']) and all(s['status']=='completed' for s in sections.values()) else 'incomplete'
            state['stop_reason']=state['stop_reason'] or ('combined_completed' if state['status']=='completed' else 'partial_results')
    except CancelledError:
        cancel_state(state,parent or token)
    except Exception:
        state.update(status='failed',stop_reason='execution_error')
    finally:
        token.cancel()
        for future in pending:future.cancel()
        pool.shutdown(wait=False,cancel_futures=True)
        if parent and parent.event.is_set():cancel_state(state,parent)
        for domain in task['composite_operations']:
            if domain not in sections:sections[domain]={'domain':domain,'title':DOMAINS[domain]['label'],'status':'cancelled' if state['status']=='cancelled' else 'incomplete','overview':'本事项未完成；不将缺失结果当作零。','display_metrics':[],'records':[],'limitations':[]}
        ordered=[sections[d] for d in task['composite_operations']]
        state['analysis']={'kind':'combined','title':'多事项经营查询','overview':f'{sum(s["status"]=="completed" for s in ordered)}/{len(ordered)}项数据完整；各项独立保留证据。',
            'sections':ordered,'facts':{},'hypotheses':[],'conflicts':[],'gaps':[],
            'evidence_ids':[o['evidence_id'] for o in observations.values()],
            'data_quality':'complete_for_supplied_daily_fields' if state['status']=='completed' else 'partial',
            'limitations':['并发只作用于独立取数；所有结果返回后再汇总。缺口与失败不按零处理。','联合展示不等于因果归因；库存与活动同时变化不能直接认定为销量原因。']}
        if state['status']=='incomplete' and state['stop_reason']=='partial_results':
            state['error_info']={'code':'combined_partial','status':'incomplete','category':'data','phase':'aggregation',
                'title':'部分事项数据不完整','message':'已保留可用结果，请查看各事项的缺失或失败说明；缺少记录不按零处理。',
                'retryable':False,'recovery_actions':[{'type':'view_sources','label':'查看数据覆盖'}]}
        sales=sections.get('sales',{});state['sales_trend']=sales.get('trend')
        from .combined_answer import build_answer
        state['analysis']['business_answer']=build_answer(task,ordered)
        answer=state['analysis']['business_answer']
        state['analysis']['title']=answer['title']
        state['analysis']['overview']=' '.join(p['paragraphs'][0] for p in answer['parts'] if p['paragraphs'])
        state['calendar_context']=observations.get('sales',{}).get('data',{}).get('calendar')
        state['duration_ms']=round((time.monotonic()-started)*1000)
        state['execution']['completed_operations']=[d for d in task['composite_operations'] if sections[d]['status']=='completed']
        ledger=current_ledger();state['model_runtime']=ledger.snapshot() if ledger else None
        evidence={'observations':list(observations.values())};state['evidence_sha256']=hashlib.sha256(json.dumps(evidence,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        state['validation']={'status':'references_only','checked':0,'scope':'确定性组合查询，保留原始工具证据；不代表因果已验证。'}
        lines=['# 多事项经营查询','',task['raw_question'],f'状态：{state["status"]}',f'并发上限：{MAX_CONCURRENCY}','学习用合成经营数据' if state['sample_data'] else '用户提供，未独立核验']
        lines+=['',answer['notice']]
        for part in answer['parts']:lines+=['','## '+part['title'],*part['paragraphs']]
        if answer['next_steps']:lines+=['','## 建议先看',*answer['next_steps']]
        write_json(directory/'evidence.json',evidence);write_json(directory/'validation.json',state['validation']);write_json(directory/'run.json',state)
        (directory/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        event('run_settled',status=state['status'],stop_reason=state['stop_reason'])
    return state
