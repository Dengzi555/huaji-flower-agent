"""临时公网演示入口：管理 cloudflared 子进程和带口令的本机工作台。

只加载仓库模拟数据，使用独立任务目录；网址和口令分开保存，均不进入 Git。
学习重点：隧道解决网络可达性，应用层仍负责访问授权和来源校验。
"""

import re
import secrets
import socket
import subprocess
import time

from .cli import ROOT
from .web import create_server


def serve_tunnel(args):
    """生成本次口令，申请临时域名后启动服务；退出时回收连接器和数据库。"""
    executable = args.cloudflared.resolve(strict=True)
    # 先检查端口，避免隧道意外指向已有的另一个本地应用。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    state = ROOT / "web-data/tunnel/flowers"
    state.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_file = state / "access-token.txt"
    token_file.write_text(token, encoding="utf-8")
    url_file = state / "url.txt"
    url_file.unlink(missing_ok=True)
    log_path = state / "cloudflared.log"
    service = server = connector = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            # HTTP/2 通过 TCP 连接，避免学习环境中 UDP/QUIC 被阻断。
            connector = subprocess.Popen(
                [str(executable), "tunnel", "--no-autoupdate", "--protocol", "http2",
                 "--edge-ip-version", "4",
                 "--url", f"http://127.0.0.1:{args.port}"],
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print("正在申请临时网址，连接日志：" + str(log_path), flush=True)
            deadline = time.monotonic() + 90
            public_url = None
            while time.monotonic() < deadline:
                content = log_path.read_text(encoding="utf-8", errors="replace")
                # 失败日志也会出现 api.trycloudflare.com，不能把 API 地址误当演示网址。
                match = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", content)
                if connector.poll() is not None:
                    raise OSError("cloudflared 提前退出，请查看连接日志。")
                if match and "Registered tunnel connection" in content:
                    public_url = match.group()
                    break
                time.sleep(0.2)
            if not public_url:
                raise OSError("申请临时网址超时，请查看 cloudflared.log 和网络连接。")
            from .store_workspace import StoreHub
            service = StoreHub(ROOT, state, ROOT / "runs/tunnel/flowers")
            # 精确域名白名单：不能为了隧道而关闭所有 Host 检查。
            server = create_server(service, port=args.port, token=token,
                                   public_hosts=(public_url.removeprefix("https://"),))
            server.timeout = 0.5
            url_file.write_text(public_url, encoding="utf-8")
            print("临时公网网址：" + public_url, flush=True)
            print("访问口令文件：" + str(token_file), flush=True)
            print("经营学习快照 / 确定性分析 / 不调用模型。Ctrl+C 同时停止工作台和隧道。", flush=True)
            # 同时监控连接器；子进程退出后不能继续显示“正在公网运行”。
            while connector.poll() is None:
                server.handle_request()
            raise OSError("隧道连接器已经退出，请查看日志并重新启动。")
    except KeyboardInterrupt:
        return 0
    finally:
        if server:
            server.server_close()
        if service:
            service.close()
        if connector and connector.poll() is None:
            connector.terminate()
            try:
                connector.wait(timeout=5)
            except subprocess.TimeoutExpired:
                connector.kill()
                connector.wait()
        url_file.unlink(missing_ok=True)

