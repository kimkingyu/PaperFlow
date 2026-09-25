"""Source catalog, packaged factual snapshots and traceable upstream audit metadata."""
from __future__ import annotations

import json
import os
from importlib import resources

from .models import JournalError


def get_sources():
    path = resources.files("paperflow").joinpath("resources", "journals", "source_catalog.json")
    values = json.loads(path.read_text(encoding="utf-8"))["sources"]
    permitted = {x.strip() for x in os.environ.get("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "").split(",") if x.strip()}
    audit_path = resources.files("paperflow").joinpath("resources", "journals", "source_audit.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    audited = {s.get("repo"): s for s in audit.get("seeds", [])}
    for source in values:
        if source.get("repo"):
            source["source_url"] = "https://github.com/" + source["repo"]
            if source["repo"] in audited:
                source["audit"] = audited[source["repo"]]
        source["download_permitted_by_user"] = source["id"] in permitted
        source["configured"] = bool(os.environ.get(source.get("key_env", ""))) if source.get("key_env") else False
        source["online_tracking_available"] = False
    return values


def get_source(source_id):
    if source_id == "local":
        return {"id": "local", "name": "用户提供的授权记录", "mode": "import_only", "source_url": "",
                "formats": ["json", "csv"], "capabilities": ["records", "school_policy"],
                "limitations": ["所提供证据尚未由本程序在线核验"]}
    for source in get_sources():
        if source["id"] == source_id:
            return source
    raise JournalError("INVALID_INPUT", "未知数据源，请先调用 list_journal_sources")
