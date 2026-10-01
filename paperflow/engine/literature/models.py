"""Source-attributed literature records and evidence-backed reading cards."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import Field, field_validator
from paperflow.engine.journals.models import JournalError, Model, utc_now

PAPER_ID_RE = re.compile(r"^paper-[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_QUERY_KEYS = {"token", "access_token", "api_key", "apikey", "key", "password", "secretkey", "email", "signature", "x-amz-signature"}


def paper_id_for(source_id: str, source_record: str) -> str:
    return "paper-" + hashlib.sha256((source_id + ":" + source_record).encode("utf-8")).hexdigest()[:24]


def validate_paper_id(value: str) -> str:
    if not isinstance(value, str) or not PAPER_ID_RE.fullmatch(value):
        raise JournalError("INVALID_PAPER_ID", "文献编号无效，请使用检索或导入返回的 paper_id")
    return value


def public_url(value: Any) -> str:
    """Reject credential-bearing metadata URLs; never fetch while normalizing."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            return ""
        if parts.port not in (None, 80, 443):
            return ""
        if any(key.lower() in SECRET_QUERY_KEYS or key.lower().startswith("x-amz-") for key, _ in parse_qsl(parts.query)):
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return ""


class FullTextLocation(Model):
    url: str = ""
    landing_url: str = ""
    source_id: str = ""
    version: str = "unknown"
    license: Optional[str] = None
    is_open: bool = False
    access_evidence: str = ""

    @field_validator("url", "landing_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if value and not public_url(value):
            raise ValueError("全文链接含凭据或不是公开 HTTP(S) 地址")
        return value


class PaperRecord(Model):
    paper_id: str
    source_id: str
    source_record: str
    title: str = Field(default="", max_length=3000)
    authors: List[str] = Field(default_factory=list, max_length=500)
    publication_date: Optional[str] = None
    year: Optional[int] = Field(default=None, ge=1500, le=2200)
    venue: str = ""
    publication_type: str = "unknown"
    abstract: str = Field(default="", max_length=100000)
    doi: str = ""
    arxiv_id: str = ""
    version: str = "unknown"
    landing_url: str = ""
    fulltext_locations: List[FullTextLocation] = Field(default_factory=list, max_length=50)
    related_identifiers: List[Dict[str, str]] = Field(default_factory=list, max_length=50)
    sources: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    retrieved_at: str = Field(default_factory=utc_now)
    query: str = ""

    @field_validator("paper_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_paper_id(value)

    @field_validator("landing_url")
    @classmethod
    def safe_landing(cls, value: str) -> str:
        if value and not public_url(value):
            raise ValueError("落地页不是公开地址")
        return value


SECTIONS = ("research_question", "method", "assumptions", "baselines_experiments", "results", "limitations", "relevance", "reusable_parts", "code_data")


class ReadingEvidence(Model):
    fragment_id: str = Field(min_length=1, max_length=100)
    page_number: Optional[int] = Field(default=None, ge=1)
    quote: str = Field(min_length=1, max_length=1000)
    match_method: Literal["exact", "normalized_whitespace"] = "exact"


class ReadingClaim(Model):
    section: Literal["research_question", "method", "assumptions", "baselines_experiments", "results", "limitations", "relevance", "reusable_parts", "code_data"]
    kind: Literal["author_claim", "agent_inference", "unverified"] = "unverified"
    text: str = Field(min_length=1, max_length=5000)
    evidence: List[ReadingEvidence] = Field(default_factory=list, max_length=20)


class ReadingCard(Model):
    paper_id: str
    file_sha256: str
    summary: str = Field(default="", max_length=10000)
    claims: List[ReadingClaim] = Field(default_factory=list, max_length=100)

    @field_validator("paper_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_paper_id(value)

    @field_validator("file_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("必须使用读取正文返回的文件 SHA-256")
        return value
