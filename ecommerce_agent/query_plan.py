"""受限经营查询计划：自然语言映射到白名单参数，不允许模型生成SQL或任意工具。"""
import re
from copy import deepcopy
from datetime import date,timedelta
from .natural_dates import normalize_dates,spoken_number
from .capabilities import catalog,LIMITS,question_boundaries

RANK_PATTERN=r'最好|最差|最多|最少|最高|最低|排行|排名|哪(?:一)?款.*好|哪(?:一)?款.*差'


def ranking_limit(question):
    """规则和模型共用展示数量；只有原文明说前N名或Top N时更改默认5条。"""
    match=re.search(r'前\s*([零〇一二两三四五六七八九十百\d]+)\s*(?:名|款|个)|top\s*(\d{1,2})',question,re.I)
    return spoken_number(next(value for value in match.groups() if value)) if match else 5


def grouping(question):
    """返回目录分组维度；“哪类花”公开按花材品种解释，不能混作单SKU冠军。"""
    if re.search(r'按(?:商品)?类型|哪(?:种|个)类型|花束和抱抱桶',question):return 'product_type'
    if re.search(r'按系列|哪(?:个|种)系列',question):return 'series'
    if re.search(r'哪(?:类|种)花|什么花|按品种|按花材|花材冠军',question):return 'variety'
    return 'sku'


def ranking(question):
    """只声明排序操作和口径，不在这里取数；无指标时以成品支付件数排序并公开假设。"""
    if not re.search(RANK_PATTERN,question):return None
    dimension=grouping(question)
    note={'sku':'按单款商品','variety':'按目录花材品种汇总（“哪类花”的默认口径）','product_type':'按商品类型汇总','series':'按目录系列汇总'}[dimension]
    low=bool(re.search(r'最差|最少|最低|哪(?:一)?款.*差',question))
    high=bool(re.search(r'最好|最多|最高|哪(?:一)?款.*好',question))
    return {'metric':'units_sold','group_by':dimension,'direction':'both' if low and high else 'asc' if low else 'desc',
        'limit':ranking_limit(question),'views':['summary','ranking'],'assumption':note+'，最好/最差默认按成品支付件数排序，包含有记录的零销量商品；不等于利润或广告效率排名。'}


def global_scope(question):
    """显式全店排名表达；不会把找不到的花名一律回退为全店。"""
    return grouping(question)!='sku' or bool(re.search(r'全店|所有商品|全部商品|(?:什么|哪个|哪(?:一)?款|哪些)(?:商品|花|产品)?(?:销量|销售量|[卖买](?:得|的)?)?最(?:好|差|多|少|高|低)',question))


def sales_steps(question, scope):
    """把汇总、排名和入选商品对比分解成有依赖的步骤；不让模型临时编造执行代码。"""
    rank=ranking(question)
    compare=bool(re.search(r'相比|对比|比较|比(?:上周|前周|昨天|前天|\d{4})',question))
    steps=[{'id':'summary','operation':'sales_summary','depends_on':[]}]
    if rank:steps.append({'id':'ranking','operation':'rank_products','depends_on':['summary'],'metric':'units_sold','direction':rank['direction']})
    if compare:
        steps.append({'id':'comparison','operation':'compare_selected' if rank else 'compare_scope',
            'depends_on':['ranking' if rank else 'summary'],'baseline_date':scope.get('comparison_date'),
            'selection':'all_tied_leaders' if rank else 'current_scope'})
    return steps


def resolve_time_conditions(question,today,query_context=None):
    """独立提取时间条件，区分已确定、模糊、无效和待语义理解，保留解析说明。"""
    from .task_parser import parse_dates
    from .time_scope import resolve_scope
    normalized,notes,errors=normalize_dates(question,today)
    scope=resolve_scope(normalized,today,parse_dates)
    if query_context:scope=query_context.resolve_time(scope,today)
    if errors or any(not e.startswith('请补充目标日期') for e in scope['errors']):
        status='invalid'
    elif scope.get('target_date'):
        status='resolved'
    elif re.search(r'期间|前后|最近(?!\s*\d)',normalized):
        status='ambiguous'
    else:
        status='unresolved'
    return {'normalized':normalized,'notes':notes,'errors':errors,'scope':scope,'status':status}


def plan_from_model(question,catalog,today,planner,default_intent='query_sales_performance',intent_candidates=None,allow_default_all_products=False,query_context=None):
    """最多调用一次规划；返回已校验的QueryContext及回执，直接送入公共任务编译器。"""
    # 不再用“日期词是否在正则名单中”拦截模型；模型可以建议解析，但执行日期仍由宿主复核。
    if question_boundaries(question,bool(ranking(question))):raise ValueError('问题包含尚未接入的业务能力，不能由模型改写后执行。')
    selectors=sorted({str(p.get(key)) for p in catalog.products.values() for key in ('variety','series','short_name') if p.get(key)})
    temporal=resolve_time_conditions(question,today,query_context)
    resolved_scope=temporal['scope']
    resolution=query_context.resolve_product(catalog) if query_context and query_context.product else catalog.resolve(question,today=today)
    matched_ids=sorted(p['sku_id'] for p in resolution['candidates'])
    # 保留宿主已识别的商品身份，避免模型只能选“红玫瑰”而丢失99朵等硬条件。
    allowed_selectors=['__all__',*selectors,*matched_ids]+(['__matched__'] if matched_ids else [])
    proposal=planner(question,{'business_today':today.isoformat(),'selectors':allowed_selectors,
        'metrics':['units_sold'],'max_days':LIMITS['max_days'],'default_rank_limit':5,
        'resolved_time':resolved_scope,'time_status':temporal['status'],'group_by':['sku','variety','product_type','series'],
        'product_scope_policy':'all_if_omitted' if allow_default_all_products else 'explicit_only',
        'resolved_scope':{'all_products':global_scope(question) and not resolution['candidates'],
            'group_by':grouping(question),'matched_product_count':len(resolution['candidates']),
            'sku_ids':matched_ids,'constraints':resolution.get('constraints',{}),
            'selector_policy':'优先用__matched__保留已匹配的全部商品；不能任意缩小或扩大范围。'},
        'default_intent':default_intent,'intents':['query_sales_performance','investigate_sales_change','query_inventory','query_promotions'],
        'intent_retrieval':intent_candidates or {'status':'not_requested','candidates':[]},
        'capabilities':business_capability_context()})
    receipt={'tier':'model','model_calls':1,'status':'clarification','usage':proposal.get('usage') if isinstance(proposal,dict) else None,
        'model':proposal.get('model') if isinstance(proposal,dict) else None}
    fields={'decision','selector','start_date','end_date','direction','limit','metric'}
    if not isinstance(proposal,dict) or not fields.issubset(proposal) or set(proposal)-fields-{'usage','model','group_by','intent','missing_fields','time_expression'}:raise ValueError('查询计划字段不完整或含不支持字段。')
    missing=proposal.get('missing_fields',[])
    if not isinstance(missing,list) or any(v not in ('product','date','intent') for v in missing):raise ValueError('缺失条件字段无效。')
    if proposal['decision'] not in ('select','clarify'):raise ValueError('查询计划决策无效。')
    # 真正的时间缺口可以由用户补充；模型服务错误不进入这条分支。
    if temporal['status'] in ('ambiguous','invalid'):
        receipt.update(missing_fields=['date'],temporal=temporal)
        return None,receipt
    if proposal['decision']=='clarify':
        if temporal['status']=='resolved':missing=[v for v in missing if v!='date']
        if matched_ids:missing=[v for v in missing if v!='product']
        receipt.update(missing_fields=missing or (['date'] if temporal['status']=='unresolved' else ['intent']),temporal=temporal)
        return None,receipt
    intent=proposal.get('intent',default_intent)
    if intent not in ('query_sales_performance','investigate_sales_change','query_inventory','query_promotions'):raise ValueError('计划包含不支持的意图。')
    dimension=proposal.get('group_by','sku')
    if dimension not in ('sku','variety','product_type','series'):raise ValueError('不支持该分组维度。')
    if grouping(question)!='sku' and dimension!=grouping(question):raise ValueError('模型改变了原问题的类别汇总维度。')
    if proposal['selector'] not in allowed_selectors or proposal['metric']!='units_sold' or proposal['direction'] not in ('asc','desc','both','none') or type(proposal['limit']) is not int or not 1<=proposal['limit']<=LIMITS['max_rank_limit']:
        raise ValueError('查询计划超出白名单范围。')
    a=date.fromisoformat(proposal['start_date']);b=date.fromisoformat(proposal['end_date'])
    if not 0<=(b-a).days<60 or b>=today:raise ValueError('查询计划的日期范围无效。')
    # 相对日也是硬约束；不能把“昨天”改到恰好有数据的那一天。
    scope=resolved_scope
    expected_start=scope['trend_start'] if scope['is_window'] else scope['target_date']
    if temporal['status']=='resolved':
        if (a.isoformat(),b.isoformat())!=(expected_start,scope['target_date']):
            raise ValueError('模型计划与原问题日期范围冲突。')
    else:
        # 规则未理解的时间允许模型解释，但必须引用原文；空引用不能补造日期。
        expression=proposal.get('time_expression','')
        if not isinstance(expression,str) or not expression.strip() or expression not in question:
            receipt.update(missing_fields=['date'],temporal=temporal)
            return None,receipt
        scope={**scope,'target_date':b.isoformat(),'is_window':a!=b,
               'trend_start':a.isoformat() if a!=b else (b-timedelta(days=13)).isoformat(),
               'trend_end':b.isoformat(),'comparison_date':(b-timedelta(days=1)).isoformat(),'errors':[]}
        temporal={**temporal,'scope':scope,'status':'resolved','notes':[*temporal['notes'],f'“{expression}”由模型解析为{a.isoformat()}至{b.isoformat()}。']}
    requested_rank=query_context.ranking_for(question) if query_context else ranking(question)
    if requested_rank and proposal['direction']!=requested_rank['direction']:raise ValueError('模型改变了明确的排序方向。')
    # 默认展示条数属于产品口径；普通问法不应因模型自行填5或10而得到不同计划。
    limit=ranking_limit(question)
    if not 1<=limit<=LIMITS['max_rank_limit']:raise ValueError('排名展示数量超出能力范围。')
    proposal={**proposal,'limit':limit}
    original=resolution
    if original.get('suggested_correction') or original['issues']:raise ValueError('商品存在错字或规格冲突，必须先确认。')
    selected=(original['candidates'] if proposal['selector']=='__matched__' else
        catalog.resolve(proposal['selector'],today=today)['candidates'] if proposal['selector']!='__all__' else list(catalog.products.values()))
    original_ids={p['sku_id'] for p in original['candidates']}
    if proposal['selector']=='__all__' and not original_ids and not global_scope(question) and not allow_default_all_products:raise ValueError('原问题未明确全店或商品范围，不能由模型默认查询全店。')
    if proposal['selector']=='__all__' and not original_ids and not global_scope(question):
        receipt['product_scope_source']='model_interpreted_omission_with_host_default'
    if original_ids and {p['sku_id'] for p in selected}!=original_ids:raise ValueError('模型改变了原问题的商品范围，不能擅自缩小或扩大。')
    if not selected:raise ValueError('查询计划没有有效商品。')
    from .query_context import QueryContext
    if len(selected)==1 and proposal['selector'] not in ('__all__',) and dimension=='sku':
        product={'kind':'single','sku_id':selected[0]['sku_id']}
    else:
        identity=original if original_ids else catalog.resolve(proposal['selector'],today=today)
        constraints=deepcopy(identity.get('constraints',{}))
        all_products=proposal['selector']=='__all__'
        label='全店商品' if all_products else '全部'+(constraints.get('varieties') or [proposal['selector']])[0]
        product=identity.get('selector') or {'kind':'group','constraints':constraints,
            'alias':None if all_products or constraints.get('varieties') else
                original.get('normalized_question') if proposal['selector']=='__matched__' else proposal['selector'],
            'all_products':all_products,'filter_text':'全店' if all_products else label.removeprefix('全部'),'label':label}
    rank=None
    if proposal['direction']!='none':
        rank={'metric':'units_sold','direction':proposal['direction'],'group_by':dimension,'limit':proposal['limit'],
              'views':['summary','ranking'],'assumption':'按成品支付件数排名，保留零销量和并列商品。'}
    compare=any(step['operation'].startswith('compare_') for step in sales_steps(question,scope))
    if query_context and query_context.operations is not None:compare=query_context.operations['compare']
    context=QueryContext(product=product,time={k:v for k,v in scope.items() if k!='errors'},
        operations={'ranking':rank,'compare':compare},complete=True)
    receipt.update(status='validated',parameters={**{key:proposal[key] for key in fields},'group_by':dimension,'intent':intent},
        model=proposal.get('model'),temporal=temporal)
    return context,receipt


def business_capability_context():
    """统一规划入口看到全部已注册只读能力，权限仍由执行器按冻结任务授予。"""
    return catalog()
