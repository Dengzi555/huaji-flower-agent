"""任务仓库：SQLite连接、任务/快照/事件读写与状态更新；不调用模型、不启动线程。

调用者传入同一事务连接，保证任务、记忆检索和复查关联一起提交；连接不跨线程共享。
"""
import json
import sqlite3
from contextlib import contextmanager

from .clock import utc_now


class ConflictError(ValueError):
    """同一幂等键对应不同内容时拒绝复用；由应用/API层映射为冲突响应。"""


SCHEMA='''
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, question TEXT NOT NULL, parsed_json TEXT NOT NULL,
 result_json TEXT, error TEXT, parent_id TEXT, fingerprint TEXT NOT NULL,
 idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, created_at TEXT NOT NULL,
 type TEXT NOT NULL, payload TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS events_task ON task_events(task_id,id);
CREATE TABLE IF NOT EXISTS task_cancellations (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),requested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_datasets (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), document TEXT NOT NULL,
 catalog TEXT NOT NULL, fingerprint TEXT NOT NULL
);
'''


class TaskRepository:
    """一个门店一个仓库；只接受服务端固定的数据库路径，不接受客户端路径。"""
    def __init__(self,path):self.path=path

    @contextmanager
    def connection(self):
        """事务成功才提交、异常回滚；finally关闭连接，避免Windows锁与线程串用。

        WAL 让网页读请求不被后台写事务阻塞；busy_timeout 处理短暂写竞争；
        synchronous=NORMAL 在 WAL 下仍保证进程崩溃不丢已提交事务，代价是断电边界更弱。
        """
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:yield db
        finally:db.close()

    def initialize(self,db):
        """只初始化任务自己的表；业务扩展表由应用组合，沿用原数据库布局。"""
        db.executescript(SCHEMA)

    def event(self,db,task_id,kind,payload):
        """使用调用者事务追加审计事件，与状态更新同提交/回滚。"""
        db.execute('INSERT INTO task_events(task_id,created_at,type,payload) VALUES(?,?,?,?)',
            (task_id,utc_now(),kind,json.dumps(payload,ensure_ascii=False)))

    def replay(self,db,key,digest):
        """返回可复用任务ID；在模型前预查，写入事务中再查以解决并发竞争。"""
        row=db.execute('SELECT id,request_hash FROM tasks WHERE idempotency_key=?',(key,)).fetchone() if key else None
        if row and row['request_hash']!=digest:raise ConflictError('同一个幂等键不能用于不同的问题。')
        return row['id'] if row else None

    def parent_candidates(self,db,parent_id,question):
        """父任务必须属于本仓库；仅复用待确认意图候选，不继承指标。"""
        if not parent_id:return None
        row=db.execute('SELECT question,parsed_json FROM tasks WHERE id=?',(parent_id,)).fetchone()
        if not row:raise ValueError('原任务不存在。')
        route=json.loads(row['parsed_json']).get('routing',{})
        return route.get('candidates',[]) if row['question']==question and route.get('status')=='needs_clarification' else None

    def insert(self,db,task_id,payload,prepared,key,digest):
        """任务索引与数据快照同事务写入；只有提交成功，调用者才可以入队。"""
        timestamp=utc_now()
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(task_id,timestamp,timestamp,
            prepared.status,payload.get('question'),json.dumps(prepared.parsed,ensure_ascii=False),None,None,
            payload.get('parent_id'),prepared.fingerprint,key,digest))
        db.execute('INSERT INTO task_datasets VALUES(?,?,?,?)',(task_id,json.dumps(prepared.document,ensure_ascii=False),
            json.dumps(prepared.catalog,ensure_ascii=False),prepared.fingerprint))

    def recover(self,db,fingerprint):
        """先恢复取消意图，再将结果未知的在途任务中断；仅返回可恢复的排队ID。"""
        db.execute("UPDATE tasks SET status='cancelled',error=NULL,updated_at=? WHERE status='cancelling' OR (status IN ('queued','running') AND id IN (SELECT task_id FROM task_cancellations))",(utc_now(),))
        db.execute("UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE status='running'",
            ('服务曾中断，本次执行未确认完成；可基于原问题重新调查。',utc_now()))
        db.execute("UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE status='queued' AND fingerprint != ? AND NOT EXISTS (SELECT 1 FROM task_datasets d WHERE d.task_id=tasks.id)",
            ('数据快照已变化，请重新提交以使用新的数据版本。',utc_now(),fingerprint))
        return [row[0] for row in db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at")]

    def list_tasks(self):
        """列表只查摘要，不加载报告、年度快照或事件。"""
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT id,created_at,updated_at,status,question,parent_id FROM tasks ORDER BY created_at DESC LIMIT 100')]

    def snapshot(self,task_id):
        """返回创建时冻结的数据，不重新读取导入后工作区。"""
        with self.connection() as db:row=db.execute('SELECT document FROM task_datasets WHERE task_id=?',(task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def fetch(self,db,task_id):
        """内部原始行只交给应用投影层；网页不得直接接收密钥幂等字段和目录。"""
        return db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()

    def events(self,db,task_id):
        """详情最近100条事件，按倒序读取，由应用恢复展示顺序。"""
        return db.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 100',(task_id,)).fetchall()

    def fetch_result(self,db,task_id):
        """下载入口只取已登记结果，文件路径校验仍由应用层执行。"""
        return db.execute('SELECT result_json FROM tasks WHERE id=?',(task_id,)).fetchone()

    def check_capacity(self,db):
        """与插入共用写事务，避免并发越过待执行数量限制。"""
        if db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]>=24:
            raise ConflictError('待执行任务较多，请稍后再提交。')

    def update_parsed(self,db,task_id,parsed):
        """同一创建事务补入Mission关联，不改写既有任务的原始数据版本。"""
        db.execute('UPDATE tasks SET parsed_json=? WHERE id=?',(json.dumps(parsed,ensure_ascii=False),task_id))

    def interrupt(self,db,task_id,message):
        """业务执行门槛不满足时停止认领，不能报告已执行成功。"""
        db.execute("UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE id=?",(message,utc_now(),task_id))
        self.event(db,task_id,'mission_not_current',{})

    def status(self,db,task_id):
        """内部状态读取；外部请求查不到本门店任务时明确拒绝。"""
        row=db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row:raise ValueError('当前门店不存在该任务。')
        return row[0]

    def request_cancel(self,db,task_id,status):
        """queued直接取消，running等待执行器收尾；持久取消意图用于重启恢复。"""
        db.execute('INSERT OR IGNORE INTO task_cancellations VALUES(?,?)',(task_id,utc_now()))
        target='cancelled' if status=='queued' else 'cancelling'
        db.execute('UPDATE tasks SET status=?,updated_at=? WHERE id=?',(target,utc_now(),task_id))
        self.event(db,task_id,'cancel_requested',{'previous_status':status,'status':target})
        return target

    def claim(self,db,task_id):
        """仅在写事务中认领queued任务；取消后的队列通知不能重新启动任务。"""
        row=db.execute("SELECT parsed_json FROM tasks WHERE id=? AND status='queued'",(task_id,)).fetchone()
        if not row:return None
        db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?",(utc_now(),task_id))
        self.event(db,task_id,'task_running',{})
        return json.loads(row[0])

    def finish(self,db,task_id,state):
        """终态已由执行器在取消锁内决定；仓库只负责原子写入结果与事件。"""
        db.execute('UPDATE tasks SET status=?,result_json=?,error=?,updated_at=? WHERE id=?',
            (state['status'],json.dumps(state,ensure_ascii=False),state.get('error'),utc_now(),task_id))
        self.event(db,task_id,'task_settled',{'status':state['status']})

    def fail(self,db,task_id,problem,*,cancelled=False):
        """异常收尾也尊重已持久化的取消；供应商原始异常不进数据库。"""
        if cancelled:
            db.execute("UPDATE tasks SET status='cancelled',error=NULL,updated_at=? WHERE id=?",(utc_now(),task_id))
            self.event(db,task_id,'task_cancelled',{});return
        row=db.execute('SELECT parsed_json FROM tasks WHERE id=?',(task_id,)).fetchone()
        parsed=json.loads(row[0]);parsed['execution_error']=problem
        db.execute("UPDATE tasks SET status='failed',error=?,updated_at=?,parsed_json=? WHERE id=?",
            (problem['message'],utc_now(),json.dumps(parsed,ensure_ascii=False),task_id))
        self.event(db,task_id,'task_failed',{'error_info':problem})
