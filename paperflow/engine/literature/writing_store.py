"""Writing snapshots and permanent reading pins in the existing paper library."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

_CURRENT_LOOP_OWNER: ContextVar[Optional[str]] = ContextVar("paperflow_writing_loop_owner", default=None)

from paperflow.engine.journals.models import JournalError, utc_now
from .store import PaperStore
from .writing_models import WritingSnapshot, budget_json, integer, metadata_sha256, parse_model, project_id

def _invalid_number(_):
    raise ValueError("invalid JSON number")


_TABLES = {
    "writing_projects": {"project_id", "revision", "snapshot", "created_at", "updated_at"},
    "writing_revisions": {"project_id", "revision", "snapshot", "change_note", "created_at"},
    "writing_reading_pins": {"project_id", "reading_id"},
    "writing_selected_papers": {"project_id", "paper_id"},
}
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS writing_projects(project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS writing_revisions(project_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, change_note TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(project_id,revision))",
    "CREATE TABLE IF NOT EXISTS writing_reading_pins(project_id TEXT NOT NULL, reading_id TEXT NOT NULL, PRIMARY KEY(project_id,reading_id))",
    "CREATE INDEX IF NOT EXISTS writing_reading_pins_reading ON writing_reading_pins(reading_id)",
    "CREATE TABLE IF NOT EXISTS writing_selected_papers(project_id TEXT NOT NULL, paper_id TEXT NOT NULL, PRIMARY KEY(project_id,paper_id))",
    "CREATE INDEX IF NOT EXISTS writing_selected_papers_paper ON writing_selected_papers(paper_id)",
)


class WritingStore:
    def __init__(self, data_dir: Optional[str] = None, paper_store: Optional[PaperStore] = None):
        self.library = paper_store if paper_store is not None else PaperStore(data_dir)
        self.directory = self.library.directory
        self.path = self.library.path

    @contextmanager
    def connection(self, write: bool = False):
        with self.library.connection(write) as conn:
            if conn is None:
                yield None
                return
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            selected_missing = "writing_selected_papers" not in names
            if write:
                for statement in _SCHEMA:
                    conn.execute(statement)
                names.update(_TABLES)
            required = set(_TABLES) - {"writing_selected_papers"}
            if not required.issubset(names):
                if names.intersection(_TABLES):
                    raise JournalError("WRITING_SCHEMA_CHANGED", "写作库结构不完整，请备份后检查")
                yield None
                return
            for name, columns in _TABLES.items():
                if name not in names:
                    continue  # Old writing snapshots remain readable without migration.
                actual = {row[1] for row in conn.execute("PRAGMA table_info(" + name + ")")}
                if not columns.issubset(actual):
                    raise JournalError("WRITING_SCHEMA_CHANGED", "写作库结构不兼容，请备份后检查")
            if write and selected_missing:
                # One-time incremental backfill, never done during library reads.
                for row in conn.execute("SELECT project_id,revision,snapshot FROM writing_projects").fetchall():
                    snapshot = self._decode(row)
                    self._sync_selected(conn, snapshot["project_id"], snapshot["selected_paper_ids"])
            yield conn

    @staticmethod
    def _decode(row) -> Dict[str, Any]:
        try:
            data = json.loads(row["snapshot"], parse_constant=_invalid_number)
            if not isinstance(data, dict) or data.get("project_id") != row["project_id"] or data.get("revision") != row["revision"]:
                raise ValueError()
            return parse_model(WritingSnapshot, data, "WRITING_STORE_ERROR").model_dump(mode="json")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise JournalError("WRITING_STORE_ERROR", "写作项目快照损坏") from None

    def get(self, value: str, revision: Optional[int] = None) -> Dict[str, Any]:
        value = project_id(value)
        if revision is not None:
            integer(revision, "revision", 1)
        with self.connection() as conn:
            if revision is None:
                row = conn.execute("SELECT project_id,revision,snapshot FROM writing_projects WHERE project_id=?", (value,)).fetchone() if conn else None
            else:
                row = conn.execute("SELECT project_id,revision,snapshot FROM writing_revisions WHERE project_id=? AND revision=?", (value, revision)).fetchone() if conn else None
            if row is None:
                raise JournalError("PROJECT_NOT_FOUND" if revision is None else "REVISION_NOT_FOUND", "写作项目或指定历史版本不存在")
            return self._decode(row)

    def list(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        integer(limit, "limit", 1, 100)
        integer(offset, "offset")
        with self.connection() as conn:
            if conn is None:
                return {"projects": [], "total": 0}
            rows = conn.execute("SELECT project_id,revision,snapshot FROM writing_projects ORDER BY updated_at DESC,project_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM writing_projects").fetchone()[0]
            projects = []
            for row in rows:
                snapshot = self._decode(row)
                projects.append({"project_id": snapshot["project_id"], "revision": snapshot["revision"],
                                 "title": snapshot["profile"]["title"], "research_question": snapshot["profile"]["research_question"],
                                 "stage": snapshot.get("stage", "needs_search_plan"),
                                 "candidate_count": len(snapshot.get("candidates", [])),
                                 "selected_count": len(snapshot.get("selected_paper_ids", [])),
                                 "has_draft": bool(snapshot.get("draft")), "updated_at": snapshot["updated_at"]})
            return {"projects": projects, "total": total}

    def readings(self, paper_id: str) -> List[Dict[str, Any]]:
        """Includes pinned history beyond the legacy details view's twenty rows."""
        with self.library.connection() as conn:
            rows = conn.execute("SELECT * FROM readings WHERE paper_id=? ORDER BY created_at DESC,reading_id", (paper_id,)).fetchall() if conn else []
        try:
            return [{"reading_id": row["reading_id"], "file_sha256": row["sha"], "origin": row["origin"],
                     "created_at": row["created_at"], "card": json.loads(row["card"])} for row in rows]
        except (ValueError, TypeError, RecursionError):
            raise JournalError("LIBRARY_ERROR", "文献阅读卡损坏") from None

    @staticmethod
    def _sync_selected(conn, value: str, selected: Iterable[str]):
        conn.execute("DELETE FROM writing_selected_papers WHERE project_id=?", (value,))
        conn.executemany("INSERT INTO writing_selected_papers VALUES(?,?)", [(value, pid) for pid in selected])

    def _guard_papers(self, conn, paper_states: Optional[Dict[str, Dict[str, Any]]]):
        for paper_id, state in (paper_states or {}).items():
            row = conn.execute("SELECT record,acquisition FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
            if row is None:
                raise JournalError("PAPER_NOT_FOUND", "项目所用文献已不存在")
            try:
                record, acquisition = json.loads(row["record"]), json.loads(row["acquisition"])
                if metadata_sha256(record) != state["metadata_sha256"]:
                    raise JournalError("METADATA_MISMATCH", "文献元数据已变化，请重新筛选")
                if "file_sha256" in state:
                    if acquisition.get("file_sha256") != state["file_sha256"] or acquisition.get("status") not in ("imported", "downloaded"):
                        raise JournalError("STALE_EVIDENCE", "文献正文版本已变化，请重新阅读")
                    self.library.read_pdf(state["file_sha256"])
            except (ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, JournalError):
                    raise
                raise JournalError("LIBRARY_ERROR", "文献记录损坏") from None

    @staticmethod
    def _pin(conn, value: str, reading_ids: Iterable[str]):
        for reading_id in set(reading_ids):
            row = conn.execute("SELECT r.paper_id,r.sha,r.origin,r.card,p.acquisition FROM readings r JOIN papers p ON r.paper_id=p.paper_id WHERE r.reading_id=?", (reading_id,)).fetchone()
            if row is None:
                raise JournalError("STALE_EVIDENCE", "选用的阅读卡已不存在，请重新读取")
            try:
                acquisition = json.loads(row["acquisition"])
                card = budget_json(json.loads(row["card"]))
                expected = "reading-" + hashlib.sha256((row["paper_id"] + row["sha"] + row["origin"] + card).encode("utf-8")).hexdigest()[:24]
                if expected != reading_id:
                    raise JournalError("EVIDENCE_MISMATCH", "选用阅读卡身份与保存内容哈希不匹配")
            except JournalError:
                raise
            except (TypeError, ValueError):
                raise JournalError("LIBRARY_ERROR", "文献记录损坏") from None
            if row["sha"] != acquisition.get("file_sha256") or acquisition.get("status") not in ("imported", "downloaded"):
                raise JournalError("STALE_EVIDENCE", "选用的阅读卡正文版本已失效")
            conn.execute("INSERT OR IGNORE INTO writing_reading_pins VALUES(?,?)", (value, reading_id))

    def create(self, snapshot: Dict[str, Any], reading_ids: Iterable[str] = (),
               paper_states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        snapshot = dict(snapshot)
        value, now = "writing-" + uuid4().hex[:24], utc_now()
        snapshot.update(project_id=value, revision=1, created_at=now, updated_at=now)
        snapshot = parse_model(WritingSnapshot, snapshot).model_dump(mode="json")
        payload = budget_json(snapshot)
        with self.connection(True) as conn:
            self._guard_papers(conn, paper_states)
            conn.execute("INSERT INTO writing_projects VALUES(?,?,?,?,?)", (value, 1, payload, now, now))
            conn.execute("INSERT INTO writing_revisions VALUES(?,?,?,?,?)", (value, 1, payload, "创建文献支持写作项目", now))
            self._sync_selected(conn, value, snapshot["selected_paper_ids"])
            self._pin(conn, value, reading_ids)
        return snapshot

    def save(self, value: str, snapshot: Dict[str, Any], expected_revision: int,
             change_note: str, reading_ids: Iterable[str] = (),
             paper_states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        value = project_id(value)
        integer(expected_revision, "expected_revision", 1)
        if not isinstance(change_note, str) or not change_note.strip() or len(change_note) > 1000:
            raise JournalError("INVALID_INPUT", "请提供 1 到 1000 字符的变更说明")
        snapshot = dict(snapshot)
        snapshot.update(project_id=value, revision=expected_revision + 1, updated_at=utc_now())
        snapshot = parse_model(WritingSnapshot, snapshot).model_dump(mode="json")
        payload = budget_json(snapshot)
        with self.connection(True) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "reading_loops" in names:
                row_loop = conn.execute("SELECT active_lease FROM reading_loops WHERE project_id=?", (value,)).fetchone()
                if row_loop and row_loop[0]:
                    try:
                        lease = json.loads(row_loop[0])
                        if isinstance(lease, dict) and lease.get("expires_at", 0) > time.time():
                            owner = _CURRENT_LOOP_OWNER.get()
                            if owner != value and owner != lease.get("token"):
                                raise JournalError("CONCURRENT_MUTATION", "项目正在执行自适应阅读循环动作，请稍后重试")
                    except JournalError:
                        raise
                    except Exception:
                        pass
            row = conn.execute("SELECT revision FROM writing_projects WHERE project_id=?", (value,)).fetchone()
            if row is None:
                raise JournalError("PROJECT_NOT_FOUND", "写作项目不存在")
            if row[0] != expected_revision:
                raise JournalError("REVISION_CONFLICT", "项目已更新，请读取最新 revision 后重试")
            self._guard_papers(conn, paper_states)
            conn.execute("UPDATE writing_projects SET revision=?,snapshot=?,updated_at=? WHERE project_id=? AND revision=?",
                         (snapshot["revision"], payload, snapshot["updated_at"], value, expected_revision))
            conn.execute("INSERT INTO writing_revisions VALUES(?,?,?,?,?)", (value, snapshot["revision"], payload, change_note.strip(), snapshot["updated_at"]))
            self._sync_selected(conn, value, snapshot["selected_paper_ids"])
            self._pin(conn, value, reading_ids)
        return snapshot
