"""Independent acceptance guards; every paper/PDF in this file is an offline fixture."""
from __future__ import annotations

from copy import deepcopy
import inspect
import socket

import pytest

from test_literature_writing_core import writing, selected_project, draft_for
from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.reading_loop_models import calculate_context_fingerprint
from paperflow.engine.literature.reading_loop_service import ReadingLoopService


@pytest.fixture(autouse=True)
def forbid_external_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline acceptance forbids external network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from paperflow.engine.literature import providers
    monkeypatch.setattr(providers.http, "safe_fetch", forbidden)


def data(result):
    assert result["status"] in ("success", "partial"), result
    assert isinstance(result.get("data"), dict), result
    return result["data"]


def started(writing, tmp_path):
    project = selected_project(writing, tmp_path)
    project = writing.save_draft(project["project_id"], draft_for(project), project["revision"])["data"]
    loop = ReadingLoopService(writing=writing)
    state = data(loop.control(project["project_id"], "start", 0, project["revision"]))
    return loop, project, state


def sufficient_review(project):
    ids = [r["citation_id"] for r in project["evidence_matrix"]]
    return {
        "dimensions": [{"dimension": dimension, "status": "sufficient",
                         "reason": "Offline fixture scope is explicitly supported by supplied source evidence.",
                         "question_ids": ["RQ1"], "section_ids": ["related_work"], "citation_ids": ids}
                        for dimension in ("sub_questions", "method_baselines", "contrary_findings", "draft_support")],
        "gaps": [], "decision": "stop", "reason": "This bounded fixture draft is supported; not scientific certification.",
        "examined_citation_ids": ids, "examined_section_ids": ["related_work"],
    }


@pytest.mark.parametrize("name", ["get", "control", "prepare_review", "submit_review", "step", "apply_feedback",
                                   "reserve_model_call", "settle_model_call"])
def test_public_keyword_contract_uses_project_id(name):
    assert "project_id" in inspect.signature(getattr(ReadingLoopService, name)).parameters


def test_real_writing_flat_data_prepare_is_readonly(writing, tmp_path):
    project = selected_project(writing, tmp_path)
    loop = ReadingLoopService(writing=writing)
    with writing.library.connection() as connection:
        before = set(r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    initial = data(loop.get(project_id=project["project_id"]))
    assert initial["loop_revision"] == 0 and initial["loop"]["status"] == "idle"
    prepared = data(loop.prepare_review(project_id=project["project_id"]))
    assert prepared["project"]["project_id"] == project["project_id"]
    assert len(prepared["context_fingerprint"]) == 64
    assert prepared["evidence_total"] >= 1
    with writing.library.connection() as connection:
        after = set(r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert before == after
    assert writing.get(project["project_id"])["data"]["revision"] == project["revision"]


@pytest.mark.parametrize("field", ["file_sha256", "text", "kind", "nested_quote", "nested_page"])
def test_fingerprint_includes_nested_body_not_only_citation_id(writing, tmp_path, field):
    project = selected_project(writing, tmp_path)
    original = deepcopy(project["evidence_matrix"])
    mutated = deepcopy(original)
    row = mutated[0]
    if field == "nested_quote":
        row["evidence"][0]["quote"] += " changed fixture source"
    elif field == "nested_page":
        row["evidence"][0]["page_number"] += 1
    elif field == "kind":
        row["kind"] = "agent_inference"
    elif field == "text":
        row["text"] += " changed meaning"
    else:
        row["file_sha256"] = "f" * 64
    assert calculate_context_fingerprint(project, original) != calculate_context_fingerprint(project, mutated)


@pytest.mark.parametrize("dimension_status", ["gap", "uncertain"])
def test_stop_cannot_claim_sufficient_with_open_dimension(writing, tmp_path, dimension_status):
    loop, project, state = started(writing, tmp_path)
    prepared = data(loop.prepare_review(project["project_id"]))
    review = sufficient_review(project)
    review["dimensions"][1]["status"] = dimension_status
    result = loop.submit_review(project["project_id"], review, prepared["context_fingerprint"],
                                prepared["project_revision"], prepared["loop_revision"])
    current = data(loop.get(project["project_id"]))
    assert current["loop"].get("stop_reason") != "evidence_sufficient"
    assert result["status"] == "error" or current["loop"]["status"] in ("paused", "awaiting_review", "stopped")


@pytest.mark.parametrize("category,stop_code", [("own_data", "needs_user_material"), ("manual_or_ocr", "needs_manual_review")])
def test_nonliterature_gap_is_not_false_quality_pass(writing, tmp_path, category, stop_code):
    loop, project, state = started(writing, tmp_path)
    prepared = data(loop.prepare_review(project["project_id"]))
    review = sufficient_review(project)
    review["gaps"] = [{"id": "G1", "category": category, "priority": "high", "question_ids": ["RQ1"],
                       "section_ids": ["related_work"], "reason": "Critical fixture material must be supplied.",
                       "expected_information": "Actual experiment or readable source page; not extra papers."}]
    result = loop.submit_review(project["project_id"], review, prepared["context_fingerprint"],
                                prepared["project_revision"], prepared["loop_revision"])
    current = data(loop.get(project["project_id"]))
    assert current["loop"].get("stop_reason") != "evidence_sufficient"
    if result["status"] != "error":
        assert current["loop"].get("stop_reason") == stop_code


def test_boolean_budget_rejected_without_creating_loop(writing, tmp_path):
    project = selected_project(writing, tmp_path)
    loop = ReadingLoopService(writing=writing)
    result = loop.control(project["project_id"], "start", 0, project["revision"], budget={"max_read_papers": True})
    assert result["status"] == "error"
    assert data(loop.get(project["project_id"]))["loop_revision"] == 0


def test_sufficient_review_is_caller_judgement_not_backend_certificate(writing, tmp_path):
    loop, project, state = started(writing, tmp_path)
    prepared = data(loop.prepare_review(project["project_id"]))
    result = loop.submit_review(project["project_id"], sufficient_review(project), prepared["context_fingerprint"],
                                prepared["project_revision"], prepared["loop_revision"])
    current = data(result)
    assert current["loop"]["status"] == "stopped"
    assert current["loop"]["stop_reason"] == "evidence_sufficient"
    assert result["coverage"].get("semantic_correctness_verified") is not True
    assert result["coverage"].get("backend_calls_llm") is not True


def supplement_case(writing, tmp_path, budget=None):
    from test_literature_service import pdf_bytes
    baseline = selected_project(writing, tmp_path)
    first_id = baseline["selected_paper_ids"][0]
    target = tmp_path / "additional-unread-fixture.pdf"
    target.write_bytes(pdf_bytes(["Fixture method B provides a distinct comparison. " * 180]))
    second_id = writing.literature.import_pdf(str(target), "Unread comparison fixture")["data"]["paper_id"]
    project = writing.create(baseline["profile"], paper_ids=[first_id, second_id])["data"]
    first = next(c for c in project["candidates"] if c["paper_id"] == first_id)
    from test_literature_writing_core import assessment
    project = writing.assess(project["project_id"], [assessment(first)], project["revision"],
                             selected_paper_ids=[first_id])["data"]
    project = writing.save_draft(project["project_id"], draft_for(project), project["revision"])["data"]
    loop = ReadingLoopService(writing=writing)
    state = data(loop.control(project["project_id"], "start", 0, project["revision"], budget=budget))
    prepared = data(loop.prepare_review(project["project_id"]))
    review = sufficient_review(project)
    review["decision"] = "continue"
    review["reason"] = "Read exactly the relevant comparison fixture, not a fixed number of papers."
    review["dimensions"][1]["status"] = "gap"
    review["gaps"] = [{"id": "G_compare", "category": "literature", "priority": "high",
                       "question_ids": ["RQ1"], "section_ids": ["related_work"],
                       "reason": "The comparison fixture has not yet been interpreted.",
                       "expected_information": "Its actual method, not imagined results.",
                       "read_paper_ids": [second_id]}]
    state = data(loop.submit_review(project["project_id"], review, prepared["context_fingerprint"],
                                    prepared["project_revision"], prepared["loop_revision"]))
    return loop, project, state, second_id


def approve_supplement(loop, project, state, second_id):
    action = state["loop"].get("next_action") or {}
    assert action.get("kind") == "assessment", state["loop"]
    candidate = next(c for c in state["project"]["candidates"] if c["paper_id"] == second_id)
    from test_literature_writing_core import assessment
    return data(loop.apply_feedback(project["project_id"], action["action_id"],
                {"kind": "assessment", "assessments": [assessment(candidate)],
                 "selected_paper_ids": state["project"]["selected_paper_ids"] + [second_id]},
                state["project_revision"], state["loop_revision"]))


def advance_to_text(loop, project, state):
    for _ in range(5):
        action = state["loop"].get("next_action") or {}
        if action.get("kind") == "interpretation":
            return state
        assert action.get("kind") in ("download", "read"), state["loop"]
        state = data(loop.step(project["project_id"], action["action_id"],
                               state["project_revision"], state["loop_revision"]))
    raise AssertionError("Fixture loop failed to reach bounded source interpretation")


def test_unjudged_candidate_is_assessed_before_read_and_old_selection_kept(writing, tmp_path):
    loop, project, state, second_id = supplement_case(writing, tmp_path)
    assert state["loop"]["usage"]["read_papers"] == 0
    state = approve_supplement(loop, project, state, second_id)
    assert set(state["project"]["selected_paper_ids"]) == set(project["selected_paper_ids"] + [second_id])
    assert any(a["paper_id"] == second_id for a in state["project"]["assessments"])


def test_read_budget_counts_actual_page_ranges_and_retains_same_page_cursor(writing, tmp_path):
    loop, project, state, second_id = supplement_case(writing, tmp_path,
                    {"max_read_papers": 1, "batch_size": 1, "max_text_chars": 3000, "max_pages": 1})
    state = approve_supplement(loop, project, state, second_id)
    state = advance_to_text(loop, project, state)
    action = state["loop"]["next_action"]
    material = action["material"]
    pages = material["pages"]
    actual = sum(p["offset_end"] - p["offset_start"] for p in pages)
    assert actual == state["loop"]["usage"]["text_chars"]
    assert actual <= 3000
    assert state["loop"]["usage"]["pages"] == len({p["page_number"] for p in pages}) == 1
    assert state["loop"]["usage"]["rounds"] >= 1
    assert state["loop"]["usage"]["read_papers"] == 1
    cursor = action["payload"].get("next_cursor")
    assert cursor and cursor["page_number"] == 1 and cursor["offset"] == pages[-1]["offset_end"]


def test_completed_read_action_replay_does_not_recount_or_reread(writing, tmp_path, monkeypatch):
    loop, project, state, second_id = supplement_case(writing, tmp_path)
    state = approve_supplement(loop, project, state, second_id)
    read_action = None
    for _ in range(5):
        action = state["loop"].get("next_action") or {}
        if action.get("kind") == "interpretation":
            break
        if action.get("kind") == "read":
            read_action = deepcopy(action)
        state = data(loop.step(project["project_id"], action["action_id"],
                               state["project_revision"], state["loop_revision"]))
    assert read_action and state["loop"]["status"] == "awaiting_interpretation"
    before = deepcopy(state["loop"]["usage"])
    def forbidden(*args, **kwargs):
        raise AssertionError("idempotent action replay must not re-read the PDF")
    monkeypatch.setattr(writing.literature, "read", forbidden)
    result = loop.step(project["project_id"], read_action["action_id"],
                       state["project_revision"], state["loop_revision"])
    assert result["status"] in ("success", "partial"), result
    assert data(loop.get(project["project_id"]))["loop"]["usage"] == before


def test_paused_interpretation_cannot_write_card_or_reset_usage(writing, tmp_path):
    loop, project, state, second_id = supplement_case(writing, tmp_path)
    state = approve_supplement(loop, project, state, second_id)
    state = advance_to_text(loop, project, state)
    action = deepcopy(state["loop"]["next_action"])
    material = action["material"]
    fragment = material["fragments"][0]
    card = {"paper_id": second_id, "file_sha256": material["file_sha256"], "summary": "Only this fixture range.",
            "claims": [{"section": "method", "kind": "author_claim", "text": "The fixture has a comparison method.",
                        "evidence": [{"fragment_id": fragment["fragment_id"], "page_number": fragment["page_number"],
                                      "quote": fragment["text"][:30]}]}]}
    paused = data(loop.control(project["project_id"], "pause", state["loop_revision"], state["project_revision"]))
    result = loop.apply_feedback(project["project_id"], action["action_id"],
                   {"kind": "interpretation", "reading": card, "read_more": False},
                   paused["project_revision"], paused["loop_revision"])
    assert result["status"] == "error"
    assert writing.library.get(second_id)["reading_cards"] == []
    resumed = data(loop.control(project["project_id"], "resume", paused["loop_revision"], paused["project_revision"]))
    assert resumed["loop"]["usage"] == paused["loop"]["usage"]


def test_inherited_partial_reading_is_not_reported_as_full_coverage(writing, tmp_path):
    from test_literature_service import pdf_bytes, card_for
    from test_literature_writing_core import profile, assessment
    file = tmp_path / "partially-read-baseline.pdf"
    file.write_bytes(pdf_bytes(["Fixture method described on page one.", "Unread fixture limitations on page two."]))
    paper_id = writing.literature.import_pdf(str(file), "Partial baseline fixture")["data"]["paper_id"]
    reading = writing.literature.read(paper_id, page_number=1, page_count=1)
    assert not reading["coverage"]["text_complete"]
    writing.literature.save_reading(paper_id, card_for(reading))
    project = writing.create(profile(), paper_ids=[paper_id])["data"]
    project = writing.assess(project["project_id"], [assessment(project["candidates"][0])], project["revision"])["data"]
    loop = ReadingLoopService(writing=writing)
    state = data(loop.control(project["project_id"], "start", 0, project["revision"]))
    baseline = state["loop"]["inherited_baseline"]
    assert baseline["read_papers"] == [paper_id]
    assert baseline["partially_read"] == [paper_id]
    assert state["loop"]["usage"]["read_papers"] == 0


def test_new_more_reading_request_reopens_review_and_preserves_other_limits(writing, tmp_path):
    loop, project, state = started(writing, tmp_path)
    limited = data(loop.control(project["project_id"], "update_budget", state["loop_revision"], state["project_revision"],
                  budget={"max_pages": 2, "max_text_chars": 3000, "max_gui_model_calls": 1}))
    prepared = data(loop.prepare_review(project["project_id"]))
    old_fingerprint = prepared["context_fingerprint"]
    stopped = data(loop.submit_review(project["project_id"], sufficient_review(project), old_fingerprint,
                                     prepared["project_revision"], prepared["loop_revision"]))
    assert stopped["loop"]["stop_reason"] == "evidence_sufficient"
    more = data(loop.control(project["project_id"], "update_budget", stopped["loop_revision"], stopped["project_revision"],
                  budget={"max_read_papers": 15}, request="Please strengthen the limitations discussion with up to three more relevant papers."))
    assert more["loop"]["status"] == "awaiting_review"
    assert more["loop"]["stop_reason"] is None
    assert more["loop"]["budget"]["max_read_papers"] == 15
    assert more["loop"]["budget"]["max_pages"] == 2
    assert more["loop"]["budget"]["max_text_chars"] == 3000
    assert more["loop"]["budget"]["max_gui_model_calls"] == 1
    assert more["loop"]["usage"] == stopped["loop"]["usage"]
    assert more["loop"]["inherited_baseline"] == stopped["loop"]["inherited_baseline"]
    current = data(loop.prepare_review(project["project_id"]))
    assert current["context_fingerprint"] != old_fingerprint
    stale = loop.submit_review(project["project_id"], sufficient_review(project), old_fingerprint,
                               current["project_revision"], current["loop_revision"])
    assert stale["status"] == "error"
    rejected_state = data(loop.get(project["project_id"]))["loop"]
    assert rejected_state["status"] == "paused"
    assert rejected_state["stop_reason"] == "context_changed"


def test_explicit_resume_after_stop_requires_fresh_review_not_usage_reset(writing, tmp_path):
    loop, project, state = started(writing, tmp_path)
    prepared = data(loop.prepare_review(project["project_id"]))
    stopped = data(loop.submit_review(project["project_id"], sufficient_review(project), prepared["context_fingerprint"],
                                     prepared["project_revision"], prepared["loop_revision"]))
    resumed = data(loop.control(project["project_id"], "resume", stopped["loop_revision"], stopped["project_revision"]))
    assert resumed["loop"]["status"] == "awaiting_review"
    assert resumed["loop"]["next_action"]["kind"] == "review"
    assert resumed["loop"]["usage"] == stopped["loop"]["usage"]
    assert len(resumed["loop"]["reviews"]) == len(stopped["loop"]["reviews"])


def test_fingerprint_tracks_unselected_candidate_current_pdf_version(writing, tmp_path):
    from test_literature_service import pdf_bytes
    loop, project, state, second_id = supplement_case(writing, tmp_path)
    before = data(loop.prepare_review(project["project_id"]))["context_fingerprint"]
    paper = writing.library.get(second_id)
    new_sha = writing.library.cache_pdf(pdf_bytes(["Different fixture PDF version, not a searched paper."]))
    writing.library.set_acquisition(second_id, {**paper["acquisition"], "file_sha256": new_sha})
    after = data(loop.prepare_review(project["project_id"]))["context_fingerprint"]
    assert before != after
