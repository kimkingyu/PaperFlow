"""Atomic SQLite snapshots; construction and empty reads are inert."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from uuid import uuid4

from .models import PlanningError, ResearchPlan, utc_now

SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 2 * 1024 * 1024
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_SCHEMA_MESSAGE = "研究规划数据库版本或结构不兼容，请备份后使用匹配版本"
_STORAGE_MESSAGE = "无法访问研究规划数据库，请检查存储目录和文件权限后重试"

_TABLE_COLUMNS = {
    "projects": ("project_id", "title", "goal", "latest_revision", "created_at", "updated_at"),
    "snapshots": ("project_id", "revision", "created_at", "updated_at", "change_note", "plan_json"),
}


def default_data_dir() -> Path:
    override = os.environ.get("PAPERFLOW_RESEARCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "PaperFlow" / "research" if base else Path.home() / ".paperflow" / "research"


def _integer(value: Any, code: str, message: str, *, minimum: int = 0, maximum: int = _SQLITE_MAX_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PlanningError(code, message)
    return value


def _project_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise PlanningError("INVALID_PROJECT_ID", "项目编号必须是非空且长度有限的字符串")
    return value.strip()


def _change_note(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 1000:
        raise PlanningError("INVALID_CHANGE_NOTE", "变更说明必须是非空且长度不超过 1000 的字符串")
    return value.strip()


def _payload(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    # ValidationError intentionally remains available to the caller.
    validated = ResearchPlan.model_validate(plan)
    try:
        value = validated.model_dump(mode="json")
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(serialized.encode("utf-8"))
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise PlanningError("INVALID_PLAN_JSON", "研究规划必须能转换为不含 NaN 或无穷值的 JSON 数据") from None
    if size > MAX_PLAN_BYTES:
        raise PlanningError("PLAN_TOO_LARGE", "研究规划 JSON 数据不得超过 2 MiB")
    return value, serialized


class PlanningStore:
    def __init__(self, data_dir: Optional[str] = None):
        self.directory = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        self.path = self.directory / "planning.sqlite3"

    @contextmanager
    def _connection(
        self, *, write: bool = False, create: bool = False, database_path: Optional[Path] = None,
    ) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            # rw never creates a missing file; ro cannot modify any SQLite data.
            mode = "rw" if write else "ro"
            path = self.path if database_path is None else database_path
            connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check_schema(connection, initialize=create)
            yield connection
            if write:
                connection.commit()
        except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if isinstance(exc, PlanningError):
                raise
            raise PlanningError("STORAGE_ERROR", _STORAGE_MESSAGE) from None
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _check_schema(connection: sqlite3.Connection, *, initialize: bool = False) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        if version == 0 and initialize and not objects:
            PlanningStore._initialize(connection)
            return
        if version != SCHEMA_VERSION:
            raise PlanningError("SCHEMA_CHANGED", _SCHEMA_MESSAGE)
        tables = {row["name"] for row in objects if row["type"] == "table"}
        if tables != set(_TABLE_COLUMNS) or any(row["type"] in ("view", "trigger") for row in objects):
            raise PlanningError("SCHEMA_CHANGED", _SCHEMA_MESSAGE)
        for table, expected in _TABLE_COLUMNS.items():
            # Table names are fixed internal constants, never caller input.
            columns = tuple(row["name"] for row in connection.execute("PRAGMA table_info(" + table + ")"))
            if columns != expected:
                raise PlanningError("SCHEMA_CHANGED", _SCHEMA_MESSAGE)

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        # DDL and user_version must share the snapshot transaction; executescript
        # would implicitly commit and leave partially initialized databases.
        connection.execute(
            "CREATE TABLE projects ("
            "project_id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, "
            "latest_revision INTEGER NOT NULL CHECK(latest_revision >= 1), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE snapshots ("
            "project_id TEXT NOT NULL REFERENCES projects(project_id), "
            "revision INTEGER NOT NULL CHECK(revision >= 1), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "change_note TEXT NOT NULL, plan_json TEXT NOT NULL, "
            "PRIMARY KEY(project_id, revision))"
        )
        connection.execute("CREATE INDEX projects_updated ON projects(updated_at DESC, project_id ASC)")
        connection.execute("PRAGMA user_version=1")

    @staticmethod
    def _insert_snapshot(connection: sqlite3.Connection, snapshot: Dict[str, Any], serialized: str) -> None:
        connection.execute(
            "INSERT INTO snapshots(project_id, revision, created_at, updated_at, change_note, plan_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                snapshot["project_id"], snapshot["revision"], snapshot["created_at"],
                snapshot["updated_at"], snapshot["change_note"], serialized,
            ),
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> Dict[str, Any]:
        serialized = row["plan_json"]
        if not isinstance(serialized, str) or len(serialized.encode("utf-8")) > MAX_PLAN_BYTES:
            raise PlanningError("INVALID_PLAN_JSON", "研究规划快照的数据格式或大小无效")
        try:
            # Loading must not tolerate NaN/Infinity from externally modified files.
            plan = json.loads(serialized, parse_constant=_invalid_json_number)
            if not isinstance(plan, dict):
                raise ValueError
            plan, _ = _payload(plan)
        except (ValueError, TypeError, OverflowError, UnicodeError):
            raise PlanningError("INVALID_PLAN_JSON", "研究规划快照的数据格式或内容无效") from None
        return {
            "project_id": row["project_id"], "revision": row["revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "change_note": row["change_note"], "plan": plan,
        }

    def _exists(self) -> bool:
        try:
            return self.path.exists()
        except (OSError, ValueError):
            raise PlanningError("STORAGE_ERROR", _STORAGE_MESSAGE) from None

    def create(self, plan: Dict[str, Any], change_note: str = "创建研究项目") -> Dict[str, Any]:
        value, serialized = _payload(plan)
        note = _change_note(change_note)
        timestamp = utc_now()
        snapshot = {
            "project_id": "rp_" + uuid4().hex, "revision": 1,
            "created_at": timestamp, "updated_at": timestamp,
            "change_note": note, "plan": value,
        }
        if self._exists():
            self._create_snapshot(snapshot, serialized)
        else:
            self._create_initial_database(snapshot, serialized)
        return snapshot

    def _create_snapshot(
        self, snapshot: Dict[str, Any], serialized: str, database_path: Optional[Path] = None,
    ) -> None:
        value = snapshot["plan"]
        with self._connection(write=True, create=True, database_path=database_path) as connection:
            connection.execute(
                "INSERT INTO projects(project_id, title, goal, latest_revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot["project_id"], value["profile"]["title"], value["profile"]["goal"],
                    1, snapshot["created_at"], snapshot["updated_at"],
                ),
            )
            self._insert_snapshot(connection, snapshot, serialized)

    def _create_initial_database(self, snapshot: Dict[str, Any], serialized: str) -> None:
        stage = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".planning-", suffix=".sqlite3", dir=self.directory)
            os.close(descriptor)
            stage = Path(name)
            self._create_snapshot(snapshot, serialized, database_path=stage)
            try:
                # Publish a committed database without replacing any competing
                # creator's file. Readers never see half-initialized schemas.
                os.link(stage, self.path)
            except FileExistsError:
                self._create_snapshot(snapshot, serialized)
        except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
            if isinstance(exc, PlanningError):
                raise
            raise PlanningError("STORAGE_ERROR", _STORAGE_MESSAGE) from None
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    raise PlanningError("STORAGE_ERROR", _STORAGE_MESSAGE) from None

    def get(self, project_id: str, revision: Optional[int] = None) -> Dict[str, Any]:
        identifier = _project_id(project_id)
        if revision is not None:
            _integer(revision, "INVALID_REVISION", "版本号必须是正整数", minimum=1)
        if not self._exists():
            raise PlanningError("PROJECT_NOT_FOUND", "研究项目不存在")
        with self._connection() as connection:
            project = connection.execute(
                "SELECT latest_revision FROM projects WHERE project_id=?", (identifier,)
            ).fetchone()
            if project is None:
                raise PlanningError("PROJECT_NOT_FOUND", "研究项目不存在")
            requested = project["latest_revision"] if revision is None else revision
            row = connection.execute(
                "SELECT project_id, revision, created_at, updated_at, change_note, plan_json "
                "FROM snapshots WHERE project_id=? AND revision=?", (identifier, requested)
            ).fetchone()
            if row is None:
                raise PlanningError("REVISION_NOT_FOUND", "研究项目的指定版本不存在")
            return self._snapshot(row)

    def save(
        self, project_id: str, plan: Dict[str, Any], expected_revision: int,
        change_note: str = "更新研究规划",
    ) -> Dict[str, Any]:
        identifier = _project_id(project_id)
        expected = _integer(expected_revision, "INVALID_REVISION", "预期版本号必须是正整数", minimum=1)
        value, serialized = _payload(plan)
        note = _change_note(change_note)
        if not self._exists():
            raise PlanningError("PROJECT_NOT_FOUND", "研究项目不存在")
        with self._connection(write=True) as connection:
            project = connection.execute(
                "SELECT latest_revision, created_at FROM projects WHERE project_id=?", (identifier,)
            ).fetchone()
            if project is None:
                raise PlanningError("PROJECT_NOT_FOUND", "研究项目不存在")
            if project["latest_revision"] != expected:
                raise PlanningError("REVISION_CONFLICT", "研究规划已有新版本，请重新读取后再提交修改")
            revision = _integer(expected + 1, "INVALID_REVISION", "研究项目的版本号已达到存储上限", minimum=1)
            timestamp = utc_now()
            snapshot = {
                "project_id": identifier, "revision": revision, "created_at": project["created_at"],
                "updated_at": timestamp, "change_note": note, "plan": value,
            }
            self._insert_snapshot(connection, snapshot, serialized)
            connection.execute(
                "UPDATE projects SET title=?, goal=?, latest_revision=?, updated_at=? WHERE project_id=?",
                (value["profile"]["title"], value["profile"]["goal"], revision, timestamp, identifier),
            )
        return snapshot

    def list_projects(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        _integer(limit, "INVALID_PAGINATION", "每页数量必须是 1 至 100 的整数", minimum=1, maximum=100)
        _integer(offset, "INVALID_PAGINATION", "分页偏移必须是非负整数")
        if not self._exists():
            return {"projects": [], "total": 0, "limit": limit, "offset": offset}
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            rows = connection.execute(
                "SELECT project_id, title, goal, latest_revision AS revision, created_at, updated_at "
                "FROM projects ORDER BY updated_at DESC, project_id ASC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return {"projects": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def _invalid_json_number(_: str) -> None:
    raise ValueError("JSON 数值必须有限")
