"""One allow-listed action surface shared by the local GUI server and the MCP Apps view.

The front end never reaches JournalFinder directly: every call goes through
``dispatch(action, params)``. File paths are never accepted from the UI, so a
page (local or embedded) cannot make the backend read arbitrary files.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict

from paperflow.engine.literature.models import ReadingCard, validate_paper_id

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, response

MAX_TEXT_CHARS = 60000
_FORBIDDEN_KEYS = {"file_path", "input_paths", "html_path", "previous_file_path"}


def _no_paths(params: Dict[str, Any]) -> None:
    bad = _FORBIDDEN_KEYS.intersection(params)
    if bad:
        raise JournalError("INVALID_INPUT", "界面不接受文件路径参数；请粘贴或上传内容")


def _text(params: Dict[str, Any]) -> str:
    text = params.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        raise JournalError("INVALID_INPUT", "请粘贴研究想法、摘要或稿件内容")
    if len(text) > MAX_TEXT_CHARS:
        raise JournalError("INPUT_TOO_LARGE", f"文本超过 {MAX_TEXT_CHARS} 字符上限")
    return text


def _overview(finder: JournalFinder, params: Dict[str, Any]):
    from paperflow.engine.journals.builder import summarize_coverage
    sources = finder.list_sources()
    snapshots = finder.store.snapshots()
    initialized = finder.store.path.exists()
    # Content statistics (journal counts, SCI/EI, fee/timing coverage) are computed from the
    # stored records; the store's own `coverage()` only lists snapshots and is kept separately.
    stats = summarize_coverage(finder.store.records()) if initialized else summarize_coverage([])
    return response({
        "database_initialized": initialized,
        "database_path": str(finder.store.path),
        "sources": [{k: s.get(k) for k in ("id", "name", "mode", "code_license", "data_license",
                                            "capabilities", "limitations", "source_url", "initialized")}
                    for s in sources["data"]],
        "snapshot_count": len(snapshots),
        "coverage": stats,
        "snapshots": finder.store.coverage().get("snapshots", []),
    })


def _build(finder: JournalFinder, params: Dict[str, Any]):
    return finder.build_database(dry_run=bool(params.get("dry_run", True)), force=bool(params.get("force", False)))


def _search(finder: JournalFinder, params: Dict[str, Any]):
    limit = int(params.get("limit", 20))
    return finder.search(query=str(params.get("query", ""))[:1000], filters=params.get("filters") or {},
                         sort_by=str(params.get("sort_by", "relevance")), limit=max(1, min(limit, 100)),
                         offset=max(0, int(params.get("offset", 0))))


def _details(finder: JournalFinder, params: Dict[str, Any]):
    return finder.details(query=str(params.get("query", ""))[:1000])


def _check(finder: JournalFinder, params: Dict[str, Any]):
    return finder.check_warning(query=str(params.get("query", ""))[:1000],
                                profile_id=params.get("profile_id"), warning_years=params.get("warning_years"))


def _compare(finder: JournalFinder, params: Dict[str, Any]):
    ids = params.get("journal_ids") or []
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10:
        raise JournalError("INVALID_INPUT", "对比需要 1 到 10 本期刊")
    return finder.compare(journal_ids=[str(i) for i in ids], rank_system=params.get("rank_system"),
                          rank_year=params.get("rank_year"))


def _prepare(finder: JournalFinder, params: Dict[str, Any]):
    return finder.prepare_manuscript(text=_text(params), mode=str(params.get("mode", "auto")))


def _read_project(finder: JournalFinder, params: Dict[str, Any]):
    source_type = str(params.get("source_type", "local") or "local")
    if source_type == "local":
        project_path = str(params.get("path", "") or "").strip()
        if not project_path:
            raise JournalError("INVALID_INPUT", "请提供本地项目文件夹路径")
        return finder.prepare_manuscript(project_path=project_path, mode=str(params.get("mode", "auto")))
    if source_type == "github":
        repo = str(params.get("repo", "") or "").strip()
        if not repo:
            raise JournalError("INVALID_INPUT", "请提供 GitHub 仓库名或 URL")
        return finder.prepare_manuscript(github_repo=repo, mode=str(params.get("mode", "auto")))
    raise JournalError("INVALID_INPUT", "source_type 必须为 local 或 github")


def _recommend(finder: JournalFinder, params: Dict[str, Any]):
    project_path = str(params.get("project_path", "") or "").strip()
    github_repo = str(params.get("github_repo", "") or "").strip()
    if project_path or github_repo:
        return finder.recommend(mode=str(params.get("mode", "auto")),
                                profile=params.get("profile"), assessments=params.get("assessments"),
                                candidate_records=params.get("candidate_records"),
                                preferences=params.get("preferences"),
                                project_path=project_path, github_repo=github_repo)
    return finder.recommend(text=_text(params), mode=str(params.get("mode", "auto")),
                            profile=params.get("profile"), assessments=params.get("assessments"),
                            candidate_records=params.get("candidate_records"),
                            preferences=params.get("preferences"))


def _render(finder: JournalFinder, params: Dict[str, Any]):
    from paperflow.engine.journals.html_reporter import render_html_report
    result = params.get("result")
    if not isinstance(result, dict):
        raise JournalError("INVALID_INPUT", "需要一份推荐结果对象")
    return response({"html": render_html_report(result)})


def _search_reviews(finder: JournalFinder, params: Dict[str, Any]):
    from .review_searcher import search_journal_reviews
    query = str(params.get("query", "") or params.get("title", "")).strip()[:1000]
    if not query:
        raise JournalError("INVALID_INPUT", "请提供期刊刊名或 journal_id")
    return search_journal_reviews(finder, query)


# Literature has a narrower input surface than the existing project reader.
# In particular, no URL, identifier URL or local path is accepted for acquisition.
PAPER_ACTION_PARAMS = {
    "papers_search": {"query", "limit", "sources", "year_from", "year_to", "sort_by"},
    "papers_details": {"paper_id"},
    "papers_download": {"paper_id"},
    "papers_read": {"paper_id", "page_number", "page_count", "offset", "max_chars"},
    "papers_notes": {"paper_id", "reading"},
    "papers_list": {"limit", "offset"},
    "writing_prepare": {"text"},
    "writing_create": {"profile", "queries", "paper_ids", "source_text", "input_id"},
    "writing_search": {"project_id", "expected_revision", "queries", "per_query_limit"},
    "writing_assess": {"project_id", "assessments", "expected_revision", "selected_paper_ids"},
    "writing_get": {"project_id", "revision", "evidence_offset", "evidence_limit"},
    "writing_list": {"limit", "offset"},
    "writing_materials": {"project_id", "evidence_offset", "evidence_limit"},
    "writing_draft": {"project_id", "draft", "expected_revision", "change_note"},
    "loop_get": {"project_id"},
    "loop_control": {"project_id", "action", "expected_loop_revision", "expected_project_revision", "budget", "request"},
    "loop_prepare_review": {"project_id", "evidence_offset", "evidence_limit"},
    "loop_submit_review": {"project_id", "review", "context_fingerprint", "expected_project_revision", "expected_loop_revision", "origin"},
    "loop_step": {"project_id", "action_id", "expected_project_revision", "expected_loop_revision"},
    "loop_feedback": {"project_id", "action_id", "feedback", "expected_project_revision", "expected_loop_revision", "origin"},
}


def literature_service(finder: JournalFinder, literature=None):
    """Construct an inert service using the same paper home as other surfaces."""
    if literature is not None:
        return literature
    from paperflow.engine.literature.service import LiteratureService
    home = os.environ.get("PAPERFLOW_PAPER_HOME")
    directory = Path(home).expanduser().resolve() if home else finder.store.directory / "papers"
    return LiteratureService(data_dir=str(directory))


def _only_params(params: Dict[str, Any], allowed: set[str]) -> None:
    _no_paths(params)
    extra = params.keys() - allowed
    if extra:
        raise JournalError("INVALID_INPUT", "不支持的参数：" + ", ".join(sorted(map(str, extra))))


def _integer(params: Dict[str, Any], key: str, default: int, minimum: int,
             maximum: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise JournalError("INVALID_INPUT", f"{key} 必须是整数")
    try:
        result = int(value)
    except ValueError:
        raise JournalError("INVALID_INPUT", f"{key} 必须是整数") from None
    if not minimum <= result <= maximum:
        raise JournalError("INVALID_INPUT", f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return result


def paper_read_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Shared validation for the plain read action and opt-in model endpoint."""
    if not isinstance(params, dict):
        raise JournalError("INVALID_INPUT", "参数必须是对象")
    _only_params(params, PAPER_ACTION_PARAMS["papers_read"])
    return {
        "paper_id": validate_paper_id(params.get("paper_id", "")),
        "page_number": _integer(params, "page_number", 1, 1, 1000),
        "page_count": _integer(params, "page_count", 3, 1, 10),
        "offset": _integer(params, "offset", 0, 0, 4 * 1024 * 1024),
        "max_chars": _integer(params, "max_chars", 20000, 1000, 20000),
    }


def _papers_search(service, params: Dict[str, Any]):
    query = params.get("query", "")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise JournalError("INVALID_INPUT", "请提供 1 到 1000 字符的检索关键词")
    sources = params.get("sources")
    if sources is not None and (not isinstance(sources, list) or not 1 <= len(sources) <= 10 or
                               any(not isinstance(s, str) or not s or len(s) > 64 or
                                   not s.replace("_", "").replace("-", "").isalnum() for s in sources)):
        raise JournalError("INVALID_INPUT", "sources 必须是来源编号列表，不能是 URL 或路径")
    year_from = _integer(params, "year_from", 1500, 1500, 2200) if params.get("year_from") is not None else None
    year_to = _integer(params, "year_to", 2200, 1500, 2200) if params.get("year_to") is not None else None
    if year_from is not None and year_to is not None and year_from > year_to:
        raise JournalError("INVALID_INPUT", "起始年份不能大于结束年份")
    sort_by = params.get("sort_by", "relevance")
    if sort_by not in ("relevance", "date"):
        raise JournalError("INVALID_INPUT", "sort_by 必须为 relevance 或 date")
    return service.search(query=query.strip(), limit=_integer(params, "limit", 10, 1, 50),
                          sources=sources, year_from=year_from, year_to=year_to, sort_by=sort_by)


def _papers_details(service, params: Dict[str, Any]):
    return service.get(paper_id=validate_paper_id(params.get("paper_id", "")))


def _papers_download(service, params: Dict[str, Any]):
    return service.download(validate_paper_id(params.get("paper_id", "")))


def _papers_read(service, params: Dict[str, Any]):
    return service.read(**paper_read_params(params))


def _papers_notes(service, params: Dict[str, Any]):
    paper_id = validate_paper_id(params.get("paper_id", ""))
    reading = params.get("reading")
    if not isinstance(reading, dict):
        raise JournalError("INVALID_INPUT", "reading 必须是解读卡对象")
    try:
        card = ReadingCard.model_validate(reading).model_dump(mode="json")
    except ValueError:
        raise JournalError("INVALID_INPUT", "解读卡不符合 reading_schema；不能带路径或未知字段") from None
    if card["paper_id"] != paper_id:
        raise JournalError("INVALID_INPUT", "解读卡 paper_id 与所选文献不一致")
    return service.save_reading(paper_id, card, origin="calling_agent", strict=False)


def _papers_list(service, params: Dict[str, Any]):
    return service.list(limit=_integer(params, "limit", 20, 1, 100),
                        offset=_integer(params, "offset", 0, 0, 1000000))


def writing_service(finder: JournalFinder, writing=None, literature=None):
    """Lazy core service; shared actions never import the GUI model helper."""
    if writing is not None:
        return writing
    from paperflow.engine.literature.writing_service import PaperWritingService
    home = os.environ.get("PAPERFLOW_PAPER_HOME")
    directory = Path(home).expanduser().resolve() if home else finder.store.directory / "papers"
    return PaperWritingService(data_dir=str(directory), literature=literature_service(finder, literature))


def _writing_no_paths(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key in {"path", "paths", "url", "file_path", "output_path", "data_dir",
                                                 "project_dir", "input_paths", "html_path", "pdf_path",
                                                 "previous_file_path", "github_repo"} or key.endswith("_path"):
                raise JournalError("INVALID_INPUT", "写作界面不接受路径或任意下载地址")
            _writing_no_paths(item)
    elif isinstance(value, list):
        for item in value:
            _writing_no_paths(item)


def writing_params(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Strict, bounded validation used by HTTP, MCP view and local model routes."""
    if not isinstance(params, dict) or action not in PAPER_ACTION_PARAMS or not action.startswith("writing_"):
        raise JournalError("INVALID_INPUT", "无效的写作操作参数")
    _only_params(params, PAPER_ACTION_PARAMS[action])
    _writing_no_paths(params)
    try:
        size = len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, OverflowError):
        raise JournalError("INVALID_INPUT", "写作参数必须为有效 JSON") from None
    if size > 2 * 1024 * 1024:
        raise JournalError("INPUT_TOO_LARGE", "单次写作请求超过 2 MiB")
    result = dict(params)
    if "project_id" in PAPER_ACTION_PARAMS[action]:
        ident = params.get("project_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            raise JournalError("INVALID_INPUT", "需要有效的 project_id，不能是文件路径")
    bounds = {"expected_revision": (1, 2147483647), "revision": (1, 2147483647),
              "limit": (1, 100), "offset": (0, 1000000), "per_query_limit": (1, 10),
              "evidence_offset": (0, 1000000), "evidence_limit": (1, 40)}
    if "expected_revision" in PAPER_ACTION_PARAMS[action] and "expected_revision" not in params:
        raise JournalError("INVALID_INPUT", "expected_revision 是必需的；请先加载当前项目")
    for key, (low, high) in bounds.items():
        if key in params and (params[key] is not None or key != "revision"):
            if type(params[key]) is not int or not low <= params[key] <= high:
                raise JournalError("INVALID_INPUT", f"{key} 必须是 {low} 到 {high} 的整数")
    for key in ("profile", "draft"):
        if key in PAPER_ACTION_PARAMS[action] and not isinstance(params.get(key), dict):
            raise JournalError("INVALID_INPUT", f"{key} 必须是对象")
    for key, maximum in (("paper_ids", 30), ("selected_paper_ids", 30)):
        if key in params:
            ids = params[key]
            if not isinstance(ids, list) or len(ids) > maximum or len(set(map(str, ids))) != len(ids):
                raise JournalError("INVALID_INPUT", f"{key} 必须是最多 {maximum} 个不重复文献 ID")
            result[key] = [validate_paper_id(item) for item in ids]
    if "queries" in params and params["queries"] is not None:
        queries = params["queries"]
        if not isinstance(queries, list) or len(queries) > 6 or (action == "writing_search" and not queries):
            raise JournalError("INVALID_INPUT", "每次最多六组检索词；检索至少需要一组")
        if any(not isinstance(query, dict) for query in queries):
            raise JournalError("INVALID_INPUT", "每组检索词必须是 query_schema 对象")
    if action == "writing_assess":
        assessments = params.get("assessments")
        if not isinstance(assessments, list) or len(assessments) > 60 or any(not isinstance(row, dict) for row in assessments):
            raise JournalError("INVALID_INPUT", "assessments 必须为最多六十个判断对象")
    for key, maximum in (("source_text", MAX_TEXT_CHARS), ("input_id", 128), ("change_note", 1000)):
        if key in params and (not isinstance(params[key], str) or len(params[key]) > maximum):
            raise JournalError("INVALID_INPUT", f"{key} 必须为最多 {maximum} 字符的文本")
    if action == "writing_prepare":
        result["text"] = _text(params)
    return result


def _writing_prepare(service, params):
    return service.prepare(**params)


def _writing_create(service, params):
    return service.create(**params)


def _writing_search(service, params):
    return service.search(**params)


def _writing_assess(service, params):
    return service.assess(**params, origin="calling_agent")


def _writing_get(service, params):
    return service.get(**params)


def _writing_list(service, params):
    return service.list(**params)


def _writing_materials(service, params):
    return service.prepare_writing(**params)


def _writing_draft(service, params):
    return service.save_draft(**params)


def reading_loop_service(finder: JournalFinder, loop=None, writing=None, literature=None):
    """Lazy core loop service; shared actions never import GUI model helpers."""
    if loop is not None:
        return loop
    try:
        from paperflow.engine.literature.reading_loop_service import ReadingLoopService
    except ImportError:
        raise JournalError("SERVICE_UNAVAILABLE", "ReadingLoopService 核心服务正在初始化或未就绪")
    w_svc = writing_service(finder, writing, literature)
    home = os.environ.get("PAPERFLOW_PAPER_HOME")
    directory = Path(home).expanduser().resolve() if home else finder.store.directory / "papers"
    return ReadingLoopService(data_dir=str(directory), writing=w_svc)


def loop_params(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Strict validation for reading loop actions."""
    if not isinstance(params, dict) or action not in PAPER_ACTION_PARAMS or not action.startswith("loop_"):
        raise JournalError("INVALID_INPUT", "无效的循环操作参数")
    _only_params(params, PAPER_ACTION_PARAMS[action])
    _writing_no_paths(params)
    try:
        size = len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, OverflowError):
        raise JournalError("INVALID_INPUT", "循环参数必须为有效 JSON") from None
    if size > 2 * 1024 * 1024:
        raise JournalError("INPUT_TOO_LARGE", "单次循环请求超过 2 MiB")
    result = dict(params)
    ident = params.get("project_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
        raise JournalError("INVALID_INPUT", "需要有效的 project_id，不能是文件路径")

    if action == "loop_control":
        act = params.get("action")
        if act not in ("start", "pause", "resume", "stop", "update_budget"):
            raise JournalError("INVALID_INPUT", "不支持的循环控制动作")
        if "expected_loop_revision" not in params or type(params["expected_loop_revision"]) is not int or \
                params["expected_loop_revision"] < 0:
            raise JournalError("INVALID_INPUT", "expected_loop_revision 必须是非负整数")
        if "expected_project_revision" not in params or type(params["expected_project_revision"]) is not int or \
                params["expected_project_revision"] < 1:
            raise JournalError("INVALID_INPUT", "expected_project_revision 必须是正整数")
        if "budget" in params and params["budget"] is not None:
            if not isinstance(params["budget"], dict):
                raise JournalError("INVALID_INPUT", "budget 必须是对象")
            for k, v in params["budget"].items():
                if type(v) is not int:
                    raise JournalError("INVALID_INPUT", f"预算字段 {k} 必须是整数，不能是布尔值或字符串")
        if "request" in params and params["request"] is not None:
            if not isinstance(params["request"], str) or len(params["request"]) > 4000:
                raise JournalError("INVALID_INPUT", "request 必须是最多 4000 字符的文本")

    if action == "loop_prepare_review":
        result["evidence_offset"] = _integer(params, "evidence_offset", 0, 0, 1000000)
        result["evidence_limit"] = _integer(params, "evidence_limit", 30, 1, 100)

    if action == "loop_submit_review":
        if not isinstance(params.get("review"), dict):
            raise JournalError("INVALID_INPUT", "review 必须是评审对象")
        fp = params.get("context_fingerprint")
        if not isinstance(fp, str) or not fp.strip() or len(fp) > 128:
            raise JournalError("INVALID_INPUT", "需要有效的 context_fingerprint")
        if "expected_project_revision" not in params or type(params["expected_project_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_project_revision 必须是整数")
        if "expected_loop_revision" not in params or type(params["expected_loop_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_loop_revision 必须是整数")
        if params.get("origin") not in ("calling_agent", "gui_model", "nativeAgent", None):
            raise JournalError("INVALID_INPUT", "origin 必须是 calling_agent、gui_model 或 nativeAgent")

    if action == "loop_step":
        act_id = params.get("action_id")
        if not isinstance(act_id, str) or not act_id.strip() or len(act_id) > 64:
            raise JournalError("INVALID_INPUT", "需要有效的 action_id")
        if "expected_project_revision" not in params or type(params["expected_project_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_project_revision 必须是整数")
        if "expected_loop_revision" not in params or type(params["expected_loop_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_loop_revision 必须是整数")

    if action == "loop_feedback":
        act_id = params.get("action_id")
        if not isinstance(act_id, str) or not act_id.strip() or len(act_id) > 64:
            raise JournalError("INVALID_INPUT", "需要有效的 action_id")
        if not isinstance(params.get("feedback"), dict):
            raise JournalError("INVALID_INPUT", "feedback 必须是对象")
        if "expected_project_revision" not in params or type(params["expected_project_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_project_revision 必须是整数")
        if "expected_loop_revision" not in params or type(params["expected_loop_revision"]) is not int:
            raise JournalError("INVALID_INPUT", "expected_loop_revision 必须是整数")
        if params.get("origin") not in ("calling_agent", "gui_model", "nativeAgent", None):
            raise JournalError("INVALID_INPUT", "origin 必须是 calling_agent、gui_model 或 nativeAgent")

    return result


def _loop_get(service, params):
    return service.get(params["project_id"])


def _loop_control(service, params):
    return service.control(**params)


def _loop_prepare_review(service, params):
    return service.prepare_review(**params)


def _loop_submit_review(service, params):
    return service.submit_review(**params)


def _loop_step(service, params):
    return service.step(**params)


def _loop_feedback(service, params):
    return service.apply_feedback(**params)


LOOP_ACTIONS = {
    "loop_get": _loop_get,
    "loop_control": _loop_control,
    "loop_prepare_review": _loop_prepare_review,
    "loop_submit_review": _loop_submit_review,
    "loop_step": _loop_step,
    "loop_feedback": _loop_feedback,
}


PAPER_ACTIONS = {
    "writing_prepare": _writing_prepare,
    "writing_create": _writing_create,
    "writing_search": _writing_search,
    "writing_assess": _writing_assess,
    "writing_get": _writing_get,
    "writing_list": _writing_list,
    "writing_materials": _writing_materials,
    "writing_draft": _writing_draft,
    **LOOP_ACTIONS,
    "papers_search": _papers_search,
    "papers_details": _papers_details,
    "papers_download": _papers_download,
    "papers_read": _papers_read,
    "papers_notes": _papers_notes,
    "papers_list": _papers_list,
}


ACTIONS: Dict[str, Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = {
    "overview": _overview,
    "build": _build,
    "search": _search,
    "details": _details,
    "check": _check,
    "compare": _compare,
    "prepare": _prepare,
    "read_project": _read_project,
    "recommend": _recommend,
    "render_report": _render,
    "search_reviews": _search_reviews,
    **PAPER_ACTIONS,
}


def dispatch(finder: JournalFinder, action: str, params: Dict[str, Any] | None = None,
             literature=None, writing=None, loop=None) -> Dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise JournalError("INVALID_INPUT", "参数必须是对象")
    handler = ACTIONS.get(action)
    if handler is None:
        raise JournalError("INVALID_INPUT", f"未知操作：{action}")
    _no_paths(params)
    if action in PAPER_ACTIONS:
        if action.startswith("writing_"):
            validated = writing_params(action, params)
            return handler(writing_service(finder, writing, literature), validated)
        if action.startswith("loop_"):
            validated = loop_params(action, params)
            return handler(reading_loop_service(finder, loop, writing, literature), validated)
        _only_params(params, PAPER_ACTION_PARAMS[action])
        return handler(literature_service(finder, literature), params)
    return handler(finder, params)
