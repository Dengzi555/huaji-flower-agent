"""执行轨迹：把一次 Run 的事件、证据、字段验收与用量整理成可核对的时间线。

输入是服务端运行目录与运行 ID，输出固定结构 JSON；不执行源码、不读取密钥，
也不把客户端字符串直接拼进文件路径。页面只负责展示，事实以落盘产物为准。
"""
import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA = "trace-v1"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_EVENTS = 2000
MAX_CHECKS = 500


def _read(path, default=None):
    """读取可选产物；单个文件缺失或损坏按缺失处理，避免轨迹页整体失败。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _elapsed(start, end):
    """用事件时间戳计算耗时；格式异常返回 None，页面显示“未知”而不是 0。"""
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def _checks(value):
    """字段验收只保留白名单结构和有限行数，避免页面被超大数组拖垮。"""
    checks = value.get("checks") if isinstance(value, dict) else None
    return checks[:MAX_CHECKS] if isinstance(checks, list) else []


def _verification(value):
    """提取 finish 返回的断言结果；没有断言时返回 None，不伪造“通过”。"""
    if not isinstance(value, dict) or not value.get("status"):
        return None
    return {"status": value.get("status"), "checked": value.get("checked"), "failed": value.get("failed"),
            "scope": value.get("scope"), "checks": _checks(value)}


def _events(directory):
    """读取 JSONL 事件并按序号排序；坏行跳过，不因一条日志损坏隐藏整次运行。"""
    rows = []
    try:
        lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and type(entry.get("sequence")) is int:
            rows.append(entry)
    rows.sort(key=lambda item: item["sequence"])
    return rows[:MAX_EVENTS]


def _timeline(events):
    """把模型调用、工具调用和宿主事件压成按发生顺序排列的步骤。"""
    steps, tool_started, model_started = [], {}, {}
    for entry in events:
        kind, at = entry.get("type"), entry.get("at")
        if kind == "run_started":
            steps.append({"kind": "started", "at": at, "mode": entry.get("mode")})
        elif kind == "model_started":
            model_started[entry.get("step")] = at
        elif kind == "model_completed":
            step = entry.get("step")
            steps.append({"kind": "model", "step": step, "at": at, "tool_count": entry.get("tool_count"),
                          "duration_ms": _elapsed(model_started.pop(step, None), at)})
        elif kind == "tool_started":
            tool_started[entry.get("call_id")] = {"at": at, "tool": entry.get("tool")}
        elif kind in ("tool_completed", "tool_rejected"):
            result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            started = tool_started.pop(entry.get("call_id"), {}) or {}
            step = {"kind": "tool" if kind == "tool_completed" else "tool_rejected",
                    "tool": entry.get("tool") or started.get("tool"), "call_id": entry.get("call_id"), "at": at,
                    "duration_ms": _elapsed(started.get("at"), at),
                    "ok": result.get("ok") is True, "evidence_id": result.get("evidence_id"),
                    "error": result.get("error")}
            verification = _verification(result.get("verification") or data.get("verification"))
            if verification:
                step["verification"] = verification
            if entry.get("tool") == "finish":
                step["evidence_ids"] = data.get("evidence_ids") or []
                step["answer"] = (data.get("summary") or "")[:280]
            steps.append(step)
        elif kind == "follow_up_decision":
            steps.append({"kind": "note", "at": at, "label": "追加调查决策",
                          "detail": f"selected={entry.get('selected')} · reason={entry.get('reason')}"})
        elif kind == "follow_up_settled":
            steps.append({"kind": "note", "at": at, "label": "追加调查结算",
                          "detail": json.dumps(entry.get("follow_up") or {}, ensure_ascii=False)[:300]})
        elif kind == "run_settled":
            steps.append({"kind": "settled", "at": at, "status": entry.get("status"),
                          "stop_reason": entry.get("stop_reason")})
    # 取消/崩溃时可能只有 started 没有 completed；保留待定态，不假装工具已返回。
    for call_id, started in tool_started.items():
        steps.append({"kind": "tool_pending", "call_id": call_id, "at": started.get("at"),
                      "tool": started.get("tool")})
    return steps


def build_trace(output, run_id):
    """读取 output/run_id 下的固定产物，返回可直接序列化的轨迹对象。

    output 是服务端运行根目录；run_id 必须是白名单字符，防止路径穿越。
    缺少 run.json 时仍可展示事件，但状态、用量等字段会如实留空。
    """
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("运行 ID 不合法。")
    root = Path(output)
    directory = root / run_id
    if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root.resolve()):
        raise FileNotFoundError("运行记录不存在。")
    run = _read(directory / "run.json", {})
    run = run if isinstance(run, dict) else {}
    events = _events(directory)
    steps = _timeline(events)
    request = _read(directory / "request.json", {})
    request = request if isinstance(request, dict) else {}
    evidence = _read(directory / "evidence.json", {})
    observations = evidence.get("observations") if isinstance(evidence, dict) else None
    rows = []
    for item in observations if isinstance(observations, list) else []:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        records, groups = data.get("records"), data.get("groups")
        rows.append({"evidence_id": item.get("evidence_id"), "tool": item.get("tool"),
                     "table": data.get("table"), "retrieved_at": item.get("retrieved_at"),
                     "rows": len(records) if isinstance(records, list)
                     else len(groups) if isinstance(groups, list) else None})
    validation = _read(directory / "validation.json") or run.get("validation")
    validation = validation if isinstance(validation, dict) else None
    usage = run.get("model_usage") if isinstance(run.get("model_usage"), dict) else {}
    rejections = sum(1 for step in steps if step["kind"] == "tool_rejected")
    tool_calls = run.get("tool_calls")
    model_calls = run.get("model_calls")
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": run.get("status"),
        "mode": run.get("mode"),
        "stop_reason": run.get("stop_reason"),
        "duration_ms": run.get("duration_ms"),
        "sku_id": run.get("sku_id"),
        "target_date": run.get("target_date"),
        "timezone": run.get("timezone"),
        "sample_data": run.get("sample_data"),
        "question": request.get("raw_question") or request.get("question"),
        "intent": request.get("intent") or run.get("intent"),
        "parser": request.get("parser"),
        "summary": {
            "tool_calls": tool_calls if type(tool_calls) is int else sum(1 for s in steps if s["kind"].startswith("tool")),
            "model_calls": model_calls if type(model_calls) is int else sum(1 for s in steps if s["kind"] == "model"),
            "rejections": rejections,
            "evidence": len(rows),
            "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens"),
                       "total": usage.get("total_tokens"), "cost": usage.get("cost")} if usage else None,
            "assertions": {"status": validation.get("status"), "checked": validation.get("checked"),
                           "failed": validation.get("failed")} if validation else None,
            "duration_ms": run.get("duration_ms"),
        },
        "steps": steps,
        "evidence": rows,
        "validation": validation,
        "artifacts": {name: (directory / name).is_file()
                      for name in ("report.md", "evidence.json", "events.jsonl", "validation.json")},
    }