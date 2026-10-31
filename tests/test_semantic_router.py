"""语义路由的控制流反例：候选不是授权，依赖故障不得阻断模型兜底。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.semantic_router import rank_candidates,retrieve_intents,intent_text

ROOT=Path(__file__).resolve().parents[1]


class SemanticRouterTests(unittest.TestCase):
    """使用可控检索器/规划器检查边界，不调用付费API。"""
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def parse(self,text,planner,retriever):
        return parse_task(text,self.catalog,self.store,today=date(2026,10,29),query_planner=planner,intent_retriever=retriever)

    def plan(self,question,context):
        return dict(decision='select',intent='query_inventory',selector='炽热爱意',start_date='2026-10-25',end_date='2026-10-25',metric='units_sold',direction='none',limit=5)

    def test_complete_rule_skips_both_retrieval_and_model(self):
        def forbidden(*args):raise AssertionError('快捷路径不应调用检索或模型')
        task=self.parse('99红玫10月25日缺货多久',forbidden,forbidden)
        self.assertEqual(task['status'],'ready')
        self.assertNotIn('intent_retrieval',task)

    def test_wrong_candidate_does_not_override_model_or_slots(self):
        calls=[]
        retrieval={'status':'ok','candidates':[{'intent':'query_promotions','score':.99,'examples':[{'text':'去年活动结束了吗'}]}]}
        def planner(question,context):
            calls.append(context)
            return self.plan(question,context)
        task=self.parse('炽热爱意10月25日帮我做个专题研判',planner,lambda q:retrieval)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['intent_retrieval'],retrieval)
        self.assertEqual(task['intent'],'query_inventory')
        self.assertEqual(task['target_date'],'2026-10-25')
        self.assertEqual(task['status'],'ready',task['issues'])

    def test_retrieval_failure_still_plans(self):
        def failed(question):raise RuntimeError('index unavailable')
        task=self.parse('炽热爱意10月25日帮我做个专题研判',self.plan,failed)
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['intent_retrieval']['status'],'unavailable')

    def test_write_guard_skips_retrieval_and_model(self):
        def forbidden(*args):raise AssertionError('越权请求不能绕过边界')
        task=self.parse('99红玫昨天销量怎么样并帮我改价',forbidden,forbidden)
        self.assertEqual(task['status'],'unsupported')

    def test_retrieved_candidate_does_not_bypass_date_validation(self):
        def invalid(question,context):
            return {**self.plan(question,context),'start_date':'2026-10-24','end_date':'2026-10-24'}
        task=self.parse('炽热爱意10月25日帮我做个专题研判',invalid,lambda q:{'status':'ok','candidates':[]})
        self.assertNotEqual(task['status'],'ready')

    def test_missing_local_index_falls_back_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            result=retrieve_intents('检查供货',Path(folder))
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['network_calls'],0)

    def test_scores_aggregate_per_intent_and_reject_nan(self):
        examples=[{'id':str(i),'intent':intent,'text':'例句'} for i,intent in enumerate(['a','a','a','a','b'])]
        result=rank_candidates(examples,[.9,.8,.7,.1,.85])
        self.assertEqual(result[0]['intent'],'b')
        self.assertEqual(result[1]['score'],.8)
        with self.assertRaises(ValueError):rank_candidates(examples,[float('nan')]*5)

    def test_entity_and_time_masking_preserves_request(self):
        terms=['炽热爱意','红玫瑰']
        a=intent_text('炽热爱意10月25日出单不如从前帮忙找找症结',terms)
        b=intent_text('红玫瑰2026-09-01出单不如从前帮忙找找症结',terms)
        self.assertEqual(a,b)
        self.assertIn('找找症结',a)

    def test_rejected_parameters_keep_usage_receipt(self):
        def invalid(question,context):
            return {**self.plan(question,context),'start_date':'2026-10-24','end_date':'2026-10-24',
                    'usage':{'prompt_tokens':100,'completion_tokens':20},'model':'test-model'}
        task=self.parse('炽热爱意10月25日帮我做个专题研判',invalid,lambda q:{'status':'ok','candidates':[]})
        self.assertNotEqual(task['status'],'ready')
        self.assertEqual(task['query_plan']['planning']['usage']['prompt_tokens'],100)


if __name__=='__main__':unittest.main()
