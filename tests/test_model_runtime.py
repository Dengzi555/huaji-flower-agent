"""资源管理回归：只用本地响应替身，不调用付费API。验证容量、预算、未知用量与落盘。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ecommerce_agent.model_runtime import ModelPolicy,UsageLedger,model_scope,prepare_messages,estimate
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.errors import AgentError
from ecommerce_agent.cancellation import CancelToken,cancel_scope
from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.runner import run
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.conversations import Conversations


class ModelRuntimeTests(unittest.TestCase):
    """真实适配层配合BytesIO，既检查请求体也检查拒绝时HTTP没有发生。"""
    def client(self):return ChatProvider({'api_base_url':'http://127.0.0.1:1234/v1','model':'local','max_output_tokens':64})

    def response(self,usage=None):
        value={'choices':[{'message':{'content':'可以，我会根据证据分析。'}}]}
        if usage is not None:value['usage']=usage
        return io.BytesIO(json.dumps(value).encode())

    def test_trim_complete_chat_turn_preserves_system_and_current_question(self):
        messages=[{'role':'system','content':'系统约束'},{'role':'user','content':'旧问题'*300},
            {'role':'assistant','content':'旧回答'*300},{'role':'user','content':'当前问题'}]
        original=json.dumps(messages)
        selected,receipt=prepare_messages(messages,[],64,ModelPolicy(context_units=600))
        self.assertEqual([m['role'] for m in selected],['system','user'])
        self.assertEqual(selected[-1]['content'],'当前问题');self.assertEqual(receipt['removed_messages'],2)
        self.assertEqual(json.dumps(messages),original)

    def test_tool_transcript_never_loses_call_or_receipt(self):
        messages=[{'role':'system','content':'sys'},{'role':'user','content':'question'},
            {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'read','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'a','content':'证据'*1000}]
        with self.assertRaises(AgentError) as caught:prepare_messages(messages,[],64,ModelPolicy(context_units=600))
        self.assertEqual(caught.exception.code,'model_context_limit');self.assertEqual(len(messages),4)

    def test_schema_and_output_reserve_count_before_http(self):
        client=self.client()
        with model_scope(ModelPolicy(context_units=300)),patch('urllib.request.urlopen') as http:
            with self.assertRaises(AgentError):client.complete([{'role':'user','content':'hi'}],[{'schema':'x'*500}],1)
            http.assert_not_called()

    def test_output_limit_applies_without_thinking_flag(self):
        captured={}
        def http(request,**kwargs):captured.update(json.loads(request.data));return self.response()
        with patch('urllib.request.urlopen',side_effect=http):self.client().complete([{'role':'user','content':'hi'}],[],1)
        self.assertEqual(captured['max_tokens'],64)

    def test_shared_request_budget_spans_multiple_clients(self):
        with model_scope(ModelPolicy(max_calls=1)) as ledger,patch('urllib.request.urlopen',side_effect=lambda *a,**k:self.response()) as http:
            self.client().complete([{'role':'user','content':'hi'}],[],1)
            with self.assertRaises(AgentError) as caught:self.client().complete([{'role':'user','content':'again'}],[],1)
            self.assertEqual(caught.exception.code,'model_budget_exceeded');self.assertEqual(http.call_count,1)
            self.assertEqual(ledger.snapshot()['calls'],1)

    def test_estimate_budget_rejects_before_first_request(self):
        with model_scope(ModelPolicy(max_estimated_tokens=10)) as ledger,patch('urllib.request.urlopen') as http:
            with self.assertRaises(AgentError):self.client().complete([{'role':'user','content':'hi'}],[],1)
            self.assertEqual(ledger.snapshot()['calls'],0);http.assert_not_called()

    def test_failure_does_not_turn_unknown_usage_into_zero(self):
        client=self.client()
        with model_scope() as ledger,patch('urllib.request.urlopen',side_effect=[self.response({'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}),TimeoutError()]):
            client.complete([{'role':'user','content':'hi'}],[],1)
            with self.assertRaises(AgentError):client.complete([{'role':'user','content':'again'}],[],1)
            snap=ledger.snapshot()
        self.assertEqual(snap['calls'],2);self.assertIsNone(snap['reported']['total_tokens'])
        self.assertEqual(snap['known_totals']['total_tokens'],12);self.assertEqual(snap['unknown_usage_calls'],1)
        self.assertIsNone(snap['cost']);self.assertEqual(snap['records'][1]['status'],'failed')

    def test_stream_records_usage_and_malformed_usage_stays_unknown(self):
        stream=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"usage":{"prompt_tokens":true,"completion_tokens":-1,"total_tokens":8},"choices":[]}\n\ndata: [DONE]\n'
        with model_scope() as ledger,patch('urllib.request.urlopen',return_value=io.BytesIO(stream)):
            self.client().stream_text([{'role':'user','content':'hi'}],lambda text:None)
            snap=ledger.snapshot()
        self.assertIsNone(snap['reported']['prompt_tokens']);self.assertEqual(snap['reported']['total_tokens'],8)
        self.assertEqual(snap['unknown_usage_calls'],1)

    def test_cancelled_stream_keeps_unknown_billing(self):
        token=CancelToken()
        stream=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n'
        with model_scope() as ledger,cancel_scope(token),patch('urllib.request.urlopen',return_value=io.BytesIO(stream)):
            with self.assertRaises(AgentError):self.client().stream_text([{'role':'user','content':'hi'}],lambda text:token.cancel())
            snap=ledger.snapshot()
        self.assertEqual(snap['records'][0]['status'],'cancelled');self.assertIsNone(snap['reported']['total_tokens'])

    def test_scopes_do_not_leak_between_requests(self):
        with model_scope() as first:
            with model_scope() as nested:self.assertIs(first,nested)
        with model_scope() as second:self.assertIsNot(first,second)

    def test_runner_budget_stops_before_next_tool_and_saves_evidence(self):
        class Client:
            def complete(self,*args):return {'role':'assistant','tool_calls':[{'id':'one','function':{'name':'get_sales_metrics','arguments':'{}'}}]}
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);data=root/'data.json';data.write_text(json.dumps(business_data()),encoding='utf-8')
            state=run(DataStore(data),'FLR-ROSE-99-BOUQUET','2026-10-25',root/'runs',provider=Client(),model_policy=ModelPolicy(max_calls=1))
            saved=json.loads((Path(state['run_dir'])/'run.json').read_text(encoding='utf-8'))
            evidence=json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'],'incomplete');self.assertEqual(saved['model_calls'],1)
        self.assertEqual(saved['error_info']['code'],'model_budget_exceeded');self.assertEqual(len(evidence['observations']),1)

    def test_chat_usage_persists_and_replay_makes_no_new_model_call(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);data=root/'data.json';catalog=root/'products.json'
            data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
            service=InvestigationService(data,catalog,root/'state',root/'runs',start_worker=False)
            try:
                manager=Conversations(service);doc=manager.new(manager.shops()[0]['id'])
                def chat(question,history):
                    client=self.client();result=client.complete([{'role':'user','content':question}],[],1)
                    return {'text':result['content'],'model_calls':1}
                manager.chat_responder=chat
                payload={'question':'介绍一下自己','revision':0,'mode':'model'}
                with patch('urllib.request.urlopen',side_effect=lambda *a,**k:self.response({'prompt_tokens':8,'completion_tokens':2,'total_tokens':10})) as http:
                    manager.turn(doc['id'],payload,'key');replay=manager.turn(doc['id'],payload,'key')
                saved=manager.load(doc['id'])['turns'][-1]['model_runtime']
                self.assertTrue(replay['reused']);self.assertEqual(http.call_count,1)
                self.assertEqual(saved['reported']['total_tokens'],10)
            finally:service.close()


if __name__=='__main__':unittest.main()
