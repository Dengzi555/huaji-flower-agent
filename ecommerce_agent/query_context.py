"""查询条件的保存、继承与逐店重放；不携带指标、证据或执行权限。

会话负责判断用户在改什么，本模块集中保存条件并合并修改。准入解析器仍负责
校验日期、当前目录与能力契约；结构化上下文不是一张可绕过准入的任务卡。
"""
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta

from .query_plan import grouping, ranking
from .routing import ROUTES
from .capabilities import LIMITS

TIME_FIELDS = ('is_window', 'trend_start', 'trend_end', 'comparison_policy', 'comparison_date')
GROUP_LABELS = {'sku': '单款商品', 'variety': '花材品种', 'product_type': '商品类型', 'series': '系列'}


def changes_baseline(question, scope):
    """只修改基线的追问保留目标日；单店与跨店共享同一判定。"""
    return bool(scope.get('date') and re.search(r'上周同|前周同|上星期同|(?:对比|相比|比较|基线.*)(?:前天|昨天)', question))


def snapshot_scope(task, *, confirmed_sku=None):
    """从任务提取可持久化条件白名单；调用者明确传入已确认SKU，错字候选不自动确认。"""
    scope = {
        'sku': confirmed_sku,
        'date': task.get('target_date'),
        'intent': task.get('intent') if task.get('intent') in ROUTES else None,
        'explicit_comparison': any(
            step['operation'].startswith('compare_')
            for step in task.get('query_plan', {}).get('steps', [])
        ),
    }
    if task.get('product_scope'):
        scope['product_scope'] = deepcopy(task['product_scope'])
    if 'is_window' in task:
        scope['time'] = {key: task.get(key) for key in TIME_FIELDS}
    if task.get('query_plan', {}).get('planning'):
        scope['planning'] = deepcopy(task['query_plan']['planning'])
    return scope


def group_selector(product):
    """重放筛选语义，绝不复制参照店成员；旧会话仅对商品筛选短语做兼容解析。"""
    if product.get('selector'):
        return deepcopy(product['selector'])
    return {'kind': 'group', 'legacy_filter': product['filter_text'],
            'filter_text': product['filter_text'], 'label': product['label']}


@dataclass
class QueryContext:
    """仅供服务端准入使用的条件补丁；None表示该项交给本轮原话解析。"""
    product: dict | None = None
    time: dict | None = None
    operations: dict | None = None
    baseline_from_question: bool = False
    complete: bool = False

    def document(self):
        """生成独立JSON快照，避免会话/子任务共享可变字典而污染旧任务。"""
        return deepcopy({'product': self.product, 'time': self.time,
                         'operations': self.operations, 'baseline_from_question': self.baseline_from_question,
                         'complete': self.complete})

    def ranking_for(self, question):
        """None可表示已确认不排名；返回副本，防止模型回执调整limit时改写旧上下文。"""
        return deepcopy(self.operations['ranking'] if self.operations is not None else ranking(question))

    @classmethod
    def from_document(cls, document):
        """只接收内部条件协议；公开HTTP请求不允许传入这个字段。"""
        if set(document) - {'product', 'time', 'operations', 'baseline_from_question', 'complete'}:
            raise ValueError('查询上下文包含非条件字段。')
        return cls(**deepcopy(document))

    @classmethod
    def replay(cls, scope):
        """完整重放已确认口径；商品成员和能力授权仍由目标门店重新准入。"""
        product = scope.get('product_scope')
        time = deepcopy(scope.get('time') or {})
        time['target_date'] = scope['date']
        compare = scope.get('explicit_comparison', bool(time.get('comparison_date'))) if product else bool(time.get('comparison_date'))
        return cls(
            product=group_selector(product) if product else {'kind': 'single', 'sku_id': scope['sku']},
            time=time,
            operations={'ranking': deepcopy(product.get('ranking')) if product else None,
                        'compare': compare},
            complete=True,
        )

    @classmethod
    def followup(cls, question, scope, identity, *, inherit_product, inherit_time, baseline_patch):
        """合并本轮未修改条件；显式总量查询清除排名，类型缩小保留既有花材。"""
        previous = scope.get('product_scope') or {}
        product = group_selector(previous) if inherit_product and previous else None
        narrowing = bool(re.search(r'只看|仅看|只查|仅查', question))
        constraints = identity.get('constraints', {})
        if (narrowing and previous and not identity['issues']
                and constraints.get('product_types') and not constraints.get('varieties')):
            product = group_selector(previous)
            # 新协议直接修改类型，不再对旧filter_text进行正则替换和拼接。
            product['type_override'] = list(constraints['product_types'])
        previous_rank = previous.get('ranking')
        operations = None
        if previous_rank and (inherit_product or narrowing):
            current = ranking(question)
            reset = bool(re.search(r'总|合计|一共|多少|趋势|走势|库存|缺货|活动|取消', question))
            rank = deepcopy(current or (None if reset else previous_rank))
            if rank and grouping(question) == 'sku' and not re.search(r'单款|单品|按SKU|每款', question, re.I):
                rank['group_by'] = previous_rank.get('group_by', 'sku')
                rank['assumption'] = '按' + GROUP_LABELS[rank['group_by']] + '汇总；最好/最差按成品支付件数排序，保留并列。'
            explicit_compare = bool(re.search(r'相比|对比|比较|比上周', question))
            operations = {'ranking': rank, 'compare': explicit_compare or bool(rank and previous.get('compare_selected'))}
        time = None
        if inherit_time:
            time = {**deepcopy(scope.get('time') or {}), 'target_date': scope['date']}
            if (not previous and not baseline_patch and time.get('comparison_date')
                    and time['comparison_date'] != (date.fromisoformat(scope['date']) - timedelta(days=1)).isoformat()):
                operations = {'ranking': None, 'compare': True}
        elif (operations and operations['compare'] and not baseline_patch
              and scope.get('time', {}).get('comparison_policy') == 'same_weekday_previous_week'):
            # 改目标日时继承的是“上周同日”关系，不是上一轮的绝对基线日期。
            time = {'comparison_policy': 'same_weekday_previous_week'}
        return cls(product, time, operations, baseline_patch)

    def resolve_product(self, catalog):
        """用本店目录重新求筛选集合；空集合返回缺口，不能扩大为全店或复用外店SKU。"""
        selection = self.product
        if selection['kind'] == 'single':
            product = catalog.products.get(selection['sku_id'])
            return {'candidates': [product] if product else [], 'matched_by': 'context_single',
                    'constraints': {}, 'issues': [] if product else ['该商品不在当前门店目录中。']}
        if selection['kind'] != 'group':
            raise ValueError('未知查询商品范围。')
        constraints = deepcopy(selection.get('constraints') or {})
        if selection.get('legacy_filter'):
            legacy = catalog.resolve(selection['legacy_filter'])
            candidates = list(catalog.products.values()) if selection['legacy_filter'] == '全店' else legacy['candidates']
            constraints = legacy['constraints']
        elif selection.get('alias'):
            candidates = catalog.resolve(selection['alias'])['candidates']
        else:
            candidates = list(catalog.products.values())
        if selection.get('type_override'):
            constraints['product_types'] = selection['type_override']
            # 旧筛选中已含类型时先恢复原品种/系列，再应用新类型。
            if selection.get('legacy_filter'):
                old = re.sub(r'花束|抱抱桶|花篮|花盒|花瓶', '', selection['legacy_filter'])
                candidates = catalog.resolve(old)['candidates'] if old and old != '全店' else list(catalog.products.values())
        for field, key in [('stem_count', 'stem_counts'), ('product_type', 'product_types'), ('variety', 'varieties')]:
            if constraints.get(key):
                candidates = [p for p in candidates if p.get(field) in constraints[key]]
        if constraints.get('business_tag'):
            candidates = [p for p in candidates if constraints['business_tag'] in p.get('business_tags', [])]
        if constraints.get('listed_at'):
            candidates = [p for p in candidates if p.get('listed_at') == constraints['listed_at']]
        filter_text = selection['filter_text']
        if selection.get('type_override'):
            filter_text = re.sub(r'花束|抱抱桶|花篮|花盒|花瓶', '', filter_text) + ''.join(selection['type_override'])
        # 旧会话兼容一次实体筛选后即升级为新协议，后续不再重解析旧查询句子。
        alias = selection.get('alias')
        if selection.get('legacy_filter') and not constraints.get('varieties'):
            alias = re.sub(r'花束|抱抱桶|花篮|花盒|花瓶', '', selection['legacy_filter'])
            if alias == '全店':alias = None
        selector = {'kind': 'group', 'constraints': constraints, 'alias': alias,
                    'all_products': not alias and not any(constraints.values()),
                    'filter_text': filter_text, 'label': '全店商品' if filter_text == '全店' else '全部' + filter_text}
        return {'candidates': candidates, 'matched_by': 'context_group', 'constraints': constraints,
                'normalized_question': filter_text, 'selector': selector,
                'issues': [] if candidates else ['当前门店目录没有符合本轮筛选条件的商品。']}

    def resolve_time(self, parsed, today):
        """合并冻结日期与本轮基线，并独立核验范围；不把旧窗口缩成最后一天。"""
        if not self.time:
            return parsed
        saved = deepcopy(self.time)
        if saved.get('target_date'):
            target = saved['target_date']
            window = saved.get('is_window', False)
            start = saved.get('trend_start') if window else (date.fromisoformat(target) - timedelta(days=13)).isoformat()
            # 只消除省略目标日的提示，不掩盖本轮原话的日期冲突。
            errors = [e for e in parsed['errors'] if not e.startswith('请补充目标日期')]
            if parsed.get('target_date') and parsed['target_date'] != target:
                errors.append('查询上下文与本轮明确目标日期不一致。')
            if window and parsed.get('is_window') and parsed.get('trend_start') != start:
                errors.append('查询上下文与本轮明确日期窗口不一致。')
            result = {**parsed, **saved, 'is_window': window, 'trend_start': start, 'trend_end': target, 'errors': errors}
            if self.baseline_from_question:
                for key in ('comparison_date', 'comparison_policy'):
                    result[key] = parsed[key]
        else:
            result = {**parsed, **saved}
        target = result.get('target_date')
        policy = result.get('comparison_policy')
        if target and not self.baseline_from_question:
            offset = {'same_weekday_previous_week': 7, 'two_days_before': 2}.get(policy, 1)
            if not (saved.get('target_date') and saved.get('comparison_date')) and (policy != 'explicit_baseline' or not result.get('comparison_date')):
                result['comparison_date'] = (date.fromisoformat(target) - timedelta(days=offset)).isoformat()
        if target:
            if date.fromisoformat(target) >= today:
                result['errors'].append('仅查询已结束的自然日。')
            start = result.get('trend_start')
            if not start or not 0 <= (date.fromisoformat(target) - date.fromisoformat(start)).days < LIMITS['max_days']:
                result['errors'].append('查询范围需按起止日期顺序填写，且最多60天。')
            baseline = result.get('comparison_date')
            if baseline and baseline >= target:
                result['errors'].append('对比基线应早于目标日期。')
            if start and baseline and (date.fromisoformat(target) - min(date.fromisoformat(start) - timedelta(days=28), date.fromisoformat(baseline))).days >= 90:
                result['errors'].append('指定基线与窗口相距过远，当前含基线的读取范围最多90天。')
        return result

    def steps(self, rank, comparison_date):
        """从结构化操作生成同一能力DAG，随后仍由bind_contract/validate_plan复核。"""
        steps = [{'id': 'summary', 'operation': 'sales_summary', 'depends_on': []}]
        if rank:
            steps.append({'id': 'ranking', 'operation': 'rank_products', 'depends_on': ['summary'],
                          'metric': 'units_sold', 'direction': rank['direction']})
        if self.operations['compare']:
            steps.append({'id': 'comparison', 'operation': 'compare_selected' if rank else 'compare_scope',
                          'depends_on': ['ranking' if rank else 'summary'], 'baseline_date': comparison_date,
                          'selection': 'all_tied_leaders' if rank else 'current_scope'})
        return steps
