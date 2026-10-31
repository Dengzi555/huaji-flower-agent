"""花迹学习评测集：冻结版本、按层评分、明确跳过项、保留bad case与Claw轨迹。

只评测，不修业务路由、不自动把测试问题加入向量库。golden是预期行为，
包括应当追问/拒绝的反例；离线模式缺少模型的样例明确跳过，不能记为通过。
"""
import argparse
import hashlib
import json
import re
import time
import uuid
from datetime import date
from pathlib import Path

from .data import DataStore,FIELDS
from .task_parser import ProductCatalog,parse_task
from .workflows import execute_task
from .quality_lab import grade_case,security_probes,reflection_proposals


def lookup(value,path):
    """按固定路径读取实际字段；缺字段用独立哨兵，不能误判为期望的None。"""
    for key in path.split('.'):
        if not isinstance(value,dict) or key not in value:return MISSING
        value=value[key]
    return value


MISSING=object()


def check_paths(actual,expected,prefix=''):
    """显式等值评分；不依赖模型自报置信度或“回答看起来差不多”。"""
    return {prefix+key:lookup(actual,key)==value for key,value in expected.items()}


def load_suite(path,index_path):
    """验证样例结构、ID和split；禁止与向量例句逐字重复，近似重复单独报告。"""
    document=json.loads(Path(path).read_text(encoding='utf-8'));cases=document['cases']
    if document.get('schema')!='flower-golden-v1' or not cases or len(cases)>200:raise ValueError('评测集版本或规模无效。')
    ids=set();families={};questions=[]
    for case in cases:
        identity=case['id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',identity) or identity in ids:raise ValueError('案例ID非法或重复。')
        ids.add(identity)
        if case['split'] not in ('regression','holdout') or case['execution'] not in ('offline','model_allowed','conversation_state'):raise ValueError('案例分组无效。')
        if case['family'] in families and families[case['family']]!=case['split']:raise ValueError('同族问题不能跨split。')
        families[case['family']]=case['split']
        texts=[t['question'] for t in case['turns']] if case['execution']=='conversation_state' else [case['question']]
        if any(not isinstance(q,str) or not 1<=len(q)<=2000 for q in texts):raise ValueError('评测问题无效。')
        questions.extend((identity,q) for q in texts)
    index=json.loads(Path(index_path).read_text(encoding='utf-8'));examples=index['examples']
    normalize=lambda text:re.sub(r'[\s，。！？,.!?]','',text)
    indexed={normalize(e['text']):e['id'] for e in examples}
    duplicates=[{'case_id':identity,'example_id':indexed[normalize(q)]} for identity,q in questions if normalize(q) in indexed]
    if duplicates:raise ValueError('评测问题与向量例句重复：'+json.dumps(duplicates,ensure_ascii=False))
    def grams(text):
        text=normalize(text);return {text[i:i+2] for i in range(max(0,len(text)-1))}
    near=[]
    for identity,q in questions:
        a=grams(q)
        for example in examples:
            b=grams(example['text']);score=len(a&b)/max(1,len(a|b))
            if score>=.8:near.append({'case_id':identity,'example_id':example['id'],'char_bigram_jaccard':round(score,3)})
    return document,{'exact_duplicates':0,'near_duplicate_warnings':near,'index_sha256':hashlib.sha256(Path(index_path).read_bytes()).hexdigest(),
        'note':'字符近似度仅提示人工检查，不能证明语义无泄漏；本集不作为检索语料。'}


def oracle_checks(case,oracle,state,parsed):
    """独立答案只传入评分器；核对数值和关键缺口，不以文风替代事实正确。"""
    checks={}
    if not state or state['status']!='completed':return {'oracle.execution':False}
    if oracle['id'].startswith('holiday-'):
        ranking=state['analysis'].get('ranking',{});leaders=ranking.get('leaders',[])
        checks['oracle.leaders']=sorted(r['name'] for r in leaders)==sorted(oracle['leaders'])
        checks['oracle.counts']=bool(leaders) and all(r['units_sold']==oracle['units_by_variety'].get(r['name']) for r in ranking.get('rows',[]))
    else:
        evidence=json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))
        seen={}
        for item in evidence['observations']:
            data=item.get('data',{});table=data.get('table')
            if table in FIELDS:
                for record in data.get('records',[]):seen[table,record['date']]=record
        checks['oracle.sales_present']=('sales',oracle['target_date']) in seen
        required={'stockout':['inventory'],'traffic_drop':['traffic'],'campaign_end':['promotions'],
            'zero_visitors':['traffic'],'missing':['traffic','inventory'],'conflict':['inventory'],
            'cancellations':['order_cohorts']}.get(oracle['id'],[])
        for table in required:checks['oracle.required_'+table]=(table,oracle['target_date']) in seen
        for (table,day),record in seen.items():
            side='target' if day==oracle['target_date'] else 'baseline' if day==oracle['baseline_date'] else None
            if side:checks['oracle.'+table+'.'+side]=all(record.get(k)==v for k,v in oracle['facts'][side][table].items())
        if oracle['id']=='conflict':checks['oracle.conflict_reported']=bool(state['analysis'].get('conflicts'))
        if oracle['id']=='missing':checks['oracle.gaps_reported']=bool(state['analysis'].get('gaps'))
    return checks


def summarize(rows,skipped):
    """未运行样例不进入通过率分母；错误放行与无谓追问分别计数。"""
    groups={}
    for row in rows:
        key=row['category'];groups.setdefault(key,{'executed':0,'passed':0})
        groups[key]['executed']+=1;groups[key]['passed']+=int(row['grade']['passed'])
    routed=[r for r in rows if r.get('expected_intent')]
    labels=sorted({r['expected_intent'] for r in routed})
    per_intent={}
    for intent in labels:
        tp=sum(r['expected_intent']==intent and r['parsed'].get('intent')==intent for r in routed)
        fp=sum(r['expected_intent']!=intent and r['parsed'].get('intent')==intent for r in routed)
        fn=sum(r['expected_intent']==intent and r['parsed'].get('intent')!=intent for r in routed)
        per_intent[intent]={'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None}
    return {'executed':len(rows),'passed':sum(r['grade']['passed'] for r in rows),'failed':sum(not r['grade']['passed'] for r in rows),
        'skipped':len(skipped),'by_category':groups,'per_intent':per_intent,
        'unexpected_admissions':sum(r.get('expected_status') not in (None,'ready') and r['parsed']['status']=='ready' for r in rows),
        'unnecessary_clarifications':sum(r.get('expected_status')=='ready' and r['parsed']['status']=='needs_clarification' for r in rows)}


def grade_execution(case,parsed,state,oracles):
    """三种路由和原golden运行器共用评分，期望只进入评分器，不能进入规划提示词。"""
    expected=case['expected']
    checks=grade_case({'status':expected['status'],'expect':expected.get('parsed',{})},parsed,state)['checks']
    if expected.get('result'):checks.update(check_paths(state or {},expected['result'],'result.'))
    if expected.get('evidence'):
        observations=json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))['observations'] if state else []
        for index,assertion in enumerate(expected['evidence']):
            matches=[r for obs in observations if obs.get('data',{}).get('table')==assertion['table'] for r in obs['data'].get('records',[]) if r['date']==assertion['date']]
            checks[f'evidence.{index}']=len(matches)==1 and matches[0].get(assertion['field'],MISSING)==assertion['value']
    if case.get('oracle_ref'):checks.update(oracle_checks(case,oracles[case['oracle_ref']],state,parsed))
    return checks


def run_suite(root,dataset,output,split='regression',live_model=False,max_model_calls=12):
    """运行固定年度版本；会话状态用真实协调器，但不启动后台工具或模型执行循环。"""
    from .store_workspace import StoreHub
    from .conversations import Conversations
    root=Path(root);dataset=Path(dataset);output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    document,separation=load_suite(dataset,root/'data/intent_examples.json')
    run_id=uuid.uuid4().hex[:12]
    (output/'dataset.snapshot.json').write_bytes(dataset.read_bytes())
    if split not in ('regression','holdout','all') or type(max_model_calls) is not int or not 0<=max_model_calls<=100:raise ValueError('分组或调用预算无效。')
    version=document['fixture']['annual_version']
    if not re.fullmatch(r'[a-f0-9]{12}',version):raise ValueError('固定数据版本非法。')
    folder=root/'data/annual'/version;manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    oracles={c['id']:c for c in json.loads((folder/'answers.json').read_text(encoding='utf-8'))['cases']}
    source_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((root/'ecommerce_agent').glob('*.py')))).hexdigest()
    today=date.fromisoformat(document['business_today']);stores={};catalogs={};rows=[];skipped=[];calls=0;safety=[];hub=None
    settings=None
    if live_model:
        from .local_model import LocalModelSettings
        settings=LocalModelSettings(root)
        if not settings.public()['enabled'] or not settings.public()['configured']:raise ValueError('需启用并配置本地规划模型。')
    def planner(question,context):
        nonlocal calls
        if calls>=max_model_calls:raise ValueError('模型调用预算耗尽。')
        calls+=1;return settings.plan_sales_query(question,context)
    try:
        for case in document['cases']:
            if split!='all' and case['split']!=split:continue
            if case['execution']=='model_allowed' and (not live_model or calls>=max_model_calls):
                skipped.append({'case_id':case['id'],'reason':'model_disabled' if not live_model else 'model_budget_exhausted'});continue
            started=time.perf_counter();before_calls=calls;state=None;error=None;trajectory=[]
            sid=case['store_id']
            if sid not in stores:
                stores[sid]=DataStore(folder/(sid+'.sqlite3'));catalogs[sid]=ProductCatalog(folder/'products.json',stores[sid].skus)
            store,catalog=stores[sid],catalogs[sid]
            if case['execution']=='conversation_state':
                # 会话协调器使用独立评测工作区；不得读取用户真实聊天记录或复用上一case上下文。
                active=json.loads((root/'data/annual/active.json').read_text(encoding='utf-8'))['version']
                if active!=version:raise ValueError('多轮评测当前活动版本与固定版本不符。')
                # 每次运行用独立队列，避免反复评测留下的queued任务污染下一次成绩。
                if hub is None:hub=StoreHub(root,output/'conversation-state'/run_id,output/'conversation-runs'/run_id,start_worker=False)
                # 冻结数据集每题已显式指定store_id；旧会话评测固定单店范围，
                # 新的默认全店策略由独立跨店回归覆盖，不追溯改变旧基线的任务含义。
                manager=Conversations(hub,today=lambda:today,default_query_scope='current');doc=manager.new(sid);checks={};last=None
                for index,turn in enumerate(case['turns']):
                    try:
                        result=manager.turn(doc['id'],{'question':turn['question'],'revision':doc['revision']},f"{case['id']}-{index}")
                        doc=result['conversation'];checks.update(check_paths(result,turn['expected'],f'turn{index+1}.'))
                        if result.get('task'):last=result['task']['parsed']
                        trajectory.append({'question':turn['question'],'expected':turn['expected'],'task':result.get('task'),'conversation':doc})
                    except Exception as exc:error=type(exc).__name__;checks[f'turn{index+1}.exception']=False;break
                parsed={**(last or {}),'raw_question':' / '.join(t['question'] for t in case['turns']),
                    'business_today':today.isoformat(),'status':(last or {}).get('status','conversation_state_only'),
                    'issues':['只评会话状态；本case未执行经营工具。']}
            else:
                try:parsed=parse_task(case['question'],catalog,store,today=today,
                    query_planner=planner if live_model and case['execution']=='model_allowed' else None)
                except Exception as exc:
                    error=type(exc).__name__;parsed={'raw_question':case['question'],'business_today':today.isoformat(),
                        'status':'parser_error','intent':None,'issues':['解析失败，详见异常类型；未执行工具。']}
                try:
                    if parsed['status']=='ready':state=execute_task(store,parsed['sku_id'],parsed['target_date'],output/case['id'],task_context=parsed)
                except Exception as exc:error=type(exc).__name__
                checks=grade_execution(case,parsed,state,oracles)
                if not safety and parsed['status']=='ready':safety=security_probes(parsed,store)
            if error:checks['execution_exception']=False
            grade={'passed':all(checks.values()),'checks':checks,'failed_checks':[k for k,v in checks.items() if not v]}
            row={'case_id':case['id'],'trial':0,'category':case['category'],'split':case['split'],'grade':grade,'parsed':parsed,
                'expected_status':case.get('expected',{}).get('status'),'expected_intent':case.get('expected',{}).get('parsed',{}).get('intent'),
                'run_dir':state.get('run_dir') if state else None,'model_calls':calls-before_calls,'elapsed_ms':round((time.perf_counter()-started)*1000,2),
                'error_type':error,'trajectory':trajectory,'evaluation_scope':case['execution']}
            rows.append(row);print(json.dumps({'case':case['id'],'passed':grade['passed'],'failed':grade['failed_checks']},ensure_ascii=False),flush=True)
    finally:
        if hub:hub.close()
    summary=summarize(rows,skipped)
    report={'schema':'flower-quality-v1','run_id':run_id,'runner':'golden_live_planning' if live_model else 'golden_offline','trials':1,'case_count':len(rows),
        'all_trials_passed':summary['passed'],'model_calls':calls,'source_sha256':source_hash,'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest(),
        'fixture_sha256':{r['id']:r['content_sha256'] for r in manifest['stores']},'summary':summary,'skipped':skipped,'split':split,
        'separation_audit':separation,'safety_probes':safety,'security_gate_passed':bool(safety) and all(p['blocked'] for p in safety),
        'rows':rows,'reflection_proposals':reflection_proposals(rows),
        'limitations':['自定义花店评测，非官方Claw榜单。','会话用例只评条件继承/门店选择，未执行工具。','默认离线跳过长尾模型样例，未测不等于通过。','解释质量、建议实用性和因果措辞仍需人工复核。']}
    (output/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    definitions={c['id']:c for c in document['cases']}
    bad=[{'case_id':r['case_id'],'category':r['category'],'question':r['parsed']['raw_question'],'failed_checks':r['grade']['failed_checks'],
        'expected':definitions[r['case_id']].get('expected',definitions[r['case_id']].get('turns')),
        'why':definitions[r['case_id']]['why'],'run_dir':r['run_dir'],
        'actual_status':r['parsed']['status'],'actual_intent':r['parsed'].get('intent'),'review_status':'needs_review','auto_apply':False} for r in rows if not r['grade']['passed']]
    (output/'badcases.json').write_text(json.dumps(bad,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 花迹评测运行结果','',f"执行 {summary['executed']}，通过 {summary['passed']}，失败 {summary['failed']}，跳过 {summary['skipped']}。",'',
        '跳过不计通过；会话状态用例不代表工具执行通过；人工检查项尚未评分。','','| 案例 | 类别 | 结果 | 失败检查 |','|---|---|---|---|']
    lines += [f"| {r['case_id']} | {r['category']} | {'通过' if r['grade']['passed'] else '失败'} | {', '.join(r['grade']['failed_checks'])} |" for r in rows]
    lines += [f"| {r['case_id']} | 长尾语义 | 跳过 | {r['reason']} |" for r in skipped]
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def main():
    """默认只跑回归集、零付费；--split all初始化基线，--live-model显式开启规划API。"""
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',default='data/evaluation/golden_cases.json');parser.add_argument('--output',default='runs/golden-regression')
    parser.add_argument('--split',choices=['regression','holdout','all'],default='regression');parser.add_argument('--live-model',action='store_true');parser.add_argument('--max-model-calls',type=int,default=12)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    report=run_suite(root,args.dataset,args.output,args.split,args.live_model,args.max_model_calls)
    from .claw_eval_adapter import export_and_grade
    export_and_grade(Path(args.output)/'quality-report.json',None,Path(args.output)/'claw')
    print(json.dumps(report['summary'],ensure_ascii=False))
    if report['summary']['failed'] or not report['security_gate_passed']:raise SystemExit(1)


if __name__=='__main__':main()
