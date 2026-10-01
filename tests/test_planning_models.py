"""Unit tests for research planning domain models and integrity constraints."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from paperflow.engine.planning.models import (
    Decision,
    Evidence,
    Experiment,
    PlanningError,
    PlanningTask,
    ProjectProfile,
    ResearchPlan,
    ResearchQuestion,
    ResourceItem,
    check_no_credentials,
    utc_now,
    validate_entity_id,
)


def test_utc_now_format():
    now = utc_now()
    assert isinstance(now, str)
    assert "+00:00" in now or "Z" in now


def test_validate_entity_id():
    assert validate_entity_id("exp_01") == "exp_01"
    assert validate_entity_id("Q-1") == "Q-1"
    assert validate_entity_id("task123") == "task123"

    with pytest.raises(ValueError):
        validate_entity_id("")
    with pytest.raises(ValueError):
        validate_entity_id("-invalid")
    with pytest.raises(ValueError):
        validate_entity_id("bad@id")
    with pytest.raises(ValueError):
        validate_entity_id("a" * 65)


def test_check_no_credentials():
    # 正常引用放行
    assert check_no_credentials("siyuan://blocks/20210815102030-1234567")
    assert check_no_credentials("D:\\data\\paper_sample.pdf")
    assert check_no_credentials("/home/user/workspace/result.csv")
    assert check_no_credentials("https://arxiv.org/abs/2301.00001")
    assert check_no_credentials("doi:10.1000/182")

    # 包含敏感凭据拦截
    with pytest.raises(ValueError, match="凭证|密码|密钥"):
        check_no_credentials("http://user:secret123@example.com/data")

    with pytest.raises(ValueError, match="凭证|密码|密钥"):
        check_no_credentials("https://example.com/api?token=my_secret_token")

    with pytest.raises(ValueError, match="凭证|密码|密钥"):
        check_no_credentials("https://example.com/api?api_key=abcdef")

    with pytest.raises(ValueError, match="凭证|密码|密钥"):
        check_no_credentials("s3://bucket/data.csv?secret_key=xyz")


def test_project_profile_valid_and_strip():
    profile = ProjectProfile(
        title="  高性能端侧AI编译器优化  ",
        goal="  提升在低功耗NPU上的推理能效比  ",
        constraints=[" 算力受限 <= 2W ", " 内存限制 <= 512MB "],
        start_date=date(2026, 1, 1),
        target_date=date(2026, 12, 31),
    )
    assert profile.title == "高性能端侧AI编译器优化"
    assert profile.goal == "提升在低功耗NPU上的推理能效比"
    assert profile.constraints == ["算力受限 <= 2W", "内存限制 <= 512MB"]


def test_project_profile_date_validation():
    with pytest.raises(ValidationError, match="不得晚于"):
        ProjectProfile(
            title="测试项目",
            goal="测试目标",
            start_date=date(2026, 12, 31),
            target_date=date(2026, 1, 1),
        )


def test_project_profile_empty_fields():
    with pytest.raises(ValidationError):
        ProjectProfile(title="   ", goal="目标")
    with pytest.raises(ValidationError):
        ProjectProfile(title="标题", goal="")


def test_evidence_verification_integrity():
    # unverified 不需要 verification_note / verified_by
    ev1 = Evidence(
        id="ev_01",
        kind="measurement",
        source_ref="experiments/run1.csv",
        summary="初步延迟测量",
        verification="unverified",
    )
    assert ev1.verification == "unverified"

    # verified 缺少 verification_note 报错
    with pytest.raises(ValidationError, match="verification_note"):
        Evidence(
            id="ev_02",
            kind="measurement",
            source_ref="experiments/run2.csv",
            summary="复现延迟测量",
            verification="verified",
            verified_by="Reviewer A",
            verification_note="   ",
        )

    # verified 缺少 verified_by 报错
    with pytest.raises(ValidationError, match="verified_by"):
        Evidence(
            id="ev_03",
            kind="measurement",
            source_ref="experiments/run3.csv",
            summary="复现延迟测量",
            verification="verified",
            verified_by="",
            verification_note="核验脚本通过",
        )

    # verified 完整有效
    ev_ok = Evidence(
        id="ev_ok",
        kind="simulation",
        source_ref="sim/profile.json",
        summary="仿真数据",
        verification="verified",
        verification_note="独立验证脚本校验Hash一致",
        verified_by="Tester",
    )
    assert ev_ok.verification == "verified"


def test_evidence_credentials_rejected():
    with pytest.raises(ValidationError, match="凭证|密码|密钥"):
        Evidence(
            id="ev_leak",
            kind="note",
            source_ref="https://example.com/doc?token=secret123",
            summary="泄露凭证的引用",
        )


def test_valid_minimal_plan():
    plan = ResearchPlan(
        profile=ProjectProfile(
            title="端侧编译器规划",
            goal="建立完整的端侧优化实验流程",
        )
    )
    dumped = plan.model_dump(mode="json")
    assert dumped["profile"]["title"] == "端侧编译器规划"
    assert dumped["questions"] == []
    assert dumped["tasks"] == []


def test_duplicate_entity_id_across_types():
    with pytest.raises(ValidationError, match="实体 ID 重复"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            questions=[
                ResearchQuestion(id="dup_id", question="问题1"),
            ],
            experiments=[
                Experiment(id="dup_id", question_id="dup_id", title="实验1"),
            ],
        )


def test_reference_integrity_missing_question():
    with pytest.raises(ValidationError, match="关联的研究问题 'q_not_found' 不存在"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            experiments=[
                Experiment(id="exp_01", question_id="q_not_found", title="实验1"),
            ],
        )


def test_reference_integrity_missing_evidence():
    with pytest.raises(ValidationError, match="引用的证据 'ev_not_found' 不存在"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            questions=[
                ResearchQuestion(id="q_01", question="问题1", evidence_ids=["ev_not_found"]),
            ],
        )


def test_reference_integrity_missing_task_dependency():
    with pytest.raises(ValidationError, match="依赖的前置任务 't_non_exist' 不存在"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(
                    id="t_01",
                    text="任务1",
                    completion_condition="完成标准",
                    depends_on=["t_non_exist"],
                ),
            ],
        )


def test_duplicate_references_rejected():
    # 重复 evidence_ids
    with pytest.raises(ValidationError, match="重复引用"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            evidence=[
                Evidence(id="ev_01", kind="note", source_ref="doc.txt", summary="摘要"),
            ],
            questions=[
                ResearchQuestion(id="q_01", question="问题1", evidence_ids=["ev_01", "ev_01"]),
            ],
        )

    # 重复 depends_on
    with pytest.raises(ValidationError, match="重复依赖"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_01", text="任务1", completion_condition="标准"),
                PlanningTask(id="t_02", text="任务2", completion_condition="标准", depends_on=["t_01", "t_01"]),
            ],
        )


def test_task_self_dependency():
    with pytest.raises(ValidationError, match="不能依赖自身"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_01", text="任务1", completion_condition="标准", depends_on=["t_01"]),
            ],
        )


def test_task_cycle_dependency():
    # A -> B -> A 环
    with pytest.raises(ValidationError, match="循环依赖"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_a", text="任务A", completion_condition="标准", depends_on=["t_b"]),
                PlanningTask(id="t_b", text="任务B", completion_condition="标准", depends_on=["t_a"]),
            ],
        )

    # A -> B -> C -> A 环
    with pytest.raises(ValidationError, match="循环依赖"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_1", text="任务1", completion_condition="标准", depends_on=["t_3"]),
                PlanningTask(id="t_2", text="任务2", completion_condition="标准", depends_on=["t_1"]),
                PlanningTask(id="t_3", text="任务3", completion_condition="标准", depends_on=["t_2"]),
            ],
        )


def test_task_status_constraints():
    # done 状态必须有 completion_note 或 artifact_refs
    with pytest.raises(ValidationError, match="必须提供 completion_note 或 artifact_refs"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(
                    id="t_done_empty",
                    text="完成的任务",
                    completion_condition="标准",
                    status="done",
                    completion_note="   ",
                    artifact_refs=[],
                ),
            ],
        )

    # blocked 状态必须有 blocked_reason
    with pytest.raises(ValidationError, match="必须提供 blocked_reason"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(
                    id="t_blocked",
                    text="受阻任务",
                    completion_condition="标准",
                    status="blocked",
                    blocked_reason="   ",
                ),
            ],
        )


def test_task_dependencies_must_be_done_for_done_or_doing():
    # doing 任务的前置依赖不是 done
    with pytest.raises(ValidationError, match="前置任务.*必须已完成"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_pre", text="前置", completion_condition="标准", status="todo"),
                PlanningTask(id="t_curr", text="当前", completion_condition="标准", status="doing", depends_on=["t_pre"]),
            ],
        )

    # done 任务的前置依赖不是 done
    with pytest.raises(ValidationError, match="前置任务.*必须已完成"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            tasks=[
                PlanningTask(id="t_pre", text="前置", completion_condition="标准", status="doing"),
                PlanningTask(
                    id="t_curr",
                    text="当前",
                    completion_condition="标准",
                    status="done",
                    completion_note="已完成",
                    depends_on=["t_pre"],
                ),
            ],
        )


def test_completed_experiment_constraints():
    # completed 缺少 result_summary
    with pytest.raises(ValidationError, match="必须提供 result_summary"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            evidence=[
                Evidence(id="ev_01", kind="measurement", source_ref="res.csv", summary="结果"),
            ],
            questions=[
                ResearchQuestion(id="q_01", question="问题1"),
            ],
            experiments=[
                Experiment(
                    id="exp_01",
                    question_id="q_01",
                    title="实验1",
                    status="completed",
                    evidence_ids=["ev_01"],
                    result_summary="   ",
                ),
            ],
        )

    # completed 证据全被 retracted
    with pytest.raises(ValidationError, match="未被撤回"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            evidence=[
                Evidence(
                    id="ev_01",
                    kind="measurement",
                    source_ref="res.csv",
                    summary="结果",
                    verification="retracted",
                ),
            ],
            questions=[
                ResearchQuestion(id="q_01", question="问题1"),
            ],
            experiments=[
                Experiment(
                    id="exp_01",
                    question_id="q_01",
                    title="实验1",
                    status="completed",
                    evidence_ids=["ev_01"],
                    result_summary="完成并产出结论",
                ),
            ],
        )


def test_supported_refuted_question_constraints():
    # supported 缺少 assessment_note
    with pytest.raises(ValidationError, match="必须提供 assessment_note"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            evidence=[
                Evidence(
                    id="ev_01",
                    kind="measurement",
                    source_ref="res.csv",
                    summary="结果",
                    verification="verified",
                    verification_note="OK",
                    verified_by="Tester",
                ),
            ],
            questions=[
                ResearchQuestion(
                    id="q_01",
                    question="问题1",
                    status="supported",
                    evidence_ids=["ev_01"],
                    assessment_note="   ",
                ),
            ],
        )

    # supported 证据未核验 (unverified)
    with pytest.raises(ValidationError, match="已核验"):
        ResearchPlan(
            profile=ProjectProfile(title="项目", goal="目标"),
            evidence=[
                Evidence(
                    id="ev_01",
                    kind="measurement",
                    source_ref="res.csv",
                    summary="结果",
                    verification="unverified",
                ),
            ],
            questions=[
                ResearchQuestion(
                    id="q_01",
                    question="问题1",
                    status="supported",
                    evidence_ids=["ev_01"],
                    assessment_note="该假说获得充分支持",
                ),
            ],
        )


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        ResearchPlan(
            profile=ProjectProfile(title="标题", goal="目标"),
            extra_field="disallowed",
        )


def test_planning_error_attributes():
    err = PlanningError("REVISION_CONFLICT", "版本冲突")
    assert isinstance(err, ValueError)
    assert err.code == "REVISION_CONFLICT"
    assert err.message == "版本冲突"
    assert str(err) == "版本冲突"


def complete_plan():
    return {
        "profile": {"title": "项目", "goal": "研究目标"},
        "questions": [{"id": "q1", "question": "研究问题"}],
        "experiments": [{"id": "exp1", "question_id": "q1", "title": "对照实验"}],
        "evidence": [{"id": "ev1", "kind": "measurement", "source_ref": "missing.csv", "summary": "测量记录"}],
        "tasks": [{"id": "t1", "text": "实验准备", "completion_condition": "脚本准备完毕"}],
    }


MODEL_INPUTS = [
    (ResourceItem, {"name": "资源"}, [("name", 300), ("notes", 2000), ("source_ref", 2000)]),
    (Decision, {"text": "决策"}, [("text", 2000), ("source_ref", 2000)]),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, [("title", 300), ("goal", 4000), ("focus", 4000)]),
    (ResearchQuestion, {"id": "q1", "question": "问题"}, [
        ("question", 4000), ("hypothesis", 4000), ("baseline", 4000), ("minimum_change", 4000),
        ("continue_condition", 4000), ("stop_condition", 4000), ("assessment_note", 4000),
    ]),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, [
        ("title", 300), ("protocol", 12000), ("acceptance_condition", 4000), ("result_summary", 4000),
    ]),
    (Evidence, {"id": "ev1", "kind": "note", "source_ref": "note.txt", "summary": "摘要"}, [
        ("source_ref", 2000), ("summary", 4000), ("verification_note", 4000), ("verified_by", 300),
    ]),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, [
        ("text", 4000), ("stage", 300), ("completion_condition", 4000),
        ("completion_note", 4000), ("blocked_reason", 4000),
    ]),
]
TEXT_BOUNDS = [
    (model, base, field, maximum)
    for model, base, fields in MODEL_INPUTS
    for field, maximum in fields
]


@pytest.mark.parametrize("model,base,field,maximum", TEXT_BOUNDS)
def test_every_text_field_bound_and_strip(model, base, field, maximum):
    data = dict(base, **{field: "  " + "a" * maximum + "  "})
    assert getattr(model.model_validate(data), field) == "a" * maximum
    data[field] = "a" * (maximum + 1)
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("model,base,field", [
    (ResourceItem, {"name": "资源"}, "name"),
    (Decision, {"text": "决策"}, "text"),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "title"),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "goal"),
    (ResearchQuestion, {"id": "q1", "question": "问题"}, "question"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "title"),
    (Evidence, {"id": "ev1", "kind": "note", "source_ref": "note.txt", "summary": "摘要"}, "source_ref"),
    (Evidence, {"id": "ev1", "kind": "note", "source_ref": "note.txt", "summary": "摘要"}, "summary"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "text"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "completion_condition"),
])
@pytest.mark.parametrize("empty", ["", " \n\t "])
def test_every_required_text_rejects_blank(model, base, field, empty):
    with pytest.raises(ValidationError):
        model.model_validate(dict(base, **{field: empty}))


LIST_CASES = [
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "constraints", 30, "限制"),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "resources", 30, {"name": "资源"}),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "decisions", 100, {"text": "决策"}),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "open_questions", 100, "开放问题"),
    (ResearchQuestion, {"id": "q1", "question": "问题"}, "evidence_ids", 200, "ev1"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "comparisons", 30, "基线"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "metrics", 30, "延迟"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "evidence_ids", 200, "ev1"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "depends_on", 200, "pre"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "artifact_refs", 100, "artifact.txt"),
]


@pytest.mark.parametrize("model,base,field,maximum,item", LIST_CASES)
def test_nested_list_count_bounds(model, base, field, maximum, item):
    assert len(getattr(model.model_validate(dict(base, **{field: [item] * maximum})), field)) == maximum
    with pytest.raises(ValidationError):
        model.model_validate(dict(base, **{field: [item] * (maximum + 1)}))


@pytest.mark.parametrize("model,base,field,maximum,item", [case for case in LIST_CASES if isinstance(case[-1], str)])
def test_list_string_items_reject_blank(model, base, field, maximum, item):
    with pytest.raises(ValidationError):
        model.model_validate(dict(base, **{field: [" \t "]}))


@pytest.mark.parametrize("model,base,field", [
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "constraints"),
    (ProjectProfile, {"title": "标题", "goal": "目标"}, "open_questions"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "comparisons"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "metrics"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "artifact_refs"),
])
def test_list_string_item_length_bounds(model, base, field):
    assert getattr(model.model_validate(dict(base, **{field: ["  " + "a" * 2000 + "  "]})), field) == ["a" * 2000]
    with pytest.raises(ValidationError):
        model.model_validate(dict(base, **{field: ["a" * 2001]}))


@pytest.mark.parametrize("section", ["questions", "experiments", "evidence", "tasks"])
def test_plan_entity_count_bounds(section):
    data = complete_plan()
    template = data[section][0]
    data[section] = [dict(template, id=f"{section}_{index}") for index in range(200)]
    # Other entities point only to the existing original question.
    if section == "questions":
        data["experiments"] = []
    assert len(getattr(ResearchPlan.model_validate(data), section)) == 200
    data[section].append(dict(template, id="over_limit"))
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("identifier", ["", " ", "-bad", "_bad", "中文", "a.b", "a/b", "a" * 65, None, True, 1])
def test_entity_id_fields_reject_invalid(identifier):
    with pytest.raises(ValidationError):
        ResearchQuestion(id=identifier, question="问题")


def test_entity_id_maximum_length_and_reference_strip():
    identifier = "x" * 64
    question = ResearchQuestion(id=" " + identifier + " ", question="问题", evidence_ids=["  ev1  "])
    assert question.id == identifier
    assert question.evidence_ids == ["ev1"]


@pytest.mark.parametrize("section", ["questions", "experiments", "evidence", "tasks"])
def test_duplicates_within_every_entity_type(section):
    data = complete_plan()
    data[section].append(deepcopy(data[section][0]))
    with pytest.raises(ValidationError, match="实体 ID 重复"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("left,right", [
    ("questions", "experiments"), ("questions", "evidence"), ("questions", "tasks"),
    ("experiments", "evidence"), ("experiments", "tasks"), ("evidence", "tasks"),
])
def test_duplicates_across_every_entity_pair(left, right):
    data = complete_plan()
    data[right][0]["id"] = data[left][0]["id"]
    with pytest.raises(ValidationError, match="实体 ID 重复"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("section,field,reference", [
    ("questions", "evidence_ids", ["absent"]),
    ("questions", "evidence_ids", ["exp1"]),
    ("experiments", "question_id", "absent"),
    ("experiments", "question_id", "ev1"),
    ("experiments", "evidence_ids", ["absent"]),
    ("experiments", "evidence_ids", ["q1"]),
    ("tasks", "depends_on", ["absent"]),
    ("tasks", "depends_on", ["exp1"]),
    ("tasks", "experiment_id", "absent"),
    ("tasks", "experiment_id", "q1"),
])
def test_cross_project_or_wrong_entity_references(section, field, reference):
    data = complete_plan()
    data[section][0][field] = reference
    with pytest.raises(ValidationError, match="不存在于当前计划"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("section,field,references", [
    ("questions", "evidence_ids", ["ev1", " ev1 "]),
    ("experiments", "evidence_ids", ["ev1", "ev1"]),
    ("tasks", "artifact_refs", ["missing.txt", " missing.txt "]),
])
def test_duplicate_references_after_normalization(section, field, references):
    data = complete_plan()
    data[section][0][field] = references
    with pytest.raises(ValidationError, match="重复"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("status", ["supported", "refuted"])
@pytest.mark.parametrize("verification", ["unverified", "retracted"])
def test_conclusions_require_current_verified_evidence(status, verification):
    data = complete_plan()
    data["questions"][0].update(status=status, assessment_note="评估记录", evidence_ids=["ev1"])
    data["evidence"][0]["verification"] = verification
    with pytest.raises(ValidationError, match="已核验"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("status", ["supported", "refuted"])
def test_explicit_verification_record_can_support_conclusion(status):
    data = complete_plan()
    data["questions"][0].update(status=status, assessment_note="提交者的评估记录", evidence_ids=["ev1"])
    data["evidence"][0].update(verification="verified", verification_note="提交者显式核验记录", verified_by="研究人员")
    assert ResearchPlan.model_validate(data).questions[0].status == status


def test_completed_experiment_accepts_unverified_not_retracted_evidence():
    data = complete_plan()
    data["experiments"][0].update(status="completed", result_summary="显式结果摘要", evidence_ids=["ev1"])
    assert ResearchPlan.model_validate(data).experiments[0].status == "completed"
    data["experiments"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("status", ["done", "doing"])
def test_done_dependencies_allow_task_progress(status):
    data = complete_plan()
    data["tasks"][0].update(status="done", completion_note="准备完成")
    data["tasks"].append({
        "id": "t2", "text": "运行实验", "completion_condition": "产生记录", "status": status,
        "depends_on": ["t1"], "completion_note": "完成记录",
    })
    assert ResearchPlan.model_validate(data).tasks[1].status == status


def test_done_task_accepts_unopened_artifact_and_does_not_promote_other_states():
    data = complete_plan()
    data["tasks"][0].update(status="done", experiment_id="exp1", artifact_refs=["nonexistent/out.csv"])
    plan = ResearchPlan.model_validate(data)
    assert plan.tasks[0].status == "done"
    assert plan.experiments[0].status == "planned"
    assert plan.questions[0].status == "proposed"


def test_long_acyclic_dependencies_and_cycle_at_limit():
    data = complete_plan()
    data["tasks"] = [
        {"id": f"t{i}", "text": "任务", "completion_condition": "标准", "depends_on": [f"t{i - 1}"] if i else []}
        for i in range(200)
    ]
    assert len(ResearchPlan.model_validate(data).tasks) == 200
    data["tasks"][0]["depends_on"] = ["t199"]
    with pytest.raises(ValidationError, match="循环依赖"):
        ResearchPlan.model_validate(data)


@pytest.mark.parametrize("model,base,status_field,value", [
    (ResourceItem, {"name": "资源"}, "status", "confirmed"),
    (Decision, {"text": "决策"}, "status", "confirmed"),
    (ResearchQuestion, {"id": "q1", "question": "问题"}, "status", "testing"),
    (Experiment, {"id": "exp1", "question_id": "q1", "title": "实验"}, "status", "running"),
    (Evidence, {"id": "ev1", "kind": "note", "source_ref": "note.txt", "summary": "摘要"}, "verification", "unverified"),
    (Evidence, {"id": "ev1", "source_ref": "note.txt", "summary": "摘要"}, "kind", "measurement"),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "status", "todo"),
])
def test_literal_strings_strip_and_reject_unknown(model, base, status_field, value):
    assert getattr(model.model_validate(dict(base, **{status_field: " " + value + " "})), status_field) == value
    with pytest.raises(ValidationError):
        model.model_validate(dict(base, **{status_field: "unsupported"}))


@pytest.mark.parametrize("reference", [
    "https://user:PRIVATE_SECRET@example.com/record",
    "https://example.com/?token=PRIVATE_SECRET",
    "https://example.com/?%61pi%5fkey=PRIVATE_SECRET",
    "https://example.com/?api-token=PRIVATE_SECRET",
    "https://example.com/?secretKey=PRIVATE_SECRET",
    "https://example.com/#access_token=PRIVATE_SECRET",
    "//user:PRIVATE_SECRET@example.com/record",
    "https://example.com/?x-amz-signature=PRIVATE_SECRET",
    "https://user:PRIVATE_SECRET＠example.com/record",
])
@pytest.mark.parametrize("model,base,field,is_list", [
    (Evidence, {"id": "ev1", "kind": "note", "source_ref": "record", "summary": "摘要"}, "source_ref", False),
    (ResourceItem, {"name": "资源"}, "source_ref", False),
    (Decision, {"text": "决策"}, "source_ref", False),
    (PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}, "artifact_refs", True),
])
def test_credential_references_reject_without_secret_in_error(reference, model, base, field, is_list):
    data = dict(base, **{field: [reference] if is_list else reference})
    with pytest.raises(ValidationError) as error:
        model.model_validate(data)
    assert "PRIVATE_SECRET" not in str(error.value)


@pytest.mark.parametrize("field", ["start_date", "target_date", "due_date"])
@pytest.mark.parametrize("invalid", [0, 1, 0.0, True, False, "2026-09-30T00:00:00", "20260930", "2026-02-30", datetime(2026, 9, 30)])
def test_date_fields_never_accept_timestamps_bool_or_invalid_date(field, invalid):
    if field == "due_date":
        model, data = PlanningTask, {"id": "t1", "text": "任务", "completion_condition": "标准"}
    else:
        model, data = ProjectProfile, {"title": "标题", "goal": "目标"}
    with pytest.raises(ValidationError):
        model.model_validate(dict(data, **{field: invalid}))


@pytest.mark.parametrize("value", [None, date(2026, 9, 30), " 2026-09-30 "])
def test_calendar_dates_accept_date_and_iso_date_only(value):
    profile = ProjectProfile(title="标题", goal="目标", start_date=value, target_date=value)
    task = PlanningTask(id="t1", text="任务", completion_condition="标准", due_date=value)
    expected = None if value is None else date(2026, 9, 30)
    assert profile.start_date == profile.target_date == task.due_date == expected


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_are_rejected_and_inputs_hidden(number):
    with pytest.raises(ValidationError) as error:
        ResearchPlan.model_validate({"profile": {"title": "标题", "goal": "目标", "start_date": number}})
    assert "input_value=" not in str(error.value)


@pytest.mark.parametrize("model,base,fields", MODEL_INPUTS)
def test_every_model_forbids_extra_fields_without_echoing_input(model, base, fields):
    with pytest.raises(ValidationError) as error:
        model.model_validate(dict(base, **{"extra": "PRIVATE_SECRET"}))
    assert "PRIVATE_SECRET" not in str(error.value)


def test_input_dictionary_is_not_modified_by_validation():
    data = complete_plan()
    data["profile"]["title"] = " 标题 "
    data["questions"][0]["status"] = " proposed "
    data["tasks"][0]["artifact_refs"] = [" missing.txt "]
    original = deepcopy(data)
    result = ResearchPlan.model_validate(data)
    assert data == original
    assert result.profile.title == "标题"
    assert result.tasks[0].artifact_refs == ["missing.txt"]


def test_default_lists_are_independent_between_plans():
    left = ResearchPlan(profile={"title": "左", "goal": "目标"})
    right = ResearchPlan(profile={"title": "右", "goal": "目标"})
    left.profile.constraints.append("限制")
    left.tasks.append(PlanningTask(id="t1", text="任务", completion_condition="标准"))
    assert not right.profile.constraints
    assert not right.tasks
