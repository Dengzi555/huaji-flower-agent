"""持续任务与记忆：验证Run不等于业务完成、事务幂等、期限/轮次边界与门店隔离。"""

import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ecommerce_agent.fixtures import sample_catalog,sample_data
from ecommerce_agent.missions import MissionConflict,MissionNotFound
from ecommerce_agent.task_parser import shanghai_today
from ecommerce_agent.web_service import InvestigationService


class MissionMemoryTests(unittest.TestCase):
    """临时双店服务使用独立数据库，真实查询流程验证，不发模型请求。"""

    def setUp(self):
        """固定业务数据与未来截止日期，所有写入仅在临时目录。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.data=self.root/"data.json";self.catalog=self.root/"products.json"
        self.data.write_text(json.dumps(sample_data()),encoding="utf-8")
        self.catalog.write_text(json.dumps(sample_catalog()),encoding="utf-8")
        self.service=InvestigationService(self.data,self.catalog,self.root/"state",self.root/"runs")
        self.other=InvestigationService(self.data,self.catalog,self.root/"other-state",self.root/"other-runs")
        self.addCleanup(lambda:self.service.close());self.addCleanup(self.other.close)
        self.deadline=(shanghai_today()+timedelta(days=7)).isoformat()

    def mission(self):
        """创建可人工核对的长期目标，不预先宣布销量恢复。"""
        return self.service.missions.create({"title":"跟进红玫销量","goal":"持续检查异常与补充材料","acceptance":"核对销量、库存及必要材料后人工验收","deadline":self.deadline})

    def run_mission(self,item,key="request01"):
        """显式开始一轮，等待底层任务终态后再读取Mission状态。"""
        result=self.service.missions.launch(item["id"],{"expected_revision":item["revision"],"question":"炽热爱意2026-10-23销量下降原因","request_id":key})
        task=self.wait(result["task"]["id"])
        return task,self.service.missions.get(item["id"])

    def wait(self,tid):
        """HTTP提交成功不等于运行完成，轮询数据库确认终态。"""
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            task=self.service.get(tid)
            if task["status"] not in ("queued","running"):return task
            time.sleep(.01)
        self.fail("调查未结束")

    def test_run_finishes_in_review_and_manual_acceptance_is_attestation(self):
        """Run成功仍待人工复核，完成记录不会冒充独立经营结果验证。"""
        task,item=self.run_mission(self.mission())
        self.assertEqual(task["status"],"completed")
        self.assertEqual(item["status"],"needs_review")
        self.assertEqual(task["parsed"]["mission_context"]["id"],item["id"])
        result=self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"complete","note":"仅完成教学核对；已核对来源与指标，不宣称实际恢复。"})
        self.assertEqual(result["status"],"completed")
        self.assertFalse(result["business_outcome_verified"])
        self.assertEqual(result["acceptance_kind"],"human_attested")

    def test_launch_idempotence_and_wrong_revision(self):
        """重放同一请求只保留一轮；新请求旧版本被拒绝。"""
        original=self.mission();task,item=self.run_mission(original)
        replay=self.service.missions.launch(original["id"],{"expected_revision":original["revision"],"question":"炽热爱意2026-10-23销量下降原因","request_id":"request01"})
        self.assertTrue(replay["reused"]);self.assertEqual(replay["task"]["id"],task["id"])
        with self.assertRaises(MissionConflict):self.service.missions.launch(original["id"],{"expected_revision":original["revision"],"question":"炽热爱意2026-10-23销量下降原因","request_id":"request02"})
        self.assertEqual(len(self.service.missions.get(item["id"])["runs"]),1)

    def test_four_runs_limit_and_missing_slots_rollback(self):
        """缺日期不消耗轮数，达到四轮不能无限循环。"""
        item=self.mission()
        with self.assertRaises(MissionConflict):self.service.missions.launch(item["id"],{"expected_revision":item["revision"],"question":"炽热爱意销量下降原因","request_id":"missing01"})
        self.assertEqual(self.service.list_tasks(),[])
        for index in range(4):_,item=self.run_mission(item,f"request0{index}")
        with self.assertRaises(MissionConflict):self.run_mission(item,"request05")

    def test_waiting_follow_up_deadline_and_cancelled(self):
        """补充信息可恢复跟进，过期和取消阻止续跑。"""
        item=self.mission()
        item=self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"wait_internal","note":"等待仓库解释缺货记录"})
        with self.assertRaises(MissionConflict):self.run_mission(item)
        item=self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"follow_up","note":"已拿到说明，下一轮重新核查事实"})
        with patch("ecommerce_agent.missions.shanghai_today",return_value=shanghai_today()+timedelta(days=8)):
            with self.assertRaises(MissionConflict):self.run_mission(item)
        item=self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"cancel","note":"目标调整，停止后续调查"})
        with self.assertRaises(MissionConflict):self.run_mission(item)

    def test_cross_store_mission_and_memory_sources_rejected(self):
        """相同SKU不代表相同店铺，另一店不能读取目标或引用本店来源。"""
        task,item=self.run_mission(self.mission())
        with self.assertRaises(MissionNotFound):self.other.missions.get(item["id"])
        with self.assertRaises(MissionNotFound):self.other.missions.launch(item["id"],{"expected_revision":item["revision"],"question":"炽热爱意2026-10-23销量下降原因","request_id":"request01"})
        with self.assertRaises(MissionNotFound):self.other.memory.create({"title":"阶段观察","kind":"observation","content":"待核对缺货信号","source":"本店报告","source_task_id":task["id"],"expires_on":self.deadline})
        self.assertEqual(self.other.list_tasks(),[])

    def test_legacy_memory_is_archived_and_new_tasks_use_settings_snapshot(self):
        """v0.10不再注入旧通用记忆，改为冻结明确门店档案；旧记录仍能追溯。"""
        memory=self.service.memory.create({"title":"汇报偏好","kind":"preference","content":"先列指标，再列待核对事项","source":"负责人录入"})
        self.assertEqual(self.other.memory.context(),[])
        task=self.service.create({"question":"炽热爱意2026-10-23销量下降原因"})["task"]
        self.assertEqual(task["parsed"]["store_memory_snapshot"],[])
        self.assertEqual(task["parsed"]["operating_settings_snapshot"]["revision"],0)
        self.service.memory.retract(memory["id"],{"expected_revision":1,"note":"偏好变更"})
        self.assertEqual(self.service.memory.context(),[])
        self.assertEqual(self.service.memory.list()[0]["content"],memory["content"])
        self.assertEqual(self.service.get(task["id"])["parsed"]["store_memory_snapshot"],[])
        self.wait(task["id"])

    def test_observation_requires_source_expiry_and_expires_on_read(self):
        """阶段结论不能无来源永久保存，过期背景不会进入新任务。"""
        payload={"title":"缺货观察","kind":"observation","content":"23日存在缺货相关信号，尚非根因","source":"来源待补"}
        with self.assertRaises(ValueError):self.service.memory.create(payload)
        task=self.wait(self.service.create({"question":"炽热爱意2026-10-23销量下降原因"})["task"]["id"])
        memory=self.service.memory.create({**payload,"source_task_id":task["id"],"expires_on":self.deadline})
        with patch("ecommerce_agent.missions.shanghai_today",return_value=shanghai_today()+timedelta(days=8)):
            self.assertEqual(self.service.memory.context(),[])
            self.assertTrue(self.service.memory.list()[0]["expired"])
        self.assertEqual(memory["verification_status"],"human_supplied_unverified")

    def test_restart_preserves_mission_and_memory(self):
        """持久化的是长期对象，关闭进程后可继续读取，而不是仅存在浏览器。"""
        _,item=self.run_mission(self.mission())
        self.service.memory.create({"title":"经营约束","kind":"policy","content":"仅做只读分析","source":"负责人确认"})
        self.service.close()
        self.service=InvestigationService(self.data,self.catalog,self.root/"state",self.root/"runs")
        self.assertEqual(self.service.missions.get(item["id"])["status"],"needs_review")
        self.assertEqual(len(self.service.memory.context()),1)

    def test_early_acceptance_and_missing_revision_rejected(self):
        """没有调查、版本或核验依据时，不能把长期目标标成完成。"""
        item=self.mission()
        with self.assertRaises(MissionConflict):self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"complete","note":"还没有调查"})
        with self.assertRaises(MissionConflict):self.service.missions.action(item["id"],{"action":"cancel","note":"缺版本不能覆盖"})

    def test_direction_is_carried_to_next_run_without_changing_old_request(self):
        """补充方向只影响下一轮；上一轮原问题与背景快照不被追溯改写。"""
        first,item=self.run_mission(self.mission())
        item=self.service.missions.action(item["id"],{"expected_revision":item["revision"],"action":"follow_up","note":"先核对缺货时间与订单是否重合"})
        second,item=self.run_mission(item,"direction02")
        self.assertEqual(second["parsed"]["mission_context"]["direction_notes"],["先核对缺货时间与订单是否重合"])
        self.assertEqual(self.service.get(first["id"])["parsed"]["mission_context"]["direction_notes"],[])

    def test_cancel_and_expired_queue_do_not_execute_on_restart(self):
        """排队期间取消/过期不会在重启后偷偷执行，在途结果也不能重开已取消目标。"""
        paused=InvestigationService(self.data,self.catalog,self.root/"queued-state",self.root/"queued-runs",start_worker=False)
        self.addCleanup(paused.close)
        payload={"title":"排队目标","goal":"核对指标","acceptance":"人工验收","deadline":self.deadline}
        item=paused.missions.create(payload)
        task=paused.missions.launch(item["id"],{"expected_revision":1,"question":"炽热爱意2026-10-23销量下降原因","request_id":"cancel01"})["task"]
        item=paused.missions.get(item["id"])
        paused.missions.action(item["id"],{"expected_revision":item["revision"],"action":"cancel","note":"未开始前取消"})
        self.assertEqual(paused.get(task["id"])["status"],"interrupted")
        with paused.connection() as db: paused.missions.settled(db,task["id"])
        self.assertEqual(paused.missions.get(item["id"])["status"],"cancelled")
        expired=paused.missions.create(payload)
        queued=paused.missions.launch(expired["id"],{"expected_revision":1,"question":"炽热爱意2026-10-23销量下降原因","request_id":"expire01"})["task"]
        with paused.connection() as db:db.execute("UPDATE missions SET deadline=? WHERE id=?",((shanghai_today()-timedelta(days=1)).isoformat(),expired["id"]))
        paused.close()
        restarted=InvestigationService(self.data,self.catalog,self.root/"queued-state",self.root/"queued-runs")
        self.addCleanup(restarted.close)
        deadline=time.monotonic()+5
        while restarted.get(queued["id"])["status"]=="queued" and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(restarted.get(queued["id"])["status"],"interrupted")
        self.assertIsNone(restarted.get(queued["id"])["result"])


if __name__=="__main__":unittest.main()
