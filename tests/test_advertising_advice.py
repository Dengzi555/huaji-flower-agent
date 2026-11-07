"""推广建议不是重新查数：继承同店证据、无需日期，模型不得生成执行参数。"""
import unittest
from unittest.mock import patch
import test_conversations as fixtures


class AdvertisingAdviceTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def test_original_settings_followup_reuses_previous_evidence(self):
        self.turn('福田店前天推广分析一下')
        with patch.object(self.hub.books['hx-sz-ft'],'report',side_effect=AssertionError('建议不能重新查询或替换日期')):
            result=self.turn('针对本店推广的设置')
        self.assertEqual(result['conversation']['waiting'],[])
        turn=result['conversation']['turns'][-1]
        self.assertEqual(turn['advertising']['operation'],'advertising_advice')
        self.assertEqual(turn['advertising']['reports'][0]['date'],'2026-10-24')
        self.assertIn('建议',turn['message'])
        self.assertNotIn('请补充',turn['message'])

    def test_general_advice_without_data_does_not_request_date_or_claim_evidence(self):
        result=self.turn('本店推广怎么设置比较好')
        turn=result['conversation']['turns'][-1]
        self.assertEqual(result['conversation']['waiting'],[])
        self.assertEqual(turn['advertising']['reports'],[])
        self.assertIn('没有可沿用',turn['message'])
        self.assertIn('不是平台',turn['message'])

    def test_implicit_advice_followup_and_old_bad_date_prompt_recovery(self):
        self.turn('福田店前天推广分析')
        self.turn('福田店推广分析')  # 历史版本在建议前留下的补日期状态。
        result=self.turn('那应该怎么调整预算比较合适')
        turn=result['conversation']['turns'][-1]
        self.assertEqual(result['conversation']['waiting'],[])
        self.assertEqual(turn['advertising']['operation'],'advertising_advice')
        self.assertEqual(turn['advertising']['reports'][0]['store_id'],'hx-sz-ft')

    def test_new_store_must_not_reuse_futian_evidence(self):
        self.turn('福田店前天推广分析')
        result=self.turn('南岸店的推广有什么建议')
        turn=result['conversation']['turns'][-1]
        self.assertEqual(turn['store_id'],'hx-cq-na')
        self.assertEqual(turn['advertising']['reports'],[])

    def test_model_only_selects_allowed_advice_and_failure_still_returns_grounded_advice(self):
        self.turn('福田店前天推广分析')
        self.manager.advertising_adviser=lambda question,context:{'actions':['review_cost_hours','compare_modes'],'model':'offline-test'}
        result=self.turn('针对本店推广的设置')
        receipt=result['conversation']['turns'][-1]['advertising']
        self.assertEqual(receipt['advice_actions'],['review_cost_hours','compare_modes'])
        self.assertEqual(receipt['model_calls'],1)
        self.manager.advertising_adviser=lambda question,context:{'actions':['change_budget'],'budget':999}
        result=self.turn('那有什么建议')
        receipt=result['conversation']['turns'][-1]['advertising']
        self.assertEqual(receipt['status'],'completed')
        self.assertEqual(receipt['advice_planning']['status'],'fallback')
        self.assertNotIn('999',result['conversation']['turns'][-1]['message'])

    def test_request_to_execute_does_not_change_local_settings(self):
        before=self.hub.for_store('hx-sz-ft').operations.settings()
        result=self.turn('福田店推广建议，然后把预算调成200元')
        self.assertEqual(result['conversation']['turns'][-1]['advertising']['status'],'unsupported')
        self.assertEqual(before,self.hub.for_store('hx-sz-ft').operations.settings())

    def test_new_product_or_care_question_does_not_inherit_advertising_advice(self):
        self.turn('福田店前天推广分析')
        self.manager.chat_responder=lambda q,history:{'text':'一般养护建议。','model_calls':0}
        result=self.turn('向日葵怎么养')
        self.assertNotIn('advertising',result['conversation']['turns'][-1])


if __name__=='__main__':unittest.main()
