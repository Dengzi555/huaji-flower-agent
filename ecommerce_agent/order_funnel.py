"""支付订单批次分析：日期是支付日，取消/完成是同批订单在截止时刻的互斥状态。"""
import hashlib
import random
from datetime import date

from .analysis import change


def generate_order_cohorts(document):
    """为学习快照生成同批汇总，使用独立随机源，不改变既有销量、广告或价格。"""
    rows = []
    store = document.get('business_context', {}).get('store_id', 'default')
    for sale in document['sales']:
        if sale['status'] != 'ok': continue
        seed = hashlib.sha256(f"{store}/{sale['sku_id']}/{sale['date']}".encode()).hexdigest()
        rng = random.Random(seed)
        paid = sale['paid_orders']
        rate = rng.uniform(.015, .095)
        if sale['sku_id']=='FLR-ROSE-99-BOUQUET' and sale['date']=='2026-10-25':rate=.29
        cancelled = min(paid, round(paid*rate))
        pending = min(paid-cancelled, 1 if paid and sale['date']=='2026-10-27' else 0)
        reasons = {'顾客计划变更': 0, '等待时间过长': 0, '商家缺货': 0, '原因未提供': 0}
        for _ in range(cancelled):
            reason = rng.choice(list(reasons))
            if rate>.2:reason=rng.choice(['商家缺货','等待时间过长'])
            reasons[reason] += 1
        rows.append({**{k:sale[k] for k in ('sku_id','date','status','observed_at')},
            'source_id':'flower-paid-cohort-v1','cohort_policy':'paid_date_as_of_v1',
            'cohort_paid_orders':paid,'cancelled_orders':cancelled,
            'completed_orders':paid-cancelled-pending,'pending_orders':pending,
            'late_cancelled_orders':rng.randint(0,cancelled), 'cancellation_reasons':reasons})
    return rows


def order_funnel(store, sku, day, comparison=None):
    """查询同 SKU 两个支付日批次；只比较同口径可用记录，零订单取消率保持未知。"""
    data=store.query('order_cohorts',sku,day,comparison_day=comparison)
    metrics=[]; notes=[]
    for row in data['records']:
        item={'date':row['date'],'status':row['status'],'observed_at':row.get('observed_at')}
        if row['status']=='ok':
            n=row['cohort_paid_orders']
            item.update({k:row[k] for k in ('cohort_paid_orders','cancelled_orders','completed_orders','pending_orders','late_cancelled_orders','cancellation_reasons')})
            item['cancellation_pct']=round(row['cancelled_orders']/n*100,2) if n else None
            item['age_days']=(date.fromisoformat(row['observed_at'][:10])-date.fromisoformat(row['date'])).days
            sales=store.records.get(('sales',sku,row['date']))
            if sales and sales['status']=='ok' and sales['paid_orders']!=n:
                item['status']='conflicting';notes.append(f"{row['date']}批次支付订单与销售表不一致，不计算该日变化。")
        else:notes.append(f"{row['date']}没有可用订单批次记录，不能把取消数视为零。")
        metrics.append(item)
    delta=None
    if all(m['status']=='ok' for m in metrics):
        before,after=metrics
        delta={k:change(before[k],after[k]) for k in ('cohort_paid_orders','cancelled_orders','completed_orders')}
        delta['cancellation_pp']=round(after['cancellation_pct']-before['cancellation_pct'],2) if all(m['cancellation_pct'] is not None for m in metrics) else None
    notes.extend(['按支付日归组；跨日取消回到原支付批次，不使用当日取消事件数除以当日支付数。',
        '不同批次观察时长可能不同，近期未完结订单仍会变化；完成订单不等于扣除退款后的净成交。',
        '取消原因来自记录标签，只提供排查线索，不证明销量下降的因果。'])
    return {**data,'metrics':metrics,'changes':delta,'notes':notes}
