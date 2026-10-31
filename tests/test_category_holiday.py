"""用户原话回归：节日日期、花材范围、错字确认和实际缺数据信息。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.workflows import execute_task

ROOT=Path(__file__).resolve().parents[1]


class CategoryHolidayTests(unittest.TestCase):
    def setUp(self):
        """读取实际南岸店学习目录，不调用模型，不写正式工作区。"""
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def parse(self,text,**kwargs):
        return parse_task(text,self.catalog,self.store,today=date(2026,10,28),**kwargs)

    def test_original_question_only_asks_typo_confirmation_not_date_or_single_sku(self):
        task=self.parse('南岸情人节红梅买的怎么样')
        self.assertEqual(task['target_date'],'2026-02-14')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertTrue(task.get('group_candidate'))
        self.assertFalse(any('补充目标日期' in issue or '多个 SKU' in issue for issue in task['issues']))

    def test_confirm_all_reports_missing_holiday_data(self):
        task=self.parse('南岸情人节红梅买的怎么样',sku_override='__all_matched__')
        self.assertEqual(task['status'],'needs_data')
        self.assertEqual(len(task['product_scope']['sku_ids']),3)
        self.assertTrue(any('2026-02-14' in issue for issue in task['issues']))

    def test_category_query_executes_and_never_adds_uv_buyers_or_orders(self):
        task=self.parse('南岸10月25日红玫瑰卖得怎么样')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(task['sku_id'],'__all_matched__')
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
            self.assertEqual(state['status'],'completed')
            expected=sum(self.store.records['sales',sku,'2026-10-25']['units_sold'] for sku in task['product_scope']['sku_ids'])
            self.assertEqual(state['analysis']['display_metrics'][0]['value'],expected)
            self.assertNotIn('payment_conversion_pct',state['analysis']['facts'])
            self.assertIsNone(state['sales_trend']['records'][-1]['visitors'])

    def test_specific_sku_and_marketing_title_stay_specific(self):
        task=self.parse('99红玫10月25日卖得怎么样')
        self.assertEqual(task['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertFalse(task.get('product_scope'))
        task=self.parse(self.catalog.products['FLR-ROSE-99-BOUQUET']['name']+'10月25日销量怎么样')
        self.assertEqual(task['target_date'],'2026-10-25')

    def test_explicit_year_and_unsupported_period(self):
        self.assertEqual(self.parse('去年情人节红玫瑰销量怎么样')['target_date'],'2025-02-14')
        task=self.parse('情人节期间红玫瑰卖得怎么样')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertTrue(any('期间' in issue for issue in task['issues']))

    def test_global_top_bottom_and_teachers_day_share_query_plan(self):
        for text,direction in [('昨天什么买的最好','desc'),('昨天什么买的最差','asc')]:
            task=self.parse(text);self.assertEqual(task['status'],'ready',task['issues'])
            self.assertEqual(task['product_scope']['kind'],'store')
            self.assertEqual(task['query_plan']['ranking']['direction'],direction)
            with tempfile.TemporaryDirectory() as temp:
                result=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
                rank=result['analysis']['ranking'];self.assertTrue(rank['complete_scope'])
                counts=[r['units_sold'] for r in rank['rows']]
                self.assertEqual(counts,sorted(counts,reverse=direction=='desc'))
                if direction=='asc':self.assertEqual(counts[0],0)
        task=self.parse('这次教师节向日葵买了多少，哪一款最好？')
        self.assertEqual(task['target_date'],'2026-09-10')
        self.assertEqual(task['status'],'needs_data');self.assertEqual(task['query_plan']['ranking']['direction'],'desc')

    def test_model_planner_once_and_cannot_override_dates(self):
        calls=[]
        def planner(question,context):
            calls.append(question)
            return {'decision':'select','selector':'红玫瑰','start_date':'2026-10-25','end_date':'2026-10-25',
                'metric':'units_sold','direction':'desc','limit':5}
        task=self.parse('10月25日红玫瑰按销量看哪款更受欢迎',intent_override='query_sales_performance',query_planner=planner)
        self.assertEqual(task['status'],'ready',task['issues']);self.assertEqual(len(calls),1)
        self.assertEqual(task['query_plan']['planning']['status'],'validated')
        task=self.parse('10月24日红玫瑰按销量看哪款更受欢迎',intent_override='query_sales_performance',query_planner=planner)
        # 用户日期完整而模型改成了另一天，属于计划校验失败，不能反问用户补日期。
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error_info']['code'],'plan_validation_failed')

    def test_rank_then_compare_same_products_not_store_total(self):
        """组合查询把排名结果作为下一步输入，数值必须来自同一SKU的两天记录。"""
        task=self.parse('昨天什么买的最好，比上周同一天多多少？')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual(task['comparison_date'],'2026-10-20')
        self.assertEqual([s['operation'] for s in task['query_plan']['steps']],['sales_summary','rank_products','compare_selected'])
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
            self.assertEqual(state['status'],'completed')
            selected=state['analysis']['selected_comparison']['rows']
            self.assertEqual({r['sku_id'] for r in selected},{r['sku_id'] for r in state['analysis']['ranking']['leaders']})
            for row in selected:
                target=self.store.records['sales',row['sku_id'],'2026-10-27']['units_sold']
                before=self.store.records['sales',row['sku_id'],'2026-10-20']['units_sold']
                self.assertEqual(row['absolute_change'],target-before)
            self.assertEqual(len(state['selected_sales_trends']),min(3,len(selected)))

    def test_model_parameter_plan_preserves_comparison_dependency(self):
        """模型只补排序参数，原问题明确的基线必须保留下来进入组合执行。"""
        calls=[]
        def planner(question,context):
            calls.append(question)
            return {'decision':'select','selector':'红玫瑰','start_date':'2026-10-25','end_date':'2026-10-25',
                'metric':'units_sold','direction':'desc','limit':3}
        task=self.parse('10月25日红玫瑰哪款更受欢迎，对比上周同一天',intent_override='query_sales_performance',query_planner=planner)
        self.assertEqual(task['status'],'ready',task['issues']);self.assertEqual(len(calls),1)
        self.assertEqual(task['comparison_date'],'2026-10-18')
        self.assertTrue(task['product_scope']['compare_selected'])
        self.assertEqual(task['query_plan']['steps'][-1]['depends_on'],['ranking'])

    def test_type_only_scope_and_explicit_baseline(self):
        task=self.parse('抱抱桶10月25日哪款最好，对比10月21日')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertTrue(all(self.catalog.products[sku]['product_type']=='抱抱桶' for sku in task['product_scope']['sku_ids']))
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
            self.assertEqual(state['status'],'completed')
            self.assertTrue(any(r['date']=='2026-10-21' and r['is_primary'] for r in state['sales_trend']['comparisons']))
            self.assertTrue(all(r['date']=='2026-10-21' for r in state['analysis']['selected_comparison']['rows']))

    def test_tied_worst_keep_all_and_missing_baseline_is_unknown(self):
        """零销量并列不能只选一款；目标日有记录、历史缺失时不计算虚假的增长率。"""
        task=self.parse('昨天什么买的最差，对比2026-08-20')
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
            self.assertEqual(state['status'],'completed')
            rows=state['analysis']['selected_comparison']['rows']
            self.assertGreater(len(rows),1)
            self.assertTrue(all(r['target_units']==0 and r['baseline_units'] is None and r['relative_pct'] is None for r in rows))

    def test_teachers_day_summary_and_ranking_with_supplied_history(self):
        """有真实口径的历史与目录时，同一个计划同时产生花材合计和单款排名。"""
        document=self.store.snapshot();catalog_document={'schema_version':1,'products':list(self.catalog.products.values())}
        ids=['FLR-SUNFLOWER-11-BUCKET','FLR-SUNFLOWER-5-BOUQUET']
        for sku,count in zip(ids,[7,3]):
            source=dict(self.store.records['sales',sku,'2026-10-25'])
            source.update(date='2026-09-10',units_sold=count,paid_orders=count,paid_buyers=count,paid_revenue=count*50)
            document['sales'].append(source)
        for product in catalog_document['products']:
            if product['sku_id'] in ids:product['listed_at']='2026-09-01'
        self.store=DataStore.from_document(document);self.catalog=ProductCatalog.from_document(catalog_document,self.store.skus)
        task=self.parse('这次教师节向日葵买了多少，哪一款最好？')
        self.assertEqual(task['status'],'ready',task['issues'])
        with tempfile.TemporaryDirectory() as temp:
            state=execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)
            self.assertEqual(state['analysis']['display_metrics'][0]['value'],10)
            self.assertEqual(state['analysis']['ranking']['leaders'][0]['sku_id'],ids[0])
            self.assertEqual(state['validation']['status'],'passed')


if __name__=='__main__':unittest.main()
