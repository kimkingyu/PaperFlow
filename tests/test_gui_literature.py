"""Literature GUI actions, HTTP guards and bounded model reading: no real APIs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

import pytest
from starlette.testclient import TestClient

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, response
from paperflow.engine.literature.models import ReadingCard
from paperflow.gui import actions, model_scoring, paper_reading, server
from paperflow.gui.secrets import ModelConfig, SecretStore

PAPER_ID = "paper-" + "a" * 24
SHA = "b" * 64
TOKEN = "t" * 43
PORT = 5123
SECRET = "sk-test-LITERATURE-SECRET-0123456789"
ROOT = Path(__file__).parents[1]


def in_event_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture(autouse=True)
def no_network_or_keyring(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("GUI literature tests must not call real APIs")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(model_scoring, "_http_transport", forbidden)
    monkeypatch.setattr("paperflow.gui.secrets._keyring", lambda: None)


class FakeService:
    def __init__(self, available=True):
        self.calls = []
        self.fragments = {}
        self.ranges = {}
        self.texts = ["SELECTED_PAGE_ONE evidence about robots. " * 80,
                      "SECOND_PAGE_NOT_SELECTED experiments. " * 80, ""]
        self.paper = {
            "paper_id": PAPER_ID, "title": '<script>alert("title")</script>',
            "authors": ['<img src=x onerror="alert(1)">', "Author Two"],
            "year": 2024, "doi": "10.1000/robot", "arxiv_id": "2401.01234v2",
            "abstract": "ABSTRACT_NOT_FOR_MODEL <b>unsafe markup</b>",
            "landing_url": "https://papers.example/robot", "source_id": "crossref",
            "source_record": "10.1000/robot", "version": "published",
            "publication_type": "journal_article", "fulltext_locations": [],
            "acquisition": {"status": "downloaded" if available else "not_attempted",
                            "file_sha256": SHA if available else ""},
            "reading_cards": [], "sources": [],
        }
        self.fail_download = False

    def called(self, name, **params):
        self.calls.append((name, deepcopy(params), in_event_loop()))

    def search(self, query, limit=10, sources=None, year_from=None, year_to=None,
               sort_by="relevance"):
        self.called("search", query=query, limit=limit, sources=sources,
                    year_from=year_from, year_to=year_to, sort_by=sort_by)
        return response({"papers": [deepcopy(self.paper)], "total": 1,
                         "source_statuses": [{"source_id": "crossref", "status": "success"},
                                             {"source_id": "arxiv", "status": "error",
                                              "error_code": "SOURCE_UNAVAILABLE", "message": "offline fixture"}],
                         "capabilities": {"search": ["crossref", "arxiv"], "ocr": False}}, status="partial")

    def get(self, paper_id="", identifier=""):
        self.called("get", paper_id=paper_id, identifier=identifier)
        assert paper_id == PAPER_ID and not identifier
        return response(deepcopy(self.paper))

    def download(self, paper_id):
        self.called("download", paper_id=paper_id)
        assert paper_id == PAPER_ID
        self.paper["acquisition"] = {"status": "unavailable" if self.fail_download else "downloaded",
                                     "file_sha256": "" if self.fail_download else SHA}
        return response(deepcopy(self.paper), status="partial" if self.fail_download else "success")

    def read(self, paper_id, page_number=1, page_count=3, offset=0, max_chars=20000):
        self.called("read", paper_id=paper_id, page_number=page_number,
                    page_count=page_count, offset=offset, max_chars=max_chars)
        assert paper_id == PAPER_ID
        if self.paper["acquisition"]["status"] not in ("downloaded", "imported"):
            raise JournalError("FULLTEXT_REQUIRED", "尚无本地全文；摘要不能替代正文")
        pages, fragments, next_cursor = [], [], None
        budget = max_chars
        for number in range(page_number, min(3, page_number + page_count - 1) + 1):
            text = self.texts[number - 1]
            start = offset if number == page_number else 0
            end = min(len(text), start + budget)
            chosen = text[start:end]
            pages.append({"page_number": number, "text": chosen, "offset_start": start,
                          "offset_end": end, "page_complete": end == len(text)})
            self.ranges.setdefault(number, []).append((start, end))
            if chosen:
                fragment = {"fragment_id": f"frag-{number}-{start}-{end}", "page_number": number,
                            "start": start, "end": end, "text": chosen}
                fragments.append(fragment)
                self.fragments[fragment["fragment_id"]] = fragment
            budget -= len(chosen)
            next_cursor = {"page_number": number, "offset": end} if end < len(text) else \
                {"page_number": number + 1, "offset": 0} if number < 3 else None
            if end < len(text) or budget <= 0:
                break
        fully_extracted = []
        for number, ranges in self.ranges.items():
            right = 0
            for start, end in sorted(ranges):
                if start <= right:
                    right = max(right, end)
            if self.texts[number - 1] and right >= len(self.texts[number - 1]):
                fully_extracted.append(number)
        coverage = {"fully_extracted_pages": sorted(fully_extracted), "visited_pages": sorted(self.ranges),
                    "total_pages": 3, "text_complete": False, "understanding_complete": False,
                    "empty_or_unextractable_pages": [{"page_number": 3, "reason": "needs_ocr"}] if 3 in self.ranges else [],
                    "backend_calls_llm": False}
        return response({"paper_id": paper_id, "paper": deepcopy(self.paper), "file_sha256": SHA,
                         "total_pages": 3, "pages": pages, "fragments": fragments,
                         "next_cursor": next_cursor, "truncated": next_cursor is not None,
                         "coverage": coverage, "agent_contract": {"reading_schema": ReadingCard.model_json_schema()}},
                        coverage=coverage)

    def save_reading(self, paper_id, reading, origin="calling_agent", strict=True):
        self.called("save_reading", paper_id=paper_id, reading=reading, origin=origin, strict=strict)
        card = ReadingCard.model_validate(reading).model_dump(mode="json")
        assert card["paper_id"] == paper_id == PAPER_ID and card["file_sha256"] == SHA
        for claim in card["claims"]:
            invalid = claim["kind"] != "unverified" and not claim["evidence"]
            for evidence in claim["evidence"]:
                fragment = self.fragments.get(evidence["fragment_id"])
                invalid |= not fragment or evidence["quote"] not in fragment["text"] or \
                    evidence["page_number"] not in (None, fragment["page_number"])
            if invalid:
                if strict:
                    raise JournalError("EVIDENCE_MISMATCH", "引用无法对应正文")
                claim["kind"], claim["evidence"] = "unverified", []
        saved = {"reading_id": "reading-fixture", "created_at": "2024-01-01T00:00:00Z",
                 "origin": origin, "valid": True, "card": card}
        self.paper["reading_cards"].append(deepcopy(saved))
        return response(deepcopy(saved), coverage={"evidence_check_only": True, "backend_calls_llm": False})

    def list(self, limit=20, offset=0):
        self.called("list", limit=limit, offset=offset)
        return response({"papers": [deepcopy(self.paper)] if offset == 0 else [], "total": 1})


class MockModel:
    def __init__(self, fabricate=False, provider="openai_compatible", extra_evidence=None):
        self.calls = []
        self.fabricate, self.provider, self.extra_evidence = fabricate, provider, extra_evidence

    def __call__(self, url, headers, body):
        self.calls.append((url, deepcopy(headers), deepcopy(body), in_event_loop()))
        selection = json.loads(body["messages"][-1]["content"].split("本次选择段落：\n", 1)[1])
        fragment = selection["fragments"][0]
        evidence = {"fragment_id": fragment["fragment_id"], "page_number": fragment["page_number"],
                    "quote": fragment["text"][:24]}
        claims = [{"section": "method", "kind": "author_claim", "text": "本次片段描述机器人方法。",
                   "evidence": [evidence]}]
        if self.fabricate:
            claims.extend([
                {"section": "results", "kind": "author_claim", "text": "不存在的量子突破。",
                 "evidence": [{"fragment_id": "frag-invented", "page_number": 99, "quote": "fabricated"}]},
                {"section": "assumptions", "kind": "agent_inference", "text": "没有来源的推断。", "evidence": []},
                {"section": "limitations", "kind": "author_claim", "text": "错页证据。",
                 "evidence": [{**evidence, "page_number": 99}]},
            ])
        if self.extra_evidence:
            claims.append({"section": "results", "kind": "author_claim", "text": "不在当前选择中的证据。",
                           "evidence": [self.extra_evidence]})
        content = json.dumps({"paper_id": "paper-" + "c" * 24, "file_sha256": "d" * 64,
                              "summary": "仅总结本次选择片段。", "claims": claims}, ensure_ascii=False)
        if self.provider == "anthropic":
            return {"content": [{"type": "text", "text": content}]}
        return {"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]}


def model_config(consented=True, provider="openai_compatible", base_url="https://model.example/v1", key=SECRET):
    return ModelConfig(provider=provider, base_url=base_url, model="fixture-model",
                       consented=consented, _session_key=key)


def empty_card(**changes):
    return {"paper_id": PAPER_ID, "file_sha256": SHA, "summary": "仅针对已选片段。", "claims": [], **changes}


@pytest.fixture
def finder(tmp_path):
    return JournalFinder(str(tmp_path / "journal-home"))


@pytest.fixture
def http(finder):
    service, store, model = FakeService(), SecretStore(), MockModel(fabricate=True)
    app = server.create_app(TOKEN, PORT, finder=finder, store=store, transport=model, literature=service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield client, service, store, model


def post(client, path, payload, token=TOKEN, host=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-PaperFlow-Token"] = token
    if host:
        headers["Host"] = host
    return client.post(path, content=json.dumps(payload), headers=headers)


def test_papers_search_forwards_filters_and_partial_source_status(finder):
    service = FakeService()
    result = actions.dispatch(finder, "papers_search", {"query": " robotics ", "sources": ["arxiv"],
                              "limit": "7", "year_from": 2020, "year_to": 2024, "sort_by": "date"}, literature=service)
    assert service.calls[0][1] == {"query": "robotics", "sources": ["arxiv"], "limit": 7,
                                   "year_from": 2020, "year_to": 2024, "sort_by": "date"}
    assert result["status"] == "partial" and result["data"]["total"] == 1
    assert result["data"]["source_statuses"][1]["error_code"] == "SOURCE_UNAVAILABLE"
    assert result["data"]["capabilities"]["ocr"] is False
    assert result["data"]["papers"][0]["doi"] == "10.1000/robot"
    default = actions.dispatch(finder, "papers_search", {"query": "robots"}, literature=service)
    assert default["data"]["papers"] and service.calls[-1][1]["limit"] == 10


def test_details_download_and_local_library(finder):
    service = FakeService(available=False)
    assert actions.dispatch(finder, "papers_details", {"paper_id": PAPER_ID}, service)["data"]["reading_cards"] == []
    assert actions.dispatch(finder, "papers_download", {"paper_id": PAPER_ID}, service)["data"]["acquisition"]["status"] == "downloaded"
    assert actions.dispatch(finder, "papers_list", literature=service)["data"]["total"] == 1
    assert actions.dispatch(finder, "papers_list", {"limit": 5, "offset": "20"}, service)["data"]["papers"] == []
    assert service.calls[-1][1] == {"limit": 5, "offset": 20}


def test_page_offset_and_next_cursor_remain_same_page(finder):
    service = FakeService()
    first = actions.dispatch(finder, "papers_read", {"paper_id": PAPER_ID, "page_count": 1, "max_chars": 1000}, service)
    assert first["data"]["pages"][0]["offset_start"] == 0
    assert first["data"]["pages"][0]["offset_end"] == 1000
    assert first["data"]["next_cursor"] == {"page_number": 1, "offset": 1000}
    second = actions.dispatch(finder, "papers_read", {"paper_id": PAPER_ID, "page_count": 1, "max_chars": 1000,
                                                     **first["data"]["next_cursor"]}, service)
    assert second["data"]["pages"][0]["text"] == service.texts[0][1000:2000]
    assert second["data"]["next_cursor"] == {"page_number": 1, "offset": 2000}
    assert second["coverage"]["fully_extracted_pages"] == []
    assert second["coverage"]["understanding_complete"] is False
    last = actions.dispatch(finder, "papers_read", {"paper_id": PAPER_ID, "page_number": 3}, service)
    assert last["data"]["next_cursor"] is None and last["data"]["fragments"] == []
    assert last["coverage"]["text_complete"] is False
    assert last["coverage"]["empty_or_unextractable_pages"][0]["page_number"] == 3


VALID_ACTION_PARAMS = {
    "papers_search": {"query": "robots"}, "papers_details": {"paper_id": PAPER_ID},
    "papers_download": {"paper_id": PAPER_ID}, "papers_read": {"paper_id": PAPER_ID},
    "papers_notes": {"paper_id": PAPER_ID, "reading": empty_card()}, "papers_list": {},
}


@pytest.mark.parametrize("action", VALID_ACTION_PARAMS)
@pytest.mark.parametrize("key", ["file_path", "input_paths", "html_path", "previous_file_path", "path",
                                    "project_path", "github_repo", "url", "download_url", "identifier",
                                    "data_dir", "transport", "providers", "origin", "strict", "unknown"])
def test_literature_action_parameters_are_strict(finder, action, key):
    service = FakeService()
    with pytest.raises(JournalError) as caught:
        actions.dispatch(finder, action, {**VALID_ACTION_PARAMS[action], key: "C:/secret.pdf"}, service)
    assert caught.value.code == "INVALID_INPUT"
    assert not service.calls


@pytest.mark.parametrize("action", ["papers_details", "papers_download", "papers_read", "papers_notes"])
@pytest.mark.parametrize("paper_id", ["../../secret", "C:/private.pdf", "https://example.com/x.pdf", "paper-../secret", "", None])
def test_path_like_paper_ids_cannot_reach_service(finder, action, paper_id):
    service = FakeService()
    with pytest.raises(JournalError) as caught:
        actions.dispatch(finder, action, {**VALID_ACTION_PARAMS[action], "paper_id": paper_id}, service)
    assert caught.value.code == "INVALID_PAPER_ID" and not service.calls


@pytest.mark.parametrize("params", [{"query": " "}, {"query": "x" * 1001}, {"query": "robots", "sources": "arxiv"},
                                    {"query": "robots", "sources": ["https://example.com"]},
                                    {"query": "robots", "year_from": 2025, "year_to": 2024},
                                    {"query": "robots", "limit": True}, {"query": "robots", "sort_by": "unsafe"}])
def test_invalid_search_input(finder, params):
    service = FakeService()
    with pytest.raises(JournalError):
        actions.dispatch(finder, "papers_search", params, service)
    assert not service.calls


@pytest.mark.parametrize("params", [{"page_number": 0}, {"page_count": 11}, {"offset": -1}, {"offset": 1.2},
                                    {"max_chars": 999}, {"max_chars": 20001}, {"page_number": True}])
def test_invalid_read_pagination(finder, params):
    service = FakeService()
    with pytest.raises(JournalError):
        actions.dispatch(finder, "papers_read", {"paper_id": PAPER_ID, **params}, service)
    assert not service.calls


def test_notes_schema_and_unverified_evidence(finder):
    service = FakeService()
    card = empty_card(claims=[{"section": "results", "kind": "author_claim", "text": "未定位的主张。",
                              "evidence": [{"fragment_id": "missing", "page_number": 2, "quote": "made up"}]}])
    result = actions.dispatch(finder, "papers_notes", {"paper_id": PAPER_ID, "reading": card}, service)
    assert result["data"]["card"]["claims"][0]["kind"] == "unverified"
    assert result["data"]["card"]["claims"][0]["evidence"] == []
    assert service.calls[-1][1]["strict"] is False and service.calls[-1][1]["origin"] == "calling_agent"
    for bad in (empty_card(file_path="C:/secret.pdf"), empty_card(paper_id="paper-" + "c" * 24), None):
        before = len(service.calls)
        with pytest.raises(JournalError):
            actions.dispatch(finder, "papers_notes", {"paper_id": PAPER_ID, "reading": bad}, service)
        assert len(service.calls) == before


def test_default_service_home_and_constructor_are_inert(finder, monkeypatch, tmp_path):
    monkeypatch.delenv("PAPERFLOW_PAPER_HOME", raising=False)
    service = actions.literature_service(finder)
    assert service.store.directory == finder.store.directory / "papers"
    assert actions.dispatch(finder, "papers_list")["data"] == {"papers": [], "total": 0}
    assert not finder.store.directory.exists()
    home = tmp_path / "custom-paper-home"
    monkeypatch.setenv("PAPERFLOW_PAPER_HOME", str(home))
    assert actions.literature_service(finder).store.directory == home.resolve()
    assert actions.dispatch(finder, "papers_list")["data"]["papers"] == []
    assert not home.exists()


def test_server_constructor_preserves_old_signature_and_is_lazy(finder, monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError("create_app/old actions must not construct a literature service")
    monkeypatch.setattr(server, "literature_service", unavailable)
    app = server.create_app(TOKEN, PORT, finder, SecretStore(), MockModel())
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        assert post(client, "/api/action", {"action": "overview"}).status_code == 200
        assert client.get("/").status_code == 200
    assert not finder.store.directory.exists()


def test_shared_dispatch_does_not_import_gui_model_path(tmp_path):
    script = """
import sys
from paperflow.engine.journal_finder import JournalFinder
from paperflow.gui.actions import dispatch
finder = JournalFinder(sys.argv[1])
assert dispatch(finder, 'papers_list')['data']['papers'] == []
assert 'paperflow.gui.paper_reading' not in sys.modules
assert 'paperflow.gui.model_scoring' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "isolated")],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path,payload", [("/api/action", {"action": "papers_read", "params": {"paper_id": PAPER_ID}}),
                                         ("/api/model/read-paper", {"paper_id": PAPER_ID, "consent": True})])
def test_literature_routes_preserve_host_token_guards(http, path, payload):
    client, service, _, model = http
    for token in (None, "wrong"):
        result = post(client, path, payload, token=token)
        assert result.status_code == 403 and result.json()["error_code"] == "FORBIDDEN"
    result = post(client, path, payload, host="attacker.example:5123")
    assert result.status_code == 403 and result.json()["error_code"] == "FORBIDDEN_HOST"
    assert not service.calls and not model.calls


def test_http_actions_query_pagination_and_path_boundary(http):
    client, service, _, _ = http
    result = post(client, "/api/action", {"action": "papers_search", "params": {"query": "robots", "sources": ["crossref"], "year_from": 2020}})
    assert result.status_code == 200 and result.json()["status"] == "partial"
    result = post(client, "/api/action", {"action": "papers_read", "params": {"paper_id": PAPER_ID, "page_count": 1, "max_chars": 1000}})
    cursor = result.json()["data"]["next_cursor"]
    result = post(client, "/api/action", {"action": "papers_read", "params": {"paper_id": PAPER_ID, "page_count": 1, "max_chars": 1000, **cursor}})
    assert result.json()["data"]["pages"][0]["offset_start"] == 1000
    before = len(service.calls)
    for params in ({"path": "C:/secret.pdf"}, {"url": "https://attacker.example/x.pdf"}, {"project_path": "C:/secret"}):
        result = post(client, "/api/action", {"action": "papers_download", "params": {"paper_id": PAPER_ID, **params}})
        assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    for invalid in ([], "path", False):
        assert post(client, "/api/action", {"action": "papers_list", "params": invalid}).status_code == 400
    assert len(service.calls) == before and all(not c[2] for c in service.calls)


@pytest.mark.parametrize("consent", [None, False, "yes", 1])
def test_model_route_requires_explicit_boolean_consent(http, consent):
    client, service, store, model = http
    store.config = model_config()
    payload = {"paper_id": PAPER_ID}
    if consent is not None:
        payload["consent"] = consent
    result = post(client, "/api/model/read-paper", payload)
    assert result.status_code == 400 and result.json()["error_code"] == "CONSENT_REQUIRED"
    assert not service.calls and not model.calls


def test_model_not_configured_and_persistent_consent(http):
    client, service, store, model = http
    result = post(client, "/api/model/read-paper", {"paper_id": PAPER_ID, "consent": True})
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_NOT_CONFIGURED"
    store.config = model_config(consented=False)
    result = post(client, "/api/model/read-paper", {"paper_id": PAPER_ID, "consent": True})
    assert result.json()["error_code"] == "CONSENT_REQUIRED"
    assert not service.calls and not model.calls


@pytest.mark.parametrize("extra", [{"text": "frontend substitute"}, {"fragments": []}, {"file_path": "C:/secret.pdf"},
                                   {"url": "https://attacker.example/x.pdf"}, {"origin": "calling_agent"}, {"unknown": 1}])
def test_model_route_rejects_frontend_evidence_and_unknown_parameters(http, extra):
    client, service, store, model = http
    store.config = model_config()
    result = post(client, "/api/model/read-paper", {"paper_id": PAPER_ID, "consent": True, **extra})
    assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    assert not service.calls and not model.calls


def test_model_reads_backend_selection_and_downgrades_unsupported_claims(http):
    client, service, store, model = http
    store.config = model_config()
    cursor = {"paper_id": PAPER_ID, "page_number": 1, "page_count": 1, "offset": 150, "max_chars": 1000}
    result = post(client, "/api/model/read-paper", {**cursor, "consent": True})
    assert result.status_code == 200, result.text
    envelope = result.json()
    assert service.calls[0][0] == "read" and service.calls[0][1] == cursor
    assert service.calls[1][0] == "save_reading" and service.calls[1][1]["strict"] is False
    assert envelope["data"]["origin"] == "gui_model" and envelope["data"]["backend_calls_llm"] is True
    assert envelope["coverage"]["backend_calls_llm"] is True
    assert envelope["data"]["card"]["paper_id"] == PAPER_ID
    assert envelope["data"]["card"]["file_sha256"] == SHA
    claims = envelope["data"]["card"]["claims"]
    assert claims[0]["kind"] == "author_claim"
    assert all(c["kind"] == "unverified" and c["evidence"] == [] for c in claims[1:])
    assert envelope["data"]["selection"]["next_cursor"] == {"page_number": 1, "offset": 1150}
    prompt = model.calls[0][2]["messages"][-1]["content"]
    selection = json.loads(prompt.split("本次选择段落：\n", 1)[1])
    assert selection["fragments"][0]["text"] == service.texts[0][150:1150]
    assert "SECOND_PAGE_NOT_SELECTED" not in prompt and "ABSTRACT_NOT_FOR_MODEL" not in prompt
    assert service.paper["title"] not in prompt and len(selection["fragments"][0]["text"]) == 1000
    assert model.calls[0][1]["Authorization"] == "Bearer " + SECRET
    assert not model.calls[0][3] and all(not c[2] for c in service.calls)
    assert SECRET not in result.text


def test_model_cannot_cite_other_previously_read_page():
    service = FakeService()
    old = service.read(PAPER_ID, page_number=2, page_count=1, max_chars=1000)["data"]["fragments"][0]
    evidence = {"fragment_id": old["fragment_id"], "page_number": 2, "quote": old["text"][:24]}
    result = paper_reading.read_with_model(service, model_config(), PAPER_ID, page_count=1,
                                           max_chars=1000, transport=MockModel(extra_evidence=evidence))
    claim = result["data"]["card"]["claims"][-1]
    assert claim["kind"] == "unverified" and claim["evidence"] == []
    assert result["data"]["model_reading"]["downgraded_claims"] == [1]


@pytest.mark.parametrize("provider,base_url", [("openai_compatible", "http://127.0.0.1:11434/v1"),
                                             ("anthropic", "https://model.example")])
def test_reading_reuses_provider_transport_and_supports_keyless_local_model(provider, base_url):
    model = MockModel(provider=provider)
    config = model_config(provider=provider, base_url=base_url, key="" if provider == "openai_compatible" else SECRET)
    result = paper_reading.read_with_model(FakeService(), config, PAPER_ID, page_count=1, max_chars=1000, transport=model)
    assert result["data"]["origin"] == "gui_model"
    if provider == "anthropic":
        assert model.calls[0][0].endswith("/v1/messages") and model.calls[0][1]["x-api-key"] == SECRET
    else:
        assert model.calls[0][0].endswith("/chat/completions") and "Authorization" not in model.calls[0][1]


def test_scanned_page_and_missing_fulltext_never_call_model(http):
    client, service, store, model = http
    store.config = model_config()
    result = post(client, "/api/model/read-paper", {"paper_id": PAPER_ID, "page_number": 3, "page_count": 1, "consent": True})
    assert result.status_code == 400 and result.json()["error_code"] == "NO_EXTRACTABLE_TEXT"
    service.paper["acquisition"]["status"] = "unavailable"
    result = post(client, "/api/model/read-paper", {"paper_id": PAPER_ID, "consent": True})
    assert result.json()["error_code"] == "FULLTEXT_REQUIRED" and not model.calls
    assert all(call[0] != "save_reading" for call in service.calls)


def test_model_errors_redact_existing_secret(http):
    client, service, store, _ = http
    store.config = model_config()
    def fail(url, headers, body):
        raise RuntimeError("unauthorized key " + SECRET)
    app = server.create_app(TOKEN, PORT, store=store, transport=fail, literature=service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as other:
        result = post(other, "/api/model/read-paper", {"paper_id": PAPER_ID, "page_count": 1, "max_chars": 1000, "consent": True})
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_ERROR"
    assert SECRET not in result.text
    assert not service.paper["reading_cards"]


def test_legacy_reviews_scoring_and_dispatch_are_off_event_loop(http, monkeypatch):
    client, service, store, model = http
    from paperflow.gui import review_searcher
    marks = []
    def reviews(finder, query, store=None, transport=None):
        marks.append(("reviews", in_event_loop(), transport is model))
        return response({"query": query})
    def score(finder, config, text, mode="auto", preferences=None, transport=None):
        marks.append(("score", in_event_loop(), transport is model))
        return response({"text": text})
    monkeypatch.setattr(review_searcher, "search_journal_reviews", reviews)
    monkeypatch.setattr(model_scoring, "score", score)
    store.config = model_config()
    assert post(client, "/api/action", {"action": "search_reviews", "params": {"query": "Sensors"}}).status_code == 200
    assert post(client, "/api/model/score", {"text": "existing idea"}).status_code == 200
    assert post(client, "/api/action", {"action": "papers_list"}).status_code == 200
    assert marks == [("reviews", False, True), ("score", False, True)]
    assert service.calls[-1][0] == "list" and service.calls[-1][2] is False
    before = len(marks)
    assert post(client, "/api/action", {"action": "search_reviews", "params": {"query": "Sensors", "file_path": "C:/secret"}}).status_code == 400
    assert len(marks) == before


def test_page_keeps_existing_tabs_styles_csp_and_no_pdf_upload(http):
    client, _, _, _ = http
    result = client.get("/")
    assert result.status_code == 200
    assert "default-src 'none'" in result.headers["content-security-policy"]
    assert "connect-src 'self'" in result.headers["content-security-policy"]
    assert result.headers["x-frame-options"] == "DENY"
    html = result.text
    for tab in ("overview", "search", "details", "compare", "recommend", "literature"):
        assert f'id="tab-{tab}"' in html and f'id="panel-{tab}"' in html
    assert 'data-tab="literature"' in html and "论文检索与阅读" in html
    assert 'id="rec-filter-cas" class="score-sort-group"' in html
    assert 'id="model-config-section"' in html and 'id="model-key"' in html
    panel = html.split('id="panel-literature"', 1)[1].split("<!-- Tab 5: 智能推荐 -->", 1)[0]
    assert 'type="file"' not in panel and "MCP / CLI import" in panel
    block = html.split("// --- Literature:", 1)[1].split("// --- Local Model Config", 1)[0]
    assert "innerHTML" not in block and "safeHTTPS" in block
    assert "cursor.offset = nextCursor.offset" in block and "currentCursor" in block
    assert "不自动上传整篇文件" in panel and "本版浏览器不上传 PDF" in panel
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403


def test_literature_javascript_safe_rendering_cursor_and_model_payload():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for executable front-end checks")
    html = (ROOT / "paperflow/gui/static/journal_studio.html").read_text(encoding="utf-8")
    script = re.search(r"<script[^>]*>([\s\S]*?)</script>", html).group(1)
    names = ["el", "clearEl", "safeHTTPS", "papersCall", "paperNumber", "paperAvailable", "paperAcquisitionLabel",
             "renderPaperList", "setPaperBusy", "renderReadingCards", "renderPaperDetails", "paperCursor",
             "renderPaperRead", "readPaper", "syncModelConsent", "modelReadPaper"]
    functions = []
    for name in names:
        match = re.search(r"^      (?:async )?function " + name + r"\([^\n]*", script, re.M)
        following = re.search(r"\n      (?:async )?function |\n      // |\n      document\.getElementById", script[match.end():])
        functions.append(script[match.start():match.end() + following.start()])
    harness = r'''
const assert = require("assert");
class Node {
  constructor(tag) { this.tagName = tag; this.attrs = {}; this.children = []; this.style = {}; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set innerHTML(value) { throw Error("unsafe HTML sink"); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  get firstChild() { return this.children[0]; }
  addEventListener() {}
}
const nodes = new Map();
const document = {
  createElement: tag => new Node(tag), createTextNode: text => { const n = new Node("#text"); n.textContent = text; return n; },
  getElementById: id => { if (!nodes.has(id)) nodes.set(id, new Node("div")); return nodes.get(id); }
};
const paperState = { selectedId: "paper-" + "a".repeat(24), record: null, searchResults: [], libraryResults: [], currentCursor: null, readData: null, nextCursor: null, busy: false };
let transportMode = "local", localToken = "fixture-token";
const showToast = () => {};
const calls = [], modelPayloads = [];
const attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
const paper = { paper_id: paperState.selectedId, title: attack, authors: [attack], abstract: attack,
  landing_url: "javascript:alert(1)", year: 2024, acquisition: { status: "downloaded" }, reading_cards: [] };
paperState.record = paper;
const window = { call: async (action, params) => {
  calls.push({ action, params });
  if (action === "papers_details") return { status: "success", data: paper };
  const end = params.offset + 1000;
  return { status: "success", coverage: { total_pages: 3, fully_extracted_pages: [], text_complete: false,
    empty_or_unextractable_pages: [{page_number: 3}] }, data: { paper, total_pages: 3, file_sha256: "b".repeat(64),
    pages: [{page_number: params.page_number, offset_start: params.offset, offset_end: end, page_complete: false, text: attack}],
    fragments: [{fragment_id: "frag-current", page_number: params.page_number, start: params.offset, end, text: attack}],
    next_cursor: params.offset === 0 ? { page_number: 1, offset: 1000 } : null, agent_contract: {} } };
}};
const fetch = async (url, options) => {
  if (url.endsWith("/read-paper")) modelPayloads.push(JSON.parse(options.body));
  return { ok: true, json: async () => url.endsWith("/consent") ? {consented:true} : {status:"success", data:{origin:"gui_model", backend_calls_llm:true}} };
};
__FUNCTIONS__
(async () => {
  for (const bad of ["http://example.com", "javascript:alert(1)", "https://user:password@example.com", "//example.com"]) {
    assert.strictEqual(safeHTTPS(bad, attack).tagName, "span");
  }
  const link = safeHTTPS("https://example.com/paper", attack);
  assert.strictEqual(link.tagName, "a");
  assert.strictEqual(link.attrs.rel, "noopener noreferrer");
  assert.strictEqual(link.textContent, attack);
  renderPaperList(document.getElementById("papers-results"), [paper]);
  renderReadingCards([{origin:"gui_model", card:{summary:attack, claims:[{section:"method", kind:"unverified", text:attack, evidence:[{quote:attack}]}]}}]);
  function allChildren(node) { return [node, ...node.children.flatMap(allChildren)]; }
  assert(!allChildren(document.getElementById("papers-results")).some(n => ["script", "img"].includes(n.tagName)));
  assert(document.getElementById("papers-reading-cards").textContent.includes(attack));
  for (const [id, value] of Object.entries({"papers-page-number":"1", "papers-page-count":"1", "papers-offset":"0", "papers-max-chars":"1000"})) document.getElementById(id).value = value;
  await readPaper();
  assert.deepStrictEqual(paperState.nextCursor, {page_number:1, offset:1000});
  await readPaper(paperState.nextCursor);
  assert.strictEqual(calls[1].params.page_number, 1);
  assert.strictEqual(calls[1].params.offset, 1000);
  assert(document.getElementById("papers-coverage").textContent.includes("未完整提取页：1, 2, 3"));
  assert(document.getElementById("papers-read-status").textContent.includes("未完成阅读"));
  assert(document.getElementById("papers-cursor").textContent.includes("不能据此宣称理解完成"));
  document.getElementById("papers-model-consent").checked = true;
  document.getElementById("papers-offset").value = "9999"; // unsent edits must not replace the displayed selection
  await modelReadPaper();
  assert.strictEqual(modelPayloads.length, 1);
  assert.strictEqual(modelPayloads[0].offset, 1000);
  assert.strictEqual(modelPayloads[0].consent, true);
  assert.deepStrictEqual(Object.keys(modelPayloads[0]).sort(), ["consent","max_chars","offset","page_count","page_number","paper_id"].sort());
  console.log("literature DOM, HTTPS, same-page cursor and model payload passed");
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node], input=harness.replace("__FUNCTIONS__", "\n".join(functions)),
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
