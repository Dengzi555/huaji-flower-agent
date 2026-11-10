"""用真实子进程验证从调查入队到证据结案的完整链路。"""

import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.web_service import InvestigationService


class InvestigationWorkerTests(unittest.TestCase):
    """冻结合成竞品、天气与账本数据，不调用外部服务。"""

    def test_sales_drop_runs_in_another_process_and_delivers_report(self):
        class Book:
            dates = {'2026-10-24', '2026-10-25'}

            def report(self, day):
                enabled = day == '2026-10-24'
                return {'totals': {'spend_cents': 1200 if enabled else 0, 'clicks': 4 if enabled else 0},
                        'hours': [{'enabled': enabled}], 'source_id': 'test-ads', 'sample_data': True}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = InvestigationService(Path('data/sample.json'), Path('data/products.json'),
                                           root / 'state', root / 'runs')
            try:
                service.operations.book = Book()
                service.operations.save_approval_policy({
                    'expected_revision': 0, 'min_discount_percent': 90, 'max_ad_budget_cents': 500_000,
                })
                created = service.create({
                    'question': '调查99红玫昨天销量暴跌的原因',
                    'sku': 'FLR-ROSE-99-BOUQUET',
                    'intent': 'investigate_sales_change',
                }, _today=date(2026, 10, 26))['task']
                self.assertIn(created['status'], ('queued', 'running', 'completed'), created['parsed'].get('issues'))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    task = service.get(created['id'])
                    if task['status'] not in ('queued', 'running'):
                        break
                    time.sleep(.025)
                self.assertEqual(task['status'], 'completed', task.get('error'))
                result = task['result']
                self.assertNotEqual(result['worker_pid'], os.getpid())
                self.assertEqual(result['target_date'], '2026-10-25')
                report = result['closure_report']
                self.assertEqual(report['title'], '调查结案报告')
                self.assertIn('竞品记录显示有降价', report['summary'])
                self.assertIn('未开启投放', report['summary'])
                sections = {section['title']: ' '.join(section['items']) for section in report['sections']}
                self.assertIn('下降 30%', sections['竞品价格'])
                self.assertIn('午后有小雨', sections['天气与节日'])
                self.assertIn('是否未续投', sections['近七日广告投放'])
                recommendation = report['recommendation']
                self.assertEqual(recommendation['status'], 'blocked')
                self.assertEqual(recommendation['label'], '【已拦截：超出底线折扣】')
                self.assertIn('90%', recommendation['reason'])
                artifact = service.artifact(task['id'], 'report.md').decode('utf-8')
                self.assertIn('调查结案报告', artifact)
                self.assertIn('整改建议（Action Recommendation）', artifact)
                self.assertTrue(artifact.strip().endswith(recommendation['reason']))
            finally:
                service.close()
