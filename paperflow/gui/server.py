"""Local journal studio server: ``python -m paperflow gui``.

Binds to 127.0.0.1 only, requires a per-launch token on every API call,
checks the Host header against DNS rebinding, sends a strict CSP, and never
accepts file paths from the page. Works next to any harness, including those
that cannot render MCP Apps.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import json
import re
import secrets as pysecrets
import socket
import threading
import webbrowser
from importlib import resources
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError

from . import inbox, model_scoring, paper_reading_loop
from .actions import (PAPER_ACTIONS, PAPER_ACTION_PARAMS, _no_paths, _only_params,
                      dispatch, literature_service, paper_read_params, writing_params, writing_service,
                      reading_loop_service, loop_params)
from .secrets import SecretStore, redact

MAX_BODY_BYTES = 2 * 1024 * 1024
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; frame-src 'self'; child-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def load_page() -> str:
    return resources.files("paperflow.gui").joinpath("static", "journal_studio.html").read_text(encoding="utf-8")


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "error_code": code, "message": message, "data": None},
                        status_code=status, headers=SECURITY_HEADERS)


def create_app(token: str, port: int, finder: Optional[JournalFinder] = None,
               store: Optional[SecretStore] = None, transport=None, literature=None, writing=None,
               loop=None, standalone: bool = False, application=None, agent=None, agent_transport=None) -> Starlette:
    finder = finder or getattr(application, "finder", None) or JournalFinder()
    literature = literature if literature is not None else getattr(application, "literature", None)
    writing = writing if writing is not None else getattr(application, "writing", None)
    loop = loop if loop is not None else getattr(application, "loop", None)
    store = store or SecretStore()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    service_lock = threading.RLock()

    def papers_service():
        nonlocal literature
        with service_lock:
            if literature is None:
                literature = literature_service(finder)
            return literature

    def writing_project_service():
        nonlocal writing
        with service_lock:
            if writing is None:
                writing = writing_service(finder, literature=papers_service())
            return writing

    def loop_project_service():
        nonlocal loop
        with service_lock:
            if loop is None:
                loop = reading_loop_service(finder, writing=writing_project_service(), literature=papers_service())
            return loop



    def app_services():
        nonlocal application, literature, writing, loop
        with service_lock:
            if application is None:
                from paperflow.application.services import ApplicationServices
                application = ApplicationServices(finder=finder, literature=literature,
                                                  writing=writing, loop=loop)
                literature, writing, loop = application.literature, application.writing, application.loop
            return application
    def native_agent():
        nonlocal agent
        with service_lock:
            if agent is None:
                from paperflow.agent.runtime import AgentRuntime
                services = app_services()
                agent = AgentRuntime(services.directory, services.describe_tools, services.execute,
                                     lambda: store.config, transport=agent_transport)
            return agent
    def perform_action(action_name: str, params: Dict[str, Any]):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise JournalError("INVALID_INPUT", "参数必须是对象")
        _no_paths(params)
        # Compatibility actions must not bypass the web's managed-file boundary.
        # MCP/CLI still call their own original repository/file interfaces.
        if ({"path", "project_path", "project_dir", "directory", "input_path", "github_repo"}.intersection(params)
                or str(params.get("source_type", "")).strip().lower() == "local"):
            raise JournalError("INVALID_INPUT", "网页只接受上传内容或受管文件ID；本地路径/仓库读取请使用MCP或CLI")
        if action_name == "search_reviews":
            from .review_searcher import search_journal_reviews
            return search_journal_reviews(
                finder, str(params.get("query", "") or params.get("title", "")),
                store=store, transport=transport)
        if action_name in PAPER_ACTIONS:
            _only_params(params, PAPER_ACTION_PARAMS[action_name])
            if action_name.startswith("writing_"):
                validated = writing_params(action_name, params)
                return dispatch(finder, action_name, validated, writing=writing_project_service())
            if action_name.startswith("loop_"):
                validated = loop_params(action_name, params)
                return dispatch(finder, action_name, validated, loop=loop_project_service())
            return dispatch(finder, action_name, params, literature=papers_service())
        return dispatch(finder, action_name, params)

    def perform_paper_read(params: Dict[str, Any]):
        # Import only on the local HTTP model route, never from shared dispatch.
        from .paper_reading import read_with_model
        return read_with_model(papers_service(), store.config, transport=transport,
                               consent=True, **params)

    def guard(request: Request, need_token: bool = True) -> Optional[Response]:
        if request.headers.get("host", "") not in allowed_hosts:
            return _error("FORBIDDEN_HOST", "Host 不被允许", 403)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + host for host in allowed_hosts}:
            return _error("FORBIDDEN_ORIGIN", "请求来源不被允许", 403)
        if need_token and not pysecrets.compare_digest(request.headers.get("x-paperflow-token", ""), token):
            return _error("FORBIDDEN", "缺少或错误的访问令牌", 403)
        return None

    async def body(request: Request) -> Dict[str, Any]:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise JournalError("INVALID_INPUT", "请求必须是 application/json")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise JournalError("INPUT_TOO_LARGE", "请求体超过大小限制")
        payload = await run_in_threadpool(json.loads, raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise JournalError("INVALID_INPUT", "请求体必须是对象")
        return payload

    def ok(payload: Any) -> JSONResponse:
        return JSONResponse(payload, headers=SECURITY_HEADERS)

    def failure(exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code):
            return _error(code, redact(str(exc), store.config.api_key())[:1000], 400)
        if isinstance(exc, (ValueError, TypeError)):
            return _error("INVALID_INPUT", redact(str(exc), store.config.api_key())[:500], 400)
        return _error("INTERNAL_ERROR", "操作失败，请检查输入或本地数据状态", 500)

    async def page(request: Request) -> Response:
        if standalone and request.url.path != "/legacy":
            from .app_static import app_page
            return await app_page(request, guard)
        blocked = guard(request, need_token=False)
        if blocked:
            return blocked
        return HTMLResponse(await run_in_threadpool(load_page),
                            headers={**SECURITY_HEADERS, "Content-Security-Policy": PAGE_CSP})

    async def action(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            payload = await body(request)
            action_name = str(payload.get("action", ""))
            if action_name.startswith("writing_"):
                _only_params(payload, {"action", "params"})
            result = await run_in_threadpool(perform_action, action_name, payload.get("params"))
            return ok(result)
        except Exception as exc:
            return failure(exc)

    async def inbox_list(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        return ok({"items": await run_in_threadpool(inbox.list_items, finder.store.directory)})

    async def inbox_item(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            item = await run_in_threadpool(inbox.get_item, finder.store.directory,
                                           request.path_params["item_id"])
        except Exception as exc:
            return failure(exc)
        return ok(item) if item else _error("NOT_FOUND", "没有这条结果", 404)

    async def model_status(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        return ok(await run_in_threadpool(store.config.public))

    async def model_config(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"provider", "base_url", "model", "api_key", "remember"})
            if not all(isinstance(p.get(key, ""), str) for key in ("provider", "base_url", "model", "api_key")) or type(p.get("remember", False)) is not bool:
                raise JournalError("INVALID_INPUT", "模型配置文本和 remember 布尔值类型不正确")
            provider, base_url, model = p.get("provider", ""), p.get("base_url", "").strip(), p.get("model", "").strip()
            model_scoring.validate_endpoint(provider, base_url, model)
            result = await run_in_threadpool(store.configure, provider, base_url, model,
                                             p.get("api_key", ""), p.get("remember", False))
            return ok(result)
        except RuntimeError as exc:
            return _error("KEYRING_UNAVAILABLE", str(exc), 400)
        except Exception as exc:
            return failure(exc)

    async def model_consent(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"consent"})
            if type(p.get("consent")) is not bool:
                raise JournalError("INVALID_INPUT", "consent 必须为明确的布尔值")
            return ok(await run_in_threadpool(store.consent, p["consent"]))
        except Exception as exc:
            return failure(exc)

    async def model_test(request: Request) -> Response:
        blocked = guard(request)
        if blocked is not None:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"consent"})
            if p.get("consent") is not True:
                raise JournalError("CONSENT_REQUIRED", "连接测试会向配置端点发送一个简短请求，请明确授权")
            config = store.config
            model_scoring.validate_endpoint(config.provider, config.base_url, config.model)
            if not config.configured:
                raise JournalError("MODEL_NOT_CONFIGURED", "请先配置模型与凭证")
            answer = await run_in_threadpool(model_scoring._chat, config,
                                            "This is a connection test. Reply with OK only.", "OK",
                                            transport or model_scoring._http_transport)
            if store.config is not config:
                raise JournalError("CONTEXT_CHANGED", "测试期间模型配置已变更，请重新测试")
            return ok({"status": "success", "data": {"connected": True, "model_calls": 1,
                       "provider": config.provider, "model": config.model,
                       "message": redact(answer[:300], config.api_key())}})
        except Exception as exc:
            return failure(exc)

    async def model_score(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            text = p.get("text") or ""
            if not isinstance(text, str) or not text.strip() or len(text) > 60000:
                raise JournalError("INVALID_INPUT", "请粘贴 1 到 60000 字符的研究想法或稿件")
            result = await run_in_threadpool(model_scoring.score, finder, store.config, text,
                                             str(p.get("mode", "auto")), p.get("preferences"),
                                             transport=transport)
            return ok(result)
        except Exception as exc:
            return failure(exc)

    async def model_read_paper(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, PAPER_ACTION_PARAMS["papers_read"] | {"consent"})
            if p.get("consent") is not True:
                raise JournalError("CONSENT_REQUIRED", "请明确同意仅将本次选中段落发送给已配置模型")
            params = paper_read_params({key: value for key, value in p.items() if key != "consent"})
            return ok(await run_in_threadpool(perform_paper_read, params))
        except Exception as exc:
            return failure(exc)

    def perform_writing_model(phase: str, params: Dict[str, Any]):
        # These helpers must remain on the GUI-only HTTP boundary.
        from .paper_writing import plan_with_model, assess_with_model, draft_with_model
        helper = {"plan": plan_with_model, "assess": assess_with_model, "draft": draft_with_model}[phase]
        return helper(writing_project_service(), store.config, transport=transport, **params)

    async def model_writing(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            phase = request.path_params["phase"]
            allowed = {
                "plan": {"text", "own_materials", "language", "consent"},
                "assess": {"project_id", "expected_revision", "candidate_ids", "consent"},
                "draft": {"project_id", "expected_revision", "citation_ids", "evidence_offset", "evidence_limit", "consent"},
            }
            if phase not in allowed:
                raise JournalError("INVALID_INPUT", "未知写作模型阶段")
            params = await body(request)
            _only_params(params, allowed[phase])
            if params.get("consent") is not True:
                raise JournalError("CONSENT_REQUIRED", "请明确同意本阶段明确选中的研究/摘要/证据发送")
            if phase != "plan":
                if "expected_revision" not in params:
                    raise JournalError("INVALID_INPUT", "expected_revision 是必需的")
                writing_params("writing_get", {"project_id": params.get("project_id"),
                                               "revision": params["expected_revision"]})
            return ok(await run_in_threadpool(perform_writing_model, phase, params))
        except Exception as exc:
            return failure(exc)

    async def writing_download(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            params = await body(request)
            _only_params(params, {"project_id", "revision"})
            if type(params.get("revision")) is not int:
                raise JournalError("INVALID_INPUT", "下载必须明确指定已保存的 revision")
            validated = writing_params("writing_get", params)
            document = await run_in_threadpool(lambda: writing_project_service().render_docx(**validated))
            if not isinstance(document, bytes) or not document:
                raise JournalError("WRITING_EXPORT_ERROR", "写作服务未返回有效 DOCX bytes")
            return Response(document,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={**SECURITY_HEADERS, "Content-Disposition": 'attachment; filename="paperflow-writing.docx"'})
        except Exception as exc:
            return failure(exc)

    async def loop_status(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"project_id"})
            ident = str(p.get("project_id", ""))
            if not ident or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                raise JournalError("INVALID_INPUT", "需要有效的 project_id")
            res = await run_in_threadpool(
                paper_reading_loop.get_loop_status, loop_project_service(), ident, store=store
            )
            return ok(res)
        except Exception as exc:
            return failure(exc)

    async def loop_control(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"project_id", "action", "expected_loop_revision", "expected_project_revision", "budget", "request", "consent"})
            ident = str(p.get("project_id", ""))
            act = str(p.get("action", ""))
            if not ident or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                raise JournalError("INVALID_INPUT", "需要有效的 project_id")
            l_rev = p.get("expected_loop_revision")
            p_rev = p.get("expected_project_revision")
            if type(l_rev) is not int or l_rev < 0 or type(p_rev) is not int or p_rev < 1:
                raise JournalError("INVALID_INPUT", "expected_loop_revision / expected_project_revision 必须为整数")

            if act == "pause":
                res = await run_in_threadpool(
                    paper_reading_loop.pause_loop, loop_project_service(), ident, l_rev, p_rev
                )
            elif act == "stop":
                res = await run_in_threadpool(
                    paper_reading_loop.stop_loop, loop_project_service(), ident, l_rev, p_rev
                )
            elif act == "update_budget":
                res = await run_in_threadpool(
                    paper_reading_loop.update_budget, loop_project_service(), ident, l_rev, p_rev,
                    p.get("budget") or {}, p.get("request", "")
                )
            elif act in ("start", "resume"):
                if p.get("consent") is not True:
                    raise JournalError("CONSENT_REQUIRED", "启动或恢复自动补读循环必须重新明确同意材料发送")
                res = await run_in_threadpool(
                    paper_reading_loop.start_auto_loop, loop_project_service(), store, ident,
                    l_rev, p_rev, p.get("budget"), p.get("request", ""), consent=True, transport=transport,
                    execution_services=app_services()
                )
            else:
                raise JournalError("INVALID_INPUT", f"不支持的控制动作：{act}")
            return ok(res)
        except Exception as exc:
            return failure(exc)

    async def loop_start_auto(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"project_id", "expected_loop_revision", "expected_project_revision", "budget", "request", "consent"})
            ident = str(p.get("project_id", ""))
            if not ident or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                raise JournalError("INVALID_INPUT", "需要有效的 project_id")
            if p.get("consent") is not True:
                raise JournalError("CONSENT_REQUIRED", "启动自动补读循环必须明确授权预算与材料发送")
            l_rev = p.get("expected_loop_revision")
            p_rev = p.get("expected_project_revision")
            if type(l_rev) is not int or l_rev < 0 or type(p_rev) is not int or p_rev < 1:
                raise JournalError("INVALID_INPUT", "expected_loop_revision / expected_project_revision 必须为整数")

            res = await run_in_threadpool(
                paper_reading_loop.start_auto_loop, loop_project_service(), store, ident,
                l_rev, p_rev, p.get("budget"), p.get("request", ""), consent=True, transport=transport,
                    execution_services=app_services()
            )
            return ok(res)
        except Exception as exc:
            return failure(exc)

    async def loop_step(request: Request) -> Response:
        blocked = guard(request)
        if blocked:
            return blocked
        try:
            p = await body(request)
            _only_params(p, {"project_id", "expected_loop_revision", "expected_project_revision", "consent"})
            if p.get("consent") is not True:
                raise JournalError("CONSENT_REQUIRED", "需要显式授权本次模型调用")
            ident = str(p.get("project_id", ""))
            if not ident or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                raise JournalError("INVALID_INPUT", "需要有效的 project_id")
            l_rev = p.get("expected_loop_revision")
            p_rev = p.get("expected_project_revision")
            if type(l_rev) is not int or l_rev < 0 or type(p_rev) is not int or p_rev < 1:
                raise JournalError("INVALID_INPUT", "expected_loop_revision / expected_project_revision 必须为整数")

            res = await run_in_threadpool(
                paper_reading_loop.single_step, loop_project_service(), store, ident,
                l_rev, p_rev, consent=p.get("consent") is True, transport=transport,
                execution_services=app_services()
            )
            return ok(res)
        except Exception as exc:
            return failure(exc)

    routes = [
        Route("/", page), Route("/legacy", page), Route("/api/action", action, methods=["POST"]),
        Route("/api/inbox", inbox_list), Route("/api/inbox/{item_id}", inbox_item),
        Route("/api/model/status", model_status), Route("/api/model/config", model_config, methods=["POST"]),
        Route("/api/model/consent", model_consent, methods=["POST"]),
        Route("/api/model/test", model_test, methods=["POST"]),
        Route("/api/model/score", model_score, methods=["POST"]),
        Route("/api/model/read-paper", model_read_paper, methods=["POST"]),
        Route("/api/model/writing-{phase}", model_writing, methods=["POST"]),
        Route("/api/writing/download", writing_download, methods=["POST"]),
        Route("/api/writing/loop-status", loop_status, methods=["POST"]),
        Route("/api/writing/loop-control", loop_control, methods=["POST"]),
        Route("/api/writing/loop-start-auto", loop_start_auto, methods=["POST"]),
        Route("/api/writing/loop-step", loop_step, methods=["POST"]),
    ]

    from .agent_routes import build_agent_routes
    from .app_static import build_static_routes
    from .workbench_routes import build_workbench_routes

    class LazyApplication:
        def __getattr__(self, name):
            return getattr(app_services(), name)

    routes.extend(build_static_routes(guard))
    routes.extend(build_workbench_routes(LazyApplication(), guard, failure, ok))
    routes.extend(build_agent_routes(native_agent, app_services, guard, failure, ok))

    @asynccontextmanager
    async def lifespan(app_instance):
        try:
            yield
        finally:
            paper_reading_loop.shutdown_all()
            if agent is not None:
                await run_in_threadpool(agent.shutdown)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.paperflow_services = app_services
    app.state.paperflow_agent = native_agent
    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_gui(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="paperflow web", description="PaperFlow 独立 Agent 与科研网页工作台")
    parser.add_argument("--port", type=int, default=0, help="端口，默认随机")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--legacy", action="store_true", help="打开兼容的单文件期刊工作台")
    parser.add_argument("--data-dir", default=None, help="隔离的全部业务数据目录；省略则沿用 MCP 默认目录")
    parser.add_argument("--token-file", default=None,
                        help="把本次访问链接写入该文件（便于脚本或其他工具读取），默认只打印到终端")
    args = parser.parse_args(argv)
    import uvicorn
    port = args.port or _free_port()
    token = pysecrets.token_urlsafe(32)
    finder = JournalFinder(args.data_dir)
    application = None
    if args.data_dir is not None:
        from paperflow.application.services import ApplicationServices
        application = ApplicationServices(finder=finder, data_dir=args.data_dir)
    app = create_app(token, port, finder=finder, application=application, standalone=not args.legacy)
    url = f"http://127.0.0.1:{port}/?token={token}"
    print("PaperFlow 独立科研工作台已启动（只监听本机）：", flush=True)
    print(f"  {url}", flush=True)
    print("按 Ctrl+C 退出。令牌仅本次有效。", flush=True)
    if args.token_file:
        from pathlib import Path
        Path(args.token_file).write_text(url, encoding="utf-8")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
