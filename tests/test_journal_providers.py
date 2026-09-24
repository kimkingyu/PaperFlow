"""Unit tests for journal network providers and safe HTTP client."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import pytest

from paperflow.engine.journals import catalog, providers
from paperflow.engine.journals.models import JournalError, ParsedBatch
from paperflow.engine.journals.providers import (
    _parse_retry_after,
    fetch_github_dataset,
    query_easyscholar,
    safe_fetch,
    set_dns_resolver_seam,
    set_monotonic_seam,
    set_sleep_seam,
)


class MockHTTPResponse:
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        self._data = data
        self.status = status
        self.code = status
        self.headers = headers or {}
        self._io = io.BytesIO(data)

    def read(self, amt: int | None = None):
        return self._io.read(amt)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockRead1SlowStreamingResponse:
    """Fails with AssertionError if resp.read() is called; serves small chunks via read1()."""
    def __init__(self, chunk: bytes, clock_advancer):
        self.chunk = chunk
        self.clock_advancer = clock_advancer
        self.status = 200
        self.code = 200
        self.headers = {}
        self.read1_called = 0

    def read(self, amt: int | None = None):
        raise AssertionError("read() was called instead of read1() on streaming response")

    def read1(self, amt: int | None = None):
        self.read1_called += 1
        self.clock_advancer(35.0)  # advance simulated clock by 35s per chunk
        return self.chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSlowStreamingResponse:
    def __init__(self, chunk: bytes, clock_advancer):
        self.chunk = chunk
        self.clock_advancer = clock_advancer
        self.status = 200
        self.code = 200
        self.headers = {}
        self.count = 0

    def read(self, amt: int | None = None):
        self.clock_advancer(30.0)  # advance simulated clock by 30s per read
        self.count += 1
        if self.count > 5:
            return b""
        return self.chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockOpener:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []

    def open(self, req, timeout=15.0):
        self.calls.append((req, timeout))
        if self.handler:
            return self.handler(req, timeout)
        return MockHTTPResponse(b"OK", 200)


@pytest.fixture(autouse=True)
def setup_dns_and_sleep():
    """Default fixture ensuring mock DNS points to public IPs, mock sleep does not delay, clock is reset."""
    set_dns_resolver_seam(lambda host, port: ["1.1.1.1"])
    set_sleep_seam(lambda sec: None)
    set_monotonic_seam(None)
    yield
    set_dns_resolver_seam(None)
    set_sleep_seam(None)
    set_monotonic_seam(None)


# ---------------------------------------------------------
# Test Group 1: URL, Port, and SSRF validation
# ---------------------------------------------------------

def test_safe_fetch_disallows_http():
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("http://api.github.com/test")
    assert exc_info.value.code == "INVALID_URL"


def test_safe_fetch_disallows_userinfo():
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://user:pass@api.github.com/test")
    assert exc_info.value.code == "SSRF_VIOLATION"


def test_safe_fetch_disallows_non_443_port():
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://api.github.com:8080/test")
    assert exc_info.value.code == "SSRF_VIOLATION"


def test_safe_fetch_handles_invalid_port_number():
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://api.github.com:invalid_port/test")
    assert exc_info.value.code == "INVALID_URL"


def test_safe_fetch_disallows_unapproved_hosts():
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://evil.attacker.com/data")
    assert exc_info.value.code == "SSRF_VIOLATION"


def test_safe_fetch_dns_rejects_private_and_loopback_ips():
    # Loopback
    set_dns_resolver_seam(lambda host, port: ["127.0.0.1"])
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://api.github.com/repos/test")
    assert exc_info.value.code == "SSRF_VIOLATION"

    # Private 10.x.x.x
    set_dns_resolver_seam(lambda host, port: ["10.0.0.5"])
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://api.github.com/repos/test")
    assert exc_info.value.code == "SSRF_VIOLATION"

    # Private 192.168.x.x
    set_dns_resolver_seam(lambda host, port: ["192.168.1.1"])
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://api.github.com/repos/test")
    assert exc_info.value.code == "SSRF_VIOLATION"


def test_safe_fetch_content_length_oversize_distinct_from_valueerror():
    # Content-Length says 50MB, max is 1MB
    resp_headers = {"content-length": "52428800"}
    opener = MockOpener(lambda req, timeout: MockHTTPResponse(b"small_chunk", 200, headers=resp_headers))
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://raw.githubusercontent.com/test", max_bytes=1024 * 1024, opener=opener)
    assert exc_info.value.code == "FILE_TOO_LARGE"


def test_safe_fetch_oversize_stream_body():
    large_data = b"X" * (1024 * 1024 + 5)
    opener = MockOpener(lambda req, timeout: MockHTTPResponse(large_data, 200))
    with pytest.raises(JournalError) as exc_info:
        safe_fetch(
            "https://raw.githubusercontent.com/test",
            max_bytes=1024 * 1024,
            opener=opener,
        )
    assert exc_info.value.code == "FILE_TOO_LARGE"


def test_safe_fetch_slow_streaming_triggers_deadline_timeout():
    simulated_now = [1000.0]
    def advance_clock(delta):
        simulated_now[0] += delta

    set_monotonic_seam(lambda: simulated_now[0])
    resp = MockSlowStreamingResponse(b"CHUNK_OF_DATA", advance_clock)
    opener = MockOpener(lambda req, timeout: resp)

    # Total budget is 90s, MockSlowStreamingResponse advances by 30s on each read
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://raw.githubusercontent.com/test", opener=opener)
    assert exc_info.value.code == "TIMEOUT"
    assert "总体时间预算" in str(exc_info.value)


def test_safe_fetch_prefers_read1_and_aborts_on_slow_drip():
    simulated_now = [1000.0]
    def advance_clock(delta):
        simulated_now[0] += delta

    set_monotonic_seam(lambda: simulated_now[0])
    resp = MockRead1SlowStreamingResponse(b"A", advance_clock)
    opener = MockOpener(lambda req, timeout: resp)

    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://raw.githubusercontent.com/test", opener=opener)
    assert exc_info.value.code == "TIMEOUT"
    assert resp.read1_called >= 2


def test_safe_fetch_eof_checks_deadline_before_returning():
    simulated_now = [1000.0]

    class MockEOFExpireResponse:
        def __init__(self):
            self.status = 200
            self.code = 200
            self.headers = {}
            self.first = True

        def read1(self, amt: int | None = None):
            if self.first:
                self.first = False
                return b"first_chunk"
            # On EOF read, clock expires past deadline
            simulated_now[0] += 100.0
            return b""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    set_monotonic_seam(lambda: simulated_now[0])
    opener = MockOpener(lambda req, timeout: MockEOFExpireResponse())

    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://raw.githubusercontent.com/test", opener=opener)
    assert exc_info.value.code == "TIMEOUT"


# ---------------------------------------------------------
# Test Group 2: Retry-After, Date Parsing, and Redirect Security
# ---------------------------------------------------------

def test_parse_retry_after_numeric_and_http_date():
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after("-10") is None
    assert _parse_retry_after("NaN") is None
    assert _parse_retry_after("Infinity") is None
    assert _parse_retry_after("") is None

    # HTTP-date parse test: a date in future
    future_date_str = "Wed, 21 Oct 2099 07:28:00 GMT"
    diff = _parse_retry_after(future_date_str)
    assert diff is not None and diff > 0


def test_safe_fetch_rate_limited_retry_after_over_budget():
    def rl_handler(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=429,
            msg="Too Many Requests",
            hdrs={"retry-after": "120"},
            fp=None,
        )

    opener = MockOpener(rl_handler)
    with pytest.raises(JournalError) as exc_info:
        safe_fetch("https://easyscholar.cc/test", opener=opener)
    assert exc_info.value.code == "RATE_LIMITED"


def test_safe_fetch_redirect_forbids_cross_host_when_disabled():
    def redirect_handler(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=302,
            msg="Found",
            hdrs={"location": "https://raw.githubusercontent.com/test"},
            fp=None,
        )

    opener = MockOpener(redirect_handler)
    with pytest.raises(JournalError) as exc_info:
        safe_fetch(
            "https://api.github.com/test",
            allow_cross_host_redirect=False,
            opener=opener,
        )
    assert exc_info.value.code == "SSRF_VIOLATION"


def test_safe_fetch_redirect_strips_auth_and_blocks_sensitive_query():
    # If URL contains secretKey and attempts cross-host redirect -> forbidden
    def redirect_handler(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=302,
            msg="Found",
            hdrs={"location": "https://raw.githubusercontent.com/test"},
            fp=None,
        )

    opener = MockOpener(redirect_handler)
    with pytest.raises(JournalError) as exc_info:
        safe_fetch(
            "https://api.github.com/test?secretKey=mysecret",
            allow_cross_host_redirect=True,
            opener=opener,
        )
    assert exc_info.value.code == "SSRF_VIOLATION"
    assert "敏感凭据参数" in str(exc_info.value)


# ---------------------------------------------------------
# Test Group 3: fetch_github_dataset & Shared Deadline
# ---------------------------------------------------------

def test_fetch_github_dataset_dry_run_zero_network():
    res = fetch_github_dataset(
        source_id="showjcr",
        dataset_id="FQBJCR2025-UTF8.csv",
        dry_run=True,
    )
    assert res["source_id"] == "showjcr"
    assert res["dataset_id"] == "FQBJCR2025-UTF8.csv"
    assert res["filename"] == "FQBJCR2025-UTF8.csv"
    assert res["content"] == b""
    assert "dry_run 模式" in res["warnings"][0]
    assert "plan" in res
    assert res["plan"]["commit_api_url"] == "https://api.github.com/repos/hitfyd/ShowJCR/commits/master"


def test_fetch_github_dataset_path_traversal_rejection():
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "../etc/passwd", dry_run=True)
    assert exc_info.value.code == "INVALID_INPUT"

    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "subdir/file.csv", dry_run=True)
    assert exc_info.value.code == "INVALID_INPUT"


def test_fetch_github_dataset_regex_mismatch_rejection():
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "malicious_file.exe", dry_run=True)
    assert exc_info.value.code == "INVALID_INPUT"
    assert "命名正则" in str(exc_info.value)


def test_fetch_github_dataset_permission_gate(monkeypatch):
    monkeypatch.delenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", raising=False)
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "FQBJCR2025-UTF8.csv", dry_run=False)
    assert exc_info.value.code == "LICENSE_REVIEW_REQUIRED"


def test_fetch_github_dataset_actual_download_success(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "showjcr")

    sha_40 = "a" * 40
    csv_bytes = "刊名,ISSN,大类\nScience,1234-5678,综合\n".encode("utf-8")

    def mock_handler(req, timeout):
        url = req.full_url
        if "api.github.com" in url:
            return MockHTTPResponse(json.dumps({"sha": sha_40}).encode("utf-8"), 200)
        elif "raw.githubusercontent.com" in url:
            return MockHTTPResponse(csv_bytes, 200, headers={"etag": '"etag123"'})
        raise ValueError("Unexpected URL: " + url)

    opener = MockOpener(mock_handler)
    res = fetch_github_dataset("showjcr", "FQBJCR2025-UTF8.csv", dry_run=False, _opener=opener)

    assert res["source_id"] == "showjcr"
    assert res["version"] == sha_40
    assert res["content"] == csv_bytes
    assert res["etag"] == '"etag123"'
    assert res["not_modified"] is False
    assert len(res["checksum"]) == 64
    assert len(opener.calls) == 2


def test_fetch_github_dataset_etag_if_none_match_304(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "showjcr")
    sha_40 = "d" * 40

    def mock_handler(req, timeout):
        url = req.full_url
        if "api.github.com" in url:
            return MockHTTPResponse(json.dumps({"sha": sha_40}).encode("utf-8"), 200)
        elif "raw.githubusercontent.com" in url:
            assert req.headers.get("If-none-match") == '"etag123"' or req.headers.get("If-None-Match") == '"etag123"'
            return MockHTTPResponse(b"", 304, headers={"etag": '"etag123"'})
        raise ValueError("Unexpected URL")

    opener = MockOpener(mock_handler)
    res = fetch_github_dataset(
        "showjcr",
        "FQBJCR2025-UTF8.csv",
        dry_run=False,
        if_none_match='"etag123"',
        _opener=opener,
    )

    assert res["not_modified"] is True
    assert res["content"] == b""
    assert res["etag"] == '"etag123"'


def test_fetch_github_dataset_shared_deadline_expires_in_second_call(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "showjcr")
    sha_40 = "e" * 40

    simulated_now = [5000.0]
    set_monotonic_seam(lambda: simulated_now[0])

    def mock_handler(req, timeout):
        url = req.full_url
        if "api.github.com" in url:
            simulated_now[0] += 95.0  # Elapse 95 seconds during first call
            return MockHTTPResponse(json.dumps({"sha": sha_40}).encode("utf-8"), 200)
        return MockHTTPResponse(b"OK", 200)

    opener = MockOpener(mock_handler)
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "FQBJCR2025-UTF8.csv", dry_run=False, _opener=opener)
    assert exc_info.value.code == "TIMEOUT"


def test_fetch_github_dataset_empty_content_raises_schema_changed(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "showjcr")
    sha_40 = "b" * 40

    def mock_handler(req, timeout):
        url = req.full_url
        if "api.github.com" in url:
            return MockHTTPResponse(json.dumps({"sha": sha_40}).encode("utf-8"), 200)
        return MockHTTPResponse(b"   \n", 200)

    opener = MockOpener(mock_handler)
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "FQBJCR2025-UTF8.csv", dry_run=False, _opener=opener)
    assert exc_info.value.code == "SCHEMA_CHANGED"


def test_fetch_github_dataset_html_error_page_raises_schema_changed(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_ALLOWED_SOURCES", "showjcr")
    sha_40 = "c" * 40
    html_page = b"<!DOCTYPE html><html><body>404 Not Found</body></html>"

    def mock_handler(req, timeout):
        url = req.full_url
        if "api.github.com" in url:
            return MockHTTPResponse(json.dumps({"sha": sha_40}).encode("utf-8"), 200)
        return MockHTTPResponse(html_page, 200)

    opener = MockOpener(mock_handler)
    with pytest.raises(JournalError) as exc_info:
        fetch_github_dataset("showjcr", "FQBJCR2025-UTF8.csv", dry_run=False, _opener=opener)
    assert exc_info.value.code == "SCHEMA_CHANGED"


# ---------------------------------------------------------
# Test Group 4: query_easyscholar
# ---------------------------------------------------------

def test_query_easyscholar_missing_key_raises_auth_required(monkeypatch):
    monkeypatch.delenv("PAPERFLOW_EASYSCHOLAR_KEY", raising=False)
    with pytest.raises(JournalError) as exc_info:
        query_easyscholar("Nature")
    assert exc_info.value.code == "AUTH_REQUIRED"


def test_query_easyscholar_empty_title_raises_invalid_input(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_EASYSCHOLAR_KEY", "test_key_123")
    with pytest.raises(JournalError) as exc_info:
        query_easyscholar("   ")
    assert exc_info.value.code == "INVALID_INPUT"


def test_query_easyscholar_success_and_redaction(monkeypatch):
    secret = "secret_key_abcdef123456"
    monkeypatch.setenv("PAPERFLOW_EASYSCHOLAR_KEY", secret)

    sample_resp = {
        "code": 200,
        "msg": "success",
        "data": {
            "officialRank": {
                "all": {
                    "sci": "1区",
                    "sciUp": "1区Top",
                    "sciif": "50.5",
                    "sciif5": "55.2",
                    "ccf": "A",
                }
            }
        },
    }

    opener = MockOpener(lambda req, timeout: MockHTTPResponse(json.dumps(sample_resp).encode("utf-8"), 200))
    batch: ParsedBatch = query_easyscholar("Nature", _opener=opener)

    assert len(batch.records) == 1
    rec = batch.records[0]
    assert rec.title == "Nature"
    assert rec.provenance.source_id == "easyscholar_api"
    assert rec.provenance.authority == "community"
    assert rec.provenance.data_year is None
    assert rec.provenance.observed_at is None

    # Base URL must NEVER contain secret key
    assert secret not in rec.provenance.source_url
    assert rec.provenance.source_url == "https://easyscholar.cc/open/getPublicationRank"

    # Rankings verification
    cas_rank = next(r for r in rec.rankings if r.system == "cas")
    assert cas_rank.quartile == 1
    assert cas_rank.top is True
    assert cas_rank.year is None

    ccf_rank = next(r for r in rec.rankings if r.system == "ccf")
    assert ccf_rank.grade == "A"
    assert ccf_rank.year is None

    # Metrics verification
    if_metric = next(m for m in rec.metrics if m.name == "impact_factor")
    assert if_metric.value == 50.5
    assert if_metric.year is None


def test_query_easyscholar_401_auth_error_sanitizes_secret(monkeypatch):
    secret = "super_secret_999"
    monkeypatch.setenv("PAPERFLOW_EASYSCHOLAR_KEY", secret)

    def auth_err_handler(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    opener = MockOpener(auth_err_handler)
    with pytest.raises(JournalError) as exc_info:
        query_easyscholar("IEEE Transactions on Software Engineering", _opener=opener)

    assert exc_info.value.code == "AUTH_REQUIRED"
    assert secret not in str(exc_info.value)


def test_query_easyscholar_schema_changed(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_EASYSCHOLAR_KEY", "dummy_key")

    bad_resp = {
        "code": 200,
        "data": {
            "officialRank": {
                "all": ["sci1"]
            }
        },
    }
    opener = MockOpener(lambda req, timeout: MockHTTPResponse(json.dumps(bad_resp).encode("utf-8"), 200))
    with pytest.raises(JournalError) as exc_info:
        query_easyscholar("Science", _opener=opener)
    assert exc_info.value.code == "SCHEMA_CHANGED"


def test_query_easyscholar_not_found(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_EASYSCHOLAR_KEY", "dummy_key")

    resp_empty = {
        "code": 200,
        "data": {
            "officialRank": {
                "all": {}
            }
        },
    }
    opener = MockOpener(lambda req, timeout: MockHTTPResponse(json.dumps(resp_empty).encode("utf-8"), 200))
    with pytest.raises(JournalError) as exc_info:
        query_easyscholar("Unknown Nonexistent Journal", _opener=opener)
    assert exc_info.value.code == "NOT_FOUND"
