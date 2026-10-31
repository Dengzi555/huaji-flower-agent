"""门店实体解析：目录身份→提及/排除→动作与指代→受限模型→范围校验。

输入只有问题、服务端目录和对话查询条件，不读取销量，不切店，不创建任务。
门店名称不等于访问权限：调用者提供allowed_ids；当前工作区默认可访问配置内门店，
不冒充多租户RBAC。歧义、未知实体与服务故障均返回显式状态，不回退当前店查数。
"""
import hashlib
import json
import re
import unicodedata
from difflib import get_close_matches

VERSION='flower-store-resolution-v1'
# 集合限定词与实体名称分开解析；同一语法用于剥离和判定，避免“所有的店”
# 被一层当成范围词、另一层又当未知店名。不是给每条经营问题单独补关键词。
ALL_STORES_PATTERN=r'(?:(?:所有|全部)(?:的)?|各(?:个|家)?|每(?:一)?(?:家|个))(?:门店|店铺|店)'
STORE_QUESTION_PATTERN=r'哪(?:一)?(?:个|家)(?:门店|店铺|店)|(?:门店|店铺)排行(?:榜)?|(?:门店|店铺)排名'


def normalize(text):
    """只规范全半角、大小写和空白，保留城市、街道、数字等身份信息。"""
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text)).casefold()


class StoreDirectory:
    """稳定ID与可变名称分离；别名允许重名，不能靠字典覆盖悄悄选最后一家。"""
    def __init__(self,shops):
        self.stores={};self.aliases={}
        for raw in shops:
            sid=raw.get('id');name=raw.get('name');aliases=raw.get('aliases',[])
            if (not isinstance(sid,str) or not re.fullmatch(r'[a-z0-9-]{1,40}',sid) or sid in self.stores
                    or not isinstance(name,str) or not name.strip()):raise ValueError('门店ID/名称无效或重复。')
            if not isinstance(aliases,list) or any(not isinstance(a,str) or not a.strip() for a in aliases):raise ValueError('门店别名必须为非空字符串列表。')
            if raw.get('status','active') not in ('active','inactive'):raise ValueError('门店状态无效。')
            shop={**raw,'aliases':list(aliases),'status':raw.get('status','active')}
            self.stores[sid]=shop
            # 兼容旧目录：仅依据该店city字段派生旧简称，不枚举任何城市名称。
            city=shop.get('city','')
            derived=name[len(city):] if city and name.startswith(city) else ''
            for alias in {name,sid,*aliases,derived}-{''}:
                self.aliases.setdefault(normalize(alias),set()).add(sid)
        if not self.stores:raise ValueError('门店目录不能为空。')
        self.version=hashlib.sha256(json.dumps(list(self.stores.values()),ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]

    def mentions(self,text):
        """按原文位置返回最长匹配；完整店名压过其内部短别名，但保留同名候选。"""
        found=[]
        for alias,ids in self.aliases.items():
            for match in re.finditer(re.escape(alias),text):
                # ID/英文别名不能命中另一个编号的子串。
                if alias.isascii() and ((match.start() and re.match(r'[a-z0-9-]',text[match.start()-1])) or (match.end()<len(text) and re.match(r'[a-z0-9-]',text[match.end()]))):continue
                found.append({'start':match.start(),'end':match.end(),'text':match[0],'candidate_ids':sorted(ids)})
        return sorted([m for m in found if not any(n['start']<=m['start'] and n['end']>=m['end'] and n['end']-n['start']>m['end']-m['start'] for n in found)],key=lambda m:(m['start'],m['end']))


def resolve_stores(question,directory,context,*,planner=None,allowed_ids=None,selected_store=None):
    """返回可审计解析结果；只有resolved才可交给宿主导航/准入，模型建议不直接执行。"""
    text=normalize(question)
    rank_stores=bool(re.search(STORE_QUESTION_PATTERN,text))
    allowed=set(directory.stores if allowed_ids is None else allowed_ids)
    allowed&={sid for sid,s in directory.stores.items() if s['status']=='active'}
    if selected_store is not None and selected_store not in allowed:
        return _result(directory,context,status='forbidden',message='所选门店不可访问。')
    mentions=directory.mentions(text)
    geography={field:{shop.get(field) for shop in directory.stores.values() if shop.get(field) and normalize(shop[field]) in text}
        for field in ('city','district')}
    from .routing import route_question
    geo_terms=set().union(*geography.values())
    geo_followup=bool(context.get('query_store_ids') and geo_terms and re.fullmatch(
        r'(?:'+ '|'.join(re.escape(normalize(t)) for t in geo_terms)+r'|那|呢|的|都|看看|怎么样|情况|对比|比较|[，。?？\s])+',text))
    if not mentions and not re.search(r'店|门店|店铺',text) and route_question(question)['status']!='selected' and not geo_followup:
        # 城市是地理属性而非门店身份。“成都天气”不能因为目录有两家成都店就追问选店。
        geography={'city':set(),'district':set()}
    # 同名简称可由用户给出的城市/区限定消歧；不把当前店或销量最高者当消歧证据。
    for mention in mentions:
        if len(mention['candidate_ids'])>1:
            narrowed=[sid for sid in mention['candidate_ids'] if all(not values or directory.stores[sid].get(field) in values for field,values in geography.items())]
            if narrowed:mention['candidate_ids']=narrowed
    # 先限制目录候选，再形成模型输入；不可访问门店不向客户端/模型泄露列表。
    if any(set(m['candidate_ids'])-allowed for m in mentions):
        return _result(directory,context,status='forbidden',message='本次提及包含不可访问门店。')
    positive=[];excluded=[];ambiguous=[]
    for mention in mentions:
        prefix=text[max(0,mention['start']-8):mention['start']]
        suffix=text[mention['end']:mention['end']+5]
        negative=bool(re.search(r'(?:不看|不查|不要(?:看)?|排除|不是|除了)$',prefix) or re.match(r'除外|不看|不查',suffix))
        if len(mention['candidate_ids'])>1:ambiguous.extend(mention['candidate_ids'])
        elif negative:excluded.extend(mention['candidate_ids'])
        else:positive.extend(mention['candidate_ids'])
        mention['role']='excluded' if negative else 'mentioned'
    positive=list(dict.fromkeys(positive));excluded=list(dict.fromkeys(excluded))
    evidence=[{'text':m['text'],'role':m['role'],'candidate_ids':m['candidate_ids']} for m in mentions]
    if ambiguous:
        return _result(directory,context,status='needs_clarification',candidate_ids=list(dict.fromkeys(ambiguous)),evidence=evidence,message='这个门店简称对应多家店，请选择具体门店；商品和日期会保留。')
    if (not mentions and re.search(r'(?:这|那|刚才|前面|上面)[^，。?？]{0,8}(?:店|花铺)',text)
            and not re.search(r'(?:这|那)家(?:门店|店铺|店)',text) and not re.search(ALL_STORES_PATTERN,text)):
        # 不在快捷语法中的集合指代交模型理解；候选只来自已确认集合，不能扩展权限。
        candidates=context.get('comparison_store_ids') or context.get('query_store_ids') or []
        candidates=[sid for sid in candidates if sid in allowed]
        if candidates and planner:
            return _model_resolution(question,directory,context,planner,candidates,[],allowed,
                [{'text':question,'role':'collection_reference','candidate_ids':candidates}])
        return _result(directory,context,status='needs_clarification',candidate_ids=candidates,
            message='请明确这次指的是哪些门店；有唯一上下文后会保留原查询条件继续。')
    # 已知店名与未知店名并存也要停，不能只查询已识别的一半。
    residual=text
    for mention in reversed(mentions):residual=residual[:mention['start']]+' '*len(mention['text'])+residual[mention['end']:]
    residual=re.sub(ALL_STORES_PATTERN,' ',residual)
    residual=re.sub(STORE_QUESTION_PATTERN,' ',residual)
    residual=re.sub(r'全店(?:铺)?|本店(?:铺)?|当前(?:门店|店铺)|该(?:门店|店铺|店)|这家(?:门店|店铺|店)|那家(?:门店|店铺|店)|其他(?:门店|店铺|店)|其它(?:门店|店铺|店)|别的店|(?:所有|全部|各)(?:门店|店铺|店)|两家店|两店|这两家|门店|店铺|花店',' ',residual)
    for shop in directory.stores.values():
        if shop.get('city'):residual=residual.replace(shop['city'],' ')
    unknown=re.findall(r'[\u4e00-\u9fff]{2,16}店',residual)
    if unknown:
        suggestions=[]
        for token in unknown:
            for alias in get_close_matches(token,directory.aliases,n=3,cutoff=.45):suggestions.extend(sid for sid in sorted(directory.aliases[alias]) if sid in allowed)
        return _result(directory,context,status='needs_clarification',candidate_ids=list(dict.fromkeys(suggestions)) or sorted(allowed),evidence=evidence,message='未定位到明确门店，请确认店名；不会改查当前店。')
    cities=geography['city'];districts=geography['district']
    group=bool(re.search(ALL_STORES_PATTERN,text) or rank_stores)
    other=bool(re.search(r'其他店|其它店|别的店|其他门店',text))
    pair=bool(re.search(r'两家店|两店|这两家|这几家',text))
    if not positive and (group or cities or districts):
        candidates=[sid for sid,s in directory.stores.items() if sid in allowed and (not cities or s.get('city') in cities) and (not districts or s.get('district') in districts)]
        if not group and len(candidates)>1:
            return _result(directory,context,status='needs_clarification',candidate_ids=candidates,message='该城市有多家门店，请指定一家，或说明查看该城市所有店。')
        positive=candidates
    if pair and not positive:
        positive=list(context.get('comparison_store_ids',[]))
        if not positive:return _result(directory,context,status='needs_clarification',candidate_ids=[s for s in context.get('recent_store_ids',[]) if s in allowed],message='请明确本次要比较哪几家门店。')
    references=[]
    if other and not positive:
        anchor=context.get('anchor_store') or context.get('focus_store')
        references=[anchor] if anchor in allowed and anchor not in excluded else []
        positive=[sid for sid in directory.stores if sid in allowed and sid!=anchor]
    positive=[sid for sid in positive if sid not in excluded]
    if (group or other or pair) and not positive:
        return _result(directory,context,status='needs_clarification',message='本次范围内没有可查询门店，请重新指定。')
    compare=bool(group or other or pair or re.search(r'对比|比较|分别|各自|也这样|也一样|哪个|哪家|不如',text))
    if positive:
        if any(sid not in allowed for sid in positive):return _result(directory,context,status='forbidden',message='本次范围包含不可访问门店。')
        positive_mentions=[m for m in mentions if m['role']=='mentioned']
        simple_links=all(re.fullmatch(r'(?:和|与|跟|及|、|,|相比|比|对比|比较|不如|的|门店|店铺|\s)*',
            text[a['end']:b['start']]) for a,b in zip(positive_mentions,positive_mentions[1:]))
        # 快捷路径只接收简单连接关系；实体间出现转述等复杂语句就交给模型，
        # 不靠持续补充“店长/经理/负责人...”职业名单来判断查询目标。
        complex_roles=bool(re.search(r'提到|反馈|听说|说想|说要',text)) or not simple_links or len(positive)>1 and not compare
        if complex_roles:
            if planner:
                return _model_resolution(question,directory,context,planner,positive,excluded,allowed,evidence)
            return _result(directory,context,status='needs_clarification',candidate_ids=positive,evidence=evidence,message='提到了多种门店关系，请明确本次要查询哪家，或是否需要比较。')
        action='rank' if rank_stores else 'compare' if len(positive)+len(references)>1 else 'switch' if re.search(r'切到|切换|切回|回到|返回',text) else 'query'
        return _result(directory,context,store_ids=positive,excluded_store_ids=excluded,reference_store_ids=references,action=action,evidence=evidence)
    if mentions or excluded:
        return _result(directory,context,status='needs_clarification',evidence=evidence,message='已排除提及门店，请说明要查看哪家店。')
    # “它”优先视为商品指代；只有明确门店指代才使用门店关系解析。
    previous=context.get('query_store_ids',[]) or [context.get('focus_store')]
    if re.search(r'本店|当前店|当前门店|当前店铺',text):
        sid=selected_store or context.get('focus_store')
        if sid not in allowed:return _result(directory,context,status='forbidden',message='当前门店不可访问。')
        return _result(directory,context,store_ids=[sid],source='context')
    if re.search(r'那家|这家|刚才那家|前一家',text):
        if len(previous)!=1:
            return _result(directory,context,status='needs_clarification',candidate_ids=[s for s in previous if s in allowed],message='上一轮涉及多家门店，请明确指哪一家。')
        sid=previous[0]
    elif context.get('default_query_scope')=='all':
        # 新查询的省略店名按业务配置查全部；真正的省略追问才继承已确认集合。
        followup=bool(context.get('query_store_ids') and re.match(r'那|继续|只看|仅看|换成|改查|再查|这款|它|这个|一共|总共|对比|相比',text))
        ids=previous if followup else [sid for sid in directory.stores if sid in allowed]
        if not ids or any(sid not in allowed for sid in ids):return _result(directory,context,status='forbidden',message='查询范围包含不可访问门店。')
        return _result(directory,context,store_ids=ids,action='compare' if len(ids)>1 else 'query',source='context' if followup else 'default_all')
    else:sid=selected_store or (previous[0] if len(previous)==1 else context.get('focus_store'))
    if sid not in allowed:return _result(directory,context,status='forbidden',message='当前门店不可访问。')
    return _result(directory,context,store_ids=[sid],source='context')


def _result(directory,context,*,status='resolved',store_ids=None,excluded_store_ids=None,candidate_ids=None,
            reference_store_ids=None,action='query',source='rules',evidence=None,message='',**extra):
    """统一结果契约：未知与失败的store_ids恒为空，防止下游误读候选为已授权范围。"""
    return {'version':VERSION,'catalog_version':directory.version,'context_revision':context.get('revision'),
        'status':status,'action':action,'store_ids':(store_ids or []) if status=='resolved' else [],
        'excluded_store_ids':excluded_store_ids or [],'candidate_ids':candidate_ids or [],
        'reference_store_ids':reference_store_ids or [],'source':source,'evidence':evidence or [],
        'message':message,'model_calls':0,**extra}


def _model_resolution(question,directory,context,planner,candidates,excluded,allowed,evidence):
    """复杂关系最多一次模型理解；只能选择已提及候选，不能扩展范围或重写原问题。"""
    try:
        from .cancellation import checkpoint
        checkpoint()
        proposal=planner(question,{'stores':[directory.stores[sid] for sid in candidates if sid in allowed],
            'excluded_store_ids':excluded,'context':{k:context.get(k) for k in ('focus_store','query_store_ids','comparison_store_ids','revision')},
            'actions':['query','switch','compare','rank']})
        checkpoint()
        fields={'decision','action','store_ids','excluded_store_ids'}
        if not isinstance(proposal,dict) or not fields<=set(proposal) or set(proposal)-fields-{'usage','model'}:raise ValueError('门店计划字段无效。')
        if proposal['decision'] not in ('select','clarify'):raise ValueError('门店计划决策无效。')
        if proposal['decision']=='clarify':return _result(directory,context,status='needs_clarification',candidate_ids=candidates,evidence=evidence,source='model',model_calls=1,message='请确认本次要查询的门店。')
        ids=proposal['store_ids'];omit=proposal['excluded_store_ids'];action=proposal['action']
        if (not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids)
                or not isinstance(omit,list) or any(not isinstance(i,str) for i in omit)
                or len(set(ids))!=len(ids) or not set(ids)<=set(candidates)&allowed or set(ids)&set(excluded)
                or not set(omit)<=set(candidates)|set(excluded) or set(ids)&set(omit)
                or action not in ('query','switch','compare','rank') or (action not in ('compare','rank') and len(ids)!=1)
                or (action=='compare' and len(ids)<2)):raise ValueError('门店计划范围或动作越界。')
        return _result(directory,context,store_ids=ids,excluded_store_ids=list(dict.fromkeys(excluded+omit)),action=action,
            source='model_validated',evidence=evidence,model_calls=1,model=proposal.get('model'),usage=proposal.get('usage'))
    except Exception as exc:
        from .cancellation import CancelledError
        from .errors import from_exception
        if isinstance(exc,CancelledError):raise
        problem=from_exception(exc,phase='planning',fallback='plan_validation_failed').public()
        return _result(directory,context,status='failed',source='model',model_calls=1,error_info=problem,message=problem['message'])
