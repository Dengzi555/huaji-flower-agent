"""分层路由：规则零模型、未知才分类、歧义和失败不执行经营工具。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from ecommerce_agent.routing import ROUTES,route_question
from ecommerce_agent.web_service import InvestigationService
from ecommerce_agent.flower_business_data import business_data,business_catalog


def proposal(intent='query_sales_performance',**changes):
    """可控分类输出，避免把联网服务的随机性混进路由单元回归。"""
    return dict(decision='select',intent=intent,candidates=[intent],confidence='high',**changes)


class HybridRoutingTests(unittest.TestCase):
    """检查是否真的调用分类器，以及是否有任何业务工具执行机会。"""
    def test_clear_ambiguous_and_guard_never_call_classifier(self):
        classifier=Mock(side_effect=AssertionError('不应调用模型'))
        for text,status in [('99红玫昨天销量怎么样','selected'),('99红玫销量下降，顺便查库存','needs_clarification'),
                            ('查库存然后退款','unsupported'),('最近300天这款走得如何','unsupported'),('天气怎么样','unsupported')]:
            result=route_question(text,classifier=classifier)
            self.assertEqual(result['status'],status);self.assertEqual(result['model_calls'],0)
        classifier.assert_not_called()

    def test_unknown_expression_uses_one_classifier(self):
        classifier=Mock(return_value={**proposal(),'usage':{'total_tokens':118},'model':'offline-stub'})
        result=route_question('99红玫2026-10-25出了几束',classifier=classifier)
        self.assertEqual(result['intent'],'query_sales_performance');self.assertEqual(result['tier'],'model')
        self.assertEqual(result['model_calls'],1);self.assertEqual(result['model_tokens'],118)
        classifier.assert_called_once()

    def test_low_confidence_multi_candidates_and_invalid_outputs_stop(self):
        candidates=[{**proposal(),'confidence':'low'},
                    {**proposal(),'candidates':['query_sales_performance','query_inventory']},
                    {**proposal(),'intent':'refund'}, {'decision':'select'},None]
        for index,candidate in enumerate(candidates):
            result=route_question('这款帮我看看',classifier=Mock(return_value=candidate))
            self.assertEqual(result['status'],'needs_clarification' if index<2 else 'failed');self.assertIsNone(result['intent'])
            if index>=2:self.assertEqual(result['error_info']['code'],'model_response_invalid')
        failed=route_question('这款帮我看看',classifier=Mock(side_effect=TimeoutError('secret should not leak')))
        self.assertEqual(failed['tier'],'model_fallback');self.assertNotIn('secret',json.dumps(failed))
        self.assertEqual(failed['error_info']['code'],'model_timeout')
        self.assertEqual(failed['status'],'failed')
        offline=route_question('这款帮我看看')
        self.assertEqual(offline['status'],'needs_clarification');self.assertEqual(offline['model_calls'],0)

    def test_service_confirmation_reuses_parent_and_idempotency(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data.json';catalog=root/'products.json'
            data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
            service=InvestigationService(data,catalog,root/'state',root/'runs',start_worker=False)
            try:
                classifier=Mock(return_value={**proposal(),'decision':'clarify','confidence':'medium',
                    'candidates':['query_sales_performance','query_inventory']})
                service.route_classifier=classifier
                question='99红玫2026-10-25帮我看看'
                first=service.create({'question':question},'same-request')['task']
                self.assertEqual(first['status'],'needs_clarification');self.assertEqual(service.work.qsize(),0)
                self.assertEqual(service.create({'question':question},'same-request')['task']['id'],first['id'])
                classifier.assert_called_once()
                wrong=service.create({'question':question,'intent':'query_promotions','parent_id':first['id']})['task']
                self.assertEqual(wrong['status'],'needs_clarification')
                chosen=service.create({'question':question,'intent':'query_sales_performance','parent_id':first['id']})['task']
                self.assertEqual(chosen['status'],'queued');self.assertEqual(chosen['parsed']['routing']['tier'],'user_confirmation')
                self.assertEqual(chosen['parsed']['routing']['model_calls'],0);classifier.assert_called_once()
                self.assertEqual(service.work.qsize(),1)
            finally:service.close()

    def test_model_selection_does_not_fill_missing_date_or_sku(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data.json';catalog=root/'products.json'
            data.write_text(json.dumps(business_data()),encoding='utf-8');catalog.write_text(json.dumps(business_catalog()),encoding='utf-8')
            service=InvestigationService(data,catalog,root/'state',root/'runs',start_worker=False)
            try:
                service.route_classifier=Mock(return_value=proposal())
                task=service.create({'question':'玫瑰出了几束'})['task']
                self.assertEqual(task['status'],'needs_clarification');self.assertIsNone(task['parsed']['target_date'])
                self.assertEqual(service.work.qsize(),0)
            finally:service.close()
