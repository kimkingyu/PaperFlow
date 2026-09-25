"""Offline unit tests for DOAJ open metadata parsing and security guards."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from paperflow.engine.journals.models import JournalError, JournalRecord
from paperflow.engine.journals.open_metadata import (
    DOAJ_LICENSE,
    GuardedRedirectHandler,
    build_curated_catalog,
    fetch_doaj_csv,
    load_bundled_open_metadata,
    parse_doaj_csv,
)

FIXTURE_HEADERS = [
    "Journal title",
    "Journal URL",
    "URL in DOAJ",
    "When did the journal start to publish all content using an open license?",
    "Alternative title",
    "Journal ISSN (print version)",
    "Journal EISSN (online version)",
    "Keywords",
    "Languages in which the journal accepts manuscripts",
    "Publisher",
    "Country of publisher",
    "Other organisation",
    "Country of other organisation",
    "Journal license",
    "License attributes",
    "URL for license terms",
    "Machine-readable CC licensing information embedded or displayed in articles",
    "Author holds copyright without restrictions",
    "Copyright information URL",
    "Review process",
    "Review process information URL",
    "Journal plagiarism screening policy",
    "URL for journal's aims & scope",
    "URL for the Editorial Board page",
    "URL for journal's instructions for authors",
    "Average number of weeks between article submission and publication",
    "APC",
    "APC information URL",
    "APC amount",
    "Journal waiver policy (for developing country authors etc)",
    "Waiver policy information URL",
    "Has other fees",
    "Other fees information URL",
    "Preservation Services",
    "Preservation Service: national library",
    "Preservation information URL",
    "Deposit policy directory",
    "URL for deposit policy",
    "Persistent article identifiers",
    "Does the journal comply to DOAJ's definition of open access?",
    "Continues",
    "Continued By",
    "LCC Codes",
    "Subscribe to Open",
    "Mirror Journal",
    "Open Journals Collective",
    "Subjects",
    "Added on Date",
    "Last updated Date",
    "Last Full Review Date",
    "Number of Article Records",
    "Most Recent Article Added",
]


def make_test_csv(rows: List[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIXTURE_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def test_parse_doaj_csv_basic():
    csv_data = make_test_csv(
        [
            {
                "Journal title": "Journal of Advanced Computing",
                "Journal URL": "https://example.com/jac",
                "URL in DOAJ": "https://doaj.org/toc/1580-0261",
                "Alternative title": "Adv. Comput.",
                "Journal ISSN (print version)": "1580-0261",
                "Journal EISSN (online version)": "2350-4234",
                "Keywords": "computer science, artificial intelligence, robotics",
                "Publisher": "Tech Press",
                "URL for journal's aims & scope": "https://example.com/scope",
                "Average number of weeks between article submission and publication": "14",
                "APC": "Yes",
                "APC amount": "1200 USD",
                "APC information URL": "https://example.com/apc",
                "Subjects": "Technology: Computer software | Technology: Electrical engineering. Electronics",
                "Added on Date": "2018-05-10T12:00:00Z",
                "Last updated Date": "2023-08-15T09:30:00Z",
                "Number of Article Records": "450",
            }
        ]
    )

    batch = parse_doaj_csv(csv_data, retrieved_at="2026-09-25T00:00:00Z", limit=10)
    assert len(batch.records) == 1
    assert batch.rejected_count == 0

    rec = batch.records[0]
    assert rec.title == "Journal of Advanced Computing"
    assert rec.publisher == "Tech Press"
    assert rec.issns == ["1580-0261", "2350-4234"]
    assert rec.aliases == ["Adv. Comput."]
    assert rec.indexing == ["DOAJ"]
    assert rec.oa_mode == "full"
    assert "computer science" in rec.fields
    assert "Technology: Computer software" in rec.fields

    # Provenance
    assert rec.provenance.source_id == "doaj_cc0"
    assert rec.provenance.source_url == "https://doaj.org/toc/1580-0261"
    assert rec.provenance.authority == "community"
    assert rec.provenance.observed_at == "2023-08-15T09:30:00Z"
    assert rec.provenance.data_year == 2023
    assert rec.provenance.retrieved_at == "2026-09-25T00:00:00Z"

    # Editorial profiles must NOT be fabricated
    assert rec.editorial_profiles == []

    # Metadata observations
    obs_types = {o["observation"]: o for o in rec.metadata_observations}
    assert "aims_scope_url" in obs_types
    assert obs_types["aims_scope_url"]["url"] == "https://example.com/scope"
    assert "journal_url" in obs_types
    assert obs_types["cumulative_article_records"]["count"] == 450

    # Metrics
    assert len(rec.metrics) == 1
    metric = rec.metrics[0]
    assert metric.name == "submission_to_publication_weeks"
    assert metric.stage == "publication"
    assert metric.value == 14.0
    assert metric.unit == "weeks"

    # Publication fees
    assert len(rec.publication_fees) == 1
    fee = rec.publication_fees[0]
    assert fee.amount == 1200.0
    assert fee.currency == "USD"
    assert fee.route == "open_access"


def test_parse_doaj_csv_invalid_issn_rejected():
    # 1234-5678 has invalid check digit
    csv_data = make_test_csv(
        [
            {
                "Journal title": "Faulty Journal",
                "URL in DOAJ": "https://doaj.org/toc/bad",
                "Journal ISSN (print version)": "1234-5678",
                "Last updated Date": "2020-01-01T00:00:00Z",
            }
        ]
    )
    batch = parse_doaj_csv(csv_data, retrieved_at="2026-09-25T00:00:00Z")
    assert len(batch.records) == 0
    assert batch.rejected_count == 1
    assert batch.rejected[0]["error_code"] == "INVALID_ISSN"
    assert batch.rejected[0]["field"] == "issns"


def test_parse_doaj_csv_missing_issn_rejected():
    csv_data = make_test_csv(
        [
            {
                "Journal title": "No ISSN Journal",
                "URL in DOAJ": "https://doaj.org/toc/noissn",
                "Last updated Date": "2020-01-01T00:00:00Z",
            }
        ]
    )
    batch = parse_doaj_csv(csv_data, retrieved_at="2026-09-25T00:00:00Z")
    assert len(batch.records) == 0
    assert batch.rejected_count == 1
    assert batch.rejected[0]["error_code"] == "MISSING_ISSN"


def test_parse_doaj_csv_empty_title_rejected():
    csv_data = make_test_csv(
        [
            {
                "Journal title": "",
                "URL in DOAJ": "https://doaj.org/toc/notitle",
                "Journal ISSN (print version)": "1580-0261",
                "Last updated Date": "2020-01-01T00:00:00Z",
            }
        ]
    )
    batch = parse_doaj_csv(csv_data, retrieved_at="2026-09-25T00:00:00Z")
    assert len(batch.records) == 0
    assert batch.rejected_count == 1
    assert batch.rejected[0]["error_code"] == "FIELD_EMPTY"
    assert batch.rejected[0]["field"] == "title"


def test_parse_doaj_csv_apc_scenarios():
    rows = [
        # Scenario 1: APC = No -> no fee object created, no_apc observation present, no fake USD/0
        {
            "Journal title": "No Fee Journal",
            "URL in DOAJ": "https://doaj.org/toc/nofee",
            "Journal ISSN (print version)": "1580-0261",
            "APC": "No",
            "APC information URL": "https://example.com/apc-policy",
            "Last updated Date": "2020-01-01T00:00:00Z",
        },
        # Scenario 2: APC = Yes, multi-currency "40 USD; 450000 IDR" -> 2 publication fees
        {
            "Journal title": "Multi Fee Journal",
            "URL in DOAJ": "https://doaj.org/toc/multifee",
            "Journal ISSN (print version)": "2350-4234",
            "APC": "Yes",
            "APC amount": "40 USD; 450000 IDR",
            "APC information URL": "https://example.com/apc",
            "Last updated Date": "2020-01-01T00:00:00Z",
        },
        # Scenario 3: APC = Yes, amount unknown/text -> amount=None, currency="", note preserved
        {
            "Journal title": "Text Fee Journal",
            "URL in DOAJ": "https://doaj.org/toc/textfee",
            "Journal ISSN (print version)": "2318-8081",
            "APC": "Yes",
            "APC amount": "Tiered by nation",
            "APC information URL": "https://example.com/apc",
            "Last updated Date": "2020-01-01T00:00:00Z",
        },
        # Scenario 4: APC = Yes, amount empty -> amount=None, currency=""
        {
            "Journal title": "Empty Fee Journal",
            "URL in DOAJ": "https://doaj.org/toc/emptyfee",
            "Journal ISSN (print version)": "1581-8918",
            "APC": "Yes",
            "APC amount": "",
            "APC information URL": "https://example.com/apc",
            "Last updated Date": "2020-01-01T00:00:00Z",
        },
    ]

    batch = parse_doaj_csv(make_test_csv(rows), retrieved_at="2026-09-25T00:00:00Z")
    assert len(batch.records) == 4

    rec1 = batch.records[0]
    assert rec1.publication_fees == []
    no_apc_obs = [o for o in rec1.metadata_observations if o.get("observation") == "no_apc"]
    assert len(no_apc_obs) == 1
    assert no_apc_obs[0]["has_apc"] is False

    rec2 = batch.records[1]
    assert len(rec2.publication_fees) == 2
    currencies = {f.currency: f.amount for f in rec2.publication_fees}
    assert currencies == {"USD": 40.0, "IDR": 450000.0}

    rec3 = batch.records[2]
    assert len(rec3.publication_fees) == 1
    assert rec3.publication_fees[0].amount is None
    assert rec3.publication_fees[0].currency == ""
    assert "Tiered by nation" in rec3.publication_fees[0].note

    rec4 = batch.records[3]
    assert len(rec4.publication_fees) == 1
    assert rec4.publication_fees[0].amount is None
    assert rec4.publication_fees[0].currency == ""


def test_parse_doaj_csv_weeks_and_cumulative_articles():
    csv_data = make_test_csv(
        [
            {
                "Journal title": "Timing Journal",
                "URL in DOAJ": "https://doaj.org/toc/time",
                "Journal ISSN (print version)": "1580-0261",
                "Average number of weeks between article submission and publication": "18",
                "Number of Article Records": "1850",
                "Last updated Date": "2020-01-01T00:00:00Z",
            }
        ]
    )
    batch = parse_doaj_csv(csv_data, retrieved_at="2026-09-25T00:00:00Z")
    rec = batch.records[0]

    # Weeks metric is correctly named submission_to_publication_weeks
    assert len(rec.metrics) == 1
    assert rec.metrics[0].name == "submission_to_publication_weeks"
    assert rec.metrics[0].stage == "publication"
    assert rec.metrics[0].value == 18.0

    # Ensure annual_articles is NOT inferred from cumulative article records
    metric_names = [m.name for m in rec.metrics]
    assert "annual_articles" not in metric_names
    assert "first_decision_days" not in metric_names

    # Check cumulative count is in metadata_observations
    art_obs = [o for o in rec.metadata_observations if o.get("observation") == "cumulative_article_records"]
    assert len(art_obs) == 1
    assert art_obs[0]["count"] == 1850


def test_parse_doaj_csv_dates_and_limit():
    rows = [
        {
            "Journal title": "Journal One",
            "URL in DOAJ": "https://doaj.org/toc/j1",
            "Journal ISSN (print version)": "1580-0261",
            "Added on Date": "2019-03-01T00:00:00Z",
            "Last updated Date": "2021-11-12T14:22:00Z",
        },
        {
            "Journal title": "Journal Two",
            "URL in DOAJ": "https://doaj.org/toc/j2",
            "Journal ISSN (print version)": "2350-4234",
            "Added on Date": "2017-06-01T00:00:00Z",
            "Last updated Date": "",
        },
    ]
    batch = parse_doaj_csv(make_test_csv(rows), retrieved_at="2026-09-25T12:00:00Z", limit=1)
    # Limit enforces max parsed records
    assert len(batch.records) == 1
    assert batch.records[0].provenance.observed_at == "2021-11-12T14:22:00Z"
    assert batch.records[0].provenance.data_year == 2021
    assert batch.records[0].provenance.retrieved_at == "2026-09-25T12:00:00Z"

    # Test row2 alone uses Added on Date when Last updated Date is missing
    batch2 = parse_doaj_csv(make_test_csv([rows[1]]), retrieved_at="2026-09-25T12:00:00Z")
    assert batch2.records[0].provenance.observed_at == "2017-06-01T00:00:00Z"
    assert batch2.records[0].provenance.data_year == 2017


def test_parse_doaj_csv_schema_errors():
    with pytest.raises(JournalError) as exc_empty:
        parse_doaj_csv("", retrieved_at="2026-09-25T00:00:00Z")
    assert exc_empty.value.code == "SCHEMA_CHANGED"

    with pytest.raises(JournalError) as exc_html:
        parse_doaj_csv("<!DOCTYPE html><html><body>Error 403</body></html>", retrieved_at="2026-09-25T00:00:00Z")
    assert exc_html.value.code == "SCHEMA_CHANGED"

    with pytest.raises(JournalError) as exc_header:
        parse_doaj_csv("ColA,ColB\n1,2\n", retrieved_at="2026-09-25T00:00:00Z")
    assert exc_header.value.code == "SCHEMA_CHANGED"

    with pytest.raises(JournalError) as exc_ts:
        parse_doaj_csv(make_test_csv([]), retrieved_at="not-a-timestamp")
    assert exc_ts.value.code == "INVALID_TIMESTAMP"

    with pytest.raises(JournalError) as exc_lim:
        parse_doaj_csv(make_test_csv([]), retrieved_at="2026-09-25T00:00:00Z", limit=0)
    assert exc_lim.value.code == "INVALID_PARAM"


def test_fetch_doaj_csv_security_guards():
    # Disallows HTTP scheme
    with pytest.raises(JournalError) as exc_http:
        fetch_doaj_csv("http://doaj.org/csv")
    assert exc_http.value.code == "SECURITY_VIOLATION"

    # Disallows unauthorized host
    with pytest.raises(JournalError) as exc_host:
        fetch_doaj_csv("https://evil.com/csv")
    assert exc_host.value.code == "SECURITY_VIOLATION"

    # Disallows credentials in URL
    with pytest.raises(JournalError) as exc_cred:
        fetch_doaj_csv("https://user:pass@doaj.org/csv")
    assert exc_cred.value.code == "SECURITY_VIOLATION"

    # Disallows redirect to unauthorized host
    handler = GuardedRedirectHandler()
    with pytest.raises(JournalError) as exc_red:
        handler.redirect_request(None, None, 302, "Found", {}, "https://attacker.com/malicious.csv")
    assert exc_red.value.code == "SECURITY_VIOLATION"


def test_build_curated_catalog_structure():
    row = {
        "Journal title": "Marine Systems & Robotics",
        "URL in DOAJ": "https://doaj.org/toc/ocean1",
        "Journal ISSN (print version)": "1580-0261",
        "Keywords": "marine robotics",
        "Subjects": "Technology: Marine engineering",
        "Last updated Date": "2023-01-01T00:00:00Z",
    }
    with pytest.raises(JournalError) as exc_low:
        build_curated_catalog(make_test_csv([row]), retrieved_at="2026-09-25T00:00:00Z", target_count=500)
    assert exc_low.value.code == "INVALID_PARAM"


def test_load_bundled_open_metadata_validates_records(tmp_path: Path):
    rec = JournalRecord(
        journal_id="j_test",
        title="Valid Test Journal",
        publisher="Test Pub",
        issns=["1580-0261"],
        indexing=["DOAJ"],
        oa_mode="full",
    )
    payload = {
        "schema_version": 1,
        "license": DOAJ_LICENSE,
        "source_url": "https://doaj.org/csv",
        "retrieved_at": "2026-09-25T00:00:00Z",
        "selection": {"notes": "test"},
        "records": [rec.model_dump() for _ in range(1005)],
    }
    tf = tmp_path / "test_open_metadata.json"
    with open(tf, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    loaded = load_bundled_open_metadata(str(tf))
    assert loaded["schema_version"] == 1
    assert len(loaded["records"]) == 1005


def test_real_bundled_open_metadata_file():
    data = load_bundled_open_metadata()
    assert data["schema_version"] == 1
    assert data["license"] == "CC0-1.0"
    assert data["source_url"] == "https://doaj.org/csv"
    assert len(data["records"]) >= 1000

    selection = data["selection"]
    assert selection["stem_count"] >= 800
    assert selection["breadth_count"] >= 200
    assert selection["total_selected"] == len(data["records"])

    # Sample checks across records
    for rec_dict in data["records"][:50]:
        rec = JournalRecord.model_validate(rec_dict)
        assert rec.indexing == ["DOAJ"]
        assert rec.oa_mode == "full"
        assert rec.provenance.source_id == "doaj_cc0"
        assert rec.provenance.authority == "community"
        assert rec.editorial_profiles == []
        assert rec.issns
        assert rec.journal_id.startswith("j_")

