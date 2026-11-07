"""评测平台回归：持久化进度、取消、错误参数、同条件比较和HTTP访问边界。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ecommerce_agent.eval_center import EvalCenter
from ecommerce_agent.cancellation import checkpoint
from ecommerce_agent.web import create_server

ROOT=Path(__file__).resolve().parents[1]


def fake_runner(root,dataset,output,split,live,budget,*,on_progress):
    """执行器替身保留实际平台协议，测试不发出模型请求。"""
    on_progress({'case_id':'sales-single','status':'passed','row':{'parsed':{'raw_question':'销量怎么样'},'grade':{'checks':{'scope':True},'failed_checks':[]},'elapsed_ms':1}})
    on_progress({'case_id':'failed-example','status':'failed','row':{'grade':{'checks':{'date':False},'failed_checks':['date']},'elapsed_ms':2}})
    on_progress({'case_id':'model-example','status':'skipped','reason':'model_disabled'})
    return {'summary':{'executed':2,'passed':1,'failed':1,'skipped':1},'model_calls':0,'security_gate_passed':True,'source_sha256':'test','separation_audit':{}}


class EvalCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.center=EvalCenter(ROOT,self.temp.name,runner=fake_runner);self.addCleanup(self.center.close)
        self.claw=patch('ecommerce_agent.claw_eval_adapter.export_and_grade',return_value={'cases':2});self.claw.start();self.addCleanup(self.claw.stop)

    def finish(self,job):
        self.center.thread.join(timeout=5)
        self.assertFalse(self.center.thread.is_alive())
        return self.center.get(job['id'])

    def test_dataset_create_progress_and_review_persist(self):
        self.assertEqual(len(self.center.dataset()['cases']),92)
        job=self.finish(self.center.create({'label':'修改简称'}))
        self.assertEqual(job['status'],'completed');self.assertEqual(job['summary']['failed'],1)
        self.assertEqual([r['status'] for r in job['cases']],['passed','failed','skipped'])
        detail=self.center.case_detail(job['id'],'sales-single')
        self.assertIn('expected',detail['definition']);self.assertIsNone(detail['evidence'])
        self.center.review(job['id'],{'case_id':'failed-example','status':'confirmed','note':'日期解析错误'})
        again=EvalCenter(ROOT,self.temp.name,runner=fake_runner);self.addCleanup(again.close)
        self.assertEqual(again.get(job['id'])['reviews']['failed-example']['note'],'日期解析错误')
        self.assertEqual(again.list()[0]['done'],3)

    def test_invalid_inputs_and_paths_are_rejected(self):
        for payload in ({'dataset':'../../secret'},{'mode':'shell'},{'max_model_calls':True},{'split':'other'},{'mode':'offline','max_model_calls':3},{'timeout_seconds':9999}):
            with self.assertRaises(ValueError):self.center.create(payload)
        with self.assertRaises(ValueError):self.center.get('../config.local.json')
        job=self.finish(self.center.create({}))
        with self.assertRaises(ValueError):self.center.case_detail(job['id'],'../../config.local.json')
        with self.assertRaises(ValueError):self.center.review(job['id'],{'case_id':'sales-single','status':'confirmed','note':''})

    def test_changed_source_requires_restart_before_next_run(self):
        with patch('ecommerce_agent.eval_center.code_digest',return_value='changed'):
            with self.assertRaisesRegex(ValueError,'重启'):self.center.create({})

    def test_cancel_preserves_progress_and_blocks_second_job(self):
        entered=threading.Event()
        def slow(*args,on_progress):
            on_progress({'case_id':'sales-single','status':'passed'});entered.set()
            while True:checkpoint();time.sleep(.01)
        self.center.runner=slow
        job=self.center.create({});self.assertTrue(entered.wait(2))
        with self.assertRaises(ValueError):self.center.create({})
        self.center.cancel(job['id']);done=self.finish(job)
        self.assertEqual(done['status'],'cancelled');self.assertEqual(len(done['cases']),1)

    def test_restart_marks_running_interrupted(self):
        job=self.finish(self.center.create({}));job['status']='running';self.center.save(job)
        again=EvalCenter(ROOT,self.temp.name,runner=fake_runner);self.addCleanup(again.close)
        self.assertEqual(again.get(job['id'])['status'],'interrupted')

    def test_comparison_deltas_and_compatibility(self):
        a=self.finish(self.center.create({}));b=self.finish(self.center.create({}))
        b['cases'][0]['status']='failed';b['cases'][1]['status']='passed';self.center.save(b)
        diff=self.center.compare(a['id'],b['id'])
        self.assertEqual(diff['counts'],{'fixed':1,'regressed':1,'unchanged':0,'skipped':1})
        for key,value in [('split','holdout'),('dataset_sha256','different'),('source_unchanged',False),('status','cancelled'),('scorer_sha256','different')]:
            changed={**b,key:value};self.center.save(changed)
            with self.assertRaises(ValueError):self.center.compare(a['id'],b['id'])
        self.center.save(b)

    def test_failure_keeps_partial_results(self):
        def broken(*args,on_progress):
            on_progress({'case_id':'one','status':'passed'});raise RuntimeError('internal detail')
        self.center.runner=broken;job=self.finish(self.center.create({}))
        self.assertEqual(job['status'],'failed');self.assertEqual(len(job['cases']),1)
        self.assertNotIn('internal detail',job['message'])

    def test_http_auth_launch_detail_and_validation(self):
        service=SimpleNamespace(database=Path(self.temp.name)/'tasks.sqlite3')
        server=create_server(service,port=0,token='test-access',eval_center=self.center)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urllib.request.urlopen(base+'/eval') as response:self.assertIn('评测中心',response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(base+'/api/eval/dataset')
            self.assertEqual(error.exception.code,401)
            headers={'Authorization':'Bearer test-access','Content-Type':'application/json'}
            request=urllib.request.Request(base+'/api/eval/jobs',data=b'{}',headers=headers)
            with urllib.request.urlopen(request) as response:job=json.load(response);self.assertEqual(response.status,202)
            self.finish(job)
            with urllib.request.urlopen(urllib.request.Request(base+'/api/eval/jobs/'+job['id'],headers=headers)) as response:self.assertEqual(json.load(response)['status'],'completed')
            request=urllib.request.Request(base+'/api/eval/jobs',data=b'{"dataset":"/secret"}',headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,400)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
