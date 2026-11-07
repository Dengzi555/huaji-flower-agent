"""推广页与对话共用账本：复现真实问法，验证日期、门店、来源与失败边界。"""
import unittest
from unittest.mock import patch
import test_conversations as fixtures


class AdvertisingConversationTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def test_original_request_reads_futian_book_without_model_or_sku(self):
        self.manager.query_planner=lambda *args: self.fail('明确推广请求不应调用销量规划模型')
        result=self.turn('福田店前天推广分析一下')
        self.assertIsNone(result['task'])
        turn=result['conversation']['turns'][-1]
        self.assertEqual(turn['chat']['mode'],'advertising')
        report=turn['advertising']['reports'][0]
        self.assertEqual(report['store_id'],'hx-sz-ft')
        self.assertEqual(report['date'],'2026-10-24')
        self.assertEqual(report['totals'],self.hub.books['hx-sz-ft'].report('2026-10-24')['totals'])
        self.assertEqual(turn['advertising']['tool_calls'][0]['name'],'get_advertising_report')
        self.assertIn('深圳福田店',turn['message'])
        self.assertEqual(turn['advertising']['model_calls'],0)

    def test_window_modes_and_all_stores_are_not_defaulted_to_single(self):
        result=self.turn('全部门店10月24日至25日手动推广费分析')
        receipt=result['conversation']['turns'][-1]['advertising']
        self.assertEqual(len(receipt['reports']),12)
        self.assertTrue(all(r['mode']=='manual' for r in receipt['reports']))

    def test_missing_date_is_followup_not_invalid_model(self):
        result=self.turn('福田店推广分析一下')
        self.assertIsNone(result['task'])
        self.assertEqual(result['conversation']['waiting'],['date'])
        result=self.turn('前天')
        self.assertEqual(result['conversation']['turns'][-1]['advertising']['reports'][0]['date'],'2026-10-24')

    def test_date_only_followup_keeps_advertising_not_old_sales_intent(self):
        self.turn('福田店10月25日推广分析')
        result=self.turn('那前天呢')
        self.assertEqual(result['conversation']['turns'][-1]['advertising']['reports'][0]['date'],'2026-10-24')

    def test_missing_book_day_is_unknown_not_zero_or_latest(self):
        result=self.turn('福田店2025年1月1日推广费分析')
        receipt=result['conversation']['turns'][-1]['advertising']
        self.assertEqual(receipt['reports'],[])
        self.assertEqual(receipt['status'],'needs_data')
        self.assertIn('2025-01-01',result['conversation']['turns'][-1]['message'])

    def test_replay_keeps_frozen_report_and_does_not_read_book_again(self):
        payload={'question':'福田店前天推广分析','revision':self.doc['revision']}
        first=self.manager.turn(self.doc['id'],payload,'same-ad-request')
        with patch.object(self.hub.books['hx-sz-ft'],'report',side_effect=AssertionError('幂等重放不应重新读取账本')):
            replay=self.manager.turn(self.doc['id'],payload,'same-ad-request')
        self.assertTrue(replay['reused'])
        self.assertEqual(first['conversation']['turns'][-1]['advertising'],replay['conversation']['turns'][-1]['advertising'])

    def test_advertising_registry_rejects_other_tools_and_arguments(self):
        from ecommerce_agent.advertising_query import AdvertisingToolRegistry
        registry=AdvertisingToolRegistry(self.hub.books['hx-sz-ft'],'2026-10-24',high=10,low=3,mode='all')
        for name,args in [('change_budget',{}),('get_advertising_report',{'store_id':'hx-sz-ns'})]:
            with self.assertRaises(ValueError):registry.execute(name,args)

    def test_write_product_attribution_and_mixed_queries_are_not_silently_executed(self):
        for q in ['福田店前天推广分析并把预算调成200元','福田店前天99红玫推广分析','福田店前天推广ROI是多少','福田店前天推广费和库存分别查一下']:
            with self.subTest(q=q):
                result=self.turn(q)
                self.assertIsNone(result['task'])
                receipt=result['conversation']['turns'][-1]['advertising']
                self.assertEqual(receipt['status'],'unsupported')
                self.assertEqual(receipt['tool_calls'],[])


if __name__=='__main__':unittest.main()
