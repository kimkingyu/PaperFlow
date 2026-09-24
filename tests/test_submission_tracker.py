"""Unit tests for submission tracker offline parser and snapshot comparator."""

import datetime
import pytest

from paperflow.engine.journals.tracker import analyze_tracking


def test_invalid_and_empty_inputs():
    """Verify ValueError is raised for invalid or empty inputs."""
    with pytest.raises(ValueError):
        analyze_tracking(None)  # type: ignore

    with pytest.raises(ValueError):
        analyze_tracking([])  # type: ignore

    with pytest.raises(ValueError):
        analyze_tracking({})


def test_standard_elsevier_payload():
    """Verify standard happy-path parsing with ISO dates, revisions, and status."""
    data = {
        "ManuscriptTitle": "Deep Learning for Vibration Analysis",
        "JournalName": "Mechanical Systems and Signal Processing",
        "Status": "Under Review",
        "SubmissionDate": "2024-01-10T08:00:00Z",
        "LatestRevisionNumber": 1,
        "ReviewEvents": [
            {
                "Id": "REV-A1",
                "Event": "Reviewer Invited",
                "Date": "2024-01-12T10:00:00Z",
                "Revision": 0,
            },
            {
                "Id": "REV-A1",
                "Event": "Reviewer Accepted",
                "Date": "2024-01-13T10:00:00Z",
                "Revision": 0,
            },
            {
                "Id": "REV-A1",
                "Event": "Review Completed",
                "Date": "2024-01-20T10:00:00Z",
                "Revision": 0,
            },
            {
                "Id": "REV-B2",
                "Event": "Reviewer Invited",
                "Date": "2024-01-15T10:00:00Z",
                "Revision": 0,
            },
            {
                "Id": "",
                "Event": "Revision Submitted",
                "Date": "2024-02-01T10:00:00Z",
                "Revision": 1,
            },
        ],
    }

    # 1. Default include_title=False: title must NOT be in data
    res = analyze_tracking(data, include_title=False)
    assert res["status"] == "success"
    assert "manuscript_title" not in res["data"]
    assert res["data"]["journal_name"] == "Mechanical Systems and Signal Processing"
    assert res["data"]["status"] == "Under Review"
    assert res["data"]["provider"] == "elsevier"
    assert res["data"]["latest_revision"] == 1
    assert res["data"]["latest_revision_inferred"] is False
    assert len(res["data"]["revisions"]) == 2

    # Check revision 0 details
    rev0 = res["data"]["revisions"][0]
    assert rev0["revision"] == 0
    assert rev0["invited_events"] == 2
    assert rev0["accepted_events"] == 1
    assert rev0["completed_events"] == 1
    assert rev0["count_is_exact"] is False

    # Check slot duration does not leak raw ID
    assert len(rev0["slot_durations"]) == 1
    sd = rev0["slot_durations"][0]
    assert "REV-A1" not in str(sd)
    assert sd["slot_ref"] == "slot_1"
    assert sd["semantic_verified"] is False
    assert sd["invited_to_accepted_days"] == 1.0
    assert sd["accepted_to_completed_days"] == 7.0
    assert sd["invited_to_completed_days"] == 8.0

    # 2. include_title=True: title should be present
    res_with_title = analyze_tracking(data, include_title=True)
    assert res_with_title["data"]["manuscript_title"] == "Deep Learning for Vibration Analysis"


def test_revision_zero_and_id_zero_existence():
    """Verify LatestRevisionNumber=0 and Id=0 are preserved and not dropped."""
    data = {
        "LatestRevisionNumber": 0,
        "ReviewEvents": [
            {"Id": 0, "Event": "Reviewer Invited", "Date": "2024-01-01T00:00:00Z", "Revision": 0},
            {"Id": 0, "Event": "Reviewer Accepted", "Date": "2024-01-02T00:00:00Z", "Revision": 0},
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "success"
    assert res["data"]["latest_revision"] == 0
    assert res["data"]["latest_revision_inferred"] is False
    rev0 = res["data"]["revisions"][0]
    assert rev0["revision"] == 0
    assert len(rev0["slot_durations"]) == 1
    assert rev0["slot_durations"][0]["slot_ref"] == "slot_1"
    assert rev0["slot_durations"][0]["invited_to_accepted_days"] == 1.0


def test_invalid_revision_types_and_inferred_flag():
    """Verify bool, negative numbers, and float revisions are rejected and latest_revision is inferred."""
    data = {
        "LatestRevisionNumber": "invalid",
        "ReviewEvents": [
            {"Id": "1", "Event": "Reviewer Invited", "Date": "2024-01-01T00:00:00Z", "Revision": True},   # Bool rejected
            {"Id": "1", "Event": "Reviewer Accepted", "Date": "2024-01-02T00:00:00Z", "Revision": -1},    # Negative rejected
            {"Id": "1", "Event": "Reviewer Completed", "Date": "2024-01-03T00:00:00Z", "Revision": 1.5},  # Float rejected
            {"Id": "2", "Event": "Reviewer Invited", "Date": "2024-01-04T00:00:00Z", "Revision": 2},     # Valid
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "partial"
    assert res["data"]["latest_revision"] == 2
    assert res["data"]["latest_revision_inferred"] is True


def test_timestamp_epoch_seconds_and_milliseconds():
    """Verify numeric seconds, milliseconds, and ISO in UTC."""
    # 1704067200 = 2024-01-01 00:00:00 UTC
    # 1704153600000 = 2024-01-02 00:00:00 UTC (ms)
    data = {
        "JournalName": "Test Journal",
        "SubmissionDate": 1704067200,
        "ReviewEvents": [
            {"Id": "slot-1", "Event": "Reviewer Invited", "Date": 1704153600000, "Revision": 0},
            {"Id": "slot-1", "Event": "Reviewer Accepted", "Date": "2024-01-03T12:00:00Z", "Revision": 0},
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "success"
    assert res["data"]["submission_date"].startswith("2024-01-01")
    rev0 = res["data"]["revisions"][0]
    assert len(rev0["events"]) == 2
    assert rev0["events"][0]["date"].startswith("2024-01-02")
    assert rev0["events"][1]["date"].startswith("2024-01-03T12:00:00")


def test_ambiguous_date_format_rejected():
    """Verify ambiguous slash/dash formats like 05/06/2026 are not guessed and trigger warnings."""
    data = {
        "JournalName": "Test Journal",
        "SubmissionDate": "05/06/2024",
        "ReviewEvents": [
            {"Id": "1", "Event": "Reviewer Invited", "Date": "05-06-2024", "Revision": 0},
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "partial"
    assert any("SubmissionDate could not be parsed" in w for w in res["warnings"])
    assert any("date could not be parsed" in w for w in res["warnings"])
    assert res["data"]["submission_date"] is None
    assert res["data"]["revisions"][0]["events"][0]["date"] is None


def test_redaction_of_urls_uuids_and_non_leakage_in_warnings():
    """Verify URLs, UUIDs, and complex objects are redacted in fields and never leak into warnings."""
    uuid_val = "12345678-1234-5678-1234-567812345678"
    secret_url = "https://sensitive.elsevier.com/track?token=secret123"

    data = {
        "JournalName": f"Journal with {secret_url}",
        "Status": {"malicious": "nested_dict"},  # Object where string expected
        "ManuscriptTitle": 12345,  # Non-string title when include_title=True
        "ReviewEvents": [
            {"Id": uuid_val, "Event": f"Event with {uuid_val}", "Date": secret_url, "Revision": 0},
            {"Id": "1", "Event": "Reviewer Invited", "Date": "invalid-garbage-date", "Revision": 0},
        ],
    }
    res = analyze_tracking(data, include_title=True)
    assert res["status"] == "partial"

    # Entire output as string
    output_str = str(res)
    assert "secret123" not in output_str
    assert uuid_val not in output_str
    assert "malicious" not in output_str
    # Title non-string handling
    assert res["data"]["manuscript_title"] == ""

    # Warnings must not leak input values
    for w in res["warnings"]:
        assert "secret123" not in w
        assert "invalid-garbage-date" not in w


def test_future_dates_and_negative_duration_handling():
    """Verify future dates and negative durations output null with warnings, never negative numbers."""
    future_date = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)).isoformat()
    data = {
        "JournalName": "Warning Journal",
        "SubmissionDate": future_date,
        "ReviewEvents": [
            {"Id": "slot-inv", "Event": "Reviewer Invited", "Date": "2024-01-10T00:00:00Z", "Revision": 0},
            {"Id": "slot-inv", "Event": "Reviewer Accepted", "Date": "2024-01-05T00:00:00Z", "Revision": 0},  # Negative
            {"Id": "slot-fut", "Event": "Reviewer Invited", "Date": future_date, "Revision": 0},
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "partial"
    assert res["data"]["submission_date"] is None
    assert res["data"]["days_since_latest_event"] is None

    rev0 = res["data"]["revisions"][0]
    sd = [s for s in rev0["slot_durations"] if s["slot_ref"] == "slot_1"][0]
    assert sd["invited_to_accepted_days"] is None  # Reported as null, not negative

    warn_text = " ".join(res["warnings"])
    assert "future" in warn_text
    assert "Negative duration" in warn_text


def test_repeated_invitations_marked_ambiguous():
    """Verify a single Id with multiple invitations is flagged as ambiguous and does not calculate duration."""
    data = {
        "ReviewEvents": [
            {"Id": "rev-slot", "Event": "Reviewer Invited", "Date": "2024-01-01T00:00:00Z", "Revision": 0},
            {"Id": "rev-slot", "Event": "Reviewer Invited", "Date": "2024-01-05T00:00:00Z", "Revision": 0},  # Re-invited
            {"Id": "rev-slot", "Event": "Reviewer Accepted", "Date": "2024-01-06T00:00:00Z", "Revision": 0},
        ],
    }
    res = analyze_tracking(data)
    assert res["status"] == "partial"
    rev0 = res["data"]["revisions"][0]
    sd = rev0["slot_durations"][0]
    assert sd["is_ambiguous"] is True
    assert sd["invited_to_accepted_days"] is None
    assert any("ambiguous" in w.lower() for w in res["warnings"])


def test_previous_snapshot_oversize_and_malformed_events():
    """Verify previous events list size limits, malformed elements handling, and clean diff without title."""
    prev_snapshot = {
        "Status": "Submitted",
        "ReviewEvents": ["not-a-dict", 123] + [
            {"Id": str(i), "Event": "Event", "Date": "2024-01-01T00:00:00Z", "Revision": 0}
            for i in range(10050)
        ],
    }
    curr_snapshot = {
        "Status": "Under Review",
        "ReviewEvents": [
            {"Id": "1000", "Event": "Event", "Date": "2024-01-01T00:00:00Z", "Revision": 0},
            {"Id": "new-event", "Event": "New Event", "Date": "2024-01-02T00:00:00Z", "Revision": 0},
        ],
    }
    res = analyze_tracking(curr_snapshot, previous=prev_snapshot)
    assert res["status"] == "partial"
    assert any("Previous ReviewEvents count 10052 exceeds limit" in w for w in res["warnings"])
    assert any("Previous malformed event" in w for w in res["warnings"])
    changes = res["data"]["changes"]
    assert changes["status_changed"] is True
    assert changes["new_status"] == "Under Review"
    assert changes["previous_status"] == "Submitted"
    assert changes["new_events_count"] == 1
    assert changes["new_events"][0]["event"] == "New Event"


def test_caps_on_warnings_and_displayed_events():
    """Verify MAX_OUTPUT_WARNINGS (50) and MAX_OUTPUT_EVENTS (200) caps prevent context blowout."""
    # Generate 300 events in current revision
    events = [
        {"Id": str(i), "Event": "Event", "Date": "invalid-date", "Revision": 0}
        for i in range(300)
    ]
    data = {
        "ReviewEvents": events,
    }
    res = analyze_tracking(data)
    assert res["status"] == "partial"
    # Coverage retains exact total
    assert res["coverage"]["warnings_count"] == 300
    assert res["coverage"]["warnings_truncated"] is True
    assert len(res["warnings"]) == 51  # 50 + 1 truncation summary message

    rev0 = res["data"]["revisions"][0]
    assert rev0["event_count"] == 300
    assert rev0["displayed_event_count"] == 200
    assert rev0["events_truncated"] is True
    assert len(rev0["events"]) == 200
