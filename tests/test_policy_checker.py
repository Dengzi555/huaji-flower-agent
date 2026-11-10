"""校验折扣与广告预算边界，并拒绝不可核验的建议。"""

import unittest

from ecommerce_agent.guardrails.policy_checker import check_action
from ecommerce_agent.investigation_worker import closure_report


class PolicyCheckerTests(unittest.TestCase):
    """使用结构化建议核验边界，不从文案推断金额或折扣。"""

    def test_policy_boundaries(self):
        cases = [
            ({'kind': 'discount', 'discount_percent': 80}, 'blocked', '超出底线折扣'),
            ({'kind': 'discount', 'discount_percent': 85}, 'approved', '待执行'),
            ({'kind': 'advertising', 'budget_cents': 500_000}, 'approved', '待执行'),
            ({'kind': 'advertising', 'budget_cents': 500_001}, 'blocked', '超出投放预算'),
            ({'kind': 'advertising', 'budget_cents': True}, 'blocked', '参数无效'),
            ({'kind': 'discount'}, 'blocked', '参数无效'),
            ({'kind': 'manual_review', 'budget_cents': 600_000}, 'blocked', '参数无效'),
        ]
        for action, status, label in cases:
            with self.subTest(action=action):
                decision = check_action(action)
                self.assertEqual(decision['status'], status)
                self.assertIn(label, decision['label'])
                self.assertTrue(decision['reason'])

    def test_closure_passes_bounded_ad_recommendation(self):
        task = {'sku_id': 'FLR-ROSE-99-BOUQUET', 'target_date': '2026-10-25'}
        ads = [{'date': '2026-10-24', 'enabled_hours': 1, 'spend_cents': 200, 'clicks': 2, 'sample_data': True},
               {'date': '2026-10-25', 'enabled_hours': 0, 'spend_cents': 0, 'clicks': 0, 'sample_data': True}]
        report = closure_report(task, {'competitors': [], 'weather': [], 'ads': ads}, {})
        recommendation = report['recommendation']
        self.assertEqual(recommendation['action'], {'kind': 'advertising', 'budget_cents': 100_000})
        self.assertEqual(recommendation['status'], 'approved')
        self.assertEqual(recommendation['label'], '【审批通过：待执行】')
