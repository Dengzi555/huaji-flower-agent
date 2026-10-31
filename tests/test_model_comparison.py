"""模型接入的协议与用量回归：只用本地桩，不发送外部请求、不产生模型费用。"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ecommerce_agent.fixtures import sample_data
from ecommerce_agent.model_comparison import compare
from ecommerce_agent.provider import ChatProvider


class FixtureProvider:
    """固定返回工具请求，用于验证对照入口；不能作为真实模型能力的证据。"""

    def __init__(self, config):
        """忽略测试配置，仅保存调用计数和明确标记的协议桩用量。"""
        self.index = 0
        self.last_usage = None

    def complete(self, messages, tools, timeout):
        """按四源、分析、交付顺序返回协议消息，不进行任何网络 I/O。"""
        names = ["get_sales_metrics", "get_traffic_metrics", "get_inventory_status", "get_promotion_history", "analyze_metrics", "finish"]
        name = names[self.index]
        args = {"summary": "协议桩：目标日销量50，原因待核验。", "evidence_ids": [f"fixture-{i}" for i in range(4)],
                "assertions": [{"evidence_id": "fixture-0", "date": "2026-10-23", "field": "units_sold", "equals": 50}]} if name == "finish" else {}
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"fixture-{self.index}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}
        self.index += 1
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return message


class ModelComparisonTests(unittest.TestCase):
    """检查同输入对照、实际循环计数以及未知用量不被当作零。"""

    def test_comparison_entry_with_protocol_fixture(self):
        """完整调用对照入口并读取落盘报告，所有数据和输出均在临时目录。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data.json").write_text(json.dumps(sample_data()), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"data_file": "data.json", "sku_id": "FLR-ROSE-19-BOUQUET", "target_date": "2026-10-23"}), encoding="utf-8")
            with patch("ecommerce_agent.model_comparison.ChatProvider", FixtureProvider):
                result = compare(config, root / "output")
            self.assertEqual(result["baseline"]["model_calls"], 0)
            self.assertEqual(result["model"]["model_calls"], 6)
            self.assertEqual(result["model"]["model_usage"]["total_tokens"], 90)
            self.assertIsNone(result["model"]["model_usage"]["cost"])
            self.assertEqual(result["model"]["validation"]["status"], "passed")
            self.assertTrue((root / "output/comparison.json").exists())
            self.assertTrue((root / "output/comparison.md").exists())

    def test_provider_keeps_missing_or_invalid_usage_unknown(self):
        """提供商未上报或错误上报 token 时，不伪造零成本记录。"""
        for usage in (None, {"prompt_tokens": "10", "completion_tokens": 5}):
            provider = ChatProvider({"api_base_url": "http://127.0.0.1:9999/v1", "model": "test"})
            raw = json.dumps({"choices": [{"message": {"content": "test"}}], "usage": usage}).encode()
            with patch("ecommerce_agent.provider.urllib.request.urlopen", return_value=io.BytesIO(raw)):
                provider.complete([], [], 1)
            self.assertIsNone(provider.last_usage["prompt_tokens"])
            self.assertIsNone(provider.last_usage["total_tokens"])


if __name__ == "__main__":
    unittest.main()
