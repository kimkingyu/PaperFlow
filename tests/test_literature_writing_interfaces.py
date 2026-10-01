"""Isolated literature-writing MCP/CLI contracts; every paper/evidence here is a fixture."""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ConfigDict, Field

from paperflow.cli import paper_cli
from paperflow.engine.journals.models import JournalError
from paperflow.server import mcp_server


PROJECT_ID = "writing-fixture-project"
PAPER_ID = "paper-" + "a" * 24
SHA = "b" * 64
CITATION_ID = "cite-fixture-only"
PROFILE = {"title": "离线接口测试主题", "research_question": "接口是否保留真实来源边界？",
           "language": "zh", "sub_questions": [{"id": "RQ1", "question": "证据能否分页？"}],
           "own_materials": []}
QUERIES = [{"id": "Q1", "query": "offline evidence fixture", "purpose": "核对研究问题的背景来源",
            "question_ids": ["RQ1"], "sources": ["arxiv", "crossref"], "year_from": 2020, "year_to": 2025}]
ASSESSMENTS = [{"paper_id": PAPER_ID, "metadata_sha256": SHA, "relevance": "background",
                "reason": "接口fixture只用于背景测试，未读全文", "question_ids": ["RQ1"],
                "basis": "metadata", "limitations": ["这是fixture"], "evidence_ids": []}]
DRAFT = {"title": "文献支持草稿fixture", "language": "zh",
         "outline": [{"id": "intro", "title": "引言", "level": 1, "purpose": "接口测试", "evidence_ids": [CITATION_ID]}],
         "sections": [{"id": "intro", "title": "引言", "level": 1,
                       "paragraphs": [{"text": "仅用于测试转发，不是研究事实。", "kind": "literature_summary",
                                       "citation_ids": [CITATION_ID], "own_material_ids": []}]}]}
METHODS = {"prepare", "create", "search", "assess", "get", "list", "prepare_writing", "save_draft", "export_docx"}
TOOLS = {
    "prepare_paper_writing_research": "prepare", "create_paper_writing_project": "create",
    "search_related_papers": "search", "assess_related_papers": "assess",
    "get_paper_writing_project": "get", "list_paper_writing_projects": "list",
    "prepare_paper_manuscript": "prepare_writing", "save_paper_manuscript": "save_draft",
    "export_paper_manuscript": "export_docx",
}
READ_ONLY = {"prepare_paper_writing_research", "get_paper_writing_project",
             "list_paper_writing_projects", "prepare_paper_manuscript"}
ENVELOPE_KEYS = {"status", "error_code", "message", "data", "sources", "coverage", "warnings", "suggested_options"}


def envelope():
    return {"status": "partial", "error_code": None, "message": "只保留fixture覆盖状态",
            "data": {"project_id": PROJECT_ID, "revision": 7, "profile": copy.deepcopy(PROFILE),
                     "queries": copy.deepcopy(QUERIES), "selected_paper_ids": [PAPER_ID],
                     "candidates": [], "assessments": copy.deepcopy(ASSESSMENTS), "stage": "needs_reading",
                     "evidence_matrix": [{"citation_id": CITATION_ID, "paper_id": PAPER_ID,
                                          "reading_id": "reading-fixture", "claim_index": 0, "file_sha256": SHA,
                                          "section": "method", "kind": "author_claim", "text": "fixture claim",
                                          "evidence": [{"fragment_id": "frag-fixture", "page_number": 2, "quote": "fixture"}]}],
                     "evidence_total": 9, "evidence_offset": 3, "evidence_limit": 4, "next_evidence_offset": 7,
                     "search_statuses": [{"query_id": "Q1", "source_id": "crossref", "status": "error"}],
                     "gaps": [{"code": "PARTIAL_READING", "message": "仅核对fixture", "paper_id": PAPER_ID}],
                     "draft": copy.deepcopy(DRAFT), "agent_contract": {"backend_calls_llm": False}},
            "sources": [{"source_id": "fixture"}],
            "coverage": {"backend_calls_llm": False, "evidence_check_only": True,
                         "semantic_correctness_verified": False},
            "warnings": ["fixture不是正文理解认证"], "suggested_options": ["继续核查实际来源"]}


def arguments(name, tmp_path):
    return {
        "prepare_paper_writing_research": {"text": "用户明确提供的fixture主题"},
        "create_paper_writing_project": {"profile": copy.deepcopy(PROFILE), "queries": copy.deepcopy(QUERIES),
                                         "paper_ids": [PAPER_ID], "source_text": "fixture材料", "input_id": "input-fixture"},
        "search_related_papers": {"project_id": PROJECT_ID, "expected_revision": 7,
                                  "queries": copy.deepcopy(QUERIES), "per_query_limit": 3},
        "assess_related_papers": {"project_id": PROJECT_ID, "assessments": copy.deepcopy(ASSESSMENTS),
                                  "expected_revision": 7, "selected_paper_ids": [PAPER_ID]},
        "get_paper_writing_project": {"project_id": PROJECT_ID, "revision": 4, "evidence_offset": 3, "evidence_limit": 4},
        "list_paper_writing_projects": {"limit": 4, "offset": 3},
        "prepare_paper_manuscript": {"project_id": PROJECT_ID, "evidence_offset": 3, "evidence_limit": 4},
        "save_paper_manuscript": {"project_id": PROJECT_ID, "draft": copy.deepcopy(DRAFT),
                                  "expected_revision": 7, "change_note": "fixture章节更新"},
        "export_paper_manuscript": {"project_id": PROJECT_ID, "output_path": str(tmp_path / "fixture.docx"),
                                    "revision": 4, "overwrite": True},
    }[name]


@pytest.fixture(autouse=True)
def forbid_network_and_old_services(monkeypatch):
    original_connect = socket.socket.connect

    def blocked(*args, **kwargs):
        raise AssertionError("writing interface test must not access network/models/Word")

    def local_socketpair_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return blocked()

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_socketpair_only)
    monkeypatch.setattr(mcp_server, "_get_literature_service", Mock(side_effect=blocked))
    monkeypatch.setattr(mcp_server, "_get_journal_service", Mock(side_effect=blocked))
    monkeypatch.setattr(mcp_server, "_get_planning_service", Mock(side_effect=blocked))
    monkeypatch.setattr(paper_cli, "_get_literature_service", Mock(side_effect=blocked))
    bridge = Mock()
    monkeypatch.setattr(mcp_server, "live_bridge", bridge)
    yield
    assert bridge.mock_calls == []


@pytest.fixture
def fake_service(monkeypatch):
    service = Mock(spec=sorted(METHODS))
    for method in METHODS:
        getattr(service, method).return_value = envelope()
    monkeypatch.setattr(mcp_server, "_get_paper_writing_service", Mock(return_value=service))
    monkeypatch.setattr(paper_cli, "_get_paper_writing_service", Mock(return_value=service))
    return service


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_native_tools_forward_without_mutating_and_preserve_full_safe_envelope(name, tmp_path, fake_service):
    kwargs = arguments(name, tmp_path)
    original = copy.deepcopy(kwargs)
    result = json.loads(getattr(mcp_server, name)(**kwargs))
    expected = {**original, "origin": "calling_agent"} if name == "assess_related_papers" else original
    getattr(fake_service, TOOLS[name]).assert_called_once_with(**expected)
    assert kwargs == original and result == envelope()
    assert set(result) == ENVELOPE_KEYS
    assert result["data"]["next_evidence_offset"] == 7
    mcp_server._get_paper_writing_service.assert_called_once_with()


def test_native_default_arguments_have_no_implicit_search_or_selection(fake_service):
    mcp_server.create_paper_writing_project(PROFILE)
    fake_service.create.assert_called_once_with(profile=PROFILE, queries=None, paper_ids=None, source_text="", input_id="")
    mcp_server.search_related_papers(PROJECT_ID, 1)
    fake_service.search.assert_called_once_with(project_id=PROJECT_ID, expected_revision=1, queries=None, per_query_limit=10)
    mcp_server.assess_related_papers(PROJECT_ID, ASSESSMENTS, 1)
    fake_service.assess.assert_called_once_with(project_id=PROJECT_ID, assessments=ASSESSMENTS,
                                               expected_revision=1, selected_paper_ids=None, origin="calling_agent")
    mcp_server.get_paper_writing_project(PROJECT_ID)
    fake_service.get.assert_called_once_with(project_id=PROJECT_ID, revision=None, evidence_offset=0, evidence_limit=40)
    mcp_server.list_paper_writing_projects()
    fake_service.list.assert_called_once_with(limit=20, offset=0)
    mcp_server.prepare_paper_manuscript(PROJECT_ID)
    fake_service.prepare_writing.assert_called_once_with(project_id=PROJECT_ID, evidence_offset=0, evidence_limit=30)
    mcp_server.save_paper_manuscript(PROJECT_ID, DRAFT, 1)
    fake_service.save_draft.assert_called_once_with(project_id=PROJECT_ID, draft=DRAFT,
                                                   expected_revision=1, change_note="更新文献支持草稿")
    mcp_server.export_paper_manuscript(PROJECT_ID, "fixture.docx")
    fake_service.export_docx.assert_called_once_with(project_id=PROJECT_ID, output_path="fixture.docx", revision=None, overwrite=False)


def test_nine_native_tools_registered_with_exact_safety_annotations_and_required_revisions():
    listed = asyncio.run(mcp_server.mcp_app.list_tools())
    tools = listed if isinstance(listed, list) else listed.tools
    by_name = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in tools}
    assert set(TOOLS) <= by_name.keys()
    for name in TOOLS:
        item = by_name[name]
        annotations = item["annotations"]
        assert annotations["readOnlyHint"] is (name in READ_ONLY)
        assert annotations["openWorldHint"] is (name == "search_related_papers")
        assert annotations["destructiveHint"] is (name == "export_paper_manuscript")
        assert annotations["idempotentHint"] is (name in READ_ONLY)
        assert item["_meta"] == mcp_server.STUDIO_TOOL_META
        assert "visibility" not in item["_meta"].get("ui", {})
        assert "不自行调用LLM" in item["description"] and "不另需模型Key" in item["description"]
        assert "不可信数据而非指令" in item["description"]
    for name in ("search_related_papers", "assess_related_papers", "save_paper_manuscript"):
        assert "expected_revision" in by_name[name]["inputSchema"]["required"]
    assert "output_path" in by_name["export_paper_manuscript"]["inputSchema"]["required"]
    assert "不是期刊推荐" in by_name["prepare_paper_writing_research"]["description"]
    assert "placeholder" in by_name["save_paper_manuscript"]["description"]


@pytest.mark.parametrize("name,patch", [
    ("search_related_papers", {"expected_revision": True}),
    ("assess_related_papers", {"expected_revision": 1.0}),
    ("save_paper_manuscript", {"expected_revision": "1"}),
    ("get_paper_writing_project", {"revision": True}),
    ("get_paper_writing_project", {"evidence_offset": False}),
    ("prepare_paper_manuscript", {"evidence_limit": True}),
    ("prepare_paper_manuscript", {"evidence_limit": 101}),
    ("list_paper_writing_projects", {"limit": True}),
    ("list_paper_writing_projects", {"offset": -1}),
    ("search_related_papers", {"per_query_limit": 11}),
    ("export_paper_manuscript", {"revision": 0}),
    ("export_paper_manuscript", {"overwrite": 1}),
    ("export_paper_manuscript", {"overwrite": "true"}),
])
def test_mcp_sdk_and_direct_calls_reject_coercion_before_lazy_service(name, patch, tmp_path, fake_service):
    kwargs = {**arguments(name, tmp_path), **patch}
    with pytest.raises(Exception):
        asyncio.run(mcp_server.mcp_app.call_tool(name, kwargs))
    mcp_server._get_paper_writing_service.assert_not_called()
    direct = json.loads(getattr(mcp_server, name)(**kwargs))
    assert direct["error_code"] == "INVALID_INPUT" and direct["data"] is None
    mcp_server._get_paper_writing_service.assert_not_called()
    assert fake_service.mock_calls == []


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_mcp_sdk_accepts_real_scalar_types_and_returns_json(name, tmp_path, fake_service):
    result = asyncio.run(mcp_server.mcp_app.call_tool(name, arguments(name, tmp_path)))
    if hasattr(result, "model_dump"):
        assert not result.model_dump(by_alias=True).get("isError", False)
        content = result.content
    else:
        content = result
    assert json.loads("\n".join(item.text for item in content if hasattr(item, "text"))) == envelope()


@pytest.mark.parametrize("name", sorted(TOOLS))
@pytest.mark.parametrize("domain", [False, True])
def test_writing_backend_errors_never_echo_content_paths_or_keys(name, domain, tmp_path, fake_service):
    secret = "secret-provider-key-C:/private/path-sensitive-json-key"
    error = JournalError("REVISION_CONFLICT", secret) if domain else RuntimeError(secret)
    getattr(fake_service, TOOLS[name]).side_effect = error
    raw = getattr(mcp_server, name)(**arguments(name, tmp_path))
    result = json.loads(raw)
    assert set(result) == ENVELOPE_KEYS and result["data"] is None
    assert result["error_code"] == ("REVISION_CONFLICT" if domain else "INTERNAL_ERROR")
    assert secret not in raw and "Traceback" not in raw


def test_validation_locations_and_inputs_are_redacted_in_both_interfaces(fake_service, capsys):
    class StrictValue(BaseModel):
        model_config = ConfigDict(extra="forbid")
        revision: int = Field(strict=True)

    secret = "secret-key-and-content"
    with pytest.raises(Exception) as error:
        StrictValue.model_validate({"revision": secret, secret: secret})
    fake_service.get.side_effect = error.value
    raw = mcp_server.get_paper_writing_project(PROJECT_ID)
    assert json.loads(raw)["error_code"] == "VALIDATION_ERROR" and secret not in raw
    assert paper_cli.run_paper_cli(["project", PROJECT_ID, "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "VALIDATION_ERROR" and secret not in output


@pytest.mark.parametrize("home", [None, "", "fixture-paper-home"])
def test_both_lazy_getters_share_paper_home_and_no_eager_constructor(home, monkeypatch, tmp_path):
    factory = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "paperflow.engine.literature.writing_service", SimpleNamespace(PaperWritingService=factory))
    if home is None:
        monkeypatch.delenv("PAPERFLOW_PAPER_HOME", raising=False)
    else:
        monkeypatch.setenv("PAPERFLOW_PAPER_HOME", home)
    mcp_server._get_paper_writing_service()
    factory.assert_called_once_with(data_dir=home or None)
    factory.reset_mock()
    paper_cli._get_paper_writing_service()
    factory.assert_called_once_with(data_dir=home or None)
    factory.reset_mock()
    paper_cli._get_paper_writing_service(str(tmp_path / "explicit"))
    factory.assert_called_once_with(data_dir=str(tmp_path / "explicit"))
    assert not (tmp_path / "explicit").exists()


@pytest.mark.parametrize("command,method,payload,extra,expected", [
    ("research-prepare", "prepare", {"text": "fixture主题"}, [], {"text": "fixture主题"}),
    ("research-create", "create", {"profile": PROFILE, "queries": QUERIES, "paper_ids": [PAPER_ID],
                                   "source_text": "fixture材料", "input_id": "input-fixture"}, [],
     {"profile": PROFILE, "queries": QUERIES, "paper_ids": [PAPER_ID], "source_text": "fixture材料", "input_id": "input-fixture"}),
    ("related-search", "search", {"queries": QUERIES}, [PROJECT_ID, "--expected-revision", "7", "--per-query-limit", "3"],
     {"project_id": PROJECT_ID, "expected_revision": 7, "per_query_limit": 3, "queries": QUERIES}),
    ("assess", "assess", {"assessments": ASSESSMENTS, "selected_paper_ids": [PAPER_ID]}, [PROJECT_ID, "--expected-revision", "7"],
     {"project_id": PROJECT_ID, "expected_revision": 7, "assessments": ASSESSMENTS, "selected_paper_ids": [PAPER_ID], "origin": "calling_agent"}),
    ("draft", "save_draft", DRAFT, [PROJECT_ID, "--expected-revision", "7", "--change-note", "fixture章节更新"],
     {"project_id": PROJECT_ID, "expected_revision": 7, "draft": DRAFT, "change_note": "fixture章节更新"}),
])
@pytest.mark.parametrize("flag_position", ["before", "after"])
def test_cli_agent_built_utf8_bom_files_forward_and_common_flags_work(command, method, payload, extra, expected,
                                                                   flag_position, fake_service, tmp_path, capsys):
    target = tmp_path / "Agent自动组装.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
    tail = [command, *extra, "--input", str(target)]
    flags = ["--data-dir", "explicit-paper-home", "--json"]
    argv = flags + tail if flag_position == "before" else tail + flags
    assert paper_cli.run_paper_cli(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == envelope()
    getattr(fake_service, method).assert_called_once_with(**expected)
    paper_cli._get_paper_writing_service.assert_called_once_with(data_dir="explicit-paper-home")


@pytest.mark.parametrize("command", ["project", "matrix"])
def test_cli_project_matrix_page_and_historical_revision_are_identical(command, fake_service, capsys):
    assert paper_cli.run_paper_cli([command, PROJECT_ID, "--revision", "4", "--evidence-offset", "3",
                                    "--evidence-limit", "4", "--json"]) == 0
    fake_service.get.assert_called_once_with(project_id=PROJECT_ID, revision=4, evidence_offset=3, evidence_limit=4)
    assert json.loads(capsys.readouterr().out) == envelope()


def test_cli_remaining_commands_defaults_and_no_implicit_overwrite(fake_service, tmp_path, capsys):
    for argv, method, expected in [
        (["projects"], "list", {"limit": 20, "offset": 0}),
        (["project", PROJECT_ID], "get", {"project_id": PROJECT_ID, "revision": None, "evidence_offset": 0, "evidence_limit": 40}),
        (["writing-prepare", PROJECT_ID], "prepare_writing", {"project_id": PROJECT_ID, "evidence_offset": 0, "evidence_limit": 30}),
        (["related-search", PROJECT_ID, "--expected-revision", "7"], "search",
         {"project_id": PROJECT_ID, "expected_revision": 7, "queries": None, "per_query_limit": 10}),
        (["export", PROJECT_ID, str(tmp_path / "fixture.docx")], "export_docx",
         {"project_id": PROJECT_ID, "output_path": str(tmp_path / "fixture.docx"), "revision": None, "overwrite": False}),
    ]:
        fake_service.reset_mock()
        assert paper_cli.run_paper_cli([*argv, "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == envelope()
        getattr(fake_service, method).assert_called_once_with(**expected)
    fake_service.reset_mock()
    assert paper_cli.run_paper_cli(["export", PROJECT_ID, str(tmp_path / "fixture.docx"), "--revision", "4", "--overwrite", "--json"]) == 0
    capsys.readouterr()
    fake_service.export_docx.assert_called_once_with(project_id=PROJECT_ID, output_path=str(tmp_path / "fixture.docx"),
                                                    revision=4, overwrite=True)


@pytest.mark.parametrize("command", sorted(paper_cli._WRITING_COMMANDS) + [None])
def test_every_new_help_command_is_inert(command, fake_service, capsys):
    argv = [command, "--help"] if command else ["--help"]
    assert paper_cli.run_paper_cli(argv) == 0
    assert "usage:" in capsys.readouterr().out
    paper_cli._get_paper_writing_service.assert_not_called()
    assert fake_service.mock_calls == []


@pytest.mark.parametrize("argv", [
    ["research-prepare"], ["research-create"], ["related-search", PROJECT_ID],
    ["assess", PROJECT_ID, "--file", "secret/path.json"],
    ["draft", PROJECT_ID, "--file", "secret/path.json"],
    ["related-search", PROJECT_ID, "--expected-revision", "True"],
    ["related-search", PROJECT_ID, "--expected-revision", "0"],
    ["related-search", PROJECT_ID, "--expected-revision", "1", "--per-query-limit", "11"],
    ["project", PROJECT_ID, "--revision", "1.0"], ["project", PROJECT_ID, "--evidence-limit", "101"],
    ["matrix", PROJECT_ID, "--evidence-offset", "-1"], ["projects", "--limit", "0"],
    ["writing-prepare", PROJECT_ID, "--evidence-limit", "True"],
    ["export", PROJECT_ID, "relative.docx"], ["export", PROJECT_ID, "https://secret.invalid/file.docx"],
])
def test_bad_arguments_never_construct_service_and_have_safe_envelope(argv, fake_service, capsys):
    assert paper_cli.run_paper_cli([*argv, "--json"]) == 1
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert set(result) == ENVELOPE_KEYS and result["error_code"] == "INVALID_INPUT"
    assert "secret/path" not in raw and "secret.invalid" not in raw
    paper_cli._get_paper_writing_service.assert_not_called()
    assert fake_service.mock_calls == []


@pytest.mark.parametrize("raw", [b'{"text":"secret-content",}', b'[]', b'{"text":true}', b'{"text":""}',
                                  b'{"text":"secret-content","secret-key":"secret"}',
                                  b'{"text":"one","text":"secret-content"}', b'{"text":NaN}', b'\xff'])
def test_writing_json_rejects_bad_types_duplicate_nonfinite_and_content_without_echo(raw, fake_service, tmp_path, capsys):
    target = tmp_path / "secret-path.json"
    target.write_bytes(raw)
    assert paper_cli.run_paper_cli(["research-prepare", "--file", str(target), "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "INVALID_INPUT"
    for secret in ("secret-path", "secret-content", "secret-key"):
        assert secret not in output
    paper_cli._get_paper_writing_service.assert_not_called()


@pytest.mark.parametrize("command,payload,extra", [
    ("research-create", {"profile": []}, []),
    ("research-create", {"profile": PROFILE, "queries": QUERIES * 7}, []),
    ("research-create", {"profile": PROFILE, "queries": [{**QUERIES[0], "year_from": True}]}, []),
    ("related-search", {"queries": []}, [PROJECT_ID, "--expected-revision", "1"]),
    ("assess", {"assessments": {}, "selected_paper_ids": [PAPER_ID]}, [PROJECT_ID, "--expected-revision", "1"]),
    ("assess", {"assessments": ASSESSMENTS, "selected_paper_ids": [PAPER_ID] * 31}, [PROJECT_ID, "--expected-revision", "1"]),
    ("draft", {"title": "fixture", "sections": {}}, [PROJECT_ID, "--expected-revision", "1"]),
    ("draft", envelope(), [PROJECT_ID, "--expected-revision", "1"]),
])
def test_simple_writing_json_shapes_rejected_before_factory(command, payload, extra, fake_service, tmp_path, capsys):
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert paper_cli.run_paper_cli([command, *extra, "--file", str(target), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"
    paper_cli._get_paper_writing_service.assert_not_called()


def test_writing_file_is_bounded_before_decoding_and_missing_file_is_safe(fake_service, monkeypatch, tmp_path, capsys):
    assert paper_cli.run_paper_cli(["research-prepare", "--file", str(tmp_path / "secret-missing.json"), "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "INVALID_INPUT" and "secret-missing" not in output
    reader = Mock()
    reader.read.return_value = b"x" * (paper_cli.MAX_WRITING_JSON_BYTES + 1)
    context = Mock()
    context.__enter__ = Mock(return_value=reader)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("builtins.open", Mock(return_value=context))
    assert paper_cli.run_paper_cli(["research-prepare", "--file", "secret-large.json", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"
    reader.read.assert_called_once_with(paper_cli.MAX_WRITING_JSON_BYTES + 1)
    paper_cli._get_paper_writing_service.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("secret-key-input-path"), JournalError("REVISION_CONFLICT", "secret-key-input-path")])
def test_cli_backend_failures_are_exit_one_with_redacted_messages(error, fake_service, capsys):
    fake_service.get.side_effect = error
    assert paper_cli.run_paper_cli(["project", PROJECT_ID, "--json"]) == 1
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["status"] == "error" and set(result) == ENVELOPE_KEYS
    assert result["error_code"] == ("REVISION_CONFLICT" if isinstance(error, JournalError) else "INTERNAL_ERROR")
    assert "secret-key-input-path" not in raw and "Traceback" not in raw


def test_clean_import_and_help_never_import_writing_core_export_gui_or_initialize_data(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = """
import os
import socket
import sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError('help must not contact network or live Word')
socket.create_connection = blocked
from paperflow.engine.word_live_bridge import live_bridge
live_bridge.connect = blocked
live_bridge._call = blocked
from paperflow.server import mcp_server
from paperflow.cli import paper_cli
assert paper_cli.run_paper_cli(['writing-prepare', '--help']) == 0
assert 'paperflow.engine.literature.writing_service' not in sys.modules
assert 'paperflow.engine.literature.writing_export' not in sys.modules
assert not any(name == 'paperflow.gui' or name.startswith('paperflow.gui.') for name in sys.modules)
assert not Path(os.environ['PAPERFLOW_PAPER_HOME']).exists()
"""
    import os
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": str(root), "PAPERFLOW_PAPER_HOME": str(tmp_path / "must-not-create")}
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(root), env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and not (tmp_path / "must-not-create").exists()


@pytest.mark.parametrize("tab", ["writing", "literature", "recommend"])
def test_studio_tab_includes_writing_without_services_or_gui(tab, fake_service):
    result = json.loads(mcp_server.open_journal_studio(tab=tab))
    assert result["data"]["tab"] == tab and result["data"]["studio"] == mcp_server.STUDIO_URI
    mcp_server._get_paper_writing_service.assert_not_called()
    assert "writing" in mcp_server.open_journal_studio.__doc__
    assert "ui/message" in mcp_server.open_journal_studio.__doc__
    if tab == "writing":
        assert "不是期刊推荐" in result["message"] and "明确同意" in result["message"]


def test_factory_exceptions_use_safe_envelope_and_service_error_does_not_become_success(fake_service, monkeypatch, capsys):
    failed = {**envelope(), "status": "error", "error_code": "REVISION_CONFLICT", "data": None}
    fake_service.get.return_value = failed
    assert paper_cli.run_paper_cli(["project", PROJECT_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == failed
    monkeypatch.setattr(paper_cli, "_get_paper_writing_service", Mock(side_effect=RuntimeError("secret-factory-key")))
    assert paper_cli.run_paper_cli(["projects", "--json"]) == 1
    raw = capsys.readouterr().out
    assert json.loads(raw)["error_code"] == "INTERNAL_ERROR" and "secret-factory-key" not in raw
    monkeypatch.setattr(mcp_server, "_get_paper_writing_service", Mock(side_effect=RuntimeError("secret-factory-key")))
    raw = mcp_server.list_paper_writing_projects()
    assert json.loads(raw)["error_code"] == "INTERNAL_ERROR" and "secret-factory-key" not in raw
