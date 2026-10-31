"""多店与推广回归：跨店隔离、同口径分币合计、零分母及可调阈值。"""

import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ecommerce_agent.advertising import AdvertisingBook
from ecommerce_agent.flower_business_data import business_catalog, business_data
from ecommerce_agent.store_workspace import STORES, StoreHub, generate_advertising, generate_stores
from ecommerce_agent.web import create_server


class AdvertisingTests(unittest.TestCase):
    """不依赖大模型或平台接口，直接检查可核算的小时账本。"""

    def setUp(self):
        """以同一店铺经营快照构造两种模式，销售额分配必须保持分币合计。"""
        self.data = business_data()
        self.sid = STORES[0]["id"]
        self.data["business_context"]["store_id"] = self.sid
        self.raw = generate_advertising(self.data, 0)
        self.book = AdvertisingBook(self.raw, self.sid)

    def test_hourly_revenue_matches_sales_and_modes_do_not_double_count(self):
        """营业额不能随推广模式加总两遍。"""
        all_modes = self.book.report("2026-10-25")
        manual = self.book.report("2026-10-25", mode="manual")
        automatic = self.book.report("2026-10-25", mode="automatic")
        expected = sum(round(r["paid_revenue"] * 100) for r in self.data["sales"] if r["date"] == "2026-10-25")
        self.assertEqual(all_modes["totals"]["revenue_cents"], expected)
        self.assertEqual(manual["totals"]["revenue_cents"], automatic["totals"]["revenue_cents"])
        self.assertEqual(all_modes["totals"]["spend_cents"], manual["totals"]["spend_cents"] + automatic["totals"]["spend_cents"])
        self.assertEqual(len(all_modes["hours"]), 24)

    def test_click_billing_uses_cents_and_weighted_cpc(self):
        """每小时CPC可不同，模式平均CPC用总扣费/总点击，不能算每小时CPC的简单均值。"""
        report = self.book.report("2026-10-25")
        for row in report["hours"]:
            self.assertEqual(row["spend_cents"], sum(item["clicks"] * item["unit_cpc_cents"] for item in row["modes"]))
            if row["clicks"]:
                self.assertEqual(row["cpc_cents"], round(row["spend_cents"] / row["clicks"], 2))

    def test_zero_revenue_and_paused_hours_have_distinct_meanings(self):
        """有扣费无成交不显示0%；未投放不触发低投入。"""
        raw = copy.deepcopy(self.raw)
        for row in raw["revenue"]:
            if row["date"] == "2026-10-25" and row["hour"] == 23:row.update(revenue_cents=0,paid_orders=0)
        report = AdvertisingBook(raw,self.sid).report("2026-10-25")
        self.assertEqual(report["hours"][23]["flag"], "no_revenue")
        self.assertIsNone(report["hours"][23]["spend_ratio_pct"])
        small = AdvertisingBook(generate_advertising(self.data,2),self.sid).report("2026-10-25")
        self.assertIsNone(small["hours"][0]["cpc_cents"])
        self.assertEqual(small["hours"][0]["flag"], "normal")
        self.assertFalse(small["hours"][0]["enabled"])
        self.assertIn(18, report["attention"]["high"])
        self.assertTrue(report["attention"]["low"])

    def test_all_day_shop_and_evening_paused_shop(self):
        """全天低价夜投与停投后的自然成交不能混为无数据。"""
        big=self.book.report("2026-10-25")
        self.assertTrue(all(h["operating"] and h["enabled"] for h in big["hours"]))
        self.assertGreater(big["hours"][2]["spend_cents"],0)
        small=AdvertisingBook(generate_advertising(self.data,2),self.sid).report("2026-10-25")
        self.assertFalse(small["hours"][0]["operating"])
        for h in small["hours"][20:23]:
            self.assertTrue(h["operating"]);self.assertFalse(h["enabled"])
            self.assertEqual(h["spend_cents"],0)
            self.assertGreater(h["visitors"],0)
        self.assertGreater(sum(h["paid_orders"] for h in small["hours"][20:23]),0)
        self.assertEqual(sum(h["paid_orders"] for h in big["hours"]),sum(r["paid_orders"] for r in self.data["sales"] if r["date"]=="2026-10-25"))

    def test_closed_hour_cannot_have_revenue_or_enabled_ads(self):
        """已知未营业与采集缺失分开；矛盾账本不能被界面掩盖。"""
        raw=copy.deepcopy(self.raw);raw["revenue"][0]["operating"]=False
        with self.assertRaises(ValueError):AdvertisingBook(raw,self.sid)

    def test_threshold_is_strict_and_not_rounded_display_value(self):
        """等于10%不算超过；10.001%即使显示10.00%也算超过。"""
        raw = copy.deepcopy(self.raw)
        for row in raw["revenue"]:
            if row["date"] == "2026-10-25" and row["hour"] in (12,13):
                row["revenue_cents"] = 10000 if row["hour"] == 12 else 9999
        for row in raw["ads"]:
            if row["date"] == "2026-10-25" and row["hour"] in (12,13):
                row.update(clicks=10 if row["mode"] == "manual" else 0, unit_cpc_cents=100,
                           spend_cents=1000 if row["mode"] == "manual" else 0, impressions=100)
        report = AdvertisingBook(raw,self.sid).report("2026-10-25")
        self.assertEqual(report["hours"][12]["flag"], "normal")
        self.assertEqual(report["hours"][13]["spend_ratio_pct"], 10)
        self.assertEqual(report["hours"][13]["flag"], "high")

    def test_invalid_filters_and_missing_day_are_rejected(self):
        """NaN、倒置阈值和缺数据不得默认为正常。"""
        for kwargs in ({"high":"NaN"},{"low":11},{"mode":"other"},{"high":"Infinity"}):
            with self.assertRaises(ValueError):
                self.book.report("2026-10-25", **kwargs)
        with self.assertRaises(ValueError):
            self.book.report("2026-09-14")

    def test_corrupt_or_mixed_store_ledger_rejected(self):
        """错误扣费、跨店记录、重复行和缺小时不会被汇总掩盖。"""
        for defect in ("billing","store","duplicate","missing"):
            raw = copy.deepcopy(self.raw)
            if defect == "billing": raw["ads"][0]["spend_cents"] = 1
            if defect == "store": raw["ads"][0]["store_id"] = STORES[1]["id"]
            if defect == "duplicate": raw["ads"].append(copy.deepcopy(raw["ads"][0]))
            if defect == "missing": raw["ads"].pop(0)
            with self.assertRaises(ValueError): AdvertisingBook(raw,self.sid)


class StoreHttpTests(unittest.TestCase):
    """使用真实HTTP验证选择店铺，而不是仅断言下拉框文字变化。"""

    def setUp(self):
        """六店临时工作区与随机端口；不开后台线程以测试澄清任务隔离。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        generate_stores(root,business_data())
        (root/"data/products.json").write_text(json.dumps(business_catalog()),encoding="utf-8")
        self.hub = StoreHub(root,root/"state",root/"runs",start_worker=False)
        self.addCleanup(self.hub.close)
        self.server = create_server(self.hub,port=0,token="t"*30)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        """先停HTTP，再关闭各店服务，最后删除临时目录。"""
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def test_store_context_auth_scope_confirmation_and_audit(self):
        """新记忆接口不能绕过鉴权、店铺隔离与人工确认状态。"""
        from datetime import timedelta
        from ecommerce_agent.task_parser import shanghai_today
        first,second=STORES[0]['id'],STORES[1]['id']
        self.assertEqual(self.request('/api/store-context',authorized=False)[0],401)
        payload={'slot':'response_preference','content':'先给指标，再列出待核对事项','source':'测试负责人确认',
            'valid_from':'2026-10-01','expires_on':(shanghai_today()+timedelta(days=10)).isoformat()}
        code,result=self.request('/api/store-context',store=first,payload=payload)
        self.assertEqual(code,201);memory=result['memory'];path='/api/store-context/'+memory['id']
        self.assertEqual(self.request(path,store=second)[0],404)
        self.assertEqual(self.request(path+'/action',store=second,payload={'action':'confirm','expected_revision':1,'note':'跨店'})[0],404)
        self.assertEqual(self.request(path+'/action',store=first,payload={'action':'confirm','expected_revision':1,'note':'核对来源'})[0],200)
        self.assertEqual([e['type'] for e in self.request(path,store=first)[1]['memory']['events']],['proposed','confirm'])
        self.assertEqual(self.request('/api/store-context',store=second)[1]['memories'],[])
        code,result=self.request('/api/tasks',store=first,payload={'question':'99红玫2026-10-25销量怎么样'})
        self.assertEqual(code,201);self.assertEqual(result['task']['parsed']['store_memory_snapshot'][0]['id'],memory['id'])

    def request(self,path,*,store=None,payload=None,authorized=True):
        """令牌固定为测试值；选店头随每次请求传递，不改变服务器全局状态。"""
        headers = {"Content-Type":"application/json"}
        if authorized: headers["Authorization"]="Bearer "+"t"*30
        if store: headers["X-Store-ID"]=store
        req = urllib.request.Request(self.base+path,headers=headers,data=None if payload is None else json.dumps(payload).encode())
        try:
            with urllib.request.urlopen(req,timeout=5) as response: return response.status,json.load(response)
        except urllib.error.HTTPError as error: return error.code,json.load(error)

    def test_store_list_meta_and_unknown_store(self):
        """店铺配置可见，未知店铺不能回落到第一家。"""
        self.assertEqual(len(self.request("/api/stores")[1]["stores"]),6)
        sid = STORES[1]["id"]
        meta = self.request("/api/meta",store=sid)[1]
        self.assertEqual(meta["business_context"]["store_name"],"重庆南岸店")
        self.assertEqual(self.request("/api/meta",store="unknown")[0],400)
        self.assertEqual(self.request("/api/stores",authorized=False)[0],401)

    def test_duplicate_port_cannot_serve_different_workspace(self):
        """重复启动须报错，不能让旧版和新版同时监听相同端口。"""
        with self.assertRaises(OSError):
            create_server(self.hub,port=self.server.server_port,token="t"*30)

    def test_tasks_parent_and_artifacts_do_not_cross_stores(self):
        """另一店铺不能读取、关联或下载此任务；共享口令仍可显式切回原店。"""
        first,second = STORES[0]["id"],STORES[1]["id"]
        task = self.request("/api/tasks",store=first,payload={"question":"99红玫爆款销量下降原因"})[1]["task"]
        self.assertEqual(task["parsed"]["store_id"],first)
        self.assertEqual(self.request("/api/tasks/"+task["id"],store=second)[0],404)
        self.assertEqual(self.request("/api/tasks/"+task["id"]+"/artifacts/request.json",store=second)[0],404)
        self.assertEqual(self.request("/api/tasks",store=second)[1]["tasks"],[])
        self.assertEqual(self.request("/api/tasks",store=second,payload={"question":"昨天销量怎么样","parent_id":task["id"]})[0],400)
        self.assertEqual(len(self.request("/api/tasks",store=first)[1]["tasks"]),1)

    def test_advertising_is_authorized_store_scoped_and_filterable(self):
        """推广接口沿用鉴权；切店会改变账本和费用，而非仅改变店名。"""
        path="/api/advertising?date=2026-10-25"
        self.assertEqual(self.request(path,authorized=False)[0],401)
        first=self.request(path,store=STORES[0]["id"])[1]
        second=self.request(path,store=STORES[1]["id"])[1]
        self.assertNotEqual(first["fingerprint"],second["fingerprint"])
        self.assertNotEqual(first["totals"]["revenue_cents"],second["totals"]["revenue_cents"])
        self.assertEqual(self.request(path+"&high=NaN")[0],400)
        self.assertEqual(self.request(path+"&mode=manual")[1]["mode"],"manual")

    def test_mission_memory_api_authentication_and_store_scope(self):
        """长期对象也必须受店铺与令牌范围约束。"""
        from ecommerce_agent.task_parser import shanghai_today
        from datetime import timedelta
        first,second=STORES[0]["id"],STORES[1]["id"]
        self.assertEqual(self.request("/api/memory",authorized=False)[0],401)
        self.assertEqual(self.request("/api/missions",authorized=False)[0],401)
        status,result=self.request("/api/missions",store=first,payload={"title":"本店目标","goal":"核查经营变化","acceptance":"人工核对证据","deadline":(shanghai_today()+timedelta(days=7)).isoformat()})
        self.assertEqual(status,201);mid=result["mission"]["id"]
        self.assertEqual(self.request("/api/missions/"+mid,store=second)[0],404)
        self.assertEqual(self.request("/api/missions/"+mid+"/action",store=first,payload={"expected_revision":0,"action":"cancel","note":"旧版本"})[0],409)
        self.assertEqual(self.request("/api/memory",store=first,payload={"title":"本店偏好","kind":"preference","content":"先看指标","source":"负责人"})[0],201)
        self.assertEqual(self.request("/api/memory",store=second)[1]["memories"],[])

    def test_operating_overview_followups_and_settings_api(self):
        """运营入口沿用口令和店铺范围；恢复不能仅凭操作说明。"""
        first,second=STORES[0]["id"],STORES[1]["id"]
        for path in ("/api/overview","/api/followups","/api/settings"):
            self.assertEqual(self.request(path,authorized=False)[0],401)
        status,report=self.request("/api/overview?date=2026-10-25")
        self.assertEqual(status,200);self.assertEqual(len(report["stores"]),6)
        status,result=self.request("/api/followups",store=first,payload={"source":"advertising","date":"2026-10-23","hour":18})
        self.assertEqual(status,201);item=result["followup"]
        self.assertEqual(self.request("/api/followups/"+item["id"],store=second)[0],404)
        path="/api/followups/"+item["id"]
        self.assertEqual(self.request(path+"/action",store=first,payload={"expected_revision":item["revision"],"action":"recover","note":"口头说恢复"})[0],409)
        status,result=self.request(path+"/action",store=first,payload={"expected_revision":item["revision"],"action":"handled","note":"负责人记录处理，尚未复查"})
        self.assertEqual(status,200);self.assertEqual(result["followup"]["status"],"awaiting_recheck")
        self.assertEqual(self.request(path+"/recheck",store=first,payload={"expected_revision":result["followup"]["revision"],"date":"2026-10-24"})[0],200)
        settings=self.request("/api/settings",store=first)[1]["settings"]
        payload={k:v for k,v in settings.items() if k!="revision"};payload.update(expected_revision=settings["revision"],high_pct=12)
        self.assertEqual(self.request("/api/settings",store=first,payload=payload)[0],200)
        self.assertEqual(self.request("/api/settings",store=second)[1]["settings"]["high_pct"],10)

    def test_overview_missing_product_is_not_counted_as_zero(self):
        """某个已上架SKU缺销售行时，门店汇总须标缺数据，不显示成完整业绩。"""
        service=self.hub.for_store(STORES[0]["id"])
        key=next(key for key in service.store.records if key[0]=="sales" and key[2]=="2026-10-25")
        del service.store.records[key]
        status,report=self.request("/api/overview?date=2026-10-25")
        self.assertEqual(status,200)
        self.assertFalse(report["stores"][0]["sales"]["ready"])
        self.assertTrue(report["stores"][1]["sales"]["ready"])


if __name__ == "__main__": unittest.main()
