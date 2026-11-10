"""运营日程和风险雷达：按门店数据库隔离，依据当前数据快照生成预警。"""

import calendar
import logging
import re
import threading
from datetime import date, timedelta
from uuid import uuid4

from .business_calendar import day_context
from .clock import utc_now
from .task_parser import shanghai_today
from .tools import check_activity_status

DEFAULT_SAFETY_STOCK = 8
STOCKOUT_ALERT_MINUTES = 120
LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_plans (
    id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('stocking', 'staffing')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS schedule_plans_by_date ON schedule_plans(plan_date);

CREATE TABLE IF NOT EXISTS risk_alerts (
    kind TEXT NOT NULL CHECK (kind IN ('activity_gap', 'low_stock', 'sales_drop')),
    sku_id TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    product_name TEXT NOT NULL,
    detail TEXT NOT NULL,
    question TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (kind, sku_id, observed_date)
);
CREATE INDEX IF NOT EXISTS risk_alerts_by_date ON risk_alerts(observed_date DESC);
"""


UPSERT_ALERT = """
INSERT INTO risk_alerts (kind, sku_id, observed_date, product_name, detail, question, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (kind, sku_id, observed_date) DO UPDATE SET
    product_name = excluded.product_name,
    detail = excluded.detail,
    question = excluded.question,
    updated_at = excluded.updated_at
WHERE risk_alerts.updated_at IS NULL
   OR risk_alerts.product_name <> excluded.product_name
   OR risk_alerts.detail <> excluded.detail
   OR risk_alerts.question <> excluded.question
"""


def initialize_board(db):
    """创建日程和预警表；现有数据库只补充更新时间列。"""
    db.executescript(SCHEMA)
    # 拿到写锁再检查列，两个进程启动时也不会同时尝试 ALTER。
    db.execute('BEGIN IMMEDIATE')
    if not any(row[1] == 'updated_at' for row in db.execute('PRAGMA table_info(risk_alerts)')):
        db.execute('ALTER TABLE risk_alerts ADD COLUMN updated_at TEXT')


class OperationsBoard:
    """接收门店服务，提供月历读写与有界后台扫描。"""

    def __init__(self, service, *, start_worker):
        self.service = service
        self.closed = threading.Event()
        self.worker = None
        self._warned_inventory_fingerprint = None
        if start_worker:
            self.worker = threading.Thread(target=self._watch, name='risk-radar', daemon=True)
            self.worker.start()

    def close(self):
        """发出停止信号，等扫描线程释放连接。"""
        self.closed.set()
        if self.worker is not None:
            self.worker.join(timeout=2)

    def month(self, value=None):
        """返回经营时区下的月份、节日标记和当前门店的计划。"""
        today = shanghai_today()
        value = value if value is not None else today.isoformat()[:7]
        if not re.fullmatch(r'\d{4}-\d{2}', value):
            raise ValueError('月份格式应为 YYYY-MM。')
        first = date.fromisoformat(value + '-01')
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        holidays = {}
        for offset in range(last.day):
            day = (first + timedelta(days=offset)).isoformat()
            context = day_context(day)
            names = ([context['holiday_name']] if context['holiday_name'] else []) + context['occasions']
            if names:
                holidays[day] = names
        with self.service.connection() as db:
            rows = db.execute('SELECT * FROM schedule_plans WHERE plan_date BETWEEN ? AND ? ORDER BY plan_date, created_at, id',
                              (first.isoformat(), last.isoformat())).fetchall()
        return {'month': value, 'today': today.isoformat(), 'holidays': holidays,
                'plans': [dict(row) for row in rows]}

    def list_plans(self):
        """读取当前门店的全部计划，按日期和创建顺序排列。"""
        with self.service.connection() as db:
            rows = db.execute('SELECT * FROM schedule_plans ORDER BY plan_date, created_at, id').fetchall()
        return [dict(row) for row in rows]

    def add_plan(self, payload):
        """验证计划日期、类别和标题，原子保存一项安排。"""
        if not isinstance(payload, dict) or set(payload) != {'date', 'kind', 'title'}:
            raise ValueError('计划须包含 date、kind 和 title。')
        day, kind, title = payload['date'], payload['kind'], payload['title']
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError('日期格式应为 YYYY-MM-DD。')
        date.fromisoformat(day)
        if kind not in ('stocking', 'staffing') or not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValueError('请选择备货计划或员工排班，并填写 1–120 字的内容。')
        plan = {'id': uuid4().hex, 'plan_date': day, 'kind': kind, 'title': title.strip()}
        with self.service.connection() as db:
            db.execute('INSERT INTO schedule_plans (id, plan_date, kind, title) VALUES (:id, :plan_date, :kind, :title)', plan)
        return plan

    def alerts(self):
        """从已提交的扫描结果返回当前门店预警及数据覆盖日期。"""
        store, _, _ = self.service.dataset()
        dates = self._recent_dates(store)
        with self.service.connection() as db:
            if not dates:
                return {'alerts': [], 'latest_data_date': None, 'sample_data': store.meta['sample_data']}
            rows = db.execute('SELECT * FROM risk_alerts WHERE observed_date BETWEEN ? AND ? '
                              'ORDER BY observed_date DESC, kind, sku_id', (dates[0], dates[-1])).fetchall()
        return {'alerts': [dict(row) for row in rows], 'latest_data_date': dates[-1],
                'sample_data': store.meta['sample_data']}

    @staticmethod
    def _recent_dates(store):
        """以数据日期为基准，示例快照过期时仍能演示历史预警。"""
        return sorted(set(store.dates('sales')) | set(store.dates('inventory')) |
                      set(store.dates('promotions')))[-7:]

    def scan(self):
        """扫描最近七个有记录的日期，原子清理旧告警并写入当前结果。"""
        store, catalog, fingerprint = self.service.dataset()
        dates = self._recent_dates(store)
        if not dates:
            return
        candidates = set()
        for table in ('sales', 'inventory', 'promotions'):
            candidates.update((row['sku_id'], row['date']) for row in
                              store.rows(table, start=dates[0], end=dates[-1]))
        alerts = []
        latest_inventory_alerts = {}
        stockout_streaks = {}
        missing_quantity = missing_safety = 0
        for sku, day in sorted(candidates):
            if self.closed.is_set():
                return
            product = catalog.products.get(sku, {}).get('short_name', sku)
            activity = check_activity_status(store, sku, day)
            previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            imported = store.has_import(sku, [previous, day])
            inventory = store.daily_record('inventory', sku, day, imported_only=imported)
            before = store.daily_record('sales', sku, previous, imported_only=imported)
            current = store.daily_record('sales', sku, day, imported_only=imported)

            def alert(kind, detail, subject):
                # 明确日期和商品，让跳转后的调查使用同一份业务范围。
                question = f'调查{product}（{sku}）在{day}的销量变化原因，重点核查{subject}，列出证据和待确认事项。'
                return kind, sku, day, product, detail, question

            # 缺活动字段只影响活动规则，不把缺失的续接记录当成明确停用。
            if activity['expired_without_renewal']:
                ended = activity['campaign_ended_at']
                alerts.append(alert('activity_gap', f'满减于 {ended[:10]} {ended[11:16]} 到期，次日未续接。', '满减活动到期与资源位状态'))
            elif activity['resource_slot_lost']:
                alerts.append(alert('activity_gap', '前一日有资源位，调查日已撤位。', '资源位变动与活动续接'))
            quantity, safety = inventory.get('stock_on_hand'), inventory.get('safety_stock')
            inventory_observed = inventory.get('status') == 'ok'
            quantity_known = inventory_observed and type(quantity) is int and quantity >= 0
            safety_known = inventory_observed and type(safety) is int and safety >= 0
            missing_quantity += not quantity_known
            missing_safety += not safety_known
            minutes, monitored = inventory.get('out_of_stock_minutes'), inventory.get('monitored_minutes')
            has_stockout = (inventory_observed and type(minutes) is int and type(monitored) is int
                            and 0 < minutes <= monitored <= 1440)
            threshold = safety if safety_known else DEFAULT_SAFETY_STOCK
            reasons = []
            if has_stockout and minutes > STOCKOUT_ALERT_MINUTES:
                reasons.append(f'当日累计缺货 {minutes} 分钟，超过 {STOCKOUT_ALERT_MINUTES} 分钟。')
            if quantity_known and quantity < threshold:
                threshold_label = '安全线' if safety_known else '默认安全线'
                reasons.append(f'库存 {quantity} 件，低于{threshold_label} {threshold} 件。')

            # 缺货天数以实测缺货或零库存为准；低于安全线但还有库存不计为缺货。
            streak = 0
            if has_stockout or (quantity_known and quantity == 0):
                last_day, count = stockout_streaks.get(sku, (None, 0))
                streak = count + 1 if last_day == previous else 1
                stockout_streaks[sku] = day, streak
            if reasons:
                if streak > 1:
                    reasons.append(f'连续缺货 {streak} 天。')
                # candidates 按 SKU、日期升序，同一商品只保留最新达标的库存告警。
                latest_inventory_alerts[sku] = alert('low_stock', ''.join(reasons), '库存、安全线和缺货时段')

            old, new = before.get('units_sold'), current.get('units_sold')
            if before.get('status') == current.get('status') == 'ok':
                if type(old) is int and type(new) is int and old >= 10 and 0 <= new * 2 <= old:
                    alerts.append(alert('sales_drop', f'销量从 {old} 件降至 {new} 件，较前一日下降 {(old-new)/old:.0%}。', '销量、流量、库存和活动'))
        alerts.extend(latest_inventory_alerts.values())
        if (missing_quantity or missing_safety) and self._warned_inventory_fingerprint != fingerprint:
            # 每店每个快照版本最多一条汇总 warning，避免定时任务重复刷屏。
            LOGGER.warning(
                '风险雷达库存字段缺失：门店=%s，缺件数=%d，缺安全线=%d；默认安全线=%d 件，缺件数时仅对超时缺货告警。',
                store.meta.get('business_context', {}).get('store_id', 'local'),
                missing_quantity, missing_safety, DEFAULT_SAFETY_STOCK,
            )
            self._warned_inventory_fingerprint = fingerprint
        with self.service.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old_keys = {tuple(row) for row in db.execute('SELECT kind, sku_id, observed_date FROM risk_alerts')}
            current_keys = {alert[:3] for alert in alerts}
            updated_at = utc_now()
            db.executemany(UPSERT_ALERT, ((*alert, updated_at) for alert in alerts))
            # 已恢复或离开扫描窗口的预警才删除；读者只会看到整批提交前或提交后的结果。
            db.executemany('DELETE FROM risk_alerts WHERE kind=? AND sku_id=? AND observed_date=?',
                           old_keys - current_keys)

    def _watch(self):
        """启动即扫描，随后每分钟重查；暂时错误留给下一轮恢复。"""
        while not self.closed.is_set():
            try:
                self.scan()
            except Exception:
                LOGGER.exception('风险雷达扫描失败')
            self.closed.wait(60)
