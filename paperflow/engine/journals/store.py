"""Versioned SQLite observations. Importing and reading an empty store are inert."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import identity_id, normalize_issn, normalize_name
from .models import JournalError, JournalRecord, ParsedBatch, SchoolPolicy, utc_now

FACTS = {"rankings": "rankings", "metrics": "metrics", "risks": "risk_events", "experiences": "experiences"}
SCHEMA_VERSION = 1


def default_data_dir() -> Path:
    override = os.environ.get("PAPERFLOW_JOURNAL_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "PaperFlow" / "journals") if base else Path.home() / ".paperflow" / "journals"


def serialized(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(serialized(value).encode("utf-8")).hexdigest()


class JournalStore:
    def __init__(self, data_dir: Optional[str] = None):
        self.directory = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        self.path = self.directory / "journals.sqlite3"

    @contextmanager
    def connection(self, write: bool = False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5)
        else:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise JournalError("SCHEMA_CHANGED", "期刊数据库版本不兼容，请备份后使用匹配版本")
            if write:
                conn.execute("PRAGMA journal_mode=WAL")
                if version == 0:
                    self._initialize(conn)
            elif version == 0:
                raise JournalError("SCHEMA_CHANGED", "数据库尚无有效期刊 schema")
            if not write:
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _initialize(conn):
        conn.executescript("""
            CREATE TABLE sources(source_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE snapshots(snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL, version TEXT NOT NULL, checksum TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, active INTEGER NOT NULL, row_count INTEGER NOT NULL,
                coverage TEXT NOT NULL, filename TEXT NOT NULL, data_year INTEGER);
            CREATE INDEX snapshot_scope ON snapshots(source_id,dataset_id,active);
            CREATE TABLE journals(journal_id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL);
            CREATE TABLE identifiers(issn TEXT PRIMARY KEY,journal_id TEXT NOT NULL REFERENCES journals);
            CREATE TABLE aliases(journal_id TEXT NOT NULL REFERENCES journals,alias TEXT NOT NULL,
                PRIMARY KEY(journal_id,alias));
            CREATE TABLE journal_observations(journal_id TEXT NOT NULL REFERENCES journals,
                snapshot_id TEXT NOT NULL REFERENCES snapshots,payload TEXT NOT NULL,record_key TEXT NOT NULL,
                PRIMARY KEY(journal_id,snapshot_id,record_key));
            CREATE TABLE import_runs(run_id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,report TEXT NOT NULL);
            CREATE TABLE policies(profile_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)
        for table in FACTS.values():
            conn.execute(f"CREATE TABLE {table}(journal_id TEXT NOT NULL REFERENCES journals,"
                         "snapshot_id TEXT NOT NULL REFERENCES snapshots,record_key TEXT NOT NULL,payload TEXT NOT NULL,"
                         "PRIMARY KEY(journal_id,snapshot_id,record_key))")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def snapshots(self, _connection=None) -> List[Dict[str, Any]]:
        with (nullcontext(_connection) if _connection is not None else self.connection()) as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT * FROM snapshots ORDER BY retrieved_at, snapshot_id").fetchall()
            return [dict(row, coverage=json.loads(row["coverage"])) for row in rows]

    def coverage(self, _connection=None) -> Dict[str, Any]:
        snapshots = [s for s in self.snapshots(_connection=_connection) if s["active"]]
        years = set()
        for s in snapshots:
            for item in [s["coverage"]] + s["coverage"].get("datasets", []):
                if item.get("system") == "cas_warning" and item.get("year"):
                    years.add(item["year"])
        return {"snapshots": snapshots, "warning_years": sorted(years), "live_verified": False}

    def save_policy(self, policy: SchoolPolicy, dry_run: bool = True):
        for issn in policy.prohibited_issns:
            normalize_issn(issn)
        result = {"profile_id": policy.profile_id, "dry_run": dry_run, "policy": policy.model_dump(mode="json")}
        if not dry_run:
            with self.connection(write=True) as conn:
                conn.execute("INSERT OR REPLACE INTO policies VALUES(?,?)", (policy.profile_id, serialized(policy)))
        return result

    def get_policy(self, profile_id: Optional[str], _connection=None) -> Optional[SchoolPolicy]:
        if not profile_id:
            return None
        with (nullcontext(_connection) if _connection is not None else self.connection()) as conn:
            row = None if conn is None else conn.execute("SELECT payload FROM policies WHERE profile_id=?", (profile_id,)).fetchone()
            if row is None:
                raise JournalError("INSUFFICIENT_EVIDENCE", "未找到指定学校规则，请先导入；不会默认套用其他学校")
            return SchoolPolicy.model_validate_json(row[0])

    def read_view(self, include_history: bool = False, profile_id: Optional[str] = None):
        """Read observations, coverage and policy from one consistent SQLite snapshot."""
        with self.connection() as conn:
            if conn is None:
                if profile_id:
                    raise JournalError("INSUFFICIENT_EVIDENCE", "未找到指定学校规则，请先导入")
                return [], {"snapshots": [], "warning_years": [], "live_verified": False}, None
            records = self.records(include_history=include_history, _connection=conn)
            coverage = self.coverage(_connection=conn)
            policy = self.get_policy(profile_id, _connection=conn)
            return records, coverage, policy

    def import_batch(self, batch: ParsedBatch, source_id: str, dataset_id: str,
                     version: str, checksum: str, filename: str, data_year: Optional[int] = None,
                     dry_run: bool = True, allow_shrink: bool = False, source_metadata=None):
        if not batch.records:
            raise JournalError("SCHEMA_CHANGED", "没有有效记录，旧快照保持不变")
        if len(batch.records) > 100000:
            raise JournalError("INVALID_INPUT", "单批最多 100000 条记录")
        for record in batch.records:
            record.issns = sorted({normalize_issn(i) for i in record.issns})
        snapshot_id = digest([source_id, dataset_id, version, checksum, data_year])
        snapshots = self.snapshots()
        previous = next((s for s in reversed(snapshots) if s["source_id"] == source_id and s["dataset_id"] == dataset_id and s["active"]), None)
        already = next((s for s in snapshots if s["snapshot_id"] == snapshot_id and s["active"]), None)
        if previous and not already and len(batch.records) < previous["row_count"] * 0.5 and not allow_shrink:
            raise JournalError("SCHEMA_CHANGED", "有效行数减少超过 50%，拒绝覆盖；请核查数据后显式允许缩减")
        report = {"snapshot_id": snapshot_id, "source_id": source_id, "dataset_id": dataset_id,
                  "version": version, "checksum": checksum, "dry_run": dry_run,
                  "total_rows": batch.total_rows, "accepted": len(batch.records),
                  "rejected": batch.rejected_count, "rejected_samples": batch.rejected[:20],
                  "warnings": batch.warnings, "coverage": batch.coverage,
                  "added": 0, "updated": 0, "unchanged": 0, "ambiguous": 0,
                  "previous_snapshot_id": previous["snapshot_id"] if previous else None}
        if already:
            report["unchanged"] = len(batch.records)
            return report
        existing = self.records()
        existing_ids = {r.journal_id for r in existing}
        known = {i: r.journal_id for r in existing for i in r.issns}
        assignments = []
        for record in batch.records:
            matched = {known[i] for i in record.issns if i in known}
            if len(matched) > 1:
                raise JournalError("AMBIGUOUS_JOURNAL", "一行 ISSN 指向多个已有期刊，拒绝合并，原数据未修改")
            jid = next(iter(matched)) if matched else identity_id(record.title, record.kind, record.issns)
            assignments.append((jid, record))
            for i in record.issns:
                known[i] = jid
            report["updated" if jid in existing_ids else "added"] += 1
            existing_ids.add(jid)
            if not record.issns:
                report["ambiguous"] += 1
        if dry_run:
            return report
        # The write lock and version check prevent two refreshes from racing.
        with self.connection(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT snapshot_id FROM snapshots WHERE source_id=? AND dataset_id=? AND active=1",
                                   (source_id, dataset_id)).fetchone()
            if (current[0] if current else None) != report["previous_snapshot_id"]:
                raise JournalError("SOURCE_UNAVAILABLE", "该来源刚被其他操作更新，请重新预览")
            conn.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source_id, serialized(source_metadata or {})))
            conn.execute("UPDATE snapshots SET active=0 WHERE source_id=? AND dataset_id=?", (source_id, dataset_id))
            # An explicitly re-imported historical snapshot can become active again without duplicating facts.
            conn.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (snapshot_id, source_id, dataset_id, version, checksum, utc_now(), 1,
                          len(batch.records), serialized(batch.coverage), Path(filename).name, data_year))
            conn.execute("UPDATE snapshots SET active=1 WHERE snapshot_id=?", (snapshot_id,))
            for jid, record in assignments:
                conn.execute("INSERT OR IGNORE INTO journals VALUES(?,?,?)", (jid, record.title, record.kind))
                for issn in record.issns:
                    old = conn.execute("SELECT journal_id FROM identifiers WHERE issn=?", (issn,)).fetchone()
                    if old and old[0] != jid:
                        raise JournalError("AMBIGUOUS_JOURNAL", "ISSN 关联冲突，导入已回滚")
                    conn.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)", (issn, jid))
                for alias in [record.title, record.title_zh] + record.aliases:
                    if alias:
                        conn.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (jid, normalize_name(alias)))
                payload = record.model_dump(mode="json", exclude=set(FACTS))
                payload["journal_id"] = jid
                conn.execute("INSERT OR IGNORE INTO journal_observations VALUES(?,?,?,?)", (jid, snapshot_id, serialized(payload), digest(payload)))
                for attr, table in FACTS.items():
                    for fact in getattr(record, attr):
                        value = fact.model_dump(mode="json")
                        conn.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?,?,?)", (jid, snapshot_id, digest(value), serialized(value)))
            conn.execute("INSERT INTO import_runs(created_at,report) VALUES(?,?)", (utc_now(), serialized(report)))
        return report

    def records(self, include_history: bool = False, _connection=None) -> List[JournalRecord]:
        with (nullcontext(_connection) if _connection is not None else self.connection()) as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT o.*,s.active FROM journal_observations o JOIN snapshots s USING(snapshot_id) "
                                + ("" if include_history else "WHERE s.active=1 ") + "ORDER BY s.retrieved_at,o.rowid").fetchall()
            grouped: Dict[str, JournalRecord] = {}
            for row in rows:
                payload = json.loads(row["payload"])
                jid = row["journal_id"]
                record = JournalRecord.model_validate(payload)
                observation = {k: payload[k] for k in ("title", "publisher", "indexing", "oa_mode", "provenance")}
                observation["active"] = bool(row["active"])
                if jid not in grouped:
                    grouped[jid] = record
                    record.metadata_observations = [observation]
                else:
                    self._merge(grouped[jid], record)
                    grouped[jid].metadata_observations.append(observation)
            # Keep disappeared risk-bearing journals searchable as history, never as current clearance.
            historical_rows = conn.execute(
                "SELECT o.* FROM journal_observations o WHERE o.journal_id IN "
                "(SELECT DISTINCT journal_id FROM risk_events) ORDER BY o.rowid DESC"
            ).fetchall()
            for row in historical_rows:
                if row["journal_id"] not in grouped:
                    payload = json.loads(row["payload"])
                    payload["indexing"] = []
                    payload["oa_mode"] = "unknown"
                    payload["provenance"]["freshness"] = "historical"
                    record = JournalRecord.model_validate(payload)
                    record.identity_warnings.append("仅有历史来源记录，当前快照未覆盖该刊")
                    record.metadata_observations = [{"active": False, "provenance": payload["provenance"]}]
                    grouped[row["journal_id"]] = record
            for attr, table in FACTS.items():
                fact_rows = conn.execute(f"SELECT f.*,s.active FROM {table} f JOIN snapshots s USING(snapshot_id) "
                                         + ("" if include_history or attr == "risks" else "WHERE s.active=1 ")).fetchall()
                field_type = {"rankings": "Ranking", "metrics": "Metric", "risks": "RiskEvent", "experiences": "Experience"}[attr]
                from . import models
                for row in fact_rows:
                    if row["journal_id"] not in grouped:
                        continue
                    value = json.loads(row["payload"])
                    if not row["active"]:
                        value["provenance"]["freshness"] = "superseded"
                        if attr == "risks":
                            value["historical"] = True
                    fact = getattr(models, field_type).model_validate(value)
                    if fact not in getattr(grouped[row["journal_id"]], attr):
                        getattr(grouped[row["journal_id"]], attr).append(fact)
        # Name-only facts are associated dynamically; a later homonym cannot silently inherit them.
        strong = {}
        for record in grouped.values():
            if record.issns:
                for name in [record.title, record.title_zh] + record.aliases:
                    if name:
                        strong.setdefault((normalize_name(name), record.kind), set()).add(record.journal_id)
        remove = []
        for jid, record in list(grouped.items()):
            if record.issns:
                continue
            targets = strong.get((normalize_name(record.title), record.kind), set())
            if len(targets) == 1:
                self._merge(grouped[next(iter(targets))], record)
                remove.append(jid)
            elif len(targets) > 1:
                note = "同名存在多个 ISSN，名称来源事实无法确定关联对象"
                record.identity_warnings.append(note)
                for target in targets:
                    grouped[target].identity_warnings.append(note)
            else:
                record.identity_warnings.append("仅名称身份，缺少有效 ISSN，请核对具体刊物")
        for jid in remove:
            del grouped[jid]
        return sorted(grouped.values(), key=lambda r: (normalize_name(r.title), r.journal_id))

    @staticmethod
    def _merge(target: JournalRecord, incoming: JournalRecord):
        if incoming.title != target.title and incoming.title not in target.aliases:
            target.aliases.append(incoming.title)
        for attr in ("issns", "aliases", "fields", "rankings", "metrics", "risks", "experiences", "editorial_profiles", "publication_fees", "identity_warnings", "metadata_observations"):
            for value in getattr(incoming, attr):
                if value not in getattr(target, attr):
                    getattr(target, attr).append(value)
        # Contradictory metadata remains visible and is not collapsed into a favourable union.
        if target.indexing and incoming.indexing and set(target.indexing) != set(incoming.indexing):
            target.identity_warnings.append("来源收录集合存在差异，需按年份与官方状态复核")
        elif not target.indexing:
            target.indexing = incoming.indexing[:]
        if incoming.oa_mode != "unknown":
            if target.oa_mode not in ("unknown", incoming.oa_mode):
                target.oa_mode = "unknown"
                target.identity_warnings.append("来源 OA 模式冲突，不能据此确认费用或出版方式")
            elif not any("OA 模式冲突" in n for n in target.identity_warnings):
                target.oa_mode = incoming.oa_mode
        if not target.title_zh:
            target.title_zh = incoming.title_zh
        if not target.publisher:
            target.publisher = incoming.publisher
        if not target.homepage:
            target.homepage = incoming.homepage
