"""商品识图回归：真实 HTTP 上传、兼容模型服务、字段归一化与错误边界。"""
import base64
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.product_identification import FIELDS, compose_product_title, naming_context, parse_fields
from ecommerce_agent.vision_client import VisionClient, VisionError, vision_environment
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jvJsAAAAASUVORK5CYII=")
COMPLETE = dict(zip(FIELDS, ("浪漫满屋", "11朵", "红玫瑰", "满天星", "花束", "生日礼物", "送女友")))

def multipart(image=PNG, store="local", holiday=""):
    """模拟浏览器表单，保留图片原始字节。"""
    boundary = "flower-vision-test"
    segments = [
        b'--flower-vision-test\r\nContent-Disposition: form-data; name="image"; filename="flower.png"\r\nContent-Type: image/png\r\n\r\n' + image,
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="store_id"\r\n\r\n' + store).encode(),
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="holiday"\r\n\r\n' + holiday).encode(),
        ('--' + boundary + '--\r\n').encode(),
    ]
    return b"\r\n".join(segments), "multipart/form-data; boundary=" + boundary


class ProductFieldTests(unittest.TestCase):
    """验证模型字段修复及门店、本机配置。"""
    def test_fenced_json(self):
        text = chr(96) * 3 + "json\n" + json.dumps(COMPLETE) + "\n" + chr(96) * 3
        self.assertEqual(parse_fields(text), (COMPLETE, []))

    def test_missing_and_wrong_types_are_empty_strings(self):
        fields, warnings = parse_fields('{"prefix":"浪漫满屋","spec":11,"mixed_material":null,"keyword":["生日"]}')
        self.assertEqual(set(fields), set(FIELDS))
        self.assertTrue(all(isinstance(value, str) for value in fields.values()))
        self.assertEqual(fields["spec"], "")
        self.assertIn("main_material", warnings)

    def test_invalid_json_and_non_object(self):
        for text in ("bad json", "[]", "null"):
            with self.subTest(text=text), self.assertRaises(VisionError):
                parse_fields(text)

    def test_invalid_prefix_and_uncertain_spec_are_cleared(self):
        fields, warnings = parse_fields(json.dumps({**COMPLETE, "prefix": "漂亮", "spec": "大概11朵"}))
        self.assertEqual(fields["prefix"], "")
        self.assertEqual(fields["spec"], "")
        self.assertEqual(set(warnings), {"prefix", "spec"})

    def test_holiday_suffix_removed(self):
        fields, _ = parse_fields(json.dumps({**COMPLETE, "keyword": "节日礼物 情人节限定"}))
        self.assertEqual(fields["keyword"], "节日礼物")

    def test_store_defaults_override_and_explicit_no_holiday(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/product_naming.json").write_text(json.dumps({
                "default": {"style": "温柔浪漫", "holiday": "母亲节"},
                "stores": {"a": {"style": "自然清新"}, "b": {"style": "都市浪漫"}},
            }), encoding="utf-8")
            self.assertEqual(naming_context(root, "a")["holiday"], "母亲节")
            self.assertEqual(naming_context(root, "a", "")["holiday"], "")
            self.assertEqual(naming_context(root, "b", "情人节")["style"], "都市浪漫")

    def test_dotenv_priority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"VISION_MODEL": "environment-model", "VISION_API_KEY": "environment-key"}):
            root = Path(directory)
            (root / ".env").write_text('VISION_MODEL=file-model\nVISION_API_KEY="file-key"\nOTHER_SECRET=ignored\n', encoding="utf-8")
            config = vision_environment(root)
            self.assertEqual(config["VISION_MODEL"], "environment-model")
            self.assertEqual(config["VISION_API_KEY"], "environment-key")
            self.assertNotIn("OTHER_SECRET", config)

    def test_title_matches_store_format(self):
        fields = {**COMPLETE, "prefix": "静待花开", "spec": "52朵", "main_material": "爱莎玫瑰",
                  "mixed_material": "满天星", "keyword": "生日花束生日礼物生日鲜花",
                  "target_audience": "送女友送爱人送闺蜜"}
        self.assertEqual(compose_product_title(fields, {}),
                         "【静待花开】52朵爱莎玫瑰花束生日花束生日礼物生日鲜花送女友送爱人送闺蜜花店送花上门")

    def test_title_audience_format_and_delivery_configuration(self):
        fields = {**COMPLETE, "keyword": "生日花束、生日礼物", "target_audience": "女友、爱人"}
        title = compose_product_title(fields, {"delivery_suffix": "鲜花免费配送", "holiday": "情人节"})
        self.assertEqual(title, "【浪漫满屋】11朵红玫瑰花束生日花束生日礼物情人节限定送女友送爱人鲜花免费配送")
        self.assertEqual(compose_product_title({key: "" for key in FIELDS}, {}), "")

    def test_missing_configuration(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"VISION_API_KEY": "", "VISION_MODEL": "", "VISION_BASE_URL": ""}):
            with self.assertRaises(VisionError) as result:
                VisionClient(root)
            self.assertEqual(result.exception.status, 503)


class ProductIdentifyHTTPTests(unittest.TestCase):
    """应用到本机 OpenAI 兼容服务的端到端请求，不调用外部模型。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "data.json").write_text(json.dumps(sample_data()), encoding="utf-8")
        (root / "products.json").write_text(json.dumps(sample_catalog()), encoding="utf-8")
        self.service = InvestigationService(root / "data.json", root / "products.json", root / "state", root / "runs", start_worker=False)
        self.addCleanup(self.service.close)
        self.model_status = 200
        self.model_text = json.dumps(COMPLETE)
        self.requests = []
        owner = self

        class ModelHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                owner.requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"choices": [{"message": {"content": owner.model_text}}]}).encode()
                self.send_response(owner.model_status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.model_thread = threading.Thread(target=self.model.serve_forever, daemon=True)
        self.model_thread.start()
        self.addCleanup(self.stop_server, self.model, self.model_thread)
        env = patch.dict(os.environ, {"VISION_API_KEY": "local-test-key", "VISION_MODEL": "test-vision",
                                     "VISION_BASE_URL": f"http://127.0.0.1:{self.model.server_port}/v1",
                                     "VISION_TIMEOUT_SECONDS": "5"})
        env.start()
        self.addCleanup(env.stop)
        self.server = create_server(self.service, port=0, token="test-workspace-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server, self.server, self.thread)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def request(self, image=PNG, store="local", holiday="", **headers):
        body, content_type = multipart(image, store, holiday)
        request = Request(self.base + "/api/product/identify", data=body, method="POST",
                          headers={"Content-Type": content_type, "Authorization": "Bearer test-workspace-token", **headers})
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_upload_to_compatible_model(self):
        self.model_text = chr(96) * 3 + "json\n" + json.dumps(COMPLETE) + "\n" + chr(96) * 3
        status, result = self.request(holiday="情人节")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["fields"], COMPLETE)
        path, payload = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["model"], "test-vision")
        self.assertIn("情人节", payload["messages"][0]["content"])
        part = payload["messages"][1]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertEqual(base64.b64decode(part["image_url"]["url"].split(",")[1]), PNG)

    def test_text_rewrite_reuses_compatible_transport(self):
        text = VisionClient(self.temp.name).rewrite({"name": "99朵红玫瑰花束"}, "命名规则")
        self.assertEqual(json.loads(text), COMPLETE)
        path, payload = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["messages"][0]["content"], "命名规则")
        self.assertEqual(json.loads(payload["messages"][1]["content"])["name"], "99朵红玫瑰花束")
        self.assertIsInstance(payload["messages"][1]["content"], str)

    def test_partial_model_result(self):
        self.model_text = '{"main_material":"红玫瑰","spec":11}'
        status, result = self.request()
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["fields"]), 7)
        self.assertTrue(all(isinstance(value, str) for value in result["fields"].values()))
        self.assertEqual(result["fields"]["spec"], "")

    def test_invalid_json(self):
        self.model_text = "non-json"
        status, result = self.request()
        self.assertEqual(status, 502)
        self.assertIn("JSON", result["error"])

    def test_missing_key(self):
        with patch.dict(os.environ, {"VISION_API_KEY": ""}):
            status, result = self.request()
        self.assertEqual(status, 503)
        self.assertIn("VISION_API_KEY", result["error"])
        self.assertEqual(self.requests, [])

    def test_provider_auth_error(self):
        self.model_status = 401
        status, result = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn("local-test-key", json.dumps(result))

    def test_invalid_image(self):
        self.assertEqual(self.request(image=b"not an image")[0], 400)
        self.assertEqual(self.requests, [])

    def test_store_mismatch_and_unknown(self):
        self.assertEqual(self.request(store="not-a-store")[0], 400)
        self.assertEqual(self.request(**{"X-Store-ID": "other"})[0], 400)
        self.assertEqual(self.requests, [])

    def test_auth_and_origin(self):
        self.assertEqual(self.request(Authorization="")[0], 401)
        self.assertEqual(self.request(Origin="https://other.example")[0], 403)
        self.assertEqual(self.requests, [])

    def test_bad_holiday(self):
        self.assertEqual(self.request(holiday="invalid")[0], 400)
        self.assertEqual(self.requests, [])

    def test_large_upload(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/product/identify")
        connection.putheader("Authorization", "Bearer test-workspace-token")
        connection.putheader("Content-Type", "multipart/form-data; boundary=test")
        connection.putheader("Content-Length", str(11 * 1024 * 1024))
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)
        self.assertEqual(self.requests, [])
