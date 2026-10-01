"""Offline contracts for cached papers, paginated evidence and honest coverage."""
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.models import PaperRecord, paper_id_for, public_url
from paperflow.engine.literature.pdf_reader import read_pdf_pages
from paperflow.engine.literature.service import LiteratureService
from paperflow.engine.literature.store import PaperStore


def pdf_bytes(texts):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = pypdf.PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if text is None:
            continue
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(("BT /F1 12 Tf 20 700 Td (" + escaped + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def record(source="crossref", identifier="10.1234/demo", doi="10.1234/demo", **fields):
    return PaperRecord(paper_id=paper_id_for(source, identifier), source_id=source, source_record=identifier,
                       title="Example research", doi=doi, **fields).model_dump(mode="json")


class FakeProviders:
    def __init__(self, records=None, body=None):
        self.records = records or {"crossref": [record()], "arxiv": []}
        self.body = body
        self.calls = []

    def search(self, source_id, query, **kwargs):
        self.calls.append((source_id, query, kwargs))
        results = self.records[source_id]
        if isinstance(results, Exception):
            raise results
        return results

    def details(self, identifier):
        return record()

    def resolve_oa(self, doi):
        raise JournalError("CONTACT_EMAIL_REQUIRED", "未配置联系邮箱")

    def fetch_pdf(self, location):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body, {"content-type": "application/pdf"}, location["url"]

    def capabilities(self):
        return {"sources": ["crossref", "arxiv"], "unpaywall_configured": False}


@pytest.fixture
def service(tmp_path):
    return LiteratureService(str(tmp_path / "library"), providers=FakeProviders())


def imported(service, tmp_path, texts=("First page with a real method.", "Second page with experiments.")):
    file = tmp_path / "user.pdf"
    raw = pdf_bytes(texts)
    file.write_bytes(raw)
    result = service.import_pdf(str(file), "User paper")
    return result["data"]["paper_id"], raw, file


def test_construct_and_list_are_inert(service):
    assert not service.store.directory.exists()
    assert service.list()["data"] == {"papers": [], "total": 0}
    assert not service.store.directory.exists()


def test_search_partial_failure_retains_results(service):
    service.providers.records["arxiv"] = JournalError("RATE_LIMITED", "请求限流")
    result = service.search("tensor deployment")
    assert result["status"] == "partial"
    assert len(result["data"]["papers"]) == 1
    assert result["data"]["source_statuses"][1]["error_code"] == "RATE_LIMITED"
    assert result["coverage"]["stage"] == "metadata_search"
    assert service.list()["data"]["total"] == 1


def test_all_source_failure_is_not_success_empty_search(service):
    service.providers.records = {s: JournalError("TIMEOUT", "超时") for s in ("crossref", "arxiv")}
    res = service.search("deployment")
    assert res["status"] == "partial"
    assert len(res["warnings"]) == 2
    assert not service.store.directory.exists()


def test_dedup_keeps_preprint_and_versions(service):
    pub = record()
    pre = record("arxiv", "1706.03762v7", arxiv_id="1706.03762v7", publication_type="preprint")
    v8 = record("arxiv", "1706.03762v8", arxiv_id="1706.03762v8", publication_type="preprint")
    service.providers.records = {"crossref": [pub, pub], "arxiv": [pre, v8]}
    papers = service.search("attention")["data"]["papers"]
    assert len(papers) == 3
    assert len({p["paper_id"] for p in papers}) == 3


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": True}, {"sources": []}, {"sources": ["private"]},
                                    {"year_from": 2027, "year_to": 2020}, {"sort_by": "probability"}])
def test_invalid_search_inputs(service, kwargs):
    with pytest.raises(JournalError):
        service.search("query", **kwargs)
    assert not service.providers.calls


def test_details_exclusive_input_and_paths(service):
    with pytest.raises(JournalError):
        service.get()
    with pytest.raises(JournalError):
        service.get(paper_id="../../etc/passwd")
    paper = service.get(identifier="10.1234/demo")["data"]
    assert paper["paper_id"].startswith("paper-")
    assert service.get(paper_id=paper["paper_id"])["data"]["doi"] == "10.1234/demo"


def test_metadata_only_cannot_claim_fulltext(service):
    paper_id = service.get(identifier="10.1234/demo")["data"]["paper_id"]
    with pytest.raises(JournalError) as error:
        service.read(paper_id)
    assert error.value.code == "FULLTEXT_REQUIRED"
    result = service.download(paper_id)
    assert result["status"] == "partial"
    assert result["data"]["acquisition"]["error_code"] == "CONTACT_EMAIL_REQUIRED"
    assert result["coverage"]["stage"] == "metadata_only"


def test_download_and_hash_reuse(service):
    raw = pdf_bytes(["downloaded evidence"])
    rec = record(fulltext_locations=[{"url": "https://arxiv.org/pdf/1706.03762v7", "source_id": "arxiv", "is_open": True, "access_evidence": "arxiv public preprint"}])
    service.store.put(rec)
    service.providers.body = raw
    result = service.download(rec["paper_id"])
    sha = result["data"]["acquisition"]["file_sha256"]
    assert sha == hashlib.sha256(raw).hexdigest()
    assert result["coverage"]["stage"] == "fulltext_available_not_read"
    service.providers.body = JournalError("TIMEOUT", "this must not be fetched")
    assert service.download(rec["paper_id"])["status"] == "success"
    assert service.store.read_pdf(sha) == raw


def test_import_keeps_original_and_repeat_identity(service, tmp_path):
    paper_id, raw, original = imported(service, tmp_path)
    before = original.stat().st_mtime_ns
    same = service.import_pdf(str(original))["data"]
    assert same["paper_id"] == paper_id
    assert original.read_bytes() == raw
    assert original.stat().st_mtime_ns == before
    assert "file_path" not in same
    assert service.list()["data"]["total"] == 1


def test_cursors_do_not_drop_page_tail(service, tmp_path):
    text = "A long reproducible method. " * 150
    paper_id, _, _ = imported(service, tmp_path, [text, "next page"])
    first = service.read(paper_id, page_count=2, max_chars=1000)
    assert first["coverage"]["text_complete"] is False
    assert first["data"]["next_cursor"] == {"page_number": 1, "offset": 1000}
    parts = [first["data"]["pages"][0]["text"]]
    res = first
    while res["data"]["next_cursor"]:
        cursor = res["data"]["next_cursor"]
        res = service.read(paper_id, **cursor, page_count=2, max_chars=1000)
        parts.extend(p["text"] for p in res["data"]["pages"] if p["page_number"] == 1)
    assert "".join(parts) == text.strip()
    assert res["coverage"]["text_complete"] is True
    assert res["coverage"]["understanding_complete"] is False


def test_jump_to_last_page_is_not_complete(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path, ["one", "two", "three"])
    result = service.read(paper_id, page_number=3)
    assert result["data"]["next_cursor"] is None
    assert result["coverage"]["fully_extracted_pages"] == [3]
    assert result["coverage"]["text_complete"] is False


def test_mixed_and_blank_pages_honestly_report_missing_text(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path, ["method", None])
    result = service.read(paper_id)
    assert result["status"] == "partial"
    assert result["coverage"]["stage"] == "partial_with_missing_text"
    assert result["coverage"]["text_complete"] is False
    assert result["coverage"]["empty_or_unextractable_pages"][0]["page_number"] == 2
    other, _, _ = imported(service, tmp_path, [None, None])
    assert service.read(other)["coverage"]["stage"] == "ocr_required"


def card_for(result):
    data = result["data"]
    fragment = data["fragments"][0]
    return {"paper_id": data["paper_id"], "file_sha256": data["file_sha256"], "summary": "A reading, not a verified scientific conclusion.",
            "claims": [{"section": "method", "kind": "author_claim", "text": "The author describes a method.",
                        "evidence": [{"fragment_id": fragment["fragment_id"], "page_number": fragment["page_number"], "quote": fragment["text"][:20]}]}]}


def test_saved_reading_has_real_evidence_and_no_semantic_guarantee(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    read = service.read(paper_id)
    card = card_for(read)
    saved = service.save_reading(paper_id, card)
    assert saved["data"]["origin"] == "calling_agent"
    assert saved["data"]["semantic_correctness_verified"] is False
    again = service.save_reading(paper_id, card)
    assert again["data"]["reading_id"] == saved["data"]["reading_id"]
    assert len(service.get(paper_id=paper_id)["data"]["reading_cards"]) == 1


@pytest.mark.parametrize("change", ["wrong_page", "wrong_quote", "missing_fragment", "missing_evidence"])
def test_evidence_mismatch_rejected(service, tmp_path, change):
    paper_id, _, _ = imported(service, tmp_path)
    card = card_for(service.read(paper_id))
    evidence = card["claims"][0]["evidence"][0]
    if change == "wrong_page":
        evidence["page_number"] = 2
    elif change == "wrong_quote":
        evidence["quote"] = "a fabricated quote"
    elif change == "missing_fragment":
        evidence["fragment_id"] = "does-not-exist"
    else:
        card["claims"][0]["evidence"] = []
    with pytest.raises(JournalError) as exc:
        service.save_reading(paper_id, card)
    assert exc.value.code == "EVIDENCE_MISMATCH"
    result = service.save_reading(paper_id, card, origin="gui_model", strict=False)
    assert result["status"] == "partial"
    assert result["data"]["card"]["claims"][0]["kind"] == "unverified"
    assert result["data"]["card"]["claims"][0]["evidence"] == []


def test_invalid_hash_and_modified_cache(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    card = card_for(service.read(paper_id))
    stale = {**card, "file_sha256": "a" * 64}
    with pytest.raises(JournalError) as exc:
        service.save_reading(paper_id, stale)
    assert exc.value.code == "STALE_READING"
    service.save_reading(paper_id, card)
    service.store.pdf_path(card["file_sha256"]).write_bytes(b"changed")
    with pytest.raises(JournalError) as exc:
        service.read(paper_id)
    assert exc.value.code == "CACHE_CORRUPTED"
    assert not service.get(paper_id=paper_id)["data"]["reading_cards"][0]["valid"]


def test_missing_dependency_is_structured(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(JournalError) as exc:
        read_pdf_pages(b"%PDF-1.7 dummy")
    assert exc.value.code == "DEPENDENCY_MISSING"


def test_encrypted_and_invalid_pdf():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(JournalError) as exc:
        read_pdf_pages(stream.getvalue())
    assert exc.value.code == "PDF_ENCRYPTED"
    with pytest.raises(JournalError):
        read_pdf_pages(b"<html>login</html>")
    with pytest.raises(JournalError) as exc:
        read_pdf_pages(b"%PDF-1.7 broken")
    assert exc.value.code == "PDF_EXTRACT_ERROR"


def test_cache_size_and_id_guards(tmp_path):
    store = PaperStore(str(tmp_path / "safe"))
    with pytest.raises(JournalError):
        store.get("../../outside")
    with pytest.raises(JournalError):
        store.pdf_path("../path")
    with pytest.raises(JournalError):
        store.cache_pdf(b"%PDF-" + b"x" * (32 * 1024 * 1024))
    assert not store.directory.exists()


def test_concurrent_store_writes_have_valid_transactions(service):
    def put(number):
        rec = record(identifier=f"10.1234/{number}", doi=f"10.1234/{number}")
        return service.store.put(rec)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(put, range(12)))
    assert service.list()["data"]["total"] == 12


@pytest.mark.parametrize("url", ["file:///tmp/doc", "https://x.test/doc?api_key=secret", "https://a:b@x.test/doc", "https://x.test/doc?email=private@example.com"])
def test_metadata_credentials_are_not_persisted(url):
    assert public_url(url) == ""


def test_fresh_download_repairs_only_corrupt_cache(service):
    raw = pdf_bytes(["repairable content"])
    rec = record(fulltext_locations=[{"url": "https://arxiv.org/pdf/1706.03762v7", "is_open": True, "access_evidence": "public arxiv"}])
    service.store.put(rec)
    service.providers.body = raw
    first = service.download(rec["paper_id"])
    sha = first["data"]["acquisition"]["file_sha256"]
    service.store.pdf_path(sha).write_bytes(b"corrupted cache")
    repaired = service.download(rec["paper_id"])
    assert repaired["status"] == "success"
    assert service.store.read_pdf(sha) == raw
