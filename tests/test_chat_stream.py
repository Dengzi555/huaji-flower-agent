"""真实增量协议回归：不能把整段文本切片冒充流式，也不能误分流选品建议。"""
import json
import unittest
from unittest.mock import patch,MagicMock
from pathlib import Path
from datetime import date
from ecommerce_agent.provider import ChatProvider
from ecommerce_agent.assistant_chat import is_general_chat
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog

class ChatStreamTests(unittest.TestCase):
    def test_provider_yields_each_upstream_delta_and_rejects_truncation(self):
        client=ChatProvider({'api_base_url':'http://127.0.0.1:1','model':'fixture'})
        lines=[('data: '+json.dumps({'choices':[{'delta':{'content':text}}]})+'\n').encode() for text in ['你好','，花迹']]
        response=MagicMock();response.__enter__.return_value=iter(lines+[b'data: [DONE]\n'])
        chunks=[]
        with patch('urllib.request.urlopen',return_value=response):result=client.stream_text([],chunks.append)
        self.assertEqual(chunks,['你好','，花迹']);self.assertEqual(result['content'],'你好，花迹')
        response.__enter__.return_value=iter(lines)
        with patch('urllib.request.urlopen',return_value=response),self.assertRaises(ValueError):client.stream_text([],lambda text:None)

    def test_screenshot_advice_does_not_require_investigation_choice(self):
        root=Path(__file__).resolve().parents[1];store=DataStore(root/'data/stores/hx-cq-na.json')
        catalog=ProductCatalog(root/'data/products.json',store.skus)
        self.assertTrue(is_general_chat('万圣节你觉得什么品种的花束卖得好？',catalog,date(2026,10,29),True))
        self.assertFalse(is_general_chat('万圣节实际销量哪款最好',catalog,date(2026,10,29),True))
