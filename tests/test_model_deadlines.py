"""验证真实等待截止：包括没有外部取消令牌、持续流式输出和用户取消优先。"""
import io
import json
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.errors import AgentError
from ecommerce_agent.cancellation import CancelToken,CancelledError,cancel_scope
from ecommerce_agent.model_runtime import model_scope
from ecommerce_agent.model_deadlines import deadline_scope,TIMEOUTS


class DeadlineTests(unittest.TestCase):
    def client(self,purpose='execution'):
        client=ChatProvider({'api_base_url':'http://127.0.0.1:1234','model':'local'})
        client.purpose=purpose;return client

    def test_purpose_limits_and_shorter_caller_timeout_are_respected(self):
        for purpose,limit in TIMEOUTS.items():
            with self.subTest(purpose=purpose):
                waits=[]
                def http(request,timeout):
                    waits.append(timeout)
                    return io.BytesIO(json.dumps({'choices':[{'message':{'content':'ok'}}]}).encode())
                with patch('urllib.request.urlopen',side_effect=http):
                    self.client(purpose).complete([{'role':'user','content':'hi'}],[],60)
                    self.client(purpose).complete([{'role':'user','content':'hi'}],[],.5)
                self.assertTrue(0<waits[0]<=limit)
                self.assertTrue(0<waits[1]<=.5)

    def test_blocking_read_stops_without_external_cancel_token_and_discards_late_result(self):
        release=threading.Event();done=threading.Event()
        class Slow(io.BytesIO):
            def read(self,*args):
                release.wait(2);done.set()
                return b'{"choices":[{"message":{"content":"late"}}]}'
        client=self.client();started=time.monotonic()
        try:
            with model_scope() as ledger,patch('urllib.request.urlopen',return_value=Slow()):
                with self.assertRaises(AgentError) as caught:client.complete([{'role':'user','content':'hi'}],[],.05)
                self.assertEqual(caught.exception.code,'model_timeout')
                self.assertEqual(ledger.snapshot()['records'][0]['status'],'failed')
            self.assertLess(time.monotonic()-started,1)
            self.assertIsNone(client.last_usage)
        finally:release.set();done.wait(1)

    def test_continuously_active_stream_still_has_total_deadline(self):
        class Trickle:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def close(self):pass
            def __iter__(self):return self
            def __next__(self):
                time.sleep(.01)
                return b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
        parts=[];started=time.monotonic()
        with patch('urllib.request.urlopen',return_value=Trickle()) as http:
            with self.assertRaises(AgentError) as caught:self.client('chat').stream_text([{'role':'user','content':'hi'}],parts.append,.1)
        self.assertEqual(caught.exception.code,'model_timeout');self.assertTrue(parts)
        self.assertLess(time.monotonic()-started,1);self.assertEqual(http.call_count,1)
        count=len(parts);time.sleep(.05);self.assertEqual(len(parts),count)

    def test_user_cancel_is_not_relabelled_as_timeout(self):
        parent=CancelToken()
        with cancel_scope(parent):
            with self.assertRaises(CancelledError):
                with deadline_scope(.01):parent.cancel();time.sleep(.02)

    def test_investigation_keeps_evidence_when_custom_client_ignores_timeout(self):
        from ecommerce_agent.runner import run
        from ecommerce_agent.data import DataStore
        from ecommerce_agent.flower_business_data import business_data
        release=threading.Event();done=threading.Event();limits=[]
        class Client:
            def complete(self,messages,tools,timeout):
                limits.append(timeout)
                if len(limits)==1:
                    return {'role':'assistant','tool_calls':[{'id':'sales','function':{'name':'get_sales_metrics','arguments':'{}'}}]}
                release.wait(2);done.set();return {'role':'assistant','content':'late'}
        try:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);data=root/'data.json';data.write_text(json.dumps(business_data()),encoding='utf-8')
                state=run(DataStore(data),'FLR-ROSE-99-BOUQUET','2026-10-25',root/'runs',provider=Client(),timeout_seconds=.3)
                evidence=json.loads((Path(state['run_dir'])/'evidence.json').read_text())
                self.assertEqual(state['error_info']['code'],'model_timeout')
                self.assertEqual(len(evidence['observations']),1)
                self.assertEqual(len(limits),2);self.assertTrue(all(0<v<=.3 for v in limits))
        finally:release.set();done.wait(1)


if __name__=='__main__':unittest.main()
