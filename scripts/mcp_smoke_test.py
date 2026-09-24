"""End-to-end MCP smoke test.

Spawns `python -m paperflow run` over stdio exactly like an MCP client
(NarraFork, Claude Desktop, Cursor) would, then:
1. lists the tools the server exposes,
2. calls get_active_word_doc (safe, read-only; never launches Word),
3. calls scan_anti_ai_flavor (pure text, no Office needed).

With `--journal-only`:
Skips get_active_word_doc and all Office calls, lists tools (verifying the 9 journal tools),
calls list_journal_sources, get_submission_tracker_info (no input guide), and search_academic_journals
on empty store expecting controlled error response.

Usage:
    .venv/Scripts/python.exe scripts/mcp_smoke_test.py
    .venv/Scripts/python.exe scripts/mcp_smoke_test.py --journal-only
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent

JOURNAL_TOOL_NAMES = [
    "analyze_related_journals",
    "check_journal_warning",
    "compare_academic_journals",
    "get_journal_details",
    "get_submission_tracker_info",
    "import_journal_data",
    "list_journal_sources",
    "refresh_journal_sources",
    "search_academic_journals",
    "prepare_manuscript_for_journals",
    "recommend_journals",
]


def _text(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


async def _recommendation_smoke(session, store_dir):
    text = "I plan bearing fault diagnostics using vibration data."
    async def call(name, args):
        payload = json.loads(_text(await session.call_tool(name, args)))
        assert payload["status"] == "success", payload.get("error_code")
        return payload["data"]
    prepared = await call("prepare_manuscript_for_journals", {"text": text})
    profile = {"input_id": prepared["input_id"], "mode": "idea", "summary": text,
               "keywords": ["bearing", "vibration"], "article_type": "research", "readiness": "planned"}
    provenance = {"source_id": "fictional-smoke", "source_url": "https://publisher.example/fictional",
                  "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "authority": "official"}
    candidates = [{"title": "Fictional Recommendation Journal", "issns": ["1234-5679"], "oa_mode": "full",
                   "editorial_profiles": [{"scope_summary": "Research on bearing fault diagnostics using vibration data.",
                                           "article_types": ["research"], "article_types_complete": True,
                                           "positioning": "application", "positioning_basis": "Fictional editorial criteria",
                                           "provenance": provenance}],
                   "publication_fees": [{"route": "open_access", "amount": 250, "currency": "CNY",
                                         "taxes_included": True, "provenance": provenance}]}]
    args = {"text": text, "profile": profile, "candidate_records": candidates,
            "preferences": {"max_budget": 800, "currency": "CNY"}}
    first = await call("recommend_journals", args)
    assert first["stage"] == "needs_agent_assessment"
    args["assessments"] = [{"journal_id": first["assessment_targets"][0]["journal_id"],
                            "context_id": first["context_id"], "scope_fit": 90, "goal_fit": 80,
                            "rationale": "Synthetic scope match, not an acceptance prediction",
                            "evidence": [{"manuscript_quote": "bearing fault diagnostics",
                                          "journal_quote": "bearing fault diagnostics"}]}]
    final = await call("recommend_journals", args)
    assert final["stage"] == "scored" and final["backend_calls_llm"] is False
    card = final["groups"]["efficiency"]["provisional"][0]
    assert card["score"]["value"] > 80
    assert card["cost"]["selected"]["estimated_total"] == 250
    assert not (Path(store_dir) / "journals.sqlite3").exists()
    print("[recommend_journals] prepare -> agent packet -> scored | ephemeral | no LLM/Word/network calls")


async def main() -> int:
    journal_only = "--journal-only" in sys.argv[1:]

    with tempfile.TemporaryDirectory(prefix="smoke-journal-store-") as store_dir:
        env_map = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PAPERFLOW_JOURNAL_HOME": str(store_dir),
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "paperflow", "run"],
            env=env_map,
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                print(f"[tools] {len(names)} exposed")

                if journal_only:
                    # 1. Verify 9 journal tools are registered
                    for j_tool in JOURNAL_TOOL_NAMES:
                        assert j_tool in names, f"Expected journal tool {j_tool} in exposed tools"
                    print(f"[check] Verified {len(JOURNAL_TOOL_NAMES)} journal tools exposed")

                    # 2. Call list_journal_sources (concise summary instead of dumping whole catalog)
                    res = await session.call_tool("list_journal_sources", {})
                    sources_payload = json.loads(_text(res))
                    assert sources_payload.get("status") in ("success", "partial")
                    print(f"[list_journal_sources] status={sources_payload.get('status')} | source_count={len(sources_payload['data'])}")

                    # 3. Call get_submission_tracker_info without input (returns format guidance)
                    res_tracker = await session.call_tool("get_submission_tracker_info", {})
                    tracker_payload = json.loads(_text(res_tracker))
                    assert tracker_payload.get("status") == "success"
                    assert tracker_payload["data"].get("online_available") is False
                    print(f"[get_submission_tracker_info] status={tracker_payload.get('status')} | online_available={tracker_payload['data'].get('online_available')}")

                    # 4. Call search_academic_journals on empty store (assert controlled error)
                    res_search = await session.call_tool("search_academic_journals", {"query": "Fictional Smoke Journal"})
                    search_payload = json.loads(_text(res_search))
                    assert search_payload.get("status") == "error"
                    assert search_payload.get("error_code") == "DATA_NOT_INITIALIZED"
                    print(f"[search_academic_journals - empty store] status={search_payload.get('status')} | error_code={search_payload.get('error_code')}")

                    await _recommendation_smoke(session, store_dir)

                    # 5. End-to-end import dry_run + apply + query + check in dedicated store
                    csv_path = Path(store_dir) / "FQBJCR2023.csv"
                    csv_path.write_text(
                        "刊名,ISSN,EISSN,大类名称,大类分区,小类名称,小类分区,Top,收录,OA\n"
                        "Fictional Smoke Journal,1234-5679,2049-3630,综合性期刊,1区,综合性期刊,1区,Top,SCIE,否\n",
                        encoding="utf-8-sig",
                    )
                    # Dry-run preview
                    res_dry = await session.call_tool("import_journal_data", {
                        "source_id": "showjcr",
                        "file_path": str(csv_path),
                        "data_year": 2023,
                        "dry_run": True,
                    })
                    dry_payload = json.loads(_text(res_dry))
                    assert dry_payload.get("status") == "success"
                    print(f"[import_journal_data - dry_run] status={dry_payload.get('status')}")

                    # Apply import
                    res_apply = await session.call_tool("import_journal_data", {
                        "source_id": "showjcr",
                        "file_path": str(csv_path),
                        "data_year": 2023,
                        "dry_run": False,
                    })
                    apply_payload = json.loads(_text(res_apply))
                    assert apply_payload.get("status") == "success"
                    print(f"[import_journal_data - apply] status={apply_payload.get('status')}")

                    # Search query after apply
                    res_query = await session.call_tool("search_academic_journals", {"query": "Fictional Smoke Journal"})
                    query_payload = json.loads(_text(res_query))
                    assert query_payload.get("status") == "success"
                    data_items = query_payload["data"].get("results", []) + query_payload["data"].get("provisional_results", [])
                    assert len(data_items) >= 1
                    print(f"[search_academic_journals - post import] status={query_payload.get('status')} | matches={len(data_items)}")

                    # Check warning
                    res_check = await session.call_tool("check_journal_warning", {"query": "Fictional Smoke Journal"})
                    check_payload = json.loads(_text(res_check))
                    assert check_payload.get("status") in ("success", "clean")
                    print(f"[check_journal_warning] status={check_payload.get('status')} | conclusion={check_payload['data'].get('conclusion')}")

                    print("\n[smoke test] Journal-only smoke test passed successfully.")
                    return 0

                # Default non-journal mode
                res = await session.call_tool("get_active_word_doc", {})
                print("\n[get_active_word_doc]")
                print(_text(res))

                res = await session.call_tool(
                    "scan_anti_ai_flavor",
                    {"text": "不可否认的是，本研究旨在深入探讨该问题。"},
                )
                payload = json.loads(_text(res))
                print("\n[scan_anti_ai_flavor] findings =", payload["total_findings"],
                      "| risk =", payload["ai_risk_score"])
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
