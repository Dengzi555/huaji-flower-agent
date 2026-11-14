"""商品巡检闭环回归：真实 SQLite、重启、并发、事实约束与 HTTP 更新。"""
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.product_maintenance import ProductNotFound, inspect_product
from ecommerce_agent.task_repository import ConflictError
from ecommerce_agent.vision_client import VisionError
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService

SKU = "FLR-ROSE-99-BOUQUET"
FIELDS = {"prefix": "浪漫满屋", "spec": "99朵", "main_material": "红玫瑰",
          "mixed_material": "", "product_type": "花束", "keyword": "生日礼物", "target_audience": "送女友"}


class ProductMaintenanceTests(unittest.TestCase):
    """各测试使用独立数据库和目录，不改真实商品数据。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data.json"
        self.catalog = self.root / "products.json"
        self.data.write_text(json.dumps(sample_data()), encoding="utf-8")
        catalog = sample_catalog()
        catalog["products"][0]["name"] = "99朵红玫瑰花束"
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        self.service = self.make_service()
        self.addCleanup(lambda: self.service.close())

    def make_service(self):
        return InvestigationService(self.data, self.catalog, self.root / "state", self.root / "runs", start_worker=False)

    def fix(self, fields=None, **payload):
        with patch("ecommerce_agent.product_maintenance.VisionClient") as model:
            model.return_value.rewrite.return_value = json.dumps(FIELDS if fields is None else fields)
            result = self.service.product_maintenance.auto_fix({"product_id": SKU, **payload}, self.root, "local")
        return result

    def test_audit_missing_rules_and_all_products(self):
        result = self.service.product_maintenance.audit()
        self.assertEqual(result["scanned"], 5)
        item = next(row for row in result["products"] if row["product_id"] == SKU)
        self.assertEqual(set(item["issues"]), {"缺少四字前缀", "缺少用途关键词", "缺少结构化打标", "缺少商品标签", "缺少配送尾缀", "缺少送礼引导词"})

    def test_fix_persists_and_updates_normal_catalog(self):
        old_file = self.catalog.read_bytes()
        old_fingerprint = self.service.fingerprint
        result = self.fix(expected_revision=0)
        product = self.service.catalog.products[SKU]
        self.assertEqual(result["product"]["status"], "fixed")
        self.assertEqual(product["name"], "【浪漫满屋】99朵红玫瑰花束生日礼物送女友花店送花上门")
        self.assertEqual(inspect_product(product), [])
        self.assertEqual(product["original_title"], "99朵红玫瑰花束")
        self.assertIn("炽热爱意", product["aliases"])
        self.assertEqual(product["stem_count"], 99)
        self.assertNotEqual(old_fingerprint, self.service.fingerprint)
        self.assertEqual(self.service.meta()["fingerprint"], self.service.dataset()[2])
        self.assertNotIn(SKU, [row["product_id"] for row in self.service.product_maintenance.audit()["products"]])
        self.assertEqual(self.catalog.read_bytes(), old_file)
        with self.service.connection() as db:
            row = db.execute("SELECT document FROM product_catalog_edits WHERE sku_id=?", (SKU,)).fetchone()
            self.assertEqual(json.loads(row[0])["name"], product["name"])
            self.assertEqual(db.execute("SELECT count(*) FROM product_edit_history").fetchone()[0], 1)

    def test_restart_keeps_corrected_catalog_and_fingerprint(self):
        self.fix()
        title, fingerprint = self.service.catalog.products[SKU]["name"], self.service.fingerprint
        self.service.close()
        self.service = self.make_service()
        self.assertEqual(self.service.catalog.products[SKU]["name"], title)
        self.assertEqual(self.service.fingerprint, fingerprint)
        self.assertNotIn(SKU, [row["product_id"] for row in self.service.product_maintenance.audit()["products"]])

    def test_repeat_fix_does_not_call_model_or_duplicate_history(self):
        self.fix()
        with patch("ecommerce_agent.product_maintenance.VisionClient") as model:
            result = self.service.product_maintenance.auto_fix({"product_id": SKU}, self.root, "local")
            model.assert_not_called()
        self.assertTrue(result["reused"])
        with self.service.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM product_edit_history").fetchone()[0], 1)

    def test_rare_material_is_manual_review_without_write(self):
        before = self.service.catalog.products[SKU]["name"]
        with self.assertLogs("ecommerce_agent.product_maintenance", level="WARNING"):
            result = self.fix({**FIELDS, "main_material": ""})
        self.assertEqual(result["product"]["status"], "needs_review")
        self.assertEqual(self.service.catalog.products[SKU]["name"], before)
        with self.service.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM product_catalog_edits").fetchone()[0], 0)

    def test_hallucinated_material_spec_or_type_cannot_be_saved(self):
        for key, value in (("main_material", "洋桔梗"), ("spec", "11朵"), ("product_type", "盆栽"), ("mixed_material", "罕见花材")):
            with self.subTest(key=key):
                result = self.fix({**FIELDS, key: value})
                self.assertEqual(result["product"]["status"], "needs_review")
                self.assertEqual(self.service.catalog.products[SKU]["name"], "99朵红玫瑰花束")

    def test_malformed_model_response_does_not_write(self):
        with patch("ecommerce_agent.product_maintenance.VisionClient") as model:
            model.return_value.rewrite.return_value = "bad json"
            with self.assertRaises(VisionError):
                self.service.product_maintenance.auto_fix({"product_id": SKU}, self.root, "local")
        self.assertEqual(self.service.catalog.products[SKU]["name"], "99朵红玫瑰花束")

    def test_database_failure_rolls_back_before_catalog_publish(self):
        fingerprint = self.service.fingerprint
        with self.service.connection() as db:
            db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON product_edit_history BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.fix()
        self.assertEqual(self.service.fingerprint, fingerprint)
        self.assertEqual(self.service.catalog.products[SKU]["name"], "99朵红玫瑰花束")
        with self.service.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM product_catalog_edits").fetchone()[0], 0)

    def test_unknown_product_and_stale_revision(self):
        with self.assertRaises(ProductNotFound):
            self.service.product_maintenance.auto_fix({"product_id": "UNKNOWN"}, self.root, "local")
        with self.assertRaises(ConflictError):
            self.service.product_maintenance.auto_fix({"product_id": SKU, "expected_revision": 4}, self.root, "local")

    def test_concurrent_fix_has_one_commit(self):
        barrier = threading.Barrier(2)
        results = []
        def rewrite(*_):
            barrier.wait(timeout=5)
            return json.dumps(FIELDS)
        def fix():
            try:
                results.append(self.service.product_maintenance.auto_fix({"product_id": SKU}, self.root, "local")["product"]["status"])
            except ConflictError:
                results.append("conflict")
        with patch("ecommerce_agent.product_maintenance.VisionClient") as model:
            model.return_value.rewrite.side_effect = rewrite
            threads = [threading.Thread(target=fix) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["fixed", "conflict"])
        with self.service.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM product_edit_history").fetchone()[0], 1)

    def test_new_data_version_does_not_reapply_old_edits(self):
        self.fix()
        # 模拟导入完成后发布的新版本，复用导入路径的目录恢复入口。
        from ecommerce_agent.task_parser import ProductCatalog
        with self.service.data_lock:
            self.service.catalog = ProductCatalog(self.catalog, self.service.store.skus)
            self.service.fingerprint = "new-import-version"
            self.service._dataset = (self.service.store, self.service.catalog, self.service.fingerprint)
            self.service.product_maintenance.restore()
        self.assertEqual(self.service.catalog.products[SKU]["name"], "99朵红玫瑰花束")
        self.assertEqual(self.service.fingerprint, "new-import-version")

    def test_other_store_database_is_isolated(self):
        other = InvestigationService(self.data, self.catalog, self.root / "other-state", self.root / "other-runs", start_worker=False)
        self.addCleanup(other.close)
        self.fix()
        self.assertEqual(other.catalog.products[SKU]["name"], "99朵红玫瑰花束")

    def test_http_scan_fix_rescan_and_errors(self):
        server = create_server(self.service, port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            def request(path, payload=None, token="test-token"):
                data = json.dumps(payload).encode() if payload is not None else None
                request = Request(base + path, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
                try:
                    with urlopen(request, timeout=5) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            self.assertEqual(request("/api/product/audit", token="")[0], 401)
            status, result = request("/api/product/audit")
            self.assertEqual(status, 200)
            self.assertIn(SKU, [row["product_id"] for row in result["products"]])
            with patch("ecommerce_agent.product_maintenance.VisionClient") as model:
                model.return_value.rewrite.return_value = json.dumps(FIELDS)
                status, result = request("/api/product/auto_fix", {"product_id": SKU, "expected_revision": 0})
            self.assertEqual(status, 200, result)
            self.assertEqual(result["product"]["status"], "fixed")
            self.assertNotIn(SKU, [row["product_id"] for row in request("/api/product/audit", {})[1]["products"]])
            self.assertEqual(request("/api/product/auto_fix", {"product_id": SKU, "expected_revision": 0})[0], 409)
            self.assertEqual(request("/api/product/auto_fix", {"product_id": "OTHER"})[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
