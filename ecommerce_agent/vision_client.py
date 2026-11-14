"""模型调用封装：图片识别与文本改写复用 OpenAI 兼容传输和本机配置。"""

import base64
import json
import math
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .provider import _read_response
from .api_settings import VISION_NAMES, read_environment


class VisionError(Exception):
    """可公开返回的识图错误，屏蔽供应商响应体、图片内容和凭据。"""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def vision_environment(root):
    """环境变量优先于项目 .env，仅解析视觉配置白名单，不执行文件内容。"""
    return read_environment(root, VISION_NAMES)


class VisionClient:
    """单次、限时的多模态请求；地址与密钥仅来自服务端配置。"""

    def __init__(self, root):
        config = vision_environment(root)
        self.key = config.get("VISION_API_KEY", "").strip()
        self.model = config.get("VISION_MODEL", "").strip()
        base = config.get("VISION_BASE_URL", "").strip()
        if not self.key or not self.model or not base:
            raise VisionError("请在本机 .env 配置 VISION_API_KEY、VISION_BASE_URL 和 VISION_MODEL。", 503)
        parsed = urlsplit(base)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or parsed.scheme == "http" and local)):
            raise VisionError("VISION_BASE_URL 需为 HTTPS 地址，本机模型可使用 HTTP。", 503)
        self.url = base.rstrip("/") + "/chat/completions"
        try:
            self.timeout = float(config.get("VISION_TIMEOUT_SECONDS", "45"))
            if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
                raise ValueError()
        except ValueError:
            raise VisionError("VISION_TIMEOUT_SECONDS 需在 1 至 60 秒之间。", 503) from None

    def identify(self, image, mime_type, prompt):
        """发送图片字节与命名指令，返回供应商文本；不记录或保存图片。"""
        data_url = "data:" + mime_type + ";base64," + base64.b64encode(image).decode("ascii")
        return self._complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "text", "text": "识别这张商品图，按七个字段输出 JSON。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ])

    def rewrite(self, product, prompt):
        """复用同一兼容模型的文本消息能力，按原始商品事实生成命名字段。"""
        return self._complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(product, ensure_ascii=False)},
        ])

    def _complete(self, messages):
        """文本和图片共享传输、超时、鉴权及响应格式检查。"""
        body = {"model": self.model, "max_tokens": 1024, "messages": messages}
        request = Request(self.url, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
                          method="POST")
        try:
            raw = _read_response(request, self.timeout)
            if len(raw) > 2_000_000:
                raise ValueError()
            payload = json.loads(raw)
            message = payload["choices"][0]["message"]
            text = message.get("content")
            if message.get("tool_calls") or not isinstance(text, str) or not text.strip():
                raise ValueError()
            return text
        except HTTPError as error:
            if error.code in (401, 403):
                raise VisionError("视觉服务拒绝访问，请检查 API Key 和模型权限。", 503) from None
            raise VisionError("视觉服务暂不可用，请稍后重试。") from None
        except (TimeoutError, socket.timeout):
            raise VisionError("AI 识图超时，请稍后重试。", 504) from None
        except URLError:
            raise VisionError("无法连接视觉服务，请检查网络和 VISION_BASE_URL。") from None
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            raise VisionError("视觉服务返回了无法解析的响应。") from None
