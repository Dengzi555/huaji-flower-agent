"""有限字段断言验收：核对来源、日期、字段和值，不证明自然语言因果或来源真实性。"""

import math

ASSERTION_FIELDS = {
    'product_group_sales':{'units_sold','paid_revenue'},
    "order_cohorts": {"cohort_paid_orders","cancelled_orders","completed_orders","pending_orders","late_cancelled_orders"},
    "sales": {"units_sold", "paid_orders", "paid_buyers", "paid_revenue"},
    "traffic": {"visitors"}, "inventory": {"out_of_stock_minutes", "monitored_minutes"},
    "promotions": {"active", "campaign_name"},
    "sales_trend": {"units_sold","paid_orders","paid_buyers","paid_revenue","visitors"},
    "calendar": {"day_type","weekday","is_makeup_workday","is_workday","holiday_name"},
}


def verify_assertions(observations, assertions):
    """输入本次成功工具观察和有限标量断言；返回逐条核验状态与覆盖边界。"""
    if not isinstance(assertions, list) or len(assertions) > 64:
        raise ValueError("assertions 必须为最多 64 项的数组。")
    by_id = {value["evidence_id"]: value for value in observations.values() if value.get("ok")}
    checks = []
    for assertion in assertions:
        reason = None
        if not isinstance(assertion, dict) or set(assertion) not in ({"evidence_id", "date", "field", "equals"},{"evidence_id", "date", "field", "equals","sku_id"}):
            raise ValueError("断言只支持 evidence_id、date、field、equals。")
        if any(not isinstance(assertion[key], str) for key in ("evidence_id", "date", "field")):
            raise ValueError("断言来源、日期和字段必须为字符串。")
        value = assertion["equals"]
        if type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("断言值必须为有限标量。")
        observation = by_id.get(assertion["evidence_id"])
        table = observation.get("data", {}).get("table") if observation else None
        rows = observation.get("data", {}).get("records", []) if observation else []
        # 集合证据同一天有多个SKU，必须同时按SKU定位，不能验证到另一款的同日记录。
        if table=='product_group_sales' and not isinstance(assertion.get('sku_id'),str):raise ValueError('集合断言必须明确sku_id。')
        row = next((row for row in rows if row.get("date") == assertion["date"] and ('sku_id' not in assertion or row.get('sku_id')==assertion['sku_id'])), None)
        if not observation:
            reason = "evidence_not_observed"
        elif assertion["field"] not in ASSERTION_FIELDS.get(table, set()):
            reason = "field_not_allowed"
        elif not row or row.get("status") != "ok":
            reason = "record_unavailable"
        else:
            actual = row.get(assertion["field"])
            numeric = type(actual) in (int, float) and type(value) in (int, float)
            if not (numeric or type(actual) is type(value)) or actual != value:
                reason = "value_mismatch"
        checks.append({**assertion, "status": "failed" if reason else "passed", "reason": reason})
    return {"status": "failed" if any(row["status"] == "failed" for row in checks) else "passed" if checks else "references_only",
            "checked": len(checks), "failed": sum(row["status"] == "failed" for row in checks), "checks": checks,
            "scope": "仅核对本次来源记录的白名单标量字段；不证明文本因果、来源真实性或业务恢复。"}


def observation_assertions(observations):
    """规则模式提取来源字段作为可复核交付；不是独立模型效果或语义评测。"""
    assertions = []
    for observation in observations.values():
        table = observation.get("data", {}).get("table")
        # 长窗口不自动把所有行扩成数百条断言；模型可对明确引用的少数字段逐条提交。
        if table in ('sales_trend','calendar'):continue
        records=observation.get('data',{}).get('records',[])
        if table=='product_group_sales':
            data=observation['data'];rank=data.get('ranking') or {}
            dates=rank.get('effective_dates') or [data['target_date']]
            # 验收本轮实际用于排名的可用日期；有界抽样原始字段，不宣称验证了全部窗口算术。
            records=sorted([row for row in records if row['date'] in dates and row.get('status')=='ok'],key=lambda row:row['date'],reverse=True)[:32]
        for row in records:
            if row.get("status") != "ok":
                continue
            for field in sorted(ASSERTION_FIELDS.get(table, set())):
                assertions.append({"evidence_id": observation["evidence_id"], "date": row["date"],
                                   "field": field, "equals": row[field],**({'sku_id':row['sku_id']} if table=='product_group_sales' else {})})
    return assertions
