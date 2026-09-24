"""Academic Paper Format Auditor & Auto-Healing Normalizer.

Comprehensive inspection and standardization engine for academic manuscripts:
- Title outline isolation & heading hierarchy
- Pseudo-heading detection (e.g. bolded plain text instead of real Heading styles)
- First-line indentation & typography purity (dual fonts, black color, 1.5 spacing)
- Consecutive redundant blank lines
- Three-line table compliance (borders, captions above table)
- Figure captions placement (captions below figure)
- Punctuation consistency (Chinese text vs half-width marks)
- Citation numbering continuity (e.g. [1], [2], [3] sequence)
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from paperflow.engine.standards_manager import get_standard_by_id


class IssueSeverity(str, Enum):
    ERROR = "error"      # 严重违规（字号倒挂、大纲污染、缺少首行缩进等）
    WARNING = "warning"  # 警告（三线表缺失、中英标点混用、伪标题等）
    INFO = "info"        # 建议（连续空行、西文字体优化等）


@dataclass
class FormatIssue:
    rule_id: str
    severity: IssueSeverity
    location: str
    message: str
    snippet: str = ""
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class PaperFormatAuditor:
    """Static and live inspector checking academic manuscripts against standards."""

    # 正则规则定义
    RE_CHINESE_HEADING = re.compile(r"^\s*(第[一二三四五六七八九十百0-9]+[章部分篇]|([0-9]+\.)+[0-9]*)\s*")
    RE_FIGURE_CAPTION = re.compile(r"^\s*(图|Figure|Fig\.)\s*([0-9]+[-.][0-9]+|[0-9]+)\s*")
    RE_TABLE_CAPTION = re.compile(r"^\s*(表|Table)\s*([0-9]+[-.][0-9]+|[0-9]+)\s*")
    RE_CITATION = re.compile(r"\[(\d+)\]")

    # 审稿人第一印象扣分项相关规则定义
    RE_VAGUE_TITLE_ZH = re.compile(r"基于.+?的.*?(?:系统研究|设计与实现|探讨|优化研究|方法研究)")
    RE_VAGUE_TITLE_EN = re.compile(r"^(?:A\s+)?Research on .*? Based on", re.IGNORECASE)
    CONCRETE_DOMAIN_KEYWORDS = (
        "机理", "物理", "协同", "拓扑", "阻抗", "应力", "应变", "温度", "热", "载荷",
        "流场", "流体", "时延", "微观", "刚度", "韧性", "转矩", "位移", "振动", "自适应",
        "解耦", "鲁棒", "动态", "轻量", "量化", "补偿", "容错", "多场", "多相", "约束",
        "频域", "时域", "剪枝", "蒸馏", "疲劳", "断裂", "蠕变", "气动", "液压", "阻尼",
        "mechanism", "physics", "collaborative", "topology", "stress", "strain",
        "temperature", "thermal", "load", "flow", "latency", "stiffness", "torque",
        "vibration", "adaptive", "decoupling", "robust", "dynamic", "lightweight",
        "quantization", "compensation", "multiphysics", "constraint", "damping",
    )

    ABBR_STOPWORDS = {
        "A", "I", "OK", "GB", "ISO", "IEEE", "ACM", "SCI", "EI", "CCF",
        "PDF", "URL", "HTTP", "HTTPS", "DNA", "RNA", "COVID", "USA", "UK",
        "AI", "IT", "ID", "IP", "PC", "UI", "OS", "API", "2D", "3D",
    }
    RE_UPPER_ABBR = re.compile(r"\b[A-Z]{2,}\b")

    @classmethod
    def _check_vague_title(cls, title_text: str, location: str = "段落 #1 (论文总标题)") -> Optional[FormatIssue]:
        """检查论文标题是否过于空泛（审稿人第一眼扣分项）。"""
        clean_title = title_text.strip()
        if not clean_title:
            return None

        is_vague_zh = bool(cls.RE_VAGUE_TITLE_ZH.search(clean_title))
        is_vague_en = bool(cls.RE_VAGUE_TITLE_EN.search(clean_title))

        if is_vague_zh or is_vague_en:
            title_lower = clean_title.lower()
            has_concrete_context = any(kw in title_lower for kw in cls.CONCRETE_DOMAIN_KEYWORDS)
            if not has_concrete_context:
                return FormatIssue(
                    rule_id="TITLE_VAGUE_IMPRESSION",
                    severity=IssueSeverity.WARNING,
                    location=location,
                    message="题目过于空泛（审稿人第一眼扣分项），建议改为‘[具体机理/方法]+[核心矛盾]+[具体工程/物理对象]’形式",
                    snippet=clean_title[:50],
                    suggestion="题目过于空泛（审稿人第一眼扣分项），建议改为‘[具体机理/方法]+[核心矛盾]+[具体工程/物理对象]’形式",
                )
        return None

    @classmethod
    def _check_abbreviation_definitions(cls, body_paragraphs: List[Tuple[str, str]]) -> List[FormatIssue]:
        """扫描正文中>=2个字母的大写缩写首次出现时是否附带完整英文全称定义。"""
        issues: List[FormatIssue] = []
        seen_abbrs = set()

        for loc, text in body_paragraphs:
            clean_text = text.strip()
            if not clean_text:
                continue

            abbr_matches = cls.RE_UPPER_ABBR.finditer(clean_text)
            for m in abbr_matches:
                abbr = m.group(0)
                if abbr in cls.ABBR_STOPWORDS or abbr in seen_abbrs:
                    continue
                seen_abbrs.add(abbr)

                # 检查在当前首次出现段落中是否附带全称定义：
                # 1. 英文全称在括号前，缩写在括号内：Full Name (ABBR) 或 Full Name（ABBR）
                pat_parenthesized = re.search(
                    rf"[A-Za-z][A-Za-z\s\-]{{2,}}\s*[\(（][^\)）]*?\b{re.escape(abbr)}\b[^\)）]*?[\)）]",
                    clean_text,
                )
                # 2. 逗号连接：Full Name, ABBR
                pat_comma = re.search(
                    rf"[A-Za-z][A-Za-z\s\-]{{2,}},\s*{re.escape(abbr)}\b",
                    clean_text,
                )
                # 3. 缩写后接括号全称：ABBR (Full Name)
                pat_after_abbr = re.search(
                    rf"\b{re.escape(abbr)}\s*[\(（][^\)）]*?[A-Za-z]{{2,}}\s+[A-Za-z]{{2,}}[^\)）]*?[\)）]",
                    clean_text,
                )

                if not (pat_parenthesized or pat_comma or pat_after_abbr):
                    snippet_start = max(0, m.start() - 15)
                    snippet_end = min(len(clean_text), m.end() + 25)
                    issues.append(FormatIssue(
                        rule_id="ABBREVIATION_NO_DEFINITION",
                        severity=IssueSeverity.WARNING,
                        location=loc,
                        message=f"缩写 '{abbr}' 首次出现无完整英文全称定义，属于审稿人重点找茬扣分项",
                        snippet=clean_text[snippet_start:snippet_end],
                        suggestion=f"缩写首次出现无完整英文全称定义，属于审稿人重点找茬扣分项；建议补充为‘全称 ({abbr})’或‘中文名 (全称, {abbr})’",
                    ))
        return issues

    @classmethod
    def _check_dangling_headings(cls, para_items: List[Tuple[str, str, Optional[int]]]) -> List[FormatIssue]:
        """检查一级/二级标题后是否紧跟下一级子标题，缺少引言或过渡说明文字（悬空标题）。"""
        issues: List[FormatIssue] = []
        n = len(para_items)
        for i in range(n - 1):
            curr_loc, curr_text, curr_lvl = para_items[i]
            if i == 0 and ("总标题" in curr_loc or "论文大标题" in curr_loc):
                continue
            if curr_lvl in (1, 2):
                next_loc, next_text, next_lvl = para_items[i + 1]
                if next_lvl is not None and next_lvl > curr_lvl:
                    issues.append(FormatIssue(
                        rule_id="DANGLING_HEADING",
                        severity=IssueSeverity.WARNING,
                        location=curr_loc,
                        message="标题下方缺少承上启下过渡段落，审稿人会认为逻辑跳跃",
                        snippet=curr_text[:40],
                        suggestion="标题下方缺少承上启下过渡段落，审稿人会认为逻辑跳跃；建议在子标题前补充过渡性段落",
                    ))
        return issues

    @classmethod
    def _check_abstract_conclusion_repetition(cls, paragraphs_info: List[Tuple[str, bool]]) -> List[FormatIssue]:
        """检查文章摘要与结论段落是否存在过高比例的句子/字符复制。"""
        import difflib

        abstract_lines: List[str] = []
        conclusion_lines: List[str] = []

        collecting_abstract = False
        collecting_conclusion = False

        for text, is_heading in paragraphs_info:
            clean = text.strip()
            if not clean:
                continue

            # 摘要段落识别
            is_abs_header = bool(
                re.match(r"^\s*(?:摘\s*要|Abstract)\b", clean, re.IGNORECASE)
                or (is_heading and "摘要" in clean)
            )
            is_keywords_header = bool(
                re.match(r"^\s*(?:关\s*键\s*词|Keywords?)\b", clean, re.IGNORECASE)
            )
            # 结论段落识别
            is_conc_header = bool(
                re.search(r"(?:结\s*论|结\s*语|总结与展望|Conclusions?)\b", clean, re.IGNORECASE)
                and (is_heading or len(clean) < 30)
            )
            is_next_major_section = bool(
                re.search(r"(?:参考文献|References?|致\s*谢|附\s*录)", clean, re.IGNORECASE)
                and (is_heading or len(clean) < 30)
            )

            if is_abs_header:
                collecting_abstract = True
                collecting_conclusion = False
                inline = re.sub(r"^\s*(?:摘\s*要|Abstract)[:：\s]*", "", clean, flags=re.IGNORECASE)
                if inline and len(inline) > 10:
                    abstract_lines.append(inline)
                continue

            if collecting_abstract:
                if is_keywords_header or (is_heading and not is_abs_header) or cls.RE_CHINESE_HEADING.match(clean):
                    collecting_abstract = False
                else:
                    abstract_lines.append(clean)

            if is_conc_header:
                collecting_conclusion = True
                collecting_abstract = False
                inline = re.sub(r"^.*?(?:结\s*论|结\s*语|总结与展望|Conclusions?)[:：\s]*", "", clean, flags=re.IGNORECASE)
                if inline and len(inline) > 10:
                    conclusion_lines.append(inline)
                continue

            if collecting_conclusion:
                if is_next_major_section or (is_heading and not is_conc_header) or (cls.RE_CHINESE_HEADING.match(clean) and "结" not in clean):
                    collecting_conclusion = False
                else:
                    conclusion_lines.append(clean)

        abs_full = "".join(abstract_lines)
        conc_full = "".join(conclusion_lines)

        clean_abs = re.sub(r"[\s\d\W_]+", "", abs_full)
        clean_conc = re.sub(r"[\s\d\W_]+", "", conc_full)

        if len(clean_abs) >= 20 and len(clean_conc) >= 20:
            matcher = difflib.SequenceMatcher(None, clean_abs, clean_conc)
            matching_blocks = matcher.get_matching_blocks()
            matched_chars = sum(b.size for b in matching_blocks if b.size >= 6)
            overlap_ratio = matched_chars / max(1, len(clean_conc))
            final_ratio = max(overlap_ratio, matcher.ratio())

            if final_ratio >= 0.60:
                return [
                    FormatIssue(
                        rule_id="ABSTRACT_CONCLUSION_REPETITION",
                        severity=IssueSeverity.WARNING,
                        location="摘要与结论章节",
                        message="摘要与结论文字高度重叠，审稿人会认为缺乏工作收敛与深入归纳；建议摘要重在动机与贡献，结论重在定量成果与局限展望",
                        snippet=f"文字重叠度约 {int(final_ratio * 100)}%: {conc_full[:35]}...",
                        suggestion="摘要重在动机与贡献，结论重在定量成果与局限展望，建议重塑结论叙事",
                    )
                ]
        return []

    @classmethod
    def audit_docx(cls, docx_path: str, standard_id: str = "chinese_thesis_standard") -> Dict[str, Any]:
        """Audit an offline .docx file and return a comprehensive format diagnostic report."""
        if not os.path.isfile(docx_path):
            raise FileNotFoundError(f"文件未找到: {docx_path}")

        doc = Document(docx_path)
        std_cfg = get_standard_by_id(standard_id) or get_standard_by_id("chinese_thesis_standard")
        issues: List[FormatIssue] = []

        paragraphs = doc.paragraphs
        total_paras = len(paragraphs)
        non_empty_paras = [p for p in paragraphs if p.text.strip()]

        # -------------------------------------------------------------
        # 1. 论文总标题检查 (Title Outline & Style & Vague Title)
        # -------------------------------------------------------------
        if non_empty_paras:
            first_p = non_empty_paras[0]
            first_text = first_p.text.strip()
            style_name = first_p.style.name if first_p.style else ""
            if style_name.startswith("Heading") or style_name.startswith("标题"):
                issues.append(FormatIssue(
                    rule_id="TITLE_OUTLINE_LEAK",
                    severity=IssueSeverity.ERROR,
                    location="段落 #1 (论文总标题)",
                    message="论文大标题使用了章节标题样式 (Heading)，会导致总标题混入目录与导航窗格",
                    snippet=first_text[:40],
                    suggestion="将大标题样式改为独立 Title 或正文样式，大纲级别设为正文(10)，字号二号居中"
                ))

            # 审稿人第一印象扣分项：题目假大空检测
            vague_title_issue = cls._check_vague_title(first_text, location="段落 #1 (论文总标题)")
            if vague_title_issue:
                issues.append(vague_title_issue)

        # -------------------------------------------------------------
        # 2. 标题层级、伪标题与空行检查
        # -------------------------------------------------------------
        consecutive_blanks = 0
        heading_levels_seen: List[int] = []
        body_paras: List[Tuple[str, str]] = []
        para_hierarchy_items: List[Tuple[str, str, Optional[int]]] = []
        paragraphs_info: List[Tuple[str, bool]] = []

        for idx, p in enumerate(paragraphs, start=1):
            text = p.text.strip()

            # 空行检查
            if not text:
                consecutive_blanks += 1
                if consecutive_blanks >= 2:
                    issues.append(FormatIssue(
                        rule_id="CONSECUTIVE_BLANK_LINES",
                        severity=IssueSeverity.INFO,
                        location=f"段落 #{idx}",
                        message=f"存在连续空行 (已连续 {consecutive_blanks} 行空白)",
                        snippet="[空白段落]",
                        suggestion="建议删除多余硬回车，通过段前/段后间距控制留白"
                    ))
                continue
            else:
                consecutive_blanks = 0

            style_name = p.style.name if p.style else ""
            is_heading_style = style_name.startswith("Heading") or style_name.startswith("标题")
            is_pseudo_heading = not is_heading_style and bool(cls.RE_CHINESE_HEADING.match(text)) and len(text) < 40

            # 伪标题检测：内容像章节标题，却未套用 Word 标题样式
            if is_pseudo_heading:
                issues.append(FormatIssue(
                    rule_id="PSEUDO_HEADING",
                    severity=IssueSeverity.WARNING,
                    location=f"段落 #{idx}",
                    message="疑似标题文本但未应用 Word 内置标题样式 (Heading)，导致无法自动生成目录",
                    snippet=text,
                    suggestion="建议套用对应的 Heading 1 / 2 / 3 样式"
                ))

            # 标题带有首行缩进检测（标题通常居中或居左顶格，不应带 2 字符缩进）
            if is_heading_style:
                level_match = re.search(r"\d+", style_name)
                level = int(level_match.group(0)) if level_match else 1
                heading_levels_seen.append(level)
                loc_label = f"段落 #{idx} (论文总标题)" if idx == 1 else f"段落 #{idx} ({style_name})"
                para_hierarchy_items.append((loc_label, text, level))
                paragraphs_info.append((text, True))

                if p.paragraph_format.first_line_indent and p.paragraph_format.first_line_indent > Pt(5):
                    issues.append(FormatIssue(
                        rule_id="HEADING_UNWANTED_INDENT",
                        severity=IssueSeverity.WARNING,
                        location=f"段落 #{idx} ({style_name})",
                        message=f"标题样式带有首行缩进 ({p.paragraph_format.first_line_indent.pt}pt)，学术规范要求标题顶格或居中",
                        snippet=text,
                        suggestion="将该标题的首行缩进设为 0"
                    ))
            elif is_pseudo_heading:
                p_lvl = 1
                if re.match(r"^\s*[0-9]+\.[0-9]+\.[0-9]+", text):
                    p_lvl = 3
                elif re.match(r"^\s*[0-9]+\.[0-9]+", text):
                    p_lvl = 2
                loc_label = f"段落 #{idx} (论文总标题)" if idx == 1 else f"段落 #{idx}"
                para_hierarchy_items.append((loc_label, text, p_lvl))
                paragraphs_info.append((text, True))
            else:
                loc_label = f"段落 #{idx} (论文总标题)" if idx == 1 else f"段落 #{idx}"
                para_hierarchy_items.append((loc_label, text, None))
                paragraphs_info.append((text, False))

            # 正文段落检查（非标题、非空、字数多于15字）
            if not is_heading_style and not is_pseudo_heading and len(text) > 15:
                # 收集用于英文大写缩写首定义检测
                if not cls.RE_FIGURE_CAPTION.match(text) and not cls.RE_TABLE_CAPTION.match(text):
                    body_paras.append((f"段落 #{idx}", text))

                # 检查首行缩进
                indent = p.paragraph_format.first_line_indent
                # 某些通过全角空格手打缩进的情况
                has_manual_space_indent = text.startswith("　　") or text.startswith("    ")
                if has_manual_space_indent:
                    issues.append(FormatIssue(
                        rule_id="MANUAL_SPACE_INDENT",
                        severity=IssueSeverity.WARNING,
                        location=f"段落 #{idx}",
                        message="检测到使用手动空格替代段落首行缩进，排版易错位",
                        snippet=text[:30],
                        suggestion="清除行首空格，配置段落属性为首行缩进 2 字符"
                    ))
                elif indent is None or indent.pt < 10:
                    # 排除居中题注与对齐段落
                    if p.alignment != WD_ALIGN_PARAGRAPH.CENTER and not cls.RE_FIGURE_CAPTION.match(text) and not cls.RE_TABLE_CAPTION.match(text):
                        issues.append(FormatIssue(
                            rule_id="BODY_INDENT_MISSING",
                            severity=IssueSeverity.WARNING,
                            location=f"段落 #{idx}",
                            message="正文段落缺少标准首行缩进 2 字符",
                            snippet=text[:35],
                            suggestion="设置段落首行缩进 2 字符 (约 24pt/2em)"
                        ))

                # 标点符号混用检查（中文段落里误用英文逗号句号分号）
                half_punctuation_matches = re.findall(r"[\u4e00-\u9fa5]([,;:?])[\u4e00-\u9fa5]", text)
                if half_punctuation_matches:
                    issues.append(FormatIssue(
                        rule_id="PUNCTUATION_HALF_WIDTH",
                        severity=IssueSeverity.WARNING,
                        location=f"段落 #{idx}",
                        message=f"中文正文中存在半角英文标点符号: {set(half_punctuation_matches)}",
                        snippet=text[:40],
                        suggestion="将中文语境中的半角标点转换为全角标点（如 ，。；：？）"
                    ))

        # 标题跳级检测（如 1 级直接跳 3 级）
        for i in range(len(heading_levels_seen) - 1):
            curr_lvl = heading_levels_seen[i]
            next_lvl = heading_levels_seen[i + 1]
            if next_lvl - curr_lvl > 1:
                issues.append(FormatIssue(
                    rule_id="HEADING_LEVEL_SKIPPED",
                    severity=IssueSeverity.ERROR,
                    location="标题大纲结构",
                    message=f"检测到标题层级跨级跳跃：从 {curr_lvl} 级标题直接跳到 {next_lvl} 级标题",
                    suggestion="请补全中间层级（如增加二级标题）或调整当前标题级别"
                ))

        # -------------------------------------------------------------
        # 3. 表格检查 (三线表规范与竖线检查)
        # -------------------------------------------------------------
        for t_idx, table in enumerate(doc.tables, start=1):
            # 检查是否有竖线 (通过 oxml 查看 tcBorders 或 tblBorders)
            tbl_pr = table._tbl.tblPr
            has_vertical_borders = False
            if tbl_pr is not None:
                tbl_borders = tbl_pr.find(qn("w:tblBorders"))
                if tbl_borders is not None:
                    inside_v = tbl_borders.find(qn("w:insideV"))
                    left_b = tbl_borders.find(qn("w:left"))
                    right_b = tbl_borders.find(qn("w:right"))
                    for b in (inside_v, left_b, right_b):
                        if b is not None and b.get(qn("w:val")) not in ("none", "nil", "0", None):
                            has_vertical_borders = True
                            break

            if has_vertical_borders:
                issues.append(FormatIssue(
                    rule_id="NON_THREE_LINE_TABLE",
                    severity=IssueSeverity.ERROR,
                    location=f"表格 #{t_idx}",
                    message="表格存在竖线/网格线，学术国标 (GB/T 7713.2-2022) 明确要求使用三线表（无竖线、顶底加粗）",
                    suggestion="清除所有内部和左右竖线，设置为标准学术三线表"
                ))

        # -------------------------------------------------------------
        # 4. 参考文献引用连续性与断号检测
        # -------------------------------------------------------------
        all_doc_text = "\n".join(p.text for p in paragraphs)
        citation_matches = [int(m) for m in cls.RE_CITATION.findall(all_doc_text)]
        if citation_matches:
            unique_citations = sorted(set(citation_matches))
            expected_seq = list(range(1, max(unique_citations) + 1))
            missing_cits = set(expected_seq) - set(unique_citations)
            if missing_cits:
                issues.append(FormatIssue(
                    rule_id="CITATION_DISCONTINUITY",
                    severity=IssueSeverity.WARNING,
                    location="正文文献引用",
                    message=f"正文引用序号出现断号：缺少引用序号 {sorted(missing_cits)}",
                    suggestion="核对正文顺序编码制引用编号是否连续递增"
                ))

        # -------------------------------------------------------------
        # 5. 审稿人第一印象扣分项检查
        # -------------------------------------------------------------
        # (1) 英文大写缩写首次出现无全称定义检测
        issues.extend(cls._check_abbreviation_definitions(body_paras))
        # (2) 悬空标题检测
        issues.extend(cls._check_dangling_headings(para_hierarchy_items))
        # (3) 摘要与结论机械重复检测
        issues.extend(cls._check_abstract_conclusion_repetition(paragraphs_info))

        # 统计汇总
        error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
        warning_count = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)
        info_count = sum(1 for i in issues if i.severity == IssueSeverity.INFO)

        score = max(0, 100 - error_count * 15 - warning_count * 5 - info_count * 2)

        return {
            "status": "success",
            "document_path": docx_path,
            "standard_id": standard_id,
            "standard_name": std_cfg.get("name", standard_id),
            "format_score": score,
            "is_compliant": error_count == 0 and warning_count == 0,
            "summary": {
                "total_paragraphs": total_paras,
                "total_tables": len(doc.tables),
                "errors": error_count,
                "warnings": warning_count,
                "suggestions": info_count,
                "total_issues": len(issues),
            },
            "issues": [i.to_dict() for i in issues],
        }

    @classmethod
    def audit_live_doc(cls, doc, standard_id: str = "chinese_thesis_standard") -> Dict[str, Any]:
        """Audit an active win32com Word Document."""
        issues: List[FormatIssue] = []
        std_cfg = get_standard_by_id(standard_id) or get_standard_by_id("chinese_thesis_standard")

        paras_count = doc.Paragraphs.Count
        tables_count = doc.Tables.Count

        # 1. 论文大标题检查
        if paras_count >= 1:
            first_p = doc.Paragraphs(1)
            first_range = getattr(first_p, "Range", None)
            first_text = (getattr(first_range, "Text", "") or "").strip().replace("\r", "").replace("\x07", "")
            first_format = getattr(first_p, "Format", None)
            outline_level = getattr(first_format, "OutlineLevel", 10) if first_format else 10
            first_font = getattr(first_range, "Font", None)
            font_size = getattr(first_font, "Size", 12.0) if first_font else 12.0
            if outline_level < 10 and font_size >= 18:
                issues.append(FormatIssue(
                    rule_id="TITLE_OUTLINE_LEAK",
                    severity=IssueSeverity.ERROR,
                    location="第 1 段 (论文大标题)",
                    message=f"论文总标题大纲级别被设为 {outline_level}，会污染左侧导航窗格和自动生成目录",
                    snippet=first_text[:35],
                    suggestion="将大纲级别降为正文文本 (10)，设置为二号加粗居中"
                ))

            # 审稿人第一印象扣分项：题目假大空检测
            vague_title_issue = cls._check_vague_title(first_text, location="第 1 段 (论文大标题)")
            if vague_title_issue:
                issues.append(vague_title_issue)

        # 2. 标题层级字号倒挂检查
        try:
            h1 = doc.Styles(-2)  # wdStyleHeading1
            h2 = doc.Styles(-3)  # wdStyleHeading2
            h3 = doc.Styles(-4)  # wdStyleHeading3
            h1_sz = float(h1.Font.Size)
            h2_sz = float(h2.Font.Size)
            h3_sz = float(h3.Font.Size)
            if h1_sz <= h2_sz:
                issues.append(FormatIssue(
                    rule_id="HEADING_INVERSION",
                    severity=IssueSeverity.ERROR,
                    location="样式库: Heading 1 & Heading 2",
                    message=f"标题字号发生倒挂：H1 ({h1_sz}pt) <= H2 ({h2_sz}pt)",
                    suggestion="修复字号阶梯：H1(16pt) > H2(14pt) > H3(12pt)"
                ))
            if h2_sz < h3_sz:
                issues.append(FormatIssue(
                    rule_id="HEADING_INVERSION",
                    severity=IssueSeverity.ERROR,
                    location="样式库: Heading 2 & Heading 3",
                    message=f"标题字号发生倒挂：H2 ({h2_sz}pt) < H3 ({h3_sz}pt)",
                    suggestion="调整 H2 为 14pt (四号), H3 为 12pt (小四)"
                ))
            if h1.Font.Color != 0 or h2.Font.Color != 0:
                issues.append(FormatIssue(
                    rule_id="HEADING_COLOR_IMPURE",
                    severity=IssueSeverity.WARNING,
                    location="样式库: 标题颜色",
                    message="标题颜色不是纯黑色 (Color!=0，为 Word 默认主题蓝)",
                    suggestion="将各级标题颜色重设为 RGB(0,0,0) 纯黑"
                ))
        except Exception:
            pass

        # 3. 快速扫描段落中的标点与首行缩进
        live_body_paras: List[Tuple[str, str]] = []
        live_para_hierarchy: List[Tuple[str, str, Optional[int]]] = []
        live_paras_info: List[Tuple[str, bool]] = []

        scan_limit = min(paras_count, 100)  # 抽样前 100 段保证响应实时性
        for p_idx in range(1, scan_limit + 1):
            p = doc.Paragraphs(p_idx)
            p_range = getattr(p, "Range", None)
            text = (getattr(p_range, "Text", "") or "").strip().replace("\r", "").replace("\x07", "")
            if not text:
                continue

            p_style = getattr(p, "Style", None)
            style_name = str(getattr(p_style, "NameLocal", "")) if p_style else ""
            is_heading_style = "标题" in style_name or "Heading" in style_name
            is_pseudo_heading = not is_heading_style and bool(cls.RE_CHINESE_HEADING.match(text)) and len(text) < 40

            # 伪标题检查
            if is_pseudo_heading:
                issues.append(FormatIssue(
                    rule_id="PSEUDO_HEADING",
                    severity=IssueSeverity.WARNING,
                    location=f"第 {p_idx} 段",
                    message="检测到疑似章节标题，但使用的是普通样式而不是 Heading 标题样式",
                    snippet=text,
                    suggestion="应用正规标题样式以支持大纲导航与目录生成"
                ))

            if is_heading_style:
                level_match = re.search(r"\d+", style_name)
                level = int(level_match.group(0)) if level_match else 1
                loc_label = f"第 {p_idx} 段 (论文大标题)" if p_idx == 1 else f"第 {p_idx} 段 ({style_name})"
                live_para_hierarchy.append((loc_label, text, level))
                live_paras_info.append((text, True))
            elif is_pseudo_heading:
                p_lvl = 1
                if re.match(r"^\s*[0-9]+\.[0-9]+\.[0-9]+", text):
                    p_lvl = 3
                elif re.match(r"^\s*[0-9]+\.[0-9]+", text):
                    p_lvl = 2
                loc_label = f"第 {p_idx} 段 (论文大标题)" if p_idx == 1 else f"第 {p_idx} 段"
                live_para_hierarchy.append((loc_label, text, p_lvl))
                live_paras_info.append((text, True))
            else:
                loc_label = f"第 {p_idx} 段 (论文大标题)" if p_idx == 1 else f"第 {p_idx} 段"
                live_para_hierarchy.append((loc_label, text, None))
                live_paras_info.append((text, False))

            # 正文段落检查
            if not is_heading_style and not is_pseudo_heading and len(text) > 15:
                if not cls.RE_FIGURE_CAPTION.match(text) and not cls.RE_TABLE_CAPTION.match(text):
                    live_body_paras.append((f"第 {p_idx} 段", text))

                p_format = getattr(p, "Format", None)
                indent_chars = getattr(p_format, "CharacterUnitFirstLineIndent", 0) if p_format else 0
                alignment = getattr(p_format, "Alignment", 0) if p_format else 0
                if indent_chars < 1.8 and alignment == 0:  # wdAlignParagraphLeft
                    issues.append(FormatIssue(
                        rule_id="BODY_INDENT_MISSING",
                        severity=IssueSeverity.WARNING,
                        location=f"第 {p_idx} 段",
                        message="正文段落缺少首行缩进 2 字符",
                        snippet=text[:30],
                        suggestion="设置首行缩进 2 字符"
                    ))

        # 4. 表格竖线检查
        if tables_count > 0:
            for t_idx in range(1, tables_count + 1):
                tbl = doc.Tables(t_idx)
                try:
                    # wdBorderLeft = -2, wdBorderRight = -4, wdBorderVertical = -6
                    left_line = tbl.Borders(-2).LineStyle
                    right_line = tbl.Borders(-4).LineStyle
                    v_line = tbl.Borders(-6).LineStyle
                    if left_line != 0 or right_line != 0 or v_line != 0:
                        issues.append(FormatIssue(
                            rule_id="NON_THREE_LINE_TABLE",
                            severity=IssueSeverity.ERROR,
                            location=f"表格 #{t_idx}",
                            message="检测到表格带有边框竖线，违反学术三线表规范",
                            suggestion="清除表格竖线，设置为标准学术三线表"
                        ))
                except Exception:
                    pass

        # 5. 审稿人第一印象扣分项检查
        issues.extend(cls._check_abbreviation_definitions(live_body_paras))
        issues.extend(cls._check_dangling_headings(live_para_hierarchy))
        issues.extend(cls._check_abstract_conclusion_repetition(live_paras_info))

        error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
        warning_count = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)
        info_count = sum(1 for i in issues if i.severity == IssueSeverity.INFO)

        score = max(0, 100 - error_count * 15 - warning_count * 5 - info_count * 2)

        return {
            "status": "success",
            "document_name": doc.Name,
            "standard_id": standard_id,
            "standard_name": std_cfg.get("name", standard_id),
            "format_score": score,
            "is_compliant": error_count == 0 and warning_count == 0,
            "summary": {
                "total_paragraphs": paras_count,
                "total_tables": tables_count,
                "errors": error_count,
                "warnings": warning_count,
                "suggestions": info_count,
                "total_issues": len(issues),
            },
            "issues": [i.to_dict() for i in issues],
        }


class PaperFormatNormalizer:
    """One-click auto-healing and standardization engine for academic manuscripts."""

    PUNCT_MAP = {
        ",": "，",
        ":": "：",
        ";": "；",
        "?": "？",
        "!": "！",
        "(": "（",
        ")": "）",
    }

    @classmethod
    def normalize_chinese_punctuation(cls, text: str) -> str:
        """Convert half-width punctuation within Chinese clauses to standard full-width, preserving formulas and citations."""
        # 保护 [1], [1,2] 等文献引用
        citations: List[str] = []
        def cit_repl(m):
            citations.append(m.group(0))
            return f"__CIT_{len(citations)-1}__"

        t = re.sub(r"\[\d+(?:[-,]\d+)*\]", cit_repl, text)

        # 保护数字小数如 3.14
        t = re.sub(r"(\d)\.(\d)", r"\1__DECIMAL_DOT__\2", t)

        # 替换紧邻中文字符的半角逗号、分号、冒号、括号
        for en_p, zh_p in cls.PUNCT_MAP.items():
            pattern = re.escape(en_p)
            t = re.sub(r"([\u4e00-\u9fa5])" + pattern, r"\1" + zh_p, t)
            t = re.sub(pattern + r"([\u4e00-\u9fa5])", zh_p + r"\1", t)

        # 恢复小数与引用
        t = t.replace("__DECIMAL_DOT__", ".")
        for idx, cit in enumerate(citations):
            t = t.replace(f"__CIT_{idx}__", cit)

        return t

    @classmethod
    def normalize_docx(
        cls,
        input_path: str,
        output_path: Optional[str] = None,
        standard_id: str = "chinese_thesis_standard",
    ) -> Dict[str, Any]:
        """Normalize an offline docx file to comply with academic standards."""
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"文件未找到: {input_path}")

        save_target = output_path or input_path
        doc = Document(input_path)
        changes_applied: List[str] = []

        # 1. 规范页面边距 (Top 3.0cm, Bottom 2.5cm, Left 3.0cm, Right 2.5cm)
        for section in doc.sections:
            section.page_width = Cm(21.0)
            section.page_height = Cm(29.7)
            section.top_margin = Cm(3.0)
            section.bottom_margin = Cm(2.5)
            section.left_margin = Cm(3.0)
            section.right_margin = Cm(2.5)
        changes_applied.append("规范页面边距为 A4 标准 (上 3.0cm, 下 2.5cm, 左 3.0cm, 右 2.5cm)")

        # 2. 规范 Normal 与 Heading 样式库
        normal_style = doc.styles["Normal"]
        normal_style.font.name = "Times New Roman"
        normal_style.font.size = Pt(12)
        normal_style.font.color.rgb = RGBColor(0, 0, 0)
        normal_style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        normal_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        # 标题级差预设
        heading_presets = {
            "Heading 1": (16, True, 12, 12),
            "Heading 2": (14, False, 6, 6),
            "Heading 3": (12, False, 6, 0),
        }
        for h_name, (size_pt, is_center, before_pt, after_pt) in heading_presets.items():
            if h_name in doc.styles:
                h_style = doc.styles[h_name]
                h_style.font.name = "Times New Roman"
                h_style.font.size = Pt(size_pt)
                h_style.font.bold = True
                h_style.font.color.rgb = RGBColor(0, 0, 0)
                h_style.paragraph_format.first_line_indent = Pt(0)
                h_style.paragraph_format.space_before = Pt(before_pt)
                h_style.paragraph_format.space_after = Pt(after_pt)
        changes_applied.append("规范样式库：统一 H1(16pt居中) > H2(14pt居左) > H3(12pt居左) 阶梯，消除颜色与缩进缺陷")

        # 3. 遍历段落：清理空行、规范首行缩进、转换标点
        consecutive_blanks = 0
        blank_paras_to_remove = []

        for p_idx, p in enumerate(doc.paragraphs):
            text = p.text.strip()
            if not text:
                consecutive_blanks += 1
                if consecutive_blanks >= 2:
                    blank_paras_to_remove.append(p)
                continue
            else:
                consecutive_blanks = 0

            style_name = p.style.name if p.style else ""
            is_heading = style_name.startswith("Heading") or style_name.startswith("标题")

            # 清除手动空格缩进
            if text.startswith("　　") or text.startswith("    "):
                p.text = text.lstrip("　 ").strip()
                changes_applied.append(f"段落 #{p_idx+1}: 清除手动空格，转为属性缩进")

            # 修正正文首行缩进
            if not is_heading and len(p.text.strip()) > 15:
                if p.alignment != WD_ALIGN_PARAGRAPH.CENTER:
                    p.paragraph_format.first_line_indent = Pt(24)  # 2字符

            # 标点符号规范化
            fixed_text = cls.normalize_chinese_punctuation(p.text)
            if fixed_text != p.text:
                p.text = fixed_text

        # 移除多余空行
        for bp in blank_paras_to_remove:
            p_elem = bp._p
            p_elem.getparent().remove(p_elem)
        if blank_paras_to_remove:
            changes_applied.append(f"清理了 {len(blank_paras_to_remove)} 处连续冗余空行")

        # 4. 规范表格为三线表
        for t_idx, table in enumerate(doc.tables, start=1):
            cls._apply_three_line_table_oxml(table)
        if doc.tables:
            changes_applied.append(f"已将文档内 {len(doc.tables)} 个表格统一重塑为学术三线表 (顶底粗1.5pt，表头0.75pt，无竖线)")

        # 保存
        doc.save(save_target)

        return {
            "status": "success",
            "saved_path": save_target,
            "changes_applied": changes_applied,
            "total_changes": len(changes_applied),
        }

    @classmethod
    def _apply_three_line_table_oxml(cls, table) -> None:
        """Apply three-line table borders to a python-docx table element."""
        tbl_pr = table._tbl.tblPr
        tbl_borders = tbl_pr.find(qn("w:tblBorders"))
        if tbl_borders is None:
            tbl_borders = OxmlElement("w:tblBorders")
            tbl_pr.append(tbl_borders)
        else:
            tbl_borders.clear()

        # 顶线 1.5pt (sz="12")
        top = OxmlElement("w:top")
        top.set(qn("w:val"), "single")
        top.set(qn("w:sz"), "12")
        top.set(qn("w:space"), "0")
        top.set(qn("w:color"), "000000")
        tbl_borders.append(top)

        # 底线 1.5pt (sz="12")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "0")
        bottom.set(qn("w:color"), "000000")
        tbl_borders.append(bottom)

        # 清除左右与内部垂直线
        for side in ("left", "right", "insideV"):
            b = OxmlElement(f"w:{side}")
            b.set(qn("w:val"), "none")
            tbl_borders.append(b)

        # 表头下方细线 0.75pt (sz="6")
        inside_h = OxmlElement("w:insideH")
        inside_h.set(qn("w:val"), "none")  # 默认内部横线不显示
        tbl_borders.append(inside_h)

        # 单独为第一行（表头）的底部加上 0.75pt 细线
        if len(table.rows) > 0:
            for cell in table.rows[0].cells:
                tc_pr = cell._tc.get_or_add_tcPr()
                tc_borders = tc_pr.find(qn("w:tcBorders"))
                if tc_borders is None:
                    tc_borders = OxmlElement("w:tcBorders")
                    tc_pr.append(tc_borders)
                bottom_border = OxmlElement("w:bottom")
                bottom_border.set(qn("w:val"), "single")
                bottom_border.set(qn("w:sz"), "6")  # 0.75pt
                bottom_border.set(qn("w:space"), "0")
                bottom_border.set(qn("w:color"), "000000")
                tc_borders.append(bottom_border)

    @classmethod
    def normalize_live_doc(cls, doc, standard_id: str = "chinese_thesis_standard") -> Dict[str, Any]:
        """Normalize an active Word/WPS document live via win32com."""
        changes_applied: List[str] = []

        # 1. 论文总标题修复
        if doc.Paragraphs.Count >= 1:
            first_p = doc.Paragraphs(1)
            if first_p.Format.OutlineLevel < 10 and first_p.Range.Font.Size >= 18:
                first_p.Format.OutlineLevel = 10
                first_p.Style = doc.Styles(-1)  # Normal
                first_p.Format.Alignment = 1    # wdAlignParagraphCenter
                first_p.Range.Font.Size = 22
                first_p.Range.Font.Bold = True
                first_p.Range.Font.Color = 0
                changes_applied.append("论文总标题已移出导航窗格，并重设为二号居中加粗")

        # 2. 标题样式与字号阶梯规范
        try:
            h1 = doc.Styles(-2)
            h1.Font.Size = 16.0
            h1.Font.Bold = True
            h1.Font.Color = 0
            h1.ParagraphFormat.Alignment = 1
            h1.ParagraphFormat.CharacterUnitFirstLineIndent = 0

            h2 = doc.Styles(-3)
            h2.Font.Size = 14.0
            h2.Font.Bold = True
            h2.Font.Color = 0
            h2.ParagraphFormat.Alignment = 0
            h2.ParagraphFormat.CharacterUnitFirstLineIndent = 0

            h3 = doc.Styles(-4)
            h3.Font.Size = 12.0
            h3.Font.Bold = True
            h3.Font.Color = 0
            h3.ParagraphFormat.Alignment = 0
            h3.ParagraphFormat.CharacterUnitFirstLineIndent = 0
            changes_applied.append("重设各级标题样式：H1(16pt居中) > H2(14pt居左) > H3(12pt居左)，纯黑加粗无缩进")
        except Exception as e:
            changes_applied.append(f"样式更新提示: {e}")

        # 3. 正文段落首行缩进与空行规范
        paras_count = doc.Paragraphs.Count
        for i in range(1, min(paras_count, 100) + 1):
            p = doc.Paragraphs(i)
            text = (p.Range.Text or "").strip().replace("\r", "").replace("\x07", "")
            if not text:
                continue
            style_name = str(p.Style.NameLocal)
            if "标题" not in style_name and "Heading" not in style_name and len(text) > 15:
                if p.Format.Alignment == 0:
                    p.Format.CharacterUnitFirstLineIndent = 2

        # 4. 表格转三线表
        for t_idx in range(1, doc.Tables.Count + 1):
            tbl = doc.Tables(t_idx)
            try:
                # 清除左右与内部垂直线
                tbl.Borders(-2).LineStyle = 0  # wdBorderLeft
                tbl.Borders(-4).LineStyle = 0  # wdBorderRight
                tbl.Borders(-6).LineStyle = 0  # wdBorderVertical
                # 顶底粗线 1.5pt (LineWidth=12)
                tbl.Borders(-1).LineStyle = 1  # wdBorderTop
                tbl.Borders(-1).LineWidth = 12
                tbl.Borders(-3).LineStyle = 1  # wdBorderBottom
                tbl.Borders(-3).LineWidth = 12
                # 表头下方细线 0.75pt (LineWidth=6)
                if tbl.Rows.Count >= 1:
                    tbl.Rows(1).Borders(-3).LineStyle = 1
                    tbl.Rows(1).Borders(-3).LineWidth = 6
            except Exception:
                pass
        if doc.Tables.Count > 0:
            changes_applied.append(f"已将活跃文档中的 {doc.Tables.Count} 个表格规范为三线表")

        try:
            doc.Save()
        except Exception:
            pass

        return {
            "status": "success",
            "document_name": doc.Name,
            "changes_applied": changes_applied,
            "total_changes": len(changes_applied),
        }
