"""经营时间范围与对比基线：自然日窗口、明确日期对比，不让模型补造日期。"""
import re
from datetime import date,timedelta
from .natural_dates import normalize_windows

DATE_PATTERN=r'(?<!\d)(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|号)?(?!\d)'
WINDOW_PATTERN=r'(?:最近|近|过去)\s*(\d{1,3})\s*天'


def has_window(question):
    """识别有限的完整窗口表达，不把“最近”这种无天数表达默认成7天。"""
    question=normalize_windows(question)
    if re.search(r'上个月|上月|这个月|本月',question):return True
    return bool(re.search(WINDOW_PATTERN,question) or re.search(r'上周(?!同|的同|[一二三四五六日天])|前周(?!同|的同)|本周(?![一二三四五六日天])',question) or
                re.search(r'到|至|~|～',question) and (len(re.findall(DATE_PATTERN,question))==2 or re.search(r'\d{1,2}月\d{1,2}',question)))


def resolve_scope(question,today,parse_dates,date_override=None):
    """返回目标日、展示范围、主基线和错误；最多60个自然日，今天/未来不参与整日比较。"""
    errors=[];explicit=[]
    question=normalize_windows(question)
    remainder=re.sub(DATE_PATTERN,'',question)
    if re.search(r'\d{1,2}月\d{1,2}[日号]?|\d{1,2}[-/]\d{1,2}',remainder):errors.append('日期缺少年份，请使用YYYY-MM-DD。')
    for match in re.finditer(DATE_PATTERN,question):
        try:explicit.append(date(*map(int,match.groups())).isoformat())
        except ValueError:errors.append('日期不合法：'+match.group(0))
    window=False;start=end=None;target=None;primary='previous_calendar_day';offset=1
    if re.search(r'上周同|前周同|上星期同',question):primary='same_weekday_previous_week';offset=7
    elif re.search(r'(?:相比|对比|比较|比).*前天',question):primary='two_days_before';offset=2
    count=re.search(WINDOW_PATTERN,question)
    range_expression=len(explicit)==2 and bool(re.search(r'到|至|~|～',question))
    if count:
        window=True;n=int(count[1]);end=(today-timedelta(days=1)).isoformat()
        if not 1<=n<=60:errors.append('趋势窗口须为1–60天。')
        else:start=(date.fromisoformat(end)-timedelta(days=n-1)).isoformat()
    elif range_expression:window=True;start,end=explicit
    elif re.search(r'上周(?!同|的同)|前周(?!同|的同)|本周',question):
        window=True;monday=today-timedelta(days=today.weekday())
        if '本周' in question:start=monday.isoformat();end=(today-timedelta(days=1)).isoformat()
        else:start=(monday-timedelta(days=7)).isoformat();end=(monday-timedelta(days=1)).isoformat()
    if window:
        target=end
        if start and end and not 0<=(date.fromisoformat(end)-date.fromisoformat(start)).days<60:errors.append('查询范围需按起止日期顺序填写，且最多60天。')
        # 同时写多个窗口或给范围又给相对日时不猜测优先级。
        if count and explicit:errors.append('同时指定相对窗口和完整日期，请只保留一种范围。')
    else:
        # 比较语句后面的相对日是相对目标日的基线，不作为第二个目标日期。
        anchor=re.split(r'相比|对比|比较|比(?=昨天|前天|上周|\d{4})',question,maxsplit=1)[0]
        if '昨天' in anchor and re.search(r'比前天|对比前天|相比前天',question):offset=1;primary='previous_calendar_day'
        anchor=re.sub(r'上周同(?:一)?(?:天|日)|前周同(?:一)?(?:天|日)|上星期同(?:一)?(?:天|日)','',anchor)
        days,extra=parse_dates(anchor,today);errors.extend(extra)
        if len(explicit)>=2 and re.search(r'相比|对比|比较|\bvs\b',question):target=explicit[0]
        elif len(days)==1:target=days[0]
        elif len(days)>1:errors.append('问题包含多个目标日期，请明确范围或对比关系。')
        if target:start=(date.fromisoformat(target)-timedelta(days=13)).isoformat();end=target
    if date_override:
        chosen=date.fromisoformat(date_override).isoformat()
        if target and target!=chosen:errors.append('补充日期与问题中的目标日期不一致。')
        else:
            target=chosen
            if not window:start=(date.fromisoformat(chosen)-timedelta(days=13)).isoformat();end=chosen
    if not target:errors.append('请补充目标日期或明确天数范围，例如2026-10-25、昨天或近7天。')
    elif date.fromisoformat(target)>=today:errors.append('仅查询已结束的自然日，今天和未来不与完整日混比。')
    comparison=(date.fromisoformat(target)-timedelta(days=offset)).isoformat() if target else None
    if not window and len(explicit)==2 and re.search(r'相比|对比|比较|\bvs\b',question):comparison=explicit[1];primary='explicit_baseline'
    # “昨天对比10月20日”只有一个完整日期，它是基线，不能静默退回前一日。
    baseline_match=re.search(r'(?:相比|对比|比较|比)\s*('+DATE_PATTERN+')',question)
    if baseline_match and not window:
        parsed_baseline=re.search(DATE_PATTERN,baseline_match[1])
        try:comparison=date(*map(int,parsed_baseline.groups())).isoformat();primary='explicit_baseline'
        except ValueError:errors.append('指定基线日期不合法。')
    if comparison and target and comparison>=target:errors.append('对比基线应早于目标日期。')
    if start and end and comparison and (date.fromisoformat(end)-min(date.fromisoformat(start)-timedelta(days=28),date.fromisoformat(comparison))).days>=90:
        errors.append('指定基线与窗口相距过远，当前含基线的读取范围最多90天。')
    return {'target_date':target,'comparison_date':comparison,'comparison_policy':primary,'trend_start':start,'trend_end':end,
            'is_window':window,'errors':errors}
