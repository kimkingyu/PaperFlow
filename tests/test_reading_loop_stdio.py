"""Actual MCP stdio tests for adaptive literature reading loops.

Verifies end-to-end loop over stdio using synthetic local PDF fixtures only:
Candidate B unassessed/unselected initially -> review gap with queries -> search step ->
assessment feedback (selecting B) -> download/read steps with offset union assertion ->
interpretation feedback -> draft revision feedback -> pause/resume/budget non-zero usage check ->
negative review (gap/uncertain cannot be evidence_sufficient) -> full review sufficient -> stop.
No external networks, models or Word connections.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent.parent

OFFLINE_LOOP_SERVER = textwrap.dedent("""
    import socket
    import sys
    from importlib.abc import MetaPathFinder

    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def blocked(*args, **kwargs):
        raise AssertionError("offline stdio reading loop must not contact providers, models or live Word")

    def local_socketpair_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            return original_connect(sock, address)
        return blocked()

    def local_resolution_only(host, *args, **kwargs):
        if host in ("127.0.0.1", "::1", "localhost", None):
            return original_getaddrinfo(host, *args, **kwargs)
        return blocked()

    socket.create_connection = blocked
    socket.socket.connect = local_socketpair_only
    socket.getaddrinfo = local_resolution_only

    class NoModelsOrGui(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            prefixes = ("openai", "anthropic", "google.generativeai")
            if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in prefixes):
                raise AssertionError("stdio loop must not initialize external models")
            return None

    sys.meta_path.insert(0, NoModelsOrGui())
    from paperflow.engine.word_live_bridge import live_bridge
    live_bridge.connect = blocked
    live_bridge._call = blocked
    from paperflow import __main__ as entrypoint
    sys.argv = ["paperflow", "run"]
    entrypoint.main()
""")


def _fixture_pdf(tmp_path: Path, suffix: str) -> Path:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    sentence = f"Synthetic evidence {suffix}: experimental baseline benchmark achieves significant improvement. ".encode("ascii")
    stream.set_data(b"BT /F1 12 Tf 20 760 Td (" + sentence * 30 + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": f"Synthetic protocol fixture {suffix}"})
    target = tmp_path / f"synthetic-loop-{suffix}.pdf"
    with target.open("wb") as output:
        writer.write(output)
    return target


def test_reading_loop_full_cycle_over_real_stdio(tmp_path: Path):
    pdf_a = _fixture_pdf(tmp_path, "A")
    pdf_b = _fixture_pdf(tmp_path, "B")

    async def workflow():
        paper_home = tmp_path / "papers"
        research_home = tmp_path / "research"
        journal_home = tmp_path / "journals"

        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-c", OFFLINE_LOOP_SERVER],
            cwd=str(ROOT),
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PAPERFLOW_PAPER_HOME": str(paper_home),
                "PAPERFLOW_RESEARCH_HOME": str(research_home),
                "PAPERFLOW_JOURNAL_HOME": str(journal_home),
            },
        )

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools = {item.name: item for item in (await session.list_tools()).tools}
                expected_loop_tools = {
                    "prepare_paper_writing_review",
                    "submit_paper_writing_review",
                    "step_paper_writing_loop",
                    "apply_paper_reading_feedback",
                    "control_paper_writing_loop",
                }
                assert expected_loop_tools <= tools.keys()

                async def call(name: str, arguments: dict) -> dict:
                    result = await session.call_tool(name, arguments)
                    dump = result.model_dump(by_alias=True)
                    assert not dump.get("isError", False), f"Tool {name} failed: {result}"
                    raw = "\n".join(item.text for item in result.content if hasattr(item, "text"))
                    payload = json.loads(raw)
                    return payload

                async def call_expect_success(name: str, arguments: dict) -> dict:
                    payload = await call(name, arguments)
                    assert payload.get("status") != "error", f"Tool {name} returned error: {payload.get('error_code')} - {payload.get('message')}"
                    return payload

                # 1. 导入两篇本地合成文献 A 与 B
                imp_a = await call_expect_success("import_local_paper", {"file_path": str(pdf_a), "title": "Synthetic Paper A"})
                imp_b = await call_expect_success("import_local_paper", {"file_path": str(pdf_b), "title": "Synthetic Paper B"})
                pid_a = imp_a["data"]["paper_id"]
                pid_b = imp_b["data"]["paper_id"]

                # 2. 读取文献 A 并保存解读卡
                read_a = await call_expect_success("read_academic_paper", {"paper_id": pid_a, "max_chars": 2000})
                frag_a = read_a["data"]["fragments"][0]
                sha_a = read_a["data"]["file_sha256"]
                card_a = {
                    "paper_id": pid_a,
                    "file_sha256": sha_a,
                    "summary": "这是文献A的解读卡",
                    "claims": [{
                        "section": "method",
                        "kind": "author_claim",
                        "text": "方法A提供了核心算法",
                        "evidence": [{"fragment_id": frag_a["fragment_id"], "page_number": 1, "quote": frag_a["text"][:50]}],
                    }],
                }
                await call_expect_success("save_paper_reading", {"paper_id": pid_a, "reading": card_a, "origin": "calling_agent", "strict": True})

                # 3. 创建项目：包含 A 和 B 两个候选，但初始时【B 未选用、未 assessment】
                created = await call_expect_success("create_paper_writing_project", {
                    "profile": {
                        "title": "自适应补读端到端闭环",
                        "research_question": "如何通过缺口闭环引入未选候选B并完成草稿修订？",
                        "language": "zh",
                        "sub_questions": [{"id": "RQ1", "question": "基线方法是否有实证对照？"}],
                        "own_materials": [],
                    },
                    "queries": [{"id": "Q1", "query": "baseline model", "purpose": "寻找基线文献", "question_ids": ["RQ1"]}],
                    "paper_ids": [pid_a, pid_b],
                    "source_text": "原始研究材料",
                })
                project_id = created["data"]["project_id"]
                proj_rev = created["data"]["revision"]
                assert proj_rev == 1

                candidates = {c["paper_id"]: c for c in created["data"]["candidates"]}
                # 关键联调要求：初筛仅对 A 评估并仅选用 A，B 保持未 assessment 且未选用
                initial_assessments = [
                    {"paper_id": pid_a, "metadata_sha256": candidates[pid_a]["metadata_sha256"], "relevance": "core",
                     "reason": "初始核心文献", "question_ids": ["RQ1"], "basis": "metadata", "limitations": [], "evidence_ids": []},
                ]
                assessed = await call_expect_success("assess_related_papers", {
                    "project_id": project_id,
                    "assessments": initial_assessments,
                    "expected_revision": proj_rev,
                    "selected_paper_ids": [pid_a],
                })
                proj_rev = assessed["data"]["revision"]
                assert proj_rev == 2
                assert assessed["data"]["selected_paper_ids"] == [pid_a]

                # 准备初稿草稿：仅基于文献 A 的引用
                prep_draft = await call_expect_success("prepare_paper_manuscript", {"project_id": project_id})
                cite_a = prep_draft["data"]["evidence_matrix"][0]["citation_id"]

                initial_draft = {
                    "title": "初始草稿",
                    "language": "zh",
                    "outline": [{"id": "sec-intro", "title": "引言", "level": 1, "purpose": "背景说明", "evidence_ids": [cite_a]}],
                    "sections": [{
                        "id": "sec-intro",
                        "title": "引言",
                        "level": 1,
                        "paragraphs": [{
                            "text": "文献A提出了核心方法。",
                            "kind": "literature_summary",
                            "citation_ids": [cite_a],
                            "own_material_ids": [],
                        }],
                    }],
                }
                saved_draft = await call_expect_success("save_paper_manuscript", {
                    "project_id": project_id,
                    "draft": initial_draft,
                    "expected_revision": proj_rev,
                    "change_note": "保存初稿",
                })
                proj_rev = saved_draft["data"]["revision"]
                assert proj_rev == 3

                # 4. 循环控制：start -> pause -> resume -> update_budget
                ctrl_start = await call_expect_success("control_paper_writing_loop", {
                    "project_id": project_id,
                    "action": "start",
                    "expected_loop_revision": 0,
                    "expected_project_revision": proj_rev,
                    "budget": {"max_read_papers": 6, "batch_size": 2},
                    "request": "检查是否存在基线文献缺口",
                })
                loop_rev = ctrl_start["data"]["loop_revision"]
                assert loop_rev == 1

                # 5. 首次评审：发现基线缺口 G1（literature），带检索 queries
                review_prep = await call_expect_success("prepare_paper_writing_review", {"project_id": project_id})
                fingerprint = review_prep["data"]["context_fingerprint"]
                assert len(fingerprint) == 64

                lit_review = {
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "sufficient", "reason": "文献A覆盖基本原理",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a]},
                        {"dimension": "method_baselines", "status": "gap", "reason": "缺少基线方法对比",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a]},
                        {"dimension": "contrary_findings", "status": "not_applicable", "reason": "暂无对立结论",
                         "question_ids": [], "section_ids": [], "citation_ids": []},
                        {"dimension": "draft_support", "status": "uncertain", "reason": "引言尚未包含基线支撑",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a]},
                    ],
                    "gaps": [
                        {
                            "id": "G1",
                            "category": "literature",
                            "priority": "high",
                            "question_ids": ["RQ1"],
                            "section_ids": ["sec-intro"],
                            "reason": "缺少基线文献对照",
                            "expected_information": "获取对比基线的实验数据",
                            "queries": [{"id": "Q1", "query": "baseline model", "purpose": "寻找基线文献", "question_ids": ["RQ1"]}],
                        }
                    ],
                    "decision": "continue",
                    "reason": "需要补充检索基线文献",
                    "examined_citation_ids": [cite_a],
                    "examined_section_ids": ["sec-intro"],
                }
                sub_res = await call_expect_success("submit_paper_writing_review", {
                    "project_id": project_id,
                    "review": lit_review,
                    "context_fingerprint": fingerprint,
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                loop_rev = sub_res["data"]["loop_revision"]
                proj_rev = sub_res["data"]["project_revision"]
                next_act = sub_res["data"]["loop"]["next_action"]
                assert next_act is not None
                assert next_act["kind"] == "search"

                # 6. 执行 step 搜索：发现项目中未 assessment 的候选 B，触发 awaiting_assessment
                step_search = await call_expect_success("step_paper_writing_loop", {
                    "project_id": project_id,
                    "action_id": next_act["action_id"],
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                loop_rev = step_search["data"]["loop_revision"]
                proj_rev = step_search["data"]["project_revision"]
                act_ass = step_search["data"]["loop"]["next_action"]
                assert act_ass is not None
                assert act_ass["kind"] == "assessment"

                # 7. 关键联调要求：明确提交 assessment 反馈，将 B 评估为 core 并加入 selected_paper_ids
                fb_ass = await call_expect_success("apply_paper_reading_feedback", {
                    "project_id": project_id,
                    "action_id": act_ass["action_id"],
                    "feedback": {
                        "kind": "assessment",
                        "assessments": [
                            {"paper_id": pid_b, "metadata_sha256": candidates[pid_b]["metadata_sha256"], "relevance": "core",
                             "reason": "包含所需基线模型与实验对比", "question_ids": ["RQ1"], "basis": "metadata", "limitations": [], "evidence_ids": []}
                        ],
                        "selected_paper_ids": [pid_a, pid_b],
                    },
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                loop_rev = fb_ass["data"]["loop_revision"]
                proj_rev = fb_ass["data"]["project_revision"]
                assert set(fb_ass["data"]["project"]["selected_paper_ids"]) == {pid_a, pid_b}

                # 8. 执行确定性步骤（download / read），直到进入 interpretation
                next_act = fb_ass["data"]["loop"]["next_action"]
                read_fragments = []
                while next_act and not next_act.get("requires_agent"):
                    step_res = await call_expect_success("step_paper_writing_loop", {
                        "project_id": project_id,
                        "action_id": next_act["action_id"],
                        "expected_project_revision": proj_rev,
                        "expected_loop_revision": loop_rev,
                    })
                    loop_rev = step_res["data"]["loop_revision"]
                    proj_rev = step_res["data"]["project_revision"]
                    if next_act["kind"] == "read":
                        step_mat = step_res["data"]["loop"]["next_action"].get("material", {})
                        read_fragments = step_mat.get("fragments", [])
                    next_act = step_res["data"]["loop"].get("next_action")

                assert next_act is not None and next_act["kind"] == "interpretation"
                interp_act_id = next_act["action_id"]

                # 9. 关键联调要求：断言 read 后 usage.text_chars 等于 actual pages offset 区间 union，且 usage.rounds/round_index 反映实际轮开始
                usage_after_read = step_res["data"]["loop"]["usage"]
                assert (usage_after_read["rounds"] >= 1 or step_res["data"]["loop"]["round_index"] >= 1)
                # 计算 fragments 区间 union 的实际字符数
                intervals = [(f.get("offset", 0), f.get("offset", 0) + len(f.get("text", ""))) for f in read_fragments]
                sorted_int = sorted(intervals, key=lambda x: (x[0], x[1]))
                merged = [sorted_int[0]] if sorted_int else []
                for cur_st, cur_ed in sorted_int[1:]:
                    prev_st, prev_ed = merged[-1]
                    if cur_st <= prev_ed:
                        merged[-1] = (prev_st, max(prev_ed, cur_ed))
                    else:
                        merged.append((cur_st, cur_ed))
                expected_chars = sum(ed - st for st, ed in merged)
                assert usage_after_read["text_chars"] == expected_chars
                assert usage_after_read["text_chars"] > 0

                # 10. Agent 提供针对 B 的客观解读卡反馈 (interpretation)
                frag_b = read_fragments[0]
                sha_b = next_act["payload"]["file_sha256"]
                card_b = {
                    "paper_id": pid_b,
                    "file_sha256": sha_b,
                    "summary": "依据文献B原文提取的基线实验表现",
                    "claims": [{
                        "section": "baselines_experiments",
                        "kind": "author_claim",
                        "text": "基线模型达到显著提升",
                        "evidence": [{"fragment_id": frag_b["fragment_id"], "page_number": frag_b.get("page_number", 1), "quote": frag_b["text"][:50]}],
                    }],
                }
                fb_card = await call_expect_success("apply_paper_reading_feedback", {
                    "project_id": project_id,
                    "action_id": interp_act_id,
                    "feedback": {"kind": "interpretation", "reading": card_b, "read_more": False},
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                loop_rev = fb_card["data"]["loop_revision"]
                proj_rev = fb_card["data"]["project_revision"]
                rev_act = fb_card["data"]["loop"]["next_action"]
                assert rev_act is not None and rev_act["kind"] == "revision"

                # 11. 获取矩阵中的新 citation_id 并提交草稿修订 (revision)
                prep_draft2 = await call_expect_success("prepare_paper_manuscript", {"project_id": project_id})
                cites = {ev["paper_id"]: ev["citation_id"] for ev in prep_draft2["data"]["evidence_matrix"]}
                assert pid_b in cites
                cite_b = cites[pid_b]

                revised_draft = {
                    "title": "修订后的草稿",
                    "language": "zh",
                    "outline": [{"id": "sec-intro", "title": "引言", "level": 1, "purpose": "背景与基线", "evidence_ids": [cite_a, cite_b]}],
                    "sections": [{
                        "id": "sec-intro",
                        "title": "引言",
                        "level": 1,
                        "paragraphs": [
                            {"text": "文献A提出了核心方法。", "kind": "literature_summary", "citation_ids": [cite_a], "own_material_ids": []},
                            {"text": "文献B提供了基线实验对照。", "kind": "literature_summary", "citation_ids": [cite_b], "own_material_ids": []},
                        ],
                    }],
                }
                fb_rev = await call_expect_success("apply_paper_reading_feedback", {
                    "project_id": project_id,
                    "action_id": rev_act["action_id"],
                    "feedback": {"kind": "revision", "draft": revised_draft, "change_note": "引入文献B补全基线"},
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                loop_rev = fb_rev["data"]["loop_revision"]
                proj_rev = fb_rev["data"]["project_revision"]
                assert proj_rev == 6

                # 12. 关键联调要求：验证 pause / resume / update_budget 绝不会清零已产生的 usage
                usage_before_pause = fb_rev["data"]["loop"]["usage"]
                assert usage_before_pause["text_chars"] > 0

                ctrl_pause = await call_expect_success("control_paper_writing_loop", {
                    "project_id": project_id,
                    "action": "pause",
                    "expected_loop_revision": loop_rev,
                    "expected_project_revision": proj_rev,
                })
                loop_rev = ctrl_pause["data"]["loop_revision"]
                assert ctrl_pause["data"]["loop"]["usage"]["text_chars"] == usage_before_pause["text_chars"]
                assert ctrl_pause["data"]["loop"]["usage"]["read_steps"] == usage_before_pause["read_steps"]

                ctrl_resume = await call_expect_success("control_paper_writing_loop", {
                    "project_id": project_id,
                    "action": "resume",
                    "expected_loop_revision": loop_rev,
                    "expected_project_revision": proj_rev,
                })
                loop_rev = ctrl_resume["data"]["loop_revision"]
                assert ctrl_resume["data"]["loop"]["usage"]["text_chars"] == usage_before_pause["text_chars"]

                ctrl_budget = await call_expect_success("control_paper_writing_loop", {
                    "project_id": project_id,
                    "action": "update_budget",
                    "expected_loop_revision": loop_rev,
                    "expected_project_revision": proj_rev,
                    "budget": {"max_read_papers": 10},
                })
                loop_rev = ctrl_budget["data"]["loop_revision"]
                assert ctrl_budget["data"]["loop"]["usage"]["text_chars"] == usage_before_pause["text_chars"]

                # 13. 准备复审材料与上下文指纹
                review_prep3 = await call_expect_success("prepare_paper_writing_review", {"project_id": project_id})
                fingerprint3 = review_prep3["data"]["context_fingerprint"]

                # 14. 关键联调要求：复审 dimensions 仍有 gap / uncertain 时，不能判为 evidence_sufficient
                premature_review = {
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "sufficient", "reason": "已充分",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a, cite_b]},
                        {"dimension": "method_baselines", "status": "gap", "reason": "仍缺少某些细分基线",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_b]},
                        {"dimension": "contrary_findings", "status": "not_applicable", "reason": "无",
                         "question_ids": [], "section_ids": [], "citation_ids": []},
                        {"dimension": "draft_support", "status": "uncertain", "reason": "支撑仍不确定",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a, cite_b]},
                    ],
                    "gaps": [
                        {"id": "G-remain", "category": "literature", "priority": "high", "question_ids": ["RQ1"],
                         "section_ids": ["sec-intro"], "reason": "仍有未解决缺口", "expected_information": "更多数据"}
                    ],
                    "decision": "stop",
                    "reason": "尚有缺口时过早尝试停止",
                    "examined_citation_ids": [cite_a, cite_b],
                    "examined_section_ids": ["sec-intro"],
                }
                premature_res = await call("submit_paper_writing_review", {
                    "project_id": project_id,
                    "review": premature_review,
                    "context_fingerprint": fingerprint3,
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                # 必须被拒绝（status='error'）或者不能被判定为 evidence_sufficient
                if premature_res.get("status") == "error":
                    assert premature_res.get("error_code") in ("UNRESOLVED_DIMENSION", "UNRESOLVED_CRITICAL_GAP", "INVALID_INPUT")
                else:
                    assert premature_res.get("data", {}).get("loop", {}).get("stop_reason") != "evidence_sufficient"

                # 15. 正常复审：四维度均充分，所有缺口闭环解决，成功判定 evidence_sufficient 并 stop
                final_review = {
                    "dimensions": [
                        {"dimension": "sub_questions", "status": "sufficient", "reason": "由文献A和B充分支撑",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a, cite_b]},
                        {"dimension": "method_baselines", "status": "sufficient", "reason": "已补全文献B的基线对照数据",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_b]},
                        {"dimension": "contrary_findings", "status": "not_applicable", "reason": "经核实无对立结论",
                         "question_ids": [], "section_ids": [], "citation_ids": []},
                        {"dimension": "draft_support", "status": "sufficient", "reason": "草稿引言论据与引用完全对应",
                         "question_ids": ["RQ1"], "section_ids": ["sec-intro"], "citation_ids": [cite_a, cite_b]},
                    ],
                    "gaps": [],
                    "decision": "stop",
                    "reason": "所有文献缺口已闭环解决，证据充分",
                    "examined_citation_ids": [cite_a, cite_b],
                    "examined_section_ids": ["sec-intro"],
                }
                final_res = await call_expect_success("submit_paper_writing_review", {
                    "project_id": project_id,
                    "review": final_review,
                    "context_fingerprint": fingerprint3,
                    "expected_project_revision": proj_rev,
                    "expected_loop_revision": loop_rev,
                })
                assert final_res["status"] == "success"
                assert final_res["data"]["loop"]["status"] == "stopped"
                assert final_res["data"]["loop"]["stop_reason"] == "evidence_sufficient"

    asyncio.run(asyncio.wait_for(workflow(), timeout=90))
