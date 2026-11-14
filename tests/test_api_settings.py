"""统一 API 设置回归：凭据保护、并发保存、兼容端点和本机 HTTP 权限。"""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ecommerce_agent.api_settings import VisionModelSettings, read_environment
from ecommerce_agent.fixtures import sample_catalog, sample_data
from ecommerce_agent.local_model import LocalModelSettings
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService


class APISettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        environment=patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                        if not key.startswith("VISION_") and key != "DEEPSEEK_API_KEY"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.text=LocalModelSettings(self.root)
        self.vision=VisionModelSettings(self.root)

    def test_vision_write_only_and_blank_key_preserves(self):
        key="sk-ws-test.key-with-dots-123456789"
        result=self.vision.save({"base_url":"https://vision.example/v1","model":"vision-test","api_key":key})
        self.assertTrue(result["configured"])
        self.assertNotIn(key,json.dumps(result))
        self.vision.save({"model":"vision-other","api_key":""})
        self.assertEqual(read_environment(self.root,("VISION_API_KEY",))["VISION_API_KEY"],key)
        self.assertNotIn("api_key",self.vision.public())

    def test_both_model_saves_preserve_other_environment_variables(self):
        (self.root/".env").write_text("EXISTING=value\n",encoding="utf-8")
        self.text.save({"api_key":"sk-text-test-123456789","base_url":"https://text.example/v1","model":"custom-text"})
        self.vision.save({"api_key":"sk-vision-test-123456789","base_url":"https://vision.example/v1","model":"custom-vision"})
        self.text.save({"api_key":"sk-ws-another.text-key-123456789"})
        values=read_environment(self.root,("EXISTING","VISION_API_KEY","DEEPSEEK_API_KEY"))
        self.assertEqual(values["EXISTING"],"value")
        self.assertEqual(values["VISION_API_KEY"],"sk-vision-test-123456789")
        self.assertEqual(values["DEEPSEEK_API_KEY"],"sk-ws-another.text-key-123456789")

    def test_simultaneous_saves_preserve_both_keys(self):
        barrier=threading.Barrier(2)
        errors=[]
        def save(settings,payload):
            try:
                barrier.wait(timeout=3)
                settings.save(payload)
            except Exception as error:
                errors.append(error)
        threads=[
            threading.Thread(target=save,args=(self.text,{"api_key":"sk-text-test-123456789"})),
            threading.Thread(target=save,args=(self.vision,{"api_key":"sk-vision-test-123456789","base_url":"https://vision.example/v1","model":"vl-test"})),
        ]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertFalse(errors)
        values=read_environment(self.root,("VISION_API_KEY","DEEPSEEK_API_KEY"))
        self.assertEqual(len(values),2)

    def test_text_provider_uses_configured_url_and_model(self):
        self.text.save({"base_url":"https://models.example/v1","model":"my-model","api_key":"sk-ws-key.with-dots-123456789"})
        with patch("ecommerce_agent.local_model.ChatProvider") as provider:
            self.text.provider()
            config=provider.call_args.args[0]
            self.assertEqual(config["api_base_url"],"https://models.example/v1")
            self.assertEqual(config["model"],"my-model")
            self.assertFalse(config["thinking_disabled"])

    def test_classification_uses_selected_model(self):
        self.text.save({"model":"custom-model"})
        with patch.object(self.text,"provider") as provider:
            provider.return_value.complete.return_value={"tool_calls":[{"function":{"name":"classify_intent","arguments":'{"decision":"clarify"}'}}]}
            self.text.classify("查一下销量")
            provider.assert_called_once_with()

    def test_invalid_configuration_does_not_write(self):
        for payload in (
            {"base_url":"http://untrusted.example/v1","model":"vl"},
            {"base_url":"https://vision.example/v1/chat/completions","model":"vl"},
            {"base_url":"https://vision.example/v1","model":"vl","api_key":"bad\nkey"},
            {"base_url":"https://vision.example/v1","model":"vl","timeout_seconds":0},
        ):
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                self.vision.save(payload)
        self.assertFalse((self.root/".env").exists())

    def test_environment_override_is_visible_without_secret(self):
        self.vision.save({"base_url":"https://vision.example/v1","model":"vl","api_key":"sk-local-123456789"})
        with patch.dict(os.environ,{"VISION_API_KEY":"sk-environment-123456789","VISION_MODEL":"env-model"}):
            result=self.vision.public()
        self.assertEqual(result["model"],"env-model")
        self.assertIn("VISION_API_KEY",result["environment_overrides"])
        self.assertNotIn("sk-environment",json.dumps(result))

    def test_vision_probe_uses_small_test_image(self):
        self.vision.save({"base_url":"https://vision.example/v1","model":"vl","api_key":"sk-local-123456789"})
        with patch("ecommerce_agent.vision_client.VisionClient.identify",return_value="白色") as identify:
            result=self.vision.test()
        self.assertTrue(result["vision"])
        self.assertEqual(identify.call_args.args[1],"image/png")
        self.assertTrue(identify.call_args.args[0].startswith(b"\x89PNG"))

    def test_http_vision_settings_and_local_access(self):
        data=self.root/"data.json";catalog=self.root/"products.json"
        data.write_text(json.dumps(sample_data()),encoding="utf-8")
        catalog.write_text(json.dumps(sample_catalog()),encoding="utf-8")
        service=InvestigationService(data,catalog,self.root/"state",self.root/"runs",start_worker=False)
        self.addCleanup(service.close)
        server=create_server(service,port=0,token="t"*30,public_hosts=("flowers.test",),model_settings=self.text)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            def request(path,payload=None,**headers):
                req=Request(f"http://127.0.0.1:{server.server_port}"+path,
                            data=json.dumps(payload).encode() if payload is not None else None,
                            headers={"Authorization":"Bearer "+"t"*30,"Content-Type":"application/json",**headers})
                try:
                    with urlopen(req,timeout=5) as response:return response.status,json.load(response)
                except HTTPError as error:return error.code,json.load(error)
            status,result=request("/api/vision-settings",{"base_url":"https://vision.example/v1","model":"vl","api_key":"sk-test-writeonly-123456789"})
            self.assertEqual(status,200,result)
            self.assertNotIn("sk-test-writeonly",json.dumps(request("/api/vision-settings")[1]))
            self.assertEqual(request("/api/vision-settings",Authorization="")[0],401)
            self.assertEqual(request("/api/vision-settings",Host="flowers.test")[0],403)
            self.assertEqual(request("/api/vision-settings",Origin="https://other.example")[0],403)
            self.assertEqual(request("/api/vision-test",{"unexpected":True})[0],400)
            with patch.object(self.text.vision,"test",return_value={"ok":True,"model":"vl","vision":True}):
                self.assertEqual(request("/api/vision-test",{})[0],200)
        finally:
            server.shutdown();server.server_close();worker.join(timeout=3)
