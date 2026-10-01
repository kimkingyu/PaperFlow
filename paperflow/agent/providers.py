"""Native, non-streaming tool calling for three explicitly chosen protocols."""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .security import AgentError, MAX_ARGUMENT_BYTES, canonical, endpoint

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT = 45


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AgentError("PROVIDER_REDIRECT", "拒绝重定向模型请求及凭证")


def http_transport(url: str, headers: dict, body: dict) -> dict:
    request = urllib.request.Request(url, data=canonical(body).encode("utf-8"), headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise AgentError("PROVIDER_RESPONSE_LIMIT", "模型响应超过大小上限")
        result = json.loads(data)
    except AgentError:
        raise
    except urllib.error.HTTPError as exc:
        # Do not echo upstream bodies, headers, URLs or credentials.
        raise AgentError("PROVIDER_HTTP_ERROR", "模型请求失败，HTTP " + str(exc.code)) from None
    except Exception:
        raise AgentError("PROVIDER_TRANSPORT_ERROR", "模型请求超时、网络失败或返回无效JSON") from None
    if not isinstance(result, dict):
        raise AgentError("PROVIDER_PROTOCOL_ERROR", "模型响应不是JSON对象")
    return result


def _arguments(value: Any) -> dict:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise AgentError("ARGUMENT_LIMIT", "模型工具参数过大")
        try:
            value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except Exception:
            raise AgentError("INCOMPLETE_TOOL_ARGUMENTS", "工具参数不是完整JSON，未执行") from None
    if not isinstance(value, dict):
        raise AgentError("INCOMPLETE_TOOL_ARGUMENTS", "工具参数必须是完整对象，未执行")
    return value


def _call(call_id: Any, name: Any, arguments: Any) -> dict:
    if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or not isinstance(name, str) or not name:
        raise AgentError("PROVIDER_PROTOCOL_ERROR", "模型工具调用缺少ID或名称")
    return {"id": call_id, "name": name, "arguments": _arguments(arguments)}


def _usage(raw: Any, provider: str) -> dict:
    if not isinstance(raw, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    fields = ("prompt_tokens", "completion_tokens") if provider == "openai_compatible" else ("input_tokens", "output_tokens")
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    incoming, outgoing = number(raw.get(fields[0])), number(raw.get(fields[1]))
    result = {"input_tokens": incoming, "output_tokens": outgoing,
              "total_tokens": incoming + outgoing if incoming is not None and outgoing is not None else None}
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if number(raw.get(field)) is not None:
            result[field] = number(raw[field])
    return result


class ProviderAdapter:
    def __init__(self, config: Any, transport: Optional[Callable] = None):
        self.config = config
        self.transport = transport or http_transport
        self.last_usage = _usage(None, config.provider)

    def request(self, system: str, messages: list, tools: list, continuation: Optional[dict] = None) -> dict:
        provider = self.config.provider
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key()
        local = urlsplit(self.config.base_url)
        if not key and not (local.scheme == "http" and local.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise AgentError("MODEL_NOT_CONFIGURED", "非回环模型端点需要凭证")
        continuation = continuation or {}
        if provider == "openai_compatible":
            url = endpoint(self.config.base_url, "/chat/completions")
            if key:
                headers["Authorization"] = "Bearer " + key
            body = self._chat(system, messages, tools)
        elif provider == "openai_responses":
            url = endpoint(self.config.base_url, "/responses")
            if key:
                headers["Authorization"] = "Bearer " + key
            body = self._responses(system, messages, tools, continuation)
        elif provider == "anthropic":
            url = endpoint(self.config.base_url, "/messages")
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
            body = self._anthropic(system, messages, tools, continuation)
        else:
            raise AgentError("UNSUPPORTED_PROVIDER", "请选择受支持的原生工具调用协议")
        try:
            raw = self.transport(url, headers, body)
        except AgentError:
            raise
        except Exception:
            raise AgentError("PROVIDER_TRANSPORT_ERROR", "模型请求失败；未自动重试或切换端点") from None
        if not isinstance(raw, dict) or len(canonical(raw).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise AgentError("PROVIDER_PROTOCOL_ERROR", "模型响应无效或过大")
        self.last_usage = _usage(raw.get("usage"), provider)
        if raw.get("error"):
            raise AgentError("TOOLS_UNSUPPORTED_OR_PROVIDER_ERROR", "模型或端点拒绝请求；必须支持原生工具调用，不会模拟执行")
        if provider == "openai_compatible":
            choices = raw.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "Chat Completions缺少choices")
            choice = choices[0]
            message = choice.get("message", {})
            if not isinstance(message, dict):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "Chat Completions消息无效")
            calls = message.get("tool_calls") or []
            if choice.get("finish_reason") == "tool_calls" and not calls or message.get("function_call"):
                raise AgentError("TOOLS_UNSUPPORTED", "模型未返回受支持的原生tool_calls")
            parsed = []
            for item in calls:
                if not isinstance(item, dict) or item.get("type") != "function" or not isinstance(item.get("function"), dict):
                    raise AgentError("TOOLS_UNSUPPORTED", "模型工具调用格式不受支持")
                parsed.append(_call(item.get("id"), item["function"].get("name"), item["function"].get("arguments")))
            text = message.get("content") or ""
            if not isinstance(text, str):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "模型文字内容无效")
            stop = choice.get("finish_reason")
        elif provider == "openai_responses":
            if raw.get("status") in {"failed", "incomplete", "cancelled"}:
                raise AgentError("PROVIDER_INCOMPLETE", "Responses未完整完成，工具未执行")
            output = raw.get("output")
            if not isinstance(output, list):
                raise AgentError("TOOLS_UNSUPPORTED", "Responses缺少原生output")
            parsed, texts = [], []
            for item in output:
                if not isinstance(item, dict):
                    raise AgentError("PROVIDER_PROTOCOL_ERROR", "Responses output无效")
                if item.get("type") == "function_call":
                    parsed.append(_call(item.get("call_id"), item.get("name"), item.get("arguments")))
                elif item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            texts.append(part.get("text", ""))
            text, stop = "\n".join(texts), raw.get("status")
        else:
            content = raw.get("content")
            if not isinstance(content, list):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "Anthropic缺少content")
            parsed, texts = [], []
            for item in content:
                if not isinstance(item, dict):
                    raise AgentError("PROVIDER_PROTOCOL_ERROR", "Anthropic content无效")
                if item.get("type") == "tool_use":
                    parsed.append(_call(item.get("id"), item.get("name"), item.get("input")))
                elif item.get("type") == "text":
                    texts.append(item.get("text", ""))
                # thinking/redacted_thinking are deliberately never persisted.
            text, stop = "\n".join(texts), raw.get("stop_reason")
            if stop == "tool_use" and not parsed:
                raise AgentError("TOOLS_UNSUPPORTED", "Anthropic未返回原生tool_use")
        if stop in {"length", "max_tokens"}:
            raise AgentError("PROVIDER_INCOMPLETE", "模型输出达到上限，未执行不完整工具调用")
        if provider == "openai_compatible":
            if stop not in {"stop", "tool_calls"} or message.get("refusal"):
                raise AgentError("PROVIDER_STOP_REJECTED", "模型拒答、过滤或停止原因未知，工具未执行")
            if bool(parsed) != (stop == "tool_calls"):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "模型工具调用与停止原因不一致，未执行")
        elif provider == "openai_responses":
            if stop != "completed" or any(item.get("status") not in {None, "completed"} for item in output):
                raise AgentError("PROVIDER_STOP_REJECTED", "Responses尚未完成或状态未知，工具未执行")
            if any(part.get("type") == "refusal" for item in output if item.get("type") == "message" for part in item.get("content", [])):
                raise AgentError("PROVIDER_STOP_REJECTED", "Responses拒答，工具未执行")
        else:
            if stop not in {"end_turn", "tool_use"}:
                raise AgentError("PROVIDER_STOP_REJECTED", "Anthropic拒答、暂停或停止原因未知，工具未执行")
            if bool(parsed) != (stop == "tool_use"):
                raise AgentError("PROVIDER_PROTOCOL_ERROR", "Anthropic工具调用与停止原因不一致，未执行")
        ids = [item["id"] for item in parsed]
        if len(ids) != len(set(ids)) or len(parsed) > 16:
            raise AgentError("PROVIDER_PROTOCOL_ERROR", "工具调用ID重复或单轮调用超过16次")
        protocol_context = None
        if parsed and provider == "openai_responses" and any(item.get("type") == "reasoning" for item in output):
            protocol_context = copy.deepcopy(output)
            for index, item in enumerate(protocol_context):
                if item.get("type") == "reasoning":
                    if not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
                        raise AgentError("PROTOCOL_CONTINUATION_UNAVAILABLE", "Responses推理模型未返回所需encrypted_content；不能丢弃协议续接后假装继续")
                    protocol_context[index] = {"type": "reasoning", "id": item.get("id"), "summary": [],
                                               "encrypted_content": item["encrypted_content"]}
        elif parsed and provider == "anthropic" and any(item.get("type") in {"thinking", "redacted_thinking"} for item in content):
            protocol_context = copy.deepcopy(content)
            for item in protocol_context:
                if item.get("type") == "thinking" and (not isinstance(item.get("signature"), str) or not item["signature"]):
                    raise AgentError("PROTOCOL_CONTINUATION_UNAVAILABLE", "Anthropic thinking缺少签名，不能截断后继续工具轮")
                if item.get("type") == "redacted_thinking" and not isinstance(item.get("data"), str):
                    raise AgentError("PROTOCOL_CONTINUATION_UNAVAILABLE", "Anthropic redacted_thinking无效")
        if protocol_context is not None:
            if len(canonical(protocol_context).encode("utf-8")) > 512000:
                raise AgentError("PROTOCOL_CONTINUATION_LIMIT", "协议续接数据超过内存上限")
            if key and key in canonical(protocol_context):
                raise AgentError("PROTOCOL_SECRET_LEAK", "协议续接包含凭证，不回传或保存")
        return {"text": text, "tool_calls": parsed, "usage": self.last_usage, "continuation": protocol_context}

    def _chat(self, system: str, messages: list, tools: list) -> dict:
        history = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "tool":
                history.append({"role": "tool", "tool_call_id": message["call_id"], "content": canonical(message["result"])})
            elif role == "assistant":
                item = {"role": role, "content": message.get("text") or None}
                if message.get("tool_calls"):
                    item["tool_calls"] = [{"id": call["id"], "type": "function", "function":
                                           {"name": call["name"], "arguments": canonical(call["arguments"])}}
                                          for call in message["tool_calls"]]
                history.append(item)
            else:
                history.append({"role": "user", "content": message["text"]})
        return {"model": self.config.model, "messages": history, "tools":
                [{"type": "function", "function": {"name": item["name"], "description": item["description"],
                  "parameters": item["input_schema"]}} for item in tools], "tool_choice": "auto", "stream": False}

    def _responses(self, system: str, messages: list, tools: list, continuation: dict) -> dict:
        history = []
        for message in messages:
            marker = message.get("_protocol_turn")
            if marker:
                if marker not in continuation:
                    raise AgentError("PROTOCOL_CONTINUATION_LOST", "必要的协议续接仅在原进程内存中；不重放副作用或伪造续接")
                history.extend(copy.deepcopy(continuation[marker]))
                continue
            if message["role"] == "tool":
                history.append({"type": "function_call_output", "call_id": message["call_id"],
                                "output": canonical(message["result"])})
            else:
                if message.get("text"):
                    history.append({"role": message["role"], "content": message["text"]})
                for call in message.get("tool_calls", []):
                    history.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                                    "arguments": canonical(call["arguments"])})
        return {"model": self.config.model, "instructions": system, "input": history, "store": False,
                "include": ["reasoning.encrypted_content"],
                "tools": [{"type": "function", "name": item["name"], "description": item["description"],
                           "parameters": item["input_schema"], "strict": False} for item in tools], "tool_choice": "auto", "stream": False}

    def _anthropic(self, system: str, messages: list, tools: list, continuation: dict) -> dict:
        history = []
        for message in messages:
            role = "user" if message["role"] == "tool" else message["role"]
            marker = message.get("_protocol_turn")
            if marker:
                if marker not in continuation:
                    raise AgentError("PROTOCOL_CONTINUATION_LOST", "Anthropic签名续接已随原进程退出丢失；未重放工具")
                parts = copy.deepcopy(continuation[marker])
            elif message["role"] == "tool":
                parts = [{"type": "tool_result", "tool_use_id": message["call_id"],
                          "content": canonical(message["result"]), "is_error": not message.get("ok", True)}]
            else:
                parts = ([{"type": "text", "text": message["text"]}] if message.get("text") else [])
                parts += [{"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]}
                          for call in message.get("tool_calls", [])]
            if history and history[-1]["role"] == role:
                history[-1]["content"].extend(parts)
            else:
                history.append({"role": role, "content": parts})
        return {"model": self.config.model, "system": system, "messages": history, "max_tokens": 8192,
                "tools": [{"name": item["name"], "description": item["description"], "input_schema": item["input_schema"]}
                          for item in tools], "tool_choice": {"type": "auto"}, "stream": False}
