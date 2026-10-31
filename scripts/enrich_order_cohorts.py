"""给当前学习数据补充支付批次，不重新生成销售/价格/推广，不改历史任务的冻结快照。"""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ecommerce_agent.data import DataStore
from ecommerce_agent.order_funnel import generate_order_cohorts
from ecommerce_agent.runner import write_json


def main():
    """只处理配置内门店与样例；首次保留旧文件，校验新契约后原子写入。"""
    config=json.loads((ROOT/'data/stores.json').read_text(encoding='utf-8'))
    paths=[ROOT/'data/sample.json',*[ROOT/'data/stores'/f"{shop['id']}.json" for shop in config['stores']]]
    backup=ROOT/'artifacts/before-order-cohorts';backup.mkdir(parents=True,exist_ok=True)
    documents=[]
    for path in paths:
        original=path.read_bytes();document=json.loads(original.decode('utf-8-sig'))
        document['order_cohorts']=generate_order_cohorts(document)
        DataStore.from_document(document)
        saved=backup/path.name
        if not saved.exists():saved.write_bytes(original)
        documents.append((path,document))
    for path,document in documents:
        write_json(path,document)
        print(f'{path.name}: {len(document["order_cohorts"])} paid-date cohorts')


if __name__=='__main__':main()
