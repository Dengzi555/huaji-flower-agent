"""多日对比：实际取数基线、日历调休、缺口/零值、证据与展示一致性。"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ecommerce_agent.business_calendar import day_context,calendar_range
from ecommerce_agent.data import DataStore
from ecommerce_agent.flower_business_data import business_data,business_catalog
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.sales_trend import sales_trend,comparison_notes
from ecommerce_agent.workflows import execute_task
from ecommerce_agent.runner import model_tool_view


class CalendarAndTrendTests(unittest.TestCase):
    """使用冻结经营数据，不依赖模型对日期的记忆，也不发联网请求。"""
    def setUp(self):
        self.raw=business_data();self.store=DataStore.from_document(self.raw)
        self.catalog=ProductCatalog.from_document(business_catalog(),self.store.skus)
        self.sku='FLR-ROSE-99-BOUQUET'

    def parse(self,question,**kwargs):return parse_task(question,self.catalog,self.store,today=date(2026,10,28),**kwargs)

    def test_makeup_weekend_and_holiday_are_distinct(self):
        makeup=day_context('2026-10-10');rest=day_context('2026-10-11');holiday=day_context('2026-10-04')
        self.assertTrue(makeup['is_weekend']);self.assertTrue(makeup['is_workday']);self.assertTrue(makeup['is_makeup_workday'])
        self.assertFalse(rest['is_workday']);self.assertEqual(holiday['holiday_name'],'国庆节')
        self.assertFalse(holiday['is_workday']);self.assertEqual(day_context('2026-09-20')['day_type'],'makeup_workday')
        self.assertIn('非法定假日',day_context('2026-02-14')['occasions'][0])

    def test_unknown_year_stays_unknown(self):
        value=day_context('2027-10-01');self.assertFalse(value['schedule_known']);self.assertIsNone(value['is_workday'])
        self.assertIsNone(value['is_makeup_workday']);self.assertIsNone(value['holiday_name'])
        with self.assertRaises(ValueError):calendar_range('2026-01-01','2026-12-31')

    def test_recent_window_and_last_week_parse_without_model(self):
        task=self.parse('99红玫近7天销量走势')
        self.assertEqual(task['status'],'ready');self.assertEqual((task['trend_start'],task['trend_end']),('2026-10-21','2026-10-27'))
        self.assertEqual(task['intent'],'query_sales_performance');self.assertEqual(task['routing']['model_calls'],0)
        week=self.parse('99红玫上周销量怎么样')
        self.assertEqual((week['trend_start'],week['trend_end']),('2026-10-19','2026-10-25'))

    def test_explicit_window_and_invalid_dates(self):
        task=self.parse('99红玫2026-10-01至2026-10-15销量趋势');self.assertEqual(task['status'],'ready')
        self.assertEqual(task['trend_start'],'2026-10-01')
        self.assertEqual(self.parse('99红玫10月1日到10月5日销量趋势')['status'],'ready')
        for question in ('99红玫2026-10-28销量怎么样','99红玫2026-10-15至2026-10-01销量走势','99红玫12月1日到12月5日销量趋势'):
            self.assertNotEqual(self.parse(question)['status'],'ready')
        self.assertEqual(self.parse('99红玫近61天销量趋势')['status'],'unsupported')

    def test_primary_weekly_baseline_controls_actual_tool_reads(self):
        task=self.parse('99红玫2026-10-25对比上周同日销量下降原因')
        self.assertEqual(task['comparison_date'],'2026-10-18')
        with tempfile.TemporaryDirectory() as folder:
            state=execute_task(self.store,self.sku,task['target_date'],Path(folder),task_context=task)
            self.assertEqual(state['status'],'completed')
            self.assertEqual(state['analysis']['facts']['units_sold']['before'],self.store.records['sales',self.sku,'2026-10-18']['units_sold'])
            evidence=json.loads((Path(state['run_dir'])/'evidence.json').read_text(encoding='utf-8'))['observations']
            self.assertEqual(next(r for r in evidence if r['data']['table']=='sales')['data']['records'][0]['date'],'2026-10-18')
            self.assertIn('calendar',{r['data']['table'] for r in evidence})
            self.assertIn('sales_trend',{r['data']['table'] for r in evidence})

    def test_gaps_not_listed_and_zero_are_not_interchangeable(self):
        zero=sales_trend(self.store,'FLR-GRAPE-19-BOUQUET','2026-10-20','2026-10-25',listed_at='2026-09-15')
        self.assertEqual(zero['summary']['units_sum'],0);self.assertTrue(zero['summary']['complete_window'])
        new=sales_trend(self.store,'FLR-EUSTOMA-11-BOUQUET','2026-10-12','2026-10-25',listed_at='2026-10-25')
        self.assertEqual(new['coverage']['available_days'],1)
        self.assertIsNone(next(r for r in new['records'] if r['date']=='2026-10-24')['units_sold'])
        self.assertEqual(next(r for r in new['records'] if r['date']=='2026-10-24')['status'],'not_listed')
        self.store.records.pop(('sales',self.sku,'2026-10-22'))
        missing=sales_trend(self.store,self.sku,'2026-10-20','2026-10-25')
        self.assertIn('2026-10-22',missing['coverage']['missing_days'])

    def test_calendar_campaign_flags_do_not_claim_causality(self):
        trend=sales_trend(self.store,self.sku,'2026-10-01','2026-10-11','2026-10-10')
        primary=next(c for c in trend['comparisons'] if c['is_primary'])
        self.assertIn('工作/休息日类型不同',primary['context_flags'])
        self.assertTrue(any('不能' in note for note in comparison_notes(trend)))

    def test_long_model_context_is_bounded_without_mutating_evidence(self):
        trend=sales_trend(self.store,self.sku,'2026-09-15','2026-10-27')
        raw={'ok':True,'evidence_id':'trend-source','data':trend};count=len(trend['records'])
        view=model_tool_view(raw)
        self.assertLessEqual(len(view['data']['records']),14);self.assertGreater(view['data']['model_view']['omitted_rows'],0)
        self.assertEqual(len(raw['data']['records']),count)

    def test_finish_checks_calendar_fields_and_returns_actionable_feedback(self):
        """新工具字段可验收；错误调休判断被拒绝，并给出可修正的字段原因。"""
        from ecommerce_agent.tools import ToolRegistry,TABLES
        registry=ToolRegistry(self.store,self.sku,'2026-10-11',comparison_day='2026-10-10',
            trend_scope={'start':'2026-10-01','end':'2026-10-11','listed_at':'2026-09-15'})
        registry.execute('get_sales_trend',{},'trend-source');registry.execute('get_calendar_context',{},'calendar-source')
        refs=['trend-source','calendar-source']
        for i,name in enumerate(TABLES):
            identity=f'source-{i}';refs.append(identity);registry.execute(name,{},identity)
        registry.execute('analyze_metrics',{},'analysis')
        assertion={'evidence_id':'calendar-source','date':'2026-10-10','field':'day_type','equals':'weekend_rest'}
        rejected=registry.execute('finish',{'summary':'调休日需要按已核实日历判断','evidence_ids':refs,'assertions':[assertion]},'finish-bad')
        self.assertFalse(rejected['ok']);self.assertEqual(rejected['verification']['checks'][0]['reason'],'value_mismatch')
        corrected=registry.execute('finish',{'summary':'10月10日为调休工作日，日历差异只是背景，不是根因。','evidence_ids':refs,
            'assertions':[{**assertion,'equals':'makeup_workday'}]},'finish-good')
        self.assertTrue(corrected['ok'])
