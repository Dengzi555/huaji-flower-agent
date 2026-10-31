"""实际工作台验收：结构化经营回答、能力外请求整单停止、流程页契约同步。"""
import json
import time
from verify_compound_queries_live import request


def main():
    """用固定历史日验证接口，避免把当前缺少昨天记录误当成执行失败。"""
    for question in ['南岸店10月25日什么卖得最好，比上周同一天怎么样','99红玫10月25日缺货多久']:
        task=request('/api/tasks',{'question':question})['task'];deadline=time.monotonic()+30
        while task['status'] in ('queued','running') and time.monotonic()<deadline:
            time.sleep(.2);task=request('/api/tasks/'+task['id'])['task']
        assert task['status']=='completed',(task['status'],task['parsed']['issues'])
        guidance=task['result']['analysis']['guidance']
        assert all(field in guidance for field in ['observed','supported_signals','uncertainties','next_checks'])
        assert task['parsed']['capability_contract']['read_only']
        print(json.dumps({'question':question,'task_id':task['id'],'status':task['status'],
            'operations':task['parsed']['capability_contract']['operations'],'observed_count':len(guidance['observed']),
            'causal_status':guidance['causal_status']},ensure_ascii=False),flush=True)
    for question in ['昨天红玫瑰销量下降和差评有关吗','99红玫昨天销量与预计送达有什么关系','昨天全店什么最好，再分析推广费效果']:
        task=request('/api/tasks',{'question':question})['task']
        assert task['status']=='unsupported' and not task.get('result'),task['status']
        assert task['parsed']['capability_boundaries']
        print(json.dumps({'question':question,'status':task['status'],'issues':task['parsed']['issues']},ensure_ascii=False),flush=True)
    flow=request('/api/flow')
    assert not flow['restart_required'];assert len(flow['capabilities']['operations'])==7
    print(json.dumps({'flow_contract':flow['capabilities']['version'],'capability_count':len(flow['capabilities']['operations'])},ensure_ascii=False))


if __name__=='__main__':main()
