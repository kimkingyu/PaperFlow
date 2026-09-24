"""PaperFlow CLI journal command group.

Provides offline, local-first search, risk checking, comparison, and submission event tracking.
Exit codes:
  0: Success (including empty search results)
  1: Clear/definite risk or blocked by school policy
  2: Parameter or input validation error
  3: Source unavailable or runtime error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from paperflow.engine.journal_finder import JournalFinder, read_json
from paperflow.engine.journals.models import JournalError

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = None


def _format_error_envelope(err: Exception) -> Dict[str, Any]:
    error_code = "INTERNAL_ERROR"
    message = "操作执行失败，请检查输入参数或本地数据文件状态"
    if isinstance(err, JournalError):
        error_code = getattr(err, "code", "JOURNAL_ERROR")
        message = str(err)
    elif ValidationError and isinstance(err, ValidationError):
        error_code = "VALIDATION_ERROR"
        safe_errors = []
        for e in err.errors():
            safe_errors.append({
                "loc": list(e.get("loc", ())),
                "type": e.get("type", "validation_error"),
            })
        message = "参数或数据字段校验未通过: " + json.dumps(safe_errors, ensure_ascii=False)
    return {
        "status": "error",
        "error_code": error_code,
        "message": message,
        "data": None,
        "sources": [],
        "coverage": {},
        "warnings": [],
        "suggested_options": [],
    }


def _get_finder(data_dir: Optional[str] = None) -> JournalFinder:
    target_dir = data_dir or os.environ.get("PAPERFLOW_JOURNAL_HOME")
    return JournalFinder(data_dir=target_dir if target_dir else None)


def _output(payload: Any, as_json: bool) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # Human-readable formatting without emoji
    status = payload.get("status", "")
    if status == "error":
        print(f"[错误] {payload.get('error_code', 'ERROR')}: {payload.get('message', '')}")
        return

    data = payload.get("data")
    if isinstance(data, list):
        print(f"共 {len(data)} 条记录:")
        for item in data:
            if isinstance(item, dict):
                tid = item.get("journal_id") or item.get("id") or ""
                title = item.get("title") or item.get("name") or ""
                print(f"  - {tid}: {title}")
            else:
                print(f"  - {item}")
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {v}")
    else:
        print(str(data))

    warnings = payload.get("warnings") or []
    if warnings:
        print("\n提示与局限:")
        for w in warnings:
            print(f"  * {w}")


def _determine_check_exit_code(res: Dict[str, Any]) -> int:
    # 1: 明确风险或学校政策明确拦截
    data = res.get("data") or {}
    conclusion = data.get("conclusion") or ""
    blocked = data.get("blocked")
    level = data.get("overall_risk_level") or data.get("level") or ""
    compliant = data.get("compliant")

    # 对 blocked=True 或 conclusion=flagged 返回 1
    # needs_verification 不能误当已合规 (但只要不是明确拦截 blocked=True 或 conclusion=flagged，由上层调用决定)
    if blocked is True or conclusion == "flagged" or level in ("high", "blocked", "flagged") or compliant is False:
        return 1
    return 0


def cmd_sources(finder: JournalFinder, args: argparse.Namespace) -> int:
    try:
        res = finder.list_sources(source_id=args.source_id or "")
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_import(finder: JournalFinder, args: argparse.Namespace) -> int:
    dry_run = not args.apply
    try:
        res = finder.import_data(
            source_id=args.source_id,
            file_path=args.file,
            kind=args.kind,
            data_year=args.data_year,
            encoding=args.encoding,
            dry_run=dry_run,
            source_version=args.version or "",
            dataset_id=args.dataset_id or "",
            allow_shrink=args.allow_shrink,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if (ValidationError and isinstance(e, ValidationError)) else 3


def cmd_refresh(finder: JournalFinder, args: argparse.Namespace) -> int:
    dry_run = not args.apply
    try:
        res = finder.refresh(
            source_id=args.source_id,
            dataset_id=args.dataset_id,
            dry_run=dry_run,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_search(finder: JournalFinder, args: argparse.Namespace) -> int:
    filters: Dict[str, Any] = {}
    if args.field:
        filters["field"] = args.field
    if args.kind:
        filters["kind"] = args.kind
    if args.indexing:
        filters["indexing"] = [i.strip() for i in args.indexing.split(",") if i.strip()]
    if args.rank_system:
        filters["rank_system"] = args.rank_system
    if args.rank_year is not None:
        filters["rank_year"] = args.rank_year
    if args.quartiles:
        try:
            filters["quartiles"] = [int(q.strip()) for q in args.quartiles.split(",") if q.strip()]
        except ValueError:
            err = JournalError("INVALID_INPUT", "--quartiles 必须为逗号分隔的整数(1-4)")
            _output(_format_error_envelope(err), args.json)
            return 2
    if args.category:
        filters["category"] = args.category
    if args.category_type:
        filters["category_type"] = args.category_type
    if args.ccf_grades:
        filters["ccf_grades"] = [g.strip().upper() for g in args.ccf_grades.split(",") if g.strip()]
    if args.top is not None:
        filters["top"] = args.top
    if args.oa_mode:
        filters["oa_mode"] = args.oa_mode
    if args.max_apc is not None:
        filters["max_apc"] = args.max_apc
    if args.currency:
        filters["currency"] = args.currency
    if args.max_first_decision_days is not None:
        filters["max_first_decision_days"] = args.max_first_decision_days
    if args.min_annual_articles is not None:
        filters["min_annual_articles"] = args.min_annual_articles
    if args.metric_year is not None:
        filters["metric_year"] = args.metric_year
    if args.risk_policy:
        filters["risk_policy"] = args.risk_policy
    if args.safe:
        filters["risk_policy"] = "verified_only"
    if args.warning_years:
        try:
            filters["warning_years"] = [int(y.strip()) for y in args.warning_years.split(",") if y.strip()]
        except ValueError:
            err = JournalError("INVALID_INPUT", "--warning-years 必须为逗号分隔的年份")
            _output(_format_error_envelope(err), args.json)
            return 2
    if args.allow_unknown:
        filters["allow_unknown"] = True
    if args.profile_id:
        filters["profile_id"] = args.profile_id

    # --fast 要求: max_first_decision_days=45, min_annual_articles=1000, 且必须显式指定 --metric-year
    if args.fast:
        if args.metric_year is None:
            err = JournalError("INVALID_INPUT", "--fast 选项需要显式指定 --metric-year，不能猜测年份")
            _output(_format_error_envelope(err), args.json)
            return 2
        filters["max_first_decision_days"] = 45.0
        filters["min_annual_articles"] = 1000

    try:
        res = finder.search(
            query=args.query or "",
            filters=filters,
            sort_by=args.sort_by,
            limit=args.limit,
            offset=args.offset,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if (ValidationError and isinstance(e, ValidationError)) else 3


def cmd_show(finder: JournalFinder, args: argparse.Namespace) -> int:
    try:
        res = finder.details(query=args.query)
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_check(finder: JournalFinder, args: argparse.Namespace) -> int:
    warning_years = None
    if args.warning_years:
        try:
            warning_years = [int(y.strip()) for y in args.warning_years.split(",") if y.strip()]
        except ValueError:
            err = JournalError("INVALID_INPUT", "--warning-years 必须为逗号分隔的年份")
            _output(_format_error_envelope(err), args.json)
            return 2
    try:
        res = finder.check_warning(
            query=args.query,
            profile_id=args.profile_id,
            warning_years=warning_years,
        )
        _output(res, args.json)
        if res.get("status") == "error":
            return 2 if res.get("error_code") == "INVALID_INPUT" else 3
        return _determine_check_exit_code(res)
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_compare(finder: JournalFinder, args: argparse.Namespace) -> int:
    try:
        res = finder.compare(
            journal_ids=args.ids,
            rank_system=args.rank_system,
            rank_year=args.rank_year,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_peers(finder: JournalFinder, args: argparse.Namespace) -> int:
    try:
        res = finder.peers(
            references=None,
            file_path=args.file or "",
            filters=None,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


def cmd_track(finder: JournalFinder, args: argparse.Namespace) -> int:
    data = None
    if args.data:
        try:
            data = json.loads(args.data)
        except Exception:
            err = JournalError("INVALID_INPUT", "--data 必须是合法的 JSON 字符串")
            _output(_format_error_envelope(err), args.json)
            return 2
    try:
        res = finder.tracker(
            provider=args.provider,
            data=data,
            file_path=args.file or "",
            previous_file_path=args.previous or "",
            include_title=args.include_title,
        )
        _output(res, args.json)
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code == "INVALID_INPUT" else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 3


_RECOMMEND_INPUT_ERROR_CODES = {
    "INVALID_INPUT",
    "MUTUALLY_EXCLUSIVE_INPUT",
    "INVALID_MODE",
    "INVALID_MAX_CHARS",
    "VALIDATION_ERROR",
    "INVALID_ASSESSMENT",
    "STALE_ASSESSMENT",
    "INPUT_TOO_LARGE",
    "INVALID_PATH",
    "NETWORK_PATH_REJECTED",
    "FILE_NOT_FOUND",
    "FILE_ACCESS_ERROR",
    "FILE_TOO_LARGE",
    "EMPTY_INPUT",
    "INVALID_ARCHIVE",
    "DANGEROUS_ARCHIVE",
    "ZIP_BOMB_DETECTED",
    "INVALID_DOCX",
    "INVALID_PDF",
    "PDF_RESOURCE_LIMIT",
    "PDF_ENCRYPTED",
    "PDF_EXTRACT_ERROR",
    "OCR_REQUIRED",
}


def _truncate_text(text: str, max_chars: int = 80) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _format_cost_text(cost: Optional[Dict[str, Any]]) -> str:
    if not isinstance(cost, dict):
        return "费用: 未知"
    selected = cost.get("selected")
    if not isinstance(selected, dict):
        return "费用: 未知"
    route = selected.get("route") or "未知路线"
    est = selected.get("estimated_total")
    known = selected.get("known_charges")
    cur = selected.get("currency") or ""
    amount = est if est is not None else known

    if amount is None:
        return f"费用: {route} 未知"
    # Never print 0 without context, but if estimated_total is known 0, check if amount is 0 and cur is empty/unknown
    if cur:
        return f"费用: {route} {cur} {amount:g}"
    return f"费用: {route} {amount:g}"


def _format_rankings_text(rankings: List[Dict[str, Any]]) -> str:
    if not rankings or not isinstance(rankings, list):
        return "分区: 未知"
    parts = []
    for r in rankings:
        if not isinstance(r, dict):
            continue
        sys_name = r.get("system") or ""
        year = r.get("year")
        cat = r.get("category_name") or ""
        q = r.get("quartile") or ""
        q_str = f"{q}区" if q else ""
        y_str = f"({year})" if year else ""
        sys_label = f"{sys_name.upper()}{y_str}" if sys_name else y_str
        item = f"{sys_label} {cat} {q_str}".strip()
        if item:
            parts.append(item)
    return " | ".join(parts[:2]) if parts else "分区: 未知"


def _format_risk_summary(risk: Optional[Dict[str, Any]]) -> str:
    if not isinstance(risk, dict):
        return "风险: 未知"
    level = risk.get("overall_risk_level") or risk.get("level") or "未知"
    conclusion = risk.get("conclusion") or ""
    if conclusion == "flagged":
        return f"风险: 高/预警拦截 ({conclusion})"
    if risk.get("needs_verification"):
        return f"风险: {level} (待核验)"
    return f"风险: {level}"


def _output_recommend_readable(payload: Dict[str, Any]) -> None:
    status = payload.get("status", "")
    if status == "error":
        print(f"[错误] {payload.get('error_code', 'ERROR')}: {payload.get('message', '')}")
        return

    data = payload.get("data")
    if not isinstance(data, dict):
        print(str(data))
        return

    stage = data.get("stage", "")
    context_id = data.get("context_id", "")
    input_id = data.get("input_id", "")

    print(f"=== 期刊推荐结果 (阶段: {stage}) ===")
    if context_id:
        print(f"context_id: {context_id}")
    if input_id:
        print(f"input_id: {input_id}")

    if stage == "needs_candidate_evidence":
        print("\n[说明] 缺少可用候选证据，或征稿资料过期／不完整。CLI 本身不包含任何大语言模型。")
        print("建议：可传入 --candidates 文件提供候选期刊，或先使用 paperflow journal import 导入期刊数据库。")
    elif stage == "needs_agent_assessment":
        print("\n[说明] 候选期刊已初筛，但缺少当前调用方 Agent 的语义画像或适配度评估。CLI 本身不包含任何大语言模型，不生成假智能评分。")
        print("建议：请由当前上层 Agent 根据稿件与期刊征稿范围生成画像(--profile)与评估(--assessments)后重新提交。")

    groups = data.get("groups") or {}
    group_titles = [
        ("efficiency", "【稳妥／效率档】"),
        ("balanced", "【均衡档】"),
        ("stretch", "【冲刺／领域顶刊档】"),
        ("elite", "【可选极限冲刺】"),
    ]

    for g_key, g_title in group_titles:
        g_data = groups.get(g_key)
        if not g_data or not isinstance(g_data, dict):
            continue
        rec_list = g_data.get("recommended") or []
        prov_list = g_data.get("provisional") or []

        if not rec_list and not prov_list:
            continue

        print(f"\n{g_title}")

        if rec_list:
            print("  [推荐梯队 (基于所给证据)]")
            for card in rec_list:
                _print_journal_card(card, is_provisional=False)

        if prov_list:
            print("  [待核验梯队 (关键依据/风险/费用待核验)]")
            for card in prov_list:
                _print_journal_card(card, is_provisional=True)

    unclassified = data.get("unclassified") or []
    if unclassified:
        print("\n【未分档期刊】")
        for card in unclassified:
            _print_journal_card(card, is_provisional=True)

    excluded = data.get("excluded") or []
    if excluded:
        print(f"\n【已排除期刊 ({len(excluded)} 种)】")
        for ex in excluded[:10]:
            t = ex.get("title") or ex.get("journal_id") or ""
            reasons = ex.get("reasons") or []
            r_str = "; ".join(reasons)
            print(f"  * {t}: {r_str}")
        if len(excluded) > 10:
            print(f"  ... 另有 {len(excluded) - 10} 种期刊排除未列出")

    warnings = payload.get("warnings") or []
    if warnings:
        print("\n提示与局限:")
        for w in warnings:
            print(f"  * {w}")


def _print_journal_card(card: Dict[str, Any], is_provisional: bool) -> None:
    title = card.get("title") or card.get("journal_id") or "未知期刊"
    issns = card.get("issns") or []
    issn_str = f" [ISSN: {','.join(issns)}]" if issns else ""

    score_dict = card.get("score")
    if isinstance(score_dict, dict) and score_dict.get("value") is not None:
        val = score_dict.get("value")
        comp = score_dict.get("evidence_completeness", 0)
        score_str = f"推荐分: {val:g} (证据完整度: {comp:g}%)"
    else:
        score_str = "未评估"

    rank_str = _format_rankings_text(card.get("rankings") or [])
    cost_str = _format_cost_text(card.get("cost"))
    risk_str = _format_risk_summary(card.get("risk"))

    prov_tag = " [待核验]" if is_provisional else ""
    print(f"  - {title}{issn_str}{prov_tag}")
    print(f"    {score_str} | {rank_str} | {cost_str} | {risk_str}")

    rationale = card.get("rationale")
    if rationale:
        print(f"    推荐理由: {_truncate_text(rationale, 80)}")

    improvements = card.get("improvements") or []
    if improvements and isinstance(improvements, list):
        imp_str = "; ".join(str(x) for x in improvements)
        print(f"    补强建议: {_truncate_text(imp_str, 80)}")

    missing = card.get("missing") or []
    if missing and is_provisional:
        miss_str = "; ".join(str(m) for m in missing)
        print(f"    相应缺口: {_truncate_text(miss_str, 80)}")


def _load_dict_json(path: str, label: str) -> Dict[str, Any]:
    obj = read_json(path)
    if not isinstance(obj, dict):
        raise JournalError("INVALID_INPUT", f"{label} 必须为 JSON 对象 (dict)")
    return dict(obj)


def _load_dict_list_json(path: str, label: str, max_items: int = 200) -> List[Dict[str, Any]]:
    obj = read_json(path)
    if not isinstance(obj, list):
        raise JournalError("INVALID_INPUT", f"{label} 必须为 JSON 数组 (list)")
    if len(obj) > max_items:
        raise JournalError("INVALID_INPUT", f"{label} 元素数量不能超过 {max_items} 条")
    if not all(isinstance(item, dict) for item in obj):
        raise JournalError("INVALID_INPUT", f"{label} 数组中的每个元素必须为 JSON 对象 (dict)")
    return [dict(item) for item in obj]


def cmd_recommend(finder: JournalFinder, args: argparse.Namespace) -> int:
    text = getattr(args, "text", "") or ""
    file_path = getattr(args, "input", "") or ""
    mode = getattr(args, "mode", "auto") or "auto"

    try:
        if text.strip() and file_path.strip():
            raise JournalError("INVALID_INPUT", "--text 与 --input 互斥，不能同时指定")
        if not text.strip() and not file_path.strip():
            raise JournalError("INVALID_INPUT", "必须指定 --text 或 --input 之一")
        if mode not in ("auto", "idea", "manuscript"):
            raise JournalError("INVALID_INPUT", "--mode 必须为 auto、idea 或 manuscript")

        if getattr(args, "prepare_only", False):
            max_chars = getattr(args, "max_chars", 60000)
            res = finder.prepare_manuscript(
                text=text,
                file_path=file_path,
                mode=mode,
                max_chars=max_chars,
            )
        else:
            profile = _load_dict_json(args.profile, "--profile") if getattr(args, "profile", None) else None
            assessments = (
                _load_dict_list_json(args.assessments, "--assessments", max_items=200)
                if getattr(args, "assessments", None)
                else None
            )
            candidate_records = (
                _load_dict_list_json(args.candidates, "--candidates", max_items=200)
                if getattr(args, "candidates", None)
                else None
            )
            preferences = (
                _load_dict_json(args.preferences, "--preferences")
                if getattr(args, "preferences", None)
                else None
            )
            if getattr(args, "include_elite", False):
                preferences = dict(preferences) if preferences is not None else {}
                preferences["include_elite"] = True

            res = finder.recommend(
                text=text,
                file_path=file_path,
                mode=mode,
                profile=profile,
                assessments=assessments,
                candidate_records=candidate_records,
                preferences=preferences,
            )

        if getattr(args, "prepare_only", False):
            _output(res, args.json)
        else:
            if args.json:
                _output(res, as_json=True)
            else:
                _output_recommend_readable(res)

        if isinstance(res, dict) and res.get("status") == "error":
            return 2 if res.get("error_code") in _RECOMMEND_INPUT_ERROR_CODES else 3
        return 0
    except JournalError as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if e.code in _RECOMMEND_INPUT_ERROR_CODES else 3
    except Exception as e:
        _output(_format_error_envelope(e), args.json)
        return 2 if (ValidationError and isinstance(e, ValidationError)) else 3


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperflow journal",
        description="PaperFlow 期刊本地选刊、风险预警与审稿跟踪命令行工具",
    )
    # Common flags can be set globally
    parser.add_argument("--data-dir", default=None, help="本地期刊数据库目录 (默认读取 PAPERFLOW_JOURNAL_HOME 或 AppData)")
    parser.add_argument("--json", action="store_true", help="以机器可读的 JSON 格式输出")

    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    def _add_common(subp: argparse.ArgumentParser) -> None:
        subp.add_argument("--data-dir", dest="sub_data_dir", default=None, help="本地期刊数据库目录")
        subp.add_argument("--json", dest="sub_json", action="store_true", default=False, help="以 JSON 格式输出")

    # sources
    p_sources = subparsers.add_parser("sources", help="查看数据源目录和本地快照")
    p_sources.add_argument("source_id", nargs="?", default="", help="可选的数据源 ID")
    _add_common(p_sources)

    # import
    p_import = subparsers.add_parser("import", help="导入外部期刊数据集或学校规则")
    p_import.add_argument("source_id", help="数据源 ID")
    p_import.add_argument("file", help="输入数据文件路径 (CSV/JSON/SQLite)")
    p_import.add_argument("--kind", default="auto", help="解析器类型 (默认 auto)")
    p_import.add_argument("--data-year", type=int, default=None, help="数据所属年份")
    p_import.add_argument("--encoding", default="utf-8-sig", help="文件编码 (默认 utf-8-sig)")
    group_imp = p_import.add_mutually_exclusive_group()
    group_imp.add_argument("--dry-run", action="store_true", default=True, help="仅预览导入统计 (默认)")
    group_imp.add_argument("--apply", action="store_true", help="真正写入本地数据库")
    p_import.add_argument("--version", default="", help="版本标识")
    p_import.add_argument("--dataset-id", default="", help="数据集标识")
    p_import.add_argument("--allow-shrink", action="store_true", help="允许记录总数少于上一版本")
    _add_common(p_import)

    # refresh
    p_refresh = subparsers.add_parser("refresh", help="更新或查询在线开放数据源/API")
    p_refresh.add_argument("source_id", help="数据源 ID (如 easyscholar_api 或 github 镜像)")
    p_refresh.add_argument("dataset_id", help="数据集标识或期刊名称")
    group_ref = p_refresh.add_mutually_exclusive_group()
    group_ref.add_argument("--dry-run", action="store_true", default=True, help="仅预览 (默认)")
    group_ref.add_argument("--apply", action="store_true", help="执行更新并入库")
    _add_common(p_refresh)

    # search
    p_search = subparsers.add_parser("search", help="检索期刊与指标")
    p_search.add_argument("query", nargs="?", default="", help="检索词 (期刊名/缩写/ISSN)")
    p_search.add_argument("--field", default="", help="学科分类")
    p_search.add_argument("--kind", choices=["journal", "conference"], default="journal", help="期刊或会议")
    p_search.add_argument("--indexing", default="", help="收录体系，逗号分隔 (如 SCIE,SSCI)")
    p_search.add_argument("--rank-system", choices=["cas", "jcr", "xr", "ccf", "ccft"], default=None, help="分区体系")
    p_search.add_argument("--rank-year", type=int, default=None, help="分区年度")
    p_search.add_argument("--quartiles", default="", help="分区数字，逗号分隔 (如 1,2)")
    p_search.add_argument("--category", default="", help="学科门类")
    p_search.add_argument("--category-type", choices=["major", "minor", "subject"], default=None, help="分类级别")
    p_search.add_argument("--ccf-grades", default="", help="CCF 级别，逗号分隔 (如 A,B)")
    p_search.add_argument("--top", action="store_true", default=None, help="仅 Top 期刊")
    p_search.add_argument("--oa-mode", choices=["any", "full", "hybrid", "closed", "diamond"], default="any", help="OA 模式")
    p_search.add_argument("--max-apc", type=float, default=None, help="版面费上限")
    p_search.add_argument("--currency", default=None, help="版面费币种 (如 USD, CNY)")
    p_search.add_argument("--max-first-decision-days", type=float, default=None, help="一审审稿天数上限")
    p_search.add_argument("--min-annual-articles", type=int, default=None, help="最小年发文量")
    p_search.add_argument("--metric-year", type=int, default=None, help="指标生效年度")
    p_search.add_argument("--risk-policy", choices=["exclude_known", "verified_only", "include_flagged"], default="exclude_known")
    p_search.add_argument("--safe", action="store_true", help="映射为 risk-policy=verified_only")
    p_search.add_argument("--fast", action="store_true", help="设置45天审稿与>=1000篇年发文量，必须配合--metric-year使用")
    p_search.add_argument("--warning-years", default="", help="检查预警的年份列表，逗号分隔")
    p_search.add_argument("--allow-unknown", action="store_true", help="允许未核实状态")
    p_search.add_argument("--profile-id", default=None, help="学校策略配置 profile_id")
    p_search.add_argument("--sort-by", default="relevance", help="排序字段 (relevance, impact, volume 等)")
    p_search.add_argument("--limit", type=int, default=20, help="返回条数")
    p_search.add_argument("--offset", type=int, default=0, help="偏移量")
    _add_common(p_search)

    # show
    p_show = subparsers.add_parser("show", help="查看指定期刊详情与风险画像")
    p_show.add_argument("query", help="期刊 ID、ISSN 或刊名")
    _add_common(p_show)

    # check
    p_check = subparsers.add_parser("check", help="合规与预警检查")
    p_check.add_argument("query", help="期刊 ID、ISSN 或刊名")
    p_check.add_argument("--profile-id", default=None, help="学校策略配置 profile_id")
    p_check.add_argument("--warning-years", default="", help="预警年份，逗号分隔")
    _add_common(p_check)

    # compare
    p_compare = subparsers.add_parser("compare", help="横向对比多个期刊")
    p_compare.add_argument("ids", nargs="+", help="期刊 ID 列表")
    p_compare.add_argument("--rank-system", choices=["cas", "jcr", "xr", "ccf", "ccft"], default=None, help="对比的分区体系")
    p_compare.add_argument("--rank-year", type=int, default=None, help="对比的分区年度")
    _add_common(p_compare)

    # peers
    p_peers = subparsers.add_parser("peers", help="根据参考文献推荐投递期刊")
    p_peers.add_argument("--file", required=True, help="参考文献 JSON 文件路径")
    _add_common(p_peers)

    # track
    p_track = subparsers.add_parser("track", help="投稿进度与事件时间线离线分析")
    p_track.add_argument("--provider", default="elsevier", help="投稿系统 (当前支持 elsevier)")
    p_track.add_argument("--file", "--input", dest="file", default="", help="导出的投稿事件 JSON 文件路径")
    p_track.add_argument("--data", default="", help="输入的 JSON 字符串")
    p_track.add_argument("--previous", "--previous-file", dest="previous", default="", help="上一版本的快照文件路径")
    p_track.add_argument("--include-title", action="store_true", default=False, help="输出中包含论文标题 (默认关闭)")
    _add_common(p_track)

    # recommend
    p_recommend = subparsers.add_parser("recommend", help="结合稿件信息与画像进行期刊推荐")
    p_recommend.add_argument("--text", default="", help="稿件或设想直接文本")
    p_recommend.add_argument("--input", "--file", dest="input", default="", help="稿件文件路径 (.txt, .md, .docx, .pdf)")
    p_recommend.add_argument("--mode", default="auto", help="解析/推荐模式 (auto, idea, manuscript)")
    p_recommend.add_argument("--profile", default=None, help="稿件语义画像 JSON 文件路径")
    p_recommend.add_argument("--assessments", default=None, help="期刊匹配度评估列表 JSON 文件路径")
    p_recommend.add_argument("--candidates", default=None, help="初筛候选期刊列表 JSON 文件路径")
    p_recommend.add_argument("--preferences", default=None, help="用户偏好设置 JSON 文件路径")
    p_recommend.add_argument("--include-elite", dest="include_elite", action="store_true", help="允许包含顶尖期刊 (设置 preferences.include_elite=true)")
    p_recommend.add_argument("--prepare-only", dest="prepare_only", action="store_true", help="仅执行文本提取与分块解析，获取 input_id 与结构化文本")
    p_recommend.add_argument("--max-chars", type=int, default=60000, help="提取字符上限 (默认 60000)")
    _add_common(p_recommend)

    return parser


def run_journal_cli(args: Optional[List[str]] = None) -> int:
    parser = create_parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if not parsed.subcommand:
        parser.print_help()
        return 2

    # Propagate common flags if specified at either global or subcommand level
    parsed.data_dir = getattr(parsed, "sub_data_dir", None) or getattr(parsed, "data_dir", None)
    parsed.json = bool(getattr(parsed, "sub_json", False) or getattr(parsed, "json", False))

    finder = _get_finder(data_dir=parsed.data_dir)

    dispatch = {
        "sources": cmd_sources,
        "import": cmd_import,
        "refresh": cmd_refresh,
        "search": cmd_search,
        "show": cmd_show,
        "check": cmd_check,
        "compare": cmd_compare,
        "peers": cmd_peers,
        "track": cmd_track,
        "recommend": cmd_recommend,
    }

    handler = dispatch.get(parsed.subcommand)
    if not handler:
        parser.print_help()
        return 2

    return handler(finder, parsed)
