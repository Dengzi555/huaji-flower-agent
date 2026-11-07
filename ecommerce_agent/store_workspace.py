"""多店工作区：同品牌共享界面、分店独立数据和任务库；请求显式选店，不用进程全局活动店铺。"""

import json
import random
import re
from pathlib import Path

from .advertising import load_advertising
from .web_service import InvestigationService
from .order_funnel import generate_order_cohorts

STORES = [
    {"id": "hx-sz-ns", "name": "深圳南山店", "city": "深圳", "district":"南山区", "aliases":["南山店","深圳南山"], "status":"active"},
    {"id": "hx-cq-na", "name": "重庆南岸店", "city": "重庆", "district":"南岸区", "aliases":["南岸店","重庆南岸"], "status":"active"},
    {"id": "hx-zz-trj", "name": "郑州唐人街店", "city": "郑州", "aliases":["唐人街店","郑州唐人街"], "status":"active"},
    {"id": "hx-cd-yl", "name": "成都玉林路店", "city": "成都", "aliases":["玉林路店","四川成都玉林路店"], "status":"active"},
    {"id": "hx-cd-cx", "name": "成都春熙路店", "city": "成都", "aliases":["春熙路店","四川成都春熙路店"], "status":"active"},
    {"id": "hx-sz-ft", "name": "深圳福田店", "city": "深圳", "district":"福田区", "aliases":["福田店","深圳福田"], "status":"active"},
]

# 学习场景参数，不是行业均值：规模、经营效率、营业时段与投放策略分别设置。
PROFILES = [
    {"scale":1.05,"traffic":.95,"ratio":.102,"open":0,"close":24,"ad_close":24,"label":"全天营业 · 稳定热销"},
    {"scale":.36,"traffic":1.12,"ratio":.132,"open":8,"close":24,"ad_close":23,"label":"日间营业 · 成交一般"},
    {"scale":.085,"traffic":1.55,"ratio":.192,"open":9,"close":23,"ad_close":20,"label":"晚间停投 · 转化偏弱"},
    {"scale":.22,"traffic":1.08,"ratio":.116,"open":8,"close":24,"ad_close":22,"label":"日间营业 · 平稳经营"},
    {"scale":1.48,"traffic":.86,"ratio":.096,"open":0,"close":24,"ad_close":24,"label":"全天营业 · 高成交"},
    {"scale":.065,"traffic":1.8,"ratio":.238,"open":10,"close":22,"ad_close":20,"label":"晚间停投 · 高费用低成交"},
]


def generate_stores(root, base):
    """构造6店经营快照和推广账本；店名是可改学习配置，不声称真实门店或后台记录。"""
    directory = Path(root) / "data/stores"
    directory.mkdir(parents=True, exist_ok=True)
    for index, config in enumerate(STORES):
        document = json.loads(json.dumps(base))
        context = document["business_context"]
        context.update(store_id=config["id"], store_name=config["name"], brand="花仙女")
        profile = PROFILES[index]
        config = {**config, "operating_profile": profile["label"], "open_hour": profile["open"], "close_hour": profile["close"]}
        context.update(operating_profile=profile["label"], operating_hours=f'{profile["open"]}:00–{profile["close"]}:00')
        factor = profile["scale"]
        scale_rng = random.Random(4110 + index)
        # 店内不同日期/商品不只做等比例缩放，访客与成交分开缩放以体现转化差异。
        day_factors = {day: scale_rng.uniform(.82,1.18) for day in sorted({r["date"] for r in document["sales"]})}
        sku_factors = {sku: scale_rng.uniform(.72,1.28) for sku in sorted({r["sku_id"] for r in document["sales"]})}
        for table, fields in {"sales": ["paid_orders", "paid_buyers", "units_sold"], "traffic": ["visitors"]}.items():
            for row in document[table]:
                if row["status"] != "ok":
                    continue
                old_units = row.get("units_sold", 0)
                row_factor = factor * day_factors[row["date"]] * sku_factors[row["sku_id"]]
                if table == "traffic":row_factor *= profile["traffic"]
                for field in fields:
                    row[field] = round(row[field] * row_factor)
                if table == "sales":
                    if row["units_sold"]:
                        row["paid_orders"] = max(1,min(row["units_sold"],row["paid_orders"]))
                        row["paid_buyers"] = max(1,min(row["paid_orders"],row["paid_buyers"]))
                    row["paid_revenue"] = round(row["paid_revenue"] * row["units_sold"] / old_units, 2) if old_units else 0
        traffic = {(r["sku_id"], r["date"]): r.get("visitors") for r in document["traffic"]}
        for row in document["traffic_channels"]:
            uv = traffic[row["sku_id"], row["date"]]
            original = row["attributed_visitors"]
            keys = list(original)
            values = [int(uv * original[k] / max(1,sum(original.values()))) for k in keys[:-1]]
            row["attributed_visitors"] = dict(zip(keys, values + [max(0, uv - sum(values))]))
        document['order_cohorts']=generate_order_cohorts(document)
        path = directory / (config["id"] + ".json")
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if index == 0:
            (Path(root) / "data/sample.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ledger = generate_advertising(document, index)
        (directory / (config["id"] + "-ads.json")).write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (Path(root) / "data/stores.json").write_text(json.dumps({"brand": "花仙女", "default_store": STORES[0]["id"],
        "stores": STORES}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def allocate(total, weights):
    """按权重分配整数，尾差用最大余数法分摊，保留金额/订单/访客合计。"""
    denominator = sum(weights)
    if not denominator:return [0] * len(weights)
    amounts = [int(total * weight / denominator) for weight in weights]
    order = sorted(range(len(weights)), key=lambda i: total * weights[i] / denominator - amounts[i], reverse=True)
    for i in order[:total-sum(amounts)]:amounts[i] += 1
    return amounts


def generate_advertising(document, index, *, seed=None):
    """单店小时经营/点击账本：全天店低价夜投，小店晚间停投仍可自然成交。

    非营业时段显式标记closed，已知未营业不冒充缺失采集；真正缺小时仍由加载器拒绝。
    支付、访客分配保持日级合计，日内分布仅为学习场景，不是观测到的小时事实。
    """
    rng = random.Random(8800 + index if seed is None else seed)
    profile = PROFILES[index]
    store_id = document["business_context"]["store_id"]
    daily, orders, visitors = {}, {}, {}
    for row in document["sales"]:
        day=row["date"]
        daily[day] = daily.get(day, 0) + round(row["paid_revenue"] * 100)
        orders[day] = orders.get(day, 0) + row.get("paid_orders",0)
    for row in document["traffic"]:
        visitors[row["date"]] = visitors.get(row["date"],0) + (row.get("visitors") or 0)
    ledger = {"schema_version": 1, "sample_data": True, "store_id": store_id, "revenue": [], "ads": [],
        "operating_profile": profile,
        "provenance": "学习用合成小时账本；非真实门店推广后台记录"}
    # 午饭、下午茶、晚饭三段高峰；凌晨需求较少，但全天营业店仍保留订单和投放。
    demand = [.16,.10,.07,.05,.05,.08,.14,.25,.43,.63,.9,1.8,2.25,1.6,1.95,2.1,1.3,1.85,2.5,1.7,1.1,.72,.44,.28]
    for day, total in sorted(daily.items()):
        weights = [demand[h]*rng.uniform(.84,1.16) if profile["open"]<=h<profile["close"] else 0 for h in range(24)]
        paid = allocate(orders[day],weights)
        # 没有支付订单的小时不分配销售额；自然访客可以出现而没有成交。
        amounts = allocate(total,[weights[h] if paid[h] else 0 for h in range(24)])
        uv = [paid[h]+extra for h,extra in enumerate(allocate(max(0,visitors.get(day,0)-sum(paid)),weights))]
        enabled = [profile["open"]<=h<profile["ad_close"] for h in range(24)]
        ad_weights = [demand[h] * (0.32 if h<7 else 2.1 if h==18 else .3 if h==9 else 1) * rng.uniform(.8,1.2) if enabled[h] else 0 for h in range(24)]
        budgets = allocate(round(total*profile["ratio"]*rng.uniform(.93,1.08)),ad_weights)
        for hour in range(24):
            operating = profile["open"]<=hour<profile["close"]
            ledger["revenue"].append({"store_id":store_id,"date":day,"hour":hour,"revenue_cents":amounts[hour],
                "paid_orders":paid[hour],"visitors":uv[hour],"operating":operating})
            share = rng.uniform(.32,.58)
            for mode in ("manual","automatic"):
                # 深夜降价而非一律停投；每次点击保持真实的一元多/两元多分币口径。
                cpc = rng.choice([110,120,135]) if hour<7 else rng.choice([120,150,180,220,260])
                budget = budgets[hour]*(share if mode=="manual" else 1-share)
                clicks = max(1,round(budget/cpc)) if enabled[hour] and budgets[hour] else 0
                ledger["ads"].append({"store_id":store_id,"date":day,"hour":hour,"mode":mode,
                    "enabled":enabled[hour],"unit_cpc_cents":cpc,"clicks":clicks,
                    "impressions":clicks*rng.randint(9,31)+(rng.randint(9,41) if enabled[hour] else 0),
                    "spend_cents":clicks*cpc})
    return ledger


class StoreHub:
    """每店一个服务实例和队列。逻辑隔离不是账号权限隔离，当前口令可访问配置内门店。"""

    def __init__(self, root, state_dir, output, *, start_worker=True, annual_version=None):
        """只读取固定配置下的文件，路径由白名单店铺ID决定，不接受客户端任意路径。"""
        root = Path(root)
        config = json.loads((root / "data/stores.json").read_text(encoding="utf-8"))
        self.default_query_scope=config.get('default_query_scope','current')
        if self.default_query_scope not in ('all','current'):raise ValueError('默认门店查询范围无效。')
        # 只有完整生成并发布的年度版本才切换；旧JSON保留为小规模回归样本。
        annual=None
        active_path=root/'data/annual/active.json'
        if annual_version is not None or active_path.exists():
            # 内部评测可显式绑定历史版本；不改经营工作台的active指针。
            version=annual_version if annual_version is not None else json.loads(active_path.read_text(encoding='utf-8'))['version']
            if not re.fullmatch(r'[a-f0-9]{12}',version):raise ValueError('年度版本ID不合法。')
            annual=root/'data/annual'/version
            if not (annual/'manifest.json').exists():raise ValueError('年度版本未完整发布。')
        from .store_resolution import StoreDirectory
        self.directory=StoreDirectory(config['stores'])
        self.stores, self.default_id = list(self.directory.stores.values()), config["default_store"]
        if self.default_id not in self.directory.stores or self.directory.stores[self.default_id]['status']!='active':raise ValueError('默认店铺必须存在且处于启用状态。')
        self.services, self.books = {}, {}
        try:
            for shop in self.stores:
                sid = shop["id"]
                if not re.fullmatch(r"[a-z0-9-]{1,40}", sid) or sid in self.services:
                    raise ValueError("店铺ID不合法或重复。")
                # 第一店沿用旧学习工作区，历史任务的冻结快照保持不变。
                state = Path(state_dir) if sid == self.default_id else Path(state_dir) / "stores" / sid
                target = Path(output) if sid == self.default_id else Path(output) / "stores" / sid
                data_path=annual/(sid+'.sqlite3') if annual else root/f'data/stores/{sid}.json'
                catalog_path=annual/'products.json' if annual else root/'data/products.json'
                self.services[sid] = InvestigationService(data_path,catalog_path,state,target,start_worker=start_worker)
                self.books[sid] = load_advertising(annual/(sid+'-ads.json') if annual else root/f'data/stores/{sid}-ads.json', sid)
                self.services[sid].operations.book=self.books[sid]
        except Exception:
            self.close()
            raise
        if self.default_id not in self.services:
            self.close()
            raise ValueError("默认店铺不存在。")

    def for_store(self, store_id=None):
        """每次请求选择固定实例，不修改全局选店状态，因此并发切店也不串库。"""
        sid = store_id or self.default_id
        if sid not in self.services or self.directory.stores[sid]['status']!='active':
            raise ValueError("未知店铺，请从店铺列表选择。")
        return self.services[sid]

    def close(self):
        """逐店关闭后台线程；数据库文件与历史报告保留。"""
        for service in self.services.values():
            service.close()
