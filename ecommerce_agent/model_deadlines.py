"""模型等待策略与实际截止时间；不把socket的空闲超时冒充整次调用时限。"""
from contextlib import contextmanager
import threading
from .cancellation import CancelToken,CancelledError,current_token,cancel_scope
from .errors import AgentError

TIMEOUTS={'classification':8,'store_resolution':8,'planning':10,'chat':20,'execution':25,'connection_test':10}
INVESTIGATION_SECONDS=90


@contextmanager
def deadline_scope(seconds,phase='model'):
    """用子令牌限制HTTP整体读取；超时丢弃迟到结果，用户取消不能误报为超时。

    线程关闭是尽力而为，不能保证供应商停止计费；调用线程约50ms轮询退出。
    """
    parent=current_token();child=CancelToken(parent);expired=threading.Event()
    def expire():expired.set();child.cancel()
    timer=threading.Timer(seconds,expire);timer.daemon=True
    with cancel_scope(child):
        timer.start()
        try:
            child.checkpoint()
            yield
            child.checkpoint()
        except CancelledError:
            if expired.is_set() and not (parent and parent.event.is_set()):
                raise AgentError('model_timeout',phase=phase) from None
            raise
        finally:timer.cancel()
