"""应用服务入口：组合任务准入、任务仓库、后台执行器与业务扩展。

学习重点：HTTP 请求不直接承担完整调查。先提交数据库事务，再入队；后台线程
复用 runner.run。SQLite 保存任务索引，原 runner 保存报告和证据，两者职责不同。
当前是单进程、单执行线程；不是分布式队列，也不支持多个服务进程共享此数据库。
"""

import hashlib
import json
import threading
import uuid
from pathlib import Path

from .clock import utc_now
from .data import DataStore
from .workflows import execute_task as run
from .task_parser import ProductCatalog, shanghai_today
from .reviews import ReviewCases, ReviewConflict, SCHEMA as REVIEW_SCHEMA
from .missions import Missions, StoreMemory, MissionConflict, SCHEMA as MISSION_SCHEMA
from .operations import StoreOperations, SCHEMA as OPERATING_SCHEMA
from .context_memory import ContextMemory, SCHEMA as CONTEXT_SCHEMA
from .data_imports import DataImports, SCHEMA as IMPORT_SCHEMA
from .decisions import Decisions, SCHEMA as DECISION_SCHEMA
from .cancellation import current_token,checkpoint
from .model_runtime import model_scope
from .task_admission import prepare_task,validate_request
from .operations_board import OperationsBoard, initialize_board
from .approval_drafts import ApprovalDrafts, SCHEMA as APPROVAL_DRAFTS_SCHEMA
from .investigation_worker import freeze_context, run_in_worker
from .task_repository import TaskRepository,ConflictError
from .task_execution import TaskExecutor


class InvestigationService:
    """管理任务生命周期；所有外部输入先经过 parse_task 和业务契约。"""

    def __init__(self, data_path: Path, catalog_path: Path, state_dir: Path, output: Path,
                 *, runner=run, start_worker=True):
        """冻结启动时的数据快照；测试可注入 runner，从而验证异常与恢复路径。"""
        self.store = DataStore(data_path)
        self.catalog = ProductCatalog(catalog_path, self.store.skus)
        self.data_lock=threading.RLock()
        source_identity=getattr(self.store,'content_fingerprint',None)
        self.fingerprint = hashlib.sha256(json.dumps([source_identity if source_identity else self.store.snapshot(), {
            "schema_version": 1, "products": list(self.catalog.products.values())}],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.database = state_dir / "tasks.sqlite3"
        self.runner = runner
        self.model_settings = None
        self.route_classifier = None  # 可注入离线分类器做回归；生产默认使用本地配置的DeepSeek。
        self.query_planner = None  # 规则无法填槽时才请求一次参数规划，模型不能生成SQL。
        self.repository=TaskRepository(self.database)
        with self.connection() as db:
            self.repository.initialize(db)
            db.executescript(REVIEW_SCHEMA)
            db.executescript(MISSION_SCHEMA)
            db.executescript(OPERATING_SCHEMA)
            db.executescript(CONTEXT_SCHEMA)
            db.executescript(IMPORT_SCHEMA)
            db.executescript(DECISION_SCHEMA)
            db.executescript(APPROVAL_DRAFTS_SCHEMA)
            initialize_board(db)
            current=db.execute('SELECT document,catalog,fingerprint FROM workspace_versions ORDER BY rowid DESC LIMIT 1').fetchone()
            if current:
                self.store=DataStore.from_document(json.loads(current['document']))
                self.catalog=ProductCatalog.from_document(json.loads(current['catalog']),self.store.skus)
                self.fingerprint=current['fingerprint']
            self._dataset=(self.store,self.catalog,self.fingerprint)
            self.imports=DataImports(self)
            self.reviews = ReviewCases(self)
            self.decisions = Decisions(self)
            self.approval_drafts = ApprovalDrafts(self)
            self.missions, self.memory = Missions(self), StoreMemory(self)
            self.operations=StoreOperations(self)
            self.context_memory=ContextMemory(self)
            pending=self.repository.recover(db,self.fingerprint)
            # 进程中断不能让业务工单永远停在复查中；可重新补充材料后显式复查。
            interrupted_cases = list(db.execute("SELECT id FROM review_cases WHERE status='rechecking' AND latest_task_id IN (SELECT id FROM tasks WHERE status='interrupted')"))
            db.execute("UPDATE review_cases SET status='awaiting_human',revision=revision+1,updated_at=? WHERE status='rechecking' AND latest_task_id IN (SELECT id FROM tasks WHERE status='interrupted')", (utc_now(),))
            for item in interrupted_cases:
                self.reviews.event(db, item[0], "recheck_interrupted", reason="进程中断，复查结果未知，需人工确认后重新发起。")
            for row in db.execute("SELECT r.task_id FROM mission_runs r JOIN missions m ON m.id=r.mission_id JOIN tasks t ON t.id=r.task_id WHERE m.status='running' AND t.status NOT IN ('queued','running')").fetchall():
                self.missions.settled(db,row[0])
        self.executor=TaskExecutor(self.repository,self.output,
            execute=self._run_task,default_store=lambda:self.store,
            settings=lambda:self.model_settings,can_start=self._can_start,on_skipped=self._on_skipped,
            on_result=self._on_result,on_failure=self._on_failure)
        self.executor.start(pending,enabled=start_worker)
        self.board = OperationsBoard(self, start_worker=start_worker)

    def _run_task(self, *args, **kwargs):
        """已准入的销量暴跌调查进入子进程，沿用原有任务终态和队列管理。"""
        task = kwargs['task_context']
        if self.runner is run and task.get('investigation_context'):
            return run_in_worker(*args, **kwargs)
        return self.runner(*args, **kwargs)

    def connection(self):
        """兼容业务扩展入口；连接生命周期由任务仓库统一管理。"""
        return self.repository.connection()

    @property
    def work(self):
        """兼容原有队列观察接口；通知的所有权在执行器，业务提交统一经enqueue。"""
        return self.executor.work

    def dataset(self):
        """一次读取完整不可变版本三元组，防止导入发布时任务混用新数据与旧指纹。"""
        with self.data_lock:return self._dataset

    def meta(self):
        """只暴露界面需要的目录和日期覆盖，不提供密钥、服务器文件路径等信息。"""
        store,catalog,fingerprint=self.dataset()
        days = store.dates('sales')
        model_status=self.model_settings.public() if self.model_settings else {"enabled":False,"configured":False}
        return {**store.meta, "products": list(catalog.products.values()), "available_dates": days,
                "operating_settings":self.operations.settings(),
                "mode": "hybrid_deepseek" if model_status['enabled'] and model_status['configured'] else "deterministic_workflow", "fingerprint": fingerprint,
                "capabilities": {"model_enabled": bool(model_status['enabled'] and model_status['configured']), "write_actions": False,'cancellation':True,
                                 "intents": ["investigate_sales_change", "query_sales_performance", "query_inventory", "query_promotions", "query_combined"]}}

    def create(self, payload, idempotency_key=None, *, _store=None, _catalog=None, _review=None, _today=None, _mission=None, _conversation=None):
        """直接API请求和会话请求共用准入账本，幂等命中保留原用量。"""
        with model_scope():
            return self._create(payload,idempotency_key,_store=_store,_catalog=_catalog,_review=_review,
                _today=_today,_mission=_mission,_conversation=_conversation)

    def _create(self, payload, idempotency_key=None, *, _store=None, _catalog=None, _review=None, _today=None, _mission=None, _conversation=None):
        """先持久化，再返回任务 ID；重复点击使用幂等键复用同一个任务。

        信息有歧义也保存任务，状态为 needs_clarification；用户补充后建立子请求，
        不改写旧问题，让调查输入与报告可以一直追溯。
        """
        request_hash=validate_request(payload,idempotency_key)
        question=payload.get('question')
        # 模型前先查幂等键；事务中再次检查，防止并发重复创建。
        with self.connection() as db:
            existing=self.repository.replay(db,idempotency_key,request_hash)
            if existing:return {'task':self.get(existing,include_events=False),'reused':True}
            confirmed_candidates, parent_role = self.repository.parent_context(db, payload.get('parent_id'), question)
        # 补充问题或复查延续父任务角色；明确提交的新角色仍按准入规则校验。
        if parent_role is not None and 'role' not in payload:
            payload = {**payload, 'role': parent_role}
        active_store,active_catalog,fingerprint=self.dataset()
        prepared=prepare_task(payload,_store or active_store,_catalog or active_catalog,fingerprint,
            settings=self.model_settings,classifier=self.route_classifier,planner=self.query_planner,
            today=_today,confirmed_candidates=confirmed_candidates,conversation=_conversation,
            custom_store=_store is not None)
        parsed=prepared.parsed
        fingerprint=prepared.fingerprint
        parsed['operating_settings_snapshot']=self.operations.settings()
        external = freeze_context(parsed, _store or active_store, self.operations.book,
                                  self.database.parent / 'investigation_context.json')
        if external is not None:
            parsed['investigation_context'] = external
        if _review:
            # “昨天”按原任务锚点解释，不能在数日后复查时悄悄换调查日期。
            parsed["recheck_context"] = {"case_id": _review[0], "requested_at": utc_now(),
                                         "original_business_today": _today.isoformat()}
        status=prepared.status
        if _review and status != "queued":
            raise ReviewConflict("补充后的数据仍不能执行调查，请核对任务日期和必需来源。")
        task_id = uuid.uuid4().hex
        # IMMEDIATE 将“查幂等键→插入”置于一个写事务中，防止并发请求重复创建。
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if _review:
                self.reviews.checked(db, _review[0], _review[1], {"ready_for_recheck"})
                if db.execute("SELECT count(*) FROM review_runs WHERE case_id=?", (_review[0],)).fetchone()[0] >= 4:
                    raise ReviewConflict("本工单最多进行 3 次复查，请整理问题后新建调查。")
            existing=self.repository.replay(db,idempotency_key,request_hash)
            if existing:return {'task':self.get(existing,include_events=False),'reused':True}
            self.repository.parent_context(db, payload.get('parent_id'), question)
            if _mission and status!="queued":
                raise MissionConflict("Mission续跑需要明确商品、日期与可用数据，请先在经营调查中补齐条件。")
            if status=='queued':self.repository.check_capacity(db)
            timestamp = utc_now()
            # 在任务写入事务中读取并冻结，记忆版本及审计凭据与任务一起提交。
            memories,receipt=self.context_memory.retrieve(db,parsed)
            checkpoint()
            parsed['store_memory_snapshot']=memories
            parsed['memory_retrieval']=receipt
            self.repository.insert(db,task_id,payload,prepared,idempotency_key,request_hash)
            db.execute('INSERT INTO context_retrievals VALUES(?,?,?)',(task_id,timestamp,json.dumps(receipt,ensure_ascii=False)))
            self._event(db,task_id,'memory_retrieved',receipt)
            if _mission:
                parsed["mission_context"]=self.missions.attach(db,_mission[0],_mission[1],task_id)
                self.repository.update_parsed(db,task_id,parsed)
            if _review:
                db.execute("INSERT INTO review_runs VALUES(?,?)", (task_id, _review[0]))
                db.execute("UPDATE review_cases SET status='rechecking',latest_task_id=?,revision=revision+1,updated_at=? WHERE id=?", (task_id, utc_now(), _review[0]))
                self.reviews.event(db, _review[0], "recheck_requested", task_id=task_id, fingerprint=fingerprint)
            elif parsed["status"] == "needs_data" and parsed["sku_id"] and parsed["target_date"] and not parsed.get('product_scope'):
                self.reviews.open(db, task_id, parsed["issues"])
            self._event(db, task_id, "task_created", {"status": status, "parsed": parsed})
        # 队列仅是运行通知；任务已在 SQLite 中，下次启动可重新排入尚未执行的任务。
        if status=='queued':self.executor.enqueue(task_id,current_token())
        return {"task": self.get(task_id, include_events=False), "reused": False}

    def _event(self,db,task_id,kind,payload):
        """兼容Mission等业务扩展；事件与状态沿用同一事务。"""
        self.repository.event(db,task_id,kind,payload)

    def cancel(self,task_id):
        """应用入口返回网页详情；取消状态竞争由执行器统一处理。"""
        accepted=self.executor.cancel(task_id)
        return {'task':self.get(task_id,include_events=False),'accepted':accepted}

    def list_tasks(self):
        """列表只返回任务仓库的轻量摘要。"""
        return self.repository.list_tasks()

    def get(self, task_id, *, include_events=True):
        """详情返回解析卡、结构化结果和最近事件；内部 run_dir 不直接交给浏览器。"""
        with self.connection() as db:
            row=self.repository.fetch(db,task_id)
            if row is None:
                return None
            result = dict(row)
            result["parsed"] = json.loads(result.pop("parsed_json"))
            saved_state = result.pop("result_json")
            state = json.loads(saved_state) if saved_state else None
            if state:
                state.pop("run_dir", None)
                # 历史任务也使用新的阅读视图；不改写已保存的任务、证据和数据版本。
                analysis=state.get('analysis') or {}
                if analysis.get('kind')=='combined':
                    from .combined_answer import build_answer
                    analysis['business_answer']=build_answer(result['parsed'],analysis.get('sections',[]))
            result["result"] = state
            from .errors import AgentError
            problem=(state or {}).get('error_info') or result['parsed'].get('execution_error') or result['parsed'].get('error_info')
            if not problem and result['status'] in ('failed','interrupted','incomplete'):
                code={'failed':'execution_failed','interrupted':'execution_interrupted','incomplete':'execution_incomplete'}[result['status']]
                problem=AgentError(code,phase='execution').public()
            if result['status'] in ('cancelled','cancelling'):
                problem=AgentError('request_cancelled' if result['status']=='cancelled' else 'cancel_pending',phase='execution').public()
            if problem:
                result['error_info']=problem
                if result['status'] in ('failed','interrupted','incomplete'):
                    result['error']=problem['message']
                    if state and state.get('error'):state['error']=problem['message']
            result.pop("idempotency_key", None)
            result.pop("request_hash", None)
            result["review_case"] = self.reviews.for_task(db, task_id)
            result["decision"] = self.decisions.for_task(db, task_id)
            if include_events:
                records=self.repository.events(db,task_id)
                result["events"] = [{**dict(e), "payload": json.loads(e["payload"])} for e in reversed(records)]
            return result

    def artifact(self, task_id, name):
        """仅允许下载该任务已登记的报告和证据，客户端不能传任意服务器路径。"""
        if name not in ("report.md", "evidence.json", "events.jsonl", "request.json", "validation.json"):
            return None
        with self.connection() as db:
            row=self.repository.fetch_result(db,task_id)
        if not row or not row[0]:
            return None
        folder = Path(json.loads(row[0])["run_dir"])
        target = folder / name
        if target.is_symlink() or not target.resolve().is_relative_to(self.output) or not target.is_file():
            return None
        return target.read_bytes()

    def runtime(self):
        """运行时快照：任务状态计数与执行队列指标；不含问题、报告、事件和密钥。"""
        with self.connection() as db:
            counts={row['status']:row['n'] for row in db.execute('SELECT status,count(*) AS n FROM tasks GROUP BY status')}
        tasks={key:counts.get(key,0) for key in ('queued','running','cancelling','completed','failed','interrupted','incomplete','cancelled')}
        return {'tasks':tasks,'executor':self.executor.stats()}

    def close(self):
        """应用关闭交给执行器收尾；不改变历史任务和数据版本。"""
        self.board.close()
        self.executor.close()

    def _can_start(self,db,task_id):
        """业务扩展的执行门槛；任务认领前在同一事务检查Mission是否过期。"""
        mission=db.execute('SELECT m.status,m.deadline FROM missions m JOIN mission_runs r ON r.mission_id=m.id WHERE r.task_id=?',(task_id,)).fetchone()
        if mission and (mission['status']=='cancelled' or mission['deadline']<shanghai_today().isoformat()):
            self.repository.interrupt(db,task_id,'持续任务已取消或过期，本轮不再开始。')
            self.missions.settled(db,task_id);return False
        return True

    def _on_skipped(self,db,task_id):
        """排队取消或异常取消只通知持续任务，不冒充完整调查结果。"""
        self.missions.settled(db,task_id)

    def _on_result(self,db,task_id,state):
        """正常返回的结果与复查/Mission关联同事务提交。"""
        self.reviews.on_settled(db,task_id,state);self.missions.settled(db,task_id)

    def _on_failure(self,db,task_id):
        """执行异常更新业务跟进，下一条队列通知仍可继续。"""
        self.reviews.on_settled(db,task_id,{'status':'failed'});self.missions.settled(db,task_id)
