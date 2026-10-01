"""No external model, network, desktop or user's databases in these tests."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperflow.agent import AgentError, AgentRuntime
from paperflow.agent.providers import ProviderAdapter, _NoRedirect, http_transport
from paperflow.agent.security import MAX_RESULT_CHARS, canonical
from paperflow.engine.planning.service import ResearchPlanner

KEY = "sk-native-test-super-secret-123456789"


def config(provider="openai_compatible"):
    return SimpleNamespace(provider=provider, base_url="https://model.invalid/v1", model="fake-tool-model",
                           consented=True, api_key=lambda: KEY, public=lambda: {})


def tool(name="create_plan", **flags):
    return {"name": name, "description": "保存明确的研究规划，不代表实验完成", "input_schema":
            {"type": "object", "properties": {"title": {"type": "string", "minLength": 1}},
             "required": ["title"], "additionalProperties": False}, "category": "planning",
            "available": True, "unavailable_reason": None, "mutates": True, "network": False,
            "desktop": False, "approval_required": False, **flags}


def reply(provider="openai_compatible", calls=None, text="", usage=True):
    calls = calls or []
    if provider == "openai_compatible":
        body = {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message":
                 {"role": "assistant", "content": text, "reasoning_content": "hidden chain must never be stored",
                  "tool_calls": [{"id": call["id"], "type": "function", "function":
                                  {"name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in calls]}}]}
        if usage:
            body["usage"] = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    elif provider == "openai_responses":
        body = {"status": "completed", "output": [{"type": "reasoning", "id": "rs_test", "encrypted_content": "encrypted-opaque-test", "summary": [{"text": "hidden chain must never be stored"}]}]}
        body["output"] += [{"type": "function_call", "call_id": call["id"], "name": call["name"],
                            "arguments": json.dumps(call["arguments"])} for call in calls]
        if text:
            body["output"].append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        if usage:
            body["usage"] = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}
    else:
        body = {"stop_reason": "tool_use" if calls else "end_turn", "content": [{"type": "thinking", "thinking": "hidden chain must never be stored", "signature": "signed-opaque-test"}]}
        body["content"] += [{"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]} for call in calls]
        if text:
            body["content"].append({"type": "text", "text": text})
        if usage:
            body["usage"] = {"input_tokens": 10, "output_tokens": 3}
    return body


def call(call_id="call1", name="create_plan", **arguments):
    return {"id": call_id, "name": name, "arguments": arguments or {"title": "不伪造结果的科研规划"}}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers, body):
        self.requests.append((url, headers, body))
        assert self.responses, "unexpected extra model request"
        response = self.responses.pop(0)
        return response() if callable(response) else response


def wait(runtime, run_id, statuses=None):
    statuses = statuses or {"completed", "failed", "cancelled", "interrupted", "awaiting_approval"}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = runtime.get_run(run_id)
        if current["status"] in statuses:
            return current
        threading.Event().wait(0.005)
    pytest.fail("agent did not reach expected state: " + repr(runtime.get_run(run_id)))


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real network must never be called")
    import socket
    import urllib.request
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_three_native_protocols_execute_real_sqlite_planner(tmp_path, provider):
    planner = ResearchPlanner(data_dir=str(tmp_path / "planning"))
    operations = []
    def execute(name, arguments, **context):
        operations.append((name, arguments, context))
        result = planner.create({"title": arguments["title"], "goal": "保存可验证实验规划，不预设实验结果"})
        return {"ok": result["status"] == "success", "data": result}
    transport = Transport(reply(provider, [call()]), reply(provider, text="已保存规划，未完成实验。"))
    runtime = AgentRuntime(tmp_path / "agent", lambda: [tool()], execute, lambda: config(provider), transport)
    assert not transport.requests
    session = runtime.create_session("workspace1")
    sent = runtime.send(session["id"], "保存所选课题", "req1", consent=True, allow_writes=True)
    run = wait(runtime, sent["id"])
    assert run["status"] == "completed", run
    assert len(operations) == 1
    assert operations[0][2] == {"workspace_id": "workspace1", "origin": "native_agent", "allow_network": False, "approved": False}
    assert planner.list_projects()["data"]["projects"][0]["title"] == "不伪造结果的科研规划"
    assert run["usage"]["model_calls"] == 2
    assert run["usage"]["tool_calls"] == 1
    assert run["usage"]["total_tokens"] == 26
    second = transport.requests[1][2]
    if provider == "openai_compatible":
        assert second["messages"][-1]["role"] == "tool"
        assert second["messages"][-1]["tool_call_id"] == "call1"
        assert transport.requests[0][0].endswith("/chat/completions")
    elif provider == "openai_responses":
        assert second["input"][-1]["type"] == "function_call_output"
        assert second["input"][-1]["call_id"] == "call1"
        assert second["store"] is False
        assert transport.requests[0][0].endswith("/responses")
    else:
        result = second["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "call1"
        assert transport.requests[0][0].endswith("/messages")
    events = runtime.events(run["id"])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert runtime.events(run["id"], events[-2]["seq"]) == events[-1:]
    stored = canonical(runtime.get_session(session["id"])) + canonical(events)
    assert "hidden chain must never be stored" not in stored and KEY not in stored
    runtime.shutdown()
    db = sqlite3.connect(tmp_path / "agent" / "native_agent.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM actions WHERE status='settled'").fetchone()[0] == 1
    assert KEY.encode() not in (tmp_path / "agent" / "native_agent.sqlite3").read_bytes()
    db.close()


def harness(tmp_path, responses=None, tools=None, execute=None, cfg=None):
    executed = []
    def default_execute(name, arguments, **context):
        executed.append((name, arguments, context))
        return {"ok": True, "data": {"saved": True}}
    transport = Transport(*(responses or [reply(calls=[call()]), reply(text="根据结果已保存")]))
    cfg = cfg or config()
    runtime = AgentRuntime(tmp_path, lambda: tools if tools is not None else [tool()], execute or default_execute, lambda: cfg, transport)
    session = runtime.create_session("workspace1")
    return runtime, session, executed, transport, cfg


def test_consent_missing_and_duplicate_request_no_reexecution(tmp_path):
    runtime, session, executed, transport, cfg = harness(tmp_path)
    with pytest.raises(AgentError, match="明确同意"):
        runtime.send(session["id"], "规划", "r")
    assert not transport.requests
    first = runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)
    wait(runtime, first["id"])
    duplicate = runtime.send(session["id"], "重复内容不会重新发送", "r", consent=False)
    assert duplicate["id"] == first["id"] and len(executed) == 1 and len(transport.requests) == 2
    runtime.shutdown()


def test_specific_approval_and_duplicate_approval(tmp_path):
    runtime, session, executed, transport, cfg = harness(tmp_path, tools=[tool(network=True, approval_required=True)])
    sent = runtime.send(session["id"], "规划", "r", consent=True)
    waiting = wait(runtime, sent["id"])
    assert waiting["status"] == "awaiting_approval" and not executed
    approval = waiting["pending_approval"]
    assert approval["tool_name"] == "create_plan"
    approved = runtime.approve(sent["id"], approval["approval_id"], True)
    assert approved["status"] in {"queued", "running"}
    final = wait(runtime, sent["id"], {"completed", "failed"})
    assert final["status"] == "completed", final
    runtime.approve(sent["id"], approval["approval_id"], True)
    assert len(executed) == 1 and executed[0][2]["approved"] and executed[0][2]["allow_network"]
    assert not final["allow_network"] and not final["allow_writes"]
    runtime.shutdown()


@pytest.mark.parametrize("change", ["endpoint", "key", "consent", "schema", "expiry"])
def test_old_approval_rejected_after_changes(tmp_path, change):
    tools = [tool()]
    runtime, session, executed, transport, cfg = harness(tmp_path, tools=tools)
    sent = runtime.send(session["id"], "规划", "r", consent=True)
    waiting = wait(runtime, sent["id"])
    approval_id = waiting["pending_approval"]["approval_id"]
    if change == "endpoint":
        cfg.base_url = "https://different.invalid/v1"
    elif change == "key":
        cfg.api_key = lambda: "sk-other-long-credential"
    elif change == "consent":
        cfg.consented = False
    elif change == "schema":
        tools[0]["input_schema"]["properties"]["title"]["maxLength"] = 2
    else:
        with runtime.store.transaction():
            runtime.store.db.execute("UPDATE approvals SET expires_at=0 WHERE id=?", (approval_id,))
    rejected = runtime.approve(sent["id"], approval_id, True)
    assert rejected["status"] == "interrupted" and rejected["error"] and not executed
    runtime.shutdown()


def test_cancel_inflight_model_records_usage_but_never_tool(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def delayed():
        entered.set()
        assert release.wait(3)
        return reply(calls=[call()])
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=[delayed])
    run = runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)
    assert entered.wait(2)
    runtime.cancel(run["id"])
    release.set()
    deadline = time.monotonic() + 3
    while run["id"] in runtime._threads and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    final = runtime.get_run(run["id"])
    assert final["status"] == "cancelled" and not executed
    assert final["usage"]["total_tokens"] == 13
    runtime.shutdown()


def test_cancel_inflight_tool_settles_once_and_no_second_tool(tmp_path):
    entered, release = threading.Event(), threading.Event()
    executed = []
    def execute(name, arguments, **context):
        executed.append(name)
        entered.set()
        assert release.wait(3)
        return {"ok": True, "data": "committed"}
    runtime, session, _, transport, cfg = harness(tmp_path, responses=[reply(calls=[call("one"), call("two")])], execute=execute)
    run = runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)
    assert entered.wait(2)
    runtime.cancel(run["id"])
    release.set()
    deadline = time.monotonic() + 3
    while run["id"] in runtime._threads and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert executed == ["create_plan"]
    assert any(event["type"] == "tool_result" for event in runtime.events(run["id"]))
    runtime.shutdown()


@pytest.mark.parametrize("bad_call,expected", [
    (call(name="unknown"), "TOOL_UNAVAILABLE"),
    (call(title=1), "INVALID_ARGUMENTS"),
    (call(title="x", workspace_id="other"), "INVALID_ARGUMENTS"),
])
def test_reject_invalid_tool_or_schema_before_execution(tmp_path, bad_call, expected):
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=[reply(calls=[bad_call])])
    sent = runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)
    final = wait(runtime, sent["id"])
    assert final["status"] == "failed" and final["error"]["code"] == expected and not executed
    runtime.shutdown()


@pytest.mark.parametrize("field,value", [("workspace_id", "other"), ("file_path", "C:/secret.docx"), ("approved", True), ("api_key", KEY)])
def test_security_rejects_even_permissive_schema(tmp_path, field, value):
    tools = [tool()]
    tools[0]["input_schema"] = {"type": "object"}
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=[reply(calls=[call(**{field: value})])], tools=tools)
    final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)["id"])
    assert final["status"] == "failed" and not executed
    assert KEY not in canonical(runtime.get_session(session["id"]))
    runtime.shutdown()


def test_budget_stops_before_second_tool(tmp_path):
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=[reply(calls=[call("one"), call("two")])])
    final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True,
                                     budget={"tool_calls": 1})["id"])
    assert final["error"]["code"] == "BUDGET_EXHAUSTED" and len(executed) == 1
    assert final["usage"]["tool_calls"] == 1
    runtime.shutdown()


def test_budget_stops_before_next_model_and_rejects_higher_limits(tmp_path):
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=[reply(calls=[call()])])
    with pytest.raises(AgentError):
        runtime.send(session["id"], "规划", "bad", consent=True, budget={"model_calls": 41})
    final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True,
                                     budget={"model_calls": 1})["id"])
    assert final["error"]["code"] == "BUDGET_EXHAUSTED" and len(transport.requests) == 1
    runtime.shutdown()


def test_no_progress_commits_actual_results_and_error_envelopes_not_success(tmp_path):
    responses = [reply(calls=[call(str(index))]) for index in range(3)]
    def execute(*args, **kwargs):
        return {"ok": False, "error": {"code": "REVISION_CONFLICT", "message": "实际冲突"}}
    runtime, session, executed, transport, cfg = harness(tmp_path, responses=responses, execute=execute)
    final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)["id"])
    assert final["error"]["code"] == "NO_PROGRESS" and not final["needs_reconciliation"]
    messages = runtime.get_session(session["id"])["messages"]
    results = [message for message in messages if message["role"] == "tool"]
    assert len(results) == 3 and all(not message["ok"] for message in results)
    assert all(message["result"]["error"]["code"] == "REVISION_CONFLICT" for message in results)
    assert runtime.store.db.execute("SELECT COUNT(*) FROM actions WHERE status='settled'").fetchone()[0] == 3
    runtime.shutdown()


def test_secrets_and_hidden_reasoning_are_removed_and_truncation_explicit(tmp_path):
    def execute(*args, **kwargs):
        return {"ok": True, "api_key": KEY, "data": {"text": KEY + "x" * (MAX_RESULT_CHARS * 2),
                                                  "thinking": "private chain", "password": "super-password"}}
    runtime, session, _, transport, cfg = harness(tmp_path, execute=execute,
                                               responses=[reply(calls=[call()]), reply(text="完成 " + KEY)])
    run = wait(runtime, runtime.send(session["id"], "消息 " + KEY, "r", consent=True, allow_writes=True)["id"])
    snapshot = canonical(runtime.get_session(session["id"])) + canonical(runtime.events(run["id"]))
    assert KEY not in snapshot and "private chain" not in snapshot and "super-password" not in snapshot
    assert "truncated" in snapshot
    assert KEY not in canonical(transport.requests[1][2])
    runtime.shutdown()


def test_missing_usage_is_unknown_not_character_estimate(tmp_path):
    runtime, session, _, transport, cfg = harness(tmp_path, responses=[reply(text="无工具的普通答复", usage=False)])
    final = wait(runtime, runtime.send(session["id"], "你好", "r", consent=True)["id"])
    assert final["usage"]["input_tokens"] is None and final["usage"]["total_tokens"] is None
    assert final["usage"]["unknown_requests"] == 1
    runtime.shutdown()


def test_restart_does_not_execute_pending_approval(tmp_path):
    runtime, session, executed, transport, cfg = harness(tmp_path)
    run = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True)["id"])
    approval_id = run["pending_approval"]["approval_id"]
    runtime.shutdown()
    later = Transport(reply(text="已保存"))
    resumed = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: {"ok": True, "data": "saved"}, lambda: cfg, later)
    assert not later.requests
    assert resumed.get_run(run["id"])["status"] == "awaiting_approval"
    resumed.approve(run["id"], approval_id, True)
    final = wait(resumed, run["id"], {"completed", "failed"})
    assert final["status"] == "completed", final
    resumed.shutdown()


def test_restart_unknown_intent_refuses_resume_and_no_automatic_request(tmp_path):
    runtime, session, _, transport, cfg = harness(tmp_path)
    run = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True)["id"])
    state = runtime.store.get_run(run["id"])
    with runtime.store.transaction():
        state["status"] = "running"
        runtime.store.intent(state, state["_pending_calls"][0], "digest", True)
        runtime.store.save_run(state)
    # Emulate a dead process without calling shutdown/cancel.
    runtime.store.close()
    runtime._directory_lease.close()  # Unit checkpoint fixture; real death tested separately.
    fresh = Transport()
    resumed = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: pytest.fail("must not re-execute"), lambda: cfg, fresh)
    recovered = resumed.get_run(run["id"])
    assert recovered["status"] == "interrupted" and recovered["needs_reconciliation"]
    with pytest.raises(AgentError, match="人工核对"):
        resumed.resume(run["id"], consent=True)
    assert not fresh.requests
    resumed.shutdown()


def test_resume_safe_interrupted_checkpoint(tmp_path):
    runtime, session, _, transport, cfg = harness(tmp_path)
    run = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True)["id"])
    state = runtime.store.get_run(run["id"])
    with runtime.store.transaction():
        state["status"] = "queued"
        runtime.store.save_run(state)
    runtime.store.close()
    runtime._directory_lease.close()  # Unit checkpoint fixture; real death tested separately.
    fresh = Transport(reply(text="已保存"))
    resumed = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: {"ok": True}, lambda: cfg, fresh)
    assert resumed.get_run(run["id"])["status"] == "interrupted"
    resumed.resume(run["id"], consent=True)
    # Still needs its specific write approval, not blanket consent.
    pending = wait(resumed, run["id"])
    assert pending["status"] == "awaiting_approval"
    resumed.shutdown()


def test_tool_exception_is_unknown_not_automatic_retry(tmp_path):
    def execute(*args, **kwargs):
        raise RuntimeError("could fail after commit " + KEY)
    runtime, session, _, transport, cfg = harness(tmp_path, execute=execute, responses=[reply(calls=[call()])])
    final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)["id"])
    assert final["status"] == "interrupted" and final["needs_reconciliation"]
    assert KEY not in canonical(final) and len(transport.requests) == 1
    runtime.shutdown()


def test_native_protocol_unsupported_and_partial_json_fail(tmp_path):
    for index, raw in enumerate([
        {"error": {"message": "model does not support tools"}},
        {"choices": [{"finish_reason": "tool_calls", "message": {"content": "pretend execution"}}]},
        {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "create_plan", "arguments": '{"title":'}}]}}]},
    ]):
        runtime, session, executed, transport, cfg = harness(tmp_path / str(index), responses=[raw])
        final = wait(runtime, runtime.send(session["id"], "规划", "r", consent=True, allow_writes=True)["id"])
        assert final["status"] == "failed" and not executed
        runtime.shutdown()


def test_redirect_and_nonlocal_http_are_rejected():
    with pytest.raises(AgentError, match="重定向"):
        _NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid/")
    cfg = config()
    cfg.base_url = "http://attacker.invalid/v1"
    with pytest.raises(AgentError, match="回环"):
        ProviderAdapter(cfg, lambda *args: pytest.fail("must not request")).request("", [], [tool()])
