"""组合查询交付应回答经营问题：窗口外历史不计总量，缺口只提示一次，事实可复算。"""
import unittest
from ecommerce_agent.combined_query import summarize


class CombinedAnswerTests(unittest.TestCase):
    def fixture(self):
        task={'store_name':'重庆南岸店','target_date':'2026-10-31','is_window':True,'trend_start':'2026-10-25',
              'query_plan':{'start_date':'2026-10-25','end_date':'2026-10-31'},
              'product_scope':{'label':'全部向日葵'},'candidates':[{'sku_id':'a','short_name':'晴日有你','variant':'5朵向日葵·花束'}]}
        trend={'start_date':'2026-10-25','end_date':'2026-10-31','summary':{'complete_window':False,'units_sum':None},
            'records':[{'date':'2026-10-24','status':'ok','units_sold':100},
                       {'date':'2026-10-25','status':'ok','units_sold':1},
                       {'date':'2026-10-30','status':'ok','units_sold':6},
                       {'date':'2026-10-31','status':'unavailable','units_sold':None}]}
        data={'trend':trend,'records':[],'items':[{'sku_id':'a','name':'晴日有你','variant':'5朵向日葵·花束','units_sold':7}],'ranking':None}
        sales=summarize('sales',data,'sales-source')
        inventory={'domain':'inventory','status':'partial','records':[{'sku_id':'a','date':'2026-10-30','status':'ok','out_of_stock_minutes':90},
                        {'sku_id':'a','date':'2026-10-31','status':'unavailable'}],'evidence_id':'inventory-source'}
        promotions={'domain':'promotions','status':'partial','records':[{'sku_id':'a','date':'2026-10-30','status':'ok','active':True,'campaign_name':'周五鲜花券'},
                        {'sku_id':'a','date':'2026-10-31','status':'unavailable'}],'evidence_id':'promotion-source'}
        return task,[sales,inventory,promotions]

    def test_partial_total_excludes_comparison_history(self):
        _,sections=self.fixture()
        self.assertEqual(sections[0]['display_metrics'][0]['value'],7)

    def test_answer_contains_sales_product_stockout_and_campaign_details(self):
        from ecommerce_agent.combined_answer import build_answer
        task,sections=self.fixture();answer=build_answer(task,sections)
        text=str(answer)
        self.assertIn('7束',text);self.assertIn('6束',text)
        self.assertIn('晴日有你',text);self.assertIn('90分钟',text);self.assertIn('周五鲜花券',text)
        self.assertNotIn('107束',text);self.assertNotIn('SKU日',text)
        self.assertEqual(answer['missing_dates'],['2026-10-31'])
        self.assertTrue(answer['next_steps'])

    def test_failure_is_explained_without_discarding_success(self):
        from ecommerce_agent.combined_answer import build_answer
        task,sections=self.fixture();sections[1]={'domain':'inventory','status':'failed','records':[]}
        answer=build_answer(task,sections)
        self.assertIn('暂未查到',str(answer));self.assertIn('7束',str(answer))

    def test_missing_sales_does_not_become_zero(self):
        from ecommerce_agent.combined_answer import build_answer
        task,sections=self.fixture()
        sections[0]['trend']['records']=[{'date':'2026-10-31','status':'unavailable','units_sold':None}]
        answer=build_answer(task,sections)
        self.assertIn('暂未查到',answer['parts'][0]['paragraphs'][0])
        self.assertNotIn('售出0',str(answer))

    def test_stockout_excludes_history_and_keeps_product_identity(self):
        from ecommerce_agent.combined_answer import build_answer
        task,sections=self.fixture()
        sections[1]['records'].append({'sku_id':'a','date':'2026-10-24','status':'ok','out_of_stock_minutes':800})
        answer=build_answer(task,sections)
        text=' '.join(answer['parts'][1]['paragraphs'])
        self.assertIn('90分钟',text);self.assertNotIn('890',text)
        self.assertIn('5朵向日葵',text)


if __name__=='__main__':unittest.main()
