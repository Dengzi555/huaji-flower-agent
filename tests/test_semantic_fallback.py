"""原话回归：规则未填全槽位时必须能进入受限模型规划，类别排名不能冒充单款排名。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.workflows import execute_task

ROOT=Path(__file__).resolve().parents[1]


class SemanticFallbackTests(unittest.TestCase):
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def parse(self,question,**kwargs):
        return parse_task(question,self.catalog,self.store,today=date(2026,10,29),**kwargs)

    def test_original_fathers_day_question_has_date_and_variety_ranking(self):
        task=self.parse('帮我查下父亲节哪类花麦的最好')
        self.assertEqual(task['target_date'],'2026-06-21')
        self.assertEqual(task['status'],'needs_data',task['issues'])
        self.assertEqual(task['query_plan']['ranking']['group_by'],'variety')

    def test_unrecognized_expression_reaches_model_once_and_revalidates_date(self):
        calls=[]
        def planner(question,context):
            calls.append(context)
            return {'decision':'select','selector':'__all__','start_date':'2026-06-21','end_date':'2026-06-21',
                'metric':'units_sold','direction':'desc','limit':5,'group_by':'variety'}
        task=self.parse('父亲节花材冠军是谁',intent_override='query_sales_performance',query_planner=planner)
        self.assertEqual(len(calls),1)
        self.assertEqual(task['status'],'needs_data',task['issues'])
        self.assertEqual(task['query_plan']['planning']['status'],'validated')
        self.assertEqual(task['query_plan']['ranking']['group_by'],'variety')

    def test_model_cannot_invent_a_date_when_user_provides_none(self):
        calls=[]
        def planner(question,context):
            calls.append(question)
            return {'decision':'select','selector':'__all__','start_date':'2026-10-25','end_date':'2026-10-25',
                'metric':'units_sold','direction':'desc','limit':5,'group_by':'variety'}
        task=self.parse('花材冠军是谁',intent_override='query_sales_performance',query_planner=planner)
        self.assertEqual(len(calls),1);self.assertEqual(task['status'],'needs_clarification')

    def test_single_medium_classification_can_converge_via_validated_plan(self):
        calls=[]
        def classifier(question):return {'decision':'clarify','intent':'none','candidates':['query_sales_performance'],'confidence':'medium'}
        def planner(question,context):
            calls.append(question)
            return {'decision':'select','selector':'__all__','start_date':'2026-06-21','end_date':'2026-06-21',
                'metric':'units_sold','direction':'desc','limit':5,'group_by':'variety'}
        task=self.parse('父亲节花材冠军是谁',route_classifier=classifier,query_planner=planner)
        self.assertEqual(len(calls),1);self.assertEqual(task['status'],'needs_data',task['issues'])
        self.assertEqual(task['routing']['tier'],'validated_plan')
        self.assertEqual(task['routing']['classification_status'],'needs_clarification')
        def multi(question):return {'decision':'clarify','intent':'none','candidates':['query_sales_performance','query_inventory'],'confidence':'medium'}
        task=self.parse('父亲节花材冠军是谁',route_classifier=multi,query_planner=planner)
        # 统一入口不再先请求分类器；旧分类回调即使给多候选，也不参与本次规划。
        self.assertEqual(task['status'],'needs_data');self.assertEqual(len(calls),2)
        task=self.parse('昨天99红玫销量怎么样，同时查库存',route_classifier=multi,query_planner=planner)
        self.assertEqual(task['status'],'needs_clarification');self.assertEqual(len(calls),2)

    def test_variety_totals_rank_and_missing_member_is_not_zero(self):
        task=self.parse('10月25日哪类花卖得最好')
        self.assertEqual(task['status'],'ready',task['issues'])
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
        self.assertEqual(state['status'],'completed')
        rows=state['analysis']['ranking']['rows']
        for row in rows:
            expected=sum(self.store.records['sales',sku,'2026-10-25']['units_sold'] for sku in row['member_skus'])
            self.assertEqual(row['units_sold'],expected)
            self.assertTrue(all(self.catalog.products[sku]['variety']==row['name'] for sku in row['member_skus']))
        sku=next(iter(self.catalog.products))
        self.store.records['sales',sku,'2026-10-25']['status']='unavailable'
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
        self.assertFalse(state['analysis']['ranking']['complete_scope'])
        self.assertNotIn(self.catalog.products[sku]['variety'],[r['name'] for r in state['analysis']['ranking']['rows']])

    def test_supplied_fathers_day_history_uses_category_sum_not_single_winner(self):
        """历史齐全时，红玫瑰多款合计9件应胜过单款向日葵6件；只在临时快照补测试记录。"""
        document=self.store.snapshot();products={'schema_version':1,'products':list(self.catalog.products.values())}
        counts={'FLR-ROSE-99-BOUQUET':4,'FLR-ROSE-19-BOUQUET':4,'FLR-ROSE-33-BUCKET':1,'FLR-SUNFLOWER-11-BUCKET':6}
        for product in products['products']:
            sku=product['sku_id'];product['listed_at']='2026-06-01'
            for day in ('2026-06-14','2026-06-21'):
                source=dict(self.store.records['sales',sku,'2026-10-25']);units=counts.get(sku,0)
                source.update(date=day,units_sold=units,paid_orders=units,paid_buyers=units,paid_revenue=units*50)
                document['sales'].append(source)
        self.store=DataStore.from_document(document);self.catalog=ProductCatalog.from_document(products,self.store.skus)
        task=self.parse('帮我查下父亲节哪类花麦的最好，对比上周同一天')
        self.assertEqual(task['status'],'ready',task['issues'])
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
        self.assertEqual(state['status'],'completed')
        leader=state['analysis']['ranking']['leaders'][0]
        self.assertEqual(leader['name'],'红玫瑰');self.assertEqual(leader['units_sold'],9)
        comparison=state['analysis']['selected_comparison']['rows'][0]
        self.assertEqual(comparison['baseline_units'],9);self.assertEqual(comparison['target_units'],9)

    def test_relative_holidays_and_period_ambiguity_are_revalidated(self):
        from ecommerce_agent.natural_dates import holiday_date
        self.assertEqual(holiday_date('父亲节',2025).isoformat(),'2025-06-15')
        self.assertEqual(holiday_date('母亲节',2026).isoformat(),'2026-05-10')
        self.assertEqual(self.parse('去年父亲节哪类花最好')['target_date'],'2025-06-15')
        task=self.parse('父亲节期间哪类花最好')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertTrue(any('起止日' in issue for issue in task['issues']))
