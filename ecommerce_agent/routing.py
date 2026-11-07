"""意图路由：规则匹配、能力边界和显式澄清，不把所有分支都执行一遍。

四类只读意图：规则先行，未知表达最多请求一次模型分类，歧义交用户确认。
路由不是授权：即使识别正确，后续仍需校验商品、日期和工具权限。
"""

import re
import time
from .time_scope import has_window,WINDOW_PATTERN
from .query_plan import ranking
from .natural_dates import normalize_windows

ROUTES = {
    "query_sales_performance": {"label": "销量表现与趋势", "required_table": "sales"},
    "investigate_sales_change": {"label": "销量变化调查", "required_table": "sales"},
    "query_inventory": {"label": "库存与缺货查询", "required_table": "inventory"},
    "query_promotions": {"label": "活动状态查询", "required_table": "promotions"},
    "query_combined": {"label": "多事项经营查询", "required_table": "sales"},
}

# 写动作先于业务分流，退款、取消订单等也不能被“销量”关键词掩盖。
WRITE_ACTION = r"改价|调价|下架|补货|删除|退款|退货|换货|取消订单|发货|调(?:整)?(?:广告)?预算|(?:修改|更新|设置|调整)(?:库存|活动|价格)|把.{0,30}(?:库存|价格|预算).{0,8}(?:改成|调成|设为)"


def route_question(question: str, intent_override: str | None = None, *, classifier=None,
                   confirmed_candidates=None) -> dict:
    """将原话映射到一个意图或澄清/拒绝；明确选路只能在原候选中选择。

    不返回伪概率。matched_rules 是可解释规则命中，不代表统计置信度。
    成本记录仅指当前路由阶段，不包含后续调查或网络服务的开销。
    """
    started = time.perf_counter()
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        raise ValueError("问题必须包含 1–2000 个字符。")
    decision = {"version": "hybrid_v3", "tier":"rules", "status": "needs_clarification", "intent": None,
                "candidates": [], "matched_rules": [], "reason": "请明确想查销量、库存还是活动。",
                "model_calls": 0, "model_tokens": 0}

    def finish():
        """为所有退出路径附相同的计时字段，避免拒绝请求漏记。"""
        decision["duration_ms"] = round((time.perf_counter() - started) * 1000, 4)
        return decision

    # 只处理有限的显式否定，不能宣称具备完整中文否定/引用理解能力。
    scoped = re.sub(r"(?:不要|不用|无需|不需要|别)(?:执行|进行|帮我)?(?:" + WRITE_ACTION + r")", "", question)
    # “取消订单率/原因”是读统计，“帮我取消订单”仍是写动作，不能整体放行“取消”。
    scoped = re.sub(r'取消订单(?=率|数|量|情况|原因|分析|趋势|有多少|为什么)', '订单取消', scoped)
    if re.search(WRITE_ACTION, scoped):
        decision.update(status="unsupported", intent="unsupported_action",
                        reason="只支持只读经营查询，不执行退款、改价、补货等动作。")
        decision["matched_rules"] = ["write_action_guard"]
        return finish()
    if re.search(r"订单(?:状态|到哪|查询)|查(?:一下|下)?订单|物流|销售额|利润|毛利", scoped):
        decision.update(status="unsupported", intent="unsupported_intent",
                        reason="当前不支持订单物流、销售额或利润专项查询，请使用已提供的经营查询入口。")
        return finish()
    scoped=normalize_windows(scoped)
    count=re.search(WINDOW_PATTERN,scoped)
    if re.search(r"同比|同期",scoped) or count and not 1<=int(count[1])<=60:
        decision.update(status="unsupported",intent="unsupported_time_window",reason="当前支持1–60天明确窗口；同比或同期查询尚未接入，超长窗口请缩小范围。")
        return finish()
    if re.search(r"天气|写诗|写代码|讲笑话",scoped):
        decision.update(status="unsupported",intent="unsupported_intent",reason="当前仅处理鲜花经营查询。")
        return finish()
    scoped=re.sub(r'(?:不查|不看|不要(?:查|看)?|不用(?:查|看)?)(?:销量|销售件数|库存|缺货|活动|促销)(?:(?:和|、|以及)(?:销量|库存|缺货|活动|促销))*',' ',scoped)
    sales = bool(re.search(r"销量|销售件数|卖少|少卖|卖不动|卖得少|下降原因|下滑原因|[卖买][得的]怎么样|[卖买](?:了|出)?多少|销量趋势|销量走势", scoped) or ranking(scoped))
    orders_query=bool(re.search(r'取消率|取消情况|取消原因|取消分析|取消趋势|取消数|订单取消|完成订单|履约情况',scoped))
    sales = sales or orders_query
    # 新品表现不要求前一天已有销售基线；原因调查与单日事实查询分开。
    sales_intent = "query_sales_performance" if sales and re.search(r"怎么样|如何|多少|表现", scoped) and not re.search(r"原因|为什么|下降|下滑|上涨|增长|变化|少卖|卖不动", scoped) else "investigate_sales_change"
    if sales and (has_window(scoped) or re.search(r'趋势|走势|曲线',scoped)) and not re.search(r'原因|为什么',scoped):sales_intent='query_sales_performance'
    if sales and re.search(r'哪(?:一)?款|谁|受欢迎|排行|排名',scoped) and not re.search(r'原因|为什么',scoped):sales_intent='query_sales_performance'
    if orders_query:sales_intent='investigate_sales_change'
    elif ranking(scoped) and not re.search(r'原因|为什么',scoped):sales_intent='query_sales_performance'
    inventory = bool(re.search(r"库存|缺货|断货|有货|可售", scoped))
    promotions = bool(re.search(r"活动|促销|优惠", scoped))
    # 因果语境中的来源提及不自动拆分；只有明确独立事项才编译组合计划。
    independent = bool(re.search(r"顺便|同时|另外|以及|分别|再查|并查|还(?:想|要)?查|(?:销量(?:变化|趋势|走势|情况|表现)?|销售件数|库存|缺货)\s*[、,，和及]\s*(?:库存|缺货|活动|促销)", scoped))
    if sales and re.search(r'解释|导致|造成|引起',scoped) and not re.search(r'顺便|同时查|另外|分别|再查|并查|还(?:想|要)?查',scoped):independent=False
    candidates = []
    if sales:
        candidates.append(sales_intent)
    if inventory and (not sales or independent):
        candidates.append("query_inventory")
    if promotions and (not sales or independent):
        candidates.append("query_promotions")
    decision["candidates"] = candidates
    decision["matched_rules"] = candidates.copy()
    if not candidates:
        # 明确的用户选项也可补全未识别意图；已有父任务候选时只能选其白名单。
        if intent_override:
            allowed=confirmed_candidates if confirmed_candidates is not None else list(ROUTES)
            if intent_override in ROUTES and intent_override in allowed:
                decision.update(status="selected",intent=intent_override,candidates=list(allowed),tier="user_confirmation",
                    reason="用户明确指定查询事项；仍需校验商品、日期与数据。")
            else:decision['reason']='所选事项不在原候选中，请重新确认。'
            return finish()
        if classifier is None:
            decision.update(tier="clarification",candidates=list(ROUTES),reason="规则暂不能确定查询事项，模型分类未启用；请选择本次查询事项。")
            return finish()
        decision.update(tier="model",model_calls=1,model_tokens=None)
        try:
            proposal=classifier(question)
            # 模型只建议路由，禁止新增工具/意图或将自报把握程度当真实准确率。
            if not isinstance(proposal,dict):raise ValueError('invalid classification')
            choice=proposal.get('intent');options=proposal.get('candidates')
            if (proposal.get('decision') not in ('select','clarify','unsupported') or
                proposal.get('confidence') not in ('high','medium','low') or
                not isinstance(options,list) or len(options)>len(ROUTES) or
                len(options)!=len(set(options)) or any(o not in ROUTES for o in options) or
                choice not in (*ROUTES,'none',None)):
                raise ValueError('invalid classification')
            usage=proposal.get('usage') or {}
            decision['model_tokens']=usage.get('total_tokens') if type(usage.get('total_tokens')) is int and usage['total_tokens']>=0 else None
            decision['model']=proposal.get('model','unknown')
            decision['model_confidence']=proposal['confidence']
            if choice in ROUTES and choice not in options:options=[choice,*options]
            decision['candidates']=options or list(ROUTES)
            if proposal['decision']=='unsupported' and proposal['confidence']=='high' and not options and choice in (None,'none'):
                decision.update(status='unsupported',intent='unsupported_intent',candidates=[],reason='模型判为当前能力范围之外；可改写为销量、库存或活动查询。')
            elif proposal['decision']=='select' and proposal['confidence']=='high' and choice in ROUTES and options==[choice]:
                decision.update(status='selected',intent=choice,reason='规则未命中；一次模型分类返回单一明确事项，继续本地参数校验。')
            else:decision['reason']='模型分类仍有歧义或把握不足，请选择本次先处理的事项；不会运行全部分支。'
        except Exception as exc:
            # 不泄露模型响应/密钥，不自动重试，不因超时改成“默认销量调查”。
            from .errors import from_exception
            problem=from_exception(exc,phase='classification',fallback='model_response_invalid').public()
            decision.update(tier='model_fallback',intent=None,status='failed',candidates=[],error_info=problem,
                reason=problem['message'])
        return finish()
    if intent_override=='query_combined' and len(candidates)>1:
        decision.update(status='selected',intent='query_combined',
            operations=[{'query_sales_performance':'sales','investigate_sales_change':'sales','query_inventory':'inventory','query_promotions':'promotions'}[item] for item in candidates],
            reason='明确多事项，按冻结的同一范围组合执行。')
        return finish()
    if intent_override:
        if intent_override not in ROUTES or intent_override not in candidates:
            decision["reason"] = "所选意图与原问题候选不一致，请改写问题后重新提交。"
            return finish()
        decision.update(status="selected", intent=intent_override,
                        reason="用户明确选择一个候选意图，本次只执行这一项，其余诉求不自动执行。")
    elif len(candidates) == 1:
        decision.update(status="selected", intent=candidates[0], reason="规则命中一个支持的只读意图。")
    elif candidates:
        decision.update(status='selected',intent='query_combined',
            operations=[{'query_sales_performance':'sales','investigate_sales_change':'sales','query_inventory':'inventory','query_promotions':'promotions'}[item] for item in candidates],
            reason='用户明确要求多个只读事项，冻结同一商品与日期范围后组合执行；独立取数有限并发，随后汇总证据。')
    else:
        decision.update(status="needs_clarification")
    return finish()
