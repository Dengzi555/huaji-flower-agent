"""经营建议回归：建议须有来源、可核查、冲突优先，不把缺失或相关性包装成根因。"""
import tempfile
import unittest
from pathlib import Path
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_data
from ecommerce_agent.runner import run
from ecommerce_agent.operating_guidance import build_guidance


class OperatingGuidanceTests(unittest.TestCase):
    def investigate(self,sku):
        """真实确定性工作流写入临时产物，不修改正式经营数据。"""
        with tempfile.TemporaryDirectory() as temp:
            result=run(DataStore.from_document(sample_data()),sku,'2026-10-23',Path(temp),adaptive=True)
            report=(Path(result['run_dir'])/'report.md').read_text(encoding='utf-8')
        return result,report

    def test_stockout_has_facts_candidate_and_check_not_restock_command(self):
        state,report=self.investigate('FLR-ROSE-99-BOUQUET');guidance=state['analysis']['guidance']
        self.assertTrue(guidance['observed']);self.assertTrue(guidance['supported_signals'])
        self.assertEqual(guidance['next_checks'][0]['id'],'stockout_signal')
        self.assertIn('小时级',guidance['next_checks'][0]['required_data'])
        self.assertEqual(guidance['causal_status'],'not_established')
        for title in ['发生了什么','证据支持什么','还不能确定什么','建议先查什么']:self.assertIn(title,report)
        self.assertNotIn('立即补货',report)

    def test_conflict_suppresses_signals_and_prioritizes_reconciliation(self):
        state,_=self.investigate('FLR-SUNFLOWER-11-BUCKET');g=state['analysis']['guidance']
        self.assertEqual(g['status'],'data_conflict');self.assertEqual(g['supported_signals'],[])
        self.assertEqual(g['next_checks'][0]['id'],'reconcile_sources')

    def test_missing_traffic_cannot_be_claimed_zero(self):
        state,_=self.investigate('FLR-ROSE-33-BUCKET');g=state['analysis']['guidance']
        self.assertEqual(g['status'],'evidence_gap')
        self.assertFalse(any('商品访客：' in row['text'] for row in g['observed']))
        self.assertTrue(any(row['id']=='complete_traffic' for row in g['next_checks']))

    def test_completed_channel_check_is_not_requested_again(self):
        state,_=self.investigate('FLR-ROSE-19-BOUQUET');g=state['analysis']['guidance']
        check=next(row for row in g['next_checks'] if row['id']=='traffic_signal')
        self.assertIn('已查渠道',check['title']);self.assertIn('小时级',check['required_data'])

    def test_unobserved_signal_reference_is_not_supported(self):
        analysis={'hypotheses':[{'id':'traffic_signal','text':'无来源的原因','evidence_ids':['invented']}],
            'facts':{},'gaps':[],'conflicts':[]}
        self.assertEqual(build_guidance(analysis,{})['supported_signals'],[])

    def test_cancellation_is_not_claimed_to_explain_paid_units(self):
        analysis={'facts':{},'gaps':[],'conflicts':[],'order_funnel':{'evidence_id':'orders',
            'changes':{'cancellation_pp':3.2,'cancelled_orders':{'absolute':4}}}}
        observations={'order_cohorts':{'ok':True,'evidence_id':'orders'}}
        guidance=build_guidance(analysis,observations)
        self.assertIn('尚不能解释支付件数',guidance['supported_signals'][0]['text'])
        self.assertIn('观察时长',guidance['next_checks'][0]['reason'])

    def test_partial_lookup_does_not_publish_unknown_metric_as_fact(self):
        analysis={'kind':'lookup','data_quality':'partial','display_metrics':[{'label':'销量','value':'—'}],
            'evidence_ids':['source'],'gaps':[],'conflicts':[]}
        guidance=build_guidance(analysis,{'sales':{'ok':True,'evidence_id':'source'}})
        self.assertEqual(guidance['observed'],[]);self.assertEqual(guidance['status'],'evidence_gap')
        self.assertEqual(guidance['next_checks'][0]['id'],'complete_lookup')
