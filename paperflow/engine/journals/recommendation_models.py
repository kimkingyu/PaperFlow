"""Provider-neutral contracts: the calling agent supplies reasoning, never credentials."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field, field_validator, model_validator

from .models import Model, Provenance, SearchFilters

RUBRIC_VERSION = "journal-fit-v1"
BASE_WEIGHTS = {"scope": 45, "manuscript": 20, "goal": 15, "budget": 10, "speed": 10}
GROUP_LABELS = {
    "efficiency": "稳妥／效率档",
    "balanced": "均衡档",
    "stretch": "冲刺／领域顶刊档",
}


class ResearchProfile(Model):
    input_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["idea", "manuscript"]
    title: str = Field(default="", max_length=1000)
    summary: str = Field(min_length=1, max_length=4000)
    keywords: List[str] = Field(min_length=1, max_length=30)
    article_type: str = Field(default="", max_length=100)
    data_scale: Literal["unknown", "small_sample", "medium_sample", "large_benchmark"] = "unknown"
    data_openness: Literal["unknown", "public_benchmark", "private_domain", "experimental_sample"] = "unknown"
    model_paradigm: Literal["unknown", "statistical", "tree_ensemble", "deep_learning", "pretrained_transfer"] = "unknown"
    readiness: Literal["unknown", "planned", "pilot", "validated"] = "unknown"
    readiness_quotes: List[str] = Field(default_factory=list, max_length=8)
    significance: Literal["unknown", "field", "cross_field"] = "unknown"
    significance_quotes: List[str] = Field(default_factory=list, max_length=8)
    independent_validation_quotes: List[str] = Field(default_factory=list, max_length=8)

    @field_validator("keywords", "readiness_quotes", "significance_quotes", "independent_validation_quotes")
    @classmethod
    def bounded_strings(cls, values):
        if any(not v.strip() or len(v) > 500 for v in values):
            raise ValueError("关键词和依据应为非空的短文本")
        return list(dict.fromkeys(v.strip() for v in values))

    @model_validator(mode="after")
    def evidence_is_not_a_result_prediction(self):
        if self.mode == "idea" and self.readiness in ("pilot", "validated"):
            raise ValueError("想法模式不能声称已有验证成果")
        if self.readiness in ("pilot", "validated") and not self.readiness_quotes:
            raise ValueError("研究完成度判断需要稿件原文依据")
        if self.significance == "cross_field" and not self.significance_quotes:
            raise ValueError("跨领域意义判断需要稿件原文依据")
        return self


class FitEvidence(Model):
    manuscript_quote: str = Field(min_length=4, max_length=500)
    journal_quote: str = Field(min_length=4, max_length=500)


class FitAssessment(Model):
    journal_id: str = Field(min_length=1, max_length=100)
    context_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_fit: Optional[float] = Field(default=None, ge=0, le=100)
    manuscript_fit: Optional[float] = Field(default=None, ge=0, le=100)
    goal_fit: Optional[float] = Field(default=None, ge=0, le=100)
    evidence: List[FitEvidence] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=2000)
    gaps: List[str] = Field(default_factory=list, max_length=12)

    @field_validator("gaps")
    @classmethod
    def bounded_gaps(cls, values):
        if any(len(v) > 500 for v in values):
            raise ValueError("补强建议过长")
        return values


class CurrencyRate(Model):
    source_currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate: float = Field(gt=0)
    provenance: Provenance


class RecommendationPreferences(Model):
    goal: Literal["efficiency", "balanced", "impact"] = "balanced"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    max_budget: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    publication_route: Literal["any", "subscription", "open_access", "diamond"] = "any"
    max_decision_days: Optional[float] = Field(default=None, gt=0)
    decision_stage: Literal["first_decision", "peer_review", "acceptance"] = "first_decision"
    estimated_pages: Optional[int] = Field(default=None, ge=1, le=1000)
    per_group: int = Field(default=6, ge=1, le=20)
    candidate_limit: int = Field(default=50, ge=1, le=200)
    min_scope_fit: float = Field(default=50, ge=0, le=100)
    evidence_max_age_days: int = Field(default=90, ge=1, le=365)
    scope_max_age_days: int = Field(default=730, ge=1, le=3650)
    fx_rates: List[CurrencyRate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def coherent_preferences(self):
        if self.max_budget is not None and not self.currency:
            raise ValueError("预算必须指定币种")
        f = self.filters
        if (f.rank_system or f.category or f.category_type or f.top is not None) and (not f.rank_system or f.rank_year is None):
            raise ValueError("推荐的分区、类别和 Top 筛选必须明确体系与年度")
        if self.filters.risk_policy == "include_flagged":
            raise ValueError("推荐榜不收录已知风险期刊；查看风险刊请使用原始检索工具")
        if self.filters.max_apc is not None or self.filters.max_first_decision_days is not None:
            raise ValueError("推荐的预算和周期请使用 max_budget、max_decision_days，避免两套口径混用")
        return self
