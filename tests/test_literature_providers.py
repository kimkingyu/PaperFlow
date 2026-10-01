"""Offline public-provider fixtures: never contact external APIs or repositories."""
from __future__ import annotations

import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from paperflow.engine.journals import providers as http
from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature import providers
from paperflow.engine.literature.models import PaperRecord, paper_id_for, public_url
from paperflow.engine.literature.providers import LiteratureProviders, normalize_arxiv_id, normalize_doi


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return json.dumps(response).encode(), {"Content-Type": "application/json"}, url
        if isinstance(response, bytes):
            return response, {"Content-Type": "application/atom+xml"}, url
        return response


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def offline_policy(monkeypatch):
    for key in ("PAPERFLOW_UNPAYWALL_EMAIL", "PAPERFLOW_CONTACT_EMAIL", "PAPERFLOW_PDF_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    clock = Clock()
    providers.set_arxiv_timing_seams(clock, clock.sleep)
    http.set_dns_resolver_seam(lambda host, port: ["1.1.1.1"])
    http.set_monotonic_seam(clock)
    http.set_sleep_seam(clock.sleep)
    yield clock
    providers.set_arxiv_timing_seams()
    http.set_dns_resolver_seam(None)
    http.set_monotonic_seam(None)
    http.set_sleep_seam(None)


def crossref_item(doi="10.1234/example", **overrides):
    return {
        "DOI": doi, "title": ["A title\n with whitespace"],
        "author": [{"given": "Ada", "family": "Lovelace"}, {"name": "Study Group"}],
        "published": {"date-parts": [[2023, 2, 28]]}, "container-title": ["Test Journal"],
        "type": "journal-article", "URL": "https://doi.org/" + doi,
        "abstract": "<jats:p>Abstract with <jats:italic>detail</jats:italic> &amp; evidence.</jats:p>",
        "link": [{"URL": "https://publisher.example/paper.pdf", "content-type": "application/pdf", "content-version": "vor"}],
        **overrides,
    }


def crossref_search(*items):
    return {"status": "ok", "message": {"items": list(items)}}


def atom_entry(arxiv_id="2301.12345v7", *, summary="A useful\n abstract.", doi="10.1234/example", pdf=None, published="2023-01-05T12:30:00Z", updated="2024-02-01T13:30:00Z"):
    summary_xml = "" if summary is None else "<summary>" + escape(summary) + "</summary>"
    doi_xml = "" if not doi else "<arxiv:doi>" + escape(doi) + "</arxiv:doi>"
    pdf_url = pdf if pdf is not None else "http://arxiv.org/pdf/" + arxiv_id
    return (
        "<entry><id>http://arxiv.org/abs/" + arxiv_id + "</id>"
        "<title> A preprint\n title </title><published>" + published + "</published><updated>" + updated + "</updated>"
        + summary_xml + "<author><name>Alice</name></author><author><name>Bob</name></author>" + doi_xml
        + "<arxiv:journal_ref>Journal 1 (2024), 1–10</arxiv:journal_ref>"
        + '<link rel="related" title="pdf" type="application/pdf" href="' + escape(pdf_url, quote=True) + '"/>'
        + "</entry>"
    )


def atom_feed(*entries):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
            + "".join(entries) + "</feed>").encode()


def assert_code(code, call):
    with pytest.raises(JournalError) as error:
        call()
    assert error.value.code == code
    return error.value


def test_constructor_capabilities_do_not_fetch_or_expose_email(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_UNPAYWALL_EMAIL", "owner+papers@example.org")
    fake = FakeTransport()
    api = LiteratureProviders(fake)
    capabilities = api.capabilities()
    assert capabilities["sources"]["unpaywall"]["email_configured"] is True
    assert capabilities["timeout_seconds"] == 90
    assert capabilities["max_pdf_bytes"] == 32 * 1024 * 1024
    assert "owner+papers@example.org" not in json.dumps(capabilities)
    assert fake.calls == []


def test_crossref_source_parsing_bibliographic_query_filters_and_date_sort():
    fake = FakeTransport(crossref_search(crossref_item(), crossref_item("10.1234/other")))
    records = LiteratureProviders(fake).search("crossref", 'Neural "graphs" & fusion', limit=2, year_from=2020, year_to=2024, sort_by="date")
    request, kwargs = fake.calls[0]
    params = parse_qs(urlsplit(request).query)
    assert params["query.bibliographic"] == ['Neural "graphs" & fusion']
    assert params["filter"] == ["from-pub-date:2020-01-01,until-pub-date:2024-12-31"]
    assert params["sort"] == ["published"] and params["order"] == ["desc"]
    assert params["rows"] == ["2"]
    assert kwargs["pin_dns"] is True and kwargs["allowed_hosts"] == {"api.crossref.org"}
    assert kwargs["deadline"] == 1090 and kwargs["max_bytes"] == providers.MAX_METADATA_BYTES
    record = records[0]
    assert record["paper_id"] == paper_id_for("crossref", "10.1234/example")
    assert record["title"] == "A title with whitespace"
    assert record["authors"] == ["Ada Lovelace", "Study Group"]
    assert record["publication_date"] == "2023-02-28" and record["year"] == 2023
    assert record["venue"] == "Test Journal" and record["publication_type"] == "journal-article"
    assert record["abstract"] == "Abstract with detail & evidence."
    assert record["query"] == 'Neural "graphs" & fusion'
    assert record["fulltext_locations"][0]["is_open"] is False
    assert "TDM" in record["fulltext_locations"][0]["access_evidence"]
    assert PaperRecord.model_validate(record).model_dump(mode="json") == record


@pytest.mark.parametrize("publication_type", [
    "component", "grant", "dataset", "peer-review", "standard", "standard-series",
    "journal", "journal-volume", "journal-issue", "proceedings", "proceedings-series",
    "report-series", "book-series", "book-set", " Component ", "DATASET",
])
def test_crossref_search_excludes_non_paper_objects_and_collection_containers(publication_type):
    fake = FakeTransport(crossref_search(crossref_item(type=publication_type)))
    assert LiteratureProviders(fake).search("crossref", "test", limit=1) == []
    assert len(fake.calls) == 1
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["rows"] == ["1"]


@pytest.mark.parametrize("title", [None, "", [], [""], [" \n "], [None]])
def test_crossref_search_excludes_missing_or_blank_titles(title):
    fake = FakeTransport(crossref_search(crossref_item(title=title)))
    assert LiteratureProviders(fake).search("crossref", "test") == []
    assert len(fake.calls) == 1


@pytest.mark.parametrize("publication_type", [
    "journal-article", "proceedings-article", "posted-content", "dissertation",
    "report", "book-chapter", "other", None,
])
def test_crossref_search_keeps_titled_research_records_and_unknown_types(publication_type):
    fake = FakeTransport(crossref_search(crossref_item(type=publication_type)))
    record = LiteratureProviders(fake).search("crossref", "test")[0]
    assert record["doi"] == "10.1234/example"
    assert record["publication_type"] == (publication_type or "unknown")


def test_crossref_filtering_keeps_original_rows_limit_order_and_one_request():
    # Reproduce the live query's supplement/grant noise without over-fetching
    # or filling the requested slice from extra records outside the limit.
    fake = FakeTransport(crossref_search(
        crossref_item("10.1234/supplement", type="component", title=["Supporting file"]),
        crossref_item("10.1234/grant", type="grant", title=[]),
        crossref_item("10.1234/paper", title=["Actual inference paper"]),
        crossref_item("10.1234/outside-requested-slice", title=["Extra result"]),
    ))
    records = LiteratureProviders(fake).search("crossref", "edge inference deployment", limit=3)
    assert [record["source_record"] for record in records] == ["10.1234/paper"]
    assert len(fake.calls) == 1
    assert parse_qs(urlsplit(fake.calls[0][0]).query) == {
        "query.bibliographic": ["edge inference deployment"], "rows": ["3"],
        "sort": ["relevance"], "order": ["desc"],
    }


@pytest.mark.parametrize("publication_type", ["component", "grant", "dataset", "peer-review"])
def test_crossref_doi_details_remains_queryable_for_non_papers_and_empty_title(publication_type):
    fake = FakeTransport({"message": crossref_item(type=publication_type, title=[])})
    record = LiteratureProviders(fake).details("https://doi.org/10.1234/example")
    assert record["title"] == "" and record["publication_type"] == publication_type
    assert record["paper_id"] == paper_id_for("crossref", "10.1234/example")
    assert len(fake.calls) == 1
    assert unquote(urlsplit(fake.calls[0][0]).path) == "/works/10.1234/example"


def test_crossref_filtering_does_not_swallow_malformed_record_error():
    fake = FakeTransport(crossref_search(None))
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(fake).search("crossref", "test"))


def test_crossref_relevance_default_limit_and_missing_abstract():
    item = crossref_item()
    item.pop("abstract")
    fake = FakeTransport(crossref_search(item))
    result = LiteratureProviders(fake).search("crossref", "title")
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["sort"] == ["relevance"]
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["rows"] == ["10"]
    assert result[0]["abstract"] == ""


@pytest.mark.parametrize("parts,expected", [([2022], "2022"), ([2022, 3], "2022-03"), ([2022, 3, 4], "2022-03-04")])
def test_crossref_partial_publication_dates(parts, expected):
    item = crossref_item(published={"date-parts": [parts]})
    record = LiteratureProviders(FakeTransport({"message": item})).details(item["DOI"])
    assert record["publication_date"] == expected


def test_crossref_invalid_publication_date_falls_back_without_inventing_day():
    item = crossref_item(published={"date-parts": [[2023, 2, 30]]}, issued={"date-parts": [[2023]]})
    record = LiteratureProviders(FakeTransport({"message": item})).details(item["DOI"])
    assert record["publication_date"] == "2023"


@pytest.mark.parametrize("identifier", ["10.1234/ExAmPlE", "DOI: 10.1234/ExAmPlE", "https://doi.org/10.1234/ExAmPlE", "http://dx.doi.org/10.1234%2FExAmPlE#abstract"])
def test_doi_details_normalization(identifier):
    fake = FakeTransport({"message": crossref_item()})
    record = LiteratureProviders(fake).details(identifier)
    assert unquote(urlsplit(fake.calls[0][0]).path) == "/works/10.1234/example"
    assert record["source_record"] == "10.1234/example"
    assert normalize_doi(identifier) == "10.1234/example"


@pytest.mark.parametrize("identifier", ["10/not-doi", "https://evil.example/10.1234/x", "https://user:pass@doi.org/10.1234/x", "https://doi.org/10.1234/x?email=private@example.org", "https://doi.org/10.1234/x?api_key=secret", "10.1234/a\nsecret", "https://doi.org/10.1234/x?foo=bar"])
def test_bad_identifiers_never_fetch(identifier):
    fake = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(fake).details(identifier))
    assert not fake.calls


def test_crossref_secret_metadata_urls_are_not_persisted():
    item = crossref_item(URL="https://publisher.example/x?email=secret@example.org", link=[
        {"URL": "https://publisher.example/paper.pdf?api_key=token"},
        {"URL": "https://publisher.example/paper.pdf?X-Amz-Signature=secret"},
    ])
    record = LiteratureProviders(FakeTransport({"message": item})).details("10.1234/example")
    assert record["landing_url"] == "https://doi.org/10.1234/example"
    assert record["fulltext_locations"] == []
    assert "secret@example.org" not in json.dumps(record)
    assert public_url("https://publisher.example/x?email=secret@example.org") == ""


@pytest.mark.parametrize("query", ["", "   ", "query\nsecond", "a" * 1001, None, 42])
def test_query_input_bounds_before_network(query):
    fake = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(fake).search("crossref", query))
    assert not fake.calls


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": 1.0}, {"limit": "10"},
    {"sort_by": "citation"}, {"year_from": True}, {"year_from": 1499}, {"year_to": 2201},
    {"year_from": "2023-02-29"}, {"year_to": "2023-13"}, {"year_to": "2023-00-01"},
    {"year_from": 2025, "year_to": 2024}, {"year_from": "2023-1"}, {"year_to": 2023.5},
])
def test_search_date_limit_sort_validation(kwargs):
    fake = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(fake).search("arxiv", "test", **kwargs))
    assert not fake.calls


def test_search_sources_are_crossref_and_arxiv_only():
    for source in ("unpaywall", "openalex", "search", "unknown"):
        assert_code("UNSUPPORTED_SOURCE", lambda: LiteratureProviders(FakeTransport()).search(source, "test"))


def test_precise_month_day_ranges_crossref_and_arxiv():
    crossref = FakeTransport(crossref_search())
    LiteratureProviders(crossref).search("crossref", "test", year_from="2024-02", year_to="2024-02")
    assert parse_qs(urlsplit(crossref.calls[0][0]).query)["filter"] == ["from-pub-date:2024-02-01,until-pub-date:2024-02-29"]
    arxiv = FakeTransport(atom_feed())
    LiteratureProviders(arxiv).search("arxiv", "test", year_from="2024-02-02", year_to="2024-02-29")
    assert "submittedDate:[202402020000 TO 202402292359]" in parse_qs(urlsplit(arxiv.calls[0][0]).query)["search_query"][0]


@pytest.mark.parametrize("query,expression", [
    ("PyramidTNT vision", 'all:"PyramidTNT" AND all:"vision"'),
    ("neural   edge\u3000deployment", 'all:"neural" AND all:"edge" AND all:"deployment"'),
    ("PyramidTNT", 'all:"PyramidTNT"'),
    ('"PyramidTNT vision"', 'all:"PyramidTNT vision"'),
    (' "PyramidTNT vision" ', 'all:"PyramidTNT vision"'),
    ('PyramidTNT "visual attention" edge', 'all:"PyramidTNT" AND all:"visual attention" AND all:"edge"'),
    ('"visual attention" "edge deployment"', 'all:"visual attention" AND all:"edge deployment"'),
    ('ti:"quantum criticality"', 'all:"ti:" AND all:"quantum criticality"'),
    ("视觉 Transformer 部署", 'all:"视觉" AND all:"Transformer" AND all:"部署"'),
    ('"多尺度 视觉" 部署', 'all:"多尺度 视觉" AND all:"部署"'),
    ("vision OR cat:cs.CV", 'all:"vision" AND all:"OR" AND all:"cat:cs.CV"'),
    ('vision " OR all:* " edge', 'all:"vision" AND all:"OR all:*" AND all:"edge"'),
    ("vision (OR) ANDNOT -ti:secret +abs:foo && [* TO *]", 'all:"vision" AND all:"(OR)" AND all:"ANDNOT" AND all:"-ti:secret" AND all:"+abs:foo" AND all:"&&" AND all:"[*" AND all:"TO" AND all:"*]"'),
    (r"vision\encoder", r'all:"vision\\encoder"'),
    (r'quote\"literal', r'all:"quote\\\"literal"'),
    (r'"path\\"', r'all:"path\\\\"'),
    (r'vision "quantum \"phase\"" edge', r'all:"vision" AND all:"quantum \\\"phase\\\"" AND all:"edge"'),
])
def test_arxiv_literal_keywords_and_explicit_phrases(query, expression):
    fake = FakeTransport(atom_feed(atom_entry()))
    record = LiteratureProviders(fake).search("arxiv", query, limit=2)[0]
    assert len(fake.calls) == 1
    request, kwargs = fake.calls[0]
    assert urlsplit(request).hostname == "export.arxiv.org"
    assert parse_qs(urlsplit(request).query) == {
        "search_query": [expression], "start": ["0"], "max_results": ["2"],
        "sortBy": ["relevance"], "sortOrder": ["descending"],
    }
    assert record["query"] == query.strip()
    assert kwargs["allowed_hosts"] == {"export.arxiv.org"} and kwargs["pin_dns"] is True
    assert kwargs["request_interval"] == 3.0


@pytest.mark.parametrize("query", [
    "", "   ", '""', '"   "', 'vision "" edge', 'vision "   " edge',
    '"vision', 'vision"', 'vision "attention', '"vision" "attention',
    r'"vision\"', r'vision\\"attention', '视觉 "部署',
])
def test_arxiv_empty_or_unpaired_quotes_never_request(query, offline_policy):
    fake = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(fake).search("arxiv", query))
    assert not fake.calls and not offline_policy.sleeps


@pytest.mark.parametrize("quoted", [False, True])
def test_arxiv_term_limit_accepts_32_words_or_phrases_and_rejects_33(quoted):
    term = '"two words"' if quoted else "keyword"
    expected = 'all:"two words"' if quoted else 'all:"keyword"'
    fake = FakeTransport(atom_feed())
    LiteratureProviders(fake).search("arxiv", " ".join([term] * 32))
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["search_query"] == [" AND ".join([expected] * 32)]
    denied = FakeTransport()
    error = assert_code("INVALID_INPUT", lambda: LiteratureProviders(denied).search("arxiv", " ".join([term] * 33)))
    assert "32" in str(error) and not denied.calls


def test_arxiv_long_explicit_phrase_is_one_term_and_original_character_budget_stays():
    phrase = " ".join(["word"] * 40)
    fake = FakeTransport(atom_feed(), atom_feed())
    api = LiteratureProviders(fake)
    api.search("arxiv", '"' + phrase + '"')
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["search_query"] == ['all:"' + phrase + '"']
    api.search("arxiv", "a" * 1000)
    assert parse_qs(urlsplit(fake.calls[1][0]).query)["search_query"] == ['all:"' + "a" * 1000 + '"']
    denied = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(denied).search("arxiv", "a" * 1001))
    assert not denied.calls


def test_arxiv_keyword_injection_and_date_filter_keep_original_url_and_limit_scope():
    query = 'vision OR "edge inference" &max_results=999&sortOrder=ascending submittedDate:[* TO *]'
    fake = FakeTransport(atom_feed())
    LiteratureProviders(fake).search("arxiv", query, limit=5, year_from="2024-02", year_to="2024-02", sort_by="date")
    assert len(fake.calls) == 1
    params = parse_qs(urlsplit(fake.calls[0][0]).query)
    assert params == {
        "search_query": ['(all:"vision" AND all:"OR" AND all:"edge inference" AND all:"&max_results=999&sortOrder=ascending" AND all:"submittedDate:[*" AND all:"TO" AND all:"*]") AND submittedDate:[202402010000 TO 202402292359]'],
        "start": ["0"], "max_results": ["5"], "sortBy": ["submittedDate"], "sortOrder": ["descending"],
    }


def test_crossref_bibliographic_query_is_not_changed_by_arxiv_keyword_rules():
    query = 'vision "unpaired title'
    fake = FakeTransport(crossref_search(crossref_item()))
    record = LiteratureProviders(fake).search("crossref", query)[0]
    assert record["query"] == query
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["query.bibliographic"] == [query]


def test_arxiv_atom_namespaces_metadata_dates_pdf_and_literal_escaped_query():
    fake = FakeTransport(atom_feed(atom_entry()))
    query = 'graph "models" \\ OR all:secret & max_results=500'
    record = LiteratureProviders(fake).search("arxiv", query, limit=7, year_from=2023, year_to=2024, sort_by="date")[0]
    params = parse_qs(urlsplit(fake.calls[0][0]).query)
    assert params["search_query"] == [r'(all:"graph" AND all:"models" AND all:"\\" AND all:"OR" AND all:"all:secret" AND all:"&" AND all:"max_results=500") AND submittedDate:[202301010000 TO 202412312359]']
    assert params["sortBy"] == ["submittedDate"] and params["sortOrder"] == ["descending"]
    assert params["max_results"] == ["7"]
    assert fake.calls[0][1]["request_interval"] == 3.0
    assert record["title"] == "A preprint title" and record["abstract"] == "A useful abstract."
    assert record["authors"] == ["Alice", "Bob"]
    assert record["publication_date"] == "2023-01-05"
    assert record["sources"][0]["updated"] == "2024-02-01T13:30:00Z"
    assert record["source_record"] == record["arxiv_id"] == "2301.12345v7"
    assert record["version"] == "v7" and record["publication_type"] == "preprint"
    assert record["venue"] == "Journal 1 (2024), 1–10"
    location = record["fulltext_locations"][0]
    assert location["url"] == "https://arxiv.org/pdf/2301.12345v7"
    assert location["version"] == "v7" and location["is_open"] is True and location["license"] is None
    assert record["doi"] == "10.1234/example"
    assert record["related_identifiers"][0]["relation"] == "published_version"
    assert PaperRecord.model_validate(record).model_dump(mode="json") == record


@pytest.mark.parametrize("identifier,arxiv_id", [
    ("arXiv:2301.12345v7", "2301.12345v7"),
    ("https://arxiv.org/abs/2301.12345v7", "2301.12345v7"),
    ("https://arxiv.org/pdf/2301.12345v7.pdf", "2301.12345v7"),
    ("hep-th/9901001v7", "hep-th/9901001v7"),
    ("http://export.arxiv.org/pdf/hep-th/9901001v7.pdf", "hep-th/9901001v7"),
])
def test_arxiv_details_preserve_new_and_old_style_versions(identifier, arxiv_id):
    fake = FakeTransport(atom_feed(atom_entry(arxiv_id)))
    record = LiteratureProviders(fake).details(identifier)
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["id_list"] == [arxiv_id]
    assert record["source_record"] == record["arxiv_id"] == arxiv_id
    assert record["paper_id"] == paper_id_for("arxiv", arxiv_id)
    assert record["version"] == "v7"
    assert normalize_arxiv_id(identifier) == arxiv_id


@pytest.mark.parametrize("identifier", ["2300.12345", "2313.12345", "2301.12345v0", "hep-th/9913001v1", "https://user:pass@arxiv.org/abs/2301.12345", "https://arxiv.org/abs/2301.12345?token=secret", "https://arxiv.org/src/2301.12345"])
def test_arxiv_identifier_dates_and_url_safety(identifier):
    assert_code("INVALID_INPUT", lambda: normalize_arxiv_id(identifier))


def test_arxiv_associated_doi_does_not_merge_versions_or_publication():
    fake = FakeTransport(atom_feed(atom_entry("2301.12345v1"), atom_entry("2301.12345v7")), {"message": crossref_item()})
    api = LiteratureProviders(fake)
    preprints = api.search("arxiv", "test")
    published = api.details("10.1234/example")
    assert len({preprints[0]["paper_id"], preprints[1]["paper_id"], published["paper_id"]}) == 3
    assert {record["doi"] for record in preprints} == {published["doi"]}


def test_arxiv_absent_abstract_and_invalid_dates_stay_unknown():
    record = LiteratureProviders(FakeTransport(atom_feed(atom_entry(summary=None, doi="", published="2023-02-30")))).search("arxiv", "test")[0]
    assert record["abstract"] == "" and record["doi"] == ""
    assert record["publication_date"] is None and record["year"] is None


def test_arxiv_unversioned_id_uses_explicit_pdf_version_when_present():
    record = LiteratureProviders(FakeTransport(atom_feed(atom_entry("2301.12345", pdf="http://arxiv.org/pdf/2301.12345v7")))).details("2301.12345")
    assert record["source_record"] == "2301.12345v7" and record["version"] == "v7"


def test_arxiv_explicit_details_version_mismatch_is_not_silently_latest():
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(FakeTransport(atom_feed(atom_entry("2301.12345v2")))).details("2301.12345v7"))


def test_arxiv_missing_detail_is_not_found():
    assert_code("NOT_FOUND", lambda: LiteratureProviders(FakeTransport(atom_feed())).details("2301.12345v7"))


def test_arxiv_error_feed_is_not_empty_results():
    error = "<entry><id>http://arxiv.org/api/errors#bad-query</id><title>Error</title><summary>bad query</summary></entry>"
    assert_code("NETWORK_ERROR", lambda: LiteratureProviders(FakeTransport(atom_feed(error))).search("arxiv", "test"))


@pytest.mark.parametrize("body", [b"<html>login</html>", b"<feed>", b"<!DOCTYPE feed [<!ENTITY x 'bad'>]><feed/>", "<!DOCTYPE feed [<!ENTITY x 'bad'>]><feed/>".encode("utf-16")])
def test_arxiv_rejects_malformed_non_atom_or_entity_xml(body):
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(FakeTransport(body)).search("arxiv", "test"))


def test_arxiv_serial_interval_success_cache_clone_and_ttl(offline_policy):
    fake = FakeTransport(atom_feed(atom_entry()), atom_feed(atom_entry()), atom_feed(atom_entry()))
    api = LiteratureProviders(fake)
    first = api.search("arxiv", "first")
    first[0]["title"] = "changed by caller"
    again = api.search("arxiv", "first")
    assert again[0]["title"] == "A preprint title" and len(fake.calls) == 1
    api.search("arxiv", "second")
    assert offline_policy.sleeps == [3.0]
    offline_policy.now += providers.ARXIV_CACHE_TTL + 1
    api.search("arxiv", "first")
    assert len(fake.calls) == 3


def test_arxiv_equivalent_keyword_cache_preserves_current_input_query():
    fake = FakeTransport(atom_feed(atom_entry()))
    api = LiteratureProviders(fake)
    first = api.search("arxiv", "vision edge")[0]
    cached = api.search("arxiv", ' "vision"   edge ')[0]
    assert len(fake.calls) == 1
    assert first["query"] == "vision edge"
    assert cached["query"] == '"vision"   edge'
    assert cached["retrieved_at"] == first["retrieved_at"]
    cached["query"] = "caller mutation"
    assert api.search("arxiv", "vision edge")[0]["query"] == "vision edge"
    assert len(fake.calls) == 1


def test_arxiv_cache_has_count_and_byte_bounds(monkeypatch):
    body = atom_feed(atom_entry())
    monkeypatch.setattr(providers, "ARXIV_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(providers, "ARXIV_CACHE_MAX_BYTES", len(body) * 2)
    fake = FakeTransport(body, body, body, body)
    api = LiteratureProviders(fake)
    for query in ("one", "two", "three"):
        api.search("arxiv", query)
    assert len(api._arxiv_cache) == 2
    assert api._arxiv_cache_bytes <= len(body) * 2
    api.search("arxiv", "one")
    assert len(fake.calls) == 4


def test_arxiv_failure_is_paced_not_cached_or_swallowed(offline_policy):
    fake = FakeTransport(JournalError("RATE_LIMITED", "limited"), atom_feed(atom_entry()))
    api = LiteratureProviders(fake)
    assert_code("RATE_LIMITED", lambda: api.search("arxiv", "same"))
    assert not api._arxiv_cache
    api.search("arxiv", "same")
    assert len(fake.calls) == 2 and offline_policy.sleeps == [3.0]


def test_arxiv_backwards_clock_cannot_create_unbounded_delay(offline_policy):
    fake = FakeTransport(atom_feed(atom_entry()))
    api = LiteratureProviders(fake)
    api.search("arxiv", "one")
    offline_policy.now -= 10000
    assert_code("TIMEOUT", lambda: api.search("arxiv", "two"))
    assert not offline_policy.sleeps


def test_arxiv_concurrent_calls_are_serial_across_instances():
    entered, release = threading.Event(), threading.Event()
    state = {"active": 0, "max_active": 0, "calls": 0}
    guard = threading.Lock()

    def transport(url, **kwargs):
        with guard:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["calls"] += 1
            first = state["calls"] == 1
        if first:
            entered.set()
            assert release.wait(5)
        with guard:
            state["active"] -= 1
        return atom_feed(atom_entry()), {}, url

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(LiteratureProviders(transport).search, "arxiv", "one")
        assert entered.wait(5)
        two = pool.submit(LiteratureProviders(transport).search, "arxiv", "two")
        release.set()
        assert one.result(timeout=5) and two.result(timeout=5)
    assert state["calls"] == 2 and state["max_active"] == 1


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"message":{"items":null}}', b'{"status":"error","message":"denied"}'])
def test_crossref_bad_response_is_not_no_results(body):
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(FakeTransport(body)).search("crossref", "test"))


def test_unpaywall_requires_contact_email_without_network():
    fake = FakeTransport()
    api = LiteratureProviders(fake)
    assert api.capabilities()["unpaywall_status"] == "CONTACT_EMAIL_REQUIRED"
    assert_code("CONTACT_EMAIL_REQUIRED", lambda: api.resolve_oa("10.1234/example"))
    assert not fake.calls


@pytest.mark.parametrize("env_key", ["PAPERFLOW_UNPAYWALL_EMAIL", "PAPERFLOW_CONTACT_EMAIL"])
def test_unpaywall_email_environment_alias_and_endpoint(env_key, monkeypatch):
    monkeypatch.setenv(env_key, "owner+research@example.org")
    fake = FakeTransport({"is_oa": False})
    api = LiteratureProviders(fake)
    assert api.resolve_oa("https://doi.org/10.1234/EXAMPLE") == []
    request, kwargs = fake.calls[0]
    assert request.startswith("https://api.unpaywall.org/v2/")
    assert parse_qs(urlsplit(request).query) == {"email": ["owner+research@example.org"]}
    assert unquote(urlsplit(request).path) == "/v2/10.1234/example"
    assert kwargs["allowed_hosts"] == {"api.unpaywall.org"} and kwargs["pin_dns"] is True


def test_unpaywall_email_precedence_and_no_email_in_metadata(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_UNPAYWALL_EMAIL", "unpaywall@example.org")
    monkeypatch.setenv("PAPERFLOW_CONTACT_EMAIL", "contact@example.org")
    fake = FakeTransport({"is_oa": False}, crossref_search(crossref_item()))
    api = LiteratureProviders(fake, email="explicit@example.org")
    api.resolve_oa("10.1234/example")
    result = api.search("crossref", "test")
    assert parse_qs(urlsplit(fake.calls[0][0]).query)["email"] == ["explicit@example.org"]
    assert "email=" not in fake.calls[1][0]
    assert "explicit@example.org" not in json.dumps(result)


@pytest.mark.parametrize("email", ["not-email", "Name <user@example.org>", "user@example.org\r\nheader: secret", 42])
def test_bad_contact_email_never_appears_in_errors(email):
    error = assert_code("INVALID_INPUT", lambda: LiteratureProviders(FakeTransport(), email=email))
    assert str(email) not in str(error)


def test_unpaywall_only_explicit_oa_pdf_preserves_evidence_license_version():
    best = {"url_for_pdf": "https://zenodo.org/records/1/files/paper.pdf", "url_for_landing_page": "https://zenodo.org/records/1",
            "version": "acceptedVersion", "license": "cc-by", "evidence": "oa repository"}
    fake = FakeTransport({"is_oa": True, "best_oa_location": best, "oa_locations": [best,
        {"url": "https://publisher.example/fulltext", "url_for_pdf": None},
        {"url_for_pdf": "https://repo.example/paper.pdf?signature=secret"},
        {"url_for_pdf": "https://repo.example/private.pdf", "is_oa": False},
        {"url_for_pdf": "https://repo.example/open.pdf", "version": "submittedVersion", "license": None},
    ]})
    result = LiteratureProviders(fake, email="owner@example.org").resolve_oa("10.1234/example")
    assert len(result) == 2
    assert result[0]["license"] == "cc-by" and result[0]["version"] == "acceptedVersion"
    assert result[0]["is_open"] is True and "oa repository" in result[0]["access_evidence"]
    assert result[1]["license"] is None and result[1]["version"] == "submittedVersion"
    assert "owner@example.org" not in json.dumps(result) and "signature" not in json.dumps(result)


@pytest.mark.parametrize("payload", [{"is_oa": False, "best_oa_location": {"url_for_pdf": "https://zenodo.org/x.pdf"}},
                                      {"is_oa": True, "best_oa_location": None, "oa_locations": [{"url_for_landing_page": "https://zenodo.org/x"}]}])
def test_unpaywall_closed_or_landing_only_has_no_download_candidates(payload):
    assert LiteratureProviders(FakeTransport(payload), email="owner@example.org").resolve_oa("10.1234/example") == []


@pytest.mark.parametrize("payload", [{}, {"is_oa": "true"}, {"is_oa": True, "oa_locations": "bad"}])
def test_unpaywall_missing_or_malformed_status_is_error(payload):
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(FakeTransport(payload), email="owner@example.org").resolve_oa("10.1234/example"))


def test_unpaywall_transport_exception_is_redacted():
    fake = FakeTransport(RuntimeError("https://api.unpaywall.org/v2/10.1234/x?email=owner@example.org"))
    error = assert_code("NETWORK_ERROR", lambda: LiteratureProviders(fake, email="owner@example.org").resolve_oa("10.1234/example"))
    assert "owner@example.org" not in str(error) and "email=" not in str(error)


@pytest.mark.parametrize("status,code", [(401, "AUTH_REQUIRED"), (403, "AUTH_REQUIRED"), (429, "RATE_LIMITED")])
def test_authorization_and_rate_limit_remain_errors(status, code):
    for source in ("crossref", "arxiv", "unpaywall"):
        error = urllib.error.HTTPError("https://api.unpaywall.org/?email=private@example.org", status, "secret", {}, None)
        api = LiteratureProviders(FakeTransport(error), email="owner@example.org")
        operation = (lambda: api.resolve_oa("10.1234/example")) if source == "unpaywall" else (lambda: api.search(source, "test"))
        result = assert_code(code, operation)
        assert "private@example.org" not in str(result)


def open_pdf(url="https://arxiv.org/pdf/2301.12345v7"):
    return {"url": url, "source_id": "arxiv", "version": "v7", "license": None, "is_open": True,
            "access_evidence": "arXiv public Atom PDF link"}


def test_pdf_download_https_pinned_policy_and_actual_body():
    url = "https://arxiv.org/pdf/2301.12345v7"
    body = b"%PDF-1.7\nfixture"
    fake = FakeTransport((body, {"Content-Type": "Application/Pdf; charset=binary"}, url))
    result, headers, final = LiteratureProviders(fake).fetch_pdf(open_pdf(url))
    assert result == body and final == url and headers["content-type"].startswith("Application/Pdf")
    kwargs = fake.calls[0][1]
    assert kwargs["pin_dns"] and kwargs["public_only"] and kwargs["allow_cross_host_redirect"]
    assert kwargs["max_bytes"] == 32 * 1024 * 1024 and kwargs["deadline"] == 1090


def test_pdf_crossref_unverified_location_never_downloads():
    fake = FakeTransport()
    assert_code("OPEN_ACCESS_REQUIRED", lambda: LiteratureProviders(fake).fetch_pdf({"url": "https://arxiv.org/pdf/x", "is_open": False}))
    assert not fake.calls


def test_pdf_unknown_host_denied_with_configuration_hint():
    fake = FakeTransport()
    error = assert_code("HOST_DENIED", lambda: LiteratureProviders(fake).fetch_pdf(open_pdf("https://publisher.example/paper.pdf")))
    assert "PAPERFLOW_PDF_ALLOWED_HOSTS" in str(error) and not fake.calls


def test_pdf_environment_extends_only_pdf_policy(monkeypatch):
    monkeypatch.setenv("PAPERFLOW_PDF_ALLOWED_HOSTS", "repository.example, ARCHIVE.example")
    url = "https://repository.example/open.pdf"
    fake = FakeTransport((b"%PDF-1.7\n", {}, url))
    api = LiteratureProviders(fake)
    assert {"arxiv.org", "repository.example", "archive.example"} <= api.allowed_pdf_hosts
    assert api.fetch_pdf(open_pdf(url))[0].startswith(b"%PDF-")
    assert http.ALLOWED_HOSTS == {"api.github.com", "raw.githubusercontent.com", "easyscholar.cc"}


def test_pdf_constructor_policy_is_explicit_replacement():
    api = LiteratureProviders(FakeTransport(), allowed_pdf_hosts={"repo.example"})
    assert api.allowed_pdf_hosts == {"repo.example"}
    assert_code("HOST_DENIED", lambda: api.fetch_pdf(open_pdf()))


@pytest.mark.parametrize("hosts", ["repo.example", {"*.example.org"}, {"https://repo.example"}, {"user@repo.example"}, {"foo..example"}])
def test_pdf_allowlist_is_exact_hostnames_only(hosts):
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(FakeTransport(), allowed_pdf_hosts=hosts))


@pytest.mark.parametrize("url,code", [
    ("http://arxiv.org/pdf/x", "INVALID_URL"), ("https://arxiv.org:80/pdf/x", "INVALID_URL"),
    ("https://user:secret@arxiv.org/pdf/x", "SSRF_VIOLATION"),
    ("https://arxiv.org/pdf/x?email=owner@example.org", "SSRF_VIOLATION"),
    ("https://arxiv.org/pdf/x?APIkey=secret", "SSRF_VIOLATION"),
    ("https://arxiv.org/pdf/x?X-Amz-Signature=secret", "SSRF_VIOLATION"),
    ("https://127.0.0.1/x", "SSRF_VIOLATION"), ("https://[::1]/x", "SSRF_VIOLATION"),
])
def test_pdf_credential_private_ip_and_protocol_guards(url, code):
    fake = FakeTransport()
    assert_code(code, lambda: LiteratureProviders(fake).fetch_pdf(open_pdf(url)))
    assert not fake.calls


@pytest.mark.parametrize("body,content_type", [(b"<html>login</html>", "application/pdf"),
                                               (b"%PDF-1.7\n", "text/html"), (b"%PDF-1.7\n", "application/xhtml+xml"),
                                               (b"<html>paywall</html>", "text/html"), (b"", "application/pdf")])
def test_pdf_html_masquerade_is_rejected(body, content_type):
    fake = FakeTransport((body, {"content-type": content_type}, "https://arxiv.org/pdf/x"))
    assert_code("INVALID_PDF", lambda: LiteratureProviders(fake).fetch_pdf(open_pdf()))


def test_pdf_body_limit_independent_of_content_length():
    fake = FakeTransport((b"%PDF-" + b"x" * (32 * 1024 * 1024), {"content-length": "5"}, "https://arxiv.org/pdf/x"))
    assert_code("FILE_TOO_LARGE", lambda: LiteratureProviders(fake).fetch_pdf(open_pdf()))


def test_metadata_response_has_separate_small_budget():
    fake = FakeTransport(b"x" * (providers.MAX_METADATA_BYTES + 1))
    assert_code("FILE_TOO_LARGE", lambda: LiteratureProviders(fake).search("crossref", "test"))


def test_pdf_final_url_is_revalidated():
    fake = FakeTransport((b"%PDF-1.7\n", {}, "https://unknown.example/file.pdf"))
    assert_code("HOST_DENIED", lambda: LiteratureProviders(fake).fetch_pdf(open_pdf()))


def test_transport_total_timeout_not_infinite_delay(offline_policy):
    def expired_transport(url, **kwargs):
        offline_policy.now += 91
        return atom_feed(atom_entry()), {}, url

    api = LiteratureProviders(expired_transport)
    assert_code("TIMEOUT", lambda: api.search("arxiv", "test"))
    assert not api._arxiv_cache


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["100.64.0.1"],
    ["::1"], ["::ffff:127.0.0.1"], ["2001:db8::1"], ["224.0.0.1"],
    ["1.1.1.1", "10.0.0.1"], ["not-an-ip"],
])
def test_default_pdf_transport_rejects_all_private_or_invalid_dns_before_socket(addresses, monkeypatch):
    opened = []

    def forbidden_socket(*args, **kwargs):
        opened.append(True)
        raise AssertionError("No socket is allowed for a restricted DNS answer")

    http.set_dns_resolver_seam(lambda host, port: addresses)
    monkeypatch.setattr(http.socket, "socket", forbidden_socket)
    assert_code("SSRF_VIOLATION", lambda: LiteratureProviders().fetch_pdf(open_pdf()))
    assert not opened


@pytest.mark.parametrize("field,bad_value", [("author", {"name": "Alice"}), ("link", {"URL": "https://zenodo.org/x.pdf"})])
def test_crossref_malformed_collections_use_journal_error(field, bad_value):
    fake = FakeTransport(crossref_search(crossref_item(**{field: bad_value})))
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(fake).search("crossref", "test"))


def test_crossref_lookup_never_returns_a_different_doi():
    fake = FakeTransport({"message": crossref_item("10.1234/other")})
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(fake).details("10.1234/example"))


def test_doi_encoded_size_is_bounded_before_lookup():
    fake = FakeTransport()
    assert_code("INVALID_INPUT", lambda: LiteratureProviders(fake).details("10.1234/" + "?" * 1200))
    assert not fake.calls


@pytest.mark.parametrize("doi", ["10.1234/other", None, "bad-doi"])
def test_unpaywall_lookup_identity_is_checked(doi):
    fake = FakeTransport({"is_oa": True, "doi": doi, "best_oa_location": {"url_for_pdf": "https://zenodo.org/x.pdf"}})
    assert_code("INVALID_RESPONSE", lambda: LiteratureProviders(fake, email="owner@example.org").resolve_oa("10.1234/example"))


def test_unpaywall_journal_error_cannot_leak_contact_email_or_traceback(capsys, monkeypatch):
    monkeypatch.setenv("PAPERFLOW_UNPAYWALL_EMAIL", "owner+papers@example.org")
    fake = FakeTransport(JournalError("NETWORK_ERROR", "https://api.unpaywall.org/v2/x?email=owner%2Bpapers%40example.org"))
    error = assert_code("NETWORK_ERROR", lambda: LiteratureProviders(fake).resolve_oa("10.1234/example"))
    import traceback
    traceback.print_exception(error)
    assert "owner" not in str(error)
    assert "owner" not in capsys.readouterr().err


def test_real_safe_fetch_enforces_pdf_actual_chunk_cap_with_injected_opener(monkeypatch):
    import io
    monkeypatch.setattr(http, "MAX_DOWNLOAD_SIZE_BYTES", 32)
    response_data = io.BytesIO(b"%PDF-1.7\n" + b"x" * 64)
    reads = []

    class Response:
        status = 200
        headers = {"content-type": "application/pdf", "content-length": "1"}

        def read(self, amount):
            reads.append(amount)
            return response_data.read(amount)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            response_data.close()

    class Opener:
        def open(self, req, timeout):
            return Response()

    def transport(url, **kwargs):
        return http.safe_fetch(url, opener=Opener(), **kwargs)

    assert_code("FILE_TOO_LARGE", lambda: LiteratureProviders(transport).fetch_pdf(open_pdf()))
    assert reads and max(reads) <= 33
