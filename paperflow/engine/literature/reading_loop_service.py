"""Adaptive literature reading loop service coordinating review, search, reading, and revision."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import unicodedata
from functools import wraps
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from paperflow.engine.journals.models import JournalError, utc_now
from .models import ReadingCard
from .reading_loop_models import (
    ACTION_RE, BUDGET_DEFAULTS, BUDGET_HARD_LIMITS, LOOP_STATUSES, STOP_REASONS, USAGE_KEYS,
    FeedbackAssessment, FeedbackInterpretation, FeedbackRevision,
    InheritedBaseline, LoopBudget, LoopReserved, LoopUsage, NextAction,
    ReadingLoopState, ReviewDimension, ReviewGap, WritingReview,
    calculate_context_fingerprint, generate_action_id, generate_review_id, generate_ticket_id
)
from .reading_loop_store import ReadingLoopStore
from .writing_models import budget_json, integer, parse_model, project_id as validate_project_id
from .writing_service import PaperWritingService
from .writing_store import _CURRENT_LOOP_OWNER


def _coverage() -> Dict[str, Any]:
    return {
        "backend_calls_llm": False,
        "evidence_check_only": True,
        "semantic_correctness_verified": False,
        "understanding_complete": False,
        "download_scope": "有界获取操作(内部最多5公开位置)",
        "own_materials_origin": "caller-supplied",
        "own_materials_independently_verified": False,
    }


def _response(
    data: Any,
    status: str = "success",
    error_code: Optional[str] = None,
    message: str = "",
    sources: Optional[List[Any]] = None,
    warnings: Optional[List[str]] = None,
    suggested_options: Optional[List[Any]] = None
) -> Dict[str, Any]:
    return {
        "status": status,
        "error_code": error_code,
        "message": message,
        "data": data,
        "sources": sources or [],
        "coverage": _coverage(),
        "warnings": warnings or [],
        "suggested_options": suggested_options or [],
    }


def _service_guard(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except JournalError as exc:
            return _response(None, status="error", error_code=exc.code, message=str(exc))
        except Exception:
            return _response(None, status="error", error_code="LOOP_SERVICE_ERROR", message="自适应阅读循环发生内部错误，请稍后重试")
    return wrapper


class ReadingLoopService:
    def __init__(self, data_dir: Optional[str] = None, writing: Optional[PaperWritingService] = None):
        self.writing = writing if writing is not None else PaperWritingService(data_dir)
        self.store = ReadingLoopStore(data_dir, paper_store=self.writing.store.library)
        self.literature = self.writing.literature

    def _get_project_data(self, project_id: str) -> Dict[str, Any]:
        res = self.writing.get(project_id)
        if res.get("status") == "error":
            raise JournalError(res.get("error_code") or "PROJECT_NOT_FOUND", res.get("message") or "项目不存在")
        return res["data"]

    def _compute_fingerprint(self, project_id: str, request_override: Optional[str] = None) -> str:
        snapshot = self.writing.store.get(project_id)
        context = self.writing._context(snapshot)
        all_evidence = list(context.get("evidence", {}).values())
        paper_states = copy.deepcopy(context.get("states", {}))
        for paper_id, paper in context.get("papers", {}).items():
            paper_states.setdefault(paper_id, {})["file_sha256"] = paper.get("acquisition", {}).get("file_sha256", "")
        if request_override is None:
            loop = self.store.get(project_id)
            active_request = loop.get("request", "") if loop else ""
        else:
            active_request = request_override
        fingerprint_data = {**snapshot, "reading_request": active_request}
        return calculate_context_fingerprint(fingerprint_data, all_evidence, paper_states)
    def _schedule_review(self, project_id: str, state: Dict[str, Any], reason: str) -> None:
        action_id = generate_action_id()
        state.update(status="awaiting_review", stop_reason=None, stop_detail="")
        state["next_action"] = {
            "action_id": action_id, "kind": "review", "gap_ids": [],
            "reason": reason, "payload": {}, "material": {}, "requires_agent": True,
        }
        state["context_fingerprint"] = self._compute_fingerprint(project_id, request_override=state.get("request", ""))
        self.store.save_action(project_id, action_id, state["round_index"], "review", "pending", {})

    @_service_guard
    def get(self, project_id: str) -> Dict[str, Any]:
        """Readonly query with zero side effects. Returns idle state if no loop exists yet."""
        project = self._get_project_data(project_id)
        loop = self.store.get(project_id)

        if loop is None:
            idle_state = {
                "status": "idle",
                "stop_reason": None,
                "stop_detail": "",
                "budget": LoopBudget().model_dump(mode="json"),
                "usage": LoopUsage().model_dump(mode="json"),
                "reserved": LoopReserved().model_dump(mode="json"),
                "inherited_baseline": InheritedBaseline().model_dump(mode="json"),
                "round_index": 0,
                "no_progress_rounds": 0,
                "next_action": None,
                "rounds": [],
                "reviews": [],
                "actions": [],
                "request": "",
            }
            return _response({
                "project_id": project["project_id"],
                "project_revision": project["revision"],
                "loop_revision": 0,
                "loop": idle_state,
                "project": project,
            })

        return _response({
            "project_id": project["project_id"],
            "project_revision": project["revision"],
            "loop_revision": loop["loop_revision"],
            "loop": loop,
            "project": project,
        })

    @_service_guard
    def control(
        self,
        project_id: str,
        action: str,
        expected_loop_revision: int,
        expected_project_revision: int,
        budget: Optional[Dict[str, Any]] = None,
        request: str = ""
    ) -> Dict[str, Any]:
        """Controls loop state: start, pause, resume, stop, update_budget."""
        pid = validate_project_id(project_id)
        integer(expected_loop_revision, "expected_loop_revision", 0)
        integer(expected_project_revision, "expected_project_revision", 1)

        if not isinstance(request, str) or len(request) > 4000:
            raise JournalError("INVALID_INPUT", "用户补强要求长度不得超过 4000 字符")
        request = request.strip()

        project = self._get_project_data(pid)
        current_loop = self.store.get(pid)

        if action == "start":
            if expected_loop_revision != 0:
                raise JournalError("INVALID_REVISION", "首次启动循环 expected_loop_revision 必须为 0")
            if current_loop is not None:
                raise JournalError("LOOP_ALREADY_EXISTS", "已有循环不可重新 start 清零")
            if project["revision"] != expected_project_revision:
                raise JournalError("REVISION_CONFLICT", "项目版本已改变，请核对后重试")

            validated_budget = parse_model(LoopBudget, budget or {}).model_dump(mode="json")

            # Calculate inherited baseline
            selected_ids = project.get("selected_paper_ids", [])
            inherited_read = []
            inherited_partial = []
            inherited_cards = []
            checked_context = self.writing._context(self.writing.store.get(pid))
            valid_readings = checked_context["readings"]
            for sel_id in selected_ids:
                readings = [r for r in self.writing.store.readings(sel_id)
                            if r["reading_id"] in valid_readings]
                if readings:
                    inherited_read.append(sel_id)
                    inherited_cards.extend(r["reading_id"] for r in readings)
                    acquisition = self.literature.store.get(sel_id).get("acquisition", {})
                    sha = acquisition.get("file_sha256")
                    total_pages = acquisition.get("total_pages")
                    complete = bool(
                        sha and type(total_pages) is int and total_pages > 0 and
                        self.literature.store.coverage(sel_id, sha, total_pages).get("text_complete")
                    )
                    if not complete:
                        inherited_partial.append(sel_id)

            draft = project.get("draft") or {}
            cited_ids = []
            for sec in draft.get("sections", []):
                for p in sec.get("paragraphs", []):
                    cited_ids.extend(p.get("citation_ids", []))
            cited_ids = list(set(cited_ids))

            baseline = {
                "read_papers": sorted(list(set(inherited_read))),
                "partially_read": sorted(set(inherited_partial)),
                "cards": sorted(list(set(inherited_cards))),
                "cited": sorted(cited_ids),
            }

            first_action_id = generate_action_id()
            first_action = {
                "action_id": first_action_id,
                "kind": "review",
                "gap_ids": [],
                "reason": "初始评审：请对当前草稿、研究问题及文献支撑进行质量评审并识别缺口",
                "payload": {},
                "material": {},
                "requires_agent": True,
            }

            initial_state = {
                "status": "awaiting_review",
                "stop_reason": None,
                "stop_detail": "",
                "budget": validated_budget,
                "usage": LoopUsage().model_dump(mode="json"),
                "reserved": LoopReserved().model_dump(mode="json"),
                "inherited_baseline": baseline,
                "round_index": 1,
                "no_progress_rounds": 0,
                "next_action": first_action,
                "rounds": [],
                "reviews": [],
                "actions": [],
                "request": request,
            }

            self.store.save_round(
                pid, 1, goal="初始文献与草稿质量评审", status="active",
                round_data={"queries_history": [], "new_quotes": [], "gaps": []}
            )
            self.store.save_action(
                pid, first_action_id, 1, kind="review", status="pending",
                payload={}, reserved_cost={}
            )

            created_loop = self.store.create(pid, initial_state, project["revision"])
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": created_loop["loop_revision"],
                "loop": created_loop,
                "project": project,
            })

        # Actions other than start require existing loop
        if current_loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")

        if expected_loop_revision != current_loop["loop_revision"]:
            raise JournalError("REVISION_CONFLICT", "循环已被其他操作推进，请刷新重试")

        new_loop_state = copy.deepcopy(current_loop)

        if action == "pause":
            if new_loop_state["status"] == "stopped":
                raise JournalError("LOOP_ALREADY_STOPPED", "循环已停止，无法暂停")
            new_loop_state["status"] = "paused"
            new_loop_state["stop_reason"] = "paused_by_user"
            new_loop_state["stop_detail"] = request or "由用户暂停"
            if request:
                new_loop_state["request"] = request
            saved_loop = self.store.save(
                pid, new_loop_state, expected_loop_revision, "用户暂停循环",
                expected_project_revision=new_loop_state.get("project_revision", project["revision"])
            )
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": saved_loop["loop_revision"],
                "loop": saved_loop,
                "project": project,
            })

        if action == "stop":
            new_loop_state["status"] = "stopped"
            new_loop_state["stop_reason"] = "stopped_by_user"
            new_loop_state["stop_detail"] = request or "由用户主动停止"
            if request:
                new_loop_state["request"] = request
            saved_loop = self.store.save(
                pid, new_loop_state, expected_loop_revision, "用户停止循环",
                expected_project_revision=new_loop_state.get("project_revision", project["revision"])
            )
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": saved_loop["loop_revision"],
                "loop": saved_loop,
                "project": project,
            })

        if action == "resume":
            was_stopped = new_loop_state["status"] == "stopped"
            if new_loop_state["status"] not in ("paused", "stopped"):
                raise JournalError("INVALID_STATE", "只有暂停或停止的循环才可 resume")
            request_changed = bool(request and request != new_loop_state.get("request", ""))
            new_loop_state["stop_reason"] = None
            new_loop_state["stop_detail"] = ""
            if request:
                new_loop_state["request"] = request
            current_fingerprint = self._compute_fingerprint(pid, request_override=new_loop_state.get("request", ""))
            if was_stopped or request_changed or new_loop_state.get("context_fingerprint") != current_fingerprint:
                self._schedule_review(pid, new_loop_state, "用户继续补强或上下文变化：先重新评审，不复用旧的质量结论")

            if new_loop_state.get("next_action"):
                kind = new_loop_state["next_action"]["kind"]
                if kind == "review":
                    new_loop_state["status"] = "awaiting_review"
                elif kind == "assessment":
                    new_loop_state["status"] = "awaiting_assessment"
                elif kind == "interpretation":
                    new_loop_state["status"] = "awaiting_interpretation"
                elif kind == "revision":
                    new_loop_state["status"] = "awaiting_revision"
                else:
                    new_loop_state["status"] = "ready"
            else:
                new_loop_state["status"] = "awaiting_review"
                act_id = generate_action_id()
                new_loop_state["next_action"] = {
                    "action_id": act_id,
                    "kind": "review",
                    "gap_ids": [],
                    "reason": "恢复循环并请求评审",
                    "payload": {},
                    "material": {},
                    "requires_agent": True,
                }
                self.store.save_action(pid, act_id, new_loop_state["round_index"], "review", "pending", {})

            saved_loop = self.store.save(
                pid, new_loop_state, expected_loop_revision, "恢复自适应循环",
                expected_project_revision=project["revision"]
            )
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": saved_loop["loop_revision"],
                "loop": saved_loop,
                "project": project,
            })

        if action == "update_budget":
            if budget is None:
                raise JournalError("INVALID_INPUT", "更新预算必须提供 budget 参数")
            if not isinstance(budget, dict):
                raise JournalError("INVALID_INPUT", "budget 必须是预算字段对象")
            if self.store.get_lease(pid):
                raise JournalError("LOOP_BUSY", "正在执行小步；请暂停并等待在途操作结算后调整预算")
            new_budget = parse_model(LoopBudget, {**new_loop_state["budget"], **budget}).model_dump(mode="json")
            usage = new_loop_state["usage"]
            reserved = new_loop_state["reserved"]

            for k in USAGE_KEYS:
                max_key = f"max_{k}"
                if max_key in new_budget:
                    needed = usage.get(k, 0) + reserved.get(k, 0)
                    if new_budget[max_key] < needed:
                        raise JournalError("BUDGET_TOO_LOW", f"{max_key} 不能低于已使用量与已预留量之和 ({needed})")

            new_loop_state["budget"] = new_budget
            request_changed = bool(request and request != new_loop_state.get("request", ""))
            if request:
                new_loop_state["request"] = request
            if request_changed:
                previous_status = new_loop_state["status"]
                previous_reason = new_loop_state.get("stop_reason")
                previous_detail = new_loop_state.get("stop_detail", "")
                self._schedule_review(pid, new_loop_state, "用户提出新的补强要求：重新评审，不沿用旧的足够结论")
                if previous_status == "paused" or previous_reason == "stopped_by_user":
                    new_loop_state.update(status=previous_status, stop_reason=previous_reason, stop_detail=previous_detail)
            saved_loop = self.store.save(
                pid, new_loop_state, expected_loop_revision, "调整循环预算",
                expected_project_revision=project["revision"]
            )
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": saved_loop["loop_revision"],
                "loop": saved_loop,
                "project": project,
            })

        raise JournalError("INVALID_ACTION", f"不支持的控制动作: {action}")

    @_service_guard
    def prepare_review(
        self,
        project_id: str,
        evidence_offset: int = 0,
        evidence_limit: int = 30
    ) -> Dict[str, Any]:
        """Prepares review materials, bounds evidence matrix, and computes deterministic context fingerprint."""
        pid = validate_project_id(project_id)
        integer(evidence_offset, "evidence_offset", 0)
        integer(evidence_limit, "evidence_limit", 1, 100)

        prep_res = self.writing.prepare_writing(pid, evidence_offset, evidence_limit)
        if prep_res.get("status") == "error":
            raise JournalError(prep_res.get("error_code") or "PROJECT_ERROR", prep_res.get("message") or "")
        prep_data = prep_res["data"]
        project = prep_data

        fingerprint = self._compute_fingerprint(pid)

        loop = self.store.get(pid)
        loop_rev = loop["loop_revision"] if loop else 0

        review_schema = WritingReview.model_json_schema()
        feedback_schema = {
            "assessment": FeedbackAssessment.model_json_schema(),
            "interpretation": FeedbackInterpretation.model_json_schema(),
            "revision": FeedbackRevision.model_json_schema(),
        }

        return _response({
            "project_id": pid,
            "project_revision": project["revision"],
            "loop_revision": loop_rev,
            "loop": loop,
            "project": project,
            "context_fingerprint": fingerprint,
            "review_schema": review_schema,
            "feedback_schema": feedback_schema,
            "evidence_matrix": prep_data["evidence_matrix"],
            "evidence_total": prep_data["evidence_total"],
            "evidence_offset": evidence_offset,
            "evidence_limit": evidence_limit,
            "next_evidence_offset": prep_data["next_evidence_offset"],
        })

    @_service_guard
    def submit_review(
        self,
        project_id: str,
        review: Dict[str, Any],
        context_fingerprint: str,
        expected_project_revision: int,
        expected_loop_revision: int,
        origin: str = "calling_agent"
    ) -> Dict[str, Any]:
        """Submits agent/model quality review and plan for next literature steps."""
        pid = validate_project_id(project_id)
        integer(expected_project_revision, "expected_project_revision", 1)
        integer(expected_loop_revision, "expected_loop_revision", 1)

        project = self._get_project_data(pid)
        if project["revision"] != expected_project_revision:
            raise JournalError("REVISION_CONFLICT", "项目版本已变更，请核对后重试")

        loop = self.store.get(pid)
        if loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")
        if loop["loop_revision"] != expected_loop_revision:
            raise JournalError("REVISION_CONFLICT", "循环已被其他操作推进，请刷新重试")

        if loop["status"] not in ("awaiting_review", "paused"):
            raise JournalError("INVALID_STATE", f"当前循环状态为 {loop['status']}，无法提交评审")

        # Context fingerprint verification across all selected evidence and candidate states
        current_fp = self._compute_fingerprint(pid)

        if current_fp != context_fingerprint:
            # Context changed outside loop
            loop_state = copy.deepcopy(loop)
            loop_state["status"] = "paused"
            loop_state["stop_reason"] = "context_changed"
            loop_state["stop_detail"] = "项目上下文在评审期间发生变更，请重新核对后评审"
            self.store.save(pid, loop_state, expected_loop_revision, "上下文变更导致暂停", project["revision"])
            raise JournalError("CONTEXT_CHANGED", "项目上下文已发生变更，请重新核对后评审")

        # Validate review object
        validated_review = parse_model(WritingReview, review)
        review_dict = validated_review.model_dump(mode="json")

        # Validate IDs in review against project
        sub_q_ids = {q["id"] for q in project.get("profile", {}).get("sub_questions", [])}
        sec_ids = {s["id"] for s in (project.get("draft") or {}).get("sections", [])}
        cand_ids = {c["paper_id"] for c in project.get("candidates", [])}
        known_cites = set()
        snapshot = self.writing.store.get(pid)
        ctx = self.writing._context(snapshot)
        for ev in ctx.get("evidence", {}).values():
            known_cites.add(ev["citation_id"])

        for dim in validated_review.dimensions:
            for qid in dim.question_ids:
                if qid not in sub_q_ids:
                    raise JournalError("INVALID_ID", f"研究问题 {qid} 不存在于当前项目中")
            for sid in dim.section_ids:
                if sid not in sec_ids:
                    raise JournalError("INVALID_ID", f"章节 {sid} 不存在于当前草稿中")
            for cid in dim.citation_ids:
                if cid not in known_cites:
                    raise JournalError("INVALID_ID", f"引用 {cid} 不是有效正文证据")

        for gap in validated_review.gaps:
            for qid in gap.question_ids:
                if qid not in sub_q_ids:
                    raise JournalError("INVALID_ID", f"缺口关联的研究问题 {qid} 不存在")
            for sid in gap.section_ids:
                if sid not in sec_ids:
                    raise JournalError("INVALID_ID", f"缺口关联的章节 {sid} 不存在")
            if gap.read_paper_ids:
                for rpid in gap.read_paper_ids:
                    if rpid not in cand_ids:
                        raise JournalError("INVALID_ID", f"指定阅读文献 {rpid} 不在当前项目候选库中")

        # Check evidence sufficiency condition
        if validated_review.decision == "stop":
            for d in validated_review.dimensions:
                if d.status in ("gap", "uncertain"):
                    raise JournalError("UNRESOLVED_DIMENSION", f"评审维度 {d.dimension} 尚存在未解决缺口或不确定性，不能停止")

            reason = validated_review.reason
            draft = project.get("draft")
            if not draft or not draft.get("sections"):
                raise JournalError("DRAFT_NOT_READY", "草稿尚未准备完毕，不能判定文献已充分")

            draft_cites = set()
            for s in draft.get("sections", []):
                for p in s.get("paragraphs", []):
                    draft_cites.update(p.get("citation_ids", []))

            # Examined must cover draft cites and sections
            if not draft_cites.issubset(set(validated_review.examined_citation_ids)):
                missing = draft_cites - set(validated_review.examined_citation_ids)
                raise JournalError("INCOMPLETE_EXAMINATION", f"评审未完全覆盖草稿中的有效引用: {list(missing)[:3]}")

            if not sec_ids.issubset(set(validated_review.examined_section_ids)):
                missing = sec_ids - set(validated_review.examined_section_ids)
                raise JournalError("INCOMPLETE_EXAMINATION", f"评审未覆盖所有草稿章节: {list(missing)[:3]}")

            unresolved_high = [
                g.id for g in validated_review.gaps
                if g.priority == "high" and g.category in ("literature", "draft_revision")
            ]
            if unresolved_high:
                raise JournalError("UNRESOLVED_CRITICAL_GAP", f"尚有关键缺口未解决 ({unresolved_high})，不能宣称文献充分")

        # Save review
        review_id = self.store.save_review(
            pid, review_dict, current_fp, origin,
            expected_loop_revision, loop["round_index"]
        )

        # Stagnation detection (no_progress_rounds)
        no_progress = loop.get("no_progress_rounds", 0)
        past_reviews = loop.get("reviews", [])
        if past_reviews:
            prev_rev = past_reviews[-1]
            prev_gaps = {g["id"]: (g.get("priority"), g.get("reason", "")) for g in prev_rev.get("gaps", [])}
            curr_gaps = {g["id"]: (g.get("priority"), g.get("reason", "")) for g in review_dict.get("gaps", [])}

            def _quotes_for(cids):
                qset = set()
                for c in cids:
                    row = ctx.get("evidence", {}).get(c)
                    if row:
                        for n in row.get("claim", {}).get("evidence", []):
                            qt = unicodedata.normalize("NFKC", str(n.get("quote", "")).strip())
                            if qt:
                                qset.add(qt)
                return qset

            prev_quotes = _quotes_for(prev_rev.get("examined_citation_ids", []))
            curr_quotes = _quotes_for(review_dict.get("examined_citation_ids", []))

            if prev_gaps == curr_gaps and prev_quotes == curr_quotes and validated_review.decision != "stop":
                no_progress += 1
            else:
                no_progress = 0

        new_loop = copy.deepcopy(loop)
        new_loop["no_progress_rounds"] = no_progress

        if no_progress >= 2:
            new_loop["status"] = "stopped"
            new_loop["stop_reason"] = "no_new_information"
            new_loop["stop_detail"] = "连续两轮未发现实质新信息或缺口改善，策略停滞"
            new_loop["next_action"] = None
            saved_loop = self.store.save(pid, new_loop, expected_loop_revision, "评审停滞终止循环", project["revision"])
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": saved_loop["loop_revision"],
                "loop": saved_loop,
                "project": project,
            })

        # Progress to next step based on decision
        if validated_review.decision == "stop":
            new_loop["status"] = "stopped"
            own_data_gaps = [g for g in validated_review.gaps if g.category == "own_data"]
            manual_gaps = [g for g in validated_review.gaps if g.category == "manual_or_ocr"]
            if own_data_gaps:
                new_loop["stop_reason"] = "needs_user_material"
            elif manual_gaps:
                new_loop["stop_reason"] = "needs_manual_review"
            else:
                new_loop["stop_reason"] = "evidence_sufficient"
            new_loop["stop_detail"] = validated_review.reason
            new_loop["next_action"] = None
        elif validated_review.decision == "revise":
            new_loop["status"] = "awaiting_revision"
            act_id = generate_action_id()
            new_loop["next_action"] = {
                "action_id": act_id,
                "kind": "revision",
                "gap_ids": [g.id for g in validated_review.gaps if g.category == "draft_revision"],
                "reason": validated_review.reason,
                "payload": {},
                "material": {},
                "requires_agent": True,
            }
            self.store.save_action(pid, act_id, new_loop["round_index"], "revision", "pending", {})
        elif validated_review.decision == "continue":
            lit_gaps = [g for g in validated_review.gaps if g.category == "literature"]
            # Look for explicit read_paper_ids or queries
            target_paper_ids = []
            queries_to_run = []
            for lg in lit_gaps:
                if lg.read_paper_ids:
                    target_paper_ids.extend(lg.read_paper_ids)
                if lg.queries:
                    queries_to_run.extend([q.model_dump(mode="json") for q in lg.queries])

            if queries_to_run:
                act_id = generate_action_id()
                new_loop["status"] = "ready"
                new_loop["next_action"] = {
                    "action_id": act_id,
                    "kind": "search",
                    "gap_ids": [lg.id for lg in lit_gaps if lg.queries],
                    "reason": "执行文献缺口补充检索",
                    "payload": {"queries": queries_to_run[:6]},
                    "material": {},
                    "requires_agent": False,
                }
                self.store.save_action(pid, act_id, new_loop["round_index"], "search", "pending", new_loop["next_action"]["payload"])
            elif target_paper_ids:
                # Pick the first unread target paper
                target_pid = target_paper_ids[0]
                cand = next((c for c in project.get("candidates", []) if c["paper_id"] == target_pid), None)
                assessed_pids = {a["paper_id"] for a in project.get("assessments", [])}
                selected_pids = set(project.get("selected_paper_ids", []))

                # If target paper has not been assessed yet, it must first be assessed by Agent
                if target_pid not in assessed_pids and target_pid not in selected_pids:
                    act_id = generate_action_id()
                    new_loop["status"] = "awaiting_assessment"
                    new_loop["next_action"] = {
                        "action_id": act_id,
                        "kind": "assessment",
                        "gap_ids": [lg.id for lg in lit_gaps if target_pid in (lg.read_paper_ids or [])],
                        "reason": f"缺口指定文献 {target_pid} 尚未评估，请先评定相关性并保留已有选用",
                        "payload": {"candidate_paper_ids": [target_pid]},
                        "material": {"candidates": [cand] if cand else []},
                        "requires_agent": True,
                    }
                    self.store.save_action(pid, act_id, new_loop["round_index"], "assessment", "pending", new_loop["next_action"]["payload"])
                else:
                    # Check if downloaded
                    paper_rec = cand.get("paper", {}) if cand else {}
                    sha = paper_rec.get("file_sha256")
                    is_downloaded = False
                    if sha:
                        try:
                            self.literature.library.read_pdf(sha)
                            is_downloaded = True
                        except Exception:
                            is_downloaded = False

                    act_id = generate_action_id()
                    new_loop["status"] = "ready"
                    if not is_downloaded:
                        new_loop["next_action"] = {
                            "action_id": act_id,
                            "kind": "download",
                            "gap_ids": [lg.id for lg in lit_gaps if target_pid in (lg.read_paper_ids or [])],
                            "reason": f"下载文献 {target_pid} 全文",
                            "payload": {"paper_id": target_pid},
                            "material": {"paper": paper_rec},
                            "requires_agent": False,
                        }
                        self.store.save_action(pid, act_id, new_loop["round_index"], "download", "pending", new_loop["next_action"]["payload"])
                    else:
                        new_loop["next_action"] = {
                            "action_id": next_id,
                            "kind": "read",
                            "gap_ids": [lg.id for lg in lit_gaps if target_pid in (lg.read_paper_ids or [])],
                            "reason": f"读取文献 {target_pid} 正文",
                            "payload": {"paper_id": target_pid, "page_number": 1, "offset": 0},
                            "material": {"paper": paper_rec},
                            "requires_agent": False,
                        }
                        self.store.save_action(pid, next_id, new_loop["round_index"], "read", "pending", new_loop["next_action"]["payload"])

        saved_loop = self.store.save(pid, new_loop, expected_loop_revision, "提交文献评审", project["revision"])
        return _response({
            "project_id": pid,
            "project_revision": project["revision"],
            "loop_revision": saved_loop["loop_revision"],
            "loop": saved_loop,
            "project": project,
        })

    @_service_guard
    def step(
        self,
        project_id: str,
        action_id: str,
        expected_project_revision: int,
        expected_loop_revision: int
    ) -> Dict[str, Any]:
        """Executes a single deterministic approved search/download/read step under lease."""
        pid = validate_project_id(project_id)
        integer(expected_project_revision, "expected_project_revision", 1)
        integer(expected_loop_revision, "expected_loop_revision", 1)

        project = self._get_project_data(pid)
        loop = self.store.get(pid)
        if loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")

        cur_act = loop.get("next_action")
        if not cur_act or cur_act.get("action_id") != action_id:
            # Check if this action was already completed (idempotent replay)
            past_act = self.store.get_action(pid, action_id)
            if past_act and past_act["status"] == "completed":
                return _response({
                    "project_id": pid,
                    "project_revision": project["revision"],
                    "loop_revision": loop["loop_revision"],
                    "loop": loop,
                    "project": project,
                    "action_replay": True,
                    "action_result": past_act.get("result"),
                })
            raise JournalError("ACTION_MISMATCH", f"指定动作 {action_id} 与当前待执行动作不符")

        if cur_act.get("requires_agent"):
            return _response({
                "project_id": pid,
                "project_revision": project["revision"],
                "loop_revision": loop["loop_revision"],
                "loop": loop,
                "project": project,
                "waiting_for_agent": True,
            })

        kind = cur_act["kind"]
        budget = loop["budget"]
        usage = loop["usage"]
        payload = cur_act.get("payload", {})

        # A round begins on the first actual supplementary action
        if usage["rounds"] == 0:
            usage["rounds"] = 1

        # Budget checks before reserving
        if kind == "search":
            queries = payload.get("queries", [])
            needed_calls = sum(len(set(q.get("sources") or ["crossref", "arxiv"])) for q in queries)
            if usage["search_calls"] + needed_calls > budget["max_search_calls"]:
                loop["status"] = "stopped"
                loop["stop_reason"] = "budget_exhausted"
                loop["stop_detail"] = "检索次数超出预算上限"
                saved = self.store.save(pid, loop, expected_loop_revision, "预算耗尽停止", project["revision"])
                return _response({"project_id": pid, "project_revision": project["revision"], "loop_revision": saved["loop_revision"], "loop": saved, "project": project})
        elif kind == "download":
            if usage["download_attempts"] + 1 > budget["max_download_attempts"]:
                loop["status"] = "stopped"
                loop["stop_reason"] = "budget_exhausted"
                loop["stop_detail"] = "下载尝试次数超出预算上限"
                saved = self.store.save(pid, loop, expected_loop_revision, "预算耗尽停止", project["revision"])
                return _response({"project_id": pid, "project_revision": project["revision"], "loop_revision": saved["loop_revision"], "loop": saved, "project": project})
        elif kind == "read":
            target_pid = payload.get("paper_id")
            baseline_reads = set(loop.get("inherited_baseline", {}).get("read_papers", []))
            all_past_reads = {
                act.get("payload", {}).get("paper_id")
                for act in loop.get("actions", [])
                if act.get("kind") == "read" and act.get("status") == "completed"
            }
            if target_pid not in baseline_reads and target_pid not in all_past_reads:
                if usage["read_papers"] + 1 > budget["max_read_papers"]:
                    loop["status"] = "stopped"
                    loop["stop_reason"] = "budget_exhausted"
                    loop["stop_detail"] = f"新增阅读论文篇数已达预算上限 ({budget['max_read_papers']})"
                    saved = self.store.save(pid, loop, expected_loop_revision, "阅读篇数达预算上限停止", project["revision"])
                    return _response({"project_id": pid, "project_revision": project["revision"], "loop_revision": saved["loop_revision"], "loop": saved, "project": project})

            remaining_chars = budget["max_text_chars"] - usage["text_chars"]
            remaining_pages = budget["max_pages"] - usage["pages"]
            if remaining_chars < 1000 or remaining_pages < 1 or usage["read_steps"] >= budget["max_read_steps"]:
                loop["status"] = "stopped"
                loop["stop_reason"] = "budget_exhausted"
                loop["stop_detail"] = "可用字符数不足 1000 字符或阅读页数/步数超限"
                saved = self.store.save(pid, loop, expected_loop_revision, "预算耗尽停止", project["revision"])
                return _response({"project_id": pid, "project_revision": project["revision"], "loop_revision": saved["loop_revision"], "loop": saved, "project": project})

        # Acquire short lease
        token, expires_at = self.store.acquire_lease(pid, action_id, expected_loop_revision, duration_seconds=30.0)

        step_result = None
        new_project_revision = project["revision"]
        settled_costs = {}

        try:
            # Set owner token for writing_store short guard bypass
            owner_token = _CURRENT_LOOP_OWNER.set(pid)
            try:
                if kind == "search":
                    queries = payload.get("queries", [])
                    search_res = self.writing.search(pid, project["revision"], queries=queries)
                    search_data = search_res.get("data", {})
                    new_project_revision = search_data.get("revision", project["revision"])
                    new_cands = search_data.get("candidates", [])

                    calls_spent = len(queries) * 2
                    usage["search_calls"] += calls_spent
                    settled_costs["search_calls"] = calls_spent
                    step_result = {"candidates_count": len(new_cands)}

                    # Check if there are unassessed candidates
                    assessed_pids = {a["paper_id"] for a in search_data.get("assessments", [])}
                    unassessed = [c for c in new_cands if c["paper_id"] not in assessed_pids]

                    if unassessed:
                        next_id = generate_action_id()
                        loop["status"] = "awaiting_assessment"
                        loop["next_action"] = {
                            "action_id": next_id,
                            "kind": "assessment",
                            "gap_ids": cur_act.get("gap_ids", []),
                            "reason": f"检索产生 {len(unassessed)} 篇新候选文献，请进行相关性判断",
                            "payload": {"candidate_paper_ids": [c["paper_id"] for c in unassessed]},
                            "material": {"candidates": unassessed},
                            "requires_agent": True,
                        }
                        self.store.save_action(pid, next_id, loop["round_index"], "assessment", "pending", loop["next_action"]["payload"])
                    else:
                        loop["status"] = "awaiting_review"
                        next_id = generate_action_id()
                        loop["next_action"] = {
                            "action_id": next_id,
                            "kind": "review",
                            "gap_ids": cur_act.get("gap_ids", []),
                            "reason": "检索完毕，请重新评审文献充足性",
                            "payload": {},
                            "material": {},
                            "requires_agent": True,
                        }
                        self.store.save_action(pid, next_id, loop["round_index"], "review", "pending", {})

                elif kind == "download":
                    target_pid = payload.get("paper_id")
                    # Check cache first
                    cand = next((c for c in project.get("candidates", []) if c["paper_id"] == target_pid), None)
                    paper_rec = cand.get("paper", {}) if cand else {}
                    sha = paper_rec.get("file_sha256")
                    is_cached = False
                    if sha:
                        try:
                            self.literature.library.read_pdf(sha)
                            is_cached = True
                        except Exception:
                            is_cached = False

                    if not is_cached:
                        usage["download_attempts"] += 1
                        settled_costs["download_attempts"] = 1
                        dl_res = self.literature.download(target_pid)
                        acquisition = (dl_res.get("data") or {}).get("acquisition") or {}
                        sha = acquisition.get("file_sha256")
                        if dl_res.get("status") == "error" or acquisition.get("status") not in ("downloaded", "imported") or not sha:
                            loop["status"] = "stopped"
                            loop["stop_reason"] = "no_open_fulltext"
                            detail = dl_res.get("message") or acquisition.get("message") or "来源没有返回可用公开全文"
                            loop["stop_detail"] = f"文献 {target_pid} 无法合法获取公开全文: {detail}"
                            self.store.save_action(pid, action_id, loop["round_index"], kind, "failed", payload, result={"error": detail})
                            saved = self.store.save(pid, loop, expected_loop_revision, "无公开全文停止", project["revision"])
                            return _response({"project_id": pid, "project_revision": project["revision"], "loop_revision": saved["loop_revision"], "loop": saved, "project": project})

                    download_receipt = self.literature.get(paper_id=target_pid)["data"] if is_cached else dl_res["data"]
                    step_result = {"paper_id": target_pid, "downloaded": True, "file_sha256": sha,
                                   "download_receipt": download_receipt}
                    # Next is read
                    next_id = generate_action_id()
                    loop["status"] = "ready"
                    loop["next_action"] = {
                        "action_id": next_id,
                        "kind": "read",
                        "gap_ids": cur_act.get("gap_ids", []),
                        "reason": f"读取文献 {target_pid} 正文",
                        "payload": {"paper_id": target_pid, "page_number": 1, "offset": 0},
                        "material": {"paper": paper_rec},
                        "requires_agent": False,
                    }
                    self.store.save_action(pid, next_id, loop["round_index"], "read", "pending", loop["next_action"]["payload"])

                elif kind == "read":
                    target_pid = payload.get("paper_id")
                    page_num = payload.get("page_number", 1)
                    offset = payload.get("offset", 0)

                    batch_pages = min(budget.get("batch_size", 3), budget["max_pages"] - usage["pages"])
                    batch_pages = max(1, min(batch_pages, 10))
                    max_chars = min(20000, budget["max_text_chars"] - usage["text_chars"])

                    read_res = self.literature.read(
                        target_pid, page_number=page_num, page_count=batch_pages,
                        offset=offset, max_chars=max_chars
                    )
                    if read_res.get("status") == "error":
                        raise JournalError(read_res.get("error_code") or "READ_ERROR", read_res.get("message") or "正文读取失败")

                    read_data = read_res["data"]
                    sha = read_data.get("file_sha256", "")
                    fragments = read_data.get("fragments", [])
                    pages = read_data.get("pages", [])

                    total_new_pages = 0
                    total_new_chars = 0
                    for page in pages:
                        p_no = page.get("page_number", 1)
                        p_st = page.get("offset_start", 0)
                        p_ed = page.get("offset_end", len(page.get("text", "")))
                        if p_ed > p_st:
                            np, nc = self.store.record_read_range(pid, target_pid, sha, p_no, p_st, p_ed)
                            total_new_pages += np
                            total_new_chars += nc

                    for frag in fragments:
                        p_no = frag.get("page_number", 1)
                        f_st = frag.get("start", frag.get("offset", 0))
                        f_ed = frag.get("end", f_st + len(frag.get("text", "")))
                        if f_ed > f_st:
                            np, nc = self.store.record_read_range(pid, target_pid, sha, p_no, f_st, f_ed)
                            total_new_pages += np
                            total_new_chars += nc

                    usage["read_steps"] += 1
                    usage["pages"] += total_new_pages
                    usage["text_chars"] += total_new_chars
                    settled_costs["read_steps"] = 1
                    settled_costs["pages"] = total_new_pages
                    settled_costs["text_chars"] = total_new_chars

                    # Check read_papers count
                    baseline_reads = set(loop.get("inherited_baseline", {}).get("read_papers", []))
                    all_round_reads = set()
                    for act in loop.get("actions", []):
                        if act["kind"] == "read" and act["status"] == "completed":
                            p_id = act.get("payload", {}).get("paper_id")
                            if p_id and p_id not in baseline_reads:
                                all_round_reads.add(p_id)
                    if target_pid not in baseline_reads and target_pid not in all_round_reads:
                        usage["read_papers"] += 1
                        settled_costs["read_papers"] = 1

                    step_result = {
                        "paper_id": target_pid,
                        "file_sha256": sha,
                        "pages": pages,
                        "fragments": fragments,
                        "next_cursor": read_data.get("next_cursor"),
                    }

                    # Next action is interpretation
                    next_id = generate_action_id()
                    loop["status"] = "awaiting_interpretation"
                    loop["next_action"] = {
                        "action_id": next_id,
                        "kind": "interpretation",
                        "gap_ids": cur_act.get("gap_ids", []),
                        "reason": f"文献 {target_pid} 已提取正文片段，请保存客观事实解读卡",
                        "payload": {
                            "paper_id": target_pid,
                            "file_sha256": sha,
                            "next_cursor": read_data.get("next_cursor"),
                        },
                        "material": {
                            "paper_id": target_pid,
                            "file_sha256": sha,
                            "fragments": fragments,
                            "pages": pages,
                            "agent_contract": read_data.get("agent_contract", {}),
                        },
                        "requires_agent": True,
                    }
                    self.store.save_action(pid, next_id, loop["round_index"], "interpretation", "pending", loop["next_action"]["payload"])

            finally:
                if owner_token is not None:
                    _CURRENT_LOOP_OWNER.reset(owner_token)

            # Record action completion
            self.store.save_action(
                pid, action_id, loop["round_index"], kind, "completed",
                payload, result=step_result, settled_cost=settled_costs
            )

        finally:
            self.store.release_lease(pid, action_id, token)

        # Check if paused or stopped while working
        latest_loop = self.store.get(pid)
        if latest_loop and latest_loop["status"] in ("paused", "stopped"):
            loop["status"] = latest_loop["status"]
            loop["stop_reason"] = latest_loop["stop_reason"]
            loop["stop_detail"] = latest_loop["stop_detail"]

        # Ensure usage.rounds is at least 1 once an action completes
        loop["usage"]["rounds"] = max(1, loop["usage"].get("rounds", 0))

        saved_loop = self.store.save(pid, loop, expected_loop_revision, f"执行 {kind} 动作完成", new_project_revision)
        fresh_proj = self._get_project_data(pid)
        return _response({
            "project_id": pid,
            "project_revision": fresh_proj["revision"],
            "loop_revision": saved_loop["loop_revision"],
            "loop": saved_loop,
            "project": fresh_proj,
            "step_result": step_result,
        })

    @_service_guard
    def apply_feedback(
        self,
        project_id: str,
        action_id: str,
        feedback: Dict[str, Any],
        expected_project_revision: int,
        expected_loop_revision: int,
        origin: str = "calling_agent"
    ) -> Dict[str, Any]:
        """Applies agent feedback: assessment, interpretation, or draft revision."""
        pid = validate_project_id(project_id)
        integer(expected_project_revision, "expected_project_revision", 1)
        integer(expected_loop_revision, "expected_loop_revision", 1)

        project = self._get_project_data(pid)
        if project["revision"] != expected_project_revision:
            raise JournalError("REVISION_CONFLICT", "项目版本已变更，请核对后重试")

        loop = self.store.get(pid)
        if loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")
        if loop["loop_revision"] != expected_loop_revision:
            raise JournalError("REVISION_CONFLICT", "循环已被其他操作推进，请刷新重试")

        if loop["status"] in ("paused", "stopped"):
            raise JournalError("LOOP_NOT_ACTIVE", f"循环已处于 {loop['status']}，不得应用反馈")

        cur_act = loop.get("next_action")
        if not cur_act or cur_act.get("action_id") != action_id:
            raise JournalError("ACTION_MISMATCH", f"指定动作 {action_id} 与当前待反馈动作不符")

        kind = feedback.get("kind")
        if kind != cur_act["kind"]:
            raise JournalError("KIND_MISMATCH", f"反馈类型 {kind} 与动作要求类型 {cur_act['kind']} 不匹配")

        new_loop = copy.deepcopy(loop)
        new_project_revision = project["revision"]
        saved_reading = None

        owner_token = _CURRENT_LOOP_OWNER.set(pid)
        try:
            if kind == "assessment":
                validated_fb = parse_model(FeedbackAssessment, feedback)
                assessments = [a.model_dump(mode="json") for a in validated_fb.assessments]
                selected_ids = validated_fb.selected_paper_ids

                # Preserve old selected papers used in draft
                draft = project.get("draft") or {}
                used_in_draft = set()
                snapshot = self.writing.store.get(pid)
                ctx = self.writing._context(snapshot)
                cite_to_paper = {ev["citation_id"]: ev["paper_id"] for ev in ctx.get("evidence", {}).values()}
                for s in draft.get("sections", []):
                    for p in s.get("paragraphs", []):
                        for c in p.get("citation_ids", []):
                            if c in cite_to_paper:
                                used_in_draft.add(cite_to_paper[c])

                if selected_ids is not None:
                    missing_draft_papers = used_in_draft - set(selected_ids)
                    if missing_draft_papers:
                        raise JournalError("CANNOT_DESELECT_CITED_PAPER", f"不得移除已在草稿中引用的文献: {missing_draft_papers}")

                assess_res = self.writing.assess(
                    pid, assessments, project["revision"],
                    selected_paper_ids=selected_ids, origin=origin
                )
                if assess_res.get("status") == "error":
                    raise JournalError(assess_res.get("error_code") or "ASSESS_ERROR", assess_res.get("message") or "")
                new_project_revision = assess_res["data"]["revision"]

                # Next action: pick an unread selected paper to download or read
                fresh_proj = self._get_project_data(pid)
                new_selected = fresh_proj.get("selected_paper_ids", [])
                unread = []
                for spid in new_selected:
                    readings = self.writing.store.readings(spid)
                    if not readings:
                        unread.append(spid)

                if unread:
                    next_paper = unread[0]
                    # Check downloaded
                    cand = next((c for c in fresh_proj.get("candidates", []) if c["paper_id"] == next_paper), None)
                    paper_rec = cand.get("paper", {}) if cand else {}
                    sha = paper_rec.get("file_sha256")
                    is_downloaded = False
                    if sha:
                        try:
                            self.literature.library.read_pdf(sha)
                            is_downloaded = True
                        except Exception:
                            is_downloaded = False

                    next_id = generate_action_id()
                    new_loop["status"] = "ready"
                    if not is_downloaded:
                        new_loop["next_action"] = {
                            "action_id": next_id,
                            "kind": "download",
                            "gap_ids": cur_act.get("gap_ids", []),
                            "reason": f"下载选中文献 {next_paper} 全文",
                            "payload": {"paper_id": next_paper},
                            "material": {"paper": paper_rec},
                            "requires_agent": False,
                        }
                        self.store.save_action(pid, next_id, new_loop["round_index"], "download", "pending", new_loop["next_action"]["payload"])
                    else:
                        new_loop["next_action"] = {
                            "action_id": next_id,
                            "kind": "read",
                            "gap_ids": cur_act.get("gap_ids", []),
                            "reason": f"读取选中文献 {next_paper} 正文",
                            "payload": {"paper_id": next_paper, "page_number": 1, "offset": 0},
                            "material": {"paper": paper_rec},
                            "requires_agent": False,
                        }
                        self.store.save_action(pid, next_id, new_loop["round_index"], "read", "pending", new_loop["next_action"]["payload"])
                else:
                    # Proceed to revision or review
                    next_id = generate_action_id()
                    new_loop["status"] = "awaiting_revision"
                    new_loop["next_action"] = {
                        "action_id": next_id,
                        "kind": "revision",
                        "gap_ids": cur_act.get("gap_ids", []),
                        "reason": "文献评估完毕，请修订草稿",
                        "payload": {},
                        "material": {},
                        "requires_agent": True,
                    }
                    self.store.save_action(pid, next_id, new_loop["round_index"], "revision", "pending", {})

            elif kind == "interpretation":
                validated_fb = parse_model(FeedbackInterpretation, feedback)
                target_pid = cur_act.get("payload", {}).get("paper_id")
                target_sha = cur_act.get("payload", {}).get("file_sha256")

                # Verify reading card strictly matches current fragments
                reading_card = validated_fb.reading
                if reading_card.paper_id != target_pid or reading_card.file_sha256 != target_sha:
                    raise JournalError("IDENTITY_MISMATCH", "解读卡文献身份或正文 SHA 不匹配")

                # Delegate strict saving to literature service
                save_res = self.literature.save_reading(
                    target_pid, reading_card.model_dump(mode="json"),
                    origin=origin, strict=True
                )
                if save_res.get("status") == "error":
                    raise JournalError(save_res.get("error_code") or "READING_ERROR", save_res.get("message") or "")
                saved_reading = save_res.get("data")

                # Ensure interpreted paper is in selected_paper_ids so its evidence is visible in draft and prepare_writing
                fresh_proj = self._get_project_data(pid)
                if target_pid not in fresh_proj.get("selected_paper_ids", []):
                    cand = next((c for c in fresh_proj.get("candidates", []) if c["paper_id"] == target_pid), None)
                    if cand:
                        new_selected = list(fresh_proj.get("selected_paper_ids", [])) + [target_pid]
                        assess_res = self.writing.assess(
                            pid,
                            [{"paper_id": target_pid, "metadata_sha256": cand["metadata_sha256"],
                              "relevance": "core", "reason": "定向补读后纳入选用", "question_ids": ["RQ1"], "basis": "metadata"}],
                            fresh_proj["revision"],
                            selected_paper_ids=new_selected,
                            origin=origin
                        )
                        if assess_res.get("status") == "success":
                            new_project_revision = assess_res["data"]["revision"]

                # Check read_more: default to bool(next_cursor) if None; explicit False stops target range
                has_next = bool(cur_act.get("payload", {}).get("next_cursor"))
                should_read_more = has_next if validated_fb.read_more is None else (bool(validated_fb.read_more) and has_next)

                if should_read_more:
                    next_cur = cur_act["payload"]["next_cursor"]
                    next_id = generate_action_id()
                    new_loop["status"] = "ready"
                    new_loop["next_action"] = {
                        "action_id": next_id,
                        "kind": "read",
                        "gap_ids": cur_act.get("gap_ids", []),
                        "reason": f"接续读取文献 {target_pid} 正文",
                        "payload": {
                            "paper_id": target_pid,
                            "page_number": next_cur.get("page_number", 1),
                            "offset": next_cur.get("offset", 0),
                        },
                        "material": cur_act.get("material", {}),
                        "requires_agent": False,
                    }
                    self.store.save_action(pid, next_id, new_loop["round_index"], "read", "pending", new_loop["next_action"]["payload"])
                else:
                    # Move to revision
                    next_id = generate_action_id()
                    new_loop["status"] = "awaiting_revision"
                    new_loop["next_action"] = {
                        "action_id": next_id,
                        "kind": "revision",
                        "gap_ids": cur_act.get("gap_ids", []),
                        "reason": "文献解读完成，请结合新论据修订草稿章节",
                        "payload": {},
                        "material": {},
                        "requires_agent": True,
                    }
                    self.store.save_action(pid, next_id, new_loop["round_index"], "revision", "pending", {})

            elif kind == "revision":
                validated_fb = parse_model(FeedbackRevision, feedback)
                draft_dict = validated_fb.draft.model_dump(mode="json")
                save_res = self.writing.save_draft(
                    pid, draft_dict, project["revision"],
                    change_note=validated_fb.change_note
                )
                if save_res.get("status") == "error":
                    raise JournalError(save_res.get("error_code") or "DRAFT_ERROR", save_res.get("message") or "")
                new_project_revision = save_res["data"]["revision"]

                # A round completes after draft revision
                new_loop["round_index"] += 1
                new_loop["status"] = "awaiting_review"
                next_id = generate_action_id()
                new_loop["next_action"] = {
                    "action_id": next_id,
                    "kind": "review",
                    "gap_ids": [],
                    "reason": "草稿已完成新一轮修订，请进行新一轮质量评审",
                    "payload": {},
                    "material": {},
                    "requires_agent": True,
                }
                self.store.save_round(
                    pid, new_loop["round_index"],
                    goal=f"第 {new_loop['round_index']} 轮质量评审与缺口核查",
                    status="active",
                    round_data={"queries_history": [], "new_quotes": [], "gaps": []}
                )
                self.store.save_action(pid, next_id, new_loop["round_index"], "review", "pending", {})

        finally:
            if owner_token is not None:
                _CURRENT_LOOP_OWNER.reset(owner_token)

        self.store.save_action(pid, action_id, loop["round_index"], kind, "completed", cur_act.get("payload", {}))
        saved_loop = self.store.save(pid, new_loop, expected_loop_revision, f"应用 {kind} 反馈", new_project_revision)
        fresh_proj = self._get_project_data(pid)
        return _response({
            "project_id": pid,
            "project_revision": fresh_proj["revision"],
            "loop_revision": saved_loop["loop_revision"],
            "loop": saved_loop,
            "project": fresh_proj,
            **({"saved_reading": saved_reading} if saved_reading else {}),
        })

    @_service_guard
    def reserve_model_call(
        self,
        project_id: str,
        action_id: str,
        expected_project_revision: int,
        expected_loop_revision: int
    ) -> Dict[str, Any]:
        """Reserve a GUI/native model attempt only against current project and loop revisions."""
        pid = validate_project_id(project_id)
        integer(expected_project_revision, "expected_project_revision", 1)
        integer(expected_loop_revision, "expected_loop_revision", 1)

        project = self._get_project_data(pid)
        if project["revision"] != expected_project_revision:
            raise JournalError("REVISION_CONFLICT", "项目已被其他操作修改，请刷新后重新授权模型调用")
        loop = self.store.get(pid)
        if loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")
        if loop["loop_revision"] != expected_loop_revision:
            raise JournalError("REVISION_CONFLICT", "循环已被其他操作推进，请刷新重试")

        budget = loop["budget"]
        usage = loop["usage"]
        reserved = loop["reserved"]

        if usage["gui_model_calls"] + reserved["gui_model_calls"] + 1 > budget["max_gui_model_calls"]:
            raise JournalError("BUDGET_EXHAUSTED", "GUI 模型调用次数已达预算上限")

        ticket_id = self.store.record_model_ticket(pid, action_id, expected_loop_revision)
        new_loop = copy.deepcopy(loop)
        new_loop["reserved"]["gui_model_calls"] += 1

        saved_loop = self.store.save(pid, new_loop, expected_loop_revision, "预留模型调用尝试", project["revision"])
        return _response({
            "project_id": pid,
            "project_revision": project["revision"],
            "loop_revision": saved_loop["loop_revision"],
            "loop": saved_loop,
            "project": project,
            "model_ticket": ticket_id,
        })

    @_service_guard
    def release_unissued_model_call(self, project_id: str, action_id: str, model_ticket: str) -> Dict[str, Any]:
        """Trusted coordinator only: release a ticket before any provider request.

        This is deliberately not exposed as an Agent/MCP tool. Issued/ambiguous calls
        must use normal settlement or reconciliation, never this unissued refund.
        """
        pid = validate_project_id(project_id)
        changed = self.store.release_unissued_model_ticket(pid, action_id, model_ticket)
        loop = self.store.get(pid)
        return _response({
            "project_id": pid, "loop_revision": loop["loop_revision"], "loop": loop,
            "released": changed, "provider_request_issued": False,
        })

    @_service_guard
    def settle_model_call(
        self,
        project_id: str,
        action_id: str,
        model_ticket: str,
        success: bool
    ) -> Dict[str, Any]:
        """GUI runner dedicated method: settles model call attempt (both success and failure count)."""
        pid = validate_project_id(project_id)
        self.store.settle_model_ticket(pid, action_id, model_ticket, success)

        project = self._get_project_data(pid)
        loop = self.store.get(pid)
        if loop is None:
            raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")

        new_loop = copy.deepcopy(loop)
        if new_loop["reserved"]["gui_model_calls"] > 0:
            new_loop["reserved"]["gui_model_calls"] -= 1
        new_loop["usage"]["gui_model_calls"] += 1

        saved_loop = self.store.save(
            pid, new_loop, loop["loop_revision"], "结算模型调用尝试",
            expected_project_revision=project["revision"]
        )
        return _response({
            "project_id": pid,
            "project_revision": project["revision"],
            "loop_revision": saved_loop["loop_revision"],
            "loop": saved_loop,
            "project": project,
            "success": success,
        })
