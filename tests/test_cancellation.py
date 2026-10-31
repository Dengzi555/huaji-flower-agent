"""取消语义回归：排队/执行/模型在途/完成竞态/会话规划，全部使用隔离数据与可控事件。"""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ecommerce_agent.cancellation import CancelToken,CancelledError,cancel_scope,current_token,checkpoint,RequestCancellations
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.data import DataStore
from ecommerce_agent.runner import run
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.web import create_server
from ecommerce_agent.conversations import Conversations


class CancellationTests(unittest.TestCase):
    """用Event控制顺序，避免靠固定sleep碰运气重现停止与完成竞争。"""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data=self.root/'data.json';self.catalog=self.root/'products.json'
        self.data.write_text(json.dumps(business_data()),encoding='utf-8')
        self.catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')

    def service(self,**options):
        service=InvestigationService(self.data,self.catalog,self.root/'state',self.root/'runs',**options)
        self.addCleanup(service.close);return service

    def settled(self,service,identity):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            task=service.get(identity)
            if task['status'] not in ('queued','running','cancelling'):return task
            time.sleep(.01)
        self.fail('任务未及时进入终态')

    def test_child_stop_isolated_and_parent_stops_all_children(self):
        parent=CancelToken();a=CancelToken(parent);b=CancelToken(parent)
        a.cancel();self.assertFalse(parent.event.is_set());self.assertFalse(b.event.is_set())
        parent.cancel();self.assertTrue(b.event.is_set())

    def test_cancel_after_first_tool_keeps_evidence_and_starts_no_more_tools(self):
        token=CancelToken();events=[]
        def observe(event):
            events.append(event)
            if event['type']=='tool_completed':token.cancel()
        with cancel_scope(token):state=run(DataStore(self.data),'FLR-ROSE-99-BOUQUET','2026-10-25',self.root/'out',on_event=observe)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(state['tool_calls'],1)
        self.assertEqual(len(json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))['observations']),1)
        self.assertIsNone(state['model_draft'])

    def test_late_model_tool_request_cannot_execute_after_stop(self):
        entered=threading.Event();release=threading.Event();token=CancelToken();result={}
        class SlowProvider:
            def complete(self,*args):
                entered.set();release.wait(3)
                return {'tool_calls':[{'id':'late','function':{'name':'get_sales_metrics','arguments':'{}'}}]}
        def work():
            with cancel_scope(token):result['state']=run(DataStore(self.data),'FLR-ROSE-99-BOUQUET','2026-10-25',self.root/'out',provider=SlowProvider())
        thread=threading.Thread(target=work);thread.start();self.assertTrue(entered.wait(2))
        token.cancel();thread.join(1);release.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['state']['status'],'cancelled')
        self.assertEqual(result['state']['tool_calls'],0)

    def test_blocked_http_open_cancels_without_waiting_for_response(self):
        token=CancelToken();entered=threading.Event();release=threading.Event();errors=[]
        client=ChatProvider({'api_base_url':'http://localhost:1','model':'test'})
        def opening(*args,**kwargs):entered.set();release.wait(3);return io.BytesIO(b'{}')
        def work():
            try:
                with cancel_scope(token):client.complete([],[],2)
            except Exception as exc:errors.append(exc)
        with patch('urllib.request.urlopen',side_effect=opening):
            thread=threading.Thread(target=work);thread.start();self.assertTrue(entered.wait(1))
            token.cancel();thread.join(1);release.set()
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0],CancelledError)
        self.assertEqual(token.details()['model_requests_started'],1)
        self.assertFalse(token.details()['cost_confirmed'])

    def test_stream_preserves_received_part_and_discards_late_delta(self):
        token=CancelToken();first=threading.Event();release=threading.Event();chunks=[];errors=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def close(self):pass
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n'
                release.wait(3)
                yield b'data: {"choices":[{"delta":{"content":"late"}}]}\n'
                yield b'data: [DONE]\n'
        client=ChatProvider({'api_base_url':'http://localhost:1','model':'test'})
        def delta(text):chunks.append(text);first.set()
        def work():
            try:
                with cancel_scope(token):client.stream_text([],delta,2)
            except Exception as exc:errors.append(exc)
        with patch('urllib.request.urlopen',return_value=Response()):
            thread=threading.Thread(target=work);thread.start();self.assertTrue(first.wait(1))
            token.cancel();thread.join(1);release.set()
        self.assertFalse(thread.is_alive());self.assertEqual(chunks,['first']);self.assertIsInstance(errors[0],CancelledError)

    def test_queued_stop_is_idempotent_and_survives_restart(self):
        service=self.service(start_worker=False)
        task=service.create({'question':'炽热爱意10月25日销量怎么样'})['task']
        self.assertTrue(service.cancel(task['id'])['accepted'])
        self.assertEqual(service.cancel(task['id'])['task']['status'],'cancelled')
        service.close()
        reopened=self.service(start_worker=False)
        self.assertEqual(reopened.get(task['id'])['status'],'cancelled')
        self.assertEqual(reopened.work.qsize(),0)

    def test_cancel_wins_over_late_completed_result(self):
        entered=threading.Event();release=threading.Event()
        def runner(*args,**kwargs):entered.set();release.wait(2);return {'status':'completed','analysis':None}
        service=self.service(runner=runner)
        task=service.create({'question':'炽热爱意10月25日销量怎么样'})['task'];self.assertTrue(entered.wait(1))
        requested=service.cancel(task['id']);self.assertEqual(requested['task']['status'],'cancelling')
        release.set();final=self.settled(service,task['id'])
        self.assertEqual(final['status'],'cancelled');self.assertEqual(final['result']['status'],'cancelled')
        self.assertEqual(final['error_info']['code'],'request_cancelled')

    def test_completed_result_is_not_rewritten_by_late_stop(self):
        service=self.service(runner=lambda *a,**k:{'status':'completed','analysis':None})
        task=service.create({'question':'炽热爱意10月25日销量怎么样'})['task'];self.settled(service,task['id'])
        result=service.cancel(task['id']);self.assertFalse(result['accepted']);self.assertEqual(result['task']['status'],'completed')

    def test_pre_cancel_request_has_no_task_or_tool_and_is_replayable(self):
        service=self.service(start_worker=False);manager=Conversations(service,today=lambda:date(2026,10,29))
        doc=manager.new('local');registry=RequestCancellations();token=registry.cancel(doc['id'],'request-one')
        payload={'question':'炽热爱意10月25日销量怎么样','revision':0}
        result=manager.turn(doc['id'],payload,'request-one',cancel_token=token)
        self.assertIsNone(result['task']);self.assertEqual(result['conversation']['turns'][-1]['kind'],'cancelled')
        self.assertEqual(service.work.qsize(),0)
        replay=manager.turn(doc['id'],payload,'request-one',cancel_token=token)
        self.assertTrue(replay['reused']);self.assertEqual(len(replay['conversation']['turns']),1)

    def test_http_stop_planning_does_not_wait_for_conversation_lock(self):
        service=self.service(start_worker=False);entered=threading.Event();result={}
        def planner(*args):
            entered.set()
            while not current_token().event.wait(.01):pass
            checkpoint()
        service.query_planner=planner
        server=create_server(service,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def post(path,payload,key=None):
            headers={'Content-Type':'application/json'}
            if key:headers['Idempotency-Key']=key
            return json.load(urllib.request.urlopen(urllib.request.Request(base+path,json.dumps(payload).encode(),headers),timeout=4))
        try:
            doc=post('/api/conversations',{'store_id':'local'})['conversation']
            def submit():result.update(post(f"/api/conversations/{doc['id']}/turns",{'question':'炽热爱意10月25日帮我专题研判','revision':0},'slow-request'))
            request=threading.Thread(target=submit);request.start();self.assertTrue(entered.wait(2))
            stopped=post(f"/api/conversations/{doc['id']}/cancel",{'request_id':'slow-request'})
            self.assertTrue(stopped['accepted']);request.join(1)
            self.assertFalse(request.is_alive());self.assertIsNone(result['task'])
            self.assertEqual(result['conversation']['turns'][-1]['kind'],'cancelled')
            self.assertEqual(service.work.qsize(),0)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
