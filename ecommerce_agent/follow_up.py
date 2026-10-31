"""有界追加调查：仅对真实流量相关信号拆解渠道，不将贡献分解称为因果归因。"""

from .analysis import change


def analyze_channels(traffic_observation, channel_observation):
    """核对归因口径、覆盖与合计，再输出渠道访客变化；不填补缺失渠道。"""
    result = {"status": "insufficient_data", "reason": "渠道明细缺失或延迟，不能继续拆解。",
              "rows": [], "evidence_ids": [traffic_observation["evidence_id"], channel_observation["evidence_id"]],
              "limitations": ["渠道变化是相关证据，未证明广告、搜索或推荐导致销量变化。",
                              "仅支持互斥末次归因 UV，不适用于可重叠渠道的普通去重访客数。"]}
    rows = channel_observation["data"]["records"]
    totals = traffic_observation["data"]["records"]
    if any(row["status"] != "ok" for row in rows + totals):
        return result
    before, after = rows
    if (before["attribution_policy"] != after["attribution_policy"]
            or set(before["attributed_visitors"]) != set(after["attributed_visitors"])):
        result.update(status="conflicting", reason="两日归因口径或渠道集合不同，停止比较。")
        return result
    if any(sum(channel["attributed_visitors"].values()) != total["visitors"]
           for channel, total in zip(rows, totals)):
        result.update(status="conflicting", reason="互斥归因渠道合计与商品 UV 不一致，需先核对数据。")
        return result
    for channel, value in before["attributed_visitors"].items():
        result["rows"].append({"channel": channel, **change(value, after["attributed_visitors"][channel])})
    result["rows"].sort(key=lambda row: row["absolute"])
    result.update(status="completed", reason="渠道集合和 UV 合计校验通过，已计算各渠道变化。")
    return result
