"""MCP Apps (SEP-1865) wiring: wire-level metadata and text fallback."""
import asyncio
import json

from paperflow.server import mcp_server
from paperflow.server.mcp_server import STUDIO_MIME, STUDIO_URI, mcp_app, open_journal_studio, studio_api


def _tools():
    listed = asyncio.run(mcp_app.list_tools())
    tools = listed if isinstance(listed, list) else listed.tools
    return {t.name: t.model_dump(by_alias=True, exclude_none=True) for t in tools}


def test_ui_resource_is_declared_with_spec_mime_type_and_csp(monkeypatch):
    monkeypatch.setattr("paperflow.gui.server.load_page", lambda: "<!DOCTYPE html><html></html>")
    listed = asyncio.run(mcp_app.list_resources())
    resources = listed if isinstance(listed, list) else listed.resources
    view = next(r.model_dump(by_alias=True, exclude_none=True) for r in resources if str(r.uri) == STUDIO_URI)
    assert view["mimeType"] == "text/html;profile=mcp-app" == STUDIO_MIME
    assert view["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    contents = asyncio.run(mcp_app.read_resource(STUDIO_URI))
    first = list(contents)[0]
    assert first.mime_type == STUDIO_MIME and first.content.startswith("<!DOCTYPE html>")


def test_tools_link_the_view_with_nested_meta_only():
    tools = _tools()
    for name in ("open_journal_studio", "search_academic_journals", "recommend_journals"):
        meta = tools[name]["_meta"]
        assert meta["ui"]["resourceUri"] == STUDIO_URI
        assert "ui/resourceUri" not in meta  # deprecated flat key is not used
    assert tools["studio_api"]["_meta"]["ui"]["visibility"] == ["app"]
    assert "_meta" not in tools["scan_anti_ai_flavor"] or "ui" not in tools["scan_anti_ai_flavor"].get("_meta", {})


def test_text_fallback_for_hosts_without_mcp_apps():
    payload = json.loads(open_journal_studio("search"))
    assert payload["data"]["tab"] == "search"
    assert "python -m paperflow gui" in payload["message"]
    assert json.loads(open_journal_studio("bogus"))["data"]["tab"] == "recommend"


def test_studio_api_is_allow_listed_and_path_free(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(tmp_path / "home"))
    ok = json.loads(studio_api("overview"))
    assert ok["status"] == "success" and ok["data"]["database_initialized"] is False
    bad = json.loads(studio_api("recommend", {"text": "x", "file_path": "C:/secret.txt"}))
    assert bad["status"] == "error" and bad["error_code"] == "INVALID_INPUT"
    assert json.loads(studio_api("rm_rf"))["error_code"] == "INVALID_INPUT"


def test_recommend_tool_forwards_publish_to_gui(monkeypatch):
    seen = {}

    class Fake:
        def recommend(self, **kwargs):
            seen.update(kwargs)
            return {"status": "success", "data": {}}

    monkeypatch.setattr(mcp_server, "_get_journal_service", lambda: Fake())
    mcp_server.recommend_journals(text="idea", publish_to_gui=True)
    assert seen["publish_to_gui"] is True
    seen.clear()
    mcp_server.recommend_journals(text="idea")
    assert "publish_to_gui" not in seen
