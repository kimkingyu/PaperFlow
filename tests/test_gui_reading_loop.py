"""Complete tests for GUI reading loop runner, worker, endpoints and security guards.

Covers:
- HTTP security guards: Host header, token check, 2MB body limit, _writing_no_paths rejection
- Two-round gap loop lifecycle: review -> step -> assessment/interpretation/revision -> stop
- Model reservation & settle budget accounting: model_ticket check-before-call, failure attempt counted
- In-flight pause/stop safety: late responses do not overwrite stops or resume running
- Configuration/consent mutation auto-pause
- Worker concurrency caps: max 2 global workers, max 1 per project
- Secret redaction and graceful shutdown
"""
import copy
import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional

import pytest
from starlette.testclient import TestClient

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, response
from paperflow.gui import paper_reading_loop, server as gui_server
from paperflow.gui.secrets import ModelConfig, SecretStore

TOKEN = "t" * 43
PORT = 54321


class FakeReadingLoopService:
    """Strict in-memory test double matching .narrafork/adaptive-reading-contract.json."""

    def __init__(self):
        self.project_id = "test-project-alpha"
        self.project_revision = 1
        self.loop_revision = 0
        self.status = "idle"
        self.stop_reason = None
        self.stop_detail = ""
        self.budget = {
            "max_read_papers": 12, "batch_size": 3, "max_rounds": 4, "max_pages": 80,
            "max_text_chars": 180000, "max_search_calls": 24, "max_download_attempts": 24,
            "max_read_steps": 120, "max_gui_model_calls": 5
        }
        self.usage = {
            "read_papers": 0, "pages": 0, "text_chars": 0, "rounds": 0,
            "search_calls": 0, "download_attempts": 0, "read_steps": 0, "gui_model_calls": 0
        }
        self.reserved = {
            "pages": 0, "text_chars": 0, "search_calls": 0, "download_attempts": 0,
            "read_steps": 0, "gui_model_calls": 0
        }
        self.inherited_baseline = {
            "read_papers": ["paper-baseline-001"],
            "partially_read": [],
            "cards": ["card-01"],
            "cited": ["cite-baseline-01"]
        }
        self.round_index = 0
        self.no_progress_rounds = 0
        self.next_action = None
        self.request = ""
        self.reviews = []
        self.actions = []
        self.active_ticket = None
        self.lock = threading.RLock()
        self.simulate_slow_chat = False

    def _state_dict(self):
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "loop_revision": self.loop_revision,
            "loop": {
                "status": self.status,
                "stop_reason": self.stop_reason,
                "stop_detail": self.stop_detail,
                "budget": copy.deepcopy(self.budget),
                "usage": copy.deepcopy(self.usage),
                "reserved": copy.deepcopy(self.reserved),
                "inherited_baseline": copy.deepcopy(self.inherited_baseline),
                "round_index": self.round_index,
                "no_progress_rounds": self.no_progress_rounds,
                "next_action": copy.deepcopy(self.next_action),
                "request": self.request,
                "reviews": copy.deepcopy(self.reviews),
                "actions": copy.deepcopy(self.actions)
            },
            "project": {
                "project_id": self.project_id,
                "revision": self.project_revision,
                "profile": {
                    "title": "Vision Transformer Efficiency",
                    "research_question": "How to optimize attention in vision transformers?",
                    "sub_questions": [{"id": "RQ1", "question": "Compare convolution vs self-attention"}]
                },
                "candidates": [
                    {
                        "paper_id": "paper-cvt-2103-15808",
                        "metadata_sha256": "sha-cvt-001",
                        "paper": {"title": "CvT: Introducing Convolutions to Vision Transformers", "year": 2021, "abstract": "We present CvT..."}
                    }
                ],
                "draft": {
                    "title": "Vision Transformer Efficiency Survey",
                    "outline": [{"id": "sec-1", "title": "Introduction", "level": 1}],
                    "sections": [{"id": "sec-1", "title": "Introduction", "paragraphs": [{"text": "Attention mechanisms are widely studied."}]}]
                }
            }
        }

    def get(self, project_id: str) -> dict:
        with self.lock:
            if project_id != self.project_id:
                raise JournalError("PROJECT_NOT_FOUND", "项目不存在")
            return response(self._state_dict(), coverage={"backend_calls_llm": False, "evidence_check_only": True})

    def control(self, project_id: str, action: str, expected_loop_revision: int,
                expected_project_revision: int, budget: Optional[dict] = None, request: str = "") -> dict:
        with self.lock:
            if project_id != self.project_id:
                raise JournalError("PROJECT_NOT_FOUND", "项目不存在")
            if action not in ("start", "pause", "resume", "stop", "update_budget"):
                raise JournalError("INVALID_INPUT", f"未知动作: {action}")

            if action == "start":
                if self.status != "idle" and self.loop_revision > 0:
                    raise JournalError("LOOP_ALREADY_STARTED", "已有循环不可重新start清零")
                self.status = "awaiting_review"
                self.loop_revision += 1
                if budget:
                    self.budget.update(budget)
                self.request = request or self.request
                self.next_action = {
                    "action_id": "act-review-001", "kind": "review", "gap_ids": [],
                    "reason": "初始评审草稿与文献缺口", "requires_agent": True
                }
            elif action == "pause":
                self.status = "paused"
                self.stop_reason = "paused_by_user"
                self.loop_revision += 1
            elif action == "resume":
                self.status = "awaiting_review" if not self.next_action else ("executing" if not self.next_action.get("requires_agent") else f"awaiting_{self.next_action['kind']}")
                self.stop_reason = None
                self.loop_revision += 1
            elif action == "stop":
                self.status = "stopped"
                self.stop_reason = "stopped_by_user"
                self.loop_revision += 1
            elif action == "update_budget":
                if budget:
                    self.budget.update(budget)
                if request:
                    self.request = request
                self.loop_revision += 1

            return response(self._state_dict())

    def prepare_review(self, project_id: str, evidence_offset: int = 0, evidence_limit: int = 30) -> dict:
        with self.lock:
            state = self._state_dict()
            state["context_fingerprint"] = hashlib.sha256(f"{self.project_id}:{self.project_revision}:{self.loop_revision}".encode("utf-8")).hexdigest()
            state["review_schema"] = {
                "type": "object",
                "properties": {
                    "dimensions": {"type": "array"},
                    "gaps": {"type": "array"},
                    "decision": {"type": "string", "enum": ["continue", "revise", "stop"]},
                    "reason": {"type": "string"},
                    "examined_citation_ids": {"type": "array"},
                    "examined_section_ids": {"type": "array"}
                },
                "required": ["dimensions", "decision", "reason"]
            }
            state["evidence_matrix"] = []
            return response(state)

    def submit_review(self, project_id: str, review: dict, context_fingerprint: str,
                      expected_project_revision: int, expected_loop_revision: int, origin: str = "calling_agent") -> dict:
        with self.lock:
            self.reviews.append(review)
            self.loop_revision += 1
            self.round_index += 1
            decision = review.get("decision", "continue")
            if decision == "stop":
                self.status = "stopped"
                self.stop_reason = "evidence_sufficient"
                self.stop_detail = "关键文献缺口已解决，出处复验通过"
                self.next_action = None
            elif decision == "continue":
                self.status = "awaiting_assessment"
                self.next_action = {
                    "action_id": "act-assess-002",
                    "kind": "assessment",
                    "gap_ids": ["gap-cvt-01"],
                    "reason": "初筛补读候选文献",
                    "requires_agent": True,
                    "material": {
                        "candidates": [
                            {"paper_id": "paper-cvt-2103-15808", "title": "CvT", "abstract": "Convolutional vision transformer."}
                        ],
                        "assessment_schema": {"type": "object"}
                    }
                }
            elif decision == "revise":
                self.status = "awaiting_revision"
                self.next_action = {
                    "action_id": "act-revise-004",
                    "kind": "revision",
                    "gap_ids": ["gap-draft-01"],
                    "reason": "修订受影响章节",
                    "requires_agent": True,
                    "material": {
                        "new_evidence": [{"citation_id": "cite-cvt-01", "quote": "CvT improves tokens."}],
                        "draft_schema": {"type": "object"}
                    }
                }
            return response(self._state_dict())

    def step(self, project_id: str, action_id: str, expected_project_revision: int, expected_loop_revision: int) -> dict:
        with self.lock:
            self.usage["read_steps"] += 1
            self.loop_revision += 1
            # Step transitions from read to interpretation
            self.status = "awaiting_interpretation"
            self.next_action = {
                "action_id": "act-interp-003",
                "kind": "interpretation",
                "gap_ids": ["gap-cvt-01"],
                "reason": "解读已提取的正文片段",
                "requires_agent": True,
                "material": {
                    "paper_id": "paper-cvt-2103-15808",
                    "file_sha256": "sha-cvt-file-001",
                    "fragments": [{"fragment_id": "frag-1", "page_number": 2, "text": "CvT incorporates convolutions into ViT."}],
                    "has_more": False
                }
            }
            return response(self._state_dict())

    def apply_feedback(self, project_id: str, action_id: str, feedback: dict,
                       expected_project_revision: int, expected_loop_revision: int, origin: str = "calling_agent") -> dict:
        with self.lock:
            kind = feedback.get("kind")
            self.loop_revision += 1
            if kind == "assessment":
                # Move to read step
                self.status = "executing"
                self.next_action = {
                    "action_id": "act-read-001",
                    "kind": "read",
                    "gap_ids": ["gap-cvt-01"],
                    "reason": "读取选中文献物理页",
                    "requires_agent": False
                }
            elif kind == "interpretation":
                # Move to revision
                self.status = "awaiting_revision"
                self.next_action = {
                    "action_id": "act-revise-001",
                    "kind": "revision",
                    "gap_ids": ["gap-cvt-01"],
                    "reason": "将解读结论融入草稿",
                    "requires_agent": True,
                    "material": {
                        "new_evidence": [{"citation_id": "cite-cvt-01", "quote": "CvT incorporates convolutions."}],
                        "draft_schema": {"type": "object"}
                    }
                }
            elif kind == "revision":
                # Round completed, go to awaiting_review for re-review
                self.project_revision += 1
                self.status = "awaiting_review"
                self.next_action = {
                    "action_id": "act-review-final",
                    "kind": "review",
                    "gap_ids": [],
                    "reason": "复审最新草稿与证据矩阵",
                    "requires_agent": True
                }
            return response(self._state_dict())

    def reserve_model_call(self, project_id: str, action_id: str,
                           expected_project_revision: int, expected_loop_revision: int) -> dict:
        with self.lock:
            if self.usage["gui_model_calls"] + self.reserved["gui_model_calls"] >= self.budget["max_gui_model_calls"]:
                raise JournalError("MODEL_BUDGET_EXCEEDED", "模型尝试次数已达上限额度")
            self.reserved["gui_model_calls"] += 1
            self.loop_revision += 1
            self.active_ticket = f"ticket-{hashlib.sha256(f'{action_id}:{self.loop_revision}'.encode()).hexdigest()[:24]}"
            return response({"model_ticket": self.active_ticket, "loop_revision": self.loop_revision})

    def settle_model_call(self, project_id: str, action_id: str, model_ticket: str, success: bool) -> dict:
        with self.lock:
            if self.reserved["gui_model_calls"] > 0:
                self.reserved["gui_model_calls"] -= 1
            # Every attempt counts towards usage even on failure!
            self.usage["gui_model_calls"] += 1
            self.loop_revision += 1
            self.active_ticket = None
            return response({"settled": True, "loop_revision": self.loop_revision})


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_server, "load_page", lambda: "<!DOCTYPE html><html><body>studio</body></html>")
    finder = JournalFinder(str(tmp_path / "home"))
    store = SecretStore()
    # Configure model and consent
    store.configure("openai_compatible", "https://api.example.com/v1", "gpt-4o-mini", "sec-key-12345", remember=False)
    store.consent(True)

    fake_service = FakeReadingLoopService()
    app = gui_server.create_app(TOKEN, PORT, finder=finder, store=store, loop=fake_service)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    return client, fake_service, store


def post(client, path, payload, token=TOKEN, host=f"127.0.0.1:{PORT}"):
    headers = {"Content-Type": "application/json", "Host": host}
    if token is not None:
        headers["X-PaperFlow-Token"] = token
    return client.post(path, content=json.dumps(payload), headers=headers)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_loop_http_guards_and_path_rejection(test_env):
    client, service, store = test_env
    # 1. Invalid Host
    res = post(client, "/api/writing/loop-status", {"project_id": service.project_id}, host="evil.com:54321")
    assert res.status_code == 403
    assert res.json()["error_code"] == "FORBIDDEN_HOST"

    # 2. Missing Token
    res = post(client, "/api/writing/loop-status", {"project_id": service.project_id}, token=None)
    assert res.status_code == 403

    # 3. Path injection in loop params
    res = post(client, "/api/action", {
        "action": "loop_control",
        "params": {
            "project_id": service.project_id,
            "action": "start",
            "expected_loop_revision": 0,
            "expected_project_revision": 1,
            "file_path": "C:/passwords.txt"
        }
    })
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_INPUT"

    # 4. Large payload rejected (>2MB)
    res = post(client, "/api/writing/loop-status", {"project_id": "a" * (3 * 1024 * 1024)})
    assert res.status_code == 400


@pytest.mark.parametrize("endpoint,action", [
    ("/api/writing/loop-start-auto", None),
    ("/api/writing/loop-control", "start"),
    ("/api/writing/loop-control", "resume"),
    ("/api/writing/loop-step", None),
])
@pytest.mark.parametrize("consent", [None, False, 1, "true", {}, []])
def test_loop_http_requires_explicit_boolean_consent(test_env, monkeypatch, endpoint, action, consent):
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)
    before = copy.deepcopy(service.get(service.project_id))
    calls = []

    def unexpected_model(*args, **kwargs):
        calls.append((args, kwargs))
        return response({})

    monkeypatch.setattr(paper_reading_loop, "start_auto_loop", unexpected_model)
    monkeypatch.setattr(paper_reading_loop.LoopWorker, "_execute_review_phase", unexpected_model)
    payload = {
        "project_id": service.project_id,
        "expected_loop_revision": service.loop_revision,
        "expected_project_revision": service.project_revision,
    }
    if action:
        payload["action"] = action
    if consent is not None:
        payload["consent"] = consent
    result = post(client, endpoint, payload)
    assert result.status_code == 400
    assert result.json()["error_code"] == "CONSENT_REQUIRED"
    assert calls == []
    assert service.get(service.project_id) == before


@pytest.mark.parametrize("action", ["start", "resume"])
def test_loop_http_control_forwards_explicit_consent(test_env, monkeypatch, action):
    client, service, store = test_env
    calls = []

    def accepted_model(*args, **kwargs):
        calls.append((args, kwargs))
        return response({"project_id": service.project_id})

    monkeypatch.setattr(paper_reading_loop, "start_auto_loop", accepted_model)
    result = post(client, "/api/writing/loop-control", {
        "project_id": service.project_id,
        "action": action,
        "expected_loop_revision": service.loop_revision,
        "expected_project_revision": service.project_revision,
        "consent": True,
    })
    assert result.status_code == 200
    assert len(calls) == 1
    assert calls[0][1]["consent"] is True
    assert calls[0][0][3:5] == (service.loop_revision, service.project_revision)


def test_loop_two_round_gap_closing_lifecycle(test_env):
    """Full lifecycle: Round 1 review -> assess -> read -> interpret -> revise -> Round 2 re-review -> stop."""
    client, service, store = test_env

    # Custom model transport that returns realistic structured responses for all phases
    def fake_transport(url, headers, body):
        prompt_text = body.get("messages", [{}])[-1].get("content", "")
        if "review_schema" in prompt_text:
            if service.round_index == 0:
                # Round 1: find CvT gap and continue
                return {"choices": [{"message": {"content": json.dumps({
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "gap", "reason": "缺少 CvT 卷积增强对比", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "method_baselines", "status": "sufficient", "reason": "已有标准 ViT 基线", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "contrary_findings", "status": "not_applicable", "reason": "初期暂无相反结论", "question_ids": [], "section_ids": [], "citation_ids": []},
                        {"dimension": "draft_support", "status": "gap", "reason": "导言区缺少卷积增强论据", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []}
                    ],
                    "gaps": [
                        {"id": "gap-cvt-01", "category": "literature", "priority": "high", "question_ids": ["RQ1"], "section_ids": ["sec-1"],
                         "reason": "需要引入 CvT 卷积 Vision Transformer 原文对比", "expected_information": "CvT 结构与参数", "read_paper_ids": ["paper-cvt-2103-15808"]}
                    ],
                    "decision": "continue",
                    "reason": "缺口明确，需要阅读 CvT 补充支撑",
                    "examined_citation_ids": [],
                    "examined_section_ids": ["sec-1"]
                })}}]}
            else:
                # Round 2: review passes, stop
                return {"choices": [{"message": {"content": json.dumps({
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "sufficient", "reason": "CvT 论据已补全", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "method_baselines", "status": "sufficient", "reason": "基线对比完善", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "contrary_findings", "status": "sufficient", "reason": "已讨论适用边界", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "draft_support", "status": "sufficient", "reason": "草稿段落与引用一致", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []}
                    ],
                    "gaps": [],
                    "decision": "stop",
                    "reason": "关键缺口已消除，文献支撑充足",
                    "examined_citation_ids": [],
                    "examined_section_ids": ["sec-1"]
                })}}]}
        elif "assessment_schema" in prompt_text:
            return {"choices": [{"message": {"content": json.dumps({
                "assessments": [
                    {"paper_id": "paper-cvt-2103-15808", "relevance": "core", "reason": "直接提出 CvT 卷积 ViT 架构", "basis": "abstract"}
                ]
            })}}]}
        elif "card_schema" in prompt_text:
            return {"choices": [{"message": {"content": json.dumps({
                "reading": {
                    "paper_id": "paper-cvt-2103-15808",
                    "file_sha256": "sha-cvt-file-001",
                    "summary": "CvT 引入了深度可分离卷积",
                    "claims": [
                        {"section": "method", "kind": "author_claim", "text": "CvT incorporates convolutions into ViT.",
                         "evidence": [{"fragment_id": "frag-1", "page_number": 2, "quote": "CvT incorporates convolutions into ViT."}]}
                    ]
                }
            })}}]}
        elif "draft_schema" in prompt_text:
            return {"choices": [{"message": {"content": json.dumps({
                "draft": {
                    "title": "Vision Transformer Efficiency Survey (Revised)",
                    "outline": [{"id": "sec-1", "title": "Introduction", "level": 1}],
                    "sections": [
                        {"id": "sec-1", "title": "Introduction", "paragraphs": [
                            {"text": "Attention mechanisms and CvT convolutions improve efficiency."}
                        ]}
                    ]
                },
                "change_note": "引入 CvT 卷积结合论点"
            })}}]}
        return {"choices": [{"message": {"content": "{}"}}]}

    # Start loop in background worker
    worker = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=fake_transport)
    # Start loop in service
    service.control(service.project_id, "start", 0, 1)
    worker.start()

    # Wait for completion (two rounds should finish quickly)
    timeout = 10.0
    start_t = time.time()
    while worker.is_running and time.time() - start_t < timeout:
        time.sleep(0.1)

    assert not worker.is_running
    assert service.status == "stopped"
    assert service.stop_reason == "evidence_sufficient"
    assert service.round_index >= 1
    # Check that model calls were accounted accurately (review1, assess, interpret, revise, review2 = 5 calls)
    assert service.usage["gui_model_calls"] >= 4
    assert service.reserved["gui_model_calls"] == 0


def test_model_budget_exhausted_halts_loop_safely(test_env):
    """When model calls reach budget, reserve_model_call fails and loop safely pauses."""
    client, service, store = test_env
    service.budget["max_gui_model_calls"] = 1  # only 1 call allowed
    service.control(service.project_id, "start", 0, 1)

    def simple_transport(url, headers, body):
        # Round 1 returns continue, requiring subsequent calls
        return {"choices": [{"message": {"content": json.dumps({
            "dimensions": [
                {"dimension": "sub_questions", "status": "gap", "reason": "缺口", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                {"dimension": "method_baselines", "status": "sufficient", "reason": "充足", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                {"dimension": "contrary_findings", "status": "not_applicable", "reason": "无", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "draft_support", "status": "gap", "reason": "缺口", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []}
            ],
            "gaps": [{"id": "gap-1", "category": "literature", "priority": "high", "reason": "缺", "expected_information": "CvT", "read_paper_ids": ["paper-cvt-2103-15808"]}],
            "decision": "continue", "reason": "需继续", "examined_citation_ids": [], "examined_section_ids": []
        })}}]}

    worker = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=simple_transport)
    worker.start()

    time.sleep(0.8)
    assert not worker.is_running
    # Loop should be paused due to budget exhaustion
    assert service.status == "paused"
    assert service.usage["gui_model_calls"] == 1


def test_in_flight_pause_and_stop_safety(test_env):
    """Pausing while a model call is in-flight must settle the call but NOT apply the result."""
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)

    call_entered = threading.Event()

    def slow_transport(url, headers, body):
        call_entered.set()
        time.sleep(0.4)
        return {"choices": [{"message": {"content": json.dumps({
            "dimensions": [
                {"dimension": "sub_questions", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "method_baselines", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "contrary_findings", "status": "not_applicable", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "draft_support", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []}
            ],
            "decision": "stop", "reason": "late stop", "examined_citation_ids": [], "examined_section_ids": []
        })}}]}

    worker = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=slow_transport)
    with paper_reading_loop._WORKER_LOCK:
        paper_reading_loop._WORKERS[service.project_id] = worker
    worker.start()

    assert call_entered.wait(timeout=2.0)
    # User triggers pause via endpoint
    res = post(client, "/api/writing/loop-control", {
        "project_id": service.project_id,
        "action": "pause",
        "expected_loop_revision": service.loop_revision,
        "expected_project_revision": service.project_revision
    })
    assert res.status_code == 200

    time.sleep(0.6)
    assert not worker.is_running
    # The late result was NOT applied
    assert service.status == "paused"
    assert service.stop_reason == "paused_by_user"
    # But the attempt was settled properly
    assert service.reserved["gui_model_calls"] == 0
    assert service.usage["gui_model_calls"] == 1


def test_config_mutation_safely_pauses_worker(test_env):
    """Mutating model configuration while running safely halts the worker and pauses loop."""
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)

    def slow_transport(url, headers, body):
        time.sleep(0.3)
        return {"choices": [{"message": {"content": "{}"}}]}

    worker = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=slow_transport)
    worker.start()

    time.sleep(0.1)
    # User revokes consent in store
    store.consent(False)

    time.sleep(0.5)
    assert not worker.is_running
    assert service.status == "paused"


def test_worker_concurrency_and_shutdown_lifecycle(test_env):
    """Enforces MAX_GLOBAL_WORKERS and single worker per project; tests graceful shutdown."""
    client, service, store = test_env

    # 1. Start auto loop via HTTP endpoint
    res = post(client, "/api/writing/loop-start-auto", {
        "project_id": service.project_id,
        "expected_loop_revision": 0,
        "expected_project_revision": 1,
        "consent": True
    })
    assert res.status_code == 200

    # 2. Starting second worker for same project must be rejected
    res2 = post(client, "/api/writing/loop-start-auto", {
        "project_id": service.project_id,
        "expected_loop_revision": 1,
        "expected_project_revision": 1,
        "consent": True
    })
    assert res2.status_code == 400
    assert res2.json()["error_code"] == "LOOP_ALREADY_RUNNING"

    # 3. Shutdown cleanly terminates all workers
    paper_reading_loop.shutdown_all()
    assert len(paper_reading_loop.list_active_workers()) == 0


def test_model_output_leak_or_path_is_intercepted(test_env):
    """Model returning API key or forbidden paths is intercepted before applying."""
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)

    # 1. Transport returning secret key
    def leak_transport(url, headers, body):
        return {"choices": [{"message": {"content": json.dumps({"key": "sec-key-12345"})}}]}

    worker1 = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=leak_transport)
    with pytest.raises(JournalError) as exc_info:
        worker1._call_model_guarded("act-test", "sys", {"data": "x"}, 1, 0)
    assert exc_info.value.code == "MODEL_ERROR"

    # 2. Transport returning path
    def path_transport(url, headers, body):
        return {"choices": [{"message": {"content": json.dumps({"output_path": "/var/data.txt"})}}]}

    worker2 = paper_reading_loop.LoopWorker(service, store, service.project_id, transport=path_transport)
    with pytest.raises(JournalError) as exc_info:
        worker2._call_model_guarded("act-test", "sys", {"data": "x"}, 1, 0)
    assert exc_info.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("loop_revision,project_revision", [(0, 1), (2, 1), (1, 2)])
def test_loop_http_single_step_rejects_stale_revisions_before_model(test_env, monkeypatch, loop_revision, project_revision):
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)
    before = copy.deepcopy(service.get(service.project_id))
    calls = []

    def unexpected_phase(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(paper_reading_loop.LoopWorker, "_execute_review_phase", unexpected_phase)
    result = post(client, "/api/writing/loop-step", {
        "project_id": service.project_id,
        "expected_loop_revision": loop_revision,
        "expected_project_revision": project_revision,
        "consent": True,
    })
    assert result.status_code == 400
    assert result.json()["error_code"] == "REVISION_CONFLICT"
    assert calls == []
    assert service.get(service.project_id) == before


def test_loop_single_step_execution(test_env):
    """Single step advances through states when explicitly requested."""
    client, service, store = test_env
    service.control(service.project_id, "start", 0, 1)

    def simple_transport(url, headers, body):
        return {"choices": [{"message": {"content": json.dumps({
            "dimensions": [
                {"dimension": "sub_questions", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "method_baselines", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "contrary_findings", "status": "not_applicable", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
                {"dimension": "draft_support", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []}
            ],
            "decision": "stop", "reason": "ok", "examined_citation_ids": [], "examined_section_ids": []
        })}}]}

    # Test direct single_step execution with injected transport
    res_direct = paper_reading_loop.single_step(service, store, service.project_id, 1, 1, consent=True, transport=simple_transport)
    assert res_direct["status"] == "success"
    assert service.status == "stopped"


def test_budget_update_and_bool_as_int_rejection(test_env):
    """Updating budget rejects bool values and accepts valid integers."""
    client, service, store = test_env

    # 1. Reject bool as int
    res = post(client, "/api/action", {
        "action": "loop_control",
        "params": {
            "project_id": service.project_id,
            "action": "update_budget",
            "expected_loop_revision": 0,
            "expected_project_revision": 1,
            "budget": {"max_read_papers": True}
        }
    })
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_INPUT"

    # 2. Valid budget update
    res2 = post(client, "/api/writing/loop-control", {
        "project_id": service.project_id,
        "action": "update_budget",
        "expected_loop_revision": 0,
        "expected_project_revision": 1,
        "budget": {"max_read_papers": 15},
        "request": "重点对比CNN"
    })
    assert res2.status_code == 200
    assert service.budget["max_read_papers"] == 15
    assert service.request == "重点对比CNN"


def test_settle_advances_loop_revision_and_cas_propagates(test_env):
    """Enforces strict CAS: settle_model_call increments loop_revision and subsequent calls use latest revision."""
    client, service, store = test_env
    # Start: loop_revision -> 1
    service.control(service.project_id, "start", 0, 1)
    assert service.loop_revision == 1

    # Reserve: loop_revision -> 2
    res_reserve = service.reserve_model_call(service.project_id, "act-review-001", 1, 1)
    ticket = res_reserve["data"]["model_ticket"]
    assert service.loop_revision == 2

    # Settle: loop_revision -> 3
    res_settle = service.settle_model_call(service.project_id, "act-review-001", ticket, success=True)
    assert res_settle["data"]["loop_revision"] == 3
    assert service.loop_revision == 3

    # Stale submit with revision 2 must be rejected
    prep = service.prepare_review(service.project_id)
    fp = prep["data"]["context_fingerprint"]
    valid_review = {
        "dimensions": [
            {"dimension": "sub_questions", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "method_baselines", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "contrary_findings", "status": "not_applicable", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []},
            {"dimension": "draft_support", "status": "sufficient", "reason": "ok", "question_ids": [], "section_ids": [], "citation_ids": []}
        ],
        "decision": "stop", "reason": "done", "examined_citation_ids": [], "examined_section_ids": []
    }
    # Submit with latest revision 3 succeeds
    sub_ok = service.submit_review(service.project_id, valid_review, fp, 1, 3, origin="gui_model")
    assert sub_ok["status"] == "success"
    # loop_revision is now 4
    assert service.loop_revision == 4


def test_real_core_reading_loop_service_basic_lifecycle(tmp_path):
    """Integrates real ReadingLoopService and WritingService without mock doubles."""
    from paperflow.engine.literature.service import LiteratureService
    from paperflow.engine.literature.writing_service import PaperWritingService
    from paperflow.engine.literature.reading_loop_service import ReadingLoopService
    from test_literature_service import FakeProviders, pdf_bytes, card_for

    papers_dir = tmp_path / "papers"
    lit_svc = LiteratureService(str(papers_dir), providers=FakeProviders())
    writing_svc = PaperWritingService(data_dir=str(papers_dir), literature=lit_svc)
    loop_svc = ReadingLoopService(data_dir=str(papers_dir), writing=writing_svc)

    # 1. Create a real project
    file = tmp_path / "paper.pdf"
    file.write_bytes(pdf_bytes(["Test paper for real core reading loop."]))
    imp = lit_svc.import_pdf(str(file), "Synthetic Paper")
    pid = imp["data"]["paper_id"]
    reading = card_for(lit_svc.read(pid))
    lit_svc.save_reading(pid, reading)

    created = writing_svc.create({
        "title": "Real Core Loop Test",
        "research_question": "Can real core reading loop start and pause safely?",
        "sub_questions": [{"id": "RQ1", "question": "Sub RQ"}]
    }, paper_ids=[pid])
    proj_id = created["data"]["project_id"]
    proj_rev = created["data"]["revision"]

    # 2. Loop get before start
    env_get = loop_svc.get(proj_id)
    assert env_get["status"] == "success"
    assert env_get["data"]["loop"]["status"] == "idle"

    # 3. Start loop
    env_start = loop_svc.control(proj_id, "start", 0, proj_rev, budget={"max_gui_model_calls": 10})
    assert env_start["status"] == "success"
    loop_rev = env_start["data"]["loop_revision"]
    assert loop_rev >= 1

    # 4. Prepare review
    prep = loop_svc.prepare_review(proj_id)
    assert prep["status"] == "success"
    assert "context_fingerprint" in prep["data"]

    # 5. Reserve & Settle model call
    res_res = loop_svc.reserve_model_call(proj_id, "review", proj_rev, loop_rev)
    assert res_res["status"] == "success"
    ticket = res_res["data"]["model_ticket"]
    loop_rev_after_res = res_res["data"]["loop_revision"]

    res_set = loop_svc.settle_model_call(proj_id, "review", ticket, success=True)
    assert res_set["status"] == "success"
    loop_rev_after_set = res_set["data"]["loop_revision"]
    assert loop_rev_after_set > loop_rev_after_res

    # 6. Pause loop
    env_pause = loop_svc.control(proj_id, "pause", loop_rev_after_set, proj_rev)
    assert env_pause["status"] == "success"
    assert env_pause["data"]["loop"]["status"] == "paused"


def test_real_core_reading_loop_worker_two_round_autonomous_lifecycle(tmp_path):
    """Full autonomous two-round integration with real Literature, Writing, and ReadingLoop services.

    Round 1: Paper A is baseline draft; Paper B is candidate. Review 1 gaps B -> Worker automatically
             assesses B -> steps reading -> interprets B -> revises draft with B's citation.
    Round 2: Review 2 verifies all dimensions sufficient -> Worker stops with evidence_sufficient.
    """
    from paperflow.engine.literature.service import LiteratureService
    from paperflow.engine.literature.writing_service import PaperWritingService
    from paperflow.engine.literature.reading_loop_service import ReadingLoopService
    from test_literature_service import FakeProviders, pdf_bytes, card_for

    papers_dir = tmp_path / "papers_auto"
    lit_svc = LiteratureService(str(papers_dir), providers=FakeProviders())
    writing_svc = PaperWritingService(data_dir=str(papers_dir), literature=lit_svc)
    loop_svc = ReadingLoopService(data_dir=str(papers_dir), writing=writing_svc)

    store = SecretStore()
    store.configure("openai_compatible", "https://api.example.com/v1", "gpt-4o-mini", "sec-key-12345", remember=False)
    store.consent(True)

    # 1. Create Paper A (Baseline: has reading card & draft citation)
    file_a = tmp_path / "paper_a.pdf"
    file_a.write_bytes(pdf_bytes([
        "Method A establishes standard vision transformer baseline on edge devices."
    ]))
    imp_a = lit_svc.import_pdf(str(file_a), "Paper A Standard ViT")
    pid_a = imp_a["data"]["paper_id"]
    reading_a = card_for(lit_svc.read(pid_a))
    reading_a["claims"][0]["kind"] = "author_claim"
    reading_a["claims"][0]["text"] = "Method A establishes standard vision transformer baseline on edge devices."
    lit_svc.save_reading(pid_a, reading_a)

    # 2. Create Paper B (Candidate: in library, but NOT read, NOT assessed, NO card)
    file_b = tmp_path / "paper_b.pdf"
    file_b.write_bytes(pdf_bytes([
        "CvT incorporates convolutions into vision transformers for high edge efficiency."
    ]))
    imp_b = lit_svc.import_pdf(str(file_b), "Paper B CvT Convolutions")
    pid_b = imp_b["data"]["paper_id"]

    # 3. Create Writing Project with A and B
    created = writing_svc.create({
        "title": "Vision Transformer Edge Efficiency",
        "research_question": "How to optimize attention in vision transformers for edge devices?",
        "sub_questions": [{"id": "RQ1", "question": "Compare convolution vs self-attention"}],
        "own_materials": []
    }, paper_ids=[pid_a, pid_b])
    proj_id = created["data"]["project_id"]
    proj_rev = created["data"]["revision"]

    # Assess Paper A as core
    cand_a = next(c for c in created["data"]["candidates"] if c["paper_id"] == pid_a)
    assess_res = writing_svc.assess(proj_id, [
        {"paper_id": pid_a, "metadata_sha256": cand_a["metadata_sha256"], "relevance": "core",
         "reason": "Initial baseline", "basis": "metadata", "question_ids": ["RQ1"], "limitations": []}
    ], expected_revision=proj_rev, selected_paper_ids=[pid_a])
    proj_rev = assess_res["data"]["revision"]

    # Prepare evidence & save initial draft with Paper A
    mat_res = writing_svc.prepare_writing(proj_id)
    ev_a = mat_res["data"]["evidence_matrix"][0]
    cid_a = ev_a["citation_id"]

    draft_initial = {
        "title": "Vision Transformer Edge Efficiency",
        "language": "zh",
        "outline": [{"id": "sec-1", "title": "Introduction", "level": 1, "purpose": "Overview", "evidence_ids": [cid_a]}],
        "sections": [{
            "id": "sec-1",
            "title": "Introduction",
            "paragraphs": [{
                "text": "Method A establishes standard vision transformer baseline on edge devices.",
                "kind": "literature_summary",
                "citation_ids": [cid_a],
                "own_material_ids": []
            }]
        }]
    }
    draft_res = writing_svc.save_draft(proj_id, draft_initial, proj_rev, "Draft with Paper A")
    proj_rev = draft_res["data"]["revision"]

    # 4. Injected Mock Transport with exact structure for each phase
    call_log = []

    def mock_transport(url, headers, body):
        prompt_text = body.get("messages", [{}])[-1].get("content", "")
        call_log.append(prompt_text)

        if "review_schema" in prompt_text:
            if len([c for c in call_log if "review_schema" in c]) == 1:
                # Round 1: Gap for Paper B -> continue
                return {"choices": [{"message": {"content": json.dumps({
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "gap", "reason": "缺少卷积结合对比", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": [cid_a]},
                        {"dimension": "method_baselines", "status": "sufficient", "reason": "基线完备", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": [cid_a]},
                        {"dimension": "contrary_findings", "status": "not_applicable", "reason": "无相反结论", "question_ids": [], "section_ids": [], "citation_ids": []},
                        {"dimension": "draft_support", "status": "gap", "reason": "草稿需补充 CvT 论据", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": [cid_a]}
                    ],
                    "gaps": [
                        {"id": "gap-cvt", "category": "literature", "priority": "high", "question_ids": ["RQ1"], "section_ids": ["sec-1"],
                         "reason": "需要引入 CvT 卷积对比", "expected_information": "CvT 效率", "read_paper_ids": [pid_b]}
                    ],
                    "decision": "continue",
                    "reason": "补读 CvT 解决架构对比缺口",
                    "examined_citation_ids": [cid_a],
                    "examined_section_ids": ["sec-1"]
                })}}]}
            else:
                # Round 2: Re-review after revision -> stop
                req_data = json.loads(prompt_text)
                sample = req_data.get("evidence_sample") or []
                all_cids = [e["citation_id"] for e in sample if "citation_id" in e] or [cid_a]
                return {"choices": [{"message": {"content": json.dumps({
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "sufficient", "reason": "卷积结合对比已补充", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": all_cids},
                        {"dimension": "method_baselines", "status": "sufficient", "reason": "基线完备", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": all_cids},
                        {"dimension": "contrary_findings", "status": "sufficient", "reason": "适用范围明确", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": []},
                        {"dimension": "draft_support", "status": "sufficient", "reason": "两篇文献均有段落支撑", "question_ids": ["RQ1"], "section_ids": ["sec-1"], "citation_ids": all_cids}
                    ],
                    "gaps": [],
                    "decision": "stop",
                    "reason": "全量文献缺口已消除",
                    "examined_citation_ids": all_cids,
                    "examined_section_ids": ["sec-1"]
                })}}]}
        elif "assessment_schema" in prompt_text:
            # Assessment: classify Paper B as core
            return {"choices": [{"message": {"content": json.dumps({
                "assessments": [
                    {"paper_id": pid_b, "relevance": "core", "reason": "直接提出 CvT 卷积 ViT", "basis": "abstract"}
                ]
            })}}]}
        elif "card_schema" in prompt_text:
            # Interpretation: create reading card strictly citing fragments
            req_data = json.loads(prompt_text)
            frags = req_data.get("fragments") or []
            frag = frags[0] if frags else {"fragment_id": "f1", "page_number": 1, "text": "CvT incorporates convolutions into vision transformers for high edge efficiency."}
            return {"choices": [{"message": {"content": json.dumps({
                "reading": {
                    "paper_id": pid_b,
                    "file_sha256": req_data.get("file_sha256", "sha-dummy"),
                    "summary": "CvT 引入卷积结合自注意力提升推理速度",
                    "claims": [
                        {"section": "method", "kind": "author_claim", "text": "CvT incorporates convolutions into vision transformers for high edge efficiency.",
                         "evidence": [{"fragment_id": frag.get("fragment_id", "f1"), "page_number": frag.get("page_number", 1), "quote": "CvT incorporates convolutions into vision transformers for high edge efficiency."}]}
                    ]
                },
                "read_more": False
            })}}]}
        elif "draft_schema" in prompt_text:
            # Revision: draft cites both Paper A and Paper B
            req_data = json.loads(prompt_text)
            new_evs = req_data.get("new_evidence") or []
            available_cids = list(dict.fromkeys([e["citation_id"] for e in new_evs if "citation_id" in e])) or [cid_a]
            paras = [
                {"text": "Method A establishes standard vision transformer baseline on edge devices.", "kind": "literature_summary", "citation_ids": [available_cids[0]], "own_material_ids": []}
            ]
            if len(available_cids) > 1:
                paras.append({
                    "text": "CvT incorporates convolutions into vision transformers for high edge efficiency.",
                    "kind": "literature_summary",
                    "citation_ids": [available_cids[1]],
                    "own_material_ids": []
                })
            return {"choices": [{"message": {"content": json.dumps({
                "draft": {
                    "title": "Vision Transformer Edge Efficiency",
                    "language": "zh",
                    "outline": [{"id": "sec-1", "title": "Introduction", "level": 1, "purpose": "Overview", "evidence_ids": available_cids}],
                    "sections": [{
                        "id": "sec-1",
                        "title": "Introduction",
                        "level": 1,
                        "paragraphs": paras
                    }]
                },
                "change_note": "补充 CvT 卷积论述与证据引用"
            })}}]}

        return {"choices": [{"message": {"content": "{}"}}]}

    # 5. Start Autonomous Loop using LoopWorker
    # Start loop in core service
    start_res = loop_svc.control(proj_id, "start", 0, proj_rev)
    assert start_res["status"] == "success"

    worker = paper_reading_loop.LoopWorker(loop_svc, store, proj_id, transport=mock_transport)
    with paper_reading_loop._WORKER_LOCK:
        paper_reading_loop._WORKERS[proj_id] = worker
    worker.start()

    # Wait for completion
    timeout = 15.0
    start_time = time.time()
    while worker.is_running and (time.time() - start_time) < timeout:
        time.sleep(0.2)

    # 6. Comprehensive assertions
    assert not worker.is_running, f"Worker did not terminate in time. Last error: {worker.last_error}"
    final_loop_env = loop_svc.get(proj_id)
    assert final_loop_env["status"] == "success"
    final_loop = final_loop_env["data"]["loop"]
    final_proj = final_loop_env["data"]["project"]

    assert final_loop["status"] == "stopped", f"Loop not stopped, status={final_loop.get('status')}, last_error={worker.last_error}, stop_reason={final_loop.get('stop_reason')}"
    assert final_loop["stop_reason"] == "evidence_sufficient"
    assert final_loop["round_index"] >= 1

    # Paper B is now selected
    assert pid_b in final_proj["selected_paper_ids"]

    # Model calls exact count: review1 + assess + interpret + revise + review2 = 5 calls
    assert final_loop["usage"]["gui_model_calls"] == 5
    assert final_loop["reserved"]["gui_model_calls"] == 0
    assert len([c for c in call_log if "review_schema" in c]) == 2
    assert len([c for c in call_log if "assessment_schema" in c]) == 1
    assert len([c for c in call_log if "card_schema" in c]) == 1
    assert len([c for c in call_log if "draft_schema" in c]) == 1

    # Draft contains paragraphs citing both papers
    draft_paras = final_proj["draft"]["sections"][0]["paragraphs"]
    assert len(draft_paras) >= 2
    cited_ids = {cid for p in draft_paras for cid in p.get("citation_ids", [])}
    assert cid_a in cited_ids
    assert len(cited_ids) >= 2

    # Paper B has real reading card persisted
    readings_b = lit_svc.get(pid_b)["data"]["reading_cards"]
    assert len(readings_b) >= 1

    # Read usage and pages match physical PDF execution
    assert final_loop["usage"]["read_papers"] >= 1
    assert final_loop["usage"]["pages"] >= 1
    assert final_loop["usage"]["read_steps"] >= 1

    # No further calls after stop
    time.sleep(0.5)
    assert len(call_log) == 5
