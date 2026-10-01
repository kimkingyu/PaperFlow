"""Writing GUI contracts and explicit model stages, with no live API, Key or Word."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import io
import json
import socket
import urllib.request
from zipfile import ZipFile

import pytest
from starlette.testclient import TestClient

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, response
from paperflow.engine.literature.writing_models import (
    WritingProfile, QueryBatch, AssessmentBatch, WritingDraft,
)
from paperflow.gui import actions, model_scoring, paper_writing, server
from paperflow.gui.secrets import ModelConfig, SecretStore

TOKEN, PORT = "t" * 43, 5123
KEY = "sk-WRITING-FIXTURE-NOT-A-REAL-KEY-0123456789"
PROJECT_ID = "writing-" + "1" * 24
PAPER_ID = "paper-" + "a" * 24
CITATION_ID = "cite-" + "b" * 24
SHA = "c" * 64
ATTACK = '<script>alert("untrusted")</script>'
TEXT = "怎样在低算力下改善水下机器人的定位？"


def in_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Writing GUI tests must not call a real API or user Key")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(model_scoring, "_http_transport", forbidden)
    monkeypatch.setattr("paperflow.gui.secrets._keyring", lambda: None)
    monkeypatch.delenv("PAPERFLOW_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PAPERFLOW_PAPER_HOME", raising=False)


def config(consented=True):
    return ModelConfig(provider="openai_compatible", base_url="https://fixture.invalid/v1",
                       model="offline-fixture", consented=consented, _session_key=KEY)


def profile():
    return {"title": "低算力定位研究", "research_question": TEXT, "context": "尚未进行实验",
            "language": "zh", "sub_questions": [{"id": "RQ1", "question": TEXT}], "keywords": [], "own_materials": []}


def query():
    return {"id": "Q1", "query": "underwater localization resource constrained", "purpose": "寻找同类方法与低算力基线",
            "question_ids": ["RQ1"], "sources": ["crossref", "arxiv"], "year_from": None, "year_to": None}


def assessment(ident=PAPER_ID):
    return {"paper_id": ident, "metadata_sha256": SHA, "relevance": "core",
            "reason": "同为水下定位方法，可用来比较资源受限条件下的基线。",
            "question_ids": ["RQ1"], "basis": "abstract", "limitations": ["只读摘要，未核实正文语义"], "evidence_ids": []}


def evidence(ident=CITATION_ID, paper_id=PAPER_ID):
    return {"citation_id": ident, "paper_id": paper_id, "reading_id": "reading-fixture", "claim_index": 0,
            "file_sha256": SHA, "section": "method", "kind": "author_claim", "text": "作者报告资源受限定位方法。",
            "evidence": [{"fragment_id": "frag-" + "d" * 24, "page_number": 1, "quote": "bounded real fixture evidence"}]}


def draft():
    return {"title": "低算力定位研究", "language": "zh",
            "outline": [{"id": "related_work", "title": "相关工作", "level": 1, "purpose": "对比已读方法", "evidence_ids": [CITATION_ID]}],
            "sections": [{"id": "related_work", "title": "相关工作", "level": 1,
                          "paragraphs": [{"text": "已有方法面向受限定位。", "kind": "literature_summary", "citation_ids": [CITATION_ID], "own_material_ids": []}]}]}


def docx_bytes():
    from docx import Document
    document = Document()
    document.add_paragraph("Offline literature-supported draft")
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


class FakeWritingService:
    def __init__(self):
        self.calls = []
        self.project = {"project_id": PROJECT_ID, "revision": 1, "profile": profile(), "queries": [query()],
                        "candidates": [{"paper_id": PAPER_ID, "metadata_sha256": SHA,
                                        "paper": {"title": ATTACK, "abstract": "Underwater resource constrained localization.",
                                                  "authors": [ATTACK], "doi": "10.1000/fixture", "year": 2024,
                                                  "backend_private_path": "WHOLE_PDF_OR_PRIVATE_PATH_MUST_NOT_SEND"}, "matched_query_ids": ["Q1"]}],
                        "assessments": [assessment()], "selected_paper_ids": [PAPER_ID], "draft": None,
                        "stage": "evidence_ready", "search_statuses": [], "evidence_matrix": [evidence()], "gaps": [], "agent_contract": {},
                        "export_ready": False, "draft_valid": False}
        self.export_bytes = docx_bytes()

    def called(self, name, **params):
        self.calls.append((name, deepcopy(params), in_loop()))

    def result(self, evidence_offset=0, evidence_limit=40):
        result = deepcopy(self.project)
        result.update(evidence_offset=evidence_offset, evidence_limit=evidence_limit,
                      evidence_total=len(result["evidence_matrix"]))
        result["evidence_matrix"] = result["evidence_matrix"][evidence_offset:evidence_offset + evidence_limit]
        result["evidence_next_offset"] = evidence_offset + evidence_limit if evidence_offset + evidence_limit < result["evidence_total"] else None
        return response(result, coverage={"backend_calls_llm": False, "semantic_correctness_verified": False})

    def prepare(self, text):
        self.called("prepare", text=text)
        return response({"input_id": "input-" + hashlib.sha256(text.encode()).hexdigest(), "text": text,
                         "profile_schema": WritingProfile.model_json_schema(), "query_schema": QueryBatch.model_json_schema(),
                         "assessment_schema": AssessmentBatch.model_json_schema(), "draft_schema": WritingDraft.model_json_schema(), "agent_contract": {}})

    def check(self, expected_revision):
        if expected_revision != self.project["revision"]:
            raise JournalError("REVISION_CONFLICT", "最新项目 revision 已改变")

    def create(self, profile, queries=None, paper_ids=None, source_text="", input_id=""):
        self.called("create", profile=profile, queries=queries, paper_ids=paper_ids, source_text=source_text, input_id=input_id)
        self.project.update(profile=deepcopy(profile), queries=deepcopy(queries or []))
        return self.result()

    def search(self, project_id, expected_revision, queries=None, per_query_limit=10):
        self.called("search", project_id=project_id, expected_revision=expected_revision, queries=queries, per_query_limit=per_query_limit)
        self.check(expected_revision)
        self.project["revision"] += 1
        self.project["search_statuses"] = [{"query_id": "Q1", "source_id": "crossref", "status": "success"},
                                            {"query_id": "Q1", "source_id": "arxiv", "status": "error", "error_code": "FIXTURE_UNAVAILABLE"}]
        result = self.result()
        result["status"] = "partial"
        return result

    def assess(self, project_id, assessments, expected_revision, selected_paper_ids=None, origin="calling_agent"):
        self.called("assess", project_id=project_id, assessments=assessments, expected_revision=expected_revision,
                    selected_paper_ids=selected_paper_ids, origin=origin)
        self.check(expected_revision)
        AssessmentBatch.model_validate({"assessments": assessments})
        self.project["assessments"] = deepcopy(assessments)
        if selected_paper_ids is not None:
            self.project["selected_paper_ids"] = selected_paper_ids
        self.project["revision"] += 1
        return self.result()

    def get(self, project_id, revision=None, evidence_offset=0, evidence_limit=40):
        self.called("get", project_id=project_id, revision=revision, evidence_offset=evidence_offset, evidence_limit=evidence_limit)
        return self.result(evidence_offset, evidence_limit)

    def list(self, limit=20, offset=0):
        self.called("list", limit=limit, offset=offset)
        return response({"projects": [{"project_id": PROJECT_ID, "revision": self.project["revision"], "title": ATTACK}] if offset == 0 else [], "total": 1})

    def prepare_writing(self, project_id, evidence_offset=0, evidence_limit=30):
        self.called("prepare_writing", project_id=project_id, evidence_offset=evidence_offset, evidence_limit=evidence_limit)
        result = self.result(evidence_offset, evidence_limit)
        result["data"].update(draft_schema=WritingDraft.model_json_schema(), writing_materials={"backend_private_path": "WHOLE_PDF_MUST_NOT_SEND"})
        return result

    def save_draft(self, project_id, draft, expected_revision, change_note="更新文献支持草稿"):
        self.called("save_draft", project_id=project_id, draft=draft, expected_revision=expected_revision, change_note=change_note)
        self.check(expected_revision)
        WritingDraft.model_validate(draft)
        self.project.update(draft=deepcopy(draft), stage="draft_ready", export_ready=True, draft_valid=True)
        self.project["revision"] += 1
        return self.result()

    def render_docx(self, project_id, revision=None):
        self.called("render_docx", project_id=project_id, revision=revision)
        return self.export_bytes


class FakeModel:
    def __init__(self, change=None, fail_phase=None):
        self.calls = []
        self.change, self.fail_phase = change, fail_phase

    def __call__(self, url, headers, body):
        self.calls.append((url, deepcopy(headers), deepcopy(body), in_loop()))
        payload = json.loads(body["messages"][-1]["content"])
        phase = payload["phase"]
        if self.fail_phase == phase:
            return {"choices": [{"message": {"content": ""}}]}
        if phase == "search_plan":
            result = {"profile": profile(), "queries": [query()]}
            result["profile"]["own_materials"] = [{"id": "UM_FAKE", "text": "模型假造实验成功率 99%"}]
        elif phase == "relevance":
            result = {"assessments": [assessment(row["paper_id"]) for row in payload["candidates"]]}
            for item in result["assessments"]:
                item["metadata_sha256"] = "f" * 64  # replaced by backend-owned fingerprint
        elif phase == "outline":
            result = {"outline": draft()["outline"]}
            result["outline"][0]["evidence_ids"] = [row["citation_id"] for row in payload["evidence_summaries"]]
        elif phase == "draft_sections":
            result = {"sections": []}
            for chapter in payload["chapters"]:
                paragraph = {"text": "基于本批证据组织文献讨论。", "kind": "literature_summary",
                             "citation_ids": chapter["evidence_ids"], "own_material_ids": []}
                result["sections"].append({"id": chapter["id"], "title": chapter["title"], "level": chapter["level"], "paragraphs": [paragraph]})
        else:
            raise AssertionError(phase)
        if self.change:
            result = self.change(phase, result)
        return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}


@pytest.fixture
def http(tmp_path):
    finder = JournalFinder(str(tmp_path / "journals"))
    service, model, store = FakeWritingService(), FakeModel(), SecretStore()
    store.config = config()
    app = server.create_app(TOKEN, PORT, finder=finder, store=store, transport=model, writing=service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield client, service, store, model, finder


def post(client, path, payload, token=TOKEN, host=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-PaperFlow-Token"] = token
    if host is not None:
        headers["Host"] = host
    return client.post(path, content=json.dumps(payload), headers=headers)


def stage_params(stage):
    return {"plan": {"text": TEXT, "own_materials": ["用户实际提供的实验约束"], "consent": True},
            "assess": {"project_id": PROJECT_ID, "expected_revision": 1, "candidate_ids": [PAPER_ID], "consent": True},
            "draft": {"project_id": PROJECT_ID, "expected_revision": 1, "citation_ids": [CITATION_ID], "consent": True}}[stage]


@pytest.mark.parametrize("stage", ["plan", "assess", "draft"])
@pytest.mark.parametrize("consent", [None, False, 1, "true"])
def test_model_stages_require_exact_request_consent(http, stage, consent):
    client, service, _, model, _ = http
    payload = stage_params(stage)
    payload["consent"] = consent
    result = post(client, "/api/model/writing-" + stage, payload)
    assert result.status_code == 400 and result.json()["error_code"] == "CONSENT_REQUIRED"
    assert not model.calls and not service.calls


@pytest.mark.parametrize("stage", ["plan", "assess", "draft"])
@pytest.mark.parametrize("setting,code", [("absent", "MODEL_NOT_CONFIGURED"), ("not_consented", "CONSENT_REQUIRED")])
def test_no_config_or_config_consent_never_sends_payload(http, stage, setting, code):
    client, service, store, model, _ = http
    store.config = ModelConfig() if setting == "absent" else config(False)
    result = post(client, "/api/model/writing-" + stage, stage_params(stage))
    assert result.status_code == 400 and result.json()["error_code"] == code
    assert not model.calls and not service.calls


@pytest.mark.parametrize("path,payload", [("/api/model/writing-plan", stage_params("plan")),
    ("/api/model/writing-assess", stage_params("assess")), ("/api/model/writing-draft", stage_params("draft")),
    ("/api/writing/download", {"project_id": PROJECT_ID, "revision": 1})])
def test_writing_http_token_host_and_post_guards(http, path, payload):
    client, service, _, model, _ = http
    assert post(client, path, payload, token=None).status_code == 403
    assert post(client, path, payload, token="wrong").status_code == 403
    assert post(client, path, payload, host="evil.invalid").json()["error_code"] == "FORBIDDEN_HOST"
    assert client.get(path, headers={"X-PaperFlow-Token": TOKEN}).status_code == 405
    assert not service.calls and not model.calls


def test_model_plan_schema_actual_user_materials_and_no_writes(http):
    client, service, _, model, _ = http
    result = post(client, "/api/model/writing-plan", stage_params("plan"))
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["preview_only"] is True and data["profile"]["research_question"] == TEXT
    assert data["profile"]["own_materials"] == [{"id": "UM1", "text": "用户实际提供的实验约束"}]
    assert "模型假造实验" not in result.text and KEY not in result.text
    assert {call[0] for call in service.calls} == {"prepare"}
    payload = json.loads(model.calls[0][2]["messages"][-1]["content"])
    assert "profile_schema" in payload and "query_schema" in payload
    assert model.calls[0][3] is False and service.calls[0][2] is False
    assert "不可信数据" in model.calls[0][2]["messages"][0]["content"]
    assert result.json()["coverage"]["semantic_correctness_verified"] is False


def test_relevance_batches_backend_fingerprints_and_whole_file_not_sent(http):
    client, service, _, model, _ = http
    ids = ["paper-" + f"{index:024x}" for index in range(13)]
    service.project["candidates"] = [{**deepcopy(service.project["candidates"][0]), "paper_id": ident} for ident in ids]
    service.project["candidates"][0]["paper"]["abstract"] = "A" * 6000 + "ABSTRACT_TAIL_NOT_SENT"
    payload = stage_params("assess"); payload["candidate_ids"] = ids
    result = post(client, "/api/model/writing-assess", payload)
    assert result.status_code == 200, result.text
    data, coverage = result.json()["data"], result.json()["coverage"]
    assert len(data["assessments"]) == 13 and coverage["model_calls"] == 3
    assert all(row["metadata_sha256"] == SHA for row in data["assessments"])
    assert len(model.calls) == 3 and all(call[3] is False for call in model.calls)
    requests = "".join(json.dumps(call[2], ensure_ascii=False) for call in model.calls)
    assert "WHOLE_PDF" not in requests and "PRIVATE_PATH" not in requests and "ABSTRACT_TAIL_NOT_SENT" not in requests
    assert all(len(json.loads(call[2]["messages"][-1]["content"])["candidates"]) <= 6 for call in model.calls)
    assert any("只发送前 4000" in warning for warning in result.json()["warnings"])
    assert not any(call[0] in ("assess", "save_draft") for call in service.calls)


@pytest.mark.parametrize("fault", ["wrong_id", "duplicate", "fulltext", "unknown_question", "empty_reason"])
def test_bad_model_assessment_is_atomic_and_retryable(http, fault):
    client, service, _, model, _ = http
    before = deepcopy(service.project)
    def change(phase, raw):
        if phase == "relevance":
            if fault == "wrong_id": raw["assessments"][0]["paper_id"] = "paper-" + "e" * 24
            if fault == "duplicate": raw["assessments"] *= 2
            if fault == "fulltext": raw["assessments"][0].update(basis="fulltext", evidence_ids=[CITATION_ID])
            if fault == "unknown_question": raw["assessments"][0]["question_ids"] = ["RQ_FAKE"]
            if fault == "empty_reason": raw["assessments"][0]["reason"] = ""
        return raw
    model.change = change
    result = post(client, "/api/model/writing-assess", stage_params("assess"))
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_ERROR"
    assert service.project == before
    model.change = None
    assert post(client, "/api/model/writing-assess", stage_params("assess")).status_code == 200


def test_draft_model_sections_evidence_selection_and_backend_identity(http):
    client, service, _, model, _ = http
    service.project["draft"] = draft()
    result = post(client, "/api/model/writing-draft", stage_params("draft"))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["draft"]["sections"][0]["paragraphs"][0]["citation_ids"] == [CITATION_ID]
    assert result.json()["coverage"]["model_calls"] == 2
    sent = [json.loads(call[2]["messages"][-1]["content"]) for call in model.calls]
    assert [item["phase"] for item in sent] == ["outline", "draft_sections"]
    assert sent[1]["evidence"][0]["reading_id"] == "reading-fixture"
    assert "WHOLE_PDF" not in json.dumps(sent) and "backend_private_path" not in json.dumps(sent)
    assert service.project["draft"] == draft() and service.project["revision"] == 1
    assert not any(call[0] == "save_draft" for call in service.calls)


@pytest.mark.parametrize("fault", ["fake_citation", "fake_material", "fake_outline", "inference_as_author", "invented_results",
                                  "section_title", "section_level", "empty_placeholder", "blank_placeholder", "manual_number", "control_character", "placeholder_reference"])
def test_invalid_draft_references_and_materials_keep_old_draft(http, fault):
    client, service, _, model, _ = http
    service.project["draft"] = draft()
    if fault == "inference_as_author": service.project["evidence_matrix"][0]["kind"] = "agent_inference"
    before = deepcopy(service.project)
    def change(phase, raw):
        if phase == "outline" and fault == "fake_outline": raw["outline"][0]["evidence_ids"] = ["cite-" + "e" * 24]
        if phase == "draft_sections":
            paragraph = raw["sections"][0]["paragraphs"][0]
            if fault == "fake_citation": paragraph["citation_ids"] = ["cite-" + "e" * 24]
            if fault == "fake_material": paragraph.update(kind="user_material", own_material_ids=["UM_FAKE"])
            if fault == "invented_results": raw["sections"][0]["title"] = "实验结果"
            if fault == "section_title": raw["sections"][0]["title"] = "另一个标题"
            if fault == "section_level": raw["sections"][0]["level"] = 2
            if fault in ("empty_placeholder", "blank_placeholder"):
                paragraph.update(text="" if fault == "empty_placeholder" else "   ", kind="placeholder", citation_ids=[])
            if fault == "manual_number": paragraph["text"] = "已有方法报告结果[99]。"
            if fault == "control_character": paragraph["text"] = "含非法控制字符\u0000"
            if fault == "placeholder_reference": paragraph["kind"] = "placeholder"
        return raw
    model.change = change
    result = post(client, "/api/model/writing-draft", stage_params("draft"))
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_ERROR", result.text
    assert service.project == before and not any(call[0] == "save_draft" for call in service.calls)


@pytest.mark.parametrize("fault", ["unverified", "wrong_paper", "no_hash", "invalid"])
def test_unusable_evidence_never_goes_to_model(http, fault):
    client, service, _, model, _ = http
    row = service.project["evidence_matrix"][0]
    if fault == "unverified": row["kind"] = "unverified"
    if fault == "wrong_paper": row["paper_id"] = "paper-" + "f" * 24
    if fault == "no_hash": row["file_sha256"] = ""
    if fault == "invalid": row["valid"] = False
    result = post(client, "/api/model/writing-draft", stage_params("draft"))
    assert result.status_code == 400 and result.json()["error_code"] == "INVALID_EVIDENCE"
    assert not model.calls


@pytest.mark.parametrize("stage,phase", [("plan", "search_plan"), ("assess", "relevance"), ("draft", "draft_sections")])
def test_empty_model_stage_does_not_save_partial_project(http, stage, phase):
    client, service, _, model, _ = http
    before = deepcopy(service.project)
    model.fail_phase = phase
    result = post(client, "/api/model/writing-" + stage, stage_params(stage))
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_ERROR"
    assert service.project == before
    model.fail_phase = None
    assert post(client, "/api/model/writing-" + stage, stage_params(stage)).status_code == 200


@pytest.mark.parametrize("stage", ["assess", "draft"])
def test_stale_revision_is_recoverable_without_model_call(http, stage):
    client, service, _, model, _ = http
    service.project["revision"] = 2
    result = post(client, "/api/model/writing-" + stage, stage_params(stage))
    assert result.status_code == 400 and result.json()["error_code"] == "REVISION_CONFLICT"
    assert not model.calls
    fresh = post(client, "/api/action", {"action": "writing_get", "params": {"project_id": PROJECT_ID}})
    assert fresh.json()["data"]["revision"] == 2
    payload = stage_params(stage); payload["expected_revision"] = 2
    assert post(client, "/api/model/writing-" + stage, payload).status_code == 200


def test_changed_revision_during_batched_model_is_atomic(http):
    client, service, _, model, _ = http
    def change(phase, raw):
        if phase == "relevance": service.project["revision"] += 1
        return raw
    model.change = change
    result = post(client, "/api/model/writing-assess", stage_params("assess"))
    assert result.status_code == 400 and result.json()["error_code"] == "REVISION_CONFLICT"
    assert not any(call[0] == "assess" for call in service.calls)


def test_budget_signal_instead_of_silent_evidence_drop(http):
    client, service, _, model, _ = http
    service.project["evidence_matrix"][0]["text"] = "LARGE_EVIDENCE" * 6000
    result = post(client, "/api/model/writing-draft", stage_params("draft"))
    assert result.status_code == 400 and result.json()["error_code"] == "MODEL_BUDGET_EXCEEDED"
    assert not any(call[0] == "save_draft" for call in service.calls)


def test_model_failure_redacts_dummy_secret(http, monkeypatch):
    client, service, _, _, _ = http
    def failing(*args):
        raise JournalError("MODEL_ERROR", "cannot connect with " + KEY)
    monkeypatch.setattr(model_scoring, "_chat", failing)
    result = post(client, "/api/model/writing-plan", stage_params("plan"))
    assert result.status_code == 400 and KEY not in result.text
    assert not any(call[0] == "create" for call in service.calls)


@pytest.mark.parametrize("action,params", [
    ("writing_prepare", {"text": TEXT}), ("writing_create", {"profile": profile(), "queries": [query()], "source_text": TEXT}),
    ("writing_search", {"project_id": PROJECT_ID, "expected_revision": 1, "queries": [query()], "per_query_limit": 8}),
    ("writing_assess", {"project_id": PROJECT_ID, "expected_revision": 1, "assessments": [assessment()], "selected_paper_ids": [PAPER_ID]}),
    ("writing_get", {"project_id": PROJECT_ID, "evidence_offset": 0, "evidence_limit": 30}),
    ("writing_list", {"limit": 5, "offset": 0}), ("writing_materials", {"project_id": PROJECT_ID}),
    ("writing_draft", {"project_id": PROJECT_ID, "expected_revision": 1, "draft": draft()})])
def test_shared_writing_actions_have_no_model_and_http_workers(http, action, params):
    client, service, _, model, finder = http
    assert action in actions.PAPER_ACTIONS and action in actions.PAPER_ACTION_PARAMS
    direct = actions.dispatch(finder, action, params, writing=service)
    assert direct["status"] != "error" and not model.calls
    service.project["revision"] = 1; service.calls.clear()
    result = post(client, "/api/action", {"action": action, "params": params})
    assert result.status_code == 200, result.text
    assert service.calls and all(call[2] is False for call in service.calls) and not model.calls


@pytest.mark.parametrize("field", ["output_path", "file_path", "path", "pdf_path", "url", "model_config", "api_key"])
def test_writing_actions_download_and_models_reject_paths_and_extra_params(http, field):
    client, service, _, model, _ = http
    result = post(client, "/api/action", {"action": "writing_get", "params": {"project_id": PROJECT_ID, field: "C:/private.docx"}})
    assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    for path, payload in [("/api/model/writing-plan", stage_params("plan")), ("/api/writing/download", {"project_id": PROJECT_ID})]:
        result = post(client, path, {**payload, field: "C:/private.docx"})
        assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    assert not model.calls and not service.calls


@pytest.mark.parametrize("changes", [{"expected_revision": True}, {"expected_revision": "1"}, {"expected_revision": None},
                                   {"per_query_limit": 0}, {"per_query_limit": 11}, {"queries": [query()] * 7}])
def test_writing_strict_revision_and_boundaries(http, changes):
    client, service, _, model, _ = http
    result = post(client, "/api/action", {"action": "writing_search", "params": {"project_id": PROJECT_ID, "expected_revision": 1, **changes}})
    assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    assert not service.calls and not model.calls


def test_nested_path_unknown_action_and_invalid_json_body_are_rejected(http):
    client, service, _, model, _ = http
    result = post(client, "/api/action", {"action": "writing_draft", "params": {"project_id": PROJECT_ID, "expected_revision": 1,
                                                                           "draft": {**draft(), "output_path": "C:/file.docx"}}})
    assert result.status_code == 400
    assert post(client, "/api/action", {"action": "writing_model", "params": {}}).status_code == 400
    assert post(client, "/api/model/writing-unknown", {"consent": True}).status_code == 400
    assert client.post("/api/writing/download", content="{}", headers={"X-PaperFlow-Token": TOKEN}).status_code == 400
    assert not model.calls and not service.calls


def test_authenticated_docx_bytes_not_a_backend_path(http):
    client, service, _, model, _ = http
    result = post(client, "/api/writing/download", {"project_id": PROJECT_ID, "revision": 1})
    assert result.status_code == 200
    assert result.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert result.headers["cache-control"] == "no-store" and "attachment" in result.headers["content-disposition"]
    assert result.content == service.export_bytes
    assert "word/document.xml" in ZipFile(io.BytesIO(result.content)).namelist()
    assert service.calls[-1] == ("render_docx", {"project_id": PROJECT_ID, "revision": 1}, False)
    assert not model.calls


def test_real_core_api_integration_inert_prepare_create_list(tmp_path):
    from paperflow.engine.literature.writing_service import PaperWritingService
    finder = JournalFinder(str(tmp_path / "journals"))
    core = PaperWritingService(data_dir=str(tmp_path / "papers"))
    prepared = actions.dispatch(finder, "writing_prepare", {"text": TEXT}, writing=core)
    assert prepared["data"]["query_schema"]["properties"]["queries"]
    model = FakeModel()
    preview = paper_writing.plan_with_model(core, config(), TEXT, own_materials=["真实输入的约束"], consent=True, transport=model)
    created = actions.dispatch(finder, "writing_create", {key: preview["data"][key] for key in ("profile", "queries", "source_text", "input_id")}, writing=core)
    ident = created["data"]["project_id"]
    assert created["data"]["profile"]["own_materials"] == [{"id": "UM1", "text": "真实输入的约束"}]
    assert created["data"]["stage"] == "needs_agent_assessment"
    assert actions.dispatch(finder, "writing_get", {"project_id": ident}, writing=core)["data"]["revision"] == 1
    assert actions.dispatch(finder, "writing_list", writing=core)["data"]["total"] == 1
    materials = actions.dispatch(finder, "writing_materials", {"project_id": ident}, writing=core)
    assert materials["data"]["evidence_total"] == 0 and materials["coverage"]["backend_calls_llm"] is False


@pytest.mark.parametrize("revision", [None, True, "1", 0])
def test_download_requires_explicit_saved_revision(http, revision):
    client, service, _, model, _ = http
    result = post(client, "/api/writing/download", {"project_id": PROJECT_ID, "revision": revision})
    assert result.status_code == 400 and result.json()["error_code"] == "INVALID_INPUT"
    assert not service.calls and not model.calls


def test_real_core_selected_reading_model_draft_save_and_docx_http(tmp_path):
    from test_literature_service import FakeProviders, card_for, pdf_bytes
    from paperflow.engine.literature.service import LiteratureService
    from paperflow.engine.literature.writing_service import PaperWritingService
    literature = LiteratureService(str(tmp_path / "library"), providers=FakeProviders())
    fixture = tmp_path / "synthetic-paper.pdf"
    fixture.write_bytes(pdf_bytes(["A reproducible underwater method. Resource constrained baseline."]))
    paper_id = literature.import_pdf(str(fixture), "Offline underwater fixture")["data"]["paper_id"]
    reading = literature.read(paper_id)
    literature.save_reading(paper_id, card_for(reading))
    core = PaperWritingService(literature=literature)
    initial = core.create(profile(), paper_ids=[paper_id])["data"]
    finder, store, model = JournalFinder(str(tmp_path / "journals")), SecretStore(), FakeModel()
    store.config = config()
    def metadata_basis(phase, raw):
        if phase == "relevance":
            for row in raw["assessments"]:
                row["basis"] = "metadata"  # imported fixture has no abstract
        return raw
    model.change = metadata_basis
    app = server.create_app(TOKEN, PORT, finder=finder, store=store, transport=model, literature=literature, writing=core)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        judged = post(client, "/api/model/writing-assess", {"project_id": initial["project_id"], "expected_revision": 1,
                                                           "candidate_ids": [paper_id], "consent": True})
        assert judged.status_code == 200, judged.text
        selected = post(client, "/api/action", {"action": "writing_assess", "params": {"project_id": initial["project_id"], "expected_revision": 1,
                               "assessments": judged.json()["data"]["assessments"], "selected_paper_ids": [paper_id]}})
        assert selected.status_code == 200, selected.text
        data = selected.json()["data"]
        assert data["stage"] == "evidence_ready" and data["evidence_total"] == 1
        citations = [row["citation_id"] for row in data["evidence_matrix"]]
        generated = post(client, "/api/model/writing-draft", {"project_id": data["project_id"], "expected_revision": data["revision"],
                                                               "citation_ids": citations, "consent": True})
        assert generated.status_code == 200, generated.text
        saved = post(client, "/api/action", {"action": "writing_draft", "params": {"project_id": data["project_id"], "expected_revision": data["revision"],
                                                "draft": generated.json()["data"]["draft"]}})
        assert saved.status_code == 200, saved.text
        saved_data = saved.json()["data"]
        assert saved_data["revision"] == 3 and saved_data["export_ready"] is True
        downloaded = post(client, "/api/writing/download", {"project_id": saved_data["project_id"], "revision": saved_data["revision"]})
        assert downloaded.status_code == 200, downloaded.text
        assert "word/document.xml" in ZipFile(io.BytesIO(downloaded.content)).namelist()
    assert not literature.providers.calls
