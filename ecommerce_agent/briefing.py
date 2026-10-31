"""门店每日简报（Monitor）：用昨日基线与同星期参考扫一遍全店商品，挑出值得调查的 3–5 条。

这一层只回答「今天先看哪几款」，不回答「为什么」。它和单任务调查的分工：

- 触发：昨日绝对损失、同星期中位数偏离、缺货分钟、访客骤降；活动与日历背景不一致时标注。
- 不触发：新品（上架不足 14 天）、数据缺失/延迟、未达到本类型商品的绝对损失线。
- 输出：候选条目 + 同星期参考 + 活动/日历背景 + 证据来源 + 可直接提交的调查任务草稿。

为什么不在扫描时直接调用 `assessment.assess`：经营关注分级要求四类来源完整，而年度库存
只覆盖营业时段（monitored_minutes=960），按完整口径会把全部商品判为「证据不足」。简报
因此使用独立的筛查策略（`monitor-v1`），结果是「值得核查」的线索，不是经营结论。

为什么同时保留昨日基线与同星期中位数：经营日报习惯看环比，但周末/活动/假期会让环比
失真；同星期中位数用来判断「跌」是否只是星期效应，两者都写进报告，避免只给一个数字。
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .assessment import POLICIES
from .business_calendar import day_context
from .data import DataStore
from .task_parser import ProductCatalog

POLICY_VERSION = 'monitor-v1'
NEW_PRODUCT_DAYS = 14
MIN_STOCKOUT_MINUTES = 240
TRAFFIC_DROP_PCT = 40
WINDOW_DAYS = 35


def _tier(product):
    """复用关注分级的三档商品类型；缺省按常规款，不凭空抬高阈值。"""
    tags = (product or {}).get('business_tags') or []
    return next((tag for tag in ('爆款', '长尾款') if tag in tags), '常规款')


def _promotions(store, sku, start, end):
    """活动记录按日期索引；缺失日期不补假记录，比较时只比较双方都已知的日期。"""
    return {row['date']: row for row in store.rows('promotions', sku=sku, start=start, end=end)}


def _weekly_rows(rows, promos, day, target_calendar):
    """取最多 4 个可比同星期：日历类型、假期一致，且活动状态相同。"""
    d0 = date.fromisoformat(day)
    history = []
    for weeks in range(1, 5):
        previous = (d0 - timedelta(days=7 * weeks)).isoformat()
        row = rows.get(previous)
        if not row or row['status'] != 'ok':
            continue
        calendar = day_context(previous)
        if (calendar['day_type'], calendar['holiday_name']) != (target_calendar['day_type'], target_calendar['holiday_name']):
            continue
        if previous in promos and day in promos:
            before = (promos[previous].get('active'), promos[previous].get('campaign_name'))
            after = (promos[day].get('active'), promos[day].get('campaign_name'))
            if before != after:
                continue
        history.append({'date': previous, 'units_sold': row['units_sold']})
    return history


def detect(store, catalog, sku, day):
    """判断单款是否值得关注。返回候选字典；不值得关注时返回 None 或跳过原因。"""
    product = catalog.products.get(sku)
    if not product:
        return {'skipped': 'uncatalogued', 'sku_id': sku}
    d0 = date.fromisoformat(day)
    target_calendar = day_context(day)
    listed = product.get('listed_at')
    if listed:
        age = (d0 - date.fromisoformat(listed)).days
        if age < NEW_PRODUCT_DAYS:
            return {'skipped': 'new_listing', 'sku_id': sku, 'age': age}
    start = (d0 - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = {row['date']: row for row in store.rows('sales', sku=sku, start=start, end=day)}
    target = rows.get(day)
    if not target or target['status'] != 'ok':
        return {'skipped': 'unavailable', 'sku_id': sku}
    promos = _promotions(store, sku, start, day)
    history = _weekly_rows(rows, promos, day, target_calendar)
    tier = _tier(product)
    policy = POLICIES[tier]
    signals = []

    # 昨日基线：与经营日报的环比口径一致；同星期历史不足时这是唯一可用的短期参照。
    previous_day = (d0 - timedelta(days=1)).isoformat()
    previous = rows.get(previous_day)
    loss, drop = None, None
    if previous and previous['status'] == 'ok' and previous['units_sold'] > 0:
        loss = previous['units_sold'] - target['units_sold']
        drop = loss / previous['units_sold']

    weekly = {'n': len(history), 'dates': [item['date'] for item in history],
              'median_units': None, 'target_vs_weekly_pct': None, 'weekday_effect_suspected': None}
    center = None
    if len(history) >= 3:
        center = median([item['units_sold'] for item in history])
        weekly['median_units'] = center
        weekly['target_vs_weekly_pct'] = round((target['units_sold'] - center) / center * 100, 1) if center else None
        weekly['weekday_effect_suspected'] = target['units_sold'] >= center
        boundary = center - max(policy['min_loss'], center * policy['min_drop_pct'] / 100)
        if target['units_sold'] <= boundary and center > target['units_sold']:
            signals.append({'type': 'sales_drop_weekly', 'loss_units': round(center - target['units_sold'], 1),
                            'drop_pct': round((center - target['units_sold']) / center * 100, 1),
                            'text': f'低于同星期中位数{center}件，按{tier}关注线判定'})

    # 昨日跌幅达到绝对损失线才触发；若同星期参考显示当日并未低于常态，先按星期效应排除。
    if loss is not None and loss >= max(policy['min_loss'], previous['units_sold'] * policy['min_drop_pct'] / 100):
        if center is None or target['units_sold'] < center:
            signals.append({'type': 'sales_drop_prev_day', 'loss_units': loss,
                            'drop_pct': round(drop * 100, 1),
                            'text': f'销量较昨日减少{loss}件（{drop*100:.1f}%），达到{tier}关注线'})

    # 缺货分钟只有在整日监测完整时才作为独立信号；营业时段监测不能推全日缺货。
    inventory = list(store.rows('inventory', sku=sku, start=day, end=day))
    if inventory and inventory[0]['status'] == 'ok':
        monitored = inventory[0]['monitored_minutes']
        out = inventory[0]['out_of_stock_minutes']
        if monitored >= 1440 and out >= MIN_STOCKOUT_MINUTES:
            signals.append({'type': 'stockout', 'out_of_stock_minutes': out,
                            'monitored_minutes': monitored, 'text': f'整日缺货{out}分钟'})

    # 访客骤降本身不是销量结论，但能解释「为什么先查这一款」。
    traffic = {row['date']: row for row in store.rows('traffic', sku=sku, start=previous_day, end=day)}
    before, after = traffic.get(previous_day), traffic.get(day)
    if before and after and before['status'] == 'ok' and after['status'] == 'ok' and before['visitors'] >= 100:
        visitors_drop = (before['visitors'] - after['visitors']) / before['visitors']
        if visitors_drop >= TRAFFIC_DROP_PCT / 100:
            signals.append({'type': 'traffic_drop', 'visitors_before': before['visitors'],
                            'visitors_after': after['visitors'], 'drop_pct': round(visitors_drop * 100, 1),
                            'text': f'访客由{before["visitors"]}降至{after["visitors"]}（-{visitors_drop*100:.0f}%）'})

    if not signals:
        return None

    previous_calendar = day_context(previous_day)
    context_differs = ((previous_calendar['day_type'], previous_calendar['holiday_name'])
                       != (target_calendar['day_type'], target_calendar['holiday_name']))
    promo = promos.get(day) or {}
    previous_promo = promos.get(previous_day) or {}
    if (previous_promo.get('active'), previous_promo.get('campaign_name')) != (promo.get('active'), promo.get('campaign_name')):
        context_differs = True
    priority = max(0.0, loss or 0)
    priority += sum(item.get('out_of_stock_minutes', 0) / 60 for item in signals if item['type'] == 'stockout')
    priority += sum(item.get('drop_pct', 0) / 10 for item in signals if item['type'] == 'traffic_drop')
    return {
        'sku_id': sku, 'name': product.get('name'), 'variant': product.get('variant'), 'tier': tier,
        'target_units': target['units_sold'],
        'previous_units': previous['units_sold'] if previous and previous['status'] == 'ok' else None,
        'loss_units': loss, 'loss_pct': round(drop * 100, 1) if drop is not None and drop > 0 else None,
        'weekly': weekly,
        'calendar': {'day_type': target_calendar['day_type'], 'label': target_calendar['label'],
                     'holiday_name': target_calendar['holiday_name'], 'occasions': target_calendar['occasions']},
        'campaign': {'active': promo.get('active'), 'campaign_name': promo.get('campaign_name')},
        'context_differs': context_differs,
        'signals': signals,
        'priority': round(priority, 2),
        'source_ids': [{'date': day, 'source_id': target.get('source_id'),
                        'observed_at': target.get('observed_at')}],
    }


def scan(store, catalog, day, *, limit=5, sku_ids=None, store_id=None):
    """扫描门店全部商品并按严重度取前 limit 条；不做因果推断，也不为了凑数降阈值。"""
    if store_id is None:
        store_id = (store.meta.get('business_context') or {}).get('store_id') or 'default'
    counts = {'scanned': 0, 'new_listing': 0, 'unavailable': 0, 'uncatalogued': 0, 'candidates': 0}
    items = []
    for sku in sorted(sku_ids if sku_ids is not None else store.skus):
        counts['scanned'] += 1
        result = detect(store, catalog, sku, day)
        if result is None:
            continue
        if 'skipped' in result:
            counts[result['skipped']] = counts.get(result['skipped'], 0) + 1
            continue
        counts['candidates'] += 1
        items.append(result)
    items.sort(key=lambda item: (-item['priority'], -item['loss_units'] if item['loss_units'] else 0, item['sku_id']))
    selected = items[:max(0, limit)]
    return {
        'schema': 'briefing-v1', 'store_id': store_id, 'date': day,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'policy': {'version': POLICY_VERSION, 'baseline': 'previous_day + same_weekday reference',
                   'thresholds': POLICIES, 'min_stockout_minutes': MIN_STOCKOUT_MINUTES,
                   'traffic_drop_pct': TRAFFIC_DROP_PCT, 'new_product_days': NEW_PRODUCT_DAYS,
                   'caveat': '筛查信号，不是经营结论；同星期参考只用于判断星期效应，不证明因果。'},
        'counts': {**counts, 'returned': len(selected)},
        'items': selected,
    }


def _question(item, day):
    """按触发信号组织问题，避免把缺货/访客异常也写成“销量下降”。"""
    types = {signal['type'] for signal in item['signals']}
    if types & {'sales_drop_prev_day', 'sales_drop_weekly'}:
        reason = '销量下降原因'
    elif 'stockout' in types:
        reason = '缺货与销量变化原因'
    else:
        reason = '访客下降原因'
    return f"{item['name']} {day} {reason}"


def build_tasks(result, data_path):
    """把候选转成任务草稿：问题、冻结对象、命令；不自动执行，也不消耗模型。"""
    tasks = []
    for rank, item in enumerate(result['items'], start=1):
        question = _question(item, result['date'])
        payload = {'question': question, 'sku': item['sku_id'], 'date': result['date']}
        command = (f'python -m ecommerce_agent investigate --data "{data_path}" '
                   f'--sku {item["sku_id"]} --date {result["date"]}')
        tasks.append({'rank': rank, 'sku_id': item['sku_id'], 'name': item['name'],
                      'payload': payload, 'command': command,
                      'file': f'tasks/{rank:02d}-{item["sku_id"]}.json',
                      'signals': [signal['text'] for signal in item['signals']]})
    result['tasks'] = tasks
    return result


def _change_text(item):
    """销量变化按方向显示；缺货但销量上升的商品不能写成“下降 700%”。"""
    loss = item.get('loss_units')
    if loss is None:
        return ''
    if loss > 0:
        return f"（-{item['loss_pct']}%）"
    if loss < 0:
        return f"（+{abs(loss)}件）"
    return '（持平）'


def render_markdown(result):
    """生成可粘贴到日报/工单的简报；结论与证据来源分行，避免只留一行数字。"""
    counts = result['counts']
    lines = [f"# 门店简报 · {result['store_id']} · {result['date']}", '',
             f"扫描 {counts['scanned']} 款，候选 {counts['candidates']} 条，输出 {counts['returned']} 条；"
             f"新品跳过 {counts['new_listing']}，数据缺失 {counts['unavailable']}。", '',
             f"> 策略 `{POLICY_VERSION}`：昨日基线触发绝对损失，同星期中位数判断星期效应，"
             f"活动/日历背景不同时标注。筛查信号不是经营结论。", '']
    for rank, item in enumerate(result['items'], start=1):
        weekly = item['weekly']
        lines += [f"## {rank}. {item['name']}", '',
                  f"- 销量：昨日 {item['previous_units'] if item['previous_units'] is not None else '—'}"
                  f" → 当日 {item['target_units']} 件" + _change_text(item),
                  f"- 信号：" + '；'.join(signal['text'] for signal in item['signals'])]
        if weekly['median_units'] is not None:
            lines.append(f"- 同星期参考：中位数 {weekly['median_units']} 件（{weekly['n']} 个可比周），"
                         f"当日相对 {weekly['target_vs_weekly_pct']}%"
                         + ('；当日未低于同星期常态，跌幅可能含星期效应' if weekly['weekday_effect_suspected'] else ''))
        else:
            lines.append(f"- 同星期参考：仅 {weekly['n']} 个可比周，不足以判断常态")
        background = f"- 背景：{item['calendar']['label']}"
        if item['calendar']['holiday_name']:
            background += f"（{item['calendar']['holiday_name']}）"
        if item['calendar']['occasions']:
            background += '；' + '、'.join(item['calendar']['occasions'])
        background += f"；活动：{item['campaign']['campaign_name'] or '无'}"
        if item['context_differs']:
            background += '；与昨日背景不同，需谨慎比较'
        lines += [background,
                  f"- 来源：{item['source_ids'][0]['source_id']} / {item['source_ids'][0]['date']}", '']
    lines += ['## 调查任务草稿', '']
    if result.get('tasks'):
        for task in result['tasks']:
            lines += [f"{task['rank']}. `{task['payload']['question']}`", '',
                      f"   ```bash", f"   {task['command']}", f"   ```", '']
    else:
        lines.append('当日未达到关注线；不为了凑数下调阈值。')
    lines += ['任务草稿也可直接 POST 到 `/api/tasks`（字段：question、sku、date）。']
    return '\n'.join(lines) + '\n'


def write_briefing(result, output_dir):
    """落盘 JSON、Markdown 与任务草稿；返回路径，便于 CLI 打印和测试断言。"""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'tasks').mkdir(exist_ok=True)
    for task in result.get('tasks', []):
        path = output / task['file']
        path.write_text(json.dumps(task['payload'], ensure_ascii=False, indent=2), encoding='utf-8')
    json_path = output / 'briefing.json'
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    markdown_path = output / 'briefing.md'
    markdown_path.write_text(render_markdown(result), encoding='utf-8')
    return {'json': json_path, 'markdown': markdown_path, 'tasks': [output / task['file'] for task in result.get('tasks', [])]}


def resolve_annual(annual_root):
    """读取 active.json 指向的版本目录；年度数据不入库，缺失时给出可操作的错误。"""
    annual_root = Path(annual_root)
    pointer = annual_root / 'active.json'
    if not pointer.exists():
        raise ValueError(f'未找到年度数据版本（{pointer}）；请先生成年度数据或改用 --data。')
    version = json.loads(pointer.read_text(encoding='utf-8'))['version']
    path = annual_root / version
    if not (path / 'manifest.json').exists():
        raise ValueError(f'年度版本 {version} 未完整发布。')
    return path


def run_briefing(args):
    """CLI 入口：解析门店/日期→扫描→落盘→打印摘要；不调用模型、不写业务数据。"""
    root = Path(__file__).resolve().parent.parent
    day = args.date
    try:
        date.fromisoformat(day)
    except ValueError:
        raise SystemExit('--date 需为 YYYY-MM-DD 格式。')
    if args.data:
        data_path = Path(args.data)
        catalog_path = Path(args.catalog)
        store = DataStore(data_path)
        store_id = (store.meta.get('business_context') or {}).get('store_id') or data_path.stem
    else:
        if not args.store:
            raise SystemExit('需要 --store 或 --data 指定门店数据。')
        annual = resolve_annual(Path(args.annual))
        data_path = annual / f'{args.store}.sqlite3'
        if not data_path.exists():
            raise SystemExit(f'年度数据中没有门店 {args.store}。')
        catalog_path = annual / 'products.json'
        store = DataStore(data_path)
        store_id = args.store
    catalog = ProductCatalog(catalog_path, store.skus)
    dates = store.dates('sales') or []
    if not dates:
        raise SystemExit('数据中没有销售记录。')
    if not dates[0] <= day <= dates[-1]:
        raise SystemExit(f'--date 超出数据范围：{dates[0]} 至 {dates[-1]}。')
    result = scan(store, catalog, day, limit=args.limit, store_id=store_id)
    build_tasks(result, str(data_path))
    result['data_path'] = str(data_path)
    output = Path(args.output) if args.output else root / 'runs/briefing' / store_id / day
    paths = write_briefing(result, output)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    counts = result['counts']
    print(f"门店 {store_id} · {day}：扫描 {counts['scanned']} 款，候选 {counts['candidates']} 条，输出 {counts['returned']} 条")
    print(f"（新品跳过 {counts['new_listing']}，数据缺失 {counts['unavailable']}；筛查信号不是经营结论）")
    for task in result.get('tasks', []):
        item = result['items'][task['rank'] - 1]
        print(f"  {task['rank']}. {item['name']}  " + '；'.join(task['signals']))
    print(f"报告：{paths['markdown']}")
    if result.get('tasks'):
        print(f"任务草稿：{paths['tasks'][0].parent}（可用 investigate 或 POST /api/tasks 执行）")
    else:
        print('当日未达到关注线；不为了凑数下调阈值。')
    return 0