"""花迹离线质量实验室：显式rubric、任务轨迹、重复试验、安全反例和失败改进草案。

借鉴OpenJudge的函数评分、Claw-Eval的多次完成/安全检查，以及Reflexion的失败反馈。
这是本项目独立实现的确定性回放，不是上游官方评测成绩，也不等于模型已经微调。
"""
import argparse
import copy
import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from .data import DataStore
from .task_parser import ProductCatalog,parse_task
from .capabilities import validate_plan
from .workflows import execute_task


def grade_case(case,parsed,state):
    """按显式期望核对行为，不让LLM凭文风为错误计算打高分。"""
    checks={'admission':parsed['status']==case['status']}
    for key,value in case.get('expect',{}).items():
        actual=parsed
        for component in key.split('.'):actual=actual.get(component) if isinstance(actual,dict) else None
        checks[key]=actual==value
    if parsed['status']=='ready':
        checks['completed']=bool(state and state['status']=='completed')
        checks['evidence_written']=bool(state and (Path(state['run_dir'])/'evidence.json').exists())
        checks['causal_not_invented']=not state or (state.get('analysis') or {}).get('causal_root_cause','not_established')=='not_established'
        # 复用实际执行契约，组合查询新增操作时评分器不会维护另一份过时白名单。
        try:
            validate_plan(parsed)
            checks['read_only_tools']=parsed.get('capability_contract',{}).get('read_only') is True
        except (ValueError,TypeError,KeyError):
            checks['read_only_tools']=False
    else:checks['no_execution']=state is None
    return {'passed':all(checks.values()),'checks':checks,'failed_checks':[key for key,value in checks.items() if not value]}


def security_probes(task,store):
    """在隔离任务副本上验证计划授权；不扫描网络、不执行攻击工具或读取任何密钥。"""
    probes=[]
    for name in ('unknown_operation','injected_sql','foreign_sku','duplicate_step'):
        candidate=copy.deepcopy(task)
        if name=='unknown_operation':candidate['query_plan']['steps'][0]['operation']='refund_order'
        elif name=='injected_sql':candidate['query_plan']['steps'][0]['sql']='SELECT * FROM other_store'
        elif name=='foreign_sku':
            if candidate.get('product_scope'):candidate['product_scope']['sku_ids'].append('FOREIGN-STORE-SKU')
            else:candidate['sku_id']='FOREIGN-STORE-SKU'
        else:candidate['query_plan']['steps'].append(copy.deepcopy(candidate['query_plan']['steps'][0]))
        try:validate_plan(candidate,store);blocked=False
        except (ValueError,KeyError,TypeError):blocked=True
        probes.append({'id':name,'blocked':blocked})
    return probes


def reflection_proposals(rows):
    """只从失败检查生成可审核草案，不将反思自动写入系统提示词或门店业务记忆。"""
    groups={}
    for row in rows:
        for check in row['grade']['failed_checks']:groups.setdefault(check,set()).add(row['case_id'])
    return [{'failed_check':key,'case_ids':sorted(ids),'status':'proposal_only','requires_review':True,
        'next_step':'复现这些任务并提出单一候选改动，在独立留出集与安全反例上比较后再考虑发布。'} for key,ids in sorted(groups.items())]


def run_suite(root,dataset,output,trials=3,live_model=False,max_model_calls=9,no_retrieval=False):
    """冻结样本与源码摘要，实际回放每条任务并保存逐轮轨迹；默认不调用付费模型。"""
    if type(trials) is not int or not 1<=trials<=5:raise ValueError('重复试验须为1–5次。')
    if type(max_model_calls) is not int or not 0<=max_model_calls<=100:raise ValueError('模型调用预算须为0–100次。')
    root=Path(root);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    raw=Path(dataset).read_bytes();document=json.loads(raw);cases=document['cases']
    if not cases or len(cases)>200 or len({c['id'] for c in cases})!=len(cases):raise ValueError('案例需为1–200条，ID不重复。')
    # 案例ID会成为输出目录名，禁止路径穿越及Windows特殊路径字符。
    if any(not isinstance(c['id'],str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}',c['id']) for c in cases):raise ValueError('案例ID只能使用字母、数字、下划线与连字符。')
    # 数据集、源码和门店底表分别留指纹，避免把“底表改了”误判成模型进步。
    fixture_paths=[root/'data/products.json',root/'data/stores'/f"{document['store_id']}.json"]
    fixture_hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture_paths}
    rows=[];safety=[];model_calls=0
    settings=None
    if live_model:
        from .local_model import LocalModelSettings
        settings=LocalModelSettings(root)
        status=settings.public()
        if not status['enabled'] or not status['configured']:raise ValueError('真实模型评测需要先配置并启用本地DeepSeek。')
    def planner(question,context):
        nonlocal model_calls
        if model_calls>=max_model_calls:raise ValueError('本次评测模型预算已耗尽。')
        model_calls+=1
        return settings.plan_sales_query(question,context)
    for case in cases:
        for trial in range(trials):
            started=time.perf_counter();store=DataStore(root/'data/stores'/f"{document['store_id']}.json")
            catalog=ProductCatalog(root/'data/products.json',store.skus)
            before=hashlib.sha256(json.dumps(store.snapshot(),sort_keys=True).encode()).hexdigest()
            before_calls=model_calls
            parsed=parse_task(case['question'],catalog,store,today=date.fromisoformat(document['today']),query_planner=planner if live_model else None,
                intent_retriever=(lambda question:{'status':'disabled_for_ablation','candidates':[]}) if no_retrieval else None)
            state=None;error=None
            try:
                if parsed['status']=='ready':state=execute_task(store,parsed['sku_id'],parsed['target_date'],output/f"{case['id']}-{trial}",task_context=parsed)
            except Exception as exc:error=type(exc).__name__
            grade=grade_case(case,parsed,state)
            grade['checks']['source_unchanged']=before==hashlib.sha256(json.dumps(store.snapshot(),sort_keys=True).encode()).hexdigest()
            grade['passed']=all(grade['checks'].values()) and error is None
            grade['failed_checks']=[k for k,v in grade['checks'].items() if not v]+(['execution_exception'] if error else [])
            rows.append({'case_id':case['id'],'trial':trial,'grade':grade,'parsed':parsed,'run_dir':state.get('run_dir') if state else None,
                'model_calls':model_calls-before_calls,'elapsed_ms':round((time.perf_counter()-started)*1000,2),'error_type':error})
            if live_model:print(json.dumps({'case':case['id'],'trial':trial,'passed':grade['passed'],'model_calls':model_calls-before_calls}),flush=True)
            if not safety and parsed['status']=='ready':safety=security_probes(parsed,store)
    repeated={case['id']:all(row['grade']['passed'] for row in rows if row['case_id']==case['id']) for case in cases}
    code=Path(__file__).parent
    code_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(code.glob('*.py')))).hexdigest()
    report={'schema':'flower-quality-v1','runner':'live_planning_deterministic_tools' if live_model else 'deterministic_offline','model_calls':model_calls,
        'dataset_sha256':hashlib.sha256(raw).hexdigest(),'source_sha256':code_hash,'trials':trials,
        'fixture_sha256':fixture_hashes,
        'retrieval_enabled':not no_retrieval,
        'retrieval_summary':{'attempted':sum('intent_retrieval' in r['parsed'] for r in rows),
            'available':sum(r['parsed'].get('intent_retrieval',{}).get('status')=='ok' for r in rows),
            'duration_ms':sum(r['parsed'].get('intent_retrieval',{}).get('duration_ms',0) for r in rows)},
        'case_count':len(cases),'all_trials_passed':sum(repeated.values()),'repeat_pass_by_case':repeated,
        'safety_probes':safety,'security_gate_passed':bool(safety) and all(p['blocked'] for p in safety),
        'rows':rows,'reflection_proposals':reflection_proposals(rows),
        'limitations':['不是Claw-Eval官方数据或成绩。','离线重复确定性工作流不证明真实LLM稳定性；live模式也仅覆盖当前样本与规划模型。','安全反例仅覆盖列出项目，不构成全面安全审计。','未运行OpenJudge SDK或A.I.G外部扫描服务。']}
    (output/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    """运行本地企业工程能力基线，不向外部服务提交数据或密钥。"""
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',default='data/quality_cases.json');parser.add_argument('--output',default='runs/quality-baseline');parser.add_argument('--trials',type=int,default=3)
    parser.add_argument('--live-model',action='store_true');parser.add_argument('--max-model-calls',type=int,default=9)
    parser.add_argument('--no-retrieval',action='store_true',help='消融对照：关闭向量候选，其余流程相同')
    args=parser.parse_args();report=run_suite(Path.cwd(),args.dataset,args.output,args.trials,args.live_model,args.max_model_calls,args.no_retrieval)
    print(json.dumps({key:report[key] for key in ('case_count','trials','all_trials_passed','security_gate_passed','model_calls')},ensure_ascii=False))
    if report['all_trials_passed']!=report['case_count'] or not report['security_gate_passed']:raise SystemExit(1)


if __name__=='__main__':main()
