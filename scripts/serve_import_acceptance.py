"""独立导入验收服务：业务输入只读，导入版本与任务全部写到artifacts子目录。"""
import sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ecommerce_agent.store_workspace import StoreHub
from ecommerce_agent.web import create_server


def main():
    """仅供本地UI验收，不加载模型或真实密钥，不改正式工作区数据库。"""
    output=ROOT/'artifacts'/('import-acceptance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    hub=StoreHub(ROOT,output/'state',output/'runs')
    server=create_server(hub,port=8081)
    template=output/'huaji-sales-template.csv';template.write_bytes(hub.for_store().imports.template('sales','csv'))
    print('Acceptance: http://127.0.0.1:8081',flush=True);print('Template: '+str(template),flush=True)
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:server.server_close();hub.close()


if __name__=='__main__':main()
