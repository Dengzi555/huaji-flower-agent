"""同输入的规则/模型对照：只有显式执行 compare-model 才发出可能计费的模型请求。

比较完成状态、调用、时间、用量和有限字段验收，不自动评判因果叙述。
"""

import json
from pathlib import Path

from .data import DataStore
from .provider import ChatProvider
from .runner import run, write_json


def compare(config_path: Path, output: Path):
    """验证本地配置后，以同一个只读快照运行两种模式，输出可复查报告。"""
    path = config_path.resolve(strict=True)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    provider = ChatProvider(config)
    store = DataStore(path.parent / config["data_file"])
    sku, day = config["sku_id"], config["target_date"]
    # 固定四源基线，保持工具范围一致，不将额外渠道查询混入本轮对照。
    baseline = run(store, sku, day, output / "baseline")
    model = run(store, sku, day, output / "model", provider=provider,
                max_steps=config.get("max_steps", 10), timeout_seconds=config.get("timeout_seconds", 120))
    fields = ("run_id", "status", "stop_reason", "duration_ms", "tool_calls", "model_calls", "validation", "model_usage")
    result = {"sku_id": sku, "target_date": day, "sample_data": store.meta["sample_data"],
              "scope": "同一固定四源只读任务；有限字段验收不是自然语言事实忠实度或因果评测。",
              "baseline": {key: baseline.get(key) for key in fields}, "model": {key: model.get(key) for key in fields}}
    write_json(output / "comparison.json", result)
    lines = ["# 规则与模型对照", "", result["scope"], "", "| 模式 | 执行状态 | 工具调用 | 模型调用 | 耗时毫秒 | 字段验收 |", "|---|---|---|---|---|---|"]
    for name in ("baseline", "model"):
        row = result[name]
        lines.append(f"| {name} | {row['status']} | {row['tool_calls']} | {row['model_calls']} | {row['duration_ms']} | {(row['validation'] or {}).get('status')} |")
    lines += ["", "模型上报用量：" + json.dumps(result["model"]["model_usage"], ensure_ascii=False),
              "", "费用未知，请核对提供商账单；模型未上报的 token 也保留未知。", "",
              f"规则报告：{Path(baseline['run_dir']) / 'report.md'}", f"模型报告：{Path(model['run_dir']) / 'report.md'}",
              "", "请人工比较候选结论是否有依据、是否承认缺口、是否把相关性误称为因果。一次运行不能代表稳定准确率。"]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
