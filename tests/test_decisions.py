"""决策卡与干跑：只读边界、审批前置、回执完整性和 HTTP 路由。

这些测试验证的是“调查结论能否安全地变成人工可执行草稿”，不是页面外观。
"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ecommerce_agent.decisions import DecisionConflict
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService


class DecisionTests(unittest.TestCase):
    """使用真实执行器跑完一条调查，再验证决策卡状态机。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        catalog = self.root / 'products.json'; catalog.write_text(json.dumps(sample_catalog()), encoding='utf-8')
        data = self.root / 'data.json'; data.write_text(json.dumps(sample_data()), encoding='utf-8')
        self.service = InvestigationService(data, catalog, self.root / 'state', self.root / 'runs')
        self.addCleanup(self.service.close)
        self.server = create_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop_http)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def stop_http(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def completed_task(self, question='怦然心动2026-10-23销量下降原因'):
        """提交并等待终态；测试不把“已入队”当成“已完成”。"""
        task_id = self.service.create({'question': question})['task']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.service.get(task_id)
            if task['status'] not in ('queued', 'running'): return task
            time.sleep(0.02)
        self.fail('调查未在时限内完成')

    def test_card_requires_completed_investigation(self):
        """未完成或未生成的调查不能出决策卡，避免对半成品给建议。"""
        offline = InvestigationService(self.root / 'data.json', self.root / 'products.json',
                                       self.root / 'queued-state', self.root / 'queued-runs', start_worker=False)
        self.addCleanup(offline.close)
        task_id = offline.create({'question': '怦然心动2026-10-23销量下降原因'})['task']['id']
        with self.assertRaises(DecisionConflict):
            offline.decisions.propose(task_id)
        self.assertIsNone(offline.get(task_id)['decision'])

    def test_card_items_stay_read_only_and_evidence_bound(self):
        """卡里的每一条都必须是只读、需审批、且引用本轮真实证据。"""
        task = self.completed_task()
        card = self.service.decisions.propose(task['id'])['card']
        guidance = task['result']['analysis']['guidance']
        valid = set()
        for key in ('observed', 'supported_signals'):
            for item in guidance.get(key, []): valid.update(item.get('evidence_ids', []))
        for check in guidance.get('next_checks', []): valid.update(check.get('evidence_ids', []))
        self.assertTrue(card['items'])
        for item in card['items']:
            self.assertEqual(item['risk'], 'read_only')
            self.assertTrue(item['requires_approval'])
            self.assertLessEqual(set(item['evidence_ids']), valid)
            self.assertIn(item['kind'], {'stock_review', 'traffic_review', 'promotion_review',
                                         'order_review', 'reconcile', 'data_gap', 'hold', 'review'})
        self.assertIn('未调用任何店铺写入接口', card['boundary'])
        self.assertEqual(card['verdict']['code'], 'verify_before_action')

    def test_dry_run_requires_approval_and_receipt_is_verifiable(self):
        """未批准不得干跑；批准后回执必须零副作用、可复算摘要。"""
        task = self.completed_task()
        decision = self.service.decisions.propose(task['id'])
        with self.assertRaises(DecisionConflict):
            self.service.decisions.dry_run(decision['id'])
        approved = self.service.decisions.approve(decision['id'], '店长-王')
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(approved['approval']['by'], '店长-王')
        executed = self.service.decisions.dry_run(decision['id'])
        self.assertEqual(executed['status'], 'executed')
        receipt = dict(executed['receipt'])
        digest = receipt.pop('digest')
        self.assertEqual(hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), digest)
        self.assertEqual(receipt['mode'], 'dry_run')
        self.assertEqual(receipt['side_effects'], [])
        self.assertEqual(receipt['approved_by'], '店长-王')
        for item in receipt['items']:
            self.assertEqual(item['side_effects'], [])
            self.assertGreaterEqual(len(item['steps']), 3)
            self.assertTrue(item['verification']['pass_rule'])
        with self.assertRaises(DecisionConflict):
            self.service.decisions.dry_run(decision['id'])

    def test_rejected_card_cannot_execute(self):
        """驳回是终态，不能绕过审批直接干跑。"""
        task = self.completed_task()
        decision = self.service.decisions.propose(task['id'])
        rejected = self.service.decisions.reject(decision['id'], '店长-李', '证据不足')
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(rejected['approval']['note'], '证据不足')
        with self.assertRaises(DecisionConflict):
            self.service.decisions.dry_run(decision['id'])

    def test_lookup_card_is_hold_without_action(self):
        """表现查询只记录观察，不生成经营动作，也不能被写成“观察到变化”。"""
        task = self.completed_task('怦然心动2026-10-23销量怎么样')
        decision = self.service.decisions.propose(task['id'])
        self.assertEqual(decision['card']['verdict']['code'], 'no_action_from_lookup')
        self.assertEqual([item['kind'] for item in decision['card']['items']], ['hold'])

    def test_propose_is_idempotent_per_task(self):
        """同一任务重复生成返回同一张卡，不会产生互相矛盾的版本。"""
        task = self.completed_task()
        first = self.service.decisions.propose(task['id'])
        second = self.service.decisions.propose(task['id'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['created_at'], second['created_at'])

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_http_decision_flow(self):
        """网页入口必须同样强制“先批准后干跑”，并能在任务详情里读到回执。"""
        status, body = self.request('/api/tasks', {'question': '怦然心动2026-10-23销量下降原因'})
        self.assertEqual(status, 201)
        task_id = body['task']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.service.get(task_id)['status'] in ('queued', 'running'):
            time.sleep(0.02)
        status, body = self.request(f'/api/tasks/{task_id}/decision', {})
        self.assertEqual(status, 200, body)
        decision_id = body['decision']['id']
        self.assertEqual(body['decision']['status'], 'proposed')
        status, body = self.request(f'/api/decisions/{decision_id}/execute', {})
        self.assertEqual(status, 409, body)
        status, body = self.request(f'/api/decisions/{decision_id}/approve', {'actor': '店长-王'})
        self.assertEqual(status, 200, body)
        status, body = self.request(f'/api/decisions/{decision_id}/execute', {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['decision']['status'], 'executed')
        detail = self.service.get(task_id)
        self.assertEqual(detail['decision']['status'], 'executed')
        self.assertTrue(detail['decision']['receipt']['digest'])


if __name__ == '__main__': unittest.main()