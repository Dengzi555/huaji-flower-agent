"""从统一鲜花目录定义生成模拟 JSON，避免目录、样例和测试各维护一份。

运行 python scripts/generate_flower_sample.py，只更新项目 data 下的两份模拟文件。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.store_workspace import generate_stores


def main():
    """写入显式标记的合成目录和日级观察，不接触历史 Run 或工单数据库。"""
    for filename, document in (("products.json", business_catalog()), ("sample.json", business_data())):
        path = ROOT / "data" / filename
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path)
    generate_stores(ROOT, business_data())


if __name__ == "__main__":
    main()
