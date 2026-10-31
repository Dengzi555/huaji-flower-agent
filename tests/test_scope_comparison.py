"""局部条件与跨店比较回归：不改变未修改字段，不掩盖缺口，不触发付费模型。"""
import json
import unittest
import test_conversations as fixtures
from ecommerce_agent.web_service import ConflictError


class ScopeComparisonTests(unittest.TestCase):
    """复用临时多店工作区；所有业务调用均为离线、独立冻结的任务。"""
    setUp=fixtures.ConversationTests.setUp
    turn=fixtures.ConversationTests.turn

    def settle(self,job):
        for child in job['children']:
            service=self.hub.for_store(child['store_id']);task=service.get(child['task_id'])
            if task['status']!='queued':continue
            parsed=task['parsed'];state=service.runner(service.store,parsed['sku_id'],parsed['target_date'],service.output,task_context=parsed)
            with service.connection() as db:db.execute('UPDATE tasks SET status=?,result_json=? WHERE id=?',(state['status'],json.dumps(state),task['id']))
        return self.manager.get(self.doc['id'])['comparison']

    def test_count_patch_keeps_variety_type_and_history(self):
        old=self.turn('19红玫2026-10-25销量怎么样')['task']
        new=self.turn('那99朵的呢')['task']
        self.assertEqual(new['parsed']['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertEqual(new['parsed']['conversation_context']['local_patch']['constraints']['product_type'],'花束')
        self.assertEqual(self.hub.for_store('hx-sz-ns').get(old['id'])['parsed']['sku_id'],'FLR-ROSE-19-BOUQUET')

    def test_no_exact_type_requires_confirmation(self):
        self.turn('99红玫2026-10-25销量怎么样')
        before=len(self.hub.for_store('hx-sz-ns').list_tasks())
        pending=self.turn('那33朵的呢')
        self.assertIsNone(pending['task']);self.assertEqual(len(self.hub.for_store('hx-sz-ns').list_tasks()),before)
        patch=self.doc['pending_product_patch'];self.assertFalse(patch['exact'])
        self.assertEqual(patch['constraints'],{'stem_count':33,'variety':'红玫瑰','product_type':'花束'})
        self.assertEqual([p['sku_id'] for p in patch['candidates']],['FLR-ROSE-33-BUCKET'])
        with self.assertRaises(ValueError):self.turn('那33朵的呢',sku='FLR-WUMEI-33-BUCKET')
        confirmed=self.turn('那33朵的呢',sku='FLR-ROSE-33-BUCKET')['task']
        self.assertEqual(confirmed['status'],'queued')

    def test_same_variety_words_still_keep_type(self):
        self.turn('99红玫2026-10-25销量怎么样')
        self.assertIsNone(self.turn('那33朵红玫呢')['task'])
        self.assertEqual(self.doc['pending_product_patch']['constraints']['product_type'],'花束')

    def test_baseline_patch_does_not_move_target_and_is_inherited(self):
        first=self.turn('19红玫2026-10-25销量怎么样')['task']
        changed=self.turn('换成上周同一天')['task']
        self.assertEqual(changed['parsed']['target_date'],'2026-10-25')
        self.assertEqual(changed['parsed']['comparison_date'],'2026-10-18')
        later=self.turn('那99朵的呢')['task']
        self.assertEqual(later['parsed']['comparison_date'],'2026-10-18')
        self.assertEqual(self.hub.for_store('hx-sz-ns').get(first['id'])['parsed']['comparison_date'],'2026-10-24')

    def test_comparison_tasks_are_store_bound_and_same_scope(self):
        self.turn('19红玫2026-10-25销量怎么样');self.turn('换成上周同一天')
        result=self.turn('南山店和南岸店也这样吗');job=result['conversation']['comparison']
        self.assertEqual(len(job['children']),2)
        for child in job['children']:
            task=self.hub.for_store(child['store_id']).get(child['task_id'])
            self.assertIsNone(task['parent_id']);self.assertNotIn('model_execution',task['parsed'])
            self.assertEqual(task['parsed']['comparison_date'],'2026-10-18')
            self.assertEqual(task['parsed']['sku_id'],'FLR-ROSE-19-BOUQUET')
        report=self.settle(job)
        self.assertFalse(report['pending']);self.assertEqual(report['comparable_count'],2)
        for row in report['rows']:self.assertTrue(row['evidence_ids'])

    def test_comparison_missing_baseline_is_not_zero(self):
        self.turn('19红玫2026-10-25销量怎么样')
        other=self.hub.for_store('hx-cq-na')
        other.store.records['sales','FLR-ROSE-19-BOUQUET','2026-10-24']['status']='unavailable'
        job=self.turn('南山店和南岸店对比')['conversation']['comparison'];report=self.settle(job)
        row=next(r for r in report['rows'] if r['store_id']=='hx-cq-na')
        self.assertFalse(row['comparable']);self.assertIsNone(row['metrics']['baseline_units'])
        self.assertEqual(report['comparable_count'],1)

    def test_other_stores_idempotent_and_no_scope_no_fanout(self):
        missing=self.turn('其他店也这样吗');self.assertIsNone(missing['task']);self.assertNotIn('comparison',missing['conversation'])
        self.turn('19红玫2026-10-25销量怎么样')
        payload={'question':'其他店也这样吗','revision':self.doc['revision']}
        first=self.manager.turn(self.doc['id'],payload,'compare-once');second=self.manager.turn(self.doc['id'],payload,'compare-once')
        self.assertEqual(len(first['conversation']['comparison']['children']),6)
        self.assertEqual(first['conversation']['comparison']['id'],second['conversation']['comparison']['id'])
        with self.assertRaises(ConflictError):self.manager.turn(self.doc['id'],payload,'compare-stale')

    def test_comparison_failed_store_and_child_navigation(self):
        self.turn('19红玫2026-10-25销量怎么样')
        job=self.turn('南山店和南岸店对比')['conversation']['comparison'];child=job['children'][1]
        with self.hub.for_store(child['store_id']).connection() as db:db.execute("UPDATE tasks SET status='failed' WHERE id=?",(child['task_id'],))
        report=self.settle(job);self.assertEqual(report['comparable_count'],1)
        selected=self.manager.focus(self.doc['id'],{'store_id':child['store_id'],'task_id':child['task_id'],'revision':self.doc['revision']})
        self.assertEqual(selected['conversation']['focus_store'],child['store_id'])

    def test_comparison_write_guard_does_not_create_children(self):
        self.turn('19红玫2026-10-25销量怎么样')
        result=self.turn('其他店也调整预算')
        self.assertNotIn('comparison',result['conversation'])

    def test_color_and_type_patch_keep_other_attributes(self):
        self.turn('19红玫2026-10-25销量怎么样')
        white=self.turn('换成白色')['task']
        self.assertEqual(white['parsed']['sku_id'],'FLR-ANGEL-19-BOUQUET')
        self.assertEqual(white['parsed']['target_date'],'2026-10-25')
        self.turn('19红玫2026-10-25销量怎么样')
        pending=self.turn('换成抱抱桶')
        self.assertIsNone(pending['task'])
        self.assertEqual(self.doc['pending_product_patch']['constraints'],{'stem_count':19,'variety':'红玫瑰','product_type':'抱抱桶'})

    def test_zero_sales_are_comparable_not_missing(self):
        self.turn('19红玫2026-10-25销量怎么样')
        other=self.hub.for_store('hx-cq-na')
        row=other.store.records['sales','FLR-ROSE-19-BOUQUET','2026-10-25']
        row.update(units_sold=0,paid_orders=0,paid_buyers=0,paid_revenue=0)
        report=self.settle(self.turn('南山店和南岸店对比')['conversation']['comparison'])
        zero=next(r for r in report['rows'] if r['store_id']=='hx-cq-na')
        self.assertTrue(zero['comparable']);self.assertEqual(zero['metrics']['units'],0)
        self.assertEqual(zero['metrics']['change_pct'],-100)

    def test_window_comparison_preserves_dates_and_marks_gaps(self):
        self.turn('19红玫2026-10-20至2026-10-25销量怎么样')
        self.turn('换成上周同一天')
        other=self.hub.for_store('hx-cq-na')
        other.store.records['sales','FLR-ROSE-19-BOUQUET','2026-10-21']['status']='unavailable'
        job=self.turn('南山店和南岸店比较')['conversation']['comparison']
        report=self.settle(job)
        self.assertEqual(job['scope']['time']['trend_start'],'2026-10-20')
        for child in job['children']:self.assertEqual(self.hub.for_store(child['store_id']).get(child['task_id'])['parsed']['comparison_date'],'2026-10-18')
        gap=next(r for r in report['rows'] if r['store_id']=='hx-cq-na')
        self.assertFalse(gap['comparable']);self.assertIn('window_units',gap['metrics'])

    def test_followup_comparison_keeps_the_same_two_stores(self):
        self.turn('19红玫2026-10-25销量怎么样')
        self.turn('南山店和南岸店比较')
        job=self.turn('两店库存呢')['conversation']['comparison']
        self.assertEqual(len(job['children']),2);self.assertEqual(job['scope']['intent'],'query_inventory')
        anchor=next(c for c in job['children'] if c['store_id']==job['anchor_store'])
        patched=self.turn('那99朵的呢')['task']
        self.assertEqual(patched['parsed']['intent'],'query_inventory')
        self.assertEqual(patched['parent_id'],anchor['task_id'])
