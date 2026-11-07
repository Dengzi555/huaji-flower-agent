"""将冻结的组合查询证据整理为经营回答；不补齐缺失值，不调用模型编造解释。"""
from collections import defaultdict
from datetime import date, timedelta


def short_date(value):
    """ISO 业务日期转为适合阅读的月日，查询边界仍保留完整年份。"""
    day = date.fromisoformat(value)
    return f'{day.month}月{day.day}日'


def build_answer(task, sections):
    """输入任务和独立事项的原始结果，输出可复算的正文、数据缺口和检查建议。

    趋势包含窗口外对比历史，只在请求日期内计算本期销量；每款缺货时长
    独立统计。结果可以在读取历史任务时重新生成，不改写原始证据。
    """
    scope = task.get('product_scope') or {}
    plan = task.get('query_plan') or {}
    sales = next((s for s in sections if s['domain'] == 'sales'), {})
    trend = sales.get('trend') or {}
    start = plan.get('start_date') or trend.get('start_date') or task.get('target_date')
    end = plan.get('end_date') or trend.get('end_date') or task.get('target_date')
    in_window = lambda r: bool(start and end and start <= r.get('date', '') <= end)
    products = {p['sku_id']: p for p in task.get('candidates', []) if p.get('sku_id')}
    products.update({p['sku_id']: p for p in sales.get('group_items', []) if p.get('sku_id')})

    def name(sku):
        product = products.get(sku, {})
        return ' '.join(filter(None, [product.get('short_name') or product.get('name') or sku, product.get('variant')]))

    parts, next_steps, missing = [], [], set()
    for section in sections:
        domain = section['domain']
        rows = [r for r in (trend.get('records', []) if domain == 'sales' else section.get('records', [])) if in_window(r)]
        missing.update(r['date'] for r in rows if r.get('status') in ('unavailable', 'delayed', 'partial'))
        valid = [r for r in rows if r.get('status') == 'ok']
        paragraphs = []
        title = {'sales': '销量表现', 'inventory': '缺货情况', 'promotions': '促销活动'}.get(domain, domain)
        if not valid:
            paragraphs.append(f'暂未查到可用的{title}记录。')
        elif domain == 'sales':
            total = sum(r['units_sold'] for r in valid)
            paragraphs.append(f'已回传日期合计售出{total}束/桶。' if missing else f'本期合计售出{total}束/桶。')
            latest = max(valid, key=lambda r: r['date'])
            previous_day = (date.fromisoformat(latest['date']) - timedelta(days=1)).isoformat()
            previous = next((r for r in valid if r['date'] == previous_day), None)
            change = ''
            if previous:
                delta = latest['units_sold'] - previous['units_sold']
                change = f'，比{short_date(previous_day)}' + (f'多{delta}束/桶' if delta > 0 else f'少{-delta}束/桶' if delta < 0 else '持平')
            paragraphs.append(f"最近有记录的{short_date(latest['date'])}售出{latest['units_sold']}束/桶{change}。")
            if len(valid) > 1:
                high = max(r['units_sold'] for r in valid)
                low = min(r['units_sold'] for r in valid)
                if high != low:
                    peaks = '、'.join(short_date(r['date']) for r in valid if r['units_sold'] == high)
                    troughs = '、'.join(short_date(r['date']) for r in valid if r['units_sold'] == low)
                    paragraphs.append(f'已回传日期中，{peaks}销量最高（{high}束/桶），{troughs}最低（{low}束/桶）。')
            items = sorted((p for p in sales.get('group_items', []) if isinstance(p.get('units_sold'), (int, float))), key=lambda p: p['units_sold'], reverse=True)
            leaders = [p for p in items[:3] if p['units_sold'] > 0]
            if leaders:
                paragraphs.append('销量靠前：' + '；'.join(f"{name(p['sku_id'])} {p['units_sold']}束/桶" for p in leaders) + '。')
        elif domain == 'inventory':
            stockouts = defaultdict(list)
            for row in valid:
                if (row.get('out_of_stock_minutes') or 0) > 0:
                    stockouts[row['sku_id']].append(row)
            if stockouts:
                paragraphs.append(f'{len(stockouts)}款商品出现过缺货记录，建议优先看：')
                ordered = sorted(stockouts.items(), key=lambda item: sum(r['out_of_stock_minutes'] for r in item[1]), reverse=True)
                for sku, records in ordered[:3]:
                    minutes = sum(r['out_of_stock_minutes'] for r in records)
                    days = '、'.join(short_date(r['date']) for r in sorted(records, key=lambda r: r['date']))
                    paragraphs.append(f'{name(sku)}：{days}有缺货，所列日期累计{minutes}分钟。')
                next_steps.append(f'优先核对{name(ordered[0][0])}的备花和可售状态，检查缺货日期是否也出现销量下降。')
            else:
                paragraphs.append('已回传的库存监测记录中没有缺货记录。')
        elif domain == 'promotions':
            campaigns = defaultdict(lambda: {'dates': set(), 'products': set()})
            for row in valid:
                if row.get('active'):
                    campaign = campaigns[row.get('campaign_name') or '未命名活动']
                    campaign['dates'].add(row['date']); campaign['products'].add(row['sku_id'])
            if campaigns:
                for campaign, detail in sorted(campaigns.items()):
                    days = '、'.join(short_date(day) for day in sorted(detail['dates']))
                    paragraphs.append(f'{days}参加「{campaign}」，涉及{len(detail["products"])}款商品。')
                next_steps.append('结合上面的每日销量，核对活动日的成交变化，再判断是否继续同类优惠。')
            else:
                paragraphs.append('已回传的活动记录中没有开启的促销活动。')
        parts.append({'domain': domain, 'title': title, 'paragraphs': paragraphs})
    missing_dates = sorted(missing)
    notice = ('、'.join(short_date(day) for day in missing_dates) + '部分记录尚未回传，以下先看已有数据。') if missing_dates else ''
    label = scope.get('label') or task.get('sku_name') or '所选商品'
    return {'title': f'{label}经营情况', 'notice': notice, 'missing_dates': missing_dates,
            'parts': parts, 'next_steps': next_steps,
            'period': f'{start} — {end}' if start and end else ''}
