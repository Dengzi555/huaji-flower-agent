"""失败案例回归：穿过真实准入/finish出口，不用网络随机性代替可重复反馈。"""
import unittest
import json
import tempfile
from datetime import date
from ecommerce_agent.annual_data import annual_catalog,daily_document
from ecommerce_agent.store_workspace import STORES
from ecommerce_agent.data import DataStore,FIELDS
from ecommerce_agent.task_parser import ProductCatalog,parse_task
from ecommerce_agent.tools import ToolRegistry,TABLES


class AliasNarrativeRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document=annual_catalog()
        documents=[daily_document(cls.document['products'],STORES[0],0,date(2026,10,d),'2026-10-28',20261029)[0] for d in (18,24,25)]
        merged=dict(documents[-1])
        for field in FIELDS:merged[field]=[r for doc in documents for r in doc.get(field,[])]
        cls.store=DataStore.from_document(merged)
        cls.catalog=ProductCatalog.from_document({**cls.document,'products':[p for p in cls.document['products'] if p['sku_id'] in cls.store.skus]},cls.store.skus)

    def parse(self,question,planner,policy='hybrid',**kwargs):
        return parse_task(question,self.catalog,self.store,today=date(2026,10,29),query_planner=planner,
            planning_policy=policy,intent_retriever=lambda q:{'status':'fixture','candidates':[]},**kwargs)

    def test_ambiguous_stem_alias_preserves_date_without_model_guess(self):
        def forbidden(*args):raise AssertionError('已确认的商品歧义不能交模型随意挑款')
        for policy in ('hybrid','direct_model'):
            task=self.parse('33乌梅10月25日卖了多少',forbidden,policy)
            self.assertEqual(task['status'],'needs_clarification')
            self.assertEqual(task['target_date'],'2026-10-25')
            self.assertGreater(len(task['candidates']),1)
            self.assertEqual(task['understanding']['planning_calls'],0)
            self.assertFalse(any('补充日期' in x or '补充目标日期' in x for x in task['issues']))

    def test_direct_planner_can_keep_exact_matched_scope(self):
        captured=[]
        def planner(q,ctx):
            captured.append(ctx)
            return dict(decision='select',intent='query_sales_performance',selector='__matched__',
                start_date='2026-10-25',end_date='2026-10-25',direction='none',limit=5,metric='units_sold')
        task=self.parse('99红玫10月25日卖了多少',planner,'direct_model')
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['sku_id'],'FLR-ROSE-99-BOUQUET')
        self.assertEqual(captured[0]['resolved_scope']['sku_ids'],['FLR-ROSE-99-BOUQUET'])

    def test_explicit_candidate_answer_resolves_only_product(self):
        candidates=self.catalog.resolve('33乌梅')['candidates']
        task=self.parse('33乌梅10月25日卖了多少',None,sku_override=candidates[0]['sku_id'])
        self.assertEqual(task['status'],'ready');self.assertEqual(task['target_date'],'2026-10-25')

    def test_matched_category_preserves_all_products_and_rejects_narrowing(self):
        def proposal(selector):
            return dict(decision='select',intent='query_sales_performance',selector=selector,
                start_date='2026-10-25',end_date='2026-10-25',direction='none',limit=5,metric='units_sold')
        question='红玫瑰10月25日卖了多少'
        task=self.parse(question,lambda *a:proposal('__matched__'),'direct_model')
        self.assertEqual(task['status'],'ready')
        expected={p['sku_id'] for p in self.catalog.resolve(question)['candidates']}
        self.assertEqual(set(task['product_scope']['sku_ids']),expected)
        rejected=self.parse(question,lambda *a:proposal(sorted(expected)[0]),'direct_model')
        self.assertEqual(rejected['status'],'failed')
        self.assertFalse(rejected['error_info']['data_queried'])

    def test_model_clarify_never_asks_again_for_known_product_and_date(self):
        task=self.parse('99红玫10月25日卖了多少',lambda *a:dict(decision='clarify',selector='__matched__',
            start_date='2026-10-25',end_date='2026-10-25',direction='none',limit=5,metric='units_sold'),'direct_model')
        self.assertEqual(task['status'],'needs_clarification')
        self.assertEqual(task['target_date'],'2026-10-25')
        self.assertFalse(any('补充查询商品或时间' in x for x in task['issues']))

    def registry(self):
        registry=ToolRegistry(self.store,'FLR-ROSE-99-BOUQUET','2026-10-25',comparison_day='2026-10-24',allow_orders=True,
            trend_scope={'start':'2026-10-18','end':'2026-10-25'})
        for i,name in enumerate([*TABLES,'get_sales_trend','get_order_funnel','analyze_metrics']):
            self.assertTrue(registry.execute(name,{},'source-'+str(i))['ok'])
        return registry

    def finish(self,text,registry=None):
        registry=registry or self.registry()
        refs=[o['evidence_id'] for o in registry.observations.values()]
        return registry,registry.execute('finish',{'summary':text,'evidence_ids':refs},'finish-test')

    def test_completed_must_not_include_late_cancellations(self):
        registry,result=self.finish('完成54单（含迟到取消2单），需要核查。')
        self.assertFalse(result['ok']);self.assertIsNone(registry.finished)
        self.assertIn('completed_includes_cancelled',str(result))

    def test_one_week_is_not_two_weeks(self):
        _,result=self.finish('目标日10-25，对上上周同日10-18的销量需要核查。')
        self.assertFalse(result['ok']);self.assertIn('relative_week_mismatch',str(result))

    def test_no_stockout_cannot_exclude_stockout_causally(self):
        _,result=self.finish('缺货0分钟，非缺货所致。')
        self.assertFalse(result['ok']);self.assertIn('unsupported_causality',str(result))

    def test_candidate_signal_cannot_be_promoted_to_main_cause(self):
        _,result=self.finish('主因更可能不是流量，而是成品可售中断。')
        self.assertFalse(result['ok']);self.assertIn('unsupported_causality',str(result))

    def test_classification_reason_must_come_from_host(self):
        registry=self.registry();label=registry.analysis['assessment']['label']
        _,result=self.finish('销量分级='+label+'（主要因金额已核）。',registry)
        self.assertFalse(result['ok']);self.assertIn('assessment_reason_missing',str(result))

    def test_no_conflict_cannot_manufacture_cancel_inventory_conflict(self):
        _,result=self.finish('证据冲突：取消升高与商家缺货标签为0、库存零缺货并存。')
        self.assertFalse(result['ok']);self.assertIn('unsupported_conflict',str(result))

    def test_campaign_status_is_not_paid_ad_status(self):
        _,result=self.finish('推广：两日均active=false，因此没有投放。')
        self.assertFalse(result['ok']);self.assertIn('source_domain_confusion',str(result))

    def test_stockout_field_cannot_be_attributed_to_raw_sales(self):
        _,result=self.finish('销售记录stockout字段为1440，应核查。')
        self.assertFalse(result['ok']);self.assertIn('source_domain_confusion',str(result))

    def test_missing_ad_data_is_not_claimed_as_no_campaign(self):
        _,result=self.finish('两日促销活动active=false；没有广告投放证据，无法判断推广是否开启。缺货分钟来自库存记录。')
        self.assertTrue(result['ok'],result)

    def test_cautious_wording_and_actual_assessment_reason_are_accepted(self):
        registry=self.registry();assessment=registry.analysis['assessment']
        text='取消包含跨日取消，不属于完成订单。10-18距目标日10-25为7天。当前不能排除其他因素，尚不能认定缺货为主因。库存与取消不存在直接冲突。'
        text+='销量分级：'+assessment['label']+'；'+assessment['reasons'][-1]
        _,result=self.finish(text,registry)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['narrative_audit']['status'],'limited_checks_passed')

    def test_finish_enforces_schema_length_in_runtime(self):
        _,result=self.finish('花'*1201)
        self.assertFalse(result['ok'])

    def test_cancel_rate_clause_does_not_mean_completed_includes_cancelled(self):
        _,result=self.finish('支付20单中取消12单、完成8单，取消率60%，其中迟到取消4单。库存零缺货与取消同时存在也不矛盾。')
        self.assertTrue(result['ok'],result)

    def test_model_contract_view_is_bounded_and_keeps_host_evidence(self):
        from ecommerce_agent.runner import model_tool_view
        from ecommerce_agent.narrative_contract import build_contract
        from datetime import timedelta
        registry=self.registry()
        observation={'ok':True,'tool':'analyze_metrics','data':registry.analysis}
        contract=build_contract(registry.analysis,registry.observations)
        contract['date_relations']=[{'date':(date(2026,10,25)-timedelta(days=d)).isoformat(),'days_before_target':d} for d in range(1,61)]
        observation['data']['narrative_contract']=contract
        compact=model_tool_view(observation)
        self.assertEqual(len(compact['data']['narrative_contract']['date_relations']),7)
        self.assertEqual(len(observation['data']['narrative_contract']['date_relations']),60)
        self.assertEqual(compact['data']['narrative_contract']['omitted_date_relations'],53)

    def test_rejected_draft_can_recover_but_cannot_bypass_step_budget(self):
        from ecommerce_agent.runner import run
        class ModelFixture:
            def __init__(self):self.index=0
            def complete(self,messages,tools,timeout):
                steps=[(name,{}) for name in TABLES]+[('analyze_metrics',{}),
                    ('finish',{'summary':'完成54单（含迟到取消2单）。','evidence_ids':['call-'+str(i) for i in range(1,5)]}),
                    ('finish',{'summary':'取消订单包含迟到取消；完成订单与取消订单互斥，目前尚不能认定根因。','evidence_ids':['call-'+str(i) for i in range(1,5)]})]
                name,args=steps[self.index];self.index+=1
                if self.index==7:
                    receipt=json.loads(messages[-1]['content'])
                    assert not receipt['ok'] and receipt['narrative_audit']['status']=='rejected'
                return {'role':'assistant','tool_calls':[{'id':'call-'+str(self.index),'type':'function',
                    'function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]}
        with tempfile.TemporaryDirectory() as folder:
            for budget in (6,7):
                state=run(self.store,'FLR-ROSE-99-BOUQUET','2026-10-25',folder,provider=ModelFixture(),max_steps=budget)
                if budget==6:
                    self.assertEqual(state['stop_reason'],'step_budget');self.assertIsNone(state['model_draft'])
                else:
                    self.assertEqual(state['status'],'completed')
                    self.assertEqual(state['model_draft']['narrative_audit']['status'],'limited_checks_passed')


if __name__=='__main__':unittest.main()
