"""第二步回归：用实际输入验证解析、拒绝与执行路由，而不只检查正则实现。"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ecommerce_agent.cli import ROOT, main
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.runner import run
from ecommerce_agent.task_parser import ProductCatalog, parse_dates, parse_task


class TaskParserTests(unittest.TestCase):
    """固定业务今天，防止日期相关测试随日历翻页失效。"""

    def setUp(self):
        """每个用例都有自己的输入与输出，避免测试污染真实样例和用户运行记录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_path = self.root / "data.json"
        self.data_path.write_text(json.dumps(sample_data()), encoding="utf-8")
        self.store = DataStore(self.data_path)
        self.catalog_path = self.root / "products.json"
        self.catalog_path.write_text(json.dumps(sample_catalog()), encoding="utf-8")
        self.catalog = ProductCatalog(self.catalog_path, self.store.skus)

    def parse(self, question, **kwargs):
        """默认在样例日期后一天解析，显式覆盖时再测试真实日期缺口。"""
        kwargs.setdefault("today", date(2026, 10, 24))
        return parse_task(question, self.catalog, self.store, **kwargs)

    def test_named_product_yesterday_and_iso_date_agree(self):
        """名称解析与直接 SKU 输入应归一到相同的业务任务。"""
        relative = self.parse("帮我查怦然心动昨天销量下降原因")
        explicit = self.parse("FLR-ROSE-19-BOUQUET 2026-10-23销量下降原因")
        for field in ("sku_id", "target_date", "comparison_date", "status"):
            self.assertEqual(relative[field], explicit[field])
        self.assertEqual(relative["status"], "ready")

    def test_generic_product_requires_variant(self):
        """不能把同名商品的第一个 SKU 自动当成用户指定的规格。"""
        task = self.parse("红玫瑰花束昨天销量下降原因")
        self.assertEqual(task["status"], "needs_clarification")
        self.assertEqual({p["sku_id"] for p in task["candidates"]}, {"FLR-ROSE-99-BOUQUET", "FLR-ROSE-19-BOUQUET"})
        self.assertIsNone(task["sku_id"])

    def test_sku_override_resolves_ambiguity_but_not_conflict(self):
        """显式参数可补全信息，但不能覆盖问题里指向另一个商品的明确信息。"""
        ready = self.parse("红玫瑰花束昨天销量下降原因", sku_override="FLR-ROSE-19-BOUQUET")
        self.assertEqual(ready["status"], "ready")
        conflict = self.parse("炽热爱意昨天销量下降原因", sku_override="FLR-ROSE-19-BOUQUET")
        self.assertEqual(conflict["status"], "needs_clarification")

    def test_two_products_are_not_silently_merged(self):
        """多商品问题不能把一方的流量和另一方的销量拼起来调查。"""
        task = self.parse("炽热爱意和怦然心动昨天销量下降原因")
        self.assertEqual(len(task["candidates"]), 2)
        self.assertEqual(task["status"], "needs_clarification")

    def test_missing_slots_and_unknown_product(self):
        """没有明确对象或日期时，不默认猜一个可运行任务。"""
        for text in ("怦然心动销量下降原因", "昨天销量下降原因", "电视机昨天销量下降原因"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["status"], "needs_clarification")
        self.assertEqual(self.parse("销量下降原因", sku_override="FLR-CARNATION-19-BOUQUET", date_override="2026-10-23")["status"], "ready")

    def test_yesterday_does_not_fall_back_to_latest_data(self):
        """今天为 25 日时，昨天必须是 24 日，不能为跑通样例而改成 23 日。"""
        task = self.parse("怦然心动昨天销量下降原因", today=date(2026, 10, 25))
        self.assertEqual(task["target_date"], "2026-10-24")
        self.assertEqual(task["status"], "needs_data")

    def test_today_future_and_missing_baseline(self):
        """完整日调查不接受当天部分数据；缺少对比日也不能生成增长率。"""
        for word in ("今天", "明天"):
            self.assertEqual(self.parse(f"怦然心动{word}销量下降原因")["status"], "needs_clarification")
        self.assertEqual(self.parse("怦然心动2026-10-22销量下降原因")["status"], "needs_data")

    def test_date_formats_invalid_dates_and_override_conflict(self):
        """完整年月日可复现，月日按本年；非法、未来和冲突日期退回确认。"""
        for text in ("2026年10月23日", "2026/10/23", "2026-10-23"):
            self.assertEqual(self.parse(f"温柔时光{text}销量下降原因")["status"], "ready")
        self.assertEqual(self.parse('温柔时光10月23日销量下降原因')['status'],'ready')
        for text in ("2026-02-30", "12月23日", "2026-10-22与2026-10-23"):
            self.assertEqual(self.parse(f"温柔时光{text}销量下降原因")["status"], "needs_clarification")
        self.assertEqual(self.parse("温柔时光昨天销量下降原因", date_override="2026-10-22")["status"], "needs_clarification")

    def test_cross_month_relative_date(self):
        """日期运算要跨月，不应将 3 月 1 日的昨天变成非法的 3 月 0 日。"""
        values, errors = parse_dates("昨天", date(2026, 3, 1))
        self.assertEqual(values, ["2026-02-28"])
        self.assertEqual(errors, [])

    def test_write_actions_and_unsupported_scope_do_not_route(self):
        """限定调查意图，拒绝写操作、同比和多日区间，不把它们偷换成已有流程。"""
        for text in ("怦然心动昨天销量下降，直接改价", "怦然心动同比销量下降原因", "温柔时光昨天销售额下降原因"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["status"], "unsupported")

    def test_other_sources_can_be_missing_after_task_is_ready(self):
        """销售数据足够就允许调查，其他来源的缺口应由质量检查报告，而非全部拦掉。"""
        task = self.parse("心动满怀昨天销量下降原因")
        self.assertEqual(task["status"], "ready")
        state = run(self.store, task["sku_id"], task["target_date"], self.root / "runs", task_context=task)
        self.assertEqual(state["analysis"]["data_quality"], "partial")

    def test_task_context_is_preserved_in_run_and_report(self):
        """一份报告必须能追溯到用户原话、解析日期及比较假设。"""
        task = self.parse("怦然心动昨天销量下降原因")
        state = run(self.store, task["sku_id"], task["target_date"], self.root / "runs", task_context=task)
        folder = Path(state["run_dir"])
        saved = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, task)
        self.assertIn(task["raw_question"], (folder / "report.md").read_text(encoding="utf-8"))

    def test_runner_rejects_unresolved_or_mismatched_context(self):
        """即使绕过 CLI，也不能将未消歧的任务卡伪装成执行上下文。"""
        task = self.parse("红玫瑰花束昨天销量下降原因")
        with self.assertRaises(ValueError):
            run(self.store, "FLR-ROSE-19-BOUQUET", "2026-10-23", self.root / "runs", task_context=task)
        self.assertFalse((self.root / "runs").exists())

    def test_catalog_validation_and_explicit_sku_boundary(self):
        """目录中的未知 SKU 应拒绝，SKU 前缀相似也不能当成精确匹配。"""
        self.assertEqual(self.catalog.match("FLR-ROSE-19-BOUQUET-OLD昨天销量下降"), [])
        wrong = self.root / "products.json"
        wrong.write_text(json.dumps({"schema_version": 1, "products": [{
            "sku_id": "UNKNOWN", "name": "商品", "variant": "规格"}]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProductCatalog(wrong, self.store.skus)

    def test_cli_ready_and_clarification_paths(self):
        """端到端调用真实 CLI，确认正常任务生成报告，歧义任务只落盘任务卡。"""
        for query, expected in (("怦然心动2026-10-23销量下降原因", 0), ("红玫瑰花束昨天销量下降原因", 3)):
            output = self.root / str(expected)
            argv = ["ecommerce_agent", "ask", query, "--data", str(self.data_path),
                    "--catalog", str(self.catalog_path), "--today", "2026-10-24", "--output", str(output)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), expected)
            self.assertEqual(len(list((output / "requests").glob("*.json"))), 1)
            self.assertEqual(len(list(output.glob("*/report.md"))), 1 if expected == 0 else 0)

    def test_preview_does_not_execute_tools(self):
        """预览只解析槽位，不读取四类业务工具，也不创建调查 Run。"""
        output = self.root / "preview"
        argv = ["ecommerce_agent", "ask", "怦然心动昨天销量下降原因", "--data", str(self.data_path),
                "--catalog", str(self.catalog_path), "--today", "2026-10-24", "--output", str(output), "--preview"]
        with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()), patch("ecommerce_agent.cli.run") as execute:
            self.assertEqual(main(), 0)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
