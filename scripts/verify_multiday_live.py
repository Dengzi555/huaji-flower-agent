"""显式本地验收：先检查窗口查询，再运行一条已授权模型调查；后者可能计费。"""
import json
import time
import urllib.request


def request(path,payload=None):
    """固定连接本机深圳南山店，不读取或输出凭据。"""
    req=urllib.request.Request('http://127.0.0.1:8080'+path,headers={'Content-Type':'application/json','X-Store-ID':'hx-sz-ns'},
        data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)


def wait(question):
    """提交后等真实任务终态；不把HTTP成功当调查完成。"""
    task=request('/api/tasks',{'question':question})['task'];until=time.monotonic()+190
    while task['status'] in ('queued','running') and time.monotonic()<until:
        time.sleep(.5);task=request('/api/tasks/'+task['id'])['task']
    return task


def main():
    task=wait('99红玫近7天销量走势');assert task['status']=='completed',task['status']
    state=task['result'];assert state['sales_trend']['coverage']['requested_days']==7
    assert state['model_calls']==0 and state['tool_calls']==3
    print(json.dumps({'case':'recent_window','task_id':task['id'],'coverage':state['sales_trend']['coverage'],'model_calls':state['model_calls']},ensure_ascii=False),flush=True)
    task=wait('99红玫2026-10-11销量下降原因');state=task.get('result') or {}
    print(json.dumps({'case':'calendar_investigation','task_id':task['id'],'status':task['status'],'mode':state.get('mode'),
        'model_calls':state.get('model_calls'),'tool_calls':state.get('tool_calls'),'notes':(state.get('analysis') or {}).get('comparison_notes'),
        'validation':(state.get('validation') or {}).get('status'),'error':state.get('error'),
        'draft_preview':(state.get('model_draft') or {}).get('summary','')[:600]},ensure_ascii=False),flush=True)
    assert task['status']=='completed',state.get('error')
    assert state.get('sales_trend') and state.get('calendar_context')


if __name__=='__main__':main()
