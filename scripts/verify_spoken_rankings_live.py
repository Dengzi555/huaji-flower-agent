"""截图原句实际接口验收：口语时间、双端排名、缺一天仍给带覆盖说明的参考答案。"""
import json
import time
from verify_compound_queries_live import request


def main():
    doc=request('/api/conversations',{'store_id':'hx-cq-na'})['conversation']
    for question in ['分析下近七天，哪款商品销量最好，哪款最差？','只看抱抱桶','最近一个星期哪款商品销量最好']:
        response=request(f"/api/conversations/{doc['id']}/turns",{'question':question,'revision':doc['revision']})
        doc=response['conversation'];task=response['task'];assert task is not None
        deadline=time.monotonic()+30
        while task['status'] in ('queued','running') and time.monotonic()<deadline:
            time.sleep(.2);task=request('/api/tasks/'+task['id'])['task']
        assert task['status']=='completed',(task['status'],task['parsed']['issues'])
        result=task['result'];rank=result['analysis']['ranking']
        assert rank['leaders'] and result['validation']['status']=='passed'
        if question!='最近一个星期哪款商品销量最好':assert rank['direction']=='both'
        print(json.dumps({'question':question,'task_id':task['id'],'status':task['status'],
            'window':[task['parsed']['trend_start'],task['parsed']['trend_end']],
            'coverage':rank['coverage_note'],'results':[{'direction':p['direction'],
                'leaders':[{'name':r['name'],'units':r['units_sold']} for r in p['leaders']]} for p in rank['partitions']]},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
