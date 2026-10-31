"""重新生成合成数据，只覆盖 data/sample.json，不修改自定义业务数据文件。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 从 scripts/ 直接执行时补入项目根目录，让 Python 能导入同级业务包。
sys.path.insert(0, str(ROOT))
from ecommerce_agent.fixtures import sample_data

if __name__ == "__main__":
    target = ROOT / "data/sample.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sample_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Synthetic sample written:", target)
