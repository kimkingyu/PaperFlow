"""Fail-closed boundaries for the native business agent."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

MAX_ARGUMENT_BYTES = 32768
MAX_RESULT_CHARS = 24000
MAX_HISTORY_CHARS = 120000
_SECRET_KEYS = re.compile(r"(?i)^(api[_-]?key|authorization|password|secret|access_token|refresh_token|_session_key)$")
_SECRET_TEXT = re.compile(r"(?i)(sk-[\w-]{8,}|(?:bearer\s+)[\w.\-]{8,}|(?:api[_-]?key|x-api-key)\s*[:=]\s*[\"']?[\w.\-]{8,})")
_PATH_KEYS = {"file_path", "output_path", "project_dir", "input_paths", "csv_path", "sidecar_path", "directory", "path"}


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def redact_text(value: str, key: str = "") -> str:
    if key:
        value = value.replace(key, "[REDACTED]")
    return _SECRET_TEXT.sub("[REDACTED]", value)


def scrub(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 16:
        return {"truncated": True, "reason": "depth_limit"}
    if isinstance(value, dict):
        result = {}
        for name, item in list(value.items())[:200]:
            name = redact_text(str(name), key)
            if name.lower() in {"thinking", "reasoning", "reasoning_content", "chain_of_thought"}:
                continue
            result[name] = "[REDACTED]" if _SECRET_KEYS.fullmatch(name) else scrub(item, key, depth + 1)
        if len(value) > 200:
            result["truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        items = [scrub(item, key, depth + 1) for item in value[:200]]
        if len(value) > 200:
            items.append({"truncated": True, "reason": "item_limit"})
        return items
    if isinstance(value, str):
        text = redact_text(value, key)
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n[truncated: partial material]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return redact_text(str(value), key)[:2000]


def bounded(value: Any, key: str = "", limit: int = MAX_RESULT_CHARS) -> Any:
    safe = scrub(value, key)
    text = canonical(safe)
    if len(text) > limit:
        return {"truncated": True, "reason": "result_limit", "partial_text": text[:limit],
                "warning": "Only partial material was returned; do not claim full-paper understanding."}
    return safe


def fingerprint(config: Any) -> str:
    return digest({"provider": config.provider, "base_url": config.base_url, "model": config.model,
                   "consented": bool(config.consented), "credential_digest": digest(config.api_key())})


def endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError("INVALID_ENDPOINT", "模型端点必须是无凭证、查询及片段的HTTP(S)地址")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise AgentError("INSECURE_ENDPOINT", "仅回环模型端点允许HTTP")
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def validate_schema(schema: Any, depth: int = 0) -> None:
    if depth > 30:
        raise AgentError("INVALID_SCHEMA", "schema嵌套过深")
    if isinstance(schema, dict):
        for name, value in schema.items():
            if name in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(value, str) or not value.startswith("#")):
                raise AgentError("INVALID_SCHEMA", "禁止schema加载外部网络或文件引用")
            validate_schema(value, depth + 1)
    elif isinstance(schema, list):
        for value in schema:
            validate_schema(value, depth + 1)


def validate_arguments(arguments: Any, tool: dict, workspace_id: str, key: str = "") -> dict:
    if not isinstance(arguments, dict):
        raise AgentError("INVALID_ARGUMENTS", "工具参数必须是完整JSON对象")
    if len(canonical(arguments).encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise AgentError("ARGUMENT_LIMIT", "工具参数超过大小上限")
    schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        raise AgentError("INVALID_SCHEMA", "工具没有可校验的参数schema")
    try:
        validate_schema(schema)
        Draft202012Validator.check_schema(schema)
        errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: str(e.path))
    except Exception:
        raise AgentError("INVALID_SCHEMA", "工具schema无效") from None
    if errors:
        raise AgentError("INVALID_ARGUMENTS", "工具参数不符合schema: " + redact_text(errors[0].message, key)[:500])

    def inspect(value: Any, depth: int = 0) -> None:
        if depth > 20:
            raise AgentError("INVALID_ARGUMENTS", "参数嵌套过深")
        if isinstance(value, dict):
            for name, item in value.items():
                if name in _PATH_KEYS or _SECRET_KEYS.fullmatch(name):
                    raise AgentError("UNSAFE_ARGUMENTS", "仅允许受管资源ID，不接受路径或凭证参数")
                if name == "workspace_id" and item != workspace_id:
                    raise AgentError("WORKSPACE_MISMATCH", "禁止跨工作区执行")
                if name in {"origin", "allow_network", "approved"}:
                    raise AgentError("UNSAFE_ARGUMENTS", "模型不可修改运行授权上下文")
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
        elif isinstance(value, str) and redact_text(value, key) != value:
            raise AgentError("UNSAFE_ARGUMENTS", "工具参数不可包含模型凭证或其他疑似密钥")
    inspect(arguments)
    return arguments


def public_catalog(catalog: list, key: str = "") -> list:
    tools = []
    seen = set()
    for item in catalog:
        if not isinstance(item, dict) or not item.get("available", False):
            continue
        name = item.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name) or name in seen:
            raise AgentError("INVALID_CATALOG", "工具目录名称不合法或重复")
        schema = item.get("input_schema")
        if isinstance(schema, dict):
            schema = json.loads(redact_text(canonical(schema), key))
        if not isinstance(schema, dict) or len(canonical(schema)) > MAX_ARGUMENT_BYTES:
            raise AgentError("INVALID_CATALOG", "工具schema缺失或超限")
        try:
            validate_schema(schema)
            Draft202012Validator.check_schema(schema)
        except Exception:
            raise AgentError("INVALID_CATALOG", "工具schema无效") from None
        seen.add(name)
        tools.append({"name": name, "description": redact_text(str(item.get("description", "")), key)[:2000],
                      "input_schema": schema, **{flag: bool(item.get(flag)) for flag in
                      ("mutates", "network", "desktop", "approval_required")},
                      "category": str(item.get("category", ""))[:80]})
    if len(tools) > 128 or len(canonical(tools)) > MAX_HISTORY_CHARS:
        raise AgentError("CATALOG_LIMIT", "工具目录超过上限")
    return tools
