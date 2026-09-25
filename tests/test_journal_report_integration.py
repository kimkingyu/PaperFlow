"""End-to-end journal build/recommend/export boundaries, using fictional fixtures."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from paperflow.cli.journal_cli import run_journal_cli
from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, JournalRecord
from paperflow.engine.journals.recommendation_models import RecommendationPreferences
from paperflow.server.mcp_server import build_journal_database, recommend_journals

IDEA = "We plan underwater robotics and embedded computing experiments."


def sample_records(count=15):
    observed = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    provenance = {"source_id": "fictional-report-test", "source_url": "https://publisher.example/test",
                  "observed_at": observed, "retrieved_at": observed, "authority": "official"}
    records = []
    for i in range(count):
        digits = f"{8100000 + i:07d}"
        remainder = (11 - sum(int(d) * (8 - j) for j, d in enumerate(digits)) % 11) % 11
        issn = digits[:4] + "-" + digits[4:] + ("X" if remainder == 10 else str(remainder))
        records.append({
            "title": f"Fictional Report Journal {i}", "issns": [issn], "fields": ["robotics"],
            "indexing": ["SCIE", "EI"], "oa_mode": "full", "provenance": provenance,
            "editorial_profiles": [{"scope_summary": "Studies of underwater robotics and embedded computing.",
                "topics": ["robotics"], "article_types": ["research"],
                "positioning": ["application", "general", "field_leading"][i % 3],
                "positioning_basis": "Fictional editorial positioning for tests, not a quality rating.",
                "provenance": provenance}],
            "publication_fees": [{"route": "open_access", "amount": 1200 + i, "currency": "USD",
                "taxes_included": None, "provenance": provenance}],
            "experiences": [{"summary": "Fictional subjective experience for parser testing only.",
                "url": "https://community.example/review", "subjective": True, "provenance": provenance}],
        })
    return records


def test_default_capacity_and_large_requested_groups():
    assert RecommendationPreferences().per_group == 6
    assert RecommendationPreferences(per_group=20, candidate_limit=200).candidate_limit == 200


def test_fifteen_real_protocol_cards_export_without_network_or_database(tmp_path):
    home = tmp_path / "not-created"
    service = JournalFinder(str(home))
    prepared = service.prepare_manuscript(text=IDEA, mode="idea")["data"]
    profile = {"input_id": prepared["input_id"], "mode": "idea", "summary": IDEA,
               "keywords": ["robotics", "embedded"], "article_type": "research", "readiness": "planned"}
    records = sample_records()
    with patch("socket.create_connection", side_effect=AssertionError("No network allowed")):
        first = service.recommend(text=IDEA, profile=profile, candidate_records=records)
        assessments = [{"journal_id": item["journal_id"], "context_id": first["data"]["context_id"],
                        "scope_fit": 90, "goal_fit": 80,
                        "evidence": [{"manuscript_quote": "underwater robotics", "journal_quote": "underwater robotics"}],
                        "rationale": "Fictional scope matches the test manuscript."}
                       for item in first["data"]["assessment_targets"]]
        result = service.recommend(text=IDEA, profile=profile, candidate_records=records,
                                   assessments=assessments, html_path=str(tmp_path / "report.html"))
    summary = result["data"]["display_summary"]
    assert summary["displayed_candidates"] == 15
    assert summary["assessed_candidates"] == 15
    assert summary["shortfall"] == 0
    assert summary["supported_recommendations"] == 0  # no official risk/school clearance in fixture
    assert result["data"]["stage"] == "scored"
    html = Path(result["data"]["html_path"]).read_text(encoding="utf-8")
    assert "Fictional Report Journal 14" in html
    assert ("1200" in html or "1,200" in html) and "USD" in html
    assert "Fictional subjective experience" in html
    assert not home.exists()
    with pytest.raises(JournalError):
        service.recommend(text=IDEA, candidate_records=records, html_path=str(tmp_path / "report.html"))


def test_builtin_fallback_is_read_only_and_explicit_empty_disables_it(tmp_path):
    home = tmp_path / "no-db"
    service = JournalFinder(str(home))
    with patch("paperflow.engine.journals.builder.load_curated_candidates",
               return_value=[JournalRecord.model_validate(r) for r in sample_records()]) as load:
        result = service.recommend(text=IDEA)
        assert result["coverage"]["builtin_read_only"] is True
        assert result["data"]["candidate_count"] == 15
        assert result["data"]["stage"] == "needs_agent_assessment"
        service.recommend(text=IDEA, candidate_records=[])
        service.recommend(text=IDEA, use_builtin=False)
        assert load.call_count == 1
    assert not home.exists()


def test_shortfall_is_reported_without_padding(tmp_path):
    data = JournalFinder(str(tmp_path / "home")).recommend(text=IDEA, candidate_records=sample_records(2))["data"]
    assert data["display_summary"]["displayed_candidates"] == 2
    assert data["display_summary"]["shortfall"] == 8


def test_cli_html_export_and_prepare_only_rejection(tmp_path, capsys):
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps(sample_records()), encoding="utf-8")
    html = tmp_path / "cli.html"
    code = run_journal_cli(["recommend", "--text", IDEA, "--candidates", str(candidates),
                            "--html", str(html), "--data-dir", str(tmp_path / "home"), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["data"]["html_path"] == str(html.resolve())
    assert html.is_file()
    code = run_journal_cli(["recommend", "--text", IDEA, "--prepare-only", "--html", str(html), "--json"])
    assert code == 2 and json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"


def test_cli_build_preview_apply_and_search(tmp_path, capsys):
    path = tmp_path / "records.json"
    path.write_text(json.dumps(sample_records(3)), encoding="utf-8")
    home = tmp_path / "store"
    common = ["build-db", "--input", str(path), "--data-dir", str(home), "--json"]
    assert run_journal_cli(common) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert not home.exists()
    assert run_journal_cli(common + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert len(JournalFinder(str(home)).store.records()) == 3


def test_mcp_new_options_and_build_forwarding():
    with patch("paperflow.server.mcp_server.JournalFinder.recommend", return_value={"status": "success", "data": {}}) as call:
        recommend_journals(text=IDEA, html_path="report.html", overwrite_html=True, use_builtin=False)
        assert call.call_args.kwargs["html_path"] == "report.html"
        assert call.call_args.kwargs["overwrite_html"] is True
        assert call.call_args.kwargs["use_builtin"] is False
    with patch("paperflow.server.mcp_server.JournalFinder.build_database", return_value={"status": "success"}) as call:
        build_journal_database(input_paths=["input.json"], dry_run=False, force=True)
        call.assert_called_once_with(input_paths=["input.json"], dry_run=False, force=True)


def test_homepage_is_https_only_and_rendered_as_safe_link():
    from paperflow.engine.journals.html_reporter import render_html_report
    with pytest.raises(Exception):
        JournalRecord(title="Bad", homepage="javascript:alert(1)")
    with pytest.raises(Exception):
        JournalRecord(title="Bad", homepage="http://insecure.example/journal")
    with pytest.raises(Exception):
        JournalRecord(title="Bad", homepage="https://user:pw@publisher.example/j")
    card = {"journal_id": "j_home", "title": "Linked Journal", "issns": [], "group": "balanced",
            "homepage": "https://publisher.example/journal?a=1&b=2", "missing": [], "score": None}
    plain = {"journal_id": "j_none", "title": "Unlinked Journal", "issns": [], "group": "balanced",
             "missing": [], "score": None}
    result = {"status": "success", "data": {"groups": {"balanced": {"label": "均衡档", "recommended": [],
                                                                     "provisional": [card, plain]}}}}
    html = render_html_report(result)
    assert 'href="https://publisher.example/journal?a=1&amp;b=2"' in html
    assert 'rel="noopener noreferrer"' in html and "官网 ↗" in html
    assert "官网待核验" in html  # missing homepage is shown as unknown, never guessed


def test_every_curated_journal_has_https_homepage():
    from paperflow.engine.journals.builder import load_curated_candidates
    records = load_curated_candidates()
    assert records and all(r.homepage.startswith("https://") for r in records)


def test_open_metadata_preview_never_fetches_or_writes(tmp_path):
    service = JournalFinder(str(tmp_path / "no-db"))
    with patch("paperflow.engine.journals.open_metadata.fetch_doaj_csv", side_effect=AssertionError("Preview cannot fetch")):
        result = service.refresh("open_metadata", "doaj")
        assert result["data"]["dry_run"] is True
        assert result["data"]["license"] == "CC0-1.0"
    assert not (tmp_path / "no-db").exists()
