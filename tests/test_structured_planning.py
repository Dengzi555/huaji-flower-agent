"""结构化计划回归：直接编译、缺失条件、同义表达及真实多店对话入口。"""
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import test_conversations as fixtures
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.query_context import QueryContext
from ecommerce_agent.workflows import execute_task

ROOT=Path(__file__).resolve().parents[1]


class StructuredPlanningTests(unittest.TestCase):
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def proposal(self,**changes):
        """仅给模型替身返回协议字段，预期结果由测试独立断言。"""
        return dict(decision='select',intent='query_sales_performance',selector='__matched__',
            start_date='2026-10-01',end_date='2026-10-31',direction='none',limit=5,
            metric='units_sold',**changes)

    def parse(self,question,proposal=None,**options):
        calls=[]
        def planner(text,context):
            calls.append(context)
            return proposal or self.proposal()
        task=parse_task(question,self.catalog,self.store,today=date(2026,11,5),
            query_planner=planner,intent_retriever=lambda q:{'status':'fixture','candidates':[]},**options)
        return task,calls

    def test_model_plan_does_not_roundtrip_through_rules_parser(self):
        from ecommerce_agent import task_parser
        with patch.object(task_parser,'_parse_rules_task',wraps=task_parser._parse_rules_task) as rules:
            task,calls=self.parse('红玫瑰上个月帮我做个销售盘点')
        self.assertEqual(len(calls),1)
        self.assertEqual(rules.call_count,1)
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(task['raw_question'],'红玫瑰上个月帮我做个销售盘点')
        self.assertEqual((task['trend_start'],task['target_date']),('2026-10-01','2026-10-31'))
        self.assertEqual(set(task['product_scope']['sku_ids']),{p['sku_id'] for p in self.catalog.resolve('红玫瑰')['candidates']})

    def test_model_only_ranking_keeps_limit_and_dimension(self):
        proposal=self.proposal();proposal.update(direction='desc',group_by='product_type',limit=2)
        task,_=self.parse('红玫瑰上个月按商品类型，谁最抢手，前2名',proposal)
        self.assertEqual(task['status'],'ready',task['issues'])
        rank=task['query_plan']['ranking']
        self.assertEqual((rank['direction'],rank['group_by'],rank['limit']),('desc','product_type',2))
        self.assertEqual(task['product_scope']['ranking'],rank)

    def test_ranking_count_policy_is_shared_by_rules_and_model(self):
        from ecommerce_agent.query_plan import ranking,ranking_limit
        for text in ['前两名','前2款','Top 2']:
            self.assertEqual(ranking_limit(text),2)
            self.assertEqual(ranking('红玫瑰最好的是哪些，'+text)['limit'],2)
        self.assertEqual(ranking_limit('红玫瑰哪款最好'),5)

    def test_unparsed_explicit_time_is_grounded_in_original_expression(self):
        proposal=self.proposal();proposal['time_expression']='十月整月'
        task,_=self.parse('红玫瑰十月整月帮我做个销售盘点',proposal)
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(task['trend_start'],'2026-10-01')
        self.assertTrue(any('十月整月' in note for note in task['date_resolution']))

    def test_recent_without_range_only_asks_date(self):
        for question in ['最近红玫卖得不好','最近红玫瑰帮我做个销售盘点']:
            task,_=self.parse(question)
            self.assertEqual(task['status'],'needs_clarification')
            self.assertEqual(task['clarification']['missing_fields'],['date'])
            self.assertEqual({p['variety'] for p in task['candidates']},{'红玫瑰'})
            self.assertEqual(len(task['issues']),1)

    def test_missing_time_cannot_be_filled_with_an_unquoted_date(self):
        task,_=self.parse('红玫瑰帮我做个销售盘点')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertEqual(task['clarification']['missing_fields'],['date'])
        self.assertIsNone(task['target_date'])

    def test_known_month_is_preserved_when_model_requests_clarification(self):
        proposal=self.proposal();proposal.update(decision='clarify',missing_fields=['date','intent'])
        task,_=self.parse('红玫瑰上个月帮我做个销售盘点',proposal)
        self.assertEqual(task['clarification']['missing_fields'],['intent'])
        self.assertEqual((task['trend_start'],task['target_date']),('2026-10-01','2026-10-31'))
        self.assertFalse(any('哪段时间' in issue for issue in task['issues']))

    def test_timeout_with_incomplete_slots_is_service_failure(self):
        def broken(*args):raise TimeoutError('private provider detail')
        task=parse_task('最近红玫帮我做个盘点',self.catalog,self.store,today=date(2026,11,5),
            query_planner=broken,intent_retriever=lambda q:{'status':'fixture'})
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error_info']['code'],'model_timeout')
        self.assertNotIn('private provider',str(task))

    def test_explicit_date_and_product_scope_cannot_be_changed(self):
        wrong_time=self.proposal();wrong_time['start_date']='2026-10-02'
        narrowed=self.proposal();narrowed['selector']='FLR-ROSE-99-BOUQUET'
        for proposal in [wrong_time,narrowed]:
            task,_=self.parse('红玫瑰上个月帮我做个销售盘点',proposal)
            self.assertEqual(task['status'],'failed')
            self.assertEqual(task['error_info']['code'],'plan_validation_failed')

    def test_confirmed_context_can_supply_time_for_recent_followup(self):
        context=QueryContext(time={'target_date':'2026-10-25','is_window':True,
            'trend_start':'2026-10-19','trend_end':'2026-10-25','comparison_policy':'previous_calendar_day'})
        proposal=self.proposal();proposal.update(start_date='2026-10-19',end_date='2026-10-25')
        task,_=self.parse('最近红玫帮我做个销售盘点',proposal,query_context=context)
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(task['trend_start'],'2026-10-19')


class ConversationParaphraseTests(unittest.TestCase):
    """通过真正的对话和门店协调器比较条件；各问法使用独立新会话。"""
    setUp=fixtures.ConversationTests.setUp

    def test_date_clarification_can_continue_without_reselecting_product(self):
        calls=[]
        def planner(question,context):
            calls.append(question)
            return dict(decision='select',intent='query_sales_performance',selector='__matched__',
                start_date='2026-10-20',end_date='2026-10-25',direction='none',limit=5,metric='units_sold')
        self.hub.for_store('hx-sz-ns').query_planner=planner
        first=self.manager.turn(self.doc['id'],{'question':'最近红玫帮我做个销售盘点',
            'revision':self.doc['revision']},'missing-date')
        self.assertEqual(first['conversation']['waiting'],['date'])
        self.assertEqual(len(first['task']['parsed']['candidates']),3)
        doc=first['conversation']
        second=self.manager.turn(doc['id'],{'question':'10月20日至25日','revision':doc['revision']},'supplied-date')
        self.assertEqual(second['task']['status'],'queued',second['task']['parsed']['issues'])
        self.assertEqual(second['conversation']['waiting'],[])
        self.assertEqual(second['task']['parsed']['trend_start'],'2026-10-20')
        self.assertEqual(len(second['task']['parsed']['product_scope']['sku_ids']),3)

    def test_paraphrases_keep_store_products_window_and_operations(self):
        questions=['南岸店近7天向日葵销量、库存和活动一起看',
                   '重庆南岸店最近七天向日葵销量变化，同时查库存和活动',
                   '帮我查南岸店过去一周向日葵销量以及库存、活动']
        signatures=[]
        for index,question in enumerate(questions):
            doc=self.manager.new('hx-sz-ns')
            result=self.manager.turn(doc['id'],{'question':question,'revision':doc['revision']},f'paraphrase-{index}')
            task=result['task']
            self.assertIsNotNone(task,result['conversation'].get('notice'))
            parsed=task['parsed']
            self.assertEqual(task['status'],'queued',parsed['issues'])
            signatures.append((parsed['store_id'],tuple(sorted(parsed['product_scope']['sku_ids'])),
                parsed['trend_start'],parsed['target_date'],parsed['intent'],
                tuple(sorted(parsed['composite_operations']))))
        self.assertTrue(all(s==signatures[0] for s in signatures))
        self.assertEqual(signatures[0][0],'hx-cq-na')
