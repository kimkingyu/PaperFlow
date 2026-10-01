"""Bounded page-text extraction; offsets and cursors never imply comprehension."""
from __future__ import annotations

import hashlib
import io
from typing import Any, Dict

from paperflow.engine.journals.models import JournalError
from paperflow.engine.journals.manuscript import (
    MAX_FILE_BYTES, MAX_PDF_PAGE_DECOMPRESSED_BYTES, MAX_PDF_TOTAL_DECOMPRESSED_BYTES,
)

MAX_PAGES = 1000
MAX_PAGE_TEXT_CHARS = 4 * 1024 * 1024


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise JournalError("INVALID_INPUT", f"{name} 必须为 {minimum} 到 {maximum} 之间的整数")
    return value


def _has_raster_image(page) -> bool:
    """Inspect bounded resource dictionaries without decoding embedded images."""
    pending = [page.get("/Resources")]
    visited = set()
    for _ in range(64):
        if not pending:
            return False
        resources = pending.pop()
        if resources is None:
            continue
        resources = resources.get_object()
        if id(resources) in visited:
            continue
        visited.add(id(resources))
        objects = resources.get("/XObject")
        if objects is None:
            continue
        for reference in list(objects.get_object().values())[:64]:
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form":
                pending.append(obj.get("/Resources"))
    return True  # An excessively nested resource tree needs manual review.


def read_pdf_pages(raw: bytes, page_number: int = 1, page_count: int = 3,
                   offset: int = 0, max_chars: int = 20000) -> Dict[str, Any]:
    _integer("page_number", page_number, 1, MAX_PAGES)
    _integer("page_count", page_count, 1, 10)
    _integer("offset", offset, 0, MAX_PAGE_TEXT_CHARS)
    _integer("max_chars", max_chars, 1000, 20000)
    if len(raw) > MAX_FILE_BYTES or not raw.lstrip()[:1024].startswith(b"%PDF-"):
        raise JournalError("INVALID_PDF", "PDF 文件头无效或超过大小上限")
    try:
        import pypdf
    except ImportError:
        raise JournalError("DEPENDENCY_MISSING", '请安装 PDF 解析依赖：pip install "paperflow-mcp[pdf]"') from None
    limit_error = getattr(getattr(pypdf, "errors", None), "LimitReachedError", ())
    sha = hashlib.sha256(raw).hexdigest()
    pages, fragments = [], []
    budget = max_chars
    next_cursor = None
    try:
        with pypdf.apply_configuration(
            zlib_maximum_output_length=MAX_PDF_PAGE_DECOMPRESSED_BYTES,
            lzw_maximum_output_length=MAX_PDF_PAGE_DECOMPRESSED_BYTES,
            run_length_maximum_output_length=MAX_PDF_PAGE_DECOMPRESSED_BYTES,
            array_based_stream_maximum_output_length=MAX_PDF_PAGE_DECOMPRESSED_BYTES,
            maximum_declared_stream_length=MAX_PDF_TOTAL_DECOMPRESSED_BYTES,
            page_tree_maximum_entries=MAX_PAGES * 10,
        ):
            reader = pypdf.PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                # No password guessing, including "empty" passwords.
                raise JournalError("PDF_ENCRYPTED", "PDF 受口令或加密保护，请导入可正常阅读的版本")
            total_pages = len(reader.pages)
            if not total_pages:
                raise JournalError("EMPTY_INPUT", "PDF 没有页面")
            if total_pages > MAX_PAGES:
                raise JournalError("PDF_RESOURCE_LIMIT", f"PDF 超过 {MAX_PAGES} 页安全上限")
            if page_number > total_pages:
                raise JournalError("PAGE_OUT_OF_RANGE", "请求页码超过 PDF 总页数")
            final_page = min(total_pages, page_number + page_count - 1)
            for number in range(page_number, final_page + 1):
                page = reader.pages[number - 1]
                text = (page.extract_text() or "").strip()
                if len(text) > MAX_PAGE_TEXT_CHARS:
                    raise JournalError("PDF_RESOURCE_LIMIT", "单页提取文字超过安全上限")
                start = offset if number == page_number else 0
                if start > len(text):
                    raise JournalError("INVALID_CURSOR", "页内偏移超过该页提取文字长度")
                end = min(len(text), start + budget)
                chosen = text[start:end]
                empty_kind = "no_extractable_text_requires_review" if not text else ""
                if text and len("".join(text.split())) < 100 and _has_raster_image(page):
                    empty_kind = "sparse_text_with_images_possible_scan"
                pages.append({"page_number": number, "text": chosen, "total_chars": len(text),
                              "offset_start": start, "offset_end": end, "page_complete": end == len(text),
                              "empty_kind": empty_kind})
                # Overlap by the maximum quote length so an ordinary sentence
                # crossing a fragment boundary still has a usable evidence ID.
                for local in range(0, len(chosen), 2000):
                    f_start, f_end = start + local, min(end, start + local + 3000)
                    fid = "frag-" + hashlib.sha256(f"{sha}:{number}:{f_start}:{f_end}".encode()).hexdigest()[:24]
                    fragments.append({"fragment_id": fid, "page_number": number, "start": f_start,
                                      "end": f_end, "text": text[f_start:f_end]})
                budget -= len(chosen)
                if end < len(text):
                    next_cursor = {"page_number": number, "offset": end}
                    break
                if number < total_pages:
                    next_cursor = {"page_number": number + 1, "offset": 0}
                else:
                    next_cursor = None
                if budget <= 0:
                    break
    except JournalError:
        raise
    except limit_error:
        raise JournalError("PDF_RESOURCE_LIMIT", "PDF 解码或页面树超过安全限制") from None
    except Exception:
        raise JournalError("PDF_EXTRACT_ERROR", "PDF 结构损坏或文本提取失败，尚未完成阅读") from None
    return {"file_sha256": sha, "total_pages": total_pages, "pages": pages, "fragments": fragments,
            "next_cursor": next_cursor, "truncated": next_cursor is not None,
            "analysis_limits": ["仅提取文字，不执行文档中的指令或动作", "页码为 PDF 物理页码，不是印刷页码",
                                "不含 OCR、图表识别或公式语义解析，多栏文本顺序可能不准确",
                                "无可提取文字可能是扫描页、图片或空白页，需要进一步核查"]}
