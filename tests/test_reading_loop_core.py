"""Comprehensive core and integration tests for adaptive literature reading loop service."""
import copy
import socket
import pytest

from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.models import PaperRecord, paper_id_for
from paperflow.engine.literature.reading_loop_models import (
    BUDGET_DEFAULTS, BUDGET_HARD_LIMITS,
    FeedbackAssessment, FeedbackInterpretation, FeedbackRevision,
    LoopBudget, LoopUsage, ReadingLoopState, WritingReview,
    calculate_context_fingerprint
)
from paperflow.engine.literature.reading_loop_service import ReadingLoopService
from paperflow.engine.literature.service import LiteratureService
from paperflow.engine.literature.writing_models import WritingDraft, WritingParagraph, WritingSection
from paperflow.engine.literature.writing_service import PaperWritingService
from test_literature_service import FakeProviders, card_for, imported, pdf_bytes, record


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reading loop core tests forbid real network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from paperflow.engine.literature import providers
    monkeypatch.setattr(providers.http, "safe_fetch", forbidden)


@pytest.fixture
def writing_svc(tmp_path):
    literature = LiteratureService(str(tmp_path / "library"), providers=FakeProviders())
    return PaperWritingService(literature=literature)


@pytest.fixture
def loop_svc(writing_svc):
    return ReadingLoopService(writing=writing_svc)


def create_sample_project(writing_svc, tmp_path, count=2):
    """Synthetic local project fixture with reproducible local PDFs."""
    paper_ids = []
    for i in range(count):
        file = tmp_path / f"fixture-synthetic-paper-{i}.pdf"
        file.write_bytes(pdf_bytes([
            f"Reproducible Method {i} establishes high accuracy in edge deployment.",
            f"Baseline comparisons for model {i} prove consistent latency bounds."
        ]))
        imported_res = writing_svc.literature.import_pdf(str(file), f"Synthetic Paper {i}")
        pid = imported_res["data"]["paper_id"]
        # Save reading for first paper only to establish baseline
        if i == 0:
            reading = card_for(writing_svc.literature.read(pid))
            writing_svc.literature.save_reading(pid, reading)
        paper_ids.append(pid)

    profile = {
        "title": "Adaptive Reading for Edge Deployment",
        "research_question": "Which reproducible architectures satisfy edge latency?",
        "sub_questions": [{"id": "RQ1", "question": "Which reproducible architectures satisfy edge latency?"}],
    }
    created = writing_svc.create(profile, paper_ids=paper_ids)["data"]
    proj_id = created["project_id"]

    # Assess candidate 0 as core so it becomes selected
    c0 = created["candidates"][0]
    assessed = writing_svc.assess(
        proj_id,
        [{"paper_id": c0["paper_id"], "metadata_sha256": c0["metadata_sha256"],
          "relevance": "core", "reason": "Base method", "question_ids": ["RQ1"], "basis": "metadata"}],
        created["revision"],
        selected_paper_ids=[paper_ids[0]]
    )["data"]

    # Initial draft referencing paper 0
    prep = writing_svc.prepare_writing(proj_id, 0, 10)["data"]
    cid = prep["evidence_matrix"][0]["citation_id"]
    draft = {
        "title": profile["title"],
        "language": "zh",
        "outline": [{"id": "O1", "title": "引言与基准", "level": 1, "purpose": "基准介绍", "evidence_ids": [cid]}],
        "sections": [{
            "id": "sec-intro",
            "title": "1. 现有基准",
            "level": 1,
            "paragraphs": [{
                "text": "现有研究提出了一种基于边缘的基准架构。",
                "kind": "literature_summary",
                "citation_ids": [cid],
                "own_material_ids": []
            }]
        }]
    }
    writing_svc.save_draft(proj_id, draft, assessed["revision"])
    return proj_id, paper_ids


def test_budget_fields_validation_and_strict_bool():
    # Valid default budget
    b = LoopBudget()
    assert b.max_read_papers == BUDGET_DEFAULTS["max_read_papers"]

    # Strict rejection of boolean masquerading as int
    with pytest.raises(Exception):
        LoopBudget(max_read_papers=True)

    with pytest.raises(Exception):
        LoopBudget(max_rounds=False)

    # Rejection of exceeding hard limits
    with pytest.raises(Exception):
        LoopBudget(max_read_papers=BUDGET_HARD_LIMITS["max_read_papers"] + 1)

    with pytest.raises(Exception):
        LoopBudget(max_text_chars=100)  # below minimum 1000


def test_service_guard_desensitization(loop_svc, monkeypatch):
    """Ensures raw traces, secrets, and system paths never leak into error message."""
    def crash(*args, **kwargs):
        raise RuntimeError("sk-test-secret-value-leak /Users/admin/sensitive/path")

    monkeypatch.setattr(loop_svc, "_get_project_data", crash)
    res = loop_svc.get("writing-0123456789abcdef01234567")
    assert res["status"] == "error"
    assert res["error_code"] == "LOOP_SERVICE_ERROR"
    assert "sk-test-secret" not in res["message"]
    assert "sensitive" not in res["message"]
    assert res["message"] == "自适应阅读循环发生内部错误，请稍后重试"


def test_context_fingerprint_sensitivity():
    """Validates fingerprint changes whenever nested evidence quote or sha changes."""
    base_project = {
        "project_id": "writing-0123456789abcdef01234567",
        "revision": 1,
        "profile": {"title": "Title", "research_question": "RQ1"},
        "candidates": [{"paper_id": "paper-1", "metadata_sha256": "m" * 64}],
        "selected_paper_ids": ["paper-1"],
        "draft": None,
    }
    base_ev = [{
        "citation_id": "cite-0123456789abcdef01234567",
        "paper_id": "paper-1",
        "reading_id": "reading-1",
        "file_sha256": "f" * 64,
        "section": "sec",
        "kind": "author_claim",
        "text": "Claim text A",
        "evidence": [{"fragment_id": "frag-1", "page_number": 1, "quote": "Quote text A"}]
    }]
    paper_states = {"paper-1": {"metadata_sha256": "m" * 64, "file_sha256": "f" * 64}}

    fp1 = calculate_context_fingerprint(base_project, base_ev, paper_states)

    # 1. Same citation_id but body text changed
    ev2 = copy.deepcopy(base_ev)
    ev2[0]["text"] = "Claim text B changed"
    fp2 = calculate_context_fingerprint(base_project, ev2, paper_states)
    assert fp1 != fp2

    # 2. Same citation_id but nested quote changed
    ev3 = copy.deepcopy(base_ev)
    ev3[0]["evidence"][0]["quote"] = "Quote text B changed"
    fp3 = calculate_context_fingerprint(base_project, ev3, paper_states)
    assert fp1 != fp3

    # 3. Paper SHA changed
    states4 = {"paper-1": {"metadata_sha256": "m" * 64, "file_sha256": "different_sha" * 4}}
    fp4 = calculate_context_fingerprint(base_project, base_ev, states4)
    assert fp1 != fp4


def test_control_lifecycle_and_inherited_baseline(loop_svc, writing_svc, tmp_path):
    proj_id, paper_ids = create_sample_project(writing_svc, tmp_path, count=2)

    # 1. Idle query
    idle_res = loop_svc.get(proj_id)
    assert idle_res["status"] == "success"
    assert idle_res["data"]["loop_revision"] == 0
    assert idle_res["data"]["loop"]["status"] == "idle"

    # 2. Start loop: inherited baseline correctly counts paper 0
    proj_rev = idle_res["data"]["project_revision"]
    start_res = loop_svc.control(proj_id, action="start", expected_loop_revision=0, expected_project_revision=proj_rev)
    assert start_res["status"] == "success"
    loop_data = start_res["data"]["loop"]
    assert loop_data["status"] == "awaiting_review"
    assert loop_data["loop_revision"] == 1
    assert paper_ids[0] in loop_data["inherited_baseline"]["read_papers"]
    assert loop_data["usage"]["read_papers"] == 0  # inherited does not count towards usage

    # 3. Pause
    pause_res = loop_svc.control(proj_id, action="pause", expected_loop_revision=1, expected_project_revision=proj_rev)
    assert pause_res["status"] == "success"
    assert pause_res["data"]["loop"]["status"] == "paused"
    assert pause_res["data"]["loop"]["stop_reason"] == "paused_by_user"

    # 4. Resume
    resume_res = loop_svc.control(proj_id, action="resume", expected_loop_revision=2, expected_project_revision=proj_rev)
    assert resume_res["status"] == "success"
    assert resume_res["data"]["loop"]["status"] == "awaiting_review"
    assert resume_res["data"]["loop"]["stop_reason"] is None

    # 5. Update budget
    b_update = {"max_read_papers": 20, "max_rounds": 8}
    bud_res = loop_svc.control(proj_id, action="update_budget", expected_loop_revision=3, expected_project_revision=proj_rev, budget=b_update)
    assert bud_res["status"] == "success"
    assert bud_res["data"]["loop"]["budget"]["max_read_papers"] == 20
    assert bud_res["data"]["loop"]["budget"]["max_rounds"] == 8

    # 6. Stop
    stop_res = loop_svc.control(proj_id, action="stop", expected_loop_revision=4, expected_project_revision=proj_rev, request="用户测试手动停止")
    assert stop_res["status"] == "success"
    assert stop_res["data"]["loop"]["status"] == "stopped"
    assert stop_res["data"]["loop"]["stop_reason"] == "stopped_by_user"


def test_prepare_review_and_context_changed_detection(loop_svc, writing_svc, tmp_path):
    proj_id, paper_ids = create_sample_project(writing_svc, tmp_path, count=2)
    start_res = loop_svc.control(proj_id, "start", 0, writing_svc.get(proj_id)["data"]["revision"])
    loop_rev = start_res["data"]["loop_revision"]

    prep = loop_svc.prepare_review(proj_id, evidence_offset=0, evidence_limit=10)
    assert prep["status"] == "success"
    data = prep["data"]
    fp = data["context_fingerprint"]
    assert len(fp) == 64
    assert "review_schema" in data
    assert "feedback_schema" in data
    assert "evidence_matrix" in data

    # External modification occurs before submit_review
    writing_svc.save_draft(proj_id, {
        "title": "Modified Title Outside Loop",
        "language": "zh",
        "sections": data["project"]["draft"]["sections"]
    }, data["project_revision"], change_note="External edit")

    # submit_review should detect fingerprint mismatch and fail safely
    sample_review = {
        "dimensions": [
            {"dimension": "sub_questions", "status": "sufficient", "reason": "RQ1 addressed", "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": []},
            {"dimension": "method_baselines", "status": "sufficient", "reason": "Baselines present", "question_ids": [], "section_ids": ["sec-intro"], "citation_ids": []},
            {"dimension": "contrary_findings", "status": "not_applicable", "reason": "None", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "draft_support", "status": "sufficient", "reason": "Support ok", "question_ids": [], "section_ids": ["sec-intro"], "citation_ids": []},
        ],
        "gaps": [],
        "decision": "stop",
        "reason": "Sufficient",
        "examined_citation_ids": [data["evidence_matrix"][0]["citation_id"]],
        "examined_section_ids": ["sec-intro"]
    }
    sub_res = loop_svc.submit_review(proj_id, sample_review, fp, expected_project_revision=data["project_revision"], expected_loop_revision=loop_rev)
    assert sub_res["status"] == "error"
    # Revision conflict or context changed handled safely
    assert sub_res["error_code"] in ("CONTEXT_CHANGED", "REVISION_CONFLICT")


def test_gui_model_ticket_reservation_and_settlement(loop_svc, writing_svc, tmp_path):
    proj_id, _ = create_sample_project(writing_svc, tmp_path, count=1)
    loop_svc.control(proj_id, "start", 0, writing_svc.get(proj_id)["data"]["revision"])
    cur_loop = loop_svc.get(proj_id)["data"]["loop"]
    act_id = cur_loop["next_action"]["action_id"]

    cur_proj_rev = writing_svc.get(proj_id)["data"]["revision"]
    # 1. Reserve model ticket
    res_ticket = loop_svc.reserve_model_call(proj_id, act_id, cur_proj_rev, 1)
    assert res_ticket["status"] == "success"
    ticket_id = res_ticket["data"]["model_ticket"]
    assert ticket_id.startswith("ticket-")
    assert res_ticket["data"]["loop"]["reserved"]["gui_model_calls"] == 1

    # 2. Settle model ticket (failure still counts towards attempts)
    settle_res = loop_svc.settle_model_call(proj_id, act_id, ticket_id, success=False)
    assert settle_res["status"] == "success"
    assert settle_res["data"]["loop"]["reserved"]["gui_model_calls"] == 0
    assert settle_res["data"]["loop"]["usage"]["gui_model_calls"] == 1


def test_two_round_dynamic_reading_and_revision_loop(loop_svc, writing_svc, tmp_path):
    """End-to-end multi-round test:

    Round 1: Review identifies gap -> Read second paper -> Submit reading card -> Revise draft.
    Round 2: Review verifies sufficiency -> Stops with evidence_sufficient.
    """
    proj_id, paper_ids = create_sample_project(writing_svc, tmp_path, count=2)
    init_rev = writing_svc.get(proj_id)["data"]["revision"]
    start = loop_svc.control(proj_id, "start", 0, init_rev)
    loop_rev = start["data"]["loop_revision"]

    # --- ROUND 1: PREPARE & SUBMIT REVIEW (CONTINUE) ---
    prep1 = loop_svc.prepare_review(proj_id)
    fp1 = prep1["data"]["context_fingerprint"]
    proj_rev1 = prep1["data"]["project_revision"]

    review1 = {
        "dimensions": [
            {"dimension": "sub_questions", "status": "gap", "reason": "缺少第二种架构的对比分析", "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": []},
            {"dimension": "method_baselines", "status": "sufficient", "reason": "基准完备", "question_ids": [], "section_ids": ["sec-intro"], "citation_ids": []},
            {"dimension": "contrary_findings", "status": "not_applicable", "reason": "暂无相反结论", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "draft_support", "status": "gap", "reason": "草稿尚未包含对比", "question_ids": [], "section_ids": ["sec-intro"], "citation_ids": []},
        ],
        "gaps": [{
            "id": "G1",
            "category": "literature",
            "priority": "high",
            "question_ids": ["RQ1"],
            "section_ids": ["sec-intro"],
            "reason": "需要读取第二篇候选文献获取对比数据",
            "expected_information": "获取模型 1 的吞吐与延迟指标",
            "read_paper_ids": [paper_ids[1]]
        }],
        "decision": "continue",
        "reason": "定向补充阅读第二篇文献",
        "examined_citation_ids": [prep1["data"]["evidence_matrix"][0]["citation_id"]],
        "examined_section_ids": ["sec-intro"]
    }
    sub1 = loop_svc.submit_review(proj_id, review1, fp1, proj_rev1, loop_rev)
    assert sub1["status"] == "success"
    loop_data1 = sub1["data"]["loop"]
    assert loop_data1["status"] == "awaiting_assessment"
    act_assess = loop_data1["next_action"]["action_id"]
    loop_rev = loop_data1["loop_revision"]

    # Assessment feedback: assess paper 1 as core and add to selected
    c1 = next(c for c in prep1["data"]["project"]["candidates"] if c["paper_id"] == paper_ids[1])
    fb_assess = {
        "kind": "assessment",
        "assessments": [{"paper_id": paper_ids[1], "metadata_sha256": c1["metadata_sha256"],
                         "relevance": "core", "reason": "对比架构", "question_ids": ["RQ1"], "basis": "metadata"}],
        "selected_paper_ids": [paper_ids[0], paper_ids[1]]
    }
    apply_assess = loop_svc.apply_feedback(proj_id, act_assess, fb_assess, proj_rev1, loop_rev)
    assert apply_assess["status"] == "success"
    loop_data1 = apply_assess["data"]["loop"]
    proj_rev1 = apply_assess["data"]["project_revision"]
    assert loop_data1["status"] == "ready"
    assert loop_data1["next_action"]["kind"] == "download"
    act_dl = loop_data1["next_action"]["action_id"]
    loop_rev = loop_data1["loop_revision"]

    # Step: Download paper 1
    step_dl = loop_svc.step(proj_id, act_dl, proj_rev1, loop_rev)
    assert step_dl["status"] == "success"
    loop_data_dl = step_dl["data"]["loop"]
    assert loop_data_dl["next_action"]["kind"] == "read"
    act_read = loop_data_dl["next_action"]["action_id"]
    loop_rev = loop_data_dl["loop_revision"]

    # Step: Read paper 1
    step_read = loop_svc.step(proj_id, act_read, proj_rev1, loop_rev)
    assert step_read["status"] == "success"
    loop_data_read = step_read["data"]["loop"]
    assert loop_data_read["status"] == "awaiting_interpretation"
    act_interp = loop_data_read["next_action"]["action_id"]
    loop_rev = loop_data_read["loop_revision"]
    material = loop_data_read["next_action"]["material"]

    # Feedback: Interpretation reading card
    reading_card = card_for({"data": {
        "paper_id": paper_ids[1],
        "file_sha256": material["file_sha256"],
        "pages": material["pages"],
        "fragments": material["fragments"]
    }})
    reading_card["claims"][0]["kind"] = "author_claim"
    reading_card["claims"][0]["text"] = "Reproducible Method 1 establishes high accuracy in edge deployment."
    fb_interp = {
        "kind": "interpretation",
        "reading": reading_card,
        "read_more": False
    }
    apply_interp = loop_svc.apply_feedback(proj_id, act_interp, fb_interp, proj_rev1, loop_rev)
    assert apply_interp["status"] == "success"
    loop_data_interp = apply_interp["data"]["loop"]
    assert loop_data_interp["status"] == "awaiting_revision"
    act_rev = loop_data_interp["next_action"]["action_id"]
    loop_rev = loop_data_interp["loop_revision"]

    # Feedback: Revise draft referencing both papers
    fresh_prep = loop_svc.prepare_review(proj_id)
    ev_matrix_fresh = fresh_prep["data"]["evidence_matrix"]
    all_cids = [ev["citation_id"] for ev in ev_matrix_fresh]
    assert len(all_cids) >= 2

    revised_draft = {
        "title": "Adaptive Reading for Edge Deployment",
        "language": "zh",
        "outline": [
            {"id": "O1", "title": "引言与基准", "level": 1, "purpose": "基准介绍", "evidence_ids": [all_cids[0]]},
            {"id": "O2", "title": "对比实验", "level": 1, "purpose": "方法对比", "evidence_ids": [all_cids[1]]}
        ],
        "sections": [
            {
                "id": "sec-intro",
                "title": "1. 现有基准",
                "level": 1,
                "paragraphs": [{
                    "text": "现有研究提出了一种基于边缘的基准架构。",
                    "kind": "literature_summary",
                    "citation_ids": [all_cids[0]],
                    "own_material_ids": []
                }]
            },
            {
                "id": "sec-comp",
                "title": "2. 对比架构分析",
                "level": 1,
                "paragraphs": [{
                    "text": "第二种可复现架构展示了更高的边缘吞吐表现。",
                    "kind": "literature_summary",
                    "citation_ids": [all_cids[1]],
                    "own_material_ids": []
                }]
            }
        ]
    }
    fb_revision = {
        "kind": "revision",
        "draft": revised_draft,
        "change_note": "引入第二篇对比架构正文段落"
    }
    proj_rev_interp = apply_interp["data"]["project_revision"]
    apply_rev = loop_svc.apply_feedback(proj_id, act_rev, fb_revision, proj_rev_interp, loop_rev)
    assert apply_rev["status"] == "success"
    loop_data_rev = apply_rev["data"]["loop"]
    assert loop_data_rev["status"] == "awaiting_review"
    assert loop_data_rev["round_index"] == 2
    loop_rev = loop_data_rev["loop_revision"]
    proj_rev2 = apply_rev["data"]["project_revision"]

    # --- ROUND 2: REVIEW PASSES WITH EVIDENCE SUFFICIENT ---
    prep2 = loop_svc.prepare_review(proj_id)
    fp2 = prep2["data"]["context_fingerprint"]

    review2 = {
        "dimensions": [
            {"dimension": "sub_questions", "status": "sufficient", "reason": "RQ1 两种对比架构均已获得充分支撑", "question_ids": ["RQ1"], "section_ids": ["sec-intro", "sec-comp"], "citation_ids": all_cids},
            {"dimension": "method_baselines", "status": "sufficient", "reason": "基准对比充分", "question_ids": [], "section_ids": ["sec-intro", "sec-comp"], "citation_ids": all_cids},
            {"dimension": "contrary_findings", "status": "not_applicable", "reason": "无未解决相反结论", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "draft_support", "status": "sufficient", "reason": "草稿各章节均已引用对应正文证据", "question_ids": [], "section_ids": ["sec-intro", "sec-comp"], "citation_ids": all_cids},
        ],
        "gaps": [],
        "decision": "stop",
        "reason": "关键缺口全部解决，正文论据充分支撑草稿",
        "examined_citation_ids": all_cids,
        "examined_section_ids": ["sec-intro", "sec-comp"]
    }
    sub2 = loop_svc.submit_review(proj_id, review2, fp2, proj_rev2, loop_rev)
    assert sub2["status"] == "success"
    final_loop = sub2["data"]["loop"]
    assert final_loop["status"] == "stopped"
    assert final_loop["stop_reason"] == "evidence_sufficient"
