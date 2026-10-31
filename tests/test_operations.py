"""运营闭环回归：来源建单、版本化处理、后续数据复查和门店档案范围。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from ecommerce_agent.advertising import AdvertisingBook
from ecommerce_agent.fixtures import sample_data,sample_catalog
from ecommerce_agent.flower_business_data import business_data
from ecommerce_agent.operations import OperatingConflict,OperatingNotFound
from ecommerce_agent.store_workspace import generate_advertising
from ecommerce_agent.web_service import InvestigationService


class OperatingTests(unittest.TestCase):
    """临时双店、真实只读执行器；金额/计数恢复条件由确定数据验证。"""

    def setUp(self):
        """保留23日下降，增加24日恢复的独立记录，不篡改已完成任务快照。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        raw=sample_data();sku="FLR-ROSE-99-BOUQUET"
        for table in ("sales","traffic","inventory","promotions"):
            previous=next(r for r in raw[table] if r["sku_id"]==sku and r["date"]=="2026-10-22")
            row={**previous,"date":"2026-10-24","observed_at":"2026-10-25T08:00:00+08:00"}
            if table=="sales":row.update(units_sold=120,paid_orders=117,paid_buyers=115,paid_revenue=45871.23)
            if table=="traffic":row["visitors"]=1367
            raw[table].append(row)
        data=self.root/"data.json";catalog=self.root/"products.json"
        data.write_text(json.dumps(raw),encoding="utf-8");catalog.write_text(json.dumps(sample_catalog()),encoding="utf-8")
        self.service=InvestigationService(data,catalog,self.root/"state",self.root/"runs")
        self.other=InvestigationService(data,catalog,self.root/"other",self.root/"other-runs")
        self.addCleanup(self.service.close);self.addCleanup(self.other.close)
        source=business_data();source["business_context"]["store_id"]="test-store"
        self.ledger=generate_advertising(source,0)
        self.service.operations.book=AdvertisingBook(self.ledger,"test-store")

    def task(self,question="炽热爱意2026-10-23销量下降原因"):
        """等待实际Run结束后再从其报告建立运营跟进。"""
        task=self.service.create({"question":question})["task"];deadline=time.monotonic()+5
        while task["status"] in ("queued","running") and time.monotonic()<deadline:
            time.sleep(.01);task=self.service.get(task["id"])
        self.assertEqual(task["status"],"completed",task["status"])
        return task

    def sale_followup(self):
        """建单只传任务ID，指标由服务端从冻结报告读取。"""
        task=self.task();return self.service.operations.create({"source":"task","task_id":task["id"]})["followup"]

    def action(self,item,action="handled",note="负责人记录：已核对可售情况，等待新日期复查"):
        """处理说明只作人工记录，不证明已执行经营动作。"""
        return self.service.operations.action(item["id"],{"expected_revision":item["revision"],"action":action,"note":note})

    def test_task_source_idempotence_and_foreign_task(self):
        """同一来源不会重复建单，跨店任务和任意指标注入均拒绝。"""
        task=self.task();payload={"source":"task","task_id":task["id"]}
        first=self.service.operations.create(payload);second=self.service.operations.create(payload)
        self.assertEqual(first["followup"]["id"],second["followup"]["id"]);self.assertTrue(second["reused"])
        with self.assertRaises(OperatingNotFound):self.other.operations.create(payload)
        with self.assertRaises(ValueError):self.service.operations.create({**payload,"units_sold":999})

    def test_handling_recheck_and_confirm_recovery(self):
        """写已处理不会自动恢复，后续数据达标后也仍需人工确认。"""
        item=self.sale_followup()
        with self.assertRaises(OperatingConflict):self.action(item,"recover","我觉得恢复了")
        item=self.action(item);self.assertEqual(item["status"],"awaiting_recheck")
        with self.assertRaises(OperatingConflict):self.action(item,"recover")
        item=self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-24"})
        self.assertTrue(item["latest_check"]["passed"]);self.assertEqual(item["status"],"awaiting_recheck")
        item=self.action(item,"recover","新日期销量120件，达到原基线100件；仅确认指标恢复")
        self.assertEqual(item["status"],"recovered")
        self.assertEqual(item["origin"]["analysis"]["facts"]["units_sold"]["after"],50)
        self.assertFalse(item["events"][-1]["payload"]["business_action_executed"])

    def test_missing_data_same_date_and_stale_version(self):
        """缺数据不能当0，同日不能当后续证据，旧版本不能覆盖处理。"""
        item=self.sale_followup();old=item["revision"];item=self.action(item)
        with self.assertRaises(ValueError):self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-23"})
        with self.assertRaises(OperatingConflict):self.service.operations.action(item["id"],{"expected_revision":old,"action":"dismiss","note":"旧页面"})
        item=self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-25"})
        self.assertFalse(item["latest_check"]["ready"]);self.assertFalse(item["latest_check"]["passed"])
        with self.assertRaises(OperatingConflict):self.action(item,"recover")

    def test_new_handling_invalidates_previous_check_and_reopen(self):
        """处理方案再次变更会失效旧复查，不能沿用旧通过结论收尾。"""
        item=self.action(self.sale_followup());item=self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-24"})
        item=self.action(item,note="再次修改处理方案")
        self.assertIsNone(item["latest_check"])
        with self.assertRaises(OperatingConflict):self.action(item,"recover")
        item=self.action(item,"dismiss","先观察，不认定已恢复")
        self.assertEqual(self.action(item,"reopen","重新核对来源")["status"],"pending")

    def test_advertising_sources_and_recheck_still_high(self):
        """推广异常由账本重算，后一天仍超线时不能标恢复。"""
        payload={"source":"advertising","date":"2026-10-23","hour":18,"mode":"all"}
        item=self.service.operations.create(payload)["followup"]
        self.assertEqual(item["origin"]["row"]["flag"],"high")
        self.assertEqual(self.service.operations.create(payload)["followup"]["id"],item["id"])
        item=self.action(item);item=self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-24"})
        self.assertTrue(item["latest_check"]["ready"]);self.assertFalse(item["latest_check"]["passed"])
        with self.assertRaises(OperatingConflict):self.action(item,"recover")
        # 全天店凌晨也可能是异常；不能把“0点”写死成正常时段。
        normal=[h['hour'] for h in self.service.operations.book.report('2026-10-23')['hours'] if h['flag']=='normal']
        self.assertTrue(normal)
        with self.assertRaises(OperatingConflict):self.service.operations.create({**payload,"hour":normal[0]})

    def test_advertising_recovery_uses_new_ledger_not_operator_note(self):
        """调整说明不改变扣费；只有后续账本落入原阈值才允许确认。"""
        item=self.action(self.service.operations.create({"source":"advertising","date":"2026-10-23","hour":18})["followup"])
        for row in self.ledger["ads"]:
            if row["date"]=="2026-10-24" and row["hour"]==18:row.update(clicks=1,spend_cents=row["unit_cpc_cents"])
        self.service.operations.book=AdvertisingBook(self.ledger,"test-store")
        item=self.service.operations.recheck(item["id"],{"expected_revision":item["revision"],"date":"2026-10-24"})
        self.assertTrue(item["latest_check"]["passed"])
        self.assertEqual(self.action(item,"recover")["status"],"recovered")

    def test_settings_are_typed_versioned_and_store_scoped(self):
        """门店设置是明确配置，拒绝非法SKU、NaN与旧版本。"""
        settings=self.service.operations.settings();payload={k:v for k,v in settings.items() if k!="revision"};payload.update(expected_revision=0,high_pct=12,featured_skus=["FLR-ROSE-99-BOUQUET"],notes="主推99红玫")
        saved=self.service.operations.save_settings(payload)
        self.assertEqual(saved["revision"],1);self.assertEqual(self.other.operations.settings()["high_pct"],10)
        self.assertEqual(self.service.meta()["operating_settings"]["high_pct"],12)
        task=self.service.create({"question":"炽热爱意销量下降原因"})["task"]
        self.assertEqual(task["parsed"]["operating_settings_snapshot"]["featured_skus"],["FLR-ROSE-99-BOUQUET"])
        self.assertEqual(task["parsed"]["store_memory_snapshot"],[])
        with self.assertRaises(OperatingConflict):self.service.operations.save_settings(payload)
        with self.assertRaises(ValueError):self.service.operations.save_settings({**payload,"expected_revision":1,"high_pct":"NaN"})
        with self.assertRaises(ValueError):self.service.operations.save_settings({**payload,"expected_revision":1,"featured_skus":["OTHER-SKU"]})

    def test_normal_lookup_cannot_be_presented_as_anomaly(self):
        """正常活动查询不能借客户端按钮被包装成有证据的异常。"""
        task=self.task("温柔时光2026-10-23活动结束了吗")
        with self.assertRaises(OperatingConflict):self.service.operations.create({"source":"task","task_id":task["id"]})


if __name__=="__main__":unittest.main()
