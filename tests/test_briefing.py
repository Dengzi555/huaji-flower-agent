"""门店简报：同星期参考、绝对损失、缺货/访客信号与任务草稿。

使用自建小快照，不依赖 gitignore 的年度数据；同时覆盖新人、正常款与 CLI 落盘。
"""
import json
import shutil
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from ecommerce_agent.briefing import run_briefing, scan, write_briefing
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog

TARGET = '2026-09-13'
START = '2026-08-01'


def _row(sku, day, **extra):
    base = {'sku_id': sku, 'date': day, 'source_id': 'briefing-test',
            'observed_at': '2026-09-14T08:00:00+08:00', 'status': 'ok'}
    return {**base, **extra}


def _document():
    """四周同星期历史（周日 20 件）+ 目标日大跌；另放正常款、下降款和新人。"""
    days = []
    current, end = date.fromisoformat(START), date.fromisoformat(TARGET)
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    sales, traffic, inventory, promotions = [], [], [], []
    for day in days:
        units = {'A': 20, 'B': 10, 'D': 10}
        if day == TARGET:
            units = {'A': 6, 'B': 10, 'D': 1}
        for sku, count in units.items():
            sales.append(_row(sku, day, paid_orders=count, paid_buyers=count,
                              units_sold=count, paid_revenue=count * 10))
            traffic.append(_row(sku, day, visitors=120 if sku == 'A' and day == TARGET else
                                400 if sku == 'A' else 100))
            inventory.append(_row(sku, day, out_of_stock_minutes=480 if sku == 'A' and day == TARGET else 0,
                                  monitored_minutes=1440))
            promotions.append(_row(sku, day, active=False, campaign_name=''))
    return {'schema_version': 1, 'sample_data': False, 'timezone': 'Asia/Shanghai', 'currency': 'CNY',
            'known_skus': ['A', 'B', 'C', 'D', 'E'],
            'sales': sales, 'traffic': traffic, 'inventory': inventory, 'promotions': promotions}


CATALOG = {'schema_version': 1, 'products': [
    {'sku_id': 'A', 'name': '测试玫瑰A', 'variant': '19朵', 'aliases': ['玫瑰A'], 'business_tags': ['常规款'],
     'listed_at': '2025-01-01'},
    {'sku_id': 'B', 'name': '测试款B', 'variant': '11朵', 'aliases': [], 'business_tags': ['爆款'],
     'listed_at': '2025-01-01'},
    {'sku_id': 'C', 'name': '新品尝鲜C', 'variant': '9朵', 'aliases': [], 'business_tags': ['常规款'],
     'listed_at': '2026-09-01'},
    {'sku_id': 'D', 'name': '测试款D', 'variant': '33朵', 'aliases': [], 'business_tags': ['常规款'],
     'listed_at': '2025-01-01'},
    {'sku_id': 'E', 'name': '缺数据E', 'variant': '1朵', 'aliases': [], 'business_tags': ['常规款'],
     'listed_at': '2025-01-01'},
]}


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / 'runs/_testtmp' / ('brief_' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.data = self.root / 'store.json'
        self.catalog = self.root / 'products.json'
        self.data.write_text(json.dumps(_document(), ensure_ascii=False), encoding='utf-8')
        self.catalog.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding='utf-8')
        self.store = DataStore(self.data)
        self.products = ProductCatalog(self.catalog, self.store.skus)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scan_ranks_drops_and_skips_new_listing(self):
        """A 同时有昨日损失、同星期偏离、缺货与访客骤降；D 只有损失；B 正常；C 新人。"""
        result = scan(self.store, self.products, TARGET, limit=5, store_id='test-store')
        items = result['items']
        self.assertEqual([item['sku_id'] for item in items], ['A', 'D'])
        self.assertEqual(result['counts']['scanned'], 5)
        self.assertEqual(result['counts']['candidates'], 2)
        self.assertEqual(result['counts']['new_listing'], 1)
        self.assertEqual(result['counts']['unavailable'], 1)
        first = items[0]
        self.assertEqual((first['previous_units'], first['target_units'], first['loss_units']), (20, 6, 14))
        self.assertEqual(first['weekly']['n'], 4)
        self.assertEqual(first['weekly']['median_units'], 20)
        self.assertEqual(first['weekly']['target_vs_weekly_pct'], -70.0)
        types = {signal['type'] for signal in first['signals']}
        self.assertEqual(types, {'sales_drop_prev_day', 'sales_drop_weekly', 'stockout', 'traffic_drop'})
        self.assertGreater(first['priority'], items[1]['priority'])
        self.assertEqual(first['calendar']['day_type'], 'weekend_rest')

    def test_limit_keeps_counts_separate_from_output(self):
        """候选数与输出数分开记录：截断不改变“扫到了几条”。"""
        result = scan(self.store, self.products, TARGET, limit=1)
        self.assertEqual(result['counts']['candidates'], 2)
        self.assertEqual(result['counts']['returned'], 1)
        self.assertEqual(len(result['items']), 1)

    def test_tasks_and_markdown_are_written(self):
        """任务草稿字段可直接 POST；Markdown 同时给出同星期参考与命令。"""
        result = scan(self.store, self.products, TARGET, limit=5)
        result['data_path'] = str(self.data)
        from ecommerce_agent.briefing import build_tasks
        build_tasks(result, str(self.data))
        paths = write_briefing(result, self.root / 'out')
        self.assertTrue(paths['json'].exists() and paths['markdown'].exists())
        self.assertEqual(len(paths['tasks']), 2)
        payload = json.loads(paths['tasks'][0].read_text(encoding='utf-8'))
        self.assertEqual(payload['sku'], 'A')
        self.assertEqual(payload['date'], TARGET)
        self.assertIn('销量下降原因', payload['question'])
        markdown = paths['markdown'].read_text(encoding='utf-8')
        self.assertIn('同星期参考：中位数 20', markdown)
        self.assertIn('investigate --data', markdown)

    def test_run_briefing_cli_writes_json_report(self):
        """run_briefing 走完整 CLI 路径：解析快照、扫描、落盘，并返回 0。"""
        args = SimpleNamespace(data=self.data, catalog=self.catalog, store=None, date=TARGET,
                               annual=self.root, limit=5, output=self.root / 'cli', json=True)
        with redirect_stdout(StringIO()):
            self.assertEqual(run_briefing(args), 0)
        report = json.loads((self.root / 'cli/briefing.json').read_text(encoding='utf-8'))
        self.assertEqual(report['store_id'], 'store')
        self.assertEqual(report['counts']['returned'], 2)


if __name__ == '__main__':
    unittest.main()