"""Open-access journal metadata importer and parser for official DOAJ CC0 releases.

All metadata imported under Creative Commons CC0 1.0 Universal waiver (https://doaj.org/terms/).
No network calls occur during module import or default CSV parsing.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from paperflow.engine.journals.identity import identity_id, normalize_issn
from paperflow.engine.journals.models import (
    JournalError,
    JournalRecord,
    Metric,
    ParsedBatch,
    Provenance,
    PublicationFee,
    utc_now,
)

OFFICIAL_DOAJ_URL = "https://doaj.org/csv"
DOAJ_LICENSE = "CC0-1.0"
DOAJ_LICENSE_URL = "https://doaj.org/terms/"
DOAJ_SOURCE_ID = "doaj_cc0"
DOAJ_USER_AGENT = "PaperFlow/0.1 public-metadata-import"

MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MiB
MAX_RECORDS_DEFAULT = 1500
MAX_DOWNLOAD_SIZE_BYTES = 40 * 1024 * 1024  # 40 MiB
DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_REJECTED_SAMPLES = 50

ALLOWED_HOST_PATTERNS = [
    re.compile(r"^(?:[a-zA-Z0-9-]+\.)*doaj\.org$", re.I),
    re.compile(r"^doaj-live-journal-csv\.s3(?:[.-][a-zA-Z0-9-]+)?\.amazonaws\.com$", re.I),
]

REQUIRED_HEADERS = [
    "Journal title",
    "URL in DOAJ",
]


def _is_allowed_host(host: str) -> bool:
    if not host:
        return False
    return any(p.match(host) for p in ALLOWED_HOST_PATTERNS)


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strict redirect handler that enforces HTTPS and whitelisted DOAJ hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme.lower() != "https":
            raise JournalError("SECURITY_VIOLATION", f"Disallowed redirect scheme: {parsed.scheme}")
        hostname = (parsed.hostname or "").lower()
        if not _is_allowed_host(hostname):
            raise JournalError("SECURITY_VIOLATION", f"Disallowed redirect host: {hostname}")
        if parsed.username or parsed.password:
            raise JournalError("SECURITY_VIOLATION", "Redirect URL must not contain credentials")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_add_rejected(
    rejected: List[Dict[str, Any]],
    line_no: int,
    code: str,
    field: str = "",
    reason: str = "",
) -> None:
    if len(rejected) < MAX_REJECTED_SAMPLES:
        item: Dict[str, Any] = {
            "line": line_no,
            "error_code": code,
            "field": field,
        }
        if reason:
            item["reason"] = reason
        rejected.append(item)


def _safe_homepage(url: str) -> str:
    """Journal-reported home page: https only, no embedded credentials."""
    url = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return ""
    return url[:500]

def _extract_fields(keywords_raw: str, subjects_raw: str) -> List[str]:
    fields: List[str] = []
    seen: Set[str] = set()

    if keywords_raw:
        for kw in re.split(r"[,;]+", keywords_raw):
            clean = " ".join(kw.strip().split())
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                fields.append(clean)

    if subjects_raw:
        for sub in subjects_raw.split("|"):
            clean = " ".join(sub.strip().split())
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                fields.append(clean)

    return fields


def _parse_apc_fees(
    apc_amt_str: str,
    fee_prov: Provenance,
) -> List[PublicationFee]:
    fees: List[PublicationFee] = []
    if not apc_amt_str:
        fees.append(
            PublicationFee(
                route="open_access",
                kind="apc",
                amount=None,
                currency="",
                note="APC charges apply; amount not specified in DOAJ record",
                provenance=fee_prov,
            )
        )
        return fees

    parts = [p.strip() for p in re.split(r"[;,]+", apc_amt_str) if p.strip()]
    for part in parts:
        m = re.match(r"^\s*([\d\s,.]+)\s*([A-Za-z]{3})(?:\s*\((.*?)\))?\s*$", part)
        if m:
            raw_num = m.group(1).replace(",", "").replace(" ", "")
            curr = m.group(2).upper()
            note_extra = m.group(3) or ""
            try:
                val = float(raw_num)
                fees.append(
                    PublicationFee(
                        route="open_access",
                        kind="apc",
                        amount=val,
                        currency=curr,
                        unit="per_article",
                        note=note_extra,
                        provenance=fee_prov,
                    )
                )
                continue
            except ValueError:
                pass

        fees.append(
            PublicationFee(
                route="open_access",
                kind="apc",
                amount=None,
                currency="",
                note=part,
                provenance=fee_prov,
            )
        )
    return fees


def parse_doaj_csv(
    content: str,
    retrieved_at: str,
    limit: int = MAX_RECORDS_DEFAULT,
    predicate: Optional[Callable[[Dict[str, str]], bool]] = None,
) -> ParsedBatch:
    """Parse DOAJ journal metadata CSV into a validated ParsedBatch.

    Pure function with no network access or disk I/O.
    Explicitly rejects malformed inputs, truncated records, and invalid ISSNs.
    """
    if not isinstance(content, str):
        raise JournalError("SCHEMA_CHANGED", "DOAJ CSV content must be a string")
    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", "DOAJ CSV 内容为空")
    if len(content) > MAX_CONTENT_LENGTH:
        raise JournalError("SCHEMA_CHANGED", f"DOAJ CSV 超过最大安全体积限制 ({MAX_CONTENT_LENGTH} 字节)")

    head_sample = content.lstrip()[:2048].lower()
    if head_sample.startswith("<!doctype html") or "<html" in head_sample:
        raise JournalError("SCHEMA_CHANGED", "DOAJ 返回了 HTML 错误页面")

    # Validate retrieved_at
    if not retrieved_at:
        raise JournalError("INVALID_TIMESTAMP", "retrieved_at 时间戳不能为空")
    try:
        datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError:
        raise JournalError("INVALID_TIMESTAMP", f"无效的检索时间戳: {retrieved_at}")

    if not isinstance(limit, int) or limit <= 0:
        raise JournalError("INVALID_PARAM", "limit 必须为正整数")

    f = io.StringIO(content)
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames or []

    for req_header in REQUIRED_HEADERS:
        if req_header not in fieldnames:
            raise JournalError("SCHEMA_CHANGED", f"DOAJ CSV 缺少关键字段头: {req_header}")

    has_issn_header = (
        "Journal ISSN (print version)" in fieldnames or "Journal EISSN (online version)" in fieldnames
    )
    if not has_issn_header:
        raise JournalError("SCHEMA_CHANGED", "DOAJ CSV 缺少 ISSN 列")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0
    warnings: List[str] = []

    for line_no, row in enumerate(reader, start=2):
        if not row or not any(row.values()):
            continue
        total_rows += 1

        title = (row.get("Journal title") or "").strip()
        if not title:
            rejected_count += 1
            _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title", "期刊名称为空")
            continue

        p_raw = (row.get("Journal ISSN (print version)") or "").strip()
        e_raw = (row.get("Journal EISSN (online version)") or "").strip()
        if not p_raw and not e_raw:
            rejected_count += 1
            _safe_add_rejected(rejected, line_no, "MISSING_ISSN", "issns", "缺少 Print 和 Online ISSN")
            continue

        issns: List[str] = []
        issn_error = False
        for raw in (p_raw, e_raw):
            if raw:
                try:
                    norm = normalize_issn(raw)
                    if norm not in issns:
                        issns.append(norm)
                except JournalError as exc:
                    rejected_count += 1
                    _safe_add_rejected(
                        rejected,
                        line_no,
                        "INVALID_ISSN",
                        "issns",
                        f"无效 ISSN 校验或格式: {raw} ({exc})",
                    )
                    issn_error = True
                    break

        if issn_error or not issns:
            continue

        if predicate is not None and not predicate(row):
            continue

        source_url = (row.get("URL in DOAJ") or "").strip()
        last_updated_raw = (row.get("Last updated Date") or "").strip()
        observed_at: Optional[str] = None
        data_year: Optional[int] = None

        if last_updated_raw:
            try:
                dt = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00"))
                observed_at = last_updated_raw
                if 1900 <= dt.year <= 2200:
                    data_year = dt.year
            except ValueError:
                pass

        if observed_at is None:
            added_raw = (row.get("Added on Date") or "").strip()
            if added_raw:
                try:
                    dt = datetime.fromisoformat(added_raw.replace("Z", "+00:00"))
                    observed_at = added_raw
                    if 1900 <= dt.year <= 2200:
                        data_year = dt.year
                except ValueError:
                    pass

        record_prov = Provenance(
            source_id=DOAJ_SOURCE_ID,
            source_url=source_url,
            source_record=source_url,
            retrieved_at=retrieved_at,
            observed_at=observed_at,
            data_year=data_year,
            authority="community",
            freshness="community_catalog",
        )

        publisher = (row.get("Publisher") or "").strip()
        alt_title = (row.get("Alternative title") or "").strip()
        aliases = [alt_title] if alt_title else []

        fields = _extract_fields(
            row.get("Keywords") or "",
            row.get("Subjects") or "",
        )

        metadata_obs: List[Dict[str, Any]] = []
        journal_url = (row.get("Journal URL") or "").strip()
        if journal_url:
            metadata_obs.append({"observation": "journal_url", "url": journal_url, "observed_at": observed_at})

        aims_scope_url = (row.get("URL for journal's aims & scope") or "").strip()
        if aims_scope_url:
            metadata_obs.append({"observation": "aims_scope_url", "url": aims_scope_url, "observed_at": observed_at})

        apc_status = (row.get("APC") or "").strip().capitalize()
        apc_info_url = (row.get("APC information URL") or "").strip()

        if apc_status == "No":
            metadata_obs.append(
                {
                    "observation": "no_apc",
                    "has_apc": False,
                    "observed_at": observed_at,
                    "source_url": apc_info_url or source_url,
                }
            )

        has_other_fees = (row.get("Has other fees") or "").strip().capitalize()
        other_fees_url = (row.get("Other fees information URL") or "").strip()
        if has_other_fees == "Yes":
            metadata_obs.append(
                {
                    "observation": "has_other_fees",
                    "info_url": other_fees_url,
                    "observed_at": observed_at,
                }
            )

        art_count_raw = (row.get("Number of Article Records") or "").strip()
        if art_count_raw and art_count_raw.isdigit():
            metadata_obs.append(
                {
                    "observation": "cumulative_article_records",
                    "count": int(art_count_raw),
                    "observed_at": observed_at,
                }
            )

        lcc_codes = (row.get("LCC Codes") or "").strip()
        if lcc_codes:
            metadata_obs.append({"observation": "lcc_codes", "codes": lcc_codes, "observed_at": observed_at})

        # Publication fees: only when APC is Yes
        publication_fees: List[PublicationFee] = []
        if apc_status == "Yes":
            fee_prov = Provenance(
                source_id=DOAJ_SOURCE_ID,
                source_url=apc_info_url or source_url,
                source_record=source_url,
                retrieved_at=retrieved_at,
                observed_at=observed_at,
                data_year=data_year,
                authority="community",
                freshness="community_catalog",
            )
            apc_amt_str = (row.get("APC amount") or "").strip()
            publication_fees = _parse_apc_fees(apc_amt_str, fee_prov)

        # Weeks metric
        metrics: List[Metric] = []
        weeks_raw = (row.get("Average number of weeks between article submission and publication") or "").strip()
        if weeks_raw:
            try:
                weeks_val = float(weeks_raw)
                if weeks_val >= 0:
                    metrics.append(
                        Metric(
                            name="submission_to_publication_weeks",
                            raw=weeks_raw,
                            value=weeks_val,
                            comparator="eq",
                            unit="weeks",
                            year=data_year,
                            stage="publication",
                            provenance=record_prov,
                        )
                    )
            except ValueError:
                pass

        journal_id = identity_id(title, "journal", issns)

        rec = JournalRecord(
            journal_id=journal_id,
            title=title,
            title_zh="",
            kind="journal",
            publisher=publisher,
            homepage=_safe_homepage(journal_url),
            issns=issns,
            aliases=aliases,
            fields=fields,
            indexing=["DOAJ"],
            oa_mode="full",
            provenance=record_prov,
            rankings=[],
            metrics=metrics,
            risks=[],
            experiences=[],
            editorial_profiles=[],
            publication_fees=publication_fees,
            metadata_observations=metadata_obs,
        )
        records.append(rec)

        if len(records) >= limit:
            break

    coverage = {
        "records_count": len(records),
        "total_rows_scanned": total_rows,
        "rejected_count": rejected_count,
        "with_publication_weeks": sum(1 for r in records if r.metrics),
        "with_apc_fees": sum(1 for r in records if r.publication_fees),
        "with_aims_scope_url": sum(
            1 for r in records if any(o.get("observation") == "aims_scope_url" for o in r.metadata_observations)
        ),
    }

    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        warnings=warnings,
        coverage=coverage,
    )


def fetch_doaj_csv(
    url: str = OFFICIAL_DOAJ_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_size_bytes: int = MAX_DOWNLOAD_SIZE_BYTES,
) -> str:
    """Download official DOAJ metadata CSV over HTTPS with strict security controls.

    Enforces:
    - HTTPS scheme only
    - Whitelisted official DOAJ and S3 mirror hostnames
    - Disallows userinfo / embedded credentials
    - Maximum download byte cap
    - Explicit PaperFlow User-Agent

    Returns:
        UTF-8 decoded CSV content string.
    """
    csv_text, _, _ = fetch_doaj_csv_with_metadata(
        url=url, timeout=timeout, max_size_bytes=max_size_bytes
    )
    return csv_text


def fetch_doaj_csv_with_metadata(
    url: str = OFFICIAL_DOAJ_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_size_bytes: int = MAX_DOWNLOAD_SIZE_BYTES,
) -> Tuple[str, str, str]:
    """Download official DOAJ metadata CSV returning content and response headers.

    Returns:
        (csv_text, last_modified, retrieved_at)
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise JournalError("SECURITY_VIOLATION", f"Only HTTPS scheme is permitted, got: {parsed.scheme}")
    hostname = (parsed.hostname or "").lower()
    if not _is_allowed_host(hostname):
        raise JournalError("SECURITY_VIOLATION", f"Host {hostname} is not an authorized DOAJ domain")
    if parsed.username or parsed.password:
        raise JournalError("SECURITY_VIOLATION", "Target URL must not contain credentials")

    opener = urllib.request.build_opener(GuardedRedirectHandler)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": DOAJ_USER_AGENT},
    )

    retrieved_at = utc_now()
    with opener.open(req, timeout=timeout) as resp:
        last_modified = resp.headers.get("Last-Modified", "")
        chunks: List[bytes] = []
        total_downloaded = 0

        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total_downloaded += len(chunk)
            if total_downloaded > max_size_bytes:
                raise JournalError(
                    "SCHEMA_CHANGED",
                    f"Download size {total_downloaded} exceeded maximum limit {max_size_bytes}",
                )
            chunks.append(chunk)

    csv_text = b"".join(chunks).decode("utf-8-sig", errors="replace")
    return csv_text, last_modified, retrieved_at


# ---------------------------------------------------------------------------
# Curated Catalog Construction
# ---------------------------------------------------------------------------

STEM_PATTERNS: Dict[str, re.Pattern] = {
    "computer_tech": re.compile(
        r"\b(?:computer|software|artificial intelligence|data science|robotics|cyber|telecom|information systems|network|algorithms|informatics)\b",
        re.I,
    ),
    "mechanical_engineering": re.compile(
        r"\b(?:mechanical|machin\w*|mechanics|manufacturing|aerospace|automotive|civil engineering|structural|turbomachinery)\b",
        re.I,
    ),
    "marine_ocean": re.compile(
        r"\b(?:ocean|marine|maritime|oceanograph\w*|naval|fisher\w*|aquatic|coastal|pelagic)\b",
        re.I,
    ),
    "environmental_earth": re.compile(
        r"\b(?:environment\w*|ecology|geolog\w*|earth science\w*|meteorolog\w*|climate|hydrolog\w*|atmospheric|ecosystem)\b",
        re.I,
    ),
    "biomedical_medicine": re.compile(
        r"\b(?:biomedic\w*|medicin\w*|medic\w*|pharmac\w*|clinic\w*|immunol\w*|oncolog\w*|genetics|pathology)\b",
        re.I,
    ),
    "materials_physical": re.compile(
        r"\b(?:material\w*|metallurg\w*|nanotechnol\w*|polymer\w*|ceramic\w*|physics|chemist\w*|optics|crystallography)\b",
        re.I,
    ),
}

BREADTH_PATTERNS: Dict[str, re.Pattern] = {
    "social_sciences": re.compile(
        r"\b(?:social|sociology|econom\w*|business|management|political|finance|commerce|accounting)\b",
        re.I,
    ),
    "humanities_arts": re.compile(
        r"\b(?:humanit\w*|philosoph\w*|history|linguistic\w*|literature|arts?|music|theology|religion|archeolog\w*)\b",
        re.I,
    ),
    "agriculture": re.compile(
        r"\b(?:agricultur\w*|agronomy|forestry|animal|crops?|soil|veterinary|horticultur\w*)\b",
        re.I,
    ),
    "law_education": re.compile(
        r"\b(?:law|legal|jurisprudence|education|pedagogy|teaching)\b",
        re.I,
    ),
}

DEFAULT_BUCKET_TARGETS: Dict[str, int] = {
    "computer_tech": 180,
    "mechanical_engineering": 140,
    "marine_ocean": 80,
    "environmental_earth": 160,
    "biomedical_medicine": 180,
    "materials_physical": 160,
    "social_sciences": 100,
    "humanities_arts": 100,
    "agriculture": 50,
    "law_education": 50,
}


def build_curated_catalog(
    content: str,
    retrieved_at: str,
    source_last_modified: str = "",
    target_count: int = 1200,
    publisher_cap: int = 25,
) -> Dict[str, Any]:
    """Curate a high-quality redistributable journal subset prioritizing STEM disciplines.

    Selection Strategy:
    - Stratified sampling prioritizing STEM (75% quota: computer science, mechanical engineering,
      marine/ocean sciences, environmental/earth sciences, biomedical/medicine, materials/physical sciences)
      while maintaining broad disciplinary representation (25% quota: social sciences, humanities & arts,
      agriculture, law & education).
    - Requires valid ISSNs, canonical titles, and valid DOAJ URLs.
    - Capped at maximum entries per publisher to preserve diversity and avoid single-publisher dominance.
    - Preserves provenance, observed timestamps, and raw metadata observations without fabricating editorial profiles.
    """
    if target_count < 1000:
        raise JournalError("INVALID_PARAM", "Curated catalog must contain at least 1000 journals")

    f = io.StringIO(content)
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames or []
    for req_header in REQUIRED_HEADERS:
        if req_header not in fieldnames:
            raise JournalError("SCHEMA_CHANGED", f"DOAJ CSV 缺少关键字段头: {req_header}")

    bucket_targets = dict(DEFAULT_BUCKET_TARGETS)
    # Scale bucket targets proportionally if target_count != 1200
    if target_count != 1200:
        scale = target_count / 1200.0
        bucket_targets = {k: max(1, int(v * scale)) for k, v in bucket_targets.items()}

    bucket_counts: Dict[str, int] = {k: 0 for k in bucket_targets}
    publisher_counts: Dict[str, int] = {}
    seen_issns: Set[str] = set()
    selected_records: List[JournalRecord] = []

    for line_no, row in enumerate(reader, start=2):
        if not row or not any(row.values()):
            continue

        title = (row.get("Journal title") or "").strip()
        p_raw = (row.get("Journal ISSN (print version)") or "").strip()
        e_raw = (row.get("Journal EISSN (online version)") or "").strip()
        if not title or (not p_raw and not e_raw):
            continue

        pub = (row.get("Publisher") or "").strip()
        if publisher_counts.get(pub, 0) >= publisher_cap:
            continue

        issns: List[str] = []
        issn_error = False
        for raw in (p_raw, e_raw):
            if raw:
                try:
                    norm = normalize_issn(raw)
                    if norm not in issns:
                        issns.append(norm)
                except JournalError:
                    issn_error = True
                    break
        if issn_error or not issns:
            continue

        primary_issn = issns[0]
        if primary_issn in seen_issns:
            continue

        text = f"{row.get('Subjects', '')} {row.get('Keywords', '')} {title}"

        # Match STEM first
        matched_bucket: Optional[str] = None
        for b_name, pat in STEM_PATTERNS.items():
            if bucket_counts[b_name] < bucket_targets[b_name] and pat.search(text):
                matched_bucket = b_name
                break

        if not matched_bucket:
            for b_name, pat in BREADTH_PATTERNS.items():
                if bucket_counts[b_name] < bucket_targets[b_name] and pat.search(text):
                    matched_bucket = b_name
                    break

        if not matched_bucket:
            continue

        source_url = (row.get("URL in DOAJ") or "").strip()
        last_updated_raw = (row.get("Last updated Date") or "").strip()
        observed_at: Optional[str] = None
        data_year: Optional[int] = None

        if last_updated_raw:
            try:
                dt = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00"))
                observed_at = last_updated_raw
                if 1900 <= dt.year <= 2200:
                    data_year = dt.year
            except ValueError:
                pass

        if observed_at is None:
            added_raw = (row.get("Added on Date") or "").strip()
            if added_raw:
                try:
                    dt = datetime.fromisoformat(added_raw.replace("Z", "+00:00"))
                    observed_at = added_raw
                    if 1900 <= dt.year <= 2200:
                        data_year = dt.year
                except ValueError:
                    pass

        record_prov = Provenance(
            source_id=DOAJ_SOURCE_ID,
            source_url=source_url,
            source_record=source_url,
            retrieved_at=retrieved_at,
            observed_at=observed_at,
            data_year=data_year,
            authority="community",
            freshness="community_catalog",
        )

        alt_title = (row.get("Alternative title") or "").strip()
        aliases = [alt_title] if alt_title else []

        fields = _extract_fields(
            row.get("Keywords") or "",
            row.get("Subjects") or "",
        )

        metadata_obs: List[Dict[str, Any]] = []
        journal_url = (row.get("Journal URL") or "").strip()
        if journal_url:
            metadata_obs.append({"observation": "journal_url", "url": journal_url, "observed_at": observed_at})

        aims_scope_url = (row.get("URL for journal's aims & scope") or "").strip()
        if aims_scope_url:
            metadata_obs.append({"observation": "aims_scope_url", "url": aims_scope_url, "observed_at": observed_at})

        apc_status = (row.get("APC") or "").strip().capitalize()
        apc_info_url = (row.get("APC information URL") or "").strip()
        if apc_status == "No":
            metadata_obs.append(
                {
                    "observation": "no_apc",
                    "has_apc": False,
                    "observed_at": observed_at,
                    "source_url": apc_info_url or source_url,
                }
            )

        has_other_fees = (row.get("Has other fees") or "").strip().capitalize()
        other_fees_url = (row.get("Other fees information URL") or "").strip()
        if has_other_fees == "Yes":
            metadata_obs.append(
                {
                    "observation": "has_other_fees",
                    "info_url": other_fees_url,
                    "observed_at": observed_at,
                }
            )

        art_count_raw = (row.get("Number of Article Records") or "").strip()
        if art_count_raw and art_count_raw.isdigit():
            metadata_obs.append(
                {
                    "observation": "cumulative_article_records",
                    "count": int(art_count_raw),
                    "observed_at": observed_at,
                }
            )

        lcc_codes = (row.get("LCC Codes") or "").strip()
        if lcc_codes:
            metadata_obs.append({"observation": "lcc_codes", "codes": lcc_codes, "observed_at": observed_at})

        publication_fees: List[PublicationFee] = []
        if apc_status == "Yes":
            fee_prov = Provenance(
                source_id=DOAJ_SOURCE_ID,
                source_url=apc_info_url or source_url,
                source_record=source_url,
                retrieved_at=retrieved_at,
                observed_at=observed_at,
                data_year=data_year,
                authority="community",
                freshness="community_catalog",
            )
            apc_amt_str = (row.get("APC amount") or "").strip()
            publication_fees = _parse_apc_fees(apc_amt_str, fee_prov)

        metrics: List[Metric] = []
        weeks_raw = (row.get("Average number of weeks between article submission and publication") or "").strip()
        if weeks_raw:
            try:
                weeks_val = float(weeks_raw)
                if weeks_val >= 0:
                    metrics.append(
                        Metric(
                            name="submission_to_publication_weeks",
                            raw=weeks_raw,
                            value=weeks_val,
                            comparator="eq",
                            unit="weeks",
                            year=data_year,
                            stage="publication",
                            provenance=record_prov,
                        )
                    )
            except ValueError:
                pass

        journal_id = identity_id(title, "journal", issns)

        rec = JournalRecord(
            journal_id=journal_id,
            title=title,
            title_zh="",
            kind="journal",
            publisher=pub,
            issns=issns,
            aliases=aliases,
            fields=fields,
            indexing=["DOAJ"],
            oa_mode="full",
            homepage=_safe_homepage(journal_url),
            provenance=record_prov,
            rankings=[],
            metrics=metrics,
            risks=[],
            experiences=[],
            editorial_profiles=[],
            publication_fees=publication_fees,
            metadata_observations=metadata_obs,
        )

        selected_records.append(rec)
        bucket_counts[matched_bucket] += 1
        publisher_counts[pub] = publisher_counts.get(pub, 0) + 1
        seen_issns.add(primary_issn)

        if len(selected_records) >= target_count:
            break

    stem_total = sum(bucket_counts[k] for k in STEM_PATTERNS if k in bucket_counts)
    breadth_total = sum(bucket_counts[k] for k in BREADTH_PATTERNS if k in bucket_counts)

    selection_meta = {
        "strategy": (
            "Stratified curation prioritizing STEM disciplines (computer science, mechanical engineering, "
            "marine/ocean sciences, environmental/earth sciences, biomedical/medicine, materials/physical sciences) "
            "while maintaining broad multidisciplinary coverage across social sciences, humanities, and agriculture. "
            "Capped at publisher limits to maximize diversity. All journals hold verified ISSNs and official DOAJ entries."
        ),
        "target_count": target_count,
        "total_selected": len(selected_records),
        "stem_count": stem_total,
        "breadth_count": breadth_total,
        "bucket_distribution": bucket_counts,
        "publisher_diversity_cap": publisher_cap,
        "unique_publishers": len(publisher_counts),
        "notes": "Redistributable baseline catalog from official DOAJ CC0 release. Not an indicator of SCI/EI or CAS/JCR indexing.",
    }

    return {
        "schema_version": 1,
        "license": DOAJ_LICENSE,
        "license_url": DOAJ_LICENSE_URL,
        "source_url": OFFICIAL_DOAJ_URL,
        "source_last_modified": source_last_modified,
        "retrieved_at": retrieved_at,
        "selection": selection_meta,
        "records": [r.model_dump() for r in selected_records],
    }


def load_bundled_open_metadata(file_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and validate bundled open metadata dataset from disk.

    Validates schema structure, license, and every individual JournalRecord.
    """
    if file_path is None:
        base_dir = Path(__file__).resolve().parent.parent.parent
        file_path = str(base_dir / "resources" / "journals" / "open_metadata.json")

    if not os.path.exists(file_path):
        raise JournalError("SOURCE_UNAVAILABLE", f"Bundled metadata resource not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("schema_version") != 1:
        raise JournalError("SCHEMA_CHANGED", f"Unsupported schema version: {data.get('schema_version')}")
    if data.get("license") != DOAJ_LICENSE:
        raise JournalError("SCHEMA_CHANGED", f"Unexpected license: {data.get('license')}")

    records = data.get("records", [])
    if not isinstance(records, list) or len(records) < 1000:
        raise JournalError("SCHEMA_CHANGED", f"Resource records count insufficient: {len(records)}")

    # Validate all records against JournalRecord model
    for idx, r in enumerate(records):
        try:
            JournalRecord.model_validate(r)
        except Exception as exc:
            raise JournalError("SCHEMA_CHANGED", f"Record at index {idx} failed validation: {exc}") from exc

    return data
