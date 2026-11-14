"""模型配置共用文件操作与视觉设置：密钥只写本机 .env，API 永不回显。"""
import os
from pathlib import Path
import re
import struct
import threading
from urllib.parse import urlsplit
import zlib

SETTINGS_LOCK = threading.RLock()
VISION_NAMES = ("VISION_API_KEY", "VISION_BASE_URL", "VISION_MODEL", "VISION_TIMEOUT_SECONDS")


def read_environment(root, names):
    """只解析指定变量，环境变量覆盖 .env，不执行 shell 内容。"""
    with SETTINGS_LOCK:
        values = {}
        path = Path(root) / ".env"
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, separator, value = line.strip().removeprefix("export ").partition("=")
                if separator and key.strip() in names:
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    values[key.strip()] = value
        values.update({name: os.environ[name] for name in names if name in os.environ})
        return values


def atomic_write(path, text):
    """同目录临时文件替换，调用方持配置锁，临时文件同样由 Git 忽略。"""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_environment(root, updates):
    """共享锁下更新指定行，保留另一模型及其他配置的所有变量。"""
    with SETTINGS_LOCK:
        path = Path(root) / ".env"
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        kept = [line for line in lines if line.strip().removeprefix("export ").partition("=")[0].strip() not in updates]
        atomic_write(path, "\n".join(kept + [key + "=" + value for key, value in updates.items()]) + "\n")


def validate_base_url(value):
    """接受兼容服务基础地址，远端需 HTTPS，本机可 HTTP。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写 API Base URL。")
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or parsed.scheme == "http" and local)
            or any(character.isspace() for character in value)
            or parsed.path.endswith("/chat/completions")):
        raise ValueError("请填写 HTTPS 基础地址，地址末尾无需 /chat/completions。")
    return value


def validate_key(value):
    """支持供应商带点号的 Key，拒绝空白、控制字符及换行注入。"""
    if not isinstance(value, str) or value and not re.fullmatch(r"[!-~]{16,512}", value):
        raise ValueError("API 密钥格式不正确。")
    return value


def validate_model(value):
    """模型 ID 由用户选择，排除空串、控制字符和异常长值。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value):
        raise ValueError("请填写有效的模型 ID。")
    return value


class VisionModelSettings:
    """视觉识图和商品标题修正共用配置，保存空白 Key 时保留已有凭据。"""
    def __init__(self, root):
        self.root = Path(root)

    def public(self):
        values = read_environment(self.root, VISION_NAMES)
        return {"configured": bool(values.get("VISION_API_KEY", "").strip()),
                "base_url": values.get("VISION_BASE_URL", ""),
                "model": values.get("VISION_MODEL", ""),
                "timeout_seconds": values.get("VISION_TIMEOUT_SECONDS", "45"),
                "environment_overrides": [name for name in VISION_NAMES if name in os.environ]}

    def save(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"base_url", "model", "api_key", "timeout_seconds"}:
            raise ValueError("不支持的视觉模型配置字段。")
        with SETTINGS_LOCK:
            current = self.public()
            base = validate_base_url(payload.get("base_url", current["base_url"]))
            model = validate_model(payload.get("model", current["model"]))
            key = validate_key(payload.get("api_key", ""))
            try:
                timeout = float(payload.get("timeout_seconds", current["timeout_seconds"]))
                if not 1 <= timeout <= 60:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("视觉请求超时需为 1 至 60 秒。") from None
            updates = {"VISION_BASE_URL": base, "VISION_MODEL": model, "VISION_TIMEOUT_SECONDS": str(timeout)}
            if key:
                updates["VISION_API_KEY"] = key
            write_environment(self.root, updates)
        return self.public()

    def test(self):
        """用 32×32 白色测试图验证真实图片输入能力，不发送用户商品资料。"""
        from .vision_client import VisionClient
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        image = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\xff\xff" * 32) * 32)) + chunk(b"IEND", b""))
        client = VisionClient(self.root)
        client.identify(image, "image/png", "请识别图片的主要颜色，简短回答。")
        return {"ok": True, "model": client.model, "vision": True}
