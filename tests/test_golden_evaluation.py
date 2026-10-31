"""评测集本身也需要验证：防例句泄漏、跳过冒充通过以及缺证据误打高分。"""
import json
import tempfile
import unittest
from pathlib import Path
from ecommerce_agent.golden_evaluation import lookup,MISSING,check_paths,load_suite,summarize,oracle_checks

ROOT=Path(__file__).resolve().parents[1]


class GoldenEvaluationTests(unittest.TestCase):
    def test_missing_is_not_expected_none(self):
        self.assertIs(lookup({},'sku'),MISSING)
        self.assertFalse(check_paths({}, {'sku':None})['sku'])
        self.assertTrue(check_paths({'sku':None},{'sku':None})['sku'])

    def test_dataset_has_80_cases_and_no_exact_index_overlap(self):
        document,audit=load_suite(ROOT/'data/evaluation/golden_cases.json',ROOT/'data/intent_examples.json')
        self.assertEqual(len(document['cases']),80)
        self.assertEqual(sum(c['execution']=='model_allowed' for c in document['cases']),12)
        self.assertEqual(sum(c['execution']=='conversation_state' for c in document['cases']),8)
        self.assertEqual(audit['exact_duplicates'],0)

    def test_index_duplicate_and_family_split_are_rejected(self):
        case={'id':'one','split':'regression','family':'same','execution':'offline','question':'测试问题'}
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);data=folder/'suite.json';index=folder/'examples.json'
            data.write_text(json.dumps({'schema':'flower-golden-v1','cases':[case]}),encoding='utf-8')
            index.write_text(json.dumps({'examples':[{'id':'example','text':'测试问题'}]}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'重复'):load_suite(data,index)
            data.write_text(json.dumps({'schema':'flower-golden-v1','cases':[case,{**case,'id':'two','split':'holdout'}]}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'同族'):load_suite(data,index)

    def test_skips_are_not_counted_as_passes_and_intent_errors_are_visible(self):
        rows=[{'category':'意图','grade':{'passed':False},'expected_status':'ready','expected_intent':'query_inventory',
                'parsed':{'intent':'query_promotions','status':'needs_clarification'}}]
        report=summarize(rows,[{'case_id':'not-run'}])
        self.assertEqual(report['executed'],1)
        self.assertEqual(report['passed'],0)
        self.assertEqual(report['skipped'],1)
        self.assertEqual(report['unnecessary_clarifications'],1)
        self.assertEqual(report['per_intent']['query_inventory']['fn'],1)

    def test_sales_only_evidence_cannot_pass_stockout_oracle(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            (directory/'evidence.json').write_text(json.dumps({'observations':[{'data':{'table':'sales','records':[{'date':'2026-10-25','units_sold':23}]}}]}),encoding='utf-8')
            oracle={'id':'stockout','target_date':'2026-10-25','baseline_date':'2026-10-24',
                'facts':{'target':{'sales':{'date':'2026-10-25','units_sold':23}}}}
            checks=oracle_checks({},oracle,{'status':'completed','run_dir':str(directory),'analysis':{}},{})
            self.assertTrue(checks['oracle.sales_present'])
            self.assertFalse(checks['oracle.required_inventory'])


if __name__=='__main__':unittest.main()
