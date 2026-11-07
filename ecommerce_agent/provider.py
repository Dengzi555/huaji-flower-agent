"""模型适配层：将统一消息/工具契约发送到兼容 Chat Completions 的服务。

不读取业务文件、不计算指标、不直接执行工具；只返回模型的下一条消息。
更换服务时主要调整此层，避免模型供应商细节侵入电商业务逻辑。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import queue
import threading
import time
import random
import ssl
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .errors import AgentError,from_exception
from .cancellation import current_token,cancellable_call
from .model_runtime import UsageLedger,current_ledger,prepare_messages,normalize_usage
from .model_deadlines import TIMEOUTS,deadline_scope


def _transport_problem(error,phase):
    """仅传输层瞬时故障可自动重试；公开retryable仍表示用户可以手动重试。"""
    problem=from_exception(error,phase=phase)
    if isinstance(error,urllib.error.HTTPError):
        problem.transport_retryable=error.code in (429,500,502,503,504)
        # Retry-After既允许秒数也允许HTTP日期；过长时不截短后提前请求。
        raw=error.headers.get('Retry-After','') if error.headers else ''
        if raw:
            try:
                delay=float(raw) if raw.isdecimal() else (parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds()
                if not math.isfinite(delay):raise ValueError('非法等待时间')
                problem.retry_after=max(0,delay)
            except (ValueError,TypeError,OverflowError):
                problem.transport_retryable=False
    else:
        reason=getattr(error,'reason',error)
        problem.transport_retryable=(isinstance(error,urllib.error.URLError)
            and problem.code=='model_connection' and not isinstance(reason,ssl.SSLError))
    return problem


def _wait_retry(delay,token):
    """退避等待可取消，不创建后台重试，也不占据会话以外的共享状态。"""
    if token:
        token.event.wait(delay);token.checkpoint()
    else:time.sleep(delay)


def _abortable(response,token):
    """关闭可能阻塞，因此异步关闭；执行线程依靠令牌及时返回，不等待迟到响应。"""
    def close():
        try:response.close()
        except Exception:pass
    if token:token.on_cancel(lambda:threading.Thread(target=close,daemon=True,name='flower-http-close').start())


def _read_response(request,timeout):
    with deadline_scope(timeout):return _read_response_bounded(request,timeout)


def _read_response_bounded(request,timeout):
    token=current_token()
    def read():
        if token:token.request_started()
        with urllib.request.urlopen(request,timeout=timeout) as response:
            _abortable(response,token)
            if token:token.checkpoint()
            return response.read(2_000_001)
    return cancellable_call(read)


def _response_lines(request,timeout):
    with deadline_scope(timeout,'chat'):yield from _response_lines_bounded(request,timeout)


def _response_lines_bounded(request,timeout):
    """网络线程只收字节；SSE解析和回调仍在请求线程，停止后不再输出迟到增量。"""
    token=current_token()
    if not token:
        with urllib.request.urlopen(request,timeout=timeout) as response:yield from response
        return
    messages=queue.Queue(maxsize=64)
    def send(kind,value):
        while not token.event.is_set():
            try:messages.put((kind,value),timeout=.05);return
            except queue.Full:continue
    def read():
        try:
            token.request_started()
            with urllib.request.urlopen(request,timeout=timeout) as response:
                _abortable(response,token)
                for line in response:token.checkpoint();send('line',line)
            send('end',None)
        except BaseException as exc:send('error',exc)
    threading.Thread(target=read,daemon=True,name='flower-stream-io').start()
    while True:
        token.checkpoint()
        try:kind,value=messages.get(timeout=.05)
        except queue.Empty:continue
        token.checkpoint()
        if kind=='end':return
        if kind=='error':raise value
        yield value


class ChatProvider:
    """最小 HTTP 客户端；密钥从环境变量读取，不写入配置和业务对话。"""
    supports_cancellation=True
    supports_resource_management=True
    def __init__(self, config, *, api_key=None):
        """验证地址和模型配置；本地模型可使用 localhost HTTP，远端使用 HTTPS。"""
        base = config["api_base_url"]
        parsed = urllib.parse.urlparse(base)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if not parsed.hostname or not (parsed.scheme == "https" or parsed.scheme == "http" and local):
            raise ValueError("Use HTTPS, or HTTP on localhost for local models")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not include secrets or query parameters in API URL")
        self.url = base.rstrip("/") + "/chat/completions"
        self.key = api_key if api_key is not None else os.environ.get(config.get("api_key_env", "ECOMMERCE_AGENT_API_KEY"), "")
        self.thinking_disabled = config.get("thinking_disabled", False)
        self.max_tokens = config.get('max_output_tokens',2048)
        self.model = config["model"]
        self.last_usage = None
        self.ledger=UsageLedger()
        self.purpose='execution'
        self.last_context=None
        if type(self.max_tokens) is not int or not 1<=self.max_tokens<=8192:
            raise ValueError('输出token上限必须为1–8192。')
        if not self.key and not local:
            raise AgentError('model_not_configured',phase='configuration')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("A tool-calling model ID is required")

    def _managed(self,messages,tools,operation,timeout,can_retry=lambda:True):
        """每次尝试独立预留预算；最多3次，共享阶段时限，单次退避最多8秒。

        失败用量保留未知。这里只重发模型HTTP，不执行或重放任何业务工具。
        """
        self.last_usage=None
        ledger=current_ledger() or self.ledger
        self.last_context=None
        selected,context=prepare_messages(messages,tools,self.max_tokens,ledger.policy)
        self.last_context=context
        token=current_token()
        if token:token.checkpoint()
        # 所有尝试和退避共享本阶段时限，不允许每次重试重新获得完整等待时间。
        timeout=min(timeout,TIMEOUTS.get(self.purpose,25))
        deadline=time.monotonic()+timeout
        for attempt in range(1,4):
            if token:token.checkpoint()
            record=ledger.reserve(self.model,self.purpose,context)
            record['attempt']=attempt
            self.last_usage=None;status='failed';retry_delay=None
            try:
                limit=max(.001,deadline-time.monotonic())
                result=operation(selected,limit)
                status='completed';return result
            except Exception as exc:
                record['error_code']=getattr(exc,'code','internal_error')
                if getattr(exc,'code',None)=='request_cancelled':status='cancelled'
                if attempt<3 and getattr(exc,'transport_retryable',False) and can_retry():
                    backoff=.5*2**(attempt-1)+random.uniform(0,.25)
                    delay=max(backoff,getattr(exc,'retry_after',None) or 0)
                    if delay<=8 and time.monotonic()+delay<deadline:
                        retry_delay=delay
                        record['retry_wait_ms']=round(delay*1000)
                if retry_delay is None:raise
            finally:ledger.finish(record,self.last_usage,status)
            _wait_retry(retry_delay,token)
            # 睡眠被调度延迟时也不越过重试窗口发新请求。
            if time.monotonic()>=deadline:raise AgentError('model_timeout',phase=self.purpose)

    def stream_text(self,messages,on_delta,timeout=20):
        """流式入口同样受预算约束，已收到的供应商用量即使取消也记录。"""
        emitted=False
        def emit(text):
            nonlocal emitted
            emitted=True;on_delta(text)
        return self._managed(messages,[],lambda selected,limit:self._stream_text(selected,emit,limit),timeout,lambda:not emitted)

    def _stream_text(self,messages,on_delta,timeout=20):
        """读取供应商真实SSE增量；已有可见输出的断流不自动重发。"""
        self.last_usage=None
        body={'model':self.model,'messages':messages,'stream':True,'stream_options':{'include_usage':True},'max_tokens':self.max_tokens}
        if self.thinking_disabled:body['thinking']={'type':'disabled'}
        request=urllib.request.Request(self.url,json.dumps(body).encode('utf-8'),{'Content-Type':'application/json','Authorization':'Bearer '+self.key})
        parts=[];total=0;finished=False
        try:
            for line in _response_lines(request,timeout):
                    total+=len(line)
                    if total>2_000_000:raise ValueError('流式响应过大')
                    if not line.startswith(b'data:'):continue
                    raw=line[5:].strip()
                    if raw==b'[DONE]':finished=True;break
                    value=json.loads(raw)
                    if value.get('usage'):self.last_usage=normalize_usage(value['usage'])
                    for choice in value.get('choices',[]):
                        delta=choice.get('delta',{})
                        if delta.get('tool_calls'):raise ValueError('普通对话不能执行工具')
                        text=delta.get('content')
                        if isinstance(text,str) and text:parts.append(text);on_delta(text)
            if not finished or not parts:raise ValueError('流式响应未完成')
            return {'content':''.join(parts)}
        except (urllib.error.URLError,TimeoutError) as exc:
            raise _transport_problem(exc,'chat') from None
        except (KeyError,TypeError,ValueError) as exc:
            raise from_exception(exc,phase='chat',fallback='model_response_invalid') from None

    def complete(self,messages,tools,timeout):
        """非流式入口；资源拒绝发生在HTTP之前。"""
        return self._managed(messages,tools,lambda selected,limit:self._complete(selected,tools,limit),timeout)

    def _complete(self, messages, tools, timeout):
        """发送一次模型请求，返回协议字段；工具请求仍需由 runner 执行。

        瞬时传输故障交给_managed有限重试；超时/协议错误不重试，不输出原始响应体。
        """
        self.last_usage = None
        body = {"model": self.model, "messages": messages,"max_tokens":self.max_tokens}
        if tools:body.update(tools=tools,tool_choice='auto',parallel_tool_calls=False)
        if self.thinking_disabled:
            body.update(thinking={"type":"disabled"},max_tokens=self.max_tokens)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.url, json.dumps(body).encode("utf-8"), headers)
        try:
            raw = _read_response(request,timeout)
            if len(raw) > 2_000_000:
                raise ValueError("Model response too large")
            payload = json.loads(raw)
            message = payload["choices"][0]["message"]
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                # 未上报的用量保留未知，不能把缺少计费信息显示成“免费”。
                self.last_usage = {key: usage[key] if type(usage.get(key)) is int and usage[key] >= 0 else None
                                   for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            # 只保留下一轮需要的字段，不将服务附加元数据直接混入执行协议。
            return {"role": "assistant", "content": message.get("content"),
                    **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})}
        except urllib.error.HTTPError as error:
            raise _transport_problem(error,'model') from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise _transport_problem(error,'model') from None
        except (KeyError, TypeError, IndexError,ValueError) as error:
            raise from_exception(error,phase='model',fallback='model_response_invalid') from None
