"""口语日期与同批订单的业务回归：边界、跨日归组、缺失与冲突均不能静默放行。"""
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.natural_dates import normalize_dates
from ecommerce_agent.order_funnel import generate_order_cohorts, order_funnel
from ecommerce_agent.routing import route_question
from ecommerce_agent.runner import run
from ecommerce_agent.task_parser import ProductCatalog, parse_task
from ecommerce_agent.tools import ToolRegistry


class DatesOrdersTests(unittest.TestCase):
    """使用固定业务日期，不依赖电脑今天，也不调用收费模型。"""
    def setUp(self):
        self.raw=business_data();self.raw['order_cohorts']=generate_order_cohorts(self.raw)
        self.store=DataStore.from_document(self.raw)
        self.sku='FLR-ROSE-99-BOUQUET'
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'products.json'
        path.write_text(json.dumps(business_catalog(),ensure_ascii=False),encoding='utf-8')
        self.catalog=ProductCatalog(path,self.store.skus)

    def parse(self,question):
        """在真实目录上验证规范化日期不会破坏99/33朵商品规格。"""
        return parse_task(question,self.catalog,self.store,today=date(2026,10,28))

    def test_colloquial_dates_and_auditable_original(self):
        for text,day in [('10月25日','2026-10-25'),('10/25','2026-10-25'),('上周日','2026-10-25'),
                         ('上星期六','2026-10-24'),('本周一','2026-10-26'),('大前天','2026-10-25')]:
            question='99红玫'+text+'销量下降原因';task=self.parse(question)
            self.assertEqual(task['status'],'ready',task['issues'])
            self.assertEqual(task['target_date'],day)
            self.assertEqual(task['raw_question'],question)
            self.assertTrue(task['date_resolution'])
        full=self.parse('99红玫2025-10-25销量怎么样')
        self.assertEqual(full['target_date'],'2025-10-25')

    def test_range_month_inheritance_and_future_not_last_year(self):
        task=self.parse('99红玫10月1日至15日销量走势')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual((task['trend_start'],task['trend_end']),('2026-10-01','2026-10-15'))
        for text in ('12月25日','2月30日','31号'):
            self.assertEqual(self.parse('99红玫'+text+'销量怎么样')['status'],'needs_clarification')
        text,notes,errors=normalize_dates('12月31日',date(2026,1,2))
        self.assertEqual(text,'2026-12-31');self.assertTrue(errors)

    def test_order_queries_are_readonly_but_cancelling_is_rejected(self):
        for question in ('99红玫10月25日取消率','99红玫昨天取消订单原因','99红玫10/25完成订单怎么样'):
            task=self.parse(question);self.assertEqual(task['status'],'ready',task['issues'])
            self.assertTrue(task['include_order_funnel'])
        for question in ('帮我取消订单','99红玫昨天销量下降，帮我取消订单','分析取消率然后取消订单'):
            self.assertEqual(route_question(question)['status'],'unsupported')
        self.assertEqual(self.parse('99红玫近7天取消率')['status'],'unsupported')

    def test_cohort_partition_cutoff_late_cancellation_and_snapshot(self):
        data=order_funnel(self.store,self.sku,'2026-10-25')
        row=data['metrics'][-1]
        self.assertEqual(row['cohort_paid_orders'],sum(row[k] for k in ('cancelled_orders','completed_orders','pending_orders')))
        self.assertLessEqual(row['late_cancelled_orders'],row['cancelled_orders'])
        self.assertTrue(row['observed_at'])
        self.assertEqual(DataStore.from_document(self.store.snapshot()).snapshot(),self.store.snapshot())
        task=self.parse('99红玫10月25日取消率')
        result=run(self.store,self.sku,task['target_date'],Path(self.temp.name)/'runs',task_context=task)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['order_funnel']['metrics'][-1]['date'],'2026-10-25')
        self.assertIn('同批支付订单', (Path(result['run_dir'])/'report.md').read_text(encoding='utf-8'))

    def test_missing_zero_and_cross_table_conflict(self):
        self.store.records.pop(('order_cohorts',self.sku,'2026-10-24'))
        data=order_funnel(self.store,self.sku,'2026-10-25')
        self.assertEqual(data['metrics'][0]['status'],'unavailable');self.assertIsNone(data['changes'])
        zero=order_funnel(self.store,'FLR-GRAPE-19-BOUQUET','2026-10-25')
        self.assertIsNone(zero['metrics'][-1]['cancellation_pct'])
        self.store.records['sales',self.sku,'2026-10-25']['paid_orders']+=1
        self.assertEqual(order_funnel(self.store,self.sku,'2026-10-25')['metrics'][-1]['status'],'conflicting')

    def test_invalid_partitions_and_reason_counts_rejected(self):
        for field,value in [('completed_orders',999),('late_cancelled_orders',999),('cancellation_reasons',{'未知':999})]:
            raw=copy.deepcopy(self.raw);raw['order_cohorts'][0][field]=value
            with self.assertRaises(ValueError):DataStore.from_document(raw)

    def test_tool_permission_and_frozen_scope(self):
        registry=ToolRegistry(self.store,self.sku,'2026-10-25')
        self.assertFalse(registry.execute('get_order_funnel',{},'denied')['ok'])
        registry=ToolRegistry(self.store,self.sku,'2026-10-25',allow_orders=True)
        self.assertFalse(registry.execute('get_order_funnel',{'sku':'other'},'override')['ok'])
        self.assertTrue(registry.execute('get_order_funnel',{},'orders')['ok'])


if __name__=='__main__':unittest.main()
