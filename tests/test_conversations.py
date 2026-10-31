"""同窗口多店状态回归：独立上下文、指代确认、不可变任务、版本与重启恢复。"""
import json
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path
from ecommerce_agent.conversations import Conversations
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.store_workspace import StoreHub,generate_stores
from ecommerce_agent.web_service import ConflictError
from ecommerce_agent.web import create_server


class ConversationTests(unittest.TestCase):
    def setUp(self):
        """使用真实各店SQLite及离线队列，不调用模型或支付外部费用。"""
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        generate_stores(root,business_data());(root/'data/products.json').write_text(json.dumps(business_catalog()),encoding='utf-8')
        self.hub=StoreHub(root,root/'state',root/'runs',start_worker=False);self.addCleanup(self.hub.close)
        # 此组回归显式固定旧的“当前店”模式；新默认全部门店由独立用例覆盖。
        self.hub.default_query_scope='current'
        self.manager=Conversations(self.hub,today=lambda:date(2026,10,26));self.doc=self.manager.new('hx-sz-ns');self.seq=0

    def turn(self,question,**extra):
        self.seq+=1
        result=self.manager.turn(self.doc['id'],{'question':question,'revision':self.doc['revision'],**extra},f'input-{self.seq}')
        self.doc=result['conversation'];return result

    def test_ambiguous_product_then_color_confirmation(self):
        first=self.turn('洋桔梗2026-10-25销量怎么样')['task']
        self.assertEqual(first['status'],'queued');self.assertEqual(self.doc['stores']['hx-sz-ns']['scope']['sku'],'__all_matched__')
        second=self.turn('白色那款')['task']
        self.assertEqual(second['status'],'queued');self.assertEqual(second['parsed']['sku_id'],'FLR-EUSTOMA-11-BOUQUET')
        self.assertEqual(second['parsed']['target_date'],'2026-10-25');self.assertEqual(second['question'],'白色那款')
        self.assertEqual(second['parent_id'],first['id']);self.assertEqual(second['parsed']['raw_question'],'白色那款')

    def test_product_date_and_intent_changes_keep_old_report_scope(self):
        original=self.turn('99红玫2026-10-25销量怎么样')['task']
        changed=self.turn('换成33乌梅呢')['task']
        self.assertEqual(changed['parsed']['sku_id'],'FLR-WUMEI-33-BUCKET')
        self.assertEqual(changed['parsed']['target_date'],'2026-10-25')
        earlier=self.turn('换成前天')['task'];self.assertEqual(earlier['parsed']['target_date'],'2026-10-24')
        stock=self.turn('那缺货多久')['task'];self.assertEqual(stock['parsed']['intent'],'query_inventory')
        saved=self.hub.for_store('hx-sz-ns').get(original['id']);self.assertEqual(saved['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertEqual(saved['parsed']['target_date'],'2026-10-25')

    def test_global_ranking_does_not_inherit_previous_single_product(self):
        self.turn('99红玫2026-10-25销量怎么样')
        ranked=self.turn('昨天什么买的最好')['task']
        self.assertEqual(ranked['parsed']['product_scope']['kind'],'store')
        self.assertGreater(len(ranked['parsed']['product_scope']['sku_ids']),1)
        previous=self.turn('前天呢')['task']
        self.assertEqual(previous['parsed']['target_date'],'2026-10-24')
        self.assertEqual(previous['parsed']['query_plan']['ranking']['direction'],'desc')
        bottom=self.turn('那最差呢')['task']
        self.assertEqual(bottom['parsed']['query_plan']['ranking']['direction'],'asc')

    def test_whole_store_phrase_is_scope_not_an_unknown_shop(self):
        """真实对话入口不能把日期后的“日全店”当店名；全店仍只查当前门店。"""
        result=self.turn('10月25日全店销量最高与最低各有哪些')
        task=result['task']
        self.assertIsNotNone(task,result['conversation']['notice'])
        self.assertEqual(task['status'],'queued',task['parsed']['issues'])
        self.assertEqual(task['parsed']['store_id'],'hx-sz-ns')
        self.assertEqual(task['parsed']['target_date'],'2026-10-25')
        self.assertEqual(task['parsed']['product_scope']['kind'],'store')
        self.assertEqual(task['parsed']['query_plan']['ranking']['direction'],'both')
        self.assertNotIn('store',result['conversation']['waiting'])

    def test_scope_words_do_not_hide_a_real_unknown_shop(self):
        """移除范围短语后仍需检查未知实体，不能因出现全店就跳过门店校验。"""
        result=self.turn('北京店10月25日全店销量最高与最低各有哪些')
        self.assertIsNone(result['task'])
        self.assertEqual(result['conversation']['waiting'],['store'])
        self.assertEqual(result['conversation']['focus_store'],'hx-sz-ns')

    def test_explicit_known_shop_can_query_its_whole_store(self):
        """明确换店与该店内商品范围同时出现时，沿用正常门店解析。"""
        result=self.turn('南岸店10月25日全店销量最高与最低各有哪些')
        self.assertIsNotNone(result['task'])
        self.assertEqual(result['task']['parsed']['store_id'],'hx-cq-na')
        self.assertEqual(result['task']['parsed']['product_scope']['kind'],'store')

    def test_whole_store_followup_clears_single_sku_without_changing_shop(self):
        """已有单品条件时，全店排名应放开商品范围，不能继承上一SKU。"""
        self.turn('99红玫10月25日销量怎么样')
        result=self.turn('10月25日全店销量最高与最低各有哪些')
        self.assertIsNotNone(result['task'],result['conversation']['notice'])
        parsed=result['task']['parsed']
        self.assertEqual(parsed['store_id'],'hx-sz-ns')
        self.assertEqual(parsed['sku_id'],'__all_matched__')
        self.assertEqual(parsed['query_plan']['ranking']['direction'],'both')

    def test_greeting_has_reply_and_preserves_business_scope_and_idempotency(self):
        self.turn('99红玫2026-10-25销量怎么样')
        before=json.loads(json.dumps(self.doc['stores']))
        payload={'question':'你好！','revision':self.doc['revision']}
        reply=self.manager.turn(self.doc['id'],payload,'hello-once')
        self.assertIsNone(reply['task']);self.assertEqual(reply['conversation']['stores'],before)
        turn=reply['conversation']['turns'][-1]
        self.assertEqual(turn['kind'],'chat');self.assertIn('花迹',turn['message']);self.assertEqual(turn['chat']['model_calls'],0)
        replay=self.manager.turn(self.doc['id'],payload,'hello-once')
        self.assertTrue(replay['reused']);self.assertEqual(len(replay['conversation']['turns']),2)
        self.doc=reply['conversation']
        follow=self.turn('前天呢')['task'];self.assertEqual(follow['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')

    def test_category_ranking_followup_keeps_dimension_not_single_sku(self):
        first=self.turn('10月25日哪类花卖得最好')['task']
        self.assertEqual(first['parsed']['query_plan']['ranking']['group_by'],'variety')
        earlier=self.turn('前天呢')['task']
        self.assertEqual(earlier['parsed']['query_plan']['ranking']['group_by'],'variety')
        bottom=self.turn('那最差呢')['task']
        self.assertEqual(bottom['parsed']['query_plan']['ranking']['group_by'],'variety')
        self.assertEqual(bottom['parsed']['query_plan']['ranking']['direction'],'asc')

    def test_chinese_window_replaces_prior_date_and_keeps_both_rankings(self):
        self.turn('99红玫10月25日销量怎么样')
        ranked=self.turn('分析下近七天，哪款商品销量最好，哪款最差？')['task']
        self.assertEqual(ranked['status'],'queued',ranked['parsed']['issues'])
        self.assertEqual(ranked['parsed']['trend_start'],'2026-10-19')
        self.assertEqual(ranked['parsed']['trend_end'],'2026-10-25')
        self.assertEqual(ranked['parsed']['query_plan']['ranking']['direction'],'both')
        narrow=self.turn('只看抱抱桶')['task']
        self.assertEqual(narrow['status'],'queued',narrow['parsed']['issues'])
        self.assertTrue(narrow['parsed']['is_window'])
        self.assertEqual(narrow['parsed']['query_plan']['ranking']['direction'],'both')

    def test_general_chat_uses_only_chat_history_not_business_metrics(self):
        self.turn('99红玫2026-10-25销量怎么样');self.turn('你好')
        calls=[]
        def responder(question,history):
            calls.append((question,history));return {'text':'玫瑰可以斜剪花茎并保持容器清洁。','model_calls':1,'model':'fixture'}
        self.manager.chat_responder=responder
        reply=self.turn('玫瑰怎么养护？')
        self.assertIsNone(reply['task']);self.assertEqual(reply['conversation']['turns'][-1]['chat']['mode'],'model')
        self.assertEqual(len(calls),1);self.assertTrue(all(t['kind']=='chat' for t in calls[0][1]))
        query=self.turn('你好，99红玫2026-10-25销量怎么样')['task'];self.assertEqual(query['status'],'queued')
        self.assertEqual(len(calls),1)

    def test_chat_failure_replies_and_pending_typo_stays_pending(self):
        self.turn('99红梅2026-10-25销量下降原因')
        waiting=list(self.doc['waiting']);stores=json.loads(json.dumps(self.doc['stores']))
        def unavailable(*args):raise TimeoutError('private provider error')
        self.manager.chat_responder=unavailable
        reply=self.turn('讲个笑话吧')
        self.assertIsNone(reply['task']);self.assertEqual(self.doc['waiting'],waiting);self.assertEqual(self.doc['stores'],stores)
        self.assertIn('稍后重试',self.doc['turns'][-1]['message']);self.assertNotIn('private',self.doc['turns'][-1]['message'])

    def test_cross_store_inherits_conditions_not_parent_or_evidence(self):
        first=self.turn('99红玫2026-10-25销量怎么样')['task']
        other=self.turn('南岸店呢')['task']
        self.assertEqual(other['parsed']['store_id'],'hx-cq-na');self.assertEqual(other['parsed']['sku_id'],first['parsed']['sku_id'])
        self.assertIsNone(other['parent_id']);self.assertIsNone(self.hub.for_store('hx-sz-ns').get(other['id']))
        self.turn('换成33乌梅呢')
        back=self.turn('回到南山店')['task'];self.assertEqual(back['id'],first['id'])
        self.assertEqual(self.doc['stores']['hx-cq-na']['scope']['sku'],'FLR-WUMEI-33-BUCKET')
        # 明确切回南山后，“它”指已确定商品；历史出现多店不再强制触发门店澄清。
        inventory=self.turn('那它的库存呢')['task']
        self.assertEqual(inventory['parsed']['store_id'],'hx-sz-ns')
        self.assertEqual(inventory['parsed']['sku_id'],first['parsed']['sku_id'])

    def test_compound_followups_change_only_requested_slots(self):
        self.turn('昨天什么买的最好，比上周同一天怎么样')
        earlier=self.turn('那前天呢')['task']
        self.assertEqual(earlier['status'],'queued',earlier['parsed']['issues'])
        self.assertEqual(earlier['parsed']['target_date'],'2026-10-24')
        self.assertEqual(earlier['parsed']['comparison_date'],'2026-10-17')
        narrowed=self.turn('只看抱抱桶')['task']
        self.assertEqual(narrowed['status'],'queued',narrowed['parsed']['issues'])
        self.assertEqual(narrowed['parsed']['target_date'],'2026-10-24')
        self.assertEqual(narrowed['parsed']['query_plan']['ranking']['direction'],'desc')
        self.assertTrue(all(p['product_type']=='抱抱桶' for p in narrowed['parsed']['candidates']))
        self.assertTrue(narrowed['parsed']['product_scope']['compare_selected'])
        other=self.turn('不是南山店，是南岸店')['task']
        # 同一句出现两个门店是纠正关系，而不是请求跨店比较。
        self.assertEqual(other['parsed']['store_id'],'hx-cq-na')

    def test_narrowing_type_keeps_variety_and_new_summary_resets_rank(self):
        self.turn('昨天红玫瑰哪款最好')
        narrowed=self.turn('只看抱抱桶')['task']
        self.assertEqual(narrowed['status'],'queued',narrowed['parsed']['issues'])
        self.assertTrue(all(p['variety']=='红玫瑰' and p['product_type']=='抱抱桶' for p in narrowed['parsed']['candidates']))
        summary=self.turn('一共卖了多少')['task']
        self.assertEqual(summary['status'],'queued',summary['parsed']['issues'])
        self.assertIsNone(summary['parsed']['query_plan']['ranking'])

    def test_dropdown_focus_restores_own_scope_and_reload(self):
        self.turn('99红玫2026-10-25销量怎么样');self.turn('南岸店呢');self.turn('换成33乌梅呢')
        result=self.manager.focus(self.doc['id'],{'store_id':'hx-sz-ns','revision':self.doc['revision']})
        state=result['conversation'];self.assertEqual(state['stores']['hx-sz-ns']['scope']['sku'],'FLR-ROSE-99-BOUQUET')
        restored=Conversations(self.hub).get(self.doc['id']);self.assertEqual(restored['focus_store'],'hx-sz-ns')
        with self.assertRaises(ValueError):self.manager.focus(self.doc['id'],{'store_id':'hx-sz-ns','revision':restored['revision'],'task_id':'f'*32})

    def test_unknown_replacement_multi_store_and_write_guard(self):
        self.turn('99红玫2026-10-25销量怎么样')
        self.assertIsNone(self.turn('换成不存在的花呢')['task'])
        self.assertIsNone(self.turn('郁金香呢')['task'])
        self.assertIsNone(self.turn('南山店和南岸店分别看看')['task'])
        self.assertIsNone(self.turn('两店哪个缺货多')['task'])
        self.assertIsNone(self.turn('北京店呢')['task'])
        rejected=self.turn('把99红玫库存改成100')['task'];self.assertEqual(rejected['status'],'unsupported')

    def test_idempotency_revision_and_window_isolation(self):
        payload={'question':'99红玫2026-10-25销量怎么样','revision':0}
        result=self.manager.turn(self.doc['id'],payload,'same-key');replay=self.manager.turn(self.doc['id'],payload,'same-key')
        self.assertEqual(result['task']['id'],replay['task']['id'])
        with self.assertRaises(ConflictError):self.manager.turn(self.doc['id'],payload,'another-key')
        with self.assertRaises(ConflictError):self.manager.turn(self.doc['id'],{**payload,'question':'其他问题'},'same-key')
        separate=self.manager.new('hx-sz-ns');self.assertEqual(separate['stores'],{})

    def test_typo_stays_pending_until_explicit_sku(self):
        first=self.turn('99红梅2026-10-25销量下降原因')['task']
        self.assertEqual(first['status'],'needs_clarification');self.assertIsNone(self.doc['stores']['hx-sz-ns']['scope']['sku'])
        fixed=self.turn(first['question'],sku='FLR-ROSE-99-BOUQUET')['task'];self.assertEqual(fixed['status'],'queued')

    def test_window_scope_is_inherited_as_a_window(self):
        """主线新增多日窗口时，跨店追问仍沿用起止日，不能退化成最后一天。"""
        first=self.turn('99红玫2026-10-20至2026-10-25销量怎么样')['task']
        self.assertTrue(first['parsed']['is_window'])
        second=self.turn('南岸店呢')['task']
        self.assertTrue(second['parsed']['is_window'])
        self.assertEqual(second['parsed']['trend_start'],'2026-10-20')
        self.assertEqual(second['parsed']['trend_end'],'2026-10-25')

    def test_http_authorization_origin_and_persistent_session(self):
        """新接口必须沿用工作区口令和同源校验，不能用对话入口绕过准入。"""
        server=create_server(self.hub,port=0,token='t'*30)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,payload=None,authorized=True,origin=None):
            headers={'Content-Type':'application/json','Idempotency-Key':'http-turn'}
            if authorized:headers['Authorization']='Bearer '+'t'*30
            if origin:headers['Origin']=origin
            req=urllib.request.Request(base+path,headers=headers,data=json.dumps(payload).encode() if payload is not None else None)
            try:
                with urllib.request.urlopen(req) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as e:return e.code,json.load(e)
        self.assertEqual(request('/api/conversations',{'store_id':'hx-sz-ns'},authorized=False)[0],401)
        self.assertEqual(request('/api/conversations',{'store_id':'hx-sz-ns'},origin='https://other.test')[0],403)
        code,result=request('/api/conversations',{'store_id':'hx-sz-ns'});self.assertEqual(code,201)
        cid=result['conversation']['id']
        code,result=request(f'/api/conversations/{cid}/turns',{'question':'99红玫2026-10-25销量怎么样','revision':0})
        self.assertEqual(code,200);self.assertEqual(result['task']['status'],'queued')
        self.assertEqual(request(f'/api/conversations/{cid}',authorized=False)[0],401)
        self.assertEqual(request(f'/api/conversations/{cid}')[1]['conversation']['revision'],1)
        self.assertEqual(request('/api/conversations/'+'f'*32)[0],404)
