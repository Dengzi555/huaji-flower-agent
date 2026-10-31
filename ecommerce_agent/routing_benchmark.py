"""路由消融与真实工具链评测：冻结题目/底表/评分标准，保留失败、跳过和未知用量。

三路对比只改变理解策略，下游均执行相同确定性工具流程，避免把执行差异算成路由收益。
真实模型工具循环另列分组；答案/期望只交给评分器，不传入模型，不自动回灌检索语料。
"""
import argparse
import hashlib
import json
import math
import re
import time
import uuid
from datetime import date
from pathlib import Path
from .data import DataStore
from .task_parser import ProductCatalog,parse_task
from .golden_evaluation import load_suite,grade_execution
from .quality_lab import security_probes
from .model_runtime import model_scope
from .errors import AgentError
from .capabilities import validate_plan
from .workflows import execute_task

STRATEGIES=('rules_only','direct_model','hybrid')
CHAIN_CASES=('oracle-stockout','oracle-traffic_drop','oracle-cancellations','oracle-conflict')


class CallBudget:
    """整次实验共享调用尝试上限；失败也消耗次数，不自动重试。"""
    def __init__(self,limit):
        if type(limit) is not int or not 0<=limit<=200:raise ValueError('实验模型调用预算须为0–200次。')
        self.limit=limit;self.used=0

    def take(self):
        if self.used>=self.limit:raise AgentError('model_budget_exceeded',phase='evaluation')
        self.used+=1


class BudgetedProvider:
    """保持原适配层的取消、用量与资源校验，只额外限制整次实验的API尝试次数。"""
    supports_cancellation=True
    supports_resource_management=True
    def __init__(self,provider,budget):self.provider=provider;self.budget=budget
    @property
    def last_usage(self):return self.provider.last_usage
    def complete(self,*args,**kwargs):
        self.budget.take();return self.provider.complete(*args,**kwargs)


def grade(checks):
    """空检查不能通过；失败检查完整保留，不靠重试挑选好结果。"""
    return {'passed':bool(checks) and all(checks.values()),'checks':checks,'failed_checks':[k for k,v in checks.items() if not v]}


def quantile(values,p):
    """nearest-rank分位数；同时报告样本数，不把少量单次样本当稳定SLA。"""
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None


def totals(rows):
    """累计供应商上报用量；未知字段不填0，已知部分另列。"""
    fields=('prompt_tokens','completion_tokens','total_tokens')
    return {'calls':sum(r['model_runtime']['calls'] for r in rows),
        'reported':{k:sum(r['model_runtime']['reported'][k] for r in rows) if all(r['model_runtime']['reported'][k] is not None for r in rows) else None for k in fields},
        'known_totals':{k:sum(r['model_runtime']['known_totals'][k] for r in rows) for k in fields},
        'unknown_usage_calls':sum(r['model_runtime']['unknown_usage_calls'] for r in rows),'cost':None}


def summarize_group(rows,skipped):
    """同一分母报告任务通过、误放行、无谓追问、模型触发率和阶段耗时。"""
    n=len(rows);times=[r['elapsed_ms'] for r in rows]
    expected_ready=[r for r in rows if r['expected_status']=='ready']
    intent_rows=[r for r in rows if r.get('expected_intent')]
    slot_checks=[v for r in rows for k,v in r['grade']['checks'].items() if k in ('sku_id','target_date','comparison_date') or k.startswith('product_scope.')]
    ready=[r for r in rows if r['parsed']['status']=='ready']
    passed=sum(r['grade']['passed'] for r in rows)
    return {'executed':n,'passed':passed,'failed':n-passed,'skipped':len(skipped),'pass_rate':passed/n if n else None,
        'intent_accuracy':sum(r['parsed'].get('intent')==r['expected_intent'] for r in intent_rows)/len(intent_rows) if intent_rows else None,
        'intent_labeled_cases':len(intent_rows),'slot_checks':len(slot_checks),'slot_accuracy':sum(slot_checks)/len(slot_checks) if slot_checks else None,
        'unexpected_admissions':sum(r['expected_status']!='ready' and r['parsed']['status']=='ready' for r in rows),
        'unnecessary_clarifications':sum(r['parsed']['status']=='needs_clarification' for r in expected_ready),
        'execution_completed':sum(r['execution_status']=='completed' for r in ready),'admitted_cases':len(ready),
        'model_trigger_rate':sum(r['model_calls']>0 for r in rows)/n if n else None,
        'latency_ms':{'n':n,'p50':quantile(times,.5),'p95':quantile(times,.95)},
        'planning_latency_ms':{'p50':quantile([r['planning_ms'] for r in rows],.5),'p95':quantile([r['planning_ms'] for r in rows],.95)},
        'usage':totals(rows)}


def analyze_report(report):
    """对已冻结结果做零API分析；不改变原评分，不用后验结论修改样本期望。"""
    groups={};intent_metrics={}
    for strategy in STRATEGIES:
        rows=[r for r in report['rows'] if r['strategy']==strategy]
        groups[strategy]={category:summarize_group([r for r in rows if r['category']==category],[])
            for category in sorted({r['category'] for r in rows})}
        labeled=[r for r in rows if r.get('expected_intent')];metrics={}
        for intent in sorted({r['expected_intent'] for r in labeled}):
            tp=sum(r['expected_intent']==intent and r['parsed'].get('intent')==intent for r in labeled)
            fp=sum(r['expected_intent']!=intent and r['parsed'].get('intent')==intent for r in labeled)
            fn=sum(r['expected_intent']==intent and r['parsed'].get('intent')!=intent for r in labeled)
            metrics[intent]={'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None}
        intent_metrics[strategy]={'labeled_cases':len(labeled),'unlabeled_cases':len(rows)-len(labeled),'per_intent':metrics}
    paired=next((p for p in report['paired'] if p['against']=='direct_model'),None)
    return {'source_report_run_id':report['run_id'],'source_sha256':report['source_sha256'],'per_category':groups,
        'intent_metrics_labeled_subset_only':intent_metrics,
        'paired_call_reduction':1-paired['hybrid_model_calls']/paired['other_model_calls'] if paired and paired['other_model_calls'] else None,
        'note':'仅描述本批单次实验；类别结果不构成统计显著性结论，精确率/召回率仅使用已标注意图的子集。'}


def run_trial(case,store,catalog,oracles,today,output,strategy,planner=None,retriever=None,provider=None,parsed=None):
    """一次真实试验：理解→统一准入→工具→共用评分；本函数不重试也不修改期望。"""
    started=time.perf_counter();state=None;error=None
    proposals=[]
    def observed_planner(question,context):
        """只记录工具计划白名单字段，便于复现被拒绝计划；不落原始供应商响应/请求头。"""
        try:
            proposal=planner(question,context)
            fields=('decision','intent','selector','start_date','end_date','metric','group_by','direction','limit')
            proposals.append({key:value for key,value in proposal.items() if key in fields
                and (type(value) in (int,bool) or isinstance(value,str) and len(value)<=200)} if isinstance(proposal,dict) else {'invalid_type':type(proposal).__name__})
            return proposal
        except Exception as exc:
            proposals.append({'error_type':type(exc).__name__,'code':getattr(exc,'code',None)});raise
    with model_scope() as ledger:
        planning_start=time.perf_counter()
        if parsed is None:
            parsed=parse_task(case['question'],catalog,store,today=today,query_planner=observed_planner if planner else None,
                intent_retriever=retriever,planning_policy=strategy)
        planning_ms=round((time.perf_counter()-planning_start)*1000,2)
        try:
            if parsed['status']=='ready':state=execute_task(store,parsed['sku_id'],parsed['target_date'],output,task_context=parsed,
                **({'provider':provider,'max_steps':12,'timeout_seconds':180} if provider else {}))
        except Exception as exc:error=type(exc).__name__
        checks=grade_execution(case,parsed,state,oracles)
        if error:checks['execution_exception']=False
        events=[]
        if state:
            events=[json.loads(line) for line in (Path(state['run_dir'])/'events.jsonl').read_text(encoding='utf-8').splitlines()]
            allowed=set(validate_plan(parsed,store))
            checks['tools.authorized']=all(e['tool'] in allowed for e in events if e['type']=='tool_started')
        if provider:
            validation=(state or {}).get('validation') or {}
            checks['model.numeric_assertions_verified']=validation.get('status')=='passed' and validation.get('checked',0)>0
            checks['model.finish_accepted']=bool((state or {}).get('model_draft'))
        runtime=ledger.snapshot()
    expected=case['expected']
    return {'case_id':strategy+'--'+case['id'],'original_case_id':case['id'],'strategy':strategy,'trial':0,
        'category':case['category'],'split':case['split'],'parsed':parsed,'grade':grade(checks),
        'expected_status':expected['status'],'expected_intent':expected.get('parsed',{}).get('intent'),
        'run_dir':state.get('run_dir') if state else None,'execution_status':state.get('status') if state else None,
        'model_calls':runtime['calls'],'model_runtime':runtime,'planning_ms':planning_ms,
        'plan_proposals':proposals,
        'elapsed_ms':round((time.perf_counter()-started)*1000,2),'error_type':error,'trajectory':[],
        'evaluation_scope':'live_model_tool_chain' if provider else 'routing_with_deterministic_execution'}


def run_benchmark(root,dataset,output,**options):
    """运行实验并关闭本线程创建的年度只读连接，重复评测不泄漏数据库句柄。"""
    stores={}
    try:return _run_benchmark(root,dataset,output,_stores=stores,**options)
    finally:
        for store in stores.values():
            local=getattr(getattr(store,'records',None),'local',None)
            if local is not None and hasattr(local,'db'):local.db.close();del local.db


def _run_benchmark(root,dataset,output,*,live=False,max_model_calls=128,split='all',settings=None,retriever=None,_stores):
    """固定年度版本与单轮样本；三路按case轮换顺序，报告冷启动、跳过和样本覆盖限制。"""
    root=Path(root);dataset=Path(dataset);output=Path(output).resolve()
    if split not in ('all','regression','holdout'):raise ValueError('未知样本分组。')
    if output.exists() and any(output.iterdir()):raise ValueError('请使用新输出目录，不能覆盖既有实验。')
    output.mkdir(parents=True,exist_ok=True)
    document,audit=load_suite(dataset,root/'data/intent_examples.json')
    (output/'dataset.snapshot.json').write_bytes(dataset.read_bytes())
    source_files=sorted((root/'ecommerce_agent').glob('*.py'))
    source_hash=hashlib.sha256(b''.join(p.read_bytes() for p in source_files)).hexdigest()
    source_snapshot=output/'source-snapshot';source_snapshot.mkdir()
    for path in source_files:(source_snapshot/path.name).write_bytes(path.read_bytes())
    cases=[c for c in document['cases'] if c['execution']!='conversation_state' and (split=='all' or c['split']==split)]
    version=document['fixture']['annual_version'];folder=root/'data/annual'/version
    if not re.fullmatch(r'[a-f0-9]{12}',version):raise ValueError('年度版本非法。')
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if any(c['store_id'] not in {s['id'] for s in manifest['stores']} for c in cases):raise ValueError('样本门店不在年度版本白名单。')
    oracles={c['id']:c for c in json.loads((folder/'answers.json').read_text(encoding='utf-8'))['cases']}
    budget=CallBudget(max_model_calls if live else 0)
    if live and settings is None:
        from .local_model import LocalModelSettings
        settings=LocalModelSettings(root)
    if live and not (settings.public()['enabled'] and settings.public()['configured']):raise ValueError('本地模型未配置或未启用。')
    def planner(question,context):
        budget.take();return settings.plan_sales_query(question,context)
    # 单独记录向量冷启动；用无答案的固定文本预热，不把首次加载成本偷偷归给某个策略。
    warmup=None
    if live:
        from .semantic_router import retrieve_intents
        retriever=retriever or retrieve_intents;t=time.perf_counter()
        try:result=retriever('查询经营数据');warmup={'status':result.get('status'),'elapsed_ms':round((time.perf_counter()-t)*1000,2)}
        except Exception as exc:warmup={'status':'unavailable','error_type':type(exc).__name__,'elapsed_ms':round((time.perf_counter()-t)*1000,2)}
    rows=[];skipped=[];stores=_stores;catalogs={};safety={};manual=[]
    def load(sid):
        if sid not in stores:
            stores[sid]=DataStore(folder/(sid+'.sqlite3'));catalogs[sid]=ProductCatalog(folder/'products.json',stores[sid].skus)
        return stores[sid],catalogs[sid]
    def save(row):
        rows.append(row)
        with (output/'progress.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps({'strategy':row['strategy'],'case':row['original_case_id'],'passed':row['grade']['passed'],
            'failed':row['grade']['failed_checks'],'model_calls':row['model_calls'],'budget_used':budget.used},ensure_ascii=False),flush=True)
    for index,case in enumerate(cases):
        store,catalog=load(case['store_id'])
        order=STRATEGIES[index%3:]+STRATEGIES[:index%3]
        for strategy in order:
            if strategy!='rules_only' and (not live or budget.used>=budget.limit):
                skipped.append({'case_id':case['id'],'strategy':strategy,'reason':'model_disabled' if not live else 'model_budget_exhausted'});continue
            row=run_trial(case,store,catalog,oracles,date.fromisoformat(document['business_today']),output/strategy/case['id'],strategy,
                planner=planner if live and strategy!='rules_only' else None,retriever=retriever)
            if strategy not in safety and row['parsed']['status']=='ready':safety[strategy]=security_probes(row['parsed'],store)
            save(row)
    for identity in CHAIN_CASES:
        case=next((c for c in cases if c['id']==identity),None)
        if not case:continue
        baseline=next((r for r in rows if r['strategy']=='hybrid' and r['original_case_id']==identity),None)
        if not live or budget.used>=budget.limit or not baseline or baseline['parsed']['status']!='ready':
            skipped.append({'case_id':identity,'strategy':'model_tool_chain','reason':'no_model_budget_or_validated_plan'});continue
        store,catalog=load(case['store_id'])
        row=run_trial(case,store,catalog,oracles,date.fromisoformat(document['business_today']),output/'model_tool_chain'/identity,
            'model_tool_chain',provider=BudgetedProvider(settings.provider(),budget),parsed=baseline['parsed'])
        save(row)
        state=json.loads((Path(row['run_dir'])/'run.json').read_text(encoding='utf-8')) if row['run_dir'] else {}
        manual.append({'case_id':identity,'run_dir':row['run_dir'],'draft':state.get('model_draft'),'review_status':'pending',
            'rubric':document['manual_review_rubric'],'review_scores':None,'note':'数字断言只验指定字段，不代表因果、整段回答或建议实用性已验证。'})
    summaries={key:summarize_group([r for r in rows if r['strategy']==key],[s for s in skipped if s['strategy']==key]) for key in (*STRATEGIES,'model_tool_chain')}
    # 配对比较只用两组都实际运行的相同题目；未运行样本不能贡献收益。
    paired=[]
    for other in ('rules_only','direct_model'):
        a={r['original_case_id']:r for r in rows if r['strategy']=='hybrid'};b={r['original_case_id']:r for r in rows if r['strategy']==other};ids=sorted(a.keys()&b.keys())
        paired.append({'against':other,'paired_cases':len(ids),'hybrid_only_pass':sum(a[i]['grade']['passed'] and not b[i]['grade']['passed'] for i in ids),
            'other_only_pass':sum(b[i]['grade']['passed'] and not a[i]['grade']['passed'] for i in ids),
            'hybrid_model_calls':sum(a[i]['model_calls'] for i in ids),'other_model_calls':sum(b[i]['model_calls'] for i in ids)})
    report={'schema':'flower-quality-v1','runner':'routing_ablation_live' if live else 'routing_ablation_offline','run_id':uuid.uuid4().hex[:12],
        'trials':1,'case_count':len(rows),'all_trials_passed':sum(r['grade']['passed'] for r in rows),'rows':rows,'skipped':skipped,
        'summaries':summaries,'paired':paired,'model_calls':sum(r['model_calls'] for r in rows),'budget_attempts':budget.used,'budget_limit':budget.limit,
        'dataset_version':document['version'],'business_today':document['business_today'],'annual_version':version,'split':split,
        'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest(),
        'source_sha256':source_hash,
        'source_unchanged_during_run':source_hash==hashlib.sha256(b''.join(p.read_bytes() for p in source_files)).hexdigest(),
        'fixture_sha256':{r['id']:r['content_sha256'] for r in manifest['stores']},'separation_audit':audit,'retrieval_warmup':warmup,
        'safety_probes':safety,'security_gate_passed':bool(safety) and all(p['blocked'] for probes in safety.values() for p in probes),
        'limitations':['自定义样本，非官方Claw榜单；既有holdout已被观察，不声称未见泛化成绩。',
            '三路共享目录、实体/日期规范化、默认口径、能力边界和准入校验；direct_model跳过规则快捷返回与向量候选，不是完全不使用宿主规则的端到端模型。',
            '三路只比较理解策略，下游均为确定性执行；真实模型工具循环另列。',
            '只评单轮；原有8个会话状态用例不进入本次路由分母。',
            '每题每方案一次，未估计稳定性或统计显著性；耗时受网络与执行顺序影响。',
            '槽位准确率只统计已人工标注字段，并非完整语义准确率；未做自动语义Judge，人工复核待完成。',
            '供应商未上报用量保留未知，未接账单，费用不估算为真实金额。']}
    bad=[{'strategy':r['strategy'],'case_id':r['original_case_id'],'question':r['parsed']['raw_question'],
        'failed_checks':r['grade']['failed_checks'],'actual_status':r['parsed']['status'],'actual_intent':r['parsed'].get('intent'),
        'run_dir':r['run_dir'],'review_status':'needs_review','auto_apply':False} for r in rows if not r['grade']['passed']]
    for name,value in [('report.json',report),('badcases.json',bad),('manual_review.json',manual),('analysis.json',analyze_report(report))]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 花迹路由消融与模型工具链评测','','三路使用相同题目、年度版本和评分器；模型工具链单独列出。','',
        '| 方案 | 执行/跳过 | 契约通过 | 模型调用 | 上报tokens | P50/P95毫秒 |','|---|---:|---:|---:|---:|---:|']
    for key,s in summaries.items():lines.append(f"| {key} | {s['executed']}/{s['skipped']} | {s['passed']} | {s['usage']['calls']} | {s['usage']['reported']['total_tokens']} | {s['latency_ms']['p50']}/{s['latency_ms']['p95']} |")
    lines+=['','## 解释范围','',*['- '+item for item in report['limitations']]]
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def main():
    """默认离线零付费；显式开启模型和整次实验预算，输出目录禁止覆盖。"""
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',default='data/evaluation/golden_cases.json')
    parser.add_argument('--output',required=True);parser.add_argument('--live-model',action='store_true')
    parser.add_argument('--max-model-calls',type=int,default=128);parser.add_argument('--split',choices=['all','regression','holdout'],default='all')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    report=run_benchmark(root,args.dataset,args.output,live=args.live_model,max_model_calls=args.max_model_calls,split=args.split)
    from .claw_eval_adapter import export_and_grade
    export_and_grade(Path(args.output)/'report.json',None,Path(args.output)/'claw')
    print(json.dumps({'summaries':report['summaries'],'paired':report['paired']},ensure_ascii=False))
    # 规则基线失败是实验结果，不是运行器错误；安全门禁失败单独以非零退出。
    if not report['security_gate_passed']:raise SystemExit(1)


if __name__=='__main__':main()
