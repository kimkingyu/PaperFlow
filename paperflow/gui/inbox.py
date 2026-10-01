"""Recommendation results handed from any harness Agent to the local GUI.

Files live next to the journal database. Writes are atomic, the inbox is
bounded, and nothing credential-like is ever written.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from paperflow.engine.journals.models import JournalError

MAX_ITEMS = 20
# Sortable id: UTC second + 9-digit sub-second sequence + random suffix.
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}[0-9]{9}Z-[0-9a-f]{8}$")
_LOCK = threading.Lock()
_last_ns = 0


def _next_ns() -> int:
    """Strictly increasing within this process, so ids sort in write order."""
    global _last_ns
    with _LOCK:
        _last_ns = max(time.time_ns(), _last_ns + 1)
        return _last_ns


def inbox_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "gui_inbox"


def _summary(result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data") or {}
    profile = data.get("profile") or {}
    return {
        "title": (profile.get("title") or profile.get("summary") or "未命名推荐")[:120],
        "stage": data.get("stage"),
        "display_summary": data.get("display_summary") or {},
        "assessment_origin": data.get("assessment_origin", "calling_agent"),
    }


def publish(data_dir: Path, result: Dict[str, Any]) -> str:
    """Atomically store one recommendation result; returns its id."""
    folder = inbox_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    ns = _next_ns()
    now = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    item_id = f"{now.strftime('%Y%m%dT%H%M%S')}{ns % 1_000_000_000:09d}Z-{uuid.uuid4().hex[:8]}"
    payload = {"id": item_id, "created_at": now.isoformat(), **_summary(result), "result": result}
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(folder))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(tmp, folder / f"{item_id}.json")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _prune(folder)
    return item_id


def _prune(folder: Path) -> None:
    items = sorted(p for p in folder.glob("*.json") if _ID_RE.match(p.stem))
    for stale in items[:-MAX_ITEMS]:
        try:
            stale.unlink()
        except OSError:
            pass


def list_items(data_dir: Path) -> List[Dict[str, Any]]:
    folder = inbox_dir(data_dir)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        if not _ID_RE.match(path.stem):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({k: payload.get(k) for k in ("id", "created_at", "title", "stage",
                                                  "display_summary", "assessment_origin")})
    return rows[:MAX_ITEMS]


def get_item(data_dir: Path, item_id: str) -> Optional[Dict[str, Any]]:
    if not _ID_RE.match(item_id or ""):
        raise JournalError("INVALID_INPUT", "收件箱编号格式无效")
    path = inbox_dir(data_dir) / f"{item_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
