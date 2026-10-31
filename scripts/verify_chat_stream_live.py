"""实际SSE验收：自动建议与手动模型模式都应返回供应商增量，最后提交会话。"""
import json
import urllib.request
import uuid
from verify_compound_queries_live import request

def main():
    doc=request('/api/conversations',{'store_id':'hx-cq-na'})['conversation']
    for mode,question in [('auto','万圣节你觉得什么品种的花束卖得好？'),('model','用一句话介绍你自己')]:
        payload={'question':question,'revision':doc['revision'],'mode':mode}
        req=urllib.request.Request('http://127.0.0.1:8080/api/conversations/'+doc['id']+'/turns?stream=1',
            data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Idempotency-Key':uuid.uuid4().hex,'X-Store-ID':'hx-cq-na'})
        chunks=[];final=None
        with urllib.request.urlopen(req,timeout=40) as response:
            for line in response:
                if not line.startswith(b'data:'):continue
                event=json.loads(line[5:])
                if event['type']=='delta':chunks.append(event['text'])
                if event['type']=='done':final=event['result']
                assert event['type']!='error',event
        assert chunks and final and final['task'] is None
        doc=final['conversation'];turn=doc['turns'][-1]
        assert ''.join(chunks)==turn['message'] and turn['chat']['mode']=='model'
        print(json.dumps({'mode':mode,'delta_count':len(chunks),'reply':turn['message']},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
