"""Offline literature MCP/CLI contract tests; no real provider or model calls."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from paperflow import __main__ as entrypoint
from paperflow.cli import paper_cli
from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.models import ReadingCard
from paperflow.server import mcp_server


PAPER_ID = "paper-" + "a" * 24
SHA = "b" * 64
FRAGMENT_ID = "frag-" + "c" * 24
PAPER = {"paper_id": PAPER_ID, "title": "真实记录示例", "source_id": "arxiv"}
CARD = {
    "paper_id": PAPER_ID, "file_sha256": SHA, "summary": "只依据已读片段的解读。",
    "claims": [{"section": "method", "kind": "author_claim", "text": "作者描述了方法。",
                "evidence": [{"fragment_id": FRAGMENT_ID, "page_number": 2, "quote": "method"}]}],
}
MCP_CASES = [
    ("search_academic_papers", "search",
     {"query": "PDF evidence", "limit": 7, "sources": ["arxiv", "crossref"],
      "year_from": 2020, "year_to": 2025, "sort_by": "date"}),
    ("get_academic_paper", "get", {"paper_id": "", "identifier": "10.1234/example"}),
    ("download_academic_paper", "download", {"paper_id": PAPER_ID}),
    ("import_local_paper", "import_pdf", {"file_path": "C:/用户文件/论文.pdf", "title": "用户论文"}),
    ("read_academic_paper", "read",
     {"paper_id": PAPER_ID, "page_number": 2, "page_count": 4, "offset": 350, "max_chars": 1500}),
    ("save_paper_reading", "save_reading",
     {"paper_id": PAPER_ID, "reading": CARD, "origin": "calling_agent", "strict": True}),
    ("list_paper_library", "list", {"limit": 9, "offset": 12}),
]


def envelope(method="get"):
    data = {**PAPER, "acquisition": {"status": "downloaded"}, "reading_cards": [CARD]}
    if method == "search":
        data = {"papers": [PAPER], "total": 1,
                "source_statuses": [{"source_id": "crossref", "status": "error",
                                     "error_code": "SOURCE_UNAVAILABLE", "message": "来源暂不可用"}],
                "capabilities": {"sort_by": ["relevance"]}}
    elif method == "read":
        data = {"paper_id": PAPER_ID, "paper": PAPER, "file_sha256": SHA, "total_pages": 5,
                "pages": [{"page_number": 2, "text": "method"}],
                "fragments": [{"fragment_id": FRAGMENT_ID, "page_number": 2, "text": "method"}],
                "next_cursor": {"page_number": 2, "offset": 1350},
                "agent_contract": {"reading_schema": ReadingCard.model_json_schema()}}
    elif method == "save_reading":
        data = {"reading_id": "reading-example", "card": CARD,
                "evidence_check_only": True, "semantic_correctness_verified": False}
    elif method == "list":
        data = {"papers": [PAPER], "total": 30}
    return {"status": "partial", "error_code": None, "message": "保留实际覆盖",
            "data": data, "sources": [{"source_id": "arxiv"}],
            "coverage": {"stage": "fulltext_partial", "understanding_complete": False},
            "warnings": ["尚未核对图表"], "suggested_options": ["继续阅读"]}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    original_connect = socket.socket.connect

    def blocked(*args, **kwargs):
        raise AssertionError("接口测试不得联网")

    def local_socketpair_only(sock, address):
        # Windows asyncio creates its self-pipe through a loopback socketpair.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return blocked()

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_socketpair_only)


@pytest.fixture
def fake_service(monkeypatch):
    service = Mock()
    for method in ("search", "get", "download", "import_pdf", "read", "save_reading", "list"):
        getattr(service, method).return_value = envelope(method)
    monkeypatch.setattr(mcp_server, "_get_literature_service", lambda: service)
    monkeypatch.setattr(paper_cli, "_get_literature_service", lambda data_dir=None: service)
    return service


@pytest.mark.parametrize("tool_name,method,kwargs", MCP_CASES)
def test_mcp_forwards_seven_tools_and_preserves_envelopes(tool_name, method, kwargs, fake_service, monkeypatch):
    bridge = Mock()
    monkeypatch.setattr(mcp_server, "live_bridge", bridge)
    raw = getattr(mcp_server, tool_name)(**kwargs)
    assert json.loads(raw) == envelope(method)
    getattr(fake_service, method).assert_called_once_with(**kwargs)
    assert bridge.mock_calls == []


def test_mcp_defaults_and_library_offset_are_forwarded(fake_service):
    mcp_server.search_academic_papers("evidence")
    fake_service.search.assert_called_once_with(query="evidence", limit=10, sources=None,
                                                year_from=None, year_to=None, sort_by="relevance")
    mcp_server.get_academic_paper(paper_id=PAPER_ID)
    fake_service.get.assert_called_once_with(paper_id=PAPER_ID, identifier="")
    mcp_server.import_local_paper("local.pdf")
    fake_service.import_pdf.assert_called_once_with(file_path="local.pdf", title="")
    mcp_server.read_academic_paper(PAPER_ID)
    fake_service.read.assert_called_once_with(paper_id=PAPER_ID, page_number=1, page_count=3,
                                              offset=0, max_chars=20000)
    mcp_server.save_paper_reading(PAPER_ID, CARD)
    fake_service.save_reading.assert_called_once_with(paper_id=PAPER_ID, reading=CARD,
                                                      origin="calling_agent", strict=True)
    mcp_server.list_paper_library()
    fake_service.list.assert_called_once_with(limit=20, offset=0)


def test_mcp_save_exposes_explicit_origin_and_strict_mode(fake_service):
    mcp_server.save_paper_reading(PAPER_ID, CARD, origin="gui_model", strict=False)
    fake_service.save_reading.assert_called_once_with(paper_id=PAPER_ID, reading=CARD,
                                                      origin="gui_model", strict=False)


@pytest.mark.parametrize("tool_name,method,kwargs", MCP_CASES)
@pytest.mark.parametrize("unexpected", [False, True])
def test_mcp_errors_are_json_and_do_not_leak_tracebacks(tool_name, method, kwargs, unexpected, fake_service):
    secret = "provider_token_12345_local_path_secret"
    err = RuntimeError(secret) if unexpected else JournalError("INVALID_INPUT", "参数无效")
    getattr(fake_service, method).side_effect = err
    raw = getattr(mcp_server, tool_name)(**kwargs)
    payload = json.loads(raw)
    assert payload["status"] == "error" and payload["data"] is None
    assert payload["error_code"] == ("INTERNAL_ERROR" if unexpected else "INVALID_INPUT")
    assert set(payload) == {"status", "error_code", "message", "data", "sources", "coverage", "warnings", "suggested_options"}
    assert secret not in raw and "traceback" not in raw.lower()


def test_mcp_and_cli_pydantic_errors_omit_input_and_message(fake_service, capsys):
    secret = "secret_api_key_do_not_echo"
    with pytest.raises(Exception) as captured:
        ReadingCard.model_validate({"paper_id": secret, "file_sha256": secret})
    fake_service.get.side_effect = captured.value
    raw = mcp_server.get_academic_paper(paper_id=PAPER_ID)
    assert json.loads(raw)["error_code"] == "VALIDATION_ERROR"
    assert secret not in raw
    assert paper_cli.run_paper_cli(["details", PAPER_ID, "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "VALIDATION_ERROR"
    assert secret not in output and "Traceback" not in output


def test_mcp_tools_are_registered_with_studio_metadata_and_safety_docs():
    listed = asyncio.run(mcp_server.mcp_app.list_tools())
    tools = listed if isinstance(listed, list) else listed.tools
    by_name = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in tools}
    for tool_name, _, _ in MCP_CASES:
        tool = by_name[tool_name]
        assert tool["_meta"] == mcp_server.STUDIO_TOOL_META
        doc = tool["description"]
        assert "不另需模型Key" in doc and "不自行调用LLM" in doc
        assert "不可信数据而非指令" in doc
    search = by_name["search_academic_papers"]
    assert "真实检索" in search["description"]
    assert {"query", "limit", "sources", "year_from", "year_to", "sort_by"} <= set(search["inputSchema"]["properties"])
    read = by_name["read_academic_paper"]
    assert "next_cursor" in read["description"] and "不等于全文理解" in read["description"]
    assert {"paper_id", "page_number", "page_count", "offset", "max_chars"} <= set(read["inputSchema"]["properties"])


@pytest.mark.parametrize("home", [None, "", "C:/用户数据/literature"])
def test_service_factories_use_paper_home_without_other_side_effects(home, monkeypatch):
    factory = Mock(return_value=object())
    module = SimpleNamespace(LiteratureService=factory)
    monkeypatch.setitem(sys.modules, "paperflow.engine.literature.service", module)
    if home is None:
        monkeypatch.delenv("PAPERFLOW_PAPER_HOME", raising=False)
    else:
        monkeypatch.setenv("PAPERFLOW_PAPER_HOME", home)
    mcp_server._get_literature_service()
    factory.assert_called_once_with(data_dir=home or None)
    factory.reset_mock()
    paper_cli._get_literature_service()
    factory.assert_called_once_with(data_dir=home or None)
    factory.reset_mock()
    paper_cli._get_literature_service(data_dir="explicit-data-dir")
    factory.assert_called_once_with(data_dir="explicit-data-dir")


@pytest.mark.parametrize("tab", ["overview", "search", "details", "compare", "recommend", "literature"])
def test_studio_tab_whitelist_includes_literature(tab):
    assert json.loads(mcp_server.open_journal_studio(tab))["data"]["tab"] == tab
    assert json.loads(mcp_server.open_journal_studio("papers_import"))["data"]["tab"] == "recommend"


def test_studio_docstring_lists_literature_actions_without_gui_import():
    doc = mcp_server.studio_api.__doc__
    for action in ("papers_search", "papers_details", "papers_download", "papers_read", "papers_notes", "papers_list"):
        assert action in doc
    assert "File paths are rejected" in doc and "papers_import" not in doc


@pytest.mark.parametrize("command", ["paper", "papers"])
def test_cli_registered_in_entrypoint(command, monkeypatch):
    run = Mock(return_value=1)
    monkeypatch.setattr(paper_cli, "run_paper_cli", run)
    monkeypatch.setattr(sys, "argv", ["paperflow", command, "list", "--json"])
    with pytest.raises(SystemExit) as exit_result:
        entrypoint.main()
    assert exit_result.value.code == 1
    run.assert_called_once_with(["list", "--json"])


def test_entrypoint_keeps_existing_gui_branch(monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, "paperflow.gui.server", SimpleNamespace(run_gui=run))
    monkeypatch.setattr(sys, "argv", ["paperflow", "gui", "--port", "8766"])
    with pytest.raises(SystemExit) as exit_result:
        entrypoint.main()
    assert exit_result.value.code == 0
    run.assert_called_once_with(["--port", "8766"])


@pytest.mark.parametrize("flag_position", ["before", "after"])
def test_cli_search_forwards_filters_and_common_flags(flag_position, fake_service, monkeypatch, capsys):
    factory = Mock(return_value=fake_service)
    monkeypatch.setattr(paper_cli, "_get_literature_service", factory)
    flags = ["--json", "--data-dir", "my-paper-library"]
    args = ["search", "evidence", "--sources", "arxiv, crossref", "--limit", "6",
            "--year-from", "2020", "--year-to", "2025", "--sort-by", "date"]
    args = flags + args if flag_position == "before" else args + flags
    assert paper_cli.run_paper_cli(args) == 0
    factory.assert_called_once_with(data_dir="my-paper-library")
    fake_service.search.assert_called_once_with(query="evidence", sources=["arxiv", "crossref"], limit=6,
                                                year_from=2020, year_to=2025, sort_by="date")
    assert json.loads(capsys.readouterr().out) == envelope("search")


@pytest.mark.parametrize("args,method,kwargs", [
    (["search", "keyword"], "search", {"query": "keyword", "limit": 10, "sources": None,
                                         "year_from": None, "year_to": None, "sort_by": "relevance"}),
    (["details", PAPER_ID], "get", {"paper_id": PAPER_ID, "identifier": ""}),
    (["details", "--identifier", "arXiv:2401.00001"], "get", {"paper_id": "", "identifier": "arXiv:2401.00001"}),
    (["download", PAPER_ID], "download", {"paper_id": PAPER_ID}),
    (["import", "C:/用户文件/论文.pdf", "--title", "论文"], "import_pdf",
     {"file_path": "C:/用户文件/论文.pdf", "title": "论文"}),
    (["read", PAPER_ID], "read", {"paper_id": PAPER_ID, "page_number": 1, "page_count": 3, "offset": 0, "max_chars": 20000}),
    (["list"], "list", {"limit": 20, "offset": 0}),
    (["list", "--limit", "7", "--offset", "25"], "list", {"limit": 7, "offset": 25}),
])
def test_cli_command_forwarding_and_json(args, method, kwargs, fake_service, capsys):
    assert paper_cli.run_paper_cli(args + ["--json"]) == 0
    getattr(fake_service, method).assert_called_once_with(**kwargs)
    assert json.loads(capsys.readouterr().out) == envelope(method)


def test_cli_read_preserves_cursor_and_does_not_claim_completion(fake_service, capsys):
    args = ["read", PAPER_ID, "--page-number", "2", "--page-count", "4", "--offset", "350", "--max-chars", "1000", "--json"]
    assert paper_cli.run_paper_cli(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["data"]["next_cursor"] == {"page_number": 2, "offset": 1350}
    assert first["coverage"]["understanding_complete"] is False
    fake_service.read.assert_called_once_with(paper_id=PAPER_ID, page_number=2, page_count=4, offset=350, max_chars=1000)
    fake_service.read.reset_mock()
    cursor = first["data"]["next_cursor"]
    assert paper_cli.run_paper_cli(["read", PAPER_ID, "--page-number", str(cursor["page_number"]),
                                    "--offset", str(cursor["offset"]), "--json"]) == 0
    fake_service.read.assert_called_once_with(paper_id=PAPER_ID, page_number=2, page_count=3, offset=1350, max_chars=20000)


@pytest.mark.parametrize("flag", ["--file", "--input"])
def test_cli_notes_loads_bounded_json_and_forwards_strict_card(flag, fake_service, tmp_path, capsys):
    source = tmp_path / "解读卡.json"
    source.write_text(json.dumps(CARD, ensure_ascii=False), encoding="utf-8-sig")
    assert paper_cli.run_paper_cli(["notes", PAPER_ID, flag, str(source), "--json"]) == 0
    fake_service.save_reading.assert_called_once_with(paper_id=PAPER_ID, reading=CARD, origin="calling_agent", strict=True)
    assert json.loads(capsys.readouterr().out) == envelope("save_reading")
    assert paper_cli.run_paper_cli(["details", PAPER_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["reading_cards"] == [CARD]


@pytest.mark.parametrize("content", [b"[1]", b"{broken secret_api_key}", b"\xff", b"{" * 1500])
def test_cli_notes_bad_json_is_invalid_input_without_leaking_content(content, fake_service, tmp_path, capsys):
    source = tmp_path / "private-secret-path.json"
    source.write_bytes(content)
    assert paper_cli.run_paper_cli(["notes", PAPER_ID, "--file", str(source), "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "INVALID_INPUT"
    assert "secret_api_key" not in output and "private-secret-path" not in output and "traceback" not in output.lower()
    fake_service.save_reading.assert_not_called()


def test_cli_notes_missing_file_is_invalid_input(fake_service, tmp_path, capsys):
    assert paper_cli.run_paper_cli(["notes", PAPER_ID, "--file", str(tmp_path / "missing.json"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"
    fake_service.save_reading.assert_not_called()


def test_cli_notes_bounds_read_before_json_parse(fake_service, monkeypatch, capsys):
    stream = Mock()
    stream.__enter__ = Mock(return_value=stream)
    stream.__exit__ = Mock(return_value=False)
    stream.read.return_value = b"x" * 65
    monkeypatch.setattr(paper_cli, "MAX_READING_JSON_BYTES", 64)
    monkeypatch.setattr("builtins.open", Mock(return_value=stream))
    assert paper_cli.run_paper_cli(["notes", PAPER_ID, "--file", "oversized.json", "--json"]) == 1
    stream.read.assert_called_once_with(65)
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"
    fake_service.save_reading.assert_not_called()


@pytest.mark.parametrize("args", [
    ["details"], ["details", PAPER_ID, "--identifier", "10.1234/example"],
    ["read", PAPER_ID, "--page-number", "private-token-not-an-int"],
    ["search", "keyword", "--sources", "arxiv,,crossref"],
    ["search", "keyword", "--unknown-private-token"], ["notes", PAPER_ID], [],
])
def test_cli_parser_errors_use_invalid_input_envelope(args, fake_service, capsys):
    assert paper_cli.run_paper_cli(args + ["--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error_code"] == "INVALID_INPUT" and payload["data"] is None
    assert captured.err == ""
    assert "private-token" not in captured.out and "traceback" not in captured.out.lower()
    assert fake_service.mock_calls == []


@pytest.mark.parametrize("error", [JournalError("INVALID_INPUT", "参数无效"), RuntimeError("secret-backend-token")])
def test_cli_errors_are_exit_one_and_sanitized(error, fake_service, capsys):
    fake_service.get.side_effect = error
    assert paper_cli.run_paper_cli(["details", PAPER_ID, "--json"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["error_code"] == ("INVALID_INPUT" if isinstance(error, JournalError) else "INTERNAL_ERROR")
    assert payload["status"] == "error" and payload["data"] is None
    assert "secret-backend-token" not in output and "traceback" not in output.lower()


def test_cli_service_returned_error_is_not_treated_as_success(fake_service, capsys):
    payload = paper_cli._format_error_envelope(JournalError("NO_LEGAL_FULLTEXT", "未找到合法公开全文"))
    fake_service.download.return_value = payload
    assert paper_cli.run_paper_cli(["download", PAPER_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == payload


def test_cli_service_construction_failure_is_sanitized(monkeypatch, capsys):
    factory = Mock(side_effect=RuntimeError("secret-init-token"))
    monkeypatch.setattr(paper_cli, "_get_literature_service", factory)
    assert paper_cli.run_paper_cli(["list", "--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "INTERNAL_ERROR"
    assert "secret-init-token" not in output


@pytest.mark.parametrize("args", [["--help"], ["search", "--help"], ["read", "--help"], ["notes", "--help"]])
def test_cli_help_never_initializes_service(args, monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("help must not create the service"))
    monkeypatch.setattr(paper_cli, "_get_literature_service", factory)
    assert paper_cli.run_paper_cli(args) == 0
    assert "usage:" in capsys.readouterr().out
    factory.assert_not_called()


def test_cli_help_only_lists_implemented_subcommands():
    parser = paper_cli.create_parser()
    action = next(a for a in parser._actions if isinstance(getattr(a, "choices", None), dict))
    assert set(action.choices) == {
        "search", "details", "download", "import", "read", "notes", "list",
        "research-prepare", "research-create", "related-search", "assess", "project",
        "projects", "matrix", "writing-prepare", "draft", "export",
        "loop-status", "loop-control", "review-prepare", "review-submit", "loop-step", "loop-feedback",
    }
    assert {"--json", "--data-dir"} <= {o for a in parser._actions for o in a.option_strings}


def test_cli_readable_output_keeps_card_coverage_and_warnings(fake_service, capsys):
    assert paper_cli.run_paper_cli(["details", PAPER_ID]) == 0
    output = capsys.readouterr().out
    for text in ("reading_cards", "file_sha256", "fulltext_partial", "尚未核对图表"):
        assert text in output


def _local_pdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=240, height=240)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    text = DecodedStreamObject()
    text.set_data(b"BT /F1 12 Tf 20 200 Td (" + b"Evidence from local paper. " * 60 + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(text)
    target = tmp_path / "用户论文.pdf"
    with target.open("wb") as output:
        writer.write(output)
    return target


def test_local_pdf_import_read_notes_details_roundtrip_is_offline(tmp_path, monkeypatch, capsys):
    # Keep the mock interfaces runnable even before the service is integrated.
    module = pytest.importorskip("paperflow.engine.literature.service")

    providers = Mock(spec=[])
    service = module.LiteratureService(data_dir=str(tmp_path / "library"), providers=providers)
    monkeypatch.setattr(mcp_server, "_get_literature_service", lambda: service)
    monkeypatch.setattr(paper_cli, "_get_literature_service", lambda data_dir=None: service)
    pdf = _local_pdf(tmp_path)
    assert paper_cli.run_paper_cli(["import", str(pdf), "--title", "本地论文", "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)["data"]
    pid = imported["paper_id"]
    assert imported["acquisition"]["status"] == "imported"
    first = json.loads(mcp_server.read_academic_paper(pid, max_chars=1000))
    assert first["status"] == "success", first
    assert first["data"]["next_cursor"] == {"page_number": 1, "offset": 1000}
    assert first["coverage"]["understanding_complete"] is False
    cursor = first["data"]["next_cursor"]
    assert paper_cli.run_paper_cli(["read", pid, "--page-number", str(cursor["page_number"]),
                                    "--offset", str(cursor["offset"]), "--json"]) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["data"]["next_cursor"] is None
    assert final["coverage"]["text_complete"] is True
    assert final["coverage"]["understanding_complete"] is False
    fragment = final["data"]["fragments"][0]
    card = {"paper_id": pid, "file_sha256": final["data"]["file_sha256"], "summary": "依据本地页内证据。",
            "claims": [{"section": "results", "kind": "author_claim", "text": "作者提供本地证据示例。",
                        "evidence": [{"fragment_id": fragment["fragment_id"], "page_number": fragment["page_number"],
                                      "quote": fragment["text"]}]}]}
    card_file = tmp_path / "reading.json"
    card_file.write_text(json.dumps(card), encoding="utf-8")
    assert paper_cli.run_paper_cli(["notes", pid, "--file", str(card_file), "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["data"]["evidence_check_only"] is True
    assert saved["data"]["semantic_correctness_verified"] is False
    details = json.loads(mcp_server.get_academic_paper(paper_id=pid))
    stored = details["data"]["reading_cards"][0]["card"]
    assert stored == saved["data"]["card"]
    assert stored["paper_id"] == pid and stored["file_sha256"] == card["file_sha256"]
    assert stored["claims"][0]["evidence"][0]["quote"] == fragment["text"]
    assert providers.mock_calls == []
