"""Offline incremental-schema, CAS-history and permanent-reading-pin regressions."""
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_literature_service import FakeProviders, card_for, pdf_bytes, record
from test_literature_writing_core import (
    assessment, draft_for, fake_export, local_project, no_network, profile,
    selected_project, writing,
)
from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.service import LiteratureService
from paperflow.engine.literature.store import PaperStore
from paperflow.engine.literature.writing_service import PaperWritingService
from paperflow.engine.literature.writing_store import WritingStore


def reading_count(writing, paper_id):
    with writing.library.connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM readings WHERE paper_id=?", (paper_id,)).fetchone()[0]


def generate_more_cards(writing, paper_id, count=25):
    base = card_for(writing.literature.read(paper_id))
    for number in range(count):
        card = copy.deepcopy(base)
        card["summary"] = "Subsequent non-evidence summary " + str(number)
        card["claims"][0]["text"] = "The author describes another reading " + str(number)
        writing.literature.save_reading(paper_id, card)


def test_selected_reading_survives_last_twenty_cleanup(writing, tmp_path, monkeypatch):
    selected = selected_project(writing, tmp_path)
    pid = selected["selected_paper_ids"][0]
    original = selected["evidence_matrix"][0]
    writing.save_draft(selected["project_id"], draft_for(selected), 2)
    generate_more_cards(writing, pid)
    assert reading_count(writing, pid) == 26
    with writing.library.connection() as conn:
        assert conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (original["reading_id"],)).fetchone()
        assert conn.execute("SELECT 1 FROM writing_reading_pins WHERE reading_id=?", (original["reading_id"],)).fetchone()
    rows = writing.get(selected["project_id"], evidence_limit=100)["data"]["evidence_matrix"]
    assert original in rows
    payloads = fake_export(monkeypatch)
    assert writing.render_docx(selected["project_id"])
    assert original["citation_id"] in payloads[0]["citations"]


def test_fulltext_assessment_not_selected_also_pins_its_basis(writing, tmp_path):
    created = local_project(writing, tmp_path)
    original = created["evidence_matrix"][0]
    pid = original["paper_id"]
    writing.assess(created["project_id"], [assessment(created["candidates"][0], basis="fulltext",
                                                     evidence_ids=[original["citation_id"]])],
                   1, selected_paper_ids=[])
    generate_more_cards(writing, pid)
    assert reading_count(writing, pid) == 21
    project = writing.get(created["project_id"], evidence_limit=100)["data"]
    assert original in project["evidence_matrix"]
    assert project["assessments"][0]["basis"] == "fulltext"


def test_history_pins_survive_deselection_and_current_hash_still_invalidates(writing, tmp_path, monkeypatch):
    selected = selected_project(writing, tmp_path)
    original = selected["evidence_matrix"][0]
    writing.save_draft(selected["project_id"], draft_for(selected), 2)
    writing.assess(selected["project_id"], [], 3, selected_paper_ids=[])
    generate_more_cards(writing, original["paper_id"])
    payloads = fake_export(monkeypatch)
    assert writing.render_docx(selected["project_id"], revision=3)
    with pytest.raises(JournalError) as exc:
        writing.render_docx(selected["project_id"])
    assert exc.value.code == "UNSELECTED_EVIDENCE"
    new_sha = writing.library.cache_pdf(pdf_bytes(["Updated version of the actual paper."]))
    writing.library.set_acquisition(original["paper_id"], {"status": "imported", "file_sha256": new_sha})
    old = writing.get(selected["project_id"], revision=3)["data"]
    assert original["citation_id"] not in {r["citation_id"] for r in old["evidence_matrix"]}
    assert any(g["code"] == "STALE_EVIDENCE" for g in old["gaps"])
    with pytest.raises(JournalError):
        writing.render_docx(selected["project_id"], revision=3)
    assert len(payloads) == 1
    with writing.library.connection() as conn:
        assert conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (original["reading_id"],)).fetchone()


def test_fresh_unselected_paper_keeps_legacy_twenty_only(writing, tmp_path):
    created = local_project(writing, tmp_path)
    original = created["evidence_matrix"][0]
    generate_more_cards(writing, original["paper_id"])
    assert reading_count(writing, original["paper_id"]) == 20
    assert original["citation_id"] not in {r["citation_id"] for r in writing.get(created["project_id"])["data"]["evidence_matrix"]}
    with writing.library.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM writing_reading_pins").fetchone()[0] == 0


def test_get_and_prepare_writing_do_not_mutate_or_pin(writing, tmp_path):
    created = local_project(writing, tmp_path)
    with writing.library.connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM writing_revisions").fetchone()[0]
    writing.get(created["project_id"])
    writing.prepare_writing(created["project_id"])
    with writing.library.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM writing_reading_pins").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM writing_revisions").fetchone()[0] == before


@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_cold_writing_library_initialization(tmp_path, iteration):
    root = tmp_path / ("concurrent-writing-" + str(iteration))
    instances = [PaperWritingService(literature=LiteratureService(str(root), providers=FakeProviders())) for _ in range(6)]
    def create(number):
        instance = instances[number % 6]
        return instance.create(profile(title="Cold project " + str(number)))["data"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        projects = list(pool.map(create, range(18)))
    assert len({p["project_id"] for p in projects}) == 18
    assert instances[0].list(limit=100)["data"]["total"] == 18
    with instances[0].library.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM writing_revisions").fetchone()[0] == 18
    assert all(not instance.literature.providers.calls for instance in instances)


def test_competing_cas_saves_have_exactly_one_winner(writing, tmp_path):
    selected = selected_project(writing, tmp_path)
    original = draft_for(selected)
    def save(number):
        draft = copy.deepcopy(original)
        draft["title"] = "Concurrent attempt " + str(number)
        try:
            result = writing.save_draft(selected["project_id"], draft, 2)
            return result["data"]["draft"]["title"]
        except JournalError as exc:
            assert exc.code == "REVISION_CONFLICT"
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(save, range(12)))
    winners = [x for x in outcomes if x is not None]
    assert len(winners) == 1
    current = writing.get(selected["project_id"])["data"]
    assert current["revision"] == 3 and current["draft"]["title"] == winners[0]
    with writing.library.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM writing_revisions WHERE project_id=?", (selected["project_id"],)).fetchone()[0] == 3


def test_cas_guard_detects_metadata_change_between_validation_and_commit(writing, tmp_path, monkeypatch):
    selected = selected_project(writing, tmp_path)
    original_save = writing.store.save
    changed = copy.deepcopy(selected["candidates"][0]["paper"])
    changed["title"] = "Racing real metadata update"
    def racing_save(*args, **kwargs):
        writing.library.put(changed)
        return original_save(*args, **kwargs)
    monkeypatch.setattr(writing.store, "save", racing_save)
    with pytest.raises(JournalError) as exc:
        writing.save_draft(selected["project_id"], draft_for(selected), 2)
    assert exc.value.code == "METADATA_MISMATCH"
    assert writing.store.get(selected["project_id"])["revision"] == 2


def test_cas_guard_detects_file_version_change_between_validation_and_commit(writing, tmp_path, monkeypatch):
    selected = selected_project(writing, tmp_path)
    original_save = writing.store.save
    sha = writing.library.cache_pdf(pdf_bytes(["Racing replacement version"]))
    def racing_save(*args, **kwargs):
        writing.library.set_acquisition(selected["selected_paper_ids"][0], {"status": "imported", "file_sha256": sha})
        return original_save(*args, **kwargs)
    monkeypatch.setattr(writing.store, "save", racing_save)
    with pytest.raises(JournalError) as exc:
        writing.save_draft(selected["project_id"], draft_for(selected), 2)
    assert exc.value.code == "STALE_EVIDENCE"
    assert writing.store.get(selected["project_id"])["revision"] == 2


def test_old_library_schema_and_wal_are_preserved(tmp_path):
    root = tmp_path / "old-library"
    root.mkdir()
    path = root / "papers.sqlite3"
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    library = PaperStore(str(root))
    saved = library.put(record())
    service = PaperWritingService(literature=LiteratureService(str(root), providers=FakeProviders()))
    project = service.create(profile(), paper_ids=[saved["paper_id"]])["data"]
    assert library.get(saved["paper_id"])["title"] == saved["title"]
    assert library.list()["total"] == 1
    assert service.list()["data"]["total"] == 1
    assert service.get(project["project_id"])["data"]["revision"] == 1
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_empty_existing_database_read_remains_inert(tmp_path):
    root = tmp_path / "empty-db"
    root.mkdir()
    path = root / "papers.sqlite3"
    with sqlite3.connect(str(path)):
        pass
    before = path.read_bytes()
    store = WritingStore(str(root))
    assert store.list() == {"projects": [], "total": 0}
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["extra", "profile", "boolean_revision", "unknown_paper", "bad_json"])
def test_corrupt_snapshot_has_journal_error_without_input_leak(writing, change):
    created = writing.create(profile())["data"]
    with writing.library.connection(True) as conn:
        row = conn.execute("SELECT snapshot FROM writing_projects WHERE project_id=?", (created["project_id"],)).fetchone()
        payload = json.loads(row[0])
        if change == "extra": payload["api_key"] = "NEVER-LEAK-THIS"
        elif change == "profile": payload["profile"] = None
        elif change == "boolean_revision": payload["revision"] = True
        elif change == "unknown_paper": payload["selected_paper_ids"] = ["paper-" + "f" * 24]
        serialized = "{NEVER-LEAK-THIS broken}" if change == "bad_json" else json.dumps(payload)
        conn.execute("UPDATE writing_projects SET snapshot=? WHERE project_id=?", (serialized, created["project_id"]))
    with pytest.raises(JournalError) as exc:
        writing.get(created["project_id"])
    assert exc.value.code == "WRITING_STORE_ERROR"
    assert "NEVER-LEAK" not in str(exc.value)


def test_failed_pin_transaction_retains_original_revision(writing):
    created = writing.create(profile())["data"]
    snapshot = writing.store.get(created["project_id"])
    with pytest.raises(JournalError) as exc:
        writing.store.save(created["project_id"], snapshot, 1, "pin invalid", reading_ids=["reading-missing"])
    assert exc.value.code == "STALE_EVIDENCE"
    assert writing.store.get(created["project_id"])["revision"] == 1
    with writing.library.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM writing_revisions").fetchone()[0] == 1


@pytest.mark.parametrize("bad", [True, False, 0, -1, "1", 1.5])
def test_revision_strict_integer_in_store(writing, bad):
    created = writing.create(profile())["data"]
    snapshot = writing.store.get(created["project_id"])
    with pytest.raises(JournalError):
        writing.store.save(created["project_id"], snapshot, bad, "invalid revision")
    with pytest.raises(JournalError):
        writing.store.get(created["project_id"], revision=bad)


def test_list_pagination_and_historical_snapshot_are_stable(writing):
    projects = [writing.create(profile(title="Project " + str(i)))["data"] for i in range(3)]
    listed = writing.list(limit=2)["data"]
    assert listed["total"] == 3 and len(listed["projects"]) == 2
    tail = writing.list(limit=2, offset=2)["data"]
    assert len(tail["projects"]) == 1
    assert {p["project_id"] for p in listed["projects"] + tail["projects"]} == {p["project_id"] for p in projects}
    initial = projects[0]
    assert writing.get(initial["project_id"], revision=1)["data"]["profile"] == initial["profile"]
    with pytest.raises(JournalError) as exc:
        writing.get(initial["project_id"], revision=99)
    assert exc.value.code == "REVISION_NOT_FOUND"


def test_active_selection_automatically_protects_first_new_reading(writing, tmp_path):
    selected = selected_project(writing, tmp_path)
    pid = selected["selected_paper_ids"][0]
    card = card_for(writing.literature.read(pid))
    card["summary"] = "First newly saved card after selection"
    first = writing.literature.save_reading(pid, card)["data"]
    generate_more_cards(writing, pid, 27)
    with writing.library.connection() as conn:
        assert conn.execute("SELECT 1 FROM writing_selected_papers WHERE project_id=? AND paper_id=?", (selected["project_id"], pid)).fetchone()
        assert conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (first["reading_id"],)).fetchone()
        assert conn.execute("SELECT 1 FROM writing_reading_pins WHERE project_id=? AND reading_id=?", (selected["project_id"], first["reading_id"])).fetchone()
    rows = writing.get(selected["project_id"], evidence_limit=100)["data"]["evidence_matrix"]
    assert any(r["reading_id"] == first["reading_id"] for r in rows)
    assert reading_count(writing, pid) == 29


def test_deselection_stops_auto_pinning_but_does_not_erase_history(writing, tmp_path):
    selected = selected_project(writing, tmp_path)
    pid = selected["selected_paper_ids"][0]
    card = card_for(writing.literature.read(pid))
    card["summary"] = "Pinned while active"
    pinned = writing.literature.save_reading(pid, card)["data"]
    writing.assess(selected["project_id"], [], 2, selected_paper_ids=[])
    card["summary"] = "Not selected anymore"
    unused = writing.literature.save_reading(pid, card)["data"]
    generate_more_cards(writing, pid, 27)
    with writing.library.connection() as conn:
        assert not conn.execute("SELECT 1 FROM writing_selected_papers WHERE project_id=?", (selected["project_id"],)).fetchone()
        assert conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (pinned["reading_id"],)).fetchone()
        assert not conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (unused["reading_id"],)).fetchone()


def test_selected_index_migration_is_read_inert_then_backfills_on_write(writing, tmp_path):
    selected = selected_project(writing, tmp_path)
    with writing.library.connection(True) as conn:
        conn.execute("DROP TABLE writing_selected_papers")
    assert writing.get(selected["project_id"])["data"]["revision"] == 2
    with writing.library.connection() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='writing_selected_papers'").fetchone()
    writing.create(profile(title="Explicit write triggers index backfill"))
    with writing.library.connection() as conn:
        assert conn.execute("SELECT 1 FROM writing_selected_papers WHERE project_id=? AND paper_id=?", (selected["project_id"], selected["selected_paper_ids"][0])).fetchone()


def test_physical_pdf_race_is_rechecked_inside_store_commit(writing, tmp_path, monkeypatch):
    selected = selected_project(writing, tmp_path)
    original_save = writing.store.save
    sha = selected["evidence_matrix"][0]["file_sha256"]
    def racing_save(*args, **kwargs):
        writing.library.pdf_path(sha).write_bytes(b"corrupt between validation and save")
        return original_save(*args, **kwargs)
    monkeypatch.setattr(writing.store, "save", racing_save)
    with pytest.raises(JournalError) as exc:
        writing.save_draft(selected["project_id"], draft_for(selected), 2)
    assert exc.value.code == "CACHE_CORRUPTED"
    assert writing.store.get(selected["project_id"])["revision"] == 2
