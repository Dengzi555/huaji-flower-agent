"""决策卡与干跑执行：把已观察事实转成可审批的工作单，不产生任何店铺写入。

设计边界：
- 输入只来自已完成任务的冻结结果（analysis.guidance），不再调用模型；
- 建议是“核对/复盘/观察”类只读动作，改价、调预算、补货等写操作不允许出现在卡里；
- 审批是人工动作，干跑只生成核对清单、判定阈值和回执，不发送任何外部请求。
"""
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone

BOUNDARY = '仅生成可执行草稿与核对清单；未调用任何店铺写入接口，改价、调预算、补货等动作必须由人工在商家后台完成。'

# 把排查项映射到工作单类型；类型决定干跑时的核对步骤和判定规则，而不是执行动作。
KIND_BY_CHECK = {
    'stockout_signal': 'stock_review', 'traffic_signal': 'traffic_review',
    'promotion_ended_signal': 'promotion_review', 'check_cancellations': 'order_review',
    'reconcile_sources': 'reconcile', 'observe_next_period': 'hold',
}
DATA_GAP_PREFIX = ('complete_',)

VERIFICATION = {
    'stock_review': {'metric': '缺货时段与少卖时段是否重叠', 'window': '目标日营业时段',
                     'pass_rule': '重叠且缺货分钟数高于基线时，才进入补货或陈列调整的人工审批。'},
    'traffic_review': {'metric': '渠道访客与支付转化', 'window': '目标日与同星期基线',
                       'pass_rule': '单一渠道能解释大部分访客降幅时，收窄到该渠道复盘；否则回到商品与价格。'},
    'promotion_review': {'metric': '活动结束后的同星期回落幅度', 'window': '活动结束后 3 个可比日',
                         'pass_rule': '回落到活动前基线 ±10% 内按峰值回落处理，不调整经营策略。'},
    'order_review': {'metric': '取消率与取消单数', 'window': '同一支付日批次',
                     'pass_rule': '取消集中在缺货或等待标签时，转入履约核查，不直接归因销量。'},
    'reconcile': {'metric': '来源口径差异是否消除', 'window': '同一门店、SKU 与日期',
                  'pass_rule': '冲突未消除前只做对账，不进入原因判断。'},
    'data_gap': {'metric': '缺失来源是否补齐', 'window': '目标日与基线日',
                 'pass_rule': '补齐后重新运行同一问题，确认快照版本已更新。'},
    'hold': {'metric': '下一同星期支付件数', 'window': '下一同星期日期',
             'pass_rule': '仍在基线波动区间内则维持观察，不调整经营策略。'},
    'review': {'metric': '可比历史与商品生命周期', 'window': '最近 4 个同星期',
               'pass_rule': '确认变化是否超出正常波动，再决定是否进入下一步调查。'},
}

STEPS = {
    'stock_review': ['导出目标日与同星期基线的商品级库存监测记录',
                     '对齐缺货分钟、营业时段与支付件数的小时分布',
                     '按判定规则确认是否需要在下一营业日调整补货或陈列'],
    'traffic_review': ['按互斥渠道导出目标日与基线的访客和支付数据',
                       '拆分访客变化与转化率变化，确认主因落在哪一侧',
                       '只把能解释主要降幅的渠道带入下一步复盘'],
    'promotion_review': ['列出活动起止、优惠方式和同星期对照日',
                         '比较活动结束后 3 个可比日的销量与流量',
                         '按回落阈值判断是峰值回落还是持续下滑'],
    'order_review': ['导出同支付日批次的取消、完成与待完结订单',
                     '核对批次观察截止时间，避免把未完结算成取消',
                     '按取消原因标签定位履约或商品问题'],
    'reconcile': ['拉出冲突来源的原始记录与统计截止时间',
                  '按门店、SKU、日期和口径逐项对账',
                  '冲突消除前暂停原因判断和任何经营动作'],
    'data_gap': ['确认缺失来源、覆盖范围和最近更新时间',
                 '补齐后重新提交同一问题，保留新的冻结快照',
                 '对比新旧快照，确认结论没有因补数而反转'],
    'hold': ['记录本轮观察到的变化与基线区间',
             '在下一同星期日期复跑同一问题',
             '只有超出波动区间时才升级为调查'],
    'review': ['取回最近 4 个同星期日期与商品上架/下架信息',
               '排除节日、调休、活动等背景差异',
               '确认是否超出正常波动后再决定下一步'],
}


class DecisionNotFound(ValueError):
    """任务或决策不存在；由网页层映射为 404。"""


class DecisionConflict(ValueError):
    """状态不允许当前操作（未完成、未批准、已执行）；由网页层映射为 409。"""


def _check_kind(check):
    """排查项到工作单类型的唯一映射；未知项按只读复盘处理，不猜成写操作。"""
    identity = check.get('id', '')
    if identity in KIND_BY_CHECK:
        return KIND_BY_CHECK[identity]
    if identity.startswith(DATA_GAP_PREFIX):
        return 'data_gap'
    return 'review'


def _deadline(target_date):
    """工作单截止时间：目标日下一个自然日中午，给人工留出核对窗口。"""
    try:
        day = date.fromisoformat(target_date)
    except (TypeError, ValueError):
        return None
    return (day + timedelta(days=1)).isoformat() + 'T12:00:00+08:00'


def _verdict(guidance):
    """把证据状态翻译成决策结论；冲突和缺口优先于行动建议。"""
    status = guidance.get('status')
    if status == 'data_conflict':
        return {'code': 'reconcile_first', 'label': '来源存在冲突，先对账，暂不动作'}
    if status == 'evidence_gap':
        return {'code': 'collect_evidence', 'label': '证据不足，先补数据再决定'}
    return {'code': 'verify_before_action', 'label': '观察到变化，先按清单核查再决定'}


def build_card(task):
    """从已完成任务构建决策卡；纯函数，便于验证“没有证据就不生成动作”。"""
    if task.get('status') != 'completed':
        raise DecisionConflict('只有已完成的调查才能生成决策建议。')
    state = task.get('result') or {}
    analysis = state.get('analysis') or {}
    guidance = analysis.get('guidance') or {}
    parsed = task.get('parsed') or {}
    checks = guidance.get('next_checks') or []
    lookup = analysis.get('kind') == 'lookup'
    verdict = ({'code': 'no_action_from_lookup', 'label': '表现查询，不据此建议经营动作'}
               if lookup else _verdict(guidance))
    items = []
    for check in checks:
        kind = _check_kind(check)
        items.append({
            'id': check.get('id') or f'check-{len(items)+1}',
            'title': check.get('title') or '按清单核查',
            'kind': kind,
            'priority': check.get('priority', len(items) + 1),
            'rationale': check.get('reason') or '',
            'required_data': check.get('required_data') or '',
            'availability': check.get('availability') or 'available_evidence',
            'evidence_ids': [ref for ref in check.get('evidence_ids') or [] if isinstance(ref, str)],
            'risk': 'read_only',
            'requires_approval': True,
            'deadline': _deadline(parsed.get('target_date')),
            'verification': dict(VERIFICATION.get(kind, VERIFICATION['review'])),
        })
    if not items:
        items.append({
            'id': 'hold', 'title': '本轮只记录表现，不调整经营策略' if lookup else '本轮不调整经营策略，观察下一可比经营日',
            'kind': 'hold', 'priority': 1,
            'rationale': ('表现查询不能直接推出经营动作；需要原因时请基于同一对象发起调查。' if lookup
                          else '当前证据不足以支持任何经营调整，先保留观察。'),
            'required_data': '下一同星期日期销量、活动和库存背景', 'availability': 'needs_additional_data',
            'evidence_ids': [], 'risk': 'read_only', 'requires_approval': True,
            'deadline': _deadline(parsed.get('target_date')),
            'verification': dict(VERIFICATION['hold']),
        })
    fact = (analysis.get('facts') or {}).get('units_sold') or {}
    product = parsed.get('product_name') or parsed.get('sku_name') or parsed.get('sku_id') or '所选商品'
    change = ''
    if fact:
        change = f"，支付件数 {fact.get('before')} → {fact.get('after')}"
        if fact.get('relative_pct') is not None:
            change += f"（{fact['relative_pct']:+}%）"
    return {
        'version': 'decision-card-v1',
        'task_id': task.get('id'),
        'summary': f"{product} · {parsed.get('target_date') or '所选日期'}{change}。{verdict['label']}。",
        'verdict': verdict,
        'items': items,
        'boundary': BOUNDARY,
        'caveats': ['建议只针对当前已取回的证据；未建立因果，不做经营归因。',
                    '干跑只生成核对清单与判定阈值，不发送任何外部请求。'],
    }


def _facts(analysis, limit=3):
    """回执只引用已观察事实，避免把模型草稿或推测写进可执行草稿。"""
    observed = ((analysis.get('guidance') or {}).get('observed') or [])[:limit]
    return [{'text': item.get('text', ''), 'evidence_ids': item.get('evidence_ids') or []} for item in observed]


def build_receipt(card, task, approval):
    """生成干跑回执：每个建议一份核对清单、判定规则和明确的零副作用声明。"""
    analysis = (task.get('result') or {}).get('analysis') or {}
    parsed = task.get('parsed') or {}
    items = []
    for item in card.get('items') or []:
        kind = item.get('kind', 'review')
        items.append({
            'item_id': item.get('id'), 'title': item.get('title'), 'kind': kind,
            'status': 'dry_run_completed', 'owner': '门店运营（待指派）', 'due': item.get('deadline'),
            'steps': list(STEPS.get(kind, STEPS['review'])),
            'inputs': {'store_id': parsed.get('store_id'), 'sku_id': parsed.get('sku_id'),
                       'target_date': parsed.get('target_date'), 'evidence_ids': item.get('evidence_ids') or []},
            'facts': _facts(analysis),
            'verification': item.get('verification') or VERIFICATION['review'],
            'side_effects': [],
        })
    receipt = {
        'version': 'dry-run-receipt-v1', 'receipt_id': uuid.uuid4().hex, 'mode': 'dry_run',
        'task_id': task.get('id'), 'decision_id': card.get('decision_id'),
        'approved_by': approval.get('by'), 'approved_at': approval.get('at'),
        'executed_at': _now(), 'summary': card.get('summary'), 'items': items,
        'side_effects': [], 'boundary': BOUNDARY,
    }
    receipt['digest'] = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return receipt


def _now():
    """与仓库层一致，统一使用带时区 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 payload TEXT NOT NULL,
 approval TEXT,
 receipt TEXT
);
CREATE INDEX IF NOT EXISTS decisions_task ON decisions(task_id);
'''


class Decisions:
    """决策卡仓库：一个任务一张卡，状态机 proposed → approved/rejected → executed。"""

    def __init__(self, service):
        self.service = service
        self.repository = service.repository

    def _row(self, db, decision_id):
        return db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()

    def _view(self, row):
        """把存储行转成网页可消费结构；不返回内部路径或密钥。"""
        if not row:
            return None
        return {'id': row['id'], 'task_id': row['task_id'], 'status': row['status'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'card': json.loads(row['payload']),
                'approval': json.loads(row['approval']) if row['approval'] else None,
                'receipt': json.loads(row['receipt']) if row['receipt'] else None}

    def for_task(self, db, task_id):
        """任务详情附带最近一张卡；查询失败不应影响任务本身的读取。"""
        row = db.execute('SELECT * FROM decisions WHERE task_id=?', (task_id,)).fetchone()
        return self._view(row)

    def get(self, decision_id):
        with self.repository.connection() as db:
            row = self._row(db, decision_id)
        if not row:
            raise DecisionNotFound('决策卡不存在。')
        return self._view(row)

    def propose(self, task_id):
        """生成或复用决策卡；同一任务只有一张卡，重复调用不产生新版本。"""
        task = self.service.get(task_id, include_events=False)
        if not task:
            raise DecisionNotFound('任务不存在。')
        with self.repository.connection() as db:
            row = db.execute('SELECT * FROM decisions WHERE task_id=?', (task_id,)).fetchone()
        if row:
            return self._view(row)
        card = build_card(task)
        decision_id = uuid.uuid4().hex
        timestamp = _now()
        card['decision_id'] = decision_id
        with self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM decisions WHERE task_id=?', (task_id,)).fetchone():
                return self._view(db.execute('SELECT * FROM decisions WHERE task_id=?', (task_id,)).fetchone())
            db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)',
                       (decision_id, task_id, 'proposed', timestamp, timestamp,
                        json.dumps(card, ensure_ascii=False), None, None))
            self.repository.event(db, task_id, 'decision_proposed',
                                  {'decision_id': decision_id, 'items': len(card['items'])})
        return self.get(decision_id)

    def _transition(self, decision_id, expected, target, kind, extra=None, approval=None):
        """状态转换只在写事务内完成，避免并发批准覆盖已执行结果。"""
        with self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, decision_id)
            if not row:
                raise DecisionNotFound('决策卡不存在。')
            if row['status'] != expected:
                raise DecisionConflict(f"当前状态为 {row['status']}，不能执行该操作。")
            if approval is None:
                db.execute('UPDATE decisions SET status=?,updated_at=? WHERE id=?', (target, _now(), decision_id))
            else:
                db.execute('UPDATE decisions SET status=?,approval=?,updated_at=? WHERE id=?',
                           (target, json.dumps(approval, ensure_ascii=False), _now(), decision_id))
            self.repository.event(db, row['task_id'], kind, {'decision_id': decision_id, **(extra or {})})
        return self.get(decision_id)

    def approve(self, decision_id, actor='本地操作员', note=''):
        """人工批准：记录批准人、时间和备注，作为后续干跑的前置条件。"""
        approval = {'by': actor, 'at': _now(), 'note': note}
        return self._transition(decision_id, 'proposed', 'approved', 'decision_approved', approval, approval)

    def reject(self, decision_id, actor='本地操作员', note=''):
        """人工驳回：保留卡片和理由，不生成回执。"""
        approval = {'by': actor, 'at': _now(), 'note': note}
        return self._transition(decision_id, 'proposed', 'rejected', 'decision_rejected', approval, approval)

    def dry_run(self, decision_id):
        """只对已批准卡片生成回执；重复执行会因状态不符而拒绝，避免重复出单。"""
        with self.repository.connection() as db:
            row = self._row(db, decision_id)
        if not row:
            raise DecisionNotFound('决策卡不存在。')
        task = self.service.get(row['task_id'], include_events=False)
        with self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, decision_id)
            if not row:
                raise DecisionNotFound('决策卡不存在。')
            if row['status'] != 'approved':
                raise DecisionConflict(f"当前状态为 {row['status']}，必须先批准才能干跑。")
            card = json.loads(row['payload'])
            card['decision_id'] = decision_id
            approval = json.loads(row['approval']) if row['approval'] else {}
            receipt = build_receipt(card, task, approval)
            db.execute('UPDATE decisions SET status=?,receipt=?,updated_at=? WHERE id=?',
                       ('executed', json.dumps(receipt, ensure_ascii=False), _now(), decision_id))
            self.repository.event(db, row['task_id'], 'decision_executed',
                                  {'decision_id': decision_id, 'receipt_id': receipt['receipt_id'],
                                   'side_effects': 0})
        return self.get(decision_id)