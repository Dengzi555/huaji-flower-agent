"""中国经营日历：2026年官方放假调休安排 + 明确区分的营销纪念日。

年度安排未核实时保留未知，不将星期推算冒充法定安排；节假日不等于门店停业。
官方来源：国务院办公厅国办发明电〔2025〕7号，武汉市政府转载全文。
"""
from datetime import date,timedelta
from .natural_dates import holiday_date

SOURCE='https://www.wuhan.gov.cn/zwgk/tzgg/202511/t20251105_2672471.shtml'
VERSION='cn-2026-official-20251104-v1'
HOLIDAYS_2026=[('2026-01-01','2026-01-03','元旦'),('2026-02-15','2026-02-23','春节'),
    ('2026-04-04','2026-04-06','清明节'),('2026-05-01','2026-05-05','劳动节'),
    ('2026-06-19','2026-06-21','端午节'),('2026-09-25','2026-09-27','中秋节'),('2026-10-01','2026-10-07','国庆节')]
MAKEUP_2026={'2026-01-04','2026-02-14','2026-02-28','2026-05-09','2026-09-20','2026-10-10'}
SOURCE_2025='https://www.kashi.gov.cn/ksdqxzgs/c115966/202411/e116097ae08f4324ae5802863f2a96ba.shtml'
HOLIDAYS_2025=[('2025-01-01','2025-01-01','元旦'),('2025-01-28','2025-02-04','春节'),
    ('2025-04-04','2025-04-06','清明节'),('2025-05-01','2025-05-05','劳动节'),
    ('2025-05-31','2025-06-02','端午节'),('2025-10-01','2025-10-08','国庆中秋')]
MAKEUP_2025={'2025-01-26','2025-02-08','2025-04-27','2025-09-28','2025-10-11'}


def day_context(value):
    """返回星期、官方假期/补班与营销节点；不据此判断销量变化的因果。"""
    current=date.fromisoformat(value);weekend=current.weekday()>=5;known=current.year in (2025,2026)
    periods=HOLIDAYS_2025 if current.year==2025 else HOLIDAYS_2026
    holiday=next((name for start,end,name in periods if start<=value<=end),None) if known else None
    makeup=known and value in (MAKEUP_2025 if current.year==2025 else MAKEUP_2026)
    kind='makeup_workday' if makeup else 'holiday_break' if holiday else 'weekend_rest' if weekend else 'ordinary_workday'
    if not known:kind='schedule_unknown'
    labels={'makeup_workday':'调休工作日','holiday_break':f'{holiday}假期（含调休）','weekend_rest':'周末休息日','ordinary_workday':'普通工作日','schedule_unknown':'年度假期安排未知'}
    occasions=[]
    if value.endswith('-02-14'):occasions.append('情人节（营销节点，非法定假日）')
    if value.endswith('-05-20'):occasions.append('520（营销节点）')
    if value.endswith('-09-10'):occasions.append('教师节（纪念日，非法定假期）')
    if value.endswith('-12-25'):occasions.append('圣诞节（营销节点，非法定假期）')
    for name in ('母亲节','父亲节','感恩节'):
        if current==holiday_date(name,current.year):occasions.append(name+'（营销节点，非法定假日）')
    return {'date':value,'weekday':current.isoweekday(),'weekday_label':'周'+'一二三四五六日'[current.weekday()],
        'is_weekend':weekend,'schedule_known':known,'is_makeup_workday':makeup if known else None,
        'is_workday':kind in ('makeup_workday','ordinary_workday') if known else None,
        'holiday_name':holiday,'day_type':kind,'label':labels[kind],'occasions':occasions,
        'source_id':('cn-2025-official-20241112-v1' if current.year==2025 else VERSION) if known else 'weekday_calculation_only',
        'source_url':(SOURCE_2025 if current.year==2025 else SOURCE) if known else None,
        'status':'ok' if known else 'schedule_unknown'}


def calendar_range(start,end):
    """提供有上限的只读日历工具结果，完整保留来源及版本。"""
    a,b=date.fromisoformat(start),date.fromisoformat(end)
    if not 0<=(b-a).days<90:raise ValueError('日历查询范围须为1–90天。')
    return {'table':'calendar','version':VERSION,'source_url':SOURCE,
        'records':[day_context((a+timedelta(days=i)).isoformat()) for i in range((b-a).days+1)],
        'scope':'放假安排适用于全国一般日历，不表示花店必然休息；营销节点与法定假期分别标记。'}
