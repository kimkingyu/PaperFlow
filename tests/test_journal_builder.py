"""Unit tests for journal builder, candidate loaders, and coverage analyzers.

All records are fixtures created in temporary directories.
Does not depend on external curated files or network access.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from paperflow.engine.journals.builder import (
    build_local_database,
    compute_stable_content_hash,
    deduplicate_and_filter_records,
    load_curated_candidates,
    load_open_candidates,
    summarize_coverage,
)
from paperflow.engine.journals.models import (
    EditorialProfile,
    Experience,
    JournalError,
    JournalRecord,
    Metric,
    Provenance,
    PublicationFee,
    Ranking,
    RiskEvent,
)
from paperflow.engine.journals.store import JournalStore


# ---------------------------------------------------------------------------
# Fixture Helpers
# ---------------------------------------------------------------------------

def make_valid_issn(index: int) -> str:
    """Generate a checksum-valid ISSN for testing fixtures."""
    prefix = f"{index:07d}"
    digits = [int(c) for c in prefix]
    rem = sum(v * (8 - i) for i, v in enumerate(digits)) % 11
    check = "0" if rem == 0 else ("X" if (11 - rem) == 10 else str(11 - rem))
    return f"{prefix[:4]}-{prefix[4:]}{check}"


def make_fixture_record(
    title: str = "Fixture Journal of AI",
    issns: list[str] | None = None,
    kind: str = "journal",
    indexing: list[str] | None = None,
    publication_fees: list[PublicationFee] | None = None,
    metrics: list[Metric] | None = None,
    experiences: list[Experience] | None = None,
    provenance: Provenance | None = None,
    identity_warnings: list[str] | None = None,
    **kwargs: Any,
) -> JournalRecord:
    """Create a standardized fixture JournalRecord for testing."""
    return JournalRecord(
        title=title,
        issns=["1234-5679"] if issns is None else issns,
        kind=kind,
        indexing=["SCIE"] if indexing is None else indexing,
        publication_fees=[] if publication_fees is None else publication_fees,
        metrics=[] if metrics is None else metrics,
        experiences=[] if experiences is None else experiences,
        provenance=Provenance() if provenance is None else provenance,
        identity_warnings=[] if identity_warnings is None else identity_warnings,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Candidate Loaders and Safety Tests
# ---------------------------------------------------------------------------

def test_load_curated_candidates_missing_raises_unavailable(monkeypatch: pytest.MonkeyPatch):
    """When curated_candidates.json is absent, must raise CURATED_DATA_UNAVAILABLE without fabricating data."""
    import paperflow.engine.journals.builder as b_mod
    monkeypatch.setattr(b_mod, "_find_resource_path", lambda filename: None)

    with pytest.raises(JournalError) as exc_info:
        load_curated_candidates()
    assert exc_info.value.code == "CURATED_DATA_UNAVAILABLE"


def test_load_curated_candidates_reads_array_and_filters_conferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Curated candidates loader must support array JSON and exclude conference entries."""
    import paperflow.engine.journals.builder as b_mod

    fixture_data = [
        {
            "title": "Curated Journal of Robotics",
            "kind": "journal",
            "issns": ["2049-3630"],
            "indexing": ["SCIE", "EI"],
        },
        {
            "title": "International Conference on Robotics",
            "kind": "conference",
            "issns": ["1234-5679"],
        },
    ]
    curated_file = tmp_path / "curated_candidates.json"
    curated_file.write_text(json.dumps(fixture_data, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(b_mod, "_find_resource_path", lambda f: curated_file if f == "curated_candidates.json" else None)

    records = load_curated_candidates()
    assert len(records) == 1
    assert records[0].title == "Curated Journal of Robotics"
    assert records[0].kind == "journal"


def test_load_curated_candidates_reads_envelope_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Curated candidates loader must support envelope dict `{"records": [...]}` format."""
    import paperflow.engine.journals.builder as b_mod

    fixture_data = {
        "schema_version": "1.0",
        "records": [
            {
                "title": "Curated Medical Review",
                "kind": "journal",
                "issns": ["0000-1111"],
                "indexing": ["SCIE"],
            }
        ],
    }
    curated_file = tmp_path / "curated_candidates.json"
    curated_file.write_text(json.dumps(fixture_data, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(b_mod, "_find_resource_path", lambda f: curated_file if f == "curated_candidates.json" else None)

    records = load_curated_candidates()
    assert len(records) == 1
    assert records[0].title == "Curated Medical Review"


def test_load_open_candidates_graceful_missing(monkeypatch: pytest.MonkeyPatch):
    """Missing open_metadata.json should return an empty list without raising."""
    import paperflow.engine.journals.builder as b_mod
    monkeypatch.setattr(b_mod, "_find_resource_path", lambda f: None)

    records = load_open_candidates()
    assert records == []


def test_safe_path_rejects_network_url(tmp_path: Path):
    """Local path input must strictly prohibit remote URLs or network download schemes."""
    store = JournalStore(str(tmp_path / "store"))
    with pytest.raises(JournalError) as exc_info:
        build_local_database(store, input_paths=["https://example.com/malicious.json"])
    assert exc_info.value.code == "INVALID_INPUT"

    with pytest.raises(JournalError) as exc_info:
        build_local_database(store, input_paths=["ftp://remote-server/data.csv"])
    assert exc_info.value.code == "INVALID_INPUT"


def test_safe_path_rejects_nonexistent_file(tmp_path: Path):
    """Non-existent local path must raise SOURCE_UNAVAILABLE."""
    store = JournalStore(str(tmp_path / "store"))
    bad_path = str(tmp_path / "missing_file.json")
    with pytest.raises(JournalError) as exc_info:
        build_local_database(store, input_paths=[bad_path])
    assert exc_info.value.code == "SOURCE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 2. Dry Run & Directory Invariance Tests
# ---------------------------------------------------------------------------

def test_dry_run_never_writes_database_or_creates_directory(tmp_path: Path):
    """In dry_run=True, builder must not create SQLite files or parent directory."""
    uncreated_dir = tmp_path / "never_created_dir"
    store = JournalStore(str(uncreated_dir))

    fixture_json = tmp_path / "user_input.json"
    fixture_json.write_text(
        json.dumps(
            [
                {
                    "title": "Dry Run Journal",
                    "issns": ["1234-5679"],
                    "kind": "journal",
                    "indexing": ["SCIE"],
                }
            ]
        ),
        encoding="utf-8",
    )

    res = build_local_database(store, dry_run=True, input_paths=[str(fixture_json)])

    assert res["success"] is True
    assert res["dry_run"] is True
    assert res["coverage"]["total_journals"] == 1
    assert res["coverage"]["scie_sci_count"] == 1
    # Verify no directory or sqlite file was created
    assert not uncreated_dir.exists()
    assert not store.path.exists()


# ---------------------------------------------------------------------------
# 3. Import Idempotency & Stable Content Hash Tests
# ---------------------------------------------------------------------------

def test_import_idempotency_and_stable_hash(tmp_path: Path):
    """Re-importing identical records must result in 'unchanged' without duplicate snapshots."""
    store = JournalStore(str(tmp_path / "store"))

    fixture_record_1 = {
        "title": "Idempotent Journal Alpha",
        "issns": ["1234-5679"],
        "kind": "journal",
        "indexing": ["SCIE", "EI"],
    }
    fixture_json = tmp_path / "fixture.json"
    fixture_json.write_text(json.dumps([fixture_record_1]), encoding="utf-8")

    # Run 1: initial import
    res1 = build_local_database(store, dry_run=False, input_paths=[str(fixture_json)])
    assert res1["reports"][0]["added"] == 1
    assert res1["reports"][0]["unchanged"] == 0
    snapshot_count_1 = len(store.snapshots())
    assert snapshot_count_1 == 1

    # Run 2: re-import identical content
    res2 = build_local_database(store, dry_run=False, input_paths=[str(fixture_json)])
    assert res2["reports"][0]["unchanged"] == 1
    assert res2["reports"][0]["added"] == 0
    snapshot_count_2 = len(store.snapshots())
    assert snapshot_count_2 == 1  # No duplicate snapshot created


def test_stable_hash_is_invariant_to_dynamic_retrieved_at_only():
    """Checksum must ignore purely runtime retrieved_at variations, but reflect factual observed_at changes."""
    rec1 = make_fixture_record(
        title="Stable Hash Journal",
        issns=["1234-5679"],
        provenance=Provenance(observed_at="2024-01-01T00:00:00Z", retrieved_at="2024-01-01T00:00:00Z"),
    )
    # Same factual observed_at, only runtime retrieved_at changed -> identical hash
    rec2 = make_fixture_record(
        title="Stable Hash Journal",
        issns=["1234-5679"],
        provenance=Provenance(observed_at="2024-01-01T00:00:00Z", retrieved_at="2026-04-18T12:00:00Z"),
    )
    assert compute_stable_content_hash([rec1]) == compute_stable_content_hash([rec2])

    # Factual observed_at date updated -> distinct hash! Must produce a new snapshot
    rec3 = make_fixture_record(
        title="Stable Hash Journal",
        issns=["1234-5679"],
        provenance=Provenance(observed_at="2026-04-18T00:00:00Z", retrieved_at="2024-01-01T00:00:00Z"),
    )
    assert compute_stable_content_hash([rec1]) != compute_stable_content_hash([rec3])


def test_hash_reflects_scope_taxes_and_metric_stages():
    """Hash must change when scope, tax/note, or metric stage/comparator changes."""
    base_issn = ["1234-5679"]

    # 1. Scope (editorial_profiles) change
    rec_base = make_fixture_record(title="Scope Journal", issns=base_issn)
    rec_scope = make_fixture_record(
        title="Scope Journal",
        issns=base_issn,
        editorial_profiles=[EditorialProfile(scope_summary="Covers advanced robotics and vision systems")],
    )
    assert compute_stable_content_hash([rec_base]) != compute_stable_content_hash([rec_scope])

    # 2. Taxes and fee note changes
    rec_fee_plain = make_fixture_record(
        title="Fee Journal",
        issns=base_issn,
        publication_fees=[PublicationFee(amount=2000.0, currency="USD", taxes_included=False, note="Standard APC")],
    )
    rec_fee_taxed = make_fixture_record(
        title="Fee Journal",
        issns=base_issn,
        publication_fees=[PublicationFee(amount=2000.0, currency="USD", taxes_included=True, note="Standard APC")],
    )
    rec_fee_note = make_fixture_record(
        title="Fee Journal",
        issns=base_issn,
        publication_fees=[PublicationFee(amount=2000.0, currency="USD", taxes_included=False, note="Discount available")],
    )
    assert compute_stable_content_hash([rec_fee_plain]) != compute_stable_content_hash([rec_fee_taxed])
    assert compute_stable_content_hash([rec_fee_plain]) != compute_stable_content_hash([rec_fee_note])

    # 3. Metric stage and comparator changes
    rec_metric_base = make_fixture_record(
        title="Metric Journal",
        issns=base_issn,
        metrics=[Metric(name="review_days", value=45.0, stage="first_decision", comparator="eq")],
    )
    rec_metric_stage = make_fixture_record(
        title="Metric Journal",
        issns=base_issn,
        metrics=[Metric(name="review_days", value=45.0, stage="final_acceptance", comparator="eq")],
    )
    rec_metric_comp = make_fixture_record(
        title="Metric Journal",
        issns=base_issn,
        metrics=[Metric(name="review_days", value=45.0, stage="first_decision", comparator="le")],
    )
    assert compute_stable_content_hash([rec_metric_base]) != compute_stable_content_hash([rec_metric_stage])
    assert compute_stable_content_hash([rec_metric_base]) != compute_stable_content_hash([rec_metric_comp])


# ---------------------------------------------------------------------------
# 4. Force & Shrinking Tests with Source Isolation
# ---------------------------------------------------------------------------

def test_force_allows_shrink_without_deleting_other_sources(tmp_path: Path):
    """Shrinking a snapshot >50% requires force=True, and preserves other active sources."""
    store = JournalStore(str(tmp_path / "store"))

    # 1. Import Source A with 4 records
    source_a_large = tmp_path / "source_a.json"
    source_a_large.write_text(
        json.dumps(
            [
                {"title": f"Source A Journal {i}", "issns": [make_valid_issn(100 + i)], "kind": "journal"}
                for i in range(1, 5)
            ]
        ),
        encoding="utf-8",
    )
    build_local_database(store, dry_run=False, input_paths=[str(source_a_large)])

    # 2. Import Source B with 2 records
    source_b = tmp_path / "source_b.json"
    source_b.write_text(
        json.dumps(
            [
                {"title": f"Source B Journal {i}", "issns": [make_valid_issn(200 + i)], "kind": "journal"}
                for i in range(1, 3)
            ]
        ),
        encoding="utf-8",
    )
    build_local_database(store, dry_run=False, input_paths=[str(source_b)])

    assert len([s for s in store.snapshots() if s["active"]]) == 2

    # 3. Shrink Source A down to 1 record (< 50% of 4)
    source_a_shrunk = tmp_path / "source_a.json"
    source_a_shrunk.write_text(
        json.dumps(
            [{"title": "Source A Journal 1", "issns": [make_valid_issn(101)], "kind": "journal"}]
        ),
        encoding="utf-8",
    )

    # 3a. Without force -> must raise SCHEMA_CHANGED
    with pytest.raises(JournalError) as exc_info:
        build_local_database(store, force=False, dry_run=False, input_paths=[str(source_a_shrunk)])
    assert exc_info.value.code == "SCHEMA_CHANGED"

    # 3b. With force=True -> succeeds
    res = build_local_database(store, force=True, dry_run=False, input_paths=[str(source_a_shrunk)])
    assert res["success"] is True

    # 4. Verify source isolation: Source B remains intact and active!
    active_snapshots = [s for s in store.snapshots() if s["active"]]
    assert len(active_snapshots) == 2
    b_snap = next(s for s in active_snapshots if s["dataset_id"] == "source_b.json")
    assert b_snap["row_count"] == 2

    # Verify records in store
    all_recs = store.records()
    b_titles = {r.title for r in all_recs if "Source B" in r.title}
    assert len(b_titles) == 2


# ---------------------------------------------------------------------------
# 5. Deduplication, Alias Auxiliary, & Conference Filtering Tests
# ---------------------------------------------------------------------------

def test_deduplication_by_issn_and_auxiliary_alias_matching():
    """Duplicate ISSN records merge, and title matches without ISSN serve as auxiliary aliases."""
    raw = [
        make_fixture_record(
            title="IEEE Transactions on Cybernetics",
            issns=["2168-2267"],
            indexing=["SCIE", "EI"],
        ),
        make_fixture_record(
            title="IEEE Trans Cybern",
            issns=["2168-2267"],  # Same ISSN -> should merge into first
            indexing=["SCIE"],
        ),
        make_fixture_record(
            title="IEEE Trans Cybern",
            issns=[],  # No ISSN -> auxiliary match by title/alias
        ),
    ]

    deduped, rejected, rej_cnt = deduplicate_and_filter_records(raw)
    assert len(deduped) == 1
    assert rej_cnt == 0
    record = deduped[0]
    assert record.title == "IEEE Transactions on Cybernetics"
    assert "IEEE Trans Cybern" in record.aliases


def test_conference_excluded_with_granular_rejection():
    """Conferences must be excluded from journal database and recorded in rejection details."""
    raw = [
        make_fixture_record(title="Conference on Computer Vision", kind="conference"),
        make_fixture_record(title="Journal of Computer Vision", kind="journal"),
    ]

    deduped, rejected, rej_cnt = deduplicate_and_filter_records(raw)
    assert len(deduped) == 1
    assert deduped[0].title == "Journal of Computer Vision"
    assert rej_cnt == 1
    assert rejected[0]["error_code"] == "CONFERENCE_EXCLUDED"
    assert rejected[0]["field"] == "kind"
    assert "不把会议算作期刊" in rejected[0]["reason"]


def test_user_json_with_malformed_entries_captures_rejections(tmp_path: Path):
    """Format errors in user JSON must produce rejection samples without silent loss."""
    fixture_json = tmp_path / "mixed_valid_invalid.json"
    content = [
        {"title": "Valid Journal", "issns": ["1234-5679"], "kind": "journal"},
        {"title": "", "issns": ["1234-5679"]},  # Invalid: empty title
        "not a dict entry",  # Invalid type
    ]
    fixture_json.write_text(json.dumps(content), encoding="utf-8")

    store = JournalStore(str(tmp_path / "store"))
    res = build_local_database(store, dry_run=True, input_paths=[str(fixture_json)])

    assert res["success"] is True
    assert res["total_rejected"] >= 2
    codes = [item["error_code"] for item in res["rejected_samples"]]
    assert "VALIDATION_ERROR" in codes or "INVALID_TYPE" in codes


# ---------------------------------------------------------------------------
# 6. Indexing Statistics (SCIE, SCI, EI, Dual-Indexed) Tests
# ---------------------------------------------------------------------------

def test_indexing_coverage_statistics():
    """Verify SCIE/SCI, EI, and dual-indexing counts."""
    records = [
        make_fixture_record(title="SCIE Only", issns=[make_valid_issn(301)], indexing=["SCIE"]),
        make_fixture_record(title="EI Only", issns=[make_valid_issn(302)], indexing=["EI"]),
        make_fixture_record(title="Dual Journal", issns=[make_valid_issn(303)], indexing=["SCIE", "EI"]),
        make_fixture_record(title="DOAJ Only", issns=[make_valid_issn(304)], indexing=["DOAJ"]),
    ]

    cov = summarize_coverage(records)
    assert cov["total_journals"] == 4
    assert cov["scie_sci_count"] == 2  # SCIE Only, Dual Journal
    assert cov["scie_count"] == 2
    assert cov["ei_count"] == 2        # EI Only, Dual Journal
    assert cov["dual_indexed_count"] == 1  # Dual Journal


# ---------------------------------------------------------------------------
# 7. Currency, Unknown Fee, & Duration Coverage Tests
# ---------------------------------------------------------------------------

def test_multicurrency_not_summed_and_unknown_fee_never_zero():
    """Fees in different currencies must never be summed; absent fee amount is never zero."""
    records = [
        # Journal 1: USD fee
        make_fixture_record(
            title="USD Journal",
            issns=[make_valid_issn(401)],
            publication_fees=[PublicationFee(amount=2500.0, currency="USD")],
        ),
        # Journal 2: CNY fee
        make_fixture_record(
            title="CNY Journal",
            issns=[make_valid_issn(402)],
            publication_fees=[PublicationFee(amount=12000.0, currency="CNY")],
        ),
        # Journal 3: Unknown fee amount (absent amount must NOT be counted as zero or known price)
        make_fixture_record(
            title="Unknown Fee Journal",
            issns=[make_valid_issn(403)],
            publication_fees=[PublicationFee(amount=None, currency="")],
        ),
    ]

    cov = summarize_coverage(records)
    assert cov["total_journals"] == 3
    assert cov["known_price_count"] == 2
    assert cov["unknown_counts"]["price"] == 1
    assert cov["price_coverage"] == round(2 / 3, 4)
    # Currency distribution distinct, not summed
    assert cov["currencies"] == {"USD": 1, "CNY": 1}


def test_duration_and_experience_coverage():
    """Verify known cycle and submission experience coverage."""
    records = [
        make_fixture_record(
            title="Fast Journal",
            issns=[make_valid_issn(501)],
            metrics=[Metric(name="first_decision_days", value=21.0)],
            experiences=[Experience(summary="Accepted in 3 months")],
        ),
        make_fixture_record(
            title="Slow Journal",
            issns=[make_valid_issn(502)],
            metrics=[Metric(name="review_days", lower=60.0, upper=90.0)],
        ),
        make_fixture_record(
            title="Mystery Journal",
            issns=[make_valid_issn(503)],
        ),
    ]

    cov = summarize_coverage(records)
    assert cov["known_duration_count"] == 2
    assert cov["duration_coverage"] == round(2 / 3, 4)
    assert cov["known_experience_count"] == 1
    assert cov["experience_coverage"] == round(1 / 3, 4)


# ---------------------------------------------------------------------------
# 8. Freshness & Expired Evidence Inspection Tests
# ---------------------------------------------------------------------------

def test_freshness_inspects_expired_and_stale_provenance():
    """Expired provenance or observations older than 365 days must be counted in expired_count."""
    now_ref = datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc)

    records = [
        # Record 1: Fresh observation (within 30 days)
        make_fixture_record(
            title="Fresh Journal",
            issns=[make_valid_issn(601)],
            provenance=Provenance(observed_at="2026-04-01T00:00:00Z", freshness="current"),
        ),
        # Record 2: Explicitly historical/expired freshness tag
        make_fixture_record(
            title="Stale Tag Journal",
            issns=[make_valid_issn(602)],
            provenance=Provenance(freshness="historical"),
        ),
        # Record 3: Fact with observation date from 2 years ago (>365 days)
        make_fixture_record(
            title="Old Metric Journal",
            issns=[make_valid_issn(603)],
            metrics=[
                Metric(
                    name="impact_factor",
                    value=5.2,
                    provenance=Provenance(observed_at="2023-01-01T00:00:00Z"),
                )
            ],
        ),
    ]

    cov = summarize_coverage(records, now=now_ref)
    assert cov["expired_count"] == 2  # Records 2 and 3


# ---------------------------------------------------------------------------
# 9. Conflict & Identity Warning Coverage Tests
# ---------------------------------------------------------------------------

def test_conflict_count_detects_metadata_inconsistencies():
    """Journals with indexing or OA conflicts must be reported in conflict_count."""
    records = [
        make_fixture_record(
            title="Conflicted Journal",
            issns=[make_valid_issn(701)],
            identity_warnings=["来源收录集合存在差异，需按年份与官方状态复核"],
        ),
        make_fixture_record(
            title="Clean Journal",
            issns=[make_valid_issn(702)],
        ),
    ]

    cov = summarize_coverage(records)
    assert cov["conflict_count"] == 1
    assert cov["warning_count"] == 1


# ---------------------------------------------------------------------------
# 10. Default Build Mode with Curated & Open Metadata Packs
# ---------------------------------------------------------------------------

def test_build_local_database_default_loads_curated_and_handles_open_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Default build without input_paths loads curated candidate pack and flags open_metadata status."""
    import paperflow.engine.journals.builder as b_mod

    curated_data = [
        {
            "title": "Default Curated AI Journal",
            "issns": ["2049-3630"],
            "kind": "journal",
            "indexing": ["SCIE", "EI"],
        }
    ]
    curated_file = tmp_path / "curated_candidates.json"
    curated_file.write_text(json.dumps(curated_data), encoding="utf-8")

    monkeypatch.setattr(
        b_mod,
        "_find_resource_path",
        lambda f: curated_file if f == "curated_candidates.json" else None,
    )

    store = JournalStore(str(tmp_path / "store"))
    res = build_local_database(store, dry_run=False)

    assert res["success"] is True
    assert res["total_imported"] == 1
    assert res["coverage"]["total_journals"] == 1
    assert res["open_metadata_status"] == "missing"
    assert res["coverage"]["open_metadata_missing"] is True


def test_invalid_issn_recorded_in_rejections():
    """Records with invalid ISSN format or checksum must be rejected with details."""
    raw = [
        make_fixture_record(title="Invalid ISSN Journal", issns=["1234-5678"]),  # Bad checksum
        make_fixture_record(title="Valid ISSN Journal", issns=["1234-5679"]),    # Valid checksum
    ]
    deduped, rejected, rej_cnt = deduplicate_and_filter_records(raw)
    assert len(deduped) == 1
    assert rej_cnt == 1
    assert rejected[0]["error_code"] == "INVALID_ISSN"
    assert rejected[0]["field"] == "issns"


def test_source_audit_included_when_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """When source_audit.json is present, it must be loaded read-only into response."""
    import paperflow.engine.journals.builder as b_mod

    audit_content = {"total_sources": 12, "last_audited": "2026-04-18"}
    audit_file = tmp_path / "source_audit.json"
    audit_file.write_text(json.dumps(audit_content), encoding="utf-8")

    monkeypatch.setattr(
        b_mod,
        "_find_resource_path",
        lambda f: audit_file if f == "source_audit.json" else None,
    )

    fixture_json = tmp_path / "simple.json"
    fixture_json.write_text(
        json.dumps([{"title": "Simple Journal", "issns": ["1234-5679"], "kind": "journal"}]),
        encoding="utf-8",
    )

    store = JournalStore(str(tmp_path / "store"))
    res = build_local_database(store, dry_run=True, input_paths=[str(fixture_json)])

    assert res["source_audit"] == audit_content


def test_summarize_coverage_empty_records():
    """summarize_coverage on empty list must return zeroed metrics without ZeroDivisionError."""
    cov = summarize_coverage([])
    assert cov["total_journals"] == 0
    assert cov["price_coverage"] == 0.0
    assert cov["duration_coverage"] == 0.0
    assert cov["experience_coverage"] == 0.0
    assert cov["currencies"] == {}


def test_build_local_database_with_extra_records(tmp_path: Path):
    """build_local_database supports optional extra_records supplement without network."""
    store = JournalStore(str(tmp_path / "store"))
    fixture_json = tmp_path / "input.json"
    fixture_json.write_text(
        json.dumps([{"title": "File Journal", "issns": [make_valid_issn(801)], "kind": "journal"}]),
        encoding="utf-8",
    )
    extra = [
        make_fixture_record(title="Extra Memory Journal", issns=[make_valid_issn(802)]),
    ]

    res = build_local_database(
        store,
        dry_run=False,
        input_paths=[str(fixture_json)],
        extra_records=extra,
    )
    assert res["success"] is True
    assert res["coverage"]["total_journals"] == 2
    assert len(store.records()) == 2


def test_dry_run_coverage_pissn_eissn_cross_matching(tmp_path: Path):
    """dry_run coverage must accurately deduplicate pISSN/eISSN cross matches via deduplicate_and_filter_records."""
    issn_a = make_valid_issn(901)
    issn_b = make_valid_issn(902)
    issn_c = make_valid_issn(903)

    records = [
        {"title": "Journal Print Version", "issns": [issn_a, issn_b], "kind": "journal"},
        {"title": "Journal Electronic Version", "issns": [issn_b, issn_c], "kind": "journal"},
    ]
    fixture_json = tmp_path / "cross_issn.json"
    fixture_json.write_text(json.dumps(records), encoding="utf-8")

    store = JournalStore(str(tmp_path / "store"))
    res = build_local_database(store, dry_run=True, input_paths=[str(fixture_json)])

    assert res["success"] is True
    assert res["dry_run"] is True
    # Without cross-matching by all ISSNs, these 2 records would falsely appear as 2 journals.
    # With unified deduplication, they correctly merge into 1 journal!
    assert res["coverage"]["total_journals"] == 1


