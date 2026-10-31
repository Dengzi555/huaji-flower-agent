"""统一查询上下文入口回归：条件重放、旧会话恢复、异构目录与历史隔离。"""
import unittest
from copy import deepcopy
from datetime import date

import test_conversations as fixtures
from ecommerce_agent.conversations import Conversations
from ecommerce_agent.query_context import QueryContext, snapshot_scope
from ecommerce_agent.task_parser import ProductCatalog, parse_task


class QueryContextTests(unittest.TestCase):
    """使用真实临时多店数据库；不联网、不调用付费模型。"""
    setUp = fixtures.ConversationTests.setUp
    turn = fixtures.ConversationTests.turn

    def test_followup_transports_conditions_without_rewriting_question(self):
        self.turn('10月20日至25日红玫瑰哪款最好，对比上周同一天')
        follow = self.turn('只看抱抱桶')['task']
        parsed = follow['parsed']
        self.assertEqual(follow['status'], 'queued', parsed['issues'])
        self.assertEqual(parsed['conversation_context']['resolved_question'], '只看抱抱桶')
        self.assertEqual(parsed['raw_question'], '只看抱抱桶')
        self.assertEqual((parsed['trend_start'], parsed['trend_end']), ('2026-10-20', '2026-10-25'))
        self.assertEqual(parsed['comparison_date'], '2026-10-18')
        self.assertEqual(parsed['query_plan']['ranking']['direction'], 'desc')
        self.assertTrue(all(p['variety'] == '红玫瑰' and p['product_type'] == '抱抱桶' for p in parsed['candidates']))

    def test_each_store_rebinds_filter_even_when_catalogs_differ(self):
        other = self.hub.for_store('hx-cq-na')
        products = [p for p in other.catalog.products.values() if p['sku_id'] != 'FLR-ROSE-99-BOUQUET']
        other.catalog = ProductCatalog.from_document({'schema_version': 1, 'products': products}, other.store.skus)
        other._dataset = (other.store, other.catalog, other.fingerprint)
        question = '南山店和南岸店分别看10月25日红玫瑰哪款最好'
        job = self.turn(question)['conversation']['comparison']
        members = {}
        for child in job['children']:
            service = self.hub.for_store(child['store_id'])
            parsed = service.get(child['task_id'])['parsed']
            self.assertEqual(parsed['status'], 'ready', parsed['issues'])
            self.assertEqual(parsed['raw_question'], question)
            members[child['store_id']] = set(parsed['product_scope']['sku_ids'])
            self.assertTrue(members[child['store_id']] <= set(service.catalog.products))
            self.assertNotIn('sku_ids', parsed['conversation_context']['query_context']['product'])
        self.assertIn('FLR-ROSE-99-BOUQUET', members['hx-sz-ns'])
        self.assertNotIn('FLR-ROSE-99-BOUQUET', members['hx-cq-na'])

    def test_legacy_scope_survives_restart_without_mutating_old_task(self):
        original = self.turn('99红玫10月20日至25日销量怎么样')['task']
        doc = self.manager.load(self.doc['id'])
        scope = doc['stores']['hx-sz-ns']['scope']
        scope.pop('explicit_comparison', None)
        scope.pop('planning', None)
        self.manager.save(doc)
        self.manager = Conversations(self.hub, today=lambda: date(2026, 10, 26))
        current = self.turn('南岸店呢')['task']
        self.assertIsNone(current['parent_id'])
        self.assertEqual((current['parsed']['trend_start'], current['parsed']['trend_end']), ('2026-10-20', '2026-10-25'))
        self.turn('前天呢')
        saved = self.hub.for_store('hx-sz-ns').get(original['id'])
        self.assertEqual(saved['parsed'], original['parsed'])

    def test_legacy_group_selector_is_upgraded_without_copying_members(self):
        self.turn('10月25日红玫瑰哪款最好')
        doc = self.manager.load(self.doc['id'])
        group = doc['stores']['hx-sz-ns']['scope']['product_scope']
        group.pop('selector', None)
        self.manager.save(doc)
        current = self.turn('南岸店呢')['task']
        self.assertEqual(current['status'], 'queued', current['parsed']['issues'])
        self.assertIn('selector', current['parsed']['product_scope'])
        self.assertTrue(all(p['variety'] == '红玫瑰' for p in current['parsed']['candidates']))

    def test_context_only_copies_conditions_and_output_does_not_alias_history(self):
        parsed = self.turn('10月25日红玫瑰哪款最好')['task']['parsed']
        source = deepcopy(parsed)
        scope = snapshot_scope(parsed, confirmed_sku=parsed['sku_id'])
        context = QueryContext.replay(scope)
        document = context.document()
        self.assertNotIn('sku_ids', document['product'])
        self.assertNotIn('evidence_ids', document)
        document['product']['constraints']['varieties'].clear()
        self.assertEqual(parsed, source)
        self.assertTrue(context.document()['product']['constraints']['varieties'])
        with self.assertRaises(ValueError):
            QueryContext.from_document({'evidence_ids': ['old-source']})

    def test_complete_shared_plan_never_calls_model_for_missing_local_product(self):
        first = self.turn('99红玫10月25日销量怎么样')['task']['parsed']
        scope = snapshot_scope(first, confirmed_sku=first['sku_id'])
        other = self.hub.for_store('hx-cq-na')
        products = [p for p in other.catalog.products.values() if p['sku_id'] != first['sku_id']]
        catalog = ProductCatalog.from_document({'schema_version': 1, 'products': products}, other.store.skus)
        def forbidden(*args):
            raise AssertionError('本店缺商品不能再次调用模型改用别款')
        task = parse_task('这款表现怎么样', catalog, other.store, today=date(2026, 10, 26),
                          intent_override=scope['intent'], date_override=scope['date'],
                          query_context=QueryContext.replay(scope), query_planner=forbidden)
        self.assertEqual(task['status'], 'needs_clarification')
        self.assertIn('当前门店目录', '；'.join(task['issues']))

    def test_cross_store_followup_preserves_category_and_new_rank_direction(self):
        self.turn('南山店和南岸店分别看10月20日至25日哪类花最好')
        job = self.turn('两店最差呢')['conversation']['comparison']
        for child in job['children']:
            parsed = self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']
            self.assertEqual(parsed['status'], 'ready', parsed['issues'])
            self.assertTrue(parsed['is_window'])
            self.assertEqual(parsed['trend_start'], '2026-10-20')
            self.assertEqual(parsed['query_plan']['ranking']['group_by'], 'variety')
            self.assertEqual(parsed['query_plan']['ranking']['direction'], 'asc')

    def test_public_task_request_cannot_supply_internal_context(self):
        with self.assertRaises(ValueError):
            self.hub.for_store('hx-sz-ns').create({'question': '随便查', 'query_context': {'complete': True}})

    def test_shared_context_does_not_override_explicit_dates_or_write_guard(self):
        first=self.turn('99红玫10月25日销量怎么样')['task']['parsed']
        context=QueryContext.replay(snapshot_scope(first,confirmed_sku=first['sku_id']))
        service=self.hub.for_store('hx-sz-ns')
        conflict=parse_task('10月24日销量怎么样',service.catalog,service.store,
            today=date(2026,10,26),intent_override='query_sales_performance',query_context=context)
        self.assertEqual(conflict['status'],'needs_clarification')
        self.assertIn('明确目标日期不一致','；'.join(conflict['issues']))
        denied=parse_task('把这款库存改成100',service.catalog,service.store,
            today=date(2026,10,26),intent_override='query_sales_performance',query_context=context)
        self.assertEqual(denied['status'],'unsupported')
