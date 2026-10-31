"""离线路由评测：按类别统计精确率/召回率，并报告耗时与危险请求误放行。

测试集是人工构造回归集，不能据此宣称线上准确率或业务收益。
仅评价意图路由；商品解析、工作流正确性和模型叙述需另外评测。
"""

import json
import math
from pathlib import Path

from .routing import ROUTES, route_question
from .runner import write_json


def evaluate(source: Path, output: Path):
    """读取带期望标签的数据集，执行路由并保存逐条结果、混淆矩阵和汇总。"""
    dataset = json.loads(source.read_text(encoding="utf-8-sig"))
    cases = dataset.get("cases")
    labels = [*ROUTES, "needs_clarification", "unsupported"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation requires a non-empty cases array")
    results = []
    matrix = {label: {other: 0 for other in labels} for label in labels}
    for case in cases:
        if not isinstance(case, dict) or case.get("expected") not in labels:
            raise ValueError("Invalid evaluation label")
        routing = route_question(case["question"])
        predicted = routing["intent"] if routing["status"] == "selected" else routing["status"]
        matrix[case["expected"]][predicted] += 1
        results.append({**case, "predicted": predicted, "correct": case["expected"] == predicted, "routing": routing})
    scores = {}
    for label in labels:
        tp = matrix[label][label]
        predicted = sum(matrix[other][label] for other in labels)
        actual = sum(matrix[label].values())
        scores[label] = {"precision": round(tp / predicted, 4) if predicted else None,
                         "recall": round(tp / actual, 4) if actual else None, "support": actual}
    timings = sorted(row["routing"]["duration_ms"] for row in results)
    # nearest-rank 百分位；短规则耗时受计时分辨率/机器负载影响，不外推公网端到端延迟。
    percentile = lambda p: timings[max(0, math.ceil(len(timings) * p) - 1)]
    report = {"dataset": str(source), "scope": dataset.get("description"), "total": len(results),
              "correct": sum(row["correct"] for row in results), "per_class": scores, "confusion_matrix": matrix,
              "route_latency_ms": {"p50": percentile(.5), "p95": percentile(.95)},
              "model_calls": sum(row["routing"]["model_calls"] for row in results),
              "unsupported_selected_count": sum(row["expected"] == "unsupported" and row["predicted"] in ROUTES for row in results),
              "results": results}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "routing-evaluation.json", report)
    lines = ["# 离线路由回归评测", "", str(report["scope"]), "", f"通过：{report['correct']} / {report['total']}",
             f"不支持请求被选入工作流：{report['unsupported_selected_count']}", f"路由模型调用数：{report['model_calls']}",
             "", "## 按类别结果", "", "| 类别 | 精确率 | 召回率 | 样本数 |", "|---|---|---|---|"]
    lines += [f"| {label} | {score['precision']} | {score['recall']} | {score['support']} |" for label, score in scores.items()]
    lines += ["", "## 错误样本", *[f"- {row['question']}：期望 {row['expected']}，实际 {row['predicted']}" for row in results if not row["correct"]],
              "", "本机路由耗时（毫秒）：" + json.dumps(report["route_latency_ms"]),
              "", "不能将该回归集结果写成线上用户准确率；未测独立盲测集、真实模型效果或经营收益。"]
    (output / "routing-evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
