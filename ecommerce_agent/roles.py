"""角色配置：服务端固定任务提示与工具授权，不接收客户端自定义工具列表。"""

from dataclasses import dataclass

from .errors import AgentError


@dataclass(frozen=True)
class Role:
    """一个角色的显示名称、可调用工具与默认执行提示。"""

    name: str
    allowed_tools: tuple[str, ...]
    default_task: str


# 现有销量调查的完成契约依赖四类证据；所有角色共用这一最小完整链路。
_INVESTIGATION = (
    'get_sales_metrics', 'get_traffic_metrics', 'get_inventory_status',
    'get_promotion_history', 'get_sales_trend', 'get_calendar_context',
    'analyze_metrics', 'finish',
)
DEFAULT_ROLE = 'product_investigator'
ROLES = {
    'store_operator': Role(
        '门店运营',
        _INVESTIGATION + ('get_channel_breakdown', 'get_product_group_sales',
                          'get_inventory_range', 'get_promotion_range'),
        '检查门店销量、流量、库存和活动变化，按证据指出优先关注项。',
    ),
    'product_investigator': Role(
        '商品调查',
        _INVESTIGATION + ('get_channel_breakdown', 'get_order_funnel',
                          'get_product_group_sales', 'get_inventory_range', 'get_promotion_range', 'check_activity_status'),
        '调查指定商品销量下降时，除库存和流量外，必须调用 check_activity_status 检查前一日与调查日的满减和资源位状态。'
        '发现活动到期且未续接时，在结论中写明“活动断档，需尽快联系运营续报”，引用活动证据。'
        '将活动断档列为销量下降的优先排查线索；证据缺失时写待核实，不把相关性认定为因果。',
    ),
    'supply_buyer': Role(
        '采销协同',
        _INVESTIGATION + ('get_product_group_sales', 'get_inventory_range'),
        '根据销量与库存起草待审核的补货建议；到货时间缺少依据时标为待确认。',
    ),
    'supervisor': Role(
        '经营主管',
        _INVESTIGATION + ('get_order_funnel', 'get_product_group_sales',
                          'get_inventory_range', 'get_promotion_range'),
        '汇总异常与影响，按证据给出核查优先级；缺少原因记录时不归因退款。',
    ),
}


def get_role(role_id):
    """输入角色 ID，返回服务端配置；未知或非法 ID 在准入时直接拒绝。"""
    if not isinstance(role_id, str) or role_id not in ROLES:
        raise ValueError('角色无效，请选择已配置的调查角色。')
    return ROLES[role_id]


def require_role_tool(role, name):
    """拒绝名单外工具；异常由执行边界转为可见的权限错误。"""
    if name not in role.allowed_tools:
        raise AgentError('role_tool_denied', phase='tool')
