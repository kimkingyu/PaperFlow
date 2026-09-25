"""Validated, source-attributed records shared by importers and query engines."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class Provenance(Model):
    source_id: str = "local"
    source_url: str = ""
    source_version: str = ""
    source_record: str = ""
    retrieved_at: str = Field(default_factory=utc_now)
    observed_at: Optional[str] = None
    data_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    authority: Literal["community", "official", "user"] = "community"
    freshness: str = "unknown"

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("观察时间必须为合法的 ISO 日期或时间") from None
        return value

    @field_validator("source_url")
    @classmethod
    def no_credentials(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or any(
                key.lower() in {"secretkey", "token", "access_token", "api_key", "apikey", "uuid", "password"}
                for key, _ in parse_qsl(parsed.query)
            ):
                raise ValueError("证据 URL 不得携带密钥、账号或投稿标识")
        return value


class Ranking(Model):
    system: Literal["cas", "jcr", "xr", "ccf", "ccft"]
    year: Optional[int] = Field(default=None, ge=1900, le=2200)
    category: str = ""
    category_type: Literal["major", "minor", "subject"] = "subject"
    quartile: Optional[int] = Field(default=None, ge=1, le=4)
    grade: str = ""
    top: Optional[bool] = None
    raw: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class Metric(Model):
    name: str
    raw: str = ""
    value: Optional[float] = None
    lower: Optional[float] = None
    upper: Optional[float] = None
    comparator: Literal["eq", "lt", "le", "gt", "ge", "range", "unknown"] = "unknown"
    unit: str = ""
    year: Optional[int] = Field(default=None, ge=1900, le=2200)
    stage: str = ""
    currency: str = ""
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def valid_range(self):
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("指标区间下界不能大于上界")
        if self.name in {"impact_factor", "annual_articles", "apc", "first_decision_days", "review_days"}:
            if any(v is not None and v < 0 for v in (self.value, self.lower, self.upper)):
                raise ValueError("期刊数量、费用和周期指标不能为负数")
        return self


class RiskEvent(Model):
    system: Literal["cas_warning", "xr_review", "clarivate", "school"]
    value: Literal["flagged", "not_listed", "on_hold", "under_review", "delisted", "indexed", "resolved"]
    year: Optional[int] = Field(default=None, ge=1900, le=2200)
    reason: str = ""
    level: str = ""
    historical: bool = False
    provenance: Provenance = Field(default_factory=Provenance)


class Experience(Model):
    field: str = ""
    topic: str = ""
    summary: str = ""
    url: str = ""
    subjective: bool = True
    sample_size: Optional[int] = Field(default=None, ge=0)
    provenance: Provenance = Field(default_factory=Provenance)


class EditorialProfile(Model):
    """Dated scope facts and explicitly attributed editorial positioning, not a quality certificate."""
    scope_summary: str = Field(min_length=1, max_length=8000)
    topics: List[str] = Field(default_factory=list, max_length=30)
    article_types: List[str] = Field(default_factory=list, max_length=20)
    article_types_complete: bool = False
    positioning: Literal["application", "general", "field_leading", "unknown"] = "unknown"
    positioning_basis: str = Field(default="", max_length=1200)
    recent_papers: List[str] = Field(default_factory=list, max_length=10)
    provenance: Provenance = Field(default_factory=Provenance)


class PublicationFee(Model):
    """One quote, for one publication route; absent amounts are never zero."""
    route: Literal["subscription", "open_access", "diamond", "unknown"] = "unknown"
    kind: Literal["apc", "submission", "page", "colour", "other"] = "apc"
    amount: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="", max_length=3)
    unit: Literal["per_article", "per_page", "unknown"] = "per_article"
    optional: bool = False
    taxes_included: Optional[bool] = None
    note: str = Field(default="", max_length=1200)
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("currency")
    @classmethod
    def normalized_currency(cls, value):
        value = value.upper().strip()
        if value and (len(value) != 3 or not value.isascii() or not value.isalpha()):
            raise ValueError("费用币种必须为三位字母代码")
        return value

    @model_validator(mode="after")
    def amount_needs_currency(self):
        if self.amount is not None and not self.currency:
            raise ValueError("已知费用必须明确币种")
        return self


class JournalRecord(Model):
    journal_id: str = ""
    title: str = Field(min_length=1, max_length=1000)
    title_zh: str = ""
    kind: Literal["journal", "conference"] = "journal"
    publisher: str = ""
    # Official journal home page (publisher-hosted); empty when not verified.
    homepage: str = Field(default="", max_length=500)
    identity_warnings: List[str] = Field(default_factory=list)
    metadata_observations: List[Dict[str, Any]] = Field(default_factory=list)
    issns: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    fields: List[str] = Field(default_factory=list)
    indexing: List[str] = Field(default_factory=list)
    oa_mode: Literal["full", "hybrid", "closed", "diamond", "unknown"] = "unknown"
    provenance: Provenance = Field(default_factory=Provenance)
    rankings: List[Ranking] = Field(default_factory=list)
    metrics: List[Metric] = Field(default_factory=list)
    risks: List[RiskEvent] = Field(default_factory=list)
    experiences: List[Experience] = Field(default_factory=list)
    editorial_profiles: List[EditorialProfile] = Field(default_factory=list, max_length=20)
    publication_fees: List[PublicationFee] = Field(default_factory=list, max_length=40)

    @field_validator("homepage")
    @classmethod
    def safe_homepage(cls, value):
        value = (value or "").strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("期刊官网必须是不带账号信息的 https 地址")
        return value


class ParsedBatch(Model):
    records: List[JournalRecord] = Field(default_factory=list)
    rejected: List[Dict[str, Any]] = Field(default_factory=list)
    total_rows: int = 0
    rejected_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    # Coverage is descriptive; community mirrors cannot assert current official clearance.
    coverage: Dict[str, Any] = Field(default_factory=dict)


class SearchFilters(Model):
    field: str = ""
    kind: Literal["journal", "conference"] = "journal"
    indexing: List[str] = Field(default_factory=list)
    rank_system: Optional[Literal["cas", "jcr", "xr", "ccf", "ccft"]] = None
    rank_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    quartiles: List[int] = Field(default_factory=list)
    category: str = ""
    category_type: Optional[Literal["major", "minor", "subject"]] = None
    ccf_grades: List[str] = Field(default_factory=list)
    top: Optional[bool] = None
    oa_mode: Literal["any", "full", "hybrid", "closed", "diamond"] = "any"
    max_apc: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    currency: Optional[str] = None
    max_first_decision_days: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    min_annual_articles: Optional[int] = Field(default=None, ge=0)
    metric_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    risk_policy: Literal["exclude_known", "verified_only", "include_flagged"] = "exclude_known"
    warning_years: List[int] = Field(default_factory=list)
    allow_unknown: bool = False
    profile_id: Optional[str] = None
    dynamic_max_age_days: int = Field(default=90, ge=1, le=3650)
    status_max_age_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.quartiles and (self.rank_system not in ("cas", "jcr", "xr") or self.rank_year is None):
            raise ValueError("分区筛选必须明确 CAS/JCR/XR 体系和年度")
        if any(q not in (1, 2, 3, 4) for q in self.quartiles):
            raise ValueError("quartiles 只能包含 1、2、3、4")
        if any(y < 1900 or y > 2200 for y in self.warning_years):
            raise ValueError("预警核查年度应在 1900 到 2200 之间")
        if self.max_apc is not None and not self.currency:
            raise ValueError("费用上限必须指定币种，不能隐式换汇")
        if self.min_annual_articles is not None and self.metric_year is None:
            raise ValueError("年发文量筛选必须指定 metric_year")
        if self.ccf_grades and (self.rank_system != "ccf" or self.rank_year is None):
            raise ValueError("CCF 等级筛选必须指定 ccf 体系和年度")
        return self


class SchoolPolicy(Model):
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str
    source_url: str = ""
    effective_from: Optional[str] = None
    effective_until: Optional[str] = None
    required_indexing: List[str] = Field(default_factory=list)
    rank_system: Optional[Literal["cas", "jcr", "xr", "ccf", "ccft"]] = None
    rank_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    allowed_quartiles: List[int] = Field(default_factory=list)
    prohibited_issns: List[str] = Field(default_factory=list)
    prohibited_titles: List[str] = Field(default_factory=list)
    allow_oa: Optional[bool] = None

    @field_validator("effective_from", "effective_until")
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.allowed_quartiles and (self.rank_system is None or self.rank_year is None):
            raise ValueError("学校分区规则必须指定体系和年度")
        if any(q not in (1, 2, 3, 4) for q in self.allowed_quartiles):
            raise ValueError("学校允许分区必须为 1 到 4")
        return self


def response(data: Any, status: str = "success", **kwargs) -> Dict[str, Any]:
    result = {"status": status, "data": data, "sources": [], "coverage": {}, "warnings": [], "suggested_options": []}
    result.update(kwargs)
    return result
