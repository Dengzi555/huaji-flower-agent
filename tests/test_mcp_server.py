"""MCP stdio Server：协议形状、只读工具面、参数校验与目录检索。

测试直接驱动 handle()，不启动子进程；stdio 逐行读写只做 CLI 冒烟。
"""
import json
import unittest

from ecommerce_agent.cli import ROOT
from ecommerce_agent.data import DataStore
from ecommerce_agent.mcp_server import McpServer
from ecommerce_agent.task_parser import ProductCatalog
from ecommerce_agent.tools import TABLES


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.store = DataStore(ROOT / 'data/sample.json')
        self.catalog = ProductCatalog(ROOT / 'data/products.json', self.store.skus)
        self.server = McpServer(self.store, self.catalog)
        self.sku, self.day = next((sku, day) for domain, sku, day in self.store.records if domain == 'sales')

    def rpc(self, method, params=None, identity=1):
        return self.server.handle({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params or {}})

    def call(self, name, arguments):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments})['result']

    def payload(self, response):
        return json.loads(response['content'][0]['text'])

    def test_initialize_negotiates_supported_version(self):
        """客户端给新版协议时回同版本；未知版本回退到稳定版本，不直接断开。"""
        response = self.rpc('initialize', {'protocolVersion': '2025-06-18',
                                           'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(response['result']['protocolVersion'], '2025-06-18')
        self.assertFalse(response['result']['capabilities']['tools']['listChanged'])
        fallback = self.rpc('initialize', {'protocolVersion': '1999-01-01'})
        self.assertEqual(fallback['result']['protocolVersion'], '2024-11-05')

    def test_tools_list_is_read_only_and_closed(self):
        """工具面只有四个查询 + 目录检索；不能出现分析器、finish 或写操作。"""
        names = [tool['name'] for tool in self.rpc('tools/list')['result']['tools']]
        self.assertEqual(set(names), set(TABLES) | {'find_products'})
        self.assertEqual(len(names), 5)
        self.assertNotIn('finish', names)
        self.assertNotIn('analyze_metrics', names)
        for tool in self.rpc('tools/list')['result']['tools']:
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            self.assertTrue(tool['description'])

    def test_find_products_returns_identity_only(self):
        """目录检索只返回 sku_id/名称/别名，不夹带经营记录。"""
        product = next(iter(self.catalog.products.values()))
        result = self.payload(self.call('find_products', {'query': product['name'][:2]}))
        self.assertTrue(result['ok'])
        self.assertTrue(result['matches'])
        self.assertEqual(set(result['matches'][0]), {'sku_id', 'name', 'variant', 'aliases'})
        self.assertNotIn('records', result['matches'][0])

    def test_data_tool_reuses_registry_observation(self):
        """查询返回与网页调查相同的观察结构：ok、evidence_id、两条基线记录。"""
        result = self.payload(self.call('get_sales_metrics', {'sku_id': self.sku, 'date': self.day}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['tool'], 'get_sales_metrics')
        self.assertTrue(result['evidence_id'])
        self.assertEqual(len(result['data']['records']), 2)
        self.assertEqual(result['data']['target_date'], self.day)

    def test_unknown_sku_and_future_date_are_correctable_errors(self):
        """未知对象与未来日期返回 isError，而不是让客户端进程崩溃。"""
        unknown = self.call('get_traffic_metrics', {'sku_id': 'NO-SUCH-SKU', 'date': self.day})
        self.assertTrue(unknown['isError'])
        self.assertIn('find_products', self.payload(unknown)['error'])
        future = self.call('get_inventory_status', {'sku_id': self.sku, 'date': '2999-01-01'})
        self.assertTrue(future['isError'])
        self.assertIn('beyond', self.payload(future)['error'])

    def test_extra_arguments_and_bad_date_are_rejected(self):
        """额外参数不能作为切换 SKU 或注入字段的通道。"""
        extra = self.call('get_promotion_history', {'sku_id': self.sku, 'date': self.day, 'store': 'x'})
        self.assertTrue(extra['isError'])
        self.assertIn('Unexpected', self.payload(extra)['error'])
        bad_date = self.call('get_sales_metrics', {'sku_id': self.sku, 'date': '10-23'})
        self.assertTrue(bad_date['isError'])
        self.assertIn('Invalid date', self.payload(bad_date)['error'])

    def test_unknown_tool_and_method_return_jsonrpc_errors(self):
        """未知工具走协议错误；通知没有响应；批量请求逐条返回。"""
        tool = self.rpc('tools/call', {'name': 'delete_sku', 'arguments': {}})
        self.assertEqual(tool['error']['code'], -32602)
        self.assertEqual(self.rpc('resources/watch')['error']['code'], -32601)
        self.assertIsNone(self.server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        batch = self.server.handle([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ])
        self.assertEqual([item['id'] for item in batch], [1, 2])


if __name__ == '__main__': unittest.main()