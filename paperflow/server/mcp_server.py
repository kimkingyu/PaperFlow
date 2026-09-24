"""PaperFlow MCP Server.

Provides a standard Model Context Protocol (MCP) server that connects AI clients
(WorkBuddy, Qwen, Cursor, Claude Desktop, Cherry Studio) directly to Microsoft Word / WPS
for real-time document creation, editing, review comments, and academic anti-AI polishing.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    from mcp.server.mcpserver import MCPServer
    mcp_app = MCPServer("PaperFlow-Word-Assistant")
except Exception:
    from mcp.server.fastmcp import FastMCP
    mcp_app = FastMCP("PaperFlow-Word-Assistant")

from paperflow.engine.anti_ai_cleaner import anti_ai_engine
from paperflow.engine.docx_builder import AcademicDocxBuilder
from paperflow.engine.formatter import PaperFormatAuditor, PaperFormatNormalizer
from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError
from paperflow.engine.standards_manager import get_standard_by_id, list_standards
from paperflow.engine.zotero_field import parse_citations
from paperflow.engine.word_live_bridge import live_bridge

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = None


def _format_journal_error(err: Exception) -> str:
    """Format exceptions into standardized response envelope without leaking secrets/tracebacks."""
    error_code = "INTERNAL_ERROR"
    message = "操作执行失败，请检查输入参数或本地数据文件状态"
    if isinstance(err, JournalError):
        error_code = getattr(err, "code", "JOURNAL_ERROR")
        message = str(err)
    elif ValidationError and isinstance(err, ValidationError):
        error_code = "VALIDATION_ERROR"
        safe_errors = []
        for e in err.errors():
            safe_errors.append({
                "loc": list(e.get("loc", ())),
                "type": e.get("type", "validation_error"),
            })
        message = "参数或数据字段校验未通过: " + json.dumps(safe_errors, ensure_ascii=False)

    return json.dumps({
        "status": "error",
        "error_code": error_code,
        "message": message,
        "data": None,
        "sources": [],
        "coverage": {},
        "warnings": [],
        "suggested_options": [],
    }, ensure_ascii=False, indent=2)


def _get_journal_service() -> JournalFinder:
    """Get JournalFinder reading PAPERFLOW_JOURNAL_HOME without creating DB or networking on init."""
    data_dir = os.environ.get("PAPERFLOW_JOURNAL_HOME")
    return JournalFinder(data_dir=data_dir if data_dir else None)



@mcp_app.tool()
def get_active_word_doc(file_path: str = "") -> str:
    """Get information about the currently open Word or WPS document.
    
    Reads document title, path, paragraph count, word count, and text preview.
    If file_path is specified (e.g. 'D:\\tes\\test.docx'), automatically targets and activates that document.
    
    Args:
        file_path: Optional full path or document name to target. Defaults to currently active window.
    """
    try:
        info = live_bridge.get_document_info(target_path=file_path if file_path else None)
        paras = info.get("paragraph_count", 0)
        doc_name = info.get("document_name", "文档")
        if paras <= 1:
            info["suggested_options"] = [
                f"1. (推荐) 针对当前文档 '{doc_name}' 生成三级学术大纲并写入",
                "2. 确立 3 点核心创新贡献并起草引言",
                "3. 确认排版标准（如国内高校学位论文或 2025 新国标）",
            ]
        else:
            info["suggested_options"] = [
                f"1. (推荐) 在当前文档 '{doc_name}' 光标位置续写下一章节",
                "2. 对已有段落进行去 AI 味与学术套话审查",
                "3. 插入标准学术三线表或实验数据对比",
            ]
        return json.dumps(info, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Could not read active Word/WPS document: {e}. Please ensure Word or WPS is open.",
            "suggested_options": [
                "1. (推荐) 在桌面上打开 Word 或 WPS 论文文件后再次对我说：'连接 Word'",
                "2. 切换为离线模式生成新论文文档 (generate_offline_paper_docx)",
            ],
        }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def select_target_word_doc(file_path: str) -> str:
    """Lock and activate a specific Word document by absolute path or filename (e.g. 'D:\\tes\\test.docx').
    
    Prevents accidentally modifying other open documents (like reference papers or chat files).
    If the document is already open in Word, brings it to focus. If not yet open, opens it in Word.
    
    Args:
        file_path: Absolute path or filename to lock onto, e.g. 'D:\\tes\\test.docx'.
    """
    try:
        res = live_bridge.select_document(file_path)
        res["suggested_options"] = [
            "1. (推荐) 读取该文档当前结构与段落内容 (get_active_word_doc)",
            "2. 开始向该文档写入学术大纲或正文 (write_to_active_word)",
            "3. 应用 2021-2026 学术排版预设 (apply_academic_style_preset)",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to select target Word document: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def get_word_selection() -> str:
    """Get the text currently highlighted/selected by the user's cursor in Word or WPS.
    
    Use this when the user asks to review, polish, or rewrite a specific section
    without requiring them to manually copy-paste into the chat window.
    """
    try:
        res = live_bridge.get_selected_text()
        if res.get("has_selection"):
            res["suggested_options"] = [
                "1. (推荐) 开启修订模式 (Track Changes) 并替换为学术润色版",
                "2. 插入 Word 原生批注气泡 (Comments)，指出问题保留原文",
                "3. 扫描这段文字中的 AI 套话与句式缺陷",
            ]
        else:
            res["suggested_options"] = [
                "1. 请在 Word 窗口中用鼠标划选一段文字后重试",
                "2. 直接告诉我您想要审查或修改哪一节的内容",
            ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to get selection: {e}",
            "suggested_options": [
                "1. 确认 Word 处于打开激活状态",
                "2. 用鼠标划选文字后再试",
            ],
        }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def write_to_active_word(
    content: str,
    heading_level: int = 0,
    position: str = "cursor"
) -> str:
    """Write text or headings directly into the user's running Word or WPS window.
    
    Args:
        content: The text content to write.
        heading_level:
            -1 for Paper Title (二号22pt黑体居中加粗，不混入章节大纲和目录),
             0 for regular body text (小四12pt宋体首行缩进),
             1 for Heading 1 (三号16pt黑体居中),
             2 for Heading 2 (四号14pt黑体居左),
             3 for Heading 3 (小四12pt黑体居左).
        position: 'cursor' (write at cursor) or 'end' (append to document end).
    """
    try:
        res = live_bridge.insert_text(text=content, heading_level=heading_level, position=position)
        res["suggested_options"] = [
            "1. (推荐) 继续撰写下一小节内容",
            "2. 在此插入学术三线表或对比数据",
            "3. 为当前论述插入 [@Zotero_Key] 动态活引用",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to write to Word: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def clear_word_document() -> str:
    """Clear all content in the active targeted Word document (use with care when resetting a draft)."""
    try:
        res = live_bridge.clear_document_content()
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to clear document: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def replace_word_selection(new_text: str) -> str:
    """Replace the currently highlighted text in Word or WPS with new polished text.
    
    Use this after rewriting or de-flavoring a user's selected paragraph.
    """
    try:
        res = live_bridge.replace_selection(new_text)
        res["suggested_options"] = [
            "1. (推荐) 在 Word 界面中检查修订标记（可右键接受修改）",
            "2. 继续划选审查下一个段落",
            "3. 针对修改后的段落添加真实文献引用",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to replace selection: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def insert_academic_table(
    headers: List[str],
    rows: List[List[str]],
    caption: str = "",
    position: str = "cursor",
) -> str:
    """Insert a publication-grade academic three-line table into active Word or WPS.
    
    Complies with academic standards: 1.5pt top/bottom border, 0.75pt header border,
    no vertical lines, and a centered table caption above the table.
    
    Args:
        headers: Column title list, e.g. ["模型架构", "量化位数", "延迟 (ms)", "准确率"].
        rows: 2D data list, e.g. [["Baseline", "32-bit", "142.5", "81.2%"], ["Ours", "8-bit", "38.2", "80.9%"]].
        caption: Table title placed above the table, e.g. "表 2-1  不同模型在边缘设备上的推理表现对比".
        position: 'cursor' or 'end'.
    """
    try:
        res = live_bridge.insert_academic_table(
            headers=headers, rows=rows, caption=caption, position=position
        )
        res["suggested_options"] = [
            "1. (推荐) 在表格下方补充'注：数据测试环境为...'说明段落",
            "2. 在正文中引用此表（如'结果如表所示'）",
            "3. 继续推进下一实验分析",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to insert academic table: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def add_word_comment(comment_text: str, author: str = "PaperFlow AI") -> str:
    """Add a native Word review comment bubble (Comments) to the currently selected text.
    
    Like a supervisor reviewing a manuscript, this adds a real comment bubble
    directly on the selected phrase or paragraph in the user's Word window.
    
    Args:
        comment_text: The review critique or suggestion.
        author: The comment author name (default: 'PaperFlow AI').
    """
    try:
        res = live_bridge.add_comment(comment_text=comment_text, author=author)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to add comment: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def set_word_track_revisions(enable: bool = True) -> str:
    """Turn on or off Word's Track Changes (Revision mode).
    
    When enabled, any subsequent edits appear as red/green strikethroughs and underlines
    in Word, allowing the user to review each change and click 'Accept' or 'Reject'.
    """
    try:
        res = live_bridge.set_track_revisions(enable=enable)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to toggle track revisions: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def list_academic_standards(category: str = "all") -> str:
    """List built-in 2021-2026 academic standards and formatting guidelines.
    
    Includes:
    - GB/T 7714-2025 (latest Chinese reference standard)
    - GB/T 7714-2015 (widely used Chinese reference standard)
    - GB/T 7713.2-2022 (academic paper writing & structure standard)
    - GB/T 7713.1-2006 (thesis writing guidelines)
    - chinese_thesis_standard (common university thesis layout preset)
    - chinese_journal_standard (Chinese core/tech journal standard)
    - ieee_conference_2024 (IEEE Transactions/Conferences 2024-2026)
    - acm_master_2024 (ACM Primary Article Template 2024-2026)
    - apa_7th_edition (APA 7th Author-Date standard)
    - nature_springer_2024 (Nature Portfolio guidelines 2023-2026)
    
    Args:
        category: 'all', 'reference_gb', 'structure_gb', 'layout_preset', or 'international'.
    """
    stds = list_standards(category=category)
    return json.dumps({
        "status": "success",
        "total": len(stds),
        "standards": stds,
        "suggested_options": [
            "1. (推荐) 使用国内高校博硕学位论文通用标准 (chinese_thesis_standard)",
            "2. 采用 GB/T 7714-2025 最新参考文献著录规则",
            "3. 切换为国际期刊规范 (IEEE / APA / ACM)",
        ]
    }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def apply_academic_style_preset(standard_id: str = "chinese_thesis_standard") -> str:
    """Apply an academic typography preset to the active document's Normal style.
    
    Available built-in 2021-2026 standards:
    - 'chinese_thesis_standard': 宋体/Times New Roman, 12pt (小四), 1.5行距, 首行缩进2字符 (默认推荐)
    - 'chinese_journal_standard': 中文核心期刊紧凑版式, 10.5pt (五号), 1.25行距
    - 'ieee_conference_2024': IEEE 国际会议规范, Times New Roman, 10pt, 单倍行距
    - 'apa_7th_edition': APA 7th 社科规范, Times New Roman, 12pt, 双倍行距
    
    Note: Do NOT apply if user is working in an official university/journal template.
    """
    try:
        res = live_bridge.apply_academic_preset(standard_id=standard_id)
        res["suggested_options"] = [
            "1. (推荐) 查看 Word 正文样式是否生效",
            "2. 检查各级标题 (Heading 1-3) 样式与字号",
            "3. 继续撰写正文或插入标准三线表",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to apply style preset: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def scan_anti_ai_flavor(text: str) -> str:
    """Analyze academic text for AI clichés, robotic transitions, and stylistic risks.
    
    Scans for overused AI buzzwords ('delve into', 'testament to', '深入探讨', '不可否认的是'),
    evaluates voice balance, and provides human scholar rewriting recommendations.
    """
    res = anti_ai_engine.analyze(text)
    if res.get("total_findings", 0) > 0:
        res["suggested_options"] = [
            "1. (推荐) 开启 Word 修订模式并替换为学术学者去套话重写版",
            "2. 在 Word 中为这几处套话打上批注气泡，指出问题但保留原文",
            "3. 仅剥离'深入探讨'等空泛副词，维持原有句式骨架",
        ]
    else:
        res["suggested_options"] = [
            "1. (推荐) 本段学术表达自然扎实，无需改动，继续撰写下一节",
            "2. 为本段关键学术断言添加 [@Zotero_Key] 真实文献引用",
        ]
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp_app.tool()
def generate_offline_paper_docx(
    output_path: str,
    title: str,
    abstract: str,
    keywords: List[str],
    sections: List[Dict[str, Any]],
    is_chinese: bool = True,
) -> str:
    """Generate a complete, publication-ready .docx paper file from scratch (offline mode).
    
    Supports Heading hierarchy, dual fonts, academic tables, paragraph formatting, and [@KEY] Zotero live citations.
    
    Args:
        output_path: Absolute or relative destination path (e.g. 'my_paper.docx').
        title: Title of the academic paper.
        abstract: Abstract paragraph text.
        keywords: List of 3-6 keywords.
        sections: List of section dicts: [{'title': '1 引言', 'level': 1, 'paragraphs': ['...'], 'tables': [{'headers': [...], 'rows': [...], 'caption': '...'}]}]
        is_chinese: True for Chinese paper (宋体), False for English (Times New Roman).
    """
    try:
        # Number citations by first appearance across the WHOLE paper, not per paragraph.
        all_text = "\n\n".join(p for sec in sections for p in sec.get("paragraphs", []))
        _, key_to_number = parse_citations(all_text)
        builder = AcademicDocxBuilder(is_chinese=is_chinese)
        builder.add_title(title)
        builder.add_abstract(abstract, keywords)

        for sec in sections:
            sec_title = sec.get("title", "")
            sec_level = sec.get("level", 1)
            if sec_title:
                builder.add_heading(sec_title, level=sec_level)
            for para in sec.get("paragraphs", []):
                builder.add_paragraph_with_citations(para, global_key_mapping=key_to_number)
            for tbl in sec.get("tables", []):
                builder.add_three_line_table(
                    headers=tbl.get("headers", []),
                    rows=tbl.get("rows", []),
                    caption=tbl.get("caption", ""),
                )

        builder.add_bibliography_section()
        saved_path = builder.save(output_path)
        return json.dumps({
            "status": "success",
            "output_path": saved_path,
            "message": f"Successfully created academic Word document at {saved_path}",
            "suggested_options": [
                "1. (推荐) 在桌面上用 Word 打开此文件并启动实时交互",
                "2. 检查生成的内置标题样式与 Zotero 域代码",
                "3. 继续向该文档增补后续实验章节",
            ]
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to generate paper docx: {e}"
        }, ensure_ascii=False)


@mcp_app.tool()
def audit_paper_format(
    file_path: str = "",
    standard_id: str = "chinese_thesis_standard",
    add_comments: bool = False,
) -> str:
    """Audit academic paper formatting and identify defects (live Word/WPS or offline docx).

    Examines:
    - Title outline isolation & heading hierarchy (inversion, level skip, unwanted indents)
    - Pseudo-headings (bold text without real Heading style)
    - First-line paragraph indentation (missing indent, manual space indent)
    - Three-line table compliance (checks for unwanted vertical gridlines)
    - Punctuation consistency (half-width marks inside Chinese text)
    - Sequential citation continuity (checks for broken/missing citation indices)
    - Redundant consecutive blank paragraphs

    Args:
        file_path: Optional path to an offline .docx file. If empty, audits the currently active Word/WPS window.
        standard_id: Academic standard ID (default: 'chinese_thesis_standard').
        add_comments: In live Word mode, attach native review comment bubbles at problem locations.
    """
    try:
        if file_path:
            report = PaperFormatAuditor.audit_docx(docx_path=file_path, standard_id=standard_id)
        else:
            report = live_bridge.audit_document(standard_id=standard_id, add_comments=add_comments)

        score = report.get("format_score", 100)
        report["suggested_options"] = [
            f"1. (推荐) 一键自动规范化与修复排版缺陷 (normalize_paper_format)",
            "2. 查看具体段落问题明细并人工核验",
            "3. 切换审核规范（如改用 chinese_journal_standard）",
        ] if score < 90 else [
            "1. (推荐) 论文排版格式极佳，可直接推进答辩或投稿准备",
            "2. 检查正文 AI 味与学术表达 (scan_anti_ai_flavor)",
        ]

        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to audit paper format: {e}"
        }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def normalize_paper_format(
    file_path: str = "",
    output_path: str = "",
    standard_id: str = "chinese_thesis_standard",
) -> str:
    """Auto-heal and normalize academic paper formatting to strictly adhere to standards.

    Actions performed:
    - Cleans redundant consecutive blank paragraphs
    - Enforces standard margins (Top 3.0cm, Bottom 2.5cm, Left 3.0cm, Right 2.5cm)
    - Formats Heading 1-3 hierarchy (H1 16pt center, H2 14pt left, H3 12pt left, pure black, no indent)
    - Ensures body paragraph styling (12pt, 1.5 line spacing, 2-character first-line indent)
    - Re-shapes all tables into standard academic three-line tables (no vertical lines, 1.5pt top/bottom)
    - Replaces misplaced half-width punctuation in Chinese text with standard full-width punctuation
    - Lowers paper title outline level to 10 to keep it out of navigation pane and auto TOC

    Args:
        file_path: Optional path to an offline .docx file. If empty, normalizes active Word/WPS document.
        output_path: Optional destination path for offline docx. If empty, updates file in place.
        standard_id: Academic standard ID (default: 'chinese_thesis_standard').
    """
    try:
        if file_path:
            res = PaperFormatNormalizer.normalize_docx(
                input_path=file_path,
                output_path=output_path if output_path else None,
                standard_id=standard_id,
            )
        else:
            res = live_bridge.normalize_document(standard_id=standard_id)

        res["suggested_options"] = [
            "1. (推荐) 再次运行格式体检验证合规状态 (audit_paper_format)",
            "2. 在 Word 中查看排版效果并保存",
            "3. 导出或提交审阅",
        ]
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to normalize paper format: {e}"
        }, ensure_ascii=False, indent=2)


# =====================================================================
# Journal Tools (9 items)
# Capability note: local snapshot + manual official evidence != real-time guarantee of safety.
# Submission tracking is offline parse only.
# =====================================================================


@mcp_app.tool()
def list_journal_sources(source_id: str = "") -> str:
    """List supported academic journal sources, datasets, and local snapshot statuses.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        source_id: Optional specific source ID to inspect.
    """
    try:
        service = _get_journal_service()
        res = service.list_sources(source_id=source_id)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def import_journal_data(
    source_id: str,
    file_path: str,
    kind: str = "auto",
    data_year: Optional[int] = None,
    encoding: str = "utf-8-sig",
    dry_run: bool = True,
    source_version: str = "",
    dataset_id: str = "",
    allow_shrink: bool = False,
) -> str:
    """Import journal rankings, risk lists, or school policies from local files.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.
    dry_run defaults to True: previews imported counts and rejections without altering DB.

    Args:
        source_id: ID of the source catalog.
        file_path: Absolute or relative path to data file (CSV, JSON, SQLite).
        kind: Data parser kind or 'auto'.
        data_year: Explicit data year if applicable.
        encoding: File encoding (default 'utf-8-sig').
        dry_run: Whether to run in preview-only mode (default True).
        source_version: Optional version tag.
        dataset_id: Optional dataset identifier.
        allow_shrink: Allow snapshot record count to shrink.
    """
    try:
        service = _get_journal_service()
        res = service.import_data(
            source_id=source_id,
            file_path=file_path,
            kind=kind,
            data_year=data_year,
            encoding=encoding,
            dry_run=dry_run,
            source_version=source_version,
            dataset_id=dataset_id,
            allow_shrink=allow_shrink,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def refresh_journal_sources(
    source_id: str,
    dataset_id: str,
    dry_run: bool = True,
) -> str:
    """Check or update open source datasets / APIs for journal index updates.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.
    dry_run defaults to True.

    Args:
        source_id: Source ID (e.g. GitHub mirrors or easyScholar API).
        dataset_id: Specific dataset or journal query.
        dry_run: Preview only if True (default True).
    """
    try:
        service = _get_journal_service()
        res = service.refresh(source_id=source_id, dataset_id=dataset_id, dry_run=dry_run)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def search_academic_journals(
    query: str = "",
    filters: Optional[Dict[str, Any]] = None,
    sort_by: str = "relevance",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search academic journals by title, alias, ISSN, or combined filters.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        query: Search keywords or journal title / ISSN.
        filters: Filter dictionary mapping to SearchFilters.
        sort_by: Sorting field ('relevance', 'impact', 'volume', etc.).
        limit: Max records to return.
        offset: Record offset for pagination.
    """
    try:
        service = _get_journal_service()
        res = service.search(
            query=query,
            filters=filters,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def get_journal_details(query: str) -> str:
    """Get detailed records and comprehensive risk evaluation for a specific journal.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        query: Journal ID, ISSN, or unambiguous title.
    """
    try:
        service = _get_journal_service()
        res = service.details(query=query)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def check_journal_warning(
    query: str,
    profile_id: Optional[str] = None,
    warning_years: Optional[List[int]] = None,
) -> str:
    """Check warning, on-hold, delisted status and school policy compliance for a journal.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        query: Journal ID, ISSN, or title.
        profile_id: Optional school policy profile ID.
        warning_years: Optional list of years to inspect.
    """
    try:
        service = _get_journal_service()
        res = service.check_warning(
            query=query,
            profile_id=profile_id,
            warning_years=warning_years,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def compare_academic_journals(
    journal_ids: List[str],
    rank_system: Optional[str] = None,
    rank_year: Optional[int] = None,
) -> str:
    """Side-by-side comparison of multiple journals across tiers, metrics, and risks.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        journal_ids: List of canonical journal IDs to compare.
        rank_system: Optional ranking system filter ('cas', 'jcr', 'xr', 'ccf', 'ccft').
        rank_year: Optional ranking year.
    """
    try:
        service = _get_journal_service()
        res = service.compare(
            journal_ids=journal_ids,
            rank_system=rank_system,
            rank_year=rank_year,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def analyze_related_journals(
    references: Optional[List[Dict[str, Any]]] = None,
    file_path: str = "",
    filters: Optional[Dict[str, Any]] = None,
) -> str:
    """Analyze manuscript reference list to discover and rank potential peer target journals.

    Note: Local snapshot and manual official evidence != real-time guarantee of safety.

    Args:
        references: List of reference dicts (up to 200 items).
        file_path: Path to JSON file containing references list.
        filters: SearchFilters dict for screening target journals.
    """
    try:
        service = _get_journal_service()
        res = service.peers(
            references=references,
            file_path=file_path,
            filters=filters,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def get_submission_tracker_info(
    provider: str = "elsevier",
    data: Optional[Dict[str, Any]] = None,
    file_path: str = "",
    previous_file_path: str = "",
    include_title: bool = False,
) -> str:
    """Offline submission event parser and timeline analyzer.

    Note: Completely offline. Does not connect to live publisher APIs, public CORS proxies,
    or third-party trackers. Does not ask for or store user passwords.

    Args:
        provider: Submission tracker provider (currently 'elsevier').
        data: Optional raw ReviewEvents JSON data dict.
        file_path: Path to JSON file exported from submission system.
        previous_file_path: Path to prior snapshot JSON file for diff detection.
        include_title: Whether to include manuscript title in output (default False).
    """
    try:
        service = _get_journal_service()
        res = service.tracker(
            provider=provider,
            data=data,
            file_path=file_path,
            previous_file_path=previous_file_path,
            include_title=include_title,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def prepare_manuscript_for_journals(
    text: str = "",
    file_path: str = "",
    mode: str = "auto",
    max_chars: int = 60000,
) -> str:
    """Parse and normalize a manuscript or research idea locally for journal recommendation.

    Note:
    - 使用调用方当前Agent的模型；服务不自行调用LLM/不另需模型Key；
    - 缺画像/适配判断返回needs_agent_assessment而非假智能评分；
    - 不自动联网上传全文；
    - 推荐分不是录用概率。
    - Does not connect to Word/WPS live_bridge.

    Args:
        text: Direct manuscript or idea text (mutually exclusive with file_path).
        file_path: Local path to manuscript file (.txt, .md, .docx, .pdf; mutually exclusive with text).
        mode: Parsing mode ('auto', 'idea', or 'manuscript').
        max_chars: Maximum character count to extract (default 60000).
    """
    try:
        service = _get_journal_service()
        res = service.prepare_manuscript(
            text=text,
            file_path=file_path,
            mode=mode,
            max_chars=max_chars,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


@mcp_app.tool()
def recommend_journals(
    text: str = "",
    file_path: str = "",
    mode: str = "auto",
    profile: Optional[Dict[str, Any]] = None,
    assessments: Optional[List[Dict[str, Any]]] = None,
    candidate_records: Optional[List[Dict[str, Any]]] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> str:
    """Recommend academic journals by combining caller Agent semantic assessment with local database rules.

    Note:
    - 使用调用方当前Agent的模型；服务不自行调用LLM/不另需模型Key；
    - 缺画像/适配判断返回needs_agent_assessment而非假智能评分；
    - 不自动联网上传全文；
    - 推荐分不是录用概率。
    - Local snapshot and manual official evidence != real-time guarantee of safety.
    - Does not connect to Word/WPS live_bridge.

    Args:
        text: Direct manuscript or idea text (mutually exclusive with file_path).
        file_path: Local path to manuscript file (.txt, .md, .docx, .pdf; mutually exclusive with text).
        mode: Recommendation mode ('auto', 'idea', or 'manuscript').
        profile: Manuscript semantic profile dictionary generated by caller Agent.
        assessments: List of candidate journal fit assessment dicts (up to 200 items) generated by caller Agent.
        candidate_records: Optional list of candidate journal record dicts (up to 200 items).
        preferences: Optional author preferences and hard filter constraints dictionary.
    """
    try:
        service = _get_journal_service()
        res = service.recommend(
            text=text,
            file_path=file_path,
            mode=mode,
            profile=profile,
            assessments=assessments,
            candidate_records=candidate_records,
            preferences=preferences,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_journal_error(e)


def run_server() -> None:
    """Entrypoint to run the MCP server over stdio."""
    mcp_app.run(transport="stdio")


if __name__ == "__main__":
    run_server()
