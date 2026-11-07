"""离线验证网络重试：预算逐次记账、取消、限流等待和流式输出边界，不调用真实模型。"""
import io
import json
import ssl
import unittest
import urllib.error
from datetime import datetime,timedelta,timezone
from email.utils import format_datetime
from unittest.mock import patch
from ecommerce_agent.provider import ChatProvider,_transport_problem,_wait_retry
from ecommerce_agent.model_runtime import model_scope,ModelPolicy
from ecommerce_agent.cancellation import CancelToken,cancel_scope,CancelledError
from ecommerce_agent.errors import AgentError

MESSAGES=[{'role':'user','content':'hi'}]


class RetryTests(unittest.TestCase):
    def client(self):return ChatProvider({'api_base_url':'http://127.0.0.1:1234','model':'local'})
    def response(self):return io.BytesIO(json.dumps({'choices':[{'message':{'content':'ok'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}}).encode())
    def error(self,code,headers=None):return urllib.error.HTTPError('http://localhost',code,'error',headers or {},None)

    def test_transient_connection_then_success_records_each_attempt(self):
        with model_scope() as ledger,patch('urllib.request.urlopen',side_effect=[urllib.error.URLError('temporary'),self.response()]) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
            result=self.client().complete(MESSAGES,[],8)
        self.assertEqual(result['content'],'ok');self.assertEqual(http.call_count,2);wait.assert_called_once()
        snap=ledger.snapshot();self.assertEqual(snap['calls'],2);self.assertIsNone(snap['reported']['total_tokens'])
        self.assertEqual(snap['known_totals']['total_tokens'],3)
        self.assertEqual([r['status'] for r in snap['records']],['failed','completed'])
        self.assertEqual([r['attempt'] for r in snap['records']],[1,2])

    def test_three_attempt_cap_does_not_execute_business_tools(self):
        with model_scope() as ledger,patch('urllib.request.urlopen',side_effect=self.error(503)) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
            with self.assertRaises(AgentError) as caught:self.client().complete(MESSAGES,[],8)
        self.assertEqual(caught.exception.code,'model_unavailable');self.assertEqual(http.call_count,3)
        self.assertEqual(wait.call_count,2);self.assertEqual(ledger.snapshot()['calls'],3)

    def test_non_transient_errors_and_uncertain_timeout_are_not_retried(self):
        errors=[self.error(400),self.error(401),self.error(403),self.error(404),self.error(408),
                TimeoutError(),urllib.error.URLError(TimeoutError()),urllib.error.URLError(ssl.SSLCertVerificationError())]
        for error in errors:
            with self.subTest(error=type(error).__name__),patch('urllib.request.urlopen',side_effect=error) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
                with self.assertRaises(AgentError):self.client().complete(MESSAGES,[],8)
                self.assertEqual(http.call_count,1);wait.assert_not_called()

    def test_invalid_response_is_not_retried(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'not-json')) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
            with self.assertRaises(AgentError) as caught:self.client().complete(MESSAGES,[],8)
        self.assertEqual(caught.exception.code,'model_response_invalid');self.assertEqual(http.call_count,1);wait.assert_not_called()

    def test_retry_after_is_respected_without_truncating_long_wait(self):
        with patch('urllib.request.urlopen',side_effect=[self.error(429,{'Retry-After':'2'}),self.response()]),patch('ecommerce_agent.provider._wait_retry') as wait:
            self.client().complete(MESSAGES,[],8)
        self.assertEqual(wait.call_args.args[0],2)
        with patch('urllib.request.urlopen',side_effect=self.error(429,{'Retry-After':'120'})) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
            with self.assertRaises(AgentError) as caught:self.client().complete(MESSAGES,[],8)
        self.assertEqual(caught.exception.retry_after,120);self.assertEqual(http.call_count,1);wait.assert_not_called()

    def test_retry_after_http_date_and_bad_header(self):
        value=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=5),usegmt=True)
        problem=_transport_problem(self.error(503,{'Retry-After':value}),'model')
        self.assertTrue(problem.transport_retryable);self.assertTrue(3<problem.retry_after<=5)
        self.assertFalse(_transport_problem(self.error(503,{'Retry-After':'invalid'}),'model').transport_retryable)

    def test_budget_stops_before_next_http_attempt(self):
        with model_scope(ModelPolicy(max_calls=1)) as ledger,patch('urllib.request.urlopen',side_effect=self.error(503)) as http,patch('ecommerce_agent.provider._wait_retry'):
            with self.assertRaises(AgentError) as caught:self.client().complete(MESSAGES,[],8)
        self.assertEqual(caught.exception.code,'model_budget_exceeded');self.assertEqual(http.call_count,1)
        self.assertEqual(ledger.snapshot()['calls'],1)

    def test_cancel_during_backoff_prevents_next_attempt(self):
        token=CancelToken()
        def waiting(delay,active):
            active.cancel();_wait_retry(delay,active)
        with cancel_scope(token),patch('urllib.request.urlopen',side_effect=self.error(503)) as http,patch('ecommerce_agent.provider._wait_retry',side_effect=waiting):
            with self.assertRaises(CancelledError):self.client().complete(MESSAGES,[],8)
        self.assertEqual(http.call_count,1)

    def test_stream_retries_only_before_any_visible_text(self):
        stream=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n'
        parts=[]
        with patch('urllib.request.urlopen',side_effect=[self.error(502),io.BytesIO(stream)]) as http,patch('ecommerce_agent.provider._wait_retry'):
            self.client().stream_text(MESSAGES,parts.append)
        self.assertEqual(parts,['hi']);self.assertEqual(http.call_count,2)
        class BrokenStream(io.BytesIO):
            def __next__(self):
                value=super().__next__()
                if not value.strip():raise urllib.error.URLError('connection lost')
                return value
        parts=[]
        with patch('urllib.request.urlopen',return_value=BrokenStream(stream)) as http,patch('ecommerce_agent.provider._wait_retry') as wait:
            with self.assertRaises(AgentError):self.client().stream_text(MESSAGES,parts.append)
        self.assertEqual(parts,['hi']);self.assertEqual(http.call_count,1);wait.assert_not_called()

    def test_retry_window_prevents_late_resend_and_clamps_attempt_timeout(self):
        clock=[0.0];timeouts=[]
        def http(*args,**kwargs):
            timeouts.append(kwargs['timeout']);clock[0]=29.9;raise self.error(503)
        with patch('ecommerce_agent.provider.time.monotonic',side_effect=lambda:clock[0]),patch('urllib.request.urlopen',side_effect=http) as request,patch('ecommerce_agent.provider._wait_retry') as wait:
            with self.assertRaises(AgentError):self.client().complete(MESSAGES,[],100)
        self.assertEqual(timeouts,[25]);self.assertEqual(request.call_count,1);wait.assert_not_called()

    def test_retry_timeout_uses_remaining_phase_deadline(self):
        clock=[0.0];timeouts=[]
        def http(*args,**kwargs):
            timeouts.append(kwargs['timeout'])
            if len(timeouts)==1:clock[0]=20;raise self.error(503)
            return self.response()
        with patch('ecommerce_agent.provider.time.monotonic',side_effect=lambda:clock[0]),patch('urllib.request.urlopen',side_effect=http),patch('ecommerce_agent.provider._wait_retry'):
            self.client().complete(MESSAGES,[],60)
        self.assertEqual(timeouts,[25,5])


if __name__=='__main__':unittest.main()
