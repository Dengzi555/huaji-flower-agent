"""运行时治理：SQLite 并发参数、队列满拒绝与运行时快照。

这些行为不改变任务语义，只决定“服务繁忙或重启时，任务不会停在无人执行的中间态”。
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.model_runtime import model_scope
from ecommerce_agent.task_admission import prepare_task, validate_request
from ecommerce_agent.task_execution import TaskExecutor
from ecommerce_agent.task_parser import ProductCatalog
from ecommerce_agent.task_repository import TaskRepository
from ecommerce_agent.web import runtime_view


class RuntimeTests(unittest.TestCase):
    """用独立数据库和可控执行函数验证运行层，不启动网页服务。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = TaskRepository(self.root / 'tasks.sqlite3')
        with self.repo.connection() as db: self.repo.initialize(db)
        self.store = DataStore.from_document(business_data())
        self.catalog = ProductCatalog.from_document(business_catalog(), self.store.skus)
        self.payload = {'question': '炽热爱意10月25日销量怎么样'}

    def prepared(self, payload=None):
        with model_scope():
            return prepare_task(payload or self.payload, self.store, self.catalog, 'version-one', today=date(2026, 10, 29))

    def insert(self, task_id):
        with self.repo.connection() as db:
            self.repo.insert(db, task_id, self.payload, self.prepared(), None, validate_request(self.payload))

    def executor(self, execute=None, **overrides):
        hooks = {'can_start': lambda db, tid: True, 'on_skipped': lambda db, tid: None,
                 'on_result': lambda db, tid, state: None, 'on_failure': lambda db, tid: None}
        hooks.update(overrides)
        executor = TaskExecutor(self.repo, self.root / 'runs', execute=execute or (lambda *a, **k: {'status': 'completed'}),
                                default_store=lambda: self.store, settings=lambda: None, **hooks)
        self.addCleanup(executor.close); return executor

    def test_connection_enables_wal_and_bounded_write_wait(self):
        """网页读请求不能因为后台正在写而无限等待；写竞争最多等 5 秒后报错。"""
        with self.repo.connection() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_queue_full_marks_task_failed_instead_of_leaving_queued(self):
        """数据库已提交但通知入队失败时，任务必须显式失败，不能永远停在 queued。"""
        self.insert('full'); failures = []
        executor = self.executor(on_failure=lambda db, tid: failures.append(tid))
        while executor.work.qsize() < executor.work.maxsize:
            executor.work.put_nowait('padding')
        self.assertFalse(executor.enqueue('full'))
        with self.repo.connection() as db:
            row = self.repo.fetch(db, 'full')
            events = [event['type'] for event in self.repo.events(db, 'full')]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('队列已满', row['error'])
        self.assertEqual(json.loads(row['parsed_json'])['execution_error']['code'], 'queue_full')
        self.assertIn('task_failed', events)
        self.assertEqual(failures, ['full'])
        self.assertEqual(executor.stats()['registered_tokens'], 0)

    def test_stats_exposes_queue_and_worker_state_without_report_data(self):
        """运行时快照只含计数和队列指标，供健康检查与面板使用。"""
        executor = self.executor(); self.insert('waiting'); executor.enqueue('waiting')
        stats = executor.stats()
        self.assertEqual(stats['queue_depth'], 1)
        self.assertEqual(stats['queue_capacity'], 32)
        self.assertTrue(stats['accepting'])
        self.assertFalse(stats['worker_alive'])
        self.assertIsNone(stats['current_task'])
        self.assertNotIn('question', stats)

    def test_runtime_view_sums_stores_and_keeps_store_ids(self):
        """多店汇总只加计数，不把某一店的状态当成全局状态。"""
        class Store:
            def __init__(self, queued):
                self.queued = queued

            def runtime(self):
                return {'tasks': {'queued': self.queued, 'running': 0},
                        'executor': {'queue_depth': self.queued, 'worker_alive': True}}

        hub = type('Hub', (), {'services': {'a': Store(1), 'b': Store(2)}})()
        view = runtime_view(hub)
        self.assertEqual([store['store_id'] for store in view['stores']], ['a', 'b'])
        self.assertEqual(view['totals']['queued'], 3)
        self.assertEqual(view['totals']['queue_depth'], 3)
        self.assertEqual(view['totals']['failed'], 0)
        self.assertTrue(view['totals']['worker_alive'])


if __name__ == '__main__': unittest.main()