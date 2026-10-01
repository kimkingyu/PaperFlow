"""Domain models and integrity constraints for research planning."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


def utc_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


class PlanningError(ValueError):
    """Domain-specific error with an error code and message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ENTITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:^|[?&#]|(?<=\s))(?:password|passwd|pwd|token|access[-_]?token|"
    r"api[-_]?(?:key|token)|secret(?:[-_]?key)?|client[-_]?secret|authorization|credentials)"
    r"\s*[:=]\s*[^&\s]+",
    re.IGNORECASE,
)
SENSITIVE_QUERY_KEYS = {
    "token", "accesstoken", "apikey", "apitoken", "secret", "secretkey",
    "clientsecret", "password", "passwd", "pwd", "auth", "authorization",
    "credential", "credentials", "signature", "privatekey", "awssecretaccesskey",
}


def validate_entity_id(v: str) -> str:
    if not isinstance(v, str) or not ENTITY_ID_PATTERN.fullmatch(v):
        raise ValueError("ID 格式不合法：必须以英文字母或数字开头，长度为 1-64，仅包含字母、数字、下划线及减号")
    return v


def check_no_credentials(val: str) -> str:
    """Check references only; never open, resolve or fetch their destinations."""
    if not val:
        return val
    if "://" in val or val.startswith("//"):
        try:
            parts = urlsplit(val)
        except ValueError:
            # urlsplit errors can contain the original credential-bearing netloc.
            raise ValueError("引用 URI 格式无效，请移除凭证并检查格式") from None
        if parts.username or parts.password:
            raise ValueError("引用路径/URL 不得携带用户名或密码凭证")
        for component in (parts.query, parts.fragment):
            for key, _ in parse_qsl(component, keep_blank_values=True):
                normalized = key.lower().replace("_", "").replace("-", "")
                if normalized in SENSITIVE_QUERY_KEYS or normalized.startswith(("xamz", "xgoog")):
                    raise ValueError("引用路径/URL 不得携带密钥、Token 或密码凭证")
    if SECRET_KEY_PATTERN.search(val):
        raise ValueError("引用路径中包含可疑凭证或密钥参数")
    return val


EntityId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
NonEmptyStr300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
NonEmptyStr2000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
NonEmptyStr4000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


def _calendar_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("日期必须是 YYYY-MM-DD 格式的有效日期或 date 对象")


class PlanningModel(BaseModel):
    """Base model enforcing strictly validated, sanitized data."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    @model_validator(mode="before")
    @classmethod
    def strip_text_fields(cls, value: Any) -> Any:
        # Config string stripping does not apply to Literal fields in Pydantic.
        # Copy the input so normalization never mutates the caller's dictionary.
        if not isinstance(value, dict):
            return value
        normalized = {}
        for key, item in value.items():
            if isinstance(item, str):
                item = item.strip()
            elif isinstance(item, list):
                item = [entry.strip() if isinstance(entry, str) else entry for entry in item]
            normalized[key] = item
        return normalized


class ResourceItem(PlanningModel):
    name: NonEmptyStr300
    status: Literal["confirmed", "unverified"] = "unverified"
    notes: str = Field(default="", max_length=2000)
    source_ref: str = Field(default="", max_length=2000)

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, v: str) -> str:
        return check_no_credentials(v)


class Decision(PlanningModel):
    text: NonEmptyStr2000
    status: Literal["proposed", "confirmed"] = "proposed"
    source_ref: str = Field(default="", max_length=2000)

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, v: str) -> str:
        return check_no_credentials(v)


class ProjectProfile(PlanningModel):
    title: NonEmptyStr300
    goal: NonEmptyStr4000
    focus: str = Field(default="", max_length=4000)
    constraints: List[NonEmptyStr2000] = Field(default_factory=list, max_length=30)
    resources: List[ResourceItem] = Field(default_factory=list, max_length=30)
    decisions: List[Decision] = Field(default_factory=list, max_length=100)
    open_questions: List[NonEmptyStr2000] = Field(default_factory=list, max_length=100)
    start_date: Optional[date] = None
    target_date: Optional[date] = None

    @field_validator("start_date", "target_date", mode="before")
    @classmethod
    def validate_calendar_dates(cls, value: Any) -> Optional[date]:
        return _calendar_date(value)

    @model_validator(mode="after")
    def validate_dates(self) -> "ProjectProfile":
        if self.start_date is not None and self.target_date is not None:
            if self.start_date > self.target_date:
                raise ValueError("开始日期 (start_date) 不得晚于目标日期 (target_date)")
        return self


class ResearchQuestion(PlanningModel):
    id: EntityId
    question: NonEmptyStr4000
    hypothesis: str = Field(default="", max_length=4000)
    baseline: str = Field(default="", max_length=4000)
    minimum_change: str = Field(default="", max_length=4000)
    continue_condition: str = Field(default="", max_length=4000)
    stop_condition: str = Field(default="", max_length=4000)
    status: Literal["proposed", "testing", "supported", "refuted", "inconclusive"] = "proposed"
    evidence_ids: List[EntityId] = Field(default_factory=list, max_length=200)
    assessment_note: str = Field(default="", max_length=4000)


class Experiment(PlanningModel):
    id: EntityId
    question_id: EntityId
    title: NonEmptyStr300
    comparisons: List[NonEmptyStr2000] = Field(default_factory=list, max_length=30)
    metrics: List[NonEmptyStr2000] = Field(default_factory=list, max_length=30)
    protocol: str = Field(default="", max_length=12000)
    acceptance_condition: str = Field(default="", max_length=4000)
    status: Literal["planned", "running", "completed", "failed", "inconclusive"] = "planned"
    evidence_ids: List[EntityId] = Field(default_factory=list, max_length=200)
    result_summary: str = Field(default="", max_length=4000)


class Evidence(PlanningModel):
    id: EntityId
    kind: Literal["measurement", "simulation", "literature", "note", "artifact"]
    source_ref: NonEmptyStr2000
    summary: NonEmptyStr4000
    verification: Literal["unverified", "verified", "retracted"] = "unverified"
    verification_note: str = Field(default="", max_length=4000)
    verified_by: str = Field(default="", max_length=300)

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, v: str) -> str:
        return check_no_credentials(v)

    @model_validator(mode="after")
    def validate_verification_integrity(self) -> "Evidence":
        if self.verification == "verified":
            if not self.verification_note.strip():
                raise ValueError("核验通过 (verified) 的证据必须提供非空的核验说明 (verification_note)")
            if not self.verified_by.strip():
                raise ValueError("核验通过 (verified) 的证据必须提供非空的核验人 (verified_by)")
        return self


class PlanningTask(PlanningModel):
    id: EntityId
    text: NonEmptyStr4000
    stage: str = Field(default="", max_length=300)
    completion_condition: NonEmptyStr4000
    status: Literal["todo", "doing", "done", "blocked"] = "todo"
    depends_on: List[EntityId] = Field(default_factory=list, max_length=200)
    experiment_id: Optional[EntityId] = None
    due_date: Optional[date] = None
    artifact_refs: List[NonEmptyStr2000] = Field(default_factory=list, max_length=100)
    completion_note: str = Field(default="", max_length=4000)
    blocked_reason: str = Field(default="", max_length=4000)

    @field_validator("due_date", mode="before")
    @classmethod
    def validate_due_date(cls, value: Any) -> Optional[date]:
        return _calendar_date(value)

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, refs: List[str]) -> List[str]:
        for ref in refs:
            check_no_credentials(ref)
        return refs


class ResearchPlan(PlanningModel):
    profile: ProjectProfile
    questions: List[ResearchQuestion] = Field(default_factory=list, max_length=200)
    experiments: List[Experiment] = Field(default_factory=list, max_length=200)
    evidence: List[Evidence] = Field(default_factory=list, max_length=200)
    tasks: List[PlanningTask] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_plan_integrity(self) -> "ResearchPlan":
        # 1. 跨所有实体检测重复 ID
        all_ids: Dict[str, str] = {}
        for q in self.questions:
            if q.id in all_ids:
                raise ValueError(f"实体 ID 重复: '{q.id}' (已被 {all_ids[q.id]} 使用)")
            all_ids[q.id] = "ResearchQuestion"

        for e in self.experiments:
            if e.id in all_ids:
                raise ValueError(f"实体 ID 重复: '{e.id}' (已被 {all_ids[e.id]} 使用)")
            all_ids[e.id] = "Experiment"

        for ev in self.evidence:
            if ev.id in all_ids:
                raise ValueError(f"实体 ID 重复: '{ev.id}' (已被 {all_ids[ev.id]} 使用)")
            all_ids[ev.id] = "Evidence"

        for t in self.tasks:
            if t.id in all_ids:
                raise ValueError(f"实体 ID 重复: '{t.id}' (已被 {all_ids[t.id]} 使用)")
            all_ids[t.id] = "PlanningTask"

        q_map = {q.id: q for q in self.questions}
        e_map = {e.id: e for e in self.experiments}
        ev_map = {ev.id: ev for ev in self.evidence}
        t_map = {t.id: t for t in self.tasks}

        # 2. 检查列表内部重复引用
        for q in self.questions:
            if len(q.evidence_ids) != len(set(q.evidence_ids)):
                raise ValueError(f"研究问题 {q.id} 的 evidence_ids 中包含重复引用")
            for eid in q.evidence_ids:
                if eid not in ev_map:
                    raise ValueError(f"研究问题 {q.id} 引用的证据 '{eid}' 不存在于当前计划")

        for e in self.experiments:
            if len(e.evidence_ids) != len(set(e.evidence_ids)):
                raise ValueError(f"实验 {e.id} 的 evidence_ids 中包含重复引用")
            if e.question_id not in q_map:
                raise ValueError(f"实验 {e.id} 关联的研究问题 '{e.question_id}' 不存在于当前计划")
            for eid in e.evidence_ids:
                if eid not in ev_map:
                    raise ValueError(f"实验 {e.id} 引用的证据 '{eid}' 不存在于当前计划")

        for t in self.tasks:
            if len(t.depends_on) != len(set(t.depends_on)):
                raise ValueError(f"任务 {t.id} 的 depends_on 中包含重复依赖")
            if len(t.artifact_refs) != len(set(t.artifact_refs)):
                raise ValueError(f"任务 {t.id} 的 artifact_refs 中包含重复产物引用")
            if t.experiment_id is not None and t.experiment_id not in e_map:
                raise ValueError(f"任务 {t.id} 关联的实验 '{t.experiment_id}' 不存在于当前计划")
            for dep_id in t.depends_on:
                if dep_id == t.id:
                    raise ValueError(f"任务 {t.id} 不能依赖自身")
                if dep_id not in t_map:
                    raise ValueError(f"任务 {t.id} 依赖的前置任务 '{dep_id}' 不存在于当前计划")

        # 3. 依赖环检测 (拓扑排序 / DFS 3-color)
        visited: Dict[str, int] = {t.id: 0 for t in self.tasks}  # 0: unvisited, 1: visiting, 2: visited

        def dfs(node: str, path: List[str]) -> None:
            visited[node] = 1
            for neighbor in t_map[node].depends_on:
                if visited[neighbor] == 1:
                    cycle = " -> ".join(path + [neighbor])
                    raise ValueError(f"检测到任务循环依赖: {cycle}")
                if visited[neighbor] == 0:
                    dfs(neighbor, path + [neighbor])
            visited[node] = 2

        for t in self.tasks:
            if visited[t.id] == 0:
                dfs(t.id, [t.id])

        # 4. 任务状态与前置依赖约束
        for t in self.tasks:
            if t.status == "done":
                if not (t.completion_note.strip() or len(t.artifact_refs) > 0):
                    raise ValueError(f"已完成任务 {t.id} 必须提供 completion_note 或 artifact_refs")
            elif t.status == "blocked":
                if not t.blocked_reason.strip():
                    raise ValueError(f"阻塞任务 {t.id} 必须提供 blocked_reason")

            if t.status in ("done", "doing"):
                for dep_id in t.depends_on:
                    dep_task = t_map[dep_id]
                    if dep_task.status != "done":
                        raise ValueError(
                            f"任务 {t.id} 状态为 {t.status}，但其前置任务 {dep_id} 状态为 {dep_task.status}（前置任务必须已完成）"
                        )

        # 5. 实验状态约束
        for e in self.experiments:
            if e.status == "completed":
                if not e.result_summary.strip():
                    raise ValueError(f"已完成实验 {e.id} 必须提供 result_summary")
                valid_evs = [ev_map[eid] for eid in e.evidence_ids if ev_map[eid].verification != "retracted"]
                if not valid_evs:
                    raise ValueError(f"已完成实验 {e.id} 必须至少关联 1 个未被撤回 (retracted) 的证据")

        # 6. 研究问题状态约束
        for q in self.questions:
            if q.status in ("supported", "refuted"):
                if not q.assessment_note.strip():
                    raise ValueError(f"状态为 {q.status} 的研究问题 {q.id} 必须提供 assessment_note")
                verified_evs = [
                    ev_map[eid]
                    for eid in q.evidence_ids
                    if ev_map[eid].verification == "verified"
                ]
                if not verified_evs:
                    raise ValueError(f"状态为 {q.status} 的研究问题 {q.id} 必须至少关联 1 个已核验 (verified) 的证据")

        return self
