"""回答的事实契约与有限校验，不使用额外模型，也不声称完成通用语义核验。

数字字段断言只证明单个值。这里补充容易写错的集合关系、日期间隔、分级依据和
因果边界；宿主先给出可直接使用的事实，再在finish出口拒绝已覆盖的高风险表述。
未覆盖的同义改写仍可能出错，审计结果必须保留这一限制。
"""
import re
from datetime import date

VERSION='flower-narrative-v1'


def build_contract(analysis,observations):
    """只读当前调查证据；所有日期关系相对目标日，不相对服务器今天。"""
    sales=observations.get('sales',{}).get('data',{})
    target=sales.get('target_date')
    dates={r['date'] for key in ('sales','sales_trend') for r in observations.get(key,{}).get('data',{}).get('records',[]) if r.get('date')}
    relations=[]
    if target:
        for day in sorted(dates):
            distance=(date.fromisoformat(target)-date.fromisoformat(day)).days
            if distance>0:
                relations.append({'date':day,'days_before_target':distance})
    assessment=analysis.get('assessment',{})
    return {'version':VERSION,'target_date':target,'date_relations':relations,
        'order_relation':'支付日批次中，完成、取消、待完成互斥；跨日/迟到取消是取消的子集，不属于完成订单。取消不等于退款。',
        'assessment':{'label':assessment.get('label'),'reasons':assessment.get('reasons',[]),
            'policy':'引用销量分级时，至少原样引用一条宿主理由，不自行编造分级依据。'},
        'conflict_count':len(analysis.get('conflicts',[])),
        'source_domains':'promotions.active只表示促销活动是否生效，不是广告推广开启状态；本调查未读取广告投放。缺货分钟来自inventory，sales_trend为跨来源整合，不是原始销售表字段。',
        'causal_status':analysis.get('causal_root_cause','not_established'),
        'wording_policy':'日期优先写具体日期和相距天数；候选信号不是主因，零缺货记录不足以排除缺货影响；取消与零缺货可同时成立。',
        'validation_scope':'有限关系与措辞检查；不证明所有数字、建议或全文语义正确。'}


def audit_summary(summary,contract):
    """拒绝可确定的违规并给模型纠正依据；不偷偷改写原文，不修改业务分级。"""
    text=re.sub(r'[*`_#]','',summary)
    findings=[]
    def reject(code,message):
        if not any(f['code']==code for f in findings):findings.append({'code':code,'message':message})

    # “完成54单（含迟到取消2单）”单字段均可为真，但集合关系是假的。
    # 有“取消6单，其中...”时最近状态是取消，因此允许该正确表达。
    for match in re.finditer(r'(?:迟到|跨日)取消',text):
        prefix=text[max(0,match.start()-45):match.start()]
        states=list(re.finditer(r'(完成|取消)(?:订单)?\s*\d+(?:\.\d+)?\s*单',prefix))
        tail=prefix[states[-1].end():] if states else ''
        if (states and states[-1][1]=='完成' and len(tail)<=20 and not re.search(r'取消|[。；\n]',tail)
                and re.search(r'含|其中|包括',tail)):
            reject('completed_includes_cancelled',contract['order_relation'])

    # 对明确关联的日期标签做算术核对；未关联具体日期的泛指不臆测其锚点。
    for relation in contract['date_relations']:
        day=date.fromisoformat(relation['date'])
        token=rf'(?:{day.year}[-/])?0?{day.month}[-/]0?{day.day}(?!\d)|{day.month}月0?{day.day}[日号]?'
        for match in re.finditer(r'(上上周|上周)(?:同日|同期|同星期|同一天)?[^。；;\n]{0,16}?(?:'+token+r')',text):
            expected=14 if match[1]=='上上周' else 7
            if relation['days_before_target']!=expected:
                reject('relative_week_mismatch',f"{relation['date']}距目标日{contract['target_date']}为{relation['days_before_target']}天，请使用具体日期与相距天数。")

    if contract['causal_status']=='not_established':
        for match in re.finditer(r'(?:主因|根因|主要原因)(?:更)?(?:可能)?(?:不?是|为|在于)|非(?:缺货|流量|库存|活动|取消)[^。；，\n]{0,6}所致',text):
            prefix=text[max(0,match.start()-14):match.start()]
            if not re.search(r'不能|无法|尚未|尚不能|不足以|未能|是否|核查|核实',prefix):
                reject('unsupported_causality','宿主尚未建立因果。请写观察事实、候选信号与待核查事项，不认定主因或断言排除某原因。')

    assessment=contract['assessment']
    if assessment['label'] and assessment['label'] in text and assessment['reasons']:
        if not any(reason.rstrip('。') in text for reason in assessment['reasons']):
            reject('assessment_reason_missing','销量分级依据必须引用宿主原话：'+' / '.join(assessment['reasons']))
    if contract['conflict_count']==0:
        for match in re.finditer(r'冲突|矛盾',text):
            prefix=text[max(0,match.start()-14):match.start()]
            if not re.search(r'无|没有|不存在|不构成|不能认定|未发现|并非|不直接|尚不能|不$',prefix):
                reject('unsupported_conflict','宿主未发现数据冲突。取消增长与零缺货可同时成立；需要补证据不等于数据冲突。')
    # 字段数值即便正确，挂到错误业务域上也会误导运营；只拦截明确的错误归属。
    if re.search(r'(?:推广|广告|投放)[^。；\n]{0,30}active\s*[=:]\s*false',text,re.I):
        reject('source_domain_confusion',contract['source_domains'])
    if re.search(r'销售(?:记录|来源|表)[^。；\n]{0,12}(?:stockout|缺货分钟|缺货时长)(?:字段)?\s*(?:为|是|=)',text,re.I):
        reject('source_domain_confusion',contract['source_domains'])
    return {'version':VERSION,'status':'rejected' if findings else 'limited_checks_passed',
        'findings':findings,'scope':contract['validation_scope']}
