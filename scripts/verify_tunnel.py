"""从公网入口验证模拟调查链路；口令只从本机文件读取，不打印到日志。

运行：python -X utf8 scripts/verify_tunnel.py。会创建一个明确标记的模拟任务。
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    """验证拒绝未授权、提交、完成、下载和跨站拦截，失败时立即报错。"""
    state = ROOT / "web-data/tunnel/flowers"
    base = (state / "url.txt").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", base):
        raise ValueError("只向本次 Cloudflare 临时域名发送演示访问口令。")
    token = (state / "access-token.txt").read_text(encoding="utf-8").strip()

    def request(path, payload=None, authorized=True, origin=None, store=None):
        """发送真实 HTTPS 请求；只返回状态与内容，不记录认证头。"""
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        if store:
            headers["X-Store-ID"] = store
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    assert request("/", authorized=False)[0] == 200
    assert request("/api/tasks", authorized=False)[0] == 401
    status, raw = request("/api/meta")
    assert status == 200 and json.loads(raw)["sample_data"] is True, "Only verify synthetic demo workspaces"
    status, raw = request("/api/stores")
    shops = json.loads(raw)["stores"]
    assert status == 200 and len(shops) == 6
    for path in ("/api/missions","/api/memory","/api/overview","/api/followups","/api/settings"):
        assert request(path,authorized=False)[0]==401
        assert request(path,store=shops[0]["id"])[0]==200
    assert request("/api/missions/"+"0"*32)[0]==404
    ad_path = "/api/advertising?date=2026-10-25"
    assert request(ad_path, authorized=False)[0] == 401
    status, raw = request(ad_path, store=shops[0]["id"])
    ad = json.loads(raw)
    assert status == 200 and len(ad["hours"]) == 24 and 18 in ad["attention"]["high"]
    status, raw = request(ad_path, store=shops[1]["id"])
    other = json.loads(raw)
    assert status == 200 and ad["fingerprint"] != other["fingerprint"]
    assert ad["totals"]["revenue_cents"] != other["totals"]["revenue_cents"]
    assert request(ad_path + "&high=NaN")[0] == 400
    assert request("/api/tasks", {"question": "怦然心动2026-10-23销量下降原因"},
                   authorized=False)[0] == 401
    status, raw = request("/api/tasks", {"question": "怦然心动2026-10-23销量下降原因"}, origin=base)
    assert status == 201, status
    task_id = json.loads(raw)["task"]["id"]
    assert request("/api/tasks/" + task_id, store=shops[1]["id"])[0] == 404
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, raw = request("/api/tasks/" + task_id)
        assert status == 200, status
        task = json.loads(raw)["task"]
        if task["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert task["status"] == "completed", task["status"]
    for name in ("report.md", "evidence.json", "events.jsonl", "request.json", "validation.json"):
        path = f"/api/tasks/{task_id}/artifacts/{name}"
        assert request(path, authorized=False)[0] == 401
        status, data = request(path)
        assert status == 200 and data, (name, status)
    assert request("/api/tasks", origin="https://example.invalid")[0] == 403
    # 公网不仅能访问首页，也应能使用新增的最小权限工作流与混合意图拦截。
    for question, expected_intent in (("炽热爱意2026-10-23缺货多久", "query_inventory"),
                                      ("温柔时光2026-10-23活动结束了吗", "query_promotions")):
        status, raw = request("/api/tasks", {"question": question}, origin=base)
        assert status == 201, status
        lookup_id = json.loads(raw)["task"]["id"]
        deadline = time.monotonic() + 30
        lookup = None
        while time.monotonic() < deadline:
            status, raw = request("/api/tasks/" + lookup_id)
            assert status == 200
            lookup = json.loads(raw)["task"]
            if lookup["status"] not in ("queued", "running"):
                break
            time.sleep(0.2)
        assert lookup and lookup["status"] == "completed", lookup
        assert lookup["result"]["intent"] == expected_intent
        assert lookup["result"]["tool_calls"] == 1
    status, raw = request("/api/tasks", {"question": "怦然心动2026-10-23销量下降，顺便查库存"}, origin=base)
    mixed = json.loads(raw)["task"]
    assert status == 201 and mixed["status"] == "needs_clarification" and mixed["result"] is None
    # 工单闭环也只在明确的合成工作区验证，不将自动化说明冒充真实人工业务核验。
    status, raw = request("/api/tasks", {"question": "向阳而生2026-10-23销量下降原因"}, origin=base)
    assert status == 201
    original_id = json.loads(raw)["task"]["id"]
    deadline = time.monotonic() + 30
    original = None
    while time.monotonic() < deadline:
        status, raw = request("/api/tasks/" + original_id)
        original = json.loads(raw)["task"]
        if original["status"] not in ("queued", "running"):
            break
        time.sleep(.2)
    assert original and original["review_case"]["status"] == "awaiting_human"
    case_id = original["review_case"]["id"]
    assert request("/api/reviews", authorized=False)[0] == 401
    assert request(f"/api/reviews/{case_id}/materials", {}, authorized=False)[0] == 401
    status, raw = request(f"/api/reviews/{case_id}")
    case = json.loads(raw)["case"]
    status, raw = request(f"/api/reviews/{case_id}/materials", {"expected_revision": case["revision"],
        "note": "自动化合成案例验收测试，不代表真实盘点。", "source": "自动化合成盘点表",
        "corrections": [{"table": "inventory", "date": "2026-10-23", "values": {
            "out_of_stock_minutes": 120, "monitored_minutes": 1440}}]}, origin=base)
    assert status == 200
    case = json.loads(raw)["case"]
    status, raw = request(f"/api/reviews/{case_id}/recheck", {"expected_revision": case["revision"]}, origin=base)
    assert status == 200
    new_id = json.loads(raw)["task"]["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, raw = request(f"/api/reviews/{case_id}")
        case = json.loads(raw)["case"]
        if case["status"] != "rechecking":
            break
        time.sleep(.2)
    assert case["status"] == "awaiting_acceptance"
    status, raw = request(f"/api/reviews/{case_id}/accept", {"expected_revision": case["revision"],
        "task_id": new_id, "note": "自动化合成闭环测试通过，不代表真实业务已恢复。"}, origin=base)
    assert status == 200 and json.loads(raw)["case"]["status"] == "resolved"
    print("PUBLIC HTTPS: PASS; task:", task_id)
    print("unauthorized GET/POST/download/reviews: 401; sales/inventory/promotions: completed; mixed intent: clarification; artifacts: 5; cross-origin: 403; review loop: resolved (synthetic)")


if __name__ == "__main__":
    main()
