"""本地接口验收：组合计划、同款对比、连续筛选与门店纠正；不读取或输出模型密钥。"""
import json
import time
import urllib.request
import uuid


def request(path,payload=None,store='hx-cq-na'):
    """所有数据和任务始终通过工作台接口，验证真实入队和执行路径。"""
    req=urllib.request.Request('http://127.0.0.1:8080'+path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','X-Store-ID':store,'Idempotency-Key':uuid.uuid4().hex})
    with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)


def main():
    """初始查询用明确日期，避免样例历史截止日与运行日不同导致误判测试失败。"""
    doc=request('/api/conversations',{'store_id':'hx-cq-na'})['conversation']
    for question in ['南岸店10月25日什么买的最好，比上周同一天怎么样','那前天呢','只看抱抱桶','不是南岸店，是南山店']:
        response=request(f"/api/conversations/{doc['id']}/turns",{'question':question,'revision':doc['revision']})
        doc=response['conversation'];task=response['task'];assert task is not None,response
        store=task['parsed']['store_id'];deadline=time.monotonic()+40
        while task['status'] in ('queued','running') and time.monotonic()<deadline:
            time.sleep(.25);task=request('/api/tasks/'+task['id'],store=store)['task']
        assert task['status']=='completed',(question,task['status'],task['parsed']['issues'])
        state=task['result'];assert state['validation']['status']=='passed',state['validation']
        assert state['analysis'].get('selected_comparison'),question
        if question=='只看抱抱桶':assert all(p['product_type']=='抱抱桶' for p in task['parsed']['candidates'])
        if '不是南岸' in question:assert store=='hx-sz-ns'
        print(json.dumps({'question':question,'task_id':task['id'],'status':task['status'],'store':store,
            'date':task['parsed']['target_date'],'baseline':task['parsed']['comparison_date'],
            'steps':task['parsed']['query_plan']['steps'],'compared_skus':[r['sku_id'] for r in state['analysis']['selected_comparison']['rows']],
            'model_calls':state['model_calls'],'tool_calls':state['tool_calls']},ensure_ascii=False),flush=True)
    flow=request('/api/flow');assert not flow.get('restart_required'),flow.get('restart_required')


if __name__=='__main__':main()
