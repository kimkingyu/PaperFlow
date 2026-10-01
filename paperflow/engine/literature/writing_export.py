"""Offline DOCX rendering of a core-validated, literature-supported draft.

This module performs structural defence in depth, not PDF/quote verification or
scientific assessment. Citations are ordinary text, never fabricated Zotero keys.
"""
from __future__ import annotations

import io
import json
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from paperflow.engine.docx_builder import AcademicDocxBuilder
from paperflow.engine.journals.models import JournalError
from paperflow.engine.validator import AcademicInvariantValidator

__all__ = ["render_writing_docx", "export_writing_docx"]

_PARAGRAPH_KINDS = {
    "literature_summary", "literature_inference", "author_proposal",
    "user_material", "placeholder",
}
_CLAIM_KINDS = {"author_claim", "agent_inference", "unverified"}
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_MANUAL_CITATION = re.compile(
    r"\[\s*@|(?<![\w./])@[\w.:-]+|\b(?:ADDIN\s+ZOTERO|ZOTERO_(?:ITEM|BIBL))\b"
    r"|\[\s*\d+(?:\s*[,;，；\-–—]\s*\d+)*\s*\]",
    re.IGNORECASE,
)
_SECRET_PARAMETER = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|api[\-_]?key|signature)",
    re.IGNORECASE,
)


def _fail(message: str, code: str = "INVALID_INPUT") -> None:
    # Error messages contain field names only, never payload text or local paths.
    raise JournalError(code, message) from None


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} 必须是对象")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{field} 必须是列表")
    return value


def _text(value: Any, field: str, *, optional: bool = False, limit: int = 100000) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or _XML_BAD.search(value):
        _fail(f"{field} 必须是有效的有界文本")
    if not optional and not value.strip():
        _fail(f"{field} 不能为空")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _text(value, field, limit=256)
    if value != value.strip() or any(char.isspace() for char in value):
        _fail(f"{field} 必须是无空白的标识符")
    return value


def _ids(value: Any, field: str) -> list[str]:
    values = [_identifier(item, field) for item in _list(value, field)]
    if len(values) != len(set(values)):
        _fail(f"{field} 不能包含重复标识符")
    return values


def _integer(value: Any, field: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _fail(f"{field} 必须是有效整数")
    return value


def _draft_text(value: Any, field: str, *, optional: bool = False) -> str:
    text = _text(value, field, optional=optional)
    if _MANUAL_CITATION.search(unicodedata.normalize("NFKC", text)):
        _fail("草稿不得手工写入引用编号或 Zotero 标记；请使用 citation_ids")
    return text


def _public_url(value: Any) -> str:
    value = _text(value, "references.landing_url", optional=True, limit=4096)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        invalid = (
            parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "\\" in value or any(char.isspace() for char in value)
            or any(
                _SECRET_PARAMETER.search(key)
                for part in (parsed.query, parsed.fragment)
                for key, _ in parse_qsl(part)
            )
        )
    except ValueError:
        invalid = True
    if invalid:
        _fail("references.landing_url 必须是不含凭据的公开 HTTP(S) 地址")
    return value


def _reference(record: Any) -> tuple[str, dict[str, Any]]:
    record = _object(record, "references[]")
    metadata = _object(record.get("paper", record), "references.metadata")
    identities = [
        value for value in (
            record.get("paper_id"), record.get("id"),
            metadata.get("paper_id"), metadata.get("id"),
        ) if value is not None and value != ""
    ]
    if not identities:
        _fail("参考元数据缺少真实 paper_id", "MISSING_REFERENCE")
    paper_id = _identifier(identities[0], "references.paper_id")
    if any(_identifier(value, "references.paper_id") != paper_id for value in identities):
        _fail("参考元数据的论文身份不一致", "CITATION_MISMATCH")
    result: dict[str, Any] = {"title": _text(metadata.get("title"), "references.title")}
    author_values = metadata.get("authors")
    authors = _list([] if author_values is None else author_values, "references.authors")
    names = []
    for author in authors:
        if isinstance(author, str):
            names.append(_text(author, "references.authors[]"))
        elif isinstance(author, dict):
            name = author.get("name") or author.get("literal")
            if name:
                names.append(_text(name, "references.authors.name"))
            else:
                parts = [
                    _text(author.get(key), "references.authors.name", optional=True)
                    for key in ("given", "family")
                ]
                if not any(parts):
                    _fail("references.authors[] 缺少姓名")
                names.append(" ".join(part for part in parts if part))
        else:
            _fail("references.authors[] 必须是姓名或姓名对象")
    result["authors"] = names
    year = metadata.get("year")
    if year is not None and year != "":
        if type(year) is not int or not 1 <= year <= 9999:
            _fail("references.year 必须是年份整数")
        result["year"] = year
    for key in ("venue", "doi", "arxiv_id"):
        value = _text(metadata.get(key), f"references.{key}", optional=True)
        if value:
            result[key] = value
    # Accept a literal arxiv metadata alias, never derive an identifier from paper_id.
    if not result.get("arxiv_id") and metadata.get("arxiv"):
        result["arxiv_id"] = _text(metadata["arxiv"], "references.arxiv")
    result["landing_url"] = _public_url(metadata.get("landing_url"))
    return paper_id, result


def _validate(payload: Any) -> dict[str, Any]:
    payload = _object(payload, "payload")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail("payload 必须是有效 JSON 数据")
    if len(encoded) > 2 * 1024 * 1024:
        _fail("payload 超出 2 MiB 的导出边界")
    project_id = _identifier(payload.get("project_id"), "project_id")
    revision = _integer(payload.get("revision"), "revision")
    profile = _object(payload.get("profile"), "profile")
    _text(profile.get("title"), "profile.title")
    research_question = _text(profile.get("research_question"), "profile.research_question")
    language = profile.get("language", "zh")
    if language not in ("zh", "en"):
        _fail("profile.language 必须是 zh 或 en")
    own_ids: set[str] = set()
    for material in _list(profile.get("own_materials", []), "profile.own_materials"):
        material = _object(material, "profile.own_materials[]")
        material_id = _identifier(material.get("id"), "profile.own_materials.id")
        _text(material.get("text"), "profile.own_materials.text")
        if material_id in own_ids:
            _fail("用户材料标识符不能重复")
        own_ids.add(material_id)
    draft = _object(payload.get("draft"), "draft")
    title = _draft_text(draft.get("title"), "draft.title")
    if draft.get("language", language) != language:
        _fail("profile 与 draft 的语种不一致")
    references = {}
    for record in _list(payload.get("references"), "references"):
        paper_id, metadata = _reference(record)
        if paper_id in references:
            _fail("参考元数据存在重复论文身份", "CITATION_MISMATCH")
        references[paper_id] = metadata
    citations = _object(payload.get("citations"), "citations")
    reading_bindings: dict[str, tuple[str, str]] = {}
    for citation_id, row in citations.items():
        _identifier(citation_id, "citations.id")
        row = _object(row, "citations[]")
        if row.get("citation_id") != citation_id:
            _fail("引用标识符与证据行不一致", "CITATION_MISMATCH")
        paper_id = _identifier(row.get("paper_id"), "citations.paper_id")
        if paper_id not in references:
            _fail("引用缺少对应的真实参考元数据", "MISSING_REFERENCE")
        _identifier(row.get("reading_id"), "citations.reading_id")
        _integer(row.get("claim_index"), "citations.claim_index", minimum=0)
        _text(row.get("section"), "citations.section", optional=True)
        _text(row.get("text"), "citations.text")
        if not isinstance(row.get("kind"), str) or row["kind"] not in _CLAIM_KINDS:
            _fail("证据行的主张类别无效")
        sha = row.get("file_sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            _fail("证据行缺少有效 PDF SHA256")
        binding = (paper_id, sha.lower())
        reading_id = row["reading_id"]
        if reading_id in reading_bindings and reading_bindings[reading_id] != binding:
            _fail("同一阅读卡不能绑定不同论文或 PDF 版本", "CITATION_MISMATCH")
        reading_bindings[reading_id] = binding
        evidence = _list(row.get("evidence"), "citations.evidence")
        if not evidence:
            _fail("证据行缺少 PDF 定位")
        for item in evidence:
            item = _object(item, "citations.evidence[]")
            _identifier(item.get("fragment_id"), "citations.evidence.fragment_id")
            _integer(item.get("page_number"), "citations.evidence.page_number")
            _text(item.get("quote"), "citations.evidence.quote")
            for key in ("paper_id", "reading_id", "file_sha256", "citation_id"):
                if key in item and item[key] != row[key]:
                    _fail("证据定位与论文、阅读卡或版本身份不一致", "CITATION_MISMATCH")

    def checked_ids(values: Any, field: str) -> list[str]:
        values = _ids(values, field)
        if any(value not in citations for value in values):
            _fail("草稿使用了不存在的 citation_id", "UNKNOWN_CITATION")
        return values

    outlines, sections = [], []
    outline_map: dict[str, dict[str, Any]] = {}
    section_ids: set[str] = set()
    for source in _list(draft.get("outline", []), "draft.outline"):
        source = _object(source, "draft.outline[]")
        section_id = _identifier(source.get("id"), "draft.outline.id")
        heading = _draft_text(source.get("title"), "draft.outline.title")
        level = _integer(source.get("level", 1), "draft.outline.level")
        if level not in (1, 2, 3) or section_id in outline_map:
            _fail("大纲章节标识符或标题级别无效")
        outline = {
            "id": section_id, "title": heading, "level": level,
            "evidence_ids": checked_ids(source.get("evidence_ids", []), "draft.outline.evidence_ids"),
        }
        outline_map[section_id] = outline
        outlines.append(outline)
    for source in _list(draft.get("sections", []), "draft.sections"):
        source = _object(source, "draft.sections[]")
        section_id = _identifier(source.get("id"), "draft.sections.id")
        heading = _draft_text(source.get("title"), "draft.sections.title")
        level = _integer(source.get("level", 1), "draft.sections.level")
        if level not in (1, 2, 3) or section_id in section_ids:
            _fail("正文章节标识符或标题级别无效")
        section_ids.add(section_id)
        if section_id in outline_map:
            outline = outline_map[section_id]
            if heading != outline["title"] or level != outline["level"]:
                _fail("正文与大纲的章节绑定不一致", "CITATION_MISMATCH")
        paragraphs = []
        for source_paragraph in _list(source.get("paragraphs"), "draft.sections.paragraphs"):
            paragraph = _object(source_paragraph, "draft.sections.paragraphs[]")
            kind = paragraph.get("kind")
            if not isinstance(kind, str) or kind not in _PARAGRAPH_KINDS:
                _fail("草稿段落类别无效")
            text = _draft_text(paragraph.get("text"), "draft.paragraph.text", optional=kind == "placeholder")
            paragraph_citations = checked_ids(paragraph.get("citation_ids", []), "draft.paragraph.citation_ids")
            materials = _ids(paragraph.get("own_material_ids", []), "draft.paragraph.own_material_ids")
            if any(material_id not in own_ids for material_id in materials):
                _fail("草稿绑定了不存在的用户材料")
            if kind in ("literature_summary", "literature_inference") and not paragraph_citations:
                _fail("文献段落必须绑定真实 citation_ids")
            if kind == "user_material" and not materials:
                _fail("用户材料段落必须绑定真实 own_material_ids")
            for citation_id in paragraph_citations:
                row = citations[citation_id]
                if row["kind"] == "unverified":
                    _fail("未核实主张不能作为正文引用证据")
                if kind == "literature_summary" and row["kind"] != "author_claim":
                    _fail("Agent 推断不得伪装成文献作者主张")
                # ReadingClaim.section describes the source paper, not this draft.
                for key in ("section_id", "draft_section_id"):
                    if key in row and row[key] != section_id:
                        _fail("引用与草稿章节绑定不一致", "CITATION_MISMATCH")
                if "section_ids" in row and section_id not in _ids(row["section_ids"], "citations.section_ids"):
                    _fail("引用与草稿章节绑定不一致", "CITATION_MISMATCH")
            paragraphs.append({"text": text, "kind": kind, "citation_ids": paragraph_citations})
        sections.append({"id": section_id, "title": heading, "level": level, "paragraphs": paragraphs})
    if not outlines and not sections:
        _fail("草稿至少需要一个大纲或正文章节")
    gaps = _list(payload.get("gaps"), "gaps")
    for gap in gaps:
        if not isinstance(gap, (str, dict)):
            _fail("gaps[] 必须是文本或对象")
        if isinstance(gap, str):
            _text(gap, "gaps[]")
    _object(payload.get("coverage"), "coverage")
    # Partial chapter saves are supported. Unsaved outline chapters stay placeholders.
    section_map = {section["id"]: section for section in sections}
    ordered = [
        section_map.get(outline["id"], {**outline, "paragraphs": []})
        for outline in outlines
    ]
    ordered.extend(section for section in sections if section["id"] not in outline_map)
    return {
        "project_id": project_id, "revision": revision, "title": title,
        "language": language, "research_question": research_question,
        "sections": ordered, "outlines": outlines, "citations": citations,
        "references": references, "gaps": gaps,
    }


def _gap_note(value: Any) -> str:
    # Do not dump arbitrary coverage, acquisition, raw-source or credential fields.
    if isinstance(value, dict):
        parts = [value.get(key) for key in ("code", "message", "reason", "text", "description")]
        missing = value.get("missing")
        if isinstance(missing, list):
            parts.extend(part for part in missing if isinstance(part, str))
        value = "；".join(part for part in parts if isinstance(part, str) and part)
    if not value:
        return "存在待核实缺口；请在项目中检查详情。"
    value = _text(value, "gaps.note")
    value = re.sub(r"[A-Za-z]:[\\/][^\s；;,，]+|\\\\[^\s；;,，]+", "<本地路径已省略>", value)
    value = re.sub(r"(?<![\w:])/(?:[^/\s]+/)+[^\s；;,，]+", "<本地路径已省略>", value)
    value = re.sub(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|token|password|secret|authorization)\s*[:=]\s*\S+",
        "<凭据已省略>", value,
    )
    # A gap can itself contain a private URL; its locating details are not needed.
    value = re.sub(r"https?://[^\s；;,，]+", "<地址详情已省略>", value)
    return value


def _reference_line(number: int, reference: dict[str, Any]) -> str:
    parts = [reference["title"]]
    if reference["authors"]:
        parts.append("; ".join(reference["authors"]))
    if "year" in reference:
        parts.append(str(reference["year"]))
    if reference.get("venue"):
        parts.append(reference["venue"])
    for key, label in (("doi", "DOI"), ("arxiv_id", "arXiv"), ("landing_url", "URL")):
        if reference.get(key):
            parts.append(f"{label}: {reference[key]}")
    return f"[{number}] " + " | ".join(parts)


def render_writing_docx(payload: dict[str, Any]) -> bytes:
    """Render validated source locators into bytes, without disk/Word/network I/O.

    Current PDF hashes and quotes must already have been checked by the core.
    No Zotero field conversion is performed, including for incidental raw text.
    """
    data = _validate(payload)
    zh = data["language"] == "zh"
    builder = AcademicDocxBuilder(is_chinese=zh)
    builder.add_title(data["title"])
    builder.doc.core_properties.title = data["title"][:255]
    builder.doc.core_properties.subject = "文献支持的可修改草稿" if zh else "Editable literature-supported draft"
    notice = (
        "文献支持的可修改草稿，不是已完成实验或已验证研究结果。版式采用通用学术惯例，"
        "不代表已符合任何学校或期刊标准。使用普通编号引用；不包含原生 Zotero 域。"
        if zh else
        "Editable literature-supported draft, not completed experiments or verified research results. "
        "Formatting follows general academic conventions, not certified journal or institutional standards. "
        "Citations are ordinary numbered text, not native Zotero fields."
    )
    builder.doc.add_paragraph(notice)
    builder.doc.add_paragraph(
        ("项目：" if zh else "Project: ") + data["project_id"]
        + ("；修订：" if zh else "; revision: ") + str(data["revision"])
    )
    builder.doc.add_paragraph(("研究问题：" if zh else "Research question: ") + data["research_question"])
    labels = {
        "literature_summary": "【文献作者主张综述】" if zh else "[Literature author-claim summary] ",
        "literature_inference": "【文献推断／非作者原述，待语义核查】" if zh else "[Literature inference / not the authors' own claim; needs semantic review] ",
        "author_proposal": "【作者建议／待验证，非已完成实验】" if zh else "[Author proposal / unverified, not a completed experiment] ",
        "user_material": "【用户材料／未独立核验】" if zh else "[User material / not independently verified] ",
        "placeholder": "【占位／待补充，非已验证结果】" if zh else "[Placeholder / incomplete, not verified results] ",
    }
    numbers: dict[str, int] = {}
    cited_ids: list[str] = []
    for section in data["sections"]:
        builder.add_heading(section["title"], level=section["level"])
        if not section["paragraphs"]:
            builder.doc.add_paragraph(labels["placeholder"] + ("本章节尚未提供正文。" if zh else "No chapter text has been supplied."))
        for paragraph in section["paragraphs"]:
            paragraph_numbers = []
            for citation_id in paragraph["citation_ids"]:
                if citation_id not in cited_ids:
                    cited_ids.append(citation_id)
                paper_id = data["citations"][citation_id]["paper_id"]
                if paper_id not in numbers:
                    numbers[paper_id] = len(numbers) + 1
                if numbers[paper_id] not in paragraph_numbers:
                    paragraph_numbers.append(numbers[paper_id])
            suffix = "".join(f"[{number}]" for number in paragraph_numbers)
            text = paragraph["text"] or ("待补充。" if zh else "To be supplied.")
            builder.doc.add_paragraph(labels[paragraph["kind"]] + text + (" " + suffix if suffix else ""))
    builder.add_heading("参考信息（按正文首次引用顺序）" if zh else "References (first appearance in the text)")
    if not numbers:
        builder.doc.add_paragraph("暂无正文编号引用；不生成虚构参考文献。" if zh else "No numbered body citations; no references are invented.")
    for paper_id, number in numbers.items():
        builder.doc.add_paragraph(_reference_line(number, data["references"][paper_id]))
    builder.add_heading("证据定位附录" if zh else "Evidence locator appendix")
    builder.doc.add_paragraph(
        "仅列出处定位，不默认复制原文。页码是 PDF 物理页码，不等同于印刷页码。"
        "来源校验不保证语义正确、同行评审或全文理解；大纲素材不自动构成正文编号引用。"
        if zh else
        "Locators only; source quotes are not copied by default. Pages are physical PDF pages, not printed page labels. "
        "Provenance checks do not verify semantics, peer review or full-paper understanding. Outline material is not automatically a body citation."
    )
    appendix_ids = list(cited_ids)
    for outline in data["outlines"]:
        for citation_id in outline["evidence_ids"]:
            if citation_id not in appendix_ids:
                appendix_ids.append(citation_id)
    rows = []
    claim_labels = {
        "author_claim": "作者主张" if zh else "Author claim",
        "agent_inference": "Agent 推断／非作者原述" if zh else "Agent inference / not the authors' claim",
        "unverified": "待核实／不可作论据" if zh else "Unverified / not supporting evidence",
    }
    for citation_id in appendix_ids:
        row = data["citations"][citation_id]
        kind = claim_labels[row["kind"]]
        if citation_id not in cited_ids:
            kind += "；仅大纲素材" if zh else "; outline material only"
        for evidence in row["evidence"]:
            rows.append([
                citation_id, data["references"][row["paper_id"]]["title"], row["reading_id"],
                row["file_sha256"], str(evidence["page_number"]), evidence["fragment_id"], kind,
            ])
    if rows:
        headers = (
            ["引用ID", "文献标题", "阅读卡ID", "PDF SHA256", "物理页码", "片段ID", "证据类别"]
            if zh else ["Citation ID", "Paper title", "Reading ID", "PDF SHA256", "Physical page", "Fragment ID", "Claim kind"]
        )
        builder.add_three_line_table(headers, rows)
    else:
        builder.doc.add_paragraph("尚无可列出的文献证据定位。" if zh else "No literature evidence locators are available.")
    builder.add_heading("缺口与核验边界" if zh else "Gaps and verification boundaries")
    builder.doc.add_paragraph(
        "导出器只核对引用、参考信息和章节的结构绑定；当前 PDF SHA 和短引由核心预先核对。"
        "不验证用户材料、科学结论、实验完成状态或语义正确性，也不宣称已经读懂全文。"
        if zh else
        "The exporter checks structural citation/reference/chapter bindings only; the core must recheck current PDF hashes and quotes. "
        "User material, scientific conclusions, experiment completion and semantic correctness are not independently verified; full-paper understanding is not claimed."
    )
    for gap in data["gaps"]:
        builder.doc.add_paragraph(("待核实：" if zh else "Needs verification: ") + _gap_note(gap))
    if not data["gaps"]:
        builder.doc.add_paragraph("系统未列出额外缺口；这不等于研究完成或结论已获证实。" if zh else "No additional gaps were supplied; this does not establish completed research or verified conclusions.")
    AcademicInvariantValidator.verify_docx_builder(builder)
    stream = io.BytesIO()
    builder.doc.save(stream)  # AcademicDocxBuilder.save creates directories: do not use it.
    return stream.getvalue()


def _no_redirects(path: Path) -> None:
    # Inspect ancestors first: do not follow a redirected parent to stat its child.
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path:
                continue
            _fail("输出父目录必须已经存在", "INVALID_OUTPUT_PATH")
        except OSError:
            _fail("输出路径必须可访问", "INVALID_OUTPUT_PATH")
        else:
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                # Reject all Windows reparse points, including junctions and symlinks.
                _fail("输出路径不能经过符号链接、junction 或 reparse point", "INVALID_OUTPUT_PATH")


def _target_path(output_path: Any, overwrite: bool) -> Path:
    try:
        raw = os.fspath(output_path)
    except TypeError:
        _fail("须明确提供本地绝对 .docx 输出路径", "INVALID_OUTPUT_PATH")
    if not isinstance(raw, str) or not raw.strip() or _XML_BAD.search(raw) or any(char in raw for char in "\r\n\t"):
        _fail("须明确提供本地绝对 .docx 输出路径", "INVALID_OUTPUT_PATH")
    raw = raw.strip()
    if raw.replace("\\", "/").startswith("//") or "://" in raw or raw.lower().startswith("file:"):
        _fail("不允许网络、URL 或 UNC 输出路径", "INVALID_OUTPUT_PATH")
    candidate = Path(raw)
    if not candidate.is_absolute() or candidate.suffix.lower() != ".docx" or ".." in candidate.parts:
        _fail("须明确提供本地绝对 .docx 输出路径", "INVALID_OUTPUT_PATH")
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if any(":" in part or part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved for part in candidate.parts[1:]):
            _fail("输出路径包含 Windows 保留名称或 alternate data stream", "INVALID_OUTPUT_PATH")
        import ctypes
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor))
        if drive_type in (0, 1, 4):
            _fail("输出目录必须位于可访问的本地卷", "INVALID_OUTPUT_PATH")
    _no_redirects(candidate)
    try:
        parent = candidate.parent.resolve(strict=True)
        parent_is_directory = parent.is_dir()
        target = parent / candidate.name
        exists, is_file = target.exists(), target.is_file()
    except (OSError, RuntimeError):
        _fail("输出父目录必须已存在且可访问", "INVALID_OUTPUT_PATH")
    if not parent_is_directory:
        _fail("输出父目录必须是现有本地目录", "INVALID_OUTPUT_PATH")
    _no_redirects(target)
    if exists and not is_file:
        _fail("输出目标必须是普通文件", "INVALID_OUTPUT_PATH")
    if exists and not overwrite:
        _fail("输出文件已存在；未覆盖原文件", "OUTPUT_EXISTS")
    return target


def export_writing_docx(
    payload: dict[str, Any], output_path: str | Path, overwrite: bool = False,
) -> Path:
    """Publish a completed local DOCX atomically; do not overwrite by default."""
    if type(overwrite) is not bool:
        _fail("overwrite 必须是布尔值")
    target = _target_path(output_path, overwrite)
    blob = render_writing_docx(payload)  # Invalid input never creates a temporary file.
    staged: Path | None = None
    try:
        parent_identity = target.parent.stat()
        if _target_path(output_path, overwrite) != target:
            _fail("输出目录在导出期间改变", "INVALID_OUTPUT_PATH")
        descriptor, temporary = tempfile.mkstemp(prefix=".paperflow-writing-", suffix=".docx", dir=str(target.parent))
        staged = Path(temporary)
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if _target_path(output_path, overwrite) != target:
            _fail("输出目录在导出期间改变", "INVALID_OUTPUT_PATH")
        current = target.parent.stat()
        if (current.st_dev, current.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
            _fail("输出目录在导出期间改变", "INVALID_OUTPUT_PATH")
        _no_redirects(staged)
        if overwrite:
            os.replace(staged, target)
        else:
            # Same-directory, same-volume exclusive publication: a raced target wins.
            os.link(staged, target)
    except FileExistsError:
        _fail("输出文件已存在；未覆盖原文件", "OUTPUT_EXISTS")
    except OSError:
        _fail("草稿导出失败，请检查本地目录及文件权限", "EXPORT_FAILED")
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                _fail("无法清理草稿临时文件，请检查本地目录权限", "EXPORT_FAILED")
    return target
