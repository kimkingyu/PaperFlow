"""PaperFlow MCP Server.

Provides a standard Model Context Protocol (MCP) server that connects AI clients
(WorkBuddy, Qwen, Cursor, Claude Desktop, Cherry Studio) directly to Microsoft Word / WPS
for real-time document creation, editing, review comments, and academic anti-AI polishing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    from mcp.server.mcpserver import MCPServer
    mcp_app = MCPServer("PaperFlow-Word-Assistant")
except Exception:
    from mcp.server.fastmcp import FastMCP
    mcp_app = FastMCP("PaperFlow-Word-Assistant")

from paperflow.engine.anti_ai_cleaner import anti_ai_engine
from paperflow.engine.docx_builder import AcademicDocxBuilder
from paperflow.engine.standards_manager import get_standard_by_id, list_standards
from paperflow.engine.zotero_field import parse_citations
from paperflow.engine.word_live_bridge import live_bridge


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


def run_server() -> None:
    """Entrypoint to run the MCP server over stdio."""
    mcp_app.run(transport="stdio")


if __name__ == "__main__":
    run_server()
