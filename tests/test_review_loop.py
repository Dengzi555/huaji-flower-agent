"""人工核验闭环：从真实冲突到版本化复查和验收，并验证越界、重复与并发。"""

import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
from datetime import date
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path

from ecommerce_agent.cli import ROOT
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.reviews import ReviewConflict
from ecommerce_agent.tools import ToolRegistry
from ecommerce_agent.verification import verify_assertions
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService


class ReviewLoopTests(unittest.TestCase):
    """所有材料和任务落在临时工作区，确认原文件和原 Run 永远不被修正覆盖。"""

    def setUp(self):
        """准备原本有“全日缺货但有成交”冲突的合成案例。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalog_path = self.root / "products.json"
        self.catalog_path.write_text(json.dumps(sample_catalog()), encoding="utf-8")
        self.path = self.root / "data.json"
        self.path.write_text(json.dumps(sample_data()), encoding="utf-8")
        self.service = InvestigationService(self.path, self.catalog_path, self.root / "state", self.root / "runs")
        self.addCleanup(lambda: self.service.close())

    def wait(self, task_id):
        """等待明确终态，不能将 HTTP 提交成功当成调查完成。"""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.service.get(task_id)
            if task["status"] not in ("queued", "running"):
                return task
            time.sleep(.02)
        self.fail("Task did not settle")

    def conflict_case(self):
        """从业务入口产生实际冲突与自动工单，不手工插入业务状态。"""
        task = self.service.create({"question": "向阳而生2026-10-23销量下降原因"})["task"]
        task = self.wait(task["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["review_case"]["status"], "awaiting_human")
        return task, self.service.reviews.get(task["review_case"]["id"])

    def material(self, case, corrections=True):
        """提交明确来源的人工修正；它是人工材料，不代表来源真实性已被程序证明。"""
        return self.service.reviews.add_material(case["id"], {
            "expected_revision": case["revision"], "note": "核对后发现原全日缺货记录口径错误，提供日级修正。",
            "source": "测试人工盘点表", "corrections": [{"table": "inventory", "date": "2026-10-23",
                "values": {"out_of_stock_minutes": 120, "monitored_minutes": 1440}}] if corrections else []})

    def test_conflict_material_recheck_accept_preserves_original(self):
        """完整闭环同时检查业务/运行状态分离、数据版本、证据来源与旧产物不变。"""
        before = self.path.read_bytes()
        original, case = self.conflict_case()
        old_evidence = self.service.artifact(original["id"], "evidence.json")
        old_report = self.service.artifact(original["id"], "report.md")
        case = self.material(case)
        result = self.service.reviews.recheck(case["id"], {"expected_revision": case["revision"]})
        new = self.wait(result["task"]["id"])
        case = self.service.reviews.get(case["id"])
        self.assertEqual(case["status"], "awaiting_acceptance")
        self.assertEqual(new["status"], "completed")
        self.assertNotEqual(new["fingerprint"], original["fingerprint"])
        self.assertEqual(new["parent_id"], original["id"])
        self.assertEqual(new["result"]["validation"]["status"], "passed")
        with self.assertRaises(ReviewConflict):
            self.service.reviews.accept(case["id"], {"expected_revision": case["revision"],
                "task_id": original["id"], "note": "不能用旧报告验收"})
        new_evidence = json.loads(self.service.artifact(new["id"], "evidence.json"))
        inventory = next(o for o in new_evidence["observations"] if o["data"]["table"] == "inventory")
        self.assertEqual(inventory["data"]["records"][-1]["out_of_stock_minutes"], 120)
        self.assertEqual(inventory["data"]["records"][-1]["verification_status"], "human_supplied_unverified")
        case = self.service.reviews.accept(case["id"], {"expected_revision": case["revision"],
            "task_id": new["id"], "note": "已核对修正来源和新报告；这不代表销量已恢复。"})
        self.assertEqual(case["status"], "resolved")
        self.assertEqual(len(case["runs"]), 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.artifact(original["id"], "evidence.json"), old_evidence)
        self.assertEqual(self.service.artifact(original["id"], "report.md"), old_report)
        self.assertTrue(self.service.artifact(new["id"], "validation.json"))

    def test_note_only_does_not_erase_conflict_or_enable_acceptance(self):
        """人工写“查过了”不会把原记录变成正确数据，复查后仍然需要处理冲突。"""
        _, case = self.conflict_case()
        case = self.material(case, corrections=False)
        result = self.service.reviews.recheck(case["id"], {"expected_revision": case["revision"]})
        self.wait(result["task"]["id"])
        case = self.service.reviews.get(case["id"])
        self.assertEqual(case["status"], "awaiting_human")
        with self.assertRaises(ReviewConflict):
            self.service.reviews.accept(case["id"], {"expected_revision": case["revision"], "task_id": result["task"]["id"], "note": "直接关闭"})

    def test_wrong_scope_and_bad_values_roll_back_material(self):
        """日期越界、额外 SKU、非法监测数值都不能通过补充材料改动调查对象。"""
        _, case = self.conflict_case()
        for correction in (
            {"table": "inventory", "date": "2026-10-24", "values": {"out_of_stock_minutes": 0, "monitored_minutes": 1440}},
            {"table": "inventory", "date": "2026-10-23", "values": {"sku_id": "OTHER", "out_of_stock_minutes": 0, "monitored_minutes": 1440}},
            {"table": "inventory", "date": "2026-10-23", "values": {"out_of_stock_minutes": 2000, "monitored_minutes": 1440}}):
            with self.assertRaises(ValueError):
                self.service.reviews.add_material(case["id"], {"expected_revision": case["revision"],
                    "note": "测试", "source": "测试表", "corrections": [correction]})
        current = self.service.reviews.get(case["id"])
        self.assertEqual(current["revision"], case["revision"])
        self.assertEqual(current["materials"], [])

    def test_stale_revision_and_concurrent_material_cannot_duplicate(self):
        """同一版本的两个提交只能成功一次，防止重复点击或并发覆盖材料。"""
        _, case = self.conflict_case()

        def submit():
            """真实线程争抢同一 SQLite 写事务，不模拟返回结果。"""
            try:
                self.material(case)
                return "ok"
            except ReviewConflict:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(len(self.service.reviews.get(case["id"])["materials"]), 1)

    def test_recheck_is_bounded_and_old_report_cannot_be_accepted(self):
        """连续未解决的复查有次数上限，且不能把旧报告当成最新结果验收。"""
        original, case = self.conflict_case()
        for _ in range(3):
            case = self.material(case, corrections=False)
            result = self.service.reviews.recheck(case["id"], {"expected_revision": case["revision"]})
            self.wait(result["task"]["id"])
            case = self.service.reviews.get(case["id"])
        case = self.material(case)
        with self.assertRaises(ReviewConflict):
            self.service.reviews.recheck(case["id"], {"expected_revision": case["revision"]})

    def test_missing_data_case_and_material_survive_restart(self):
        """资料缺失也可进入业务核验，材料与时间线跨服务重启保存。"""
        task = self.service.create({"question": "心动满怀2026-10-23库存情况"})["task"]
        self.assertEqual(task["status"], "needs_data")
        case = self.service.reviews.get(task["review_case"]["id"])
        case = self.material(case)
        self.service.close()
        self.service = InvestigationService(self.path, self.catalog_path, self.root / "state", self.root / "runs")
        persisted = self.service.reviews.get(case["id"])
        self.assertEqual(persisted["materials"], case["materials"])
        result = self.service.reviews.recheck(case["id"], {"expected_revision": persisted["revision"]})
        new = self.wait(result["task"]["id"])
        self.assertEqual(new["status"], "completed")

    def test_relative_date_anchor_stays_fixed_across_recheck_days(self):
        """几天后复查“昨天”的任务，必须仍调查原来的23日，不可重新解释为别的日期。"""
        with patch("ecommerce_agent.task_parser.shanghai_today", return_value=date(2026, 10, 24)):
            task = self.service.create({"question": "向阳而生昨天销量下降原因"})["task"]
        original = self.wait(task["id"])
        case = self.service.reviews.get(original["review_case"]["id"])
        case = self.material(case)
        with patch("ecommerce_agent.task_parser.shanghai_today", return_value=date(2026, 11, 1)):
            result = self.service.reviews.recheck(case["id"], {"expected_revision": case["revision"]})
        new = self.wait(result["task"]["id"])
        self.assertEqual(new["status"], "completed")
        self.assertEqual(new["parsed"]["target_date"], "2026-10-23")
        self.assertEqual(new["parsed"]["business_today"], "2026-10-24")
        self.assertEqual(new["question"], original["question"])

    def test_http_protects_case_reads_and_mutations(self):
        """工单 API 使用与任务相同的口令/跨站边界，非法状态映射为 409。"""
        _, case = self.conflict_case()
        server = create_server(self.service, port=0, token="t" * 32)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(path, payload=None, token=False):
            """只发送到测试随机端口，不访问真实 Tunnel 或模型。"""
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + "t" * 32
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        self.assertEqual(request("/api/reviews"), 401)
        self.assertEqual(request(f"/api/reviews/{case['id']}/materials", {}), 401)
        self.assertEqual(request("/api/reviews", token=True), 200)
        self.assertEqual(request(f"/api/reviews/{case['id']}/recheck", {"expected_revision": 0}, True), 400)
        self.assertEqual(request(f"/api/reviews/{case['id']}/recheck", {"expected_revision": case["revision"]}, True), 409)


class AssertionTests(unittest.TestCase):
    """将真实工具返回与伪造字段断言对照，验证引用存在不足以通过验收。"""

    def registry(self):
        """准备四类观察；每个测试实例互不共享查询状态。"""
        registry = ToolRegistry(DataStore.from_document(sample_data()), "FLR-ROSE-19-BOUQUET", "2026-10-23")
        for index, tool in enumerate(("get_sales_metrics", "get_traffic_metrics", "get_inventory_status", "get_promotion_history"), 1):
            registry.execute(tool, {}, f"source-{index}")
        registry.execute("analyze_metrics", {}, "analysis")
        return registry

    def test_existing_reference_wrong_value_is_rejected_then_can_correct(self):
        """引用确实存在，但把50说成500仍应拒绝 finish；纠正后可提交。"""
        registry = self.registry()
        args = {"summary": "目标日销量50，原因待核验", "evidence_ids": [f"source-{i}" for i in range(1, 5)],
                "assertions": [{"evidence_id": "source-1", "date": "2026-10-23", "field": "units_sold", "equals": 500}]}
        self.assertFalse(registry.execute("finish", args, "bad-finish")["ok"])
        self.assertIsNone(registry.finished)
        args["assertions"][0]["equals"] = 50
        self.assertTrue(registry.execute("finish", args, "good-finish")["ok"])
        self.assertEqual(registry.finished["verification"]["status"], "passed")

    def test_unknown_reference_date_field_and_boolean_are_not_numbers(self):
        """不允许猜测来源、错日引用、任意字段或将 True 当作 1。"""
        registry = self.registry()
        base = {"evidence_id": "source-1", "date": "2026-10-23", "field": "units_sold", "equals": 50}
        for patch in ({"evidence_id": "invented"}, {"date": "2026-10-24"}, {"field": "customer_phone"}, {"equals": True}):
            self.assertEqual(verify_assertions(registry.observations, [{**base, **patch}])["status"], "failed")
        self.assertEqual(verify_assertions(registry.observations, [base])["status"], "passed")
        self.assertEqual(verify_assertions(registry.observations, [])["status"], "references_only")


if __name__ == "__main__":
    unittest.main()
