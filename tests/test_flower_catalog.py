"""闪购鲜花标题回归：花名、朵数、品种、类型与 SEO 场景词各有不同职责。"""

import copy
import unittest
from datetime import date

from ecommerce_agent.cli import ROOT
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import FLOWER_PRODUCTS, sample_catalog, sample_data
from ecommerce_agent.task_parser import ProductCatalog, parse_task


class FlowerCatalogTests(unittest.TestCase):
    """不启动模型或外部平台，以明确目录验证候选与澄清边界。"""

    def setUp(self):
        """使用与文件生成器一致的鲜花目录和模拟日级观察。"""
        self.store = DataStore.from_document(sample_data())
        self.catalog = ProductCatalog.from_document(sample_catalog(), self.store.skus)

    def parse(self, question, **kwargs):
        """固定今天为24日，样例23日是已结束的自然日。"""
        return parse_task(question, self.catalog, self.store, today=date(2026, 10, 24), **kwargs)

    def test_full_marketing_titles_resolve_all_five_products(self):
        """长标题中重复的生日/节日词不会冲淡有效花名，也不会制造多意图。"""
        for product in FLOWER_PRODUCTS:
            with self.subTest(product=product["short_name"]):
                task = self.parse(product["name"] + "2026-10-23销量下降原因")
                self.assertEqual(task["status"], "ready", task)
                self.assertEqual(task["sku_id"], product["sku_id"])
                self.assertEqual(task["product_resolution"]["matched_by"], "flower_name")

    def test_original_user_title_and_missing_date(self):
        """用户给出的原标题能识别，但没有时间时仍只澄清，不执行。"""
        title = "【炽热爱意】99朵红玫瑰花束送女友送爱人生日礼物生日鲜花花店鲜花上门"
        task = self.parse(title + "销量下降原因")
        self.assertEqual(task["sku_id"], "FLR-ROSE-99-BOUQUET")
        self.assertEqual(task["status"], "needs_clarification")
        self.assertIsNone(task["target_date"])
        self.assertEqual(self.parse(title + "2026-10-23销量下降原因")["status"], "ready")

    def test_name_and_specification_are_conjunctive_not_competing_scores(self):
        """明确花名与数量/品种/类型矛盾，不能偷偷改选另一个目录商品。"""
        for text in ("炽热爱意19朵红玫瑰花束", "炽热爱意99朵白玫瑰花束", "炽热爱意99朵红玫瑰抱抱桶"):
            task = self.parse(text + "2026-10-23销量下降原因", sku_override="FLR-ROSE-99-BOUQUET")
            self.assertEqual(task["status"], "needs_clarification", text)
            self.assertTrue(any("不一致" in issue for issue in task["issues"]))

    def test_name_takes_priority_over_shared_flower_alias(self):
        """花名已经明确时，“红玫瑰”不会把同品种的其他规格重新拉进候选。"""
        matches = self.catalog.match("怦然心动红玫瑰花束")
        self.assertEqual([p["sku_id"] for p in matches], ["FLR-ROSE-19-BOUQUET"])

    def test_ambiguous_variety_and_container_resolution(self):
        """玫瑰有三款，红玫瑰花束有两款；明确朵数或抱抱桶类型才能进一步缩小。"""
        self.assertEqual(len(self.catalog.match("红玫瑰")), 3)
        self.assertEqual(len(self.catalog.match("红玫瑰花束")), 2)
        self.assertEqual(self.catalog.match("33朵红玫瑰抱抱桶")[0]["sku_id"], "FLR-ROSE-33-BUCKET")
        self.assertEqual(self.catalog.match("99朵花束")[0]["sku_id"], "FLR-ROSE-99-BOUQUET")
        self.assertEqual(self.parse("红玫瑰2026-10-23销量下降原因")["status"], "needs_clarification")

    def test_seo_words_are_not_identity_or_actual_promotion(self):
        """生日场景词不唯一定位商品；母亲节标题也不等于10月23日真的有活动。"""
        for word in ("生日礼物", "生日花束", "情人节鲜花", "母亲节鲜花"):
            self.assertEqual(self.catalog.match(word), [], word)
        product = next(p for p in FLOWER_PRODUCTS if p["short_name"] == "温柔时光")
        task = self.parse(product["name"] + "2026-10-23活动开启了吗")
        self.assertEqual(task["intent"], "query_promotions")
        self.assertFalse(self.store.query("promotions", product["sku_id"], "2026-10-23")["records"][-1]["active"])

    def test_bucket_title_with_birthday_bouquet_keyword_is_still_bucket(self):
        """抱抱桶标题包含“生日花束”时，类型取核心规格，不能被 SEO 词覆盖。"""
        product = next(p for p in FLOWER_PRODUCTS if p["short_name"] == "心动满怀")
        task = self.parse(product["name"] + "2026-10-23销量下降原因")
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["product_resolution"]["constraints"]["product_types"], ["抱抱桶"])

    def test_unknown_name_color_and_type_do_not_become_known_sku(self):
        """相似规格不能覆盖未登记花名或未供应颜色/容器。"""
        for query in ("【海誓山盟】99朵红玫瑰花束", "99朵白玫瑰花束", "99朵红玫瑰花篮"):
            self.assertEqual(self.parse(query + "2026-10-23销量下降原因")["status"], "needs_clarification", query)

    def test_flower_unit_and_per_product_revenue(self):
        """99朵是单件花材规格，售出100件不变成9900件；成交金额使用该款模拟单价。"""
        for product in FLOWER_PRODUCTS:
            row = self.store.query("sales", product["sku_id"], "2026-10-23")["records"][-1]
            self.assertEqual(row["units_sold"], 50)
            self.assertEqual(row["paid_revenue"], 50 * product["sample_unit_price"])
        self.assertEqual(self.store.meta["business_context"]["scenario"], "flash_flowers")

    def test_flower_name_containing_zhi_is_not_a_time_range(self):
        """花名中的“至”不是时间区间语法，不能直接判不支持。"""
        catalog = sample_catalog()
        catalog["products"][0]["short_name"] = "至臻浪漫"
        catalog["products"][0]["name"] = catalog["products"][0]["name"].replace("炽热爱意", "至臻浪漫")
        self.catalog = ProductCatalog.from_document(catalog, self.store.skus)
        self.assertEqual(self.parse("至臻浪漫2026-10-23销量下降原因")["status"], "ready")

    def test_catalog_rejects_invalid_spec_and_seo_alias(self):
        """目录也需要业务契约，布尔朵数和把生日礼物当唯一别名都拒绝。"""
        for modifier in ("count", "alias"):
            catalog = sample_catalog()
            if modifier == "count":
                catalog["products"][0]["stem_count"] = True
            else:
                catalog["products"][0]["aliases"].append("生日礼物")
            with self.assertRaises(ValueError):
                ProductCatalog.from_document(catalog, self.store.skus)


if __name__ == "__main__":
    unittest.main()
