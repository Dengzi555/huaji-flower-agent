"""主年度快照 E2E：只读提取红玫瑰样本，经扫描、SQLite 与 HTTP 验证预警。"""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path

from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService


ROOT = Path(__file__).resolve().parents[1]
SKU = 'FLR-ROSE-99-BOUQUET'
BEFORE, TARGET = '2026-10-24', '2026-10-25'


class MainSnapshotRiskE2ETests(unittest.TestCase):
    """通过实际年度加载器和 HTTP 服务检查相同的预警与预填指令。"""

    def test_red_rose_sales_drop_reaches_database_and_api(self):
        annual = ROOT / 'data' / 'annual'
        pointer = annual / 'active.json'
        if not pointer.is_file():
            self.skipTest('未安装本地主年度快照。')
        version = json.loads(pointer.read_text(encoding='utf-8'))['version']
        directory = annual / version
        stores = json.loads((ROOT / 'data' / 'stores.json').read_text(encoding='utf-8'))
        source = directory / (stores['default_store'] + '.sqlite3')
        self.assertTrue(source.is_file(), f'已激活年度快照缺少默认门店数据库：{source}')

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = root / 'red-rose.sqlite3'
            catalog = root / 'products.json'
            product = self.make_snapshot(source, directory / 'products.json', snapshot, catalog)
            service = InvestigationService(snapshot, catalog, root / 'state', root / 'runs', start_worker=False)
            try:
                service.board.scan()
                with service.connection() as db:
                    row = db.execute(
                        'SELECT * FROM risk_alerts WHERE kind=? AND sku_id=? AND observed_date=?',
                        ('sales_drop', SKU, TARGET),
                    ).fetchone()
                self.assertIsNotNone(row)
                alert = dict(row)
                self.assertIn('从 12 件降至 5 件', alert['detail'])
                self.assertIn('下降 58%', alert['detail'])

                with create_server(service, port=0) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
                    thread.start()
                    try:
                        base = f'http://127.0.0.1:{server.server_port}'
                        with urllib.request.urlopen(base + '/api/risk-alerts', timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            payload = json.load(response)
                    finally:
                        server.shutdown()
                        thread.join(timeout=5)
                returned = [item for item in payload['alerts']
                            if item['kind'] == 'sales_drop' and item['sku_id'] == SKU and item['observed_date'] == TARGET]
                self.assertEqual(returned, [alert])
                self.assertEqual(returned[0]['product_name'], product['short_name'])
                self.assertEqual(returned[0]['question'],
                                 f"调查{product['short_name']}（{SKU}）在{TARGET}的销量变化原因，"
                                 '重点核查销量、流量、库存和活动，列出证据和待确认事项。')
            finally:
                service.close()
                service.store.records.connection().close()

    def make_snapshot(self, source, catalog_source, snapshot, catalog):
        """从主库提取两天的真实样例记录，只在临时 SQLite 中模拟销量。"""
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            meta = json.loads(db.execute("SELECT value FROM metadata WHERE key='dataset'").fetchone()[0])
            records = db.execute('SELECT domain, day, body FROM records WHERE sku=? AND day BETWEEN ? AND ?',
                                 (SKU, BEFORE, TARGET)).fetchall()

        dates = {}
        seen = set()
        copied = []
        for domain, day, body in records:
            dates.setdefault(domain, []).append(day)
            if domain == 'sales':
                row = json.loads(body)
                self.assertEqual(row['status'], 'ok')
                units = {BEFORE: 12, TARGET: 5}[day]
                row['units_sold'] = units
                # 保持支付买家数 ≤ 订单数 ≤ 件数；预警只使用件数。
                row['paid_orders'] = min(row['paid_orders'], units)
                row['paid_buyers'] = min(row['paid_buyers'], row['paid_orders'])
                body = json.dumps(row, ensure_ascii=False)
                seen.add(day)
            copied.append((domain, SKU, day, body))
        self.assertEqual(seen, {BEFORE, TARGET}, '主快照缺少红玫瑰的两日销量记录')
        meta.update(skus=[SKU], dates=dates, content_sha256='red-rose-e2e-' + meta['content_sha256'])
        with closing(sqlite3.connect(snapshot)) as db, db:
            db.executescript('''
                CREATE TABLE records(domain TEXT, sku TEXT, day TEXT, body TEXT,
                    PRIMARY KEY(domain, sku, day)) WITHOUT ROWID;
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            ''')
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('dataset', json.dumps(meta, ensure_ascii=False)))
            db.executemany('INSERT INTO records VALUES (?, ?, ?, ?)', copied)

        document = json.loads(catalog_source.read_text(encoding='utf-8'))
        product = next(row for row in document['products'] if row['sku_id'] == SKU)
        document['products'] = [product]
        catalog.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        return product
