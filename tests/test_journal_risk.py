"""Unit tests for paperflow.engine.journals.risk."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from paperflow.engine.journals.models import (
    JournalError,
    JournalRecord,
    Provenance,
    Ranking,
    RiskEvent,
    SchoolPolicy,
    SearchFilters,
)
from paperflow.engine.journals.risk import assess_risk


FIXED_NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

VALID_COVERAGE = {
    "snapshots": [
        {
            "source_id": "cas_official",
            "active": True,
            "data_year": 2024,
            "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
        },
        {
            "source_id": "cas_official",
            "active": True,
            "data_year": 2025,
            "coverage": {"system": "cas_warning", "year": 2025, "complete": True},
        },
        {
            "source_id": "cas_official",
            "active": True,
            "data_year": 2026,
            "coverage": {"system": "cas_warning", "year": 2026, "complete": True},
        },
    ],
    "warning_years": [2024, 2025, 2026],
}


class TestJournalRisk(unittest.TestCase):
    def test_invalid_record_raises_journal_error(self):
        with self.assertRaises(JournalError) as ctx:
            assess_risk("not_a_record", as_of=FIXED_NOW)  # type: ignore
        self.assertEqual(ctx.exception.code, "INVALID_RECORD")

    def test_cas_warning_flagged_in_target_year(self):
        rec = JournalRecord(
            journal_id="j1",
            title="Flagged Journal",
            issns=["1234-5678"],
            risks=[
                RiskEvent(
                    system="cas_warning",
                    value="flagged",
                    year=2024,
                    level="High",
                    reason="Paper mill concerns",
                )
            ],
        )
        filters = SearchFilters(warning_years=[2024], risk_policy="exclude_known")
        res = assess_risk(rec, coverage=VALID_COVERAGE, filters=filters, as_of=FIXED_NOW)
        self.assertEqual(res["status"], "success")
        data = res["data"]
        self.assertEqual(data["conclusion"], "flagged")
        self.assertTrue(data["blocked"])
        cas_check = next(c for c in data["checks"] if c["system"] == "cas_warning")
        self.assertEqual(cas_check["state"], "flagged")
        self.assertEqual(cas_check["year"], 2024)

    def test_coverage_strict_active_and_parent_false_not_bypassed(self):
        rec = JournalRecord(title="Test Journal", issns=["1111-2222"])

        # 1. Missing active or string 'true' / 'false' is rejected
        cov_missing_active = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    # active missing!
                    "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
                }
            ]
        }
        res1 = assess_risk(rec, coverage=cov_missing_active, filters=SearchFilters(warning_years=[2024]), as_of=FIXED_NOW)
        c1 = next(c for c in res1["data"]["checks"] if c["system"] == "cas_warning")
        self.assertEqual(c1["state"], "unknown")

        cov_string_active = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    "active": "true",  # string! rejected
                    "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
                }
            ]
        }
        res2 = assess_risk(rec, coverage=cov_string_active, filters=SearchFilters(warning_years=[2024]), as_of=FIXED_NOW)
        c2 = next(c for c in res2["data"]["checks"] if c["system"] == "cas_warning")
        self.assertEqual(c2["state"], "unknown")

        # 2. Parent coverage.complete == False CANNOT be bypassed by child datasets.complete=True
        cov_bypassed_attempt = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {
                        "system": "cas_warning",
                        "year": 2024,
                        "complete": False,  # Parent is explicitly False!
                        "datasets": [
                            {"system": "cas_warning", "year": 2024, "complete": True}
                        ]
                    },
                }
            ]
        }
        res3 = assess_risk(rec, coverage=cov_bypassed_attempt, filters=SearchFilters(warning_years=[2024]), as_of=FIXED_NOW)
        c3 = next(c for c in res3["data"]["checks"] if c["system"] == "cas_warning")
        self.assertEqual(c3["state"], "unknown")

        # 3. SQLite integer 1 is accepted for active
        cov_sqlite_int = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    "active": 1,  # SQLite integer 1 accepted
                    "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
                }
            ]
        }
        res4 = assess_risk(rec, coverage=cov_sqlite_int, filters=SearchFilters(warning_years=[2024]), as_of=FIXED_NOW)
        c4 = next(c for c in res4["data"]["checks"] if c["system"] == "cas_warning")
        self.assertEqual(c4["state"], "not_listed")

    def test_cas_superseded_in_same_year(self):
        # Journal has a superseded/historical warning event in 2024, and current complete 2024 snapshot has no active flag
        rec = JournalRecord(
            journal_id="superseded_j",
            title="Superseded Journal",
            issns=["3333-4444"],
            risks=[
                RiskEvent(
                    system="cas_warning",
                    value="flagged",
                    year=2024,
                    historical=True,
                    reason="Superseded 2024 provisional warning",
                )
            ],
        )
        filters = SearchFilters(warning_years=[2024], risk_policy="exclude_known")
        res = assess_risk(rec, coverage=VALID_COVERAGE, filters=filters, as_of=FIXED_NOW)
        data = res["data"]
        # In current check, status is not_listed with historical=True
        cas_check = next(c for c in data["checks"] if c["system"] == "cas_warning")
        self.assertEqual(cas_check["state"], "not_listed")
        self.assertTrue(cas_check["historical"])
        # Main conclusion is historical_caution, NOT flagged
        self.assertEqual(data["conclusion"], "historical_caution")
        # In exclude_known, historical_caution is not blocked!
        self.assertFalse(data["blocked"])

    def test_xr_historical_does_not_block_as_current_under_review(self):
        rec = JournalRecord(
            journal_id="hist_xr",
            title="Historical XR Journal",
            issns=["5555-6666"],
            risks=[
                RiskEvent(
                    system="xr_review",
                    value="under_review",
                    year=2023,
                    historical=True,
                    reason="Historical 2023 review resolved",
                )
            ],
        )
        res = assess_risk(rec, coverage=VALID_COVERAGE, as_of=FIXED_NOW)
        xr_check = next(c for c in res["data"]["checks"] if c["system"] == "xr_review")
        self.assertEqual(xr_check["state"], "not_applicable")
        self.assertTrue(xr_check["historical"])
        self.assertFalse(res["data"]["blocked"])
        self.assertEqual(res["data"]["conclusion"], "historical_caution")

    def test_historical_positive_coexists_with_current_complete_list(self):
        # A journal was warned in 2020. The current 2026 complete list does not include it.
        # It must be not_listed for 2026, but historical_caution must remain True!
        rec = JournalRecord(
            journal_id="hist_pos",
            title="Past Warned Journal",
            issns=["7777-8888"],
            risks=[
                RiskEvent(
                    system="cas_warning",
                    value="flagged",
                    year=2020,
                    historical=True,
                    reason="2020 warning",
                )
            ],
        )
        filters = SearchFilters(warning_years=[2026], risk_policy="exclude_known")
        res = assess_risk(rec, coverage=VALID_COVERAGE, filters=filters, as_of=FIXED_NOW)
        data = res["data"]
        cas_check = next(c for c in data["checks"] if c["system"] == "cas_warning")
        self.assertEqual(cas_check["state"], "not_listed")
        self.assertTrue(data["historical_caution"])
        self.assertEqual(data["conclusion"], "historical_caution")
        self.assertFalse(data["blocked"])

    def test_no_warning_years_specified_missing_current_year_defaults_to_unknown(self):
        rec = JournalRecord(title="Clean Journal", issns=["8888-9999"])
        # Coverage only has 2020 and 2024, but current year as_of is 2026
        past_only_cov = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {"system": "cas_warning", "year": 2020, "complete": True},
                },
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
                },
            ]
        }
        res = assess_risk(rec, coverage=past_only_cov, as_of=FIXED_NOW)
        cas_check = next(c for c in res["data"]["checks"] if c["system"] == "cas_warning")
        # Missing current year (2026) coverage -> state must be unknown, reporting existing covered years
        self.assertEqual(cas_check["state"], "unknown")
        self.assertIn("2026", cas_check["evidence"])
        self.assertIn("2020, 2024", cas_check["evidence"])
        self.assertTrue(res["data"]["needs_verification"])

    def test_clarivate_authority_without_official_host_fails(self):
        rec = JournalRecord(
            title="Fake Official Journal",
            risks=[
                RiskEvent(
                    system="clarivate",
                    value="indexed",
                    provenance=Provenance(
                        source_url="https://some-mirror-site.com/clarivate.html",
                        observed_at="2026-04-01T00:00:00Z",
                        authority="official",
                    ),
                )
            ],
        )
        res = assess_risk(rec, coverage=VALID_COVERAGE, as_of=FIXED_NOW)
        cl_check = next(c for c in res["data"]["checks"] if c["system"] == "clarivate")
        self.assertEqual(cl_check["state"], "unknown")
        self.assertTrue(res["data"]["needs_verification"])

    def test_clarivate_historical_on_hold_with_fresh_indexed_causes_conflict(self):
        rec = JournalRecord(
            title="Conflicting Journal",
            risks=[
                RiskEvent(
                    system="clarivate",
                    value="on_hold",
                    year=2024,
                    reason="Under Clarivate editorial investigation",
                    provenance=Provenance(source_id="clarivate_alert"),
                ),
                RiskEvent(
                    system="clarivate",
                    value="indexed",
                    provenance=Provenance(
                        source_url="https://mjl.clarivate.com/journal",
                        observed_at="2026-04-01T00:00:00Z",
                        authority="official",
                    ),
                ),
            ],
        )
        res = assess_risk(rec, coverage=VALID_COVERAGE, as_of=FIXED_NOW)
        cl_check = next(c for c in res["data"]["checks"] if c["system"] == "clarivate")
        self.assertEqual(cl_check["state"], "conflict")
        self.assertEqual(res["data"]["conclusion"], "needs_verification")

    def test_identity_warnings_triggers_needs_verification_and_blocks_verified_only(self):
        rec = JournalRecord(
            title="Ambiguous Journal",
            issns=["1234-5678"],
            identity_warnings=["Name matches multiple distinct ISSN records; entity unlinked"],
            risks=[
                RiskEvent(
                    system="clarivate",
                    value="indexed",
                    provenance=Provenance(
                        source_url="https://mjl.clarivate.com/journal",
                        observed_at="2026-04-01T00:00:00Z",
                        authority="official",
                    ),
                )
            ],
        )
        f_ver = SearchFilters(warning_years=[2024], risk_policy="verified_only")
        res = assess_risk(rec, coverage=VALID_COVERAGE, filters=f_ver, as_of=FIXED_NOW)
        id_check = next(c for c in res["data"]["checks"] if c["system"] == "identity")
        self.assertEqual(id_check["state"], "conflict")
        self.assertTrue(res["data"]["needs_verification"])
        self.assertTrue(res["data"]["blocked"])

    def test_verified_only_truly_passes_with_strict_clearance(self):
        rec = JournalRecord(
            title="Clean Journal",
            issns=["1234-5678"],
            indexing=["SCIE"],
            risks=[
                RiskEvent(
                    system="clarivate",
                    value="indexed",
                    provenance=Provenance(
                        source_url="https://mjl.clarivate.com/journal",
                        observed_at="2026-04-01T00:00:00Z",
                        authority="official",
                    ),
                )
            ],
        )
        filters = SearchFilters(warning_years=[2024, 2025], risk_policy="verified_only")
        res = assess_risk(rec, coverage=VALID_COVERAGE, filters=filters, as_of=FIXED_NOW)
        self.assertEqual(res["data"]["conclusion"], "no_known_flags_in_checked_sources")
        self.assertFalse(res["data"]["blocked"])
        self.assertFalse(res["data"]["needs_verification"])


if __name__ == "__main__":
    unittest.main()
