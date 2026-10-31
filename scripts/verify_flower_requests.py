"""通过真实HTTP验证花店口语与新品请求；仅允许向学习快照创建测试任务。"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    """默认验证本地；--public读取本项目Tunnel网址/口令，不打印凭证。"""
    base, headers = "http://127.0.0.1:8080", {"Content-Type": "application/json"}
    if "--public" in sys.argv:
        state = ROOT / "web-data/tunnel/flowers"
        base = (state / "url.txt").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", base):
            raise ValueError("仅向本项目临时Tunnel发送学习请求。")
        headers["Authorization"] = "Bearer " + (state / "access-token.txt").read_text(encoding="utf-8").strip()

    def request(path, body=None):
        """访问同一工作区，不记录请求头；网络失败直接报错，不伪造通过。"""
        req = urllib.request.Request(base + path, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)

    meta = request("/api/meta")
    assert meta["sample_data"] is True and meta["business_context"]["dataset_id"] == "flower-business-v07"
    cases = [
        ("99红玫爆款销量下降原因", "needs_clarification", "FLR-ROSE-99-BOUQUET"),
        ("33乌梅包括2026-10-25下降原因", "completed", "FLR-WUMEI-33-BUCKET"),
        ("2026-10-25新上架的洋桔梗销量怎么样", "completed", "FLR-EUSTOMA-11-BOUQUET"),
        ("洋桔梗2026-10-25销量怎么样", "needs_clarification", None),
        ("晴天葡萄2026-10-25销量怎么样", "completed", "FLR-GRAPE-19-BOUQUET"),
    ]
    for question, expected, sku in cases:
        task = request("/api/tasks", {"question": question})["task"]
        deadline = time.monotonic() + 20
        while task["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(.1)
            task = request("/api/tasks/" + task["id"])["task"]
        assert (task["status"], task["parsed"]["sku_id"]) == (expected, sku), task["status"]
        if expected == "completed":
            assert task["result"]["model_calls"] == 0
        else:
            assert task["result"] is None
    print(f"FLOWER HTTP: {len(cases)}/{len(cases)} PASS; products={len(meta['products'])}; model_calls=0")


if __name__ == "__main__":
    main()
