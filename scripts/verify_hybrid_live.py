"""显式运行少量本地HTTP验收，未知表达会消费一次已配置DeepSeek分类请求。"""
import json
import time
import urllib.request


def request(path,payload=None):
    """只连接本机工作台，不读取或打印密钥。"""
    req=urllib.request.Request('http://127.0.0.1:8080'+path,
        data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Store-ID':'hx-sz-ns'})
    with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)


def main():
    """对比规则、歧义和模型分流；仅执行便宜的单日销量查询，不启动销量归因循环。"""
    for label,question in [('clear','99红玫2026-10-25销量怎么样'),('ambiguous','99红玫2026-10-25销量下降，顺便查库存'),('model','99红玫2026-10-25出了几束')]:
        task=request('/api/tasks',{'question':question})['task']
        until=time.monotonic()+10
        while task['status'] in ('queued','running') and time.monotonic()<until:
            time.sleep(.1);task=request('/api/tasks/'+task['id'])['task']
        route=task['parsed']['routing']
        if label=='clear':assert route['model_calls']==0 and task['status']=='completed'
        if label=='ambiguous':assert route['model_calls']==0 and task['status']=='needs_clarification' and task['result'] is None
        if label=='model':assert route['model_calls']==1 and route['tier'] in ('model','model_fallback')
        print(json.dumps({'case':label,'task_id':task['id'],'status':task['status'],'route':route,
            'execution_tools':(task['result'] or {}).get('tool_calls',0),'execution_model_calls':(task['result'] or {}).get('model_calls',0)},ensure_ascii=False))


if __name__=='__main__':main()
