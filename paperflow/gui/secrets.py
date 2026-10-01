"""Model credentials for GUI self-hosted scoring.

Default: session memory only. Optional: environment variable, or the OS keyring
when the user ticks "remember" and the optional ``keyring`` package is installed.
Keys are never written to reports, logs, the inbox or JSON config files.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Optional

KEYRING_SERVICE = "paperflow-gui"
ENV_KEYS = {"openai_compatible": "PAPERFLOW_MODEL_API_KEY", "openai_responses": "PAPERFLOW_MODEL_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY"}
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{8,}"),
]


def _keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


def keyring_available() -> bool:
    return _keyring() is not None


def redact(text: str, known: Optional[str] = None) -> str:
    """Mask API keys in any message that could be shown or logged."""
    text = str(text or "")
    if known and len(known) >= 4:
        text = text.replace(known, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


@dataclass
class ModelConfig:
    provider: str = ""
    base_url: str = ""
    model: str = ""
    remembered: bool = False
    consented: bool = False
    _session_key: str = field(default="", repr=False)

    @property
    def keyring_account(self) -> str:
        # Credentials for one endpoint must never be reused by a different endpoint.
        digest = hashlib.sha256(self.base_url.rstrip("/").encode("utf-8")).hexdigest()
        return f"{self.provider}:{digest}"

    @property
    def configured(self) -> bool:
        local = urlsplit(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        return bool(self.provider and self.model and self.base_url and (self.api_key() or local))

    def public(self) -> dict:
        return {"configured": self.configured,
                "provider": self.provider, "model": self.model, "base_url": self.base_url,
                "remembered": self.remembered, "consented": self.consented,
                "keyring_available": keyring_available()}

    def api_key(self) -> str:
        if self._session_key:
            return self._session_key
        ring = _keyring()
        if self.remembered and ring and self.provider:
            try:
                value = ring.get_password(KEYRING_SERVICE, self.keyring_account)
                if value:
                    return value
            except Exception:
                pass
        return os.environ.get(ENV_KEYS.get(self.provider, ""), "") if self.provider else ""


class SecretStore:
    """Process-wide holder; one GUI server owns one instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.config = ModelConfig()

    def configure(self, provider: str, base_url: str, model: str, api_key: str, remember: bool) -> dict:
        if type(remember) is not bool or not all(isinstance(v, str) for v in (provider, base_url, model, api_key)):
            raise ValueError("模型配置字段类型不正确")
        from .model_scoring import validate_endpoint
        validate_endpoint(provider, base_url, model)
        base_url = base_url.rstrip("/")
        with self._lock:
            previous = self.config
            same_endpoint = (provider, base_url) == (previous.provider, previous.base_url.rstrip("/"))
            config = ModelConfig(provider=provider, base_url=base_url, model=model)
            if api_key:
                config._session_key = api_key
            elif same_endpoint:
                config._session_key = previous._session_key
            if remember:
                ring = _keyring()
                if ring is None:
                    raise RuntimeError("未安装 keyring，无法记住 Key；可 pip install paperflow-mcp[gui]，或只在本次会话使用")
                if config._session_key:
                    ring.set_password(KEYRING_SERVICE, config.keyring_account, config._session_key)
                config.remembered = True
            same_key = config.api_key() == previous.api_key()
            config.consented = bool(previous.consented and same_endpoint and model == previous.model and same_key)
            # Failed keyring operations leave the previous valid configuration intact.
            self.config = config
            return config.public()

    def consent(self, value: bool) -> dict:
        with self._lock:
            self.config.consented = bool(value)
            return self.config.public()
