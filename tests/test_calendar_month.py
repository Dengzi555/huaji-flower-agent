"""自然月和类别范围回归：用户已给时间和花材时不得重复索要单SKU/日期。"""
import unittest
from pathlib import Path
from datetime import date
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.natural_dates import normalize_dates

class CalendarMonthTests(unittest.TestCase):
    def test_last_month_category_not_single_product(self):
        root=Path(__file__).resolve().parents[1];s=DataStore(root/'data/stores/hx-cq-na.json');c=ProductCatalog(root/'data/products.json',s.skus)
        for text in ['上个月红玫瑰卖的怎么样，分析一下','上月红玫瑰销量表现']:
            t=parse_task(text,c,s,today=date(2026,10,29))
            self.assertEqual(t['status'],'ready',t['issues'])
            self.assertEqual((t['trend_start'],t['trend_end']),('2026-09-01','2026-09-30'))
            self.assertEqual(len(t['product_scope']['sku_ids']),3)
        t=parse_task('上个月红梅卖的怎么样，分析一下',c,s,today=date(2026,10,29))
        self.assertEqual(t['target_date'],'2026-09-30');self.assertTrue(t['group_candidate'])
        self.assertFalse(any('补充目标日期' in issue or '多个 SKU' in issue for issue in t['issues']))

    def test_year_rollover_and_leap_month(self):
        for today,expected in [(date(2026,1,8),'2025-12-01至2025-12-31'),(date(2024,3,8),'2024-02-01至2024-02-29')]:
            self.assertEqual(normalize_dates('上个月',today)[0],expected)
