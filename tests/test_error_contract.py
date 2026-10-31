"""统一错误契约回归：故障不追问，真缺信息才追问，原始错误与凭据不得泄露。"""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import threading
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ecommerce_agent.errors import AgentError,from_exception
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.conversations import Conversations


class ErrorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store=DataStore.from_document(business_data())
        cls.catalog=ProductCatalog.from_document(business_catalog(),cls.store.skus)

    def parse(self,question,planner=None):
        return parse_task(question,self.catalog,self.store,today=date(2026,10,29),query_planner=planner,
            intent_retriever=lambda q:{'status':'test','candidates':[]})

    def test_http_codes_are_typed_and_provider_body_never_leaks(self):
        client=ChatProvider({'api_base_url':'http://localhost:1','model':'test'})
        for status,code in [(401,'model_authentication'),(403,'model_authentication'),(402,'model_quota_exceeded'),
                            (429,'model_rate_limited'),(500,'model_unavailable'),(503,'model_unavailable'),(504,'model_timeout')]:
            error=urllib.error.HTTPError('http://localhost:1',status,'SECRET_BODY',{'Retry-After':'17'},io.BytesIO(b'SECRET_BODY'))
            with self.subTest(status=status),patch('urllib.request.urlopen',side_effect=error):
                with self.assertRaises(AgentError) as caught:client.complete([],[],1)
                public=caught.exception.public()
                self.assertEqual(public['code'],code)
                self.assertNotIn('SECRET',json.dumps(public))
                self.assertEqual(public['retry_after_seconds'],17)

    def test_stream_authentication_is_not_collapsed_into_connection_error(self):
        client=ChatProvider({'api_base_url':'http://localhost:1','model':'test'})
        error=urllib.error.HTTPError('http://localhost:1',401,'SECRET_BODY',{},None)
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(AgentError) as caught:client.stream_text([],lambda text:None,1)
        self.assertEqual(caught.exception.code,'model_authentication')

    def test_network_failure_types_remain_distinct(self):
        self.assertEqual(from_exception(TimeoutError('SECRET')).code,'model_timeout')
        self.assertEqual(from_exception(urllib.error.URLError(TimeoutError())).code,'model_timeout')
        self.assertEqual(from_exception(urllib.error.URLError('SECRET')).code,'model_connection')

    def test_planner_timeout_is_failed_not_a_request_for_dates(self):
        calls=[]
        def planner(*args):calls.append(1);raise TimeoutError('SECRET')
        task=self.parse('炽热爱意10月25日帮我做个专题研判',planner)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error_info']['code'],'model_timeout')
        self.assertEqual(task['error_info']['phase'],'planning')
        self.assertEqual(task['target_date'],'2026-10-25')
        self.assertEqual(task['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertFalse(task['error_info']['data_queried'])
        self.assertTrue(task['error_info']['retryable'])
        self.assertEqual(len(calls),1)
        self.assertNotIn('补充',task['error_info']['title'])
        self.assertNotIn('SECRET',json.dumps(task))

    def test_invalid_plan_on_complete_input_is_a_validation_failure(self):
        def planner(*args):return dict(decision='select',intent='query_inventory',selector='炽热爱意',
            start_date='2026-10-24',end_date='2026-10-24',metric='units_sold',direction='none',limit=5)
        task=self.parse('炽热爱意10月25日帮我做个专题研判',planner)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error_info']['code'],'plan_validation_failed')

    def test_real_missing_information_still_asks_user(self):
        task=self.parse('炽热爱意销量怎么样')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertEqual(task['error_info']['code'],'needs_information')
        self.assertFalse(task['error_info']['retryable'])
        self.assertEqual(task['error_info']['recovery_actions'][0]['type'],'clarify')

    def test_data_and_capability_are_not_reclassified_as_model_failures(self):
        task=self.parse('父亲节哪类花卖得最好')
        self.assertEqual(task['error_info']['code'],'data_missing')
        task=self.parse('10月25日推广费为什么超标')
        self.assertEqual(task['status'],'unsupported')
        self.assertEqual(task['error_info']['code'],'capability_unsupported')
        task=self.parse('帮我退款')
        self.assertEqual(task['error_info']['code'],'permission_denied')

    def test_service_failure_is_not_queued_and_keeps_conversation_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data.json';catalog=root/'products.json'
            data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
            service=InvestigationService(data,catalog,root/'state',root/'runs',start_worker=False)
            try:
                manager=Conversations(service,today=lambda:date(2026,10,29));doc=manager.new('local')
                first=manager.turn(doc['id'],{'question':'炽热爱意10月25日销量怎么样','revision':0},'one');before=copy.deepcopy(first['conversation']['stores'])
                def fail(*args):raise TimeoutError('SECRET')
                service.query_planner=fail
                second=manager.turn(doc['id'],{'question':'炽热爱意10月25日谁更受欢迎','revision':1},'two')
                self.assertEqual(second['task']['status'],'failed')
                self.assertEqual(second['task']['error_info']['code'],'model_timeout')
                self.assertEqual(second['conversation']['stores'],before)
                self.assertEqual(second['conversation']['waiting'],[])
                self.assertEqual(service.work.qsize(),1)  # 只有第一条成功准入的任务。
                manager.chat_responder=lambda *args:fail()
                chat=manager.turn(doc['id'],{'question':'聊聊花艺灵感','revision':2},'three')
                self.assertEqual(chat['conversation']['turns'][-1]['chat']['error_info']['code'],'model_timeout')
                self.assertEqual(chat['conversation']['stores'],before)
            finally:service.close()

    def test_api_model_auth_is_502_not_workspace_401(self):
        """供应商鉴权失败应打开模型设置，不能诱导用户重复输入工作区口令。"""
        from ecommerce_agent.web import create_server
        class Settings:
            def test(self):raise AgentError('model_authentication',phase='connection_test')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data.json';catalog=root/'products.json'
            data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
            service=InvestigationService(data,catalog,root/'state',root/'runs',start_worker=False)
            server=create_server(service,port=0,model_settings=Settings())
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/model-test',b'{}',{'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code,502)
                body=json.loads(caught.exception.read())
                self.assertEqual(body['error_info']['code'],'model_authentication')
                self.assertEqual(body['error_info']['recovery_actions'][0]['type'],'open_settings')
            finally:server.shutdown();server.server_close();thread.join();service.close()


if __name__=='__main__':unittest.main()
