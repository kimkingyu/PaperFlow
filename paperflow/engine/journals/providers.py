"""Outbound HTTP client and network providers for external journal datasets and APIs.

Only permitted external hostnames:
- api.github.com
- raw.githubusercontent.com
- easyscholar.cc

All network accesses enforce strict SSRF guards, DNS resolution validation (no loopback/private/link-local IPs),
HTTPS port 443 only, redirect controls, credential masking, timeout budgets, and retry semantics.
"""
from __future__ import annotations

import email.utils
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from paperflow.engine.journals import catalog
from paperflow.engine.journals.models import (
    JournalError,
    JournalRecord,
    Metric,
    ParsedBatch,
    Provenance,
    Ranking,
)

ALLOWED_HOSTS = {
    "api.github.com",
    "raw.githubusercontent.com",
    "easyscholar.cc",
}

MAX_DOWNLOAD_SIZE_BYTES = 32 * 1024 * 1024  # 32 MiB
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3
MAX_RETRIES = 2
TOTAL_TIME_BUDGET_SECONDS = 90.0
HEX_40_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Test seams: injectable DNS resolver, sleep, and clock for isolated unit tests
_DNS_RESOLVER: Callable[[str, int], List[str]] = lambda host, port: [
    addr[4][0]
    for addr in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
]
_SLEEP_FN: Callable[[float], None] = time.sleep
_MONOTONIC_FN: Callable[[], float] = time.monotonic


def set_dns_resolver_seam(fn: Optional[Callable[[str, int], List[str]]]) -> None:
    global _DNS_RESOLVER
    if fn is None:
        _DNS_RESOLVER = lambda host, port: [
            addr[4][0]
            for addr in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ]
    else:
        _DNS_RESOLVER = fn


def set_sleep_seam(fn: Optional[Callable[[float], None]]) -> None:
    global _SLEEP_FN
    if fn is None:
        _SLEEP_FN = time.sleep
    else:
        _SLEEP_FN = fn


def set_monotonic_seam(fn: Optional[Callable[[], float]]) -> None:
    global _MONOTONIC_FN
    if fn is None:
        _MONOTONIC_FN = time.monotonic
    else:
        _MONOTONIC_FN = fn


def _mask_url_for_error(url: str) -> str:
    """Mask credentials, secrets, query parameters, or userinfo from error messages."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or "unknown"
        port_part = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        return f"{parsed.scheme}://{hostname}{port_part}{parsed.path}"
    except Exception:
        return "https://[masked-url]"


def _update_resp_socket_timeout(resp: Any, timeout: float) -> bool:
    """Update the underlying socket timeout on an HTTPResponse before each read call."""
    for path in (
        ("fp", "raw", "_sock"),
        ("fp", "_sock"),
        ("_sock",),
    ):
        obj = resp
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                break
        if obj is not None and hasattr(obj, "settimeout"):
            try:
                obj.settimeout(timeout)
                return True
            except Exception:
                pass
    return False


def _parse_retry_after(raw_val: Optional[str]) -> Optional[float]:
    """Parse Retry-After header into seconds.

    Supports integer/float seconds as well as RFC 7231 / HTTP-date strings.
    Rejects negative, NaN, or infinite values.
    """
    if not raw_val:
        return None
    val = raw_val.strip()
    if not val:
        return None

    # Try numeric seconds first
    try:
        sec = float(val)
        if math.isnan(sec) or math.isinf(sec) or sec < 0:
            return None
        return sec
    except ValueError:
        pass

    # Try HTTP-date format (RFC 7231 / RFC 2822 / RFC 850)
    try:
        parsed_tuple = email.utils.parsedate_to_datetime(val)
        if parsed_tuple is not None:
            now_epoch = time.time()
            target_epoch = parsed_tuple.timestamp()
            diff = target_epoch - now_epoch
            if math.isnan(diff) or math.isinf(diff) or diff < 0:
                return 0.0
            return diff
    except Exception:
        pass

    return None


def _validate_safe_url(url: str, expected_host: Optional[str] = None) -> urllib.parse.SplitResult:
    """Validate that the URL satisfies all SSRF constraints before any network request."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        raise JournalError("INVALID_URL", "URL 无法正确解析")

    if parsed.scheme.lower() != "https":
        raise JournalError("INVALID_URL", f"仅支持 HTTPS 请求，拒绝非 HTTPS 协议")

    if parsed.username or parsed.password:
        raise JournalError("SSRF_VIOLATION", "禁止在 URL 中包含 userinfo/凭据")

    try:
        port = parsed.port
    except ValueError:
        raise JournalError("INVALID_URL", "URL 包含非法的端口号")

    if port is not None and port != 443:
        raise JournalError("SSRF_VIOLATION", f"仅允许 443 端口，拒绝非 443 端口访问")

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise JournalError("INVALID_URL", "URL 缺少有效主机名")

    if expected_host and hostname != expected_host.lower():
        raise JournalError("SSRF_VIOLATION", f"目标主机必须为预期主机，实际请求被拒绝")

    if hostname not in ALLOWED_HOSTS:
        raise JournalError("SSRF_VIOLATION", f"目标主机未在安全白名单中")

    # DNS check: resolve hostname and ensure no resolved address is private/loopback/link-local/multicast
    try:
        resolved_ips = _DNS_RESOLVER(hostname, 443)
    except Exception:
        raise JournalError("NETWORK_ERROR", "域名 DNS 解析失败")

    if not resolved_ips:
        raise JournalError("NETWORK_ERROR", "域名未解析到有效 IP 地址")

    for ip_str in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise JournalError("SSRF_VIOLATION", "DNS 返回非合规 IP 地址")

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise JournalError("SSRF_VIOLATION", "DNS 解析命中私网或受限保留地址段")

    return parsed


def safe_fetch(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    expected_host: Optional[str] = None,
    max_bytes: int = MAX_DOWNLOAD_SIZE_BYTES,
    allow_cross_host_redirect: bool = False,
    opener: Optional[Any] = None,
    deadline: Optional[float] = None,
) -> Tuple[bytes, Dict[str, str], str]:
    """Execute a guarded HTTP GET request using urllib standard library.

    Args:
        url: Target HTTPS URL.
        headers: Optional dictionary of HTTP request headers.
        expected_host: Optional strict host validation.
        max_bytes: Upper cap on downloaded body.
        allow_cross_host_redirect: Whether cross-host redirection is allowed.
        opener: Optional urllib opener test seam.
        deadline: Monotonic deadline (time.monotonic() timestamp). If None, defaults to now + 90s.

    Returns:
        (content_bytes, response_headers, final_url)
    """
    if deadline is None:
        deadline = _MONOTONIC_FN() + TOTAL_TIME_BUDGET_SECONDS

    current_url = url
    headers_dict = dict(headers or {})
    redirect_count = 0
    retries = 0

    while True:
        now = _MONOTONIC_FN()
        remaining_budget = deadline - now
        if remaining_budget <= 0:
            raise JournalError("TIMEOUT", "请求超过总体时间预算 (90秒)")

        # Step 1: SSRF pre-check
        _validate_safe_url(current_url, expected_host=expected_host if redirect_count == 0 else None)
        parsed_current = urllib.parse.urlsplit(current_url)

        # Refresh remaining budget after DNS validation before opening connection
        now = _MONOTONIC_FN()
        remaining_budget = deadline - now
        if remaining_budget <= 0:
            raise JournalError("TIMEOUT", "请求超过总体时间预算 (90秒)")

        # Step 2: Build request
        req = urllib.request.Request(current_url, headers=headers_dict, method="GET")

        # Create no-redirect opener to manually inspect and validate each hop
        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        actual_opener = opener or urllib.request.build_opener(_NoRedirectHandler)

        try:
            step_timeout = min(REQUEST_TIMEOUT_SECONDS, max(remaining_budget, 0.1))
            with actual_opener.open(req, timeout=step_timeout) as resp:
                status_code = resp.status if hasattr(resp, "status") else resp.code
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}

                # Handle 304 Not Modified if ETag/If-None-Match was provided
                if status_code == 304:
                    return b"", resp_headers, current_url

                # Check Content-Length if present: parse and check separately to avoid masking JournalError
                cl_header = resp_headers.get("content-length")
                parsed_cl: Optional[int] = None
                if cl_header is not None:
                    try:
                        parsed_cl = int(cl_header.strip())
                    except ValueError:
                        parsed_cl = None

                if parsed_cl is not None and parsed_cl > max_bytes:
                    raise JournalError("FILE_TOO_LARGE", f"响应声明的文件大小 ({parsed_cl}) 超过上限 {max_bytes} 字节")

                # Read response chunks with strict upper limit and deadline check at every chunk
                chunks = []
                total_read = 0
                chunk_size = 64 * 1024
                read_fn = getattr(resp, "read1", None) or resp.read
                while True:
                    rem = deadline - _MONOTONIC_FN()
                    if rem <= 0:
                        raise JournalError("TIMEOUT", "流式读取响应数据超过总体时间预算")

                    _update_resp_socket_timeout(resp, min(REQUEST_TIMEOUT_SECONDS, max(rem, 0.01)))

                    remaining_cap = max_bytes - total_read + 1
                    data = read_fn(min(chunk_size, remaining_cap))
                    if _MONOTONIC_FN() >= deadline:
                        raise JournalError("TIMEOUT", "流式读取响应数据超过总体时间预算")
                    if not data:
                        break

                    total_read += len(data)
                    if total_read > max_bytes:
                        raise JournalError("FILE_TOO_LARGE", f"文件下载实际大小超过 {max_bytes} 字节上限")
                    chunks.append(data)

                return b"".join(chunks), resp_headers, current_url

        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            try:
                e.close()
            except Exception:
                pass

            # Handle 304 Not Modified
            if status == 304:
                return b"", resp_headers, current_url

            # Handle redirects (301, 302, 303, 307, 308)
            if status in (301, 302, 303, 307, 308):
                location = resp_headers.get("location")
                if not location:
                    raise JournalError("NETWORK_ERROR", f"重定向状态码 {status} 缺少 Location 响应头")

                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    raise JournalError("NETWORK_ERROR", f"重定向次数超过上限 ({MAX_REDIRECTS})")

                new_url = urllib.parse.urljoin(current_url, location)
                new_parsed = urllib.parse.urlsplit(new_url)

                if new_parsed.hostname != parsed_current.hostname:
                    if not allow_cross_host_redirect:
                        raise JournalError("SSRF_VIOLATION", "拒绝未授权的跨主机重定向")

                    # Check for secret parameters in current URL; forbid cross-host redirect if credentials present in query
                    curr_query = parsed_current.query.lower()
                    if any(s in curr_query for s in ("secretkey", "token", "password", "key=")):
                        raise JournalError("SSRF_VIOLATION", "禁止携带敏感凭据参数进行跨主机重定向")

                    # Case-insensitively strip authorization / credential headers
                    keys_to_remove = [
                        k for k in headers_dict.keys()
                        if k.lower() in ("authorization", "secretkey", "x-api-key", "cookie", "token")
                    ]
                    for k in keys_to_remove:
                        headers_dict.pop(k, None)

                # Validate new destination URL
                _validate_safe_url(new_url)
                current_url = new_url
                continue

            # Handle 401 / 403
            if status in (401, 403):
                raise JournalError("AUTH_REQUIRED", f"目标服务认证失败或拒绝访问 (HTTP {status})")

            # Handle 429
            if status == 429:
                parsed_wait = _parse_retry_after(resp_headers.get("retry-after"))
                if parsed_wait is None:
                    parsed_wait = 5.0

                now_mono = _MONOTONIC_FN()
                rem = deadline - now_mono
                if parsed_wait > rem:
                    raise JournalError("RATE_LIMITED", f"服务限流 (429)，Retry-After ({parsed_wait}s) 超过总体时间预算")

                if retries < MAX_RETRIES and (now_mono + parsed_wait < deadline):
                    retries += 1
                    _SLEEP_FN(parsed_wait)
                    continue
                raise JournalError("RATE_LIMITED", "服务限流 (429)")

            # Handle 5xx retryable errors
            if status in (500, 502, 503, 504):
                if retries < MAX_RETRIES:
                    retries += 1
                    backoff = 1.0 * (2 ** (retries - 1))
                    if _MONOTONIC_FN() + backoff < deadline:
                        _SLEEP_FN(backoff)
                        continue
                raise JournalError("NETWORK_ERROR", f"上游服务器错误 (HTTP {status})")

            # Other 4xx
            raise JournalError("NETWORK_ERROR", f"HTTP 请求失败 (状态码 {status})")

        except JournalError:
            raise
        except socket.timeout:
            if retries < MAX_RETRIES:
                retries += 1
                if _MONOTONIC_FN() + 1.0 < deadline:
                    _SLEEP_FN(1.0)
                    continue
            raise JournalError("TIMEOUT", "请求网络超时")
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                if retries < MAX_RETRIES:
                    retries += 1
                    if _MONOTONIC_FN() + 1.0 < deadline:
                        _SLEEP_FN(1.0)
                        continue
                raise JournalError("TIMEOUT", "网络连接超时")
            raise JournalError("NETWORK_ERROR", "网络请求连接失败")
        except Exception:
            raise JournalError("NETWORK_ERROR", "网络请求发生未知异常")


def fetch_github_dataset(
    source_id: str,
    dataset_id: str,
    dry_run: bool = True,
    if_none_match: Optional[str] = None,
    _opener: Optional[Any] = None,
) -> Dict[str, Any]:
    """Fetch an authorized raw dataset from GitHub, or generate a dry-run plan.

    Args:
        source_id: The source identifier registered in source_catalog.json.
        dataset_id: Target filename to download.
        dry_run: If True, returns execution plan without network I/O or disk writes.
        if_none_match: Optional ETag for conditional GET.
        _opener: Test seam for injecting mocked urllib opener.

    Returns:
        Dict with keys: source_id, dataset_id, version, filename, source_url, checksum, content, warnings, etag, not_modified.
    """
    source = catalog.get_source(source_id)

    repo = source.get("repo", "")
    ref = source.get("ref", "")
    prefix = source.get("prefix", "")
    file_pattern = source.get("file_pattern")

    if not repo or not ref:
        raise JournalError("INVALID_INPUT", f"数据源 {source_id} 缺少 repo 或 ref 配置")

    if file_pattern is None:
        raise JournalError("INVALID_INPUT", f"数据源 {source_id} 未配置 file_pattern，不支持下载")

    # Filename validation: prevent directory traversal and path manipulation
    if ".." in dataset_id or "/" in dataset_id or "\\" in dataset_id:
        raise JournalError("INVALID_INPUT", "文件名包含非法路径字符或路径穿越符号")

    # Match against catalog file_pattern
    pattern_re = re.compile(file_pattern)
    if not pattern_re.fullmatch(dataset_id):
        raise JournalError(
            "INVALID_INPUT",
            f"文件名 '{dataset_id}' 不符合数据源登记的命名正则 '{file_pattern}'"
        )

    # Dry-run handling: does zero network I/O
    if dry_run:
        full_path = f"{prefix}{dataset_id}"
        expected_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{urllib.parse.quote(full_path)}"
        return {
            "source_id": source_id,
            "dataset_id": dataset_id,
            "version": source.get("verified_commit") or ref,
            "filename": dataset_id,
            "source_url": expected_url,
            "checksum": "",
            "content": b"",
            "warnings": [
                "dry_run 模式：未执行网络请求",
                "数据条款需用户单独确认，下载授权不代表条款自动免除",
            ],
            "plan": {
                "commit_api_url": f"https://api.github.com/repos/{repo}/commits/{ref}",
                "raw_file_url": expected_url,
                "max_bytes": MAX_DOWNLOAD_SIZE_BYTES,
            },
            "etag": None,
            "not_modified": False,
        }

    # User authorization check
    if not source.get("download_permitted_by_user", False):
        raise JournalError(
            "LICENSE_REVIEW_REQUIRED",
            f"数据源 {source_id} 尚未获得用户环境授权 (需在 PAPERFLOW_JOURNAL_ALLOWED_SOURCES 显式包含)"
        )

    # Establish a shared operation deadline for both calls (total 90s across entire operation)
    op_deadline = _MONOTONIC_FN() + TOTAL_TIME_BUDGET_SECONDS

    # Step 1: Resolve commit SHA via GitHub API: api.github.com/repos/{repo}/commits/{ref}
    commit_api_url = f"https://api.github.com/repos/{repo}/commits/{ref}"
    api_headers = {
        "User-Agent": "PaperFlow-Journal-Provider/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    commit_bytes, _, _ = safe_fetch(
        commit_api_url,
        headers=api_headers,
        expected_host="api.github.com",
        max_bytes=1024 * 1024,
        allow_cross_host_redirect=False,
        opener=_opener,
        deadline=op_deadline,
    )

    try:
        commit_json = json.loads(commit_bytes.decode("utf-8"))
    except Exception:
        raise JournalError("SCHEMA_CHANGED", "GitHub Commit API 返回非合规 JSON 响应")

    if not isinstance(commit_json, dict) or "sha" not in commit_json:
        raise JournalError("SCHEMA_CHANGED", "GitHub Commit API 响应缺少 sha 字段")

    commit_sha = commit_json["sha"]
    if not isinstance(commit_sha, str) or not HEX_40_RE.fullmatch(commit_sha):
        raise JournalError("SCHEMA_CHANGED", "GitHub 返回的 sha 非 40 字符十六进制哈希")

    # Step 2: Download raw file from raw.githubusercontent.com/{repo}/{sha}/{quote(prefix+filename)}
    raw_path_part = urllib.parse.quote(f"{prefix}{dataset_id}")
    raw_file_url = f"https://raw.githubusercontent.com/{repo}/{commit_sha}/{raw_path_part}"
    raw_headers = {
        "User-Agent": "PaperFlow-Journal-Provider/1.0",
    }
    if if_none_match:
        raw_headers["If-None-Match"] = if_none_match

    content_bytes, raw_resp_headers, _ = safe_fetch(
        raw_file_url,
        headers=raw_headers,
        expected_host="raw.githubusercontent.com",
        max_bytes=MAX_DOWNLOAD_SIZE_BYTES,
        allow_cross_host_redirect=False,
        opener=_opener,
        deadline=op_deadline,
    )

    resp_etag = raw_resp_headers.get("etag")

    # Conditional GET support: 304 Not Modified
    if not content_bytes and if_none_match and resp_etag == if_none_match:
        return {
            "source_id": source_id,
            "dataset_id": dataset_id,
            "version": commit_sha,
            "filename": dataset_id,
            "source_url": raw_file_url,
            "checksum": "",
            "content": b"",
            "warnings": [],
            "etag": resp_etag,
            "not_modified": True,
        }

    # Empty content check
    if not content_bytes or len(content_bytes.strip()) == 0:
        raise JournalError("SCHEMA_CHANGED", "下载的文件内容为空")

    # HTML error page detection
    preview = content_bytes[:512].decode("utf-8", errors="ignore").lower()
    if "<!doctype html" in preview or "<html" in preview:
        raise JournalError("SCHEMA_CHANGED", "下载的文件返回了 HTML 错误页面而非原始数据")

    checksum = hashlib.sha256(content_bytes).hexdigest()

    return {
        "source_id": source_id,
        "dataset_id": dataset_id,
        "version": commit_sha,
        "filename": dataset_id,
        "source_url": raw_file_url,
        "checksum": checksum,
        "content": content_bytes,
        "warnings": [],
        "etag": resp_etag,
        "not_modified": False,
    }


def query_easyscholar(
    title: str,
    _opener: Optional[Any] = None,
) -> ParsedBatch:
    """Query easyScholar open API for journal publication ranks.

    Reads secretKey exclusively from the PAPERFLOW_EASYSCHOLAR_KEY environment variable.
    All outputs strictly strip the API key from source_url and errors.
    Unknown observation years remain None and are not fabricated.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise JournalError("INVALID_INPUT", "期刊或文献名称不能为空")

    api_key = os.environ.get("PAPERFLOW_EASYSCHOLAR_KEY", "").strip()
    if not api_key:
        raise JournalError("AUTH_REQUIRED", "未配置 easyScholar Key，请在环境变量 PAPERFLOW_EASYSCHOLAR_KEY 中提供")

    clean_base_url = "https://easyscholar.cc/open/getPublicationRank"

    query_params = urllib.parse.urlencode({
        "publicationName": clean_title,
        "secretKey": api_key,
    })
    target_url = f"{clean_base_url}?{query_params}"

    headers = {
        "User-Agent": "PaperFlow-Journal-Provider/1.0",
        "Accept": "application/json",
    }

    raw_bytes, _, _ = safe_fetch(
        target_url,
        headers=headers,
        expected_host="easyscholar.cc",
        max_bytes=2 * 1024 * 1024,
        allow_cross_host_redirect=False,
        opener=_opener,
    )

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        raise JournalError("SCHEMA_CHANGED", "easyScholar 返回无法解析的 JSON 数据")

    if not isinstance(body, dict):
        raise JournalError("SCHEMA_CHANGED", "easyScholar 返回非字典格式的根数据结构")

    code = body.get("code")
    if code is not None and code != 200:
        msg = str(body.get("msg") or body.get("message") or "")
        if code in (401, 403) or "key" in msg.lower() or "auth" in msg.lower():
            raise JournalError("AUTH_REQUIRED", "easyScholar 认证失败或密钥无效")
        if code == 429:
            raise JournalError("RATE_LIMITED", "easyScholar 接口调用超限")
        raise JournalError("SCHEMA_CHANGED", f"easyScholar 返回非成功状态码: {code}")

    data = body.get("data")
    if data is None:
        raise JournalError("NOT_FOUND", f"easyScholar 未检索到期刊 '{clean_title}' 的数据")

    if not isinstance(data, dict):
        raise JournalError("SCHEMA_CHANGED", "easyScholar 响应中的 data 字段非字典结构")

    official_rank = data.get("officialRank")
    if not isinstance(official_rank, dict):
        raise JournalError("SCHEMA_CHANGED", "easyScholar 响应缺少 officialRank 字典结构")

    all_ranks = official_rank.get("all")
    if not isinstance(all_ranks, dict):
        raise JournalError("SCHEMA_CHANGED", "easyScholar 响应中的 officialRank.all 字段必须为映射对象")

    if not all_ranks:
        raise JournalError("NOT_FOUND", f"easyScholar 未找到 '{clean_title}' 的分区或指标信息")

    prov = Provenance(
        source_id="easyscholar_api",
        source_url=clean_base_url,
        authority="community",
        freshness="dynamic_api",
        data_year=None,
        observed_at=None,
    )

    rankings: List[Ranking] = []
    metrics: List[Metric] = []

    # Map CCF rank
    ccf_val = all_ranks.get("ccf")
    if ccf_val and isinstance(ccf_val, str) and ccf_val.strip():
        grade_match = re.search(r"[ABC]", ccf_val.strip().upper())
        grade = grade_match.group(0) if grade_match else ccf_val.strip()
        rankings.append(
            Ranking(
                system="ccf",
                year=None,
                grade=grade,
                raw=str(ccf_val),
                provenance=prov,
            )
        )

    # Map SCI / CAS quartile
    sci_up = all_ranks.get("sciUp") or all_ranks.get("sci")
    if sci_up and isinstance(sci_up, str) and sci_up.strip():
        quartile = None
        q_match = re.search(r"([1-4])\s*区", sci_up)
        if q_match:
            quartile = int(q_match.group(1))
        elif sci_up.strip().upper() in ("Q1", "1"):
            quartile = 1
        elif sci_up.strip().upper() in ("Q2", "2"):
            quartile = 2
        elif sci_up.strip().upper() in ("Q3", "3"):
            quartile = 3
        elif sci_up.strip().upper() in ("Q4", "4"):
            quartile = 4

        is_top = True if ("top" in sci_up.lower() or "TOP" in sci_up) else None

        rankings.append(
            Ranking(
                system="cas",
                year=None,
                quartile=quartile,
                top=is_top,
                raw=str(sci_up),
                provenance=prov,
            )
        )

    # Map Impact Factor
    sciif = all_ranks.get("sciif")
    if sciif is not None:
        try:
            val = float(str(sciif).strip())
            metrics.append(
                Metric(
                    name="impact_factor",
                    raw=str(sciif),
                    value=val,
                    comparator="eq",
                    year=None,
                    provenance=prov,
                )
            )
        except (ValueError, TypeError):
            pass

    # Map 5-year Impact Factor
    sciif5 = all_ranks.get("sciif5")
    if sciif5 is not None:
        try:
            val5 = float(str(sciif5).strip())
            metrics.append(
                Metric(
                    name="impact_factor_5yr",
                    raw=str(sciif5),
                    value=val5,
                    comparator="eq",
                    year=None,
                    provenance=prov,
                )
            )
        except (ValueError, TypeError):
            pass

    record = JournalRecord(
        journal_id=f"easyscholar:{clean_title.lower()}",
        title=clean_title,
        provenance=prov,
        rankings=rankings,
        metrics=metrics,
    )

    return ParsedBatch(
        records=[record],
        rejected=[],
        total_rows=1,
        rejected_count=0,
        warnings=[],
        coverage={"provider": "easyscholar_api", "official_clearance": False},
    )
