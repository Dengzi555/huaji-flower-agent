"""业务数据适配层：将标准化 JSON 变成经过校验的日级记录。

这里负责字段和业务口径，不负责判断下降原因。以后接 ERP/平台 API，
也应先转换到这套契约，再交给分析器；不让下游直接依赖不同平台的原始字段。
"""

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path


# 订单数、买家数和件数各有含义，不能为了简化数据而合并成一个“销量”。
FIELDS = {
    "sales": {"paid_orders": int, "paid_buyers": int, "units_sold": int, "paid_revenue": (int, float)},
    "traffic": {"visitors": int},
    "inventory": {"out_of_stock_minutes": int, "monitored_minutes": int},
    "promotions": {"active": bool, "campaign_name": str},
    "traffic_channels": {"attribution_policy": str, "attributed_visitors": dict},
    "order_cohorts": {"cohort_policy":str,"cohort_paid_orders":int,"cancelled_orders":int,
        "completed_orders":int,"pending_orders":int,"late_cancelled_orders":int,"cancellation_reasons":dict},
}


def previous_day(day: str) -> str:
    """按自然日取前一天，跨月/跨年由 date 运算处理，而不是手工减日期数字。"""
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


class DataStore:
    """只读数据快照；用 (数据域, SKU, 日期) 作为唯一键，防止日记录静默覆盖。"""
    def __init__(self, path: Path):
        """加载并验证输入。无效数据立即拒绝，缺失数据保留给查询阶段表达。"""
        path=Path(path)
        if path.suffix=='.sqlite3':
            from .annual_store import load_annual
            load_annual(self,path);return
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        self._load(raw)

    @classmethod
    def from_document(cls, document):
        """从持久化任务快照构造独立只读数据，复查不修改运行中的原始快照。"""
        instance = cls.__new__(cls)
        instance._load(json.loads(json.dumps(document, ensure_ascii=False)))
        return instance

    def snapshot(self,sku_ids=None,start=None,end=None):
        """导出当前版本的标准化副本，供任务与人工补充版本持久化。"""
        # 元数据、渠道和取消原因含嵌套字典；浅拷贝会让“仅预览”意外修改活动快照。
        if getattr(self,'annual_metadata',None) and sku_ids is None:
            raise ValueError('年度数据须按商品和日期冻结；不支持整库JSON复制或当前整库导入预览。')
        document={**self.meta, **{table:list(self.rows(table,start=start,end=end,sku_ids=sku_ids)) for table in FIELDS}}
        if getattr(self,'annual_metadata',None):
            # 未上架/已停售成员可能在冻结窗口没有任何记录，但仍属于合法目录范围。
            document['known_skus']=sorted(set(sku_ids)&self.skus)
        return json.loads(json.dumps(document,ensure_ascii=False))

    def rows(self,table,sku=None,start=None,end=None,sku_ids=None):
        """文件与年度库共享范围读取接口，避免页面概览/解析扫描全部年度记录。"""
        if hasattr(self.records,'rows'):
            yield from self.records.rows(table,sku,start,end,sku_ids);return
        selected=set(sku_ids) if sku_ids is not None else None
        for (domain,identity,day),row in self.records.items():
            if domain==table and (sku is None or identity==sku) and (selected is None or identity in selected) and (start is None or day>=start) and (end is None or day<=end):yield dict(row)

    def dates(self,table='sales'):
        """日期覆盖来自索引元数据，不扫描每一款商品。"""
        if getattr(self,'annual_metadata',None):return self.annual_metadata['dates'].get(table,[])
        return sorted({day for domain,_,day in self.records if domain==table})

    def has_domain(self,table):
        """只检查数据域是否存在，不以任意其他商品的数据代替本商品查询。"""
        return bool(self.dates(table))

    def _load(self, raw):
        """共享文件/数据库快照校验，避免人工补充路径绕开原数据契约。"""
        if raw.get("schema_version") != 1:
            raise ValueError("Unsupported schema_version; expected 1")
        if type(raw.get("sample_data")) is not bool:
            raise ValueError("sample_data must explicitly be true or false")
        if raw.get("timezone") != "Asia/Shanghai":
            raise ValueError("V1 requires daily data normalized to Asia/Shanghai")
        if not isinstance(raw.get("currency"), str) or not raw["currency"]:
            raise ValueError("currency is required")
        self.meta = {key: raw[key] for key in ("schema_version", "sample_data", "timezone", "currency")}
        if "business_context" in raw:
            context = raw["business_context"]
            if not isinstance(context, dict) or any(not isinstance(value, str) for value in context.values()):
                raise ValueError("business_context must be an object of descriptive strings")
            self.meta["business_context"] = dict(context)
        self.records = {}
        self.skus = set()
        if 'known_skus' in raw:
            identities=raw['known_skus']
            if not isinstance(identities,list) or len(identities)>500 or any(not isinstance(s,str) or not s for s in identities) or len(set(identities))!=len(identities):
                raise ValueError('Invalid declared catalog identities')
            self.skus.update(identities);self.meta['known_skus']=list(identities)
        for table, fields in FIELDS.items():
            rows = raw.get(table, [])
            if not isinstance(rows, list):
                raise ValueError(f"{table} must be an array")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"{table}: record must be an object")
                for name in ("sku_id", "date", "source_id", "observed_at"):
                    if not isinstance(row.get(name), str) or not row[name]:
                        raise ValueError(f"{table}: missing {name}")
                date.fromisoformat(row["date"])
                observed = datetime.fromisoformat(row["observed_at"])
                if observed.tzinfo is None:
                    raise ValueError("observed_at must include a timezone offset")
                if row.get("status") not in ("ok", "unavailable", "delayed"):
                    raise ValueError("status must be ok, unavailable or delayed")
                if row["status"] == "ok":
                    # 只有声明完整的记录才校验指标；延迟/不可用记录允许没有数值。
                    for field, kind in fields.items():
                        value = row.get(field)
                        # Python 的 bool 是 int 子类，因此用 type 而不是 isinstance，避免 True 被当成 1 单。
                        if type(value) not in (kind if isinstance(kind, tuple) else (kind,)):
                            raise ValueError(f"{table}: invalid type for {field}")
                        if type(value) in (int, float) and (not math.isfinite(value) or value < 0):
                            raise ValueError(f"{table}: {field} must be finite and non-negative")
                    if table == "inventory" and not (0 < row["monitored_minutes"] <= 1440
                            and row["out_of_stock_minutes"] <= row["monitored_minutes"]):
                        raise ValueError("Invalid inventory observation window")
                    if table == "sales" and row["paid_orders"] > row["units_sold"]:
                        raise ValueError("paid_orders must not exceed units_sold under the V1 contract")
                    if table == "sales" and row["paid_buyers"] > row["paid_orders"]:
                        raise ValueError("paid_buyers must not exceed paid_orders")
                    if table == "traffic_channels":
                        # UV 一般不能直接跨渠道相加。本契约显式要求互斥的末次归因人数。
                        if row["attribution_policy"] != "exclusive_last_touch_v1":
                            raise ValueError("Channel data requires exclusive_last_touch_v1 attribution")
                        channels = row["attributed_visitors"]
                        if not channels or any(not isinstance(key, str) or not key or type(value) is not int
                                               or value < 0 for key, value in channels.items()):
                            raise ValueError("Channel visitors must be non-negative integer counts")
                    if table == 'order_cohorts':
                        # 三个终态互斥并覆盖支付批次，跨日取消只能是取消订单的子集。
                        if row['cohort_policy']!='paid_date_as_of_v1' or row['cohort_paid_orders'] != sum(row[k] for k in ('cancelled_orders','completed_orders','pending_orders')) or row['late_cancelled_orders']>row['cancelled_orders']:
                            raise ValueError('Invalid paid order cohort partition')
                        reasons=row['cancellation_reasons']
                        if any(not isinstance(k,str) or not k or type(v) is not int or v<0 for k,v in reasons.items()) or sum(reasons.values())!=row['cancelled_orders']:
                            raise ValueError('Cancellation reasons must partition cancelled orders')
                        if observed.date()<date.fromisoformat(row['date']):raise ValueError('Cohort cutoff precedes payment date')
                key = (table, row["sku_id"], row["date"])
                if key in self.records:
                    raise ValueError(f"Duplicate daily record: {key}")
                self.records[key] = row
                self.skus.add(row["sku_id"])

    def has_import(self,sku_id,days):
        """混合学习工作区中检查来源边界；原本就声明为用户数据的快照不受此隔离策略影响。"""
        if getattr(self,'annual_metadata',None):return False  # 年度不可变库不混入导入版本。
        return self.meta['sample_data'] and any(
            self.records.get((table,sku_id,day),{}).get('source_id','').startswith('import:')
            for day in days for table in FIELDS)

    def daily_record(self,table,sku_id,day,*,imported_only=False):
        """按日期取值；导入涉及的商品/日期不再用学习样例补充其他数据域。"""
        from .cancellation import checkpoint
        checkpoint()
        row=self.records.get((table,sku_id,day))
        isolated=imported_only or self.has_import(sku_id,[day])
        if row and (not isolated or row['source_id'].startswith('import:')):return json.loads(json.dumps(row))
        return {'sku_id':sku_id,'date':day,'status':'unavailable','source_id':None,'observed_at':None,
            'reason':'Imported data cannot use learning examples as missing domains or baselines' if row and isolated else 'No daily record supplied'}

    def query(self, table: str, sku_id: str, day: str, *, comparison_day=None) -> dict:
        """返回 [对比日, 目标日] 两条观察；没有记录时返回 unavailable，而不伪造零。"""
        if sku_id not in self.skus:
            raise ValueError(f"Unknown sku_id: {sku_id}")
        result = []
        baseline=comparison_day or previous_day(day)
        date.fromisoformat(baseline)
        imported_only=self.has_import(sku_id,[baseline,day])
        for current in (baseline, day):
            row = self.daily_record(table,sku_id,current,imported_only=imported_only)
            result.append(dict(row))
        return {"table": table, "sku_id": sku_id, "target_date": day,
                "comparison_date": baseline, **self.meta, "records": result}
