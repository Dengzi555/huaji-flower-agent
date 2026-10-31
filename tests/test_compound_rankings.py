"""截图原话回归：中文时间窗、双端排名、共同可用日期与缺失不补零。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.workflows import execute_task

ROOT=Path(__file__).resolve().parents[1]


class CompoundRankingTests(unittest.TestCase):
    def setUp(self):
        self.store=DataStore(ROOT/'data/stores/hx-cq-na.json')
        self.catalog=ProductCatalog(ROOT/'data/products.json',self.store.skus)

    def parse(self,question,today=date(2026,10,29),**kwargs):
        return parse_task(question,self.catalog,self.store,today=today,**kwargs)

    def execute(self,task):
        with tempfile.TemporaryDirectory() as temp:
            return execute_task(self.store,task['sku_id'],task['target_date'],Path(temp),task_context=task)

    def test_screenshot_question_returns_both_rankings_with_coverage(self):
        task=self.parse('分析下近七天，哪款商品销量最好，哪款最差？')
        self.assertEqual(task['status'],'ready',task['issues'])
        self.assertEqual((task['trend_start'],task['trend_end']),('2026-10-22','2026-10-28'))
        self.assertEqual(task['query_plan']['ranking']['direction'],'both')
        result=self.execute(task);self.assertEqual(result['status'],'completed')
        self.assertEqual(result['validation']['status'],'passed')
        rank=result['analysis']['ranking'];self.assertEqual({p['direction'] for p in rank['partitions']},{'asc','desc'})
        self.assertEqual(rank['period_scope'],'shared_available_dates');self.assertEqual(len(rank['effective_dates']),6)
        self.assertFalse(rank['complete_scope']);self.assertNotIn('2026-10-28',rank['effective_dates'])
        for part in rank['partitions']:
            self.assertTrue(part['leaders'])
            counts=[r['units_sold'] for r in part['rows']]
            self.assertEqual(counts,sorted(counts,reverse=part['direction']=='desc'))

    def test_spoken_windows_share_one_parser(self):
        for words,days in [('近七天',7),('最近七日',7),('最近一个星期',7),('过去两周',14),('近二十一天',21)]:
            with self.subTest(words=words):
                task=self.parse(words+'全店销量排名')
                self.assertEqual(task['status'],'ready',task['issues'])
                self.assertEqual((date.fromisoformat(task['trend_end'])-date.fromisoformat(task['trend_start'])).days+1,days)
        for words in ['近一百天','最近','近半个月']:
            self.assertNotEqual(self.parse(words+'全店销量排名')['status'],'ready')

    def test_complete_window_dual_rank_and_zero_remains_valid(self):
        task=self.parse('最近三天全店最高和最低销量排名',today=date(2026,10,28))
        self.assertEqual(task['status'],'ready',task['issues'])
        rank=self.execute(task)['analysis']['ranking']
        self.assertEqual(rank['period_scope'],'requested_window');self.assertTrue(rank['complete_scope'])
        self.assertTrue(all(p['leaders'] for p in rank['partitions']))

    def test_model_cannot_drop_half_of_a_dual_request(self):
        from ecommerce_agent.query_plan import plan_from_model
        def planner(question,context):
            return {'decision':'select','selector':'__all__','start_date':'2026-10-22','end_date':'2026-10-28',
                'metric':'units_sold','direction':'desc','limit':5}
        with self.assertRaises(ValueError):plan_from_model('最近七天全店最好和最差',self.catalog,date(2026,10,29),planner)
