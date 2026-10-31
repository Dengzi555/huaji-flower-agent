"""协作式取消：请求/任务各自持有令牌，跨线程传递，停止后禁止新的模型与工具调用。

已在途的HTTP可能已计费；取消不等于撤销供应商请求。阻塞网络放在有超时的守护线程，
主执行线程及时退出；迟到的响应丢弃，绝不再用它触发工具或覆盖取消终态。
"""
import contextvars
import queue
import threading
import time
from contextlib import contextmanager
from .errors import AgentError

_current=contextvars.ContextVar('flower_cancel_token',default=None)


class CancelledError(AgentError):
    """内部控制流异常，与模型超时、协议错误分别处理。"""
    def __init__(self):super().__init__('request_cancelled',phase='cancellation')


class CancelToken:
    """Event先置位再通知回调，故DB锁或网络关闭较慢也不会允许下一次调用。"""
    def __init__(self,parent=None):
        self.event=threading.Event();self.lock=threading.RLock();self.callbacks=[];self.requests_started=0;self.parent=parent;self.children=[]
        if parent:
            with parent.lock:parent.children.append(self)
            parent.on_cancel(self.cancel)

    def checkpoint(self):
        if self.event.is_set():raise CancelledError()

    def on_cancel(self,callback):
        with self.lock:
            if not self.event.is_set():self.callbacks.append(callback);return
        callback()

    def cancel(self):
        with self.lock:
            if self.event.is_set():return False
            self.event.set();callbacks=self.callbacks;self.callbacks=[]
        for callback in callbacks:
            try:callback()
            except Exception:pass  # 控制信号已生效；关闭回调失败不准恢复执行。
        return True

    def request_started(self):
        with self.lock:self.checkpoint();self.requests_started+=1

    def details(self):
        def total(token):
            with token.lock:own=token.requests_started;children=list(token.children)
            return own+sum(total(child) for child in children)
        sent=total(self)+(self.parent.requests_started if self.parent else 0)
        return {'model_requests_started':sent,'cost_confirmed':False,
            'note':'已发起的模型请求可能已计费；中断后未收到的用量保留未知。' if sent else '本阶段没有发起模型请求。'}


def current_token():return _current.get()


def checkpoint():
    """工具/循环中的轻量检查；未提供令牌时兼容原CLI及离线评测。"""
    token=current_token()
    if token:token.checkpoint()


@contextmanager
def cancel_scope(token):
    """线程/请求隔离，不用全局“停止全部任务”开关。"""
    reset=_current.set(token)
    try:yield
    finally:_current.reset(reset)


def cancellable_call(operation):
    """取消时不等待阻塞网络返回；网络操作本身必须设置有限超时。"""
    token=current_token()
    if token is None:return operation()
    token.checkpoint();result=queue.Queue(maxsize=1);context=contextvars.copy_context()
    def work():
        try:token.checkpoint();value=operation();result.put((True,value))
        except BaseException as exc:result.put((False,exc))
    threading.Thread(target=lambda:context.run(work),daemon=True,name='flower-cancellable-io').start()
    while True:
        token.checkpoint()
        try:ok,value=result.get(timeout=.05)
        except queue.Empty:continue
        token.checkpoint()
        if ok:return value
        raise value


def cancel_state(state,token=None):
    """统一终态：部分证据保留，迟到的finish不能变成完成。"""
    token=token or current_token()
    state.update(status='cancelled',stop_reason='user_cancelled',error=None,
        error_info=CancelledError().public(),model_draft=None,
        cancellation=token.details() if token else {'note':'用户停止执行，已有证据保留。','cost_confirmed':False})
    return state


class RequestCancellations:
    """取消端点不占用会话执行锁；预取消记录处理“停止先于POST到达”的竞态。"""
    def __init__(self):self.lock=threading.RLock();self.entries={}

    def token(self,cid,key):
        with self.lock:
            now=time.monotonic()
            self.entries={k:v for k,v in self.entries.items() if now-v[0]<900}
            identity=(cid,key)
            if identity not in self.entries:
                if len(self.entries)>=256:raise ValueError('并行请求记录较多，请稍后重试。')
                self.entries[identity]=(now,CancelToken())
            return self.entries[identity][1]

    def cancel(self,cid,key):
        token=self.token(cid,key);token.cancel();return token
