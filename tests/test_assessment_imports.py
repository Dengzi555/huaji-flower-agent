"""综合分级与报表发布回归：业务反例、文件契约、跨店隔离和冻结快照。"""
import base64
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ecommerce_agent.assessment import assess
from ecommerce_agent.data import DataStore
from ecommerce_agent.fixtures import sample_catalog,sample_data
from ecommerce_agent.tabular import read_table,write_template
from ecommerce_agent.web_service import InvestigationService


class AssessmentTests(unittest.TestCase):
    """构造业务对照，不把学习策略阈值解释成行业结论。"""
    def evidence(self,before=100,after=40,history=(100,102,98),tier='常规款'):
        day='2026-10-27';base='2026-10-26'
        rows=[{'date':d,'status':'ok','units_sold':n,'campaign_active':False,'campaign_name':''} for d,n in zip(('2026-10-20','2026-10-13','2026-09-29'),history)]
        rows.append({'date':day,'status':'ok','units_sold':after,'campaign_active':False,'campaign_name':''})
        observations={'sales':{'ok':True,'evidence_id':'sales','data':{'target_date':day,'records':[{'date':base},{'date':day}]}},
            'sales_trend':{'ok':True,'evidence_id':'trend','data':{'records':rows}},
            'promotions':{'ok':True,'evidence_id':'promo','data':{'records':[{'status':'ok','active':False,'campaign_name':''}]*2}}}
        analysis={'facts':{'units_sold':{'before':before,'after':after,'relative_pct':(after-before)/before*100}},'gaps':[],'conflicts':[]}
        product={'listed_at':'2026-09-01','business_tags':[tier]}
        return analysis,observations,product

    def test_absolute_loss_not_only_percentage(self):
        small=self.evidence(2,1,(2,2,2));large=self.evidence()
        self.assertEqual(assess(*small)['code'],'normal')
        self.assertEqual(assess(*large)['code'],'attention')

    def test_tier_and_new_product(self):
        regular=self.evidence(10,5,(10,10,10));hot=self.evidence(10,5,(10,10,10),'爆款')
        self.assertEqual(assess(*regular)['code'],'attention');self.assertEqual(assess(*hot)['code'],'normal')
        regular[2]['listed_at']='2026-10-25';self.assertEqual(assess(*regular)['code'],'insufficient')

    def test_holiday_campaign_missing_and_conflict_priority(self):
        analysis,obs,product=self.evidence()
        obs['sales_trend']['data']['records'][0]['campaign_active']=True
        result=assess(analysis,obs,product);self.assertEqual(result['code'],'insufficient')
        self.assertEqual(len(result['history_excluded']),1)
        analysis['gaps']=[{'table':'traffic'}];self.assertEqual(assess(analysis,obs,product)['code'],'insufficient')
        analysis['conflicts']=[{'kind':'conflict'}];self.assertEqual(assess(analysis,obs,product)['code'],'conflict')

    def test_missing_history_never_invented_and_robust_band(self):
        analysis,obs,product=self.evidence(100,40,())
        self.assertEqual(assess(analysis,obs,product)['code'],'insufficient')
        analysis,obs,product=self.evidence(100,70,(95,100,105))
        result=assess(analysis,obs,product);self.assertEqual(result['reference']['median_units'],100)
        self.assertEqual(result['reference']['mad_units'],5);self.assertEqual(result['code'],'attention')

    def test_learning_history_not_mixed_with_user_imports(self):
        analysis,obs,product=self.evidence()
        obs['sales']['data']['sample_data']=True
        obs['sales_trend']['data']['records'][-1]['records']={'sales':{'source_id':'import:current'}}
        result=assess(analysis,obs,product)
        self.assertEqual(result['code'],'insufficient');self.assertEqual(len(result['history_used']),0)


class ImportTests(unittest.TestCase):
    """每条导入使用临时工作区，绝不改项目正在使用的业务文件。"""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.raw=sample_data();self.raw['business_context'].update(store_id='store-a',store_name='测试A店')
        (self.root/'data.json').write_text(json.dumps(self.raw),encoding='utf-8');(self.root/'products.json').write_text(json.dumps(sample_catalog()),encoding='utf-8')
        self.service=InvestigationService(self.root/'data.json',self.root/'products.json',self.root/'state',self.root/'runs',start_worker=False)
        self.addCleanup(self.service.close);self.importer=self.service.imports
        self.sku='FLR-ROSE-99-BOUQUET'

    def upload(self,text,kind='sales',filename='后台导出.csv'):
        return self.importer.upload({'kind':kind,'filename':filename,'content_base64':base64.b64encode(text.encode('utf-8-sig')).decode()})

    def csv(self,day='2026-10-23',extra=''):
        return f'日期,商品ID,支付订单数,支付买家数,支付件数,实付金额{extra}\n{day},{self.sku},10,9,12,123.45'+(',其他店' if extra else '')+'\n'

    def preview(self,stage,**kw):return self.importer.preview(stage['id'],{'mapping':stage['mapping'],**kw})
    def commit(self,stage,preview):return self.importer.commit(stage['id'],{'confirm':True,'preview_token':preview['preview_token']})

    def test_templates_csv_and_xlsx_roundtrip(self):
        for kind in ('sales','traffic','inventory','promotions','order_cohorts'):
            for fmt in ('csv','xlsx'):
                raw=self.importer.template(kind,fmt);table=read_table('template.'+fmt,raw)
                self.assertEqual(len(table['rows']),1);self.assertIn('日期',table['headers'])

    def test_auto_mapping_preview_atomic_commit_idempotency_and_restart(self):
        stage=self.upload(self.csv());original=self.service.store.snapshot();fingerprint=self.service.fingerprint
        preview=self.preview(stage);self.assertTrue(preview['can_commit']);self.assertEqual(preview['replaced_rows'],1)
        self.assertEqual(self.service.store.snapshot(),original)
        receipt=self.commit(stage,preview);self.assertFalse(receipt['reused']);self.assertNotEqual(self.service.fingerprint,fingerprint)
        self.assertEqual(self.service.store.query('sales',self.sku,'2026-10-23')['records'][-1]['paid_orders'],10)
        self.assertTrue(self.commit(stage,preview)['reused'])
        # 不能拿旧学习库存、流量和前日样例，为新导入的销售数据拼出完整结论。
        self.assertEqual(self.service.store.query('inventory',self.sku,'2026-10-23')['records'][-1]['status'],'unavailable')
        self.assertEqual(self.service.store.query('sales',self.sku,'2026-10-23')['records'][0]['status'],'unavailable')
        reopened=InvestigationService(self.root/'data.json',self.root/'products.json',self.root/'state',self.root/'runs2',start_worker=False)
        try:self.assertEqual(reopened.fingerprint,self.service.fingerprint)
        finally:reopened.close()
        self.assertEqual(DataStore(self.root/'data.json').snapshot(),original)

    def test_queued_task_keeps_old_evidence_and_new_task_gets_new_version(self):
        old=self.service.create({'question':'99红玫2026-10-23销量下降原因'})['task']
        stage=self.upload(self.csv());preview=self.preview(stage);self.commit(stage,preview)
        newer=self.service.create({'question':'99红玫2026-10-23销量下降原因'})['task']
        with self.service.connection() as db:
            records=[json.loads(db.execute('SELECT document FROM task_datasets WHERE task_id=?',(t['id'],)).fetchone()[0]) for t in (old,newer)]
        old_value=next(r['paid_orders'] for r in records[0]['sales'] if r['sku_id']==self.sku and r['date']=='2026-10-23')
        self.assertNotEqual(old_value,10);self.assertEqual(next(r['paid_orders'] for r in records[1]['sales'] if r['sku_id']==self.sku and r['date']=='2026-10-23'),10)

    def test_duplicate_negative_blank_and_wrong_store_block_entire_batch(self):
        texts=[self.csv()+self.csv().splitlines()[1]+'\n',self.csv().replace(',10,9,12,',',-1,9,12,'),
            self.csv().replace(',10,9,12,',',10,,12,'),self.csv(extra=',门店')]
        for text in texts:
            stage=self.upload(text);preview=self.preview(stage);self.assertFalse(preview['can_commit']);self.assertTrue(preview['errors'])
            with self.assertRaises(ValueError):self.commit(stage,preview)

    def test_preview_token_and_dataset_version_conflicts(self):
        first=self.upload(self.csv());second=self.upload(self.csv());p1=self.preview(first);p2=self.preview(second)
        changed=self.preview(first)
        with self.assertRaises(ValueError):self.commit(first,p1)
        self.commit(first,changed)
        with self.assertRaises(ValueError):self.commit(second,p2)

    def test_unknown_product_mapping_or_explicit_new_product(self):
        stage=self.upload(self.csv().replace(self.sku,'PLATFORM-123'))
        preview=self.preview(stage);self.assertEqual(preview['unmatched_products'],['PLATFORM-123'])
        mapped=self.preview(stage,product_map={'PLATFORM-123':self.sku});self.assertTrue(mapped['can_commit'])
        text='日期,商品ID,商品名称,支付订单数,支付买家数,支付件数,实付金额\n2026-10-23,NEW-1,新花束,2,2,2,99.8\n'
        stage=self.upload(text);preview=self.preview(stage,allow_new_products=True)
        self.assertTrue(preview['can_commit']);self.commit(stage,preview);self.assertIn('NEW-1',self.service.catalog.products)

    def test_product_mapping_reused_per_store_and_report_type(self):
        stage=self.upload(self.csv().replace(self.sku,'PLATFORM-123'))
        mapped=self.preview(stage,product_map={'PLATFORM-123':self.sku});self.commit(stage,mapped)
        later=self.upload(self.csv(day='2026-10-22').replace(self.sku,'PLATFORM-123'))
        preview=self.preview(later)
        self.assertTrue(preview['can_commit']);self.assertEqual(preview['applied_product_map'],{'PLATFORM-123':self.sku})

    def test_header_row_preserves_error_line_and_cohort_policy_confirmation(self):
        table=read_table('report.csv',('平台经营日报\n'+self.csv()).encode('utf-8'),header_row=2)
        self.assertEqual(table['rows'][0]['line'],3)
        raw=self.importer.template('order_cohorts','csv')
        stage=self.importer.upload({'kind':'order_cohorts','filename':'orders.csv','content_base64':base64.b64encode(raw).decode()})
        with self.assertRaises(ValueError):self.preview(stage)
        self.assertTrue(self.preview(stage,paid_cohort_confirmed=True)['can_commit'])

    def test_missing_required_fields_not_inferred_and_cancel_stage(self):
        stage=self.upload('日期,商品ID,支付订单数\n2026-10-23,'+self.sku+',2\n')
        with self.assertRaises(ValueError):self.preview(stage)
        self.importer.cancel(stage['id'])
        with self.assertRaises(ValueError):self.preview(stage)

    def test_file_guards_and_gb_csv(self):
        raw=self.csv().encode('gb18030');self.assertEqual(len(read_table('x.csv',raw)['rows']),1)
        with self.assertRaises(ValueError):read_table('x.xls',b'test')
        raw=write_template(['日期'],['2026-10-25'],True);original=zipfile.ZipFile(io.BytesIO(raw));output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            for name in original.namelist():
                body=original.read(name)
                if name.endswith('sheet1.xml'):body=body.replace(b'<is>',b'<f>1+1</f><is>',1)
                z.writestr(name,body)
        with self.assertRaises(ValueError):read_table('formula.xlsx',output.getvalue())


if __name__=='__main__':unittest.main()
