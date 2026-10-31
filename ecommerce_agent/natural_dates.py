"""口语日期规范化：以门店业务今天为锚点，补全结果可展示、可审计，不回退猜去年。"""
import re
from datetime import date, timedelta

FULL_DATE = r'(?<!\d)\d{4}(?:年|[-/])\d{1,2}(?:月|[-/])\d{1,2}(?:日|号)?(?!\d)'
FIXED_HOLIDAYS={'情人节':(2,14),'教师节':(9,10),'国庆节':(10,1),'元旦':(1,1),'圣诞节':(12,25)}
WEEKDAY_HOLIDAYS={'父亲节':(6,6,3),'母亲节':(5,6,2),'感恩节':(11,3,4)}


def spoken_number(text):
    """有限中文整数转数字，仅用于时间数量；不改商品名中的数字或规格。"""
    if text.isdecimal():return int(text)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if text in digits:return digits[text]
    for unit,value in [('百',100),('十',10)]:
        if unit in text:
            left,right=text.split(unit,1)
            return (spoken_number(left) if left else 1)*value+(spoken_number(right.lstrip('零〇')) if right.lstrip('零〇') else 0)
    raise ValueError('时间数量不是明确整数。')


def normalize_windows(question):
    """路由、对话继承和日期解析共享时间表达；一周指7个滚动完整自然日，不是上周。"""
    def replace(match):
        try:count=spoken_number(match[2])*(7 if match[3] in ('周','星期','礼拜') else 1)
        except ValueError:return match[0]
        return ('近' if match[1]=='这' else match[1])+str(count)+'天'
    return re.sub(r'(最近|近|过去|这)\s*([零〇一二两三四五六七八九十百\d]+)\s*(?:个)?\s*(天|日|周|星期|礼拜)',replace,question)


def holiday_date(name,year):
    """营销日按门店采用的公历约定：某月第几个星期几；与法定放假安排分开。"""
    if name in FIXED_HOLIDAYS:return date(year,*FIXED_HOLIDAYS[name])
    month,weekday,nth=WEEKDAY_HOLIDAYS[name];first=date(year,month,1)
    return first+timedelta(days=(weekday-first.weekday())%7+7*(nth-1))


def normalize_dates(question, today):
    """返回规范化文本、解析说明和错误；保留用户原话，由调用方冻结到任务卡。"""
    notes, errors, saved = [], [], []
    normalized_window=normalize_windows(question)
    if normalized_window!=question:notes.append('口语时间数量已规范化：'+normalized_window+'。')
    question=normalized_window
    def month_window(match):
        """自然月使用日历边界；不把上个月误当成最近30天，本月只含已结束日期。"""
        first=today.replace(day=1)
        if match[0] in ('上个月','上月'):
            end=first-timedelta(days=1);start=end.replace(day=1)
        else:start=first;end=today-timedelta(days=1)
        if end<start:errors.append('本月尚无已结束的自然日。')
        result=start.isoformat()+'至'+end.isoformat()
        notes.append('“'+match[0]+'”按自然月解析为'+result+'。')
        return result
    question=re.sub(r'上个月|上月|这个月|本月',month_window,question)
    def holiday(match):
        """节日按本年补全；标题中的“情人节鲜花”仍是营销词，不当成查询日。"""
        prefix=match[1] or '';name=match[2]
        year=int(prefix[:-1]) if prefix.endswith('年') and prefix[:-1].isdigit() else today.year+{'去年':-1,'前年':-2}.get(prefix,0)
        chosen=holiday_date(name,year)
        if re.match(r'期间|前后|活动期',question[match.end():]):
            errors.append(f'“{name}期间”没有固定经营起止日，请明确要查哪天到哪天。');return match[0]
        notes.append(f'“{match[0]}”按{name}当天解析为{chosen.isoformat()}'+('（按本年）' if not prefix else '')+'。')
        if not prefix and chosen>today:errors.append('按本年解析的节日尚未到来，请明确年份。')
        return chosen.isoformat()
    names='|'.join([*FIXED_HOLIDAYS,*WEEKDAY_HOLIDAYS])
    text=re.sub(r'(\d{4}年|今年|去年|前年)?('+names+r')(?!鲜花)',holiday,question)
    def protect(match):
        saved.append(match[0]); return f'§{len(saved)-1}§'
    text = re.sub(FULL_DATE, protect, text)
    # 10月1日至15日：先补全结束月份，随后统一补年份；跨月应明确写月份。
    text = re.sub(r'(\d{1,2})月(\d{1,2})[日号]?\s*(至|到|~|～)\s*(\d{1,2})[日号]',
                  lambda m: f'{m[1]}月{m[2]}日{m[3]}{m[1]}月{m[4]}日', text)
    def month_day(match):
        try:
            chosen = date(today.year, int(match[1]), int(match[2]))
            notes.append(f'“{match[0]}”按本年解析为{chosen.isoformat()}。')
            if chosen > today: errors.append('省略年份后得到未来日期，请明确年份；不会自动改为去年。')
            return chosen.isoformat()
        except ValueError:
            errors.append('日期不合法：'+match[0]); return match[0]
    text = re.sub(r'(?<![\d年/-])(\d{1,2})(?:月|/|-)(\d{1,2})(?:日|号)?(?!\d)', month_day, text)
    def weekday(match):
        offset = {'上': -1, '本': 0, '这': 0}[match[1]]
        weekday_index = '一二三四五六日天'.index(match[2])
        chosen = today - timedelta(days=today.weekday()) + timedelta(weeks=offset, days=min(weekday_index, 6))
        notes.append(f'“{match[0]}”按周一至周日解析为{chosen.isoformat()}。')
        return chosen.isoformat()
    text = re.sub(r'(上|本|这)(?:周|星期)([一二三四五六日天])', weekday, text)
    if '大前天' in text:
        chosen = (today-timedelta(days=3)).isoformat()
        notes.append(f'“大前天”解析为{chosen}。'); text = text.replace('大前天', chosen)
    for n, original in enumerate(saved): text = text.replace(f'§{n}§', original)
    return text, notes, errors
