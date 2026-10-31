"""推广扣费分析：用分币计算时段费用，不把UV当广告点击，不将低投入直接认定为差。"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path


class AdvertisingBook:
    """冻结单店小时账本；广告模式拆分、店铺营业额单独保存以免重复加总。"""

    def __init__(self, document, store_id):
        """验证每日24小时、两种模式、唯一键及点击计费；缺数不能补成零花费。"""
        self.document = json.loads(json.dumps(document, ensure_ascii=False))
        if document.get("schema_version") != 1 or document.get("sample_data") is not True or document.get("store_id") != store_id:
            raise ValueError("推广学习账本必须明确来源并与所选店铺一致。")
        self.store_id, self.ads, self.revenue = store_id, {}, {}
        for row in document.get("revenue", []):
            key = self._key(row)
            if key in self.revenue or type(row.get("revenue_cents")) is not int or row["revenue_cents"] < 0:
                raise ValueError("营业额必须是唯一小时记录与非负分币。")
            self.revenue[key] = row
            if "operating" in row and type(row["operating"]) is not bool:
                raise ValueError("营业时段状态必须为布尔值。")
            if row.get("operating") is False and row["revenue_cents"]:
                raise ValueError("未营业时段不能携带支付营业额。")
        for row in document.get("ads", []):
            key = (*self._key(row), row.get("mode"))
            if key in self.ads or key[-1] not in ("manual", "automatic"):
                raise ValueError("广告小时模式记录重复或模式错误。")
            for field in ("clicks", "impressions", "spend_cents", "unit_cpc_cents"):
                if type(row.get(field)) is not int or row[field] < 0:
                    raise ValueError("点击/曝光/分币须为非负整数。")
            if type(row.get("enabled")) is not bool or row["clicks"] > row["impressions"]:
                raise ValueError("投放状态或曝光点击口径错误。")
            if row["spend_cents"] != row["clicks"] * row["unit_cpc_cents"]:
                raise ValueError("当前账本按小时固定CPC建模，点击费用必须相等。")
            if not row["enabled"] and (row["clicks"] or row["spend_cents"]):
                raise ValueError("当前学习口径下暂停投放时不能产生新点击扣费。")
            self.ads[key] = row
            if self.revenue.get(key[:2],{}).get("operating") is False and row["enabled"]:
                raise ValueError("未营业时段不能标记为已开启推广。")
        self.dates = sorted({key[0] for key in self.revenue})
        for day in self.dates:
            if any((day, hour) not in self.revenue or any((day, hour, mode) not in self.ads
                for mode in ("manual", "automatic")) for hour in range(24)):
                raise ValueError("小时账本不完整，拒绝把缺失时段当零。")
        if any(key[:2] not in self.revenue for key in self.ads):
            raise ValueError("广告扣费缺少对应时段营业额。")
        self.fingerprint = hashlib.sha256(json.dumps(self.document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _key(self, row):
        """每条记录都核对店铺，防止文件混入别店小时数据。"""
        if row.get("store_id") != self.store_id or type(row.get("hour")) is not int or not 0 <= row["hour"] < 24:
            raise ValueError("推广记录店铺或小时错误。")
        date.fromisoformat(row["date"])
        return row["date"], row["hour"]

    @staticmethod
    def threshold(value):
        """接受有限百分比，拒绝NaN/Infinity；阈值是操作者关注线，不是行业标准。"""
        try:
            number = Decimal(str(value))
            if not number.is_finite() or not 0 <= number <= 100:
                raise ValueError()
            return number
        except Exception:
            raise ValueError("关注线必须为0到100之间的有限百分数。") from None

    def report(self, day, *, high=10, low=3, mode="all"):
        """返回小时表、模式汇总与关注时段；营业额分母是店铺成交，不是广告归因收入。"""
        date.fromisoformat(day)
        high, low = self.threshold(high), self.threshold(low)
        if low >= high or mode not in ("all", "manual", "automatic"):
            raise ValueError("低投入线须小于高费用线，模式须为全部/手动/自动。")
        if day not in self.dates:
            raise ValueError("目标日推广账本尚未提供，不替换成最近一天。")
        hours = []
        modes = ("manual", "automatic") if mode == "all" else (mode,)
        totals = {kind: {"spend_cents": 0, "clicks": 0, "impressions": 0} for kind in modes}
        for hour in range(24):
            records = [self.ads[day, hour, kind] for kind in modes]
            spend, clicks = sum(r["spend_cents"] for r in records), sum(r["clicks"] for r in records)
            revenue = self.revenue[day, hour]["revenue_cents"]
            business = self.revenue[day, hour]
            enabled = any(r["enabled"] for r in records)
            ratio = round(spend / revenue * 100, 2) if revenue else None
            # 阈值判断用分币交叉相乘，不用四舍五入后展示的比例判断边界。
            flag = "no_revenue" if not revenue and spend else "high" if revenue and spend * 100 > high * revenue else "low" if enabled and revenue and spend * 100 < low * revenue else "normal"
            hours.append({"hour": hour, "label": f"{hour:02d}:00–{hour+1:02d}:00", "spend_cents": spend,
                "revenue_cents": revenue, "clicks": clicks, "impressions": sum(r["impressions"] for r in records),
                "cpc_cents": round(spend / clicks, 2) if clicks else None, "spend_ratio_pct": ratio,
                "enabled": enabled, "flag": flag, "modes": records})
            hours[-1].update(operating=business.get("operating",True),paid_orders=business.get("paid_orders"),visitors=business.get("visitors"))
            for row in records:
                for field in totals[row["mode"]]:
                    totals[row["mode"]][field] += row[field]
        total_spend, total_revenue = sum(r["spend_cents"] for r in hours), sum(r["revenue_cents"] for r in hours)
        return {"store_id": self.store_id, "date": day, "timezone": "Asia/Shanghai", "currency": "CNY",
            "mode": mode, "thresholds": {"high_pct": float(high), "low_pct": float(low)}, "hours": hours,
            "operating_profile": self.document.get("operating_profile",{}),
            "totals": {"spend_cents": total_spend, "revenue_cents": total_revenue,
                "clicks": sum(r["clicks"] for r in hours),
                "spend_ratio_pct": round(total_spend / total_revenue * 100, 2) if total_revenue else None},
            "by_mode": totals, "attention": {flag: [r["hour"] for r in hours if r["flag"] == flag]
                for flag in ("high", "low", "no_revenue")}, "fingerprint": self.fingerprint,
            "source_id": "flower-ad-hourly-ledger", "sample_data": True,
            "scope": "当日店铺营业额来自同一销售快照的小时分配；不是广告归因收入，费用率不等于ROAS或利润。",
            "limitations": ["低投入仅表示低于自定义关注线，不证明流量不足或需要加预算。", "小时扣费按点击发生时点归集，成交按支付时点归集，未控制转化延迟。",
                "账本为学习用合成数据；不调用推广后台、不修改出价或预算。"]}


def load_advertising(path, store_id):
    """只由已核验的本地店铺配置加载账本，HTTP不接受文件路径。"""
    return AdvertisingBook(json.loads(Path(path).read_text(encoding="utf-8")), store_id)
