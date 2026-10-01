"""Tests for reading_loop_store: schema isolation, CAS, leases, range unions, and tickets."""
import time
import pytest

from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.reading_loop_models import (
    BUDGET_DEFAULTS, LoopBudget, LoopUsage, LoopReserved, InheritedBaseline, ReadingLoopState
)
from paperflow.engine.literature.reading_loop_store import ReadingLoopStore
from paperflow.engine.literature.store import PaperStore
from paperflow.engine.literature.writing_models import project_id
from paperflow.engine.literature.writing_store import WritingStore, _CURRENT_LOOP_OWNER
from paperflow.engine.literature.service import LiteratureService
from paperflow.engine.literature.writing_service import PaperWritingService
from test_literature_service import FakeProviders, card_for, imported, pdf_bytes, record


@pytest.fixture
def store(tmp_path):
    paper_store = PaperStore(str(tmp_path / "library"))
    return ReadingLoopStore(paper_store=paper_store)


@pytest.fixture
def writing_store(tmp_path):
    paper_store = PaperStore(str(tmp_path / "library"))
    return WritingStore(paper_store=paper_store)


def make_initial_state(pid="writing-0123456789abcdef01234567"):
    return {
        "status": "awaiting_review",
        "stop_reason": None,
        "stop_detail": "",
        "budget": LoopBudget().model_dump(mode="json"),
        "usage": LoopUsage().model_dump(mode="json"),
        "reserved": LoopReserved().model_dump(mode="json"),
        "inherited_baseline": InheritedBaseline().model_dump(mode="json"),
        "round_index": 1,
        "no_progress_rounds": 0,
        "next_action": None,
        "rounds": [],
        "reviews": [],
        "actions": [],
        "request": "",
    }


def test_readonly_connection_no_tables_no_side_effects(store):
    with store.connection(write=False) as conn:
        if conn is not None:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "reading_loops" not in names
    res = store.get("writing-0123456789abcdef01234567")
    assert res is None


def test_create_and_cas_save(store):
    pid = "writing-0123456789abcdef01234567"
    state = make_initial_state(pid)
    created = store.create(pid, state, 1)
    assert created["loop_revision"] == 1
    assert created["status"] == "awaiting_review"

    # Cannot create twice
    with pytest.raises(JournalError) as exc:
        store.create(pid, state, 1)
    assert exc.value.code == "LOOP_ALREADY_EXISTS"

    # CAS mismatch
    state["status"] = "ready"
    with pytest.raises(JournalError) as exc:
        store.save(pid, state, expected_loop_revision=99, change_note="Wrong rev")
    assert exc.value.code == "REVISION_CONFLICT"

    # Successful CAS save
    saved = store.save(pid, state, expected_loop_revision=1, change_note="Advance to ready")
    assert saved["loop_revision"] == 2
    assert saved["status"] == "ready"


def test_interval_union_merging_no_double_counting(store):
    pid = "writing-0123456789abcdef01234567"
    store.create(pid, make_initial_state(pid), 1)

    paper_id = "paper-0123456789abcdef01234567"
    sha = "a" * 64
    page_no = 1

    # First read: offset 0 to 500 -> 1 page, 500 chars
    p1, c1 = store.record_read_range(pid, paper_id, sha, page_no, 0, 500)
    assert p1 == 1
    assert c1 == 500

    # Overlapping read: offset 300 to 800 -> 0 new pages, only 300 new chars!
    p2, c2 = store.record_read_range(pid, paper_id, sha, page_no, 300, 800)
    assert p2 == 0
    assert c2 == 300  # 800 - 500 = 300

    # Fully contained read: offset 100 to 400 -> 0 new pages, 0 new chars!
    p3, c3 = store.record_read_range(pid, paper_id, sha, page_no, 100, 400)
    assert p3 == 0
    assert c3 == 0

    # Non-overlapping read on same page: offset 1000 to 1200 -> 0 new pages, 200 new chars
    p4, c4 = store.record_read_range(pid, paper_id, sha, page_no, 1000, 1200)
    assert p4 == 0
    assert c4 == 200

    # Read on page 2: offset 0 to 400 -> 1 new page, 400 new chars
    p5, c5 = store.record_read_range(pid, paper_id, sha, 2, 0, 400)
    assert p5 == 1
    assert c5 == 400


def test_lease_acquire_and_release(store):
    pid = "writing-0123456789abcdef01234567"
    store.create(pid, make_initial_state(pid), 1)
    act_id = "act-0123456789abcdef01234567"
    store.save_action(pid, act_id, 1, "read", "pending", {})

    token, expires_at = store.acquire_lease(pid, act_id, expected_loop_revision=1, duration_seconds=10.0)
    assert token
    assert expires_at > time.time()

    # Active lease detected
    active = store.get_lease(pid)
    assert active is not None
    assert active["token"] == token

    # Concurrent acquire rejected
    with pytest.raises(JournalError) as exc:
        store.acquire_lease(pid, "act-0123456789abcdef01234568", expected_loop_revision=1, duration_seconds=10.0)
    assert exc.value.code == "CONCURRENT_OPERATION"

    # Release lease
    store.release_lease(pid, act_id, token)
    assert store.get_lease(pid) is None

    # Can acquire again after release
    token2, _ = store.acquire_lease(pid, act_id, expected_loop_revision=1, duration_seconds=10.0)
    assert token2 != token


def test_writing_store_short_guard_rejects_concurrent_mutation(store, writing_store, tmp_path):
    lit_svc = LiteratureService(str(tmp_path / "library"), providers=FakeProviders())
    writing_svc = PaperWritingService(literature=lit_svc)
    created = writing_svc.create({"title": "Test Title", "research_question": "Test RQ"})
    pid = created["data"]["project_id"]

    # Start loop and acquire lease
    store.create(pid, make_initial_state(pid), 1)
    act_id = "act-0123456789abcdef01234567"
    store.save_action(pid, act_id, 1, "read", "pending", {})
    token, _ = store.acquire_lease(pid, act_id, expected_loop_revision=1, duration_seconds=20.0)

    # Calling writing_store.save without owner context should be rejected
    snapshot = writing_store.get(pid)
    with pytest.raises(JournalError) as exc:
        writing_store.save(pid, snapshot, expected_revision=1, change_note="Manual save")
    assert exc.value.code == "CONCURRENT_MUTATION"

    # With owner context set, mutation passes
    tok = _CURRENT_LOOP_OWNER.set(pid)
    try:
        updated = writing_store.save(pid, snapshot, expected_revision=1, change_note="Owner save")
        assert updated["revision"] == 2
    finally:
        _CURRENT_LOOP_OWNER.reset(tok)


def test_model_ticket_lifecycle(store):
    pid = "writing-0123456789abcdef01234567"
    store.create(pid, make_initial_state(pid), 1)
    act_id = "act-0123456789abcdef01234567"

    tid = store.record_model_ticket(pid, act_id, loop_revision=1)
    assert tid.startswith("ticket-")

    # Settle ticket success
    store.settle_model_ticket(pid, act_id, tid, success=True)

    # Cannot settle again
    with pytest.raises(JournalError) as exc:
        store.settle_model_ticket(pid, act_id, tid, success=False)
    assert exc.value.code == "TICKET_ALREADY_SETTLED"
