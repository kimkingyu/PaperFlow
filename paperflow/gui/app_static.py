"""Packaged standalone UI resources, separate from the single-file MCP App."""
from __future__ import annotations

import re
import stat
from importlib import resources
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "frame-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store", "X-Frame-Options": "DENY",
}
_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
          ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff"}
_MISSING = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>PaperFlow 工作台</title>
<body><h1>PaperFlow 工作台尚未构建</h1><p>开发环境请在 frontend 目录运行 npm ci 和 npm run build，
然后刷新页面。发行版应自带这些资源，最终用户不需要 Node。</p><p>原有 MCP 工具不受影响。</p></body></html>"""


def _linked(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) &
                                         getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _read(name: str) -> bytes | None:
    parts = name.split("/")
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p.startswith(".") for p in parts):
        return None
    root = Path(str(resources.files("paperflow.gui").joinpath("static", "app")))
    if _linked(root) or _linked(root.parent):
        return None
    current = root
    for part in parts:
        current = current / part
        if _linked(current):
            return None
    try:
        current.resolve().relative_to(root.resolve())
        if not current.is_file() or current.stat().st_size > 16 * 1024 * 1024:
            return None
        return current.read_bytes()
    except (OSError, ValueError):
        return None


async def app_page(request, guard):
    blocked = guard(request, need_token=False)
    if blocked is not None:
        return blocked
    content = await run_in_threadpool(_read, "index.html")
    return HTMLResponse(content if content is not None else _MISSING,
                        status_code=200 if content is not None else 503,
                        headers={**HEADERS, "Content-Security-Policy": APP_CSP})


def build_static_routes(guard) -> list[Route]:
    async def index(request):
        return await app_page(request, guard)

    async def asset(request):
        blocked = guard(request, need_token=False)
        if blocked is not None:
            return blocked
        name = request.path_params["name"]
        mime = _TYPES.get(Path(name).suffix)
        content = await run_in_threadpool(_read, name) if mime else None
        if content is None:
            return Response("Not found", status_code=404, headers=HEADERS)
        return Response(content, media_type=mime, headers=HEADERS)

    return [Route("/app", index), Route("/app/", index), Route("/app/{name:path}", asset)]
