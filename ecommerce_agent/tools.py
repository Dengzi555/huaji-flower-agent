"""工具层：把普通业务函数包装成模型可请求、程序可校验的只读能力。

Schema 是模型看到的说明，execute 是程序真正的入口；有 Schema 不代表可以省略
服务端参数校验。未来可在外面包装 MCP，但当前文件本身不是 MCP Server。
"""

from datetime import datetime, timezone

from .analysis import analyze
from .verification import verify_assertions
from .sales_trend import sales_trend,comparison_notes
from .business_calendar import calendar_range
from .order_funnel import order_funnel
from .assessment import assess
from .operating_guidance import build_guidance
from .capabilities import CAPABILITIES
from .roles import require_role_tool
from .data import previous_day
from .group_sales import group_sales
from .narrative_contract import build_contract,audit_summary
from datetime import date,timedelta


TABLES = {"get_sales_metrics": "sales", "get_traffic_metrics": "traffic",
          "get_inventory_status": "inventory", "get_promotion_history": "promotions"}
FOLLOW_UP_TOOL = "get_channel_breakdown"


def definition(name, description, properties=None, required=None):
    """生成 function calling 工具契约；禁止额外参数，避免悄悄混入其他 SKU。"""
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": required or [], "additionalProperties": False}}}


SCHEMAS = [definition(name, (f"Read {table} for the fixed investigation SKU and dates; no mutation. "
    + ('Campaign events/coupons only. active is NOT paid advertising status; this tool cannot confirm ad spending or whether ads are enabled.' if table=='promotions' else '')))
           for name, table in TABLES.items()] + [
    definition("analyze_metrics", "Compute verified facts and candidate signals after all four data tools. Missing data stays missing."),
    definition("finish", "Finish with a concise Chinese draft (prefer <=600 characters, hard limit 1200) and cite source IDs. Follow analyze_metrics.narrative_contract; rejected narrative checks must be corrected within the existing budget. Submit at most 12 raw-field assertions, never derived rates, nested analyze_metrics fields or causality claims.",
               {"summary": {"type": "string","maxLength":1200}, "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "assertions": {"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"evidence_id": {"type": "string"}, "date": {"type": "string"},
                                   "field": {"type": "string","enum":["units_sold","paid_orders","paid_buyers","paid_revenue","visitors","out_of_stock_minutes","monitored_minutes","active","campaign_name","day_type","weekday","is_makeup_workday","is_workday","holiday_name"]}, "equals": {"type": ["string", "number", "boolean"]}},
                    "required": ["evidence_id", "date", "field", "equals"]}}},
               ["summary", "evidence_ids"]),
]
SCHEMAS += [definition('get_sales_trend','Read fixed SKU daily sales series and previous-day, two-days-before and same-weekday baselines. Missing is null, never zero.'),
            definition('get_calendar_context','Read verified annual holiday/makeup-workday calendar for the fixed window; do not infer a holiday from weekday alone.')]
SCHEMAS += [definition('get_order_funnel','Read paid-date order cohorts for fixed SKU and two dates as of observed_at. Cancelled/completed/pending are exclusive states; late cancellations belong to original paid date. Never infer refunds or causality.')]
SCHEMAS += [definition('get_product_group_sales','Read frozen product-group members and dates. Sum item units and allocated revenue only with complete coverage; never sum distinct visitors, buyers or order counts.')]
# 只在广告专项注册表授权执行；销量任务不会因列出说明而获得推广账本权限。
ACTIVITY_TOOL = 'check_activity_status'
SCHEMAS.append(definition(
    ACTIVITY_TOOL,
    'Check the frozen SKU on the investigation date and previous calendar day for discount expiry and resource-slot loss. Missing status is unknown.',
    {'sku_id': {'type': 'string'}, 'date': {'type': 'string', 'format': 'date'}},
    ['sku_id', 'date'],
))

ADVERTISING_SCHEMA=definition('get_advertising_report','Read a frozen store/day paid advertising ledger. Store revenue is NOT attributed ad revenue; never infer ROI or profit.')
ADVERTISING_SETTINGS_SCHEMA=definition('get_advertising_settings','Read local operating attention thresholds and hours for the frozen store. This is NOT platform budget, bid or live campaign configuration.')
INVENTORY_RANGE_SCHEMA=definition('get_inventory_range','Read frozen product members and dates for daily stockout monitoring. Missing stays unknown; SKU-day minutes are not store elapsed time.')
PROMOTION_RANGE_SCHEMA=definition('get_promotion_range','Read frozen product members and dates for campaign states. Campaign active is NOT paid advertising status.')
# finish 只能断言原始批次计数；派生取消率由宿主计算，不作为原始来源字段。
next(schema for schema in SCHEMAS if schema['function']['name']=='finish')['function']['parameters']['properties']['assertions']['items']['properties']['field']['enum'] += [
    'cohort_paid_orders','cancelled_orders','completed_orders','pending_orders','late_cancelled_orders']


def check_activity_status(store, sku_id, day):
    """查询指定商品调查日和前一自然日，返回原始来源与可核实的活动状态。"""
    data = store.query('promotions', sku_id, day, comparison_day=previous_day(day))
    before, current = data['records']
    both_observed = before['status'] == current['status'] == 'ok'
    expired = before.get('campaign_ended_at') if before['status'] == 'ok' else None
    expired_without_renewal = both_observed and bool(expired) and current.get('active') is False
    slot_lost = (both_observed and before.get('resource_slot_active') is True
                 and current.get('resource_slot_active') is False)
    data.update(
        table='activity_status',
        campaign_ended_at=expired,
        expired_without_renewal=expired_without_renewal,
        resource_slot_lost=slot_lost,
        observation_complete=both_observed,
    )
    return data


class ToolRegistry:
    """单次调查的工具与证据容器，不跨会话共享 SKU、分析结果或完成状态。"""
    def __init__(self, store, sku_id, day, *, allowed_tools=None, allow_follow_up=False,comparison_day=None,trend_scope=None,allow_orders=False,product_context=None,group_scope=None,group_products=None,role=None):
        """宿主在创建时冻结 SKU/日期，后续模型工具只能查询这个调查对象。"""
        self.store, self.sku_id, self.day = store, sku_id, day
        self.observations = {}
        self.results = {}
        self.analysis = None
        self.finished = None
        self.comparison_day=comparison_day
        self.trend_scope=trend_scope
        self.product_context=product_context or {}
        self.group_scope=group_scope;self.group_products=group_products or []
        self.allowed_tools = set(TABLES) | {"analyze_metrics", "finish"} if allowed_tools is None else set(allowed_tools)
        if allow_follow_up:
            self.allowed_tools.add(FOLLOW_UP_TOOL)
        if trend_scope:self.allowed_tools.update({'get_sales_trend','get_calendar_context'})
        if allow_orders:self.allowed_tools.add('get_order_funnel')
        if role is not None and allowed_tools is None and ACTIVITY_TOOL in role.allowed_tools:
            self.allowed_tools.add(ACTIVITY_TOOL)
        self.role = role
        if role is not None:
            self.allowed_tools.intersection_update(role.allowed_tools)
        # 注册表保留原有按需开关；能力目录是额外上界，列入目录不等于本轮全部授予。
        contract_tools={name for spec in CAPABILITIES.values() for name in spec['tools']}
        if self.allowed_tools-contract_tools:raise ValueError('工具授权包含能力目录之外的名称。')

    def execute(self, name, arguments, call_id):
        """校验请求→执行业务函数→封装观察；业务拒绝作为 ok=False 返回供模型纠正。"""
        from .cancellation import checkpoint,CancelledError
        checkpoint()
        if call_id in self.results:
            raise ValueError("Duplicate tool call ID")
        # 角色拒绝是硬错误，不能被下方可纠正的参数错误捕获并交回模型重试。
        if self.role is not None:
            require_role_tool(self.role, name)
        verification=None;narrative_audit=None
        try:
            if name not in self.allowed_tools:
                raise ValueError("Unknown tool; only read-only investigation tools are available")
            if not isinstance(arguments, dict):
                raise ValueError("Arguments must be a JSON object")
            expected = ({"summary", "evidence_ids"} if name == "finish" else
                        {"sku_id", "date"} if name == ACTIVITY_TOOL else set())
            if set(arguments) != expected and not (name == "finish" and set(arguments) == expected | {"assertions"}):
                raise ValueError("Arguments do not match the tool contract")
            if name == ACTIVITY_TOOL:
                # 日期取调查日，前一天由宿主计算，不能由模型换商品或扩大范围。
                if arguments != {'sku_id': self.sku_id, 'date': self.day}:
                    raise ValueError('活动检查的商品与日期必须匹配冻结任务。')
                data = check_activity_status(self.store, self.sku_id, self.day)
            elif name=='get_product_group_sales':
                if not self.group_scope or not self.trend_scope:raise ValueError('需要宿主冻结商品集合与日期。')
                data=group_sales(self.store,self.group_scope,self.trend_scope['start'],self.trend_scope['end'],self.comparison_day,self.group_products)
            elif name in ('get_sales_trend','get_calendar_context'):
                scope=self.trend_scope
                if name=='get_sales_trend':
                    data=sales_trend(self.store,self.sku_id,scope['start'],scope['end'],self.comparison_day,scope.get('listed_at'))
                else:
                    start=(date.fromisoformat(scope['start'])-timedelta(days=28)).isoformat()
                    if self.comparison_day:start=min(start,self.comparison_day)
                    data=calendar_range(start,scope['end'])
                self.analysis=None
            elif name == 'get_order_funnel':
                data=order_funnel(self.store,self.sku_id,self.day,self.comparison_day)
                self.analysis=None
            elif name == FOLLOW_UP_TOOL:
                if not self.analysis or self.analysis["conflicts"] or not any(
                        item["id"] == "traffic_signal" for item in self.analysis["hypotheses"]):
                    raise ValueError("Channel follow-up requires a non-conflicting traffic signal")
                data = self.store.query("traffic_channels", self.sku_id, self.day,comparison_day=self.comparison_day)
            elif name in TABLES:
                data = self.store.query(TABLES[name], self.sku_id, self.day,comparison_day=self.comparison_day)
                # 重读来源可能改变证据，旧分析必须失效，防止用旧分析为新数据背书。
                self.analysis = None
            elif name == "analyze_metrics":
                # “查询成功但无数据”也是观察，允许进入分析器输出缺口。
                if not set(TABLES.values()).issubset(self.observations):
                    raise ValueError("Query all four sources first; unavailable results are valid observations")
                self.analysis = analyze(self.observations,self.product_context)
                if 'order_cohorts' in self.observations:
                    cohort=self.observations['order_cohorts']
                    self.analysis['order_funnel']={**cohort['data'],'evidence_id':cohort['evidence_id']}
                    for row in cohort['data']['metrics']:
                        if row['status']=='conflicting':
                            self.analysis['conflicts'].append({'kind':'paid_cohort_sales_mismatch','date':row['date'],
                                'evidence_ids':[cohort['evidence_id'],self.observations['sales']['evidence_id']]})
                            self.analysis['data_quality']='conflicting'
                        elif row['status']!='ok':
                            self.analysis['gaps'].append({'table':'order_cohorts','reason':'Missing paid cohort data','evidence_id':cohort['evidence_id']})
                            if not self.analysis['conflicts']:self.analysis['data_quality']='partial'
                if 'sales_trend' in self.observations:
                    self.analysis['comparison_notes']=comparison_notes(self.observations['sales_trend']['data'])
                    self.analysis['comparison_evidence_ids']=[self.observations['sales_trend']['evidence_id']]
                    if 'calendar' in self.observations:self.analysis['comparison_evidence_ids'].append(self.observations['calendar']['evidence_id'])
                self.analysis['assessment']=assess(self.analysis,self.observations,self.product_context)
                self.analysis['guidance']=build_guidance(self.analysis,self.observations)
                self.analysis['narrative_contract']=build_contract(self.analysis,self.observations)
                data = self.analysis
            else:
                if ACTIVITY_TOOL in self.allowed_tools and 'activity_status' not in self.observations:
                    raise ValueError('商品调查必须先调用 check_activity_status，再提交结论。')
                if self.analysis is None:
                    raise ValueError("Run analyze_metrics before finish")
                summary, refs = arguments["summary"], arguments["evidence_ids"]
                if not isinstance(summary, str) or not 1 <= len(summary) <= 1200 or not summary.strip():
                    raise ValueError("Summary must contain 1..1200 characters; prefer at most 600")
                if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                    raise ValueError("evidence_ids must be an array of strings")
                needed = {item["evidence_id"] for item in self.observations.values()}
                # 这里只验证引用真的存在，不能证明模型的每句话被引用内容支持。
                if not needed.issubset(set(refs)) or any(ref not in self.results or not self.results[ref]["ok"] for ref in refs):
                    raise ValueError("Cite existing successful evidence records for all four source queries")
                verification = verify_assertions(self.observations, arguments.get("assertions", []))
                if verification["status"] == "failed":
                    raise ValueError("Field assertions failed. Read verification checks; cite raw query evidence IDs and raw fields only, not analyze_metrics output or derived percentages.")
                narrative_audit=audit_summary(summary,build_contract(self.analysis,self.observations))
                if narrative_audit['status']=='rejected':
                    raise ValueError('Narrative checks rejected. Read narrative_audit.findings and correct the draft; do not change evidence or classification.')
                self.finished = {"summary": summary, "evidence_ids": refs, "verification": verification,
                                 "narrative_status": "unverified_model_draft","narrative_audit":narrative_audit}
                data = self.finished
            result = {"ok": True, "evidence_id": call_id, "tool": name,
                      "retrieved_at": datetime.now(timezone.utc).isoformat(), "data": data}
            if name in TABLES:
                self.observations[TABLES[name]] = result
            elif name in (ACTIVITY_TOOL,'get_sales_trend','get_calendar_context','get_order_funnel','get_product_group_sales'):self.observations[data['table']]=result
            elif name == FOLLOW_UP_TOOL:
                self.observations["traffic_channels"] = result
        except CancelledError:
            raise
        except ValueError as error:
            result = {"ok": False, "tool": name, "evidence_id": call_id, "error": str(error)}
            if verification and verification['status']=='failed':result['verification']=verification
            if narrative_audit:result['narrative_audit']=narrative_audit
        self.results[call_id] = result
        return result
