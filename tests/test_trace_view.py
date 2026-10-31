"""执行轨迹：事件配对、拒绝原因、断言表与用量摘要；只读落盘产物，不执行模型。"""
import json
import shutil
import unittest
import uuid
from pathlib import Path

from ecommerce_agent.trace_view import build_trace


class TraceTests(unittest.TestCase):
    def setUp(self):
        """每次在独立临时目录构造一次运行，避免依赖真实 runs 的历史版本。"""
        self.root = Path.cwd() / "runs/_testtmp" / ("trace_" + uuid.uuid4().hex)
        self.run_id = "20261031T000000Z-abcdef12"
        self.directory = self.root / self.run_id
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name, value):
        """按真实产物格式写文件；事件是 JSONL，其余是 JSON。"""
        path = self.directory / name
        if name.endswith(".jsonl"):
            path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value), encoding="utf-8")
        else:
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def fixture(self):
        """覆盖模型、成功工具、被拒工具、finish 断言和收尾事件。"""
        self.write("run.json", {
            "run_id": self.run_id, "status": "completed", "mode": "live_model", "duration_ms": 4200,
            "sku_id": "SKU-A", "target_date": "2026-10-23", "model_calls": 2, "tool_calls": 2,
            "model_usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200, "cost": None},
        })
        base = "2026-10-31T04:00:00"
        events = [
            {"sequence": 1, "type": "run_started", "at": base + ".000000+00:00", "mode": "live_model"},
            {"sequence": 2, "type": "model_started", "at": base + ".000000+00:00", "step": 1},
            {"sequence": 3, "type": "model_completed", "at": base + ".500000+00:00", "step": 1, "tool_count": 2},
            {"sequence": 4, "type": "tool_started", "at": base + ".500000+00:00", "tool": "get_sales_metrics", "call_id": "call-1"},
            {"sequence": 5, "type": "tool_completed", "at": base + ".600000+00:00", "tool": "get_sales_metrics", "call_id": "call-1",
             "result": {"ok": True, "evidence_id": "source-1", "tool": "get_sales_metrics",
                        "data": {"table": "sales", "records": [{"date": "2026-10-23"}]}}},
            {"sequence": 6, "type": "tool_started", "at": base + ".600000+00:00", "tool": "finish", "call_id": "call-2"},
            {"sequence": 7, "type": "tool_rejected", "at": base + ".750000+00:00", "tool": "finish", "call_id": "call-2",
             "result": {"ok": False, "evidence_id": "call-2", "error": "Arguments must be JSON",
                        "verification": {"status": "failed", "checked": 1, "failed": 1,
                                         "checks": [{"evidence_id": "source-1", "date": "2026-10-23",
                                                     "field": "units_sold", "equals": 9, "status": "failed",
                                                     "reason": "value_mismatch"}]}}},
            {"sequence": 8, "type": "model_started", "at": base + ".750000+00:00", "step": 2},
            {"sequence": 9, "type": "model_completed", "at": base + "1.000000+00:00", "step": 2, "tool_count": 1},
            {"sequence": 10, "type": "tool_started", "at": base + "1.000000+00:00", "tool": "finish", "call_id": "call-3"},
            {"sequence": 11, "type": "tool_completed", "at": base + "1.100000+00:00", "tool": "finish", "call_id": "call-3",
             "result": {"ok": True, "evidence_id": "call-3", "tool": "finish",
                        "data": {"summary": "销量下降的结论与来源一致。", "evidence_ids": ["source-1"],
                                 "verification": {"status": "passed", "checked": 1, "failed": 0, "checks": []}}}},
            {"sequence": 12, "type": "run_settled", "at": base + "1.100000+00:00", "status": "completed",
             "stop_reason": "evidence_linked_draft_completed"},
        ]
        self.write("events.jsonl", events)
        self.write("request.json", {"raw_question": "99红玫 2026-10-23 销量下降原因", "intent": "investigate_sales_change",
                                    "parser": "deterministic_rules_v1"})
        self.write("evidence.json", {"observations": [
            {"evidence_id": "source-1", "tool": "get_sales_metrics", "retrieved_at": base + ".600000+00:00",
             "data": {"table": "sales", "records": [{"date": "2026-10-23"}]}}]})
        self.write("validation.json", {"status": "passed", "checked": 1, "failed": 0, "scope": "仅核对来源字段",
                                       "checks": [{"evidence_id": "source-1", "date": "2026-10-23", "field": "units_sold",
                                                   "equals": 9, "status": "passed", "reason": None}]})

    def test_timeline_pairs_calls_and_explains_rejection(self):
        """模型/工具事件按序配对；拒绝步骤保留错误信息和失败断言，finish 显示证据与草稿。"""
        self.fixture()
        trace = build_trace(self.root, self.run_id)
        self.assertEqual(trace["schema"], "trace-v1")
        self.assertEqual(trace["question"], "99红玫 2026-10-23 销量下降原因")
        summary = trace["summary"]
        self.assertEqual((summary["tool_calls"], summary["model_calls"]), (2, 2))
        self.assertEqual(summary["rejections"], 1)
        self.assertEqual(summary["tokens"]["total"], 1200)
        self.assertEqual(summary["assertions"]["checked"], 1)
        self.assertEqual(trace["evidence"][0]["rows"], 1)

        kinds = [step["kind"] for step in trace["steps"]]
        self.assertEqual(kinds, ["started", "model", "tool", "tool_rejected", "model", "tool", "settled"])
        model, tool, rejected, _, finish, settled = trace["steps"][1:]
        self.assertEqual(model["duration_ms"], 500)
        self.assertEqual(tool["duration_ms"], 100)
        self.assertEqual(tool["evidence_id"], "source-1")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "Arguments must be JSON")
        self.assertEqual(rejected["verification"]["failed"], 1)
        self.assertEqual(rejected["verification"]["checks"][0]["reason"], "value_mismatch")
        self.assertEqual(finish["evidence_ids"], ["source-1"])
        self.assertIn("销量下降", finish["answer"])
        self.assertEqual(finish["verification"]["status"], "passed")
        self.assertEqual(settled["stop_reason"], "evidence_linked_draft_completed")

    def test_rejects_bad_or_missing_run(self):
        """路径穿越、非法 ID 与不存在的运行一律拒绝，不能把任意目录当运行读取。"""
        for value in ("../x", "..", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                build_trace(self.root, value)
        with self.assertRaises(FileNotFoundError):
            build_trace(self.root, "20261031T000000Z-deadbeef")

    def test_partial_run_keeps_unknowns(self):
        """只有事件没有 run.json 时仍可展示待定工具，不伪造状态、用量或断言。"""
        self.write("events.jsonl", [
            {"sequence": 1, "type": "run_started", "at": "2026-10-31T04:00:00+00:00", "mode": "deterministic_workflow"},
            {"sequence": 2, "type": "tool_started", "at": "2026-10-31T04:00:01+00:00", "tool": "get_traffic_metrics",
             "call_id": "source-2"}])
        trace = build_trace(self.root, self.run_id)
        self.assertIsNone(trace["status"])
        self.assertIsNone(trace["summary"]["tokens"])
        self.assertIsNone(trace["validation"])
        self.assertEqual([step["kind"] for step in trace["steps"]], ["started", "tool_pending"])


if __name__ == "__main__":
    unittest.main()