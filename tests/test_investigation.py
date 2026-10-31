"""第一版业务回归：用反例检查指标、证据、工具边界和执行终态。

这些测试验证程序契约；ModelFixture/HTTP mock 只验证协议，不代表真实模型效果。
"""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ecommerce_agent.analysis import change
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_data
from ecommerce_agent.runner import run
from ecommerce_agent.tools import TABLES, ToolRegistry


class InvestigationTests(unittest.TestCase):
    """每个测试使用独立临时快照，确认分析不会修改用户输入。"""
    def setUp(self):
        """准备可复现的模拟数据，并在测试结束后清理临时目录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "data.json"
        self.write(sample_data())

    def write(self, data):
        """将测试输入写到临时文件，不覆盖项目自带 sample.json。"""
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def investigate(self, sku, **kwargs):
        """统一测试调查入口，固定日期以避免测试随当前时间漂移。"""
        return run(DataStore(self.path), sku, "2026-10-23", self.root / "runs", **kwargs)

    def test_five_cases_end_to_end_and_input_unchanged(self):
        """验证五类业务结果、原文件不变、事件终态以及证据快照哈希。"""
        original = self.path.read_bytes()
        expected = {"FLR-ROSE-99-BOUQUET": ("stockout_signal", "complete_for_supplied_daily_fields"),
                    "FLR-ROSE-19-BOUQUET": ("traffic_signal", "complete_for_supplied_daily_fields"),
                    "FLR-CARNATION-19-BOUQUET": ("promotion_ended_signal", "complete_for_supplied_daily_fields"),
                    "FLR-ROSE-33-BUCKET": (None, "partial"), "FLR-SUNFLOWER-11-BUCKET": (None, "conflicting")}
        for sku, (signal, quality) in expected.items():
            with self.subTest(sku=sku):
                result = self.investigate(sku)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["analysis"]["data_quality"], quality)
                self.assertEqual(result["analysis"]["causal_root_cause"], "not_established")
                signals = [item["id"] for item in result["analysis"]["hypotheses"]]
                self.assertIn(signal, signals) if signal else self.assertEqual(signals, [])
                directory = Path(result["run_dir"])
                self.assertIn("学习用合成经营快照，非真实店铺记录", (directory / "report.md").read_text(encoding="utf-8"))
                events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(events[-1]["type"], "run_settled")
                self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
                snapshot = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
                digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                self.assertEqual(digest, result["evidence_sha256"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_conversion_uses_buyers_not_units_or_orders(self):
        """订单和件数变化不应影响以买家/访客为口径的支付转化率。"""
        data = sample_data()
        for row in data["sales"]:
            if row["sku_id"] == "FLR-ROSE-19-BOUQUET":
                row["paid_orders"] *= 2
                row["units_sold"] *= 3
        self.write(data)
        fact = self.investigate("FLR-ROSE-19-BOUQUET")["analysis"]["facts"]["payment_conversion_pct"]
        self.assertEqual((fact["before"], fact["after"], fact["percentage_point_change"]), (10, 10, 0))

    def test_missing_and_delayed_are_not_zero(self):
        """延迟和缺失均不能被填成零后拿去计算跌幅。"""
        analysis = self.investigate("FLR-ROSE-33-BUCKET")["analysis"]
        self.assertNotIn("visitors", analysis["facts"])
        self.assertEqual({gap["table"] for gap in analysis["gaps"]}, {"traffic", "inventory"})

    def test_zero_baseline_does_not_divide_by_zero(self):
        """基线为零时增长率未知，不输出无穷大或误报百分比。"""
        self.assertIsNone(change(0, 10)["relative_pct"])

    def test_inventory_conflict_does_not_claim_stockout_root_cause(self):
        """全日缺货与有成交冲突时，应要求核对而不是认定缺货根因。"""
        analysis = self.investigate("FLR-SUNFLOWER-11-BUCKET")["analysis"]
        self.assertEqual(analysis["conflicts"][0]["kind"], "full_day_stockout_with_sales")
        self.assertEqual(analysis["hypotheses"], [])

    def test_partial_stock_monitoring_is_not_full_day_signal(self):
        """只监测部分时段，不足以得出全天缺货信号。"""
        data = sample_data()
        for row in data["inventory"]:
            if row["sku_id"] == "FLR-ROSE-99-BOUQUET" and row["date"] == "2026-10-23":
                row["monitored_minutes"] = 600
        self.write(data)
        analysis = self.investigate("FLR-ROSE-99-BOUQUET")["analysis"]
        self.assertEqual(analysis["data_quality"], "partial")
        self.assertEqual(analysis["hypotheses"], [])

    def test_invalid_data_contracts(self):
        """重复、负数、无时区或缺少数据标签等输入错误应在加载时拒绝。"""
        for mutation in ("duplicate", "negative", "naive_time", "missing_sample_flag", "bad_window"):
            data = sample_data()
            if mutation == "duplicate":
                data["sales"].append(copy.deepcopy(data["sales"][0]))
            elif mutation == "negative":
                data["traffic"][0]["visitors"] = -1
            elif mutation == "naive_time":
                data["sales"][0]["observed_at"] = "2026-10-24T08:00:00"
            elif mutation == "missing_sample_flag":
                del data["sample_data"]
            else:
                data["inventory"][0]["out_of_stock_minutes"] = 1500
            self.write(data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                DataStore(self.path)

    def test_unknown_sku_and_invalid_date(self):
        """调查对象和日期非法时，在工具执行前终止。"""
        with self.assertRaises(ValueError):
            self.investigate("UNKNOWN")
        with self.assertRaises(ValueError):
            run(DataStore(self.path), "FLR-ROSE-19-BOUQUET", "wrong-date", self.root / "runs")

    def test_no_write_tool(self):
        """模型即使请求改价，工具注册表也没有这种能力。"""
        registry = ToolRegistry(DataStore(self.path), "FLR-ROSE-19-BOUQUET", "2026-10-23")
        self.assertFalse(registry.execute("change_price", {}, "bad")["ok"])

    def test_cannot_override_sku_in_tools(self):
        """SKU 由宿主冻结，模型不能通过额外参数切换调查对象。"""
        registry = ToolRegistry(DataStore(self.path), "FLR-ROSE-19-BOUQUET", "2026-10-23")
        self.assertFalse(registry.execute("get_sales_metrics", {"sku_id": "OTHER"}, "bad")["ok"])

    def test_finish_requires_analysis_and_real_source_references(self):
        """完成请求必须有分析和实际来源引用，拒绝编造 evidence ID。"""
        registry = ToolRegistry(DataStore(self.path), "FLR-ROSE-19-BOUQUET", "2026-10-23")
        args = {"summary": "draft", "evidence_ids": ["fake"]}
        self.assertFalse(registry.execute("finish", args, "early")["ok"])
        self.assertFalse(registry.execute("analyze_metrics", {}, "too-early")["ok"])
        refs = []
        for index, name in enumerate(TABLES):
            ref = f"source-{index}"
            refs.append(ref)
            self.assertTrue(registry.execute(name, {}, ref)["ok"])
        registry.execute("analyze_metrics", {}, "analysis")
        self.assertFalse(registry.execute("finish", args, "fake")["ok"])
        self.assertTrue(registry.execute("finish", {**args, "evidence_ids": refs}, "done")["ok"])

    def test_model_tool_loop_and_budget(self):
        """协议 fixture 检查多轮工具回填及预算耗尽，不测模型推理质量。"""
        class ModelFixture:
            def __init__(self):
                self.steps = [(name, {}) for name in TABLES] + [("analyze_metrics", {}), ("finish", {
                    "summary": "流量和销量同时下降，需要进一步核查渠道，不能认定因果。",
                    "evidence_ids": ["call-1", "call-2", "call-3", "call-4"]})]
                self.index = 0

            def complete(self, messages, tools, timeout):
                name, args = self.steps[self.index]
                self.index += 1
                return {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{self.index}",
                        "function": {"name": name, "arguments": json.dumps(args)}, "type": "function"}]}
        result = self.investigate("FLR-ROSE-19-BOUQUET", provider=ModelFixture())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_draft"]["narrative_status"], "unverified_model_draft")
        short = self.investigate("FLR-ROSE-19-BOUQUET", provider=ModelFixture(), max_steps=1)
        self.assertEqual(short["status"], "incomplete")
        self.assertEqual(short["stop_reason"], "step_budget")

    def test_batched_tools_have_sequential_receipts_and_finish_boundary(self):
        """多查询同批返回时逐一回填；finish 后的多余工具不能再改变证据。"""
        class BatchFixture:
            def __init__(self):
                self.index = 0

            def complete(self, messages, tools, timeout):
                batches = [[(name, {}) for name in TABLES], [("analyze_metrics", {})],
                    [("finish", {"summary": "需进一步核查渠道。", "evidence_ids": [f"batch-{n}" for n in range(4)]}),
                     ("get_sales_metrics", {})]]
                if self.index:
                    previous = next(message for message in reversed(messages) if message["role"] == "assistant")
                    self.receipts = [message["tool_call_id"] for message in messages[-len(previous["tool_calls"]):]]
                    assert self.receipts == [call["id"] for call in previous["tool_calls"]]
                batch = batches[self.index]
                offset = (0, 4, 5)[self.index]
                self.index += 1
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"batch-{offset+n}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}} for n, (name, args) in enumerate(batch)]}

        result = self.investigate("FLR-ROSE-19-BOUQUET", provider=BatchFixture())
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["model_calls"], result["tool_calls"]), (3, 6))
        messages = json.loads((Path(result["run_dir"]) / "conversation.json").read_text(encoding="utf-8"))["messages"]
        self.assertEqual(json.loads(messages[-1]["content"])["error"], "Investigation already finished")

    def test_invalid_batch_rejected_before_any_execution(self):
        """整批大小和 ID 校验在调用前执行，避免执行半批才发现协议错误。"""
        class BadBatch:
            def __init__(self, count, duplicate):
                self.count, self.duplicate = count, duplicate

            def complete(self, *args):
                return {"role": "assistant", "tool_calls": [
                    {"id": "same" if self.duplicate else str(n), "function": {
                        "name": "get_sales_metrics", "arguments": "{}"}} for n in range(self.count)]}

        for count, duplicate in ((2, True), (7, False)):
            result = self.investigate("FLR-ROSE-19-BOUQUET", provider=BadBatch(count, duplicate))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["tool_calls"], 0)

    def test_provider_failure_has_terminal_report(self):
        """模型连接异常也必须有失败终态与报告，不让任务永久显示运行中。"""
        class Broken:
            def complete(self, *args):
                raise ValueError("offline")
        result = self.investigate("FLR-ROSE-19-BOUQUET", provider=Broken())
        self.assertEqual(result["status"], "failed")
        self.assertTrue((Path(result["run_dir"]) / "report.md").is_file())

    def test_buyer_traffic_conflict_suppresses_conversion(self):
        """同口径买家超过访客时，标冲突而不是输出超过 100% 的转化率。"""
        data = sample_data()
        for row in data["traffic"]:
            if row["sku_id"] == "FLR-ROSE-19-BOUQUET" and row["date"] == "2026-10-23":
                row["visitors"] = 1
        self.write(data)
        analysis = self.investigate("FLR-ROSE-19-BOUQUET")["analysis"]
        self.assertEqual(analysis["data_quality"], "conflicting")
        self.assertNotIn("payment_conversion_pct", analysis["facts"])
        self.assertNotIn("traffic_signal", [item["id"] for item in analysis["hypotheses"]])

    def test_model_plain_success_statement_cannot_finish(self):
        """一句“完成了”无法替代工具执行和 finish 验收。"""
        class PlainAnswer:
            def complete(self, *args):
                return {"role": "assistant", "content": "All done"}
        result = self.investigate("FLR-ROSE-19-BOUQUET", provider=PlainAnswer())
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["analysis"])

    def test_http_provider_with_local_mock(self):
        """本地 HTTP 服务验证请求路径和工具协议，没有访问外部模型。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from ecommerce_agent.provider import ChatProvider

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "http-1", "type": "function", "function": {"name": "get_sales_metrics", "arguments": "{}"}}]}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ChatProvider({"api_base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                     "model": "mock-model", "api_key_env": "UNUSED_ECOMMERCE_TEST_KEY"})
            result = provider.complete([{"role": "user", "content": "test"}], [], 3)
            self.assertEqual(result["tool_calls"][0]["id"], "http-1")
            self.assertEqual(requests[0][0], "/v1/chat/completions")
            # 普通文本通道不发送空工具配置；有工具时仍显式禁止并行执行。
            self.assertNotIn('tools',requests[0][1]);self.assertNotIn('tool_choice',requests[0][1])
            from ecommerce_agent.tools import SCHEMAS
            provider.complete([{'role':'user','content':'test tools'}],SCHEMAS,3)
            self.assertFalse(requests[1][1]['parallel_tool_calls'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
