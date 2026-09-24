"""Word Live Bridge Engine.

Connects directly to the live, running Microsoft Word or WPS Office instance
via Windows COM Automation, allowing MCP clients (NarraFork, Claude Desktop,
Cursor, ...) to interact with the open document in real time:
- Read active document content and structure
- Read current user selection (without copy-pasting)
- Write headings and paragraphs at cursor or document end
- Add native Word comments to selected text
- Toggle Track Changes for visible diffs
- Apply Chinese academic typography presets

Threading model
---------------
COM objects are apartment-bound. The MCP server runs synchronous tools on
arbitrary worker threads, so every COM call here is funnelled through ONE
dedicated thread that calls CoInitialize once and owns the Word proxy.
Public methods are safe to call from any thread.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from paperflow.engine.validator import AcademicInvariantValidator

_T = TypeVar("_T")


class WordNotAvailableError(RuntimeError):
    """Raised when no running Word/WPS instance or no open document is found."""


class _ComThread:
    """A single long-lived thread that owns COM initialisation."""

    def __init__(self) -> None:
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    @staticmethod
    def _init_com() -> None:
        import pythoncom

        pythoncom.CoInitialize()

    def run(self, fn: Callable[[], _T], timeout: float = 30.0) -> _T:
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="paperflow-com",
                    initializer=self._init_com,
                )
            executor = self._executor
        return executor.submit(fn).result(timeout=timeout)


class WordLiveBridge:
    """Live COM bridge connecting directly to running Microsoft Word or WPS."""

    def __init__(self) -> None:
        self._word_app = None
        self._app_type: Optional[str] = None  # "msword" or "wps"
        self._com = _ComThread()
        self._target_doc_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection (runs on the COM thread)
    # ------------------------------------------------------------------
    def _connect_on_com_thread(self) -> Tuple[bool, str]:
        try:
            import win32com.client
        except ImportError:
            return False, "pywin32 is not installed. Please run: pip install pywin32"

        candidates = (
            ("Word.Application", "msword"),
            ("KWps.Application", "wps"),
            ("Wps.Application", "wps"),
        )
        for progid, app_type in candidates:
            try:
                app = win32com.client.GetActiveObject(progid)
            except Exception:
                continue
            if app:
                self._word_app = app
                self._app_type = app_type
                return True, f"已连接到正在运行的 {progid}。"

        self._word_app = None
        self._app_type = None
        # Never launch Word ourselves: that would pop a window on the user's desktop.
        return False, "未检测到正在运行的 Word 或 WPS。请先在桌面上打开一篇文档再试。"

    def connect(self) -> Tuple[bool, str]:
        """Attach to an already running Word or WPS instance (never launches one)."""
        return self._com.run(self._connect_on_com_thread)

    # ------------------------------------------------------------------
    # Helpers (must only be called on the COM thread)
    # ------------------------------------------------------------------
    def _active_doc(self):
        """Return the active or targeted document. Reconnects once if the proxy went stale."""
        for attempt in range(2):
            if self._word_app is None:
                ok, msg = self._connect_on_com_thread()
                if not ok:
                    raise WordNotAvailableError(msg)
            try:
                # If target doc path is set, search open documents or open it from disk
                if self._target_doc_path:
                    norm_target = os.path.normpath(self._target_doc_path).lower()
                    target_name = os.path.basename(self._target_doc_path).lower()
                    for i in range(1, self._word_app.Documents.Count + 1):
                        d = self._word_app.Documents(i)
                        try:
                            if os.path.normpath(d.FullName).lower() == norm_target or d.Name.lower() == target_name:
                                d.Activate()
                                return d
                        except Exception:
                            continue
                    if os.path.isfile(self._target_doc_path):
                        opened = self._word_app.Documents.Open(os.path.abspath(self._target_doc_path))
                        opened.Activate()
                        return opened

                if self._word_app.Documents.Count == 0:
                    raise WordNotAvailableError("Word/WPS 已运行，但没有打开任何文档。请先打开或新建一篇文档。")
                return self._word_app.ActiveDocument
            except WordNotAvailableError:
                raise
            except Exception:
                # Word was closed and reopened: the cached proxy is dead.
                self._word_app = None
                if attempt == 1:
                    raise
        raise WordNotAvailableError("无法获取当前文档。")

    @staticmethod
    def _clean(text: Optional[str]) -> str:
        return (text or "").replace("\r", "\n").replace("\x07", "").strip()

    def _call(self, fn: Callable[[], _T]) -> _T:
        return self._com.run(fn)

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------
    def select_document(self, file_path_or_name: str) -> Dict[str, Any]:
        """Lock and activate a specific document by path or filename (e.g. 'D:\\tes\\test.docx')."""
        def work() -> Dict[str, Any]:
            self._target_doc_path = file_path_or_name
            doc = self._active_doc()
            return {
                "success": True,
                "document_name": doc.Name,
                "document_path": doc.FullName,
                "paragraph_count": doc.Paragraphs.Count,
                "message": f"已成功锁定并激活目标文档：{doc.FullName}",
            }
        return self._call(work)

    def get_document_info(self, preview_paragraphs: int = 10, target_path: Optional[str] = None) -> Dict[str, Any]:
        """Name, path, counts and a short text preview of the active or targeted document."""

        def work() -> Dict[str, Any]:
            if target_path:
                self._target_doc_path = target_path
            doc = self._active_doc()
            paras = doc.Paragraphs.Count
            preview = []
            for i in range(1, min(paras, preview_paragraphs) + 1):
                text = self._clean(doc.Paragraphs(i).Range.Text)
                if text:
                    preview.append(text)
            return {
                "status": "connected",
                "app_type": self._app_type,
                "document_name": doc.Name,
                "document_path": doc.FullName,
                "saved": bool(doc.Saved),
                "paragraph_count": paras,
                "word_count": doc.ComputeStatistics(0),  # wdStatisticWords
                "track_revisions": bool(doc.TrackRevisions),
                "preview_content": preview,
            }

        return self._call(work)

    def get_selected_text(self) -> Dict[str, Any]:
        """Text the user currently has selected in Word."""

        def work() -> Dict[str, Any]:
            self._active_doc()
            sel = self._word_app.Selection
            text = self._clean(sel.Text)
            has_selection = sel.End > sel.Start and bool(text)
            return {
                "has_selection": has_selection,
                "text": text if has_selection else "",
                "start": sel.Start,
                "end": sel.End,
            }

        return self._call(work)

    def clear_document_content(self) -> Dict[str, Any]:
        """Clear all content in the active document (use when resetting a draft)."""
        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            # Clear all story ranges
            doc.Content.Delete()
            return {"success": True, "message": "文档内容已清空"}
        return self._call(work)

    def insert_text(self, text: str, heading_level: int = 0, position: str = "cursor") -> Dict[str, Any]:
        """Insert a paragraph at the cursor or at the end of the document.

        heading_level:
            -1 = 论文总标题 (Title: 二号22pt黑体居中加粗，不混入章节导航窗格和目录)
             0 = 正文 (Normal: 小四12pt宋体，首行缩进2字符)
             1 = 一级标题 (Heading 1: 三号16pt黑体居中)
             2 = 二级标题 (Heading 2: 四号14pt黑体居左)
             3 = 三级标题 (Heading 3: 小四12pt黑体居左)
        position: 'cursor' or 'end'.
        """
        if heading_level not in (-1, 0, 1, 2, 3):
            raise ValueError("heading_level must be -1, 0, 1, 2 or 3")
        if position not in ("cursor", "end"):
            raise ValueError("position must be 'cursor' or 'end'")

        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            sel = self._word_app.Selection
            if position == "end":
                sel.EndKey(Unit=6)  # wdStory

            if heading_level == -1:
                # 论文总大标题 (Paper Title)：二号22pt 黑体 居中 纯黑 加粗，大纲级别为正文文本(10)
                sel.Style = doc.Styles(-1)
                sel.ParagraphFormat.Alignment = 1  # 居中
                sel.ParagraphFormat.CharacterUnitFirstLineIndent = 0
                sel.ParagraphFormat.SpaceBefore = 18
                sel.ParagraphFormat.SpaceAfter = 18
                sel.ParagraphFormat.OutlineLevel = 10  # wdOutlineLevelBodyText，绝不进入左侧导航
                sel.Font.NameFarEast = "黑体"
                sel.Font.NameAscii = "Times New Roman"
                sel.Font.Size = 22  # 二号字
                sel.Font.Bold = True
                sel.Font.Color = 0
                sel.TypeText(text)
                sel.TypeParagraph()
                # 恢复为普通正文
                sel.Style = doc.Styles(-1)
                sel.ParagraphFormat.Alignment = 0
                sel.ParagraphFormat.CharacterUnitFirstLineIndent = 2
                sel.ParagraphFormat.SpaceBefore = 0
                sel.ParagraphFormat.SpaceAfter = 0
                sel.ParagraphFormat.OutlineLevel = 10
            else:
                # Built-in style ids: wdStyleNormal=-1, wdStyleHeading1=-2, Heading2=-3, Heading3=-4
                style_id = -1 - heading_level if heading_level else -1
                sel.Style = doc.Styles(style_id)
                sel.TypeText(text)
                sel.TypeParagraph()
                if heading_level:
                    sel.Style = doc.Styles(-1)

            guard_report = None
            if heading_level in (-1, 1, 2, 3):
                guard_report = AcademicInvariantValidator.verify_and_heal_live_doc(doc)

            return {
                "success": True,
                "inserted_chars": len(text),
                "heading_level": heading_level,
                "position": position,
                "invariant_guard": guard_report,
            }

        return self._call(work)

    def replace_selection(self, new_text: str) -> Dict[str, Any]:
        """Replace the current selection. Refuses to act on an empty selection."""

        def work() -> Dict[str, Any]:
            self._active_doc()
            sel = self._word_app.Selection
            old = self._clean(sel.Text)
            if sel.End <= sel.Start or not old:
                return {"success": False, "error": "Word 中没有选中任何文字，未做修改。"}
            sel.Text = new_text
            return {"success": True, "old_text": old, "new_text": new_text}

        return self._call(work)

    def add_comment(self, comment_text: str, author: str = "PaperFlow AI") -> Dict[str, Any]:
        """Attach a native Word comment to the current selection."""

        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            sel = self._word_app.Selection
            target = self._clean(sel.Text)
            if sel.End <= sel.Start or not target:
                return {"success": False, "error": "Word 中没有选中任何文字，请先选中要批注的内容。"}
            comment = doc.Comments.Add(sel.Range, comment_text)
            try:
                comment.Author = author
            except Exception:
                pass  # some Word/WPS builds make Author read-only
            return {"success": True, "target_text": target, "comment": comment_text, "author": author}

        return self._call(work)

    def set_track_revisions(self, enable: bool = True) -> Dict[str, Any]:
        """Turn Track Changes on or off for the active document."""

        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            doc.TrackRevisions = bool(enable)
            return {"success": True, "track_revisions": bool(doc.TrackRevisions)}

        return self._call(work)

    def apply_academic_preset(self, standard_id: str = "chinese_thesis_standard") -> Dict[str, Any]:
        """Apply academic preset based on built-in 2021-2026 standard ID."""
        presets = {
            "chinese_thesis_standard": {
                "far_east": "宋体",
                "ascii": "Times New Roman",
                "size": 12.0,
                "line_rule": 1,  # wdLineSpace1pt5
                "indent": 2,
                "desc": "国内高校博硕/本科学位论文标准 (宋体/Times New Roman | 小四 12pt | 1.5倍行距 | 首行缩进2字符)",
            },
            "chinese_journal_standard": {
                "far_east": "宋体",
                "ascii": "Times New Roman",
                "size": 10.5,
                "line_rule": 1,
                "indent": 2,
                "desc": "中文核心/科技期刊标准 (宋体/Times New Roman | 五号 10.5pt | 紧凑行距 | 首行缩进2字符)",
            },
            "ieee_conference_2024": {
                "far_east": "Times New Roman",
                "ascii": "Times New Roman",
                "size": 10.0,
                "line_rule": 0,  # wdLineSpaceSingle
                "indent": 2,
                "desc": "IEEE Transactions/Conferences 2024-2026 标准 (Times New Roman | 10pt | 单倍行距)",
            },
            "apa_7th_edition": {
                "far_east": "Times New Roman",
                "ascii": "Times New Roman",
                "size": 12.0,
                "line_rule": 2,  # wdLineSpaceDouble
                "indent": 2,
                "desc": "APA 7th Edition 标准 (Times New Roman | 12pt | 双倍行距 | 1英寸页边距)",
            },
        }
        cfg = presets.get(standard_id, presets["chinese_thesis_standard"])

        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            normal = doc.Styles(-1)  # wdStyleNormal
            font = normal.Font
            font.NameFarEast = cfg["far_east"]
            font.NameAscii = cfg["ascii"]
            font.NameOther = cfg["ascii"]
            font.Size = cfg["size"]
            pf = normal.ParagraphFormat
            pf.LineSpacingRule = cfg["line_rule"]
            pf.CharacterUnitFirstLineIndent = cfg["indent"]

            # Configure Heading 1-3 built-in styles for Chinese standards
            if "chinese" in standard_id:
                # Heading 1: 三号 16pt 黑体 居中 纯黑 加粗
                h1 = doc.Styles(-2)
                h1.Font.NameFarEast = "黑体"
                h1.Font.NameAscii = "Times New Roman"
                h1.Font.Size = 16
                h1.Font.Bold = True
                h1.Font.Color = 0
                h1.ParagraphFormat.Alignment = 1  # wdAlignParagraphCenter
                h1.ParagraphFormat.CharacterUnitFirstLineIndent = 0
                h1.ParagraphFormat.SpaceBefore = 12
                h1.ParagraphFormat.SpaceAfter = 12
                h1.ParagraphFormat.LineSpacingRule = 1

                # Heading 2: 四号 14pt 黑体 居左 纯黑 加粗
                h2 = doc.Styles(-3)
                h2.Font.NameFarEast = "黑体"
                h2.Font.NameAscii = "Times New Roman"
                h2.Font.Size = 14
                h2.Font.Bold = True
                h2.Font.Color = 0
                h2.ParagraphFormat.Alignment = 0  # wdAlignParagraphLeft
                h2.ParagraphFormat.CharacterUnitFirstLineIndent = 0
                h2.ParagraphFormat.SpaceBefore = 6
                h2.ParagraphFormat.SpaceAfter = 6
                h2.ParagraphFormat.LineSpacingRule = 1

                # Heading 3: 小四 12pt 黑体 居左 纯黑 加粗
                h3 = doc.Styles(-4)
                h3.Font.NameFarEast = "黑体"
                h3.Font.NameAscii = "Times New Roman"
                h3.Font.Size = 12
                h3.Font.Bold = True
                h3.Font.Color = 0
                h3.ParagraphFormat.Alignment = 0
                h3.ParagraphFormat.CharacterUnitFirstLineIndent = 0
                h3.ParagraphFormat.SpaceBefore = 6
                h3.ParagraphFormat.SpaceAfter = 0
                h3.ParagraphFormat.LineSpacingRule = 1

                # 严格的不变量自检：禁止任何字号倒挂发生
                h1_sz = h1.Font.Size
                h2_sz = h2.Font.Size
                h3_sz = h3.Font.Size
                if not (h1_sz > h2_sz >= h3_sz):
                    raise RuntimeError(
                        f"排版不变量破坏：标题字号出现倒挂！H1={h1_sz}pt, H2={h2_sz}pt, H3={h3_sz}pt"
                    )

            # 自动执行无条件强制自检与自愈守门器
            guard_report = AcademicInvariantValidator.verify_and_heal_live_doc(doc, standard_id)

            try:
                doc.Save()
            except Exception:
                pass

            return {
                "success": True,
                "standard_id": standard_id,
                "details": cfg["desc"],
                "invariant_guard": guard_report,
            }

        return self._call(work)

    def apply_chinese_academic_preset(self) -> Dict[str, Any]:
        """Backward-compatible alias for chinese_thesis_standard."""
        return self.apply_academic_preset("chinese_thesis_standard")

    def insert_academic_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        caption: str = "",
        position: str = "cursor",
    ) -> Dict[str, Any]:
        """Insert an academic three-line table (1.5pt top/bottom, 0.75pt header bottom, no vertical lines) into active Word."""
        num_cols = len(headers)
        if num_cols == 0:
            return {"success": False, "error": "headers cannot be empty"}

        def work() -> Dict[str, Any]:
            doc = self._active_doc()
            sel = self._word_app.Selection
            if position == "end":
                sel.EndKey(Unit=6)  # wdStory

            # 1. Caption paragraph (居中，五号粗体)
            if caption:
                sel.Style = doc.Styles(-1)  # Normal
                sel.ParagraphFormat.Alignment = 1  # wdAlignParagraphCenter
                sel.ParagraphFormat.CharacterUnitFirstLineIndent = 0
                sel.Font.Bold = True
                sel.Font.Size = 10.5
                sel.TypeText(caption)
                sel.TypeParagraph()

            # 2. Add Table at selection
            total_rows = 1 + len(rows)
            tbl = doc.Tables.Add(Range=sel.Range, NumRows=total_rows, NumColumns=num_cols)
            tbl.Range.ParagraphFormat.Alignment = 1
            tbl.Range.ParagraphFormat.CharacterUnitFirstLineIndent = 0
            tbl.Range.Font.Size = 10.5
            tbl.Range.Font.Bold = False

            # Set three-line borders (clear all borders, then set top & bottom to 1.5pt)
            try:
                for border_id in (-1, -2, -3, -4, -5, -6):
                    tbl.Borders(border_id).LineStyle = 0
                tbl.Borders(-1).LineStyle = 1  # wdBorderTop
                tbl.Borders(-1).LineWidth = 12  # 1.5pt
                tbl.Borders(-3).LineStyle = 1  # wdBorderBottom
                tbl.Borders(-3).LineWidth = 12  # 1.5pt
            except Exception:
                pass

            # Header row
            for c_idx, h_text in enumerate(headers, start=1):
                cell = tbl.Cell(1, c_idx)
                cell.Range.Text = str(h_text)
                cell.Range.Font.Bold = True
                try:
                    cell.Borders(-3).LineStyle = 1  # wdBorderBottom
                    cell.Borders(-3).LineWidth = 6  # 0.75pt
                except Exception:
                    pass

            # Data rows
            for r_idx, row_data in enumerate(rows, start=2):
                for c_idx in range(1, min(num_cols, len(row_data)) + 1):
                    tbl.Cell(r_idx, c_idx).Range.Text = str(row_data[c_idx - 1])

            # Collapse selection after table
            tbl.Range.Select()
            sel.Collapse(Direction=0)  # wdCollapseEnd
            sel.TypeParagraph()
            sel.Style = doc.Styles(-1)
            sel.ParagraphFormat.Alignment = 0
            sel.ParagraphFormat.CharacterUnitFirstLineIndent = 2

            return {
                "success": True,
                "rows": total_rows,
                "columns": num_cols,
                "caption": caption,
            }

        return self._call(work)


# Singleton used by the MCP server
live_bridge = WordLiveBridge()
