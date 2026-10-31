"""能力契约回归：未知操作、越权参数、错误依赖和未接数据域不能悄悄变成可执行查询。"""
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.capabilities import catalog,validate_plan
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.workflows import execute_task

ROOT=Path(__file__).resolve().parents[1]


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.products=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def parse(self,question,**kwargs):
        return parse_task(question,self.products,self.store,today=date(2026,10,28),**kwargs)

    def test_compound_plan_and_contract_choose_actual_tools(self):
        task=self.parse('昨天什么卖得最好，比上周同一天怎么样')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(validate_plan(task,self.store),['get_product_group_sales'])
        self.assertEqual(task['capability_contract']['operations'],['sales_summary','rank_products','compare_selected'])

    def test_tampered_operation_dependency_and_parameters_are_rejected_before_io(self):
        original=self.parse('昨天什么卖得最好，比上周同一天怎么样')
        def operation(t):t['query_plan']['steps'][0]['operation']='refund_order'
        def dependency(t):t['query_plan']['steps'][1]['depends_on']=['comparison']
        def parameters(t):t['query_plan']['steps'][0]['sql']='SELECT * FROM other_store'
        def baseline(t):t['query_plan']['steps'][-1]['baseline_date']='2026-10-19'
        def metric(t):t['product_scope']['ranking']['metric']='profit'
        def members(t):t['product_scope']['sku_ids'].append('OTHER-STORE-SKU')
        for change in [operation,dependency,parameters,baseline,metric,members]:
            task=copy.deepcopy(original);change(task)
            with self.subTest(change=change.__name__),tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
                self.assertEqual(list(Path(temp).iterdir()),[])

    def test_missing_data_domain_stops_entire_request_and_does_not_call_planner(self):
        """不能查完销量就假装回答了广告、配送或差评影响。"""
        for question in ['昨天红玫瑰销量下降和差评有关吗','99红玫昨天销量与预计送达有什么关系','昨天全店什么最好，再分析推广费效果']:
            with self.subTest(question=question):
                def planner(*args):raise AssertionError('不允许模型绕过数据域边界')
                task=self.parse(question,query_planner=planner)
                self.assertEqual(task['status'],'unsupported',task['issues'])
                self.assertTrue(task['capability_boundaries'])
                self.assertNotIn('query_plan',task)

    def test_single_domain_plans_are_not_mislabeled_sales(self):
        for text,operation,tool in [('99红玫昨天缺货多久','inventory_status','get_inventory_status'),('99红玫昨天活动状态','promotion_status','get_promotion_history')]:
            task=self.parse(text);self.assertEqual(task['status'],'ready',task['issues'])
            self.assertEqual(task['query_plan']['steps'][0]['operation'],operation)
            self.assertEqual(validate_plan(task,self.store),[tool])

    def test_manifest_return_value_cannot_modify_host_permissions(self):
        document=catalog();document['operations']['rank_products']['scopes'].append('unrestricted')
        self.assertNotIn('unrestricted',catalog()['operations']['rank_products']['scopes'])
