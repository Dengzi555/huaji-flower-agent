"""确定性分析器：计算事实、检查质量、生成候选信号；不把相关性说成因果。

输入是四类工具的观察结果，输出 facts/gaps/conflicts/hypotheses。
将算术留在程序里，使同样数据能重复验证，也避免模型自行编造百分比。
"""
from .assessment import POLICIES


def change(before, after):
    """同时保留绝对变化和相对变化；基线为零时，相对百分比没有定义。"""
    return {"before": before, "after": after, "absolute": round(after - before, 4),
            "relative_pct": round((after - before) / before * 100, 2) if before != 0 else None}


def analyze(observations: dict[str, dict], product_context=None) -> dict:
    """按质量检查→指标计算→信号生成的顺序处理，证据 ID 贯穿每项结论。"""
    facts = {}
    gaps = []
    conflicts = []
    hypotheses = []
    pairs = {}
    # 成对的两天都可用，才能计算变化；不能拿昨天的真实值和今天的缺失值比较。
    for table in ("sales", "traffic", "inventory", "promotions"):
        observation = observations[table]
        rows = observation["data"]["records"]
        if any(row["status"] != "ok" for row in rows):
            gaps.append({"table": table, "evidence_id": observation["evidence_id"],
                         "reason": "Missing, delayed or unavailable daily data; not treated as zero"})
        else:
            pairs[table] = rows

    # 引用工具观察而非模型文本；一项计算可以同时依赖销售与流量两个证据。
    refs = lambda *tables: [observations[table]["evidence_id"] for table in tables]
    if "sales" in pairs:
        before, after = pairs["sales"]
        for field in ("paid_orders", "paid_buyers", "units_sold", "paid_revenue"):
            facts[field] = {**change(before[field], after[field]), "evidence_ids": refs("sales")}
    if "traffic" in pairs:
        before, after = pairs["traffic"]
        facts["visitors"] = {**change(before["visitors"], after["visitors"]), "evidence_ids": refs("traffic")}
    if "sales" in pairs and "traffic" in pairs:
        rates = []
        for sale, traffic in zip(pairs["sales"], pairs["traffic"]):
            # 支付买家转化率用去重买家数 / 访客 UV；不能用订单数或件数当分子。
            if sale["paid_buyers"] > traffic["visitors"]:
                conflicts.append({"kind": "buyers_exceed_visitors", "date": sale["date"],
                                  "evidence_ids": refs("sales", "traffic")})
                rates.append(None)
            else:
                rates.append(sale["paid_buyers"] / traffic["visitors"] * 100 if traffic["visitors"] else None)
        if all(value is not None for value in rates):
            facts["payment_conversion_pct"] = {"before": round(rates[0], 2), "after": round(rates[1], 2),
                "percentage_point_change": round(rates[1] - rates[0], 2),
                "evidence_ids": refs("sales", "traffic")}

    sales_down = facts.get("units_sold", {}).get("relative_pct")
    # 候选线索也先过滤绝对损失，避免2→1触发一整轮下降归因；最终分级另看可比历史。
    tags=(product_context or {}).get('business_tags') or []
    tier=next((tag for tag in ('爆款','长尾款') if tag in tags),'常规款');policy=POLICIES[tier]
    sale_fact=facts.get('units_sold',{})
    declining = sales_down is not None and sales_down <= -policy['min_drop_pct'] and sale_fact['before']-sale_fact['after']>=policy['min_loss']
    if "inventory" in pairs:
        before, after = pairs["inventory"]
        full_day = all(row["monitored_minutes"] == 1440 for row in (before, after))
        if not full_day:
            # 半天的库存监测不能用于推断全天缺货情况。
            gaps.append({"table": "inventory", "reason": "Partial-day stock monitoring; cannot infer full-day availability",
                         "evidence_id": observations["inventory"]["evidence_id"]})
        conflict = ("sales" in pairs and any(
            stock["out_of_stock_minutes"] == stock["monitored_minutes"] == 1440 and sale["units_sold"] > 0
            for sale, stock in zip(pairs["sales"], pairs["inventory"])))
        if conflict:
            conflicts.append({"kind": "full_day_stockout_with_sales", "evidence_ids": refs("sales", "inventory")})
        if declining and full_day and not conflict and after["out_of_stock_minutes"] > before["out_of_stock_minutes"]:
            # 只能提出时间上伴随的候选原因；小时级对齐和反向证据仍待补查。
            hypotheses.append({"id": "stockout_signal", "status": "supported_signal",
                "text": "销量下降同时出现缺货时长增加；可能限制销售，仍需小时级销售与可售库存对齐核验。",
                "evidence_ids": refs("sales", "inventory"), "next_check": "查询小时级库存和订单，确认异常是否时间重合。"})
    visitor_change = facts.get("visitors", {}).get("relative_pct")
    if declining and visitor_change is not None and visitor_change <= -20 and not any(
            item["kind"] == "buyers_exceed_visitors" for item in conflicts):
        hypotheses.append({"id": "traffic_signal", "status": "supported_signal",
            "text": "访客数与销量同时下降；这是相关信号，尚未定位流量来源或证明因果。",
            "evidence_ids": refs("sales", "traffic"), "next_check": "按自然搜索、付费广告和推荐渠道拆解流量。"})
    if declining and "promotions" in pairs:
        before, after = pairs["promotions"]
        if before["active"] and not after["active"]:
            hypotheses.append({"id": "promotion_ended_signal", "status": "supported_signal",
                "text": "前日活动开启、目标日未开启；活动结束可能与回落有关，需要活动前基线进一步验证。",
                "evidence_ids": refs("sales", "promotions"), "next_check": "比较活动前同星期基线、价格及渠道，避免把活动峰值当常态。"})
    if conflicts:
        # 冲突优先级高于缺失；“完整”也仅表示所提供日级字段齐全，不是根因已查明。
        quality = "conflicting"
    elif gaps:
        quality = "partial"
    else:
        quality = "complete_for_supplied_daily_fields"
    return {"facts": facts, "data_quality": quality, "gaps": gaps, "conflicts": conflicts,
            "hypotheses": hypotheses, "causal_root_cause": "not_established",
            "sales_decline_detected": declining,
            "limitations": ["候选线索使用选定基线；经营分级综合历史与背景，仍未控制所有混杂因素。",
                            "候选线索先过滤商品类型阈值与绝对损失，最终分级另核对历史波动、生命周期和可比背景；策略并非行业标准。",
                            "没有支持信号不等于没有问题；数据缺失不等于业务指标为零。"]}
