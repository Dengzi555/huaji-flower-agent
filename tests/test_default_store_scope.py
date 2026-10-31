"""生产默认范围与统一业务规划：不写门店查全部，哪个店比较门店，模型规划不循环。"""
import unittest
import test_conversations as fixtures
import test_multistore_rankings as ranking_fixtures
from ecommerce_agent.conversations import Conversations
from ecommerce_agent.store_ranking import summarize


class DefaultStoreScopeTests(unittest.TestCase):
    turn=fixtures.ConversationTests.turn
    seasonal_data=ranking_fixtures.MultiStoreRankingsTests.seasonal_data
    finish=ranking_fixtures.MultiStoreRankingsTests.finish

    def setUp(self):
        fixtures.ConversationTests.setUp(self)
        self.hub.default_query_scope='all'
        self.manager=Conversations(self.hub,today=self.manager.today)
        self.doc=self.manager.new('hx-sz-ns')

    def test_no_store_defaults_all_and_explicit_current_store_remains_single(self):
        result=self.turn('99红玫10月25日销量怎么样')
        self.assertEqual(len(result['conversation']['comparison']['children']),6)
        self.assertEqual(result['conversation']['comparison']['store_resolution']['source'],'default_all')
        result=self.turn('本店99红玫10月25日销量怎么样')
        self.assertEqual(result['task']['parsed']['store_id'],'hx-sz-ns')

    def test_product_champion_and_store_champion_are_different_plans(self):
        self.seasonal_data()
        product=self.turn('今年情人节哪款卖得最好')['conversation']['comparison']
        self.assertNotIn('store_ranking',product);self.assertTrue(product['scope']['product_scope']['ranking'])
        store=self.turn('今年情人节哪个店卖得最好')['conversation']['comparison']
        self.assertEqual(store['store_ranking']['metric'],'units_sold')
        self.assertIsNone(store['scope']['product_scope']['ranking'])
        result=self.finish(store)
        self.assertEqual(result['comparable_count'],6)
        self.assertEqual(result['rows'][0]['store_rank'],1)
        units=[r['metrics']['units'] for r in result['rows']]
        self.assertEqual(units,sorted(units,reverse=True))
        self.assertTrue(result['store_ranking']['partitions'][0]['leaders'])

    def test_store_ranking_preserves_product_filter_and_needs_date_if_missing(self):
        self.seasonal_data()
        missing=self.turn('哪个店卖得最好')
        self.assertIsNone(missing['task']);self.assertNotIn('comparison',missing['conversation'])
        self.assertIn('日期',missing['conversation']['notice'])
        job=self.turn('2月14日哪个店99红玫卖得最好')['conversation']['comparison']
        for child in job['children']:
            parsed=self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']
            self.assertEqual(parsed['sku_id'],'FLR-ROSE-99-BOUQUET')

    def test_multi_store_followup_keeps_set_and_new_question_resets_default_all(self):
        self.turn('南山店19红玫10月25日销量怎么样')
        single=self.turn('那前天呢')['task'];self.assertEqual(single['parsed']['store_id'],'hx-sz-ns')
        all_stores=self.turn('19红玫10月25日销量怎么样')['conversation']['comparison']
        self.assertEqual(len(all_stores['children']),6)
        earlier=self.turn('那前天呢')['conversation']['comparison']
        self.assertEqual(len(earlier['children']),6);self.assertEqual(earlier['scope']['date'],'2026-10-24')

    def test_rule_miss_uses_one_shared_model_plan_then_compiles_to_all_stores(self):
        calls=[]
        def planner(q,ctx):
            calls.append(q)
            return dict(decision='select',intent='query_sales_performance',selector='__matched__',
                start_date='2026-10-25',end_date='2026-10-25',metric='units_sold',direction='none',limit=5)
        self.manager.query_planner=planner
        result=self.turn('炽热爱意10月25日成交几束')
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        self.assertEqual(len(calls),1)
        for child in result['conversation']['comparison']['children']:
            parsed=self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']
            self.assertEqual(parsed['sku_id'],'FLR-ROSE-99-BOUQUET')
            self.assertEqual(parsed['target_date'],'2026-10-25')

    def test_longtail_store_summary_uses_host_omission_policy_and_one_plan(self):
        self.seasonal_data();calls=[]
        def planner(q,ctx):
            calls.append(ctx)
            return dict(decision='select',intent='query_sales_performance',selector='__all__',
                start_date='2026-02-14',end_date='2026-02-14',metric='units_sold',direction='none',limit=5)
        self.manager.query_planner=planner
        result=self.turn('情人节我们几家门店生意咋样')
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['product_scope_policy'],'all_if_omitted')
        scope=result['conversation']['comparison']['scope']
        self.assertEqual(scope['planning']['product_scope_source'],'model_interpreted_omission_with_host_default')
        self.assertEqual(len(result['conversation']['comparison']['children']),6)

    def test_invalid_model_plan_does_not_call_model_again_or_create_tasks(self):
        calls=[]
        def planner(q,ctx):
            calls.append(q)
            return dict(decision='select',intent='query_sales_performance',selector='__matched__',
                start_date='2026-10-24',end_date='2026-10-24',metric='units_sold',direction='none',limit=5)
        self.manager.query_planner=planner
        result=self.turn('炽热爱意10月25日成交几束')
        self.assertEqual(len(calls),1);self.assertIsNone(result['task'])
        self.assertEqual(sum(len(s.list_tasks()) for s in self.hub.services.values()),0)
        self.assertEqual(self.doc['waiting'],[])
        self.assertEqual(self.doc['turns'][-1]['chat']['error_info']['code'],'plan_validation_failed')

    def test_missing_or_unsupported_metric_does_not_become_sales_ranking(self):
        self.seasonal_data()
        result=self.turn('2月14日哪个店取消率最高')
        self.assertNotIn('comparison',result['conversation'])
        self.assertEqual(sum(len(s.list_tasks()) for s in self.hub.services.values()),0)

    def test_general_chat_does_not_query_all_stores(self):
        result=self.turn('你好');self.assertIsNone(result['task'])
        self.assertEqual(sum(len(s.list_tasks()) for s in self.hub.services.values()),0)
        self.assertEqual(self.doc['turns'][-1]['kind'],'chat')

    def test_store_ties_zero_and_missing_keep_correct_denominator(self):
        rows=[{'store_id':i,'store_name':i,'comparable':v is not None,'metrics':{'units':v}} for i,v in [('a',10),('b',10),('c',0),('d',None)]]
        view=summarize({'rows':rows},{'metric':'units_sold','direction':'both'})
        self.assertEqual(len(view['store_ranking']['partitions'][0]['leaders']),2)
        self.assertEqual(view['store_ranking']['partitions'][1]['leaders'][0]['units_sold'],0)
        self.assertEqual(view['store_ranking']['excluded_store_ids'],['d'])
        self.assertFalse(view['store_ranking']['complete_scope'])


if __name__=='__main__':unittest.main()
