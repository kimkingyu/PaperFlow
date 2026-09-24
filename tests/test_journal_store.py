"""Storage invariants; all records are fictional and live in temporary folders."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from paperflow.engine.journals.catalog import get_source, get_sources
from paperflow.engine.journals.identity import normalize_issn, normalize_name
from paperflow.engine.journals.models import JournalError, JournalRecord, ParsedBatch, Provenance, Ranking, RiskEvent, SchoolPolicy
from paperflow.engine.journals.store import JournalStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "not-created"
        self.store = JournalStore(str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, title="Fictional Systems", issns=None, **kw):
        return JournalRecord(title=title, issns=["1234-5679"] if issns is None else issns, **kw)

    def put(self, records, dataset="JCR2025.csv", checksum="a", source="local", dry_run=False):
        return self.store.import_batch(ParsedBatch(records=records, total_rows=len(records)), source, dataset,
                                       "fixture-v1", hashlib.sha256(checksum.encode()).hexdigest(), dataset, 2025, dry_run)

    def test_empty_and_preview_are_inert(self):
        self.assertEqual(self.store.records(), [])
        self.assertEqual(self.store.coverage()["snapshots"], [])
        self.put([self.record()], dry_run=True)
        self.assertFalse(self.home.exists())

    def test_read_view_uses_one_snapshot_during_refresh(self):
        from unittest.mock import patch

        first = self.put([self.record()])
        original_coverage = self.store.coverage
        refreshed = []

        def refresh_before_coverage(_connection=None):
            if _connection is not None and not refreshed:
                refreshed.append(self.put([self.record(title="Updated Fiction")], checksum="refresh"))
            return original_coverage(_connection=_connection)

        with patch.object(self.store, "coverage", side_effect=refresh_before_coverage):
            records, coverage, policy = self.store.read_view()
        self.assertEqual(records[0].title, "Fictional Systems")
        self.assertEqual(coverage["snapshots"][0]["snapshot_id"], first["snapshot_id"])
        self.assertIsNone(policy)
        self.assertNotEqual(self.store.coverage()["snapshots"][0]["snapshot_id"], first["snapshot_id"])

    def test_catalog_has_twelve_references_without_runtime_creation(self):
        sources = get_sources()
        self.assertEqual(len(sources), 12)
        self.assertEqual(get_source("showjcr")["mode"], "import_only")
        self.assertFalse(self.home.exists())

    def test_identity_and_idempotency(self):
        a = self.put([self.record()])
        b = self.put([self.record()])
        self.assertEqual(a["snapshot_id"], b["snapshot_id"])
        self.assertEqual(b["unchanged"], 1)
        self.assertEqual(len(self.store.snapshots()), 1)
        self.assertEqual(self.store.records()[0].issns, ["1234-5679"])

    def test_invalid_issn_preserves_snapshot(self):
        self.put([self.record()])
        with self.assertRaises(JournalError):
            self.put([self.record(issns=["1234-5678"])], checksum="bad")
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_schema_and_years_do_not_overwrite(self):
        self.put([self.record(rankings=[Ranking(system="cas", year=2025, quartile=3)])], dataset="cas2025")
        self.put([self.record(rankings=[Ranking(system="xr", year=2026, quartile=1)])], dataset="xr2026")
        r = self.store.records()[0]
        self.assertEqual({(v.system, v.year, v.quartile) for v in r.rankings}, {("cas", 2025, 3), ("xr", 2026, 1)})

    def test_removed_positive_risk_remains_historical(self):
        self.put([self.record(risks=[RiskEvent(system="cas_warning", value="flagged", year=2025)])])
        self.put([self.record()], checksum="new")
        r = self.store.records()[0]
        self.assertEqual(len(r.risks), 1)
        self.assertTrue(r.risks[0].historical)
        self.assertEqual(sum(s["active"] for s in self.store.snapshots()), 1)

    def test_removed_journal_warning_remains_searchable(self):
        self.put([self.record(risks=[RiskEvent(system="cas_warning", value="flagged", year=2025)])])
        self.put([self.record(title="Replacement Fiction", issns=["2049-3630"])], checksum="replacement")
        old = next(r for r in self.store.records() if r.title == "Fictional Systems")
        self.assertTrue(old.risks[0].historical)
        self.assertTrue(old.identity_warnings)
        self.assertEqual(old.indexing, [])

    def test_changed_title_is_preserved_as_alias_in_history(self):
        self.put([self.record()])
        self.put([self.record(title="Renamed Fiction")], checksum="renamed")
        history = self.store.records(include_history=True)[0]
        self.assertIn("Renamed Fiction", [history.title] + history.aliases)
        self.assertEqual(len(history.metadata_observations), 2)

    def test_later_homonym_does_not_inherit_title_only_warning(self):
        self.put([self.record()])
        self.put([self.record(issns=[], risks=[RiskEvent(system="cas_warning", value="flagged", year=2025)])], dataset="warnings")
        self.assertEqual(len(self.store.records()[0].risks), 1)
        self.put([self.record(issns=["2049-3630"])], dataset="homonym")
        strong = [r for r in self.store.records() if r.issns]
        self.assertEqual(len(strong), 2)
        self.assertTrue(all(r.identity_warnings for r in strong))
        self.assertTrue(all(not r.risks for r in strong))

    def test_shrink_refused_and_state_preserved(self):
        self.put([self.record(title="Fake %d" % i, issns=[]) for i in range(4)])
        with self.assertRaises(JournalError):
            self.put([self.record()], checksum="short")
        self.assertEqual(len(self.store.records()), 4)
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_policy_preview_and_explicit_selection(self):
        policy = SchoolPolicy(profile_id="sample", name="Fictional school", prohibited_issns=["1234-5679"])
        self.store.save_policy(policy)
        self.assertFalse(self.home.exists())
        self.store.save_policy(policy, dry_run=False)
        self.assertIsNone(self.store.get_policy(None))
        self.assertEqual(self.store.get_policy("sample").name, policy.name)
        with self.assertRaises(JournalError):
            self.store.get_policy("not-existing")

    def test_conflicting_issn_binding_refused(self):
        self.put([self.record(), self.record(title="Another Fictional", issns=["2049-3630"])])
        with self.assertRaises(JournalError):
            self.put([self.record(issns=["1234-5679", "2049-3630"])], checksum="conflict")
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_normalization_keeps_distinctions(self):
        self.assertEqual(normalize_issn("12345679"), "1234-5679")
        self.assertEqual(normalize_name("  Fictional   SYSTEMS "), "fictional systems")
        with self.assertRaises(JournalError):
            normalize_issn("1234-5678")


if __name__ == "__main__":
    unittest.main()
