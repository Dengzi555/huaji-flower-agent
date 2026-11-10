"""验证运营日程持久化和风险扫描的业务口径。"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from unittest.mock import patch

from ecommerce_agent.data import DataStore
from ecommerce_agent.operations_board import SCHEMA as BOARD_SCHEMA
from ecommerce_agent.web_service import InvestigationService


class OperationsBoardTests(unittest.TestCase):
    """使用隔离 SQLite 与合成快照，避免修改日常工作区。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = self.make_service('first')

    def make_service(self, name):
        service = InvestigationService(Path('data/sample.json'), Path('data/products.json'),
                                       self.root / name, self.root / 'runs', start_worker=False)
        self.addCleanup(service.close)
        return service

    def test_calendar_marks_holidays_and_persists_store_local_plans(self):
        board = self.service.board
        self.assertIn('2026-02-14', board.month('2026-02')['holidays'])
        self.assertIn('2026-09-10', board.month('2026-09')['holidays'])
        for kind in ('stocking', 'staffing'):
            board.add_plan({'date': '2026-02-14', 'kind': kind, 'title': '测试计划'})
        with self.assertRaises(ValueError):
            board.add_plan({'date': '2026-02-30', 'kind': 'stocking', 'title': '无效日期'})
        self.assertEqual(len(self.make_service('first').board.month('2026-02')['plans']), 2)
        board.add_plan({'date': '2026-01-20', 'kind': 'staffing', 'title': '跨月计划'})
        restored = self.make_service('first').board
        self.assertEqual([plan['plan_date'] for plan in restored.list_plans()],
                         ['2026-01-20', '2026-02-14', '2026-02-14'])
        self.assertEqual(self.make_service('second').board.list_plans(), [])

    def test_scan_deduplicates_and_generates_scoped_investigation_prompts(self):
        board = self.service.board
        board.scan()
        first = board.alerts()
        board.scan()
        self.assertEqual(board.alerts(), first)
        alerts = [alert for alert in first['alerts']
                  if alert['sku_id'] == 'FLR-ROSE-99-BOUQUET' and alert['observed_date'] == '2026-10-25']
        self.assertEqual({alert['kind'] for alert in alerts}, {'activity_gap', 'low_stock', 'sales_drop'})
        self.assertTrue(all('2026-10-25' in alert['question'] and alert['sku_id'] in alert['question']
                            for alert in alerts))



    def test_legacy_annual_rows_degrade_independently(self):
        """旧年度库直接解码 JSON，缺字段不能中止另外两类扫描。"""
        sku = 'FLR-ROSE-99-BOUQUET'
        day = '2026-10-25'
        before = '2026-10-24'
        annual = self.root / 'legacy.sqlite3'
        meta = {
            'schema': 'flower-annual-sqlite-v1',
            'meta': self.service.store.meta,
            'skus': sorted(self.service.store.skus),
            'dates': {table: [before, day] for table in ('sales', 'inventory', 'promotions')},
            'content_sha256': 'legacy-fixture',
        }
        rows = {
            ('inventory', before): {'stock_on_hand': 3},
            ('inventory', day): {'out_of_stock_minutes': 90, 'monitored_minutes': 720},
            ('promotions', before): {'active': True, 'campaign_ended_at': before + 'T18:00:00+08:00'},
            ('promotions', day): {},
            ('sales', before): {'units_sold': 50},
            ('sales', day): {'units_sold': 20},
        }
        with closing(sqlite3.connect(annual)) as db, db:
            db.executescript('CREATE TABLE records(domain, sku, day, body); CREATE TABLE metadata(key, value);')
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('dataset', json.dumps(meta)))
            db.executemany('INSERT INTO records VALUES (?, ?, ?, ?)', [
                (table, sku, observed_date, json.dumps({
                    'sku_id': sku, 'date': observed_date, 'status': 'ok',
                    'source_id': 'annual-test', 'observed_at': '2026-10-26T08:00:00+08:00', **fields,
                }))
                for (table, observed_date), fields in rows.items()
            ])
        service = InvestigationService(annual, Path('data/products.json'),
                                       self.root / 'legacy', self.root / 'runs', start_worker=False)
        self.addCleanup(service.close)
        self.addCleanup(service.store.records.connection().close)
        with self.assertLogs('ecommerce_agent.operations_board', level='WARNING') as warnings:
            service.board.scan()
            service.board.scan()
        self.assertEqual(len(warnings.records), 1)
        alerts = service.board.alerts()['alerts']
        self.assertEqual({(alert['kind'], alert['observed_date']) for alert in alerts}, {
            ('low_stock', before), ('sales_drop', day),
        })
        self.assertTrue(any('默认安全线 8 件' in alert['detail'] for alert in alerts))
        self.assertFalse(any('缺货 90 分钟' in alert['detail'] for alert in alerts))
        service.store.records.connection().close()

        with closing(sqlite3.connect(annual)) as db, db:
            body = {'sku_id': sku, 'date': day, 'status': 'ok', 'source_id': 'annual-test',
                    'observed_at': '2026-10-26T08:00:00+08:00'}
            db.execute('UPDATE records SET body=? WHERE domain=? AND day=?', (json.dumps(body), 'sales', day))
        reopened = InvestigationService(annual, Path('data/products.json'),
                                        self.root / 'legacy', self.root / 'runs', start_worker=False)
        self.addCleanup(reopened.close)
        self.addCleanup(reopened.store.records.connection().close)
        with self.assertLogs('ecommerce_agent.operations_board', level='WARNING'):
            reopened.board.scan()
        self.assertTrue(all(alert['kind'] == 'low_stock' for alert in reopened.board.alerts()['alerts']))

    def test_repeated_sales_alert_updates_in_place_and_preserves_unchanged_timestamp(self):
        """同日 50%→60% 覆盖原行；重复读到 60% 时保留上次变化时间。"""
        sku = 'FLR-ROSE-99-BOUQUET'
        before, day = '2026-10-24', '2026-10-25'
        morning = '2026-10-25T01:00:00+00:00'
        afternoon = '2026-10-25T06:00:00+00:00'
        later = '2026-10-25T06:01:00+00:00'
        document = self.service.store.snapshot()
        sales = {row['date']: row for row in document['sales']
                 if row['sku_id'] == sku and row['date'] in (before, day)}

        def scan(units, timestamp):
            for date, count in ((before, 20), (day, units)):
                sales[date].update(units_sold=count, paid_orders=count, paid_buyers=count)
            store = DataStore.from_document(document)
            with patch.object(self.service, 'dataset', return_value=(store, self.service.catalog, 'same-day')), \
                    patch('ecommerce_agent.operations_board.utc_now', return_value=timestamp):
                self.service.board.scan()
            with self.service.connection() as db:
                return [dict(row) for row in db.execute(
                    "SELECT rowid, detail, updated_at FROM risk_alerts WHERE kind='sales_drop' AND sku_id=? AND observed_date=?",
                    (sku, day),
                )]

        first = scan(10, morning)
        second = scan(8, afternoon)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]['rowid'], second[0]['rowid'])
        self.assertIn('下降 50%', first[0]['detail'])
        self.assertEqual(first[0]['updated_at'], morning)
        self.assertIn('下降 60%', second[0]['detail'])
        self.assertEqual(second[0]['updated_at'], afternoon)
        self.assertEqual(scan(8, later), second)
        self.assertEqual(scan(12, later), [])

    def test_existing_board_database_adds_updated_at_without_losing_rows(self):
        """旧工作区首次启动时保留预警，首次扫描再给记录更新时间。"""
        path = self.root / 'legacy-schema'
        path.mkdir()
        with closing(sqlite3.connect(path / 'tasks.sqlite3')) as db, db:
            db.executescript(BOARD_SCHEMA.replace('    updated_at TEXT,\n', ''))
            db.execute('INSERT INTO risk_alerts VALUES (?, ?, ?, ?, ?, ?)',
                       ('sales_drop', 'FLR-ROSE-99-BOUQUET', '2026-10-25', '炽热爱意', '旧预警', '旧指令'))
        service = self.make_service('legacy-schema')
        with service.connection() as db:
            row = db.execute('SELECT detail, updated_at FROM risk_alerts').fetchone()
        self.assertEqual(tuple(row), ('旧预警', None))
        service.board.scan()
        with service.connection() as db:
            row = db.execute("SELECT detail, updated_at FROM risk_alerts WHERE kind='sales_drop' AND sku_id=? AND observed_date=?",
                             ('FLR-ROSE-99-BOUQUET', '2026-10-25')).fetchone()
        self.assertIn('下降', row['detail'])
        self.assertIsNotNone(row['updated_at'])

    def test_stockout_noise_threshold_and_latest_sku_aggregation(self):
        """120 分钟不报警，121 分钟即使库存充足仍报警，同 SKU 只保留最新。"""
        sku = 'FLR-ROSE-99-BOUQUET'
        mild = 'FLR-ROSE-19-BOUQUET'
        before, day = '2026-10-24', '2026-10-25'
        document = self.service.store.snapshot()
        rows = [row for row in document['inventory'] if row['sku_id'] not in (sku, mild)]

        def record(sku_id, observed_date, minutes, quantity):
            return {'sku_id': sku_id, 'date': observed_date, 'status': 'ok', 'source_id': 'fixture',
                    'observed_at': '2026-10-26T08:00:00+08:00', 'stock_on_hand': quantity,
                    'safety_stock': 8, 'out_of_stock_minutes': minutes, 'monitored_minutes': 720}

        document['inventory'] = rows + [record(sku, before, 180, 20), record(sku, day, 121, 20),
                                        record(mild, day, 120, 20)]
        store = DataStore.from_document(document)
        with self.service.connection() as db:
            db.executemany('INSERT INTO risk_alerts (kind, sku_id, observed_date, product_name, detail, question) '
                           'VALUES (?, ?, ?, ?, ?, ?)', [
                               ('low_stock', sku, before, '红玫瑰', '旧告警', '旧任务'),
                               ('low_stock', mild, day, '红玫瑰', '旧告警', '旧任务'),
                           ])
        with patch.object(self.service, 'dataset', return_value=(store, self.service.catalog, 'noise-fixture')):
            self.service.board.scan()
        with self.service.connection() as db:
            alerts = db.execute("SELECT sku_id, observed_date, detail FROM risk_alerts WHERE kind='low_stock' "
                                "AND sku_id IN (?, ?)", (sku, mild)).fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['sku_id'], alerts[0]['observed_date']), (sku, day))
        self.assertIn('累计缺货 121 分钟', alerts[0]['detail'])
        self.assertIn('连续缺货 2 天', alerts[0]['detail'])
