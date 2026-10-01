"""Inert-on-construction SQLite library and content-addressed local PDFs."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from functools import wraps
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from paperflow.engine.journals.models import JournalError, utc_now
from paperflow.engine.journals.store import default_data_dir
from .models import PaperRecord, SHA256_RE, validate_paper_id

MAX_FILE_BYTES = 32 * 1024 * 1024
_CACHE_LOCK = threading.RLock()


def _cache_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _CACHE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def default_paper_dir() -> Path:
    override = os.environ.get("PAPERFLOW_PAPER_HOME")
    return Path(override).expanduser().resolve() if override else default_data_dir() / "papers"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _merge_ranges(ranges: List[List[int]]) -> List[List[int]]:
    result: List[List[int]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


class PaperStore:
    def __init__(self, data_dir: Optional[str] = None):
        self.directory = Path(data_dir).expanduser().resolve() if data_dir else default_paper_dir()
        self.path = self.directory / "papers.sqlite3"

    def _inside(self, path: Path) -> Path:
        # SQLite WAL/SHM leaves can disappear during Windows final-path lookup.
        # Resolve their stable parent, and inspect the leaf's link flag separately.
        is_junction = getattr(path, "is_junction", lambda: False)
        if path.is_symlink() or is_junction() or not path.parent.resolve().is_relative_to(self.directory.resolve()):
            raise JournalError("UNSAFE_CACHE_PATH", "拒绝文献库中的符号链接或越界路径")
        return path

    @contextmanager
    def connection(self, write: bool = False):
        if not write and not self.path.exists():
            yield None
            return
        self._inside(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            self._inside(Path(str(self.path) + suffix))
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(str(self.path) if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA trusted_schema=OFF")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise JournalError("SCHEMA_CHANGED", "文献库版本不兼容，请备份后使用匹配版本")
            if not write and version == 0:
                yield None
                return
            if write:
                # Journal-mode changes can bypass SQLite's normal busy timeout
                # during concurrent cold opens. Keep the database's existing mode.
                if version == 0:
                    conn.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE IF NOT EXISTS papers(paper_id TEXT PRIMARY KEY, record TEXT NOT NULL, acquisition TEXT NOT NULL, updated_at TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS pages(paper_id TEXT NOT NULL, sha TEXT NOT NULL, page INTEGER NOT NULL, total_chars INTEGER NOT NULL, ranges TEXT NOT NULL, empty_kind TEXT NOT NULL, PRIMARY KEY(paper_id,sha,page));
                        CREATE TABLE IF NOT EXISTS fragments(fragment_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, sha TEXT NOT NULL, page INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS readings(reading_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, sha TEXT NOT NULL, origin TEXT NOT NULL, card TEXT NOT NULL, created_at TEXT NOT NULL);
                        PRAGMA user_version=1;
                        COMMIT;
                    """)
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except sqlite3.Error:
            if conn:
                conn.rollback()
            raise JournalError("LIBRARY_ERROR", "文献库操作失败，请检查数据库完整性或稍后重试") from None
        except Exception:
            if conn and write:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def put(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record = PaperRecord.model_validate(record).model_dump(mode="json")
        with self.connection(True) as conn:
            old = conn.execute("SELECT record FROM papers WHERE paper_id=?", (record["paper_id"],)).fetchone()
            if old:
                previous = json.loads(old[0])
                # A metadata refresh with a missing abstract must not destroy an existing one.
                for key in ("abstract", "fulltext_locations", "sources"):
                    if not record.get(key):
                        record[key] = previous.get(key, record.get(key))
            conn.execute("INSERT INTO papers VALUES(?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET record=excluded.record,updated_at=excluded.updated_at",
                         (record["paper_id"], _json(record), _json({"status": "metadata_only"}), utc_now()))
        return self.get(record["paper_id"])

    def get(self, paper_id: str) -> Dict[str, Any]:
        validate_paper_id(paper_id)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone() if conn else None
            if row is None:
                raise JournalError("PAPER_NOT_FOUND", "文献不存在，请先检索、查询标识或导入")
            try:
                record = PaperRecord.model_validate(json.loads(row["record"])).model_dump(mode="json")
                acquisition = json.loads(row["acquisition"])
            except (ValueError, TypeError):
                raise JournalError("LIBRARY_ERROR", "文献元数据已损坏") from None
            cards = conn.execute("SELECT * FROM readings WHERE paper_id=? ORDER BY created_at DESC LIMIT 20", (paper_id,)).fetchall()
            record["acquisition"] = acquisition
            record["reading_cards"] = [{"reading_id": c["reading_id"], "created_at": c["created_at"], "origin": c["origin"],
                                        "valid": c["sha"] == acquisition.get("file_sha256") and acquisition.get("status") in ("downloaded", "imported"),
                                        "card": json.loads(c["card"])} for c in cards]
            return record

    def list(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        with self.connection() as conn:
            if conn is None:
                return {"papers": [], "total": 0}
            ids = conn.execute("SELECT paper_id FROM papers ORDER BY updated_at DESC,paper_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        return {"papers": [self.get(r[0]) for r in ids], "total": total}

    def set_acquisition(self, paper_id: str, acquisition: Dict[str, Any]) -> None:
        validate_paper_id(paper_id)
        with self.connection(True) as conn:
            old = conn.execute("SELECT acquisition FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
            if not old:
                raise JournalError("PAPER_NOT_FOUND", "文献不存在")
            # A failed racing download must not erase a successful one.
            previous = json.loads(old[0])
            if acquisition.get("status") in ("failed", "unavailable", "host_denied") and previous.get("status") in ("downloaded", "imported"):
                return
            conn.execute("UPDATE papers SET acquisition=?,updated_at=? WHERE paper_id=?", (_json(acquisition), utc_now(), paper_id))

    def pdf_path(self, sha: str) -> Path:
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            raise JournalError("INVALID_HASH", "PDF 哈希无效")
        return self._inside(self.directory / "pdf" / (sha + ".pdf"))

    @_cache_locked
    def cache_pdf(self, raw: bytes) -> str:
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise JournalError("FILE_TOO_LARGE", "PDF 为空或超过 32MiB 上限")
        if not raw.lstrip()[:1024].startswith(b"%PDF-"):
            raise JournalError("INVALID_PDF", "文件不是 PDF，可能是网页或登录页")
        sha = hashlib.sha256(raw).hexdigest()
        path = self.pdf_path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self.read_pdf(sha)
                return sha
            except JournalError as exc:
                if exc.code != "CACHE_CORRUPTED":
                    raise
                # Only our content-addressed cache is repaired, never the user's
                # import source. A verified fresh payload replaces corrupt bytes.
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".pending-", delete=False) as stream:
                pending = stream.name
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        except OSError:
            raise JournalError("CACHE_WRITE_ERROR", "无法写入本地 PDF 缓存") from None
        finally:
            if pending and os.path.exists(pending):
                os.unlink(pending)
        return sha

    @_cache_locked
    def read_pdf(self, sha: str) -> bytes:
        path = self.pdf_path(sha)
        if not path.is_file():
            raise JournalError("PDF_NOT_FOUND", "本地 PDF 缓存不存在，请重新下载或导入")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise JournalError("CACHE_READ_ERROR", "无法读取本地 PDF 缓存") from None
        if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != sha:
            raise JournalError("CACHE_CORRUPTED", "PDF 缓存与记录哈希不一致，停止阅读")
        return raw

    def record_read(self, paper_id: str, sha: str, pages: List[Dict[str, Any]], fragments: List[Dict[str, Any]]) -> None:
        with self.connection(True) as conn:
            for page in pages:
                old = conn.execute("SELECT ranges FROM pages WHERE paper_id=? AND sha=? AND page=?", (paper_id, sha, page["page_number"])).fetchone()
                ranges = json.loads(old[0]) if old else []
                ranges = _merge_ranges(ranges + [[page["offset_start"], page["offset_end"]]])
                conn.execute("INSERT INTO pages VALUES(?,?,?,?,?,?) ON CONFLICT(paper_id,sha,page) DO UPDATE SET ranges=excluded.ranges,total_chars=excluded.total_chars,empty_kind=excluded.empty_kind",
                             (paper_id, sha, page["page_number"], page["total_chars"], _json(ranges), page.get("empty_kind", "")))
            for f in fragments:
                conn.execute("INSERT OR IGNORE INTO fragments VALUES(?,?,?,?,?,?,?)",
                             (f["fragment_id"], paper_id, sha, f["page_number"], f["start"], f["end"], f["text"]))

    def coverage(self, paper_id: str, sha: str, total_pages: int) -> Dict[str, Any]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM pages WHERE paper_id=? AND sha=? ORDER BY page", (paper_id, sha)).fetchall() if conn else []
        extracted, visited, empty = [], [], []
        for row in rows:
            visited.append(row["page"])
            if row["empty_kind"]:
                empty.append({"page_number": row["page"], "reason": row["empty_kind"]})
            else:
                ranges = json.loads(row["ranges"])
                if ranges and ranges[0][0] == 0 and ranges[0][1] >= row["total_chars"]:
                    extracted.append(row["page"])
        complete = len(extracted) == total_pages
        all_visited = len(visited) == total_pages
        stage = "fulltext_text_complete" if complete else "fulltext_partial"
        if empty:
            stage = "ocr_required" if all_visited and len(empty) == total_pages else "partial_with_missing_text"
        return {"stage": stage, "total_pages": total_pages, "visited_pages": visited, "fully_extracted_pages": extracted,
                "empty_or_unextractable_pages": empty, "all_pages_visited": all_visited, "text_complete": complete,
                "understanding_complete": False, "images_extracted": False, "formulas_parsed": False,
                "ocr_performed": False, "backend_calls_llm": False}

    def fragment(self, paper_id: str, sha: str, fragment_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(fragment_id, str) or len(fragment_id) > 100:
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM fragments WHERE fragment_id=? AND paper_id=? AND sha=?", (fragment_id, paper_id, sha)).fetchone() if conn else None
        return dict(row) if row else None

    def save_card(self, paper_id: str, sha: str, card: Dict[str, Any], origin: str) -> Dict[str, Any]:
        payload = _json(card)
        if len(payload.encode("utf-8")) > 1024 * 1024:
            raise JournalError("INPUT_TOO_LARGE", "解读卡超过 1MiB 上限")
        reading_id = "reading-" + hashlib.sha256((paper_id + sha + origin + payload).encode("utf-8")).hexdigest()[:24]
        created = utc_now()
        with self.connection(True) as conn:
            conn.execute("INSERT INTO readings VALUES(?,?,?,?,?,?) ON CONFLICT(reading_id) DO NOTHING", (reading_id, paper_id, sha, origin, payload, created))
            row = conn.execute("SELECT created_at FROM readings WHERE reading_id=?", (reading_id,)).fetchone()
            created = row[0]
            # Writing projects pin their selected/cited cards across all revisions.
            # Unused papers retain the original last-twenty cleanup behavior.
            has_pins = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='writing_reading_pins'").fetchone()
            has_selected = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='writing_selected_papers'").fetchone()
            if has_pins and has_selected:
                # New readings of currently selected papers are selected material,
                # too. The indexed relation avoids scanning project JSON on save.
                conn.execute("INSERT OR IGNORE INTO writing_reading_pins(project_id,reading_id) SELECT project_id,? FROM writing_selected_papers WHERE paper_id=?", (reading_id, paper_id))
            protected = " AND reading_id NOT IN (SELECT reading_id FROM writing_reading_pins)" if has_pins else ""
            conn.execute("DELETE FROM readings WHERE paper_id=? AND reading_id NOT IN (SELECT reading_id FROM readings WHERE paper_id=? ORDER BY created_at DESC LIMIT 20)" + protected, (paper_id, paper_id))
        return {"reading_id": reading_id, "created_at": created, "origin": origin, "valid": True, "card": card,
                "evidence_check_only": True, "semantic_correctness_verified": False}
