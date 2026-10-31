"""本地实际对话验收：规则问候、一次真实普通对话、再回到经营查询。"""
import json
import time
from verify_compound_queries_live import request


def main():
    doc=request('/api/conversations',{'store_id':'hx-cq-na'})['conversation']
    for question in ['你好','请用一句话介绍你自己']:
        reply=request(f"/api/conversations/{doc['id']}/turns",{'question':question,'revision':doc['revision']})
        doc=reply['conversation'];turn=doc['turns'][-1]
        assert reply['task'] is None and turn['kind']=='chat' and not doc['stores']
        print(json.dumps({'question':question,'reply':turn['message'],'mode':turn['chat']['mode'],
            'model_calls':turn['chat']['model_calls']},ensure_ascii=False),flush=True)
        if question=='你好':assert turn['chat']['model_calls']==0
        else:assert turn['chat']['mode']=='model','普通对话返回兜底，需要检查模型配置或连接'
    reply=request(f"/api/conversations/{doc['id']}/turns",{'question':'99红玫10月25日销量怎么样','revision':doc['revision']})
    doc=reply['conversation'];task=reply['task'];deadline=time.monotonic()+30
    while task['status'] in ('queued','running') and time.monotonic()<deadline:
        time.sleep(.2);task=request('/api/tasks/'+task['id'])['task']
    assert task['status']=='completed'
    scope=doc['stores']
    reply=request(f"/api/conversations/{doc['id']}/turns",{'question':'谢谢','revision':doc['revision']})
    assert reply['task'] is None and reply['conversation']['stores']==scope
    print(json.dumps({'business_query':task['status'],'sku':task['parsed']['sku_id'],'greeting_preserves_scope':True},ensure_ascii=False))


if __name__=='__main__':main()
