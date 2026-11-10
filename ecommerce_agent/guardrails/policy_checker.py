"""整改建议的硬性业务规则。校验结构化参数，拒绝未识别或缺参动作。"""

MIN_DISCOUNT_PERCENT = 85
MAX_AD_BUDGET_CENTS = 500_000


def policy_limits(config=None):
    """读取门店审批规则，旧配置缺字段时使用统一默认值。"""
    config = config or {}
    return {
        'min_discount_percent': config.get('min_discount_percent', MIN_DISCOUNT_PERCENT),
        'max_ad_budget_cents': config.get('max_ad_budget_cents', MAX_AD_BUDGET_CENTS),
    }


def check_action(action, config=None):
    """输入一项结构化建议，返回状态、展示标签和核验原因。"""
    limits = policy_limits(config)
    minimum = limits['min_discount_percent']
    maximum = limits['max_ad_budget_cents']
    kind = action.get('kind') if isinstance(action, dict) else None
    if kind == 'discount' and set(action) == {'kind', 'discount_percent'}:
        discount = action['discount_percent']
        if type(discount) is int and 0 <= discount <= 100:
            if discount < minimum:
                return {'status': 'blocked', 'label': '【已拦截：超出底线折扣】',
                        'reason': f'建议按原价的 {discount}% 销售，低于全店 {minimum}% 的折扣底线。'}
            return {'status': 'approved', 'label': '【审批通过：待执行】',
                    'reason': f'建议按原价的 {discount}% 销售，符合全店折扣底线。'}
    elif kind == 'advertising' and set(action) == {'kind', 'budget_cents'}:
        budget = action['budget_cents']
        if type(budget) is int and budget > 0:
            limit = f"{maximum / 100:.2f}"
            if budget > maximum:
                return {'status': 'blocked', 'label': '【已拦截：超出投放预算】',
                        'reason': f'建议预算 {budget / 100:.2f} 元，超过单次 {limit} 元上限。'}
            return {'status': 'approved', 'label': '【审批通过：待执行】',
                    'reason': f'建议预算 {budget / 100:.2f} 元，符合单次 {limit} 元上限。'}
    elif kind == 'manual_review' and set(action) == {'kind'}:
        return {'status': 'approved', 'label': '【审批通过：待执行】',
                'reason': '仅安排人工核查，未涉及改价或广告支出。'}
    return {'status': 'blocked', 'label': '【已拦截：建议参数无效】',
            'reason': '无法核验动作类型或金额、折扣参数。'}
