"""真实对话入口验收门店解析：候选确认、原条件继续、无误扇出和重启恢复。"""
import unittest
import json
import threading
import urllib.request
import urllib.error
from datetime import date
import test_conversations as fixtures
from ecommerce_agent.conversations import Conversations
from ecommerce_agent.web import create_server


class StoreConversationTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def count(self):return sum(len(s.list_tasks()) for s in self.hub.services.values())

    def ambiguous_alias(self):
        for shop in self.hub.stores:
            if shop['id'] in ('hx-sz-ns','hx-cq-na'):shop['aliases'].append('中心店')

    def test_shared_alias_confirm_continues_original_conditions_not_old_scope(self):
        self.ambiguous_alias();self.turn('19红玫10月25日销量怎么样')
        before=self.count();query='中心店10月24日99红玫销量怎么样'
        waiting=self.turn(query)
        self.assertIsNone(waiting['task']);self.assertEqual(self.count(),before)
        self.assertEqual(set(self.doc['store_choices']),{'hx-sz-ns','hx-cq-na'})
        answer=self.turn('重庆南岸店')['task']
        self.assertEqual(answer['parsed']['store_id'],'hx-cq-na')
        self.assertEqual(answer['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertEqual(answer['parsed']['target_date'],'2026-10-24')
        self.assertEqual(answer['question'],query)
        self.assertEqual(self.doc['turns'][-1]['question'],'重庆南岸店')
        self.assertEqual(answer['parsed']['conversation_context']['store_resolution']['source'],'user_confirmed')

    def test_button_choice_and_ordinal_survive_restart(self):
        self.ambiguous_alias();self.turn('中心店10月25日99红玫销量怎么样')
        self.manager=Conversations(self.hub,today=self.manager.today)
        answer=self.turn('第二家')['task']
        # 候选有稳定ID排序，UI显示顺序和自然语言序号解释完全一致。
        self.assertEqual(answer['parsed']['store_id'],'hx-sz-ns')
        self.assertEqual(answer['parsed']['target_date'],'2026-10-25')
        self.turn('中心店10月24日99红玫销量怎么样')
        result=self.turn('中心店10月24日99红玫销量怎么样',store_choice='hx-cq-na')
        self.assertEqual(result['task']['parsed']['target_date'],'2026-10-24')

    def test_relative_date_is_frozen_across_midnight_confirmation(self):
        self.ambiguous_alias();self.turn('中心店昨天99红玫销量怎么样')
        self.manager.today=lambda:date(2026,10,27)
        task=self.turn('重庆南岸店')['task']
        self.assertEqual(task['parsed']['target_date'],'2026-10-25')
        self.assertEqual(task['parsed']['business_today'],'2026-10-26')

    def test_natural_confirmation_preserves_scope_but_new_query_does_not_replay(self):
        self.ambiguous_alias();self.turn('中心店10月25日99红玫销量怎么样')
        task=self.turn('就南岸店吧')['task']
        self.assertEqual(task['parsed']['target_date'],'2026-10-25')
        self.assertEqual(task['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.turn('中心店10月25日99红玫销量怎么样')
        task=self.turn('南岸店10月24日19红玫销量怎么样')['task']
        self.assertEqual(task['parsed']['target_date'],'2026-10-24')
        self.assertEqual(task['parsed']['sku_id'],'FLR-ROSE-19-BOUQUET')

    def test_first_turn_comparison_uses_given_scope_without_extra_anchor_task(self):
        result=self.turn('南山店和南岸店10月25日99红玫销量对比')
        job=result['conversation']['comparison']
        self.assertEqual(self.count(),2)
        for child in job['children']:
            task=self.hub.for_store(child['store_id']).get(child['task_id'])
            self.assertEqual(task['parsed']['target_date'],'2026-10-25')
            self.assertEqual(task['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')

    def test_forged_choice_and_stale_directory_are_rejected_without_tasks(self):
        self.ambiguous_alias();self.turn('中心店10月25日99红玫销量怎么样');before=self.count()
        with self.assertRaises(ValueError):self.turn('确认',store_choice='hx-sz-ft')
        self.hub.stores[0]['aliases'].append('新名称')
        with self.assertRaises(ValueError):self.turn('确认',store_choice='hx-cq-na')
        self.assertEqual(self.count(),before)

    def test_known_and_unknown_never_partially_compare(self):
        self.turn('19红玫10月25日销量怎么样');before=self.count()
        result=self.turn('南山店和北京店对比')
        self.assertIsNone(result['task']);self.assertEqual(self.count(),before)
        self.assertNotIn('comparison',result['conversation'])

    def test_negative_store_does_not_create_comparison(self):
        self.turn('19红玫10月25日销量怎么样')
        result=self.turn('不看南山店，看看南岸店')
        self.assertEqual(result['task']['parsed']['store_id'],'hx-cq-na')
        self.assertNotIn('comparison',result['conversation'])
        self.assertEqual(result['task']['parsed']['sku_id'],'FLR-ROSE-19-BOUQUET')

    def test_flower_words_in_store_name_do_not_change_product_scope(self):
        self.turn('19红玫10月25日销量怎么样')
        shop=next(s for s in self.hub.stores if s['id']=='hx-cq-na');shop['aliases'].append('紫玫瑰店')
        task=self.turn('紫玫瑰店的库存呢')['task']
        self.assertEqual(task['parsed']['store_id'],'hx-cq-na')
        self.assertEqual(task['parsed']['sku_id'],'FLR-ROSE-19-BOUQUET')
        self.assertEqual(task['question'],'紫玫瑰店的库存呢')
        self.assertNotIn('紫玫瑰店',task['parsed']['conversation_context']['resolved_question'])

    def test_switch_unvisited_store_never_creates_data_task(self):
        before=self.count();result=self.turn('切到玉林路店')
        self.assertIsNone(result['task']);self.assertEqual(self.count(),before)
        self.assertEqual(self.doc['focus_store'],'hx-cd-yl')
        self.assertEqual(self.doc['turns'][-1]['kind'],'resume')

    def test_switch_with_colloquial_date_runs_query_instead_of_ignoring_date(self):
        self.turn('19红玫10月25日销量怎么样');self.turn('南岸店呢');self.turn('回到南山店')
        task=self.turn('切回南岸店10月24日怎么样')['task']
        self.assertIsNotNone(task);self.assertEqual(task['parsed']['target_date'],'2026-10-24')
        self.assertEqual(task['parsed']['store_id'],'hx-cq-na')

    def test_city_group_and_model_role_selection_use_same_contract(self):
        self.turn('19红玫10月25日销量怎么样')
        result=self.turn('成都所有店对比')
        ids={c['store_id'] for c in result['conversation']['comparison']['children']}
        self.assertEqual(ids,{'hx-cd-yl','hx-cd-cx'})
        self.manager.store_planner=lambda *a:dict(decision='select',action='query',store_ids=['hx-cq-na'],excluded_store_ids=[])
        result=self.turn('南山店店长说想看南岸店的情况')
        self.assertEqual(result['task']['parsed']['store_id'],'hx-cq-na')
        self.assertEqual(result['task']['parsed']['conversation_context']['store_resolution']['source'],'model_validated')

    def test_two_mentions_without_relationship_only_clarify(self):
        self.turn('19红玫10月25日销量怎么样');before=self.count()
        result=self.turn('南山店提到南岸店')
        self.assertIsNone(result['task']);self.assertEqual(before,self.count())
        self.assertEqual(self.doc['waiting'],['store'])

    def test_model_failure_is_not_user_missing_information(self):
        def unavailable(*a):raise TimeoutError()
        self.manager.store_planner=unavailable;before=self.count()
        result=self.turn('南山店店长说南岸店')
        self.assertIsNone(result['task']);self.assertEqual(self.count(),before)
        self.assertEqual(self.doc['waiting'],[])
        self.assertEqual(self.doc['turns'][-1]['store_resolution']['status'],'failed')
        self.assertNotIn('pending_store',self.doc)

    def test_http_confirmation_uses_target_store_not_browser_store_header(self):
        self.ambiguous_alias();server=create_server(self.hub,port=0,token='t'*30)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,payload=None,key='confirm',sid='hx-sz-ns'):
            headers={'Authorization':'Bearer '+'t'*30,'Content-Type':'application/json','Idempotency-Key':key,'X-Store-ID':sid}
            req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        _,created=request('/api/conversations',{'store_id':'hx-sz-ns'})
        cid=created['conversation']['id'];path=f'/api/conversations/{cid}/turns'
        question='中心店10月25日99红玫销量怎么样'
        _,waiting=request(path,{'question':question,'revision':0},'waiting')
        self.assertEqual(waiting['conversation']['waiting'],['store'])
        code,result=request(path,{'question':question,'revision':1,'store_choice':'hx-cq-na'},'confirmed')
        self.assertEqual(code,200);task=result['task']
        self.assertEqual(task['parsed']['store_id'],'hx-cq-na')
        self.assertEqual(request('/api/tasks/'+task['id'])[0],404)
        self.assertEqual(request('/api/tasks/'+task['id'],sid='hx-cq-na')[0],200)

    def test_shop_confirmation_does_not_reuse_another_store_parent(self):
        first=self.turn('19红玫10月25日销量怎么样')['task']
        result=self.turn('南岸店呢')['task']
        self.assertIsNone(result['parent_id'])
        self.assertIsNone(self.hub.for_store('hx-cq-na').get(first['id']))
        self.assertEqual(result['parsed']['conversation_context']['store_resolution']['catalog_version'],self.doc['turns'][-1]['store_resolution']['catalog_version'])


if __name__=='__main__':unittest.main()
