"""任务解析层：把中文问题变成冻结范围、日期和能力计划；规则优先，必要时一次受限模型规划。

学习重点：先解析任务，再查业务数据。目录匹配到多个 SKU 时不能挑第一个；
“昨天”也不能偷偷解释成样例里最新一天。歧义、缺数据、可执行分别返回不同状态。
规则和模型共享能力契约；模型建议的参数必须重新校验，不是通用中文理解或任意SQL执行。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .routing import ROUTES, route_question
from .flower_language import normalize_flower_query
from .time_scope import resolve_scope
from .natural_dates import normalize_dates
from .query_plan import ranking,global_scope,plan_from_model,sales_steps,grouping,resolve_time_conditions
from .capabilities import question_boundaries,bind_contract,validate_plan,LIMITS
from .query_context import QueryContext


def shanghai_today() -> date:
    """在业务时区取今天；固定 UTC+8 避免 Windows 缺少时区数据库的依赖。

    当前业务只处理现代中国自然日，不用于历史夏令时日期的时区转换。
    测试可显式传入 today，使“昨天”的结果不随测试运行日期漂移。
    """
    return datetime.now(timezone(timedelta(hours=8))).date()


def normalize(text: str) -> str:
    """目录匹配时忽略空白与大小写；不删颜色、规格等区分 SKU 的关键信息。"""
    return re.sub(r"\s+", "", text).casefold()


class ProductCatalog:
    """商品目录负责名称到 SKU 的映射，销量表不能替代商品身份信息。"""

    def __init__(self, path: Path, allowed_skus: set[str]):
        """读取独立目录，并检查目录 SKU 能否在当前数据快照中找到。"""
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        self._load(raw, allowed_skus)

    @classmethod
    def from_document(cls, document, allowed_skus):
        """恢复任务创建时的目录版本，复查不受后来商品别名变化影响。"""
        instance = cls.__new__(cls)
        instance._load(document, allowed_skus)
        return instance

    def _load(self, raw, allowed_skus):
        """所有目录入口共享结构与 SKU 一致性检查。"""
        if raw.get("schema_version") != 1 or not isinstance(raw.get("products"), list):
            raise ValueError("Catalog requires schema_version=1 and a products array")
        self.products = {}
        for product in raw["products"]:
            if not isinstance(product, dict):
                raise ValueError("Catalog product must be an object")
            for field in ("sku_id", "name", "variant"):
                if not isinstance(product.get(field), str) or not product[field].strip():
                    raise ValueError(f"Catalog requires a non-empty {field}")
            aliases = product.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
                raise ValueError("Catalog aliases must be non-empty strings")
            if "stem_count" in product:
                if type(product["stem_count"]) is not int or not 1 <= product["stem_count"] <= 9999:
                    raise ValueError("Flower stem_count must be a positive integer")
                if product.get("product_type") not in ("花束", "抱抱桶"):
                    raise ValueError("Flower product_type must be 花束 or 抱抱桶")
                if any(not isinstance(product.get(key), str) or not product[key].strip() for key in ("short_name", "variety")):
                    raise ValueError("Flower catalog requires short_name and variety")
                # 节日、赠礼等 SEO 词不是商品身份，防止一个词自动命中任意 SKU。
                if set(aliases) & {"生日礼物", "生日花束", "生日鲜花", "情人节鲜花", "母亲节鲜花", "同城配送", "鲜花上门"}:
                    raise ValueError("Occasion keywords must not be used as product aliases")
            sku = product["sku_id"]
            if product.get("listed_at"):
                date.fromisoformat(product["listed_at"])
            if sku in self.products or sku not in allowed_skus:
                raise ValueError(f"Catalog SKU is duplicate or absent from data: {sku}")
            self.products[sku] = {**product, "aliases": aliases}

    def match(self, question: str) -> list[dict]:
        """兼容旧调用：只返回候选；完整依据与规格冲突由 resolve 提供。"""
        return self.resolve(question)["candidates"]

    def resolve(self, question: str, *, today=None) -> dict:
        """包装实体解析并保留口语纠正；上架日期是目录筛选条件，不是销量证据。"""
        # “红梅”本身可能是真花材，不能静默当成红玫瑰。目录无此花材时只推荐候选。
        suspected = bool(re.search(r"红梅(?!花)", question)) and not any(
            "红梅" in str(value) for product in self.products.values()
            for value in [product.get("name", ""), product.get("variety", ""), *product["aliases"]]
        )
        candidate_question = re.sub(r"红梅(?!花)", "红玫瑰", question) if suspected else question
        canonical, changes = normalize_flower_query(candidate_question)
        result = self._resolve_core(canonical)
        if suspected:
            result["suggested_correction"] = {"original": "红梅", "candidate": "红玫瑰", "requires_confirmation": True}
        result.update(normalized_question=canonical, normalization_rules=changes)
        candidates = result["candidates"]
        if "爆款" in question:
            # 爆款必须是目录中明确维护的经营标签，不能临时挑销量最大的商品冒充。
            candidates = [p for p in candidates if "爆款" in p.get("business_tags", [])]
            result["constraints"]["business_tag"] = "爆款"
            if not candidates:
                result["issues"].append("没有同时符合规格和爆款标签的商品，请确认商品。")
        if re.search(r"新上架|新上的|上新", question):
            days, errors = parse_dates(question, today or shanghai_today())
            if len(days) == 1 and not errors:
                candidates = [p for p in candidates if p.get("listed_at") == days[0]]
                result["constraints"]["listed_at"] = days[0]
                if not candidates:
                    result["issues"].append("该上架日期没有符合条件的商品，请确认上架时间或商品名称。")
            else:
                result["issues"].append("请明确新品上架日期，例如昨天或完整日期。")
        result["candidates"] = candidates
        return result

    def _resolve_core(self, question: str) -> dict:
        """按 SKU/花名/有效别名定位，再核对朵数、品种与类型；不使用节日词唯一匹配。"""
        text = normalize(question)
        known_names = {normalize(product["short_name"]) for product in self.products.values() if product.get("short_name")}
        bracketed = re.findall(r"【([^】]{1,32})】", question)
        if known_names and any(normalize(name) not in known_names for name in bracketed):
            return {"candidates": [], "matched_by": "unknown_flower_name", "constraints": {},
                    "issues": ["括号中的花名不在当前目录，不能仅凭相似朵数或营销词换成其他商品。"]}
        explicit = []
        for sku, product in self.products.items():
            # 防止 FLR-ROSE-19-BOUQUET-OLD 被误匹配成 FLR-ROSE-19-BOUQUET。
            pattern = r"(?<![a-z0-9_-])" + re.escape(sku.casefold()) + r"(?![a-z0-9_-])"
            # 精确 SKU 使用原始空白边界；不能先删空格，否则 SKU 和后面的日期会黏在一起。
            if re.search(pattern, question.casefold()):
                explicit.append(product)
        named = [product for product in self.products.values() if product.get("short_name")
                 and normalize(product["short_name"]) in text]
        if explicit and named and {p["sku_id"] for p in explicit} != {p["sku_id"] for p in named}:
            return {"candidates": [], "matched_by": "conflicting_identity", "constraints": {},
                    "issues": ["SKU 与四字花名指向不同商品，请先确认，不能自动覆盖。"]}
        if explicit or named:
            return self._constrain(question, explicit or named, "sku" if explicit else "flower_name")
        hits = []
        for product in self.products.values():
            aliases = [product["name"], product["name"] + product["variant"], *product["aliases"]]
            if product.get('series'):aliases.extend([product['series'],product['series'].removesuffix('系列')])
            for alias in aliases:
                token = normalize(alias)
                if token in text:
                    hits.append((token, product))
        specific = [(token, product) for token, product in hits
                    if not any(token != other and token in other for other, _ in hits)]
        candidates = list({product["sku_id"]: product for _, product in specific}.values())
        if candidates:
            return self._constrain(question, candidates, "catalog_alias")
        # 没有花名时，“99朵+花束”等明确结构也可用于缩小目录，不凭营销场景猜商品。
        result = self._constrain(question, [p for p in self.products.values() if "stem_count" in p], "flower_attributes")
        if not any(result["constraints"].values()):
            result.update(candidates=[], issues=[], matched_by="not_found")
        return result

    def _constrain(self, question, candidates, matched_by):
        """结构化规格是硬约束；身份与规格矛盾时回退澄清，不改选另一个可运行 SKU。"""
        core = normalize(question).replace("红色玫瑰", "红玫瑰").replace("粉色康乃馨", "粉康乃馨")
        # 抱抱桶标题也可能带“生日花束”SEO词，先移除，避免误判为花束类型。
        core = re.sub(r"生日花束|生日鲜花|生日礼物|情人节鲜花|母亲节鲜花|毕业季鲜花", "", core)
        counts = {int(value) for value in re.findall(r"(?<!\d)(\d{1,6})(?:朵|枝)", core)}
        types = {kind for kind in ("花束", "抱抱桶", "花篮", "花盒", "花瓶") if kind in core}
        varieties = {p["variety"] for p in self.products.values() if p.get("variety") and normalize(p["variety"]) in core}
        varieties.update(color + flower for color, flower in re.findall(r"(红|粉|白|黄|蓝|紫|香槟|混色)色?(玫瑰|康乃馨)", core))
        constraints = {"stem_counts": sorted(counts), "product_types": sorted(types), "varieties": sorted(varieties)}
        filtered = [product for product in candidates if "stem_count" not in product or (
            (not counts or product["stem_count"] in counts) and (not types or product["product_type"] in types)
            and (not varieties or product["variety"] in varieties))]
        issues = ["花名/商品与所述朵数、品种或类型不一致，请确认规格后改写问题；不会自动换商品。"] if candidates and not filtered else []
        return {"candidates": filtered, "matched_by": matched_by, "constraints": constraints, "issues": issues}


def parse_dates(question: str, today: date) -> tuple[list[str], list[str]]:
    """支持完整日期、月日、星期与相对日；省略年份明确按本年，不静默回退去年。"""
    question, _, errors = normalize_dates(question, today)
    dates = []
    # 口语日期已规范化；完整日期稍后移除，避免误判其中月日为另一日期。
    pattern = r"(?<!\d)(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|号)?(?!\d)"
    for match in re.finditer(pattern, question):
        try:
            dates.append(date(*map(int, match.groups())).isoformat())
        except ValueError:
            errors.append(f"日期不合法：{match.group(0)}")
    remainder = re.sub(pattern, "", question)
    if re.search(r"\d{1,2}月\d{1,2}[日号]?|\d{1,2}[-/]\d{1,2}", remainder):
        errors.append("日期缺少年份，请使用 YYYY-MM-DD。")
    if "大前天" in remainder:
        errors.append("暂不支持“大前天”，请填写完整日期。")
        remainder = remainder.replace("大前天", "")
    for word, offset in (("昨天", -1), ("昨日", -1), ("前天", -2), ("今天", 0), ("今日", 0), ("明天", 1)):
        if word in remainder:
            dates.append((today + timedelta(days=offset)).isoformat())
    return sorted(set(dates)), errors


def parse_task(question,catalog,store,*,today=None,sku_override=None,date_override=None,intent_override=None,
               route_classifier=None,confirmed_candidates=None,query_planner=None,intent_retriever=None,planning_policy='hybrid',default_product_scope=None,query_context=None):
    """公共入口给每个未准入结果附统一问题码，服务故障不会伪装成用户信息不足。"""
    from .errors import attach_task_issue
    return attach_task_issue(_parse_task(question,catalog,store,today=today,sku_override=sku_override,
        date_override=date_override,intent_override=intent_override,route_classifier=route_classifier,
        confirmed_candidates=confirmed_candidates,query_planner=query_planner,intent_retriever=intent_retriever,planning_policy=planning_policy,default_product_scope=default_product_scope,query_context=query_context))


def _parse_task(question,catalog,store,*,today=None,sku_override=None,date_override=None,intent_override=None,
               route_classifier=None,confirmed_candidates=None,query_planner=None,intent_retriever=None,planning_policy='hybrid',default_product_scope=None,query_context=None):
    """统一入口：规则快捷解析完整则直接返回；否则仅一次模型规划，再走相同业务准入。"""
    today=today or shanghai_today()
    if query_context is not None and not isinstance(query_context,QueryContext):raise ValueError('查询上下文必须由服务端构造。')
    if default_product_scope not in (None,'all_if_omitted'):raise ValueError('默认商品范围策略无效。')
    # 评测消融只更换理解路径；统一业务边界和模型计划校验不能被任何方案绕过。
    if planning_policy not in ('hybrid','rules_only','direct_model'):raise ValueError('未知规划策略。')
    if planning_policy=='rules_only':query_planner=None;route_classifier=None
    if planning_policy=='direct_model' and query_planner is None:raise ValueError('直接模型规划需要规划器。')
    options=dict(today=today,sku_override=sku_override,date_override=date_override,intent_override=intent_override,
        confirmed_candidates=confirmed_candidates,query_context=query_context)
    task=_parse_rules_task(question,catalog,store,route_classifier=None if query_planner else route_classifier,**options)
    task['understanding']={'path':'rules','planning_calls':0}
    # 未接入数据域也能在无路由命中时明确返回能力边界，而不是要求补日期/商品。
    boundaries=[item for item in question_boundaries(question,bool(ranking(question))) if item['code']!='unsupported_ranking_metric']
    if boundaries and task['status']!='unsupported':
        task.update(status='unsupported',capability_boundaries=boundaries,issues=[item['message'] for item in boundaries])
    # 跨店重放已有共享计划只做本店校验；本店没有商品/数据也不能再次调模型改口径。
    if (not query_planner or task['status']=='unsupported'
            or query_context and query_context.complete
            or planning_policy!='direct_model' and task['status'] in ('ready','needs_data')):
        return task
    resolution=query_context.resolve_product(catalog) if query_context and query_context.product else catalog.resolve(question,today=today)
    # 权限/能力边界和明确实体冲突不是理解失败，不能交模型改写绕过。
    if question_boundaries(question,bool(ranking(question))) or resolution['issues'] or resolution.get('suggested_correction'):return task
    if len(task['routing'].get('matched_rules',[]))>1:return task
    # 已知意图下，简称对应多款且不能按品类汇总，这是业务歧义，不是模型故障。
    # 日期已由规则独立识别；只澄清商品，禁止规划器替用户挑第一款或扩大花材范围。
    if (task['routing']['status']=='selected' and len(task.get('candidates',[]))>1
            and not task.get('product_scope') and not sku_override):
        task['clarification']={'kind':'product','candidate_sku_ids':[p['sku_id'] for p in task['candidates']],
            'known_date':task.get('target_date')}
        return task
    # 检索只给模型候选证据，不用例句里的商品/日期覆盖用户条件；故障仍继续规划。
    from .semantic_router import retrieve_intents
    try:
        retrieval=({'status':'disabled_for_direct_model','candidates':[]} if planning_policy=='direct_model'
            else (intent_retriever or retrieve_intents)(question))
        if not isinstance(retrieval,dict):raise ValueError('候选检索返回值无效。')
    except Exception:
        retrieval={'status':'unavailable','candidates':[],'policy':'model_fallback'}
    task['intent_retrieval']=retrieval
    observed={}
    def observed_planner(text,context):
        """参数被拒绝也保留供应商用量回执；不保存原始响应或把未知用量计成零。"""
        from .errors import from_exception
        try:proposal=query_planner(text,context)
        except Exception as exc:raise from_exception(exc,phase='planning',fallback='model_response_invalid') from None
        if isinstance(proposal,dict):
            observed.update({key:proposal[key] for key in ('usage','model') if key in proposal})
        return proposal
    try:
        context,receipt=plan_from_model(question+(' '+date_override if date_override else ''),catalog,today,observed_planner,
            default_intent=task.get('intent') if task.get('intent') in ROUTES else 'query_sales_performance',intent_candidates=retrieval,
            allow_default_all_products=default_product_scope=='all_if_omitted',query_context=query_context)
        task['understanding']={'path':'model_plan','planning_calls':1}
        if context is None:
            task['status']='needs_clarification'
            task['planning_attempt']=receipt
            _clarify_plan(task,receipt,resolution)
            return task
        intent=receipt['parameters']['intent']
        # 明确人工选项不能被模型更换；多种意图也不能由规划器偷偷挑一个。
        if intent_override and intent!=intent_override:raise ValueError('模型改变了用户明确选择的事项。')
        routing={**task['routing'],'status':'selected','intent':intent,'tier':'validated_plan',
            'classification_status':task['routing']['status'],'reason':'规则未完整解析，统一模型计划经参数与能力校验后继续准入。'}
        # 已校验的字段直接进入公共编译器，保留原话，不再生成问句或重新选路。
        planned=_new_query_task(question,store,today,routing)
        planned=_compile_query(planned,question,catalog,store,today,sku_override,context,
            temporal=receipt['temporal'])
        planned['query_plan']['planning']=receipt
        planned['intent_retrieval']=retrieval
        planned['understanding']=task['understanding']
        rank=planned['query_plan'].get('ranking')
        if rank and planned.get('product_scope'):rank['limit']=receipt['parameters']['limit'];planned['product_scope']['ranking']['limit']=rank['limit']
        return planned
    except Exception as exc:
        from .errors import AgentError,from_exception
        task['understanding']={'path':'model_plan','planning_calls':1}
        # 源问题确实缺少条件，仍向用户追问；模型/网络失败或篡改完整条件则属于服务错误。
        temporal=resolve_time_conditions(question+(' '+date_override if date_override else ''),today,query_context)
        source_time=temporal['scope'];date_errors=temporal['errors']
        problem=from_exception(exc,phase='planning',fallback='plan_validation_failed').public()
        problem['data_queried']=False
        if '尚未开始查数' not in problem['message']:problem['message']+=' 本次尚未开始查数。'
        # 可独立确认的原始条件仍可展示/重试；status保持failed，绝不因此授权工具执行。
        if not source_time['errors'] and not date_errors:
            for field in ('target_date','comparison_date','trend_start','trend_end','is_window'):
                if field in source_time:task[field]=source_time[field]
        if len(resolution['candidates'])==1:
            task['sku_id']=resolution['candidates'][0]['sku_id'];task['candidates']=resolution['candidates']
        task.update(status=problem['status'],error_info=problem,issues=[problem['message']])
        task.setdefault('query_plan',{})['planning']={'tier':'model','model_calls':1,'status':'failed',
            'error_type':type(exc).__name__,**observed}
        return task


def _clarify_plan(task,receipt,resolution):
    """仅展示尚缺条件；保留独立识别的商品、时间，不把模型失败包装成信息缺失。"""
    temporal=receipt['temporal'];scope=temporal['scope']
    task.update({key:value for key,value in scope.items() if key!='errors'})
    task['candidates']=resolution['candidates']
    if len(resolution['candidates'])==1:task['sku_id']=resolution['candidates'][0]['sku_id']
    missing=receipt['missing_fields']
    prompts={'date':'你想查看哪段时间？例如上个月、近7天，或10月1日至15日。',
             'product':'你想查看哪类花或哪款商品？',
             'intent':'你希望了解销售表现，还是调查经营变化的原因？'}
    task['issues']=[prompts[field] for field in missing]
    if temporal['errors']:task['issues']=list(temporal['errors'])
    task['clarification']={'kind':missing[0] if len(missing)==1 else 'conditions','missing_fields':missing,
        'known_date':scope.get('target_date')}


def _new_query_task(question,store,today,routing):
    """规则和模型共享任务初始结构，路由已确定后只编译条件和验证能力。"""
    task={"parser":"deterministic_rules_v1","raw_question":question,
          "business_today":today.isoformat(),"timezone":"Asia/Shanghai",
          "intent":routing['intent'],"routing":routing,"status":"needs_clarification",
          "sku_id":None,"target_date":None,"comparison_date":None,
          "comparison_policy":"previous_calendar_day","candidates":[],"issues":[],
          "assumptions":["只调查一个 SKU 的单个自然日，与前一自然日比较。"],
          "sample_data":store.meta['sample_data']}
    if store.meta.get('business_context',{}).get('scenario')=='flash_flowers':
        task['assumptions'].append('销量按成品束/桶计；标题朵数是单件花材规格，不是成交件数。')
    if task['intent']=='query_sales_performance':
        task['comparison_policy']='target_day_only'
        task['assumptions'][0]='只查询目标日销售事实；不要求前一日基线，不计算降幅。'
    return task


def _parse_rules_task(question: str, catalog: ProductCatalog, store, *, today: date | None = None,
               sku_override: str | None = None, date_override: str | None = None,
               intent_override: str | None = None, route_classifier=None, confirmed_candidates=None,query_planner=None,query_context=None,routing_decision=None) -> dict:
    """生成任务卡：ready 才能交给 runner，其他状态只输出待确认或缺数据信息。

    --sku/--date 用来补全槽位；若它们和问题中的明确信息矛盾，拒绝覆盖。
    日期默认只与前一天比较，这个假设会写入任务卡和报告，不能暗中切换同比口径。
    """
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        raise ValueError("问题必须包含 1–2000 个字符。")
    today = today or shanghai_today()
    routing = routing_decision or route_question(question, intent_override,classifier=route_classifier,confirmed_candidates=confirmed_candidates)
    if routing.get('intent')=='query_combined' and routing.get('status')=='selected':
        from .combined_query import parse_combined
        return parse_combined(question,catalog,store,today=today,sku_override=sku_override,date_override=date_override,
            query_context=query_context,routing=routing)
    task=_new_query_task(question,store,today,routing)
    if routing["status"] != "selected":
        task["status"] = routing["status"]
        task["issues"].append(routing["reason"])
        return task
    return _compile_query(task,question,catalog,store,today,sku_override,query_context,date_override=date_override)


def _compile_query(task,question,catalog,store,today,sku_override,query_context,*,date_override=None,temporal=None):
    """把结构化条件绑定成可执行任务；共享目录、时间、能力和数据覆盖检查。"""
    boundaries=question_boundaries(question,bool(ranking(question)))
    task['capability_boundaries']=boundaries
    if any(item['code']!='unsupported_ranking_metric' for item in boundaries):
        task['status']='unsupported'
        task['issues']=[item['message'] for item in boundaries]
        return task
    # 原话继续负责边界检查；已确认条件直接逐店重绑定，不再序列化成自然语言重新猜测。
    resolution = query_context.resolve_product(catalog) if query_context and query_context.product else catalog.resolve(question, today=today)
    context_group=bool(query_context and query_context.product and query_context.product['kind']=='group')
    rank=query_context.ranking_for(question) if query_context else ranking(question)
    candidates = resolution["candidates"]
    whole_store=task['intent']=='query_sales_performance' and global_scope(question) and not candidates and not resolution['issues']
    if whole_store:candidates=list(catalog.products.values())
    task["product_resolution"] = {key: value for key, value in resolution.items() if key != "candidates"}
    task["issues"].extend(resolution["issues"])
    task["candidates"] = candidates
    constraints=resolution.get('constraints',{})
    # 花材级表现是集合查询，不把“所有红玫瑰”误判成必须选一款的歧义。
    common_aliases=set.intersection(*[set(p.get('aliases',[])) for p in candidates]) if candidates else set()
    if candidates and len({p.get('series') for p in candidates})==1 and candidates[0].get('series'):
        common_aliases.update([candidates[0]['series'],candidates[0]['series'].removesuffix('系列')])
    shared=sorted([alias for alias in common_aliases if alias in question],key=len,reverse=True)
    groupable=(context_group and bool(candidates) and not resolution['issues'] and task['intent']=='query_sales_performance') or ((len(candidates)>1 or bool(candidates) and grouping(question)!='sku') and not resolution['issues'] and task['intent']=='query_sales_performance'
        and resolution.get('matched_by') not in ('sku','flower_name','context_single') and not constraints.get('stem_counts')
        and (len(constraints.get('varieties',[]))==1 or bool(shared) or whole_store or bool(constraints.get('product_types'))))
    if groupable:
        filter_text='全店' if whole_store else (constraints['varieties'][0] if constraints.get('varieties') else shared[0] if shared else '')+''.join(constraints.get('product_types',[]))
        task['group_candidate']={'kind':'store' if whole_store else 'category','label':'全店商品' if whole_store else '全部'+filter_text,'filter_text':filter_text,
            'sku_ids':sorted(p['sku_id'] for p in candidates)}
        selector=resolution.get('selector') or {'kind':'group','constraints':constraints,
            'alias':shared[0] if shared and not constraints.get('varieties') and not whole_store else None,
            'all_products':whole_store,'filter_text':filter_text,'label':task['group_candidate']['label']}
        task['group_candidate'].update(selector=selector,filter_text=selector['filter_text'],label=selector['label'],
            kind='store' if selector.get('all_products') else 'category')
        if len(candidates)>LIMITS['max_products']:task['issues'].append(f"一次品类查询最多{LIMITS['max_products']}款商品，请缩小花材或系列范围。")
    use_group=groupable and (sku_override=='__all_matched__' or not sku_override and not resolution.get('suggested_correction'))
    if resolution.get("suggested_correction") and not sku_override:
        task["issues"].append("你输入了“红梅”，是否指“红玫瑰”？请确认下方商品，或修改问题；确认前不查询经营数据。")
    if use_group:
        task['sku_id']='__all_matched__';task['product_scope']=task['group_candidate']
    elif sku_override:
        if sku_override not in catalog.products:
            task["issues"].append("--sku 不在当前商品目录中。")
        elif candidates and sku_override not in {p["sku_id"] for p in candidates}:
            task["issues"].append("--sku 与问题中明确提到的商品不一致。")
        else:
            task["sku_id"] = sku_override
    elif len(candidates) == 1:
        task["sku_id"] = candidates[0]["sku_id"]
    elif candidates and not groupable:
        task["issues"].append(f"这个简称对应{len(candidates)}款商品，请选择具体花名或补充类型；已提供的日期会保留。")
    elif not candidates:
        task["issues"].append("未找到明确商品，请提供目录中的名称、别名或 --sku。")

    if temporal is None:
        normalized_question,date_notes,date_errors=normalize_dates(question,today)
        scope=resolve_scope(normalized_question,today,parse_dates,date_override)
        if query_context:scope=query_context.resolve_time(scope,today)
    else:
        normalized_question=temporal['normalized'];date_notes=temporal['notes'];date_errors=temporal['errors']
        scope={**temporal['scope'],'errors':list(temporal['scope']['errors'])}
    task['normalized_question']=normalized_question
    task['date_resolution']=date_notes
    task['issues'].extend(date_errors)
    task["issues"].extend(scope.pop("errors"))
    task.update(scope)
    task['query_plan']={'version':'sales-query-v1','tier':'rules','model_calls':0,'operation':'sales_summary',
        'ranking':rank,'metric':'units_sold','start_date':task['trend_start'] if task['is_window'] else task['target_date'],'end_date':task['target_date']}
    task['query_plan']['steps']=(query_context.steps(rank,task['comparison_date'])
        if query_context and query_context.operations is not None else sales_steps(question,task))
    bind_contract(task)
    unsupported_rank=any(item['code']=='unsupported_ranking_metric' for item in boundaries)
    if unsupported_rank:task['issues'].extend(item['message'] for item in boundaries)
    if rank:task['assumptions'].append(rank['assumption'])
    if task.get('product_scope'):
        task['product_scope']['summary_start']=task['trend_start'] if task['is_window'] else task['target_date']
        task['product_scope']['ranking']=rank
        task['product_scope']['compare_selected']=any(step['operation']=='compare_selected' for step in task['query_plan']['steps'])
    task['include_trend']=task['intent'] in ('investigate_sales_change','query_sales_performance')
    task['product_context']={key:catalog.products.get(task['sku_id'],{}).get(key) for key in ('listed_at','business_tags')}
    task['include_order_funnel']=task['intent']=='investigate_sales_change' and (bool(re.search(r'取消|履约|完成订单',question)) or store.has_domain('order_cohorts'))
    if task['is_window'] and not task['include_trend']:
        task.update(status='unsupported',intent='unsupported_time_window')
        task['issues'].append('多日窗口目前用于销量表现与销量变化调查；库存/活动专项请指定单日。')
        return task
    if task['is_window'] and re.search(r'取消|完成订单|履约',question):
        task.update(status='unsupported',intent='unsupported_time_window')
        task['issues'].append('订单取消分析当前按单个支付日批次对比，请指定昨天、10月25日等单日；销量曲线仍支持多日。')
        return task
    if task['intent']=='query_sales_performance':
        if not any(step['operation'].startswith('compare_') for step in task['query_plan']['steps']):
            task['comparison_policy']='multi_day_context' if task['is_window'] else 'target_day_only'
    task['assumptions']=[*date_notes,*([rank['assumption']] if rank else []),f"展示范围：{task['trend_start']}至{task['trend_end']}；对比相对目标日计算。",'多基线与日历/活动只提供背景，不构成因果结论。']
    semantic_operation=task['intent']=='query_sales_performance' and not rank and bool(re.search(r'哪|谁|更受欢迎|抢手',question)) and not (query_context and query_context.operations is not None)
    if task["issues"] or semantic_operation:
        if semantic_operation:task['issues'].append('还需确认比较口径，请明确是否按成品销量排名，以及要看最高还是最低。')
        if task['issues']:return task
    try:
        task['capability_contract']['tools']=validate_plan(task,store)
    except (ValueError,TypeError,KeyError) as error:
        task['issues'].append(str(error));return task
    if task.get('product_scope'):
        # 集合查询独立准入：任一商品有数据即可展示覆盖情况，全部缺失则明确报缺数据。
        scope=task['product_scope'];days=[task['target_date']]
        if task['is_window']:
            start=date.fromisoformat(task['trend_start']);end=date.fromisoformat(task['trend_end'])
            days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
        found=any(store.daily_record('sales',sku,day)['status']=='ok' for sku in scope['sku_ids'] for day in days)
        task['status']='ready' if found else 'needs_data'
        if not found:
            available=store.dates('sales')
            task['issues']=[f"{task['target_date']} 没有可用的{scope['label']}销量数据。"+
                (f"当前销售记录覆盖 {available[0]} 至 {available[-1]}；需要补充所查期间的历史报表。" if available else '请导入该期间的销售报表。')]
        return task
    # 销量调查要求销售基线；专项查询只要求自身来源，不强制读取其他数据域。
    # 不要求四类全齐，否则会把“调查中发现缺口”的正常流程错误地拦住。
    required = ROUTES[task["intent"]]["required_table"]
    records = store.query(required, task["sku_id"], task["target_date"],comparison_day=task["comparison_date"])["records"]
    checked = records if task["intent"] == "investigate_sales_change" else records[-1:]
    if task['intent']=='query_sales_performance' and task['is_window']:
        available=[r for r in store.rows('sales',sku=task['sku_id'],start=task['trend_start'],end=task['trend_end']) if r['status']=='ok']
        missing=[] if available else [task['trend_start']+'至'+task['trend_end']]
    else:missing = [row["date"] for row in checked if row["status"] != "ok"]
    if missing:
        task["status"] = "needs_data"
        task["issues"].append("缺少所选查询必需的可用数据（" + required + "）：" + ", ".join(missing))
        task["issues"].append("不会把“昨天”替换成样例最新日期；请补充数据或明确指定已有日期。")
        return task
    task["status"] = "ready"
    return task
