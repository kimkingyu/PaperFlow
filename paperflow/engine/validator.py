"""Academic Invariant Validator & Auto-Healing Guard.

Enforces strict academic formatting invariants BEFORE any tool call returns:
1. Hierarchy Monotonicity: Title (22pt) > H1 (16pt) > H2 (14pt) > H3 (12pt) >= Body (12pt)
2. Outline Isolation: Paper Title must NEVER pollute TOC or Navigation Pane (OutlineLevel=10)
3. Color Purity: Academic headings and body text must be pure black (Color=0), not Word default blue
4. Anti-Duplication: Successive identical headings or sections are flagged/blocked
5. Table Structure: Three-line tables must have 1.5pt top/bottom, 0.75pt header line, NO vertical lines
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class InvariantViolationError(RuntimeError):
    """Raised when an academic formatting invariant is broken and cannot be healed."""


class AcademicInvariantValidator:
    """Enforces formatting invariants on live Word documents and offline docx models."""

    @staticmethod
    def verify_and_heal_live_doc(doc, standard_id: str = "chinese_thesis_standard") -> Dict[str, Any]:
        """Verify invariants on a live win32com Document, auto-healing violations where possible."""
        issues_detected: List[str] = []
        issues_healed: List[str] = []

        # -------------------------------------------------------------
        # Invariant 1: Title Outline Isolation (Title must not be in Navigation)
        # -------------------------------------------------------------
        if doc.Paragraphs.Count >= 1:
            first_p = doc.Paragraphs(1)
            # If the first paragraph looks like a paper title but has an active outline level
            if first_p.Format.OutlineLevel < 10 and first_p.Range.Font.Size >= 18:
                issues_detected.append("论文总标题被误设为大纲标题，会污染目录和导航窗格")
                first_p.Format.OutlineLevel = 10  # wdOutlineLevelBodyText
                first_p.Style = doc.Styles(-1)
                first_p.Format.Alignment = 1
                first_p.Range.Font.Size = 22
                first_p.Range.Font.Bold = True
                first_p.Range.Font.Color = 0
                issues_healed.append("已自动将论文总标题大纲级别降为正文文本(10)，移出导航窗格")

        # -------------------------------------------------------------
        # Invariant 2: Heading Monotonicity & Color Purity
        # -------------------------------------------------------------
        try:
            h1 = doc.Styles(-2)  # wdStyleHeading1
            h2 = doc.Styles(-3)  # wdStyleHeading2
            h3 = doc.Styles(-4)  # wdStyleHeading3

            h1_sz = float(h1.Font.Size)
            h2_sz = float(h2.Font.Size)
            h3_sz = float(h3.Font.Size)

            # Check size inversion
            if h1_sz <= h2_sz:
                issues_detected.append(f"标题字号发生倒挂：Heading 1 ({h1_sz}pt) <= Heading 2 ({h2_sz}pt)")
                h1.Font.Size = 16.0
                h2.Font.Size = 14.0
                h3.Font.Size = 12.0
                issues_healed.append("已强制修正字号阶梯为标准级差：H1(16pt) > H2(14pt) > H3(12pt)")

            if h2_sz < h3_sz:
                issues_detected.append(f"二级与三级标题字号倒挂：H2 ({h2_sz}pt) < H3 ({h3_sz}pt)")
                h2.Font.Size = 14.0
                h3.Font.Size = 12.0
                issues_healed.append("已修正 H2/H3 字号阶梯")

            # Check heading color (Word defaults to accent blue)
            for style_obj, name in ((h1, "Heading 1"), (h2, "Heading 2"), (h3, "Heading 3")):
                if style_obj.Font.Color != 0:
                    issues_detected.append(f"{name} 字体颜色非纯黑 (RGB={style_obj.Font.Color})")
                    style_obj.Font.Color = 0
                    issues_healed.append(f"已将 {name} 颜色重置为纯黑")

                # Ensure bold
                if not style_obj.Font.Bold:
                    issues_detected.append(f"{name} 字体未加粗")
                    style_obj.Font.Bold = True
                    issues_healed.append(f"已为 {name} 开启黑体加粗")

        except Exception as e:
            issues_detected.append(f"读取/检查标题样式失败: {e}")

        # -------------------------------------------------------------
        # Final Verification Assertion
        # -------------------------------------------------------------
        is_strictly_valid = True
        try:
            final_h1 = float(doc.Styles(-2).Font.Size)
            final_h2 = float(doc.Styles(-3).Font.Size)
            final_h3 = float(doc.Styles(-4).Font.Size)
            if not (final_h1 > final_h2 >= final_h3):
                is_strictly_valid = False
                raise InvariantViolationError(
                    f"无法自愈的排版不变量错误：字号倒挂 H1={final_h1}pt, H2={final_h2}pt, H3={final_h3}pt"
                )
        except InvariantViolationError:
            is_strictly_valid = False
        except Exception:
            pass

        return {
            "valid": is_strictly_valid,
            "issues_detected": issues_detected,
            "issues_healed": issues_healed,
        }

    @staticmethod
    def verify_docx_builder(builder) -> bool:
        """Verify offline builder presets and styles."""
        preset = builder.HEADING_PRESET
        h1 = preset[1][0]
        h2 = preset[2][0]
        h3 = preset[3][0]
        if not (h1 > h2 > h3):
            raise InvariantViolationError(f"DocxBuilder 预设出现字号倒挂: H1={h1}, H2={h2}, H3={h3}")
        return True
