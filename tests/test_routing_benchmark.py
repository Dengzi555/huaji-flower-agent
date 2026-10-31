"""路由对比的评分器也需要回归：公平路径、零调用基线、真实ledger、失败与跳过分母。"""
import io
import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch
from ecommerce_agent.data import DataStore,FIELDS
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.model_runtime import model_scope
from ecommerce_agent.routing_benchmark import CallBudget,grade,summarize_group,run_benchmark
from ecommerce_agent.claw_eval_adapter import export_and_grade
from ecommerce_agent.golden_evaluation import grade_execution

ROOT=Path(__file__).resolve().parents[1]
# 固定快照随仓库分发；缺少依赖时跳过，CI 会安装 pydantic/pyyaml 后执行。
CLAW=ROOT/'vendor/claw-eval'
try:
    import pydantic  # noqa: F401
    import yaml  # noqa: F401
    CLAW_READY=(CLAW/'src/claw_eval/models/task.py').exists()
except ImportError:
    CLAW_READY=False


class RoutingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)
        self.question='炽热爱意10月25日销量怎么样'

    def parse(self,policy,planner,retriever=None,question=None):
        return parse_task(question or self.question,self.catalog,self.store,today=date(2026,10,29),
            query_planner=planner,intent_retriever=retriever,planning_policy=policy)

    def proposal(self):
        return dict(decision='select',intent='query_sales_performance',selector='炽热爱意',start_date='2026-10-25',
            end_date='2026-10-25',metric='units_sold',direction='none',group_by='sku',limit=5)

    def benchmark_fixture(self,folder,document):
        """用随仓库分发的小快照构建临时年度接口，不依赖被忽略的本地整年数据库。

        测试仍执行真实SQLite只读适配、工具、评分器和Claw导出，仅模型HTTP使用替身。
        这两条样例没有年度oracle断言，因此不能借用本机answers.json充当测试条件。
        """
        root=folder/'fixture-root';version='0'*12;annual=root/'data/annual'/version
        annual.mkdir(parents=True);(root/'ecommerce_agent').mkdir()
        for source in (ROOT/'ecommerce_agent').glob('*.py'):
            (root/'ecommerce_agent'/source.name).write_bytes(source.read_bytes())
        (root/'data/intent_examples.json').write_bytes((ROOT/'data/intent_examples.json').read_bytes())
        (annual/'products.json').write_bytes((ROOT/'data/products.json').read_bytes())
        source_store=DataStore(ROOT/'data/stores/hx-sz-ns.json');snapshot=source_store.snapshot()
        metadata={'schema':'flower-annual-sqlite-v1','meta':{k:v for k,v in snapshot.items() if k not in FIELDS},
            'skus':sorted(source_store.skus),'dates':{table:sorted({r['date'] for r in snapshot.get(table,[])}) for table in FIELDS},
            'content_sha256':hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()}
        with closing(sqlite3.connect(annual/'hx-sz-ns.sqlite3')) as db, db:
            db.executescript('CREATE TABLE records(domain TEXT,sku TEXT,day TEXT,body TEXT,PRIMARY KEY(domain,sku,day)); CREATE TABLE metadata(key TEXT,value TEXT);')
            for table in FIELDS:
                db.executemany('INSERT INTO records VALUES(?,?,?,?)',[(table,r['sku_id'],r['date'],json.dumps(r)) for r in snapshot.get(table,[])])
            db.execute('INSERT INTO metadata VALUES(?,?)',('dataset',json.dumps(metadata)))
        manifest={'version':version,'stores':[{'id':'hx-sz-ns','content_sha256':metadata['content_sha256']}],
            'fixture':'small_offline_test_snapshot'}
        (annual/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        (annual/'answers.json').write_text(json.dumps({'cases':[]}),encoding='utf-8')
        document['fixture']['annual_version']=version
        return root

    def test_rules_and_hybrid_shortcut_zero_calls_direct_calls_model_without_retrieval(self):
        calls=[]
        def planner(question,context):calls.append(context);return self.proposal()
        def forbidden(question):raise AssertionError('直接模型基线不能偷用向量候选')
        for policy in ('rules_only','hybrid'):
            self.assertEqual(self.parse(policy,planner)['status'],'ready');self.assertEqual(calls,[])
        direct=self.parse('direct_model',planner,forbidden)
        self.assertEqual(direct['status'],'ready');self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['intent_retrieval']['status'],'disabled_for_direct_model')
        self.assertNotIn('expected',calls[0]);self.assertNotIn('oracle',calls[0])

    def test_direct_model_clarification_does_not_silently_return_original_ready(self):
        task=self.parse('direct_model',lambda *a:{**self.proposal(),'decision':'clarify'})
        self.assertEqual(task['status'],'needs_clarification')

    def test_direct_model_cannot_change_known_date_or_bypass_write_boundary(self):
        task=self.parse('direct_model',lambda *a:{**self.proposal(),'start_date':'2026-10-24','end_date':'2026-10-24'})
        self.assertEqual(task['status'],'failed');self.assertEqual(task['error_info']['code'],'plan_validation_failed')
        def forbidden(*args):raise AssertionError('写动作应在调用前拦截')
        task=self.parse('direct_model',forbidden,question='把炽热爱意10月25日价格改成50元')
        self.assertEqual(task['status'],'unsupported')

    def test_budget_failure_counts_attempt_and_empty_grade_never_passes(self):
        budget=CallBudget(1);budget.take()
        with self.assertRaises(ValueError):budget.take()
        self.assertEqual(budget.used,1);self.assertFalse(grade({})['passed'])

    def test_unknown_usage_and_skips_not_counted_as_passes(self):
        with model_scope() as ledger:runtime=ledger.snapshot()
        row={'grade':grade({'sku_id':False}),'expected_status':'ready','expected_intent':'query_inventory',
            'parsed':{'status':'needs_clarification','intent':'query_promotions'},'model_calls':0,'model_runtime':runtime,
            'elapsed_ms':20,'planning_ms':10,'execution_status':None}
        summary=summarize_group([row],[{'case_id':'skipped'}])
        self.assertEqual(summary['executed'],1);self.assertEqual(summary['skipped'],1)
        self.assertEqual(summary['pass_rate'],0);self.assertEqual(summary['slot_accuracy'],0)
        self.assertEqual(summary['intent_accuracy'],0);self.assertEqual(summary['unnecessary_clarifications'],1)

    def test_model_failure_without_analysis_is_graded_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as folder:
            checks=grade_execution({'expected':{'status':'ready'}},
                {'status':'ready','query_plan':{'steps':[]}},
                {'status':'failed','analysis':None,'run_dir':folder},{})
        self.assertFalse(checks['completed']);self.assertFalse(checks['evidence_written'])

    @unittest.skipUnless(CLAW_READY,'需要仓库内Claw-Eval快照与 pydantic/pyyaml')
    def test_claw_preserves_unknown_usage_in_audit_and_summary(self):
        unknown={'prompt_tokens':None,'completion_tokens':None,'total_tokens':None}
        row={'case_id':'unknown-usage','trial':0,'parsed':{'raw_question':'测试','business_today':'2026-10-29','issues':[]},
            'run_dir':None,'elapsed_ms':1,'model_calls':1,'model_runtime':{'reported':unknown},
            'grade':{'passed':False,'checks':{'model_response':False},'failed_checks':['model_response']}}
        report={'rows':[row],'runner':'fixture','trials':1,'model_calls':1,'security_gate_passed':True,
            'source_sha256':'source','dataset_sha256':'dataset'}
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path=folder/'report.json';path.write_text(json.dumps(report),encoding='utf-8')
            result=export_and_grade(path,CLAW,folder/'claw')
            trace=[json.loads(line) for line in (folder/'claw/unknown-usage-0.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIsNone(result['model_usage']['total_tokens']);self.assertEqual(result['usage_unknown_trials'],1)
        audits=[e for e in trace if e.get('service_name')=='flower']
        self.assertTrue(audits[0]['audit_data']['trace_token_fields_contain_unknown_placeholders'])

    @unittest.skipUnless(CLAW_READY,'需要仓库内Claw-Eval快照与 pydantic/pyyaml')
    def test_small_live_fixture_compares_three_paths_and_exports_actual_claw(self):
        document=json.loads((ROOT/'data/evaluation/golden_cases.json').read_text(encoding='utf-8'))
        document['cases']=[c for c in document['cases'] if c['id'] in ('sales-single','deny-price')]
        proposal=self.proposal()
        class Settings:
            def public(self):return {'enabled':True,'configured':True}
            def plan_sales_query(self,question,context):
                client=ChatProvider({'api_base_url':'http://127.0.0.1:1','model':'test','max_output_tokens':384})
                client.purpose='planning'
                reply=client.complete([{'role':'user','content':question}],[],1)
                result=json.loads(reply['tool_calls'][0]['function']['arguments'])
                return {**result,'usage':client.last_usage,'model':client.model}
        def response(*args,**kwargs):
            message={'tool_calls':[{'id':'plan','function':{'name':'plan_sales_query','arguments':json.dumps(proposal)}}]}
            return io.BytesIO(json.dumps({'choices':[{'message':message}],'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}}).encode())
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);fixture_root=self.benchmark_fixture(folder,document)
            dataset=folder/'suite.json';dataset.write_text(json.dumps(document),encoding='utf-8')
            with patch('urllib.request.urlopen',side_effect=response) as http:
                report=run_benchmark(fixture_root,dataset,folder/'output',live=True,settings=Settings(),retriever=lambda q:{'status':'fixture','candidates':[]})
            self.assertEqual(http.call_count,1)
            self.assertEqual(len(report['rows']),6)
            for policy in ('rules_only','direct_model','hybrid'):
                self.assertEqual(report['summaries'][policy]['passed'],2)
            self.assertEqual(report['summaries']['direct_model']['usage']['reported']['total_tokens'],50)
            self.assertEqual(report['paired'][0]['paired_cases'],2)
            claw=export_and_grade(folder/'output/report.json',CLAW,folder/'claw')
            self.assertEqual(claw['cases'],6);self.assertEqual(claw['model_usage']['total_tokens'],50)
            self.assertTrue(all(m['pass_hat_3'] is None for m in claw['metrics'].values()))
            with self.assertRaises(ValueError):run_benchmark(fixture_root,dataset,folder/'output')


if __name__=='__main__':unittest.main()
