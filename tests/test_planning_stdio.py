"""Exercise the actual MCP stdio protocol with a disposable research database."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from docx import Document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PLANNING_TOOLS = {
    "prepare_research_plan", "create_research_project", "save_research_plan",
    "list_research_projects", "get_research_project", "update_research_task",
    "record_research_evidence", "export_research_plan",
}


def test_research_planning_over_real_stdio(tmp_path):
    async def workflow():
        research_home = tmp_path / "research"
        journal_home = tmp_path / "journals"
        params = StdioServerParameters(
            command=sys.executable, args=["-B", "-m", "paperflow", "run"], cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
                 "PYTHONDONTWRITEBYTECODE": "1", "PAPERFLOW_RESEARCH_HOME": str(research_home),
                 "PAPERFLOW_JOURNAL_HOME": str(journal_home)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {item.name: item for item in (await session.list_tools()).tools}
                assert PLANNING_TOOLS <= tools.keys()
                assert {"get_active_word_doc", "generate_offline_paper_docx", "recommend_journals"} <= tools.keys()
                for name in ("prepare_research_plan", "list_research_projects", "get_research_project"):
                    assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is True
                for name in PLANNING_TOOLS - {"prepare_research_plan", "list_research_projects", "get_research_project"}:
                    assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is False

                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.model_dump(by_alias=True).get("isError", False)
                    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                empty = await call("list_research_projects", {})
                assert empty["status"] == "success" and empty["data"]["total"] == 0
                prepared = await call("prepare_research_plan", {"text": "临时协议测试：需要规划实验，不是实际研究结果。"})
                assert prepared["data"]["stage"] == "needs_agent_plan"
                assert "properties" in prepared["data"]["plan_schema"]
                assert not research_home.exists()
                malformed = await session.call_tool("list_research_projects", {"limit": True})
                assert malformed.model_dump(by_alias=True).get("isError", False)
                assert not research_home.exists()

                created = await call("create_research_project", {"profile": {
                    "title": "临时 MCP 测试项目", "goal": "核对协议，不产生科学结论",
                    "resources": [{"name": "待核实的测试环境"}],
                }})
                assert created["status"] == "success"
                project_id = created["data"]["project_id"]
                assert created["data"]["revision"] == 1
                plan = created["data"]["plan"]
                assert plan["profile"]["resources"][0]["status"] == "unverified"
                plan["questions"] = [{"id": "RQ1", "question": "测试任务完成是否自动证明假设？"}]
                plan["experiments"] = [{"id": "E1", "question_id": "RQ1", "title": "协议状态检查"}]
                plan["tasks"] = [{"id": "T1", "text": "检查任务记录", "completion_condition": "留下一条测试完成记录",
                                  "experiment_id": "E1"}]
                saved = await call("save_research_plan", {
                    "project_id": project_id, "plan": plan, "expected_revision": 1,
                })
                assert saved["status"] == "success" and saved["data"]["revision"] == 2
                completed = await call("update_research_task", {
                    "project_id": project_id, "task_id": "T1", "expected_revision": 2,
                    "updates": {"status": "done", "completion_note": "协议测试已检查；这不是研究结果。"},
                })
                assert completed["data"]["revision"] == 3
                assert completed["data"]["plan"]["experiments"][0]["status"] == "planned"
                assert completed["data"]["plan"]["questions"][0]["status"] == "proposed"
                recorded = await call("record_research_evidence", {
                    "project_id": project_id, "expected_revision": 3, "experiment_ids": ["E1"],
                    "evidence": {"id": "EV1", "kind": "note", "source_ref": "https://example.invalid/test-note",
                                 "summary": "测试引用，仅保存而不访问"},
                })
                assert recorded["data"]["revision"] == 4
                assert recorded["data"]["overview"]["matrix"][0]["evidence"][0]["kind"] == "note"

                stale = await call("update_research_task", {
                    "project_id": project_id, "task_id": "T1", "expected_revision": 2,
                    "updates": {"status": "todo"},
                })
                assert stale["error_code"] == "REVISION_CONFLICT"
                unsupported = recorded["data"]["plan"]
                unsupported["questions"][0].update(status="supported", evidence_ids=["EV1"], assessment_note="缺少核验依据的测试判断")
                rejected = await call("save_research_plan", {
                    "project_id": project_id, "expected_revision": 4, "plan": unsupported,
                })
                assert rejected["error_code"] == "VALIDATION_ERROR"
                historical = await call("get_research_project", {"project_id": project_id, "revision": 2})
                assert historical["data"]["plan"]["tasks"][0]["status"] == "todo"
                assert historical["data"]["plan"]["evidence"] == []
                assert historical["data"]["latest_revision"] == 4

                destination = tmp_path / "historical-plan.docx"
                exported = await call("export_research_plan", {
                    "project_id": project_id, "revision": 2, "output_path": str(destination),
                })
                assert exported["status"] == "success" and exported["data"]["revision"] == 2
                doc = Document(destination)
                assert "研究规划，不是已完成实验的论文" in "\n".join(p.text for p in doc.paragraphs)
                assert "ADDIN ZOTERO" not in doc.element.xml
                latest = await call("get_research_project", {"project_id": project_id})
                assert latest["data"]["revision"] == 4
                assert not journal_home.exists()
    asyncio.run(asyncio.wait_for(workflow(), timeout=60))
