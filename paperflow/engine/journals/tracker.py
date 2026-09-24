"""Submission tracking parser and snapshot comparator for academic journals.

Pure standard-library offline parsing module.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_EVENTS = 10000
MAX_OUTPUT_WARNINGS = 50
MAX_OUTPUT_EVENTS = 200

# Event name normalizations and classifications
INVITED_EVENT_KEYWORDS = ("invite", "invitation", "invited")
ACCEPTED_EVENT_KEYWORDS = ("accept", "agree", "accepted", "agreed")
COMPLETED_EVENT_KEYWORDS = ("complete", "completed", "finish", "submitted", "received")

# URL pattern to sanitize strings
_URL_PATTERN = re.compile(r"https?://[^\s]+|ftp://[^\s]+", re.IGNORECASE)
# UUID pattern
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)


def _sanitize_string(val: Any, max_len: int = 300) -> str:
    """Validate and sanitize a string field, removing URLs and raw UUIDs, truncating length."""
    if val is None:
        return ""
    if not isinstance(val, (str, int, float)):
        # If dict, list, or other object passed where a string was expected, do not leak it
        return ""
    s = str(val).strip()
    # Mask URLs
    s = _URL_PATTERN.sub("[URL_REDACTED]", s)
    # Mask UUIDs
    s = _UUID_PATTERN.sub("[ID_REDACTED]", s)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _parse_timestamp(val: Any) -> Optional[datetime.datetime]:
    """Parse ISO date/time or integer/float timestamp (seconds or milliseconds).
    
    Only supports ISO 8601 (or strict ISO with Z/space) and numeric timestamps in UTC.
    Ambiguous date formats like MM/DD/YYYY vs DD/MM/YYYY are intentionally NOT supported.
    Returns datetime in UTC timezone-aware or None if invalid.
    """
    if val is None or val == "" or isinstance(val, bool):
        return None

    if isinstance(val, (int, float)):
        try:
            ts_float = float(val)
            if ts_float > 1e11:  # Milliseconds
                ts_sec = ts_float / 1000.0
            elif 1e8 <= ts_float <= 1e11:  # Seconds (1973 to 5138)
                ts_sec = ts_float
            else:
                return None
            dt = datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc)
            return dt
        except (ValueError, OverflowError, OSError):
            return None

    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # Try numeric string
        if re.match(r"^\d+(\.\d+)?$", val):
            try:
                return _parse_timestamp(float(val))
            except ValueError:
                pass

        # Try ISO format
        cleaned_iso = val.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(cleaned_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            else:
                dt = dt.astimezone(datetime.timezone.utc)
            return dt
        except ValueError:
            pass

    return None


def _parse_revision_number(raw_rev: Any) -> Tuple[Optional[int], bool]:
    """Parse revision number strictly.
    
    Returns (rev_number, is_valid).
    Bools, negative numbers, floats (e.g. 1.5), and non-digits are strictly rejected.
    Revision 0 is valid.
    """
    if raw_rev is None:
        return None, True
    if isinstance(raw_rev, bool):
        return None, False
    if isinstance(raw_rev, int):
        if raw_rev >= 0:
            return raw_rev, True
        return None, False
    if isinstance(raw_rev, float):
        # Floats like 1.5 or even 1.0 are rejected to prevent truncation
        return None, False
    if isinstance(raw_rev, str):
        s = raw_rev.strip()
        if not s:
            return None, True
        if re.match(r"^\d+$", s):
            val = int(s)
            return val, True
        return None, False
    return None, False


def _classify_event(event_name: str) -> str:
    """Classify review event to 'invited', 'accepted', 'completed', or 'other'."""
    lower = event_name.lower()
    if any(k in lower for k in COMPLETED_EVENT_KEYWORDS):
        return "completed"
    if any(k in lower for k in ACCEPTED_EVENT_KEYWORDS):
        return "accepted"
    if any(k in lower for k in INVITED_EVENT_KEYWORDS):
        return "invited"
    return "other"


def _anonymize_id(raw_id: str, id_map: Dict[str, str]) -> str:
    """Map raw participant/slot id to safe opaque anonymized key (slot_1, slot_2) without leaking raw id."""
    if raw_id not in id_map:
        id_map[raw_id] = f"slot_{len(id_map) + 1}"
    return id_map[raw_id]


def _extract_id_str(val: Any) -> Optional[str]:
    """Extract ID representation without dropping 0, strictly rejecting bool."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, str)):
        s = str(val).strip()
        return s if s != "" else None
    return None


def analyze_tracking(
    data: dict,
    previous: Optional[dict] = None,
    include_title: bool = False,
) -> dict:
    """Analyze submission tracking data offline without external network or Word dependencies.

    Args:
        data: Current submission tracking dictionary.
        previous: Optional previous tracking dictionary for diff computation.
        include_title: Whether to include ManuscriptTitle in output data (default False).

    Returns:
        Structured result dict matching NarraFork contract:
        {
            'status': 'success' | 'partial',
            'data': {
                'provider': str,
                'journal_name': str,
                'status': str,
                'latest_revision': int | None,
                'latest_revision_inferred': bool,
                'revisions': [...],
                'days_since_latest_event': float | None,
                'changes': {...},
                ...
            },
            'warnings': [str],
            'sources': [],
            'coverage': {...},
            'suggested_options': []
        }

    Raises:
        ValueError: If data is invalid or empty.
    """
    if not isinstance(data, dict):
        raise ValueError("Input data must be a dictionary.")

    if not data:
        raise ValueError("Input data cannot be empty.")

    warnings: List[str] = []
    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)

    # 1. Basic root fields with existence check (not 'or') and sanitization
    def get_field(d: dict, *keys: str) -> Any:
        for k in keys:
            if k in d:
                return d[k]
        return None

    raw_provider = get_field(data, "Provider", "provider")
    provider_clean = _sanitize_string(raw_provider, max_len=50).lower() or "elsevier"

    raw_journal = get_field(data, "JournalName", "journal_name")
    journal_clean = _sanitize_string(raw_journal, max_len=200)

    raw_status = get_field(data, "Status", "status")
    status_clean = _sanitize_string(raw_status, max_len=100)

    raw_title = get_field(data, "ManuscriptTitle", "manuscript_title")
    title_clean = _sanitize_string(raw_title, max_len=500) if isinstance(raw_title, str) else ""

    sub_date_raw = get_field(data, "SubmissionDate", "submission_date")
    parsed_sub_date: Optional[datetime.datetime] = None
    if sub_date_raw is not None:
        parsed_sub_date = _parse_timestamp(sub_date_raw)
        if parsed_sub_date is None:
            warnings.append("SubmissionDate could not be parsed as a valid ISO or epoch date.")
        elif parsed_sub_date > now_utc + datetime.timedelta(days=1):
            warnings.append("SubmissionDate appears to be in the future; ignored in duration calculation.")
            parsed_sub_date = None

    raw_latest_rev = get_field(data, "LatestRevisionNumber", "latest_revision_number")
    latest_rev_num: Optional[int] = None
    latest_rev_inferred = False
    if raw_latest_rev is not None:
        parsed_l_rev, valid_l_rev = _parse_revision_number(raw_latest_rev)
        if valid_l_rev and parsed_l_rev is not None:
            latest_rev_num = parsed_l_rev
        else:
            warnings.append("LatestRevisionNumber is invalid; must be a non-negative integer.")

    # 2. Extract and validate ReviewEvents
    raw_events = get_field(data, "ReviewEvents", "review_events")
    if raw_events is None:
        raw_events = []
    elif not isinstance(raw_events, list):
        warnings.append("ReviewEvents must be a list. Treated as empty.")
        raw_events = []

    total_events_in_input = len(raw_events)
    if total_events_in_input > MAX_EVENTS:
        warnings.append(
            f"ReviewEvents count {total_events_in_input} exceeds maximum limit {MAX_EVENTS}. Truncated."
        )
        raw_events = raw_events[:MAX_EVENTS]

    # Deduplicate exact events: (Id, Event, Date, Revision)
    seen_events = set()
    cleaned_events = []
    has_future_event_detected = False

    for ev_idx, ev in enumerate(raw_events):
        if not isinstance(ev, dict):
            warnings.append(f"Malformed event at index {ev_idx} is not an object and was skipped.")
            continue

        raw_id_val = get_field(ev, "Id", "id")
        raw_id = _extract_id_str(raw_id_val)

        raw_event_name_val = get_field(ev, "Event", "event")
        raw_event_name = _sanitize_string(raw_event_name_val, max_len=100)
        if not raw_event_name:
            raw_event_name = "Unknown Event"

        raw_date = get_field(ev, "Date", "date")
        raw_rev = get_field(ev, "Revision", "revision")

        parsed_rev, valid_rev = _parse_revision_number(raw_rev)
        if not valid_rev:
            warnings.append(f"Event at index {ev_idx} has invalid revision (must be non-negative integer).")

        parsed_date: Optional[datetime.datetime] = None
        has_future_date = False
        if raw_date is not None and str(raw_date).strip() != "":
            parsed_date = _parse_timestamp(raw_date)
            if parsed_date is None:
                warnings.append(f"Event at index {ev_idx} date could not be parsed as valid ISO/epoch.")
            elif parsed_date > now_utc + datetime.timedelta(days=1):
                warnings.append(f"Event at index {ev_idx} date appears to be in the future; date nulled.")
                has_future_date = True
                has_future_event_detected = True
                parsed_date = None

        # Exact dedup key: treat Id existence cleanly (0 is '0', None is '')
        dedup_key = (
            raw_id or "",
            raw_event_name,
            parsed_date.isoformat() if parsed_date else ("<future>" if has_future_date else ""),
            parsed_rev,
        )
        if dedup_key in seen_events:
            continue
        seen_events.add(dedup_key)

        cleaned_events.append({
            "raw_id": raw_id,
            "event_name": raw_event_name,
            "date": parsed_date,
            "has_future_date": has_future_date,
            "revision": parsed_rev,
        })

    # Group by Revision
    rev_groups: Dict[Any, List[dict]] = {}
    for ev in cleaned_events:
        r = ev["revision"]
        rev_groups.setdefault(r, []).append(ev)

    int_revs = sorted([r for r in rev_groups if isinstance(r, int)])
    has_none = None in rev_groups
    ordered_rev_keys = int_revs + ([None] if has_none else [])

    revisions_output = []
    latest_event_dt: Optional[datetime.datetime] = None

    for rev_key in ordered_rev_keys:
        ev_list = rev_groups[rev_key]
        ev_list.sort(
            key=lambda x: (
                x["date"] is None,
                x["date"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            )
        )

        invited_events = 0
        accepted_events = 0
        completed_events = 0
        other_events = 0

        # Mapping for anonymized slot ids within this revision
        id_map: Dict[str, str] = {}
        # Track slot timestamps with list of events: slot_name -> {cat: [dates]}
        slot_events_log: Dict[str, Dict[str, List[datetime.datetime]]] = {}

        rev_events_output = []
        first_event_dt: Optional[datetime.datetime] = None
        last_rev_event_dt: Optional[datetime.datetime] = None

        for item in ev_list:
            dt = item["date"]
            if dt is not None:
                if first_event_dt is None or dt < first_event_dt:
                    first_event_dt = dt
                if last_rev_event_dt is None or dt > last_rev_event_dt:
                    last_rev_event_dt = dt
                if latest_event_dt is None or dt > latest_event_dt:
                    latest_event_dt = dt

            cat = _classify_event(item["event_name"])
            if cat == "invited":
                invited_events += 1
            elif cat == "accepted":
                accepted_events += 1
            elif cat == "completed":
                completed_events += 1
            else:
                other_events += 1

            slot_anon: Optional[str] = None
            if item["raw_id"] is not None:
                slot_anon = _anonymize_id(item["raw_id"], id_map)
                if dt is not None and cat in ("invited", "accepted", "completed"):
                    slot_events_log.setdefault(slot_anon, {}).setdefault(cat, []).append(dt)

            rev_events_output.append({
                "event": item["event_name"],
                "category": cat,
                "date": dt.isoformat() if dt else None,
                "slot_ref": slot_anon,
            })

        # Calculate durations for slots within this revision
        slot_durations: List[dict] = []
        for slot_anon, cats in slot_events_log.items():
            inv_dates = cats.get("invited", [])
            acc_dates = cats.get("accepted", [])
            comp_dates = cats.get("completed", [])

            is_ambiguous = (
                len(inv_dates) > 1 or len(acc_dates) > 1 or len(comp_dates) > 1
            )

            inv_to_acc_days = None
            acc_to_comp_days = None
            inv_to_comp_days = None

            if is_ambiguous:
                warnings.append(
                    f"Slot {slot_anon} has multiple repeated invitation/status events; duration marked ambiguous and not computed."
                )
            else:
                # Exactly one timestamp per stage, compute safely
                inv = inv_dates[0] if inv_dates else None
                acc = acc_dates[0] if acc_dates else None
                comp = comp_dates[0] if comp_dates else None

                if inv and acc:
                    diff = (acc - inv).total_seconds() / 86400.0
                    if diff < 0:
                        warnings.append(
                            f"Negative duration detected for {slot_anon} between invited and accepted; reported as null."
                        )
                    else:
                        inv_to_acc_days = round(diff, 2)

                if acc and comp:
                    diff = (comp - acc).total_seconds() / 86400.0
                    if diff < 0:
                        warnings.append(
                            f"Negative duration detected for {slot_anon} between accepted and completed; reported as null."
                        )
                    else:
                        acc_to_comp_days = round(diff, 2)

                if inv and comp:
                    diff = (comp - inv).total_seconds() / 86400.0
                    if diff < 0:
                        warnings.append(
                            f"Negative duration detected for {slot_anon} between invited and completed; reported as null."
                        )
                    else:
                        inv_to_comp_days = round(diff, 2)

            has_pair = (
                bool(inv_dates and acc_dates)
                or bool(acc_dates and comp_dates)
                or bool(inv_dates and comp_dates)
            )
            if is_ambiguous or has_pair:
                slot_durations.append({
                    "slot_ref": slot_anon,
                    "is_ambiguous": is_ambiguous,
                    "invited_to_accepted_days": inv_to_acc_days,
                    "accepted_to_completed_days": acc_to_comp_days,
                    "invited_to_completed_days": inv_to_comp_days,
                    "semantic_verified": False,
                    "note": (
                        "Ambiguous event repetition"
                        if is_ambiguous
                        else "Associated duration based on same Id within revision only; semantics unverified, does not claim true identity."
                    ),
                })

        # Revision duration
        rev_duration_days: Optional[float] = None
        if first_event_dt and last_rev_event_dt:
            diff = (last_rev_event_dt - first_event_dt).total_seconds() / 86400.0
            if diff < 0:
                warnings.append(f"Negative revision span detected in revision {rev_key}; reported as null.")
            else:
                rev_duration_days = round(diff, 2)

        # Cap displayed events per revision to MAX_OUTPUT_EVENTS
        displayed_events = rev_events_output[:MAX_OUTPUT_EVENTS]

        revisions_output.append({
            "revision": rev_key,
            "event_count": len(ev_list),
            "displayed_event_count": len(displayed_events),
            "events_truncated": len(ev_list) > len(displayed_events),
            "invited_events": invited_events,
            "accepted_events": accepted_events,
            "completed_events": completed_events,
            "other_events": other_events,
            "count_is_exact": False,
            "count_note": "Event counts only. Id cannot determine true reviewer identity or exact headcount.",
            "duration_days": rev_duration_days,
            "first_event_date": first_event_dt.isoformat() if first_event_dt else None,
            "latest_event_date": last_rev_event_dt.isoformat() if last_rev_event_dt else None,
            "slot_durations": slot_durations,
            "events": displayed_events,
        })

    # If latest_rev_num was not explicitly provided, infer from revisions_output
    if latest_rev_num is None:
        if int_revs:
            latest_rev_num = int_revs[-1]
            latest_rev_inferred = True

    # Days since latest event (must never show negative numbers)
    days_since_latest: Optional[float] = None
    if has_future_event_detected:
        days_since_latest = None
    elif latest_event_dt is not None:
        diff_now = (now_utc - latest_event_dt).total_seconds() / 86400.0
        if diff_now < -0.01:
            warnings.append("Latest event date appears to be in the future; days_since_latest reported as null.")
            days_since_latest = None
        else:
            days_since_latest = max(0.0, round(diff_now, 2))

    # 3. Previous comparison (snapshot diff)
    changes: Dict[str, Any] = {
        "status_changed": False,
        "previous_status": None,
        "new_status": status_clean,
        "new_events_count": 0,
        "displayed_new_events_count": 0,
        "new_events_truncated": False,
        "new_events": [],
    }

    if previous is not None and isinstance(previous, dict):
        raw_prev_status = get_field(previous, "Status", "status")
        prev_status = _sanitize_string(raw_prev_status, max_len=100)
        changes["previous_status"] = prev_status
        changes["status_changed"] = (prev_status != status_clean and bool(prev_status or status_clean))

        prev_raw_events = get_field(previous, "ReviewEvents", "review_events")
        if prev_raw_events is not None and not isinstance(prev_raw_events, list):
            warnings.append("Previous ReviewEvents is not a list. Diff treated previous as empty.")
            prev_raw_events = []
        elif prev_raw_events is None:
            prev_raw_events = []

        if len(prev_raw_events) > MAX_EVENTS:
            warnings.append(
                f"Previous ReviewEvents count {len(prev_raw_events)} exceeds limit {MAX_EVENTS}. Truncated for diff."
            )
            prev_raw_events = prev_raw_events[:MAX_EVENTS]

        prev_seen = set()
        for p_idx, pev in enumerate(prev_raw_events):
            if not isinstance(pev, dict):
                warnings.append(f"Previous malformed event at index {p_idx} was skipped.")
                continue
            pid_val = get_field(pev, "Id", "id")
            pid = _extract_id_str(pid_val) or ""
            pname_val = get_field(pev, "Event", "event")
            pname = _sanitize_string(pname_val, max_len=100)
            pdate = get_field(pev, "Date", "date")
            prev_rev_val = get_field(pev, "Revision", "revision")
            parsed_prev_rev, _ = _parse_revision_number(prev_rev_val)
            parsed_pdate = _parse_timestamp(pdate) if pdate is not None else None
            key = (
                pid,
                pname,
                parsed_pdate.isoformat() if parsed_pdate else "",
                parsed_prev_rev,
            )
            prev_seen.add(key)

        new_events_list = []
        for item in cleaned_events:
            dt = item["date"]
            key = (
                item["raw_id"] or "",
                item["event_name"],
                dt.isoformat() if dt else "",
                item["revision"],
            )
            if key not in prev_seen:
                new_events_list.append({
                    "event": item["event_name"],
                    "category": _classify_event(item["event_name"]),
                    "date": dt.isoformat() if dt else None,
                    "revision": item["revision"],
                })

        total_new_events = len(new_events_list)
        displayed_new = new_events_list[:MAX_OUTPUT_EVENTS]
        changes["new_events_count"] = total_new_events
        changes["displayed_new_events_count"] = len(displayed_new)
        changes["new_events_truncated"] = total_new_events > len(displayed_new)
        changes["new_events"] = displayed_new

    # Output status: success or partial
    status_str = "partial" if warnings else "success"

    # Assemble data payload
    data_payload: Dict[str, Any] = {
        "provider": provider_clean,
        "journal_name": journal_clean,
        "status": status_clean,
        "submission_date": parsed_sub_date.isoformat() if parsed_sub_date else None,
        "latest_revision": latest_rev_num,
        "latest_revision_inferred": latest_rev_inferred,
        "revisions": revisions_output,
        "days_since_latest_event": days_since_latest,
        "changes": changes,
        "total_unique_events": len(cleaned_events),
    }

    if include_title:
        data_payload["manuscript_title"] = title_clean

    total_warnings_count = len(warnings)
    capped_warnings = warnings[:MAX_OUTPUT_WARNINGS]
    if total_warnings_count > MAX_OUTPUT_WARNINGS:
        capped_warnings.append(
            f"Warnings truncated: {total_warnings_count} total warnings, showing first {MAX_OUTPUT_WARNINGS}."
        )

    # Construct standard NarraFork response
    result = {
        "status": status_str,
        "data": data_payload,
        "warnings": capped_warnings,
        "sources": [],
        "coverage": {
            "has_revisions": len(revisions_output) > 0,
            "has_submission_date": parsed_sub_date is not None,
            "has_latest_event": latest_event_dt is not None,
            "events_analyzed": len(cleaned_events),
            "warnings_count": total_warnings_count,
            "warnings_truncated": total_warnings_count > MAX_OUTPUT_WARNINGS,
        },
        "suggested_options": [],
    }

    return result
