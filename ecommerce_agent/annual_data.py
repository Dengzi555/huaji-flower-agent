"""年度鲜花场景工厂：固定种子、320款、滚动365天、关系校验与独立答案清单。

生成器参数是学习场景设定，不是行业统计。已知干预保存在answers.json，业务适配器
只能读取经营表，分析模型仍须区分可观察证据与未知因果，不能直接读取标准答案。
"""
import argparse
import copy
import hashlib
import json
import math
import random
import sqlite3
from datetime import date,timedelta
from pathlib import Path

from .flower_business_data import business_catalog
from .store_workspace import STORES,PROFILES,generate_advertising,allocate
from .business_calendar import day_context
from .natural_dates import holiday_date
from .data import DataStore,FIELDS

VERSION='flower-annual-v1'
DEFAULT_END='2026-10-28'
VARIETIES=['红玫瑰','白玫瑰','粉玫瑰','紫玫瑰','乌梅玫瑰','卡布奇诺玫瑰','曼塔玫瑰','海洋之歌玫瑰',
    '紫霞仙子玫瑰','白雪山玫瑰','香槟玫瑰','粉荔枝玫瑰','弗洛伊德玫瑰','洛神玫瑰','苹果杰克',
    '向日葵','粉康乃馨','红康乃馨','白洋桔梗','紫洋桔梗','粉洋桔梗','绿洋桔梗','白百合','粉百合',
    '蓝绣球','粉绣球','白乒乓菊','混色玫瑰','多头泡泡玫瑰','非洲菊']


def canonical(value):
    """固定键序和紧凑编码用于可复现指纹，不依赖文件时间或Python哈希随机化。"""
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def seeded(seed,*parts):
    """逐店/逐SKU/逐日独立随机源，增一款商品不会重排其他商品的整年数据。"""
    return random.Random(int.from_bytes(hashlib.sha256(canonical([seed,*parts]).encode()).digest()[:8],'big'))


def annual_catalog(count=320,end=DEFAULT_END,seed=20261029):
    """保留已学过的商品ID，补充品种/规格/包装组合；生命周期属于目录而非缺失值。"""
    if type(count) is not int or not 300<=count<=500:raise ValueError('年度目录支持300–500款。')
    last=date.fromisoformat(end);start=last-timedelta(days=364)
    products=copy.deepcopy(business_catalog()['products'])
    for p in products:
        if '新品' not in p['business_tags']:p['listed_at']=start.isoformat()
    prefixes=['春野','晴空','月下','星河','云间','花朝','暮色','清风','暖阳','晨露','初夏','晚霞','雪落','青山','海棠','秋水','南风','雨后','朝露','流光','玉露','林间','远山','静夜','花间','晚风','碧空','锦时','时光','白昼']
    suffixes=['来信','心语','微醺','如歌','映月','入梦','有约','漫舞','拾光','知意','寄情','予你','相逢','绽放','留香','听雨','晴好','初见','念你','花语']
    used={p['short_name'] for p in products};n=0
    while len(products)<count:
        index=len(products)-28;variety=VARIETIES[(index//10)%len(VARIETIES)]
        stems=([3,5,9,11,19] if variety=='向日葵' else [3,6,9,11,19] if '百合' in variety or '绣球' in variety else [9,11,19,33,52])[(index//2)%5]
        kind='花束' if index%2==0 else '抱抱桶'
        while True:
            short=prefixes[(n//len(suffixes))%len(prefixes)]+suffixes[n%len(suffixes)];n+=1
            if short not in used:used.add(short);break
        rng=seeded(seed,'catalog',index)
        listed=start if index%5 else start+timedelta(days=rng.randint(60,315))
        retired=(start+timedelta(days=rng.randint(180,345))).isoformat() if index%17==0 and listed<start+timedelta(days=150) else None
        tag='爆款' if index%23==0 else '常规款' if index%3==0 else '长尾款'
        series=('多巴胺系列' if index%19==0 else '晴天葡萄' if index%29==0 else '天使之翼' if variety=='白玫瑰' else '紫玫系列' if variety=='紫玫瑰' else variety.replace('玫瑰','')+'系列')
        price=round((29+stems*(1.7 if '玫瑰' in variety else 2.2)+(18 if kind=='抱抱桶' else 0)+rng.choice([0,5,10]))//10*10+9.9,2)
        keywords=['生日礼物','生日鲜花','鲜花上门']
        if '康乃馨' in variety:keywords+=['母亲节鲜花']
        elif variety=='向日葵':keywords+=['教师节鲜花','毕业季鲜花']
        elif '玫瑰' in variety:keywords+=['情人节鲜花']
        product={'sku_id':f'FLR-YEAR-{index+1:04d}','short_name':short,'stem_count':stems,'variety':variety,'product_type':kind,
            'sample_unit_price':price,'listed_at':listed.isoformat(),'business_tags':[tag],'series':series,
            'name':f'【{short}】{stems}朵{variety}{kind}'+''.join(keywords),
            'variant':f'{stems}朵{variety}·{kind}','occasion_keywords':keywords,
            'aliases':sorted({short,variety,series,f'{stems}朵{variety}{kind}',f'{variety}{kind}',*(['洋桔梗'] if '洋桔梗' in variety else [])})}
        if retired:product['retired_at']=retired
        products.append(product)
    return {'schema_version':1,'products':products}


def occasion(current,variety):
    """设定品种相关的营销峰值和前后肩部，不把所有法定假日一律放大。"""
    nodes=[('情人节',date(current.year,2,14)),('520',date(current.year,5,20)),
        ('母亲节',holiday_date('母亲节',current.year)),('父亲节',holiday_date('父亲节',current.year)),
        ('教师节',date(current.year,9,10)),('圣诞节',date(current.year,12,25))]
    for label,center in nodes:
        distance=abs((current-center).days)
        if distance<=2:
            relevant=('玫瑰' in variety and label in ('情人节','520','圣诞节') or '康乃馨' in variety and label=='母亲节'
                or variety=='向日葵' and label in ('父亲节','教师节') or '洋桔梗' in variety and label=='教师节')
            if relevant:return (5.5 if distance==0 else 2.1 if distance==1 else 1.3),label+'鲜花礼遇'
    return 1.,''


def scenario_override(sku,day,store_id,visitors,orders,stockout,active,campaign):
    """可控干预只作用于明确日期/SKU；标准答案承认干预，不要求Agent证明因果。"""
    if store_id=='hx-sz-ns':
        if sku=='FLR-ROSE-99-BOUQUET' and day in ('2026-10-24','2026-10-25'):
            visitors,orders,stockout=(1200,60,0) if day.endswith('24') else (1150,23,480)
        if sku=='FLR-WUMEI-33-BUCKET' and day in ('2026-10-24','2026-10-25'):
            visitors,orders,stockout=(400,20,0) if day.endswith('24') else (120,6,0)
        if sku=='FLR-WUMEI-19-BOUQUET' and day in ('2026-10-24','2026-10-25'):
            visitors,orders,stockout=320,20,0
        if sku=='FLR-ROSE-19-BOUQUET' and day in ('2026-05-20','2026-05-21'):
            visitors,orders,stockout=(900,45,0) if day.endswith('20') else (240,12,0)
            active=day.endswith('20');campaign='520鲜花礼遇' if active else ''
        if sku=='FLR-SUNFLOWER-11-BUCKET' and day=='2026-10-25':visitors,orders,stockout=180,7,1440
        if sku=='FLR-HYDRANGEA-3-BOUQUET' and day=='2026-10-25':visitors,orders,stockout=0,0,0
    if sku=='FLR-GRAPE-19-BOUQUET':orders=0
    return visitors,orders,stockout,active,campaign


def daily_document(products,shop,index,current,end,seed):
    """先生成曝光与营业/库存，再得到支付、件数、金额和支付日批次，保证代数关系。"""
    profile=PROFILES[index];day=current.isoformat();cutoff=(date.fromisoformat(end)+timedelta(days=1)).isoformat()+'T08:15:00+08:00'
    meta={'schema_version':1,'sample_data':True,'timezone':'Asia/Shanghai','currency':'CNY',
        'business_context':{'scenario':'flash_flowers','dataset_id':VERSION,'snapshot_as_of':cutoff[:10],
            'store_id':shop['id'],'store_name':shop['name'],'brand':'花仙女','operating_profile':profile['label'],
            'provenance':'固定种子年度合成经营数据，非真实门店记录',
            'count_policy':'支付订单与访客按主要SKU互斥分配；件数可大于订单，非平台通用归因口径',
            'inventory_scope':'成品日级可售监测，不是花材实时库存'}}
    document={**meta,**{table:[] for table in FIELDS}}
    calendar=day_context(day)
    for product in products:
        sku=product['sku_id']
        if day<product['listed_at'] or product.get('retired_at') and day>product['retired_at']:continue
        rng=seeded(seed,shop['id'],sku,day);hot='爆款' in product['business_tags'];tail='长尾款' in product['business_tags']
        uv_base=rng.uniform(180,340) if hot else rng.uniform(6,33) if tail else rng.uniform(24,90)
        seasonal=1+.12*math.sin((current.timetuple().tm_yday+index*13)*2*math.pi/365)
        weekend=1.17 if calendar['day_type']=='weekend_rest' else .93 if calendar['day_type']=='makeup_workday' else 1.
        factor,campaign=occasion(current,product['variety']);active=bool(campaign) or current.weekday()==4
        if not campaign and active:campaign='周五鲜花券'
        visitors=max(0,round(uv_base*profile['scale']*profile['traffic']*seasonal*weekend*factor))
        age=(current-date.fromisoformat(product['listed_at'])).days
        if age<7:visitors=round(visitors*.45)
        monitored=(profile['close']-profile['open'])*60
        stockout=rng.choice([0,0,0,0,0,15,40,90])
        conversion=rng.uniform(.035,.065) if hot else rng.uniform(.003,.018) if tail else rng.uniform(.018,.05)
        orders=int(visitors*conversion*(1-stockout/max(1,monitored))/profile['traffic'])
        visitors,orders,stockout,active,campaign=scenario_override(sku,day,shop['id'],visitors,orders,stockout,active,campaign)
        if sku=='FLR-EUSTOMA-11-BOUQUET':visitors,orders,stockout=43+index*3,1 if day==product['listed_at'] else 2,0
        units=orders+(rng.randint(0,max(1,orders//12)) if orders else 0)
        buyers=max(1,orders-rng.choice([0,0,1])) if orders else 0
        unit_cents=round(product['sample_unit_price']*100);discount=rng.randint(880,960) if active else rng.randint(970,1000)
        cents=(units*unit_cents*discount+500)//1000
        common={'sku_id':sku,'date':day,'observed_at':cutoff,'status':'ok'}
        document['sales'].append({**common,'source_id':'annual-paid-orders','paid_orders':orders,'paid_buyers':buyers,'units_sold':units,'paid_revenue':cents/100})
        delayed=shop['id']=='hx-sz-ns' and sku=='FLR-ROSE-33-BUCKET' and day=='2026-10-25'
        document['traffic'].append({**common,'source_id':'annual-sku-visitors',**({'status':'delayed','reason':'日结回传延迟'} if delayed else {'visitors':visitors})})
        if not delayed:
            document['inventory'].append({**common,'source_id':'annual-availability','out_of_stock_minutes':stockout,'monitored_minutes':monitored})
        document['promotions'].append({**common,'source_id':'annual-campaigns','active':active,'campaign_name':campaign})
        cancelled=min(orders,round(orders*rng.uniform(.025,.105)))
        if shop['id']=='hx-sz-ns' and sku=='FLR-WUMEI-19-BOUQUET' and day in ('2026-10-24','2026-10-25'):
            # 固定支付量再注入取消上升，取消仍为支付订单子集。
            cancelled=min(orders,0 if day.endswith('24') else max(1,round(orders*.6)))
        pending=0 if day<end else min(orders-cancelled,rng.choice([0,0,1]))
        reasons={'顾客计划变更':cancelled//2,'等待时间过长':cancelled-cancelled//2,'商家缺货':0,'原因未提供':0}
        document['order_cohorts'].append({**common,'source_id':'annual-paid-cohort','cohort_policy':'paid_date_as_of_v1',
            'cohort_paid_orders':orders,'cancelled_orders':cancelled,'completed_orders':orders-cancelled-pending,
            'pending_orders':pending,'late_cancelled_orders':cancelled//3,'cancellation_reasons':reasons})
    ledger=generate_advertising(document,index,seed=int.from_bytes(hashlib.sha256(f'{seed}-{day}'.encode()).digest()[:4],'big'))
    # 点击可以重复，付费访客是点击去重后的一部分；渠道互斥加总等于商品UV。
    traffic=[r for r in document['traffic'] if r['status']=='ok'];clicks=sum(r['clicks'] for r in ledger['ads'])
    paid=allocate(min(sum(r['visitors'] for r in traffic),int(clicks*.82)),[r['visitors'] for r in traffic])
    for row,ad_uv in zip(traffic,paid):
        remaining=row['visitors']-ad_uv;search=round(remaining*.63)
        document['traffic_channels'].append({**{k:row[k] for k in ('sku_id','date','observed_at','status')},
            'source_id':'annual-exclusive-channels','attribution_policy':'exclusive_last_touch_v1',
            'attributed_visitors':{'自然搜索':search,'付费广告':ad_uv,'推荐':remaining-search}})
    DataStore.from_document(document)  # 写入之前复用生产字段/批次分区校验。
    return document,ledger


def write_json(path,value):
    """只发布完整JSON；年度数据库另以新文件构建，不改写服务正在读的旧版本。"""
    path=Path(path);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def generate(root,count=320,end=DEFAULT_END,seed=20261029):
    """逐店逐日生成并验证，内存只持有一天经营数据及单店小时账本。"""
    root=Path(root);catalog=annual_catalog(count,end,seed);start=date.fromisoformat(end)-timedelta(days=364)
    code=Path(__file__).parent
    generator_hash=hashlib.sha256(b''.join((code/name).read_bytes() for name in ('annual_data.py','flower_business_data.py','fixtures.py','store_workspace.py','business_calendar.py','natural_dates.py'))).hexdigest()
    identifier=hashlib.sha256(canonical([VERSION,count,end,seed,catalog,generator_hash]).encode()).hexdigest()[:12]
    folder=root/'data/annual'/identifier;folder.mkdir(parents=True,exist_ok=True)
    manifest_path=folder/'manifest.json'
    if manifest_path.exists() and (folder/'answers.json').exists():
        write_json(root/'data/annual/active.json',{'version':identifier})
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    write_json(folder/'products.json',catalog)
    manifests=[]
    for index,shop in enumerate(STORES):
        path=folder/(shop['id']+'.sqlite3');temporary=path.with_suffix('.building')
        if temporary.exists():raise ValueError('存在未完成年度库，请检查后选择新的seed，避免覆盖。')
        db=sqlite3.connect(temporary)
        db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
        db.executescript('CREATE TABLE records(domain TEXT,sku TEXT,day TEXT,body TEXT,PRIMARY KEY(domain,sku,day)) WITHOUT ROWID; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);')
        ledger_all=None;row_count=0;meta=None;table_dates={table:[] for table in FIELDS}
        try:
            for offset in range(365):
                current=start+timedelta(days=offset)
                document,ledger=daily_document(catalog['products'],shop,index,current,end,seed)
                meta={k:v for k,v in document.items() if k not in FIELDS}
                if ledger_all is None:ledger_all={**ledger,'revenue':[],'ads':[]}
                ledger_all['revenue'].extend(ledger['revenue']);ledger_all['ads'].extend(ledger['ads'])
                for table in FIELDS:
                    records=document[table];table_dates[table].append(current.isoformat())
                    db.executemany('INSERT INTO records VALUES(?,?,?,?)',[(table,r['sku_id'],r['date'],canonical(r)) for r in records]);row_count+=len(records)
                if offset%60==0:db.commit();print(canonical({'store':shop['id'],'days':offset+1,'rows':row_count}),flush=True)
            db.execute('CREATE INDEX by_day ON records(domain,day,sku)')
            content=hashlib.sha256()
            for domain,sku,day,body in db.execute('SELECT domain,sku,day,body FROM records ORDER BY domain,sku,day'):
                content.update(canonical([domain,sku,day,body]).encode())
            metadata={'schema':'flower-annual-sqlite-v1','meta':meta,'skus':[p['sku_id'] for p in catalog['products']],
                'dates':table_dates,'rows':row_count,'content_sha256':content.hexdigest(),'seed':seed,'catalog_sha256':hashlib.sha256(canonical(catalog).encode()).hexdigest()}
            db.execute('INSERT INTO metadata VALUES(?,?)',('dataset',canonical(metadata)));db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('年度库完整性检查失败。')
        finally:db.close()
        if path.exists():raise ValueError('年度版本已存在，不覆盖不可变库。')
        temporary.rename(path)
        from .advertising import AdvertisingBook
        AdvertisingBook(ledger_all,shop['id'])
        write_json(folder/(shop['id']+'-ads.json'),ledger_all)
        manifests.append({'id':shop['id'],'rows':row_count,'content_sha256':metadata['content_sha256'],'bytes':path.stat().st_size})
    manifest={'schema':VERSION,'version':identifier,'seed':seed,'generator_sha256':generator_hash,'products':count,'start':start.isoformat(),'end':end,'days':365,
        'provenance':'可复现合成业务数据，不代表真实门店或行业分布','stores':manifests,'catalog':'products.json',
        'answers_policy':'answers.json仅供评测，运行时工具不读取；生成器干预不等于Agent已证实原因'}
    answers=make_answers(folder,catalog)
    write_json(folder/'answers.json',answers)
    write_json(manifest_path,manifest)
    write_json(root/'data/annual/active.json',{'version':identifier})
    return manifest


def make_answers(folder,catalog):
    """把已知干预和精确可核对数值分开；节日排名从冻结数据计算，不靠模型打分。"""
    store=DataStore(Path(folder)/'hx-sz-ns.sqlite3')
    def facts(sku,day):
        return {table:store.daily_record(table,sku,day) for table in FIELDS}
    definitions=[
        ('stockout','FLR-ROSE-99-BOUQUET','2026-10-25','2026-10-24','支付单60降至23，缺货480分钟；只能判为缺货线索，不能证明唯一根因'),
        ('traffic_drop','FLR-WUMEI-33-BUCKET','2026-10-25','2026-10-24','访客400降至120，支付单20降至6，转化率同为5%'),
        ('campaign_end','FLR-ROSE-19-BOUQUET','2026-05-21','2026-05-20','520活动后回落；比较背景不同，不能仅凭日降幅认定经营异常'),
        ('zero_tail','FLR-GRAPE-19-BOUQUET','2026-10-25','2026-10-24','有访客但持续零支付；转化为0而非无数据'),
        ('zero_visitors','FLR-HYDRANGEA-3-BOUQUET','2026-10-25','2026-10-24','访客与支付均0，转化率分母为0应为未知'),
        ('missing','FLR-ROSE-33-BUCKET','2026-10-25','2026-10-24','流量延迟、库存缺失，不允许补零'),
        ('conflict','FLR-SUNFLOWER-11-BUCKET','2026-10-25','2026-10-24','监测全天无货但有7笔支付，必须报告数据冲突'),
        ('new_listing','FLR-EUSTOMA-11-BOUQUET','2026-10-25','2026-10-24','上架首日不能要求上架前成交基线或补零')]
    definitions.append(('cancellations','FLR-WUMEI-19-BOUQUET','2026-10-25','2026-10-24','支付订单均20，取消由0增至12；不能把支付件数与最终完成订单混为一谈'))
    cases=[{'id':key,'store_id':'hx-sz-ns','sku_id':sku,'target_date':day,'baseline_date':baseline,
            'expected_behavior':behavior,'facts':{'target':facts(sku,day),'baseline':facts(sku,baseline)}} for key,sku,day,baseline,behavior in definitions]
    for day,label in [('2026-02-14','情人节'),('2026-05-10','母亲节'),('2026-06-21','父亲节'),('2026-09-10','教师节')]:
        groups={};names={p['sku_id']:p['variety'] for p in catalog['products']}
        for row in store.rows('sales',start=day,end=day):groups[names[row['sku_id']]]=groups.get(names[row['sku_id']],0)+row['units_sold']
        ranking=sorted(groups.items(),key=lambda x:(-x[1],x[0]))
        cases.append({'id':'holiday-'+day,'store_id':'hx-sz-ns','date':day,'label':label,'group_by':'variety',
            'units_by_variety':dict(ranking),'leaders':[k for k,v in ranking if v==ranking[0][1]],
            'expected_behavior':'按完整在售商品范围汇总，保留并列；营销节点不是因果证明'})
    return {'schema':'flower-annual-oracle-v1','business_today':'2026-10-29','not_available_to_agent':True,'cases':cases}


def main():
    """一条命令生成并激活年度版本；固定参数重复运行复用已有不可变版本。"""
    parser=argparse.ArgumentParser();parser.add_argument('--products',type=int,default=320);parser.add_argument('--end',default=DEFAULT_END);parser.add_argument('--seed',type=int,default=20261029)
    args=parser.parse_args();result=generate(Path(__file__).resolve().parents[1],args.products,args.end,args.seed)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
