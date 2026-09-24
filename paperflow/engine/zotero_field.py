"""Zotero Live Citation Engine for Word (.docx).

Constructs native Word field codes (ADDIN ZOTERO_ITEM and ADDIN ZOTERO_BIBL)
so that when the generated Word document is opened, Zotero Word Plugin
recognizes them as active, dynamic citations with live bibliography generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

_CITATION_RE = re.compile(r"\[@([A-Za-z0-9_\-:\.]+)(?:,\s*@([A-Za-z0-9_\-:\.]+))*\]")


@dataclass
class TextSegment:
    """Parsed segment of text -- either plain text or a citation."""
    kind: str  # 'text' or 'citation'
    text: str = ""
    keys: List[str] = field(default_factory=list)
    numbers: List[int] = field(default_factory=list)


def parse_citations(text: str) -> Tuple[List[TextSegment], Dict[str, int]]:
    """Parse text with [@KEY] citation markers into text segments and sequential numbers."""
    segments: List[TextSegment] = []
    mapping: Dict[str, int] = {}
    counter = 0
    last_end = 0

    pattern = re.compile(r"\[@([A-Za-z0-9_\-:\.]+(?:,\s*@[A-Za-z0-9_\-:\.]+)*)\]")

    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last_end:
            segments.append(TextSegment(kind="text", text=text[last_end:start]))

        raw_keys = match.group(1)
        keys = [k.strip().lstrip("@") for k in raw_keys.split(",")]

        numbers: List[int] = []
        for key in keys:
            if key not in mapping:
                counter += 1
                mapping[key] = counter
            numbers.append(mapping[key])

        segments.append(TextSegment(kind="citation", keys=keys, numbers=numbers))
        last_end = end

    if last_end < len(text):
        segments.append(TextSegment(kind="text", text=text[last_end:]))

    return segments, mapping


def make_run() -> OxmlElement:
    """Create a w:r run element."""
    return OxmlElement("w:r")


def make_fld_char(char_type: str) -> OxmlElement:
    """Create a w:fldChar element (begin, separate, end)."""
    run = make_run()
    fld_char = OxmlElement("w:fldChar")
    fld_char.set(qn("w:fldCharType"), char_type)
    run.append(fld_char)
    return run


def make_instr_text(text: str) -> OxmlElement:
    """Create a w:instrText element holding Word field instruction."""
    run = make_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = text
    run.append(instr)
    return run


def make_superscript_run(text: str) -> OxmlElement:
    """Create a superscript run for displaying citation numbers like [1]."""
    run = make_run()
    rpr = OxmlElement("w:rPr")
    vert_align = OxmlElement("w:vertAlign")
    vert_align.set(qn("w:val"), "superscript")
    rpr.append(vert_align)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    return run


def zotero_item_to_csl(item_data: Dict[str, Any], user_id: str = "0") -> Dict[str, Any]:
    """Convert raw Zotero metadata to CSL-JSON."""
    item_key = item_data.get("key", "item")
    item_type = item_data.get("itemType", "journalArticle")

    csl = {
        "type": "article-journal" if item_type == "journalArticle" else "book",
        "id": int(hashlib.md5(item_key.encode()).hexdigest()[:8], 16),
        "title": item_data.get("title", "Untitled"),
        "_uris": [f"http://zotero.org/users/{user_id}/items/{item_key}"],
    }

    if "publicationTitle" in item_data:
        csl["container-title"] = item_data["publicationTitle"]
    if "date" in item_data:
        csl["issued"] = {"date-parts": [[item_data.get("date")]]}
    if "DOI" in item_data:
        csl["DOI"] = item_data["DOI"]

    creators = item_data.get("creators", [])
    authors = [
        {"family": c.get("lastName", ""), "given": c.get("firstName", "")}
        for c in creators
        if c.get("creatorType", "author") == "author"
    ]
    if authors:
        csl["author"] = authors

    return csl


def append_zotero_citation_field(
    paragraph,
    citation_keys: List[str],
    display_numbers: List[int],
    item_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    user_id: str = "0",
) -> None:
    """Insert a native Zotero CSL field code into a docx paragraph."""
    p_elem = paragraph._element
    display_text = f"[{','.join(str(n) for n in display_numbers)}]"

    citation_items = []
    for key in citation_keys:
        meta = (item_metadata or {}).get(key, {"key": key, "title": key})
        csl = zotero_item_to_csl(meta, user_id=user_id)
        uris = csl.pop("_uris", [])
        citation_items.append({
            "id": csl["id"],
            "uris": uris,
            "itemData": csl,
        })

    citation_json = {
        "citationID": f"pf_{hashlib.md5(''.join(citation_keys).encode()).hexdigest()[:8]}",
        "properties": {
            "formattedCitation": display_text,
            "plainCitation": display_text,
            "dontUpdate": False,
        },
        "citationItems": citation_items,
        "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
    }

    json_str = json.dumps(citation_json, ensure_ascii=False)
    instr = f"ADDIN ZOTERO_ITEM CSL_CITATION {json_str}"

    p_elem.append(make_fld_char("begin"))
    p_elem.append(make_instr_text(instr))
    p_elem.append(make_fld_char("separate"))
    p_elem.append(make_superscript_run(display_text))
    p_elem.append(make_fld_char("end"))


def append_zotero_bibliography_field(paragraph) -> None:
    """Insert an ADDIN ZOTERO_BIBL field code into a Word paragraph."""
    p_elem = paragraph._element
    instr = 'ADDIN ZOTERO_BIBL {"uncited":[],"custom":[]} CSL_BIBLIOGRAPHY'
    p_elem.append(make_fld_char("begin"))
    p_elem.append(make_instr_text(instr))
    p_elem.append(make_fld_char("separate"))
    p_elem.append(make_fld_char("end"))
