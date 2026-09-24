"""Unit tests for paperflow.engine.journals.search."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from paperflow.engine.journals.models import (
    JournalError,
    JournalRecord,
    Metric,
    Provenance,
    Ranking,
    RiskEvent,
    SchoolPolicy,
    SearchFilters,
)
from paperflow.engine.journals.search import (
    analyze_references,
    compare_records,
    search_records,
)


FIXED_NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


class TestJournalSearch(unittest.TestCase):
    def setUp(self):
        self.cov = {
            "snapshots": [
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {"system": "cas_warning", "year": 2024, "complete": True},
                },
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {"system": "cas_warning", "year": 2025, "complete": True},
                },
                {
                    "source_id": "cas_official",
                    "active": True,
                    "coverage": {"system": "cas_warning", "year": 2026, "complete": True},
                },
            ],
            "warning_years": [2024, 2025, 2026],
        }
        self.rec1 = JournalRecord(
            journal_id="rec1",
            title="IEEE Transactions on Pattern Analysis and Machine Intelligence",
            title_zh="IEEE模式分析与机器智能汇刊",
            issns=["0162-8828"],
            aliases=["TPAMI"],
            fields=["cs_ai", "computer_vision"],
            indexing=["SCI", "SCIE"],
            oa_mode="hybrid",
            rankings=[
                Ranking(system="cas", year=2024, category="计算机科学", quartile=1, top=True),
                Ranking(system="ccf", year=2024, grade="A"),
            ],
            metrics=[
                Metric(name="first_decision_days", stage="first_decision", value=45.0, comparator="le", unit="days"),
                Metric(name="impact_factor", year=2024, value=20.8),
                Metric(name="annual_articles", year=2024, value=300),
                Metric(name="apc", value=2490.0, currency="USD"),
            ],
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

        self.rec2 = JournalRecord(
            journal_id="rec2",
            title="Cybersecurity and Privacy Review",
            title_zh="网络安全与隐私评论",
            issns=["2222-3333"],
            fields=["cybersecurity"],
            indexing=["SCIE"],
            oa_mode="full",
            rankings=[
                Ranking(system="cas", year=2024, category="网络空间安全", quartile=2, top=False),
            ],
            metrics=[
                Metric(name="first_decision_days", stage="first_decision", upper=60.0, comparator="range", unit="days"),
                Metric(name="impact_factor", year=2024, value=5.2),
                Metric(name="annual_articles", year=2024, value=800),
                Metric(name="apc", value=1500.0, currency="USD"),
            ],
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

        self.rec3 = JournalRecord(
            journal_id="rec3",
            title="Warning Journal of Fault Diagnosis",
            issns=["4444-5555"],
            fields=["fault_diagnosis"],
            indexing=["SCIE"],
            oa_mode="closed",
            rankings=[
                Ranking(system="cas", year=2024, category="机械工程", quartile=4),
            ],
            risks=[
                RiskEvent(system="cas_warning", value="flagged", year=2024, reason="High warning"),
            ],
        )

    def test_search_relevance_and_query(self):
        records = [self.rec1, self.rec2, self.rec3]
        res = search_records(records, query="TPAMI", coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["total"], 2)  # rec3 flagged, excluded by default
        self.assertEqual(res["data"]["results"][0]["journal_id"], "rec1")

    def test_search_cas_quartile_filtering(self):
        records = [self.rec1, self.rec2, self.rec3]
        # Q1 and Q2 desired
        filters = SearchFilters(
            rank_system="cas",
            rank_year=2024,
            quartiles=[1, 2],
        )
        res = search_records(records, filters=filters, coverage=self.cov, as_of=FIXED_NOW)
        # rec1 (Q1) and rec2 (Q2) pass, rec3 flagged
        self.assertEqual(res["total"], 2)
        
        # Only Q1 desired
        f_q1 = SearchFilters(rank_system="cas", rank_year=2024, quartiles=[1])
        res_q1 = search_records(records, filters=f_q1, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res_q1["total"], 1)
        self.assertEqual(res_q1["data"]["results"][0]["journal_id"], "rec1")
        self.assertIn("quartiles", res_q1["rejected_constraints"])

    def test_search_esci_cannot_replace_sci_scie(self):
        rec_esci = JournalRecord(
            journal_id="esci_rec",
            title="Emerging Science",
            issns=["7777-8888"],
            indexing=["ESCI"],
        )
        filters = SearchFilters(indexing=["SCI"])
        res = search_records([rec_esci], filters=filters, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["total"], 0)
        self.assertIn("indexing", res["rejected_constraints"])

    def test_search_hard_constraints_missing_and_allow_unknown(self):
        rec_nodata = JournalRecord(
            journal_id="nodata",
            title="Incomplete Journal",
            issns=["8888-9999"],
            indexing=["SCIE"],
        )
        # Filter requires max_apc <= 2000 USD
        filters = SearchFilters(max_apc=2000.0, currency="USD", allow_unknown=False)
        res = search_records([rec_nodata], filters=filters, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["total"], 0)
        self.assertEqual(res["total_provisional"], 0)
        self.assertIn("max_apc", res["rejected_constraints"])

        # When allow_unknown is True -> goes to provisional_results
        f_allow = SearchFilters(max_apc=2000.0, currency="USD", allow_unknown=True)
        res_allow = search_records([rec_nodata], filters=f_allow, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res_allow["total"], 0)
        self.assertEqual(res_allow["total_provisional"], 1)
        self.assertEqual(res_allow["data"]["provisional_results"][0]["journal_id"], "nodata")

    def test_search_conflicting_sources_anti_cherrypicking(self):
        # Record has two conflicting first_decision_days metrics: 20 days and 60 days
        rec_conflict = JournalRecord(
            journal_id="conflict_fd",
            title="Conflicting Speed Journal",
            indexing=["SCIE"],
            metrics=[
                Metric(name="first_decision_days", stage="first_decision", value=20.0, comparator="eq", unit="days"),
                Metric(name="first_decision_days", stage="first_decision", value=60.0, comparator="eq", unit="days"),
            ],
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
        # Limit is 30 days -> must NOT cherry-pick 20.0 to pass; worst-case 60.0 fails limit!
        filters = SearchFilters(max_first_decision_days=30.0)
        res = search_records([rec_conflict], filters=filters, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["total"], 0)
        self.assertIn("max_first_decision_days", res["rejected_constraints"])

    def test_search_unbounded_first_decision_days_fails_limit(self):
        rec_unbounded = JournalRecord(
            journal_id="unbounded",
            title="Unbounded Cycle Journal",
            indexing=["SCIE"],
            metrics=[
                Metric(name="first_decision_days", stage="first_decision", value=30.0, comparator="gt", unit="days")
            ],
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
        filters = SearchFilters(max_first_decision_days=40.0)
        res = search_records([rec_unbounded], filters=filters, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["total"], 0)
        self.assertIn("max_first_decision_days", res["rejected_constraints"])

    def test_search_sorting(self):
        records = [self.rec1, self.rec2]
        
        # Sort by impact
        filters_impact = SearchFilters(metric_year=2024)
        res_impact = search_records(records, filters=filters_impact, sort_by="impact", coverage=self.cov, as_of=FIXED_NOW)
        # rec1 IF 20.8 > rec2 IF 5.2
        self.assertEqual(res_impact["data"]["results"][0]["journal_id"], "rec1")

        # Sort by volume
        filters_vol = SearchFilters(metric_year=2024)
        res_vol = search_records(records, filters=filters_vol, sort_by="volume", coverage=self.cov, as_of=FIXED_NOW)
        # rec2 vol 800 > rec1 vol 300
        self.assertEqual(res_vol["data"]["results"][0]["journal_id"], "rec2")

        # Sort by speed
        res_speed = search_records(records, sort_by="speed", coverage=self.cov, as_of=FIXED_NOW)
        # rec1 first_decision 45 < rec2 60
        self.assertEqual(res_speed["data"]["results"][0]["journal_id"], "rec1")

    def test_compare_records_up_to_10_and_limit(self):
        records = [self.rec1, self.rec2, self.rec3]
        res = compare_records(records, journal_ids=["rec1", "0162-8828", "rec3"], coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["data"]["comparisons"]), 3)
        self.assertIn("missing_ids", res["data"])

        # Exceeding 10 raises JournalError
        with self.assertRaises(JournalError) as ctx:
            compare_records(records, journal_ids=[f"id_{i}" for i in range(11)])
        self.assertEqual(ctx.exception.code, "EXCEEDED_LIMIT")

    def test_analyze_references_dedup_and_unknown_doi(self):
        records = [self.rec1, self.rec2]
        refs = [
            {"doi": "10.1109/TPAMI.2023.123", "journal": "IEEE Transactions on Pattern Analysis and Machine Intelligence"},
            {"doi": "https://doi.org/10.1109/TPAMI.2023.123", "journal": "TPAMI"},  # Duplicate DOI
            {"doi": "10.1000/182", "journal": "Cybersecurity and Privacy Review"},
            {"doi": "10.1000/no_meta_doi"},  # DOI with no metadata -> must be flagged as missing metadata, no guessing
        ]
        res = analyze_references(records, refs, coverage=self.cov, as_of=FIXED_NOW)
        self.assertEqual(res["status"], "success")
        data = res["data"]
        self.assertEqual(data["total_references_analyzed"], 4)
        self.assertEqual(data["deduplicated_references_count"], 2)  # 2 valid deduplicated
        self.assertEqual(len(data["missing_metadata_dois"]), 1)
        self.assertEqual(data["missing_metadata_dois"][0], "10.1000/no_meta_doi")
        self.assertEqual(len(data["distribution"]), 2)
        
        # Verify JSON serializability
        json.dumps(res)


if __name__ == "__main__":
    unittest.main()
