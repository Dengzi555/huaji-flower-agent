"""统一规划入口：不依赖新增关键词；规则命中零调用，未知表达一次规划覆盖多类能力。"""
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
ROOT=Path(__file__).resolve().parents[1]

class UnifiedPlannerTests(unittest.TestCase):
    def test_unknown_expressions_use_one_plan_without_classifier(self):
        store=DataStore(ROOT/'data/stores/hx-cq-na.json');catalog=ProductCatalog(ROOT/'data/products.json',store.skus)
        for intent in ['query_sales_performance','query_inventory','query_promotions','investigate_sales_change']:
            with self.subTest(intent=intent):
                calls=[]
                def classifier(*args):raise AssertionError('不能额外调用分类模型')
                def planner(question,context):
                    calls.append(context)
                    return dict(decision='select',intent=intent,selector='炽热爱意',start_date='2026-10-25',end_date='2026-10-25',metric='units_sold',direction='none',limit=5)
                task=parse_task('炽热爱意10月25日帮我做个专题研判',catalog,store,today=date(2026,10,29),query_planner=planner,route_classifier=classifier)
                self.assertEqual(len(calls),1);self.assertEqual(task['status'],'ready',task['issues']);self.assertEqual(task['intent'],intent)
                self.assertEqual(task['understanding']['path'],'model_plan')
        def no_call(*args):raise AssertionError('规则完整时不调用模型')
        task=parse_task('99红玫10月25日缺货多久',catalog,store,today=date(2026,10,29),query_planner=no_call)
        self.assertEqual(task['status'],'ready');self.assertEqual(task['understanding']['planning_calls'],0)
