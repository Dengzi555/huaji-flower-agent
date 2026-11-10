"""验证角色准入、工具最小授权与越权终态，不调用真实模型。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.data import DataStore
from ecommerce_agent.errors import AgentError
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.model_runtime import model_scope
from ecommerce_agent.roles import DEFAULT_ROLE, ROLES
from ecommerce_agent.task_admission import prepare_task, validate_request
from ecommerce_agent.task_parser import ProductCatalog
from ecommerce_agent.tools import (
    FOLLOW_UP_TOOL, INVENTORY_RANGE_SCHEMA, PROMOTION_RANGE_SCHEMA, SCHEMAS, ToolRegistry,
)
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.workflows import execute_task


class RoleTests(unittest.TestCase):
    """使用已冻结任务卡与可控模型响应，检验角色边界。"""

    def setUp(self):
        self.store = DataStore.from_document(business_data())
        self.catalog = ProductCatalog.from_document(business_catalog(), self.store.skus)
        self.question = '炽热爱意10月25日销量下降原因'

    def test_role_configs_and_request_validation(self):
        schemas = [*SCHEMAS, INVENTORY_RANGE_SCHEMA, PROMOTION_RANGE_SCHEMA]
        registered = {schema['function']['name'] for schema in schemas} | {FOLLOW_UP_TOOL}
        self.assertEqual(set(ROLES), {'store_operator', 'product_investigator', 'supply_buyer', 'supervisor'})
        for role_id, role in ROLES.items():
            self.assertTrue(role.name and role.default_task)
            self.assertLessEqual(set(role.allowed_tools), registered)
            validate_request({'question': self.question, 'role': role_id})
        for invalid in (None, '', 'admin', ['store_operator']):
            with self.subTest(role=invalid), self.assertRaisesRegex(ValueError, '角色无效'):
                validate_request({'question': self.question, 'role': invalid})
        with model_scope():
            prepared = prepare_task({'question': self.question}, self.store, self.catalog,
                                    'fixture', today=date(2026, 10, 29))
        self.assertEqual(prepared.parsed['role'], DEFAULT_ROLE)

    def test_registry_raises_before_executing_an_out_of_role_tool(self):
        registry = ToolRegistry(self.store, 'FLR-ROSE-99-BOUQUET', '2026-10-25',
                                allow_orders=True, role=ROLES['supply_buyer'])
        self.assertNotIn('get_order_funnel', registry.allowed_tools)
        with self.assertRaisesRegex(AgentError, '无权'):
            registry.execute('get_order_funnel', {}, 'denied')
        self.assertEqual(registry.results, {})
        self.assertTrue(registry.execute('get_sales_metrics', {}, 'allowed')['ok'])

    def test_child_task_inherits_parent_role_when_not_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, catalog = root / 'data.json', root / 'catalog.json'
            data.write_text(json.dumps(business_data()), encoding='utf-8')
            catalog.write_text(json.dumps(business_catalog()), encoding='utf-8')
            service = InvestigationService(data, catalog, root / 'state', root / 'runs', start_worker=False)
            try:
                today = date(2026, 10, 29)
                parent = service.create({'question': self.question, 'role': 'supply_buyer'}, _today=today)['task']
                child = service.create({'question': self.question, 'parent_id': parent['id']}, _today=today)['task']
                self.assertEqual(child['parsed']['role'], 'supply_buyer')
            finally:
                service.close()

    def test_model_role_violation_stops_batch_and_records_reason(self):
        with model_scope():
            task = prepare_task({'question': self.question, 'role': 'supply_buyer'},
                                self.store, self.catalog, 'fixture', today=date(2026, 10, 29)).parsed
        self.assertEqual(task['status'], 'ready')
        calls = []

        class Client:
            def complete(self, messages, tools, timeout):
                calls.append((messages, tools))
                return {'role': 'assistant', 'tool_calls': [
                    {'id': 'allowed', 'function': {'name': 'get_sales_metrics', 'arguments': '{}'}},
                    {'id': 'denied', 'function': {'name': 'get_order_funnel', 'arguments': '{}'}},
                ]}

        with tempfile.TemporaryDirectory() as folder:
            state = execute_task(self.store, task['sku_id'], task['target_date'], folder,
                                 task_context=task, provider=Client())
            events = [json.loads(line) for line in
                      (Path(state['run_dir']) / 'events.jsonl').read_text(encoding='utf-8').splitlines()]

        self.assertEqual(len(calls), 1)
        messages, tools = calls[0]
        self.assertIn(ROLES['supply_buyer'].default_task, messages[0]['content'])
        self.assertNotIn('get_order_funnel', {tool['function']['name'] for tool in tools})
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['stop_reason'], 'role_tool_denied')
        self.assertIn('无权', state['error'])
        self.assertFalse(any(event.get('call_id') in ('allowed', 'denied')
                             for event in events if event['type'] == 'tool_started'))
        self.assertTrue(any(event['type'] == 'tool_rejected' and event['call_id'] == 'denied'
                            and event['result']['error'] == state['error'] for event in events))
