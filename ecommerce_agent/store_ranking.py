"""门店排名：把“哪个店”编译为各店同口径取证，再用代码排序，不让模型编造名次。"""
import re
from .store_resolution import STORE_QUESTION_PATTERN


def compile_request(question):
    """输入原问题，输出只读销量查询副本与门店排序契约；原话另行保留。"""
    if re.search(r'取消|退款|库存|缺货|活动|促销|广告|推广|转化率|营业额|销售额|利润|毛利|下降|下滑|增长|变化',question):
        raise ValueError('门店排名当前按成品支付件数统计；该指标尚未接入门店排名，请明确查询销量，或分别查询各店变化。')
    low=bool(re.search(r'最差|最少|最低|垫底',question));high=bool(re.search(r'最好|最多|最高|第一',question))
    spec={'metric':'units_sold','direction':'both' if low and high else 'asc' if low else 'desc',
        'assumption':'门店最好/最差默认按指定商品范围内的成品支付件数排序，不代表利润或整店健康。'}
    text=re.sub(STORE_QUESTION_PATTERN,' ',question)
    text=re.sub(r'最好|最差|最多|最少|最高|最低|第一|垫底|排名|排行(?:榜)?',' ',text)
    # 全店是商品默认范围；明确花材/规格仍由目录过滤，不能删去用户给出的商品和日期。
    return text+' 全店 销量怎么样',spec


def summarize(view,spec):
    """仅对覆盖完整的门店排序；零销量参与、缺失排除，并列名次全部保留。"""
    eligible=[r for r in view['rows'] if r['comparable'] and r['metrics'].get('units') is not None]
    partitions=[]
    for direction in (['desc','asc'] if spec['direction']=='both' else [spec['direction']]):
        ordered=sorted(eligible,key=lambda r:(r['metrics']['units'] if direction=='asc' else -r['metrics']['units'],r['store_id']))
        leaders=[{'store_id':r['store_id'],'store_name':r['store_name'],'units_sold':r['metrics']['units']} for r in ordered if r['metrics']['units']==ordered[0]['metrics']['units']] if ordered else []
        partitions.append({'direction':direction,'leaders':leaders})
    first_direction='asc' if spec['direction']=='asc' else 'desc'
    ordered=sorted(eligible,key=lambda r:(r['metrics']['units'] if first_direction=='asc' else -r['metrics']['units'],r['store_id']))
    for row in ordered:
        row['store_rank']=1+sum((r['metrics']['units']<row['metrics']['units']) if first_direction=='asc' else (r['metrics']['units']>row['metrics']['units']) for r in eligible)
    view['rows']=ordered+[r for r in view['rows'] if r not in eligible]
    view['store_ranking']={**spec,'partitions':partitions,'complete_scope':len(eligible)==len(view['rows']),
        'excluded_store_ids':[r['store_id'] for r in view['rows'] if not r['comparable']]}
    statements=[]
    for part in partitions:
        if part['leaders']:
            statements.append(('销量最低' if part['direction']=='asc' else '销量最高')+'：'+'、'.join(r['store_name'] for r in part['leaders'])+f"，{part['leaders'][0]['units_sold']}件"+('（并列）' if len(part['leaders'])>1 else ''))
    view['summary']=f"按成品支付件数比较门店，{len(eligible)}/{len(view['rows'])}家数据完整。"+('；'.join(statements)+'。' if statements else '尚不能判断哪家最好或最差。')
    if len(eligible)!=len(view['rows']):view['summary']+='缺数或失败门店未参与，当前不是全部门店的完整排名。'
    return view
