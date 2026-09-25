"""Local database builder, candidate loaders, and coverage analyzers for journals.

No network requests, source isolation, idempotent snapshots, and safe local evaluation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from paperflow.engine.journals.identity import identity_id, normalize_issn, normalize_name, normalize_indexing
from paperflow.engine.journals.importers import MAX_FILE_SIZE_BYTES, parse_file
from paperflow.engine.journals.models import (
    JournalError,
    JournalRecord,
    ParsedBatch,
    Provenance,
    PublicationFee,
    utc_now,
)
from paperflow.engine.journals.store import JournalStore


def _find_resource_path(filename: str) -> Optional[Path]:
    """Safely locate a bundled resource file if present on disk."""
    try:
        traversable = resources.files("paperflow").joinpath("resources", "journals", filename)
        candidate = Path(str(traversable))
        if candidate.is_file():
            return candidate
    except Exception:
        pass

    fallback = Path(__file__).resolve().parent.parent.parent / "resources" / "journals" / filename
    if fallback.is_file():
        return fallback
    return None


def _check_safe_local_path(path_str: str) -> Path:
    """Validate that path is strictly a safe local file, never a network URL, and within size limit."""
    trimmed = path_str.strip()
    lower = trimmed.lower()
    if lower.startswith(("http://", "https://", "ftp://", "sftp://", "file://", "smb://", "\\\\")) or "://" in lower:
        raise JournalError("INVALID_INPUT", f"禁止将本地路径输入变成网络下载或远程调用: {path_str}")

    path = Path(trimmed).expanduser().resolve()
    if not path.is_file():
        raise JournalError("SOURCE_UNAVAILABLE", f"指定的文件不存在: {path_str}")

    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise JournalError("SCHEMA_CHANGED", f"文件超出最大允许大小 32 MiB: {path_str} ({size} 字节)")

    return path


def _sanitize_for_hash(val: Any) -> Any:
    """Recursively clean dumped data by stripping dynamic `retrieved_at` and derived `journal_id`.

    Retains observed_at, source_version, authority, freshness, editorial_profiles, fee taxes/notes,
    metric stages/comparators, ensuring true factual changes produce distinct hashes.
    """
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            if k in {"retrieved_at", "journal_id"}:
                continue
            cleaned[k] = _sanitize_for_hash(v)
        return cleaned
    elif isinstance(val, (list, tuple, set)):
        cleaned_items = [_sanitize_for_hash(item) for item in val]
        try:
            return sorted(cleaned_items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        except Exception:
            return cleaned_items
    return val


def compute_stable_content_hash(records: List[JournalRecord]) -> str:
    """Compute deterministic SHA-256 hash using complete model_dump of all records,

    recursively stripping only dynamic runtime `retrieved_at` and derived `journal_id`.
    Ensures that factual evidence changes—such as actual `observed_at` updates, editorial_profiles,
    fee taxes_included/note, metric stage/comparator—properly produce a new snapshot hash,
    while run-time retrieved_at variations are ignored.
    """
    cleaned_records = []
    for r in records:
        dumped = r.model_dump(mode="json")
        cleaned = _sanitize_for_hash(dumped)
        cleaned_records.append(cleaned)

    cleaned_records.sort(key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    serialized_str = json.dumps(cleaned_records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized_str.encode("utf-8")).hexdigest()


def load_curated_candidates() -> List[JournalRecord]:
    """Load curated candidate journals from curated_candidates.json.

    Supports both raw array `[Record, ...]` and envelope `{"records": [Record, ...]}`.
    Fails explicitly with CURATED_DATA_UNAVAILABLE when missing; never fabricates data.
    """
    res_path = _find_resource_path("curated_candidates.json")
    if res_path is None or not res_path.exists():
        raise JournalError("CURATED_DATA_UNAVAILABLE", "未找到精选期刊候选包资源 curated_candidates.json，绝不编造假数据")

    if res_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise JournalError("SCHEMA_CHANGED", "精选包文件超出最大大小限制 32 MiB")

    try:
        with open(res_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        raise JournalError("SCHEMA_CHANGED", f"读取精选包 JSON 失败: {e}")

    raw_items: List[Any] = []
    if isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        if "records" in raw and isinstance(raw["records"], list):
            raw_items = raw["records"]
        else:
            raw_items = [raw]
    else:
        raise JournalError("SCHEMA_CHANGED", "精选包必须为记录数组或包含 records 的对象")

    records: List[JournalRecord] = []
    for item in raw_items:
        if isinstance(item, JournalRecord):
            rec = item
        elif isinstance(item, dict):
            rec = JournalRecord.model_validate(item)
        else:
            continue
        if rec.kind == "journal":
            records.append(rec)

    return records


def load_open_candidates() -> List[JournalRecord]:
    """Load open metadata candidate journals (e.g. DOAJ CC0 baseline) from open_metadata.json.

    Returns empty list if the optional baseline file is missing; does not fabricate data.
    """
    res_path = _find_resource_path("open_metadata.json")
    if res_path is None or not res_path.exists():
        return []

    if res_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise JournalError("SCHEMA_CHANGED", "open_metadata 文件超出最大大小限制 32 MiB")

    try:
        with open(res_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        raise JournalError("SCHEMA_CHANGED", f"读取 open_metadata JSON 失败: {e}")

    raw_items: List[Any] = []
    if isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        if "records" in raw and isinstance(raw["records"], list):
            raw_items = raw["records"]
        else:
            raw_items = [raw]
    else:
        raise JournalError("SCHEMA_CHANGED", "open_metadata 必须为记录数组或包含 records 的对象")

    records: List[JournalRecord] = []
    for item in raw_items:
        if isinstance(item, JournalRecord):
            rec = item
        elif isinstance(item, dict):
            rec = JournalRecord.model_validate(item)
        else:
            continue
        if rec.kind == "journal":
            records.append(rec)

    return records


def _load_source_audit() -> Optional[Dict[str, Any]]:
    """Safely inspect source_audit.json if present; purely read-only, never modifies it."""
    path = _find_resource_path("source_audit.json")
    if path is None or not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            return None
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def deduplicate_and_filter_records(
    raw_records: List[JournalRecord],
) -> Tuple[List[JournalRecord], List[Dict[str, Any]], int]:
    """Deduplicate records by ISSN first with auxiliary alias matching, exclude conferences,

    and collect granular rejection details.
    """
    rejected: List[Dict[str, Any]] = []
    rejected_count = 0

    # 1. Filter out conference records
    journal_records: List[JournalRecord] = []
    for idx, r in enumerate(raw_records, start=1):
        if r.kind != "journal":
            rejected_count += 1
            rejected.append({
                "line": idx,
                "error_code": "CONFERENCE_EXCLUDED",
                "field": "kind",
                "title": r.title,
                "reason": "不把会议算作期刊，已排除",
            })
            continue
        journal_records.append(r)

    # 2. ISSN priority grouping and alias auxiliary matching
    by_issn: Dict[str, JournalRecord] = {}
    name_to_issn_record: Dict[str, JournalRecord] = {}
    no_issn_records: List[JournalRecord] = []
    accepted: List[JournalRecord] = []

    def _register_names(target_record: JournalRecord) -> None:
        for name in [target_record.title, target_record.title_zh] + target_record.aliases:
            if name:
                norm = normalize_name(name)
                name_to_issn_record[norm] = target_record

    for idx, r in enumerate(journal_records, start=1):
        norm_issns: List[str] = []
        has_invalid_issn = False
        for raw_issn in r.issns:
            try:
                norm_issns.append(normalize_issn(raw_issn))
            except Exception:
                has_invalid_issn = True
                rejected_count += 1
                rejected.append({
                    "line": idx,
                    "error_code": "INVALID_ISSN",
                    "field": "issns",
                    "title": r.title,
                    "raw_issn": raw_issn,
                    "reason": "ISSN 格式或校验位无效",
                })
                break

        if has_invalid_issn:
            continue

        norm_issns = sorted(set(norm_issns))
        r.issns = norm_issns

        matched_target: Optional[JournalRecord] = None
        for i in norm_issns:
            if i in by_issn:
                matched_target = by_issn[i]
                break

        if matched_target is not None:
            JournalStore._merge(matched_target, r)
            for i in norm_issns:
                by_issn[i] = matched_target
            if r.title != matched_target.title and r.title not in matched_target.aliases:
                matched_target.aliases.append(r.title)
            _register_names(matched_target)
        elif norm_issns:
            for i in norm_issns:
                by_issn[i] = r
            accepted.append(r)
            _register_names(r)
        else:
            no_issn_records.append(r)

    # 3. Auxiliary alias matching for records without ISSN
    for r in no_issn_records:
        norm_name = normalize_name(r.title)
        if norm_name in name_to_issn_record:
            target = name_to_issn_record[norm_name]
            JournalStore._merge(target, r)
            _register_names(target)
        else:
            r.identity_warnings.append("缺少有效 ISSN，仅名称身份")
            accepted.append(r)

    return accepted, rejected, rejected_count


def _is_evidence_expired(prov: Provenance, ref_time: datetime) -> bool:
    """Check if a provenance observation is expired or stale."""
    if prov.freshness in {"expired", "stale", "superseded", "historical"}:
        return True
    ts_str = prov.observed_at or prov.retrieved_at
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (ref_time - dt).total_seconds() > 365 * 86400:
                return True
        except Exception:
            pass
    return False


def summarize_coverage(
    records: List[JournalRecord],
    now: Optional[datetime] = None,
    rejected_count: int = 0,
) -> Dict[str, Any]:
    """Pure summary function computing exact coverage metrics from journal records.

    Strict rules:
    - Never sum amounts across different currencies;
    - Missing fee amount is never zero;
    - Conference is excluded from journal counts;
    - Inspects freshness of provenance facts;
    - Reports SCIE/SCI, EI, dual-indexed counts and ratios.
    """
    ref_time = now or datetime.now(timezone.utc)
    if isinstance(ref_time, str):
        ref_time = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))

    total = len(records)
    if total == 0:
        return {
            "total_journals": 0,
            "scie_sci_count": 0,
            "sci_count": 0,
            "scie_count": 0,
            "ei_count": 0,
            "dual_indexed_count": 0,
            "known_price_count": 0,
            "price_coverage": 0.0,
            "known_duration_count": 0,
            "duration_coverage": 0.0,
            "known_experience_count": 0,
            "experience_coverage": 0.0,
            "unknown_counts": {
                "price": 0,
                "duration": 0,
                "experience": 0,
                "oa_mode": 0,
            },
            "expired_count": 0,
            "conflict_count": 0,
            "warning_count": 0,
            "rejected_count": rejected_count,
            "currencies": {},
        }

    sci_count = 0
    scie_count = 0
    ei_count = 0
    dual_count = 0

    known_price_count = 0
    known_duration_count = 0
    known_experience_count = 0

    expired_count = 0
    conflict_count = 0
    warning_count = 0
    unknown_oa_count = 0

    currency_counts: Dict[str, int] = {}

    for r in records:
        # Indexing
        idxs = {normalize_indexing(idx) for idx in r.indexing}
        has_scie = "SCIE" in idxs
        has_sci = has_scie
        has_ei = "EI" in idxs

        if has_scie:
            scie_count += 1
        if has_sci:
            sci_count += 1
        if has_ei:
            ei_count += 1
        if has_sci and has_ei:
            dual_count += 1

        # Price / Fee (Absent amounts are NEVER treated as zero; currencies NEVER summed)
        has_price = False
        currencies_seen_in_record: Set[str] = set()
        for fee in r.publication_fees:
            if fee.amount is not None:
                has_price = True
                if fee.currency:
                    currencies_seen_in_record.add(fee.currency)
        for m in r.metrics:
            if m.name == "apc" and m.value is not None:
                has_price = True
                if m.currency:
                    currencies_seen_in_record.add(m.currency)

        if has_price:
            known_price_count += 1
            for c in currencies_seen_in_record:
                currency_counts[c] = currency_counts.get(c, 0) + 1

        # Duration
        has_duration = any(
            m.name in {"first_decision_days", "review_days", "duration_days", "publication_days"}
            and (m.value is not None or m.lower is not None or m.upper is not None)
            for m in r.metrics
        )
        if has_duration:
            known_duration_count += 1

        # Experience
        if len(r.experiences) > 0:
            known_experience_count += 1

        # OA mode
        if r.oa_mode == "unknown":
            unknown_oa_count += 1

        # Freshness / expired evidence
        has_expired = _is_evidence_expired(r.provenance, ref_time)
        if not has_expired:
            all_facts = r.rankings + r.metrics + r.risks + r.experiences + r.publication_fees
            if any(_is_evidence_expired(f.provenance, ref_time) for f in all_facts):
                has_expired = True
        if has_expired:
            expired_count += 1

        # Warnings and conflicts
        if r.identity_warnings:
            warning_count += 1
            if any("冲突" in w or "差异" in w or "conflict" in w.lower() for w in r.identity_warnings):
                conflict_count += 1

    return {
        "total_journals": total,
        "scie_sci_count": sci_count,
        "sci_count": sci_count,
        "scie_count": scie_count,
        "ei_count": ei_count,
        "dual_indexed_count": dual_count,
        "known_price_count": known_price_count,
        "price_coverage": round(known_price_count / total, 4),
        "known_duration_count": known_duration_count,
        "duration_coverage": round(known_duration_count / total, 4),
        "known_experience_count": known_experience_count,
        "experience_coverage": round(known_experience_count / total, 4),
        "unknown_counts": {
            "price": total - known_price_count,
            "duration": total - known_duration_count,
            "experience": total - known_experience_count,
            "oa_mode": unknown_oa_count,
        },
        "expired_count": expired_count,
        "conflict_count": conflict_count,
        "warning_count": warning_count,
        "rejected_count": rejected_count,
        "currencies": currency_counts,
    }


def _parse_records_from_user_json(
    path: Path,
) -> Tuple[List[JournalRecord], List[Dict[str, Any]], int]:
    """Parse user-provided JSON records while collecting granular rejection details."""
    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", f"Empty file: {path}")

    try:
        data = json.loads(content)
    except Exception as e:
        raise JournalError("SCHEMA_CHANGED", f"Invalid JSON in {path}: {e}")

    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "records" in data and isinstance(data["records"], list):
            items = data["records"]
        else:
            items = [data]
    else:
        raise JournalError("SCHEMA_CHANGED", f"Expected list or object with records: {path}")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    rejected_count = 0

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            rejected_count += 1
            rejected.append({"line": idx, "error_code": "INVALID_TYPE", "field": "record"})
            continue
        try:
            if "provenance" not in item:
                item["provenance"] = {
                    "source_id": "local",
                    "source_record": str(idx),
                    "authority": "user",
                    "observed_at": None,
                }
            rec = JournalRecord.model_validate(item)
            records.append(rec)
        except Exception:
            rejected_count += 1
            rejected.append({"line": idx, "error_code": "VALIDATION_ERROR", "field": "record"})

    return records, rejected, rejected_count


def build_local_database(
    store: JournalStore,
    force: bool = False,
    dry_run: bool = True,
    input_paths: Optional[List[str]] = None,
    extra_records: Optional[List[JournalRecord]] = None,
) -> Dict[str, Any]:
    """Build or preview local journal database snapshot.

    Features:
    - Default loads curated candidate pack and DOAJ open metadata pack;
    - If input_paths is non-empty, loads authorized user local JSON/CSV and DOES NOT
      automatically blend bundled packs;
    - Preserves existing observed_at dates without dynamic rewrites;
    - Content hash uses fixed data fields for true import idempotency;
    - In dry_run mode, NEVER writes to SQLite or creates database directory;
    - force=True explicitly permits shrinking an existing snapshot (>50% drop) without deleting other sources;
    - Returns comprehensive coverage report and source audit overview (read-only).
    """
    reports: List[Dict[str, Any]] = []
    all_rejected: List[Dict[str, Any]] = []
    total_rejected = 0
    all_candidate_records: List[JournalRecord] = []
    open_metadata_status = "not_requested"
    open_metadata_missing = False

    # 1. Custom input paths branch
    if input_paths:
        for p_str in input_paths:
            path = _check_safe_local_path(p_str)
            raw_records: List[JournalRecord] = []
            file_rejected: List[Dict[str, Any]] = []
            file_rejected_cnt = 0

            if path.suffix.lower() == ".json":
                raw_records, file_rejected, file_rejected_cnt = _parse_records_from_user_json(path)
            else:
                batch = parse_file(source_id="local", file_path=str(path))
                raw_records = batch.records
                file_rejected = batch.rejected
                file_rejected_cnt = batch.rejected_count

            deduped, conf_rejected, conf_rejected_cnt = deduplicate_and_filter_records(raw_records)
            combined_rejected = file_rejected + conf_rejected
            combined_rejected_cnt = file_rejected_cnt + conf_rejected_cnt

            all_rejected.extend(combined_rejected)
            total_rejected += combined_rejected_cnt

            if not deduped:
                raise JournalError("SCHEMA_CHANGED", f"文件 {path.name} 中没有有效的期刊记录")

            checksum = compute_stable_content_hash(deduped)
            batch = ParsedBatch(
                records=deduped,
                rejected=combined_rejected,
                total_rows=len(raw_records) + file_rejected_cnt,
                rejected_count=combined_rejected_cnt,
            )

            rep = store.import_batch(
                batch=batch,
                source_id="local",
                dataset_id=path.name,
                version=f"v1-{checksum[:8]}",
                checksum=checksum,
                filename=path.name,
                dry_run=dry_run,
                allow_shrink=force,
            )
            reports.append(rep)
            all_candidate_records.extend(deduped)

    # 2. Bundled resources branch (default)
    else:
        # 2a. Curated pack
        curated_raw = load_curated_candidates()
        curated_records, curated_rej, curated_rej_cnt = deduplicate_and_filter_records(curated_raw)
        all_rejected.extend(curated_rej)
        total_rejected += curated_rej_cnt

        if not curated_records:
            raise JournalError("CURATED_DATA_UNAVAILABLE", "精选包中无有效期刊记录")

        curated_checksum = compute_stable_content_hash(curated_records)
        curated_batch = ParsedBatch(
            records=curated_records,
            rejected=curated_rej,
            total_rows=len(curated_raw),
            rejected_count=curated_rej_cnt,
        )
        curated_rep = store.import_batch(
            batch=curated_batch,
            source_id="curated",
            dataset_id="curated_candidates.json",
            version=f"v1-{curated_checksum[:8]}",
            checksum=curated_checksum,
            filename="curated_candidates.json",
            dry_run=dry_run,
            allow_shrink=force,
        )
        reports.append(curated_rep)
        all_candidate_records.extend(curated_records)

        # 2b. Open metadata pack (DOAJ baseline, optional fallback)
        open_raw = load_open_candidates()
        if open_raw:
            open_records, open_rej, open_rej_cnt = deduplicate_and_filter_records(open_raw)
            all_rejected.extend(open_rej)
            total_rejected += open_rej_cnt
            if open_records:
                open_checksum = compute_stable_content_hash(open_records)
                open_batch = ParsedBatch(
                    records=open_records,
                    rejected=open_rej,
                    total_rows=len(open_raw),
                    rejected_count=open_rej_cnt,
                )
                open_rep = store.import_batch(
                    batch=open_batch,
                    source_id="open_metadata",
                    dataset_id="open_metadata.json",
                    version=f"v1-{open_checksum[:8]}",
                    checksum=open_checksum,
                    filename="open_metadata.json",
                    dry_run=dry_run,
                    allow_shrink=force,
                )
                reports.append(open_rep)
                all_candidate_records.extend(open_records)
                open_metadata_status = "loaded"
            else:
                open_metadata_status = "empty"
                open_metadata_missing = True
        else:
            open_metadata_status = "missing"
            open_metadata_missing = True

    # 3. Optional extra records supplement
    if extra_records:
        extra_deduped, extra_rej, extra_rej_cnt = deduplicate_and_filter_records(extra_records)
        all_rejected.extend(extra_rej)
        total_rejected += extra_rej_cnt
        if extra_deduped:
            extra_checksum = compute_stable_content_hash(extra_deduped)
            extra_batch = ParsedBatch(
                records=extra_deduped,
                rejected=extra_rej,
                total_rows=len(extra_records),
                rejected_count=extra_rej_cnt,
            )
            extra_rep = store.import_batch(
                batch=extra_batch,
                source_id="local_supplement",
                dataset_id="extra_records",
                version=f"v1-{extra_checksum[:8]}",
                checksum=extra_checksum,
                filename="extra_records",
                dry_run=dry_run,
                allow_shrink=force,
            )
            reports.append(extra_rep)
            all_candidate_records.extend(extra_deduped)

    # 4. Resolve full dataset for coverage summary
    if not dry_run and store.path.exists():
        final_records = store.records()
    else:
        # In dry_run or pre-creation, consistently deduplicate across all existing and candidate records
        combined_pool: List[JournalRecord] = []
        if store.path.exists():
            combined_pool.extend(copy.deepcopy(store.records()))
        combined_pool.extend(copy.deepcopy(all_candidate_records))
        final_records, _, _ = deduplicate_and_filter_records(combined_pool)

    coverage = summarize_coverage(
        records=final_records,
        rejected_count=total_rejected,
    )
    if open_metadata_missing:
        coverage["open_metadata_missing"] = True

    source_audit = _load_source_audit()

    return {
        "success": True,
        "dry_run": dry_run,
        "force": force,
        "total_imported": sum(r.get("accepted", 0) for r in reports),
        "total_rejected": total_rejected,
        "rejected_samples": all_rejected[:20],
        "reports": reports,
        "coverage": coverage,
        "source_audit": source_audit,
        "open_metadata_status": open_metadata_status,
    }
