"""模型配置与网页边界测试：不使用真实密钥、不发付费请求。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from ecommerce_agent.local_model import LocalModelSettings
from ecommerce_agent.web import create_server
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.flower_business_data import business_catalog,business_data


class ModelSettingsTests(unittest.TestCase):
    """只允许本机配置官方服务；不把密钥回显到meta或任务快照。"""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.model=LocalModelSettings(self.root)

    def test_secret_is_write_only_and_empty_preserves(self):
        key='sk-'+'testing-secret-1234567890'
        result=self.model.save({'api_key':key,'enabled':True,'nickname':'学习者'})
        self.assertNotIn(key,json.dumps(result));self.assertTrue(result['configured'])
        self.model.save({'api_key':'','model':'deepseek-v4-pro'})
        with patch.dict('os.environ',{},clear=True):self.assertEqual(self.model.key(),key)
        self.assertNotIn(key,(self.root/'config.local.json').read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):self.model.save({'base_url':'https://other.test'})
        with self.assertRaises(ValueError):self.model.save({'api_key':'bad\nvalue'})

    def test_local_api_guards_and_task_snapshot(self):
        data=self.root/'data.json';catalog=self.root/'products.json'
        data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
        service=InvestigationService(data,catalog,self.root/'state',self.root/'runs',start_worker=False);self.addCleanup(service.close)
        self.model.save({'api_key':'sk-testing-only-123456789','enabled':True})
        service.model_settings=self.model
        server=create_server(service,port=0,token='t'*30,public_hosts=('flowers.test',),model_settings=self.model)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),worker.join()))
        url=f'http://127.0.0.1:{server.server_port}'
        def status(headers):
            req=urllib.request.Request(url+'/api/model-settings',headers=headers)
            try:
                with urllib.request.urlopen(req) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as e:return e.code,{}
        self.assertEqual(status({})[0],401)
        code,result=status({'Authorization':'Bearer '+'t'*30});self.assertEqual(code,200);self.assertNotIn('api_key',result)
        self.assertEqual(status({'Authorization':'Bearer '+'t'*30,'Host':'flowers.test'})[0],403)
        self.assertEqual(status({'Authorization':'Bearer '+'t'*30,'Origin':'https://other.test'})[0],403)
        task=service.create({'question':'99红玫2026-10-25销量下降原因'})['task']
        self.assertEqual(task['parsed']['model_execution']['provider'],'DeepSeek')
        self.assertNotIn('sk-testing',json.dumps(task))
        pending=service.create({'question':'99红梅2026-10-25销量下降原因'})['task']
        self.assertNotIn('model_execution',pending['parsed'])
        lookup=service.create({'question':'99红玫2026-10-25销量怎么样'})['task']
        self.assertNotIn('model_execution',lookup['parsed'])
