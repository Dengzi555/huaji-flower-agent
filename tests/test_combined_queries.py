"""多事项共用冻结范围；独立工具并发、部分失败、取消及权限边界回归。"""
import tempfile
import threading
import unittest
from unittest.mock import patch
import test_conversations as fixtures
from ecommerce_agent.workflows import execute_task
from ecommerce_agent.cancellation import CancelToken,cancel_scope


class CombinedQueryTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def prepare(self,question='向日葵销量变化、库存以及是否搞活动，近七天近七天'):
        result=self.turn(question)
        self.assertIsNotNone(result['task'])
        self.assertEqual(result['task']['status'],'queued',result['task']['parsed']['issues'])
        return result['task']['parsed']

    def execute(self,parsed,**kwargs):
        service=self.hub.for_store(parsed['store_id'])
        return execute_task(service.store,parsed['sku_id'],parsed['target_date'],self.temp.name+'/combined-runs',task_context=parsed,**kwargs)

    def test_original_category_question_compiles_three_operations(self):
        parsed=self.prepare()
        self.assertEqual(parsed['intent'],'query_combined')
        self.assertEqual(parsed['composite_operations'],['sales','inventory','promotions'])
        self.assertEqual(parsed['trend_start'],'2026-10-19')
        self.assertEqual(parsed['target_date'],'2026-10-25')
        result=self.execute(parsed)
        self.assertEqual(len(result['analysis']['sections']),3)
        self.assertIsNotNone(result['sales_trend'])
        self.assertEqual(result['execution']['max_concurrency'],3)

    def test_missing_date_keeps_known_operations_for_clarification_view(self):
        result=self.turn('看下向日葵的销量变化 库存以及是否搞活动')
        parsed=result['task']['parsed']
        self.assertEqual(parsed['status'],'needs_clarification')
        self.assertEqual(parsed['composite_operations'],['sales','inventory','promotions'])
        self.assertIsNone(parsed['target_date'])
        self.assertIsNone(result['task']['result'])

    def test_three_tools_really_overlap_and_results_keep_request_order(self):
        from ecommerce_agent.combined_query import CombinedToolRegistry
        parsed=self.prepare('99红玫10月25日销量、库存以及活动情况')
        original=CombinedToolRegistry.execute
        gate=threading.Barrier(3);lock=threading.Lock();active=0;peak=0
        def execute(registry,*args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            try:gate.wait(timeout=3);return original(registry,*args)
            finally:
                with lock:active-=1
        with patch.object(CombinedToolRegistry,'execute',execute):result=self.execute(parsed)
        self.assertEqual(peak,3)
        self.assertEqual([s['domain'] for s in result['analysis']['sections']],['sales','inventory','promotions'])

    def test_one_tool_failure_does_not_discard_other_results(self):
        from ecommerce_agent.combined_query import CombinedToolRegistry
        parsed=self.prepare('99红玫10月25日销量、库存以及活动情况')
        original=CombinedToolRegistry.execute
        def execute(registry,name,args):
            if name=='get_inventory_range':raise TimeoutError('injected inventory timeout')
            return original(registry,name,args)
        with patch.object(CombinedToolRegistry,'execute',execute):result=self.execute(parsed)
        self.assertEqual(result['status'],'incomplete')
        sections={s['domain']:s for s in result['analysis']['sections']}
        self.assertEqual(sections['inventory']['status'],'failed')
        self.assertEqual(sections['inventory']['error_info']['code'],'tool_timeout')
        self.assertEqual(sections['sales']['status'],'completed')
        self.assertEqual(sections['promotions']['status'],'completed')

    def test_inventory_and_campaigns_can_combine_without_sales(self):
        parsed=self.prepare('向日葵10月24日至25日库存和活动')
        self.assertEqual(parsed['composite_operations'],['inventory','promotions'])
        result=self.execute(parsed)
        self.assertEqual(result['tool_calls'],2)
        self.assertNotIn('sales',[s['domain'] for s in result['analysis']['sections']])

    def test_write_request_still_rejected_and_ambiguous_product_not_guessed(self):
        result=self.turn('向日葵近7天销量和库存，顺便把库存改成100')
        self.assertEqual(result['task']['status'],'unsupported')
        result=self.turn('红梅近7天销量、库存和活动')
        self.assertEqual(result['task']['status'],'needs_clarification')

    def test_cancel_preserves_completed_sections_and_late_result_cannot_complete(self):
        parsed=self.prepare('99红玫10月25日销量、库存以及活动情况')
        token=CancelToken()
        def event(entry):
            if entry['type']=='tool_completed':token.cancel()
        with cancel_scope(token):result=self.execute(parsed,on_event=event)
        self.assertEqual(result['status'],'cancelled')
        self.assertNotEqual(result['stop_reason'],'combined_completed')

    def test_composite_followup_inherits_operations_and_changes_only_date(self):
        self.prepare('99红玫10月25日销量、库存以及活动情况')
        parsed=self.prepare('那前天呢')
        self.assertEqual(parsed['composite_operations'],['sales','inventory','promotions'])
        self.assertEqual(parsed['target_date'],'2026-10-24')

    def test_plan_tampering_cannot_expand_scope_or_concurrency(self):
        from copy import deepcopy
        from ecommerce_agent.combined_query import validate_combined
        task=self.prepare('99红玫10月25日销量、库存以及活动情况')
        for field,value in [('max_concurrency',100),('steps',[{'id':'write','operation':'update_stock','depends_on':[]}])]:
            altered=deepcopy(task);altered['query_plan'][field]=value
            with self.assertRaises(ValueError):validate_combined(altered,self.hub.for_store(task['store_id']).store)

    def test_explicit_list_and_negative_branch_are_not_confused(self):
        parsed=self.prepare('99红玫10月25日销量、库存、活动')
        self.assertEqual(len(parsed['composite_operations']),3)
        result=self.turn('99红玫10月25日销量怎么样，不查库存和活动')
        self.assertEqual(result['task']['parsed']['intent'],'query_sales_performance')

    def test_all_stores_get_separate_combined_tasks_and_evidence(self):
        import test_multistore_rankings as ranking_fixtures
        result=self.turn('全部门店99红玫10月25日销量、库存、活动')
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        view=ranking_fixtures.MultiStoreRankingsTests.finish(self,result['conversation']['comparison'])
        self.assertEqual(len(view['rows']),6)
        self.assertTrue(all(len(row['sections'])==3 for row in view['rows']))


if __name__=='__main__':unittest.main()
