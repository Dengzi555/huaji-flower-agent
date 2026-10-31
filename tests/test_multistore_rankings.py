"""原句端到端回归：情人节+全部门店+全商品排名，不要求用户指定SKU、不执行半句。"""
import json
import hashlib
import unittest
from datetime import date
import test_conversations as fixtures
from ecommerce_agent.annual_data import annual_catalog,daily_document
from ecommerce_agent.store_workspace import STORES
from ecommerce_agent.data import DataStore,FIELDS
from ecommerce_agent.task_parser import ProductCatalog


class MultiStoreRankingsTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def seasonal_data(self,include_future=False):
        # 固定种子、真实生产数据契约，只缩小目录使测试快速；不篡改被测工具结果。
        products=[p for p in annual_catalog()['products'] if p['listed_at']<='2026-02-13' and (not p.get('retired_at') or p['retired_at']>='2026-02-14')][:12]
        if include_future:products.append(next(p for p in annual_catalog()['products'] if p['listed_at']>'2026-02-14'))
        for index,shop in enumerate(STORES):
            docs=[daily_document(products,shop,index,date(2026,2,day),'2026-10-28',20261029)[0] for day in (13,14)]
            document=dict(docs[-1])
            document['known_skus']=[p['sku_id'] for p in products]
            for field in FIELDS:document[field]=[r for d in docs for r in d.get(field,[])]
            service=self.hub.for_store(shop['id']);service.store=DataStore.from_document(document)
            service.catalog=ProductCatalog.from_document({'schema_version':1,'products':products},service.store.skus)
            service.fingerprint=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
            service._dataset=(service.store,service.catalog,service.fingerprint)

    def finish(self,job):
        for child in job['children']:
            service=self.hub.for_store(child['store_id']);task=service.get(child['task_id'])
            self.assertEqual(task['status'],'queued',task['parsed']['issues'])
            pair=service.executor._claim(child['task_id'])
            parsed,token=pair;state=service.executor._execute(child['task_id'],parsed,token)
            service.executor._settle(child['task_id'],state,token)
        return self.manager.get(self.doc['id'])['comparison']

    def test_original_question_returns_each_store_champion_without_sku_followup(self):
        self.seasonal_data();question='今年情人节哪款卖的最好，所有的店都看下'
        result=self.turn(question)
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        job=result['conversation']['comparison'];self.assertEqual(len(job['children']),len(STORES))
        for child in job['children']:
            task=self.hub.for_store(child['store_id']).get(child['task_id'])
            self.assertEqual(task['parsed']['target_date'],'2026-02-14')
            self.assertEqual(task['parsed']['sku_id'],'__all_matched__')
            self.assertEqual(task['parsed']['query_plan']['ranking']['direction'],'desc')
        view=self.finish(job)
        self.assertEqual(view['comparable_count'],len(STORES))
        for row in view['rows']:
            self.assertTrue(row['ranking']['leaders']);self.assertTrue(row['evidence_ids'])
            self.assertEqual(row['ranking']['leaders'][0]['units_sold'],max(i['units_sold'] for i in row['ranking']['rows']))

    def test_existing_single_product_does_not_leak_into_all_product_ranking(self):
        self.seasonal_data();self.turn('99红玫2月13日销量怎么样')
        result=self.turn('情人节哪款卖的最好，所有店都看下')
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        for child in result['conversation']['comparison']['children']:
            task=self.hub.for_store(child['store_id']).get(child['task_id'])
            self.assertEqual(task['parsed']['sku_id'],'__all_matched__')
            self.assertGreater(len(task['parsed']['product_scope']['sku_ids']),1)

    def test_future_products_are_inactive_not_missing_historical_sales(self):
        self.seasonal_data(include_future=True)
        job=self.turn('今年情人节哪款卖的最好，所有的店都看下')['conversation']['comparison']
        view=self.finish(job)
        self.assertEqual(view['comparable_count'],len(STORES))
        self.assertTrue(all(row['ranking']['complete_scope'] for row in view['rows']))

    def test_missing_sales_and_tied_leaders_are_explicit(self):
        self.seasonal_data()
        shop=self.hub.for_store('hx-sz-ns');ids=sorted(shop.catalog.products)
        for sku in ids[:2]:shop.store.records['sales',sku,'2026-02-14']['units_sold']=100000
        missing=self.hub.for_store('hx-cq-na');del missing.store.records['sales',ids[0],'2026-02-14']
        job=self.turn('今年情人节哪款卖的最好，所有的店都看下')['conversation']['comparison'];view=self.finish(job)
        good=next(r for r in view['rows'] if r['store_id']=='hx-sz-ns');bad=next(r for r in view['rows'] if r['store_id']=='hx-cq-na')
        self.assertEqual(len(good['ranking']['leaders']),2)
        self.assertFalse(bad['comparable']);self.assertFalse(bad['ranking']['complete_scope'])
        self.assertEqual(view['comparable_count'],len(STORES)-1)

    def test_multiday_dual_ranking_and_explicit_baseline_are_preserved(self):
        self.seasonal_data()
        result=self.turn('2月13日至14日哪款最好哪款最差，所有的店都看下')
        self.assertIn('comparison',result['conversation'],result['conversation']['notice'])
        for child in result['conversation']['comparison']['children']:
            parsed=self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']
            self.assertTrue(parsed['is_window']);self.assertEqual(parsed['trend_start'],'2026-02-13')
            self.assertEqual(parsed['query_plan']['ranking']['direction'],'both')
        changed=self.turn('2月14日所有商品销量怎么样，对比2月12日，所有店都看下')
        self.assertIn('comparison',changed['conversation'],changed['conversation']['notice'])
        for child in changed['conversation']['comparison']['children']:
            parsed=self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']
            self.assertEqual(parsed['comparison_date'],'2026-02-12')


if __name__=='__main__':unittest.main()
