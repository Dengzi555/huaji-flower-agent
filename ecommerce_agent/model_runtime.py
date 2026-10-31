"""模型资源管理：容量估算、完整轮次裁剪、调用预算和供应商用量账本。

容量按UTF-8字节估算，包含工具定义和输出预留，不等于供应商分词或账单。
普通聊天可删除最早完整轮次；调查中的工具调用/回执与证据不拆开、不伪造摘要。
"""
import contextvars
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from .errors import AgentError

FIELDS=('prompt_tokens','completion_tokens','total_tokens')
_ledger=contextvars.ContextVar('flower_model_ledger',default=None)


@dataclass(frozen=True)
class ModelPolicy:
    """宿主资源上限；调用者可收紧，不能由用户问题或记忆修改。"""
    context_units:int=48000
    max_calls:int=16
    max_estimated_tokens:int=240000

    def __post_init__(self):
        if any(type(v) is not int or v<=0 for v in (self.context_units,self.max_calls,self.max_estimated_tokens)):
            raise ValueError('模型资源上限必须为正整数。')


def estimate(value):
    """保守容量单位：序列化后的UTF-8字节数；只用于准入，绝不冒充实际tokens。"""
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))


def normalize_usage(raw):
    """缺失/负数/布尔值均为未知；不从文本长度或缺失字段反推账单。"""
    raw=raw if isinstance(raw,dict) else {}
    return {k:raw[k] if type(raw.get(k)) is int and raw[k]>=0 else None for k in FIELDS}


def prepare_messages(messages,tools,output_limit,policy):
    """返回发送副本及裁剪凭据；保留系统、当前问题和工具协议，不修改原始历史。"""
    selected=list(messages);removed=0
    def size():return estimate({'messages':selected,'tools':tools})+output_limit
    before=size()
    # 有工具协议时不删除历史证据；否则按user起点删除最早完整对话轮次。
    tool_history=any(m.get('tool_calls') or m.get('role')=='tool' for m in selected)
    while size()>policy.context_units and not tool_history:
        starts=[i for i,m in enumerate(selected) if m.get('role')=='user']
        if len(starts)<2:break
        start,end=starts[0],starts[1]
        drop=[i for i in range(start,end) if selected[i].get('role')!='system']
        removed+=len(drop);selected=[m for i,m in enumerate(selected) if i not in drop]
    receipt={'estimate_method':'utf8_bytes_upper_estimate_v1','before_units':before,'input_units':size()-output_limit,
        'reserved_output_tokens':output_limit,'after_units':size(),'removed_messages':removed,
        'context_limit_units':policy.context_units}
    if size()>policy.context_units:raise AgentError('model_context_limit',phase='context')
    return selected,receipt


class UsageLedger:
    """每个请求/任务独立账本；请求发出前预留，失败和取消不自动退回估算预算。"""
    def __init__(self,policy=None):self.policy=policy or ModelPolicy();self.records=[];self.lock=threading.RLock()

    def reserve(self,model,purpose,context):
        with self.lock:
            if len(self.records)>=self.policy.max_calls or sum(r['reserved_units'] for r in self.records)+context['after_units']>self.policy.max_estimated_tokens:
                raise AgentError('model_budget_exceeded',phase='budget')
            record={'sequence':len(self.records)+1,'model':model,'purpose':purpose,'status':'pending',
                'reserved_units':context['after_units'],'context':context,'usage':normalize_usage(None),'started':time.monotonic()}
            self.records.append(record);return record

    def finish(self,record,usage,status):
        with self.lock:
            record.update(usage=normalize_usage(usage),status=status,duration_ms=round((time.monotonic()-record.pop('started'))*1000))

    def snapshot(self):
        """上报总量只有每次都已知才求和；另给已知部分，避免把取消/失败请求算成0。"""
        with self.lock:
            records=[{k:v for k,v in r.items() if k!='started'} for r in self.records]
            totals={k:sum(r['usage'][k] for r in records) if all(r['usage'][k] is not None for r in records) else None for k in FIELDS}
            return {'calls':len(records),'reported':totals,'known_totals':{k:sum(r['usage'][k] or 0 for r in records) for k in FIELDS},
                'unknown_usage_calls':sum(any(r['usage'][k] is None for k in FIELDS) for r in records),
                'reserved_units':sum(r['reserved_units'] for r in records),'records':records,'cost':None,
                'budget':{'max_calls':self.policy.max_calls,'max_estimated_tokens':self.policy.max_estimated_tokens,
                    'context_units':self.policy.context_units},'estimate_is_billing':False}


def current_ledger():return _ledger.get()


@contextmanager
def model_scope(policy=None):
    """嵌套规划共享同一请求预算；后台调查另起作用域，不把不同用户/门店混账。"""
    existing=current_ledger()
    if existing is not None:
        yield existing;return
    ledger=UsageLedger(policy);reset=_ledger.set(ledger)
    try:yield ledger
    finally:_ledger.reset(reset)
