"""Native agent uses the real core business services, never an MCP or GUI loop."""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from paperflow.agent import AgentRuntime
from paperflow.agent.security import canonical
from paperflow.application.services import ApplicationServices
from test_native_agent import call, config, reply, wait
from test_literature_service import card_for, pdf_bytes
from test_literature_writing_core import assessment, draft_for


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No real external API or GUI model worker")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)


def test_selected_local_pdf_reading_draft_and_real_docx_export(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "business")
    workspace = services.workspaces.create("明确选中的本地文献")
    selected = services.assets.upload(workspace["id"], "selected.pdf", "application/pdf",
                                     pdf_bytes(["A reproducible method uses matched device settings.", "The reported baseline is scoped to this study."]))
    history = []
    state = {}
    def model(url, headers, body):
        index = len(history)
        history.append(body)
        if index == 0:
            operation = call("import", "papers_import", asset_id=selected["asset_id"], title="Selected reproducible method")
        else:
            envelope = json.loads(body["messages"][-1]["content"])
            assert envelope.get("status") in {"success", "partial"}, envelope
            data = envelope["data"]
            if index == 1:
                state["paper_id"] = data["paper_id"]
                operation = call("read", "papers_read", paper_id=data["paper_id"], page_number=1, page_count=2)
            elif index == 2:
                reading = card_for(envelope)
                operation = call("notes", "papers_notes", paper_id=state["paper_id"], reading=reading)
            elif index == 3:
                operation = call("create", "writing_create", profile={"title": "Reproducible methods", "research_question": "How are device settings matched?"},
                                 paper_ids=[state["paper_id"]])
            elif index == 4:
                state["project_id"] = data["project_id"]
                operation = call("assess", "writing_assess", project_id=data["project_id"], expected_revision=data["revision"],
                                 assessments=[assessment(candidate) for candidate in data["candidates"]])
            elif index == 5:
                operation = call("materials", "writing_materials", project_id=state["project_id"])
            elif index == 6:
                operation = call("draft", "writing_draft", project_id=state["project_id"], expected_revision=data["revision"], draft=draft_for(data))
            elif index == 7:
                state["draft_revision"] = data["revision"]
                operation = call("export", "writing_export", project_id=state["project_id"], revision=data["revision"])
            else:
                state["artifact"] = data
                return reply(text="已按本地来源保存解读、草稿与DOCX。阅读仅覆盖两页；没有执行实验。")
        return reply(calls=[operation])
    runtime = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, model)
    session = runtime.create_session(workspace["id"])
    started = runtime.send(session["id"], "仅使用已选资产 " + selected["asset_id"] + "，保存证据支持草稿并导出。不得添加实验结果。", "workflow",
                           consent=True, allow_writes=True)
    final = wait(runtime, started["id"], {"completed", "failed", "interrupted", "awaiting_approval"})
    assert final["status"] == "completed", final
    assert final["usage"]["tool_calls"] == 8 and final["usage"]["model_calls"] == 9
    saved = services.writing.get(state["project_id"])["data"]
    assert saved["revision"] == state["draft_revision"]
    assert saved["evidence_matrix"] and saved["draft"]["sections"]
    card = services.literature.get(state["paper_id"])["data"]["reading_cards"][0]
    assert card["origin"] == "nativeAgent"
    artifacts = services.assets.list(workspace["id"], "artifact")
    assert len(artifacts) == 1
    file = services.assets.get(artifacts[0]["artifact_id"], workspace["id"], kind="artifact")
    target = services.assets.path(file["artifact_id"], file["filename"])
    assert target.read_bytes().startswith(b"PK")
    from docx import Document
    document = Document(target)
    exported = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Reproducible" in exported and "OWN_EXPERIMENTS_NOT_PROVIDED" in exported
    assert str(tmp_path) not in canonical(runtime.get_session(session["id"]))
    runtime.shutdown()


def test_real_registry_rejects_unlinked_project_and_cas_conflict(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "business")
    workspace = services.workspaces.create("当前工作区")
    foreign = services.planning.create({"title": "未关联对象", "goal": "不得跨项目"})["data"]
    count = 0
    def model(url, headers, body):
        nonlocal count
        count += 1
        if count == 1:
            return reply(calls=[call("cross", "planning_get", project_id=foreign["project_id"])])
        result = json.loads(body["messages"][-1]["content"])
        assert result["ok"] is False and "SCOPE_DENIED" in canonical(result)
        return reply(text="对象不属于当前工作区，未读取。")
    runtime = AgentRuntime(tmp_path / "agent", services.describe_tools, services.execute, config, model)
    session = runtime.create_session(workspace["id"])
    final = wait(runtime, runtime.send(session["id"], "检查给定ID是否允许读取", "cross", consent=True)["id"])
    assert final["status"] == "completed" and not final["needs_reconciliation"], final
    assert "未关联对象" not in canonical(runtime.get_session(session["id"]))
    runtime.shutdown()


def test_bound_business_model_hooks_mirror_requests_and_settle_failures(tmp_path):
    class Services:
        def __init__(self):
            self.calls = []
        def catalog(self):
            return []
        def before(self, **kwargs):
            self.calls.append(("before", kwargs))
            return {"ok": True}
        agent_before_model = before
        def after(self, **kwargs):
            self.calls.append(("after", kwargs))
        agent_after_model = after
        def release(self, **kwargs):
            self.calls.append(("release", kwargs))
        agent_release = release
        def execute(self, *args, **kwargs):
            pytest.fail("not a tool request")
    for malformed in (False, True):
        services = Services()
        def model(*args):
            if malformed:
                return {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
            return reply(text="材料不足，保留未验证问题")
        runtime = AgentRuntime(tmp_path / str(malformed), services.catalog, services.execute, config, model)
        session = runtime.create_session("workspace")
        final = wait(runtime, runtime.send(session["id"], "分析所选材料", "r", consent=True)["id"])
        deadline = __import__('time').monotonic() + 2
        while final["id"] in runtime._threads and __import__('time').monotonic() < deadline:
            threading.Event().wait(0.005)
        assert [name for name, _ in services.calls] == ["before", "after", "release"]
        assert services.calls[1][1]["success"] is (not malformed)
        assert final["usage"]["total_tokens"] == (7 if malformed else 13)
        runtime.shutdown()
