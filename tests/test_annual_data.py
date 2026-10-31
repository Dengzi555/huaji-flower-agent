"""年度数据检验：可复现、生命周期、代数勾稽，以及磁盘适配的范围冻结。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.annual_data import annual_catalog,daily_document,canonical
from ecommerce_agent.store_workspace import STORES
from ecommerce_agent.data import DataStore,FIELDS
from ecommerce_agent.business_calendar import day_context


class AnnualDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog=annual_catalog()

    def day(self,day,index=0):
        return daily_document(self.catalog['products'],STORES[index],index,date.fromisoformat(day),'2026-10-28',20261029)

    def test_catalog_and_daily_generation_are_repeatable(self):
        self.assertEqual(len(self.catalog['products']),320)
        self.assertEqual(len({p['short_name'] for p in self.catalog['products']}),320)
        self.assertEqual(canonical(self.catalog),canonical(annual_catalog()))
        a,_=self.day('2026-05-10');b,_=self.day('2026-05-10')
        self.assertEqual(canonical(a),canonical(b))
        self.assertNotEqual(canonical(a),canonical(daily_document(self.catalog['products'],STORES[0],0,date(2026,5,10),'2026-10-28',123)[0]))

    def test_all_store_counts_money_channels_and_advertising_reconcile(self):
        totals=[]
        for index in range(len(STORES)):
            document,ledger=self.day('2026-05-10',index);store=DataStore.from_document(document)
            totals.append(sum(r['paid_orders'] for r in document['sales']))
            self.assertEqual(sum(round(r['paid_revenue']*100) for r in document['sales']),sum(r['revenue_cents'] for r in ledger['revenue']))
            self.assertEqual(totals[-1],sum(r['paid_orders'] for r in ledger['revenue']))
            for row in document['traffic_channels']:
                self.assertEqual(sum(row['attributed_visitors'].values()),store.daily_record('traffic',row['sku_id'],row['date'])['visitors'])
            self.assertLessEqual(sum(r['attributed_visitors']['付费广告'] for r in document['traffic_channels']),sum(r['clicks'] for r in ledger['ads']))
            self.assertTrue(all(r['spend_cents']==r['clicks']*r['unit_cpc_cents'] for r in ledger['ads']))
        self.assertGreater(max(totals),min(totals)*5)

    def test_known_interventions_and_new_product_not_zero_filled(self):
        document,_=self.day('2026-10-25');store=DataStore.from_document(document)
        self.assertEqual(store.daily_record('sales','FLR-ROSE-99-BOUQUET','2026-10-25')['paid_orders'],23)
        self.assertEqual(store.daily_record('inventory','FLR-ROSE-99-BOUQUET','2026-10-25')['out_of_stock_minutes'],480)
        self.assertEqual(store.daily_record('traffic','FLR-ROSE-33-BUCKET','2026-10-25')['status'],'delayed')
        self.assertEqual(store.daily_record('inventory','FLR-ROSE-33-BUCKET','2026-10-25')['status'],'unavailable')
        self.assertEqual(store.daily_record('order_cohorts','FLR-WUMEI-19-BOUQUET','2026-10-25')['cancelled_orders'],12)
        before,_=self.day('2026-10-24')
        self.assertFalse(any(r['sku_id']=='FLR-EUSTOMA-11-BOUQUET' for r in before['sales']))

    def test_lifecycle_never_produces_prelaunch_or_postretirement_rows(self):
        products={p['sku_id']:p for p in self.catalog['products']}
        for day in ('2025-10-29','2026-02-14','2026-09-10','2026-10-28'):
            document,_=self.day(day)
            for row in document['sales']:
                product=products[row['sku_id']]
                self.assertLessEqual(product['listed_at'],day)
                if product.get('retired_at'):self.assertGreaterEqual(product['retired_at'],day)
        self.assertTrue(day_context('2025-12-25')['schedule_known'])
        self.assertTrue(day_context('2026-02-14')['is_makeup_workday'])
        self.assertTrue(day_context('2026-02-14')['occasions'])

    def test_sqlite_adapter_only_freezes_requested_scope(self):
        document,_=self.day('2026-10-25');sku='FLR-ROSE-99-BOUQUET'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'annual.sqlite3';db=sqlite3.connect(path)
            db.executescript('CREATE TABLE records(domain TEXT,sku TEXT,day TEXT,body TEXT,PRIMARY KEY(domain,sku,day)); CREATE TABLE metadata(key TEXT,value TEXT);')
            for table in FIELDS:
                db.executemany('INSERT INTO records VALUES(?,?,?,?)',[(table,r['sku_id'],r['date'],canonical(r)) for r in document[table]])
            metadata={'schema':'flower-annual-sqlite-v1','meta':{k:v for k,v in document.items() if k not in FIELDS},
                'skus':[r['sku_id'] for r in document['sales']]+['UNLISTED-TEST'],'dates':{t:['2026-10-25'] for t in FIELDS},'content_sha256':'test-version'}
            db.execute('INSERT INTO metadata VALUES(?,?)',('dataset',canonical(metadata)));db.commit();db.close()
            store=DataStore(path)
            self.assertEqual(store.daily_record('sales',sku,'2026-10-25')['paid_orders'],23)
            snapshot=store.snapshot(sku_ids=[sku,'UNLISTED-TEST'],start='2026-10-25',end='2026-10-25')
            self.assertEqual(len(snapshot['sales']),1)
            frozen=DataStore.from_document(snapshot)
            self.assertIn('UNLISTED-TEST',frozen.skus)
            self.assertEqual(frozen.daily_record('sales','UNLISTED-TEST','2026-10-25')['status'],'unavailable')
            with self.assertRaises(ValueError):store.snapshot()
            self.assertEqual(store.dates(),['2026-10-25'])
            store.records.connection().close()


if __name__=='__main__':unittest.main()
