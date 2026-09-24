"""Risk assessment and compliance engine for academic journals.

Pure functions only: no network, no disk I/O, deterministic output.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from .models import JournalError, JournalRecord, RiskEvent, SchoolPolicy, SearchFilters, response


CLARIVATE_OFFICIAL_DOMAINS = (
    "clarivate.com",
    "mjl.clarivate.com",
    "webofscience.com",
)


def _parse_iso_date(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        clean = dt_str.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        if len(clean) == 10 and clean[4] == "-" and clean[7] == "-":
            clean += "T00:00:00+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_official_clarivate_host(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        for domain in CLARIVATE_OFFICIAL_DOMAINS:
            if hostname == domain or hostname.endswith("." + domain):
                return True
        return False
    except Exception:
        return False


def _extract_cas_complete_years(coverage: Optional[Dict[str, Any]]) -> Set[int]:
    """
    Extracts explicitly verified complete CAS warning years from coverage snapshots.
    Requirements:
    1. snapshot 'active' must be literal True or integer 1 (reject None/missing, strings like 'true'/'false').
    2. cov.complete must be literal True (reject strings).
    3. If cov.complete is literal False, it CANNOT be bypassed by child datasets.complete=True.
    """
    complete_years: Set[int] = set()
    if not coverage:
        return complete_years

    snapshots = coverage.get("snapshots") or []
    for s in snapshots:
        # Check active strictly: literal True or integer 1 (not isinstance(True, int), so True is ok)
        act = s.get("active")
        if act is not True and act != 1:
            continue
        
        cov = s.get("coverage")
        if not isinstance(cov, dict):
            continue

        # If parent cov explicitly marks complete as False (literal False), child datasets cannot bypass
        if cov.get("complete") is False:
            continue

        # Check single system coverage
        if cov.get("system") == "cas_warning":
            if cov.get("complete") is True:
                yr = cov.get("year") or s.get("data_year")
                if yr:
                    complete_years.add(int(yr))
        
        # Check datasets list inside snapshot coverage (only if parent complete is not False)
        datasets = cov.get("datasets") or []
        for d in datasets:
            if isinstance(d, dict) and d.get("system") == "cas_warning":
                if d.get("complete") is True:
                    yr = d.get("year") or cov.get("year") or s.get("data_year")
                    if yr:
                        complete_years.add(int(yr))

    return complete_years


def _check_cas_warning(
    record: JournalRecord,
    target_years: List[int],
    coverage: Optional[Dict[str, Any]],
    as_of: datetime,
) -> Dict[str, Any]:
    events = [r for r in record.risks if r.system == "cas_warning"]
    
    # Separate active (non-historical, non-superseded) flagged events vs historical flagged events
    active_flagged_in_target = []
    historical_flagged_in_target = []
    historical_or_past_flagged = []
    
    for ev in events:
        if ev.value == "flagged":
            is_hist = ev.historical or "superseded" in (ev.reason or "").lower()
            if target_years:
                if ev.year in target_years:
                    if is_hist:
                        historical_flagged_in_target.append(ev)
                    else:
                        active_flagged_in_target.append(ev)
                else:
                    historical_or_past_flagged.append(ev)
            else:
                if is_hist:
                    historical_or_past_flagged.append(ev)
                else:
                    active_flagged_in_target.append(ev)

    # 1. If active (non-historical) flagged in target/current year -> flagged
    if active_flagged_in_target:
        latest = active_flagged_in_target[0]
        return {
            "system": "cas_warning",
            "state": "flagged",
            "evidence": latest.reason or latest.level or "Listed in CAS warning list",
            "year": latest.year,
            "scope": f"CAS Warning List {latest.year}" if latest.year else "CAS Warning List",
            "historical": False,
        }

    # Verify coverage completeness
    complete_years = _extract_cas_complete_years(coverage)

    # If target_years specified, check if all target years are covered
    if target_years:
        missing_years = [y for y in target_years if y not in complete_years]
        if missing_years:
            return {
                "system": "cas_warning",
                "state": "unknown",
                "evidence": f"Incomplete CAS warning list coverage; missing verified complete snapshot for years: {missing_years}",
                "year": target_years[-1] if target_years else None,
                "scope": f"CAS Warning {target_years}",
                "historical": False,
            }
        
        # All target years are covered and no active flagged event in target years
        # Note: if historical_flagged_in_target exists (e.g. superseded event in that year),
        # return not_listed for current status while marking historical=True for caution
        has_superseded = bool(historical_flagged_in_target)
        return {
            "system": "cas_warning",
            "state": "not_listed",
            "evidence": "Not currently listed in verified complete CAS warning lists" + (
                " (superseded historical warning on file)" if has_superseded else ""
            ),
            "year": target_years[-1] if target_years else None,
            "scope": f"CAS Warning {target_years}",
            "historical": has_superseded,
        }

    # If no target_years specified:
    # Rule 3: Missing current year coverage defaults to unknown (reports existing covered years)
    # A complete snapshot from years ago cannot assert that current risk has been verified!
    current_year = as_of.year
    if current_year not in complete_years:
        cov_info = f"covered past years: {sorted(list(complete_years))}" if complete_years else "no verified complete years"
        return {
            "system": "cas_warning",
            "state": "unknown",
            "evidence": f"No verified complete CAS warning list snapshot for current year {current_year} ({cov_info})",
            "year": current_year,
            "scope": f"CAS Warning Current ({current_year})",
            "historical": False,
        }

    # Current year is covered and complete!
    return {
        "system": "cas_warning",
        "state": "not_listed",
        "evidence": f"Not listed in verified complete CAS warning lists ({sorted(list(complete_years))})",
        "year": current_year,
        "scope": f"CAS Warning {current_year}",
        "historical": False,
    }


def _check_xr_review(record: JournalRecord) -> Dict[str, Any]:
    events = [r for r in record.risks if r.system == "xr_review"]
    if not events:
        return {
            "system": "xr_review",
            "state": "not_applicable",
            "evidence": "No XR review event recorded",
            "year": None,
            "scope": "XR Review",
            "historical": False,
        }
    
    # Distinguish active vs historical events
    active_events = [e for e in events if not e.historical and "historical" not in (e.reason or "").lower()]
    historical_events = [e for e in events if e.historical or "historical" in (e.reason or "").lower()]

    if not active_events:
        # Only historical XR events exist: this is NOT current under_review!
        latest_hist = historical_events[0]
        return {
            "system": "xr_review",
            "state": "not_applicable",
            "evidence": f"Historical XR review record on file ({latest_hist.reason or latest_hist.value}), no active review",
            "year": latest_hist.year,
            "scope": "XR Review",
            "historical": True,
        }

    # Check active events
    under_review_ev = [e for e in active_events if e.value == "under_review"]
    if under_review_ev:
        ev = under_review_ev[0]
        return {
            "system": "xr_review",
            "state": "under_review",
            "evidence": ev.reason or "Journal actively marked as Under Review in XR",
            "year": ev.year,
            "scope": "XR Review",
            "historical": False,
        }
    
    flagged_ev = [e for e in active_events if e.value == "flagged"]
    if flagged_ev:
        ev = flagged_ev[0]
        return {
            "system": "xr_review",
            "state": "flagged",
            "evidence": ev.reason or "Actively flagged in XR",
            "year": ev.year,
            "scope": "XR Review",
            "historical": False,
        }

    ev = active_events[0]
    return {
        "system": "xr_review",
        "state": ev.value,
        "evidence": ev.reason or f"XR status {ev.value}",
        "year": ev.year,
        "scope": "XR Review",
        "historical": False,
    }


def _check_clarivate(
    record: JournalRecord,
    as_of: datetime,
    status_max_age_days: int = 30,
) -> Dict[str, Any]:
    events = [r for r in record.risks if r.system == "clarivate"]
    if not events:
        return {
            "system": "clarivate",
            "state": "unknown",
            "evidence": "No Clarivate indexing/on-hold data available",
            "year": None,
            "scope": "Clarivate Status",
            "historical": False,
        }

    evaluated_events = []

    for ev in events:
        prov = ev.provenance
        has_official_host = _is_official_clarivate_host(prov.source_url)
        valid_authority = prov.authority in ("official", "user")

        is_positive_risk = ev.value in ("on_hold", "delisted", "flagged")

        if is_positive_risk:
            evaluated_events.append((
                ev,
                ev.value,
                ev.reason or f"Clarivate {ev.value} recorded (source: {prov.source_url or prov.source_id})",
                True
            ))
            continue

        if not prov.observed_at:
            evaluated_events.append((ev, "unknown", "Missing observed_at date; retrieved_at cannot be used", False))
            continue

        obs_dt = _parse_iso_date(prov.observed_at)
        if not obs_dt:
            evaluated_events.append((ev, "unknown", "Invalid observed_at format", False))
            continue

        if obs_dt > as_of:
            evaluated_events.append((ev, "unknown", "Future observed_at date rejected", False))
            continue

        age_days = (as_of - obs_dt).total_seconds() / 86400.0
        if age_days > status_max_age_days:
            evaluated_events.append((ev, "stale", f"Status observed {int(age_days)} days ago (> {status_max_age_days}d)", False))
            continue

        if not (has_official_host and valid_authority):
            evaluated_events.append((
                ev,
                "unknown",
                "Negative claim lacks official Clarivate host URL or recognized official/user authority",
                False
            ))
            continue

        val = "indexed" if ev.value == "indexed" else ("not_listed" if ev.value == "not_listed" else "resolved")
        evaluated_events.append((
            ev,
            val,
            ev.reason or f"Clarivate {val} verified from official host {prov.source_url}",
            False
        ))

    positive_entries = [e for e in evaluated_events if e[3]]
    fresh_indexed_entries = [e for e in evaluated_events if e[1] == "indexed"]

    if positive_entries and fresh_indexed_entries:
        pos_ev, pos_state, pos_reason, _ = positive_entries[0]
        return {
            "system": "clarivate",
            "state": "conflict",
            "evidence": f"Conflict: Record has {pos_state} ({pos_reason}) concurrently with indexed record",
            "year": pos_ev.year,
            "scope": "Clarivate Status",
            "historical": False,
        }

    if positive_entries:
        ev, state, reason, _ = positive_entries[0]
        return {
            "system": "clarivate",
            "state": state,
            "evidence": reason,
            "year": ev.year,
            "scope": "Clarivate Status",
            "historical": ev.historical,
        }

    if fresh_indexed_entries:
        ev, state, reason, _ = fresh_indexed_entries[0]
        return {
            "system": "clarivate",
            "state": "indexed",
            "evidence": reason,
            "year": ev.year,
            "scope": "Clarivate Status",
            "historical": False,
        }

    stale_entries = [e for e in evaluated_events if e[1] == "stale"]
    if stale_entries:
        ev, state, reason, _ = stale_entries[0]
        return {
            "system": "clarivate",
            "state": "stale",
            "evidence": reason,
            "year": ev.year,
            "scope": "Clarivate Status",
            "historical": ev.historical,
        }

    ev, state, reason, _ = evaluated_events[0]
    return {
        "system": "clarivate",
        "state": state,
        "evidence": reason,
        "year": ev.year,
        "scope": "Clarivate Status",
        "historical": ev.historical,
    }


def _check_identity_and_metadata(record: JournalRecord) -> Dict[str, Any]:
    if record.identity_warnings:
        return {
            "system": "identity",
            "state": "conflict",
            "evidence": f"Identity warnings present: {'; '.join(record.identity_warnings)}",
            "year": None,
            "scope": "Identity Verification",
            "historical": False,
        }
    return {
        "system": "identity",
        "state": "verified",
        "evidence": "Single distinct identity without ambiguity warnings",
        "year": None,
        "scope": "Identity Verification",
        "historical": False,
    }


def _normalize_issn_internal(issn: str) -> str:
    clean = issn.strip().upper().replace("-", "")
    if len(clean) == 8:
        return f"{clean[:4]}-{clean[4:]}"
    return clean


def _check_school_policy(
    record: JournalRecord,
    policy: SchoolPolicy,
    as_of: datetime,
) -> Dict[str, Any]:
    is_expired = False
    if policy.effective_until:
        until_dt = _parse_iso_date(policy.effective_until)
        if until_dt and as_of > until_dt:
            is_expired = True

    if policy.effective_from:
        from_dt = _parse_iso_date(policy.effective_from)
        if from_dt and as_of < from_dt:
            is_expired = True

    if is_expired:
        return {
            "system": "school",
            "state": "stale",
            "evidence": f"School policy {policy.name} is not effective as of {as_of.isoformat()}",
            "year": None,
            "scope": f"Policy: {policy.name}",
            "historical": True,
        }

    record_issns = {_normalize_issn_internal(i) for i in record.issns if i}
    policy_prohibited_issns = {_normalize_issn_internal(i) for i in policy.prohibited_issns if i}
    matched_issns = record_issns.intersection(policy_prohibited_issns)
    if matched_issns:
        return {
            "system": "school",
            "state": "flagged",
            "evidence": f"ISSN {sorted(list(matched_issns))} explicitly prohibited by school policy {policy.name}",
            "year": None,
            "scope": f"Policy: {policy.name}",
            "historical": False,
        }

    record_titles = {record.title.strip().lower()}
    if record.title_zh:
        record_titles.add(record.title_zh.strip().lower())
    for a in record.aliases:
        record_titles.add(a.strip().lower())

    for pt in policy.prohibited_titles:
        if pt.strip().lower() in record_titles:
            return {
                "system": "school",
                "state": "flagged",
                "evidence": f"Journal title '{pt}' prohibited by school policy {policy.name}",
                "year": None,
                "scope": f"Policy: {policy.name}",
                "historical": False,
            }

    if policy.allow_oa is False:
        if record.oa_mode in ("full", "diamond"):
            return {
                "system": "school",
                "state": "flagged",
                "evidence": f"OA mode '{record.oa_mode}' prohibited by school policy {policy.name}",
                "year": None,
                "scope": f"Policy: {policy.name}",
                "historical": False,
            }

        if record.oa_mode in ("hybrid", "unknown"):
            return {"system": "school", "state": "unknown",
                    "evidence": "OA publication route has not been verified for this manuscript",
                    "year": None, "scope": f"Policy: {policy.name}", "historical": False}

    if policy.required_indexing:
        record_indexing = set(i.lower() for i in record.indexing)
        missing_idx = []
        for req in policy.required_indexing:
            req_l = req.lower()
            if req_l in ("sci", "scie"):
                if "sci" not in record_indexing and "scie" not in record_indexing:
                    missing_idx.append(req)
            else:
                if req_l not in record_indexing:
                    missing_idx.append(req)
        if missing_idx:
            return {
                "system": "school",
                "state": "unknown",
                "evidence": f"Missing required indexing: {missing_idx} for policy {policy.name}",
                "year": None,
                "scope": f"Policy: {policy.name}",
                "historical": False,
            }

    if policy.allowed_quartiles and policy.rank_system and policy.rank_year:
        matched_rankings = [
            r for r in record.rankings
            if r.system == policy.rank_system and r.year == policy.rank_year and r.quartile is not None
        ]
        if not matched_rankings:
            return {
                "system": "school",
                "state": "unknown",
                "evidence": f"No ranking found for required system {policy.rank_system} year {policy.rank_year}",
                "year": policy.rank_year,
                "scope": f"Policy: {policy.name}",
                "historical": False,
            }
        
        any_allowed = any(r.quartile in policy.allowed_quartiles for r in matched_rankings)
        if not any_allowed:
            found_qs = [r.quartile for r in matched_rankings]
            return {
                "system": "school",
                "state": "flagged",
                "evidence": f"Journal quartiles {found_qs} not in allowed {policy.allowed_quartiles}",
                "year": policy.rank_year,
                "scope": f"Policy: {policy.name}",
                "historical": False,
            }

    return {
        "system": "school",
        "state": "not_listed",
        "evidence": f"Complies with checked school policy rules ({policy.name})",
        "year": None,
        "scope": f"Policy: {policy.name}",
        "historical": False,
    }


def assess_risk(
    record: JournalRecord,
    coverage: Optional[Dict[str, Any]] = None,
    policy: Optional[SchoolPolicy] = None,
    filters: Optional[SearchFilters] = None,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pure function to evaluate multi-source warning radar and policy compliance."""
    if not isinstance(record, JournalRecord):
        raise JournalError("INVALID_RECORD", "Expected JournalRecord instance")

    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    status_max_age_days = filters.status_max_age_days if filters else 30
    target_warning_years = filters.warning_years if filters and filters.warning_years else []
    risk_policy = filters.risk_policy if filters else "exclude_known"

    checks: List[Dict[str, Any]] = []

    # 1. CAS Warning List Check
    cas_res = _check_cas_warning(record, target_warning_years, coverage, as_of)
    checks.append(cas_res)

    # 2. XR Review Check
    xr_res = _check_xr_review(record)
    checks.append(xr_res)

    # 3. Clarivate Status Check
    clarivate_res = _check_clarivate(record, as_of, status_max_age_days)
    checks.append(clarivate_res)

    # 4. Identity & Metadata Ambiguity Check
    id_res = _check_identity_and_metadata(record)
    checks.append(id_res)

    # 5. School Policy Check (if provided)
    if policy:
        school_res = _check_school_policy(record, policy, as_of)
        checks.append(school_res)

    # Historical caution determination:
    # Any record with historical=True or past years or superseded warnings
    historical_caution = False
    for r in record.risks:
        if r.value in ("flagged", "on_hold", "under_review", "delisted"):
            if r.historical or "superseded" in (r.reason or "").lower():
                historical_caution = True
            elif target_warning_years and r.year and r.year not in target_warning_years:
                historical_caution = True
            elif not target_warning_years and r.year and r.year < as_of.year:
                historical_caution = True

    for c in checks:
        if c.get("historical") and c.get("state") in ("flagged", "under_review", "on_hold", "delisted", "not_listed"):
            if c.get("historical") is True:
                # If check itself flags historical evidence
                if any(r.value in ("flagged", "on_hold", "under_review", "delisted") for r in record.risks):
                    historical_caution = True

    # Active states (must EXCLUDE checks where historical is True!)
    active_flagged = any(c["state"] == "flagged" and not c.get("historical", False) for c in checks)
    active_on_hold = any(c["state"] == "on_hold" and not c.get("historical", False) for c in checks)
    active_delisted = any(c["state"] == "delisted" and not c.get("historical", False) for c in checks)
    active_under_review = any(c["state"] == "under_review" and not c.get("historical", False) for c in checks)

    has_hard_risk = active_flagged or active_on_hold or active_delisted

    required_checks = [c for c in checks if c["system"] in ("cas_warning", "clarivate", "identity", "school")]

    has_required_unknown = any(c["state"] == "unknown" for c in required_checks)
    has_required_stale = any(c["state"] == "stale" for c in required_checks)
    has_required_conflict = any(c["state"] == "conflict" for c in required_checks)

    needs_verification = any(c["state"] in ("unknown", "stale", "conflict") for c in checks)

    # Determine conclusion:
    # Priority: active hard risk / active under_review > required conflict > historical_caution > needs_verification > no_known_flags_in_checked_sources
    if has_hard_risk or active_under_review:
        conclusion = "flagged"
    elif has_required_conflict:
        conclusion = "needs_verification"
    elif historical_caution:
        conclusion = "historical_caution"
    elif needs_verification:
        conclusion = "needs_verification"
    else:
        conclusion = "no_known_flags_in_checked_sources"

    # Blocked decision:
    # exclude_known: blocks active hard risk and active under_review (historical caution is NOT blocked!)
    # verified_only: blocks active hard risk, active under_review, OR any required check is unknown/stale/conflict
    # include_flagged: never blocked
    blocked = False
    if risk_policy == "include_flagged":
        blocked = False
    elif risk_policy == "verified_only":
        if has_hard_risk or active_under_review:
            blocked = True
        elif has_required_unknown or has_required_stale or has_required_conflict:
            blocked = True
    else:  # exclude_known
        if has_hard_risk or active_under_review:
            blocked = True

    data = {
        "conclusion": conclusion,
        "checks": checks,
        "blocked": blocked,
        "historical_caution": historical_caution,
        "needs_verification": needs_verification,
        "risk_policy": risk_policy,
        "as_of": as_of.isoformat(),
    }

    warnings = []
    if needs_verification:
        warnings.append("Risk/indexing verification has unknown, stale, conflicting, or ambiguous dimensions.")
    if historical_caution:
        warnings.append("Journal has historical caution or past warning records.")
    if record.identity_warnings:
        warnings.extend(record.identity_warnings)

    return response(data=data, warnings=warnings, coverage=coverage or {})
