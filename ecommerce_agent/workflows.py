"""工作流分发：共用任务卡与证据契约，为不同意图只授予所需的只读工具。

销量调查复用既有执行器；库存和活动专项不查询销售、流量，也不调用模型。
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .routing import ROUTES
from .runner import run, write_json
from .tools import ToolRegistry
from .verification import observation_assertions, verify_assertions
from .operating_guidance import build_guidance,guidance_markdown
from .capabilities import validate_plan
from .cancellation import checkpoint,current_token,CancelledError,cancel_state


def execute_task(store, sku_id, day, output, *, task_context, provider=None,
                 max_steps=16, timeout_seconds=180, on_event=None):
    """校验已准入任务，按白名单意图分发；返回统一运行状态与落盘产物。"""
    if (task_context.get("status") != "ready" or task_context.get("sku_id") != sku_id
            or task_context.get("target_date") != day):
        raise ValueError("Task context must be ready and match the execution SKU/date")
    intent = task_context.get("intent")
    if intent not in ROUTES:
        raise ValueError("Unsupported workflow intent")
    # 路由只识别意图；冻结计划仍需在取数前独立复核，不能信任模型自报参数。
    validate_plan(task_context,store)
    if intent=='query_combined':
        from .combined_query import execute_combined
        return execute_combined(store,task_context,output,on_event=on_event,timeout_seconds=timeout_seconds)
    if task_context.get('product_scope'):
        if intent!='query_sales_performance' or provider is not None:raise ValueError('品类范围当前只支持确定性销量表现查询。')
        return lookup(store,sku_id,day,output,task_context,on_event)
    if intent == "investigate_sales_change":
        return run(store, sku_id, day, output, provider=provider, max_steps=max_steps,
                   timeout_seconds=timeout_seconds, on_event=on_event, task_context=task_context,
                   adaptive=provider is None)
    if provider is not None:
        raise ValueError("库存/活动专项当前只支持确定性查询，不需要模型配置。")
    return lookup(store, sku_id, day, output, task_context, on_event)


def lookup(store, sku_id, day, output, task, on_event):
    """执行一个受限专项查询，保留原问题、来源、事件、报告与证据哈希。"""
    started = time.perf_counter()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = Path(output).resolve() / run_id
    directory.mkdir(parents=True)
    intent = task["intent"]
    tool = 'get_product_group_sales' if task.get('product_scope') else {"query_inventory": "get_inventory_status", "query_promotions": "get_promotion_history",
            "query_sales_performance": "get_sales_metrics"}[intent]
    scope={'start':task['trend_start'],'end':task['trend_end'],'listed_at':next((p.get('listed_at') for p in task.get('candidates',[]) if p['sku_id']==sku_id),None)} if task.get('include_trend') else None
    registry = ToolRegistry(store, sku_id, day, allowed_tools=set(validate_plan(task,store)),comparison_day=task.get('comparison_date'),trend_scope=scope,
        group_scope=task.get('product_scope'),group_products=task.get('candidates'))
    state = {"run_id": run_id, "sku_id": sku_id, "target_date": day, "status": "running",
             "mode": "deterministic_lookup", "intent": intent, **store.meta, "run_dir": str(directory),
             "task_context": task, "tool_calls": 0, "model_calls": 0, "analysis": None, "model_draft": None}
    entries = []

    def event(kind, **values):
        """先落盘再向 UI 通知，让专项查询与旧调查保持相同事件结构。"""
        entry = {"sequence": len(entries) + 1, "run_id": run_id, "type": kind,
                 "at": datetime.now(timezone.utc).isoformat(), **values}
        entries.append(entry)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if on_event:
            on_event(entry)

    write_json(directory / "request.json", task)
    try:
        checkpoint()
        event("run_started", mode=state["mode"])
        event("workflow_selected", intent=intent, reason="只调用当前意图必需的工具。")
        if scope and not task.get('product_scope'):
            for name,identity in [('get_sales_trend','trend-source'),('get_calendar_context','calendar-source')]:
                event('tool_started',tool=name,call_id=identity)
                result=registry.execute(name,{},identity);state['tool_calls']+=1
                event('tool_completed',tool=name,call_id=identity,result=result)
                if not result['ok']:raise ValueError(result['error'])
        event("tool_started", tool=tool, call_id="source-1")
        result = registry.execute(tool, {}, "source-1")
        state["tool_calls"] += 1
        event("tool_completed", tool=tool, call_id="source-1", result=result)
        if not result["ok"]:
            raise ValueError(result["error"])
        if task.get('product_scope'):
            data=result['data'];trend=data['trend'];chosen=trend['summary'] if task['is_window'] else trend['records'][-1]
            units=chosen.get('units_sum') if task['is_window'] else chosen.get('units_sold')
            revenue=chosen.get('revenue_sum') if task['is_window'] else chosen.get('paid_revenue')
            state['analysis']={'kind':'lookup','title':task['product_scope']['label']+' · 销量表现',
                'overview':f"本轮范围包含{len(data['items'])}款商品，逐款明细如下。",'facts':{},'hypotheses':[],
                'data_quality':'partial' if units is None or task['is_window'] and not trend['summary']['complete_window'] else 'complete_for_supplied_daily_fields','gaps':[],'conflicts':[],
                'evidence_ids':['source-1'],'limitations':data['limitations'],'group_items':data['items'],
                'display_metrics':[{'label':'已取回日期销量合计' if task['is_window'] and not trend['summary']['complete_window'] else '成品销量合计','value':units if units is not None else '—','unit':'束/桶'},
                    {'label':'商品分摊支付金额','value':revenue if revenue is not None else '—','unit':'元'}]}
            if data.get('ranking'):
                rank=data['ranking'];state['analysis']['ranking']=rank
                state['analysis']['overview']+=rank['assumption']
                for part in rank.get('partitions',[rank]):
                    if not part['leaders']:continue
                    prefix=('销量最低' if part['direction']=='asc' else '销量最高') if rank['complete_scope'] else '可比记录中的'+('最低' if part['direction']=='asc' else '最高')
                    names='、'.join(item['name']+' · '+item['variant'] for item in part['leaders'][:3])
                    if len(part['leaders'])>3:names+=f"等{len(part['leaders'])}款"
                    state['analysis']['display_metrics'].append({'label':prefix+('（并列）' if len(part['leaders'])>1 else ''),'value':names,'unit':''})
                if rank.get('coverage_note'):
                    state['analysis']['overview']+=rank['coverage_note']
                    state['analysis']['limitations'].append(rank['coverage_note'])
                if not rank['leaders']:
                    state['analysis']['overview']='所选范围没有足够的可比销售记录，暂不能判断最好或最差；下方保留逐款数据覆盖情况。'
                if not rank['complete_scope']:state['analysis']['limitations'].append('部分商品缺少完整期间数据，排名仅覆盖可比记录，不能认定全范围最好或最差。')
            state['sales_trend']=trend;state['calendar_context']=data['calendar']
            if data.get('selected_comparison'):
                state['analysis']['selected_comparison']=data['selected_comparison']
                state['selected_sales_trends']=data['selected_trends']
                state['sales_trend']['display_label']=task['product_scope']['label']+'（整体）'
                state['analysis']['overview']+=' 已先选出入选商品，再比较这些商品的同一SKU历史销量。'
        else:state["analysis"] = summarize_lookup(intent, result)
        if scope and not task.get('product_scope'):
            from .sales_trend import comparison_notes
            trend=registry.observations['sales_trend']['data'];state['sales_trend']=trend
            state['calendar_context']=registry.observations['calendar']['data']
            state['analysis']['comparison_notes']=comparison_notes(trend)
            state['analysis']['evidence_ids']=['source-1','trend-source','calendar-source']
            if task.get('is_window'):
                state['analysis'].update(title='多日销量表现',overview=f"窗口内已取回{trend['coverage']['available_days']}/{trend['coverage']['requested_days']}天销量。汇总只覆盖可用日期。",
                    data_quality='complete_for_supplied_daily_fields' if trend['summary']['complete_window'] else 'partial',
                    limitations=['仅汇总窗口中可用日期，不把缺失或未上架补成零。','对比卡片针对窗口末日；曲线按日期对齐，日历/活动背景不构成因果或季节调整。'],
                    display_metrics=[{'label':'可用日期销量合计','value':trend['summary']['units_sum'],'unit':'束/桶'},
                        {'label':'可用日期支付金额','value':trend['summary']['revenue_sum'],'unit':'元'},
                        {'label':'数据覆盖','value':f"{trend['coverage']['available_days']}/{trend['coverage']['requested_days']}",'unit':'天'}])
        state['analysis']['guidance']=build_guidance(state['analysis'],registry.observations,task)
        state.update(status="completed", stop_reason="read_only_lookup_completed")
    except CancelledError:
        cancel_state(state)
    except Exception as error:
        from .errors import from_exception
        problem=from_exception(error,phase='execution',fallback='execution_failed').public()
        state.update(status="failed", stop_reason="execution_error", error=problem['message'],error_info=problem)
    finally:
        if current_token() and current_token().event.is_set():cancel_state(state)
        state["duration_ms"] = round((time.perf_counter() - started) * 1000)
        evidence = {"observations": list(registry.observations.values())}
        state["validation"] = verify_assertions(registry.observations, observation_assertions(registry.observations))
        state["evidence_sha256"] = hashlib.sha256(json.dumps(evidence, ensure_ascii=False,
                                                             sort_keys=True).encode()).hexdigest()
        write_json(directory / "evidence.json", evidence)
        write_json(directory / "validation.json", state["validation"])
        write_json(directory / "run.json", state)
        analysis = state["analysis"] or {}
        lines = ["# " + ROUTES[intent]["label"], "", '状态：'+state['status'],
                 '已停止，以下仅为已取回的部分数据，不代表查询完成。' if state['status']=='cancelled' else '',
                 "数据：" + ("学习用合成经营快照" if state["sample_data"] else "用户提供，未独立核验"),
                 "", "## 原问题", task["raw_question"], "", f"SKU：{sku_id}；目标日：{day}",
                 "", "## 路由依据", task["routing"]["reason"], "", "## 查询结果", analysis.get("overview", "未生成结果。")]
        lines += [f"- {item['label']}：{item['value']}{item.get('unit', '')}（证据 source-1）"
                  for item in analysis.get("display_metrics", [])]
        if analysis.get('guidance'):lines+=guidance_markdown(analysis['guidance'])
        if analysis.get('group_items'):
            lines+=['','## 商品明细','| 商品 | 规格 | 可用日期销量 | 可用天数 |','|---|---|---|---|']
            for item in analysis['group_items']:lines.append(f"| {item['name']} | {item['variant']} | {item['units_sold']} | {item['available_days']}/{item['requested_days']} |")
            if analysis.get('ranking'):lines+=['',analysis['ranking']['assumption']]
            if analysis.get('ranking'):
                for part in analysis['ranking'].get('partitions',[]):
                    lines+=['','### '+('销量最高' if part['direction']=='desc' else '销量最低')]
                    lines += [f"- {item['name']} · {item['variant']}：{item['units_sold']}束/桶" for item in part['rows']]
        if analysis.get('selected_comparison'):
            comparison=analysis['selected_comparison']
            lines+=['','## 入选商品历史对比',comparison['scope_note']]
            for item in comparison['rows']:
                lines.append(f"- {item['name']}（{item['sku_id']}）：{item['date']} {item['baseline_units']} → {item['target_date']} {item['target_units']}；差额 {item['absolute_change']}；变化 {item['relative_pct']}%。")
        lines += ["", "## 边界与缺口", *["- " + note for note in analysis.get("limitations", [])],
                  "- completed 表示查询完成，不代表业务问题已解决。", "", "完整原始记录见 evidence.json；执行过程见 events.jsonl。"]
        if state.get('sales_trend'):
            lines += ['', '## 多日对比背景',*[ '- '+note for note in analysis.get('comparison_notes',[])]]
            for item in state['sales_trend']['comparisons']:lines.append(f"- {item['label']} {item['date']}：销量 {item['baseline_units']} → {item['target_units']}；变化 {item['relative_pct']}%。")
        (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        event("run_settled", status=state["status"], stop_reason=state["stop_reason"])
    return state


def summarize_lookup(intent, observation):
    """从观察记录生成专项展示数据，缺失值保持未知，活动状态不推导经营效果。"""
    rows = observation["data"]["records"]
    target = rows[-1]
    result = {"kind": "lookup", "title": ROUTES[intent]["label"], "facts": {}, "hypotheses": [],
              "conflicts": [], "gaps": [], "records": rows, "evidence_ids": [observation["evidence_id"]],
              "data_quality": "complete_for_supplied_daily_fields", "display_metrics": [], "limitations": []}
    if target["status"] != "ok":
        result.update(data_quality="partial", overview="目标日记录不可用，不能推断实际状态。")
        result["limitations"].append("来源缺失或延迟不等于零；需要补充目标日数据。")
        return result
    if intent == "query_sales_performance":
        result["overview"] = f"目标日售出 {target['units_sold']} 件成品，支付订单 {target['paid_orders']} 单。"
        result["display_metrics"] = [{"label": "成品销量", "value": target["units_sold"], "unit": "束/桶"},
                                     {"label": "支付订单", "value": target["paid_orders"], "unit": "单"},
                                     {"label": "支付金额", "value": target["paid_revenue"], "unit": "元"}]
        result["limitations"] = ["仅查询目标日销售事实，不判断好坏或下降原因。", "新品缺少上架前销售记录时，不补零，也不计算增长率。"]
    elif intent == "query_inventory":
        minutes, monitored = target["out_of_stock_minutes"], target["monitored_minutes"]
        result["overview"] = f"目标日已监测 {monitored} 分钟，其中缺货 {minutes} 分钟。"
        result["display_metrics"] = [{"label": "缺货时长", "value": minutes, "unit": "分钟"},
                                     {"label": "监测时长", "value": monitored, "unit": "分钟"},
                                     {"label": "监测内非缺货时长", "value": monitored - minutes, "unit": "分钟"}]
        result["limitations"] = ["没有实时库存件数，不能回答当前还能卖多少件。", "未交叉核对订单，库存记录本身不能证明销量损失。"]
        if monitored < 1440:
            result["data_quality"] = "partial"
            result["limitations"].append("监测不足全天，不能推断未监测时段是否缺货。")
    else:
        result["overview"] = "目标日活动开启。" if target["active"] else "目标日没有开启活动的记录。"
        result["display_metrics"] = [{"label": "目标日活动", "value": "开启" if target["active"] else "未开启"},
                                     {"label": "活动名称", "value": target["campaign_name"] or "—"}]
        result["limitations"] = ["记录为日级活动状态，不包含优惠金额或活动效果。", "活动状态变化不等于已经证明其导致销量变化。"]
    if rows[0]["status"] != "ok":
        result["limitations"].append("所选基线来源不可用，不能计算跨日变化。")
    return result
