"""门店报表导入：文件检查→字段/商品匹配→冻结预览→确认发布版本，历史任务不变。"""
import base64
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone, timedelta, date

from .clock import utc_now
from .data import DataStore, FIELDS
from .task_parser import ProductCatalog
from .tabular import read_table, write_template

LABELS={'sales':'销售日报','traffic':'商品流量','inventory':'库存监测','promotions':'活动记录','order_cohorts':'支付订单批次'}
NAMES={'date':'日期','sku_id':'商品编码','product_name':'商品名称','store_id':'门店','observed_at':'统计截至时间',
    'paid_orders':'支付订单数','paid_buyers':'支付买家数','units_sold':'支付件数','paid_revenue':'支付金额(元)',
    'visitors':'商品访客数','out_of_stock_minutes':'缺货分钟数','monitored_minutes':'监测分钟数',
    'active':'是否有活动','campaign_name':'活动名称','cohort_paid_orders':'批次支付订单数',
    'cancelled_orders':'取消订单数','completed_orders':'完成订单数','pending_orders':'待完结订单数',
    'late_cancelled_orders':'跨日取消订单数','cancellation_reasons':'取消原因计数(JSON)'}
ALIASES={'date':['统计日期','业务日期','支付日期','日期'],'sku_id':['商品ID','SKU','sku','sku_id','商品编码','商家商品编码'],
    'product_name':['商品名','商品名称','商品标题'],'store_id':['门店ID','店铺名称','门店名称','门店'],
    'observed_at':['数据更新时间','统计截至时间','observed_at'],'paid_orders':['支付订单数','支付订单量'],
    'paid_buyers':['支付人数','支付买家数'],'units_sold':['支付件数','销售件数','商品销量','销量'],
    'paid_revenue':['支付金额','支付金额(元)','实付金额','实付金额(元)'],'visitors':['商品访客数','访客数','访客人数','访客UV','UV']}
EXAMPLES={'date':'2026-10-25','sku_id':'FLR-ROSE-99-BOUQUET','product_name':'炽热爱意99朵红玫瑰花束',
    'store_id':'','observed_at':'2026-10-28T08:15:00+08:00','paid_orders':25,'paid_buyers':24,'units_sold':27,'paid_revenue':3839.09,
    'visitors':1201,'out_of_stock_minutes':30,'monitored_minutes':1440,'active':'是','campaign_name':'周末鲜花满减券',
    'cohort_paid_orders':25,'cancelled_orders':7,'completed_orders':18,'pending_orders':0,'late_cancelled_orders':1,
    'cancellation_reasons':'{"商家缺货":4,"等待时间过长":3}'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS workspace_versions(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,document TEXT NOT NULL,catalog TEXT NOT NULL,fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_imports(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,filename TEXT NOT NULL,file_sha256 TEXT NOT NULL,kind TEXT NOT NULL,base_fingerprint TEXT NOT NULL,parsed TEXT NOT NULL,preview TEXT,status TEXT NOT NULL,version_id TEXT);
CREATE TABLE IF NOT EXISTS import_mappings(kind TEXT PRIMARY KEY,mapping TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_product_mappings(kind TEXT NOT NULL,external_id TEXT NOT NULL,sku_id TEXT NOT NULL,PRIMARY KEY(kind,external_id));
'''


def fingerprint(document,catalog):
    """与任务快照使用同一内容哈希口径，避免不同版本被误认为同一份数据。"""
    return hashlib.sha256(json.dumps([document,catalog],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class DataImports:
    """复用门店数据库事务；预览、发布、字段映射均局限本门店，不自动覆盖其他门店。"""
    def __init__(self,service):
        self.service=service

    def fields(self,kind):
        """提供可选来源列映射；商品名称/编码至少一个，指标列不推算、不补零。"""
        if kind not in LABELS:raise ValueError('不支持的报表类型。')
        return ['date','sku_id','product_name','store_id','observed_at',*[k for k in FIELDS[kind] if k!='cohort_policy']]

    def schema(self):
        return {'kinds':LABELS,'fields':{kind:[{'key':k,'label':NAMES[k],
            'required':k=='date' or k in FIELDS[kind] and k!='cohort_policy'} for k in self.fields(kind)] for kind in LABELS},
            'products':[{'sku_id':p['sku_id'],'name':p.get('short_name',p['name']),'variant':p['variant']} for p in self.service.catalog.products.values()],
            'fingerprint':self.service.fingerprint}

    def template(self,kind,fmt):
        fields=self.fields(kind)
        if fmt not in ('csv','xlsx'):raise ValueError('模板格式不支持。')
        return write_template([NAMES[k] for k in fields],[EXAMPLES[k] for k in fields],fmt=='xlsx')

    def upload(self,payload):
        """解析文件但不导入指标；限制类型、大小、工作表、公式和行数。"""
        kind=payload.get('kind');fields=self.fields(kind)
        filename=payload.get('filename','')
        if not isinstance(filename,str) or not 1<=len(filename)<=180:raise ValueError('文件名无效。')
        try:raw=base64.b64decode(payload.get('content_base64',''),validate=True)
        except (ValueError,TypeError):raise ValueError('文件编码不合法。') from None
        parsed=read_table(filename,raw,payload.get('sheet_index',0),payload.get('header_row',1))
        with self.service.connection() as db:
            saved=db.execute('SELECT mapping FROM import_mappings WHERE kind=?',(kind,)).fetchone()
        mapping={}
        for key in fields:
            names=[key,NAMES[key],*ALIASES.get(key,[])]
            choices=[h for h in parsed['headers'] if h.casefold() in {n.casefold() for n in names}]
            if len(choices)==1:mapping[key]=choices[0]
        if saved:
            mapping.update({k:v for k,v in json.loads(saved[0]).items() if k in fields and v in parsed['headers']})
        identity=uuid.uuid4().hex
        with self.service.connection() as db:
            # 暂存也需有界；避免失效导入文件无限积累，已发布审计不清除。
            if db.execute("SELECT count(*) FROM report_imports WHERE status='staged'").fetchone()[0]>=30:raise ValueError('待处理文件过多，请先确认或取消已有导入。')
            db.execute('INSERT INTO report_imports VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,utc_now(),filename,hashlib.sha256(raw).hexdigest(),kind,
                self.service.fingerprint,json.dumps(parsed,ensure_ascii=False),None,'staged',None))
        return {'id':identity,'kind':kind,'filename':filename,'headers':parsed['headers'],'rows':len(parsed['rows']),
            'sample':parsed['rows'][:5],'mapping':mapping,'sheets':parsed['sheets']}

    def preview(self,identity,payload):
        """转换并验证所有行，保存待确认内容；任何错误均阻止整批发布，不静默丢弃。"""
        with self.service.connection() as db:row=db.execute('SELECT * FROM report_imports WHERE id=?',(identity,)).fetchone()
        if not row or row['status']!='staged':raise ValueError('导入文件不存在或已处理。')
        if row['base_fingerprint']!=self.service.fingerprint:raise ValueError('数据版本已变化，请重新上传并预览。')
        parsed=json.loads(row['parsed']);mapping=payload.get('mapping',{});product_map=payload.get('product_map',{})
        if row['kind']=='order_cohorts' and payload.get('paid_cohort_confirmed') is not True:
            raise ValueError('请确认报表按支付日归组，取消数是同批支付订单截至指定时刻的状态；按取消发生日统计的日报不能直接导入此类型。')
        if not isinstance(mapping,dict) or not isinstance(product_map,dict):raise ValueError('映射需为对象。')
        fields=self.fields(row['kind'])
        if any(k not in fields or v not in parsed['headers'] for k,v in mapping.items()):raise ValueError('字段映射引用了未知字段或来源列。')
        if len(set(mapping.values()))!=len(mapping.values()):raise ValueError('同一来源列不能对应多个不同口径字段。')
        required=['date',*[k for k in FIELDS[row['kind']] if k!='cohort_policy']]
        if any(k not in mapping for k in required) or not {'sku_id','product_name'}&mapping.keys():raise ValueError('请匹配全部必填指标、日期和商品编码或名称。')
        store,catalog,_=self.service.dataset();document=store.snapshot();products=list(catalog.products.values())
        if any(not isinstance(k,str) or not k or not isinstance(v,str) or v not in catalog.products for k,v in product_map.items()):raise ValueError('商品映射需指向当前门店的有效商品编码。')
        with self.service.connection() as db:
            saved_products={r['external_id']:r['sku_id'] for r in db.execute('SELECT external_id,sku_id FROM import_product_mappings WHERE kind=?',(row['kind'],)) if r['sku_id'] in catalog.products}
        product_map={**saved_products,**product_map}
        by_key={(r['sku_id'],r['date']):r for r in document[row['kind']]};errors=[];unmatched=set();converted=[];new_products={};seen=set();replaced=0
        scope=store.meta.get('business_context',{});known_stores={scope.get('store_id'),scope.get('store_name')}
        observed_default=utc_now();source='import:'+identity;allow_new=payload.get('allow_new_products') is True
        for source_row in parsed['rows']:
            line=source_row['line'];values=source_row['values'];read=lambda key:values.get(mapping.get(key,''),'')
            try:
                if read('store_id') and read('store_id') not in known_stores:raise ValueError('文件门店与当前门店不一致，请拆分门店报表。')
                identifier=read('sku_id') or read('product_name');sku=product_map.get(identifier,read('sku_id'))
                if not sku:
                    hits=[p['sku_id'] for p in products if read('product_name') in [p['name'],p.get('short_name'),*p.get('aliases',[])]]
                    sku=hits[0] if len(hits)==1 else None
                if sku not in catalog.products:
                    if allow_new and read('sku_id') and read('product_name') and identifier not in product_map:
                        sku=read('sku_id')
                        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',sku):raise ValueError('新增商品编码需为1–100位字母/数字/点/横线。')
                        candidate={'sku_id':sku,'name':read('product_name'),'short_name':read('product_name'),'variant':read('product_name'),'aliases':[],'business_tags':['常规款']}
                        if sku in new_products and new_products[sku]['name']!=candidate['name']:raise ValueError('同一新编码对应不同商品名称。')
                        new_products[sku]=candidate
                    else:
                        unmatched.add(identifier);raise ValueError('商品未匹配，请选择对应商品或显式确认新增编码。')
                day=read('date').split(' ')[0].split('T')[0].replace('/','-')
                try:day=date.fromisoformat(day).isoformat()
                except ValueError:
                    try:day=datetime.strptime(day,'%Y-%m-%d').date().isoformat()
                    except ValueError:raise ValueError('日期需包含年份，例如2026-10-25。') from None
                if day>=datetime.now(timezone(timedelta(hours=8))).date().isoformat():raise ValueError('日级报表仅导入已结束自然日；不能把今天累计与全天混合。')
                observed=read('observed_at') or observed_default
                if row['kind']=='order_cohorts' and not read('observed_at'):raise ValueError('订单批次必须提供实际统计截止时间。')
                cutoff=datetime.fromisoformat(observed);business_tz=timezone(timedelta(hours=8))
                if cutoff.tzinfo is None:cutoff=cutoff.replace(tzinfo=business_tz)
                observed=cutoff.astimezone(business_tz).isoformat()
                record={'sku_id':sku,'date':day,'observed_at':observed,'source_id':source,'status':'ok'}
                for field,kind in FIELDS[row['kind']].items():
                    if field=='cohort_policy':record[field]='paid_date_as_of_v1';continue
                    value=read(field)
                    if kind is bool:
                        if value.lower() not in ('是','否','true','false','1','0','有','无'):raise ValueError(f'{NAMES[field]}只接受是/否、true/false或1/0。')
                        value=value.lower() in ('是','true','1','有')
                    elif kind is dict:
                        try:value=json.loads(value)
                        except json.JSONDecodeError:raise ValueError('取消原因需为JSON计数字典。') from None
                    elif kind is str:
                        pass
                    else:
                        cleaned=value.replace(',','').replace('￥','').replace('¥','')
                        try:number=float(cleaned)
                        except ValueError:raise ValueError(NAMES[field]+'需为数值，空白不视为0。') from None
                        if not math.isfinite(number) or number<0 or kind is int and not number.is_integer():raise ValueError(NAMES[field]+'需为非负数；计数必须为整数。')
                        value=int(number) if kind is int else number
                    record[field]=value
                key=(sku,day)
                if key in seen:raise ValueError('文件内同商品同日期重复，请先汇总或去重。')
                DataStore.from_document({**store.meta,row['kind']:[record]})
                seen.add(key);replaced+=key in by_key;converted.append(record)
            except (ValueError,TypeError) as error:errors.append({'line':line,'message':str(error)})
        if not errors:
            for record in converted:by_key[record['sku_id'],record['date']]=record
            document[row['kind']]=list(by_key.values());new_store=DataStore.from_document(document)
            products+=list(new_products.values());cat={'schema_version':1,'products':products}
            ProductCatalog.from_document(cat,new_store.skus)
            # 不把混合快照伪装成真实店铺；行级source_id和批次审计区分来源。
            document.setdefault('business_context',{})['import_provenance']='包含用户导入报表；来源真实性未经独立核验，其他商品/日期可能仍为学习数据，不能视为真实整店汇总。导入指标不与学习样例混作分析依据。'
        else:cat=None
        identifiers={r['values'].get(mapping.get(key,'')) for r in parsed['rows'] for key in ('sku_id','product_name')}
        preview={'preview_token':uuid.uuid4().hex,'mapping':mapping,'product_map':product_map,'applied_product_map':{key:value for key,value in product_map.items() if key in identifiers},'allow_new_products':allow_new,'errors':errors[:100],
            'error_count':len(errors),'unmatched_products':sorted(unmatched),'total_rows':len(parsed['rows']),
            'valid_rows':len(converted),'replaced_rows':replaced,'new_rows':len(converted)-replaced,
            'new_products':list(new_products.values()),'sample':converted[:10],
            'can_commit':not errors and bool(converted),'source_id':source,
            'document':document if not errors else None,'catalog':cat}
        with self.service.connection() as db:db.execute('UPDATE report_imports SET preview=? WHERE id=?',(json.dumps(preview,ensure_ascii=False),identity))
        return {k:v for k,v in preview.items() if k not in ('document','catalog')}

    def commit(self,identity,payload):
        """发布单门店新版本：事务原子化、幂等确认、版本冲突拒绝；已排队任务继续用旧快照。"""
        if set(payload)!={'confirm','preview_token'} or payload['confirm'] is not True:raise ValueError('请预览后明确确认导入。')
        with self.service.data_lock:
            with self.service.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM report_imports WHERE id=?',(identity,)).fetchone()
                if not row:raise ValueError('导入批次不存在。')
                if row['status']=='committed':return {'version_id':row['version_id'],'reused':True}
                if row['status']!='staged' or not row['preview']:raise ValueError('请先校验预览。')
                if row['base_fingerprint']!=self.service.fingerprint:raise ValueError('数据版本已变化，请重新上传。')
                preview=json.loads(row['preview'])
                if payload['preview_token']!=preview['preview_token']:raise ValueError('预览已变化，请重新核对后确认。')
                if not preview['can_commit']:raise ValueError('存在错误，不能导入。')
                document=preview['document'];catalog=preview['catalog'];new_store=DataStore.from_document(document)
                new_catalog=ProductCatalog.from_document(catalog,new_store.skus);version=uuid.uuid4().hex;digest=fingerprint(document,catalog)
                db.execute('INSERT INTO workspace_versions VALUES(?,?,?,?,?)',(version,utc_now(),json.dumps(document,ensure_ascii=False),json.dumps(catalog,ensure_ascii=False),digest))
                db.execute("UPDATE report_imports SET status='committed',version_id=? WHERE id=?",(version,identity))
                db.execute('INSERT OR REPLACE INTO import_mappings VALUES(?,?)',(row['kind'],json.dumps(preview['mapping'],ensure_ascii=False)))
                for external,sku in preview['product_map'].items():db.execute('INSERT OR REPLACE INTO import_product_mappings VALUES(?,?,?)',(row['kind'],external,sku))
            self.service.store,self.service.catalog,self.service.fingerprint=new_store,new_catalog,digest
            self.service._dataset=(new_store,new_catalog,digest)
        return {'version_id':version,'fingerprint':digest,'reused':False,'imported_rows':preview['valid_rows']}

    def cancel(self,identity):
        """取消尚未发布的暂存文件；保留审计ID，释放原文件解析内容。"""
        with self.service.connection() as db:
            db.execute("UPDATE report_imports SET status='cancelled',parsed='{}',preview=NULL WHERE id=? AND status='staged'",(identity,))
        return {'cancelled':True}

    def history(self):
        """只返回来源与版本摘要，不在列表暴露完整报表。"""
        with self.service.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,created_at,filename,kind,status,version_id FROM report_imports ORDER BY rowid DESC LIMIT 30')]
