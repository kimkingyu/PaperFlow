"""Authenticated HTTP adapters for the native Agent (never used by MCP)."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.routing import Route

from paperflow.engine.journals.models import JournalError

MAX_AGENT_BODY = 256 * 1024
_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "paused", "awaiting_approval"}


async def _body(request, allowed: set[str]) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise JournalError("INVALID_INPUT", "请求必须是 application/json")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_AGENT_BODY:
            raise JournalError("INPUT_TOO_LARGE", "Agent 请求体超过大小限制")
    try:
        value = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeError):
        raise JournalError("INVALID_INPUT", "请求不是有效 JSON") from None
    if not isinstance(value, dict) or set(value) - allowed:
        raise JournalError("INVALID_INPUT", "请求对象包含未知字段")
    return value


def _text(data: dict, field: str, maximum: int, *, default: str | None = None) -> str:
    value = data.get(field, default)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise JournalError("INVALID_INPUT", f"{field} 需要非空文本，最多 {maximum} 字符")
    return value.strip()


def _bool(data: dict, field: str, default: bool = False) -> bool:
    value = data.get(field, default)
    if type(value) is not bool:
        raise JournalError("INVALID_INPUT", f"{field} 必须是布尔值")
    return value


def build_agent_routes(runtime_getter: Callable, services_getter: Callable,
                       guard: Callable, failure: Callable, ok: Callable) -> list[Route]:
    """Build routes with injected services; importing this module starts no worker."""
    def success(value):
        return ok({"status": "success", "data": value})

    async def sessions(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            runtime = await run_in_threadpool(runtime_getter)
            if request.method == "GET":
                workspace = request.query_params.get("workspace_id") or None
                return success(await run_in_threadpool(runtime.list_sessions, workspace))
            data = await _body(request, {"workspace_id", "title"})
            workspace = _text(data, "workspace_id", 100)
            services = await run_in_threadpool(services_getter)
            if not await run_in_threadpool(services.workspaces.get, workspace):
                raise JournalError("NOT_FOUND", "工作区不存在")
            return success(await run_in_threadpool(runtime.create_session, workspace,
                                                  _text(data, "title", 300, default="新会话")))
        except Exception as exc:
            return failure(exc)

    async def session(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            runtime = await run_in_threadpool(runtime_getter)
            return success(await run_in_threadpool(runtime.get_session, request.path_params["session_id"]))
        except Exception as exc:
            return failure(exc)

    async def messages(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            data = await _body(request, {"message", "request_id", "allow_network", "allow_writes", "consent", "budget"})
            request_id = _text(data, "request_id", 128)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
                raise JournalError("INVALID_INPUT", "request_id 格式不正确")
            runtime = await run_in_threadpool(runtime_getter)
            value = await run_in_threadpool(
                runtime.send, request.path_params["session_id"], _text(data, "message", 60000), request_id,
                allow_network=_bool(data, "allow_network"), allow_writes=_bool(data, "allow_writes"),
                consent=_bool(data, "consent"), budget=data.get("budget"),
            )
            return success(value)
        except Exception as exc:
            return failure(exc)

    async def run(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            runtime = await run_in_threadpool(runtime_getter)
            return success(await run_in_threadpool(runtime.get_run, request.path_params["run_id"]))
        except Exception as exc:
            return failure(exc)

    async def control(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            action = request.path_params["operation"]
            if action not in {"cancel", "resume", "approve"}:
                raise JournalError("INVALID_INPUT", "未知 Agent 操作")
            allowed = {"consent"} if action == "resume" else {"approval_id", "approved"} if action == "approve" else set()
            data = await _body(request, allowed)
            runtime = await run_in_threadpool(runtime_getter)
            run_id = request.path_params["run_id"]
            if action == "cancel":
                value = await run_in_threadpool(runtime.cancel, run_id)
            elif action == "resume":
                value = await run_in_threadpool(runtime.resume, run_id, consent=_bool(data, "consent"))
            else:
                if "approved" not in data:
                    raise JournalError("INVALID_INPUT", "审批必须明确接受或拒绝")
                value = await run_in_threadpool(runtime.approve, run_id,
                                                _text(data, "approval_id", 128), _bool(data, "approved"))
            return success(value)
        except Exception as exc:
            return failure(exc)

    async def events(request):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            raw_cursor = request.query_params.get("after", "0")
            if not re.fullmatch(r"[0-9]{1,15}", raw_cursor):
                raise JournalError("INVALID_INPUT", "事件游标必须为非负整数")
            cursor = int(raw_cursor)
            runtime = await run_in_threadpool(runtime_getter)
            run_id = request.path_params["run_id"]
            await run_in_threadpool(runtime.get_run, run_id)
        except Exception as exc:
            return failure(exc)

        async def stream():
            nonlocal cursor
            idle = 0
            while not await request.is_disconnected():
                try:
                    rows = await run_in_threadpool(runtime.events, run_id, after=cursor)
                    for event in rows:
                        sequence = int(event["seq"])
                        if sequence <= cursor:
                            continue
                        cursor = sequence
                        payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
                        yield f"id: {cursor}\ndata: {payload}\n\n"
                    state = await run_in_threadpool(runtime.get_run, run_id)
                    if state["status"] in _TERMINAL:
                        # Drain events written between the previous event read and state read.
                        remaining = await run_in_threadpool(runtime.events, run_id, after=cursor)
                        for event in remaining:
                            if int(event["seq"]) > cursor:
                                cursor = int(event["seq"])
                                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False, allow_nan=False)}\n\n"
                        break
                    idle += 1
                    if idle % 30 == 0:
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.4)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never expose provider errors or keys in an unguarded streaming response.
                    yield 'event: error\ndata: {"error_code":"EVENT_STREAM_INTERRUPTED","message":"事件流中断，请重新连接；任务不会重复执行"}\n\n'
                    break

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Accel-Buffering": "no",
        })

    return [
        Route("/api/agent/sessions", sessions, methods=["GET", "POST"]),
        Route("/api/agent/sessions/{session_id}", session),
        Route("/api/agent/sessions/{session_id}/messages", messages, methods=["POST"]),
        Route("/api/agent/runs/{run_id}", run),
        Route("/api/agent/runs/{run_id}/events", events),
        Route("/api/agent/runs/{run_id}/{operation}", control, methods=["POST"]),
    ]
