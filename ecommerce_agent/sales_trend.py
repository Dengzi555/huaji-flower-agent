"""多日销量工具：读取真实日级记录、保留缺口、多基线比较与日历/活动差异。"""
from datetime import date,timedelta
from .business_calendar import day_context


def sales_trend(store,sku,start,end,comparison=None,listed_at=None,retired_at=None):
    """展示窗口最多60天，额外读取前28日供同星期历史判断；总范围不超过90天。"""
    a,b=date.fromisoformat(start),date.fromisoformat(end)
    if not 0<=(b-a).days<60:raise ValueError('销量趋势只支持1–60天窗口。')
    earliest=a-timedelta(days=28)
    if comparison:earliest=min(earliest,date.fromisoformat(comparison))
    if (b-earliest).days>=90:raise ValueError('基线与查询范围合计不能超过90天。')
    rows=[]
    imported_only=store.has_import(sku,[(a+timedelta(days=i)).isoformat() for i in range((b-a).days+1)])
    for offset in range((b-earliest).days+1):
        current=(earliest+timedelta(days=offset)).isoformat()
        domains={table:store.daily_record(table,sku,current,imported_only=imported_only)
                 for table in ('sales','traffic','inventory','promotions')}
        sale=domains['sales'];status='not_listed' if listed_at and current<listed_at else sale['status']
        if retired_at and current>retired_at:status='retired'
        values={field:sale.get(field) if status=='ok' else None for field in ('units_sold','paid_orders','paid_buyers','paid_revenue')}
        rows.append({'date':current,'status':status,**values,
            'visitors':domains['traffic'].get('visitors') if domains['traffic']['status']=='ok' else None,
            'campaign_active':domains['promotions'].get('active') if domains['promotions']['status']=='ok' else None,
            'campaign_name':domains['promotions'].get('campaign_name') if domains['promotions']['status']=='ok' else None,
            'stockout_minutes':domains['inventory'].get('out_of_stock_minutes') if domains['inventory']['status']=='ok' else None,
            'monitored_minutes':domains['inventory'].get('monitored_minutes') if domains['inventory']['status']=='ok' else None,
            'records':domains})
    by_day={r['date']:r for r in rows};target=by_day[end];comparisons=[]
    baselines=[('前一日',(b-timedelta(days=1)).isoformat()),('前两日',(b-timedelta(days=2)).isoformat()),('上周同日',(b-timedelta(days=7)).isoformat())]
    if comparison and comparison not in {day for _,day in baselines}:baselines.append(('指定基线',comparison))
    for label,day in baselines:
        before=by_day.get(day);valid=target['status']=='ok' and before and before['status']=='ok'
        delta=target['units_sold']-before['units_sold'] if valid else None
        rate=round(delta/before['units_sold']*100,2) if valid and before['units_sold'] else None
        tcal,bcal=day_context(end),day_context(day)
        flags=[]
        if not tcal['schedule_known'] or not bcal['schedule_known']:flags.append('年度假期安排未核实')
        elif tcal['day_type']!=bcal['day_type']:flags.append('工作/休息日类型不同')
        if tcal['holiday_name']!=bcal['holiday_name']:flags.append('假期背景不同')
        if before and target['campaign_active'] is not None and before['campaign_active'] is not None:
            if (target['campaign_active'],target['campaign_name'])!=(before['campaign_active'],before['campaign_name']):flags.append('活动状态或名称不同')
        else:flags.append('活动记录不完整')
        comparisons.append({'label':label,'date':day,'target_date':end,'is_primary':day==comparison,
            'baseline_units':before['units_sold'] if before and before['status']=='ok' else None,
            'target_units':target['units_sold'],'absolute_change':delta,'relative_pct':rate,
            'status':'ok' if valid else 'insufficient_data','zero_baseline':bool(valid and before['units_sold']==0),
            'context_flags':flags,'baseline_calendar':bcal,'target_calendar':tcal})
    visible=[r for r in rows if start<=r['date']<=end];complete=[r for r in visible if r['status']=='ok']
    return {'table':'sales_trend','sku_id':sku,'start_date':start,'end_date':end,'records':rows,'comparisons':comparisons,
        'coverage':{'available_days':len(complete),'requested_days':len(visible),'missing_days':[r['date'] for r in visible if r['status']!='ok']},
        'summary':{'units_sum':sum(r['units_sold'] for r in complete) if complete else None,
            'revenue_sum':round(sum(r['paid_revenue'] for r in complete),2) if complete else None,
            'complete_window':len(visible)==len(complete)},
        'scope':'汇总仅含已提供且可用的自然日；缺失和未上架不补零，日历/活动差异是背景而非因果证明。'}


def comparison_notes(trend):
    """将多基线、星期/节日与活动差异作为审慎提示，不自动认定异常或完成季节调整。"""
    notes=[]
    if trend['coverage']['missing_days']:notes.append('展示窗口有缺失或未上架日期，曲线断开，汇总不是完整期间总量。')
    primary=next((c for c in trend['comparisons'] if c['is_primary']),None)
    if primary and primary['context_flags']:notes.append('主基线背景存在差异：'+'、'.join(primary['context_flags'])+'；不能仅凭单次降幅认定经营异常。')
    weekly=next(c for c in trend['comparisons'] if c['label']=='上周同日')
    if weekly['status']!='ok':notes.append('上周同日基线不足，不能用零替代，也不能声称已经排除星期因素。')
    elif weekly['context_flags']:notes.append('上周同日也存在背景差异：'+'、'.join(weekly['context_flags'])+'。')
    else:notes.append('可同时参考上周同日；仍未控制配送、价格、竞争和广告归因等因素。')
    if primary and primary['relative_pct'] is not None and primary['relative_pct']<=-20 and weekly['relative_pct'] is not None and weekly['relative_pct']>-10:
        notes.append('相对主基线下降较多，但相对上周同日接近；先核查基线是否处于高峰，不能把回落直接称为持续异常。')
    return notes
