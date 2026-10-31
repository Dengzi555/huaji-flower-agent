# 第四步：用 Tunnel 演示个人项目

## 请求经过哪里

浏览器 → Cloudflare 的 HTTPS 临时网址 → 电脑上的 cloudflared → 127.0.0.1:8081 → Python 工作台 → 调查服务 → SQLite / 报告文件。

Tunnel 让外部请求到达本地应用。它没有把 Python 程序、数据或后台任务搬进云端。电脑关机、网络断开或者启动命令退出，演示就会中断。

## 启动和使用

在项目目录执行：

```powershell
python -m ecommerce_agent tunnel
```

程序读取 `.tools/cloudflared.exe`。也可以通过 `--cloudflared C:\路径\cloudflared.exe` 指定已安装的官方连接器。端口默认 8081，可用 `--port` 调整；同一状态目录只启动一个实例。

本次安装版本为官方 cloudflared 2026.10.3（Windows AMD64），SHA256：`76b2805253d0d6ffacb092a7acfbee99e7af6917835c0c5410e23f3ef5052da2`。发布资产信息保存在 `.tools/cloudflared-release.json`。首次实测发现 IPv6 超时，因此启动器使用 IPv4 与 HTTP/2；这是连接器参数，不修改操作系统网络设置。

1. 等终端输出 `https://….trycloudflare.com`。
2. 打开网址，在页面的访问口令栏粘贴 `web-data/tunnel/flowers/access-token.txt` 的内容。
3. 输入“怦然心动2026-10-23销量下降原因”，查看任务状态、报告、证据和执行记录。
4. 按 Ctrl+C 停止，工作台和连接器一起退出。

每次启动生成新口令，旧口令失效；网址也会改变。`web-data/tunnel/flowers/url.txt` 保存本次网址，正常退出时删除。任务历史保存在 `web-data/tunnel/flowers`，报告保存在 `runs/tunnel/flowers`。这些目录以及连接器二进制均被 Git 忽略。不要把口令文件提交到仓库。

## 为什么需要新增这些代码

- `ecommerce_agent/tunnel.py`：编排连接器生命周期，取得精确域名，启动受保护的应用，退出时清理子进程。只加载仓库模拟数据，不接受来自公网的文件路径。
- `web.py`：所有页面检查 Host/Origin，任务和下载 API 额外检查 Bearer 口令。登录页的静态资源允许公开加载，里面没有任务数据。公网域名白名单必须配合至少 24 字符口令。
- `cli.py`：增加 tunnel 命令，复用原来的 InvestigationService，业务分析规则没有复制一份。
- `tests/test_web.py`：验证合法公网 Host、无口令拒绝、跨站拒绝，以及静态资源不能绕过 Host 检查。

这个演示使用共享口令，不具备用户账号、租户隔离、配额或完整生产运维能力。当前仍为模拟数据和确定性工作流，不会调用真实模型。

## 自己动手验证

先不输入口令，确认页面能打开、任务列表不能读取。再输入口令，提交明确商品和日期。随后提交“红玫瑰花束2026-10-23销量下降原因”，观察系统要求消歧而没有直接猜测商品。最后下载报告，沿着任务 ID 找到本地 evidence.json 与 events.jsonl，理解公网入口如何复用已有可追溯链路。

遇到 502 或连接失败，先检查 cloudflared 是否连上，再检查本地端口；“已分配网址”不代表隧道已经连通。具体日志位于 `web-data/tunnel/flowers/cloudflared.log`。

可运行 `python -X utf8 scripts/verify_tunnel.py` 重验公网链路。它会新建一条模拟任务，检查无口令访问拦截、完成状态、四类文件下载及跨站拦截，不会打印口令。

参考：[Cloudflare Quick Tunnels 官方文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。临时隧道适合测试和学习；固定域名与长期运行是后续部署阶段。
