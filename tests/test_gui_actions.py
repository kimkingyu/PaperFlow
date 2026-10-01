"""GUI action surface and the Agent-to-GUI inbox."""
import json

import pytest

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError
from paperflow.gui import inbox
from paperflow.gui.actions import dispatch

IDEA = "We plan underwater robotics and embedded computing experiments."


def test_unknown_action_and_file_paths_are_rejected(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    with pytest.raises(JournalError):
        dispatch(finder, "delete_everything", {})
    for key in ("file_path", "input_paths", "html_path"):
        with pytest.raises(JournalError) as caught:
            dispatch(finder, "recommend", {"text": IDEA, key: "C:/Windows/win.ini"})
        assert caught.value.code == "INVALID_INPUT"


def test_overview_does_not_create_database(tmp_path):
    home = tmp_path / "home"
    data = dispatch(JournalFinder(str(home)), "overview")["data"]
    assert data["database_initialized"] is False
    assert len(data["sources"]) == 14
    assert not home.exists()


def test_overview_reports_content_statistics_after_build(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    dispatch(finder, "build", {"dry_run": False})
    cov = dispatch(finder, "overview")["data"]["coverage"]
    assert cov["total_journals"] >= 1200
    assert cov["scie_count"] >= 1 and cov["ei_count"] >= 1
    assert 0 < cov["price_coverage"] <= 1


def test_text_limits(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    with pytest.raises(JournalError):
        dispatch(finder, "prepare", {"text": "   "})
    with pytest.raises(JournalError) as caught:
        dispatch(finder, "prepare", {"text": "x" * 60001})
    assert caught.value.code == "INPUT_TOO_LARGE"


def test_recommend_uses_builtin_candidates_and_renders_report(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    result = dispatch(finder, "recommend", {"text": IDEA})
    assert result["data"]["candidate_count"] >= 30
    html = dispatch(finder, "render_report", {"result": result})["data"]["html"]
    assert "<!DOCTYPE html>" in html


def test_publish_to_gui_writes_bounded_atomic_inbox(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    for _ in range(inbox.MAX_ITEMS + 3):
        result = finder.recommend(text=IDEA, publish_to_gui=True)
    item_id = result["data"]["gui_inbox_id"]
    items = inbox.list_items(finder.store.directory)
    assert len(items) == inbox.MAX_ITEMS
    assert items[0]["id"] == item_id
    stored = inbox.get_item(finder.store.directory, item_id)
    assert stored["result"]["data"]["context_id"] == result["data"]["context_id"]
    folder = inbox.inbox_dir(finder.store.directory)
    assert not list(folder.glob(".tmp-*"))
    assert not (finder.store.path).exists()  # inbox never initializes the journal DB


def test_inbox_rejects_path_like_ids(tmp_path):
    with pytest.raises(JournalError):
        inbox.get_item(tmp_path, "../../secrets")
    assert inbox.get_item(tmp_path, "20260925T010203000000001Z-deadbeef") is None
    with pytest.raises(JournalError):
        inbox.get_item(tmp_path, "20260925T010203Z-deadbeef")  # legacy/short form is not accepted


def test_search_reviews_action_enriches_journal_and_updates_easiness(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    dispatch(finder, "build", {"dry_run": False})

    # Pick a journal from the built DB
    res = dispatch(finder, "search_reviews", {"query": "Sensors"})
    data = res["data"]
    assert data["status"] == "success"
    assert data["new_reviews_count"] >= 1
    assert len(data["experiences"]) >= 1
    summary = data["decision_summary"]
    if summary["tier"]["cas_quartile"] is None:
        assert summary["easiness"]["score"] is None
        assert summary["missing"]
    else:
        assert summary["easiness"]["score"] is not None
    assert "网络评论" in data["message"] or "最新" in data["message"]
