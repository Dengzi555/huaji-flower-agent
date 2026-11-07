"""统一能力契约：规划器看到能力说明，执行器按同一白名单复核，失败时不自动扩散分支。"""
import copy
import re
from datetime import date

CONTRACT_VERSION='flower-capabilities-v1'
LIMITS={'max_steps':8,'max_days':60,'max_products':500,'max_rank_limit':10}
CAPABILITIES={
    'combined_read':{'intents':['query_combined'],'scopes':['single','group'],'requires':['frozen_product_and_time_scope'],
        'produces':['独立事项结果','部分失败与数据缺口','销量曲线','原始工具证据'],
        'tools':['get_product_group_sales','get_inventory_range','get_promotion_range'],'parameters':[],
        'failure_policy':'最多三个独立工具并发；主线程统一汇总，失败不丢弃其他结果，取消后拒绝迟到结果'},
    'advertising_advice':{'intents':['query_advertising'],'scopes':['store'],
        'requires':['local_operating_settings'],'produces':['同店证据引用','本地关注线','只读配置建议'],
        'tools':['get_advertising_settings'],'parameters':[],'entrypoint':'conversation',
        'failure_policy':'不要求新日期；只沿用同店冻结证据；模型最多一次选择白名单建议，失败使用基础建议，不写平台配置'},
    'advertising_summary':{'intents':['query_advertising'],'scopes':['store'],
        'requires':['advertising_hourly_ledger'],'produces':['推广扣费','广告点击/CPC','费用率','小时关注项','缺失日期'],
        'tools':['get_advertising_report'],'parameters':[],'entrypoint':'conversation',
        'failure_policy':'独立推广注册表执行；固定门店/日期/模式；无SKU归因，不计算ROI/利润，缺失不补零'},
    'sales_summary':{'intents':['query_sales_performance'],'scopes':['single','group'],
        'requires':['sales'],'produces':['支付件数','商品分摊金额','数据覆盖'],
        'tools':['get_sales_metrics','get_product_group_sales'],'parameters':[],
        'failure_policy':'缺失保留未知；部分覆盖不能冒充完整期间总计'},
    'rank_products':{'intents':['query_sales_performance'],'scopes':['group'],
        'requires':['sales_summary'],'produces':['SKU/品种/类型/系列支付件数排名','并列入选项','排除项'],
        'tools':['get_product_group_sales'],'parameters':['metric','direction'],
        'failure_policy':'只在完整记录中排名，公开覆盖不足；不替换为利润或转化率'},
    'compare_selected':{'intents':['query_sales_performance'],'scopes':['group'],
        'requires':['rank_products'],'produces':['同SKU基线差额','变化率','独立曲线'],
        'tools':['get_product_group_sales'],'parameters':['baseline_date','selection'],
        'failure_policy':'固定入选SKU；历史缺失为未知，零基线不计算变化率'},
    'compare_scope':{'intents':['query_sales_performance'],'scopes':['single','group'],
        'requires':['sales_summary'],'produces':['范围销量基线差额','变化率'],
        'tools':['get_sales_trend','get_product_group_sales'],'parameters':['baseline_date','selection'],
        'failure_policy':'只比较同范围末日；不把日对比包装为窗口环比'},
    'investigate_sales_change':{'intents':['investigate_sales_change'],'scopes':['single'],
        'requires':['sales','traffic','inventory','promotions'],'produces':['事实','候选线索','缺口/冲突','关注判断','排查顺序'],
        'tools':['get_sales_metrics','get_traffic_metrics','get_inventory_status','get_promotion_history','get_sales_trend','get_calendar_context','get_order_funnel','get_channel_breakdown','analyze_metrics','finish'],
        'parameters':[],'failure_policy':'保留已读证据；冲突先核对，不把相关性认定为根因'},
    'inventory_status':{'intents':['query_inventory'],'scopes':['single'],'requires':['inventory'],
        'produces':['缺货分钟','监测分钟'],'tools':['get_inventory_status'],'parameters':[],
        'failure_policy':'监测不全不推断全天；无实时库存件数'},
    'promotion_status':{'intents':['query_promotions'],'scopes':['single'],'requires':['promotions'],
        'produces':['日级活动状态','活动名称'],'tools':['get_promotion_history'],'parameters':[],
        'failure_policy':'日级状态不证明活动效果'},
}
UNCONNECTED_DOMAINS=[
    {'code':'delivery_evidence','pattern':r'配送范围|配送半径|预计送达|配送时长|配送费',
        'message':'对话分析尚未接入配送范围和时效数据，不能据此判断配送影响；可先查销量变化，补充配送配置及订单时效后再核查。',
        'required_data':['配送配置历史','订单承诺与实际送达时间']},
    {'code':'review_evidence','pattern':r'差评|评价|评分',
        'message':'对话分析尚未接入评价明细，不能把销量变化归因于差评；需要评价时间、商品及订单关联数据。',
        'required_data':['评价明细','评价关联订单/SKU与时间']},
    {'code':'advertising_evidence','pattern':r'推广费|广告费|投产比|ROAS|ROI|点击成本|推广扣费',
        'message':'推广账本请通过对话推广专项查询扣费、点击与费用率；销量调查不能代答推广效果，当前没有广告归因数据，不计算ROI/ROAS。',
        'required_data':['小时投放扣费和点击','同口径成交/取消订单','广告归因口径']},
]


def catalog():
    """返回可展示给模型/流程页的契约副本，调用方不能修改服务端授权定义。"""
    return {'version':CONTRACT_VERSION,'limits':dict(LIMITS),'operations':copy.deepcopy(CAPABILITIES)}


def question_boundaries(question,rank_requested=False):
    """提前检查已知未接入的数据域；不是声称能识别全部中文意图，未知表达仍走澄清。"""
    issues=[]
    for domain in UNCONNECTED_DOMAINS:
        if re.search(domain['pattern'],question,re.I):issues.append({k:copy.deepcopy(v) for k,v in domain.items() if k!='pattern'})
    if rank_requested and re.search(r'按(?:营业额|销售额|金额|利润|转化率|客单价)|(?:利润|营业额|销售额|转化率|客单价).*(?:最|排名|排行)',question):
        issues.append({'code':'unsupported_ranking_metric','message':'当前商品排名支持成品支付件数；利润、转化率或金额排名尚未接入相应口径，请确认是否改按销量。',
            'required_data':['对应指标的同口径商品明细']})
    return issues


def bind_contract(task):
    """根据已解析意图生成执行契约；参数仍取冻结任务，不接受模型新增工具名。"""
    plan=task['query_plan'];intent=task['intent']
    if intent!='query_sales_performance':
        operation={'investigate_sales_change':'investigate_sales_change','query_inventory':'inventory_status','query_promotions':'promotion_status'}[intent]
        plan.update(operation=operation,ranking=None,metric=None,steps=[{'id':'query','operation':operation,'depends_on':[]}])
        plan['start_date']=task['trend_start'] if intent=='investigate_sales_change' else task['target_date']
    # 只有一个明确SKU时无需排名；不伪造“在全店第一”，也不扩大成全店查询。
    elif not task.get('product_scope'):
        plan['ranking']=None
        plan['steps']=[step for step in plan['steps'] if step['operation']!='rank_products']
        for step in plan['steps']:
            if step['operation']=='compare_selected':step.update(operation='compare_scope',depends_on=['summary'],selection='current_scope')
    plan['contract_version']=CONTRACT_VERSION
    task['capability_contract']={'version':CONTRACT_VERSION,'operations':[step['operation'] for step in plan['steps']],
        'scope':'group' if task.get('product_scope') else 'single','failure_policy':'stop_and_explain','read_only':True}


def validate_plan(task,store=None):
    """执行前重新检查DAG、参数、意图、门店SKU和范围；返回真实所需工具清单。"""
    if task.get('intent')=='query_combined':
        from .combined_query import validate_combined
        return validate_combined(task,store)
    plan=task.get('query_plan')
    if not isinstance(plan,dict) or plan.get('contract_version')!=CONTRACT_VERSION:raise ValueError('查询能力契约缺失或版本已变化，请重新提交问题。')
    allowed={'version','tier','model_calls','operation','ranking','metric','start_date','end_date','steps','contract_version','planning'}
    if set(plan)-allowed:raise ValueError('查询计划存在未支持字段。')
    expected_start=task['trend_start'] if task.get('is_window') or task['intent']=='investigate_sales_change' else task['target_date']
    if (plan.get('start_date'),plan.get('end_date'))!=(expected_start,task['target_date']):raise ValueError('查询计划日期与冻结任务不一致。')
    if task.get('business_today') and task['target_date']>=task['business_today']:raise ValueError('不支持未结束自然日的整日查询。')
    steps=plan.get('steps');group=task.get('product_scope');scope='group' if group else 'single';seen={};operations=[]
    if not isinstance(steps,list) or not 1<=len(steps)<=LIMITS['max_steps']:raise ValueError('查询计划步骤数超出能力限制。')
    for step in steps:
        if not isinstance(step,dict):raise ValueError('查询步骤必须是结构化对象。')
        op=step.get('operation');spec=CAPABILITIES.get(op) if isinstance(op,str) else None;identity=step.get('id');deps=step.get('depends_on')
        if not spec or task.get('intent') not in spec['intents'] or scope not in spec['scopes']:raise ValueError('查询计划包含未支持或不适用于当前范围的能力。')
        if set(step)!={'id','operation','depends_on',*spec['parameters']}:raise ValueError('能力参数不符合白名单，禁止SQL、代码或自行指定工具。')
        if not isinstance(identity,str) or not identity or identity in seen or not isinstance(deps,list) or any(not isinstance(dep,str) for dep in deps) or len(set(deps))!=len(deps) or any(dep not in seen for dep in deps):raise ValueError('查询步骤存在重复、循环或尚未完成的依赖。')
        required=[req for req in spec['requires'] if req in CAPABILITIES]
        if [seen[dep] for dep in deps]!=required:raise ValueError('查询步骤依赖与能力契约不一致。')
        if op=='rank_products' and (step['metric']!='units_sold' or step['direction'] not in ('asc','desc','both')):raise ValueError('排名只支持成品支付件数及最高/最低/双端排序。')
        if op.startswith('compare_'):
            if step['baseline_date']!=task.get('comparison_date') or not step['baseline_date']:raise ValueError('计划基线与冻结任务不一致。')
            if step['selection']!=('all_tied_leaders' if op=='compare_selected' else 'current_scope'):raise ValueError('不能更换历史对比的入选商品。')
        seen[identity]=op;operations.append(op)
    if len(set(operations))!=len(operations):raise ValueError('同一能力不能在本次计划中重复执行。')
    if plan.get('operation')!=operations[0]:raise ValueError('查询入口与实际执行能力不一致。')
    if group:
        ids=group.get('sku_ids');rank=group.get('ranking')
        if group.get('summary_start')!=plan['start_date'] or rank!=plan.get('ranking'):raise ValueError('集合汇总窗口或排名与计划不一致。')
        if not isinstance(ids,list) or not 1<=len(ids)<=LIMITS['max_products'] or any(not isinstance(sku,str) for sku in ids) or len(set(ids))!=len(ids):raise ValueError('商品集合超出能力范围。')
        if store and any(sku not in store.skus for sku in ids):raise ValueError('集合商品不属于当前门店。')
        if bool(rank)!=('rank_products' in operations) or bool(group.get('compare_selected'))!=('compare_selected' in operations):raise ValueError('集合工具参数与查询计划不一致。')
        if rank and (rank.get('metric')!='units_sold' or type(rank.get('limit')) is not int or not 1<=rank['limit']<=LIMITS['max_rank_limit'] or rank['direction']!=next(step['direction'] for step in steps if step['operation']=='rank_products')):raise ValueError('集合排名参数超出契约。')
        if rank and rank.get('group_by','sku') not in ('sku','variety','product_type','series'):raise ValueError('类别汇总维度超出能力契约。')
    elif store and task.get('sku_id') not in store.skus:raise ValueError('商品不属于当前门店。')
    if task.get('include_trend'):
        a=date.fromisoformat(task['trend_start']);b=date.fromisoformat(task['trend_end'])
        if not 0<=(b-a).days<LIMITS['max_days'] or b.isoformat()!=task['target_date']:raise ValueError('日期窗口超出能力限制。')
    intent=task['intent']
    if intent=='investigate_sales_change':return list(CAPABILITIES['investigate_sales_change']['tools'])
    if group:return ['get_product_group_sales']
    tools={'query_sales_performance':['get_sales_metrics'],'query_inventory':['get_inventory_status'],'query_promotions':['get_promotion_history']}[intent]
    return tools+(['get_sales_trend','get_calendar_context'] if task.get('include_trend') else [])
