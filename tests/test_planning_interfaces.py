"""MCP planning tools: registration, thin forwarding, real workflow, safe errors."""
from __future__ import annotations

import http.client
import json
import socket
import urllib.request
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from docx import Document
from pydantic import ValidationError

from paperflow.engine import word_live_bridge
from paperflow.engine.planning.models import PlanningError, ProjectProfile
from paperflow.engine.planning.service import ResearchPlanner
from paperflow.server import mcp_server


TOOLS = {
    "prepare_research_plan": "prepare",
    "create_research_project": "create",
    "save_research_plan": "save",
    "list_research_projects": "list_projects",
    "get_research_project": "get",
    "update_research_task": "update_task",
    "record_research_evidence": "record_evidence",
    "export_research_plan": "export",
}
READ_ONLY = {"prepare_research_plan", "list_research_projects", "get_research_project"}


@pytest.fixture(autouse=True)
def isolated_research_home(tmp_path, monkeypatch):
    home = tmp_path / "research"
    monkeypatch.setenv("PAPERFLOW_RESEARCH_HOME", str(home))
    bridge = MagicMock(name="forbidden_live_bridge", spec=word_live_bridge.WordLiveBridge)
    monkeypatch.setattr(word_live_bridge, "live_bridge", bridge)
    monkeypatch.setattr(mcp_server, "live_bridge", bridge)
    guards = []
    for owner, attribute in (
        (word_live_bridge.WordLiveBridge, "connect"),
        (word_live_bridge.WordLiveBridge, "_connect_on_com_thread"),
        (word_live_bridge._ComThread, "run"),
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (urllib.request, "urlopen"),
        (urllib.request, "urlretrieve"),
        (urllib.request.OpenerDirector, "open"),
        (http.client.HTTPConnection, "connect"),
        (http.client.HTTPSConnection, "connect"),
    ):
        guard = MagicMock(side_effect=AssertionError("Planning tools must not use network or live Word"))
        monkeypatch.setattr(owner, attribute, guard)
        guards.append(guard)
    yield home
    assert bridge.mock_calls == []
    for guard in guards:
        guard.assert_not_called()


def invoke(name, **kwargs):
    raw = getattr(mcp_server, name)(**kwargs)
    assert isinstance(raw, str)
    return json.loads(raw)


def success(payload):
    assert payload["status"] == "success", payload
    assert payload["sources"] == []
    assert isinstance(payload["warnings"], list) and payload["warnings"]
    for field in ("backend_calls_llm", "network_access", "scientific_verification", "live_word_access"):
        assert payload["coverage"][field] is False
    return payload["data"]


def error(payload, code, secret=""):
    assert payload["status"] == "error"
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    assert payload["data"] is None
    assert "coverage" in payload and "warnings" in payload
    raw = json.dumps(payload, ensure_ascii=False)
    assert "traceback" not in raw.lower()
    if secret:
        assert secret not in raw
    return payload


def tool_arguments(name, tmp_path):
    return {
        "prepare_research_plan": {"text": "端侧研究材料", "file_path": "", "max_chars": 1000},
        "create_research_project": {"profile": {"title": "研究标题", "goal": "待验证目标"}},
        "save_research_plan": {"project_id": "rp_local", "plan": {"profile": {"title": "研究标题", "goal": "待验证目标"}},
                               "expected_revision": 7, "change_note": "显式修订说明"},
        "list_research_projects": {"limit": 3, "offset": 2},
        "get_research_project": {"project_id": "rp_local", "revision": 4},
        "update_research_task": {"project_id": "rp_local", "task_id": "T1", "updates": {"text": "显式修改任务"},
                                 "expected_revision": 7},
        "record_research_evidence": {"project_id": "rp_local", "evidence": {
            "id": "EV1", "kind": "note", "source_ref": "https://example.invalid/source",
            "summary": "仅保存来源", "verification": "unverified",
        }, "expected_revision": 7, "experiment_ids": ["E1", "E2"]},
        "export_research_plan": {"project_id": "rp_local", "output_path": str(tmp_path / "out.docx"),
                                 "revision": 4, "overwrite": True},
    }[name]


def test_all_eight_planning_tools_are_registered_with_explicit_annotations():
    registered = {tool.name: tool for tool in mcp_server.mcp_app._tool_manager.list_tools()}
    assert set(TOOLS) <= set(registered)
    for name in TOOLS:
        tool = registered[name]
        annotations = tool.annotations.model_dump(by_alias=True)
        assert annotations["readOnlyHint"] is (name in READ_ONLY)
        assert annotations["openWorldHint"] is False
        assert annotations["destructiveHint"] is (name == "export_research_plan")
        assert callable(getattr(mcp_server, name))
        assert getattr(mcp_server, name).__doc__
    for name in ("save_research_plan", "update_research_task", "record_research_evidence"):
        assert "expected_revision" in registered[name].parameters["required"]
    assert "output_path" in registered["export_research_plan"].parameters["required"]


def test_planning_factory_reads_research_home_each_time(monkeypatch, isolated_research_home, tmp_path):
    constructor = MagicMock(spec=ResearchPlanner)
    monkeypatch.setattr(mcp_server, "ResearchPlanner", constructor)
    mcp_server._get_planning_service()
    constructor.assert_called_once_with(data_dir=str(isolated_research_home))
    second_home = tmp_path / "other-research"
    monkeypatch.setenv("PAPERFLOW_RESEARCH_HOME", str(second_home))
    mcp_server._get_planning_service()
    assert constructor.call_args.kwargs == {"data_dir": str(second_home)}
    assert not isolated_research_home.exists() and not second_home.exists()


@pytest.mark.parametrize("name", list(TOOLS))
def test_each_tool_forwards_every_argument_without_mutation(name, tmp_path, monkeypatch, isolated_research_home):
    operation = TOOLS[name]
    service = MagicMock(spec=ResearchPlanner)
    expected = {
        "status": "success", "data": {"marker": operation}, "sources": [],
        "coverage": {"backend_calls_llm": False, "network_access": False,
                     "scientific_verification": False, "live_word_access": False},
        "warnings": ["提交方记录，不代表独立核实"], "suggested_options": [],
    }
    getattr(service, operation).return_value = expected
    factory = MagicMock(return_value=service)
    monkeypatch.setattr(mcp_server, "_get_planning_service", factory)
    kwargs = tool_arguments(name, tmp_path)
    original = deepcopy(kwargs)
    payload = invoke(name, **kwargs)
    assert payload == expected
    factory.assert_called_once_with()
    getattr(service, operation).assert_called_once_with(**original)
    assert kwargs == original
    assert not isolated_research_home.exists()


def test_eight_tools_form_an_offline_persistent_workflow(tmp_path, isolated_research_home):
    prepared = success(invoke("prepare_research_plan", text="尚未实施的研究想法 [@fake]"))
    assert prepared["stage"] == "needs_agent_plan"
    assert "plan_schema" in prepared
    assert not isolated_research_home.exists()
    created = success(invoke("create_research_project", profile={
        "title": "MCP 集成研究", "goal": "规划公平对照，不虚构科研结论",
        "resources": [{"name": "借用设备"}], "decisions": [{"text": "拟用候选实现"}],
    }))
    project_id = created["project_id"]
    plan = deepcopy(created["plan"])
    plan["questions"] = [{"id": "RQ1", "question": "候选实现是否减少延迟？", "hypothesis": "尚待验证"}]
    plan["experiments"] = [{
        "id": "E1", "question_id": "RQ1", "title": "公平对照", "comparisons": ["baseline", "candidate"],
        "metrics": ["device latency ms"], "protocol": "repeat on same device",
        "acceptance_condition": "复核后再判定",
    }]
    plan["tasks"] = [{"id": "T1", "text": "记录 pilot", "stage": "pilot",
                       "completion_condition": "保存日志", "experiment_id": "E1"}]
    saved = success(invoke("save_research_plan", project_id=project_id, plan=plan,
                           expected_revision=1, change_note="MCP 保存规划"))
    assert saved["revision"] == 2
    listed = success(invoke("list_research_projects", limit=1, offset=0))
    assert listed["total"] == 1 and listed["projects"][0]["project_id"] == project_id
    updated = success(invoke("update_research_task", project_id=project_id, task_id="T1",
                             updates={"status": "done", "completion_note": "仅完成日志保存"}, expected_revision=2))
    assert updated["revision"] == 3
    evidence = {"id": "EV1", "kind": "simulation", "source_ref": "https://example.invalid/no-fetch",
                "summary": "仿真记录并非实测", "verification": "unverified"}
    recorded = success(invoke("record_research_evidence", project_id=project_id, evidence=evidence,
                              expected_revision=3, experiment_ids=["E1"]))
    assert recorded["revision"] == 4
    current = success(invoke("get_research_project", project_id=project_id))
    assert current["plan"] == recorded["plan"]
    assert current["plan"]["evidence"][0]["source_ref"] == evidence["source_ref"]
    assert current["plan"]["experiments"][0]["status"] == "planned"
    assert current["plan"]["experiments"][0]["result_summary"] == ""
    assert current["plan"]["questions"][0]["status"] == "proposed"
    assert current["plan"]["profile"]["resources"][0]["status"] == "unverified"
    assert current["plan"]["profile"]["decisions"][0]["status"] == "proposed"
    assert current["overview"]["matrix"][0]["evidence"] == [
        {"id": "EV1", "kind": "simulation", "verification": "unverified"},
    ]
    historical = success(invoke("get_research_project", project_id=project_id, revision=2))
    assert historical["revision"] == 2 and historical["latest_revision"] == 4
    assert historical["plan"]["evidence"] == []
    assert historical["plan"]["tasks"][0]["status"] == "todo"
    output = tmp_path / "mcp-history.docx"
    exported = success(invoke("export_research_plan", project_id=project_id, output_path=str(output), revision=2))
    assert set(exported) == {"project_id", "revision", "output_path", "document_type"}
    assert exported["revision"] == 2 and exported["document_type"] == "research_plan"
    text = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)
    assert "版本：2" in text and "尚未提交证据" in text
    assert "EV1；来源" not in text
    assert success(invoke("get_research_project", project_id=project_id)) == current


@pytest.mark.parametrize("name", list(TOOLS))
def test_unexpected_backend_exception_does_not_leak_secret(name, tmp_path, monkeypatch):
    secret = "unexpectedException-secret-local-key-8822"
    service = MagicMock(spec=ResearchPlanner)
    getattr(service, TOOLS[name]).side_effect = RuntimeError(f"unexpectedException at private/path: {secret}")
    monkeypatch.setattr(mcp_server, "_get_planning_service", MagicMock(return_value=service))
    payload = error(invoke(name, **tool_arguments(name, tmp_path)), "INTERNAL_ERROR", secret)
    assert "unexpectedException" not in payload["message"]
    assert "private/path" not in payload["message"]


@pytest.mark.parametrize("name", list(TOOLS))
def test_raw_validation_error_input_is_redacted(name, tmp_path, monkeypatch):
    secret = "ValidationError-secret-raw-input-7711"
    with pytest.raises(ValidationError) as raised:
        ProjectProfile.model_validate({"title": "标题", "goal": {"raw_secret": secret}})
    assert secret in repr(raised.value.errors())
    service = MagicMock(spec=ResearchPlanner)
    getattr(service, TOOLS[name]).side_effect = raised.value
    monkeypatch.setattr(mcp_server, "_get_planning_service", MagicMock(return_value=service))
    payload = error(invoke(name, **tool_arguments(name, tmp_path)), "VALIDATION_ERROR", secret)
    prefix = "研究规划字段校验未通过: "
    assert payload["message"].startswith(prefix)
    details = json.loads(payload["message"][len(prefix):])
    assert details and all(set(item) == {"loc", "type"} for item in details)
    assert "input" not in details[0] and "msg" not in details[0] and "ctx" not in details[0]


@pytest.mark.parametrize("name", list(TOOLS))
def test_domain_error_code_survives_interface(name, tmp_path, monkeypatch):
    service = MagicMock(spec=ResearchPlanner)
    getattr(service, TOOLS[name]).side_effect = PlanningError("REVISION_CONFLICT", "读取最新版本后再提交")
    monkeypatch.setattr(mcp_server, "_get_planning_service", MagicMock(return_value=service))
    payload = error(invoke(name, **tool_arguments(name, tmp_path)), "REVISION_CONFLICT")
    assert payload["message"] == "读取最新版本后再提交"


def test_factory_failure_also_uses_safe_error_envelope(monkeypatch):
    secret = "constructor-secret-do-not-expose"
    monkeypatch.setattr(mcp_server, "_get_planning_service", MagicMock(side_effect=RuntimeError(secret)))
    error(invoke("list_research_projects"), "INTERNAL_ERROR", secret)


@pytest.mark.parametrize("bad_profile", [
    {"title": "缺少目标"}, {"title": " ", "goal": "目标"},
    {"title": "标题", "goal": "目标", "unknown": "raw-secret-in-extra-field"},
])
def test_real_validation_errors_do_not_initialize_database(isolated_research_home, bad_profile):
    error(invoke("create_research_project", profile=bad_profile), "VALIDATION_ERROR", "raw-secret-in-extra-field")
    assert not isolated_research_home.exists()


@pytest.mark.parametrize("name,kwargs,code", [
    ("prepare_research_plan", {}, "MUTUALLY_EXCLUSIVE_INPUT"),
    ("get_research_project", {"project_id": "../escape"}, "INVALID_ID"),
    ("get_research_project", {"project_id": "missing"}, "PROJECT_NOT_FOUND"),
    ("get_research_project", {"project_id": "missing", "revision": True}, "INVALID_REVISION"),
    ("list_research_projects", {"limit": True}, "INVALID_PAGINATION"),
    ("list_research_projects", {"offset": -1}, "INVALID_PAGINATION"),
])
def test_real_read_errors_remain_inert(name, kwargs, code, isolated_research_home):
    error(invoke(name, **kwargs), code)
    assert not isolated_research_home.exists()


def test_real_write_conflicts_validation_and_bad_export_do_not_mutate_store(tmp_path):
    created = success(invoke("create_research_project", profile={"title": "冲突检测", "goal": "显式规划"}))
    project_id = created["project_id"]
    saved = success(invoke("save_research_plan", project_id=project_id, plan=created["plan"], expected_revision=1))
    before = success(invoke("get_research_project", project_id=project_id))
    error(invoke("save_research_plan", project_id=project_id, plan=created["plan"], expected_revision=1), "REVISION_CONFLICT")
    error(invoke("save_research_plan", project_id=project_id, plan=created["plan"], expected_revision=True), "INVALID_REVISION")
    error(invoke("update_research_task", project_id=project_id, task_id="T1", updates={"id": "T2"}, expected_revision=2), "INVALID_UPDATE")
    invalid_evidence = {"id": "EV1", "kind": "note", "summary": "不能保存凭证",
                        "source_ref": "https://example.invalid/doc?token=secret-evidence-token"}
    error(invoke("record_research_evidence", project_id=project_id, evidence=invalid_evidence,
                 expected_revision=2), "VALIDATION_ERROR", "secret-evidence-token")
    invalid_plan = deepcopy(saved["plan"])
    invalid_plan["unexpected"] = "secret-invalid-plan-field"
    error(invoke("save_research_plan", project_id=project_id, plan=invalid_plan,
                 expected_revision=2), "VALIDATION_ERROR", "secret-invalid-plan-field")
    error(invoke("export_research_plan", project_id=project_id, output_path="relative.docx"), "INVALID_OUTPUT_PATH")
    assert success(invoke("get_research_project", project_id=project_id)) == before
    error(invoke("get_research_project", project_id=project_id, revision=3), "REVISION_NOT_FOUND")
    assert not (tmp_path / "relative.docx").exists()
