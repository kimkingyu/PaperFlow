"""Native HTTP integration with real services and deterministic model responses.

No real model API, personal research database or live Office document is touched.
"""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from starlette.testclient import TestClient

from paperflow.application.services import ApplicationServices
from paperflow.gui.secrets import SecretStore
from paperflow.gui.server import create_app

TOKEN = "integration-access-token"
SECRET = "sk-integration-not-a-real-api-key"
HEADERS = {"Host": "127.0.0.1:8765", "X-PaperFlow-Token": TOKEN}


def post(client, path, payload):
    return client.post(path, json=payload, headers=HEADERS)


def data(result):
    assert result.status_code == 200, result.text
    envelope = result.json()
    assert envelope.get("status") != "error", envelope
    return envelope.get("data", envelope)


def wait_run(client, run_id, statuses=("completed", "failed", "awaiting_approval")):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = data(client.get(f"/api/agent/runs/{run_id}", headers=HEADERS))
        if run["status"] in statuses:
            return run
        time.sleep(0.01)
    pytest.fail("native run did not reach a terminal/input state")


class PlanningModel:
    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        assert url == "https://model.example.test/v1/chat/completions"
        assert headers.get("Authorization") == f"Bearer {SECRET}"
        previous = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        if not previous:
            name, args = "planning_create", {"profile": {"title": "隔离测试科研规划", "goal": "验证计划保存与真实文档导出"}}
        elif len(previous) == 1:
            result = previous[-1]
            if "result" in result:
                result = result["result"]
            name, args = "planning_export", {"project_id": result["data"]["project_id"]}
        else:
            return {"choices": [{"message": {"content": "科研规划已经保存，并生成可下载的 DOCX。"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}}
        return {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": f"call_{self.calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
        }]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}}


@pytest.fixture
def native(tmp_path):
    services = ApplicationServices(data_dir=tmp_path / "isolated")
    store = SecretStore()
    store.configure("openai_compatible", "https://model.example.test/v1", "test-model", SECRET, False)
    store.consent(True)
    model = PlanningModel()
    app = create_app(TOKEN, 8765, finder=services.finder, store=store, application=services,
                     literature=services.literature, writing=services.writing, loop=services.loop,
                     agent_transport=model, standalone=True)
    with TestClient(app) as client:
        yield client, services, store, model


class EvidenceWritingModel:
    """Uses only IDs, hashes and quotes returned by actual preceding tools."""
    def __init__(self, asset_id):
        self.asset_id = asset_id
        self.calls = 0
        self.paper_id = None
        self.project = None

    def __call__(self, url, headers, body):
        from test_literature_service import card_for
        from test_literature_writing_core import assessment, draft_for, profile
        history = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        last = history[-1] if history else None
        if last and "result" in last:
            last = last["result"]
        if last:
            assert last.get("status") != "error", last
        step = self.calls
        self.calls += 1
        if step == 0:
            name, args = "papers_import", {"asset_id": self.asset_id, "title": "本地测试方法文献"}
        elif step == 1:
            self.paper_id = last["data"]["paper_id"]
            name, args = "papers_read", {"paper_id": self.paper_id}
        elif step == 2:
            name, args = "papers_notes", {"paper_id": self.paper_id, "reading": card_for(last)}
        elif step == 3:
            name, args = "writing_create", {"profile": profile(), "paper_ids": [self.paper_id]}
        elif step == 4:
            self.project = last["data"]
            name, args = "writing_assess", {"project_id": self.project["project_id"],
                "expected_revision": self.project["revision"], "assessments": [assessment(c) for c in self.project["candidates"]]}
        elif step == 5:
            self.project = last["data"]
            name, args = "writing_draft", {"project_id": self.project["project_id"],
                "expected_revision": self.project["revision"], "draft": draft_for(self.project)}
        elif step == 6:
            name, args = "writing_export", {"project_id": self.project["project_id"]}
        else:
            return {"choices": [{"message": {"content": "已保存带原文证据的文献草稿并导出；没有声称完成自有实验。"}, "finish_reason": "stop"}]}
        return {"choices": [{"message": {"content": None, "tool_calls": [{"id": f"evidence_{step}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}]}


def test_native_evidence_reading_to_real_manuscript_docx(tmp_path):
    from test_literature_service import pdf_bytes
    services = ApplicationServices(data_dir=tmp_path / "evidence-app")
    store = SecretStore()
    store.configure("openai_compatible", "https://model.example.test/v1", "test-model", SECRET, False)
    store.consent(True)
    model = EvidenceWritingModel("")
    app = create_app(TOKEN, 8765, finder=services.finder, store=store, application=services,
                     literature=services.literature, writing=services.writing, loop=services.loop,
                     agent_transport=model, standalone=True)
    with TestClient(app) as client:
        workspace = data(post(client, "/api/workbench/workspaces", {"title": "原文证据闭环"}))
        upload = client.post("/api/workbench/assets", params={"workspace_id": workspace["id"]},
                             content=pdf_bytes(["A real reproducible method.", "Observed baseline in the referenced study."]),
                             headers={**HEADERS, "X-File-Name": "fixture.pdf", "Content-Type": "application/pdf"})
        model.asset_id = data(upload)["asset_id"]
        session = data(post(client, "/api/agent/sessions", {"workspace_id": workspace["id"], "title": "阅读已上传文献并导出"}))
        submitted = data(post(client, f"/api/agent/sessions/{session['id']}/messages", {
            "message": f"阅读已上传文献 {model.asset_id}，保存有证据的文献草稿并导出，不编造自有实验。",
            "request_id": "evidence-workflow", "consent": True, "allow_writes": True,
        }))
        run = wait_run(client, submitted["id"])
        assert run["status"] == "completed", run
        assert run["usage"]["tool_calls"] == 7
        project = services.writing.get(model.project["project_id"])["data"]
        assert project["draft"]["sections"] and project["evidence_matrix"]
        assert project["evidence_matrix"][0]["evidence"][0]["page_number"] == 1
        assert project["profile"]["own_materials"] == []
        result = data(client.get("/api/workbench/artifacts", params={"workspace_id": workspace["id"]}, headers=HEADERS))
        items = result if isinstance(result, list) else result["items"]
        assert len(items) == 1
        artifact = items[0]
        download = client.get(artifact.get("download_url") or f"/api/workbench/artifacts/{artifact['artifact_id']}/download", headers=HEADERS)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            xml = archive.read("word/document.xml").decode()
        assert "reproducible method" in xml and "相关" not in project.get("error_code", "")
        assert SECRET not in xml


@pytest.mark.parametrize("action,params", [
    ("read_project", {"source_type": "local", "path": "C:/private/research"}),
    ("read_project", {"source_type": " LOCAL "}),
    ("recommend", {"project_path": "C:/private/research"}),
    ("prepare", {"project_dir": "C:/private/research"}),
    ("read_project", {"github_repo": "owner/repository"}),
    ("recommend", {"input_path": "C:/private/research.txt"}),
])
def test_legacy_http_cannot_bypass_managed_file_boundary(native, monkeypatch, action, params):
    client, *_ = native
    called = []
    monkeypatch.setattr("paperflow.gui.server.dispatch", lambda *a, **k: called.append(a))
    result = post(client, "/api/action", {"action": action, "params": params})
    assert result.status_code == 400
    assert result.json()["error_code"] == "INVALID_INPUT"
    assert called == []


def test_workspace_connection_always_closes_and_rolls_back(native):
    import sqlite3
    _, services, *_ = native
    with services.workspaces.connect() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
    workspace = services.workspaces.create("original title")
    with pytest.raises(RuntimeError, match="rollback fixture"):
        with services.workspaces.connect() as failing:
            failing.execute("UPDATE workspaces SET title=? WHERE id=?", ("uncommitted", workspace["id"]))
            raise RuntimeError("rollback fixture")
    with pytest.raises(sqlite3.ProgrammingError):
        failing.execute("SELECT 1")
    assert services.workspaces.get(workspace["id"])["title"] == "original title"


def test_native_agent_creates_real_plan_and_downloads_docx(native):
    client, services, store, model = native
    workspace = data(post(client, "/api/workbench/workspaces", {"title": "端到端隔离工作区", "description": "测试数据"}))
    session = data(post(client, "/api/agent/sessions", {"workspace_id": workspace["id"], "title": "创建并导出科研规划"}))
    submitted = data(post(client, f"/api/agent/sessions/{session['id']}/messages", {
        "message": "创建科研规划并导出文档", "request_id": "create-plan-once", "consent": True, "allow_writes": True,
    }))
    run = wait_run(client, submitted["id"])
    assert run["status"] == "completed", run
    assert run["usage"]["model_calls"] == 3
    assert run["usage"]["tool_calls"] == 2
    links = services.workspaces.get(workspace["id"])["links"]
    assert len([link for link in links if link["kind"] == "planning"]) == 1
    artifacts = data(client.get("/api/workbench/artifacts", params={"workspace_id": workspace["id"]}, headers=HEADERS))
    items = artifacts if isinstance(artifacts, list) else artifacts["items"]
    assert len(items) == 1
    artifact = items[0]
    download = client.get(artifact.get("download_url") or f"/api/workbench/artifacts/{artifact['artifact_id']}/download", headers=HEADERS)
    assert download.status_code == 200 and download.content.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        xml = archive.read("word/document.xml").decode()
    assert "隔离测试科研规划" in xml and "验证计划保存" in xml
    repeated = data(post(client, f"/api/agent/sessions/{session['id']}/messages", {
        "message": "创建科研规划并导出文档", "request_id": "create-plan-once", "consent": True, "allow_writes": True,
    }))
    assert repeated["id"] == run["id"] and model.calls == 3
    events = client.get(f"/api/agent/runs/{run['id']}/events?after=0", headers=HEADERS)
    assert events.status_code == 200 and "data:" in events.text
    assert SECRET not in events.text
    history = client.get(f"/api/agent/sessions/{session['id']}", headers=HEADERS)
    assert SECRET not in history.text
    assert client.get(artifact.get("download_url") or f"/api/workbench/artifacts/{artifact['artifact_id']}/download",
                      headers={"Host": HEADERS["Host"]}).status_code == 403


def test_native_http_requires_auth_and_explicit_boolean_consent(native):
    client, services, store, model = native
    assert client.get("/api/agent/sessions", headers={"Host": HEADERS["Host"]}).status_code == 403
    workspace = data(post(client, "/api/workbench/workspaces", {"title": "授权边界"}))
    session = data(post(client, "/api/agent/sessions", {"workspace_id": workspace["id"]}))
    response = post(client, f"/api/agent/sessions/{session['id']}/messages", {
        "message": "不要执行", "request_id": "invalid-consent", "consent": "false",
    })
    assert response.status_code == 400 and model.calls == 0
    assert post(client, "/api/model/consent", {"consent": "false"}).status_code == 400
    assert post(client, "/api/model/config", {"provider": "openai_compatible", "base_url": "https://model.example.test/v1",
                                            "model": "test", "remember": "false"}).status_code == 400
    assert client.get("/api/agent/sessions", headers={**HEADERS, "Origin": "https://attacker.example"}).status_code == 403


def test_native_write_approval_is_bound_and_not_implicit(native):
    client, services, store, model = native
    workspace = data(post(client, "/api/workbench/workspaces", {"title": "审批边界"}))
    session = data(post(client, "/api/agent/sessions", {"workspace_id": workspace["id"]}))
    submitted = data(post(client, f"/api/agent/sessions/{session['id']}/messages", {
        "message": "创建科研规划", "request_id": "approval-needed", "consent": True,
    }))
    run = wait_run(client, submitted["id"])
    assert run["status"] == "awaiting_approval"
    assert not services.workspaces.get(workspace["id"])["links"]
    approval = run["pending_approval"]["approval_id"]
    assert post(client, f"/api/agent/runs/{run['id']}/approve", {"approval_id": approval, "approved": "true"}).status_code == 400
    result = data(post(client, f"/api/agent/runs/{run['id']}/approve", {"approval_id": approval, "approved": False}))
    assert result["status"] in {"paused", "cancelled", "failed"}
    assert not services.workspaces.get(workspace["id"])["links"]


def test_launch_page_and_mcp_single_file_are_separate(native):
    client, *_ = native
    from paperflow.gui.server import load_page
    legacy = load_page()
    assert "ui/initialize" in legacy and "PaperFlow" in legacy
    response = client.get("/", headers={"Host": HEADERS["Host"]})
    assert response.status_code in {200, 503}
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/app/.env", headers={"Host": HEADERS["Host"]}).status_code == 404
    assert client.get("/app/assets/secret.json", headers={"Host": HEADERS["Host"]}).status_code == 404
