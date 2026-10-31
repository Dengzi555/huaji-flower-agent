"""本机实际接口验收：用户原话→错字确认→集合查数；可选真实模型参数规划。"""
import json
import time
import urllib.request
import uuid


def request(path,payload=None):
    """固定本地南岸店，不读取/输出密钥；用户原话内的门店仍由对话层识别。"""
    req=urllib.request.Request('http://127.0.0.1:8080'+path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','X-Store-ID':'hx-cq-na','Idempotency-Key':uuid.uuid4().hex})
    with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)


def wait(task):
    """查询到终态，检查工具真实返回，不能只看提交HTTP成功。"""
    until=time.monotonic()+40
    while task['status'] in ('queued','running') and time.monotonic()<until:
        time.sleep(.25);task=request('/api/tasks/'+task['id'])['task']
    return task


def main():
    doc=request('/api/conversations',{'store_id':'hx-sz-ns'})['conversation']
    original='南岸店情人节红梅买的怎么样？'
    response=request(f"/api/conversations/{doc['id']}/turns",{'question':original,'revision':doc['revision'],'store_id':'hx-sz-ns'})
    doc=response['conversation'];task=response['task'];parsed=task['parsed']
    assert doc['focus_store']=='hx-cq-na' and parsed['target_date']=='2026-02-14'
    assert parsed['group_candidate'] and task['status']=='needs_clarification'
    assert not any('补充目标日期' in issue or '多个 SKU' in issue for issue in parsed['issues'])
    response=request(f"/api/conversations/{doc['id']}/turns",{'question':original,'revision':doc['revision'],
        'store_id':'hx-cq-na','sku':'__all_matched__','intent':'query_sales_performance'})
    task=response['task'];assert task['status']=='needs_data'
    print(json.dumps({'case':'original_and_confirmation','status':task['status'],'date':task['parsed']['target_date'],
        'scope':task['parsed']['product_scope']['label'],'issues':task['parsed']['issues']},ensure_ascii=False),flush=True)
    for question,direction in [('昨天什么买的最好','desc'),('昨天什么买的最差','asc')]:
        task=wait(request('/api/tasks',{'question':question})['task']);state=task.get('result') or {}
        assert task['status']=='completed',state.get('error')
        rank=state['analysis']['ranking'];assert rank['direction']==direction and rank['complete_scope']
        assert state['validation']['status']=='passed' and state['model_calls']==0
        print(json.dumps({'case':question,'task':task['id'],'status':task['status'],'tool_calls':state['tool_calls'],
            'leaders':[{'name':r['name'],'units':r['units_sold']} for r in rank['leaders']]},ensure_ascii=False),flush=True)
    task=request('/api/tasks',{'question':'这次教师节向日葵买了多少，哪一款最好？'})['task']
    assert task['status']=='needs_data' and task['parsed']['target_date']=='2026-09-10'
    print(json.dumps({'case':'teachers_day','status':task['status'],'issues':task['parsed']['issues']},ensure_ascii=False),flush=True)
    # 该表达不在精确规则中，应走一次分类+受限参数规划，不是新增专用业务分支。
    task=wait(request('/api/tasks',{'question':'南岸店昨天销冠是哪款？'})['task']);state=task.get('result') or {}
    print(json.dumps({'case':'semantic_plan','task':task['id'],'status':task['status'],'plan':task['parsed'].get('query_plan'),
        'tool_calls':state.get('tool_calls'),'error':state.get('error')},ensure_ascii=False),flush=True)
    assert task['status']=='completed'
    assert task['parsed']['query_plan'].get('planning',{}).get('status')=='validated'


if __name__=='__main__':main()
