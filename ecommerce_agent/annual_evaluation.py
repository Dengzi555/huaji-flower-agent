"""年度已知场景验收：从原话走生产工具，再用独立答案核对实际取数和节日排名。

答案只在评测进程读取，不传给parse_task或ToolRegistry，不使用LLM Judge掩盖数字错误。
"""
import hashlib
import json
import time
from datetime import date
from pathlib import Path
from .data import DataStore,FIELDS
from .task_parser import ProductCatalog,parse_task
from .workflows import execute_task
from .quality_lab import security_probes,reflection_proposals


def evaluate(root):
    """返回与Claw适配器一致的逐任务报告；不调用付费模型。"""
    root=Path(root);active=json.loads((root/'data/annual/active.json').read_text(encoding='utf-8'))['version']
    folder=root/'data/annual'/active;oracle=json.loads((folder/'answers.json').read_text(encoding='utf-8'))
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    store=DataStore(folder/'hx-sz-ns.sqlite3');catalog=ProductCatalog(folder/'products.json',store.skus)
    output=root/'runs/annual-evaluation'/active;output.mkdir(parents=True,exist_ok=True)
    rows=[];safety=[]
    for case in oracle['cases']:
        started=time.perf_counter();holiday=case['id'].startswith('holiday-')
        if holiday:question=case['label']+'哪类花卖得最好'
        else:
            name=catalog.products[case['sku_id']]['short_name']
            question=name+' '+case['target_date']+(' 销量怎么样' if case['id'] in ('new_listing','zero_tail') else ' 销量下降原因')
        parsed=parse_task(question,catalog,store,today=date.fromisoformat(oracle['business_today']))
        checks={'admission':parsed['status']=='ready'};state=None;error=None
        if parsed['status']=='ready':
            try:state=execute_task(store,parsed['sku_id'],parsed['target_date'],output,task_context=parsed)
            except Exception as exc:error=type(exc).__name__
        checks['completed']=bool(state and state['status']=='completed')
        if checks['completed']:
            if holiday:
                rank=state['analysis'].get('ranking',{})
                checks['holiday_leaders']=sorted(r['name'] for r in rank.get('leaders',[]))==sorted(case['leaders'])
                checks['holiday_counts']=all(r['units_sold']==case['units_by_variety'][r['name']] for r in rank.get('rows',[]))
            else:
                evidence=json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))
                observed={}
                for item in evidence['observations']:
                    data=item.get('data',{});table=data.get('table')
                    if table in FIELDS:
                        for record in data.get('records',[]):observed[table,record['date']]=record
                checks['sales_evidence']=('sales',case['target_date']) in observed
                for (table,day),record in observed.items():
                    side='target' if day==case['target_date'] else 'baseline' if day==case['baseline_date'] else None
                    if not side:continue
                    expected=case['facts'][side][table]
                    # 取数工具可增加来源说明，但原始字段值必须逐项一致。
                    checks[table+'-'+side]=all(record.get(k)==v for k,v in expected.items())
                if case['id']=='conflict':checks['conflict_reported']=bool(state['analysis'].get('conflicts'))
                if case['id']=='missing':checks['missing_reported']=bool(state['analysis'].get('gaps'))
            checks['no_causal_overclaim']=state['analysis'].get('causal_root_cause','not_established')=='not_established'
        if error:checks['execution_exception']=False
        grade={'passed':all(checks.values()),'checks':checks,'failed_checks':[k for k,v in checks.items() if not v]}
        rows.append({'case_id':case['id'],'trial':0,'grade':grade,'parsed':parsed,'run_dir':state['run_dir'] if state else None,
            'elapsed_ms':round((time.perf_counter()-started)*1000,2),'model_calls':0,'error_type':error})
        if not safety and parsed['status']=='ready':safety=security_probes(parsed,store)
        print(json.dumps({'case':case['id'],'passed':grade['passed'],'failed_checks':grade['failed_checks']},ensure_ascii=False),flush=True)
    report={'schema':'flower-quality-v1','runner':'annual_known_scenario_tools','model_calls':0,'trials':1,'case_count':len(rows),
        'dataset_sha256':hashlib.sha256((folder/'answers.json').read_bytes()).hexdigest(),
        'source_sha256':hashlib.sha256(b''.join(p.read_bytes() for p in sorted((root/'ecommerce_agent').glob('*.py')))).hexdigest(),
        'fixture_sha256':{m['id']:m['content_sha256'] for m in manifest['stores']},'all_trials_passed':sum(r['grade']['passed'] for r in rows),
        'safety_probes':safety,'security_gate_passed':all(p['blocked'] for p in safety),'rows':rows,
        'reflection_proposals':reflection_proposals(rows),'limitations':['自定义年度合成场景，未验证真实经营效果。','检查任务准入、工具取数、节日排名与缺口/冲突标记；不等于全面语义或因果评测。']}
    (output/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report,output


def main():
    """运行年度场景并导出真正Claw核心格式；使用仓库内固定快照。"""
    root=Path(__file__).resolve().parents[1];report,output=evaluate(root)
    from .claw_eval_adapter import export_and_grade
    result=export_and_grade(output/'quality-report.json',None,output/'claw')
    print(json.dumps({'cases':len(report['rows']),'passed':report['all_trials_passed'],'report':str(output/'quality-report.json')},ensure_ascii=False))
    if report['all_trials_passed']!=report['case_count'] or not result['security_gate_passed']:raise SystemExit(1)


if __name__=='__main__':main()
