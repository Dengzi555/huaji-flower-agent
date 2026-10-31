"""MCP stdio Server：把四个只读业务工具暴露给 Claude Desktop / Cursor。

协议实现保持零依赖：JSON-RPC 2.0，按行分隔；stdout 只写协议响应，诊断信息写
stderr，避免污染客户端的消息流。工具调用复用 ToolRegistry，字段口径与证据 ID
和网页调查一致；只暴露查询工具与一个目录检索工具，不暴露分析器、finish 或写操作。

调用示例（Claude Desktop / Cursor 配置）：
    {"mcpServers": {"huaji": {"command": "python",
      "args": ["-m", "ecommerce_agent", "mcp"], "cwd": "<项目目录>"}}}
"""
import json
import sys
import uuid
from datetime import date
from pathlib import Path

from .data import DataStore
from .task_parser import ProductCatalog
from .tools import TABLES, ToolRegistry

PROTOCOL_VERSION = '2024-11-05'
SUPPORTED_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18'}
SERVER_INFO = {'name': 'huaji-readonly', 'version': '0.1.0'}

TOOL_DESCRIPTIONS = {
    'get_sales_metrics': 'Read daily sales for one SKU and date (paid orders, buyers, units sold, revenue), '
                         'including the previous-day baseline. Missing records stay unavailable, never zero. Read-only.',
    'get_traffic_metrics': 'Read daily product traffic (visitors) for one SKU and date with the previous-day baseline. '
                           'Read-only.',
    'get_inventory_status': 'Read daily inventory monitoring (out-of-stock minutes, monitored minutes) for one SKU '
                            'and date with the previous-day baseline. Cannot answer real-time stock counts. Read-only.',
    'get_promotion_history': 'Read campaign on/off records and campaign names for one SKU and date. active is not '
                             'paid advertising status; this cannot confirm ad spend or whether ads are enabled. Read-only.',
}

DATA_SCHEMA = {
    'type': 'object',
    'properties': {
        'sku_id': {'type': 'string', 'description': 'Exact SKU id, e.g. FLR-ROSE-19-BOUQUET; use find_products first.'},
        'date': {'type': 'string', 'description': 'Business date in Asia/Shanghai, format YYYY-MM-DD.'},
        'store_id': {'type': 'string', 'description': 'Optional store id; defaults to the configured workspace store.'},
    },
    'required': ['sku_id', 'date'],
    'additionalProperties': False,
}

FIND_SCHEMA = {
    'type': 'object',
    'properties': {
        'query': {'type': 'string', 'description': 'Chinese or English name fragment, alias, or SKU id.'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'description': 'Max matches, default 10.'},
    },
    'required': ['query'],
    'additionalProperties': False,
}


def _tool(name, description, schema):
    """生成 MCP 工具描述；schema 与服务端校验共用同一份约束。"""
    return {'name': name, 'description': description, 'inputSchema': schema}


class McpServer:
    """单进程、无状态的文件/年度快照读取；每个工具调用独立构造调查上下文。"""

    def __init__(self, store: DataStore, catalog: ProductCatalog):
        self.store = store
        self.catalog = catalog
        self.store_id = (store.meta.get('business_context') or {}).get('store_id')
        dates = store.dates('sales') or []
        self.first_date = dates[0] if dates else None
        self.last_date = dates[-1] if dates else None

    def tools(self):
        """只列出只读工具；没有 analyze/finish，也没有任何写操作。"""
        items = [_tool(name, TOOL_DESCRIPTIONS[name], DATA_SCHEMA) for name in TABLES]
        items.append(_tool('find_products',
                           'Search the product catalog by name, alias, or SKU id. Call this before the data tools '
                           'when you do not know the exact sku_id. Read-only.', FIND_SCHEMA))
        return items

    def handle(self, message):
        """处理一条 JSON-RPC 消息；通知没有 id，返回 None。批量请求按数组返回。"""
        if isinstance(message, list):
            responses = [self._one(item) for item in message]
            return [item for item in responses if item is not None] or None
        return self._one(message)

    def _one(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return _error(None, -32600, 'Invalid Request')
        identity = message.get('id')
        method = message['method']
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return _error(identity, -32602, 'params must be an object')
        if method == 'initialize':
            requested = params.get('protocolVersion')
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            return _result(identity, {'protocolVersion': version,
                                      'capabilities': {'tools': {'listChanged': False}},
                                      'serverInfo': SERVER_INFO})
        if method == 'ping':
            return _result(identity, {})
        if method == 'tools/list':
            return _result(identity, {'tools': self.tools()})
        if method == 'tools/call':
            return self._call(identity, params)
        if method in ('notifications/initialized', 'notifications/cancelled'):
            return None
        return _error(identity, -32601, f'Method not found: {method}')

    def _call(self, identity, params):
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(identity, -32602, 'tools/call requires name and an object arguments')
        if name == 'find_products':
            payload = self._find(arguments)
        elif name in TABLES:
            payload = self._read(name, arguments)
        else:
            return _error(identity, -32602, f'Unknown tool: {name}')
        return _result(identity, {'content': [{'type': 'text',
                                              'text': json.dumps(payload, ensure_ascii=False, indent=2)}],
                                  'isError': not payload.get('ok', False)})

    def _read(self, name, arguments):
        """校验参数后复用 ToolRegistry；未知 SKU、未来日期和额外字段都返回可纠正的错误。"""
        extra = set(arguments) - {'sku_id', 'date', 'store_id'}
        if extra:
            return {'ok': False, 'tool': name, 'error': f'Unexpected arguments: {sorted(extra)}'}
        sku_id = arguments.get('sku_id')
        day = arguments.get('date')
        store_id = arguments.get('store_id')
        if not isinstance(sku_id, str) or not sku_id:
            return {'ok': False, 'tool': name, 'error': 'sku_id is required; call find_products first.'}
        if not isinstance(day, str):
            return {'ok': False, 'tool': name, 'error': 'date is required in YYYY-MM-DD format.'}
        try:
            date.fromisoformat(day)
        except ValueError:
            return {'ok': False, 'tool': name, 'error': f'Invalid date: {day}. Use YYYY-MM-DD.'}
        if store_id is not None and self.store_id and store_id != self.store_id:
            return {'ok': False, 'tool': name,
                    'error': f'Unknown store_id for this workspace; available store is {self.store_id}.'}
        if sku_id not in self.store.skus:
            return {'ok': False, 'tool': name,
                    'error': f'Unknown sku_id: {sku_id}. Call find_products with a name fragment first.'}
        if self.last_date and day > self.last_date:
            return {'ok': False, 'tool': name,
                    'error': f'Date {day} is beyond the available data (last date {self.last_date}).'}
        # 每次调用冻结一个对象，注册表只授权当前工具；模型不能借助额外参数切换 SKU。
        registry = ToolRegistry(self.store, sku_id, day, allowed_tools={name})
        result = registry.execute(name, {}, uuid.uuid4().hex)
        result['store_id'] = self.store_id
        return result

    def _find(self, arguments):
        """目录检索只返回身份信息，不读取经营记录；结果用于后续工具的 sku_id。"""
        extra = set(arguments) - {'query', 'limit'}
        if extra:
            return {'ok': False, 'tool': 'find_products', 'error': f'Unexpected arguments: {sorted(extra)}'}
        query = arguments.get('query')
        limit = arguments.get('limit', 10)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 40:
            return {'ok': False, 'tool': 'find_products', 'error': 'query must be 1..40 characters.'}
        if type(limit) is not int or not 1 <= limit <= 20:
            return {'ok': False, 'tool': 'find_products', 'error': 'limit must be an integer in 1..20.'}
        needle = query.strip().lower()
        matches = []
        for sku_id, product in self.catalog.products.items():
            haystack = ' '.join([sku_id, product.get('name', ''), product.get('variant', ''),
                                 *product.get('aliases', [])]).lower()
            if needle in haystack:
                score = 0 if needle == sku_id.lower() else 1 if needle in product.get('name', '').lower() else 2
                matches.append((score, sku_id, product))
        matches.sort(key=lambda item: (item[0], item[1]))
        return {'ok': True, 'tool': 'find_products', 'store_id': self.store_id,
                'matches': [{'sku_id': sku_id, 'name': product.get('name'),
                             'variant': product.get('variant'),
                             'aliases': product.get('aliases', [])}
                            for _, sku_id, product in matches[:limit]],
                'total': len(matches)}


def _result(identity, payload):
    return {'jsonrpc': '2.0', 'id': identity, 'result': payload}


def _error(identity, code, message):
    return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': code, 'message': message}}


def serve(args):
    """从 stdin 逐行读取 JSON-RPC；stdout 只输出响应，退出时返回 0。"""
    store = DataStore(Path(args.data))
    catalog = ProductCatalog(Path(args.catalog), store.skus)
    server = McpServer(store, catalog)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response = _error(None, -32700, 'Parse error')
            else:
                response = server.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
                sys.stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0