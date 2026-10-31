"""实际接口验收：原句、真实模型兜底、类别汇总与固定类别历史对比。"""
import json
import time
from verify_compound_queries_live import request


def main():
    doc=request('/api/conversations',{'store_id':'hx-cq-na'})['conversation']
    for question in ['帮我查下父亲节哪类花麦的最好','父亲节花材冠军是谁']:
        response=request(f"/api/conversations/{doc['id']}/turns",{'question':question,'revision':doc['revision']})
        doc=response['conversation'];task=response['task'];assert task is not None
        parsed=task['parsed']
        assert task['status']=='needs_data',(task['status'],parsed['issues'])
        assert parsed['target_date']=='2026-06-21' and parsed['query_plan']['ranking']['group_by']=='variety'
        if question=='父亲节花材冠军是谁':assert parsed['query_plan']['planning']['status']=='validated',parsed['query_plan']
        print(json.dumps({'question':question,'task_id':task['id'],'status':task['status'],'date':parsed['target_date'],
            'group_by':parsed['query_plan']['ranking']['group_by'],'planning':parsed['query_plan'].get('planning'),'issues':parsed['issues']},ensure_ascii=False),flush=True)
    task=request('/api/tasks',{'question':'10月25日哪类花卖得最好，对比上周同一天'})['task'];deadline=time.monotonic()+30
    while task['status'] in ('queued','running') and time.monotonic()<deadline:
        time.sleep(.2);task=request('/api/tasks/'+task['id'])['task']
    assert task['status']=='completed',task['status']
    rank=task['result']['analysis']['ranking'];leader=rank['leaders'][0]
    assert leader['member_skus'] and task['result']['analysis']['selected_comparison']['rows']
    print(json.dumps({'question':'10月25日哪类花卖得最好，对比上周同一天','status':task['status'],
        'leader_category':leader['name'],'category_units':leader['units_sold'],'member_count':len(leader['member_skus'])},ensure_ascii=False))


if __name__=='__main__':main()
