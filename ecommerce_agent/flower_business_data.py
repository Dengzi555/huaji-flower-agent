"""鲜花经营学习数据集：多品种、生命周期、非整百计数和可复现日级经营变化。

这是合成数据生成器，不抓取真实店铺数据。固定种子和日期便于复盘，
与 fixtures.py 中用于精确算术回归的小案例分开维护。
"""

import copy
import random
from datetime import date, timedelta

from .fixtures import sample_catalog
from .business_calendar import day_context


def business_catalog():
    """生成经营目录；系列/商品名为店铺命名，不能当作植物学品种认证。"""
    products = sample_catalog()["products"]
    for product in products:
        product.update(listed_at="2026-09-15", business_tags=["常规款"], series="经典鲜花")
    products[0]["business_tags"] = ["爆款"]
    # 图片参考花材名称；用户提及的系列另行维护，未采用图片批发价计算零售价。
    specs = [
        ("WUMEI-33-BUCKET", "暮色微醺", 33, "乌梅玫瑰", "抱抱桶", 269.90, "乌梅系列", "常规款", "2026-09-15"),
        ("WUMEI-19-BOUQUET", "莓好如初", 19, "乌梅玫瑰", "花束", 159.90, "乌梅系列", "常规款", "2026-09-15"),
        ("EUSTOMA-11-BOUQUET", "云间来信", 11, "白洋桔梗", "花束", 119.90, "洋桔梗系列", "新品", "2026-10-25"),
        ("EUSTOMA-19-BOUQUET", "紫意悠然", 19, "紫洋桔梗", "花束", 169.90, "洋桔梗系列", "常规款", "2026-09-15"),
        ("APPLEJACK-19-BOUQUET", "青野来信", 19, "苹果杰克", "花束", 179.90, "苹果杰克系列", "常规款", "2026-09-15"),
        ("APPLEJACK-33-BUCKET", "绿意漫生", 33, "苹果杰克", "抱抱桶", 289.90, "苹果杰克系列", "长尾款", "2026-09-15"),
        ("DOPAMINE-19-BOUQUET", "缤纷心动", 19, "混色玫瑰", "花束", 139.90, "多巴胺系列", "爆款", "2026-09-15"),
        ("DOPAMINE-33-BUCKET", "夏日缤纷", 33, "混色玫瑰", "抱抱桶", 239.90, "多巴胺系列", "常规款", "2026-09-15"),
        ("PURPLE-19-BOUQUET", "紫梦星河", 19, "紫玫瑰", "花束", 159.90, "紫玫系列", "常规款", "2026-09-15"),
        ("PURPLE-33-BUCKET", "紫夜星辰", 33, "紫玫瑰", "抱抱桶", 259.90, "紫玫系列", "长尾款", "2026-09-15"),
        ("ANGEL-19-BOUQUET", "纯白心语", 19, "白玫瑰", "花束", 169.90, "天使之翼", "常规款", "2026-09-15"),
        ("GRAPE-19-BOUQUET", "晴光微醺", 19, "混色玫瑰", "花束", 189.90, "晴天葡萄", "长尾款", "2026-09-15"),
        ("CAPPUCCINO-19-BOUQUET", "暖调私语", 19, "卡布奇诺玫瑰", "花束", 199.90, "复古系列", "常规款", "2026-09-15"),
        ("MANTA-19-BOUQUET", "柔雾晨光", 19, "曼塔玫瑰", "花束", 179.90, "复古系列", "长尾款", "2026-09-15"),
        ("OCEAN-19-BOUQUET", "海风轻语", 19, "海洋之歌玫瑰", "花束", 189.90, "紫调系列", "常规款", "2026-09-15"),
        ("ZIXIA-19-BOUQUET", "霞光入梦", 19, "紫霞仙子玫瑰", "花束", 179.90, "紫调系列", "长尾款", "2026-09-15"),
        ("SNOW-19-BOUQUET", "雪落清欢", 19, "白雪山玫瑰", "花束", 159.90, "纯白系列", "常规款", "2026-09-15"),
        ("SUNFLOWER-5-BOUQUET", "晴日有你", 5, "向日葵", "花束", 79.90, "向阳系列", "常规款", "2026-09-15"),
        ("LILY-6-BOUQUET", "百事合意", 6, "白百合", "花束", 129.90, "百合系列", "常规款", "2026-09-15"),
        ("HYDRANGEA-3-BOUQUET", "蓝屿清风", 3, "蓝绣球", "花束", 139.90, "绣球系列", "长尾款", "2026-09-15"),
        ("PINGPONG-9-BOUQUET", "团团圆满", 9, "白乒乓菊", "花束", 99.90, "清新系列", "长尾款", "2026-09-15"),
        ("LISIANTHUS-19-BUCKET", "春野轻语", 19, "粉洋桔梗", "抱抱桶", 179.90, "洋桔梗系列", "新品", "2026-10-24"),
        ("CARNATION-11-BOUQUET", "念念温柔", 11, "粉康乃馨", "花束", 89.90, "感恩系列", "长尾款", "2026-09-15"),
    ]
    for suffix, short, stems, variety, kind, price, series, tag, listed in specs:
        aliases = [short, variety, series, f"{stems}朵{variety}{kind}", f"{variety}{kind}"]
        if "洋桔梗" in variety:
            aliases.append("洋桔梗")
        products.append({"sku_id": "FLR-" + suffix, "short_name": short, "stem_count": stems,
            "variety": variety, "product_type": kind, "sample_unit_price": price,
            "series": series, "business_tags": [tag], "listed_at": listed,
            "name": f"【{short}】{stems}朵{variety}{kind}{series}生日礼物生日花束同城鲜花上门",
            "variant": f"{stems}朵{variety}·{kind}", "aliases": sorted(set(aliases)),
            "occasion_keywords": ["生日礼物", "生日花束", "同城鲜花上门"]})
    # 闪购客单以平价和百元款为主，少量大规格保留溢价；不使用批发单枝价充当成品价。
    prices=[159.9,59.9,49.9,99.9,69.9,119.9,69.9,59.9,79.9,89.9,129.9,59.9,99.9,
            69.9,109.9,79.9,89.9,99.9,79.9,89.9,79.9,69.9,49.9,69.9,79.9,59.9,89.9,49.9]
    for product,price in zip(products,prices):product['sample_unit_price']=price
    return {"schema_version": 1, "products": products}


def business_data():
    """生成2026年9月15日至10月27日的经营快照，覆盖假期与调休；未上架不生成记录。"""
    rng = random.Random(20261026)
    data = {"schema_version": 1, "sample_data": True, "timezone": "Asia/Shanghai", "currency": "CNY",
        "business_context": {"scenario": "flash_flowers", "label": "闪购鲜花", "dataset_id": "flower-business-v07",
            "provenance": "学习用合成数据，非真实店铺记录；花材参考用户提供图片及命名要求",
            "snapshot_as_of": "2026-10-28", "sales_unit": "件（成品花束/抱抱桶），UV/订单/件数均为整数",
            "inventory_scope": "成品SKU日级监测，非花材枝数或实时库存",
            "limitations": "仅日级经营快照，不含配送半径、履约时效、损耗与成本核算"},
        "sales": [], "traffic": [], "inventory": [], "promotions": [], "traffic_channels": []}
    for product in business_catalog()["products"]:
        sku, tags = product["sku_id"], product["business_tags"]
        for offset in range(43):
            current = date(2026, 9, 15) + timedelta(days=offset)
            day = current.isoformat()
            if day < product["listed_at"]:
                continue
            # 周末曝光更高；爆款/常规/长尾分层，但日波动不复制同一组数字。
            hot, tail = "爆款" in tags, "长尾款" in tags
            visitors = rng.randint(467, 1287) if hot else rng.randint(7, 97) if tail else rng.randint(63, 389)
            calendar=day_context(day)
            # 合成场景中设定不同曝光背景用于学习，不把这套系数当真实节日效果。
            if calendar['day_type']=='holiday_break':visitors=round(visitors*1.45)
            elif calendar['day_type']=='weekend_rest':visitors=round(visitors*1.19)
            conversion = rng.uniform(.025, .068) if hot else rng.uniform(0, .024) if tail else rng.uniform(.012, .048)
            orders = int(visitors * conversion)
            stockout, monitored = rng.choice([0, 0, 0, 13, 27, 51]), 1440
            active = current.weekday() in (4, 5)
            campaign = "周末鲜花满减券" if active else ""
            if calendar['holiday_name']=='国庆节':active=True;campaign='国庆鲜花满减'
            elif calendar['holiday_name']=='中秋节':active=True;campaign='中秋团圆鲜花券'
            # 可复现的业务案例：数值变化提供观察线索，不预写因果结论。
            if sku == "FLR-ROSE-99-BOUQUET" and day in ("2026-10-24", "2026-10-25"):
                visitors, orders, stockout = (1267, 61, 17) if day.endswith("24") else (1193, 24, 487)
            if sku == "FLR-WUMEI-33-BUCKET" and day in ("2026-10-24", "2026-10-25"):
                visitors, orders, stockout = (387, 17, 0) if day.endswith("24") else (143, 6, 0)
            if sku == "FLR-ROSE-19-BOUQUET" and day in ("2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"):
                visitors, orders = (863, 37) if day.endswith(("22", "24")) else (341, 14)
            if sku == "FLR-SUNFLOWER-11-BUCKET" and day in ("2026-10-23", "2026-10-25"):
                stockout, orders = 1440, 7  # 留一条明确冲突，训练人工核验而非掩盖脏数据。
            if sku == "FLR-GRAPE-19-BOUQUET":
                orders = 0  # 有曝光但连续零成交；不能把有货或标题好看当作有销量。
            if sku == "FLR-HYDRANGEA-3-BOUQUET" and day == "2026-10-25":
                visitors, orders = 0, 0  # 无曝光零成交，转化率分母为零应保持未知。
            if sku == "FLR-EUSTOMA-11-BOUQUET":
                visitors, orders, stockout = 137, 3, 0
            buyers = max(1, orders - rng.choice([0, 0, 0, 1])) if orders else 0
            units = orders + (rng.randint(0, max(1, orders // 9)) if orders else 0)
            revenue = round(units * product["sample_unit_price"] * rng.uniform(.87, .985), 2)
            common = {"sku_id": sku, "date": day, "observed_at": "2026-10-28T08:15:00+08:00", "status": "ok"}
            data["sales"].append({**common, "source_id": "flower-order-daily", "paid_orders": orders,
                "paid_buyers": buyers, "units_sold": units, "paid_revenue": revenue})
            delayed = sku == "FLR-ROSE-33-BUCKET" and day in ("2026-10-23", "2026-10-25")
            data["traffic"].append({**common, "source_id": "flower-traffic-daily", **(
                {"status": "delayed", "reason": "流量日报尚未完成回传"} if delayed else {"visitors": visitors})})
            if not (sku == "FLR-ROSE-33-BUCKET" and day in ("2026-10-23", "2026-10-25")):
                quantities = {"stock_on_hand": 2, "safety_stock": 8} if (
                    sku == "FLR-ROSE-99-BOUQUET" and day == "2026-10-25") else {}
                data["inventory"].append({**common, "source_id": "flower-availability-daily",
                    "out_of_stock_minutes": stockout, "monitored_minutes": monitored, **quantities})
            activity = {}
            if sku == "FLR-ROSE-99-BOUQUET" and day == "2026-10-24":
                active, campaign = True, "情人节大促满减"
                activity = {"campaign_ended_at": "2026-10-24T18:00:00+08:00", "resource_slot_active": True}
            elif sku == "FLR-ROSE-99-BOUQUET" and day == "2026-10-25":
                active, campaign = False, ""
                activity = {"resource_slot_active": False}
            data["promotions"].append({**common, "source_id": "flower-campaign-daily", "active": active,
                                       "campaign_name": campaign, **activity})
            if not delayed:
                search = int(visitors * rng.uniform(.38, .59))
                ads = int(visitors * rng.uniform(.12, .29))
                data["traffic_channels"].append({**common, "source_id": "flower-channel-daily",
                    "attribution_policy": "exclusive_last_touch_v1",
                    "attributed_visitors": {"自然搜索": search, "付费广告": ads, "推荐": visitors - search - ads}})
    return copy.deepcopy(data)
