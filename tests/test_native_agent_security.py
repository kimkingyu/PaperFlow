"""Real OS-lock ownership, protocol continuation and scoped material boundaries."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from paperflow.agent import AgentError, AgentRuntime
from paperflow.agent.providers import ProviderAdapter, http_transport, MAX_RESPONSE_BYTES
from paperflow.agent.security import canonical
from paperflow.application.services import ApplicationServices
from paperflow.engine.planning.service import ResearchPlanner
from test_native_agent import Transport, call, config, reply, tool, wait


def test_two_live_runtimes_do_not_interrupt_or_duplicate_owner(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def transport(*args):
        entered.set()
        assert release.wait(3)
        return reply(text="done")
    first = AgentRuntime(tmp_path, lambda: [], lambda *a, **kw: pytest.fail("no tool"), config, transport)
    session = first.create_session("workspace")
    started = first.send(session["id"], "hello", "r", consent=True)
    assert entered.wait(2)
    second = AgentRuntime(tmp_path, lambda: [], lambda *a, **kw: pytest.fail("second must be read-only"), config,
                          lambda *args: pytest.fail("second cannot model"))
    assert second.read_only
    assert second.get_run(started["id"])["status"] == "running"
    assert second.get_session(session["id"])["messages"]
    for operation in (lambda: second.resume(started["id"], consent=True), lambda: second.cancel(started["id"]),
                      lambda: second.send(session["id"], "hello", "r2", consent=True)):
        with pytest.raises(AgentError, match="只读"):
            operation()
    second.shutdown()
    assert first.get_run(started["id"])["status"] == "running"
    release.set()
    assert wait(first, started["id"])["status"] == "completed"
    first.shutdown()
    third = AgentRuntime(tmp_path, lambda: [], lambda *a, **kw: None, config, Transport())
    assert not third.read_only and third.get_run(started["id"])["status"] == "completed"
    third.shutdown()


def test_actual_process_death_releases_os_lock_and_unknown_intent_is_not_replayed(tmp_path):
    source = r'''
import json, os, sys, threading
from types import SimpleNamespace
from paperflow.agent import AgentRuntime
from paperflow.engine.planning.service import ResearchPlanner
root = sys.argv[1]
planner = ResearchPlanner(data_dir=root + '/planning')
entered = threading.Event()
cfg = SimpleNamespace(provider='openai_compatible', base_url='http://127.0.0.1:1/v1',model='fake',consented=True,api_key=lambda:'')
item = {'name':'save','description':'test','input_schema':{'type':'object'},'available':True,'mutates':True}
def transport(*args):
    return {'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'one','type':'function','function':{'name':'save','arguments':'{}'}}]}}]}
def execute(*args, **kwargs):
    planner.create({'title':'Actual child side effect','goal':'Unknown completion must not replay'})
    entered.set()
    threading.Event().wait(30)
runtime=AgentRuntime(root+'/agent', lambda:[item],execute,lambda:cfg,transport)
session=runtime.create_session('workspace')
run=runtime.send(session['id'],'save','r',consent=True,allow_writes=True)
assert entered.wait(10)
print(json.dumps({'run_id':run['id'],'session_id':session['id']}),flush=True)
sys.stdin.readline()
os._exit(17)
'''
    child = subprocess.Popen([sys.executable, "-c", source, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        info = json.loads(child.stdout.readline())
        observer = AgentRuntime(tmp_path / "agent", lambda: [], lambda *a, **kw: pytest.fail("observer write"), config, Transport())
        assert observer.read_only and observer.get_run(info["run_id"])["status"] == "running"
        observer.shutdown()
        child.stdin.write("exit\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 17
        fresh_transport = Transport()
        matching_config = config()
        matching_config.base_url = "http://127.0.0.1:1/v1"
        matching_config.model = "fake"
        matching_config.api_key = lambda: ""
        recovered = AgentRuntime(tmp_path / "agent", lambda: [], lambda *a, **kw: pytest.fail("must not replay unknown intent"), lambda: matching_config, fresh_transport)
        run = recovered.get_run(info["run_id"])
        assert not recovered.read_only and run["status"] == "interrupted" and run["needs_reconciliation"]
        with pytest.raises(AgentError, match="人工核对"):
            recovered.resume(info["run_id"], consent=True)
        assert not fresh_transport.requests
        assert ResearchPlanner(data_dir=str(tmp_path / "planning")).list_projects()["data"]["total"] == 1
        recovered.shutdown()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("provider", ["openai_responses", "anthropic"])
def test_reasoning_protocol_continuation_echoes_only_in_memory(tmp_path, provider):
    first = reply(provider, calls=[call()])
    if provider == "anthropic":
        first["content"].insert(1, {"type": "redacted_thinking", "data": "opaque-redacted-payload"})
    transport = Transport(first, reply(provider, text="done"))
    executed = []
    runtime = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: executed.append(args) or {"ok": True}, lambda: config(provider), transport)
    session = runtime.create_session("workspace")
    started = runtime.send(session["id"], "save", "r", consent=True)
    waiting = wait(runtime, started["id"])
    assert waiting["status"] == "awaiting_approval"
    runtime.approve(started["id"], waiting["pending_approval"]["approval_id"], True)
    assert wait(runtime, started["id"], {"completed", "failed"})["status"] == "completed"
    second = transport.requests[1][2]
    if provider == "openai_responses":
        assert "reasoning.encrypted_content" in second["include"]
        reasoning = [item for item in second["input"] if item.get("type") == "reasoning"][0]
        assert reasoning["encrypted_content"] == "encrypted-opaque-test" and reasoning["summary"] == []
        assert "hidden chain must never be stored" not in canonical(second)
    else:
        assistant = [item for item in second["messages"] if item["role"] == "assistant"][0]
        assert assistant["content"] == first["content"]
    snapshot = canonical(runtime.get_session(session["id"])) + canonical(runtime.events(started["id"]))
    assert "hidden chain must never be stored" not in snapshot
    assert "signed-opaque-test" not in snapshot and "opaque-redacted-payload" not in snapshot and "encrypted-opaque-test" not in snapshot
    runtime.shutdown()
    assert b"hidden chain must never be stored" not in (tmp_path / "native_agent.sqlite3").read_bytes()


@pytest.mark.parametrize("provider", ["openai_responses", "anthropic"])
def test_restart_missing_continuation_rejects_approval_before_tool(tmp_path, provider):
    runtime = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: pytest.fail("unapproved"), lambda: config(provider),
                           Transport(reply(provider, calls=[call()])))
    session = runtime.create_session("workspace")
    waiting = wait(runtime, runtime.send(session["id"], "save", "r", consent=True)["id"])
    runtime.shutdown()
    fresh = Transport()
    resumed = AgentRuntime(tmp_path, lambda: [tool()], lambda *args, **kwargs: pytest.fail("must not tool after lost protocol state"), lambda: config(provider), fresh)
    run = resumed.get_run(waiting["id"])
    assert run["status"] == "interrupted" and run["error"]["code"] == "PROTOCOL_CONTINUATION_LOST"
    with pytest.raises(AgentError, match="续接"):
        resumed.resume(run["id"], consent=True)
    with pytest.raises(AgentError, match="失效"):
        resumed.approve(run["id"], waiting["pending_approval"]["approval_id"], True)
    assert not fresh.requests
    resumed.cancel(run["id"])
    resumed.shutdown()


def test_local_empty_key_and_public_empty_key_are_distinct(tmp_path):
    cfg = config()
    cfg.base_url = "http://127.0.0.1:1234/v1"
    cfg.api_key = lambda: ""
    transport = Transport(reply(text="local response"))
    runtime = AgentRuntime(tmp_path, lambda: [], lambda *a, **kw: None, lambda: cfg, transport)
    session = runtime.create_session("workspace")
    final = wait(runtime, runtime.send(session["id"], "hello", "r", consent=True)["id"])
    assert final["status"] == "completed"
    assert "Authorization" not in transport.requests[0][1]
    cfg.base_url = "https://model.invalid/v1"
    with pytest.raises(AgentError, match="凭证"):
        runtime.send(session["id"], "hello", "public", consent=True)
    runtime.shutdown()


def test_scope_lease_blocks_new_materials_during_inflight_model(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "business")
    workspace = services.workspaces.create("当前已关联材料范围")
    foreign = services.planning.create({"title": "Not selected", "goal": "must not be added during model request"})["data"]
    entered, release = threading.Event(), threading.Event()
    def model(*args):
        entered.set()
        assert release.wait(3)
        return reply(text="completed")
    runtime = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, model)
    session = runtime.create_session(workspace["id"])
    run = runtime.send(session["id"], "只分析启动时当前范围", "scope", consent=True)
    assert entered.wait(3)
    from paperflow.engine.journals.models import JournalError
    # Explicit external context is on another thread; it cannot impersonate the native owner.
    errors = []
    def external():
        for action in (lambda: services.workspaces.link(workspace["id"], "planning", foreign["project_id"]),
                       lambda: services.assets.upload(workspace["id"], "new.txt", "text/plain", b"new secret material")):
            try:
                action()
            except JournalError as exc:
                errors.append(exc.code)
    thread = threading.Thread(target=external)
    thread.start()
    thread.join(3)
    assert errors == ["EXECUTION_BUSY", "EXECUTION_BUSY"]
    release.set()
    assert wait(runtime, run["id"])["status"] == "completed"
    runtime.shutdown()


def test_external_schema_references_cannot_fetch_network(tmp_path):
    tools = [tool()]
    tools[0]["input_schema"] = {"$ref": "https://attacker.invalid/schema"}
    transport = Transport()
    runtime = AgentRuntime(tmp_path, lambda: tools, lambda *a, **kw: pytest.fail("never execute"), config, transport)
    session = runtime.create_session("workspace")
    final = wait(runtime, runtime.send(session["id"], "test", "schema", consent=True)["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "INVALID_CATALOG" and not transport.requests
    runtime.shutdown()


def test_default_http_transport_timeout_and_response_bound(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, amount):
            captured["read_limit"] = amount
            return b" " * amount
    class Opener:
        def open(self, request, timeout):
            captured["timeout"] = timeout
            return Response()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(AgentError, match="大小上限"):
        http_transport("https://model.invalid", {}, {})
    assert captured == {"timeout": 45, "read_limit": MAX_RESPONSE_BYTES + 1}


def test_denied_idle_approval_releases_workspace_lease(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "business")
    workspace = services.workspaces.create("待审批")
    transport = Transport(reply(calls=[call("create", "planning_create", profile={"title": "规划", "goal": "不要自动写入"})]))
    runtime = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, transport)
    session = runtime.create_session(workspace["id"])
    waiting = wait(runtime, runtime.send(session["id"], "规划课题", "r", consent=True)["id"])
    deadline = time.monotonic() + 2
    while waiting["id"] in runtime._threads and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    rejected = runtime.approve(waiting["id"], waiting["pending_approval"]["approval_id"], False)
    assert rejected["status"] == "cancelled" and rejected["usage"]["tool_calls"] == 0
    # A manually chosen new material is now allowed for a subsequent, separately consented run.
    asset = services.assets.upload(workspace["id"], "later.txt", "text/plain", b"later selection")
    assert asset["asset_id"]
    runtime.shutdown()


def test_elapsed_budget_stops_before_first_tool(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def model(*args):
        entered.set()
        assert release.wait(3)
        return reply(calls=[call()])
    runtime = AgentRuntime(tmp_path, lambda: [tool()], lambda *a, **kw: pytest.fail("budget must stop before write"), config, model)
    session = runtime.create_session("workspace")
    started = runtime.send(session["id"], "save", "r", consent=True, allow_writes=True, budget={"seconds": 1})
    assert entered.wait(2)
    with runtime.store.transaction():
        state = runtime.store.get_run(started["id"])
        state["created_at"] -= 2
        runtime.store.save_run(state)
    release.set()
    final = wait(runtime, started["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "BUDGET_EXHAUSTED"
    assert final["usage"]["model_calls"] == 1 and final["usage"]["tool_calls"] == 0
    runtime.shutdown()


@pytest.mark.parametrize("provider,stop", [
    ("openai_compatible", "content_filter"), ("openai_compatible", "unknown"), ("openai_compatible", None),
    ("openai_responses", "queued"), ("openai_responses", "in_progress"), ("openai_responses", "incomplete"),
    ("openai_responses", "unknown"), ("openai_responses", None),
    ("anthropic", "pause_turn"), ("anthropic", "refusal"), ("anthropic", "stop_sequence"), ("anthropic", "unknown"),
])
def test_nonfinal_refused_or_unknown_stop_reasons_never_execute(tmp_path, provider, stop):
    raw = reply(provider, calls=[call()])
    if provider == "openai_compatible":
        raw["choices"][0]["finish_reason"] = stop
    elif provider == "openai_responses":
        raw["status"] = stop
    else:
        raw["stop_reason"] = stop
    runtime = AgentRuntime(tmp_path, lambda: [tool()], lambda *a, **kw: pytest.fail("nonfinal response cannot authorize execution"),
                           lambda: config(provider), Transport(raw))
    session = runtime.create_session("workspace")
    final = wait(runtime, runtime.send(session["id"], "save", "r", consent=True, allow_writes=True)["id"])
    assert final["status"] == "failed" and final["usage"]["tool_calls"] == 0
    assert final["usage"]["total_tokens"] == 13
    runtime.shutdown()


def test_only_exclusive_recovery_owner_cleans_dead_business_leases(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "business")
    workspace = services.workspaces.create("待核对工作区")
    runtime = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config,
                           Transport(reply(calls=[call("create", "planning_create", profile={"title": "规划", "goal": "等待确认"})])))
    session = runtime.create_session(workspace["id"])
    waiting = wait(runtime, runtime.send(session["id"], "save", "r", consent=True)["id"])
    deadline = time.monotonic() + 2
    while waiting["id"] in runtime._threads and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    with runtime.store.transaction():
        state = runtime.store.get_run(waiting["id"])
        state["status"] = "running"
        runtime.store.save_run(state)
    second = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, Transport())
    assert second.read_only
    with services.workspaces.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_leases WHERE owner=?", ("agent:" + waiting["id"],)).fetchone()[0] >= 1
    second.shutdown()
    runtime.store.close()
    runtime._directory_lease.close()  # Kernel/process-death ownership is tested above.
    recovered = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, Transport())
    assert recovered.get_run(waiting["id"])["status"] == "interrupted"
    with services.workspaces.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_leases WHERE owner=?", ("agent:" + waiting["id"],)).fetchone()[0] == 0
    assert services.assets.upload(workspace["id"], "manual.txt", "text/plain", b"manual reconciliation material")["asset_id"]
    recovered.cancel(waiting["id"])
    recovered.shutdown()
