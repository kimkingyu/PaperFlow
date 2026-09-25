"""Public service tests with fictional, authorised local fixtures only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, Provenance, SearchFilters


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = JournalFinder(str(self.root / "runtime"))
        self.records = [{"title": "Fictional Engineering Journal", "issns": ["1234-5679"],
                         "fields": ["fault_diagnosis"], "indexing": ["SCIE"],
                         "rankings": [{"system": "cas", "year": 2025, "category_type": "major", "quartile": 3}]}]
        self.input = self.root / "records.json"
        self.input.write_text(json.dumps(self.records), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def initialize(self):
        return self.service.import_data("local", str(self.input), kind="records", data_year=2025, dry_run=False)

    def test_no_implicit_database_and_no_fake_results(self):
        self.assertEqual(len(self.service.list_sources()["data"]), 14)
        with self.assertRaises(JournalError) as ctx:
            self.service.search()
        self.assertEqual(ctx.exception.code, "DATA_NOT_INITIALIZED")
        self.assertFalse((self.root / "runtime").exists())

    def test_preview_apply_idempotency_and_details(self):
        preview = self.service.import_data("local", str(self.input), kind="records", data_year=2025)
        self.assertTrue(preview["data"]["dry_run"])
        self.assertFalse((self.root / "runtime").exists())
        applied = self.initialize()
        again = self.initialize()
        self.assertFalse(applied["data"]["dry_run"])
        self.assertEqual(again["data"]["unchanged"], 1)
        detail = self.service.details("1234-5679")
        self.assertEqual(detail["data"]["journal"]["title"], self.records[0]["title"])
        self.assertTrue(list((self.root / "runtime" / "raw").glob("*.snapshot")))

    def test_fuzzy_query_must_not_make_definite_risk_claim(self):
        self.initialize()
        result = self.service.check_warning("Fictional Engineering")
        self.assertEqual(result["status"], "needs_disambiguation")
        self.assertEqual(len(result["data"]["candidates"]), 1)

    def test_strict_requires_evidence(self):
        self.initialize()
        result = self.service.search(filters={"rank_system": "cas", "rank_year": 2025,
                                              "quartiles": [1, 2, 3], "risk_policy": "verified_only"})
        self.assertEqual(result["data"]["results"], [])

    def test_sorting_metrics_requires_explicit_year(self):
        with self.assertRaises(JournalError):
            self.service.search(sort_by="impact")

    def test_bad_filters_do_not_initialize_store(self):
        with self.assertRaises(ValueError):
            SearchFilters(quartiles=[3])
        with self.assertRaises(ValueError):
            SearchFilters(max_apc=1000)
        with self.assertRaises(ValueError):
            SearchFilters(min_annual_articles=1000)
        with self.assertRaises(ValueError):
            Provenance(source_url="https://easyscholar.cc/open?secretKey=DO-NOT-STORE")
        self.assertFalse((self.root / "runtime").exists())

    def test_invalid_metric_ranges_and_years_are_rejected(self):
        from paperflow.engine.journals.models import Metric

        for args in ({"name": "first_decision_days", "value": -1},
                     {"name": "apc", "lower": 200, "upper": 100},
                     {"name": "impact_factor", "value": float("nan")}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                Metric(**args)
        with self.assertRaises(ValueError):
            SearchFilters(warning_years=[1])
        with self.assertRaises(ValueError) as ctx:
            Provenance(observed_at="private-test-secret")
        self.assertNotIn("private-test-secret", str(ctx.exception))

    def test_empty_tracker_only_describes_offline_capability(self):
        result = self.service.tracker()
        self.assertFalse(result["data"]["online_available"])
        self.assertNotIn("latest_status", result["data"])
        self.assertFalse((self.root / "runtime").exists())

    def test_input_exclusivity_and_limits(self):
        with self.assertRaises(JournalError):
            self.service.tracker(data={}, file_path=str(self.input))
        with self.assertRaises(JournalError):
            self.service.peers(references=[], file_path=str(self.input))
        with self.assertRaises(JournalError):
            self.service.peers(references=[{}] * 201)
        with self.assertRaises(JournalError):
            self.service.tracker(provider="not-implemented")

    def test_network_permissions_are_not_implicit(self):
        with patch.dict(os.environ, {"PAPERFLOW_JOURNAL_ALLOWED_SOURCES": "", "PAPERFLOW_EASYSCHOLAR_KEY": ""}):
            with self.assertRaises(JournalError) as ctx:
                self.service.refresh("showjcr", "JCR2025-UTF8.csv", dry_run=False)
            self.assertEqual(ctx.exception.code, "LICENSE_REVIEW_REQUIRED")
            with self.assertRaises(JournalError) as ctx:
                self.service.refresh("easyscholar_api", "Fictional Journal", dry_run=False)
            self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertFalse((self.root / "runtime").exists())

    def test_sqlite_reuses_real_csv_schemas_and_reports_coverage(self):
        import sqlite3
        from contextlib import closing
        from paperflow.engine.journals.importers import parse_file

        database = self.root / "sources # yearly.db"
        title = self.records[0]["title"]
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute('CREATE TABLE JCR2025 (Journal TEXT, ISSN TEXT, "IF(2025)" TEXT, Category_1 TEXT, "IF Quartile(2025)_1" TEXT, Category_2 TEXT, "IF Quartile(2025)_2" TEXT)')
            conn.execute('INSERT INTO JCR2025 VALUES (?,?,?,?,?,?,?)', (title, "1234-5679", "2.5", "Engineering", "Q2", "Computing", "Q3"))
            conn.execute('CREATE TABLE XR2026 (Journal TEXT, 年份 TEXT, 预警标记 TEXT, 大类中文名 TEXT, 大类新锐分区 TEXT, 大类2中文名 TEXT, 大类2新锐分区 TEXT)')
            conn.execute('INSERT INTO XR2026 VALUES (?,?,?,?,?,?,?)', (title, "2026", "Under Review", "工程技术", "2", "计算机", "3"))
            conn.execute('CREATE TABLE GJQKYJMD2025 (Journal TEXT, 预警原因 TEXT)')
            conn.execute('INSERT INTO GJQKYJMD2025 VALUES (?,?)', (title, "Fictional fixture reason"))
        original = database.read_bytes()
        self.service.import_data("showjcr", str(database), dry_run=False)
        self.assertEqual(original, database.read_bytes())
        coverage = self.service.store.coverage()
        self.assertEqual(coverage["warning_years"], [2025])
        self.assertTrue(all(isinstance(d, dict) for d in coverage["snapshots"][0]["coverage"]["datasets"]))
        record = self.service.store.records()[0]
        self.assertEqual(len([r for r in record.rankings if r.system == "jcr"]), 2)
        self.assertEqual(len([r for r in record.rankings if r.system == "xr"]), 2)
        self.assertEqual(len([r for r in record.risks if r.system == "cas_warning"]), 1)
        self.assertEqual(len([r for r in record.risks if r.system == "xr_review"]), 1)
        self.assertEqual(self.service.list_sources("showjcr")["status"], "success")
        with patch("paperflow.engine.journals.importers.MAX_RECORDS_LIMIT", 2):
            with self.assertRaises(JournalError) as ctx:
                parse_file("showjcr", str(database))
        self.assertEqual(ctx.exception.code, "INPUT_TOO_LARGE")

    def test_policy_json_preview_does_not_apply(self):
        path = self.root / "school.json"
        path.write_text(json.dumps({"profile_id": "school", "name": "Fictional school", "source_url": "https://school.example/rules",
                                    "rank_system": "cas", "rank_year": 2025, "allowed_quartiles": [1, 2, 3]}), encoding="utf-8")
        self.service.import_data("local", str(path), kind="school_policy")
        self.assertFalse((self.root / "runtime").exists())
        self.service.import_data("local", str(path), kind="school_policy", dry_run=False)
        self.assertEqual(self.service.store.get_policy("school").rank_year, 2025)


if __name__ == "__main__":
    unittest.main()
