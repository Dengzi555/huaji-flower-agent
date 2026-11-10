"""审批草稿的门店列表、策略校验和人工状态回归。"""

import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from ecommerce_agent.decisions import DecisionConflict
from ecommerce_agent.guardrails.policy_checker import check_action
from ecommerce_agent.operations import OperatingConflict
from ecommerce_agent.web_service import InvestigationService


class ApprovalDraftTests(unittest.TestCase):
    """用本地工作区 SQLite 验证批准、拒绝与重复提交。"""

    def test_pending_list_and_server_side_policy_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = InvestigationService(Path('data/sample.json'), Path('data/products.json'),
                                           root / 'state', root / 'runs', start_worker=False)
            try:
                blocked = self.save_report(service, 80)
                pending = self.save_report(service, 85)
                drafts = {draft['id']: draft for draft in service.approval_drafts.list_pending()}
                self.assertEqual(set(drafts), {blocked, pending})
                self.assertEqual(drafts[blocked]['status'], 'pending')
                self.assertEqual(drafts[blocked]['policy']['status'], 'blocked')
                self.assertIn('85%', drafts[blocked]['policy']['reason'])
                with self.assertRaisesRegex(DecisionConflict, '85%'):
                    service.approval_drafts.decide(blocked, {'action': 'approve'})
                self.assertEqual(service.approval_drafts.decide(blocked, {'action': 'reject'})['status'], 'rejected')
                for _ in range(2):
                    self.assertEqual(service.approval_drafts.decide(pending, {'action': 'approve'})['status'], 'approved')
                with self.assertRaises(DecisionConflict):
                    service.approval_drafts.decide(pending, {'action': 'reject'})
                self.assertEqual(service.approval_drafts.list_pending(), [])
            finally:
                service.close()

    def test_policy_changes_recheck_pending_drafts_and_stay_store_local(self):
        """验证待审批草稿按最新规则复核，且规则仅影响当前门店。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(InvestigationService(Path('data/sample.json'), Path('data/products.json'),
                                      root / 'a', root / 'runs-a', start_worker=False)) as first:
                with closing(InvestigationService(Path('data/sample.json'), Path('data/products.json'),
                                          root / 'b', root / 'runs-b', start_worker=False)) as second:
                    formerly_allowed = self.save_report(first, 85)
                    settings = {'expected_revision': 0, 'min_discount_percent': 90,
                                'max_ad_budget_cents': 200_050}
                    updated = first.operations.save_approval_policy(settings)
                    self.assertEqual(first.operations.settings()['approval_policy']['max_ad_budget_cents'], 200_050)
                    self.assertEqual(second.operations.approval_policy()['min_discount_percent'], 85)
                    with self.assertRaises(OperatingConflict):
                        first.operations.save_approval_policy(settings)
                    with self.assertRaisesRegex(DecisionConflict, '90%'):
                        first.approval_drafts.decide(formerly_allowed, {'action': 'approve'})
                    settings.update(expected_revision=updated['revision'], min_discount_percent=80)
                    first.operations.save_approval_policy(settings)
                    formerly_blocked = self.save_report(first, 80)
                    self.assertEqual(first.approval_drafts.decide(formerly_blocked, {'action': 'approve'})['status'],
                                     'approved')

    def save_report(self, service, discount):
        task = service.create({'question': '99红玫10月25日销量为什么下降',
                               'sku': 'FLR-ROSE-99-BOUQUET'}, _today=date(2026, 10, 26))['task']
        action = {'kind': 'discount', 'discount_percent': discount}
        recommendation = {'action': action, 'description': '促销草稿', **check_action(action)}
        with service.connection() as db:
            service.repository.finish(db, task['id'], {'status': 'completed',
                                                       'closure_report': {'recommendation': recommendation}})
        return task['id']
