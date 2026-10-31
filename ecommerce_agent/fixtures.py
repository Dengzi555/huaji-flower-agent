"""闪购鲜花合成案例：标题、花材规格、成品销售与缺货观察，不是实际花店数据。"""

FLOWER_PRODUCTS = [
    {"sku_id": "FLR-ROSE-99-BOUQUET", "short_name": "炽热爱意", "stem_count": 99, "variety": "红玫瑰", "product_type": "花束", "sample_unit_price": 399,
     "name": "【炽热爱意】99朵红玫瑰花束送女友送爱人生日礼物生日鲜花花店鲜花上门情人节鲜花",
     "variant": "99朵红玫瑰·花束", "occasion_keywords": ["生日礼物", "生日鲜花", "情人节鲜花"],
     "aliases": ["炽热爱意", "99朵红玫瑰花束", "99朵红玫瑰", "99朵玫瑰", "红玫瑰花束", "玫瑰花束", "红玫瑰", "玫瑰"]},
    {"sku_id": "FLR-ROSE-19-BOUQUET", "short_name": "怦然心动", "stem_count": 19, "variety": "红玫瑰", "product_type": "花束", "sample_unit_price": 129,
     "name": "【怦然心动】19朵红玫瑰花束送女友生日礼物生日花束情人节鲜花同城配送",
     "variant": "19朵红玫瑰·花束", "occasion_keywords": ["生日礼物", "生日花束", "情人节鲜花"],
     "aliases": ["怦然心动", "19朵红玫瑰花束", "19朵红玫瑰", "19朵玫瑰", "红玫瑰花束", "玫瑰花束", "红玫瑰", "玫瑰"]},
    {"sku_id": "FLR-CARNATION-19-BOUQUET", "short_name": "温柔时光", "stem_count": 19, "variety": "粉康乃馨", "product_type": "花束", "sample_unit_price": 169,
     "name": "【温柔时光】19朵粉康乃馨花束送妈妈生日礼物生日花束母亲节鲜花同城配送",
     "variant": "19朵粉康乃馨·花束", "occasion_keywords": ["生日礼物", "生日花束", "母亲节鲜花"],
     "aliases": ["温柔时光", "19朵粉康乃馨花束", "19朵康乃馨", "粉康乃馨", "粉色康乃馨", "康乃馨花束", "康乃馨"]},
    {"sku_id": "FLR-ROSE-33-BUCKET", "short_name": "心动满怀", "stem_count": 33, "variety": "红玫瑰", "product_type": "抱抱桶", "sample_unit_price": 269,
     "name": "【心动满怀】33朵红玫瑰抱抱桶送女友生日礼物生日花束情人节鲜花闪购配送",
     "variant": "33朵红玫瑰·抱抱桶", "occasion_keywords": ["生日礼物", "生日花束", "情人节鲜花"],
     "aliases": ["心动满怀", "33朵红玫瑰抱抱桶", "33朵玫瑰", "红玫瑰抱抱桶", "玫瑰抱抱桶", "红玫瑰", "玫瑰"]},
    {"sku_id": "FLR-SUNFLOWER-11-BUCKET", "short_name": "向阳而生", "stem_count": 11, "variety": "向日葵", "product_type": "抱抱桶", "sample_unit_price": 199,
     "name": "【向阳而生】11朵向日葵抱抱桶生日礼物生日花束毕业季鲜花同城配送",
     "variant": "11朵向日葵·抱抱桶", "occasion_keywords": ["生日礼物", "生日花束", "毕业季鲜花"],
     "aliases": ["向阳而生", "11朵向日葵抱抱桶", "11朵向日葵", "向日葵抱抱桶", "向日葵"]},
]


def sample_catalog():
    """目录保存完整营销标题与可核验规格；场景词单独保存，不作为唯一商品别名。"""
    import copy
    return {"schema_version": 1, "products": copy.deepcopy(FLOWER_PRODUCTS)}


CASES = {
    "FLR-ROSE-99-BOUQUET": "缺货时长增加，销量下降",
    "FLR-ROSE-19-BOUQUET": "流量与销量同降，支付转化率不变",
    "FLR-CARNATION-19-BOUQUET": "活动结束，销量回落",
    "FLR-ROSE-33-BUCKET": "库存缺失、流量延迟",
    "FLR-SUNFLOWER-11-BUCKET": "整日无货记录与成交冲突",
}


def sample_data():
    """生成五个 SKU、两个自然日的数据；固定日期保证教学材料和测试一致。"""
    data = {"schema_version": 1, "sample_data": True, "timezone": "Asia/Shanghai", "currency": "CNY",
            "business_context": {"scenario": "flash_flowers", "label": "闪购鲜花", "sales_unit": "件（每件为一束花束或一个抱抱桶）",
                "inventory_scope": "成品SKU日级可售监测，不代表花材枝数或实时库存",
                "limitations": "合成教学数据；未模拟配送范围、履约时效、营业时段或鲜花损耗"},
            "sales": [], "traffic": [], "inventory": [], "promotions": [], "traffic_channels": []}
    for sku in CASES:
        for index, day in enumerate(("2026-10-22", "2026-10-23")):
            common = {"sku_id": sku, "date": day, "observed_at": "2026-10-24T08:00:00+08:00", "status": "ok"}
            orders = 100 if index == 0 else 50
            # 流量案例让买家数与访客数同比例下降，因此转化率保持 10%。
            visitors = 1000
            if sku == "FLR-ROSE-19-BOUQUET" and index:
                visitors = 500
            stockout = 600 if sku == "FLR-ROSE-99-BOUQUET" and index else 0
            if sku == "FLR-SUNFLOWER-11-BUCKET" and index:
                # 刻意保留“整日无货却有成交”的冲突，检验程序不会强行归因。
                stockout = 1440
            price = next(product["sample_unit_price"] for product in FLOWER_PRODUCTS if product["sku_id"] == sku)
            data["sales"].append({**common, "source_id": "synthetic-flower-orders", "paid_orders": orders,
                "paid_buyers": orders, "units_sold": orders, "paid_revenue": orders * price})
            traffic = {**common, "source_id": "synthetic-traffic", "visitors": visitors}
            if sku == "FLR-ROSE-33-BUCKET" and index:
                traffic = {**common, "source_id": "synthetic-traffic", "status": "delayed",
                           "reason": "Simulated delayed analytics export"}
            data["traffic"].append(traffic)
            if sku == "FLR-ROSE-19-BOUQUET":
                data["traffic_channels"].append({**common, "source_id": "synthetic-exclusive-channel-attribution",
                    "attribution_policy": "exclusive_last_touch_v1",
                    "attributed_visitors": {"自然搜索": 500 if index == 0 else 150,
                                            "付费广告": 300 if index == 0 else 150, "推荐": 200}})
            if not (sku == "FLR-ROSE-33-BUCKET" and index):
                data["inventory"].append({**common, "source_id": "synthetic-inventory",
                    "out_of_stock_minutes": stockout, "monitored_minutes": 1440})
            data["promotions"].append({**common, "source_id": "synthetic-promotions",
                "active": sku == "FLR-CARNATION-19-BOUQUET" and index == 0,
                "campaign_name": "模拟闪购鲜花满减券" if sku == "FLR-CARNATION-19-BOUQUET" and index == 0 else ""})
    return data
