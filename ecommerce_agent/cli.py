"""命令行入口：解析用户参数，选择业务流程，再打印产物位置。

学习时先读 main() 的分支：demo 是模拟案例，investigate 是结构化参数，
ask 先解析中文任务，agent 则使用真实模型请求工具。这里不做指标计算。
"""

import argparse
import json
import uuid
from datetime import date
from pathlib import Path

from .data import DataStore
from .fixtures import CASES, sample_data
from .provider import ChatProvider
from .runner import run, write_json
from .task_parser import ProductCatalog, parse_task
from .tools import SCHEMAS
from .routing import ROUTES, route_question
from .workflows import execute_task

ROOT = Path(__file__).resolve().parent.parent


def progress(event):
    """把工具结果投影到终端；ok 表示工具执行成功，不表示业务数据完整。"""
    if event["type"] == "tool_completed":
        print(f"  {event['tool']}: {'ok' if event['result']['ok'] else 'rejected'}", flush=True)


def ask(args):
    """先生成可审查任务卡，仅 ready 状态进入调查；返回状态与进程退出码。"""
    store = DataStore(args.data)
    catalog = ProductCatalog(args.catalog, store.skus)
    task = parse_task(args.question, catalog, store,
                      today=date.fromisoformat(args.today) if args.today else None,
                      sku_override=args.sku, date_override=args.date, intent_override=args.intent)
    # 被拦住的请求也保留解析结果，方便理解为什么没有调用业务工具。
    request_path = args.output / "requests" / (uuid.uuid4().hex[:12] + ".json")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(request_path, task)
    print(f"任务状态：{task['status']}；解析器：{task['parser']}")
    print(f"SKU：{task['sku_id']}；日期：{task['target_date']}；对比日：{task['comparison_date']}")
    print(f"数据：经营快照；业务今天：{task['business_today']}（数据来源见数据说明）")
    for candidate in task["candidates"]:
        print(f"  候选：{candidate['sku_id']} — {candidate['name']} {candidate['variant']}")
    for assumption in task["assumptions"]:
        print("  假设：", assumption)
    for issue in task["issues"]:
        print("  待处理：", issue)
    print("任务卡：", request_path.resolve())
    if task["status"] != "ready" or args.preview:
        # 3 是“需要补信息/数据”，不是成功完成调查；preview 永远不执行数据工具。
        return [], 0 if task["status"] == "ready" else 3
    config = {}
    provider = None
    if args.model_config:
        config = json.loads(args.model_config.read_text(encoding="utf-8-sig"))
        provider = ChatProvider(config)
    state = execute_task(store, task["sku_id"], task["target_date"], args.output, provider=provider,
                max_steps=config.get("max_steps", 16), timeout_seconds=config.get("timeout_seconds", 180),
                on_event=progress, task_context=task)
    return [state], 0 if state["status"] == "completed" else 1


def main():
    """组装命令，不默认发出模型请求；只有 agent / --model-config 启用模型。"""
    parser = argparse.ArgumentParser(description="Read-only ecommerce SKU investigation prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run synthetic cases with a deterministic workflow; no API")
    demo.add_argument("--case", choices=["all", *CASES], default="all")
    investigate = commands.add_parser("investigate", help="Investigate a SKU from a normalized JSON snapshot")
    investigate.add_argument("--data", type=Path, default=ROOT / "data/sample.json")
    investigate.add_argument("--sku", required=True)
    investigate.add_argument("--date", required=True)
    investigate.add_argument("--output", type=Path, default=ROOT / "runs")
    natural = commands.add_parser("ask", help="Parse a Chinese sales question, clarify SKU/date, then investigate")
    natural.add_argument("question", help="例如：帮我查怦然心动2026-10-23销量下降原因")
    natural.add_argument("--data", type=Path, default=ROOT / "data/sample.json")
    natural.add_argument("--catalog", type=Path, default=ROOT / "data/products.json")
    natural.add_argument("--output", type=Path, default=ROOT / "runs")
    natural.add_argument("--sku", help="补全或消歧商品；不覆盖问题中矛盾的明确 SKU")
    natural.add_argument("--date", help="补全目标日期 YYYY-MM-DD")
    natural.add_argument("--intent", choices=list(ROUTES), help="在混合意图候选中明确选择一项")
    natural.add_argument("--today", help="演示/测试的业务今天 YYYY-MM-DD；默认 Asia/Shanghai 当前日期")
    natural.add_argument("--preview", action="store_true", help="只展示并保存任务卡，不调用业务工具")
    natural.add_argument("--model-config", type=Path, help="可选模型配置；不传则执行确定性调查")
    live = commands.add_parser("agent", help="Let a real model choose tool calls; requires model configuration")
    live.add_argument("--config", type=Path, required=True)
    comparison = commands.add_parser("compare-model", help="显式调用模型，与规则基线对照；可能产生 API 费用")
    comparison.add_argument("--config", type=Path, required=True)
    comparison.add_argument("--output", type=Path, default=ROOT / "runs/model-comparison")
    commands.add_parser("tools", help="Print tool schemas for adapter development (MCP: ecommerce_agent mcp)")
    route = commands.add_parser("route", help="只查看意图路由依据，不执行工具")
    route.add_argument("question")
    evaluation = commands.add_parser("evaluate-routing", help="运行离线路由回归集，不调用模型")
    evaluation.add_argument("--cases", type=Path, default=ROOT / "data/routing_eval.json")
    evaluation.add_argument("--output", type=Path, default=ROOT / "runs/evaluation")
    web = commands.add_parser("web", help="启动网页工作台、API 与单线程后台调查服务")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument("--data", type=Path, default=ROOT / "data/sample.json")
    web.add_argument("--catalog", type=Path, default=ROOT / "data/products.json")
    web.add_argument("--state-dir", type=Path, default=ROOT / "web-data/flowers")
    web.add_argument("--output", type=Path, default=ROOT / "runs/flowers")
    briefing = commands.add_parser("briefing", help="扫描一家门店，输出值得调查的 3–5 条每日简报")
    briefing.add_argument("--store", help="年度数据中的门店 ID，例如 hx-cq-na")
    briefing.add_argument("--date", required=True, help="目标日期 YYYY-MM-DD")
    briefing.add_argument("--annual", type=Path, default=ROOT / "data/annual", help="年度数据根目录（默认读取 active.json）")
    briefing.add_argument("--data", type=Path, help="单文件快照；提供后替代 --store")
    briefing.add_argument("--catalog", type=Path, default=ROOT / "data/products.json", help="与 --data 配套的目录")
    briefing.add_argument("--limit", type=int, default=5, help="最多输出的候选条数，默认 5")
    briefing.add_argument("--output", type=Path, help="简报输出目录；默认 runs/briefing/<store>/<date>")
    briefing.add_argument("--json", action="store_true", help="把完整结果打印到 stdout")
    tunnel = commands.add_parser("tunnel", help="启动带口令的临时 Cloudflare Tunnel 演示")
    tunnel.add_argument("--port", type=int, default=8081)
    tunnel.add_argument("--cloudflared", type=Path, default=ROOT / ".tools/cloudflared.exe")
    mcp = commands.add_parser("mcp", help="以 stdio MCP Server 暴露四个只读工具（Claude Desktop / Cursor）")
    mcp.add_argument("--data", type=Path, default=ROOT / "data/sample.json")
    mcp.add_argument("--catalog", type=Path, default=ROOT / "data/products.json")
    args = parser.parse_args()
    try:
        if args.command == "compare-model":
            from .model_comparison import compare
            result = compare(args.config, args.output)
            print("对照报告：", (args.output / "comparison.md").resolve())
            return 0 if result["model"]["status"] == "completed" else 1
        if args.command == "evaluate-routing":
            from .evaluation import evaluate
            report = evaluate(args.cases, args.output)
            print(f"离线路由回归：{report['correct']}/{report['total']}；模型调用：{report['model_calls']}")
            print("报告：", (args.output / "routing-evaluation.md").resolve())
            return 0 if report["correct"] == report["total"] else 1
        if args.command == "route":
            print(json.dumps(route_question(args.question), ensure_ascii=False, indent=2))
            return 0
        if args.command == "tunnel":
            from .tunnel import serve_tunnel
            return serve_tunnel(args)
        if args.command == "mcp":
            # 延迟导入，模型客户端只有在真正启动 Server 时才加载目录与数据。
            from .mcp_server import serve
            return serve(args)
        if args.command == "briefing":
            from .briefing import run_briefing
            return run_briefing(args)
        if args.command == "web":
            # 延迟导入，保持原命令行工具独立；Web 只是同一业务内核的新入口。
            from .web import serve
            return serve(args)
        if args.command == "tools":
            print(json.dumps(SCHEMAS, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ask":
            states, code = ask(args)
            if not states:
                return code
        elif args.command == "demo":
            store = DataStore.from_document(sample_data())
            print("SYNTHETIC DATA / DETERMINISTIC WORKFLOW / NO MODEL CALLS")
            skus = CASES if args.case == "all" else [args.case]
            states = []
            for sku in skus:
                print(f"\n{sku}: {CASES[sku]}")
                states.append(run(store, sku, "2026-10-23", ROOT / "runs", on_event=progress))
        elif args.command == "investigate":
            states = [run(DataStore(args.data), args.sku, args.date, args.output, on_event=progress)]
        else:
            path = args.config.resolve(strict=True)
            config = json.loads(path.read_text(encoding="utf-8-sig"))
            states = [run(DataStore(path.parent / config["data_file"]), config["sku_id"], config["target_date"],
                          path.parent / config.get("output_dir", "runs"), provider=ChatProvider(config),
                          max_steps=config.get("max_steps", 16), timeout_seconds=config.get("timeout_seconds", 180),
                          on_event=progress)]
        for state in states:
            print(f"\n{state['sku_id']}: {state['status']} ({state['stop_reason']})")
            print(f"Report: {Path(state['run_dir']) / 'report.md'}")
            if state.get("error"):
                print("Error:", state["error"])
        return 0 if all(state["status"] == "completed" for state in states) else 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Input/configuration error: {error}")
        return 2
