"""直接验证模块边界：不启动网页服务也能准入、原子落盘和驱动队列。

使用隔离SQLite和可控执行函数，覆盖回滚、幂等、重复/无效通知、失败隔离与重启恢复。
"""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.task_admission import prepare_task,validate_request
from ecommerce_agent.task_repository import TaskRepository,ConflictError
from ecommerce_agent.task_execution import TaskExecutor
from ecommerce_agent.model_runtime import model_scope


class TaskModuleTests(unittest.TestCase):
    """模块通过数据和回调连接；测试无需构造InvestigationService或HTTP服务器。"""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=TaskRepository(self.root/'tasks.sqlite3')
        with self.repo.connection() as db:self.repo.initialize(db)
        self.store=DataStore.from_document(business_data())
        self.catalog=ProductCatalog.from_document(business_catalog(),self.store.skus)
        self.payload={'question':'炽热爱意10月25日销量怎么样'}

    def prepared(self,payload=None):
        with model_scope():return prepare_task(payload or self.payload,self.store,self.catalog,'version-one',today=date(2026,10,29))

    def insert(self,identity,prepared=None):
        with self.repo.connection() as db:
            self.repo.insert(db,identity,self.payload,prepared or self.prepared(),None,validate_request(self.payload))

    def executor(self,execute,**overrides):
        hooks={'can_start':lambda db,tid:True,'on_skipped':lambda db,tid:None,
            'on_result':lambda db,tid,state:None,'on_failure':lambda db,tid:None}
        hooks.update(overrides)
        executor=TaskExecutor(self.repo,self.root/'runs',execute=execute,default_store=lambda:self.store,
            settings=lambda:None,**hooks)
        self.addCleanup(executor.close);return executor

    def drained(self,executor):
        deadline=time.monotonic()+3
        while executor.work.unfinished_tasks and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(executor.work.unfinished_tasks,0)

    def test_admission_produces_snapshot_without_persisting_or_executing(self):
        ready=self.prepared()
        self.assertEqual(ready.status,'queued');self.assertEqual(ready.parsed['target_date'],'2026-10-25')
        self.assertEqual(ready.parsed['dataset_fingerprint'],'version-one')
        self.assertTrue(ready.document);self.assertEqual(self.repo.list_tasks(),[])
        missing=self.prepared({'question':'炽热爱意销量下降原因'})
        self.assertEqual(missing.status,'needs_clarification');self.assertEqual(self.repo.list_tasks(),[])

    def test_repository_rollback_removes_task_snapshot_and_event_together(self):
        with self.assertRaises(RuntimeError):
            with self.repo.connection() as db:
                self.repo.insert(db,'rolled-back',self.payload,self.prepared(),'key',validate_request(self.payload))
                self.repo.event(db,'rolled-back','task_created',{})
                raise RuntimeError('business hook failed')
        with self.repo.connection() as db:
            for table in ('tasks','task_datasets','task_events'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_concurrent_idempotency_reuses_one_task_and_rejects_different_request(self):
        prepared=self.prepared();digest=validate_request(self.payload);barrier=threading.Barrier(2)
        def submit(identity):
            barrier.wait(timeout=2)
            with self.repo.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                found=self.repo.replay(db,'same-key',digest)
                if found:return found
                self.repo.insert(db,identity,self.payload,prepared,'same-key',digest);return identity
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(submit,['one','two']))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(len(self.repo.list_tasks()),1)
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM task_datasets').fetchone()[0],1)
            with self.assertRaises(ConflictError):self.repo.replay(db,'same-key','different-hash')

    def test_executor_ignores_unknown_notification_and_survives_one_failed_run(self):
        calls=[]
        def execute(*args,**kwargs):
            calls.append(kwargs['task_context']['target_date'])
            if len(calls)==1:raise RuntimeError('vendor-secret-must-not-leak')
            return {'status':'completed','analysis':None}
        self.insert('first');self.insert('second')
        executor=self.executor(execute);executor.work.put_nowait('unknown')
        executor.enqueue('first');executor.enqueue('second');executor.start();self.drained(executor)
        with self.repo.connection() as db:
            failed=self.repo.fetch(db,'first');completed=self.repo.fetch(db,'second')
        self.assertEqual(failed['status'],'failed');self.assertEqual(completed['status'],'completed')
        self.assertNotIn('vendor-secret',failed['error']);self.assertEqual(len(calls),2)

    def test_cancelled_and_duplicate_notifications_never_execute(self):
        calls=[];skipped=[];self.insert('cancelled')
        executor=self.executor(lambda *a,**k:calls.append(True),on_skipped=lambda db,tid:skipped.append(tid))
        executor.enqueue('cancelled');executor.work.put_nowait('cancelled')
        self.assertTrue(executor.cancel('cancelled'));self.assertTrue(executor.cancel('cancelled'))
        executor.start();self.drained(executor)
        self.assertEqual(calls,[]);self.assertEqual(skipped,['cancelled'])
        with self.repo.connection() as db:self.assertEqual(self.repo.status(db,'cancelled'),'cancelled')

    def test_business_result_hook_failure_cannot_commit_half_a_completed_task(self):
        self.insert('hook-failure')
        def broken(db,tid,state):raise RuntimeError('settlement hook failed')
        executor=self.executor(lambda *a,**k:{'status':'completed'},on_result=broken)
        executor.enqueue('hook-failure');executor.start();self.drained(executor)
        with self.repo.connection() as db:
            row=self.repo.fetch(db,'hook-failure')
            events=[e['type'] for e in self.repo.events(db,'hook-failure')]
        self.assertEqual(row['status'],'failed');self.assertIsNone(row['result_json'])
        self.assertNotIn('task_settled',events);self.assertIn('task_failed',events)

    def test_recovery_distinguishes_cancelled_running_and_frozen_queued(self):
        for identity in ('cancelled','running','queued'):self.insert(identity)
        with self.repo.connection() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id IN ('cancelled','running')")
            self.repo.request_cancel(db,'cancelled','running')
        reopened=TaskRepository(self.root/'tasks.sqlite3')
        with reopened.connection() as db:
            pending=reopened.recover(db,'new-workspace-version')
            statuses={tid:reopened.status(db,tid) for tid in ('cancelled','running','queued')}
        self.assertEqual(pending,['queued'])  # 已冻结数据，工作区更新不改变旧任务。
        self.assertEqual(statuses,{'cancelled':'cancelled','running':'interrupted','queued':'queued'})


if __name__=='__main__':unittest.main()
