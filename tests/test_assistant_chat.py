"""普通对话与经营工具分流：人设、预算、无工具授权和空回复失败处理。"""
import tempfile
import unittest
from unittest.mock import Mock
from ecommerce_agent.assistant_chat import local_reply,CHAT_PROMPT
from ecommerce_agent.local_model import LocalModelSettings


class AssistantChatTests(unittest.TestCase):
    def test_greeting_is_whole_message_not_prefix_match(self):
        self.assertIn('花迹',local_reply('你好呀！'))
        self.assertIsNone(local_reply('你好，99红玫昨天销量怎么样？'))

    def test_model_chat_has_persona_no_tools_and_bounded_history(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=LocalModelSettings(temp);client=Mock(model='fixture',last_usage={'total_tokens':12})
            client.complete.return_value={'content':'我是花迹，可以聊聊鲜花养护。'};settings.provider=Mock(return_value=client)
            history=[{'question':str(i),'message':'历史回复'} for i in range(20)]
            reply=settings.chat('你好',history);messages,tools,timeout=client.complete.call_args.args
            # 先选最近12个完整轮次，再由真实provider按容量继续裁剪；不截半条回答。
            self.assertEqual(messages[0],{'role':'system','content':CHAT_PROMPT});self.assertEqual(len(messages),27)
            self.assertEqual(messages[2]['content'],'8');self.assertEqual(messages[-1]['content'],'你好')
            self.assertEqual(tools,[]);self.assertEqual(timeout,10);self.assertEqual(client.max_tokens,512)
            self.assertEqual(reply['model_calls'],1)
            for invalid in [{'content':None},{'content':'','tool_calls':[]},{'content':'执行退款','tool_calls':[{'function':{'name':'refund'}}]}]:
                client.complete.return_value=invalid
                with self.assertRaises(ValueError):settings.chat('普通聊天',[])

    def test_calendar_question_is_chat_even_with_business_context(self):
        from datetime import date
        from pathlib import Path
        from ecommerce_agent.assistant_chat import is_general_chat,calendar_context
        from ecommerce_agent.task_parser import ProductCatalog
        from ecommerce_agent.data import DataStore
        root=Path(__file__).resolve().parents[1];store=DataStore(root/'data/stores/hx-cq-na.json')
        catalog=ProductCatalog(root/'data/products.json',store.skus)
        for question in ['最近的节日是什么','下一个节日是哪天呢','明天是什么节日']:
            self.assertTrue(is_general_chat(question,catalog,date(2026,10,29),True))
        self.assertFalse(is_general_chat('最近七天销量怎么样',catalog,date(2026,10,29),True))
        context=calendar_context(date(2026,10,29))
        self.assertEqual(context['upcoming'][0]['date'],'2026-10-31')
