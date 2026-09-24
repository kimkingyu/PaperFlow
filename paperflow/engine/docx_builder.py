"""Word Document Builder (Offline / Direct File Generator).

Builds publication-grade .docx files following Chinese National Standard (GB)
or International Academic Conferences, complete with:
- Standard Heading 1, 2, 3 hierarchy
- Dual fonts: 宋体 (SimSun) for Chinese & Times New Roman for English
- First line indent: 2 characters
- 1.5 line spacing
- Integrated Zotero CSL field codes
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from paperflow.engine.validator import AcademicInvariantValidator
from paperflow.engine.zotero_field import (
    append_zotero_bibliography_field,
    append_zotero_citation_field,
    parse_citations,
)


class AcademicDocxBuilder:
    """Builds publication-ready Word documents adhering to academic standards."""

    def __init__(self, is_chinese: bool = True) -> None:
        self.doc = Document()
        self.is_chinese = is_chinese
        self._setup_page_margins()
        self._configure_styles()

    def _setup_page_margins(self) -> None:
        """Set A4 page size and academic standard margins."""
        for section in self.doc.sections:
            section.page_width = Cm(21.0)
            section.page_height = Cm(29.7)
            # Standard academic margins: Top 3.0cm, Bottom 2.5cm, Left 3.0cm, Right 2.5cm
            section.top_margin = Cm(3.0)
            section.bottom_margin = Cm(2.5)
            section.left_margin = Cm(3.0)
            section.right_margin = Cm(2.5)

    @staticmethod
    def _set_element_fonts(rpr_owner, ascii_font: str, east_asia_font: str) -> None:
        """Safely set w:rFonts on a style or run element."""
        rpr = rpr_owner._element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        rfonts.set(qn("w:ascii"), ascii_font)
        rfonts.set(qn("w:hAnsi"), ascii_font)
        rfonts.set(qn("w:eastAsia"), east_asia_font)
        rfonts.set(qn("w:cs"), ascii_font)

    # Default preset. Values are common conventions, NOT mandated by a national
    # standard; see skills/paper-assistant/references/format_thesis_layout.md §4.
    # (size_pt, centered, space_before_pt, space_after_pt)
    HEADING_PRESET = {
        1: (16, True, 12, 12),
        2: (14, False, 6, 6),
        3: (12, False, 6, 0),
    }

    def _configure_styles(self) -> None:
        """Configure Normal and built-in Heading 1-3 styles."""
        east_asia_body = "宋体" if self.is_chinese else "Times New Roman"
        east_asia_head = "黑体" if self.is_chinese else "Times New Roman"

        normal_style = self.doc.styles["Normal"]
        normal_style.font.name = "Times New Roman"
        normal_style.font.size = Pt(12)  # 小四
        normal_style.font.color.rgb = RGBColor(0, 0, 0)
        self._set_element_fonts(normal_style, "Times New Roman", east_asia_body)

        para_format = normal_style.paragraph_format
        para_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        para_format.space_after = Pt(0)
        para_format.space_before = Pt(0)
        para_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        if self.is_chinese:
            para_format.first_line_indent = Pt(24)  # 2 characters at 12pt

        # Use the real built-in heading styles so Word's TOC and navigation
        # pane recognise them. Strip the default theme colour/italic.
        for level, (size, centered, before, after) in self.HEADING_PRESET.items():
            style = self.doc.styles[f"Heading {level}"]
            style.font.name = "Times New Roman"
            style.font.size = Pt(size)
            style.font.bold = True
            style.font.italic = False
            style.font.color.rgb = RGBColor(0, 0, 0)
            self._set_element_fonts(style, "Times New Roman", east_asia_head)
            pf = style.paragraph_format
            pf.first_line_indent = Pt(0)
            pf.space_before = Pt(before)
            pf.space_after = Pt(after)
            pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            pf.alignment = WD_ALIGN_PARAGRAPH.CENTER if centered else WD_ALIGN_PARAGRAPH.LEFT
            pf.keep_with_next = True

    def add_title(self, title: str) -> None:
        """Add paper title with academic centering and bold font."""
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(12)

        run = p.add_run(title)
        run.bold = True
        run.font.size = Pt(18)  # 二号字
        run.font.name = "Times New Roman"
        self._set_element_fonts(
            run,
            ascii_font="Times New Roman",
            east_asia_font="黑体" if self.is_chinese else "Times New Roman",
        )

    def add_abstract(self, abstract_text: str, keywords: List[str]) -> None:
        """Add abstract and keywords block with academic styling."""
        # Abstract heading
        p_head = self.doc.add_paragraph()
        p_head.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_head.paragraph_format.first_line_indent = Pt(0)
        p_head.paragraph_format.space_before = Pt(8)
        run_h = p_head.add_run("摘  要" if self.is_chinese else "ABSTRACT")
        run_h.bold = True
        run_h.font.size = Pt(13)

        # Abstract body
        p_body = self.doc.add_paragraph()
        p_body.paragraph_format.first_line_indent = Pt(24)
        run_b = p_body.add_run(abstract_text)
        run_b.font.size = Pt(10.5)  # 五号字

        # Keywords
        p_kw = self.doc.add_paragraph()
        p_kw.paragraph_format.first_line_indent = Pt(24)
        p_kw.paragraph_format.space_after = Pt(12)
        run_label = p_kw.add_run("关键词：" if self.is_chinese else "Keywords: ")
        run_label.bold = True
        run_label.font.size = Pt(10.5)

        run_kw = p_kw.add_run("；".join(keywords) if self.is_chinese else "; ".join(keywords))
        run_kw.font.size = Pt(10.5)

    def add_heading(self, text: str, level: int = 1) -> None:
        """Add a heading using Word's built-in Heading 1-3 style (TOC-compatible)."""
        if level not in self.HEADING_PRESET:
            raise ValueError("level must be 1, 2 or 3")
        self.doc.add_paragraph(text, style=f"Heading {level}")

    def add_paragraph_with_citations(
        self,
        text: str,
        global_key_mapping: Optional[Dict[str, int]] = None,
        item_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Add paragraph and convert [@KEY] citations into live Zotero CSL field codes."""
        segments, local_map = parse_citations(text)
        p = self.doc.add_paragraph()

        # Markdown bold/italic parser
        bold_pattern = re.compile(r"\*\*(.+?)\*\*")

        for seg in segments:
            if seg.kind == "text":
                # Handle simple bold
                parts = bold_pattern.split(seg.text)
                for i, part in enumerate(parts):
                    if not part:
                        continue
                    run = p.add_run(part)
                    if i % 2 == 1:
                        run.bold = True
            elif seg.kind == "citation":
                # Compute display numbers from global map if provided
                if global_key_mapping:
                    numbers = [global_key_mapping.get(k, 1) for k in seg.keys]
                else:
                    numbers = seg.numbers

                append_zotero_citation_field(
                    paragraph=p,
                    citation_keys=seg.keys,
                    display_numbers=numbers,
                    item_metadata=item_metadata,
                )

    def add_three_line_table(
        self,
        headers: List[str],
        rows: List[List[str]],
        caption: str = "",
    ) -> None:
        """Add an academic three-line table (顶底粗线 1.5pt, 栏目细线 0.75pt, 无竖线) with an optional caption."""
        if caption:
            p_cap = self.doc.add_paragraph()
            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_cap.paragraph_format.first_line_indent = Pt(0)
            p_cap.paragraph_format.space_before = Pt(6)
            p_cap.paragraph_format.space_after = Pt(3)
            p_cap.paragraph_format.keep_with_next = True
            r_cap = p_cap.add_run(caption)
            r_cap.bold = True
            r_cap.font.size = Pt(10.5)  # 五号字
            r_cap.font.name = "Times New Roman"
            self._set_element_fonts(
                r_cap,
                "Times New Roman",
                "黑体" if self.is_chinese else "Times New Roman",
            )

        num_cols = len(headers)
        if num_cols == 0:
            return
        table = self.doc.add_table(rows=1 + len(rows), cols=num_cols)
        table.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Configure table-level borders: top=12 (1.5pt), bottom=12 (1.5pt), others=none
        tbl_pr = table._tbl.tblPr
        tbl_borders = tbl_pr.find(qn("w:tblBorders"))
        if tbl_borders is not None:
            tbl_pr.remove(tbl_borders)
        tbl_borders = OxmlElement("w:tblBorders")
        for border_name, val, sz in (
            ("top", "single", "12"),
            ("bottom", "single", "12"),
            ("left", "none", "0"),
            ("right", "none", "0"),
            ("insideH", "none", "0"),
            ("insideV", "none", "0"),
        ):
            b = OxmlElement(f"w:{border_name}")
            b.set(qn("w:val"), val)
            if val != "none":
                b.set(qn("w:sz"), sz)
                b.set(qn("w:space"), "0")
                b.set(qn("w:color"), "000000")
            tbl_borders.append(b)
        tbl_pr.append(tbl_borders)

        east_asia_font = "宋体" if self.is_chinese else "Times New Roman"

        # Fill header row and apply 0.75pt (sz=6) bottom border
        hdr_cells = table.rows[0].cells
        for col_idx, text in enumerate(headers):
            cell = hdr_cells[col_idx]
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(str(text))
            run.bold = True
            run.font.size = Pt(10.5)
            run.font.name = "Times New Roman"
            self._set_element_fonts(run, "Times New Roman", east_asia_font)

            tc_pr = cell._tc.get_or_add_tcPr()
            tc_borders = OxmlElement("w:tcBorders")
            b_bottom = OxmlElement("w:bottom")
            b_bottom.set(qn("w:val"), "single")
            b_bottom.set(qn("w:sz"), "6")  # 0.75pt
            b_bottom.set(qn("w:space"), "0")
            b_bottom.set(qn("w:color"), "000000")
            tc_borders.append(b_bottom)
            tc_pr.append(tc_borders)

        # Fill data rows
        for r_idx, row_data in enumerate(rows):
            row_cells = table.rows[r_idx + 1].cells
            for col_idx in range(min(num_cols, len(row_data))):
                cell = row_cells[col_idx]
                p = cell.paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(2)
                run = p.add_run(str(row_data[col_idx]))
                run.font.size = Pt(10.5)
                run.font.name = "Times New Roman"
                self._set_element_fonts(run, "Times New Roman", east_asia_font)

    def add_bibliography_section(self) -> None:
        """Add formal Bibliography section with ADDIN ZOTERO_BIBL dynamic field."""
        self.add_heading("参考文献" if self.is_chinese else "References", level=1)
        p = self.doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(0)
        append_zotero_bibliography_field(p)

    def save(self, filepath: str) -> str:
        """Save the document to file after running invariant validation."""
        AcademicInvariantValidator.verify_docx_builder(self)
        abs_path = os.path.abspath(filepath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        self.doc.save(abs_path)
        return abs_path
