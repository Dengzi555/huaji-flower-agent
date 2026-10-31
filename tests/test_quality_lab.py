"""评测设施的反例测试：错误必须扣分，失败草案不能自动改变生产提示词。"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ecommerce_agent.quality_lab import grade_case, reflection_proposals, run_suite
from ecommerce_agent.claw_eval_adapter import export_and_grade, replay_claw

ROOT=Path(__file__).resolve().parents[1]
CLAW=ROOT/'vendor/claw-eval'
try:
    import pydantic  # noqa: F401
    import yaml  # noqa: F401
    CLAW_READY=(CLAW/'src/claw_eval/models/task.py').exists()
except ImportError:
    # 核心运行时零第三方依赖；只有外部评测适配层需要 pydantic/pyyaml。
    CLAW_READY=False
CLAW_SKIP='需要仓库内Claw-Eval快照与 pydantic/pyyaml'


class QualityLabTests(unittest.TestCase):
    """核对失败检测和输出边界，不调用外部模型。"""

    def test_wrong_date_fails_even_when_status_matches(self):
        case={'status':'needs_clarification','expect':{'target_date':'2026-09-30'}}
        result=grade_case(case,{'status':'needs_clarification','target_date':'2026-10-29'},None)
        self.assertFalse(result['passed'])
        self.assertIn('target_date',result['failed_checks'])

    def test_reflection_is_reviewable_proposal(self):
        rows=[{'case_id':'bad-date','grade':{'failed_checks':['target_date']}}]
        before=copy.deepcopy(rows)
        proposal=reflection_proposals(rows)[0]
        self.assertEqual(proposal['status'],'proposal_only')
        self.assertTrue(proposal['requires_review'])
        self.assertEqual(rows,before)

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);dataset=directory/'cases.json'
            dataset.write_text(json.dumps({'cases':[{'id':'../escape'}]}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'案例ID'):
                run_suite(ROOT,dataset,directory/'out')

    @unittest.skipUnless(CLAW_READY,CLAW_SKIP)
    def test_upstream_core_rejects_failed_host_case_and_records_model_usage(self):
        """真上游reader/评分集成：单轮失败不能被其余成功试验掩盖。"""
        parsed={'raw_question':'测试日期','business_today':'2026-10-29','issues':['需要确认'],
                'query_plan':{'planning':{'model':'test-planner','usage':{'prompt_tokens':12,'completion_tokens':3}}}}
        def row(trial,passed):
            return {'case_id':'negative-case','trial':trial,'parsed':parsed,'run_dir':None,
                    'elapsed_ms':20,'model_calls':1,
                    'grade':{'passed':passed,'checks':{'date':passed},'failed_checks':[] if passed else ['date']}}
        report={'rows':[row(0,True),row(1,False),row(2,True)],'runner':'live_planning_deterministic_tools',
                'trials':3,'model_calls':3,'security_gate_passed':True,'source_sha256':'source','dataset_sha256':'dataset'}
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);path=directory/'report.json'
            path.write_text(json.dumps(report),encoding='utf-8')
            result=export_and_grade(path,CLAW,directory/'claw')
            self.assertEqual(result['observed_all_trials_passed'],0)
            self.assertFalse(result['results'][1]['passed'])
            trace=[json.loads(line) for line in (directory/'claw/negative-case-1.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(trace[0]['model'],'test-planner')
            self.assertEqual(trace[-1]['total_tokens'],15)
            self.assertEqual(trace[-1]['failure_modes'],['date'])

    @unittest.skipUnless(CLAW_READY,CLAW_SKIP)
    def test_claw_record_then_replay_matches_without_source_report(self):
        """录制冻结轨迹后，回放只依赖快照和轨迹；分数必须逐项一致。"""
        parsed={'raw_question':'测试回放','business_today':'2026-10-29','issues':['需要确认']}
        def row(trial,passed):
            return {'case_id':'replay-case','trial':trial,'parsed':parsed,'run_dir':None,'elapsed_ms':10,
                    'model_calls':0,'grade':{'passed':passed,'checks':{'date':passed},'failed_checks':[] if passed else ['date']}}
        report={'rows':[row(0,True),row(1,False),row(2,True)],'runner':'deterministic_workflow','trials':3,
                'model_calls':0,'security_gate_passed':True,'source_sha256':'source','dataset_sha256':'dataset'}
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);path=directory/'report.json'
            path.write_text(json.dumps(report),encoding='utf-8')
            recorded=export_and_grade(path,CLAW,directory/'claw')
            self.assertEqual(recorded['observed_all_trials_passed'],0)
            self.assertFalse(Path(recorded['results'][0]['trace']).is_absolute())
            replay=replay_claw(directory/'claw',CLAW,directory/'replay')
            self.assertTrue(replay['matched'],replay['mismatches'])
            self.assertEqual(replay['checked'],3)
            self.assertEqual(len(replay['upstream']['commit']),40)
            manifest=json.loads((directory/'claw/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['schema'],'claw-recording-v1')
            self.assertEqual(manifest['source_report_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            manifest['upstream']['commit']='0'*40
            (directory/'claw/manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            drifted=replay_claw(directory/'claw',CLAW)
            self.assertFalse(drifted['matched'])
            self.assertIn('upstream_commit_mismatch',[item['reason'] for item in drifted['mismatches']])

    def test_vendor_snapshot_blob_hashes_match(self):
        """固定快照逐文件比对上游 git blob SHA，防止本地改动或换版本后仍冒充同一 commit。"""
        snapshot=json.loads((CLAW/'SOURCE_SNAPSHOT.json').read_text(encoding='utf-8'))
        self.assertEqual(len(snapshot['upstream_commit']),40)
        for item in snapshot['files']:
            data=(CLAW/item['path']).read_bytes()
            blob=hashlib.sha1(b'blob %d\0'%len(data)+data).hexdigest()
            self.assertEqual(blob,item['git_blob_sha'],item['path'])

    @unittest.skipUnless(CLAW_READY,CLAW_SKIP)
    def test_replay_frozen_fixture_without_report_or_model(self):
        """仓库内冻结轨迹可直接回放；CI 不需要模型、网络或原始报告。"""
        replay=replay_claw(ROOT/'tests/fixtures/claw-replay',CLAW)
        self.assertTrue(replay['matched'],replay['mismatches'])
        self.assertEqual(replay['checked'],3)
        self.assertEqual(replay['recorded_cases'],1)
        self.assertEqual(replay['recorded_trials'],3)
        self.assertTrue(replay['security_gate_passed'])


if __name__=='__main__':unittest.main()
