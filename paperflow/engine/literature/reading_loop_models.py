"""Strict data models and schemas for adaptive literature reading loops."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Literal, Optional, Set, Type, TypeVar
from pydantic import ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from paperflow.engine.journals.models import JournalError, Model
from .models import ReadingCard
from .writing_models import (
    CITATION_RE, ENTITY_RE, MAX_INPUT_BYTES, PROJECT_RE,
    WritingAssessment, WritingDraft, WritingModel, WritingQuery,
    budget_json, parse_model, integer
)

ACTION_RE = re.compile(r"^act-[0-9a-f]{24}$")
REVIEW_RE = re.compile(r"^rev-[0-9a-f]{24}$")
TICKET_RE = re.compile(r"^ticket-[0-9a-f]{24}$")

BUDGET_DEFAULTS = {
    "max_read_papers": 12,
    "batch_size": 3,
    "max_rounds": 4,
    "max_pages": 80,
    "max_text_chars": 180000,
    "max_search_calls": 24,
    "max_download_attempts": 24,
    "max_read_steps": 120,
    "max_gui_model_calls": 40,
}

BUDGET_HARD_LIMITS = {
    "max_read_papers": 30,
    "batch_size": 6,
    "max_rounds": 10,
    "max_pages": 1000,
    "max_text_chars": 1000000,
    "max_search_calls": 120,
    "max_download_attempts": 120,
    "max_read_steps": 1000,
    "max_gui_model_calls": 200,
    "existing_candidates": 60,
    "existing_selected": 30,
}

USAGE_KEYS = (
    "read_papers", "pages", "text_chars", "rounds",
    "search_calls", "download_attempts", "read_steps", "gui_model_calls"
)

STOP_REASONS = (
    "evidence_sufficient", "budget_exhausted", "no_new_information",
    "needs_user_material", "needs_manual_review", "source_unavailable",
    "no_open_fulltext", "paused_by_user", "stopped_by_user", "context_changed"
)

LOOP_STATUSES = (
    "idle", "awaiting_review", "awaiting_assessment",
    "awaiting_interpretation", "awaiting_revision",
    "ready", "executing", "paused", "stopped"
)


def _unique(values: List[str], name: str) -> List[str]:
    if len(set(values)) != len(values):
        raise ValueError(name + " 不得重复")
    return values


class LoopBudget(WritingModel):
    max_read_papers: StrictInt = Field(default=BUDGET_DEFAULTS["max_read_papers"], ge=1, le=BUDGET_HARD_LIMITS["max_read_papers"])
    batch_size: StrictInt = Field(default=BUDGET_DEFAULTS["batch_size"], ge=1, le=BUDGET_HARD_LIMITS["batch_size"])
    max_rounds: StrictInt = Field(default=BUDGET_DEFAULTS["max_rounds"], ge=1, le=BUDGET_HARD_LIMITS["max_rounds"])
    max_pages: StrictInt = Field(default=BUDGET_DEFAULTS["max_pages"], ge=1, le=BUDGET_HARD_LIMITS["max_pages"])
    max_text_chars: StrictInt = Field(default=BUDGET_DEFAULTS["max_text_chars"], ge=1000, le=BUDGET_HARD_LIMITS["max_text_chars"])
    max_search_calls: StrictInt = Field(default=BUDGET_DEFAULTS["max_search_calls"], ge=0, le=BUDGET_HARD_LIMITS["max_search_calls"])
    max_download_attempts: StrictInt = Field(default=BUDGET_DEFAULTS["max_download_attempts"], ge=0, le=BUDGET_HARD_LIMITS["max_download_attempts"])
    max_read_steps: StrictInt = Field(default=BUDGET_DEFAULTS["max_read_steps"], ge=1, le=BUDGET_HARD_LIMITS["max_read_steps"])
    max_gui_model_calls: StrictInt = Field(default=BUDGET_DEFAULTS["max_gui_model_calls"], ge=0, le=BUDGET_HARD_LIMITS["max_gui_model_calls"])

    @model_validator(mode="before")
    @classmethod
    def reject_bool(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, bool):
                    raise ValueError(f"{k} 必须是整数，不能是布尔值")
        return values


class LoopUsage(WritingModel):
    read_papers: StrictInt = Field(default=0, ge=0)
    pages: StrictInt = Field(default=0, ge=0)
    text_chars: StrictInt = Field(default=0, ge=0)
    rounds: StrictInt = Field(default=0, ge=0)
    search_calls: StrictInt = Field(default=0, ge=0)
    download_attempts: StrictInt = Field(default=0, ge=0)
    read_steps: StrictInt = Field(default=0, ge=0)
    gui_model_calls: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def reject_bool(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, bool):
                    raise ValueError(f"{k} 必须是整数，不能是布尔值")
        return values


class LoopReserved(WritingModel):
    pages: StrictInt = Field(default=0, ge=0)
    text_chars: StrictInt = Field(default=0, ge=0)
    search_calls: StrictInt = Field(default=0, ge=0)
    download_attempts: StrictInt = Field(default=0, ge=0)
    read_steps: StrictInt = Field(default=0, ge=0)
    gui_model_calls: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def reject_bool(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, bool):
                    raise ValueError(f"{k} 必须是整数，不能是布尔值")
        return values


class InheritedBaseline(WritingModel):
    read_papers: List[str] = Field(default_factory=list, max_length=60)
    partially_read: List[str] = Field(default_factory=list, max_length=60)
    cards: List[str] = Field(default_factory=list, max_length=200)
    cited: List[str] = Field(default_factory=list, max_length=200)


class ReviewDimension(WritingModel):
    dimension: Literal["sub_questions", "method_baselines", "contrary_findings", "draft_support"]
    status: Literal["sufficient", "gap", "uncertain", "not_applicable"]
    reason: str = Field(min_length=1, max_length=10000)
    question_ids: List[str] = Field(default_factory=list, max_length=30)
    section_ids: List[str] = Field(default_factory=list, max_length=30)
    citation_ids: List[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_dimension(self):
        _unique(self.question_ids, "研究问题 ID")
        _unique(self.section_ids, "章节 ID")
        _unique(self.citation_ids, "引用 ID")
        for cid in self.citation_ids:
            if not CITATION_RE.fullmatch(cid):
                raise ValueError("引用 ID 必须是 cite- 后接 24 位十六进制")
        return self


class ReviewGap(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    category: Literal["literature", "own_data", "manual_or_ocr", "draft_revision"]
    priority: Literal["high", "medium", "low"]
    question_ids: List[str] = Field(default_factory=list, max_length=30)
    section_ids: List[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=10000)
    expected_information: str = Field(min_length=1, max_length=10000)
    queries: Optional[List[WritingQuery]] = Field(default=None, max_length=6)
    read_paper_ids: Optional[List[str]] = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def validate_gap(self):
        _unique(self.question_ids, "研究问题 ID")
        _unique(self.section_ids, "章节 ID")
        if self.category in ("own_data", "manual_or_ocr"):
            if self.queries:
                raise ValueError(f"{self.category} 类型的缺口不得包含自动检索方案")
            if self.read_paper_ids:
                raise ValueError(f"{self.category} 类型的缺口不得包含自动阅读文献")
        if self.read_paper_ids:
            _unique(self.read_paper_ids, "指定阅读文献 ID")
            for pid in self.read_paper_ids:
                if not re.fullmatch(r"^paper-[0-9a-f]{24}$", pid):
                    raise ValueError("read_paper_ids 必须是真实 paper ID")
        return self


class WritingReview(WritingModel):
    dimensions: List[ReviewDimension] = Field(min_length=4, max_length=4)
    gaps: List[ReviewGap] = Field(default_factory=list, max_length=30)
    decision: Literal["continue", "revise", "stop"]
    reason: str = Field(min_length=1, max_length=10000)
    examined_citation_ids: List[str] = Field(default_factory=list, max_length=200)
    examined_section_ids: List[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_review(self):
        dims = [d.dimension for d in self.dimensions]
        if set(dims) != {"sub_questions", "method_baselines", "contrary_findings", "draft_support"}:
            raise ValueError("评审必须完整包含四个维度（sub_questions, method_baselines, contrary_findings, draft_support）")
        _unique([g.id for g in self.gaps], "缺口 ID")
        _unique(self.examined_citation_ids, "已核查引用 ID")
        _unique(self.examined_section_ids, "已核查章节 ID")
        for cid in self.examined_citation_ids:
            if not CITATION_RE.fullmatch(cid):
                raise ValueError("已核查引用 ID 格式无效")

        if self.decision == "continue":
            lit_gaps = [g for g in self.gaps if g.category == "literature"]
            if not lit_gaps:
                raise ValueError("决策为 continue 时，必须包含至少一个 category 为 literature 的可执行缺口")
            has_action = any(bool(g.queries) or bool(g.read_paper_ids) for g in lit_gaps)
            if not has_action:
                raise ValueError("literature 缺口必须提供具体检索 queries 或指定 read_paper_ids")
        elif self.decision == "revise":
            rev_gaps = [g for g in self.gaps if g.category == "draft_revision"]
            if not rev_gaps and not self.examined_section_ids:
                raise ValueError("决策为 revise 时，必须包含 draft_revision 缺口或指定需要修改的 examined_section_ids")

        return self


class NextAction(WritingModel):
    action_id: str = Field(pattern=ACTION_RE.pattern)
    kind: Literal["review", "search", "download", "read", "assessment", "interpretation", "revision"]
    gap_ids: List[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=5000)
    payload: Dict[str, Any] = Field(default_factory=dict)
    material: Dict[str, Any] = Field(default_factory=dict)
    requires_agent: bool = False


class FeedbackAssessment(WritingModel):
    kind: Literal["assessment"] = "assessment"
    assessments: List[WritingAssessment] = Field(min_length=1, max_length=60)
    selected_paper_ids: Optional[List[str]] = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def validate_ids(self):
        _unique([a.paper_id for a in self.assessments], "文献判断 ID")
        if self.selected_paper_ids is not None:
            _unique(self.selected_paper_ids, "选用文献 ID")
        return self


class FeedbackInterpretation(WritingModel):
    kind: Literal["interpretation"] = "interpretation"
    reading: ReadingCard
    read_more: Optional[StrictBool] = None


class FeedbackRevision(WritingModel):
    kind: Literal["revision"] = "revision"
    draft: WritingDraft
    change_note: str = Field(min_length=1, max_length=1000)


class ReadingLoopState(WritingModel):
    project_id: Optional[str] = None
    loop_revision: Optional[StrictInt] = Field(default=None, ge=1)
    project_revision: Optional[StrictInt] = Field(default=None, ge=1)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    context_fingerprint: Optional[str] = None
    status: Literal[
        "idle", "awaiting_review", "awaiting_assessment",
        "awaiting_interpretation", "awaiting_revision",
        "ready", "executing", "paused", "stopped"
    ]
    stop_reason: Optional[Literal[
        "evidence_sufficient", "budget_exhausted", "no_new_information",
        "needs_user_material", "needs_manual_review", "source_unavailable",
        "no_open_fulltext", "paused_by_user", "stopped_by_user", "context_changed"
    ]] = None
    stop_detail: str = Field(default="", max_length=2000)
    budget: LoopBudget = Field(default_factory=LoopBudget)
    usage: LoopUsage = Field(default_factory=LoopUsage)
    reserved: LoopReserved = Field(default_factory=LoopReserved)
    inherited_baseline: InheritedBaseline = Field(default_factory=InheritedBaseline)
    round_index: StrictInt = Field(default=0, ge=0)
    no_progress_rounds: StrictInt = Field(default=0, ge=0)
    next_action: Optional[NextAction] = None
    rounds: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    reviews: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    actions: List[Dict[str, Any]] = Field(default_factory=list, max_length=500)
    request: str = Field(default="", max_length=4000)


def generate_action_id() -> str:
    import uuid
    return f"act-{uuid.uuid4().hex[:24]}"


def generate_review_id() -> str:
    import uuid
    return f"rev-{uuid.uuid4().hex[:24]}"


def generate_ticket_id() -> str:
    import uuid
    return f"ticket-{uuid.uuid4().hex[:24]}"


def calculate_context_fingerprint(
    project_data: Dict[str, Any],
    evidence_matrix: Optional[List[Dict[str, Any]]] = None,
    paper_states: Optional[Dict[str, Dict[str, Any]]] = None
) -> str:
    """Calculates a deterministic 64-hex SHA-256 fingerprint of project state, paper shas, and full body evidence."""
    pid = project_data.get("project_id", "")
    rev = project_data.get("revision", 0)
    profile = project_data.get("profile", {})
    candidates = sorted([
        (c.get("paper_id", ""), c.get("metadata_sha256", ""))
        for c in project_data.get("candidates", [])
    ])
    selected = sorted(project_data.get("selected_paper_ids", []))

    states = paper_states or project_data.get("paper_states") or {}
    paper_shas = sorted([
        (p, s.get("file_sha256", ""), s.get("metadata_sha256", ""))
        for p, s in states.items()
    ])

    ev_items = []
    matrix = evidence_matrix if evidence_matrix is not None else project_data.get("evidence_matrix", [])
    for row in matrix:
        nested_ev = []
        for ev in row.get("evidence", []):
            nested_ev.append((
                ev.get("fragment_id", ""),
                ev.get("page_number", 0),
                unicodedata.normalize("NFKC", str(ev.get("quote", "")).strip())
            ))
        nested_ev.sort()
        ev_items.append((
            row.get("citation_id", ""),
            row.get("paper_id", ""),
            row.get("reading_id", ""),
            row.get("file_sha256", ""),
            row.get("section", ""),
            row.get("kind", ""),
            unicodedata.normalize("NFKC", str(row.get("text", "")).strip()),
            nested_ev
        ))
    ev_items.sort()

    draft = project_data.get("draft")
    stable_repr = {
        "project_id": pid,
        "revision": rev,
        "profile": profile,
        "reading_request": project_data.get("reading_request", ""),
        "candidates": candidates,
        "selected": selected,
        "paper_shas": paper_shas,
        "evidence": ev_items,
        "draft": draft,
    }
    raw = json.dumps(stable_repr, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
