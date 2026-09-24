# PaperFlow-MCP: Live Office/Word Bridge & 0-to-1 Academic Writing Suite for AI Clients

<p align="center">
  <b>Enabling Tencent WorkBuddy, Qwen, Cursor, and Claude Desktop to directly mount and control live Microsoft Word & WPS instances in real time!</b><br>
  Featuring real-time cursor-level control, native Word comment bubbles, live Zotero CSL field code citations, anti-AI de-flavoring, and a complete 0-to-1 scholarly manuscript pipeline.
</p>

---

## Key Highlights

1. **One-Click Auto-Connect (`python -m paperflow setup`)**:
   Automatically detects and safely injects MCP configuration into:
   - Tencent WorkBuddy / CodeBuddy
   - Qwen Desktop / Qwen Agent / Qoder
   - Claude Desktop
   - Cursor
   - Cherry Studio
   - VS Code (Cline / Roo Code)

2. **True Real-Time Office/Word Live Integration**:
   - **Selection Sensing**: Reads whatever text the user highlights with their cursor in Word—no manual copy-pasting required.
   - **Native Comment Bubbles**: AI critique and review remarks appear directly as genuine Word comment bubbles (`Comments`) attached to the relevant sentences.
   - **Track Changes Mode**: One-click revision tracking showing transparent red/green diffs so researchers can accept/reject each modification.
   - **Academic Typography**: Standardized dual fonts (SimSun / Times New Roman), 1.5 line spacing, 2-character indentation, and hierarchical heading numbering.

3. **Live Zotero Citation Injection (CSL Field Codes)**:
   Embeds authentic `ADDIN ZOTERO_ITEM` and `ADDIN ZOTERO_BIBL` fields. Opening the document in Word allows researchers to click "Refresh" in the Zotero plugin to reformat citations across IEEE, APA, Nature, and GB/T formats on the fly.

4. **Academic Anti-AI De-flavoring**:
   Systematically identifies and rewrites robotic AI markers (`delve into`, `testament to`, `vital role`, repetitive `moreover`) to replicate authentic scholarly cadence.

---

## MCP Tools Reference

| Tool Name | Description |
| :--- | :--- |
| `get_active_word_doc` | Inspect active Word/WPS document name, path, word count, and text preview |
| `get_word_selection` | Read the text currently selected by the user's cursor in Word |
| `write_to_active_word` | Insert text or headings (levels 1-3) directly at cursor or document end |
| `replace_word_selection` | Replace the highlighted text with polished academic phrasing |
| `add_word_comment` | Attach a native Word review comment bubble onto the selected text |
| `set_word_track_revisions` | Toggle Word's Track Changes mode on/off |
| `apply_academic_style_preset` | Apply standardized academic layout and typography rules |
| `scan_anti_ai_flavor` | Scan text for AI clichés and receive structured human scholar rewrite suggestions |
| `generate_offline_paper_docx` | Build a publication-ready `.docx` file from scratch with Zotero live citations |

---

## Quick Start

```bash
git clone https://github.com/your-username/paperflow-mcp.git
cd paperflow-mcp

# Install
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .

# Auto-connect to all installed AI clients
python -m paperflow setup

# Check environment
python -m paperflow doctor
```

Open Word or WPS, restart your AI client, and start writing!

---

## License

MIT License. Contributions and PRs are warmly welcomed!
