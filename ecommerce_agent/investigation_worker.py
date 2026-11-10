"""销量暴跌调查子进程：读取冻结的外部证据，运行调查并交付可追溯报告。"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from .business_calendar import day_context
from .cancellation import checkpoint
from .data import DataStore
from .guardrails.policy_checker import check_action
from .provider import ChatProvider
from .runner import write_json
from .workflows import execute_task

PROFILE = 'sales_drop_investigation'


def freeze_context(task, store, book, source_path):
    """只冻结当前门店、商品与日期的本地记录；广告账本按门店而非 SKU 归因。"""
    if task.get('intent') != 'investigate_sales_change' or task['status'] != 'ready':
        return None
    if not re.search(r'销量(?:暴跌|骤降|断崖(?:式)?下跌)', task['raw_question']):
        return None
    sku, day = task['sku_id'], task['target_date']
    context = store.meta.get('business_context', {})
    store_id = context.get('store_id')
    if not source_path.is_file() and store.meta['sample_data']:
        source_path = Path(__file__).resolve().parents[1] / 'data' / 'investigation_context.sample.json'
    try:
        records = json.loads(source_path.read_text(encoding='utf-8')) if source_path.is_file() else {}
    except (ValueError, OSError):
        records = {}
    if not isinstance(records, dict):
        records = {}

    def matching(domain):
        rows = records.get(domain)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)
                and row.get('store_id') == store_id and row.get('date') == day
                and isinstance(row.get('source'), str) and row['source'].strip()]

    competitors = [row for row in matching('competitors') if row.get('sku_id') == sku]
    weather = matching('weather')
    ads = []
    if book is not None:
        target = date.fromisoformat(day)
        for offset in range(6, -1, -1):
            observed_day = (target - timedelta(days=offset)).isoformat()
            if observed_day not in book.dates:
                continue
            report = book.report(observed_day)
            ads.append({
                'date': observed_day,
                'spend_cents': report['totals']['spend_cents'],
                'clicks': report['totals']['clicks'],
                'enabled_hours': sum(hour['enabled'] for hour in report['hours']),
                'source': report['source_id'],
                'sample_data': report['sample_data'],
            })
    return {'profile': PROFILE, 'competitors': competitors[:20], 'weather': weather[:5], 'ads': ads}


def closure_report(task, context, state):
    """依据读取到的记录生成三类证据及可复核结论；缺数与确认停投分别表达。"""
    day = task['target_date']
    sections = []
    competitors = []
    price_drop = False
    for row in context['competitors']:
        previous, current = row.get('previous_price_cents'), row.get('price_cents')
        name = row.get('name')
        if isinstance(name, str) and name.strip() and type(previous) is int and type(current) is int and previous > 0 and current >= 0:
            change = (current - previous) / previous
            price_drop |= change < 0
            label = '下降' if change < 0 else '上涨' if change > 0 else '持平'
            delta = f'{label} {abs(change):.0%}' if change else label
            competitors.append(f"{name}：¥{previous / 100:.2f} → ¥{current / 100:.2f}（{delta}）。来源：{row['source']}。")
    sections.append({'title': '竞品价格', 'items': competitors or ['缺少该商品当日的竞品价格记录，无法核实降价。']})
    calendar = day_context(day)
    occasions = ([calendar['holiday_name']] if calendar['holiday_name'] else []) + calendar['occasions']
    conditions = [f"{row['summary']}。来源：{row['source']}。" for row in context['weather']
                  if isinstance(row.get('summary'), str) and row['summary'].strip()]
    conditions.append('节日/营销节点：' + '、'.join(occasions) if occasions else '经营日历未标注当日节日或营销节点。')
    if len(conditions) == 1:
        conditions.insert(0, '缺少该门店当日的天气记录。')
    sections.append({'title': '天气与节日', 'items': conditions})
    ads = context['ads']
    entries = [
        f"{row['date']}：开启 {row['enabled_hours']} 小时，扣费 ¥{row['spend_cents'] / 100:.2f}，"
        f"{row['clicks']} 次点击（门店账本{ '，模拟数据' if row['sample_data'] else ''}）。"
        for row in ads
    ]
    target = next((row for row in ads if row['date'] == day), None)
    ads_stopped = bool(target and target['enabled_hours'] == 0
                       and any(row['enabled_hours'] for row in ads if row['date'] < day))
    if ads_stopped:
        entries.append('账本显示当日全店未开启投放，近期曾开启。是否未续投、是否影响该 SKU，仍需核对投放后台。')
    elif not target:
        entries.append('缺少调查当日的投放记录，不能判断是否续投。')
    sections.append({'title': '近七日广告投放', 'items': entries})
    sales = (state.get('analysis') or {}).get('facts', {}).get('units_sold')
    summary = f"销量由对比日的 {sales['before']} 件变为 {sales['after']} 件。" if sales else '本次未取得可比销量。'
    if price_drop:
        summary += '竞品记录显示有降价，建议核对同规格商品与降价时段。'
    if ads_stopped:
        summary += '门店广告账本显示当日未开启投放，建议核对续投记录。'
    summary += '现有记录无法单独证明销量变化的原因。'
    if price_drop:
        action = {'kind': 'discount', 'discount_percent': 80}
        description = f"建议对 SKU {task['sku_id']} 做 8 折促销试验。"
    elif ads_stopped:
        action = {'kind': 'advertising', 'budget_cents': 100_000}
        description = f"建议复核 SKU {task['sku_id']} 的广告账户后重启投放，单次预算 1000 元。"
    else:
        action = {'kind': 'manual_review'}
        description = '建议由运营核对同规格竞品、缺货时段及投放记录，再决定促销或投放方案。'
    policy = task.get('operating_settings_snapshot', {}).get('approval_policy')
    recommendation = {'action': action, 'description': description, **check_action(action, policy)}
    return {'title': '调查结案报告', 'summary': summary, 'sections': sections,
            'recommendation': recommendation}


def run_in_worker(store, sku, day, output, *, task_context, on_event, provider=None,
                  max_steps=16, timeout_seconds=180):
    """将冻结输入通过管道交给独立 Python 进程；取消或超时会终止子进程。"""
    config = None
    if provider is not None:
        config = {'api_base_url': provider.url.removesuffix('/chat/completions'), 'model': provider.model,
                  'thinking_disabled': provider.thinking_disabled, 'max_output_tokens': provider.max_tokens,
                  'key': provider.key}
    payload = json.dumps({
        'store': store.snapshot(), 'sku': sku, 'day': day, 'output': str(output),
        'task': task_context, 'provider': config, 'max_steps': max_steps, 'timeout_seconds': timeout_seconds,
    }, ensure_ascii=False).encode('utf-8')
    with subprocess.Popen(
        [sys.executable, '-B', '-m', 'ecommerce_agent.investigation_worker'],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    ) as process:
        deadline = time.monotonic() + timeout_seconds + 10
        try:
            while True:
                checkpoint()
                if time.monotonic() >= deadline:
                    raise TimeoutError('调查 Worker 超时。')
                try:
                    stdout, _ = process.communicate(payload, timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            if process.returncode:
                raise RuntimeError('调查 Worker 执行失败。')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    result = json.loads(stdout)
    for entry in result['events']:
        checkpoint()
        on_event(entry)
    return result['state']


def main():
    """子进程入口：输出结果与轨迹，凭据仅在管道和进程内存中使用。"""
    payload = json.loads(sys.stdin.buffer.read())
    config = payload['provider']
    provider = ChatProvider(config, api_key=config.pop('key')) if config else None
    task = payload['task']
    # 外部来源文本不进入模型的任务提示，只用于宿主证据报告。
    context = task.pop('investigation_context')
    events = []
    state = execute_task(
        DataStore.from_document(payload['store']), payload['sku'], payload['day'], payload['output'],
        task_context=task, provider=provider, max_steps=payload['max_steps'],
        timeout_seconds=payload['timeout_seconds'], on_event=events.append,
    )
    state['worker_pid'] = os.getpid()
    if state['status'] == 'completed':
        report = closure_report(task, context, state)
        state['closure_report'] = report
        directory = Path(state['run_dir'])
        artifact = directory / 'report.md'
        lines = ['', '## ' + report['title'], '', report['summary']]
        for section in report['sections']:
            lines += ['', '### ' + section['title'], *('- ' + item for item in section['items'])]
        recommendation = report['recommendation']
        lines += ['', '### 整改建议（Action Recommendation）', recommendation['description'],
                  recommendation['label'], recommendation['reason']]
        temporary = artifact.with_suffix('.md.tmp')
        temporary.write_text(artifact.read_text(encoding='utf-8') + '\n'.join(lines) + '\n', encoding='utf-8')
        temporary.replace(artifact)
        write_json(directory / 'run.json', state)
    sys.stdout.buffer.write(json.dumps({'state': state, 'events': events}, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
