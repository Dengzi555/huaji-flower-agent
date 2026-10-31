"""网页层回归：真实 HTTP、数据库事务、后台任务和重启后的状态，而非页面截图快照。"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ecommerce_agent.cli import ROOT
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import ConflictError, InvestigationService


class WebTests(unittest.TestCase):
    """所有读写落在临时目录，不改用户任务数据库或真实样例。"""

    def setUp(self):
        """准备独立数据/SQLite/HTTP 服务；使用真实执行器验证网页到报告的链路。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "products.json"
        self.catalog_path.write_text(json.dumps(sample_catalog()), encoding="utf-8")
        self.data = self.root / "data.json"
        self.data.write_text(json.dumps(sample_data()), encoding="utf-8")
        self.service = self.make_service()
        self.addCleanup(lambda: self.service.close())
        self.server = create_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.payload = {"question": "怦然心动2026-10-23销量下降原因"}

    def make_service(self, **kwargs):
        """同一数据目录可反复创建服务，模拟单进程服务正常重启。"""
        return InvestigationService(self.data, self.catalog_path, self.root / "state", self.root / "runs", **kwargs)

    def stop_http(self):
        """先停连接再关闭 socket，保证测试不残留后台监听。"""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, payload=None, headers=None, raw=None):
        """统一发真实 HTTP 请求，并保留错误状态码以验证边界。"""
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        supplied = {"Content-Type": "application/json", **(headers or {})}
        request = urllib.request.Request(self.base + path, data=body, headers=supplied)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def wait_task(self, task_id):
        """等待有明确上限，不把“提交成功”当成“任务完成”。"""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.service.get(task_id)
            if task["status"] not in ("queued", "running"):
                return task
            time.sleep(0.02)
        self.fail("Background task did not settle")

    def test_http_to_report_evidence_and_source_unchanged(self):
        """创建→后台执行→查询→下载完整走通，并检查输入没有被写回。"""
        before = self.data.read_bytes()
        status, raw, _ = self.request("/api/tasks", self.payload, {"Idempotency-Key": "request-1"})
        self.assertEqual(status, 201, raw)
        task_id = json.loads(raw)["task"]["id"]
        task = self.wait_task(task_id)
        self.assertEqual(task["status"], "completed", task)
        self.assertNotIn("run_dir", task["result"])
        status, report, headers = self.request(f"/api/tasks/{task_id}/artifacts/report.md")
        self.assertEqual(status, 200)
        self.assertIn("FLR-ROSE-19-BOUQUET", report.decode())
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(self.service.artifact(task_id, "evidence.json"))
        self.assertEqual(self.data.read_bytes(), before)

    def test_ambiguous_task_and_followup_link(self):
        """歧义任务不执行；补充规格建立关联新请求，保留原问题历史。"""
        original = self.service.create({"question": "红玫瑰花束2026-10-23销量下降原因"})["task"]
        self.assertEqual(original["status"], "needs_clarification")
        self.assertIsNone(original["result"])
        follow = self.service.create({"question": original["question"], "sku": "FLR-ROSE-19-BOUQUET", "parent_id": original["id"]})["task"]
        self.assertEqual(self.wait_task(follow["id"])["status"], "completed")
        self.assertEqual(self.service.get(follow["id"])["parent_id"], original["id"])
        self.assertEqual(self.service.get(original["id"])["status"], "needs_clarification")

    def test_idempotency_and_conflict(self):
        """相同幂等键/内容仅创建一次；键相同内容不同返回冲突。"""
        first = self.service.create(self.payload, "stable-key")
        second = self.service.create(self.payload, "stable-key")
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertTrue(second["reused"])
        with self.assertRaises(ConflictError):
            self.service.create({"question": "温柔时光2026-10-23销量下降原因"}, "stable-key")
        self.assertEqual(len(self.service.list_tasks()), 1)

    def test_completed_history_survives_restart(self):
        """服务对象销毁后，任务详情和产物仍能从 SQLite/磁盘找到。"""
        task_id = self.service.create(self.payload)["task"]["id"]
        self.wait_task(task_id)
        self.service.close()
        restarted = self.make_service()
        try:
            self.assertEqual(restarted.get(task_id)["status"], "completed")
            self.assertTrue(restarted.artifact(task_id, "report.md"))
        finally:
            restarted.close()

    def test_running_is_interrupted_and_queued_resumes_after_restart(self):
        """只重排未开始的任务；旧的在途执行不能伪装成恢复成功。"""
        self.service.close()
        self.service = self.make_service(start_worker=False)
        running = self.service.create(self.payload)["task"]["id"]
        queued = self.service.create(self.payload)["task"]["id"]
        with self.service.connection() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))
        self.service.close()
        self.service = self.make_service()
        self.assertEqual(self.service.get(running)["status"], "interrupted")
        self.assertEqual(self.wait_task(queued)["status"], "completed")

    def test_changed_dataset_resumes_with_pinned_snapshot(self):
        """新任务已有冻结快照：重启后仍使用原版本，不能静默采用当前文件的新数值。"""
        self.service.close()
        self.service = self.make_service(start_worker=False)
        task_id = self.service.create(self.payload)["task"]["id"]
        changed = sample_data()
        for row in changed["sales"]:
            row["units_sold"] = 999
        self.data.write_text(json.dumps(changed, indent=2), encoding="utf-8")
        self.service.close()
        self.service = self.make_service()
        settled = self.wait_task(task_id)
        self.assertEqual(settled["status"], "completed")
        self.assertEqual(settled["result"]["analysis"]["facts"]["units_sold"]["after"], 50)
        self.assertNotEqual(settled["fingerprint"], self.service.fingerprint)

    def test_failed_runner_does_not_kill_worker(self):
        """单次执行错误后，工作线程仍可接收下一条任务并输出明确失败状态。"""
        self.service.close()

        def broken(*args, **kwargs):
            """注入故障，不通过篡改真实业务数据模拟线程失败。"""
            raise ValueError("fixture failure")

        self.service = self.make_service(runner=broken)
        for _ in range(2):
            task_id = self.service.create(self.payload)["task"]["id"]
            self.assertEqual(self.wait_task(task_id)["status"], "failed")

    def test_http_validation_origin_and_paths(self):
        """拒绝跨站、任意路径、无效 JSON 和不受支持的输入参数。"""
        self.assertEqual(self.request("/api/tasks", self.payload, {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("/api/tasks", raw=b"bad-json")[0], 400)
        self.assertEqual(self.request("/api/tasks", {**self.payload, "data_file": "secret"})[0], 400)
        self.assertEqual(self.request("/api/tasks", self.payload, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/tasks", raw=b"x" * 16001)[0], 413)
        self.assertEqual(self.request("/../../config.local.json")[0], 404)
        self.assertIsNone(self.service.artifact("0" * 32, "../../secret"))

    def test_api_requires_token_when_configured(self):
        """配置口令后，静态页面可打开，但目录、任务和证据 API 必须带 Bearer。"""
        protected = create_server(self.service, port=0, token="test-token-long-enough-for-web")
        thread = threading.Thread(target=protected.serve_forever, daemon=True)
        thread.start()
        previous = self.base
        self.base = f"http://127.0.0.1:{protected.server_port}"
        try:
            self.assertEqual(self.request("/")[0], 200)
            self.assertEqual(self.request("/api/meta")[0], 401)
            self.assertEqual(self.request("/api/meta", headers={"Authorization": "Bearer test-token-long-enough-for-web"})[0], 200)
        finally:
            self.base = previous
            protected.shutdown()
            protected.server_close()
            thread.join(timeout=3)

    def test_public_binding_rejects_missing_token(self):
        """公网监听配置必须先设置口令，不允许默认无保护暴露业务数据。"""
        with self.assertRaises(ValueError):
            create_server(self.service, host="0.0.0.0", port=0)

    def test_tunnel_domain_and_authorization(self):
        """模拟反向代理转发公网 Host：合法域名仍需口令，伪造来源不能读取数据。"""
        with self.assertRaises(ValueError):
            create_server(self.service, port=0, public_hosts=("demo.trycloudflare.com",))
        protected = create_server(self.service, port=0, token="x" * 32,
                                  public_hosts=("demo.trycloudflare.com",))
        thread = threading.Thread(target=protected.serve_forever, daemon=True)
        thread.start()
        previous = self.base
        self.base = f"http://127.0.0.1:{protected.server_port}"
        headers = {"Host": "demo.trycloudflare.com", "Origin": "https://demo.trycloudflare.com"}
        try:
            self.assertEqual(self.request("/", headers=headers)[0], 200)
            self.assertEqual(self.request("/api/tasks", headers=headers)[0], 401)
            auth = {**headers, "Authorization": "Bearer " + "x" * 32}
            self.assertEqual(self.request("/api/tasks", headers=auth)[0], 200)
            self.assertEqual(self.request("/api/tasks", self.payload, auth)[0], 201)
            self.assertEqual(self.request("/api/tasks", headers={**auth, "Origin": "https://evil.invalid"})[0], 403)
            for path in ("/", "/healthz", "/app.js", "/api/tasks"):
                self.assertEqual(self.request(path, headers={**auth, "Host": "evil.invalid"})[0], 403)
        finally:
            self.base = previous
            protected.shutdown()
            protected.server_close()
            thread.join(timeout=3)

    def test_static_page_and_health(self):
        """资源可以正确加载，且页面不依赖第三方 CDN 或模型服务。"""
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertIn("花仙女", self.request("/")[1].decode())
        for path in ("/app.js", "/style.css"):
            self.assertEqual(self.request(path)[0], 200)

    def test_trace_page_and_run_api(self):
        """轨迹页只返回静态壳，运行数据由 API 从服务端产物读取；未知运行不泄露路径。"""
        run_id = "20261031T000000Z-abcdef12"
        folder = self.service.output / run_id
        folder.mkdir(parents=True)
        (folder / "run.json").write_text(json.dumps({"run_id": run_id, "status": "completed",
                                                     "sku_id": "SKU-A", "target_date": "2026-10-23"}), encoding="utf-8")
        (folder / "events.jsonl").write_text(json.dumps({"sequence": 1, "type": "run_started",
                                                         "at": "2026-10-31T04:00:00+00:00",
                                                         "mode": "deterministic_workflow"}) + "\n", encoding="utf-8")
        status, body, _ = self.request(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertIn(b"RUN TRACE", body)
        for path in ("/trace.js", "/trace.css"):
            self.assertEqual(self.request(path)[0], 200)
        status, body, _ = self.request(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["run_id"], run_id)
        self.assertEqual(self.request("/api/runs/20261031T000000Z-deadbeef")[0], 404)


if __name__ == "__main__":
    unittest.main()
