"""审批草稿收件箱：使用结案报告建议，单独记录人工批准或驳回。"""

import json

from .clock import utc_now
from .decisions import DecisionConflict, DecisionNotFound
from .guardrails.policy_checker import check_action

SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendation_reviews (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    status TEXT NOT NULL CHECK (status IN ('approved', 'rejected')),
    decided_at TEXT NOT NULL
);
"""

SELECT_DRAFTS = """
SELECT tasks.id, tasks.updated_at,
       json_extract(tasks.parsed_json, '$.sku_id') AS sku_id,
       json_extract(tasks.parsed_json, '$.target_date') AS target_date,
       json_extract(tasks.result_json, '$.closure_report.recommendation') AS recommendation,
       recommendation_reviews.status AS review_status
FROM tasks LEFT JOIN recommendation_reviews ON recommendation_reviews.task_id = tasks.id
WHERE tasks.status = 'completed'
  AND json_type(tasks.result_json, '$.closure_report.recommendation') = 'object'
"""


class ApprovalDrafts:
    """按当前门店读取待办，事务内校验策略并持久化人工决定。"""

    def __init__(self, service):
        self.service = service

    @staticmethod
    def _draft(row, limits):
        """把人工处理状态与规则状态分别返回，拦截记录仍可出现在待办中。"""
        recommendation = json.loads(row['recommendation'])
        policy = check_action(recommendation.get('action'), limits)
        return {
            'id': row['id'], 'status': row['review_status'] or 'pending',
            'sku_id': row['sku_id'], 'date': row['target_date'], 'updated_at': row['updated_at'],
            'description': recommendation.get('description') or '无建议说明',
            'policy': policy,
        }

    def list_pending(self):
        """返回尚无人工决定的结案建议。"""
        with self.service.connection() as db:
            rows = db.execute(SELECT_DRAFTS + ' AND recommendation_reviews.status IS NULL '
                              'ORDER BY tasks.updated_at DESC, tasks.id DESC LIMIT 100').fetchall()
            limits = self.service.operations.approval_policy(db)
        return [self._draft(row, limits) for row in rows]

    def decide(self, task_id, payload):
        """批准只记录决策；被拦截的草稿不得批准。"""
        if not isinstance(payload, dict) or set(payload) != {'action'} or payload['action'] not in ('approve', 'reject'):
            raise ValueError('请选择 approve 或 reject。')
        status = 'approved' if payload['action'] == 'approve' else 'rejected'
        with self.service.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(SELECT_DRAFTS + ' AND tasks.id = ?', (task_id,)).fetchone()
            if row is None:
                raise DecisionNotFound('当前门店不存在该审批草稿。')
            draft = self._draft(row, self.service.operations.approval_policy(db))
            if draft['status'] == status:
                return draft
            if draft['status'] != 'pending':
                raise DecisionConflict('这条草稿已经处理，请刷新待办。')
            if status == 'approved' and draft['policy']['status'] == 'blocked':
                raise DecisionConflict(draft['policy']['reason'])
            db.execute('INSERT INTO recommendation_reviews VALUES (?, ?, ?)', (task_id, status, utc_now()))
        return {**draft, 'status': status}
