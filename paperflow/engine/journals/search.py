"""Search, compare, and reference analysis for academic journals.

Pure functions only: no network, no disk I/O, deterministic output.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    JournalError,
    JournalRecord,
    Metric,
    Ranking,
    RiskEvent,
    SchoolPolicy,
    SearchFilters,
    response,
)
from .risk import _normalize_issn_internal, _parse_iso_date, assess_risk


# Fixed topic keywords mapping for local relevance matching
FIELD_KEYWORDS: Dict[str, List[str]] = {
    "cs_ai": [
        "artificial intelligence", "machine learning", "deep learning", "neural network",
        "computer vision", "pattern recognition", "data mining", "natural language processing",
        "ai", "robotics", "intelligent systems", "人工智能", "机器学习", "深度学习", "计算机视觉", "模式识别"
    ],
    "cybersecurity": [
        "cybersecurity", "information security", "network security", "cryptography",
        "privacy", "intrusion detection", "malware", "secure computing",
        "网络安全", "信息安全", "密码学", "隐私保护", "系统安全"
    ],
    "fault_diagnosis": [
        "fault diagnosis", "condition monitoring", "prognostics and health management",
        "phm", "vibration analysis", "bearing fault", "signal processing", "defect detection",
        "故障诊断", "状态监测", "健康管理", "轴承故障", "信号处理"
    ],
    "medical_imaging": [
        "medical imaging", "medical image", "radiology", "magnetic resonance", "mri",
        "computed tomography", "ct", "ultrasound", "biomedical imaging", "pet scan",
        "医学图像", "医学成像", "放射学", "磁共振", "超声医学"
    ],
}


def _normalize_str(s: str) -> str:
    if not s:
        return ""
    cleaned = re.sub(r"[\s\-_,.:;'/\"()]+", " ", s.lower()).strip()
    return cleaned


def _normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    clean = doi.strip().lower()
    clean = re.sub(r"^https?://(dx\.)?doi\.org/", "", clean)
    clean = re.sub(r"^doi:\s*", "", clean)
    return clean.strip()


def _calculate_relevance(
    record: JournalRecord,
    query: str,
    field_filter: str,
) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []

    norm_query = _normalize_str(query)
    q_tokens = [t for t in norm_query.split() if len(t) > 1] if norm_query else []

    title_norm = _normalize_str(record.title)
    title_zh_norm = _normalize_str(record.title_zh)
    aliases_norm = [_normalize_str(a) for a in record.aliases]

    if norm_query:
        if norm_query == title_norm or (title_zh_norm and norm_query == title_zh_norm):
            score += 100.0
            reasons.append("Exact title match")
        elif any(norm_query == a for a in aliases_norm):
            score += 90.0
            reasons.append("Exact alias match")
        elif norm_query in title_norm or (title_zh_norm and norm_query in title_zh_norm):
            score += 50.0
            reasons.append("Title substring match")
        else:
            matched_tokens = [t for t in q_tokens if t in title_norm or (title_zh_norm and t in title_zh_norm)]
            if matched_tokens:
                token_score = 30.0 * (len(matched_tokens) / len(q_tokens))
                score += token_score
                reasons.append(f"Title matched tokens: {matched_tokens}")

        query_issn = _normalize_issn_internal(query)
        record_issns = [_normalize_issn_internal(i) for i in record.issns if i]
        if query_issn in record_issns:
            score += 150.0
            reasons.append(f"ISSN match: {query_issn}")

    target_field = field_filter or ""
    all_target_keywords: List[str] = []
    if target_field in FIELD_KEYWORDS:
        all_target_keywords.extend(FIELD_KEYWORDS[target_field])

    record_fields_norm = [_normalize_str(f) for f in record.fields]
    if target_field:
        target_norm = _normalize_str(target_field)
        if any(target_norm in rf for rf in record_fields_norm):
            score += 40.0
            reasons.append(f"Field matches {target_field}")

    record_text = f"{title_norm} {title_zh_norm} {' '.join(record_fields_norm)}"
    for rk in record.rankings:
        record_text += f" {_normalize_str(rk.category)}"

    for kw in all_target_keywords:
        if kw in record_text:
            score += 15.0
            reasons.append(f"Subject keyword match: {kw}")
            break

    return score, reasons


def _evaluate_filters(
    record: JournalRecord,
    filters: SearchFilters,
    coverage: Optional[Dict[str, Any]],
    policy: Optional[SchoolPolicy],
    as_of: datetime,
) -> Tuple[bool, bool, Dict[str, str], List[str]]:
    """
    Evaluates whether record passes filters.
    Strictly forbids cherry-picking the best value when multiple sources conflict.
    If multiple sources conflict or lack clear calibration, fail hard constraints.
    """
    passed = True
    is_provisional = False
    rejected: Dict[str, str] = {}
    missing_hard: List[str] = []

    # 1. Kind check
    if filters.kind and record.kind != filters.kind:
        rejected["kind"] = f"Record kind {record.kind} != filter {filters.kind}"
        passed = False

    # 2. Indexing check (SCI / SCIE cannot be replaced by ESCI)
    if filters.indexing:
        rec_idx = {i.strip().lower() for i in record.indexing if i}
        missing_idx = []
        for req in filters.indexing:
            req_l = req.strip().lower()
            if req_l in ("sci", "scie"):
                if "sci" not in rec_idx and "scie" not in rec_idx:
                    missing_idx.append(req)
            else:
                if req_l not in rec_idx:
                    missing_idx.append(req)
        if missing_idx:
            rejected["indexing"] = f"Missing indexing: {missing_idx}"
            passed = False

    # 3. Ranking / Quartiles / Top / CCF check
    if filters.rank_system and filters.rank_year is not None:
        matching_ranks = [
            r for r in record.rankings
            if r.system == filters.rank_system and r.year == filters.rank_year
        ]
        if not matching_ranks:
            missing_hard.append(f"rankings_{filters.rank_system}_{filters.rank_year}")
            rejected["ranking"] = f"No ranking found for {filters.rank_system} year {filters.rank_year}"
            passed = False
        else:
            if filters.quartiles:
                # Quartiles must explicitly match specified quartiles
                # Check for contradictory rankings in the same category
                q_matched = [r for r in matching_ranks if r.quartile is not None and r.quartile in filters.quartiles]
                if not q_matched:
                    found_qs = [r.quartile for r in matching_ranks if r.quartile is not None]
                    if not found_qs:
                        missing_hard.append("quartile_unknown")
                        rejected["quartiles"] = "Quartile unknown in specified rank system/year"
                    else:
                        rejected["quartiles"] = f"Quartiles {found_qs} not in desired {filters.quartiles}"
                    passed = False

            if filters.category:
                cat_norm = _normalize_str(filters.category)
                cat_matched = [r for r in matching_ranks if cat_norm in _normalize_str(r.category)]
                if not cat_matched:
                    rejected["category"] = f"Category '{filters.category}' not matched"
                    passed = False

            if filters.category_type:
                type_matched = [r for r in matching_ranks if r.category_type == filters.category_type]
                if not type_matched:
                    rejected["category_type"] = f"Category type '{filters.category_type}' not matched"
                    passed = False

            if filters.top is not None:
                top_matched = [r for r in matching_ranks if r.top == filters.top]
                if not top_matched:
                    rejected["top"] = f"Top status {filters.top} not matched"
                    passed = False

            if filters.ccf_grades:
                grade_matched = [
                    r for r in matching_ranks
                    if r.grade and r.grade.strip().upper() in [g.strip().upper() for g in filters.ccf_grades]
                ]
                if not grade_matched:
                    found_grades = [r.grade for r in matching_ranks if r.grade]
                    rejected["ccf_grades"] = f"CCF grade {found_grades} not in {filters.ccf_grades}"
                    passed = False

    # 4. OA Mode check
    if filters.oa_mode != "any":
        if record.oa_mode == "unknown":
            missing_hard.append("oa_mode_unknown")
            rejected["oa_mode"] = "OA mode is unknown"
            passed = False
        elif record.oa_mode != filters.oa_mode:
            rejected["oa_mode"] = f"OA mode '{record.oa_mode}' != '{filters.oa_mode}'"
            passed = False

    # 5. APC Limit check
    # Currency must strictly match, no implicit FX conversion
    # Freshness: observed_at must not be stale (> dynamic_max_age_days)
    # Anti-cherrypicking: If multiple APC metrics exist for the target currency and conflict,
    # the worst-case (maximum) or uncalibrated conflict must not silently pass.
    if filters.max_apc is not None:
        apc_metrics = [
            m for m in record.metrics
            if ("apc" in m.name.lower() or "fee" in m.name.lower() or m.currency)
            and m.currency and m.currency.upper() == (filters.currency or "").upper()
        ]
        if not apc_metrics:
            missing_hard.append("apc_missing")
            rejected["max_apc"] = f"APC information missing for currency {filters.currency}"
            passed = False
        else:
            # Check freshness
            fresh_apcs = []
            for m in apc_metrics:
                if m.provenance.observed_at:
                    obs_dt = _parse_iso_date(m.provenance.observed_at)
                    if obs_dt:
                        age_days = (as_of - obs_dt).total_seconds() / 86400.0
                        if age_days > filters.dynamic_max_age_days:
                            continue
                fresh_apcs.append(m)

            if not fresh_apcs:
                missing_hard.append("apc_stale")
                rejected["max_apc"] = f"All APC records are stale (> {filters.dynamic_max_age_days}d)"
                passed = False
            else:
                # Anti-cherrypicking: Check all fresh values. If any fresh value exceeds max_apc, fail.
                values = []
                for m in fresh_apcs:
                    v = m.upper if m.upper is not None else m.value
                    if v is not None:
                        values.append(v)
                
                if not values:
                    missing_hard.append("apc_unspecified_value")
                    rejected["max_apc"] = "APC values unspecified"
                    passed = False
                elif max(values) > filters.max_apc:
                    # Worst-case violates upper limit (cannot pick lowest to bypass!)
                    rejected["max_apc"] = f"Highest observed APC ({max(values)}) > limit {filters.max_apc} {filters.currency}"
                    passed = False

    # 6. First Decision Days check
    # Only comparable metrics with clear stage == "first_decision"
    # Anti-cherrypicking: If conflicting sources exist, cannot pick fastest.
    if filters.max_first_decision_days is not None:
        fd_metrics = [
            m for m in record.metrics
            if m.stage == "first_decision" or m.name == "first_decision_days"
        ]
        if not fd_metrics:
            missing_hard.append("first_decision_days_missing")
            rejected["max_first_decision_days"] = "First decision days missing"
            passed = False
        else:
            fresh_fds = []
            for m in fd_metrics:
                if m.provenance.observed_at:
                    obs_dt = _parse_iso_date(m.provenance.observed_at)
                    if obs_dt:
                        age_days = (as_of - obs_dt).total_seconds() / 86400.0
                        if age_days > filters.dynamic_max_age_days:
                            continue
                fresh_fds.append(m)

            if not fresh_fds:
                missing_hard.append("first_decision_days_stale")
                rejected["max_first_decision_days"] = f"First decision metrics stale (> {filters.dynamic_max_age_days}d)"
                passed = False
            else:
                # Any unbounded record (>X weeks) cannot pass cycle limit
                has_unbounded = any(m.comparator in ("gt", "ge") and m.upper is None for m in fresh_fds)
                if has_unbounded:
                    rejected["max_first_decision_days"] = "Contains unbounded cycle metric (> X without upper limit)"
                    passed = False
                else:
                    bounds = []
                    for m in fresh_fds:
                        b = m.upper if m.upper is not None else m.value
                        if b is not None:
                            bounds.append(b)
                    
                    if not bounds:
                        missing_hard.append("first_decision_days_unspecified")
                        rejected["max_first_decision_days"] = "First decision days values unspecified"
                        passed = False
                    elif max(bounds) > filters.max_first_decision_days:
                        # Upper bound exceeds limit (do not cherrypick smallest)
                        rejected["max_first_decision_days"] = f"Worst-case first decision days ({max(bounds)}) > limit {filters.max_first_decision_days}"
                        passed = False

    # 7. Annual Articles check
    # Must specify metric_year, strictly filter by metric.year == filters.metric_year
    if filters.min_annual_articles is not None:
        art_metrics = [
            m for m in record.metrics
            if (m.name in ("annual_articles", "article_volume", "articles") or "article" in m.name.lower())
            and m.year == filters.metric_year
        ]
        if not art_metrics:
            missing_hard.append(f"annual_articles_{filters.metric_year}_missing")
            rejected["min_annual_articles"] = f"Annual articles missing for year {filters.metric_year}"
            passed = False
        else:
            # Check values
            vals = [m.value for m in art_metrics if m.value is not None]
            if not vals:
                missing_hard.append(f"annual_articles_{filters.metric_year}_empty")
                rejected["min_annual_articles"] = "Annual articles value is empty"
                passed = False
            elif min(vals) < filters.min_annual_articles:
                rejected["min_annual_articles"] = f"Annual articles ({min(vals)}) < required {filters.min_annual_articles}"
                passed = False

    if not passed and missing_hard:
        is_provisional = True

    return passed, is_provisional, rejected, missing_hard


def search_records(
    records: List[JournalRecord],
    query: str = "",
    filters: Optional[SearchFilters] = None,
    sort_by: str = "relevance",
    limit: int = 20,
    offset: int = 0,
    coverage: Optional[Dict[str, Any]] = None,
    policy: Optional[SchoolPolicy] = None,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pure search function implementing filters, ranking, risk gating, and pagination."""
    if not isinstance(records, list):
        raise JournalError("INVALID_INPUT", "records must be a list of JournalRecord")
    for r in records:
        if not isinstance(r, JournalRecord):
            raise JournalError("INVALID_RECORD", "Record is not an instance of JournalRecord")

    if filters is None:
        filters = SearchFilters()

    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    rejected_constraints: Dict[str, int] = {}
    eligible_items = []
    provisional_items = []

    for rec in records:
        # Risk assessment
        risk_res = assess_risk(rec, coverage=coverage, policy=policy, filters=filters, as_of=as_of)
        risk_data = risk_res["data"]
        
        is_blocked = risk_data["blocked"]
        needs_verif = risk_data["needs_verification"]
        historical_caution = risk_data["historical_caution"]

        # Filter evaluation
        passed, is_prov, rejected_reasons, missing_hard = _evaluate_filters(
            rec, filters, coverage, policy, as_of
        )

        for constraint in rejected_reasons.keys():
            rejected_constraints[constraint] = rejected_constraints.get(constraint, 0) + 1

        if is_blocked:
            rejected_constraints["risk_blocked"] = rejected_constraints.get("risk_blocked", 0) + 1
            continue

        rel_score, rel_reasons = _calculate_relevance(rec, query, filters.field)

        item = {
            "record": rec,
            "journal_id": rec.journal_id or rec.title,
            "title": rec.title,
            "issns": rec.issns,
            "relevance_score": rel_score,
            "match_reasons": rel_reasons,
            "risk": risk_data,
            "rejected_reasons": rejected_reasons,
            "missing_hard_constraints": missing_hard,
        }

        # Under verified_only, any needs_verification or blocked cannot enter results or provisional
        if filters.risk_policy == "verified_only":
            if needs_verif or is_blocked:
                continue

        if passed:
            if filters.risk_policy == "exclude_known" and needs_verif:
                provisional_items.append(item)
            else:
                eligible_items.append(item)
        else:
            if is_prov and filters.allow_unknown:
                provisional_items.append(item)

    # Sorting
    def sort_key(item):
        rec: JournalRecord = item["record"]
        if sort_by == "relevance":
            return (-item["relevance_score"], rec.title)
        
        elif sort_by == "speed":
            fd_metrics = [m for m in rec.metrics if m.stage == "first_decision" or m.name == "first_decision_days"]
            vals = []
            for m in fd_metrics:
                v = m.upper if m.upper is not None else m.value
                if v is not None:
                    vals.append(v)
            val = min(vals) if vals else float("inf")
            return (val, -item["relevance_score"], rec.title)

        elif sort_by == "impact":
            target_year = filters.metric_year
            if_metrics = [
                m for m in rec.metrics
                if ("impact_factor" in m.name.lower() or m.name.lower() == "if")
                and (target_year is None or m.year == target_year)
            ]
            val = -float("inf")
            for m in if_metrics:
                v = m.value if m.value is not None else m.upper
                if v is not None and v > val:
                    val = v
            return (-val if val != -float("inf") else float("inf"), -item["relevance_score"], rec.title)

        elif sort_by == "volume":
            target_year = filters.metric_year
            vol_metrics = [
                m for m in rec.metrics
                if (m.name in ("annual_articles", "article_volume", "articles") or "article" in m.name.lower())
                and (target_year is None or m.year == target_year)
            ]
            val = -float("inf")
            for m in vol_metrics:
                if m.value is not None and m.value > val:
                    val = m.value
            return (-val if val != -float("inf") else float("inf"), -item["relevance_score"], rec.title)

        elif sort_by == "balanced":
            risk_penalty = 1 if item["risk"]["conclusion"] != "no_known_flags_in_checked_sources" else 0
            return (risk_penalty, -item["relevance_score"], rec.title)

        else:
            return (-item["relevance_score"], rec.title)

    eligible_items.sort(key=sort_key)
    provisional_items.sort(key=sort_key)

    total_results = len(eligible_items)
    total_provisional = len(provisional_items)

    paged_results = eligible_items[offset : offset + limit]

    def serialize_item(it):
        rec: JournalRecord = it["record"]
        return {
            "journal_id": it["journal_id"],
            "title": it["title"],
            "title_zh": rec.title_zh,
            "kind": rec.kind,
            "publisher": rec.publisher,
            "issns": rec.issns,
            "indexing": rec.indexing,
            "oa_mode": rec.oa_mode,
            "relevance_score": it["relevance_score"],
            "match_reasons": it["match_reasons"],
            "risk": it["risk"],
            "rankings": [r.model_dump() for r in rec.rankings],
            "metrics": [m.model_dump() for m in rec.metrics],
            "experiences_count": len(rec.experiences),
            "rejected_reasons": it["rejected_reasons"],
        }

    serialized_results = [serialize_item(it) for it in paged_results]
    serialized_provisional = [serialize_item(it) for it in provisional_items]

    data = {
        "results": serialized_results,
        "provisional_results": serialized_provisional,
    }

    return response(
        data=data,
        total=total_results,
        total_provisional=total_provisional,
        limit=limit,
        offset=offset,
        rejected_constraints=rejected_constraints,
        coverage=coverage or {},
    )


def compare_records(
    records: List[JournalRecord],
    journal_ids: List[str],
    rank_system: Optional[str] = None,
    rank_year: Optional[int] = None,
    coverage: Optional[Dict[str, Any]] = None,
    policy: Optional[SchoolPolicy] = None,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compare up to 10 journals side by side with all categories displayed."""
    if not isinstance(records, list):
        raise JournalError("INVALID_INPUT", "records must be a list of JournalRecord")
    if not isinstance(journal_ids, list):
        raise JournalError("INVALID_INPUT", "journal_ids must be a list")
    if len(journal_ids) > 10:
        raise JournalError("EXCEEDED_LIMIT", "compare_records supports at most 10 journal_ids")

    if as_of is None:
        as_of = datetime.now(timezone.utc)

    id_map: Dict[str, JournalRecord] = {}
    for r in records:
        if r.journal_id:
            id_map[r.journal_id.lower()] = r
        id_map[r.title.strip().lower()] = r
        for i in r.issns:
            norm_i = _normalize_issn_internal(i)
            if norm_i:
                id_map[norm_i.lower()] = r

    comparisons: List[Dict[str, Any]] = []
    missing_ids: List[str] = []

    for target_id in journal_ids:
        tid_norm = target_id.strip().lower()
        rec = id_map.get(tid_norm)
        if not rec:
            norm_i = _normalize_issn_internal(target_id)
            if norm_i:
                rec = id_map.get(norm_i.lower())

        if not rec:
            missing_ids.append(target_id)
            continue

        risk_res = assess_risk(rec, coverage=coverage, policy=policy, as_of=as_of)

        filtered_rankings = []
        for rk in rec.rankings:
            if rank_system and rk.system != rank_system:
                continue
            if rank_year and rk.year != rank_year:
                continue
            filtered_rankings.append({
                "system": rk.system,
                "year": rk.year,
                "category": rk.category,
                "category_type": rk.category_type,
                "quartile": rk.quartile,
                "grade": rk.grade,
                "top": rk.top,
                "raw": rk.raw,
            })

        formatted_metrics = []
        for m in rec.metrics:
            formatted_metrics.append({
                "name": m.name,
                "stage": m.stage,
                "value": m.value,
                "lower": m.lower,
                "upper": m.upper,
                "comparator": m.comparator,
                "unit": m.unit,
                "currency": m.currency,
                "year": m.year,
                "observed_at": m.provenance.observed_at,
            })

        comp_entry = {
            "journal_id": rec.journal_id or rec.title,
            "title": rec.title,
            "title_zh": rec.title_zh,
            "issns": rec.issns,
            "publisher": rec.publisher,
            "kind": rec.kind,
            "oa_mode": rec.oa_mode,
            "indexing": rec.indexing,
            "rankings": filtered_rankings,
            "metrics": formatted_metrics,
            "risk": risk_res["data"],
            "experiences_count": len(rec.experiences),
        }
        comparisons.append(comp_entry)

    return response(
        data={
            "comparisons": comparisons,
            "missing_ids": missing_ids,
            "rank_system": rank_system,
            "rank_year": rank_year,
        }
    )


def analyze_references(
    records: List[JournalRecord],
    references: List[Dict[str, Any]],
    filters: Optional[SearchFilters] = None,
    coverage: Optional[Dict[str, Any]] = None,
    policy: Optional[SchoolPolicy] = None,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Analyze up to 200 bibliographic references, deduplicate by DOI, count journal distribution."""
    if not isinstance(records, list):
        raise JournalError("INVALID_INPUT", "records must be a list of JournalRecord")
    if not isinstance(references, list):
        raise JournalError("INVALID_INPUT", "references must be a list of dicts")
    if len(references) > 200:
        raise JournalError("EXCEEDED_LIMIT", "analyze_references accepts at most 200 references")

    if as_of is None:
        as_of = datetime.now(timezone.utc)

    seen_dois: Set[str] = set()
    deduped_refs: List[Dict[str, Any]] = []
    missing_metadata_dois: List[str] = []

    for ref in references:
        if not isinstance(ref, dict):
            continue
        raw_doi = ref.get("doi", "")
        norm_doi = _normalize_doi(raw_doi)
        if norm_doi:
            if norm_doi in seen_dois:
                continue
            seen_dois.add(norm_doi)

        j_title = ref.get("journal") or ref.get("journal_name") or ref.get("title", "")
        issn = ref.get("issn", "")

        if norm_doi and not j_title and not issn:
            missing_metadata_dois.append(norm_doi)
            continue

        deduped_refs.append(ref)

    issn_map: Dict[str, JournalRecord] = {}
    title_map: Dict[str, JournalRecord] = {}
    alias_map: Dict[str, List[JournalRecord]] = {}

    for r in records:
        for i in r.issns:
            ni = _normalize_issn_internal(i)
            if ni:
                issn_map[ni] = r
        t_norm = _normalize_str(r.title)
        if t_norm:
            title_map[t_norm] = r
        if r.title_zh:
            title_map[_normalize_str(r.title_zh)] = r
        for a in r.aliases:
            an = _normalize_str(a)
            if an:
                alias_map.setdefault(an, []).append(r)

    journal_counts: Dict[str, int] = {}
    journal_record_map: Dict[str, JournalRecord] = {}
    ambiguous_entries: List[Dict[str, Any]] = []
    unmatched_entries: List[Dict[str, Any]] = []

    for ref in deduped_refs:
        ref_issn = ref.get("issn", "")
        norm_ref_issn = _normalize_issn_internal(ref_issn) if ref_issn else ""
        ref_journal = ref.get("journal") or ref.get("journal_name") or ""
        ref_title_norm = _normalize_str(ref_journal)

        matched_record: Optional[JournalRecord] = None

        if norm_ref_issn and norm_ref_issn in issn_map:
            matched_record = issn_map[norm_ref_issn]
        elif ref_title_norm and ref_title_norm in title_map:
            matched_record = title_map[ref_title_norm]
        elif ref_title_norm and ref_title_norm in alias_map:
            candidates = alias_map[ref_title_norm]
            if len(candidates) == 1:
                matched_record = candidates[0]
            else:
                ambiguous_entries.append({
                    "reference": ref,
                    "candidates": [c.title for c in candidates],
                    "reason": "Multiple journals match alias",
                })
                continue

        if matched_record:
            jid = matched_record.journal_id or matched_record.title
            journal_counts[jid] = journal_counts.get(jid, 0) + 1
            journal_record_map[jid] = matched_record
        else:
            unmatched_entries.append(ref)

    distribution = []
    for jid, count in sorted(journal_counts.items(), key=lambda x: -x[1]):
        rec = journal_record_map[jid]
        risk_res = assess_risk(rec, coverage=coverage, policy=policy, filters=filters, as_of=as_of)
        
        distribution.append({
            "journal_id": jid,
            "title": rec.title,
            "title_zh": rec.title_zh,
            "issns": rec.issns,
            "sample_frequency": count,
            "frequency_note": "Sample reference frequency indicates citation co-occurrence, not acceptance rate",
            "risk": risk_res["data"],
            "indexing": rec.indexing,
            "oa_mode": rec.oa_mode,
            "rankings": [r.model_dump() for r in rec.rankings],
            "experiences": [e.model_dump() for e in rec.experiences],
        })

    data = {
        "total_references_analyzed": len(references),
        "deduplicated_references_count": len(deduped_refs),
        "matched_journals_count": len(distribution),
        "distribution": distribution,
        "missing_metadata_dois": missing_metadata_dois,
        "ambiguous_entries": ambiguous_entries,
        "unmatched_entries_count": len(unmatched_entries),
    }

    warnings = []
    if missing_metadata_dois:
        warnings.append(f"{len(missing_metadata_dois)} DOIs lack journal metadata and were not fetched online.")
    if ambiguous_entries:
        warnings.append(f"{len(ambiguous_entries)} reference titles have ambiguous journal candidates.")

    return response(data=data, warnings=warnings, coverage=coverage or {})
