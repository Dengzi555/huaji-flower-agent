"""门店持续任务与记忆：借鉴CRM的Mission/Run边界，业务验收不随一次运行自动完成。"""

import json
import re
import uuid

from .clock import utc_now
from .task_parser import shanghai_today

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
 id TEXT PRIMARY KEY,title TEXT NOT NULL,goal TEXT NOT NULL,acceptance TEXT NOT NULL,
 deadline TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,max_runs INTEGER NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,acceptance_note TEXT
);
CREATE TABLE IF NOT EXISTS mission_runs (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),mission_id TEXT NOT NULL REFERENCES missions(id)
);
CREATE TABLE IF NOT EXISTS mission_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL REFERENCES missions(id),
 created_at TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_memories (
 id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,content TEXT NOT NULL,
 source TEXT NOT NULL,source_task_id TEXT REFERENCES tasks(id),expires_on TEXT,
 status TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT NOT NULL REFERENCES store_memories(id),
 created_at TEXT NOT NULL,type TEXT NOT NULL,note TEXT NOT NULL
);
"""


def text(value, field, limit=1000):
    """校验用户文本，拒绝空值和超长内容，不执行其中指令。"""
    if not isinstance(value,str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{field}需为1–{limit}字文本。")
    return value.strip()


class MissionNotFound(ValueError):
    """对象不属于当前门店或不存在，HTTP返回404。"""


class MissionConflict(ValueError):
    """版本、期限、额度或状态不允许继续，HTTP返回409。"""


class Missions:
    """复用单店SQLite事务；Run关联、状态变化和任务创建在同一事务提交。"""

    def __init__(self,service):
        self.service = service

    def event(self,db,mid,kind,**payload):
        """时间线追加审计事件，目标和历史运行不被覆盖。"""
        db.execute("INSERT INTO mission_events(mission_id,created_at,type,payload) VALUES(?,?,?,?)",
            (mid,utc_now(),kind,json.dumps(payload,ensure_ascii=False)))

    def checked(self,db,mid,revision=None):
        """读取本店对象并做乐观版本检查，防止旧页面覆盖新状态。"""
        row = db.execute("SELECT * FROM missions WHERE id=?",(mid,)).fetchone()
        if not row: raise MissionNotFound("当前门店没有这个持续任务。")
        if revision is not None and (type(revision) is not int or row["revision"] != revision):
            raise MissionConflict("持续任务版本已变化，请刷新后操作。")
        return dict(row)

    def create(self,payload):
        """保存目标、验收条件和期限，不创建无人确认的后台循环。"""
        if not isinstance(payload,dict) or set(payload)-{"title","goal","acceptance","deadline"}:
            raise ValueError("持续任务仅接受标题、目标、验收条件和截止日期。")
        values = [text(payload.get(key),key,120 if key=="title" else 1500) for key in ("title","goal","acceptance")]
        deadline = payload.get("deadline")
        from datetime import date
        deadline=date.fromisoformat(deadline).isoformat()
        if deadline < shanghai_today().isoformat(): raise ValueError("截止日期不能早于业务今天。")
        mid,now=uuid.uuid4().hex,utc_now()
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM missions WHERE status NOT IN ('completed','cancelled')").fetchone()[0]>=20:
                raise MissionConflict("当前门店最多保留20个未终结持续任务。")
            db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?)",(mid,*values,deadline,"active",1,4,now,now,None))
            self.event(db,mid,"mission_created")
        return self.get(mid)

    def list(self):
        """列表按店隔离；不把Mission完成状态从Run文本里推断出来。"""
        with self.service.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM missions ORDER BY updated_at DESC LIMIT 100")]

    def get(self,mid):
        """返回历次运行、补充方向和验收记录，供刷新/重启后继续工作。"""
        with self.service.connection() as db:
            item=self.checked(db,mid)
            item["runs"]=[dict(row) for row in db.execute("SELECT t.id,t.status,t.question,t.created_at FROM tasks t JOIN mission_runs m ON m.task_id=t.id WHERE m.mission_id=? ORDER BY t.created_at",(mid,))]
            item["events"]=[{**dict(row),"payload":json.loads(row["payload"])} for row in db.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY id",(mid,))]
            item["business_outcome_verified"]=False
            item["acceptance_kind"]="human_attested" if item["status"]=="completed" else None
            return item

    def launch(self,mid,payload):
        """显式续跑，幂等键防重复；新轮必须是已经确定商品、日期的只读查询。"""
        if not isinstance(payload,dict) or set(payload)-{"expected_revision","question","request_id"}:
            raise ValueError("续跑仅接受版本、问题和请求ID。")
        request_id=payload.get("request_id","")
        if type(payload.get("expected_revision")) is not int: raise MissionConflict("续跑必须提供当前任务版本。")
        if not isinstance(request_id,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,32}",request_id): raise ValueError("续跑需要8–32位幂等请求ID。")
        return self.service.create({"question":text(payload.get("question"),"调查问题",2000)},
            "mission_"+mid+"_"+request_id,_mission=(mid,payload.get("expected_revision")))

    def attach(self,db,mid,revision,task_id):
        """在创建任务的同一事务检查额度/期限并关联，失败时整笔回滚。"""
        item=self.checked(db,mid,revision)
        if item["status"] not in ("active","needs_review"): raise MissionConflict("请先补充信息或结束当前轮，不能并行续跑同一Mission。")
        if item["deadline"] < shanghai_today().isoformat(): raise MissionConflict("持续任务已过期，不能自动续跑，请人工取消或新建目标。")
        if db.execute("SELECT count(*) FROM mission_runs WHERE mission_id=?",(mid,)).fetchone()[0]>=item["max_runs"]:
            raise MissionConflict("本Mission已达到4轮上限，请人工复核后收尾。")
        db.execute("INSERT INTO mission_runs VALUES(?,?)",(task_id,mid))
        db.execute("UPDATE missions SET status='running',revision=revision+1,updated_at=? WHERE id=?",(utc_now(),mid))
        self.event(db,mid,"run_requested",task_id=task_id)
        directions=[json.loads(row[0]).get("note","") for row in db.execute("SELECT payload FROM mission_events WHERE mission_id=? AND type='follow_up' ORDER BY id DESC LIMIT 3",(mid,))]
        return {"id":mid,"goal":item["goal"],"acceptance":item["acceptance"],"deadline":item["deadline"],"direction_notes":directions,"business_outcome_verified":False}

    def settled(self,db,task_id):
        """不论运行成功与否都进入人工复核；已取消任务不会被迟到结果重新打开。"""
        row=db.execute("SELECT m.id,m.status FROM missions m JOIN mission_runs r ON r.mission_id=m.id WHERE r.task_id=?",(task_id,)).fetchone()
        if row and row["status"]=="running":
            db.execute("UPDATE missions SET status='needs_review',revision=revision+1,updated_at=? WHERE id=?",(utc_now(),row["id"]))
            self.event(db,row["id"],"run_settled_needs_review",task_id=task_id)

    def action(self,mid,payload):
        """补充方向、等待内部信息、人工完成或取消；说明是人工陈述，不是外部事实证明。"""
        if not isinstance(payload,dict) or set(payload)-{"expected_revision","action","note"}: raise ValueError("持续任务操作字段不合法。")
        action=payload.get("action")
        if action not in ("follow_up","wait_internal","complete","cancel"): raise ValueError("未知持续任务操作。")
        note=text(payload.get("note"),"操作说明",1500)
        if type(payload.get("expected_revision")) is not int: raise MissionConflict("操作必须提供当前任务版本。")
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            item=self.checked(db,mid,payload.get("expected_revision"))
            if item["status"] in ("completed","cancelled"): raise MissionConflict("持续任务已经终结。")
            if item["status"]=="running" and action!="cancel": raise MissionConflict("本轮运行中；可取消，其他操作需等本轮结束。")
            if action=="complete":
                latest=db.execute("SELECT t.status,t.result_json FROM tasks t JOIN mission_runs r ON r.task_id=t.id WHERE r.mission_id=? ORDER BY t.created_at DESC LIMIT 1",(mid,)).fetchone()
                if item["status"]!="needs_review" or not latest or latest["status"]!="completed": raise MissionConflict("需要一轮已完成的调查及人工复核，不能直接验收。")
                state=json.loads(latest["result_json"] or "{}")
                if (state.get("validation") or {}).get("status")!="passed": raise MissionConflict("最近一轮字段核验未通过，不能验收。")
            status={"follow_up":"active","wait_internal":"waiting_internal","complete":"completed","cancel":"cancelled"}[action]
            if action=="cancel":
                for row in db.execute("SELECT t.id FROM tasks t JOIN mission_runs r ON r.task_id=t.id WHERE r.mission_id=? AND t.status='queued'",(mid,)).fetchall():
                    db.execute("UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE id=?",("持续任务已取消，本轮未开始。",utc_now(),row[0]))
                    self.service._event(db,row[0],"mission_cancelled_before_execution",{})
            db.execute("UPDATE missions SET status=?,revision=revision+1,updated_at=?,acceptance_note=? WHERE id=?",(status,utc_now(),note if action=="complete" else item["acceptance_note"],mid))
            self.event(db,mid,action,note=note,attestation="human_supplied")
        return self.get(mid)


class StoreMemory:
    """门店长期背景与偏好，和销售/库存事实证据分开；不自动记住模型结论。"""

    def __init__(self,service): self.service=service

    def list(self):
        """保留撤回和过期记录，读取时显式计算有效性，不悄悄删除旧记忆。"""
        with self.service.connection() as db:
            items=[dict(row) for row in db.execute("SELECT * FROM store_memories ORDER BY created_at DESC LIMIT 200")]
        today=shanghai_today().isoformat()
        for item in items:
            item["expired"]=bool(item["expires_on"] and item["expires_on"]<today)
            item["verification_status"]="human_supplied_unverified"
        return items

    def create(self,payload):
        """人工明确保存；观察结论必须关联本店已完成调查并设有效期。"""
        keys={"title","kind","content","source","source_task_id","expires_on"}
        if not isinstance(payload,dict) or set(payload)-keys: raise ValueError("门店记忆字段不合法。")
        title,content,source=[text(payload.get(key),key,80 if key=="title" else 500) for key in ("title","content","source")]
        kind=payload.get("kind")
        if kind not in ("preference","policy","observation"): raise ValueError("记忆类型须为偏好、经营约束或阶段观察。")
        expires=payload.get("expires_on") or None
        if expires:
            from datetime import date
            expires=date.fromisoformat(expires).isoformat()
            if expires<shanghai_today().isoformat(): raise ValueError("新记忆有效期不能早于今天。")
        task_id=payload.get("source_task_id") or None
        if kind=="observation" and (not expires or not task_id): raise ValueError("阶段观察需要本店调查任务ID和有效期，不能永久记住旧结论。")
        mid=uuid.uuid4().hex
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if task_id and not db.execute("SELECT 1 FROM tasks WHERE id=? AND status='completed'",(task_id,)).fetchone(): raise MissionNotFound("当前门店不存在这条已完成调查。")
            if db.execute("SELECT count(*) FROM store_memories WHERE status='active' AND (expires_on IS NULL OR expires_on>=?)",(shanghai_today().isoformat(),)).fetchone()[0]>=20: raise MissionConflict("每店最多20条有效记忆，请先撤回旧记录。")
            if db.execute("SELECT 1 FROM store_memories WHERE title=? AND kind=? AND status='active'",(title,kind)).fetchone(): raise MissionConflict("同名同类记忆已存在；请先撤回旧记录，避免互相矛盾。")
            db.execute("INSERT INTO store_memories VALUES(?,?,?,?,?,?,?,?,?,?)",(mid,title,kind,content,source,task_id,expires,"active",1,utc_now()))
            db.execute("INSERT INTO memory_events(memory_id,created_at,type,note) VALUES(?,?,?,?)",(mid,utc_now(),"created",source))
        return next(item for item in self.list() if item["id"]==mid)

    def retract(self,mid,payload):
        """显式撤回并记录原因，已经冻结进旧任务的背景不会被追溯修改。"""
        if not isinstance(payload,dict) or set(payload)-{"expected_revision","note"}: raise ValueError("撤回记忆字段不合法。")
        note=text(payload.get("note"),"撤回原因",500)
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM store_memories WHERE id=?",(mid,)).fetchone()
            if not row: raise MissionNotFound("当前门店没有该记忆。")
            if row["status"]!="active" or type(payload.get("expected_revision")) is not int or row["revision"]!=payload["expected_revision"]: raise MissionConflict("记忆状态或版本已变化。")
            db.execute("UPDATE store_memories SET status='retracted',revision=revision+1 WHERE id=?",(mid,))
            db.execute("INSERT INTO memory_events(memory_id,created_at,type,note) VALUES(?,?,?,?)",(mid,utc_now(),"retracted",note))
        return {"id":mid,"status":"retracted"}

    def context(self):
        """新任务只带入本店未过期背景；不参与证据字段验收，不覆盖每日业务数据。"""
        return [item for item in self.list() if item["status"]=="active" and not item["expired"]][:20]
