"""经营场景回归：验证口语命中、新品无基线、零值、数据分层及最小工具范围。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.task_parser import ProductCatalog, parse_task
from ecommerce_agent.workflows import execute_task


class FlowerBusinessTests(unittest.TestCase):
    """使用完整经营快照验证业务行为，固定今天避免相对日期随日历漂移。"""

    def setUp(self):
        """目录和数据来自同一生成版本；测试输出全部写入临时目录。"""
        self.raw = business_data()
        self.store = DataStore.from_document(self.raw)
        self.catalog = ProductCatalog.from_document(business_catalog(), self.store.skus)

    def parse(self, question, **kwargs):
        """固定26日解析昨天为25日，不偷换成快照最后一天。"""
        return parse_task(question, self.catalog, self.store, today=date(2026, 10, 26), **kwargs)

    def test_red_mei_typo_requires_confirmation(self):
        """疑似错字即使只剩一个规格也先澄清，显式选定候选后才查询。"""
        question = "99红梅2026-10-25销量下降原因"
        pending = self.parse(question)
        self.assertEqual(pending["status"], "needs_clarification")
        self.assertEqual([p["sku_id"] for p in pending["candidates"]], ["FLR-ROSE-99-BOUQUET"])
        self.assertTrue(pending["product_resolution"]["suggested_correction"]["requires_confirmation"])
        confirmed = self.parse(question, sku_override="FLR-ROSE-99-BOUQUET")
        self.assertEqual(confirmed["status"], "ready")
        wrong = self.parse(question, sku_override="FLR-ROSE-19-BOUQUET")
        self.assertEqual(wrong["status"], "needs_clarification")

    def test_red_mei_does_not_choose_size_or_rewrite_real_flower(self):
        """没有规格不猜选，目录确有红梅时也不纠正为玫瑰。"""
        pending = self.parse("红梅2026-10-25销量怎么样")
        self.assertEqual(pending["status"], "needs_clarification")
        self.assertGreater(len(pending["candidates"]), 1)
        self.assertEqual(self.parse("66红梅2026-10-25销量怎么样")["status"], "needs_clarification")
        self.catalog.products["FLR-ROSE-99-BOUQUET"]["aliases"].append("红梅")
        actual = self.catalog.resolve("红梅2026-10-25销量怎么样")
        self.assertNotIn("suggested_correction", actual)

    def test_user_short_questions_match_but_require_missing_date(self):
        """省略朵数单位和具体错字可解释命中，缺日期仍不能运行。"""
        for question, sku in (("99红玫爆款销量下降原因", "FLR-ROSE-99-BOUQUET"),
                              ("33乌梅包括下降原因", "FLR-WUMEI-33-BUCKET")):
            task = self.parse(question)
            self.assertEqual(task["sku_id"], sku, task)
            self.assertEqual(task["status"], "needs_clarification")
            self.assertTrue(task["product_resolution"]["normalization_rules"])
            self.assertEqual(self.parse(question + "昨天")["status"], "ready")

    def test_new_product_uses_listing_date_not_invented_spec(self):
        """同品种三款中，25日新上架的只有一款；不靠销量大小选择。"""
        task = self.parse("昨天新上架的洋桔梗销量怎么样")
        self.assertEqual(task["status"], "ready", task)
        self.assertEqual(task["sku_id"], "FLR-EUSTOMA-11-BOUQUET")
        self.assertEqual(task["intent"], "query_sales_performance")
        self.assertEqual(task["target_date"], "2026-10-25")
        self.assertEqual(task["comparison_policy"], "target_day_only")
        self.assertEqual(task["product_resolution"]["constraints"]["listed_at"], "2026-10-25")

    def test_variety_or_series_performance_uses_group_but_causal_question_clarifies(self):
        """表现查询可覆盖同花材/系列全部款；单品原因调查仍明确规格。"""
        for query in ("洋桔梗昨天销量怎么样", "多巴胺昨天销量怎么样", "苹果杰克昨天销量怎么样"):
            self.assertEqual(self.parse(query)["status"], "ready", query)
            self.assertTrue(self.parse(query).get('product_scope'))
        self.assertEqual(self.parse('紫玫昨天销量下降原因')['status'],'needs_clarification')
        for query in ("天使之翼昨天销量怎么样", "晴天葡萄昨天销量怎么样"):
            self.assertEqual(self.parse(query)["status"], "ready", query)

    def test_two_new_products_same_day_still_clarify(self):
        """同一天同品种上新两款时，上架日期也不足以唯一定位。"""
        catalog = business_catalog()
        for product in catalog["products"]:
            if product["sku_id"] == "FLR-EUSTOMA-19-BOUQUET":
                product["listed_at"] = "2026-10-25"
        self.catalog = ProductCatalog.from_document(catalog, self.store.skus)
        task = self.parse("昨天新上架的洋桔梗销量怎么样")
        self.assertEqual(task["status"], "ready")
        self.assertTrue(task.get('product_scope'))
        self.assertEqual(len(task["candidates"]), 2)

    def test_mismatches_and_unknown_alias_do_not_execute(self):
        """爆款标签、规格和上架日都是合取约束，未知错别字不自动纠正。"""
        for query in ("19红玫爆款昨天销量下降原因", "99乌梅昨天销量下降原因",
                      "昨天新上架的苹果杰克销量怎么样", "33乌每包括昨天下降原因"):
            self.assertEqual(self.parse(query)["status"], "needs_clarification", query)

    def test_new_product_decline_requires_baseline(self):
        """新品可以查单日事实，但没有24日记录时，不能做25日下降归因。"""
        task = self.parse("昨天新上架的洋桔梗销量下降原因")
        self.assertEqual(task["status"], "needs_data", task)
        rows = self.store.query("sales", task["sku_id"], task["target_date"])["records"]
        self.assertEqual(rows[0]["status"], "unavailable")
        self.assertNotIn("units_sold", rows[0])

    def test_sales_performance_adds_trend_calendar_and_keeps_evidence(self):
        """新品允许无前日基线，新增趋势/日历工具，不调用模型。"""
        task = self.parse("昨天新上架的洋桔梗销量怎么样")
        with tempfile.TemporaryDirectory() as output:
            state = execute_task(self.store, task["sku_id"], task["target_date"], output, task_context=task)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["tool_calls"], 3)
            self.assertEqual(state["model_calls"], 0)
            self.assertEqual(state["validation"]["status"], "passed")
            metrics = {item["label"]: item["value"] for item in state["analysis"]["display_metrics"]}
            target = self.store.query("sales", task["sku_id"], task["target_date"])["records"][-1]
            self.assertEqual(metrics["成品销量"], target["units_sold"])
            folder = Path(state["run_dir"])
            events = [json.loads(row) for row in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["tool"] for e in events if e["type"] == "tool_started"], ["get_sales_trend", "get_calendar_context", "get_sales_metrics"])

    def test_dataset_has_realistic_count_types_zero_sales_and_missing(self):
        """计数保持整数；金额有分币；明确区分零成交、零曝光、延迟和上架前缺失。"""
        self.assertEqual(len(self.catalog.products), 28)
        self.assertGreater(len(self.raw["sales"]), 350)
        self.assertTrue(any(row["visitors"] % 100 for row in self.raw["traffic"] if row["status"] == "ok"))
        self.assertTrue(all(type(row["visitors"]) is int for row in self.raw["traffic"] if row["status"] == "ok"))
        self.assertTrue(any(row["paid_revenue"] != int(row["paid_revenue"]) for row in self.raw["sales"]))
        row = self.store.query("sales", "FLR-GRAPE-19-BOUQUET", "2026-10-25")["records"][-1]
        self.assertEqual((row["status"], row["units_sold"], row["paid_revenue"]), ("ok", 0, 0))
        self.assertTrue(any(row.get("visitors") == 0 for row in self.raw["traffic"]))
        self.assertTrue(any(row["status"] == "delayed" for row in self.raw["traffic"]))
        self.assertTrue(self.raw["sample_data"])
        self.assertIn("合成", self.raw["business_context"]["provenance"])

    def test_channels_are_exclusive_and_sum_to_daily_uv(self):
        """互斥归因渠道必须加总等于当天UV，不能为了更自然的数字破坏口径。"""
        for row in self.raw["traffic_channels"]:
            traffic = self.store.query("traffic", row["sku_id"], row["date"])["records"][-1]
            self.assertEqual(sum(row["attributed_visitors"].values()), traffic["visitors"])

    def test_all_full_titles_resolve_without_shared_marketing_conflicts(self):
        """扩目录后仍能用完整标题定位，系列中的共享词不能抢走明确花名。"""
        for product in self.catalog.products.values():
            task = self.parse(product["name"] + "2026-10-25销量怎么样")
            self.assertEqual(task["status"], "ready", task)
            self.assertEqual(task["sku_id"], product["sku_id"])

    def test_zero_sales_and_zero_traffic_analysis_does_not_invent_ratio(self):
        """零销量可分析，零UV不应产生除零异常、NaN或虚构转化率。"""
        for name in ("晴天葡萄", "蓝屿清风"):
            task = self.parse(name + "昨天销量下降原因")
            self.assertEqual(task["status"], "ready", task)
            with tempfile.TemporaryDirectory() as output:
                state = execute_task(self.store, task["sku_id"], task["target_date"], output, task_context=task)
                self.assertEqual(state["status"], "completed", state)
                self.assertEqual(state["analysis"]["facts"]["units_sold"]["after"], 0)
                conversion = state["analysis"]["facts"].get("payment_conversion_pct")
                if name == "蓝屿清风":
                    self.assertTrue(conversion is None or conversion["after"] is None)


if __name__ == "__main__":
    unittest.main()
