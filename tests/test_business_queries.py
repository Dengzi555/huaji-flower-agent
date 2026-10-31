"""固定业务问题评测：按任务状态、日期、商品范围和操作检查，避免只测关键词是否命中。"""
import json
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task

ROOT=Path(__file__).resolve().parents[1]


class BusinessQueryEvaluation(unittest.TestCase):
    def test_business_question_set(self):
        """固定业务今天和门店，离线评测可重复运行，不调用付费模型。"""
        store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        catalog=ProductCatalog(ROOT/'data/products.json',store.skus)
        cases=json.loads((ROOT/'tests/fixtures/business_queries.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(question=case['question']):
                task=parse_task(case['question'],catalog,store,today=date(2026,10,28))
                self.assertEqual(task['status'],case['status'],task['issues'])
                for expected,actual in [('date','target_date'),('baseline','comparison_date'),('sku','sku_id'),('window','is_window')]:
                    if expected in case:self.assertEqual(task[actual],case[expected])
                if 'direction' in case:self.assertEqual(task['query_plan']['ranking']['direction'],case['direction'])
                if 'scope' in case:self.assertEqual(task['product_scope']['kind'],case['scope'])
                if 'type' in case:self.assertTrue(all(p['product_type']==case['type'] for p in task['candidates']))
                if case.get('compare_selected'):self.assertTrue(task['product_scope']['compare_selected'])
                if task['status']=='ready':self.assertEqual(task['query_plan']['model_calls'],0)

    def test_unsupported_metric_cannot_be_silently_rewritten_by_model(self):
        """规则已确定的能力边界不能由参数规划模型改写，避免用销量冒充利润。"""
        store=DataStore(ROOT/'data/stores/hx-cq-na.json');catalog=ProductCatalog(ROOT/'data/products.json',store.skus)
        def planner(*args):raise AssertionError('不应调用模型绕过指标边界')
        task=parse_task('昨天全店按营业额排名',catalog,store,today=date(2026,10,28),query_planner=planner)
        self.assertEqual(task['status'],'needs_clarification')
        self.assertNotIn('planning',task['query_plan'])
