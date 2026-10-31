"""多入口、有界深挖与权限回归：检查实际执行的工具和保存的产物。"""

import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.cli import ROOT
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.routing import route_question
from ecommerce_agent.task_parser import ProductCatalog, parse_task
from ecommerce_agent.tools import ToolRegistry
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.workflows import execute_task
from ecommerce_agent.evaluation import evaluate


class RoutingWorkflowTests(unittest.TestCase):
    """临时数据隔离所有运行记录，断言业务行为而非仅检查函数返回标签。"""

    def setUp(self):
        """固定业务时间，保留可修改样例以注入缺失和冲突。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalog_path = self.root / "products.json"
        self.catalog_path.write_text(json.dumps(sample_catalog()), encoding="utf-8")
        self.raw = sample_data()
        self.path = self.root / "sample.json"
        self.reload()

    def reload(self):
        """重建数据快照，模拟数据接入边界，不修改服务运行中的对象。"""
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.store = DataStore(self.path)
        self.catalog = ProductCatalog(self.catalog_path, self.store.skus)

    def task(self, text, **kwargs):
        """对外入口统一先路由再解析参数，固定今天避免日期漂移。"""
        return parse_task(text, self.catalog, self.store, today=date(2026, 10, 24), **kwargs)

    def execute(self, text):
        """执行真实分发器并返回状态及落盘事件，用于核对工具最小集合。"""
        task = self.task(text)
        self.assertEqual(task["status"], "ready", task)
        state = execute_task(self.store, task["sku_id"], task["target_date"], self.root / "runs", task_context=task)
        folder = Path(state["run_dir"])
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        return state, events

    def test_three_routes_execute_only_needed_tools(self):
        """库存与活动专项各查询一个来源，销量调查只在符合条件时多查渠道。"""
        for text, expected in (("炽热爱意2026-10-23缺货多久", {"get_inventory_status"}),
                               ("温柔时光2026-10-23活动结束了吗", {"get_promotion_history"})):
            state, events = self.execute(text)
            self.assertEqual(state["status"], "completed")
            self.assertEqual({e["tool"] for e in events if e["type"] == "tool_started"}, expected)
            self.assertEqual(state["tool_calls"], 1)
            for filename in ("report.md", "evidence.json", "events.jsonl", "request.json"):
                self.assertTrue((Path(state["run_dir"]) / filename).read_bytes())

    def test_mixed_intent_is_not_executed_until_chosen(self):
        """混合请求只产生候选，显式选择合法候选后才能继续；不能选任意意图。"""
        text = "怦然心动2026-10-23销量下降，顺便查库存"
        task = self.task(text)
        self.assertEqual(task["status"], "needs_clarification")
        self.assertEqual(len(task["routing"]["candidates"]), 2)
        selected = self.task(text, intent_override="query_inventory")
        self.assertEqual(selected["status"], "ready")
        wrong = self.task(text, intent_override="query_promotions")
        self.assertEqual(wrong["status"], "needs_clarification")
        with self.assertRaises(ValueError):
            execute_task(self.store, "FLR-ROSE-19-BOUQUET", "2026-10-23", self.root / "runs", task_context=task)

    def test_write_request_cannot_be_hidden_by_sales_or_override(self):
        """退款/改价与销量混合也被拦住；选择只读标签不能绕过原始请求的边界。"""
        for text in ("怦然心动2026-10-23销量下降，顺便退款", "查库存后补货", "查活动然后调整广告预算"):
            self.assertEqual(route_question(text, "query_inventory")["status"], "unsupported")

    def test_causal_mentions_are_not_three_independent_intents(self):
        """调查中提到库存或活动是原因候选，不应机械拆成多任务。"""
        result = route_question("怦然心动昨天销量下降，是不是库存不足或者活动结束？")
        self.assertEqual(result["intent"], "investigate_sales_change")
        self.assertEqual(result["status"], "selected")

    def test_inventory_does_not_require_sales_baseline(self):
        """库存专项只需目标日库存；不能被不相关的销售缺失挡住。"""
        self.raw["sales"] = []
        self.reload()
        self.assertEqual(self.task("炽热爱意2026-10-23库存情况")["status"], "ready")
        self.assertEqual(self.task("心动满怀2026-10-23库存情况")["status"], "needs_data")

    def test_tool_allowlist_is_enforced(self):
        """即使直接调用 Registry，专项工具集合之外也不能执行。"""
        registry = ToolRegistry(self.store, "FLR-ROSE-99-BOUQUET", "2026-10-23", allowed_tools={"get_inventory_status"})
        self.assertFalse(registry.execute("get_sales_metrics", {}, "x")["ok"])
        self.assertFalse(registry.execute("get_channel_breakdown", {}, "y")["ok"])

    def test_traffic_followup_is_bounded_and_reconciled(self):
        """流量信号只追加一次，来源证据进入快照，并保留可核验的渠道变化。"""
        state, events = self.execute("怦然心动2026-10-23销量下降原因")
        self.assertEqual(state["tool_calls"], 8)
        self.assertEqual(state["follow_up"]["status"], "completed")
        self.assertEqual(state["follow_up"]["rows"][0]["absolute"], -350)
        self.assertEqual(sum(e["type"] == "tool_started" and e["tool"] == "get_channel_breakdown" for e in events), 1)
        evidence = json.loads((Path(state["run_dir"]) / "evidence.json").read_text(encoding="utf-8"))
        self.assertIn("traffic_channels", {o["data"]["table"] for o in evidence["observations"]})

    def test_conflicting_base_evidence_stops_followup(self):
        """冲突优先于深挖：库存与成交矛盾时不能继续自动给出更多归因。"""
        state, events = self.execute("向阳而生2026-10-23销量下降原因")
        self.assertIsNone(state["follow_up"])
        decisions = [e for e in events if e["type"] == "follow_up_decision"]
        self.assertFalse(decisions[0]["selected"])
        self.assertIn("冲突", decisions[0]["reason"])

    def test_missing_and_inconsistent_channels_do_not_claim_success(self):
        """渠道缺失或合计不匹配时，保留基础分析，深挖明确标记不足/冲突。"""
        for channels, expected in (([], "insufficient_data"), (sample_data()["traffic_channels"], "conflicting")):
            if channels:
                channels[-1]["attributed_visitors"]["推荐"] += 1
            self.raw["traffic_channels"] = channels
            self.reload()
            state, _ = self.execute("怦然心动2026-10-23销量下降原因")
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["follow_up"]["status"], expected)
            self.assertEqual(state["follow_up"]["rows"], [])

    def test_channel_contract_rejects_overlap_policy_and_boolean_counts(self):
        """普通跨渠道 UV 不可相加；布尔值也不能伪装成人数。"""
        self.raw["traffic_channels"][0]["attribution_policy"] = "overlapping_uv"
        with self.assertRaises(ValueError):
            self.reload()
        self.raw = sample_data()
        self.raw["traffic_channels"][0]["attributed_visitors"]["推荐"] = True
        with self.assertRaises(ValueError):
            self.reload()

    def test_web_service_mixed_followup_and_lookup(self):
        """通过网页共用服务验证澄清→选择意图→保存关联→后台专项查询。"""
        service = InvestigationService(self.path, self.catalog_path, self.root / "web", self.root / "web-runs")
        self.addCleanup(service.close)
        question = "炽热爱意2026-10-23销量下降，顺便查库存"
        first = service.create({"question": question})["task"]
        self.assertEqual(first["status"], "needs_clarification")
        task = service.create({"question": question, "intent": "query_inventory", "parent_id": first["id"]})["task"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = service.get(task["id"])
            if task["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["result"]["tool_calls"], 1)
        self.assertEqual(task["parent_id"], first["id"])

    def test_evaluation_reports_real_errors_and_rejections(self):
        """评测器必须能记录错误，不允许把所有输入都输出成漂亮的成功指标。"""
        cases = self.root / "evaluation.json"
        cases.write_text(json.dumps({"cases": [
            {"question": "查库存", "expected": "query_promotions"},
            {"question": "帮我退款", "expected": "unsupported"}]}), encoding="utf-8")
        result = evaluate(cases, self.root / "evaluation")
        self.assertEqual(result["correct"], 1)
        self.assertEqual(result["confusion_matrix"]["query_promotions"]["query_inventory"], 1)
        self.assertEqual(result["unsupported_selected_count"], 0)
        self.assertEqual(result["model_calls"], 0)

    def test_negated_write_and_explanatory_mentions(self):
        """有限否定可允许只读请求；解释性提到库存和促销不机械拆分。"""
        for text, expected in (("不要补货，只查库存", "query_inventory"),
                               ("查销量，看看库存和促销能否解释变化", "investigate_sales_change")):
            self.assertEqual(route_question(text)["intent"], expected)


if __name__ == "__main__":
    unittest.main()
