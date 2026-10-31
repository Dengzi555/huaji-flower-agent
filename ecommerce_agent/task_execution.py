"""任务执行器：单进程后台队列、认领、取消与终态竞争；不解析用户意图，不持有HTTP对象。

数据库操作交给仓库；复查/Mission通过事务回调接入，不反向导入网页服务。
取消与完成共用锁；锁定顺序始终是取消锁→数据库写事务，避免相互等待。
"""
import queue
import threading
from pathlib import Path
from .data import DataStore
from .cancellation import CancelToken,CancelledError,cancel_scope,cancel_state,checkpoint
from .errors import from_exception


class TaskExecutor:
    """显式注入执行函数、配置读取器与生命周期回调，可用独立仓库测试，无需网页服务。"""
    def __init__(self,repository,output,*,execute,default_store,settings,can_start,on_skipped,on_result,on_failure):
        self.repository=repository;self.output=Path(output).resolve()
        self.execute=execute;self.default_store=default_store;self.settings=settings
        self.can_start=can_start;self.on_skipped=on_skipped;self.on_result=on_result;self.on_failure=on_failure
        self.work=queue.Queue(maxsize=32);self.closed=threading.Event();self.lock=threading.RLock()
        self.tokens={};self.thread=None;self.current=None

    def start(self,pending=(),*,enabled=True):
        """启动前恢复已提交的队列通知；不把running或cancelled恢复为queued。"""
        for task_id in pending:self.enqueue(task_id)
        if enabled:
            self.thread=threading.Thread(target=self._worker,name='investigation-worker',daemon=True)
            self.thread.start()

    def enqueue(self,task_id,parent=None):
        """调用者必须先提交数据库；内存队列丢失时，可按持久状态恢复通知。

        队列满时不把任务留在 queued：数据库已提交，若通知丢失，任务会永远
        停在“待执行”。这里显式置为失败并留下事件，用户可以重试。
        """
        token=CancelToken(parent)
        with self.lock:self.tokens[task_id]=token
        token.on_cancel(lambda:self.cancel(task_id))
        try:self.work.put(task_id,timeout=2)
        except queue.Full:
            self._reject(task_id);return False
        return True

    def _reject(self,task_id):
        """入队失败后的补偿：只处理仍为 queued 的任务，不覆盖取消或已完成终态。"""
        problem={'code':'queue_full','message':'执行队列已满，任务尚未开始；请稍后重试。',
                 'phase':'admission','retryable':True}
        with self.lock,self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:status=self.repository.status(db,task_id)
            except ValueError:status=None
            if status!='queued':self.tokens.pop(task_id,None);return
            self.repository.fail(db,task_id,problem)
            try:self.on_failure(db,task_id)
            except Exception:pass  # 失败回调不能回滚已确定的失败终态
            self.tokens.pop(task_id,None)

    def stats(self):
        """运行时快照：队列深度、容量、当前任务与工作线程状态；不读报告正文。"""
        with self.lock:
            thread=self.thread
            return {'queue_depth':self.work.qsize(),'queue_capacity':self.work.maxsize,
                'registered_tokens':len(self.tokens),'current_task':self.current,
                'worker_alive':bool(thread and thread.is_alive()),'accepting':not self.closed.is_set()}

    def cancel(self,task_id):
        """返回是否接受停止；终态不可改写，先提交取消意图再通知在途执行。"""
        with self.lock:
            with self.repository.connection() as db:
                db.execute('BEGIN IMMEDIATE');status=self.repository.status(db,task_id)
                if status not in ('queued','running'):return status in ('cancelling','cancelled')
                target=self.repository.request_cancel(db,task_id,status)
                if target=='cancelled':self.on_skipped(db,task_id)
            token=self.tokens.get(task_id)
        if token:token.cancel()
        return True

    def _persist_cancelled(self,state,token):
        """修正尚未提交的迟到产物；已有证据保留，报告明确说明只是部分结果。"""
        cancel_state(state,token)
        if state.get('run_dir'):
            folder=Path(state['run_dir']).resolve()
            if folder.is_relative_to(self.output) and folder.is_dir():
                from .runner import write_json
                write_json(folder/'run.json',state)
                (folder/'report.md').write_text('# 已停止的部分结果\n\n本次查询已停止，不代表完成。已取回的观察保存在 evidence.json，不能把部分结果当最终结论。\n',encoding='utf-8')

    def _claim(self,task_id):
        """认领与令牌绑定一起受取消锁保护，重启恢复的任务也没有取消窗口。"""
        with self.lock,self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.repository.fetch(db,task_id)
            if row is None or row['status']!='queued':return None
            if not self.can_start(db,task_id):return None
            task=self.repository.claim(db,task_id)
            token=self.tokens.setdefault(task_id,CancelToken())
            return task,token

    def _execute(self,task_id,task,token):
        """从冻结快照构建工具数据源；模型配置读取器仅在本次确需模型时调用。"""
        snapshot=self.repository.snapshot(task_id)
        store=DataStore.from_document(snapshot) if snapshot else self.default_store()
        def observe(entry):
            with self.repository.connection() as db:self.repository.event(db,task_id,entry['type'],entry)
        with cancel_scope(token):
            checkpoint();options={}
            if task.get('model_execution'):
                settings=self.settings()
                if not settings:raise ValueError('本地模型配置不可用。')
                options={'provider':settings.provider(task['model_execution']['model']),'max_steps':12,'timeout_seconds':180}
            return self.execute(store,task['sku_id'],task['target_date'],self.output,
                task_context=task,on_event=observe,**options)

    def _settle(self,task_id,state,token):
        """取消先提交则覆盖迟到完成，完成先提交则后续停止不改写终态。"""
        with self.lock,self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.repository.status(db,task_id) in ('cancelling','cancelled') or token.event.is_set():
                self._persist_cancelled(state,token)
            self.repository.finish(db,task_id,state);self.on_result(db,task_id,state)

    def _fail(self,task_id,exc):
        """失败隔离：单条任务异常只影响本条，不结束后台线程。"""
        problem=from_exception(exc,phase='execution',fallback='execution_failed').public()
        with self.lock,self.repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            cancelled=self.repository.status(db,task_id) in ('cancelling','cancelled') or isinstance(exc,CancelledError)
            self.repository.fail(db,task_id,problem,cancelled=cancelled)
            if cancelled:self.on_skipped(db,task_id)
            else:self.on_failure(db,task_id)

    def _worker(self):
        """通知只是提示，数据库状态决定是否执行；每条通知都在finally归还队列计数。"""
        while not self.closed.is_set():
            try:task_id=self.work.get(timeout=.2)
            except queue.Empty:continue
            try:
                claimed=self._claim(task_id)
                if claimed:
                    task,token=claimed
                    with self.lock:self.current=task_id
                    state=self._execute(task_id,task,token)
                    self._settle(task_id,state,token)
            except Exception as exc:self._fail(task_id,exc)
            finally:
                with self.lock:
                    self.tokens.pop(task_id,None);self.current=None
                self.work.task_done()

    def close(self):
        """停止接取新通知，等待当前只读任务收尾；不声称恢复在途供应商调用。"""
        self.closed.set()
        if self.thread:self.thread.join(timeout=10)
