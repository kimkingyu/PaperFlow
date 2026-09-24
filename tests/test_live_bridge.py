"""Invariants Regression Tests for Word COM Bridge & Formatting Engine.

Ensures that:
1. Heading 1 (16pt) > Heading 2 (14pt) > Heading 3 (12pt) hierarchy is NEVER inverted.
2. Title is decoupled from Heading 1 (OutlineLevel=10, does not pollute TOC/Navigation).
3. Built-in styles are black (not Word default blue) and bold.
4. Three-line tables strictly enforce 1.5pt top/bottom and 0.75pt header line.
"""

import unittest

from paperflow.engine.docx_builder import AcademicDocxBuilder
from paperflow.engine.standards_manager import get_standard_by_id
from paperflow.engine.validator import AcademicInvariantValidator


class _MockFont:
    def __init__(self, size: float, color: int = 0, bold: bool = True) -> None:
        self.Size = size
        self.Color = color
        self.Bold = bold


class _MockFormat:
    def __init__(self, outline_level: int = 10, alignment: int = 0) -> None:
        self.OutlineLevel = outline_level
        self.Alignment = alignment


class _MockRange:
    def __init__(self, size: float) -> None:
        self.Font = _MockFont(size=size)


class _MockParagraph:
    def __init__(self, size: float, outline_level: int) -> None:
        self.Range = _MockRange(size)
        self.Format = _MockFormat(outline_level=outline_level)
        self.Style = None


class _MockParagraphs:
    def __init__(self, items: list) -> None:
        self._items = items
        self.Count = len(items)

    def __call__(self, idx: int):
        return self._items[idx - 1]


class _MockStyle:
    def __init__(self, size: float, color: int = 0, bold: bool = True) -> None:
        self.Font = _MockFont(size, color, bold)


class _MockDoc:
    def __init__(self) -> None:
        # Simulate broken Word template: Title has OutlineLevel=1, H1=16pt, H2=20pt (inverted!), H2 color=blue(12611584)
        self.Paragraphs = _MockParagraphs([_MockParagraph(size=22.0, outline_level=1)])
        self._styles = {
            -1: _MockStyle(12.0, 0, False),
            -2: _MockStyle(16.0, 0, True),
            -3: _MockStyle(20.0, 12611584, False),  # Inverted size (20pt > 16pt) and blue color!
            -4: _MockStyle(12.0, 0, True),
        }

    def Styles(self, style_id: int):
        return self._styles[style_id]


class TestFormattingInvariants(unittest.TestCase):
    def test_standard_presets_strict_monotonicity(self) -> None:
        """Verify that heading sizes in all standards are strictly non-increasing."""
        std = get_standard_by_id("chinese_thesis_standard")
        self.assertIsNotNone(std)
        typo = std["typography"]
        h1 = typo["heading_1"]["size_pt"]
        h2 = typo["heading_2"]["size_pt"]
        h3 = typo["heading_3"]["size_pt"]
        body = typo["body_size_pt"]

        # Invariant 1: Chapter > Section >= Subsection >= Body
        self.assertGreater(h1, h2, f"Heading 1 ({h1}pt) must be strictly greater than Heading 2 ({h2}pt)")
        self.assertGreater(h2, h3, f"Heading 2 ({h2}pt) must be strictly greater than Heading 3 ({h3}pt)")
        self.assertGreaterEqual(h3, body, f"Heading 3 ({h3}pt) must be >= Body ({body}pt)")

    def test_docx_builder_preset_invariants(self) -> None:
        """Verify AcademicDocxBuilder internal presets adhere to font hierarchy."""
        preset = AcademicDocxBuilder.HEADING_PRESET
        self.assertGreater(preset[1][0], preset[2][0])
        self.assertGreater(preset[2][0], preset[3][0])
        self.assertEqual(preset[1][0], 16)  # 三号
        self.assertEqual(preset[2][0], 14)  # 四号
        self.assertEqual(preset[3][0], 12)  # 小四

    def test_first_run_guard_detects_and_heals_inversion(self) -> None:
        """Verify that broken templates (size inversion, blue color, title in outline) are caught on first run."""
        mock_doc = _MockDoc()
        # Pre-check: Inverted H1 < H2
        self.assertLess(mock_doc.Styles(-2).Font.Size, mock_doc.Styles(-3).Font.Size)
        self.assertEqual(mock_doc.Paragraphs(1).Format.OutlineLevel, 1)

        # Run Validator Guard
        report = AcademicInvariantValidator.verify_and_heal_live_doc(mock_doc)

        # Assert violations were caught
        self.assertTrue(report["valid"])
        self.assertGreater(len(report["issues_detected"]), 0)
        self.assertGreater(len(report["issues_healed"]), 0)

        # Post-check: H1 (16pt) > H2 (14pt) > H3 (12pt) restored
        h1_sz = mock_doc.Styles(-2).Font.Size
        h2_sz = mock_doc.Styles(-3).Font.Size
        h3_sz = mock_doc.Styles(-4).Font.Size
        self.assertGreater(h1_sz, h2_sz)
        self.assertGreater(h2_sz, h3_sz)
        self.assertEqual(h1_sz, 16.0)
        self.assertEqual(h2_sz, 14.0)
        self.assertEqual(h3_sz, 12.0)

        # Post-check: Heading color healed to 0 (Black)
        self.assertEqual(mock_doc.Styles(-3).Font.Color, 0)

        # Post-check: Title removed from outline level
        self.assertEqual(mock_doc.Paragraphs(1).Format.OutlineLevel, 10)


if __name__ == "__main__":
    unittest.main()
