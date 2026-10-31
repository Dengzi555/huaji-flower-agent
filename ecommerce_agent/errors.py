"""花迹统一问题契约：区分用户信息不足、业务缺口与服务故障，不回显供应商原始错误。

恢复动作只是建议，不能自动重试付费调用或放宽工具权限。phase说明失败阶段，
是否已查数由任务状态/证据决定，不能把执行中失败一概说成“尚未执行”。
"""
import socket
import urllib.error

DEFINITIONS={
    'model_budget_exceeded':('incomplete','resource','模型调用预算已用尽','本次请求达到模型调用或估算容量预算，已停止继续调用；已有证据保留，请缩小查询范围后重新提问。',False,'edit_question','缩小查询范围'),
    'model_context_limit':('incomplete','resource','上下文容量超限','当前问题、工具定义和必要证据超过上下文容量上限；为保留证据完整性，本次不继续调用模型。请缩小查询范围或新建对话。',False,'edit_question','缩小范围或新建对话'),
    'request_cancelled':('cancelled','cancellation','已停止','本次请求已停止，已经取得的证据会保留。',False,'edit_question','重新提问'),
    'cancel_pending':('cancelling','cancellation','正在停止','停止信号已提交，正在结束当前执行并保存已有证据。',False,'view_sources','查看已有证据'),
    'needs_information':('needs_clarification','information','需要补充信息','请补充必要的查询条件。',False,'clarify','补充信息'),
    'data_missing':('needs_data','data','缺少业务数据','所选范围缺少可用业务数据，请查看数据来源与覆盖情况。',False,'view_sources','查看来源数据'),
    'capability_unsupported':('unsupported','capability','当前能力不支持','当前查询能力尚未接入，不能用其他数据代答。',False,'edit_question','修改问题'),
    'permission_denied':('unsupported','permission','不支持此操作','当前助手只支持经营查询，不能执行改价、退款等操作。',False,'edit_question','修改问题'),
    'model_not_configured':('failed','configuration','模型尚未配置','请在账号与API设置中启用模型并配置密钥。',False,'open_settings','检查模型设置'),
    'model_authentication':('failed','configuration','模型鉴权失败','模型服务拒绝了当前凭据，请检查API密钥和账号权限。',False,'open_settings','检查模型设置'),
    'model_quota_exceeded':('failed','configuration','模型额度不可用','模型服务额度或余额不足，请检查账号额度。',False,'open_settings','检查模型设置'),
    'model_timeout':('failed','service','模型请求超时','模型服务未在规定时间内返回。可以稍后重试，无需重复补充已提供的条件。',True,'retry','重试本次请求'),
    'model_connection':('failed','service','模型连接失败','暂时无法连接模型服务，请检查网络后重试。',True,'retry','重试本次请求'),
    'model_rate_limited':('failed','service','模型请求受限','模型服务暂时限制请求频率，请稍后重试。',True,'retry','稍后重试'),
    'model_unavailable':('failed','service','模型服务暂不可用','模型服务暂时无法处理请求，可以稍后重试。',True,'retry','稍后重试'),
    'model_response_invalid':('failed','protocol','模型响应无效','模型返回的内容不符合接口约定，本次不能继续处理。请稍后重试。',True,'retry','重新请求'),
    'plan_validation_failed':('failed','validation','查询计划未通过校验','模型生成的查询条件未通过校验，尚未开始查数。可以重新解析本次问题。',True,'retry','重新解析'),
    'execution_failed':('failed','execution','查询执行失败','本次查询未能完成，已取得的证据会保留。请查看执行过程后重试。',True,'retry','重新查询'),
    'execution_incomplete':('incomplete','execution','查询尚未完成','本次查询达到执行限制，已有证据会保留。请查看执行过程后重新查询。',True,'retry','重新查询'),
    'execution_interrupted':('interrupted','execution','查询曾中断','执行过程曾中断，不能确认已完成。请核对已有证据后重新查询。',True,'retry','重新查询'),
    'internal_error':('failed','internal','服务处理失败','本次请求未能完成，请稍后重试。',True,'retry','重试')
}


class AgentError(ValueError):
    """异常只携带白名单code和阶段；继承ValueError兼容已有CLI/API错误边界。"""
    def __init__(self,code,*,phase='request',retry_after=None):
        if code not in DEFINITIONS:code='internal_error'
        self.code=code;self.phase=phase;self.retry_after=retry_after
        super().__init__(DEFINITIONS[code][3])

    def public(self):
        """输出浏览器可见字段，禁止异常堆栈、密钥、URL请求头和原始响应体。"""
        status,category,title,message,retryable,action,label=DEFINITIONS[self.code]
        result={'code':self.code,'status':status,'category':category,'phase':self.phase,'title':title,
            'message':message,'retryable':retryable,'recovery_actions':[{'type':action,'label':label}]}
        if self.retry_after is not None:result['retry_after_seconds']=self.retry_after
        return result


def from_exception(exc,*,phase='request',fallback='internal_error'):
    """按异常类型和HTTP状态分类，不通过匹配任意错误文本猜测，更不回显文本。"""
    if isinstance(exc,AgentError):
        if exc.code=='request_cancelled':return exc
        return AgentError(exc.code,phase=phase,retry_after=exc.retry_after)
    if isinstance(exc,urllib.error.HTTPError):
        code={401:'model_authentication',403:'model_authentication',402:'model_quota_exceeded',
            408:'model_timeout',429:'model_rate_limited',500:'model_unavailable',502:'model_unavailable',
            503:'model_unavailable',504:'model_timeout'}.get(exc.code,'model_response_invalid')
        raw=exc.headers.get('Retry-After','') if exc.headers else ''
        retry_after=min(300,int(raw)) if raw.isdecimal() else None
        return AgentError(code,phase=phase,retry_after=retry_after)
    if isinstance(exc,(TimeoutError,socket.timeout)):return AgentError('model_timeout',phase=phase)
    if isinstance(exc,urllib.error.URLError):
        return AgentError('model_timeout' if isinstance(exc.reason,(TimeoutError,socket.timeout)) else 'model_connection',phase=phase)
    return AgentError(fallback,phase=phase)


def attach_task_issue(task):
    """所有解析退出路径共享结构；已有服务错误不再被包装成用户需要补信息。"""
    if task.get('error_info'):return task
    inherited=task.get('routing',{}).get('error_info')
    if inherited:
        task.update(status=inherited['status'],error_info=inherited,issues=[inherited['message']]);return task
    status=task.get('status')
    code={'needs_clarification':'needs_information','needs_data':'data_missing','unsupported':'capability_unsupported'}.get(status)
    if status=='unsupported' and task.get('routing',{}).get('intent')=='unsupported_action':code='permission_denied'
    if code:
        info=AgentError(code,phase='admission').public()
        if task.get('issues'):info['message']='\n'.join(task['issues'])
        task['error_info']=info
    return task
