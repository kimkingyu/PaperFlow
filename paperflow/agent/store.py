"""Append-only events and durable intents; deliberately not an exactly-once ledger."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .security import AgentError, canonical


class AgentStore:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "native_agent.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            request_id TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(session_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
            run_id TEXT NOT NULL REFERENCES runs(id), body TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, type TEXT NOT NULL,
            data TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(run_id, seq)
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), binding TEXT NOT NULL,
            state TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL, arguments_hash TEXT NOT NULL, status TEXT NOT NULL,
            mutates INTEGER NOT NULL, result TEXT, created_at REAL NOT NULL, UNIQUE(run_id, call_id)
        );
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def create_session(self, workspace_id: str, title: str) -> dict:
        session = {"id": uuid.uuid4().hex, "workspace_id": workspace_id, "title": title, "created_at": time.time()}
        with self.transaction():
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", tuple(session.values()))
        return session

    def list_sessions(self, workspace_id: Optional[str] = None) -> list:
        with self.lock:
            if workspace_id is None:
                rows = self.db.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM sessions WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise AgentError("SESSION_NOT_FOUND", "会话不存在")
            result = dict(row)
            result["messages"] = self.messages(session_id)
            result["runs"] = [json.loads(row[0]) for row in self.db.execute("SELECT state FROM runs WHERE session_id=? ORDER BY rowid", (session_id,))]
            return result

    def messages(self, session_id: str) -> list:
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM messages WHERE session_id=? ORDER BY seq", (session_id,))]

    def model_messages(self, session_id: str, run_id: str) -> list:
        # A failed/cancelled run can end halfway through a tool batch. Its unmatched
        # calls must not be sent as executable conversation state in a later run.
        with self.lock:
            rows = self.db.execute("SELECT m.run_id,m.body,r.state FROM messages m JOIN runs r ON r.id=m.run_id WHERE m.session_id=? ORDER BY m.seq", (session_id,)).fetchall()
            result = []
            for row in rows:
                if row[0] != run_id and json.loads(row[2])["status"] != "completed":
                    continue
                message = json.loads(row[1])
                if row[0] != run_id:
                    message.pop("_protocol_turn", None)
                result.append(message)
            return result

    def add_message(self, session_id: str, run_id: str, message: dict) -> None:
        self.db.execute("INSERT INTO messages(session_id,run_id,body,created_at) VALUES (?,?,?,?)",
                        (session_id, run_id, canonical(message), time.time()))

    def find_request(self, session_id: str, request_id: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT state FROM runs WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
            return json.loads(row[0]) if row else None

    def create_run(self, state: dict, message: dict) -> dict:
        with self.transaction():
            existing = self.find_request(state["session_id"], state["request_id"])
            if existing:
                return existing
            rows = self.db.execute("SELECT state FROM runs WHERE session_id=?", (state["session_id"],)).fetchall()
            for row in rows:
                run = json.loads(row[0])
                if run["status"] in {"queued", "running", "awaiting_approval", "paused", "interrupted"}:
                    raise AgentError("SESSION_BUSY", "该会话已有未结束任务，请先停止或恢复")
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?)", (state["id"], state["session_id"], state["request_id"], canonical(state)))
            self.add_message(state["session_id"], state["id"], message)
            self.event(state["id"], "status", {"status": "queued"})
        return state

    def get_run(self, run_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise AgentError("RUN_NOT_FOUND", "运行不存在")
            return json.loads(row[0])

    def save_run(self, state: dict) -> None:
        self.db.execute("UPDATE runs SET state=? WHERE id=?", (canonical(state), state["id"]))

    def event(self, run_id: str, kind: str, data: dict) -> dict:
        row = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()
        event = {"seq": row[0], "type": kind, "data": data, "created_at": time.time()}
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (run_id, event["seq"], kind, canonical(data), event["created_at"]))
        return event

    def events(self, run_id: str, after: int) -> list:
        with self.lock:
            self.get_run(run_id)
            return [{"seq": row[0], "type": row[1], "data": json.loads(row[2]), "created_at": row[3]}
                    for row in self.db.execute("SELECT seq,type,data,created_at FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after))]

    def approval(self, approval_id: str) -> dict:
        row = self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise AgentError("APPROVAL_NOT_FOUND", "审批不存在")
        result = dict(row)
        result["binding"] = json.loads(result["binding"])
        return result

    def intent(self, state: dict, call: dict, arguments_hash: str, mutates: bool) -> None:
        self.db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, state["id"], call["id"], call["name"], arguments_hash,
                         "intent", int(mutates), None, time.time()))

    def action(self, run_id: str, call_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM actions WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
        return dict(row) if row else None

    def settle(self, state: dict, call: dict, result: dict, ok: bool) -> None:
        self.db.execute("UPDATE actions SET status='settled',result=? WHERE run_id=? AND call_id=?",
                        (canonical(result), state["id"], call["id"]))
        self.add_message(state["session_id"], state["id"], {"role": "tool", "call_id": call["id"],
                         "tool_name": call["name"], "result": result, "ok": ok})

    def recover(self) -> list:
        recovered = []
        with self.transaction():
            rows = self.db.execute("SELECT state FROM runs").fetchall()
            for row in rows:
                state = json.loads(row[0])
                protocol_lost = state.get("_requires_continuation", False)
                if state["status"] not in {"running", "queued"} and not (protocol_lost and state["status"] in {"awaiting_approval", "paused"}):
                    continue
                unknown = self.db.execute("SELECT id FROM actions WHERE run_id=? AND status='intent'", (state["id"],)).fetchone()
                state["status"] = "interrupted"
                state["needs_reconciliation"] = bool(unknown)
                state["error"] = {"code": "NEEDS_RECONCILIATION" if unknown else "PROCESS_INTERRUPTED",
                                  "message": "副作用完成状态未知，必须人工核对；不会自动重发" if unknown else "进程中断，未自动恢复模型或工具调用"}
                if protocol_lost and not unknown:
                    state["error"] = {"code": "PROTOCOL_CONTINUATION_LOST", "message": "原进程协议续接已丢失；未重放工具，请新建任务"}
                    state["pending_approval"] = None
                    self.db.execute("UPDATE approvals SET state='invalidated' WHERE run_id=? AND state='pending'", (state["id"],))
                state["_active_started"] = None
                self.save_run(state)
                self.event(state["id"], "status", {"status": "interrupted", "error": state["error"]})
                recovered.append(state["id"])
        return recovered

    def close(self):
        with self.lock:
            self.db.close()
