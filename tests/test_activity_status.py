"""活动证据、范围约束与报告回归，使用合成快照。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.model_runtime import model_scope
from ecommerce_agent.roles import ROLES
from ecommerce_agent.task_admission import prepare_task
from ecommerce_agent.task_parser import ProductCatalog
from ecommerce_agent.tools import ACTIVITY_TOOL, ToolRegistry, check_activity_status
from ecommerce_agent.workflows import execute_task


SKU = 'FLR-ROSE-99-BOUQUET'
DAY = '2026-10-25'


class ActivityStatusTests(unittest.TestCase):
    """覆盖确认断档、续接、数据缺失与越出冻结范围的请求。"""

    def test_activity_observations_from_checked_in_demo(self):
        data = check_activity_status(DataStore(Path('data/sample.json')), SKU, DAY)
        self.assertEqual(data['campaign_ended_at'], '2026-10-24T18:00:00+08:00')
        self.assertTrue(data['expired_without_renewal'])
        self.assertTrue(data['resource_slot_lost'])
        self.assertEqual(data['records'][0]['campaign_name'], '情人节大促满减')

    def test_renewed_or_missing_activity_is_not_reported_as_a_gap(self):
        document = business_data()
        current = next(row for row in document['promotions'] if row['sku_id'] == SKU and row['date'] == DAY)
        current.update(active=True, campaign_name='次日满减', resource_slot_active=True)
        renewed = check_activity_status(DataStore.from_document(document), SKU, DAY)
        self.assertFalse(renewed['expired_without_renewal'])
        self.assertFalse(renewed['resource_slot_lost'])

        document['promotions'].remove(current)
        missing = check_activity_status(DataStore.from_document(document), SKU, DAY)
        self.assertFalse(missing['observation_complete'])
        self.assertFalse(missing['expired_without_renewal'])
        self.assertEqual(missing['records'][1]['status'], 'unavailable')

    def test_product_role_cannot_finish_without_scoped_activity_evidence(self):
        registry = ToolRegistry(DataStore.from_document(business_data()), SKU, DAY,
                                role=ROLES['product_investigator'])
        with self.assertRaisesRegex(ValueError, '无权'):
            ToolRegistry(registry.store, SKU, DAY, role=ROLES['supply_buyer']).execute(
                ACTIVITY_TOOL, {'sku_id': SKU, 'date': DAY}, 'denied')
        wrong = registry.execute(ACTIVITY_TOOL, {'sku_id': SKU, 'date': '2026-10-24'}, 'wrong-day')
        self.assertFalse(wrong['ok'])
        self.assertIn('冻结任务', wrong['error'])
        denied = registry.execute('finish', {'summary': '结论', 'evidence_ids': []}, 'too-early')
        self.assertIn('必须先调用', denied['error'])
        self.assertTrue(registry.execute(ACTIVITY_TOOL, {'sku_id': SKU, 'date': DAY}, 'activity')['ok'])

    def test_completed_report_includes_activity_evidence_and_follow_up(self):
        store = DataStore.from_document(business_data())
        catalog = ProductCatalog.from_document(business_catalog(), store.skus)
        with model_scope():
            task = prepare_task({'question': '99红玫10月25日销量下降原因', 'sku': SKU}, store, catalog,
                                'fixture', today=date(2026, 10, 29)).parsed
        with tempfile.TemporaryDirectory() as folder:
            state = execute_task(store, SKU, DAY, folder, task_context=task)
            report = (Path(state['run_dir']) / 'report.md').read_text(encoding='utf-8')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['activity_status']['tool'], ACTIVITY_TOOL)
        self.assertIn('活动断档，需尽快联系运营续报', report)
        self.assertIn('销量下降的优先核查线索', report)
