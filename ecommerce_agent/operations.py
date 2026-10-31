"""门店运营闭环：从真实查询快照建跟进单，记录处理，再用后续数据复查指标。

当前仍使用显式标记的学习数据。不执行推广后台操作，不把处理说明当作恢复证据。
"""

import json
import re
import uuid
from datetime import date
from decimal import Decimal

from .clock import utc_now
from .task_parser import shanghai_today

SCHEMA="""
CREATE TABLE IF NOT EXISTS operating_settings (
 id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operating_settings_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,previous TEXT NOT NULL,current TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operating_followups (
 id TEXT PRIMARY KEY,source_key TEXT NOT NULL UNIQUE,title TEXT NOT NULL,kind TEXT NOT NULL,
 status TEXT NOT NULL,revision INTEGER NOT NULL,scope TEXT NOT NULL,origin TEXT NOT NULL,
 latest_check TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operating_followup_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,followup_id TEXT NOT NULL REFERENCES operating_followups(id),
 created_at TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL
);
"""


class OperatingNotFound(ValueError):
    """对象不在当前门店，HTTP返回404。"""


class OperatingConflict(ValueError):
    """状态、版本或复查证据不符合要求，HTTP返回409。"""


def ended_day(value):
    """只接受已结束自然日，不把今天的部分数据当全天。"""
    day=date.fromisoformat(value).isoformat()
    if day>=shanghai_today().isoformat():raise ValueError("当前只支持已结束自然日，请选择昨天或更早日期。")
    return day


class StoreOperations:
    """本店的档案、异常跟进和时间线；复用现有独立SQLite与只读数据源。"""

    def __init__(self,service):
        """绑定单店服务；推广账本由StoreHub启动时注入，不接受HTTP文件路径。"""
        self.service,self.book=service,None

    def settings(self):
        """返回有版本的明确配置；营业时间仅作档案，不重算历史监测数据。"""
        with self.service.connection() as db:
            row=db.execute("SELECT revision,document FROM operating_settings WHERE id=1").fetchone()
        return {"revision":row[0],**json.loads(row[1])} if row else {"revision":0,"open_time":"09:00","close_time":"23:00","high_pct":10,"low_pct":3,"featured_skus":[],"notes":""}

    def save_settings(self,payload):
        """验证时间、阈值、主推SKU及版本；不把配置修改冒充广告预算执行。"""
        keys={"expected_revision","open_time","close_time","high_pct","low_pct","featured_skus","notes"}
        if not isinstance(payload,dict) or set(payload)!=keys:raise ValueError("请完整提交门店设置与当前版本。")
        for key in ("open_time","close_time"):
            value=payload[key]
            if not isinstance(value,str) or not (re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",value) or key=="close_time" and value=="24:00"):
                raise ValueError("营业时间须为HH:MM；结束时间也可填写24:00。")
        if payload["open_time"]==payload["close_time"]:raise ValueError("起止时间不能相同；全天营业请填写00:00至24:00。")
        from .advertising import AdvertisingBook
        high,low=AdvertisingBook.threshold(payload["high_pct"]),AdvertisingBook.threshold(payload["low_pct"])
        if low>=high:raise ValueError("低投入关注线必须小于高费用线。")
        skus=payload["featured_skus"]
        if not isinstance(skus,list) or len(skus)>10 or any(not isinstance(s,str) or s not in self.service.catalog.products for s in skus) or len(set(skus))!=len(skus):raise ValueError("主推商品最多10款，必须属于本店目录且不能重复。")
        if not isinstance(payload["notes"],str) or len(payload["notes"])>2000:raise ValueError("经营约定最多2000字。")
        document={k:payload[k] for k in keys-{"expected_revision"}}
        document.update(high_pct=float(high),low_pct=float(low))
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT revision,document FROM operating_settings WHERE id=1").fetchone()
            revision=row[0] if row else 0
            if type(payload["expected_revision"]) is not int or payload["expected_revision"]!=revision:raise OperatingConflict("门店设置已变化，请刷新后保存。")
            value=json.dumps(document,ensure_ascii=False)
            db.execute("INSERT OR REPLACE INTO operating_settings VALUES(1,?,?)",(revision+1,value))
            db.execute("INSERT INTO operating_settings_events(created_at,previous,current) VALUES(?,?,?)",(utc_now(),row[1] if row else "{}",value))
        return {"revision":revision+1,**document}

    def event(self,db,fid,kind,**payload):
        """处理说明和复查证据分别追加，来源快照与旧结论不覆盖。"""
        db.execute("INSERT INTO operating_followup_events(followup_id,created_at,type,payload) VALUES(?,?,?,?)",(fid,utc_now(),kind,json.dumps(payload,ensure_ascii=False)))

    def decode(self,row):
        """数据库JSON恢复成可展示的门店范围对象。"""
        item=dict(row)
        for key in ("scope","origin","latest_check"):item[key]=json.loads(item[key]) if item[key] else None
        return item

    def list(self):
        """返回最近跟进单，包含已恢复和暂不处理记录。"""
        with self.service.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM operating_followups ORDER BY updated_at DESC LIMIT 150")]

    def get(self,fid):
        """只读取当前门店对象及完整时间线，跨店ID不能穿透。"""
        with self.service.connection() as db:
            row=db.execute("SELECT * FROM operating_followups WHERE id=?",(fid,)).fetchone()
            if not row:raise OperatingNotFound("当前门店不存在这条异常跟进。")
            item=self.decode(row)
            item["events"]=[{**dict(e),"payload":json.loads(e["payload"])} for e in db.execute("SELECT * FROM operating_followup_events WHERE followup_id=? ORDER BY id",(fid,))]
        return item

    def create(self,payload):
        """只从本店已完成调查或重算后的推广时段创建；客户端不能捏造异常指标。"""
        if not isinstance(payload,dict):raise ValueError("异常来源必须为对象。")
        source=payload.get("source")
        if source=="task":
            if set(payload)!={"source","task_id"}:raise ValueError("调查跟进只接受本店任务ID。")
            if not isinstance(payload["task_id"],str) or not re.fullmatch(r"[a-f0-9]{32}",payload["task_id"]):raise ValueError("调查任务ID格式不合法。")
            task=self.service.get(payload["task_id"])
            if not task:raise OperatingNotFound("本店调查不存在。")
            if task["status"]!="completed" or not task["result"]:raise OperatingConflict("调查尚未完成，不能以未确认结果建立异常。")
            analysis=task["result"].get("analysis") or {};facts=analysis.get("facts") or {}
            sales=facts.get("units_sold")
            bad=analysis.get("data_quality") in ("partial","conflicting")
            if bad:kind,title="data_quality","数据缺口或来源冲突"
            elif sales and sales.get("relative_pct") is not None and sales["relative_pct"]<0:kind,title="sales",f"销量下降 {abs(sales['relative_pct'])}%"
            elif task["parsed"]["intent"]=="query_inventory" and (analysis.get("records") or [{}])[-1].get("out_of_stock_minutes",0)>0:kind,title="inventory","存在缺货时段"
            else:raise OperatingConflict("本次查询没有可跟进的销量下降、缺货或数据异常。")
            scope={"sku_id":task["parsed"]["sku_id"],"date":task["parsed"]["target_date"],"task_id":task["id"]}
            product=self.service.catalog.products.get(scope["sku_id"],{})
            title=f"{product.get('short_name',scope['sku_id'])} · {title}"
            origin={"source":"task","task_id":task["id"],"fingerprint":task["fingerprint"],"analysis":analysis,"baseline_units":sales.get("before") if sales else None}
            key="task:"+task["id"]
        elif source=="advertising":
            if set(payload)-{"source","date","hour","mode","high","low"}:raise ValueError("推广跟进字段不合法。")
            if self.book is None:raise ValueError("本店尚未提供推广账本。")
            day=ended_day(payload.get("date"));hour=payload.get("hour")
            if type(hour) is not int or not 0<=hour<24:raise ValueError("请选择0–23时的异常时段。")
            settings=self.settings();report=self.book.report(day,mode=payload.get("mode","all"),high=payload.get("high",settings["high_pct"]),low=payload.get("low",settings["low_pct"]))
            row=report["hours"][hour]
            if row["flag"]=="normal":raise OperatingConflict("该时段未触发当前关注线，不能伪造推广异常。")
            kind="advertising";title=f"{row['label']} · "+{"high":"推广费高于费用线","low":"低投入待核对","no_revenue":"有扣费无成交"}[row["flag"]]
            scope={"date":day,"hour":hour,"mode":report["mode"]}
            origin={"source":"advertising","fingerprint":report["fingerprint"],"row":row,"thresholds":report["thresholds"]}
            key=f"ad:{day}:{hour}:{report['mode']}"
        else:raise ValueError("请从调查结果或推广异常时段发起跟进。")
        fid,now=uuid.uuid4().hex,utc_now()
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old=db.execute("SELECT id FROM operating_followups WHERE source_key=?",(key,)).fetchone()
            if old:return {"followup":self.get(old[0]),"reused":True}
            db.execute("INSERT INTO operating_followups VALUES(?,?,?,?,?,?,?,?,?,?,?)",(fid,key,title,kind,"pending",1,json.dumps(scope,ensure_ascii=False),json.dumps(origin,ensure_ascii=False),None,now,now))
            self.event(db,fid,"opened",source=source,source_key=key)
        return {"followup":self.get(fid),"reused":False}

    def action(self,fid,payload):
        """记录人工处理或关闭；恢复必须有处理之后的新日期复查通过。"""
        if not isinstance(payload,dict) or set(payload)!={"expected_revision","action","note"}:raise ValueError("请提交跟进版本、操作与说明。")
        action,note=payload["action"],payload["note"]
        if action not in ("handled","recover","dismiss","reopen") or not isinstance(note,str) or not 1<=len(note.strip())<=2000:raise ValueError("请选择有效操作并填写处理/复核说明。")
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE");item=self._checked(db,fid,payload["expected_revision"])
            if action=="recover" and (item["status"]!="awaiting_recheck" or not (item["latest_check"] or {}).get("passed")):raise OperatingConflict("需要先记录处理，再以后续日期数据复查通过，才能标记恢复。")
            if item["status"] in ("recovered","dismissed") and action!="reopen":raise OperatingConflict("跟进已收尾，请先重新打开。")
            if action=="reopen" and item["status"] not in ("recovered","dismissed"):raise OperatingConflict("当前跟进尚未收尾。")
            status={"handled":"awaiting_recheck","recover":"recovered","dismiss":"dismissed","reopen":"pending"}[action]
            keep=item["latest_check"] if action in ("recover","dismiss") else None
            db.execute("UPDATE operating_followups SET status=?,revision=revision+1,latest_check=?,updated_at=? WHERE id=?",(status,json.dumps(keep,ensure_ascii=False) if keep else None,utc_now(),fid))
            self.event(db,fid,action,note=note.strip(),attestation="operator_recorded_unverified",business_action_executed=False)
        return self.get(fid)

    def _checked(self,db,fid,revision):
        """同一写事务校验对象、版本，防止旧页面覆盖新处理。"""
        row=db.execute("SELECT * FROM operating_followups WHERE id=?",(fid,)).fetchone()
        if not row:raise OperatingNotFound("当前门店不存在这条异常跟进。")
        if type(revision) is not int or row["revision"]!=revision:raise OperatingConflict("跟进版本已更新，请刷新后操作。")
        return self.decode(row)

    def recheck(self,fid,payload):
        """读取后续日期同SKU或同小时的来源，保存证据；不凭处理说明自动恢复。"""
        if not isinstance(payload,dict) or set(payload)!={"expected_revision","date"}:raise ValueError("复查需要版本和业务日期。")
        day=ended_day(payload["date"]);item=self.get(fid)
        if day<=item["scope"]["date"]:raise ValueError("请选择异常发生日之后的已结束日期；同日快照不作为恢复证据。")
        if item["status"] not in ("pending","awaiting_recheck"):raise OperatingConflict("请先重新打开该跟进。")
        check={"date":day,"checked_at":utc_now(),"passed":False,"ready":False,"scope":"只核对预先固定的指标恢复条件，不证明根因或处理措施的因果效果。"}
        if item["kind"]=="advertising":
            try:
                report=self.book.report(day,mode=item["scope"]["mode"],high=item["origin"]["thresholds"]["high_pct"],low=item["origin"]["thresholds"]["low_pct"])
                row=report["hours"][item["scope"]["hour"]];flag=item["origin"]["row"]["flag"]
                high,low=Decimal(str(item["origin"]["thresholds"]["high_pct"])),Decimal(str(item["origin"]["thresholds"]["low_pct"]))
                # 费用线仍用原分币交叉相乘，避免显示值四舍五入后错过边界。
                within=row["revenue_cents"]>0 and row["spend_cents"]*100<=high*row["revenue_cents"]
                passed=within if flag=="high" else within and row["enabled"] and row["spend_cents"]*100>=low*row["revenue_cents"] if flag=="low" else row["spend_cents"]==0 or within
                check.update(ready=True,passed=bool(passed),row=row,fingerprint=report["fingerprint"],criterion={"original_flag":flag,"high_pct":float(high),"low_pct":float(low)})
            except (ValueError,AttributeError):check["reason"]="所选日期推广账本未提供，不能把未知扣费当成零。"
        else:
            sku=item["scope"]["sku_id"]
            try:records={table:self.service.store.query(table,sku,day)["records"][-1] for table in ("sales","traffic","inventory","promotions")}
            except ValueError:records={}
            check["records"]=records;check["fingerprint"]=self.service.fingerprint
            complete=len(records)==4 and all(r["status"]=="ok" for r in records.values())
            if complete:
                sales,traffic,inventory=records["sales"],records["traffic"],records["inventory"]
                conflict=sales["paid_buyers"]>traffic["visitors"] or inventory["out_of_stock_minutes"]==1440 and sales["units_sold"]>0
                if item["kind"]=="sales":passed=item["origin"]["baseline_units"] is not None and sales["units_sold"]>=item["origin"]["baseline_units"]
                elif item["kind"]=="inventory":passed=inventory["monitored_minutes"]==1440 and inventory["out_of_stock_minutes"]==0
                else:passed=not conflict
                check.update(ready=True,passed=bool(passed and not conflict and inventory["monitored_minutes"]==1440),criterion={"kind":item["kind"],"baseline_units":item["origin"].get("baseline_units")})
            else:check["reason"]="复查来源缺失或延迟，无法确认恢复；缺失不当作零。"
        with self.service.connection() as db:
            db.execute("BEGIN IMMEDIATE");self._checked(db,fid,payload["expected_revision"])
            db.execute("UPDATE operating_followups SET latest_check=?,revision=revision+1,updated_at=? WHERE id=?",(json.dumps(check,ensure_ascii=False),utc_now(),fid))
            self.event(db,fid,"rechecked",check=check)
        return self.get(fid)


def overview(hub,day):
    """各门店同日概览：汇总计数，缺失显式标记，不把未提供日期切到最后一天。"""
    from .data import previous_day
    day=ended_day(day);shops=[]
    for shop in hub.stores:
        if shop.get('status','active')!='active':continue
        service=hub.for_store(shop["id"]);by_day=[]
        for current in (previous_day(day),day):
            rows=list(service.store.rows('sales',start=current,end=current))
            expected={sku for sku,p in service.catalog.products.items() if (not p.get("listed_at") or p["listed_at"]<=current) and (not p.get('retired_at') or current<=p['retired_at'])}
            seen={r["sku_id"] for r in rows}
            by_day.append({"ready":bool(rows) and expected.issubset(seen) and all(r["status"]=="ok" for r in rows),"revenue_cents":sum(round(r.get("paid_revenue",0)*100) for r in rows),"orders":sum(r.get("paid_orders",0) for r in rows),"units":sum(r.get("units_sold",0) for r in rows)})
        before,current=by_day
        change=round((current["units"]-before["units"])/before["units"]*100,2) if current["ready"] and before["ready"] and before["units"] else None
        try:
            config=service.operations.settings();ads=hub.books[shop["id"]].report(day,high=config["high_pct"],low=config["low_pct"])
            ad={"ready":True,**ads["totals"],"high_hours":len(ads["attention"]["high"]),"no_revenue_hours":len(ads["attention"]["no_revenue"])}
        except ValueError:ad={"ready":False}
        shops.append({**shop,"sales":current,"units_change_pct":change,"advertising":ad,"open_followups":sum(r["status"] in ("pending","awaiting_recheck") for r in service.operations.list())})
    return {"date":day,"stores":shops,"sample_data":True,"scope":"门店日级学习快照；费用率分母为同店营业额，不是广告归因收入。"}
