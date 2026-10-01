"""Independent storage for adaptive reading loops, rounds, reviews, and actions."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from paperflow.engine.journals.models import JournalError, utc_now
from .models import PaperRecord
from .reading_loop_models import (
    ACTION_RE, REVIEW_RE, TICKET_RE,
    ReadingLoopState, LoopBudget, LoopUsage, LoopReserved, InheritedBaseline, NextAction,
    generate_action_id, generate_review_id, generate_ticket_id
)
from .store import PaperStore
from .writing_models import budget_json, integer, parse_model, project_id

_LOOP_TABLES = {
    "reading_loops": {
        "project_id", "loop_revision", "project_revision", "status",
        "stop_reason", "stop_detail", "budget", "usage", "reserved",
        "inherited_baseline", "round_index", "no_progress_rounds",
        "request", "next_action", "context_fingerprint", "active_lease",
        "created_at", "updated_at"
    },
    "reading_loop_revisions": {
        "project_id", "loop_revision", "snapshot", "change_note", "created_at"
    },
    "reading_loop_reviews": {
        "review_id", "project_id", "loop_revision", "round_index",
        "review_data", "context_fingerprint", "origin", "created_at"
    },
    "reading_loop_rounds": {
        "project_id", "round_index", "goal", "status", "started_at", "ended_at", "data"
    },
    "reading_loop_actions": {
        "action_id", "project_id", "round_index", "kind", "status",
        "payload", "result", "reserved_cost", "settled_cost",
        "lease_token", "lease_expires_at", "created_at", "updated_at"
    },
    "reading_loop_read_ranges": {
        "project_id", "paper_id", "sha", "page_number", "start_offset", "end_offset"
    },
    "reading_loop_model_tickets": {
        "ticket_id", "project_id", "action_id", "loop_revision", "status", "created_at"
    }
}

_LOOP_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS reading_loops(
        project_id TEXT PRIMARY KEY,
        loop_revision INTEGER NOT NULL,
        project_revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        stop_reason TEXT,
        stop_detail TEXT,
        budget TEXT NOT NULL,
        usage TEXT NOT NULL,
        reserved TEXT NOT NULL,
        inherited_baseline TEXT NOT NULL,
        round_index INTEGER NOT NULL,
        no_progress_rounds INTEGER NOT NULL,
        request TEXT NOT NULL,
        next_action TEXT,
        context_fingerprint TEXT,
        active_lease TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reading_loop_revisions(
        project_id TEXT NOT NULL,
        loop_revision INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        change_note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(project_id, loop_revision)
    )""",
    """CREATE TABLE IF NOT EXISTS reading_loop_reviews(
        review_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        loop_revision INTEGER NOT NULL,
        round_index INTEGER NOT NULL,
        review_data TEXT NOT NULL,
        context_fingerprint TEXT NOT NULL,
        origin TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS reading_loop_reviews_proj ON reading_loop_reviews(project_id, round_index)",
    """CREATE TABLE IF NOT EXISTS reading_loop_rounds(
        project_id TEXT NOT NULL,
        round_index INTEGER NOT NULL,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY(project_id, round_index)
    )""",
    """CREATE TABLE IF NOT EXISTS reading_loop_actions(
        action_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        round_index INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        payload TEXT NOT NULL,
        result TEXT,
        reserved_cost TEXT,
        settled_cost TEXT,
        lease_token TEXT,
        lease_expires_at REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS reading_loop_actions_proj ON reading_loop_actions(project_id, round_index)",
    """CREATE TABLE IF NOT EXISTS reading_loop_read_ranges(
        project_id TEXT NOT NULL,
        paper_id TEXT NOT NULL,
        sha TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        start_offset INTEGER NOT NULL,
        end_offset INTEGER NOT NULL,
        PRIMARY KEY(project_id, paper_id, sha, page_number, start_offset, end_offset)
    )""",
    "CREATE INDEX IF NOT EXISTS reading_loop_read_ranges_idx ON reading_loop_read_ranges(project_id, paper_id, sha, page_number)",
    """CREATE TABLE IF NOT EXISTS reading_loop_model_tickets(
        ticket_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        loop_revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


def _merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not intervals:
        return []
    sorted_int = sorted(intervals, key=lambda x: (x[0], x[1]))
    merged = [sorted_int[0]]
    for cur_start, cur_end in sorted_int[1:]:
        prev_start, prev_end = merged[-1]
        if cur_start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, cur_end))
        else:
            merged.append((cur_start, cur_end))
    return merged


class ReadingLoopStore:
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
            if write:
                for statement in _LOOP_SCHEMA:
                    conn.execute(statement)
                names.update(_LOOP_TABLES)

            required = set(_LOOP_TABLES)
            if not required.issubset(names):
                if not write:
                    # In readonly mode, if tables don't exist yet, simply yield conn and handle gracefully
                    yield conn
                    return
                raise JournalError("LOOP_SCHEMA_CHANGED", "阅读循环表结构不完整，请检查")

            for name, columns in _LOOP_TABLES.items():
                if name not in names:
                    continue
                actual = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
                if not columns.issubset(actual):
                    raise JournalError("LOOP_SCHEMA_CHANGED", f"阅读循环表 {name} 结构不兼容")
            yield conn

    def get_lease(self, pid: str) -> Optional[Dict[str, Any]]:
        pid = project_id(pid)
        with self.connection(False) as conn:
            if conn is None:
                return None
            try:
                row = conn.execute("SELECT active_lease FROM reading_loops WHERE project_id=?", (pid,)).fetchone()
            except Exception:
                return None
            if not row or not row["active_lease"]:
                return None
            try:
                lease = json.loads(row["active_lease"])
                if isinstance(lease, dict) and lease.get("expires_at", 0) > time.time():
                    return lease
            except Exception:
                pass
            return None

    def acquire_lease(self, pid: str, action_id: str, expected_loop_revision: int, duration_seconds: float = 30.0) -> Tuple[str, float]:
        pid = project_id(pid)
        integer(expected_loop_revision, "expected_loop_revision", 1)
        now = time.time()
        expires_at = now + duration_seconds
        token = uuid4().hex
        lease_data = {
            "token": token,
            "action_id": action_id,
            "acquired_at": now,
            "expires_at": expires_at
        }
        payload = json.dumps(lease_data)

        with self.connection(True) as conn:
            row = conn.execute("SELECT loop_revision, active_lease FROM reading_loops WHERE project_id=?", (pid,)).fetchone()
            if row is None:
                raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环未启动")
            if row["loop_revision"] != expected_loop_revision:
                raise JournalError("REVISION_CONFLICT", "循环已被其他操作推进，请刷新重试")

            if row["active_lease"]:
                try:
                    cur = json.loads(row["active_lease"])
                    if cur.get("expires_at", 0) > now and cur.get("token") != token:
                        raise JournalError("CONCURRENT_OPERATION", "当前循环已有动作正在执行，请稍后")
                except JournalError:
                    raise
                except Exception:
                    pass

            conn.execute("UPDATE reading_loops SET active_lease=?, updated_at=? WHERE project_id=?",
                         (payload, utc_now(), pid))
            conn.execute("UPDATE reading_loop_actions SET lease_token=?, lease_expires_at=?, updated_at=? WHERE action_id=? AND project_id=?",
                         (token, expires_at, utc_now(), action_id, pid))
        return token, expires_at

    def release_lease(self, pid: str, action_id: str, token: str):
        pid = project_id(pid)
        with self.connection(True) as conn:
            row = conn.execute("SELECT active_lease FROM reading_loops WHERE project_id=?", (pid,)).fetchone()
            if row and row["active_lease"]:
                try:
                    cur = json.loads(row["active_lease"])
                    if cur.get("token") == token:
                        conn.execute("UPDATE reading_loops SET active_lease=NULL, updated_at=? WHERE project_id=?",
                                     (utc_now(), pid))
                except Exception:
                    pass
            conn.execute("UPDATE reading_loop_actions SET lease_token=NULL, lease_expires_at=NULL, updated_at=? WHERE action_id=? AND project_id=?",
                         (utc_now(), action_id, pid))

    def get(self, pid: str, loop_revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
        pid = project_id(pid)
        if loop_revision is not None:
            integer(loop_revision, "loop_revision", 1)

        with self.connection(False) as conn:
            if conn is None:
                return None
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "reading_loops" not in names:
                return None

            if loop_revision is not None:
                rev_row = conn.execute(
                    "SELECT snapshot FROM reading_loop_revisions WHERE project_id=? AND loop_revision=?",
                    (pid, loop_revision)
                ).fetchone()
                if not rev_row:
                    return None
                try:
                    return json.loads(rev_row["snapshot"])
                except Exception:
                    raise JournalError("LOOP_STORE_ERROR", "历史循环快照损坏")

            row = conn.execute("SELECT * FROM reading_loops WHERE project_id=?", (pid,)).fetchone()
            if not row:
                return None

            # Fetch rounds
            round_rows = conn.execute(
                "SELECT round_index, goal, status, started_at, ended_at, data FROM reading_loop_rounds WHERE project_id=? ORDER BY round_index",
                (pid,)
            ).fetchall()
            rounds = []
            for r in round_rows:
                rd = json.loads(r["data"]) if r["data"] else {}
                rd.update({
                    "round_index": r["round_index"],
                    "goal": r["goal"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"]
                })
                rounds.append(rd)

            # Fetch reviews
            rev_rows = conn.execute(
                "SELECT review_id, loop_revision, round_index, review_data, context_fingerprint, origin, created_at "
                "FROM reading_loop_reviews WHERE project_id=? ORDER BY created_at, review_id",
                (pid,)
            ).fetchall()
            reviews = []
            for rev in rev_rows:
                rdata = json.loads(rev["review_data"])
                rdata.update({
                    "review_id": rev["review_id"],
                    "loop_revision": rev["loop_revision"],
                    "round_index": rev["round_index"],
                    "context_fingerprint": rev["context_fingerprint"],
                    "origin": rev["origin"],
                    "created_at": rev["created_at"]
                })
                reviews.append(rdata)

            # Fetch actions
            act_rows = conn.execute(
                "SELECT action_id, round_index, kind, status, payload, result, reserved_cost, settled_cost, created_at, updated_at "
                "FROM reading_loop_actions WHERE project_id=? ORDER BY created_at, action_id",
                (pid,)
            ).fetchall()
            actions = []
            for act in act_rows:
                actions.append({
                    "action_id": act["action_id"],
                    "round_index": act["round_index"],
                    "kind": act["kind"],
                    "status": act["status"],
                    "payload": json.loads(act["payload"]) if act["payload"] else {},
                    "result": json.loads(act["result"]) if act["result"] else None,
                    "reserved_cost": json.loads(act["reserved_cost"]) if act["reserved_cost"] else {},
                    "settled_cost": json.loads(act["settled_cost"]) if act["settled_cost"] else {},
                    "created_at": act["created_at"],
                    "updated_at": act["updated_at"]
                })

            next_act = json.loads(row["next_action"]) if row["next_action"] else None

            res = {
                "project_id": row["project_id"],
                "loop_revision": row["loop_revision"],
                "project_revision": row["project_revision"],
                "status": row["status"],
                "stop_reason": row["stop_reason"],
                "stop_detail": row["stop_detail"] or "",
                "budget": json.loads(row["budget"]),
                "usage": json.loads(row["usage"]),
                "reserved": json.loads(row["reserved"]),
                "inherited_baseline": json.loads(row["inherited_baseline"]),
                "round_index": row["round_index"],
                "no_progress_rounds": row["no_progress_rounds"],
                "request": row["request"] or "",
                "next_action": next_act,
                "context_fingerprint": row["context_fingerprint"],
                "rounds": rounds,
                "reviews": reviews,
                "actions": actions,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
            return res

    def create(self, pid: str, state: Dict[str, Any], project_revision: int, change_note: str = "初始化自适应文献阅读循环") -> Dict[str, Any]:
        pid = project_id(pid)
        integer(project_revision, "project_revision", 1)
        now = utc_now()

        state_copy = dict(state)
        state_copy["loop_revision"] = 1
        state_copy["project_revision"] = project_revision
        loop_obj = parse_model(ReadingLoopState, state_copy)
        dumped = loop_obj.model_dump(mode="json")

        budget_str = budget_json(dumped["budget"])
        usage_str = budget_json(dumped["usage"])
        reserved_str = budget_json(dumped["reserved"])
        baseline_str = budget_json(dumped["inherited_baseline"])
        next_act_str = budget_json(dumped["next_action"]) if dumped.get("next_action") else None
        snapshot_str = budget_json({
            "project_id": pid,
            "loop_revision": 1,
            "project_revision": project_revision,
            **dumped
        })

        with self.connection(True) as conn:
            existing = conn.execute("SELECT loop_revision FROM reading_loops WHERE project_id=?", (pid,)).fetchone()
            if existing is not None:
                raise JournalError("LOOP_ALREADY_EXISTS", "该项目已启动自适应阅读循环，不能重新创建清零")

            conn.execute(
                """INSERT INTO reading_loops(
                    project_id, loop_revision, project_revision, status, stop_reason, stop_detail,
                    budget, usage, reserved, inherited_baseline, round_index, no_progress_rounds,
                    request, next_action, context_fingerprint, active_lease, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    pid, 1, project_revision, dumped["status"], dumped.get("stop_reason"),
                    dumped.get("stop_detail", ""), budget_str, usage_str, reserved_str,
                    baseline_str, dumped["round_index"], dumped["no_progress_rounds"],
                    dumped.get("request", ""), next_act_str, dumped.get("context_fingerprint"),
                    now, now
                )
            )
            conn.execute(
                """INSERT INTO reading_loop_revisions(
                    project_id, loop_revision, snapshot, change_note, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (pid, 1, snapshot_str, change_note, now)
            )
        return self.get(pid)

    def save(
        self,
        pid: str,
        state: Dict[str, Any],
        expected_loop_revision: int,
        change_note: str,
        expected_project_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        pid = project_id(pid)
        integer(expected_loop_revision, "expected_loop_revision", 1)
        if expected_project_revision is not None:
            integer(expected_project_revision, "expected_project_revision", 1)
        if not isinstance(change_note, str) or not change_note.strip() or len(change_note) > 1000:
            raise JournalError("INVALID_INPUT", "请提供 1 到 1000 字符的循环变更说明")

        now = utc_now()
        new_loop_rev = expected_loop_revision + 1

        state_copy = dict(state)
        state_copy["loop_revision"] = new_loop_rev
        loop_obj = parse_model(ReadingLoopState, state_copy)
        dumped = loop_obj.model_dump(mode="json")

        budget_str = budget_json(dumped["budget"])
        usage_str = budget_json(dumped["usage"])
        reserved_str = budget_json(dumped["reserved"])
        baseline_str = budget_json(dumped["inherited_baseline"])
        next_act_str = budget_json(dumped["next_action"]) if dumped.get("next_action") else None

        with self.connection(True) as conn:
            row = conn.execute(
                "SELECT loop_revision, project_revision FROM reading_loops WHERE project_id=?",
                (pid,)
            ).fetchone()
            if row is None:
                raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环不存在")
            if row["loop_revision"] != expected_loop_revision:
                raise JournalError("REVISION_CONFLICT", "自适应阅读循环版本冲突，请读取最新 revision 后重试")

            current_proj_rev = row["project_revision"]
            proj_rev = dumped.get("project_revision") or expected_project_revision or current_proj_rev

            snapshot_str = budget_json({
                "project_id": pid,
                "loop_revision": new_loop_rev,
                "project_revision": proj_rev,
                **dumped
            })

            conn.execute(
                """UPDATE reading_loops SET
                    loop_revision=?, project_revision=?, status=?, stop_reason=?, stop_detail=?,
                    budget=?, usage=?, reserved=?, inherited_baseline=?, round_index=?,
                    no_progress_rounds=?, request=?, next_action=?, context_fingerprint=?,
                    updated_at=?
                WHERE project_id=? AND loop_revision=?""",
                (
                    new_loop_rev, proj_rev, dumped["status"], dumped.get("stop_reason"),
                    dumped.get("stop_detail", ""), budget_str, usage_str, reserved_str,
                    baseline_str, dumped["round_index"], dumped["no_progress_rounds"],
                    dumped.get("request", ""), next_act_str, dumped.get("context_fingerprint"),
                    now, pid, expected_loop_revision
                )
            )
            conn.execute(
                """INSERT INTO reading_loop_revisions(
                    project_id, loop_revision, snapshot, change_note, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (pid, new_loop_rev, snapshot_str, change_note, now)
            )

        return self.get(pid)

    def record_read_range(
        self,
        pid: str,
        paper_id: str,
        sha: str,
        page_number: int,
        start_offset: int,
        end_offset: int
    ) -> Tuple[int, int]:
        """Records a read page and character offset range, returning (new_pages_count, new_chars_count).

        Performs interval union merging so overlapping fragments on the same physical page
        do not double count characters.
        """
        pid = project_id(pid)
        integer(page_number, "page_number", 1, 10000)
        integer(start_offset, "start_offset", 0)
        integer(end_offset, "end_offset", start_offset)
        if start_offset == end_offset:
            return 0, 0

        with self.connection(True) as conn:
            existing_rows = conn.execute(
                """SELECT start_offset, end_offset FROM reading_loop_read_ranges
                   WHERE project_id=? AND paper_id=? AND sha=? AND page_number=?""",
                (pid, paper_id, sha, page_number)
            ).fetchall()

            new_page = 1 if len(existing_rows) == 0 else 0
            existing_intervals = [(r["start_offset"], r["end_offset"]) for r in existing_rows]
            merged_before = _merge_intervals(existing_intervals)
            total_chars_before = sum(end - start for start, end in merged_before)

            all_intervals = existing_intervals + [(start_offset, end_offset)]
            merged_after = _merge_intervals(all_intervals)
            total_chars_after = sum(end - start for start, end in merged_after)
            new_chars = max(0, total_chars_after - total_chars_before)

            # Replace intervals with merged intervals
            conn.execute(
                """DELETE FROM reading_loop_read_ranges
                   WHERE project_id=? AND paper_id=? AND sha=? AND page_number=?""",
                (pid, paper_id, sha, page_number)
            )
            for m_start, m_end in merged_after:
                conn.execute(
                    """INSERT INTO reading_loop_read_ranges(
                        project_id, paper_id, sha, page_number, start_offset, end_offset
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (pid, paper_id, sha, page_number, m_start, m_end)
                )

        return new_page, new_chars

    def get_read_ranges(self, pid: str, paper_id: str, sha: str, page_number: int) -> List[Tuple[int, int]]:
        pid = project_id(pid)
        with self.connection(False) as conn:
            if conn is None:
                return []
            rows = conn.execute(
                """SELECT start_offset, end_offset FROM reading_loop_read_ranges
                   WHERE project_id=? AND paper_id=? AND sha=? AND page_number=?
                   ORDER BY start_offset""",
                (pid, paper_id, sha, page_number)
            ).fetchall()
            return [(r["start_offset"], r["end_offset"]) for r in rows]

    def save_review(
        self,
        pid: str,
        review_data: Dict[str, Any],
        context_fingerprint: str,
        origin: str,
        loop_revision: int,
        round_index: int,
        review_id: Optional[str] = None
    ) -> str:
        pid = project_id(pid)
        rid = review_id or generate_review_id()
        now = utc_now()
        data_str = budget_json(review_data)

        with self.connection(True) as conn:
            conn.execute(
                """INSERT INTO reading_loop_reviews(
                    review_id, project_id, loop_revision, round_index,
                    review_data, context_fingerprint, origin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, pid, loop_revision, round_index, data_str, context_fingerprint, origin, now)
            )
        return rid

    def save_round(self, pid: str, round_index: int, goal: str, status: str, round_data: Dict[str, Any], started_at: Optional[str] = None, ended_at: Optional[str] = None):
        pid = project_id(pid)
        now = utc_now()
        started = started_at or now
        d_str = budget_json(round_data)

        with self.connection(True) as conn:
            conn.execute(
                """INSERT INTO reading_loop_rounds(
                    project_id, round_index, goal, status, started_at, ended_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, round_index) DO UPDATE SET
                    goal=excluded.goal, status=excluded.status, ended_at=excluded.ended_at, data=excluded.data""",
                (pid, round_index, goal, status, started, ended_at, d_str)
            )

    def save_action(
        self,
        pid: str,
        action_id: str,
        round_index: int,
        kind: str,
        status: str,
        payload: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        reserved_cost: Optional[Dict[str, Any]] = None,
        settled_cost: Optional[Dict[str, Any]] = None
    ):
        pid = project_id(pid)
        now = utc_now()
        payload_str = budget_json(payload)
        res_str = budget_json(result) if result is not None else None
        res_cost_str = budget_json(reserved_cost) if reserved_cost is not None else None
        set_cost_str = budget_json(settled_cost) if settled_cost is not None else None

        with self.connection(True) as conn:
            conn.execute(
                """INSERT INTO reading_loop_actions(
                    action_id, project_id, round_index, kind, status,
                    payload, result, reserved_cost, settled_cost,
                    lease_token, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    status=excluded.status, result=excluded.result,
                    reserved_cost=coalesce(excluded.reserved_cost, reading_loop_actions.reserved_cost),
                    settled_cost=excluded.settled_cost, updated_at=excluded.updated_at""",
                (action_id, pid, round_index, kind, status, payload_str, res_str, res_cost_str, set_cost_str, now, now)
            )

    def get_action(self, pid: str, action_id: str) -> Optional[Dict[str, Any]]:
        pid = project_id(pid)
        with self.connection(False) as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM reading_loop_actions WHERE project_id=? AND action_id=?",
                (pid, action_id)
            ).fetchone()
            if not row:
                return None
            return {
                "action_id": row["action_id"],
                "project_id": row["project_id"],
                "round_index": row["round_index"],
                "kind": row["kind"],
                "status": row["status"],
                "payload": json.loads(row["payload"]) if row["payload"] else {},
                "result": json.loads(row["result"]) if row["result"] else None,
                "reserved_cost": json.loads(row["reserved_cost"]) if row["reserved_cost"] else {},
                "settled_cost": json.loads(row["settled_cost"]) if row["settled_cost"] else {},
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }

    def record_model_ticket(self, pid: str, action_id: str, loop_revision: int) -> str:
        pid = project_id(pid)
        tid = generate_ticket_id()
        now = utc_now()
        with self.connection(True) as conn:
            conn.execute(
                """INSERT INTO reading_loop_model_tickets(
                    ticket_id, project_id, action_id, loop_revision, status, created_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?)""",
                (tid, pid, action_id, loop_revision, now)
            )
        return tid

    def release_unissued_model_ticket(self, pid: str, action_id: str, ticket_id: str) -> bool:
        """Release only a reservation known not to have reached the provider.

        The ticket and reserved counter change in the same immediate transaction.
        Already released tickets are idempotent; settled requests cannot be refunded.
        """
        pid = project_id(pid)
        with self.connection(True) as conn:
            ticket = conn.execute(
                "SELECT status FROM reading_loop_model_tickets WHERE ticket_id=? AND project_id=? AND action_id=?",
                (ticket_id, pid, action_id),
            ).fetchone()
            if ticket is None:
                raise JournalError("TICKET_NOT_FOUND", "模型凭证不存在或不属于当前项目动作")
            if ticket["status"] == "released_unissued":
                return False
            if ticket["status"] != "reserved":
                raise JournalError("TICKET_ALREADY_SETTLED", "已请求并结算的模型调用不能退回预算")
            row = conn.execute(
                "SELECT loop_revision, reserved FROM reading_loops WHERE project_id=?", (pid,),
            ).fetchone()
            if row is None:
                raise JournalError("LOOP_NOT_FOUND", "自适应阅读循环不存在")
            reserved = json.loads(row["reserved"])
            if type(reserved.get("gui_model_calls")) is not int or reserved["gui_model_calls"] < 1:
                raise JournalError("NEEDS_RECONCILIATION", "预留计数与模型凭证不一致，请核对后处理")
            saved = conn.execute(
                "SELECT snapshot FROM reading_loop_revisions WHERE project_id=? AND loop_revision=?",
                (pid, row["loop_revision"]),
            ).fetchone()
            if saved is None:
                raise JournalError("NEEDS_RECONCILIATION", "预留对应循环快照缺失，请核对后处理")
            reserved["gui_model_calls"] -= 1
            revision, now = row["loop_revision"] + 1, utc_now()
            snapshot = json.loads(saved["snapshot"])
            snapshot.update(loop_revision=revision, reserved=reserved)
            conn.execute("UPDATE reading_loops SET reserved=?, loop_revision=?, updated_at=? WHERE project_id=?",
                         (budget_json(reserved), revision, now, pid))
            conn.execute("UPDATE reading_loop_model_tickets SET status='released_unissued' WHERE ticket_id=?", (ticket_id,))
            conn.execute(
                "INSERT INTO reading_loop_revisions(project_id, loop_revision, snapshot, change_note, created_at) VALUES (?,?,?,?,?)",
                (pid, revision, budget_json(snapshot), "释放确定未请求模型的预留，不计调用次数", now),
            )
        return True

    def settle_model_ticket(self, pid: str, action_id: str, ticket_id: str, success: bool):
        pid = project_id(pid)
        status = "settled_success" if success else "settled_failed"
        with self.connection(True) as conn:
            row = conn.execute(
                "SELECT status FROM reading_loop_model_tickets WHERE ticket_id=? AND project_id=? AND action_id=?",
                (ticket_id, pid, action_id)
            ).fetchone()
            if row is None:
                raise JournalError("TICKET_NOT_FOUND", "模型凭证不存在或不属于当前项目动作")
            if row["status"] != "reserved":
                raise JournalError("TICKET_ALREADY_SETTLED", "该模型凭证已被结算，不得重复使用")
            conn.execute(
                "UPDATE reading_loop_model_tickets SET status=? WHERE ticket_id=?",
                (status, ticket_id)
            )
