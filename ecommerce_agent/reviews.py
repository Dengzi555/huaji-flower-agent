"""持续核验工单：业务状态独立于单次 Run，材料追加、版本化复查、人工验收。

人工说明不是事实覆盖；结构化修正只作用于后续任务快照，原始数据与旧报告不变。
当前共享工作区无独立用户身份，审计只表示本地/持口令操作者提交，不声称多人 RBAC。
"""

import json
import uuid
from datetime import date

from .clock import utc_now
from .data import DataStore, FIELDS
from .task_parser import ProductCatalog

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cases (
 id TEXT PRIMARY KEY, origin_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 latest_task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL,
 reason TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_runs (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), case_id TEXT NOT NULL REFERENCES review_cases(id)
);
CREATE TABLE IF NOT EXISTS review_materials (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES review_cases(id), created_at TEXT NOT NULL,
 note TEXT NOT NULL, source TEXT NOT NULL, corrections TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL REFERENCES review_cases(id),
 created_at TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class ReviewConflict(ValueError):
    """状态或版本已变化，调用者应刷新页面，而不是覆盖别人的新材料。"""


class ReviewNotFound(ValueError):
    """工单不存在，HTTP 层可返回 404。"""


def review_reasons(state):
    """从执行结果提取待核验条件；完成状态不替代来源质量和有限字段验收。"""
    reasons = []
    analysis = state.get("analysis") or {}
    if state.get("status") != "completed":
        reasons.append("本轮执行未确认完成，需要检查失败或中断原因。")
    if analysis.get("conflicts") or analysis.get("data_quality") == "conflicting":
        reasons.append("来源存在冲突，需核对订单、库存或统计口径。")
    if analysis.get("gaps") or analysis.get("data_quality") == "partial":
        reasons.append("来源缺失、延迟或监测不足，需补充可核验材料。")
    if (state.get("follow_up") or {}).get("status") in ("conflicting", "insufficient_data"):
        reasons.append("追加渠道调查存在缺口或合计冲突。")
    if (state.get("validation") or {}).get("status") != "passed":
        reasons.append("有限字段验收尚未通过；引用存在不等于结论可核验。")
    return reasons


class ReviewCases:
    """复用调查服务的 SQLite 事务；版本号用于阻止并发材料和重复验收覆盖。"""

    def __init__(self, service):
        """只保存服务引用，建表由服务启动事务统一完成。"""
        self.service = service

    def event(self, db, case_id, kind, **payload):
        """工单时间线只追加事件，材料与旧任务不会被改写。"""
        db.execute("INSERT INTO review_events(case_id,created_at,type,payload) VALUES(?,?,?,?)",
                   (case_id, utc_now(), kind, json.dumps(payload, ensure_ascii=False)))

    def open(self, db, task_id, reasons):
        """每个起始任务最多创建一个工单，防止重复结算时产生多份待办。"""
        existing = db.execute("SELECT id FROM review_cases WHERE origin_task_id=?", (task_id,)).fetchone()
        if existing:
            return existing[0]
        case_id, now = uuid.uuid4().hex, utc_now()
        db.execute("INSERT INTO review_cases VALUES(?,?,?,?,?,?,?,?)", (
            case_id, task_id, task_id, "awaiting_human", json.dumps(reasons, ensure_ascii=False), 1, now, now))
        db.execute("INSERT INTO review_runs VALUES(?,?)", (task_id, case_id))
        self.event(db, case_id, "case_opened", task_id=task_id, reasons=reasons)
        return case_id

    def on_settled(self, db, task_id, state):
        """复查结束后更新业务状态；没有新冲突也只进入待验收，绝不自动关单。"""
        reasons = review_reasons(state)
        linked = db.execute("SELECT c.* FROM review_cases c JOIN review_runs r ON r.case_id=c.id WHERE r.task_id=?", (task_id,)).fetchone()
        if linked:
            if linked["latest_task_id"] != task_id or linked["status"] != "rechecking":
                return
            status = "awaiting_human" if reasons else "awaiting_acceptance"
            db.execute("UPDATE review_cases SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                       (status, utc_now(), linked["id"]))
            self.event(db, linked["id"], "recheck_settled", task_id=task_id, status=status, reasons=reasons)
        elif reasons:
            self.open(db, task_id, reasons)

    def for_task(self, db, task_id):
        """给任务详情附轻量业务状态，不把整份材料和数据快照混入每次轮询。"""
        row = db.execute("SELECT c.* FROM review_cases c JOIN review_runs r ON r.case_id=c.id WHERE r.task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self):
        """工作区最近工单，既显示待办，也保留已验收记录。"""
        with self.service.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM review_cases ORDER BY updated_at DESC LIMIT 100")]

    def get(self, case_id):
        """读取工单、追加材料、各轮调查和事件，不返回服务器目录或模型密钥。"""
        with self.service.connection() as db:
            row = db.execute("SELECT * FROM review_cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise ReviewNotFound("核验工单不存在。")
            case = dict(row)
            case["reasons"] = json.loads(case.pop("reason"))
            case["materials"] = [{**dict(row), "corrections": json.loads(row["corrections"])} for row in
                                  db.execute("SELECT * FROM review_materials WHERE case_id=? ORDER BY rowid", (case_id,))]
            case["events"] = [{**dict(row), "payload": json.loads(row["payload"])} for row in
                               db.execute("SELECT * FROM review_events WHERE case_id=? ORDER BY id", (case_id,))]
            case["runs"] = [dict(row) for row in db.execute(
                "SELECT t.id,t.status,t.created_at,t.fingerprint FROM tasks t JOIN review_runs r ON r.task_id=t.id WHERE r.case_id=? ORDER BY t.created_at,t.id", (case_id,))]
            original = db.execute("SELECT parsed_json FROM tasks WHERE id=?", (case["origin_task_id"],)).fetchone()
            parsed = json.loads(original[0])
            case["scope"] = {key: parsed.get(key) for key in ("sku_id", "target_date", "comparison_date", "intent")}
            return case

    def checked(self, db, case_id, revision, statuses):
        """统一执行乐观锁与状态门禁；写入事务必须在调用前 BEGIN IMMEDIATE。"""
        if type(revision) is not int or revision < 1:
            raise ValueError("请提供有效的 expected_revision。")
        row = db.execute("SELECT * FROM review_cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ReviewNotFound("核验工单不存在。")
        if row["revision"] != revision or row["status"] not in statuses:
            raise ReviewConflict("工单状态或版本已变化，请刷新后重试。")
        return dict(row)

    def dataset(self, db, case):
        """始终从起始任务快照重建，再按时间叠加材料，避免重复应用或污染原始数据。"""
        row = db.execute("SELECT document,catalog FROM task_datasets WHERE task_id=?", (case["origin_task_id"],)).fetchone()
        if not row:
            raise ReviewConflict("旧任务没有可复查的数据快照，请基于当前数据重新调查。")
        document, catalog = json.loads(row[0]), json.loads(row[1])
        parsed = json.loads(db.execute("SELECT parsed_json FROM tasks WHERE id=?", (case["origin_task_id"],)).fetchone()[0])
        for material in db.execute("SELECT * FROM review_materials WHERE case_id=? ORDER BY rowid", (case["id"],)):
            self.apply(document, parsed, json.loads(material["corrections"]), material["source"], material["created_at"], material["id"])
        return document, catalog, parsed

    def apply(self, document, parsed, corrections, source, received_at, material_id):
        """只允许同 SKU、原两日、白名单数据域的完整字段修正；所有内容仍标记为人工提交未独立核验。"""
        if not isinstance(corrections, list) or len(corrections) > 10:
            raise ValueError("corrections 必须是最多 10 条的数组。")
        seen = set()
        for correction in corrections:
            if not isinstance(correction, dict) or set(correction) != {"table", "date", "values"}:
                raise ValueError("每条修正只支持 table、date、values。")
            table, day, values = correction["table"], correction["date"], correction["values"]
            if (not isinstance(table, str) or table not in FIELDS or not isinstance(day, str)
                    or day not in (parsed["target_date"], parsed["comparison_date"])):
                raise ValueError("修正仅限本工单的数据域和原调查两日。")
            if (table, day) in seen:
                raise ValueError("同一材料中不可重复修正同一日和数据域。")
            seen.add((table, day))
            if not isinstance(values, dict) or set(values) != set(FIELDS[table]):
                raise ValueError("values 必须完整提供该数据域的指标字段，不接受 SKU、路径或额外参数。")
            replacement = {"sku_id": parsed["sku_id"], "date": day, "status": "ok",
                           "source_id": "human-submitted:" + source, "observed_at": received_at,
                           "verification_status": "human_supplied_unverified", "material_id": material_id, **values}
            document[table] = [row for row in document.get(table, []) if not (row["sku_id"] == parsed["sku_id"] and row["date"] == day)] + [replacement]

    def add_material(self, case_id, payload):
        """先校验完整新快照，再在同一事务追加材料、递增版本；说明文字不改变指标。"""
        if not isinstance(payload, dict) or set(payload) - {"expected_revision", "note", "source", "corrections"}:
            raise ValueError("材料只支持 expected_revision、note、source、corrections。")
        note, source = payload.get("note"), payload.get("source")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 4000:
            raise ValueError("请填写 1–4000 字的核验说明。")
        if not isinstance(source, str) or not 1 <= len(source.strip()) <= 100:
            raise ValueError("请填写材料来源，最多 100 字。")
        corrections = payload.get("corrections", [])
        material_id, now = uuid.uuid4().hex, utc_now()
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self.checked(db, case_id, payload.get("expected_revision"), {"awaiting_human", "ready_for_recheck", "awaiting_acceptance"})
            if db.execute("SELECT count(*) FROM review_materials WHERE case_id=?", (case_id,)).fetchone()[0] >= 20:
                raise ReviewConflict("本工单已达 20 份材料上限，请整理材料后新建调查。")
            document, _, parsed = self.dataset(db, case)
            self.apply(document, parsed, corrections, source, now, material_id)
            DataStore.from_document(document)
            db.execute("INSERT INTO review_materials VALUES(?,?,?,?,?,?)", (material_id, case_id, now, note, source,
                                                                          json.dumps(corrections, ensure_ascii=False)))
            db.execute("UPDATE review_cases SET status='ready_for_recheck',revision=revision+1,updated_at=? WHERE id=?", (now, case_id))
            self.event(db, case_id, "material_added", material_id=material_id, corrections=len(corrections), actor="workspace_operator")
        return self.get(case_id)

    def recheck(self, case_id, payload):
        """用户显式发起新一轮；任务创建和工单状态更新由服务的同一事务完成。"""
        if not isinstance(payload, dict) or set(payload) != {"expected_revision"}:
            raise ValueError("复查需提供 expected_revision。")
        with self.service.connection() as db:
            case = self.checked(db, case_id, payload["expected_revision"], {"ready_for_recheck"})
            document, catalog, parsed = self.dataset(db, case)
        store = DataStore.from_document(document)
        catalog = ProductCatalog.from_document(catalog, store.skus)
        result = self.service.create({"question": parsed["raw_question"], "sku": parsed["sku_id"],
                                      "date": parsed["target_date"], "intent": parsed["intent"],
                                      "parent_id": case["latest_task_id"]},
                                     _store=store, _catalog=catalog, _review=(case_id, payload["expected_revision"]),
                                     _today=date.fromisoformat(parsed["business_today"]))
        return {"case": self.get(case_id), **result}

    def accept(self, case_id, payload):
        """人工验收只关闭业务工单；必须基于最新复查，且不能绕过仍存在的缺口。"""
        if not isinstance(payload, dict) or set(payload) != {"expected_revision", "task_id", "note"}:
            raise ValueError("验收需提供 expected_revision、task_id、note。")
        if not isinstance(payload["note"], str) or not 1 <= len(payload["note"].strip()) <= 2000:
            raise ValueError("请填写验收说明，最多 2000 字。")
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self.checked(db, case_id, payload["expected_revision"], {"awaiting_acceptance"})
            if case["latest_task_id"] != payload["task_id"]:
                raise ReviewConflict("只能验收最新一次复查，旧报告不能代替新结果。")
            row = db.execute("SELECT result_json FROM tasks WHERE id=?", (case["latest_task_id"],)).fetchone()
            if not row or not row[0] or review_reasons(json.loads(row[0])):
                raise ReviewConflict("最新复查仍有未通过的验收条件。")
            db.execute("UPDATE review_cases SET status='resolved',revision=revision+1,updated_at=? WHERE id=?", (utc_now(), case_id))
            self.event(db, case_id, "case_accepted", task_id=payload["task_id"], note=payload["note"], actor="workspace_operator")
        return self.get(case_id)
