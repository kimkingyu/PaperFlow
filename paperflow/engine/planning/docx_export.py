"""Export a planning snapshot, not a manuscript or a scientific certification."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from paperflow.engine.docx_builder import AcademicDocxBuilder

from .models import PlanningError

STATUS_LABELS = {
    "unverified": "待核实", "verified": "提交方标注已核验", "retracted": "已撤回",
    "confirmed": "已确认", "proposed": "待讨论/待验证", "testing": "验证中",
    "supported": "提交方判断为支持", "refuted": "提交方判断为反驳", "inconclusive": "尚无定论",
    "planned": "计划中", "running": "进行中", "completed": "提交方记录已完成",
    "failed": "失败", "todo": "待办", "doing": "进行中", "done": "已完成", "blocked": "受阻",
}
KIND_LABELS = {
    "measurement": "实测记录", "simulation": "仿真记录", "literature": "文献",
    "note": "笔记", "artifact": "研究产物",
}


def _paragraph(builder: AcademicDocxBuilder, text: str) -> None:
    # Raw planning text must remain literal: [@KEY] is not a verified Zotero citation.
    builder.doc.add_paragraph(text)


def _value(value: Any) -> str:
    return str(value) if value is not None and value != "" else "未确定"


def _path(output_path: str, overwrite: bool) -> Path:
    if not isinstance(output_path, str) or not output_path.strip() or "\x00" in output_path:
        raise PlanningError("INVALID_OUTPUT_PATH", "须明确提供本地绝对 .docx 输出路径")
    raw = output_path.strip()
    if raw.startswith(("\\\\", "//")) or "://" in raw or raw.lower().startswith("file:"):
        raise PlanningError("NETWORK_PATH_REJECTED", "研究规划仅允许导出到本地路径，不允许网络或 UNC 路径")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.suffix.lower() != ".docx":
        raise PlanningError("INVALID_OUTPUT_PATH", "须明确提供本地绝对 .docx 输出路径")
    if candidate.is_symlink():
        raise PlanningError("INVALID_OUTPUT_PATH", "导出目标不能是符号链接")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PlanningError("INVALID_OUTPUT_PATH", "输出目录必须已存在且可访问") from None
    if str(parent).startswith(("\\\\", "//")) or not parent.is_dir():
        raise PlanningError("INVALID_OUTPUT_PATH", "输出目录必须是本地目录")
    target = parent / candidate.name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PlanningError("INVALID_OUTPUT_PATH", "导出目标必须是普通文件路径")
    if target.exists() and not overwrite:
        raise PlanningError("OUTPUT_EXISTS", "输出文件已存在；如确需替换，请显式设置 overwrite=true")
    return target


def build_plan_document(snapshot: Dict[str, Any]) -> AcademicDocxBuilder:
    plan = snapshot["plan"]
    profile = plan["profile"]
    builder = AcademicDocxBuilder(is_chinese=True)
    builder.add_title("研究规划：" + profile["title"])
    _paragraph(builder, "本文档是研究规划，不是已完成实验的论文。计划、实测、仿真、文献与笔记分别记录。")
    _paragraph(builder, "证据核验及假设判断均由提交方填写，PaperFlow 未独立核实科研结论。排版为通用规划惯例，并非期刊官方模板。")
    _paragraph(builder, f"项目：{snapshot['project_id']}；版本：{snapshot['revision']}；更新时间：{snapshot['updated_at']}")
    _paragraph(builder, "本版修订说明：" + snapshot["change_note"])

    builder.add_heading("1 研究档案与约束")
    _paragraph(builder, "研究目标：" + profile["goal"])
    _paragraph(builder, "研究定位：" + _value(profile["focus"]))
    _paragraph(builder, f"计划起点：{_value(profile['start_date'])}；目标时间：{_value(profile['target_date'])}。时间是计划，不是成果或录用保证。")
    for constraint in profile["constraints"]:
        _paragraph(builder, "约束：" + constraint)
    if profile["resources"]:
        builder.add_three_line_table(
            ["资源", "可用性记录", "说明", "记录来源"],
            [[item["name"], STATUS_LABELS[item["status"]], item["notes"], _value(item["source_ref"])]
             for item in profile["resources"]],
            "表 1  资源与待核实条件",
        )
    for decision in profile["decisions"]:
        _paragraph(builder, f"决定（{STATUS_LABELS[decision['status']]}）：{decision['text']}；来源：{_value(decision['source_ref'])}")
    for question in profile["open_questions"]:
        _paragraph(builder, "未决事项：" + question)

    builder.add_heading("2 研究问题、实验与证据对应")
    matrix = snapshot["overview"]["matrix"]
    if matrix:
        builder.add_three_line_table(
            ["研究问题 ID", "假设状态", "关联实验", "证据 ID 与类型"],
            [[row["question_id"], STATUS_LABELS[row["hypothesis_status"]],
              ", ".join(row["experiment_ids"]) or "待规划",
              "; ".join(f"{item['id']} ({KIND_LABELS[item['kind']]})" for item in row["evidence"]) or "待采集"]
             for row in matrix],
            "表 2  问题—实验—证据关联矩阵（不代表结论已成立）",
        )
    else:
        _paragraph(builder, "研究问题与实验尚未规划。")
    for question in plan["questions"]:
        builder.add_heading(question["id"] + " " + question["question"][:100], level=2)
        for label, field in (
            ("完整研究问题", "question"), ("待验证假设", "hypothesis"), ("成熟基线", "baseline"),
            ("最小改进", "minimum_change"), ("继续条件", "continue_condition"), ("停止条件", "stop_condition"),
        ):
            _paragraph(builder, label + "：" + _value(question[field]))
        _paragraph(builder, "提交方判断状态：" + STATUS_LABELS[question["status"]])
        _paragraph(builder, "提交方判断说明：" + _value(question["assessment_note"]))
        _paragraph(builder, "直接证据引用：" + (", ".join(question["evidence_ids"]) or "尚未提交"))

    builder.add_heading("3 实验协议与结果记录")
    for experiment in plan["experiments"]:
        builder.add_heading(experiment["id"] + " " + experiment["title"], level=2)
        _paragraph(builder, "关联问题：" + experiment["question_id"])
        _paragraph(builder, "状态：" + STATUS_LABELS[experiment["status"]])
        _paragraph(builder, "对照组：" + ("；".join(experiment["comparisons"]) or "待规划"))
        _paragraph(builder, "指标及测量口径：" + ("；".join(experiment["metrics"]) or "待规划"))
        _paragraph(builder, "测量协议：" + _value(experiment["protocol"]))
        _paragraph(builder, "验收条件：" + _value(experiment["acceptance_condition"]))
        _paragraph(builder, "提交方结果记录（不是服务生成的结论）：" + _value(experiment["result_summary"]))
        _paragraph(builder, "证据引用：" + (", ".join(experiment["evidence_ids"]) or "尚未提交"))
    if not plan["experiments"]:
        _paragraph(builder, "尚无实验协议或结果。")

    builder.add_heading("4 分类型证据与核验记录")
    for kind, label in KIND_LABELS.items():
        items = [item for item in plan["evidence"] if item["kind"] == kind]
        if not items:
            continue
        builder.add_heading(label, level=2)
        for item in items:
            _paragraph(builder, f"{item['id']}；来源：{item['source_ref']}；状态：{STATUS_LABELS[item['verification']]}")
            _paragraph(builder, "记录摘要：" + item["summary"])
            _paragraph(builder, "提交方核验说明：" + _value(item["verification_note"]))
            _paragraph(builder, "提交方核验人：" + _value(item["verified_by"]))
    if not plan["evidence"]:
        _paragraph(builder, "尚未提交证据。仿真、文献或笔记不能自动替代实测记录。")

    builder.add_heading("5 阶段任务与依赖")
    if plan["tasks"]:
        builder.add_three_line_table(
            ["任务 ID / 阶段", "状态 / 截止时间", "工作项与完成条件", "依赖"],
            [[f"{task['id']} / {_value(task['stage'])}",
              f"{STATUS_LABELS[task['status']]} / {_value(task['due_date'])}",
              f"{task['text']}\n完成条件：{task['completion_condition']}",
              ", ".join(task["depends_on"]) or "无"] for task in plan["tasks"]],
            "表 3  有可观察完成条件的阶段任务",
        )
        for task in plan["tasks"]:
            _paragraph(builder, f"{task['id']}：关联实验：{_value(task['experiment_id'])}；完成说明：{_value(task['completion_note'])}；阻塞原因：{_value(task['blocked_reason'])}")
            _paragraph(builder, "产物引用：" + ("；".join(task["artifact_refs"]) or "尚未提交"))
    else:
        _paragraph(builder, "尚未安排阶段任务。")

    builder.add_heading("6 剩余缺口与可推进事项")
    gaps = snapshot["overview"]["structural_gaps"]
    _paragraph(builder, "以下仅为结构性缺口，不是科研质量评分或录用概率。任务完成不会自动证明假设成立。")
    for gap in gaps:
        _paragraph(builder, f"{gap['entity_type']} {gap['id']}：" + ", ".join(gap["missing"]))
    if not gaps:
        _paragraph(builder, "当前结构性字段无缺项；仍需独立核对证据内容与研究结论。")
    for task in snapshot["overview"]["ready_tasks"]:
        _paragraph(builder, f"可推进：{task['id']} {task['text']}；完成条件：{task['completion_condition']}")
    return builder


def export_plan_docx(snapshot: Dict[str, Any], output_path: str, overwrite: bool = False) -> Path:
    if type(overwrite) is not bool:
        raise PlanningError("INVALID_INPUT", "overwrite 必须是布尔值")
    target = _path(output_path, overwrite)
    try:
        with tempfile.TemporaryDirectory(prefix=".paperflow-plan-", dir=str(target.parent)) as temporary:
            staged = Path(temporary) / "plan.docx"
            build_plan_document(snapshot).save(str(staged))
            if overwrite:
                os.replace(staged, target)
            else:
                # Same-volume hard link publishes the completed file exclusively and atomically.
                # A raced-in destination is never overwritten and a failed export leaves no partial file.
                os.link(staged, target)
    except FileExistsError:
        raise PlanningError("OUTPUT_EXISTS", "输出文件已存在；未覆盖原文件") from None
    except OSError:
        raise PlanningError("EXPORT_FAILED", "规划文档导出失败，请检查本地目录及文件权限") from None
    return target
