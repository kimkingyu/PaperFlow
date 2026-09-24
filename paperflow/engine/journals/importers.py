"""Validated, source-attributed importers and parsers for journal datasets.

Supported source IDs:
- showjcr: CSV files (FQBJCR, JCR, GJQKYJMD, XR, CCF, CCFT) or SQLite database (jcr.db)
- xr_journal_query: XR static HTML or JSON extracts containing `const D = [...]` and `const CN = {...}`
- scipythonspider: Historical submission duration and acceptance CSVs
- easyscholar_legacy: Static exported JSON from legacy easyScholar extension
- zotero_updateifs: JSON/CSV mapping ISSN to metrics/IF
- cs_experiences: Computer science markdown/HTML/bullet submission experiences
- security_experiences: Security domain experiences
- fault_experiences: Fault diagnosis domain experiences
- medical_experiences: Medical/clinical domain experiences
- local: Generic JSON/CSV datasets, records, or models
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from paperflow.engine.journals.models import (
    Experience,
    JournalError,
    JournalRecord,
    Metric,
    ParsedBatch,
    Provenance,
    Ranking,
    RiskEvent,
)

MAX_FILE_SIZE_BYTES = 32 * 1024 * 1024  # 32 MiB
MAX_RECORDS_LIMIT = 100000
MAX_REJECTED_SAMPLES = 20

WHITELIST_TABLE_PATTERNS = [
    r"^FQBJCR\d*$",
    r"^JCR\d*$",
    r"^GJQKYJMD\d*$",
    r"^XR\d*$",
    r"^CCF\d*$",
    r"^CCFT\d*$",
    r"^journals$",
]


def _check_file_safety(file_path: str) -> None:
    if not os.path.exists(file_path):
        raise JournalError("SOURCE_UNAVAILABLE", f"File does not exist: {file_path}")
    size = os.path.getsize(file_path)
    if size > MAX_FILE_SIZE_BYTES:
        raise JournalError("SCHEMA_CHANGED", f"File exceeds maximum size of 32 MiB: {size} bytes")


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in {"null", "none", "nan", "-"}:
        return ""
    return s


def _clean_issns(raw_val: Any) -> List[str]:
    """Split concatenated ISSN/EISSN values by slash, comma, semicolon, space and validate 8-char pattern."""
    if not raw_val:
        return []
    s = str(raw_val).strip()
    parts = re.split(r"[/,;，；\s]+", s)
    res: List[str] = []
    for p in parts:
        cleaned = re.sub(r"[^\dX]", "", p.upper())
        if len(cleaned) == 8:
            formatted = f"{cleaned[:4]}-{cleaned[4:]}"
            if formatted not in res:
                res.append(formatted)
    return res


def _find_col(headers: List[str], candidates: List[str]) -> Optional[int]:
    """Find column index: exact match first across all candidates, then controlled fallback."""
    # 1. Exact match pass
    for cand in candidates:
        cand_l = cand.lower()
        for idx, h in enumerate(headers):
            if h.strip().lower() == cand_l:
                return idx
    # 2. Controlled substring match pass
    for cand in candidates:
        cand_l = cand.lower()
        for idx, h in enumerate(headers):
            h_l = h.strip().lower()
            if cand_l in h_l:
                return idx
    return None


def _find_cols_matching(headers: List[str], pattern: str) -> List[Tuple[int, str]]:
    """Find all column indices and header names matching a regex pattern."""
    res = []
    for idx, h in enumerate(headers):
        if re.search(pattern, h, re.IGNORECASE):
            res.append((idx, h.strip()))
    return res


def _extract_year_from_filename(filename: str) -> Optional[int]:
    base = os.path.splitext(os.path.basename(filename))[0]
    # Check known prefixes like XR2026, FQBJCR2025, JCR2024, CCF2026, CCFT2025, GJQKYJMD2024
    m_known = re.search(r"(?:XR|FQBJCR|JCR|CCFT?|GJQKYJMD|WARNING)[_-]?((?:19|20)\d{2})", base, re.IGNORECASE)
    if m_known:
        return int(m_known.group(1))
    # Standalone 4-digit year bounded by non-digits
    years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", base)
    if len(years) == 1:
        return int(years[0])
    return None


def _extract_row_year(headers: List[str], row: List[str], expected_year: Optional[int]) -> Optional[int]:
    year_idx = _find_col(headers, ["年份", "年度", "Year", "data_year"])
    if year_idx is not None and year_idx < len(row):
        val = _clean_str(row[year_idx])
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", val)
        if m:
            ry = int(m.group(1))
            if expected_year is not None and ry != expected_year:
                raise JournalError("SCHEMA_CHANGED", f"Row year conflict: row has {ry}, expected {expected_year}")
            return ry
    return expected_year


def _is_html_error_page(content: str) -> bool:
    """Detect if HTML is an actual error page rather than data-bearing HTML with CSS classes."""
    lower = content[:3000].lower()
    if "<html" in lower or "<!doctype html" in lower:
        # If it has data array like 'const D' or table tags with tabular content, it's not an error page
        if "const d" in lower or "<table" in lower or "<tr" in lower:
            return False
        error_titles = ["<title>404", "<title>500", "<title>error", "<title>access denied", "<title>forbidden"]
        if any(et in lower for et in error_titles):
            return True
        error_bodies = ["404 not found", "500 internal server error", "access denied", "page not found"]
        if any(eb in lower for eb in error_bodies):
            return True
    return False


def _safe_add_rejected(rejected: List[Dict[str, Any]], line_no: int, code: str, field: str = "") -> None:
    if len(rejected) < MAX_REJECTED_SAMPLES:
        rejected.append({
            "line": line_no,
            "error_code": code,
            "field": field,
        })


def parse_file(
    source_id: str,
    file_path: str,
    kind: str = "auto",
    data_year: Optional[int] = None,
    encoding: str = "utf-8-sig",
    source_version: str = "",
) -> ParsedBatch:
    """Parse a journal dataset file into a standard ParsedBatch."""
    _check_file_safety(file_path)
    filename = os.path.basename(file_path)
    file_year = _extract_year_from_filename(filename)
    detected_year = data_year if data_year is not None else file_year

    if source_id == "showjcr":
        if file_path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            return _parse_showjcr_sqlite(file_path, data_year, source_version)
        return _parse_showjcr_csv(file_path, kind, detected_year, encoding, source_version)
    elif source_id == "xr_journal_query":
        return _parse_xr_journal_query(file_path, detected_year, encoding, source_version)
    elif source_id == "scipythonspider":
        return _parse_scipythonspider_csv(file_path, detected_year, encoding, source_version)
    elif source_id in {"cs_experiences", "security_experiences", "fault_experiences", "medical_experiences"}:
        return _parse_domain_experiences(source_id, file_path, detected_year, encoding, source_version)
    elif source_id == "easyscholar_legacy":
        return _parse_easyscholar_legacy(file_path, detected_year, encoding, source_version)
    elif source_id == "zotero_updateifs":
        return _parse_zotero_updateifs(file_path, detected_year, encoding, source_version)
    elif source_id == "local":
        if kind == "records" or file_path.endswith(".json"):
            return _parse_records_json(source_id, file_path, detected_year, encoding, source_version)
        elif file_path.endswith(".csv"):
            return _parse_generic_csv(source_id, file_path, detected_year, encoding, source_version)
        else:
            return _parse_records_json(source_id, file_path, detected_year, encoding, source_version)
    else:
        if file_path.endswith(".csv"):
            return _parse_generic_csv(source_id, file_path, detected_year, encoding, source_version)
        return _parse_records_json(source_id, file_path, detected_year, encoding, source_version)


# ---------------------------------------------------------------------------
# ShowJCR CSV Routing & Parsers
# ---------------------------------------------------------------------------

def _parse_showjcr_csv(
    file_path: str,
    kind: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        head_preview = f.read(2048)
        if not head_preview.strip():
            raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
        if _is_html_error_page(head_preview):
            raise JournalError("SCHEMA_CHANGED", f"HTML error page detected in {file_path}")
        f.seek(0)
        reader = csv.reader(f)
        try:
            raw_headers = next(reader, None)
        except Exception as e:
            raise JournalError("SCHEMA_CHANGED", "Failed reading CSV header")

    if not raw_headers:
        raise JournalError("SCHEMA_CHANGED", f"Empty CSV headers: {file_path}")

    headers = [h.strip() for h in raw_headers]
    fname_upper = os.path.basename(file_path).upper()

    # 1. Respect explicit kind parameter first
    kind_lower = kind.lower().strip()
    if kind_lower in {"cas", "fqbjcr"}:
        return _parse_showjcr_cas(file_path, headers, data_year, encoding, source_version)
    elif kind_lower in {"jcr"}:
        return _parse_showjcr_jcr(file_path, headers, data_year, encoding, source_version)
    elif kind_lower in {"xr"}:
        return _parse_showjcr_xr(file_path, headers, data_year, encoding, source_version)
    elif kind_lower in {"ccf"}:
        return _parse_showjcr_ccf(file_path, headers, data_year, encoding, source_version)
    elif kind_lower in {"ccft"}:
        return _parse_showjcr_ccft(file_path, headers, data_year, encoding, source_version)
    elif kind_lower in {"warning", "cas_warning"}:
        return _parse_showjcr_warning(file_path, headers, data_year, encoding, source_version)

    # 2. Known filename prefix priority (prevent XR2026 with '预警标记' being hijacked by warning)
    if "XR" in fname_upper:
        return _parse_showjcr_xr(file_path, headers, data_year, encoding, source_version)
    if "CCFT" in fname_upper:
        return _parse_showjcr_ccft(file_path, headers, data_year, encoding, source_version)
    if "CCF" in fname_upper:
        return _parse_showjcr_ccf(file_path, headers, data_year, encoding, source_version)
    if "FQBJCR" in fname_upper or "FQB" in fname_upper:
        return _parse_showjcr_cas(file_path, headers, data_year, encoding, source_version)
    if "JCR" in fname_upper:
        return _parse_showjcr_jcr(file_path, headers, data_year, encoding, source_version)
    if any(k in fname_upper for k in ["GJQKYJMD", "YJMD", "WARNING"]):
        return _parse_showjcr_warning(file_path, headers, data_year, encoding, source_version)

    # 3. Header inspection fallback
    if any(h in headers for h in ["T分区", "CCFT等级"]):
        return _parse_showjcr_ccft(file_path, headers, data_year, encoding, source_version)
    if any("CCF推荐" in h for h in headers):
        return _parse_showjcr_ccf(file_path, headers, data_year, encoding, source_version)
    if any("新锐分区" in h for h in headers) or "大类2" in "".join(headers):
        return _parse_showjcr_xr(file_path, headers, data_year, encoding, source_version)
    if any("大类" in h for h in headers) and any("小类" in h for h in headers):
        return _parse_showjcr_cas(file_path, headers, data_year, encoding, source_version)
    if any("预警" in h or "预警原因" in h or "预警等级" in h for h in headers):
        return _parse_showjcr_warning(file_path, headers, data_year, encoding, source_version)
    if any("IF" in h or "影响因子" in h or "Quartile" in h for h in headers):
        return _parse_showjcr_jcr(file_path, headers, data_year, encoding, source_version)

    return _parse_showjcr_cas(file_path, headers, data_year, encoding, source_version)


def _parse_showjcr_cas(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title column in CAS/FQBJCR: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    eissn_idx = _find_col(headers, ["EISSN", "eissn", "e-issn"])
    combined_issn_idx = _find_col(headers, ["ISSN/EISSN", "ISSN / EISSN"])

    major_idx = _find_col(headers, ["大类名称", "大类", "Major"])
    major_q_idx = _find_col(headers, ["大类分区", "大类等级", "大类Q"])
    top_idx = _find_col(headers, ["Top", "TOP", "是否Top", "top"])

    # Small categories 1..N
    minor_cols: List[Tuple[int, int]] = []  # (name_idx, q_idx)
    # Check numbered minor categories like 小类1名称, 小类1分区 or 小类名称, 小类分区
    for i in range(1, 10):
        m_name = _find_col(headers, [f"小类{i}名称", f"小类{i}", f"Minor{i}"])
        m_q = _find_col(headers, [f"小类{i}分区", f"小类{i}等级", f"小类{i}Q"])
        if m_name is not None:
            minor_cols.append((m_name, m_q if m_q is not None else -1))
    if not minor_cols:
        single_minor_name = _find_col(headers, ["小类名称", "小类", "Minor"])
        single_minor_q = _find_col(headers, ["小类分区", "小类等级", "小类Q"])
        if single_minor_name is not None:
            minor_cols.append((single_minor_name, single_minor_q if single_minor_q is not None else -1))

    # Core value check: must have at least major or minor quartile/grade
    if major_q_idx is None and not any(q >= 0 for _, q in minor_cols):
        raise JournalError("SCHEMA_CHANGED", "Missing critical quartile columns in CAS/FQBJCR")

    # Indexing: Web of Science, SCIE, ESCI
    wos_idx = _find_col(headers, ["Web of Science", "收录", "Index", "SCIE", "ESCI", "SCI"])
    # Open Access: exact match candidates to avoid matching "OAJ"
    oa_idx = _find_col(headers, ["Open Access", "是否OA", "OA", "OpenAccess"])

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            # Year extraction & validation
            row_year = _extract_row_year(headers, row, data_year)

            # ISSN collection & splitting
            issns: List[str] = []
            if combined_issn_idx is not None and combined_issn_idx < len(row):
                issns.extend(_clean_issns(row[combined_issn_idx]))
            if issn_idx is not None and issn_idx < len(row):
                for val in _clean_issns(row[issn_idx]):
                    if val not in issns:
                        issns.append(val)
            if eissn_idx is not None and eissn_idx < len(row):
                for val in _clean_issns(row[eissn_idx]):
                    if val not in issns:
                        issns.append(val)

            # Indexing
            indexing: List[str] = []
            if wos_idx is not None and wos_idx < len(row):
                wos_val = _clean_str(row[wos_idx])
                if wos_val:
                    for part in re.split(r"[,;/ ]+", wos_val):
                        part_u = part.upper()
                        if part_u in {"SCIE", "SCI", "ESCI", "SSCI", "AHCI", "WOS"}:
                            indexing.append(part_u)

            # OA Mode
            oa_mode: Literal["full", "hybrid", "closed", "diamond", "unknown"] = "unknown"
            if oa_idx is not None and oa_idx < len(row):
                oa_val = _clean_str(row[oa_idx]).lower()
                if oa_val in {"yes", "true", "y", "是", "oa", "open access"}:
                    oa_mode = "full"
                elif oa_val in {"no", "false", "n", "否", "closed"}:
                    oa_mode = "closed"

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            is_top: Optional[bool] = None
            if top_idx is not None and top_idx < len(row):
                top_val = _clean_str(row[top_idx]).lower()
                if top_val in {"yes", "true", "y", "1", "是", "top"}:
                    is_top = True
                elif top_val in {"no", "false", "n", "0", "否"}:
                    is_top = False

            rankings: List[Ranking] = []
            # Major category
            if major_idx is not None and major_idx < len(row):
                maj_name = _clean_str(row[major_idx])
                maj_q_str = _clean_str(row[major_q_idx]) if major_q_idx is not None and major_q_idx < len(row) else ""
                q_num = None
                m_q = re.search(r"([1-4])", maj_q_str)
                if m_q:
                    q_num = int(m_q.group(1))
                if maj_name or q_num:
                    rankings.append(
                        Ranking(
                            system="cas",
                            year=row_year,
                            category=maj_name,
                            category_type="major",
                            quartile=q_num,
                            grade=maj_q_str,
                            top=is_top,
                            raw=maj_q_str,
                            provenance=prov,
                        )
                    )

            # Minor categories
            for m_n_idx, m_q_idx in minor_cols:
                if m_n_idx < len(row):
                    min_name = _clean_str(row[m_n_idx])
                    min_q_str = _clean_str(row[m_q_idx]) if m_q_idx >= 0 and m_q_idx < len(row) else ""
                    q_num = None
                    m_q = re.search(r"([1-4])", min_q_str)
                    if m_q:
                        q_num = int(m_q.group(1))
                    if min_name or q_num:
                        rankings.append(
                            Ranking(
                                system="cas",
                                year=row_year,
                                category=min_name,
                                category_type="minor",
                                quartile=q_num,
                                grade=min_q_str,
                                top=is_top,
                                raw=min_q_str,
                                provenance=prov,
                            )
                        )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    indexing=indexing,
                    oa_mode=oa_mode,
                    provenance=prov,
                    rankings=rankings,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid CAS/FQBJCR records parsed from file")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "cas",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_showjcr_jcr(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title column in JCR: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    eissn_idx = _find_col(headers, ["EISSN", "eissn"])
    combined_issn_idx = _find_col(headers, ["ISSN/EISSN", "ISSN / EISSN"])

    if_idx = _find_col(headers, ["影响因子", "IF", "Impact Factor", "if", "Journal Impact Factor"])

    # Find category and quartile pairs: Category_1, IF Quartile(2025)_1, Category_2...
    cat_q_pairs: List[Tuple[int, int]] = []
    for i in range(1, 10):
        c_idx = _find_col(headers, [f"Category_{i}", f"Category{i}", f"学科{i}", f"类别{i}"])
        q_idx = _find_col(headers, [
            f"IF Quartile({data_year})_{i}" if data_year else f"IF Quartile_{i}",
            f"Quartile_{i}",
            f"Quartile{i}",
            f"分区{i}",
            f"Q_{i}",
            f"IF Quartile_{i}",
        ])
        if c_idx is not None or q_idx is not None:
            cat_q_pairs.append((c_idx if c_idx is not None else -1, q_idx if q_idx is not None else -1))

    if not cat_q_pairs:
        single_cat = _find_col(headers, ["学科", "Category", "类别", "分类"])
        single_q = _find_col(headers, ["分区", "Quartile", "JCR分区", "Q"])
        if single_cat is not None or single_q is not None:
            cat_q_pairs.append((single_cat if single_cat is not None else -1, single_q if single_q is not None else -1))

    # Core value check
    if if_idx is None and not any(q >= 0 for _, q in cat_q_pairs):
        raise JournalError("SCHEMA_CHANGED", "Missing critical IF or Quartile columns in JCR")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)

            issns: List[str] = []
            if combined_issn_idx is not None and combined_issn_idx < len(row):
                issns.extend(_clean_issns(row[combined_issn_idx]))
            if issn_idx is not None and issn_idx < len(row):
                for v in _clean_issns(row[issn_idx]):
                    if v not in issns:
                        issns.append(v)
            if eissn_idx is not None and eissn_idx < len(row):
                for v in _clean_issns(row[eissn_idx]):
                    if v not in issns:
                        issns.append(v)

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            rankings: List[Ranking] = []
            metrics: List[Metric] = []

            for c_i, q_i in cat_q_pairs:
                cat_name = _clean_str(row[c_i]) if c_i >= 0 and c_i < len(row) else ""
                q_str = _clean_str(row[q_i]) if q_i >= 0 and q_i < len(row) else ""
                q_num = None
                m_q = re.search(r"Q?([1-4])", q_str, re.IGNORECASE)
                if m_q:
                    q_num = int(m_q.group(1))
                if cat_name or q_num:
                    rankings.append(
                        Ranking(
                            system="jcr",
                            year=row_year,
                            category=cat_name,
                            category_type="subject",
                            quartile=q_num,
                            grade=q_str,
                            raw=q_str,
                            provenance=prov,
                        )
                    )

            if if_idx is not None and if_idx < len(row):
                if_val_str = _clean_str(row[if_idx])
                if if_val_str:
                    comparator: Literal["eq", "lt", "le", "gt", "ge", "range", "unknown"] = "eq"
                    val = None
                    if if_val_str.startswith("<"):
                        comparator = "lt"
                        num_part = re.sub(r"[^\d.]", "", if_val_str)
                        val = float(num_part) if num_part else None
                    elif if_val_str.startswith(">"):
                        comparator = "gt"
                        num_part = re.sub(r"[^\d.]", "", if_val_str)
                        val = float(num_part) if num_part else None
                    else:
                        try:
                            val = float(if_val_str)
                        except ValueError:
                            comparator = "unknown"
                    metrics.append(
                        Metric(
                            name="impact_factor",
                            raw=if_val_str,
                            value=val,
                            comparator=comparator,
                            year=row_year,
                            provenance=prov,
                        )
                    )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                    rankings=rankings,
                    metrics=metrics,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid JCR records parsed from file")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "jcr",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_showjcr_warning(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title column in warning list: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    level_idx = _find_col(headers, ["等级", "预警等级", "预警级别", "Level", "level"])
    reason_idx = _find_col(headers, ["原因", "预警原因", "Reason", "reason", "说明"])

    if level_idx is None and reason_idx is None:
        raise JournalError("SCHEMA_CHANGED", "Missing critical level or reason columns in warning list")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)

            issns: List[str] = []
            if issn_idx is not None and issn_idx < len(row):
                issns = _clean_issns(row[issn_idx])

            level = _clean_str(row[level_idx]) if level_idx is not None and level_idx < len(row) else ""
            reason = _clean_str(row[reason_idx]) if reason_idx is not None and reason_idx < len(row) else ""

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            risk = RiskEvent(
                system="cas_warning",
                value="flagged",
                year=row_year,
                level=level,
                reason=reason,
                historical=False,
                provenance=prov,
            )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                    risks=[risk],
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid warning records parsed from file")

    # If even a single row was rejected in a warning list, complete must be false
    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "cas_warning",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_showjcr_xr(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title in XR: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    combined_issn_idx = _find_col(headers, ["ISSN/EISSN", "ISSN / EISSN"])

    # Real XR headers: 大类中文名, 大类英文名, 大类新锐分区, Top, 大类2中文名, 大类2新锐分区, 小类1..N
    maj1_cn = _find_col(headers, ["大类中文名", "第一大类", "大类1中文名", "大类1"])
    maj1_en = _find_col(headers, ["大类英文名", "大类1英文名"])
    maj1_tier = _find_col(headers, ["大类新锐分区", "大类1新锐分区", "大类分区", "第一大类分区"])
    top_idx = _find_col(headers, ["Top", "TOP", "是否Top", "大类Top"])

    maj2_cn = _find_col(headers, ["大类2中文名", "第二大类", "大类2", "大类2名称"])
    maj2_tier = _find_col(headers, ["大类2新锐分区", "第二大类分区", "大类2分区"])

    # Minor categories
    minor_cols: List[Tuple[int, int]] = []
    for i in range(1, 10):
        mc = _find_col(headers, [f"小类{i}中文名", f"小类{i}名称", f"小类{i}"])
        mq = _find_col(headers, [f"小类{i}新锐分区", f"小类{i}分区", f"小类{i}等级"])
        if mc is not None:
            minor_cols.append((mc, mq if mq is not None else -1))

    # Review flag column: Under Review or 预警标记
    review_idx = _find_col(headers, ["预警标记", "Under Review", "UnderReview", "在审", "核查"])

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)

            issns: List[str] = []
            if combined_issn_idx is not None and combined_issn_idx < len(row):
                issns.extend(_clean_issns(row[combined_issn_idx]))
            if issn_idx is not None and issn_idx < len(row):
                for v in _clean_issns(row[issn_idx]):
                    if v not in issns:
                        issns.append(v)

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            is_top: Optional[bool] = None
            if top_idx is not None and top_idx < len(row):
                top_val = _clean_str(row[top_idx]).lower()
                if top_val in {"yes", "true", "y", "1", "是", "top"}:
                    is_top = True
                elif top_val in {"no", "false", "n", "0", "否"}:
                    is_top = False

            rankings: List[Ranking] = []
            risks: List[RiskEvent] = []

            # Major 1
            m1_name = _clean_str(row[maj1_cn]) if maj1_cn is not None and maj1_cn < len(row) else ""
            if not m1_name and maj1_en is not None and maj1_en < len(row):
                m1_name = _clean_str(row[maj1_en])
            m1_t_str = _clean_str(row[maj1_tier]) if maj1_tier is not None and maj1_tier < len(row) else ""
            q_num = None
            m_q = re.search(r"([1-4])", m1_t_str)
            if m_q:
                q_num = int(m_q.group(1))
            if m1_name or q_num:
                rankings.append(
                    Ranking(
                        system="xr",
                        year=row_year,
                        category=m1_name,
                        category_type="major",
                        quartile=q_num,
                        grade=m1_t_str,
                        top=is_top,
                        raw=m1_t_str,
                        provenance=prov,
                    )
                )

            # Major 2
            if maj2_cn is not None and maj2_cn < len(row):
                m2_name = _clean_str(row[maj2_cn])
                m2_t_str = _clean_str(row[maj2_tier]) if maj2_tier is not None and maj2_tier < len(row) else ""
                q2_num = None
                m2_q = re.search(r"([1-4])", m2_t_str)
                if m2_q:
                    q2_num = int(m2_q.group(1))
                if m2_name or q2_num:
                    rankings.append(
                        Ranking(
                            system="xr",
                            year=row_year,
                            category=m2_name,
                            category_type="major",
                            quartile=q2_num,
                            grade=m2_t_str,
                            raw=m2_t_str,
                            provenance=prov,
                        )
                    )

            # Minor categories
            for mc_idx, mq_idx in minor_cols:
                if mc_idx < len(row):
                    min_name = _clean_str(row[mc_idx])
                    min_q_str = _clean_str(row[mq_idx]) if mq_idx >= 0 and mq_idx < len(row) else ""
                    q_num = None
                    m_q = re.search(r"([1-4])", min_q_str)
                    if m_q:
                        q_num = int(m_q.group(1))
                    if min_name or q_num:
                        rankings.append(
                            Ranking(
                                system="xr",
                                year=row_year,
                                category=min_name,
                                category_type="minor",
                                quartile=q_num,
                                grade=min_q_str,
                                raw=min_q_str,
                                provenance=prov,
                            )
                        )

            # Review risk
            if review_idx is not None and review_idx < len(row):
                rev_val = _clean_str(row[review_idx])
                if rev_val and rev_val.lower() not in {"0", "false", "no", "否", "-"}:
                    risks.append(
                        RiskEvent(
                            system="xr_review",
                            value="under_review",
                            year=row_year,
                            reason=rev_val,
                            provenance=prov,
                        )
                    )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                    rankings=rankings,
                    risks=risks,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid XR records parsed from file")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "xr",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_showjcr_ccf(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical title in CCF: {headers}")

    cat_idx = _find_col(headers, ["方向名称", "方向", "领域", "Category"])
    # Exact match candidate list: "CCF推荐类型" is grade (A/B/C), "CCF推荐类别（国际学术刊物/会议）" is kind
    grade_idx = _find_col(headers, ["CCF推荐类型", "CCF推荐等级", "CCF等级", "等级", "Grade", "CCF"])
    kind_idx = _find_col(headers, ["CCF推荐类别（国际学术刊物/会议）", "CCF推荐类别", "类型", "Kind", "期刊/会议"])
    abbr_idx = _find_col(headers, ["简称", "缩写", "Abbr"])

    if grade_idx is None:
        raise JournalError("SCHEMA_CHANGED", "Missing critical CCF grade column (CCF推荐类型)")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)

            kind: Literal["journal", "conference"] = "journal"
            if kind_idx is not None and kind_idx < len(row):
                kval = _clean_str(row[kind_idx]).lower()
                if "会议" in kval or "conf" in kval:
                    kind = "conference"

            aliases: List[str] = []
            if abbr_idx is not None and abbr_idx < len(row):
                ab = _clean_str(row[abbr_idx])
                if ab:
                    aliases.append(ab)

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            cat = _clean_str(row[cat_idx]) if cat_idx is not None and cat_idx < len(row) else ""
            grade = _clean_str(row[grade_idx]) if grade_idx is not None and grade_idx < len(row) else ""

            rankings: List[Ranking] = []
            if grade:
                rankings.append(
                    Ranking(
                        system="ccf",
                        year=row_year,
                        category=cat,
                        category_type="subject",
                        grade=grade,
                        raw=grade,
                        provenance=prov,
                    )
                )

            records.append(
                JournalRecord(
                    title=title,
                    kind=kind,
                    aliases=aliases,
                    fields=[cat] if cat else [],
                    provenance=prov,
                    rankings=rankings,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid CCF records parsed from file")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "ccf",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_showjcr_ccft(
    file_path: str,
    headers: List[str],
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical title in CCFT: {headers}")

    # Exact match candidates: T分区 is grade (T1/T2/T3)
    grade_idx = _find_col(headers, ["T分区", "CCFT等级", "T类分区", "CCFT分区"])
    cat_idx = _find_col(headers, ["领域", "方向", "Category", "方向名称"])

    if grade_idx is None:
        raise JournalError("SCHEMA_CHANGED", "Missing critical CCFT grade column (T分区)")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)
            grade = _clean_str(row[grade_idx]) if grade_idx is not None and grade_idx < len(row) else ""
            cat = _clean_str(row[cat_idx]) if cat_idx is not None and cat_idx < len(row) else ""

            prov = Provenance(
                source_id="showjcr",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            rankings: List[Ranking] = []
            if grade:
                rankings.append(
                    Ranking(
                        system="ccft",
                        year=row_year,
                        category=cat,
                        category_type="subject",
                        grade=grade,
                        raw=grade,
                        provenance=prov,
                    )
                )

            records.append(
                JournalRecord(
                    title=title,
                    fields=[cat] if cat else [],
                    provenance=prov,
                    rankings=rankings,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid CCFT records parsed from file")

    # If all rankings are empty, complete cannot be true
    has_rankings = any(bool(r.rankings) for r in records)
    complete = (rejected_count == 0 and len(records) > 0 and has_rankings)
    coverage = {
        "system": "rankings",
        "subsystem": "ccft",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# ShowJCR SQLite: jcr.db (Read-only URI, trusted_schema=OFF, unified parse)
# ---------------------------------------------------------------------------

def _parse_showjcr_sqlite(
    file_path: str,
    data_year: Optional[int],
    source_version: str,
) -> ParsedBatch:
    """Read whitelisted static tables and reuse the exact CSV adapters for every schema."""
    path = Path(file_path).resolve()
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise JournalError("SOURCE_UNAVAILABLE", "请先导出静态 SQLite 快照；不读取正在写入的 WAL 数据库")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise JournalError("SOURCE_UNAVAILABLE", "无法以只读模式打开 SQLite 文件") from None
    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    datasets: List[Dict[str, Any]] = []
    warnings: List[str] = []
    total_rows = rejected_count = 0
    try:
        conn.enable_load_extension(False)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        with tempfile.TemporaryDirectory(prefix="paperflow-sqlite-import-") as folder:
            for table, declaration in tables:
                if not any(re.fullmatch(pattern, table, re.IGNORECASE) for pattern in WHITELIST_TABLE_PATTERNS):
                    continue
                if re.search(r"CREATE\s+VIRTUAL\s+TABLE", declaration or "", re.IGNORECASE):
                    raise JournalError("SCHEMA_CHANGED", "期刊导入只接受普通数据表，不执行虚拟表模块")
                # Full-match allowlist excludes quotes and SQL fragments before interpolation.
                headers = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
                remaining = MAX_RECORDS_LIMIT - total_rows
                rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (remaining + 1,)).fetchall()
                if len(rows) > remaining:
                    raise JournalError("INPUT_TOO_LARGE", "SQLite 多表总行数超过上限，未导入部分数据")
                total_rows += len(rows)
                table_year = _extract_year_from_filename(table)
                if data_year is not None and table_year is not None and table_year != data_year:
                    raise JournalError("SCHEMA_CHANGED", "SQLite 表年度与显式 data_year 不一致")
                year = data_year if data_year is not None else table_year
                temporary_csv = Path(folder) / (table + ".csv")
                with temporary_csv.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(rows)
                batch = _parse_showjcr_csv(str(temporary_csv), "auto", year, "utf-8", source_version)
                for record in batch.records:
                    record.provenance = record.provenance.model_copy(update={
                        "source_record": table + ":" + record.provenance.source_record})
                    for group in (record.rankings, record.metrics, record.risks, record.experiences):
                        for fact in group:
                            fact.provenance = fact.provenance.model_copy(update={
                                "source_record": table + ":" + fact.provenance.source_record})
                records.extend(batch.records)
                rejected_count += batch.rejected_count
                for item in batch.rejected:
                    if len(rejected) < MAX_REJECTED_SAMPLES:
                        rejected.append(dict(item, table=table))
                datasets.append(dict(batch.coverage, table=table))
                warnings.extend(batch.warnings)
        if not records:
            raise JournalError("SCHEMA_CHANGED", "SQLite 中没有可导入的白名单数据表记录")
        return ParsedBatch(records=records, rejected=rejected, total_rows=total_rows,
                           rejected_count=rejected_count, warnings=list(dict.fromkeys(warnings)),
                           coverage={"system": "mixed", "year": data_year,
                                     "complete": rejected_count == 0 and all(d.get("complete") is True for d in datasets),
                                     "authority": "community", "datasets": datasets})
    except sqlite3.Error:
        raise JournalError("SCHEMA_CHANGED", "SQLite 数据或表结构不合法，未更新期刊库") from None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# XR Journal Query (Static HTML extract with JSONDecoder.raw_decode)
# ---------------------------------------------------------------------------

def _parse_xr_journal_query(
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        content = f.read()

    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
    if _is_html_error_page(content):
        raise JournalError("SCHEMA_CHANGED", f"HTML error page detected: {file_path}")

    decoder = json.JSONDecoder()
    d_items: Optional[List[Any]] = None
    cn_map: Dict[str, str] = {}

    # Extract CN mapping if present: const CN = {...}
    m_cn = re.search(r"(?:const|var|let)\s+CN\s*=\s*", content)
    if m_cn:
        start_idx = m_cn.end()
        try:
            parsed_cn, _ = decoder.raw_decode(content[start_idx:].lstrip())
            if isinstance(parsed_cn, dict):
                cn_map = {str(k): str(v) for k, v in parsed_cn.items()}
        except Exception:
            pass

    # Extract D items: const D = [...]
    m_d = re.search(r"(?:const|var|let)\s+D\s*=\s*", content)
    if m_d:
        start_idx = m_d.end()
        try:
            parsed_d, _ = decoder.raw_decode(content[start_idx:].lstrip())
            if isinstance(parsed_d, list):
                d_items = parsed_d
        except Exception as e:
            raise JournalError("SCHEMA_CHANGED", f"Failed decoding JSON array from 'const D': {e}")
    else:
        # Try raw JSON
        if content.strip().startswith(("{", "[")):
            try:
                raw_json = json.loads(content)
                if isinstance(raw_json, list):
                    d_items = raw_json
                elif isinstance(raw_json, dict) and "records" in raw_json:
                    d_items = raw_json["records"]
                elif isinstance(raw_json, dict):
                    d_items = [raw_json]
            except Exception:
                pass

    if d_items is None:
        raise JournalError("SCHEMA_CHANGED", f"Could not find valid 'const D = [...]' in XR file: {file_path}")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = len(d_items)
    rejected_count = 0

    for idx, item in enumerate(d_items, start=1):
        if idx > MAX_RECORDS_LIMIT:
            raise JournalError("SCHEMA_CHANGED", f"XR items exceed row limit of {MAX_RECORDS_LIMIT}")

        prov = Provenance(
            source_id="xr_journal_query",
            source_record=str(idx),
            data_year=data_year,
            authority="community",
            observed_at=None,
            source_version=source_version,
        )

        rankings: List[Ranking] = []
        risks: List[RiskEvent] = []

        # 1. Real 2D array format: [catId, title, tier, isTop]
        if isinstance(item, (list, tuple)):
            if len(item) < 2:
                rejected_count += 1
                _safe_add_rejected(rejected, idx, "ROW_TOO_SHORT", "array")
                continue
            cat_id = str(item[0])
            title = _clean_str(item[1])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, idx, "FIELD_EMPTY", "title")
                continue

            tier = _clean_str(item[2]) if len(item) > 2 else ""
            is_top_val = item[3] if len(item) > 3 else None
            is_top: Optional[bool] = None
            if is_top_val is not None:
                if str(is_top_val) in {"1", "true", "True"}:
                    is_top = True
                elif str(is_top_val) in {"0", "false", "False"}:
                    is_top = False

            cat_name = cn_map.get(cat_id, cat_id)
            q_num = None
            m_q = re.search(r"([1-4])", tier)
            if m_q:
                q_num = int(m_q.group(1))

            if cat_name or q_num:
                rankings.append(
                    Ranking(
                        system="xr",
                        year=data_year,
                        category=cat_name,
                        category_type="major",
                        quartile=q_num,
                        grade=tier,
                        top=is_top,
                        raw=tier,
                        provenance=prov,
                    )
                )

            records.append(
                JournalRecord(
                    title=title,
                    issns=[],
                    provenance=prov,
                    rankings=rankings,
                )
            )

        # 2. JSON object format
        elif isinstance(item, dict):
            title = _clean_str(item.get("jName") or item.get("title") or item.get("name") or item.get("journal"))
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, idx, "FIELD_EMPTY", "title")
                continue

            issns: List[str] = []
            for k in ["issn", "eissn", "ISSN", "EISSN"]:
                if k in item and item[k]:
                    for v in _clean_issns(item[k]):
                        if v not in issns:
                            issns.append(v)

            cat1 = _clean_str(item.get("major1") or item.get("major") or item.get("cat1"))
            cat2 = _clean_str(item.get("major2") or item.get("cat2"))
            if cat1:
                rankings.append(Ranking(system="xr", year=data_year, category=cat1, category_type="major", provenance=prov))
            if cat2:
                rankings.append(Ranking(system="xr", year=data_year, category=cat2, category_type="major", provenance=prov))

            under_review = item.get("under_review") or item.get("underReview") or item.get("review")
            if under_review:
                risks.append(
                    RiskEvent(
                        system="xr_review",
                        value="under_review",
                        year=data_year,
                        reason=str(under_review),
                        provenance=prov,
                    )
                )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                    rankings=rankings,
                    risks=risks,
                )
            )
        else:
            rejected_count += 1
            _safe_add_rejected(rejected, idx, "INVALID_TYPE", "item")

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid XR records parsed")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "xr",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# SciPythonSpider Historical CSV
# ---------------------------------------------------------------------------

def _parse_duration_string(text: str) -> Tuple[Optional[float], Optional[float], str, str]:
    text = text.strip()
    if not text:
        return (None, None, "", "unknown")

    m_weeks = re.search(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*(?:周|weeks?)", text)
    if m_weeks:
        w_low, w_high = float(m_weeks.group(1)), float(m_weeks.group(2))
        return (w_low * 7, w_high * 7, "days", "range")

    m_weeks_single = re.search(r"(\d+(?:\.\d+)?)\s*(?:周|weeks?)", text)
    if m_weeks_single:
        w = float(m_weeks_single.group(1))
        return (w * 7, w * 7, "days", "eq")

    m_months = re.search(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*(?:个?月|months?)", text)
    if m_months:
        m_low, m_high = float(m_months.group(1)), float(m_months.group(2))
        return (m_low * 30, m_high * 30, "days", "range")

    m_months_single = re.search(r"(\d+(?:\.\d+)?)\s*(?:个?月|months?)", text)
    if m_months_single:
        m = float(m_months_single.group(1))
        return (m * 30, m * 30, "days", "eq")

    m_gt = re.search(r"[>大于]\s*(\d+(?:\.\d+)?)\s*(?:周|weeks?)", text)
    if m_gt:
        w = float(m_gt.group(1))
        return (w * 7, None, "days", "gt")

    m_range_nounit = re.search(r"^(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)$", text)
    if m_range_nounit:
        return (float(m_range_nounit.group(1)), float(m_range_nounit.group(2)), "", "range")

    return (None, None, "", "unknown")


def _parse_scipythonspider_csv(
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        head_preview = f.read(2048)
        if not head_preview.strip():
            raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
        if _is_html_error_page(head_preview):
            raise JournalError("SCHEMA_CHANGED", f"HTML error page detected in {file_path}")
        f.seek(0)
        reader = csv.reader(f)
        headers = next(reader, None)

    if not headers:
        raise JournalError("SCHEMA_CHANGED", f"Empty CSV headers in {file_path}")

    headers = [h.strip() for h in headers]
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title column in SciPythonSpider: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    cycle_idx = _find_col(headers, ["初审周期", "审稿周期", "周期", "Duration", "cycle"])
    volume_idx = _find_col(headers, ["年发文量", "发文量", "Volume", "annual_volume"])
    diff_idx = _find_col(headers, ["投稿难度", "录取难度", "难度", "Difficulty"])
    ratio_idx = _find_col(headers, ["录用比例", "录用率", "Acceptance Rate"])

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)
            issns = _clean_issns(row[issn_idx]) if issn_idx is not None and issn_idx < len(row) else []

            prov = Provenance(
                source_id="scipythonspider",
                source_record=str(line_no),
                data_year=row_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )

            metrics: List[Metric] = []
            experiences: List[Experience] = []

            if cycle_idx is not None and cycle_idx < len(row):
                cycle_str = _clean_str(row[cycle_idx])
                if cycle_str:
                    lower, upper, unit, comp = _parse_duration_string(cycle_str)
                    metrics.append(
                        Metric(
                            name="review_speed",
                            raw=cycle_str,
                            lower=lower,
                            upper=upper,
                            comparator=comp,
                            unit=unit,
                            year=row_year,
                            stage="unknown",
                            provenance=prov,
                        )
                    )

            if volume_idx is not None and volume_idx < len(row):
                vol_str = _clean_str(row[volume_idx])
                if vol_str:
                    vol_num = None
                    m_num = re.search(r"(\d+)", vol_str)
                    if m_num:
                        vol_num = float(m_num.group(1))
                    metrics.append(
                        Metric(
                            name="annual_volume",
                            raw=vol_str,
                            value=vol_num,
                            comparator="eq" if vol_num is not None else "unknown",
                            year=row_year,
                            provenance=prov,
                        )
                    )

            diff_str = _clean_str(row[diff_idx]) if diff_idx is not None and diff_idx < len(row) else ""
            ratio_str = _clean_str(row[ratio_idx]) if ratio_idx is not None and ratio_idx < len(row) else ""
            if diff_str or ratio_str:
                summary_parts = []
                if diff_str:
                    summary_parts.append(f"难度: {diff_str}")
                if ratio_str:
                    summary_parts.append(f"录用比例: {ratio_str}")
                experiences.append(
                    Experience(
                        field="general",
                        topic="submission_difficulty",
                        summary="; ".join(summary_parts),
                        subjective=True,
                        provenance=prov,
                    )
                )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                    metrics=metrics,
                    experiences=experiences,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid SciPythonSpider records parsed")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "submission_experience",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Domain Experience Repositories (Markdown / HTML Table / Bullets)
# ---------------------------------------------------------------------------

def _parse_domain_experiences(
    source_id: str,
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        content = f.read()

    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", f"Empty domain experience file: {file_path}")
    if _is_html_error_page(content):
        raise JournalError("SCHEMA_CHANGED", f"HTML error page detected: {file_path}")

    field_map = {
        "cs_experiences": "computer_science",
        "security_experiences": "cyber_security",
        "fault_experiences": "fault_diagnosis",
        "medical_experiences": "medicine",
    }
    field_name = field_map.get(source_id, "general")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    lines = content.splitlines()
    has_md_table = any(line.strip().startswith("|") and "|" in line.strip()[1:] for line in lines)
    has_html_table = "<table" in content.lower()

    if has_md_table:
        in_table = False
        headers: List[str] = []
        for line_no, line in enumerate(lines, start=1):
            sline = line.strip()
            if sline.startswith("|") and sline.endswith("|"):
                cells = [c.strip() for c in sline[1:-1].split("|")]
                if not in_table:
                    if any(w in c for c in cells for w in ["刊名", "Journal", "会议", "名称", "Title"]):
                        headers = cells
                        in_table = True
                        continue
                else:
                    if all(re.match(r"^:?-+:?$", c) for c in cells):
                        continue
                    total_rows += 1
                    if total_rows > MAX_RECORDS_LIMIT:
                        raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")
                    rec, err_code = _parse_experience_cells(source_id, field_name, headers, cells, line_no, data_year, source_version)
                    if err_code:
                        rejected_count += 1
                        _safe_add_rejected(rejected, line_no, err_code, "row")
                    elif rec:
                        records.append(rec)
            else:
                if in_table and sline and not sline.startswith("|"):
                    in_table = False

    elif has_html_table:
        row_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.DOTALL | re.IGNORECASE)
        headers: List[str] = []
        for r_idx, row_html in enumerate(row_matches, start=1):
            th_cells = re.findall(r"<th[^>]*>(.*?)</th>", row_html, re.DOTALL | re.IGNORECASE)
            if th_cells and not headers:
                headers = [re.sub(r"<[^>]+>", "", c).strip() for c in th_cells]
                continue
            td_cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL | re.IGNORECASE)
            if td_cells:
                total_rows += 1
                if total_rows > MAX_RECORDS_LIMIT:
                    raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")
                clean_tds = [re.sub(r"<[^>]+>", "", c).strip() for c in td_cells]
                links = re.findall(r'href=[\'"]([^\'"]+)[\'"]', row_html)
                link_url = links[0] if links else ""
                rec, err_code = _parse_experience_cells(
                    source_id, field_name, headers, clean_tds, r_idx, data_year, source_version, external_url=link_url
                )
                if err_code:
                    rejected_count += 1
                    _safe_add_rejected(rejected, r_idx, err_code, "row")
                elif rec:
                    records.append(rec)
    else:
        # Bullet points
        for line_no, line in enumerate(lines, start=1):
            sline = line.strip()
            if not sline.startswith(("-", "*", "•")):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            clean_item = re.sub(r"^[-*•]\s*", "", sline)
            url_match = re.search(r"https?://[^\s)\]]+", clean_item)
            url = url_match.group(0) if url_match else ""

            title = ""
            m_bold = re.search(r"\*\*([^*]+)\*\*", clean_item)
            if m_bold:
                title = m_bold.group(1).strip()
            elif ":" in clean_item or "：" in clean_item:
                parts = re.split(r"[:：]", clean_item, 1)
                title = parts[0].strip()

            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            summary = clean_item
            if url:
                summary = summary.replace(url, "").strip()

            prov = Provenance(
                source_id=source_id,
                source_record=str(line_no),
                data_year=data_year,
                authority="community",
                observed_at=None,
                source_version=source_version,
            )
            exp = Experience(
                field=field_name,
                topic="submission_experience",
                summary=summary[:200],
                url=url,
                subjective=True,
                provenance=prov,
            )
            kind: Literal["journal", "conference"] = "conference" if any(w in title.upper() for w in ["CONF", "SYMPOSIUM", "WORKSHOP", "会议", "CCS", "USENIX", "NDSS", "IEEE S&P"]) else "journal"
            records.append(
                JournalRecord(
                    title=title,
                    kind=kind,
                    fields=[field_name],
                    provenance=prov,
                    experiences=[exp],
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", f"No valid experience records parsed from {file_path}")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "domain_experiences",
        "field": field_name,
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_experience_cells(
    source_id: str,
    field_name: str,
    headers: List[str],
    cells: List[str],
    line_no: int,
    data_year: Optional[int],
    source_version: str,
    external_url: str = "",
) -> Tuple[Optional[JournalRecord], Optional[str]]:
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "会议", "名称", "name"]) if headers else 0
    if title_idx is None or title_idx >= len(cells):
        return None, "NO_TITLE_COLUMN"

    title = _clean_str(cells[title_idx])
    if not title:
        return None, "FIELD_EMPTY"

    url = external_url
    if not url:
        for c in cells:
            m = re.search(r"https?://[^\s)\]]+", c)
            if m:
                url = m.group(0)
                break

    kind: Literal["journal", "conference"] = "conference" if any(w in title.upper() for w in ["CONF", "SYMPOSIUM", "WORKSHOP", "会议", "CCS", "USENIX", "NDSS", "IEEE S&P"]) else "journal"

    prov = Provenance(
        source_id=source_id,
        source_record=str(line_no),
        data_year=data_year,
        authority="community",
        observed_at=None,
        source_version=source_version,
    )

    summary_parts = []
    for idx, c in enumerate(cells):
        if idx != title_idx and c:
            h_label = headers[idx] if idx < len(headers) else ""
            clean_c = re.sub(r"https?://[^\s)\]]+", "", c).strip()
            if clean_c:
                summary_parts.append(f"{h_label}: {clean_c}" if h_label else clean_c)

    exp = Experience(
        field=field_name,
        topic="submission_experience",
        summary="; ".join(summary_parts)[:200],
        url=url,
        subjective=True,
        provenance=prov,
    )

    return JournalRecord(
        title=title,
        kind=kind,
        fields=[field_name],
        provenance=prov,
        experiences=[exp],
    ), None


# ---------------------------------------------------------------------------
# easyScholar Legacy Extension Static Export
# ---------------------------------------------------------------------------

def _parse_easyscholar_legacy(
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        content = f.read()

    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
    if _is_html_error_page(content):
        raise JournalError("SCHEMA_CHANGED", f"HTML error page detected: {file_path}")

    if re.search(r"(?:function\s*\(|=>\s*\{|eval\(|window\.|document\.)", content):
        raise JournalError("SCHEMA_CHANGED", "Executable JavaScript rejected in easyScholar legacy import")

    try:
        data = json.loads(content)
    except Exception as e:
        raise JournalError("SCHEMA_CHANGED", "Failed to parse JSON in easyScholar legacy")

    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "records" in data and isinstance(data["records"], list):
            items = data["records"]
        else:
            items = [data]

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = len(items)
    rejected_count = 0

    for idx, item in enumerate(items, start=1):
        if idx > MAX_RECORDS_LIMIT:
            raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

        if not isinstance(item, dict):
            rejected_count += 1
            _safe_add_rejected(rejected, idx, "INVALID_TYPE", "item")
            continue

        title = _clean_str(item.get("publicationName") or item.get("title") or item.get("name"))
        if not title:
            rejected_count += 1
            _safe_add_rejected(rejected, idx, "FIELD_EMPTY", "publicationName")
            continue

        issns: List[str] = []
        for k in ["issn", "eissn"]:
            if k in item and item[k]:
                for v in _clean_issns(item[k]):
                    if v not in issns:
                        issns.append(v)

        prov = Provenance(
            source_id="easyscholar_legacy",
            source_record=str(idx),
            data_year=data_year,
            authority="community",
            observed_at=None,
            source_version=source_version,
        )

        rankings: List[Ranking] = []
        metrics: List[Metric] = []

        if "sci" in item and item["sci"]:
            rankings.append(Ranking(system="cas", year=data_year, grade=str(item["sci"]), provenance=prov))
        if "ccf" in item and item["ccf"]:
            rankings.append(Ranking(system="ccf", year=data_year, grade=str(item["ccf"]), provenance=prov))
        if "sciif" in item and item["sciif"]:
            try:
                v = float(item["sciif"])
                metrics.append(Metric(name="impact_factor", value=v, comparator="eq", year=data_year, provenance=prov))
            except ValueError:
                metrics.append(Metric(name="impact_factor", raw=str(item["sciif"]), comparator="unknown", year=data_year, provenance=prov))

        records.append(
            JournalRecord(
                title=title,
                issns=issns,
                provenance=prov,
                rankings=rankings,
                metrics=metrics,
            )
        )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid easyScholar legacy records parsed")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings",
        "subsystem": "easyscholar",
        "year": data_year,
        "complete": complete,
        "authority": "community",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Zotero UpdateIFs
# ---------------------------------------------------------------------------

def _parse_zotero_updateifs(
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        head_preview = f.read(2048)
        if not head_preview.strip():
            raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
        if _is_html_error_page(head_preview):
            raise JournalError("SCHEMA_CHANGED", f"HTML error page detected in {file_path}")
        f.seek(0)
        content = f.read()

    if content.strip().startswith(("{", "[")):
        try:
            data = json.loads(content)
        except Exception:
            raise JournalError("SCHEMA_CHANGED", "Failed parsing JSON in zotero_updateifs")

        records: List[JournalRecord] = []
        rejected: List[Dict[str, Any]] = []
        total_rows = 0
        rejected_count = 0

        if isinstance(data, dict):
            for idx, (k, val) in enumerate(data.items(), start=1):
                total_rows += 1
                if total_rows > MAX_RECORDS_LIMIT:
                    raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

                issns = _clean_issns(k)
                title = ""
                if_val = None
                raw_val = ""
                if isinstance(val, dict):
                    title = val.get("title") or val.get("journal") or (issns[0] if issns else "")
                    raw_val = str(val.get("if") or val.get("impact_factor") or "")
                else:
                    title = issns[0] if issns else ""
                    raw_val = str(val)

                if not title:
                    rejected_count += 1
                    _safe_add_rejected(rejected, idx, "FIELD_EMPTY", "title")
                    continue

                try:
                    if_val = float(raw_val) if raw_val else None
                except ValueError:
                    pass

                prov = Provenance(
                    source_id="zotero_updateifs",
                    source_record=str(idx),
                    data_year=data_year,
                    authority="community",
                    observed_at=None,
                    source_version=source_version,
                )
                m = Metric(
                    name="impact_factor",
                    raw=raw_val,
                    value=if_val,
                    comparator="eq" if if_val else "unknown",
                    year=data_year,
                    provenance=prov,
                )
                records.append(
                    JournalRecord(
                        title=title,
                        issns=issns,
                        provenance=prov,
                        metrics=[m],
                    )
                )

            if not records:
                raise JournalError("SCHEMA_CHANGED", "No valid zotero_updateifs records parsed")

            complete = (rejected_count == 0 and len(records) > 0)
            return ParsedBatch(
                records=records,
                rejected=rejected,
                total_rows=total_rows,
                rejected_count=rejected_count,
                coverage={"system": "metrics", "subsystem": "if", "year": data_year, "complete": complete, "authority": "community"},
            )

    return _parse_generic_csv("zotero_updateifs", file_path, data_year, encoding, source_version)


# ---------------------------------------------------------------------------
# Generic CSV / JSON (Local & Records)
# ---------------------------------------------------------------------------

def _parse_records_json(
    source_id: str,
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        content = f.read()

    if not content.strip():
        raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
    if _is_html_error_page(content):
        raise JournalError("SCHEMA_CHANGED", f"HTML error page detected: {file_path}")

    try:
        data = json.loads(content)
    except Exception as e:
        raise JournalError("SCHEMA_CHANGED", f"Invalid JSON in {file_path}: {e}")

    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "records" in data and isinstance(data["records"], list):
            items = data["records"]
        else:
            items = [data]
    else:
        raise JournalError("SCHEMA_CHANGED", f"Expected list or object with records: {file_path}")

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = len(items)
    rejected_count = 0

    for idx, item in enumerate(items, start=1):
        if idx > MAX_RECORDS_LIMIT:
            raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

        if not isinstance(item, dict):
            rejected_count += 1
            _safe_add_rejected(rejected, idx, "INVALID_TYPE", "record")
            continue

        try:
            if "provenance" not in item:
                item["provenance"] = {
                    "source_id": source_id,
                    "source_record": str(idx),
                    "data_year": data_year,
                    "authority": "community" if source_id != "local" else "user",
                    "observed_at": None,
                    "source_version": source_version,
                }
            rec = JournalRecord.model_validate(item)
            records.append(rec)
        except Exception:
            rejected_count += 1
            # Never echo raw fields or full exception string which may contain secrets
            _safe_add_rejected(rejected, idx, "VALIDATION_ERROR", "record")

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid records parsed from JSON")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "rankings" if source_id != "local" else "local",
        "year": data_year,
        "complete": complete,
        "authority": "community" if source_id != "local" else "user",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )


def _parse_generic_csv(
    source_id: str,
    file_path: str,
    data_year: Optional[int],
    encoding: str,
    source_version: str,
) -> ParsedBatch:
    with open(file_path, "r", encoding=encoding) as f:
        head_preview = f.read(2048)
        if not head_preview.strip():
            raise JournalError("SCHEMA_CHANGED", f"Empty file: {file_path}")
        if _is_html_error_page(head_preview):
            raise JournalError("SCHEMA_CHANGED", f"HTML error page detected in {file_path}")
        f.seek(0)
        reader = csv.reader(f)
        headers = next(reader, None)

    if not headers:
        raise JournalError("SCHEMA_CHANGED", f"Empty CSV headers in {file_path}")

    headers = [h.strip() for h in headers]
    title_idx = _find_col(headers, ["刊名", "Journal", "Title", "期刊名称", "name"])
    if title_idx is None:
        raise JournalError("SCHEMA_CHANGED", f"Missing critical journal title column in generic CSV: {headers}")

    issn_idx = _find_col(headers, ["ISSN", "issn"])
    eissn_idx = _find_col(headers, ["EISSN", "eissn"])
    combined_issn_idx = _find_col(headers, ["ISSN/EISSN", "ISSN / EISSN"])

    records: List[JournalRecord] = []
    rejected: List[Dict[str, Any]] = []
    total_rows = 0
    rejected_count = 0

    with open(file_path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            total_rows += 1
            if total_rows > MAX_RECORDS_LIMIT:
                raise JournalError("SCHEMA_CHANGED", f"File exceeds row limit of {MAX_RECORDS_LIMIT}")

            if title_idx >= len(row):
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "ROW_TRUNCATED", "title")
                continue

            title = _clean_str(row[title_idx])
            if not title:
                rejected_count += 1
                _safe_add_rejected(rejected, line_no, "FIELD_EMPTY", "title")
                continue

            row_year = _extract_row_year(headers, row, data_year)

            issns: List[str] = []
            if combined_issn_idx is not None and combined_issn_idx < len(row):
                issns.extend(_clean_issns(row[combined_issn_idx]))
            if issn_idx is not None and issn_idx < len(row):
                for v in _clean_issns(row[issn_idx]):
                    if v not in issns:
                        issns.append(v)
            if eissn_idx is not None and eissn_idx < len(row):
                for v in _clean_issns(row[eissn_idx]):
                    if v not in issns:
                        issns.append(v)

            prov = Provenance(
                source_id=source_id,
                source_record=str(line_no),
                data_year=row_year,
                authority="community" if source_id != "local" else "user",
                observed_at=None,
                source_version=source_version,
            )

            records.append(
                JournalRecord(
                    title=title,
                    issns=issns,
                    provenance=prov,
                )
            )

    if not records:
        raise JournalError("SCHEMA_CHANGED", "No valid records parsed from generic CSV")

    complete = (rejected_count == 0 and len(records) > 0)
    coverage = {
        "system": "general",
        "year": data_year,
        "complete": complete,
        "authority": "community" if source_id != "local" else "user",
    }
    return ParsedBatch(
        records=records,
        rejected=rejected,
        total_rows=total_rows,
        rejected_count=rejected_count,
        coverage=coverage,
    )
