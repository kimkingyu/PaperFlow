"""Tests for Journal MCP tools and CLI command interface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from paperflow.cli.journal_cli import create_parser, run_journal_cli
from paperflow.engine.journal_finder import JournalFinder
from paperflow.server.mcp_server import (
    analyze_related_journals,
    check_journal_warning,
    compare_academic_journals,
    get_journal_details,
    get_submission_tracker_info,
    import_journal_data,
    list_journal_sources,
    search_academic_journals,
)

SHOWJCR_SAMPLE_CSV = (
    "刊名,ISSN,EISSN,大类名称,大类分区,小类名称,小类分区,Top,收录,OA\n"
    "Nature,0028-0836,1476-4687,综合性期刊,1区,综合性期刊,1区,Top,SCIE,否\n"
    "Science,0036-8075,1095-9203,综合性期刊,1区,综合性期刊,1区,Top,SCIE,否\n"
)


@pytest.fixture
def temp_journal_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "journals"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture
def populated_journal_dir(temp_journal_dir: Path) -> Path:
    finder = JournalFinder(str(temp_journal_dir))
    csv_file = temp_journal_dir / "FQBJCR2023.csv"
    csv_file.write_text(SHOWJCR_SAMPLE_CSV, encoding="utf-8-sig")

    finder.import_data(
        source_id="showjcr",
        file_path=str(csv_file),
        data_year=2023,
        dry_run=False,
    )
    return temp_journal_dir


# =====================================================================
# MCP Tool Tests
# =====================================================================


def test_mcp_list_journal_sources_success(monkeypatch, temp_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    raw = list_journal_sources()
    payload = json.loads(raw)
    assert payload["status"] == "success"
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) > 0
    assert "coverage" in payload
    assert "warnings" in payload
    assert "suggested_options" in payload


def test_mcp_list_journal_sources_single(monkeypatch, temp_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    raw = list_journal_sources(source_id="showjcr")
    payload = json.loads(raw)
    assert payload["status"] == "success"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["id"] == "showjcr"


def test_mcp_submission_tracker_guide():
    raw = get_submission_tracker_info()
    payload = json.loads(raw)
    assert payload["status"] == "success"
    assert payload["data"]["online_available"] is False
    assert "ReviewEvents" in payload["data"]["fields"]


def test_mcp_submission_tracker_offline_data():
    sample_data = {
        "Status": "Under Review",
        "SubmissionDate": "2023-01-01",
        "LatestRevisionNumber": 0,
        "ReviewEvents": [
            {"Date": "2023-01-02", "Event": "Under Review", "Revision": 0}
        ],
    }
    raw = get_submission_tracker_info(data=sample_data)
    payload = json.loads(raw)
    assert payload["status"] in ("success", "partial")
    assert "revisions" in payload["data"]


def test_mcp_empty_database_error_handling(monkeypatch, temp_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    raw = search_academic_journals(query="Nature")
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert payload["error_code"] == "DATA_NOT_INITIALIZED"
    assert payload["data"] is None
    assert "suggested_options" in payload
    assert "traceback" not in payload.get("message", "").lower()


def test_mcp_invalid_input_error_envelope(monkeypatch, temp_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    csv_file = temp_journal_dir / "FQBJCR2023.csv"
    csv_file.write_text(SHOWJCR_SAMPLE_CSV, encoding="utf-8-sig")
    # data_year out of valid range 1900-2200 triggers INVALID_INPUT JournalError
    raw = import_journal_data(
        source_id="showjcr",
        file_path=str(csv_file),
        data_year=1800,
    )
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert payload["error_code"] == "INVALID_INPUT"


def test_mcp_validation_error_envelope(monkeypatch, populated_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(populated_journal_dir))
    # quartiles without rank_system triggers SearchFilters Pydantic ValidationError
    raw = search_academic_journals(
        query="Nature",
        filters={"quartiles": [1]},
    )
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert payload["error_code"] == "VALIDATION_ERROR"
    # Pydantic validation error message must not leak input or msg, only loc/type
    prefix = "参数或数据字段校验未通过: "
    assert payload["message"].startswith(prefix)
    err_items = json.loads(payload["message"][len(prefix):])
    assert isinstance(err_items, list)
    assert all("loc" in item and "type" in item for item in err_items)
    assert all("input" not in item and "msg" not in item for item in err_items)


def test_mcp_and_cli_no_secret_leak_on_validation_or_unexpected_error(monkeypatch, temp_journal_dir: Path, capsys):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    secret_val = "secret_api_token_999888777"

    # Create a policy JSON with a malicious secret value in effective_from / observed_at
    bad_policy_file = temp_journal_dir / "bad_policy.json"
    bad_policy_file.write_text(
        json.dumps({
            "profile_id": "test_prof",
            "name": "Test Policy",
            "effective_from": secret_val,
            "observed_at": secret_val,
        }),
        encoding="utf-8",
    )

    # 1. MCP import school_policy triggers ValidationError (extra field or invalid format)
    mcp_raw = import_journal_data(
        source_id="showjcr",
        file_path=str(bad_policy_file),
        kind="school_policy",
    )
    assert secret_val not in mcp_raw
    mcp_payload = json.loads(mcp_raw)
    assert mcp_payload["status"] == "error"
    assert mcp_payload["error_code"] == "VALIDATION_ERROR"

    # 2. CLI import school_policy triggers ValidationError
    code = run_journal_cli([
        "--data-dir", str(temp_journal_dir),
        "--json",
        "import", "showjcr", str(bad_policy_file),
        "--kind", "school_policy",
    ])
    assert code == 2
    cli_out = capsys.readouterr().out
    assert secret_val not in cli_out
    cli_payload = json.loads(cli_out)
    assert cli_payload["error_code"] == "VALIDATION_ERROR"


def test_mcp_import_dry_run_and_search(monkeypatch, temp_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(temp_journal_dir))
    csv_file = temp_journal_dir / "FQBJCR2023.csv"
    csv_file.write_text(SHOWJCR_SAMPLE_CSV, encoding="utf-8-sig")

    # dry_run=True should not update records
    raw = import_journal_data(
        source_id="showjcr",
        file_path=str(csv_file),
        data_year=2023,
        dry_run=True,
    )
    payload = json.loads(raw)
    assert payload["status"] == "success"

    # search still DATA_NOT_INITIALIZED
    search_raw = search_academic_journals(query="Nature")
    assert json.loads(search_raw)["status"] == "error"

    # apply import
    import_journal_data(
        source_id="showjcr",
        file_path=str(csv_file),
        data_year=2023,
        dry_run=False,
    )
    search_success = json.loads(search_academic_journals(query="Nature"))
    assert search_success["status"] == "success"
    data = search_success["data"]
    items = data.get("results", []) + data.get("provisional_results", [])
    assert len(items) >= 1


def test_mcp_details_and_check_warning(monkeypatch, populated_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(populated_journal_dir))
    details_payload = json.loads(get_journal_details(query="Nature"))
    assert details_payload["status"] == "success"
    assert "journal" in details_payload["data"]
    assert "risk" in details_payload["data"]

    warning_payload = json.loads(check_journal_warning(query="Nature"))
    assert warning_payload["status"] in ("success", "clean") or "conclusion" in warning_payload.get("data", {})


def test_mcp_compare_and_peers(monkeypatch, populated_journal_dir: Path):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(populated_journal_dir))
    search_res = json.loads(search_academic_journals(query="Nature"))
    data = search_res["data"]
    items = data.get("results", []) + data.get("provisional_results", [])
    j_id = items[0]["journal_id"]

    comp_res = json.loads(compare_academic_journals(journal_ids=[j_id]))
    assert comp_res["status"] == "success"

    peers_res = json.loads(analyze_related_journals(references=[{"title": "Sample Paper", "journal": "Nature"}]))
    assert peers_res["status"] == "success"


# =====================================================================
# CLI Tests
# =====================================================================


def test_cli_parser_creation():
    parser = create_parser()
    assert parser.prog == "paperflow journal"


def test_cli_sources_json(populated_journal_dir: Path, capsys):
    code = run_journal_cli(["--data-dir", str(populated_journal_dir), "--json", "sources"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "success"


def test_cli_search_empty_store_returns_sources_error(temp_journal_dir: Path, capsys):
    # Empty store -> DATA_NOT_INITIALIZED error -> code 3
    code = run_journal_cli(["--data-dir", str(temp_journal_dir), "--json", "search", "Nature"])
    assert code == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error_code"] == "DATA_NOT_INITIALIZED"


def test_cli_search_populated_store(populated_journal_dir: Path, capsys):
    code = run_journal_cli(["--data-dir", str(populated_journal_dir), "--json", "search", "Nature"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "success"
    data = payload["data"]
    items = data.get("results", []) + data.get("provisional_results", [])
    assert len(items) >= 1


def test_cli_search_fast_without_metric_year_fails(populated_journal_dir: Path, capsys):
    # --fast without --metric-year must fail with exit code 2
    code = run_journal_cli([
        "--data-dir", str(populated_journal_dir),
        "--json",
        "search", "Nature",
        "--fast",
    ])
    assert code == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error_code"] == "INVALID_INPUT"


def test_cli_search_fast_with_metric_year(populated_journal_dir: Path, capsys):
    code = run_journal_cli([
        "--data-dir", str(populated_journal_dir),
        "--json",
        "search", "Nature",
        "--fast",
        "--metric-year", "2023",
    ])
    # Returns 0 even if 0 results match 45 days & 1000 articles
    assert code == 0


def test_cli_show_and_check_clean_vs_flagged(temp_journal_dir: Path, capsys):
    # 1. Import normal journal + warning journal
    fqb_csv = temp_journal_dir / "FQBJCR2023.csv"
    fqb_csv.write_text(SHOWJCR_SAMPLE_CSV, encoding="utf-8-sig")
    run_journal_cli([
        "import", "showjcr", str(fqb_csv),
        "--data-year", "2023",
        "--apply",
        "--data-dir", str(temp_journal_dir),
        "--json",
    ])
    capsys.readouterr()

    # Import CAS warning CSV with valid ISSN 1234-5679
    warn_csv = temp_journal_dir / "GJQKYJMD2024.csv"
    warn_csv.write_text(
        "期刊名,ISSN,预警等级,年份\nRisky Journal,1234-5679,高,2024\n",
        encoding="utf-8-sig",
    )
    run_journal_cli([
        "import", "showjcr", str(warn_csv),
        "--data-year", "2024",
        "--apply",
        "--data-dir", str(temp_journal_dir),
        "--json",
    ])
    capsys.readouterr()

    # Check Nature -> needs_verification (not flagged/blocked under default exclude_known) -> exit code 0
    code_nature = run_journal_cli(["check", "Nature", "--data-dir", str(temp_journal_dir), "--json"])
    out_nature = json.loads(capsys.readouterr().out)
    assert out_nature["data"]["conclusion"] == "needs_verification"
    assert out_nature["data"]["blocked"] is False
    assert code_nature == 0

    # Check Risky Journal -> conclusion == flagged, blocked == True -> exit code 1
    code_risky = run_journal_cli(["check", "Risky Journal", "--data-dir", str(temp_journal_dir), "--json"])
    out_risky = json.loads(capsys.readouterr().out)
    assert out_risky["data"]["conclusion"] == "flagged"
    assert out_risky["data"]["blocked"] is True
    assert code_risky == 1


def test_cli_track_input_and_previous_aliases(temp_journal_dir: Path, capsys):
    ev1 = temp_journal_dir / "ev1.json"
    ev2 = temp_journal_dir / "ev2.json"
    ev1.write_text(json.dumps({
        "Status": "With Editor",
        "SubmissionDate": "2024-01-01",
        "LatestRevisionNumber": 0,
        "ReviewEvents": [],
    }), encoding="utf-8")
    ev2.write_text(json.dumps({
        "Status": "Under Review",
        "SubmissionDate": "2024-01-01",
        "LatestRevisionNumber": 0,
        "ReviewEvents": [{"Id": "1", "Event": "Reviewer Invited", "Date": "2024-01-05", "Revision": 0}],
    }), encoding="utf-8")

    # Test --input and --previous-file aliases
    code = run_journal_cli([
        "track",
        "--input", str(ev2),
        "--previous-file", str(ev1),
        "--json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in ("success", "partial")
    assert payload["data"]["changes"]["status_changed"] is True
    assert payload["data"]["changes"]["new_events_count"] == 1


def test_cli_compare(monkeypatch, populated_journal_dir: Path, capsys):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(populated_journal_dir))
    search_raw = search_academic_journals(query="Nature")
    data = json.loads(search_raw)["data"]
    items = data.get("results", []) + data.get("provisional_results", [])
    j_id = items[0]["journal_id"]
    code = run_journal_cli(["--data-dir", str(populated_journal_dir), "--json", "compare", j_id])
    assert code == 0


def test_cli_track_guide(capsys):
    code = run_journal_cli(["--json", "track"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["data"]["online_available"] is False


def test_cli_subprocess_execution(populated_journal_dir: Path):
    cmd = [
        sys.executable, "-m", "paperflow", "journal",
        "--data-dir", str(populated_journal_dir),
        "--json", "sources", "showjcr",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["status"] == "success"
    assert payload["data"][0]["id"] == "showjcr"

