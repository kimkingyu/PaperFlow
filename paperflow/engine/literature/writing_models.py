"""Strict caller-authored writing inputs and backend-generated identities."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Literal, Optional, Type, TypeVar
from urllib.parse import unquote

from pydantic import ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator
from paperflow.engine.journals.models import JournalError, Model
from .models import PaperRecord

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_QUERIES = 6
MAX_CANDIDATES = 60
MAX_SELECTED = 30
MAX_REVISION = 2 ** 63 - 2
PROJECT_RE = re.compile(r"^writing-[0-9a-f]{24}$")
CITATION_RE = re.compile(r"^cite-[0-9a-f]{24}$")
ENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DOI_RE = re.compile(r"^10\.[0-9]{4,9}/[^\s\x00-\x1f]+$", re.I)
ARXIV_RE = re.compile(r"^(?:[0-9]{4}\.[0-9]{4,5}|[a-z][a-z.-]*/[0-9]{7})(?:v[1-9][0-9]*)?$", re.I)
METADATA_FIELDS = (
    "paper_id", "source_id", "source_record", "title", "authors", "publication_date",
    "year", "venue", "abstract", "doi", "arxiv_id", "publication_type", "version", "landing_url",
)
T = TypeVar("T", bound=Model)


def budget_json(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
            raise JournalError("INPUT_TOO_LARGE", "写作输入或项目快照超过 2MiB 上限")
        return payload
    except JournalError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise JournalError("INVALID_INPUT", "写作输入必须是有效、有界的 JSON 数据") from None


def parse_model(model: Type[T], value: Any, code: str = "INVALID_INPUT") -> T:
    budget_json(value)
    try:
        return model.model_validate(value)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise JournalError(code, "写作输入结构、字段类型或标识无效，请使用准备接口返回的 schema") from None


def integer(value: Any, name: str, minimum: int = 0, maximum: int = MAX_REVISION) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise JournalError("INVALID_INPUT", name + " 必须是指定范围内的整数")
    return value


def project_id(value: Any) -> str:
    if not isinstance(value, str) or not PROJECT_RE.fullmatch(value):
        raise JournalError("INVALID_PROJECT_ID", "请使用创建接口返回的写作项目编号")
    return value


def input_id_for(text: str) -> str:
    return "input-" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def metadata_sha256(paper: Dict[str, Any]) -> str:
    stable = {key: paper.get(key) for key in METADATA_FIELDS}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def citation_id_for(paper_id: str, reading_id: str, index: int, sha: str) -> str:
    value = json.dumps([paper_id, reading_id, index, sha], separators=(",", ":"))
    return "cite-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def reliable_identity(paper: Dict[str, Any]):
    """Never merge by title, a related DOI, or an unversioned/preprint guess."""
    arxiv = str(paper.get("arxiv_id") or "").strip().lower()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if arxiv.startswith(prefix):
            arxiv = arxiv[len(prefix):]
            break
    if paper.get("publication_type") == "preprint" and ARXIV_RE.fullmatch(arxiv):
        return ("arxiv", arxiv) if re.search(r"v[1-9][0-9]*$", arxiv) else ("arxiv", arxiv, paper.get("version", "unknown"))
    doi = str(paper.get("doi") or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):].strip()
            break
    doi = unquote(doi).lower()
    if DOI_RE.fullmatch(doi):
        if paper.get("publication_type") == "preprint":
            return ("preprint-doi", doi, paper.get("version", "unknown"))
        return ("doi", doi)
    if ARXIV_RE.fullmatch(arxiv):
        return ("arxiv", arxiv, paper.get("publication_type", "unknown"), paper.get("version", "unknown"))
    return ("paper", paper["paper_id"])


def _unique(values: List[str], name: str) -> List[str]:
    if len(set(values)) != len(values):
        raise ValueError(name + " 不得重复")
    return values


class WritingModel(Model):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              hide_input_in_errors=True, str_strip_whitespace=True)


class WritingQuestion(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    question: str = Field(min_length=1, max_length=20000)


class OwnMaterial(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    text: str = Field(min_length=1, max_length=1000000)


class WritingProfile(WritingModel):
    title: str = Field(min_length=1, max_length=1000)
    research_question: str = Field(min_length=1, max_length=20000)
    context: str = Field(default="", max_length=1000000)
    language: Literal["zh", "en"] = "zh"
    sub_questions: List[WritingQuestion] = Field(default_factory=list, max_length=30)
    keywords: List[str] = Field(default_factory=list, max_length=30)
    own_materials: List[OwnMaterial] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_profile(self):
        if not self.sub_questions:
            self.sub_questions = [WritingQuestion(id="RQ1", question=self.research_question)]
        _unique([q.id for q in self.sub_questions], "研究问题 ID")
        _unique([m.id for m in self.own_materials], "用户材料 ID")
        if any(not word.strip() or len(word) > 200 for word in self.keywords):
            raise ValueError("关键词为空或过长")
        return self


class WritingQuery(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    query: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=5000)
    question_ids: List[str] = Field(default_factory=lambda: ["RQ1"], min_length=1, max_length=30)
    sources: List[Literal["crossref", "arxiv"]] = Field(default_factory=lambda: ["crossref", "arxiv"], min_length=1, max_length=2)
    year_from: Optional[StrictInt] = Field(default=None, ge=1500, le=2200)
    year_to: Optional[StrictInt] = Field(default=None, ge=1500, le=2200)

    @model_validator(mode="after")
    def validate_query(self):
        _unique(self.question_ids, "研究问题 ID")
        _unique(self.sources, "检索来源")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("检索年份倒置")
        return self


class WritingAssessment(WritingModel):
    paper_id: str = Field(pattern=r"^paper-[0-9a-f]{24}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relevance: Literal["core", "background", "marginal", "irrelevant"]
    reason: str = Field(min_length=1, max_length=10000)
    question_ids: List[str] = Field(default_factory=list, max_length=30)
    basis: Literal["metadata", "abstract", "fulltext"]
    limitations: List[str] = Field(default_factory=list, max_length=30)
    evidence_ids: List[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_assessment(self):
        _unique(self.question_ids, "研究问题 ID")
        _unique(self.evidence_ids, "证据 ID")
        if self.relevance in ("core", "background") and not self.question_ids:
            raise ValueError("核心或背景文献须关联研究问题")
        if self.basis == "fulltext" and not self.evidence_ids:
            raise ValueError("正文判断须提供实际证据 ID")
        if any(not item.strip() or len(item) > 5000 for item in self.limitations):
            raise ValueError("局限字段无效")
        if any(not CITATION_RE.fullmatch(item) for item in self.evidence_ids):
            raise ValueError("只能引用后台生成的证据 ID")
        return self


class WritingOutlineItem(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    title: str = Field(min_length=1, max_length=1000)
    level: StrictInt = Field(default=1, ge=1, le=3)
    purpose: str = Field(default="", max_length=10000)
    evidence_ids: List[str] = Field(default_factory=list, max_length=200)


class WritingParagraph(WritingModel):
    text: str = Field(default="", max_length=100000)
    kind: Literal["literature_summary", "literature_inference", "author_proposal", "user_material", "placeholder"]
    citation_ids: List[str] = Field(default_factory=list, max_length=200)
    own_material_ids: List[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_paragraph(self):
        _unique(self.citation_ids, "引用 ID")
        _unique(self.own_material_ids, "用户材料 ID")
        if self.kind != "placeholder" and not self.text.strip():
            raise ValueError("非占位段落正文不能为空")
        if self.kind in ("literature_summary", "literature_inference") and not self.citation_ids:
            raise ValueError("文献段落必须有正文出处")
        if self.kind == "user_material" and not self.own_material_ids:
            raise ValueError("用户材料段落必须绑定实际材料")
        if self.kind != "user_material" and self.own_material_ids:
            raise ValueError("仅用户材料段落可绑定用户材料")
        if self.kind == "placeholder" and (self.citation_ids or self.own_material_ids):
            raise ValueError("占位段落不能冒充证据")
        if any(not CITATION_RE.fullmatch(item) for item in self.citation_ids):
            raise ValueError("只能引用后台生成的引用 ID")
        return self


class WritingSection(WritingModel):
    id: str = Field(min_length=1, max_length=64, pattern=ENTITY_RE.pattern)
    title: str = Field(min_length=1, max_length=1000)
    level: StrictInt = Field(default=1, ge=1, le=3)
    paragraphs: List[WritingParagraph] = Field(default_factory=list, max_length=300)


class WritingDraft(WritingModel):
    title: str = Field(min_length=1, max_length=1000)
    language: Literal["zh", "en"] = "zh"
    outline: List[WritingOutlineItem] = Field(default_factory=list, max_length=100)
    sections: List[WritingSection] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_draft(self):
        _unique([s.id for s in self.sections], "章节 ID")
        _unique([o.id for o in self.outline], "大纲 ID")
        for item in self.outline:
            _unique(item.evidence_ids, "大纲证据 ID")
            if any(not CITATION_RE.fullmatch(c) for c in item.evidence_ids):
                raise ValueError("大纲只能引用后台证据 ID")
        return self


class QueryBatch(WritingModel):
    queries: List[WritingQuery] = Field(default_factory=list, max_length=MAX_QUERIES)

    @model_validator(mode="after")
    def validate_ids(self):
        _unique([q.id for q in self.queries], "检索计划 ID")
        return self


class AssessmentBatch(WritingModel):
    assessments: List[WritingAssessment] = Field(max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def validate_ids(self):
        _unique([a.paper_id for a in self.assessments], "文献判断 ID")
        return self


class StoredAssessment(WritingAssessment):
    origin: Literal["calling_agent", "gui_model", "nativeAgent"]
    assessed_at: str


class CandidateSourceRecord(WritingModel):
    paper_id: str = Field(pattern=r"^paper-[0-9a-f]{24}$")
    source_id: str
    source_record: str
    paper: PaperRecord
    matched_query_ids: List[str] = Field(max_length=6)

    @model_validator(mode="after")
    def validate_identity(self):
        if (self.paper_id, self.source_id, self.source_record) != (self.paper.paper_id, self.paper.source_id, self.paper.source_record):
            raise ValueError("候选来源身份与真实元数据不匹配")
        _unique(self.matched_query_ids, "来源检索 ID")
        return self


class StoredCandidate(WritingModel):
    paper_id: str = Field(pattern=r"^paper-[0-9a-f]{24}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper: PaperRecord
    matched_query_ids: List[str] = Field(max_length=6)
    source_records: List[CandidateSourceRecord]
    available: bool = True

    @model_validator(mode="after")
    def validate_metadata(self):
        if self.paper_id != self.paper.paper_id or self.metadata_sha256 != metadata_sha256(self.paper.model_dump(mode="json")):
            raise ValueError("候选元数据身份或指纹不匹配")
        _unique(self.matched_query_ids, "候选检索 ID")
        _unique([source.paper_id for source in self.source_records], "候选来源 ID")
        return self


class WritingSnapshot(WritingModel):
    project_id: str = Field(pattern=PROJECT_RE.pattern)
    revision: StrictInt = Field(ge=1, le=MAX_REVISION + 1)
    created_at: str
    updated_at: str
    profile: WritingProfile
    queries: List[WritingQuery] = Field(max_length=6)
    candidates: List[StoredCandidate] = Field(max_length=60)
    assessments: List[StoredAssessment] = Field(max_length=60)
    selected_paper_ids: List[str] = Field(max_length=30)
    draft: Optional[WritingDraft]
    draft_metadata_sha256: Dict[str, str]
    search_statuses: List[Dict[str, Any]] = Field(max_length=12)
    source_text: str
    input_id: str
    stage: Literal["needs_search_plan", "needs_agent_assessment", "needs_reading", "evidence_ready", "draft_ready"]

    @model_validator(mode="after")
    def validate_snapshot(self):
        candidates = {c.paper_id for c in self.candidates}
        _unique([c.paper_id for c in self.candidates], "候选 ID")
        _unique([a.paper_id for a in self.assessments], "判断 ID")
        _unique(self.selected_paper_ids, "选用 ID")
        _unique([q.id for q in self.queries], "检索 ID")
        if not set(self.selected_paper_ids).issubset(candidates) or any(a.paper_id not in candidates for a in self.assessments):
            raise ValueError("快照中存在跨项目文献编号")
        query_ids = {q.id for q in self.queries}
        question_ids = {q.id for q in self.profile.sub_questions}
        if any(not set(q.question_ids).issubset(question_ids) for q in self.queries) or any(not set(a.question_ids).issubset(question_ids) for a in self.assessments):
            raise ValueError("快照引用不存在的研究问题")
        for candidate in self.candidates:
            if not set(candidate.matched_query_ids).issubset(query_ids) or any(not set(s.matched_query_ids).issubset(query_ids) for s in candidate.source_records):
                raise ValueError("快照引用不存在的检索计划")
        if any(p not in candidates or not re.fullmatch(r"[0-9a-f]{64}", sha) for p, sha in self.draft_metadata_sha256.items()):
            raise ValueError("草稿元数据绑定无效")
        if self.input_id != (input_id_for(self.source_text) if self.source_text else ""):
            raise ValueError("快照原始材料身份不匹配")
        return self
