"""Public metadata and explicitly open PDF providers; importing never performs I/O."""
from __future__ import annotations

import calendar
import copy
import html
import ipaddress
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple

from paperflow.engine.journals import providers as http
from paperflow.engine.journals.models import JournalError, utc_now
from paperflow.engine.literature.models import FullTextLocation, PaperRecord, paper_id_for, public_url

CROSSREF_API = "https://api.crossref.org/works"
ARXIV_API = "https://export.arxiv.org/api/query"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
API_HOSTS = frozenset({"api.crossref.org", "export.arxiv.org", "api.unpaywall.org"})
DEFAULT_PDF_ALLOWED_HOSTS = frozenset({
    "arxiv.org", "export.arxiv.org", "pmc.ncbi.nlm.nih.gov", "europepmc.org",
    "www.europepmc.org", "zenodo.org", "hal.science", "hal.archives-ouvertes.fr",
})
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_QUERY_CHARS = 1000
_MAX_ARXIV_TERMS = 32
MAX_RESULTS = 50
# Search excludes non-paper objects/collection containers, not DOI details.
_CROSSREF_NON_PAPER_TYPES = frozenset({
    "component", "grant", "dataset", "peer-review", "standard", "standard-series",
    "journal", "journal-volume", "journal-issue", "proceedings", "proceedings-series",
    "report-series", "book-series", "book-set",
})
ARXIV_REQUEST_INTERVAL = 3.0
ARXIV_CACHE_TTL = 24 * 60 * 60.0
ARXIV_CACHE_MAX_ENTRIES = 64
ARXIV_CACHE_MAX_BYTES = 8 * 1024 * 1024
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"\x00-\x1f\x7f]+", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z][a-z0-9.-]*/\d{2}(?:0[1-9]|1[0-2])\d{3})(?:v[1-9]\d{0,5})?",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"v[1-9]\d*$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^\s@<>]{1,200}@[a-z0-9.-]+\.[a-z]{2,63}", re.IGNORECASE)
_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST_AT: Optional[float] = None
_CLOCK_FN: Callable[[], float] = time.monotonic
_SLEEP_FN: Callable[[float], None] = time.sleep


def set_arxiv_timing_seams(
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> None:
    """Isolated test seam; reset the shared interval when replacing the clock."""
    global _CLOCK_FN, _SLEEP_FN, _ARXIV_LAST_REQUEST_AT
    with _ARXIV_LOCK:
        _CLOCK_FN = clock or time.monotonic
        _SLEEP_FN = sleep or time.sleep
        _ARXIV_LAST_REQUEST_AT = None


def _input_text(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise JournalError("INVALID_INPUT", f"{label}必须是非空文本，且不得超过 {max_chars} 字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise JournalError("INVALID_INPUT", f"{label}不得包含控制字符")
    try:
        if len(value.encode("utf-8")) > max_chars * 4:
            raise JournalError("INVALID_INPUT", f"{label}超过输入预算")
    except UnicodeError:
        raise JournalError("INVALID_INPUT", f"{label}包含无效文本编码") from None
    return value.strip()


def _arxiv_search_expression(query: str) -> str:
    """Join literal terms with AND; only unescaped double quotes group phrases.

    Native grammar: arXiv API User Manual, section 5.1:
    https://info.arxiv.org/help/api/user-manual.html#51-details-of-query-construction
    Backslashes remain literal (as before); an odd run shields a quote from
    being a delimiter. Emitting every term as an escaped all: phrase prevents
    user field prefixes, Boolean operators or parentheses from changing scope.
    """
    terms: List[str] = []
    buffer: List[str] = []
    quoted = False
    backslashes = 0

    def append_term():
        term = "".join(buffer).strip()
        if not term:
            raise JournalError("INVALID_INPUT", "arXiv 检索词不能包含空短语")
        if len(terms) >= _MAX_ARXIV_TERMS:
            raise JournalError("INVALID_INPUT", f"arXiv 检索最多允许 {_MAX_ARXIV_TERMS} 个关键词或短语")
        terms.append(term)
        buffer.clear()

    for char in query:
        if char == '"' and backslashes % 2 == 0:
            if quoted:
                append_term()
            elif buffer:
                append_term()
            quoted = not quoted
        elif char.isspace() and not quoted:
            if buffer:
                append_term()
        else:
            buffer.append(char)
        backslashes = backslashes + 1 if char == "\\" else 0
    if quoted:
        raise JournalError("INVALID_INPUT", "arXiv 检索词的双引号必须成对")
    if buffer:
        append_term()
    if not terms:
        raise JournalError("INVALID_INPUT", "arXiv 检索词不能为空")
    return " AND ".join('all:"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"' for term in terms)


def normalize_doi(identifier: str) -> str:
    value = _input_text(identifier, "DOI", 2048)
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    elif "://" in value:
        safe = public_url(value)
        if not safe:
            raise JournalError("INVALID_INPUT", "DOI 地址不得包含凭据或签名参数")
        parts = urllib.parse.urlsplit(safe)
        if parts.hostname not in {"doi.org", "dx.doi.org", "www.doi.org"} or parts.query:
            raise JournalError("INVALID_INPUT", "请提供 DOI 或 doi.org 公开地址")
        value = urllib.parse.unquote(parts.path.lstrip("/"))
    if not _DOI_RE.fullmatch(value):
        raise JournalError("INVALID_INPUT", "DOI 格式无效")
    value = value.lower()
    if len(urllib.parse.quote(value, safe="")) > 3500:
        raise JournalError("INVALID_INPUT", "DOI 编码后超过公开地址长度预算")
    return value


def normalize_arxiv_id(identifier: str) -> str:
    value = _input_text(identifier, "arXiv 标识", 2048)
    if value.lower().startswith("arxiv:"):
        value = value[6:].strip()
    elif "://" in value:
        safe = public_url(value)
        if not safe:
            raise JournalError("INVALID_INPUT", "arXiv 地址不得包含凭据或签名参数")
        parts = urllib.parse.urlsplit(safe)
        if parts.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} or parts.query:
            raise JournalError("INVALID_INPUT", "请提供 arXiv 标识或官方 abs/pdf 地址")
        path = urllib.parse.unquote(parts.path)
        if not path.startswith(("/abs/", "/pdf/")):
            raise JournalError("INVALID_INPUT", "arXiv 地址必须指向论文 abs/pdf 路径")
        value = path[5:]
        if value.endswith(".pdf"):
            value = value[:-4]
    if not _ARXIV_RE.fullmatch(value):
        raise JournalError("INVALID_INPUT", "arXiv 标识或版本格式无效")
    return value.lower()


def _text(value: Any, limit: int = 100000) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _first_text(value: Any, limit: int = 3000) -> str:
    return _text(value[0] if isinstance(value, list) and value else value, limit)


def _abstract(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    # JATS fragments often omit namespace declarations. No external XML entities.
    return _text(html.unescape(re.sub(r"<[^>]*>", " ", value[:200000])))


def _date_bound(value: Any, upper: bool) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise JournalError("INVALID_INPUT", "日期范围必须是年份或 YYYY-MM[-DD]")
    raw = str(value)
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", raw):
        raise JournalError("INVALID_INPUT", "日期范围必须是年份或 YYYY-MM[-DD]")
    bits = [int(part) for part in raw.split("-")]
    year = bits[0]
    if not 1500 <= year <= 2200:
        raise JournalError("INVALID_INPUT", "年份范围应在 1500 至 2200 之间")
    month = bits[1] if len(bits) > 1 else (12 if upper else 1)
    try:
        day = bits[2] if len(bits) > 2 else (calendar.monthrange(year, month)[1] if upper else 1)
        return date(year, month, day)
    except (ValueError, calendar.IllegalMonthError):
        raise JournalError("INVALID_INPUT", "日期范围包含无效的年月日") from None


def _crossref_date(item: dict) -> Tuple[Optional[str], Optional[int]]:
    for key in ("published", "published-online", "published-print", "issued"):
        value = item.get(key)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        bits = parts[0]
        if not 1 <= len(bits) <= 3 or any(type(part) is not int for part in bits):
            continue
        try:
            if not 1500 <= bits[0] <= 2200:
                continue
            date(bits[0], bits[1] if len(bits) > 1 else 1, bits[2] if len(bits) > 2 else 1)
        except ValueError:
            continue
        return "-".join([f"{bits[0]:04d}"] + [f"{part:02d}" for part in bits[1:]]), bits[0]
    return None, None


def _atom_date(value: Any) -> Tuple[Optional[str], Optional[int]]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if not 1500 <= parsed.year <= 2200:
            return None, None
        return parsed.date().isoformat(), parsed.year
    except ValueError:
        return None, None


def _host_set(value: Collection[str]) -> frozenset[str]:
    if not isinstance(value, Collection) or isinstance(value, (str, bytes)):
        raise JournalError("INVALID_INPUT", "全文允许主机必须是主机名集合，而不是 URL 或通配符")
    if len(value) > 100:
        raise JournalError("INVALID_INPUT", "全文允许主机数量超过上限")
    result = set()
    for host in value:
        if not isinstance(host, str):
            raise JournalError("INVALID_INPUT", "全文允许主机配置无效")
        name = host.strip().lower()
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name):
                raise JournalError("INVALID_INPUT", "全文允许主机必须是明确的主机名，不接受 URL 或通配符")
            if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in name.split(".")):
                raise JournalError("INVALID_INPUT", "全文允许主机配置无效")
        result.add(name)
    return frozenset(result)


class LiteratureProviders:
    def __init__(self, fetcher=None, email=None, allowed_pdf_hosts=None):
        self._fetcher = http.safe_fetch if fetcher is None else fetcher
        if not callable(self._fetcher):
            raise JournalError("INVALID_INPUT", "文献 transport 必须是可调用的 safe_fetch 兼容接口")
        configured = email if email is not None else (
            os.environ.get("PAPERFLOW_UNPAYWALL_EMAIL") or os.environ.get("PAPERFLOW_CONTACT_EMAIL") or ""
        )
        if not isinstance(configured, str) or (configured.strip() and not _EMAIL_RE.fullmatch(configured.strip())):
            raise JournalError("INVALID_INPUT", "联系邮箱配置无效；请配置纯邮箱地址，不要包含控制字符")
        self._email = configured.strip()
        policy = DEFAULT_PDF_ALLOWED_HOSTS if allowed_pdf_hosts is None else _host_set(allowed_pdf_hosts)
        extra = os.environ.get("PAPERFLOW_PDF_ALLOWED_HOSTS", "")
        self.allowed_pdf_hosts = frozenset(policy | _host_set([part for part in re.split(r"[,;\s]+", extra) if part]))
        # Only successful parsed arXiv records are cached, with both count and byte bounds.
        self._arxiv_cache: OrderedDict[str, Tuple[float, List[dict], int]] = OrderedDict()
        self._arxiv_cache_bytes = 0

    def capabilities(self) -> dict:
        return {
            "sources": {
                "crossref": {"search": True, "details": True},
                "arxiv": {"search": True, "details": True, "request_interval_seconds": ARXIV_REQUEST_INTERVAL},
                "unpaywall": {"resolve_oa": True, "email_configured": bool(self._email)},
            },
            "email_configured": bool(self._email),
            "unpaywall_email_configured": bool(self._email),
            "unpaywall_status": "ready" if self._email else "CONTACT_EMAIL_REQUIRED",
            "allowed_pdf_hosts": sorted(self.allowed_pdf_hosts),
            "max_results": MAX_RESULTS,
            "max_pdf_bytes": http.MAX_DOWNLOAD_SIZE_BYTES,
            "timeout_seconds": http.TOTAL_TIME_BUDGET_SECONDS,
            "sort_by": ["relevance", "date"],
        }

    def _request(self, url: str, *, pdf: bool = False, budget: float = 90.0, arxiv: bool = False):
        if budget <= 0 or not math.isfinite(budget):
            raise JournalError("TIMEOUT", "文献请求超过总体时间预算")
        host = urllib.parse.urlsplit(url).hostname
        cap = http.MAX_DOWNLOAD_SIZE_BYTES if pdf else MAX_METADATA_BYTES
        if not pdf and host not in API_HOSTS:
            raise JournalError("HOST_DENIED", "文献 API 主机未获授权")
        deadline = http._MONOTONIC_FN() + min(budget, http.TOTAL_TIME_BUDGET_SECONDS)
        try:
            body, headers, final_url = self._fetcher(
                url,
                headers={"User-Agent": "PaperFlow/1.0 (public literature metadata)",
                         "Accept": "application/pdf" if pdf else "application/json, application/atom+xml",
                         "Accept-Encoding": "identity"},
                expected_host=host,
                allowed_hosts=self.allowed_pdf_hosts if pdf else frozenset({host}),
                allow_cross_host_redirect=pdf,
                max_bytes=cap,
                deadline=deadline,
                pin_dns=True,
                public_only=pdf,
                request_interval=ARXIV_REQUEST_INTERVAL if arxiv else 0.0,
            )
        except JournalError as exc:
            message = "Unpaywall 请求失败（" + exc.code + "）；联系邮箱已脱敏" if host == "api.unpaywall.org" else str(exc)
            raise JournalError(exc.code, message) from None
        except urllib.error.HTTPError as exc:
            code = "AUTH_REQUIRED" if exc.code in (401, 403) else ("RATE_LIMITED" if exc.code == 429 else "NETWORK_ERROR")
            raise JournalError(code, f"文献来源请求失败 (HTTP {exc.code})") from None
        except (TimeoutError,):
            raise JournalError("TIMEOUT", "文献来源请求超时") from None
        except Exception:
            # Never expose transport exceptions, which can contain the Unpaywall email URL.
            raise JournalError("NETWORK_ERROR", "文献来源连接失败") from None
        if http._MONOTONIC_FN() >= deadline:
            raise JournalError("TIMEOUT", "文献来源请求超过总体时间预算")
        if not isinstance(body, bytes) or not isinstance(headers, dict) or not isinstance(final_url, str):
            raise JournalError("INVALID_RESPONSE", "文献来源返回了无效响应")
        if len(body) > cap:
            raise JournalError("FILE_TOO_LARGE", "文献来源响应超过体积上限")
        return body, {str(key).lower(): str(value) for key, value in headers.items()}, final_url

    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise JournalError("INVALID_RESPONSE", "文献来源返回的 JSON 无法解析") from None
        if not isinstance(value, dict):
            raise JournalError("INVALID_RESPONSE", "文献来源未返回 JSON 对象")
        return value

    def search(self, source_id: str, query: str, limit=10, year_from=None, year_to=None, sort_by="relevance") -> List[dict]:
        if source_id not in ("crossref", "arxiv"):
            raise JournalError("UNSUPPORTED_SOURCE", "文献检索来源仅支持 crossref 和 arxiv")
        query = _input_text(query, "检索词", MAX_QUERY_CHARS)
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise JournalError("INVALID_INPUT", "结果数量必须是 1 至 50 之间的整数")
        if sort_by not in ("relevance", "date"):
            raise JournalError("INVALID_INPUT", "排序仅支持 relevance 或 date")
        lower, upper = _date_bound(year_from, False), _date_bound(year_to, True)
        if lower is not None and upper is not None and lower > upper:
            raise JournalError("INVALID_INPUT", "开始日期不得晚于结束日期")
        if source_id == "crossref":
            params = {"query.bibliographic": query, "rows": limit,
                      "sort": "relevance" if sort_by == "relevance" else "published", "order": "desc"}
            filters = []
            if lower:
                filters.append("from-pub-date:" + lower.isoformat())
            if upper:
                filters.append("until-pub-date:" + upper.isoformat())
            if filters:
                params["filter"] = ",".join(filters)
            body, _, _ = self._request(CROSSREF_API + "?" + urllib.parse.urlencode(params))
            payload = self._json(body)
            message = payload.get("message")
            if payload.get("status", "ok") != "ok" or not isinstance(message, dict) or not isinstance(message.get("items"), list):
                raise JournalError("INVALID_RESPONSE", "Crossref 未返回有效检索结果")
            records = []
            for item in message["items"][:limit]:
                if isinstance(item, dict):
                    if not _first_text(item.get("title")):
                        continue
                    if _text(item.get("type"), 100).lower() in _CROSSREF_NON_PAPER_TYPES:
                        continue
                records.append(self._crossref_record(item, query))
            return records
        # Terms/explicit phrases use arXiv's native all:/AND grammar, never user operators.
        expression = _arxiv_search_expression(query)
        if lower or upper:
            start = (lower or date(1500, 1, 1)).strftime("%Y%m%d") + "0000"
            end = (upper or date(2200, 12, 31)).strftime("%Y%m%d") + "2359"
            expression = "(" + expression + ") AND submittedDate:[" + start + " TO " + end + "]"
        params = {"search_query": expression, "start": 0, "max_results": limit,
                  "sortBy": "relevance" if sort_by == "relevance" else "submittedDate", "sortOrder": "descending"}
        return self._arxiv_records(ARXIV_API + "?" + urllib.parse.urlencode(params), query)[:limit]

    def details(self, identifier: str) -> dict:
        value = _input_text(identifier, "文献标识", 2048)
        is_arxiv = value.lower().startswith("arxiv:") or bool(_ARXIV_RE.fullmatch(value))
        if "://" in value:
            try:
                is_arxiv = urllib.parse.urlsplit(value).hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
            except ValueError:
                raise JournalError("INVALID_INPUT", "文献地址无效") from None
        if is_arxiv:
            arxiv_id = normalize_arxiv_id(value)
            url = ARXIV_API + "?" + urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
            records = self._arxiv_records(url, "")
            matching = [record for record in records if _VERSION_RE.sub("", record["arxiv_id"]) == _VERSION_RE.sub("", arxiv_id)]
            if not matching:
                raise JournalError("NOT_FOUND", "arXiv 未返回该文献")
            if _VERSION_RE.search(arxiv_id) and matching[0]["arxiv_id"] != arxiv_id:
                raise JournalError("INVALID_RESPONSE", "arXiv 返回的版本与请求版本不一致")
            return matching[0]
        doi = normalize_doi(value)
        body, _, _ = self._request(CROSSREF_API + "/" + urllib.parse.quote(doi, safe=""))
        payload = self._json(body)
        if payload.get("status", "ok") != "ok" or not isinstance(payload.get("message"), dict):
            raise JournalError("INVALID_RESPONSE", "Crossref 未返回有效 DOI 记录")
        record = self._crossref_record(payload["message"], "")
        if record["doi"] != doi:
            raise JournalError("INVALID_RESPONSE", "Crossref 返回的 DOI 与请求不一致")
        return record

    @staticmethod
    def _crossref_record(item: Any, query: str) -> dict:
        if not isinstance(item, dict):
            raise JournalError("INVALID_RESPONSE", "Crossref 文献记录无效")
        try:
            doi = normalize_doi(item.get("DOI", ""))
        except JournalError:
            raise JournalError("INVALID_RESPONSE", "Crossref 文献缺少有效 DOI") from None
        published, year = _crossref_date(item)
        landing = public_url(item.get("URL")) or "https://doi.org/" + urllib.parse.quote(doi, safe="/")
        raw_authors, raw_links = item.get("author") or [], item.get("link") or []
        if not isinstance(raw_authors, list) or not isinstance(raw_links, list):
            raise JournalError("INVALID_RESPONSE", "Crossref 作者或全文链接列表无效")
        authors = []
        for author in raw_authors[:500]:
            if isinstance(author, dict):
                name = " ".join(filter(None, [_text(author.get("given"), 500), _text(author.get("family"), 500)]))
                name = name or _text(author.get("name"), 1000)
                if name:
                    authors.append(name)
        locations = []
        for link in raw_links[:50]:
            if not isinstance(link, dict):
                continue
            url = public_url(link.get("URL"))
            if not url:
                continue
            locations.append(FullTextLocation(
                url=url, landing_url=landing, source_id="crossref",
                version=_text(link.get("content-version"), 100) or "unknown",
                is_open=False, access_evidence="Crossref 元数据全文/TDM 候选；尚未确认开放获取",
            ))
        retrieved = utc_now()
        record = PaperRecord(
            paper_id=paper_id_for("crossref", doi), source_id="crossref", source_record=doi,
            title=_first_text(item.get("title")), authors=authors, publication_date=published, year=year,
            venue=_first_text(item.get("container-title")), publication_type=_text(item.get("type"), 100) or "unknown",
            abstract=_abstract(item.get("abstract")), doi=doi, landing_url=landing,
            fulltext_locations=locations, retrieved_at=retrieved, query=query,
            sources=[{"source_id": "crossref", "source_record": doi,
                      "url": CROSSREF_API + "/" + urllib.parse.quote(doi, safe=""), "retrieved_at": retrieved}],
        )
        return record.model_dump(mode="json")

    def _arxiv_records(self, url: str, query: str) -> List[dict]:
        global _ARXIV_LAST_REQUEST_AT
        deadline = _CLOCK_FN() + http.TOTAL_TIME_BUDGET_SECONDS
        if not _ARXIV_LOCK.acquire(timeout=http.TOTAL_TIME_BUDGET_SECONDS):
            raise JournalError("TIMEOUT", "arXiv 串行请求队列超时")
        try:
            now = _CLOCK_FN()
            for key, (expires, _, size) in list(self._arxiv_cache.items()):
                if expires <= now:
                    self._arxiv_cache.pop(key)
                    self._arxiv_cache_bytes -= size
            cached = self._arxiv_cache.get(url)
            if cached:
                self._arxiv_cache.move_to_end(url)
                records = copy.deepcopy(cached[1])
                for record in records:
                    record["query"] = query
                return records
            if _ARXIV_LAST_REQUEST_AT is not None:
                wait = max(0.0, ARXIV_REQUEST_INTERVAL - (now - _ARXIV_LAST_REQUEST_AT))
                if wait >= deadline - now:
                    raise JournalError("TIMEOUT", "arXiv 请求间隔超过总体时间预算")
                if wait:
                    _SLEEP_FN(wait)
            if _CLOCK_FN() >= deadline:
                raise JournalError("TIMEOUT", "arXiv 请求超过总体时间预算")
            try:
                body, _, _ = self._request(url, budget=deadline - _CLOCK_FN(), arxiv=True)
            finally:
                # Failed requests are also paced; they are never cached or converted to [].
                _ARXIV_LAST_REQUEST_AT = _CLOCK_FN()
            records = self._parse_arxiv(body, query)
            self._arxiv_cache[url] = (_CLOCK_FN() + ARXIV_CACHE_TTL, copy.deepcopy(records), len(body))
            self._arxiv_cache_bytes += len(body)
            while len(self._arxiv_cache) > ARXIV_CACHE_MAX_ENTRIES or self._arxiv_cache_bytes > ARXIV_CACHE_MAX_BYTES:
                _, (_, _, size) = self._arxiv_cache.popitem(last=False)
                self._arxiv_cache_bytes -= size
            return records
        finally:
            _ARXIV_LOCK.release()

    @staticmethod
    def _parse_arxiv(body: bytes, query: str) -> List[dict]:
        try:
            xml_text = body.decode("utf-8-sig")
        except UnicodeError:
            raise JournalError("INVALID_RESPONSE", "arXiv Atom 必须使用 UTF-8 编码") from None
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
            raise JournalError("INVALID_RESPONSE", "arXiv XML 不允许 DTD 或实体声明")
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, ValueError, RecursionError):
            raise JournalError("INVALID_RESPONSE", "arXiv 返回的 Atom 无法解析") from None
        if root.tag != "{" + NS["atom"] + "}feed":
            raise JournalError("INVALID_RESPONSE", "arXiv 未返回 Atom feed")
        records = []
        for entry in root.findall("atom:entry", NS)[:MAX_RESULTS]:
            raw_id = _text(entry.findtext("atom:id", default="", namespaces=NS), 2048)
            if "/api/errors" in raw_id:
                raise JournalError("NETWORK_ERROR", "arXiv 返回错误记录，未将其视为空结果")
            try:
                arxiv_id = normalize_arxiv_id(raw_id)
            except JournalError:
                raise JournalError("INVALID_RESPONSE", "arXiv 文献缺少有效标识") from None
            pdf_urls = []
            for link in entry.findall("atom:link", NS)[:50]:
                if link.get("title") != "pdf" and link.get("type") != "application/pdf":
                    continue
                url = public_url(link.get("href"))
                if not url:
                    continue
                try:
                    linked_id = normalize_arxiv_id(url)
                except JournalError:
                    continue
                if _VERSION_RE.sub("", linked_id) != _VERSION_RE.sub("", arxiv_id):
                    continue
                if not _VERSION_RE.search(arxiv_id) and _VERSION_RE.search(linked_id):
                    arxiv_id = linked_id
                if linked_id != arxiv_id:
                    continue
                parts = urllib.parse.urlsplit(url)
                if parts.hostname not in {"arxiv.org", "export.arxiv.org"} or not parts.path.startswith("/pdf/"):
                    continue
                pdf_urls.append(urllib.parse.urlunsplit(("https", parts.netloc, parts.path, "", "")))
            version_match = _VERSION_RE.search(arxiv_id)
            version = version_match.group() if version_match else "unknown"
            landing = "https://arxiv.org/abs/" + arxiv_id
            published_raw = _text(entry.findtext("atom:published", default="", namespaces=NS), 100)
            updated_raw = _text(entry.findtext("atom:updated", default="", namespaces=NS), 100)
            published, year = _atom_date(published_raw)
            doi_raw = _text(entry.findtext("arxiv:doi", default="", namespaces=NS), 2048)
            try:
                doi = normalize_doi(doi_raw) if doi_raw else ""
            except JournalError:
                doi = ""
            related = [{"scheme": "doi", "identifier": doi, "relation": "published_version"}] if doi else []
            retrieved = utc_now()
            record = PaperRecord(
                paper_id=paper_id_for("arxiv", arxiv_id), source_id="arxiv", source_record=arxiv_id,
                arxiv_id=arxiv_id, version=version, doi=doi, landing_url=landing,
                title=_text(entry.findtext("atom:title", default="", namespaces=NS), 3000),
                authors=[name for name in (_text(author.findtext("atom:name", default="", namespaces=NS), 1000)
                                          for author in entry.findall("atom:author", NS)[:500]) if name],
                abstract=_text(entry.findtext("atom:summary", default="", namespaces=NS)),
                publication_date=published, year=year, publication_type="preprint",
                venue=_text(entry.findtext("arxiv:journal_ref", default="", namespaces=NS), 3000),
                fulltext_locations=[FullTextLocation(url=url, landing_url=landing, source_id="arxiv", version=version,
                                                    is_open=True, access_evidence="arXiv 官方 Atom 的公开 PDF 链接；许可未知")
                                    for url in dict.fromkeys(pdf_urls)],
                related_identifiers=related, retrieved_at=retrieved, query=query,
                sources=[{"source_id": "arxiv", "source_record": arxiv_id, "url": landing,
                          "published": published_raw, "updated": updated_raw, "retrieved_at": retrieved}],
            )
            records.append(record.model_dump(mode="json"))
        return records

    def resolve_oa(self, doi: str) -> List[dict]:
        normalized = normalize_doi(doi)
        if not self._email:
            raise JournalError("CONTACT_EMAIL_REQUIRED", "请配置 PAPERFLOW_UNPAYWALL_EMAIL（或 PAPERFLOW_CONTACT_EMAIL）以定位 DOI 开放全文")
        url = UNPAYWALL_API + "/" + urllib.parse.quote(normalized, safe="") + "?" + urllib.parse.urlencode({"email": self._email})
        body, _, _ = self._request(url)
        payload = self._json(body)
        if "doi" in payload:
            try:
                returned_doi = normalize_doi(payload["doi"])
            except JournalError:
                raise JournalError("INVALID_RESPONSE", "Unpaywall 返回的 DOI 无效") from None
            if returned_doi != normalized:
                raise JournalError("INVALID_RESPONSE", "Unpaywall 返回的 DOI 与请求不一致")
        if type(payload.get("is_oa")) is not bool:
            raise JournalError("INVALID_RESPONSE", "Unpaywall 未返回明确的开放获取状态")
        if payload["is_oa"] is not True:
            return []
        candidates = [payload.get("best_oa_location")]
        locations = payload.get("oa_locations", [])
        if not isinstance(locations, list):
            raise JournalError("INVALID_RESPONSE", "Unpaywall 全文位置列表无效")
        candidates.extend(locations[:50])
        result, seen = [], set()
        for location in candidates:
            if not isinstance(location, dict) or location.get("is_oa") is False:
                continue
            pdf = public_url(location.get("url_for_pdf"))
            if not pdf or pdf in seen:
                continue
            seen.add(pdf)
            result.append(FullTextLocation(
                url=pdf, landing_url=public_url(location.get("url_for_landing_page")), source_id="unpaywall",
                version=_text(location.get("version"), 100) or "unknown", license=_text(location.get("license"), 2000) or None,
                is_open=True, access_evidence="Unpaywall v2 is_oa=true; url_for_pdf; " + (_text(location.get("evidence"), 1000) or "许可按位置记录，不推断再分发权"),
            ).model_dump(mode="json"))
            if len(result) >= 50:
                break
        return result

    def _pdf_url(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise JournalError("INVALID_URL", "全文位置缺少公开 PDF 地址")
        url = public_url(value)
        if not url:
            raise JournalError("SSRF_VIOLATION", "全文 URL 无效或包含邮箱、密钥、凭据或签名")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.port not in (None, 443):
            raise JournalError("INVALID_URL", "PDF 下载仅支持 HTTPS 443 端口")
        try:
            ip = ipaddress.ip_address(parts.hostname)
        except ValueError:
            ip = None
        if ip is not None and (not ip.is_global or ip.is_multicast):
            raise JournalError("SSRF_VIOLATION", "PDF 主机不得是私网或受限地址")
        if parts.hostname not in self.allowed_pdf_hosts:
            raise JournalError("HOST_DENIED", "全文主机未获授权；请手动取得 PDF，或通过 PAPERFLOW_PDF_ALLOWED_HOSTS 显式配置主机")
        return url

    def fetch_pdf(self, location: dict) -> Tuple[bytes, Dict[str, str], str]:
        if not isinstance(location, dict):
            raise JournalError("INVALID_INPUT", "全文位置必须是 FullTextLocation 对象")
        if location.get("is_open") is not True:
            raise JournalError("OPEN_ACCESS_REQUIRED", "仅可下载已明确开放的全文位置；Crossref 候选链接不是开放证据")
        url = self._pdf_url(location.get("url"))
        body, headers, final_url = self._request(url, pdf=True)
        final_url = self._pdf_url(final_url)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"", "application/pdf", "application/octet-stream", "binary/octet-stream"} or not body.startswith(b"%PDF-"):
            raise JournalError("INVALID_PDF", "响应不是 PDF；可能是 HTML 登录页、付费墙或错误页面")
        return body, headers, final_url
