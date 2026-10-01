"""Boundary regressions discovered while reviewing the initial implementation."""
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_literature_service import service, imported, pdf_bytes, record, card_for
from paperflow.engine.journals.models import JournalError
from paperflow.engine.literature.store import PaperStore


def test_cold_initialization_read_is_empty_not_broken(tmp_path):
    root = tmp_path / "cold"
    root.mkdir()
    conn = sqlite3.connect(str(root / "papers.sqlite3"))
    conn.close()
    store = PaperStore(str(root))
    assert store.list() == {"papers": [], "total": 0}
    store.put(record())
    assert store.list()["total"] == 1


def test_concurrent_same_pdf_cache_write(service):
    raw = pdf_bytes(["concurrent cache"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        hashes = list(pool.map(lambda _: service.store.cache_pdf(raw), range(24)))
    assert len(set(hashes)) == 1
    assert service.store.read_pdf(hashes[0]) == raw
    assert not list(service.store.pdf_path(hashes[0]).parent.glob(".pending-*"))


def test_details_reuse_extraction_coverage(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    service.read(paper_id)
    result = service.get(paper_id=paper_id)
    assert result["coverage"]["text_complete"]
    assert result["coverage"]["stage"] == "fulltext_text_complete"
    assert not result["coverage"]["understanding_complete"]


def test_invalid_read_request_does_not_damage_parse_status(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    service.read(paper_id)
    status = service.get(paper_id=paper_id)["data"]["acquisition"]["parse_status"]
    with pytest.raises(JournalError):
        service.read(paper_id, page_number=99)
    assert service.get(paper_id=paper_id)["data"]["acquisition"]["parse_status"] == status


def test_quote_across_split_boundary_has_evidence(service, tmp_path):
    quote = "TARGET consecutive evidence " * 20
    text = "A" * 2800 + quote + " rest of the page " * 100
    paper_id, _, _ = imported(service, tmp_path, [text])
    read = service.read(paper_id)
    containing = next(f for f in read["data"]["fragments"] if quote in f["text"])
    card = card_for(read)
    card["claims"][0]["evidence"] = [{"fragment_id": containing["fragment_id"], "page_number": 1, "quote": quote}]
    assert service.save_reading(paper_id, card)["status"] == "success"


def test_only_whitespace_is_normalized_in_quotes(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    read = service.read(paper_id)
    card = card_for(read)
    card["claims"][0]["evidence"][0]["quote"] = read["data"]["fragments"][0]["text"].replace(" ", "\n", 1)
    saved = service.save_reading(paper_id, card)
    assert saved["data"]["card"]["claims"][0]["evidence"][0]["match_method"] == "normalized_whitespace"


def test_gui_drops_bad_quote_but_preserves_valid_one(service, tmp_path):
    paper_id, _, _ = imported(service, tmp_path)
    card = card_for(service.read(paper_id))
    bad = {**card["claims"][0]["evidence"][0], "quote": "fabricated evidence"}
    card["claims"][0]["evidence"].append(bad)
    saved = service.save_reading(paper_id, card, origin="gui_model", strict=False)
    claim = saved["data"]["card"]["claims"][0]
    assert saved["status"] == "partial"
    assert len(claim["evidence"]) == 1
    assert claim["kind"] == "author_claim"


def test_scan_with_watermark_does_not_claim_text_complete(service, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = pypdf.PdfWriter()
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes(["Digitized watermark"])))
    writer.add_page(reader.pages[0])
    image = DecodedStreamObject()
    image.set_data(b"\0\0\0")
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    writer.pages[0]["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/Scan"): writer._add_object(image)})
    stream = io.BytesIO()
    writer.write(stream)
    path = tmp_path / "scan.pdf"
    path.write_bytes(stream.getvalue())
    paper_id = service.import_pdf(str(path))["data"]["paper_id"]
    result = service.read(paper_id)
    assert result["data"]["pages"][0]["text"] == "Digitized watermark"
    assert result["coverage"]["text_complete"] is False
    assert result["coverage"]["empty_or_unextractable_pages"][0]["reason"] == "sparse_text_with_images_possible_scan"
    assert result["status"] == "partial"


@pytest.mark.parametrize("iteration", range(10))
def test_independent_stores_initialize_concurrently(tmp_path, iteration):
    root = tmp_path / f"parallel-{iteration}"
    stores = [PaperStore(str(root)) for _ in range(6)]

    def put(number):
        return stores[number % len(stores)].put(
            record(identifier=f"10.1234/{number}", doi=f"10.1234/{number}")
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(put, range(24)))
    assert stores[0].list()["total"] == 24


def test_concurrent_writes_preserve_existing_wal_mode(tmp_path):
    root = tmp_path / "wal-library"
    root.mkdir()
    with sqlite3.connect(str(root / "papers.sqlite3")) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    store = PaperStore(str(root))

    def put(number):
        return store.put(record(identifier=f"10.1234/{number}", doi=f"10.1234/{number}"))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(put, range(24)))
    assert store.list()["total"] == 24
    with sqlite3.connect(str(root / "papers.sqlite3")) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_database_and_journal_symlinks_are_rejected(tmp_path, suffix):
    root = tmp_path / "linked-library"
    root.mkdir()
    target = tmp_path / "outside.sqlite3"
    original = b"must not be overwritten"
    target.write_bytes(original)
    try:
        (root / ("papers.sqlite3" + suffix)).symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not permit symlink creation")
    store = PaperStore(str(root))
    with pytest.raises(JournalError) as error:
        store.put(record())
    assert error.value.code == "UNSAFE_CACHE_PATH"
    assert target.read_bytes() == original
