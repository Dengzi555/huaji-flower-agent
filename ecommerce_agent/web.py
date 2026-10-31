"""HTTP 边界层：静态页面 + JSON API，业务逻辑继续由 web_service 管理。

使用标准库是为了保持本地教学安装简单，不把它描述成完整生产 Web 框架。
默认仅监听本机；绑定外网地址必须设置访问口令，部署时再由反向代理提供 HTTPS。
"""

import hmac
import json
import os
import re
import signal
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .web_service import ConflictError, InvestigationService
from .errors import AgentError,from_exception
from .cancellation import RequestCancellations
from .reviews import ReviewConflict, ReviewNotFound
from .decisions import DecisionConflict, DecisionNotFound
from .missions import MissionConflict, MissionNotFound
from .operations import OperatingConflict, OperatingNotFound, overview
from .conversations import Conversations, ConversationNotFound

STATIC = Path(__file__).resolve().parent / "web_assets"


class WorkspaceHTTPServer(ThreadingHTTPServer):
    """Windows独占监听端口，拒绝两个进程复用同一地址后随机处理工作区请求。"""

    allow_reuse_address = False

    def server_bind(self):
        """只改变本进程socket选项，不更改系统网络配置。"""
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def runtime_view(service):
    """汇总各店运行时快照：状态计数与队列指标，不含问题、报告、事件和密钥。"""
    services=service.services.items() if hasattr(service,"services") else [(None,service)]
    stores=[]
    for sid,active in services:
        item=active.runtime()
        if sid:item['store_id']=sid
        stores.append(item)
    totals={key:sum(item['tasks'].get(key,0) for item in stores)
            for key in ('queued','running','cancelling','completed','failed','interrupted','incomplete','cancelled')}
    totals['queue_depth']=sum(item['executor']['queue_depth'] for item in stores)
    totals['worker_alive']=all(item['executor']['worker_alive'] for item in stores)
    return {'stores':stores,'totals':totals}


def create_server(service, host="127.0.0.1", port=8080, token="", public_hosts=(), model_settings=None):
    """创建服务但不阻塞启动；测试可绑定随机端口并使用真实 HTTP 请求验证。"""
    from .flow_view import FlowView
    flow_view=FlowView(STATIC.parent.parent)
    public_hosts = frozenset(public_hosts)
    conversations=Conversations(service)
    request_cancellations=RequestCancellations()
    if (public_hosts or host not in ("127.0.0.1", "localhost", "::1")) and len(token) < 24:
        raise ValueError("监听非本机地址时，需设置至少 24 字符的 ECOMMERCE_WEB_TOKEN。")

    class Handler(BaseHTTPRequestHandler):
        """只开放固定 API/静态文件，不提供目录列表或任意文件读取。"""

        def log_message(self, format, *args):
            """避免默认访问日志记录用户问题；业务事件由任务表保存。"""
            return

        def send(self, status, content, kind="application/json; charset=utf-8", extra=None):
            """统一响应长度和基础安全头；HTML 不加载远程脚本或内联脚本。"""
            body = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_site(self):
            """所有路径先校验来源；Tunnel 只接受启动时取得的精确公网域名。"""
            received_host = self.headers.get("Host", "")
            if host in ("127.0.0.1", "localhost", "::1"):
                if received_host not in public_hosts and not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?", received_host):
                    self.send(403, {"error": "不允许的 Host。"})
                    return False
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != received_host:
                self.send(403, {"error": "不允许跨站请求。"})
                return False
            return True

        def allowed(self):
            """API 使用 Bearer；默认本机无需口令，公网部署必须配置口令。

            拒绝跨站 Origin 和不匹配的 Host，减少本地服务被恶意网页调用的风险。
            不提供通配 CORS；口令绝不放到 URL 查询参数中。
            """
            if not self.valid_site():
                return False
            if token:
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                    self.send(401, {"error": "需要访问口令。"})
                    return False
            return True

        def workspace(self):
            """店铺由每次请求头决定；共享口令不是分店RBAC授权。"""
            return service.for_store(self.headers.get("X-Store-ID")) if hasattr(service, "for_store") else service

        def model_access(self):
            """配置密钥仅允许本机页面；Tunnel即使转发到loopback也不能改模型设置。"""
            name=urlsplit('http://'+self.headers.get('Host','')).hostname
            if not model_settings or self.client_address[0] not in ('127.0.0.1','::1') or name not in ('localhost','127.0.0.1','::1'):
                self.send(403,{'error':'模型设置仅能从本机工作台访问。'});return False
            return True

        def do_GET(self):
            """页面资源可公开读取；任务、数据目录和证据 API 统一走授权检查。"""
            if not self.valid_site():
                return
            path = urlsplit(self.path).path
            if path == "/healthz":
                stats=[item.executor.stats() for item in (service.services.values() if hasattr(service,"services") else [service])]
                self.send(200, {"status": "ok", "version": "0.10.0", "stores": len(stats),
                                "queue_depth": sum(item["queue_depth"] for item in stats),
                                "worker_alive": all(item["worker_alive"] for item in stats)})
                return
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/flow": ("flow.html", "text/html; charset=utf-8"),
                     "/flow.js": ("flow.js", "text/javascript; charset=utf-8"),
                     "/flow.css": ("flow.css", "text/css; charset=utf-8"),
                     "/trace.js": ("trace.js", "text/javascript; charset=utf-8"),
                     "/trace.css": ("trace.css", "text/css; charset=utf-8"),
                     "/trend.js": ("trend.js", "text/javascript; charset=utf-8"),
                     "/trend.css": ("trend.css", "text/css; charset=utf-8"),
                     "/imports.js": ("imports.js", "text/javascript; charset=utf-8"),
                     "/imports.css": ("imports.css", "text/css; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/conversation.js": ("conversation.js", "text/javascript; charset=utf-8"),
                     "/brand.css": ("brand.css", "text/css; charset=utf-8"),
                     "/knowledge.css": ("knowledge.css", "text/css; charset=utf-8"),
                     "/operations.css": ("operations.css", "text/css; charset=utf-8"),
                     "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
                     "/brand-mark-dark.svg": ("brand-mark-dark.svg", "image/svg+xml"),
                     "/brand-mark.svg": ("brand-mark.svg", "image/svg+xml")}
            if path in files:
                filename, kind = files[path]
                self.send(200, (STATIC / filename).read_bytes(), kind)
                return
            # 轨迹页是固定壳，具体运行数据由 /api/runs/<id> 返回；页面本身不含任务数据。
            if re.fullmatch(r"/runs/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", path):
                self.send(200, (STATIC / "trace.html").read_bytes(), "text/html; charset=utf-8")
                return
            if not path.startswith("/api/"):
                self.send(404, {"error": "页面不存在。"})
                return
            if not self.allowed():
                return
            if path == '/api/model-settings':
                if self.model_access():self.send(200,model_settings.public())
                return
            try:
                active = self.workspace()
            except ValueError as error:
                self.send(400, {"error": str(error)})
                return
            if match:=re.fullmatch(r'/api/conversations/([a-f0-9]{32})',path):
                try:self.send(200,{'conversation':conversations.get(match[1])})
                except ConversationNotFound as error:self.send(404,{'error':str(error)})
                return
            if match:=re.fullmatch(r'/api/runs/([A-Za-z0-9][A-Za-z0-9_-]{0,63})',path):
                try:
                    from .trace_view import build_trace
                    self.send(200,build_trace(active.output,match[1]))
                except FileNotFoundError:self.send(404,{'error':'运行记录不存在。'})
                except ValueError as error:self.send(400,{'error':str(error)})
                return
            if path == "/api/stores":
                self.send(200, {"stores": service.stores if hasattr(service, "stores") else [],
                    "default_store": getattr(service, "default_id", ""), "brand": "花仙女"})
                return
            if path=='/api/imports/schema':
                self.send(200,active.imports.schema());return
            if path=='/api/imports':
                self.send(200,{'imports':active.imports.history()});return
            if match:=re.fullmatch(r'/api/imports/templates/([a-z_]+)\.(csv|xlsx)',path):
                try:
                    data=active.imports.template(match[1],match[2]);kind='text/csv; charset=utf-8' if match[2]=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    self.send(200,data,kind,{'Content-Disposition':f'attachment; filename="huaji-{match[1]}-template.{match[2]}"'})
                except ValueError as error:self.send(400,{'error':str(error)})
                return
            if path == '/api/flow':
                try:self.send(200,flow_view.snapshot(active.meta()['capabilities']['model_enabled']))
                except (ValueError,SyntaxError,KeyError):self.send(503,{'error':'源码正在编辑或结构已改变，请保存后重试，并同步流程元数据。'})
                return
            if path == "/api/advertising":
                try:
                    if not hasattr(service, "books"):
                        raise ValueError("当前自定义工作区没有推广账本。")
                    sid = self.headers.get("X-Store-ID") or service.default_id
                    query = parse_qs(urlsplit(self.path).query)
                    book = service.books[sid]
                    day = query.get("date", [book.dates[-1]])[0]
                    settings=active.operations.settings()
                    report = book.report(day, high=query.get("high", [settings["high_pct"]])[0], low=query.get("low", [settings["low_pct"]])[0], mode=query.get("mode", ["all"])[0])
                    self.send(200, report)
                except (ValueError, KeyError, IndexError) as error:
                    self.send(400, {"error": str(error)})
                return
            if path == "/api/meta":
                response=active.meta();response['capabilities']['conversation_state']=True
                self.send(200, response)
            elif path=="/api/runtime":
                self.send(200,runtime_view(service))
            elif path=="/api/overview":
                try:
                    if not hasattr(service,"stores"):raise ValueError("当前自定义工作区未配置门店概览。")
                    day=parse_qs(urlsplit(self.path).query).get("date",[active.meta()["available_dates"][-1]])[0]
                    self.send(200,overview(service,day))
                except (ValueError,IndexError) as error:self.send(400,{"error":str(error)})
            elif path=="/api/settings":self.send(200,{"settings":active.operations.settings()})
            elif path=="/api/followups":self.send(200,{"followups":active.operations.list()})
            elif match:=re.fullmatch(r"/api/followups/([a-f0-9]{32})",path):
                try:self.send(200,{"followup":active.operations.get(match[1])})
                except OperatingNotFound as error:self.send(404,{"error":str(error)})
            elif path == "/api/missions":
                self.send(200,{"missions":active.missions.list()})
            elif path == "/api/memory":
                self.send(200,{"memories":active.memory.list()})
            elif path == '/api/store-context':
                self.send(200,{'memories':active.context_memory.list()})
            elif path == '/api/context-conflicts':
                self.send(200,{'conflicts':active.context_memory.conflicts()})
            elif match:=re.fullmatch(r'/api/store-context/([a-f0-9]{32})',path):
                try:self.send(200,{'memory':active.context_memory.get(match[1])})
                except MissionNotFound as error:self.send(404,{'error':str(error)})
            elif match := re.fullmatch(r"/api/missions/([a-f0-9]{32})",path):
                try: self.send(200,{"mission":active.missions.get(match[1])})
                except MissionNotFound as error: self.send(404,{"error":str(error)})
            elif path == "/api/tasks":
                self.send(200, {"tasks": active.list_tasks()})
            elif path == "/api/reviews":
                self.send(200, {"cases": active.reviews.list()})
            elif match := re.fullmatch(r"/api/reviews/([a-f0-9]{32})", path):
                try:
                    self.send(200, {"case": active.reviews.get(match[1])})
                except ReviewNotFound as error:
                    self.send(404, {"error": str(error)})
            elif match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})/decision", path):
                task = active.get(match[1])
                if not task:
                    self.send(404, {"error": "任务不存在。"})
                elif not task.get("decision"):
                    self.send(404, {"error": "尚未生成决策卡。"})
                else:
                    self.send(200, {"decision": task["decision"]})
            elif match := re.fullmatch(r"/api/decisions/([a-f0-9]{32})", path):
                try:
                    self.send(200, {"decision": active.decisions.get(match[1])})
                except DecisionNotFound as error:
                    self.send(404, {"error": str(error)})
            elif match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})", path):
                task = active.get(match[1])
                self.send(200, {"task": task}) if task else self.send(404, {"error": "任务不存在。"})
            elif match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})/artifacts/(report\.md|evidence\.json|events\.jsonl|request\.json|validation\.json)", path):
                data = active.artifact(match[1], match[2])
                if data is None:
                    self.send(404, {"error": "产物尚未生成。"})
                else:
                    self.send(200, data, "text/plain; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{match[2]}"'})
            else:
                self.send(404, {"error": "接口不存在。"})

        def do_POST(self):
            """仅接受受限 JSON 请求；不允许用户从 API 指定文件路径或模型密钥。"""
            path = urlsplit(self.path).path
            review_action = re.fullmatch(r"/api/reviews/([a-f0-9]{32})/(materials|recheck|accept)", path)
            mission_action=re.fullmatch(r"/api/missions/([a-f0-9]{32})/(run|action)",path)
            memory_action=re.fullmatch(r"/api/memory/([a-f0-9]{32})/retract",path)
            context_action=re.fullmatch(r'/api/store-context/([a-f0-9]{32})/action',path)
            conflict_action=re.fullmatch(r'/api/context-conflicts/([a-f0-9]{32})/resolve',path)
            followup_action=re.fullmatch(r"/api/followups/([a-f0-9]{32})/(action|recheck)",path)
            conversation_action=re.fullmatch(r'/api/conversations/([a-f0-9]{32})/(turns|focus)',path)
            cancel_request=re.fullmatch(r'/api/conversations/([a-f0-9]{32})/cancel',path)
            cancel_task=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/cancel',path)
            import_action=re.fullmatch(r'/api/imports/([a-f0-9]{32})/(preview|commit|cancel)',path)
            decision_task=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/decision',path)
            decision_action=re.fullmatch(r'/api/decisions/([a-f0-9]{32})/(approve|reject|execute)',path)
            if path not in ("/api/tasks","/api/missions","/api/memory","/api/followups","/api/settings","/api/model-settings","/api/model-test","/api/store-context","/api/conversations","/api/imports/upload") and not review_action and not mission_action and not memory_action and not followup_action and not context_action and not conflict_action and not conversation_action and not import_action and not cancel_request and not cancel_task and not decision_task and not decision_action:
                self.send(404, {"error": "接口不存在。"})
                return
            if not self.allowed():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send(415, {"error": "请使用 application/json。"})
                return
            try:
                active = self.workspace()
                size = int(self.headers.get("Content-Length", "0"))
                limit=3_000_000 if path=='/api/imports/upload' else 32000 if import_action else 16000
                if not 0 < size <= limit:
                    self.send(413, {"error": "请求体超过当前接口大小限制。"})
                    return
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload,dict):raise ValueError('请求需为JSON对象。')
                if cancel_request:
                    if set(payload)!={'request_id'} or not isinstance(payload['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',payload['request_id']):raise ValueError('需要合法的请求ID。')
                    conversations.load(cancel_request[1])  # 不占用会话执行锁，模型请求期间也可停止。
                    token=request_cancellations.cancel(cancel_request[1],payload['request_id'])
                    self.send(200,{'accepted':True,'status':'cancelling','cancellation':token.details()})
                elif cancel_task:
                    if payload!={}:raise ValueError('停止任务不接受额外参数。')
                    if not active.get(cancel_task[1],include_events=False):self.send(404,{'error':'当前门店不存在该任务。'});return
                    self.send(200,active.cancel(cancel_task[1]))
                elif decision_task:
                    self.send(200,{'decision':active.decisions.propose(decision_task[1])})
                elif decision_action:
                    if decision_action[2]=='execute':
                        if payload!={}:raise ValueError('干跑执行不接受额外参数。')
                        result=active.decisions.dry_run(decision_action[1])
                    else:
                        if set(payload)-{'actor','note'}:raise ValueError('审批只接受 actor 与 note。')
                        actor=payload.get('actor','本地操作员');note=payload.get('note','')
                        if not isinstance(actor,str) or not 1<=len(actor)<=40 or any(ord(char)<32 for char in actor):
                            raise ValueError('批准人需为1-40个可见字符。')
                        if not isinstance(note,str) or len(note)>200:raise ValueError('备注最多200字。')
                        method=active.decisions.approve if decision_action[2]=='approve' else active.decisions.reject
                        result=method(decision_action[1],actor,note)
                    self.send(200,{'decision':result})
                elif path=='/api/imports/upload':
                    self.send(201,active.imports.upload(payload))
                elif import_action:
                    result=(active.imports.cancel(import_action[1]) if import_action[2]=='cancel' else getattr(active.imports,import_action[2])(import_action[1],payload))
                    self.send(200,result)
                elif path=='/api/conversations':
                    if not isinstance(payload,dict) or set(payload)!={'store_id'}:raise ValueError('新对话需指定store_id。')
                    self.send(201,{'conversation':conversations.new(payload['store_id'])})
                elif conversation_action:
                    request_key=self.headers.get('Idempotency-Key')
                    token=None
                    if conversation_action[2]=='turns':
                        if not request_key or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_key):raise ValueError('需要合法的幂等键。')
                        token=request_cancellations.token(conversation_action[1],request_key)
                    if conversation_action[2]=='turns' and urlsplit(self.path).query=='stream=1':
                        # 同源/鉴权/JSON大小校验已经完成；真实增量写入SSE，不伪造打字机效果。
                        self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control','no-cache');self.send_header('Connection','close');self.end_headers();self.close_connection=True
                        def emit(value):
                            self.wfile.write(('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode('utf-8'));self.wfile.flush()
                        try:
                            result=conversations.turn(conversation_action[1],payload,request_key,on_delta=lambda text:emit({'type':'delta','text':text}),cancel_token=token)
                            emit({'type':'done','result':result})
                        except (BrokenPipeError,ConnectionResetError):pass
                        except Exception as exc:
                            problem=from_exception(exc,phase='conversation').public()
                            emit({'type':'error','message':problem['message'],'error_info':problem})
                        return
                    result=conversations.turn(conversation_action[1],payload,request_key,cancel_token=token) if conversation_action[2]=='turns' else conversations.focus(conversation_action[1],payload)
                    self.send(200,result)
                elif path in ('/api/model-settings','/api/model-test'):
                    if not self.model_access():return
                    if path=='/api/model-test' and payload!={}:raise ValueError('连接测试不接受额外参数。')
                    self.send(200,model_settings.save(payload) if path=='/api/model-settings' else model_settings.test())
                elif path=='/api/store-context':
                    result=active.context_memory.propose(payload);self.send(200 if result['reused'] else 201,result)
                elif context_action:self.send(200,{'memory':active.context_memory.action(context_action[1],payload)})
                elif conflict_action:self.send(200,active.context_memory.resolve(conflict_action[1],payload))
                elif path=="/api/settings":self.send(200,{"settings":active.operations.save_settings(payload)})
                elif path=="/api/followups":
                    result=active.operations.create(payload);self.send(200 if result["reused"] else 201,result)
                elif followup_action:
                    method=active.operations.action if followup_action[2]=="action" else active.operations.recheck
                    self.send(200,{"followup":method(followup_action[1],payload)})
                elif path=="/api/missions": self.send(201,{"mission":active.missions.create(payload)})
                elif path=="/api/memory": self.send(201,{"memory":active.memory.create(payload)})
                elif mission_action:
                    result=active.missions.launch(mission_action[1],payload) if mission_action[2]=="run" else {"mission":active.missions.action(mission_action[1],payload)}
                    self.send(200,result)
                elif memory_action: self.send(200,active.memory.retract(memory_action[1],payload))
                elif review_action:
                    method = {"materials": active.reviews.add_material, "recheck": active.reviews.recheck,
                              "accept": active.reviews.accept}[review_action[2]]
                    result = method(review_action[1], payload)
                    self.send(200, result if review_action[2] == "recheck" else {"case": result})
                else:
                    result = active.create(payload, self.headers.get("Idempotency-Key"))
                    self.send(200 if result["reused"] else 201, result)
            except (ReviewNotFound,MissionNotFound,OperatingNotFound,ConversationNotFound,DecisionNotFound) as error:
                self.send(404, {"error": str(error)})
            except (ConflictError, ReviewConflict,MissionConflict,OperatingConflict,DecisionConflict) as error:
                self.send(409, {"error": str(error)})
            except AgentError as error:
                problem=error.public()
                self.send(503 if problem['retryable'] else 502,{'error':problem['message'],'error_info':problem})
            except (ValueError, TypeError, UnicodeError) as error:
                self.send(400, {"error": str(error)})
            except Exception:
                self.send(500, {"error": "服务处理失败，请稍后重试。"})

        def setup(self):
            """限制连接读取时间，避免半份请求无限占用一个服务线程。"""
            super().setup()
            self.connection.settimeout(15)

    server = WorkspaceHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _terminate(signum, frame):
    """容器或服务管理器发 SIGTERM 时复用 Ctrl+C 的收尾路径，不直接杀进程。"""
    raise KeyboardInterrupt


def serve(args):
    """启动一个本地进程；Ctrl+C 关闭 HTTP，再等待当前后台只读任务结束。"""
    token = os.environ.get("ECOMMERCE_WEB_TOKEN", "")
    # 先验证监听配置，再创建后台工作线程。
    if args.host not in ("127.0.0.1", "localhost", "::1") and len(token) < 24:
        raise ValueError("公网监听必须先设置至少 24 字符 ECOMMERCE_WEB_TOKEN。")
    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (AttributeError, ValueError, OSError):
        pass  # 非主线程或平台不支持信号时保持默认终止行为
    root = Path(__file__).resolve().parents[1]
    if args.data.resolve() == (root / "data/sample.json").resolve() and args.catalog.resolve() == (root / "data/products.json").resolve():
        from .store_workspace import StoreHub
        service = StoreHub(root, args.state_dir, args.output)
    else:
        service = InvestigationService(args.data, args.catalog, args.state_dir, args.output)
    from .local_model import LocalModelSettings
    model_settings=LocalModelSettings(root)
    for active in (service.services.values() if hasattr(service,'services') else [service]):active.model_settings=model_settings
    server = None
    try:
        server = create_server(service, args.host, args.port, token,model_settings=model_settings)
        print(f"经营调查工作台：http://{args.host}:{server.server_port}", flush=True)
        print("数据模式：经营学习快照；数据来源见工作区说明。", flush=True)
        print("执行模式：销量调查可使用DeepSeek工具调用，其余查询按规则执行；Ctrl+C 停止。", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        service.close()
    return 0
