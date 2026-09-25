"""Tests for Journal Recommendation Interfaces (MCP and CLI).

Verifies thin interface exposure, argument forwarding, validation, error codes,
no secret leaks, and that Word live_bridge is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paperflow.cli.journal_cli import create_parser, run_journal_cli
from paperflow.engine.journals.models import JournalError
from paperflow.server.mcp_server import (
    mcp_app,
    prepare_manuscript_for_journals,
    recommend_journals,
)


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    d = tmp_path / "recommend_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# =====================================================================
# MCP Tool Registration & Docstring Tests
# =====================================================================


def test_mcp_tools_registered():
    """Verify both new tools are registered in mcp_app tools list."""
    registered_names = set()
    # FastMCP/MCPServer tool storage inspection
    if hasattr(mcp_app, "_tool_manager"):
        for t in mcp_app._tool_manager.list_tools():
            registered_names.add(getattr(t, "name", str(t)))
    elif hasattr(mcp_app, "_tools"):
        for k in mcp_app._tools.keys():
            registered_names.add(k)

    assert "prepare_manuscript_for_journals" in registered_names or hasattr(
        mcp_app, "prepare_manuscript_for_journals"
    )
    assert "recommend_journals" in registered_names or hasattr(
        mcp_app, "recommend_journals"
    )

    doc_prep = prepare_manuscript_for_journals.__doc__ or ""
    assert "使用调用方当前Agent的模型" in doc_prep
    assert "服务不自行调用LLM/不另需模型Key" in doc_prep
    assert "缺画像/适配判断返回needs_agent_assessment而非假智能评分" in doc_prep
    assert "不自动联网上传全文" in doc_prep
    assert "推荐分不是录用概率" in doc_prep

    doc_rec = recommend_journals.__doc__ or ""
    assert "使用调用方当前Agent的模型" in doc_rec
    assert "服务不自行调用LLM/不另需模型Key" in doc_rec
    assert "缺画像/适配判断返回needs_agent_assessment而非假智能评分" in doc_rec
    assert "不自动联网上传全文" in doc_rec
    assert "推荐分不是录用概率" in doc_rec


# =====================================================================
# MCP Tool Forwarding & Error Envelope Tests
# =====================================================================


def test_mcp_prepare_manuscript_forwarding():
    """Verify prepare_manuscript_for_journals forwards all arguments."""
    with patch(
        "paperflow.server.mcp_server.JournalFinder.prepare_manuscript",
        create=True,
    ) as mock_prep:
        mock_prep.return_value = {
            "status": "success",
            "data": {
                "input_id": "inp_123",
                "extracted_text": "Sample text",
                "truncated": False,
            },
            "warnings": [],
        }

        res_str = prepare_manuscript_for_journals(
            text="Deep learning research",
            file_path="",
            mode="idea",
            max_chars=5000,
        )
        res = json.loads(res_str)
        assert res["status"] == "success"
        assert res["data"]["input_id"] == "inp_123"

        mock_prep.assert_called_once_with(
            text="Deep learning research",
            file_path="",
            mode="idea",
            max_chars=5000,
        )


def test_mcp_recommend_forwarding():
    """Verify recommend_journals forwards all arguments."""
    with patch(
        "paperflow.server.mcp_server.JournalFinder.recommend",
        create=True,
    ) as mock_rec:
        mock_rec.return_value = {
            "status": "success",
            "data": {
                "recommendations": [],
                "state": "needs_agent_assessment",
            },
            "warnings": [],
        }

        test_profile = {"fields": ["Computer Science"]}
        test_assessments = [{"journal_id": "j_1", "score": 85}]
        test_candidates = [{"journal_id": "j_1", "title": "AI Journal"}]
        test_preferences = {"max_apc": 2000.0}

        res_str = recommend_journals(
            text="Neural networks study",
            file_path="",
            mode="manuscript",
            profile=test_profile,
            assessments=test_assessments,
            candidate_records=test_candidates,
            preferences=test_preferences,
        )
        res = json.loads(res_str)
        assert res["status"] == "success"
        assert res["data"]["state"] == "needs_agent_assessment"

        mock_rec.assert_called_once_with(
            text="Neural networks study",
            file_path="",
            mode="manuscript",
            profile=test_profile,
            assessments=test_assessments,
            candidate_records=test_candidates,
            preferences=test_preferences,
        )


def test_mcp_no_secret_leak_and_format_journal_error():
    """Ensure sensitive strings or raw paths are not leaked in error response."""
    secret = "sk-super-secret-key-12345"
    with patch(
        "paperflow.server.mcp_server.JournalFinder.recommend",
        create=True,
        side_effect=Exception(f"Failed with key {secret} inside backend"),
    ):
        res_str = recommend_journals(text="Testing errors")
        res = json.loads(res_str)
        assert res["status"] == "error"
        assert res["error_code"] == "INTERNAL_ERROR"
        assert secret not in res_str
        assert secret not in res["message"]


def test_never_calls_live_bridge():
    """Ensure Word live_bridge is never called by prepare or recommend tools."""
    with patch("paperflow.server.mcp_server.live_bridge") as mock_bridge:
        with patch(
            "paperflow.server.mcp_server.JournalFinder.prepare_manuscript",
            create=True,
            return_value={"status": "success", "data": {}},
        ):
            prepare_manuscript_for_journals(text="Sample text")

        with patch(
            "paperflow.server.mcp_server.JournalFinder.recommend",
            create=True,
            return_value={"status": "success", "data": {}},
        ):
            recommend_journals(text="Sample text")

        assert mock_bridge.get_document_info.call_count == 0
        assert mock_bridge.write_text.call_count == 0
        assert mock_bridge.audit_document.call_count == 0
        assert mock_bridge.normalize_document.call_count == 0


# =====================================================================
# CLI Recommend Subcommand Tests
# =====================================================================


def test_cli_parser_recommend_options():
    """Check parser configuration for journal recommend subcommand."""
    parser = create_parser()
    subparsers_action = None
    for action in parser._actions:
        if isinstance(getattr(action, "choices", None), dict):
            subparsers_action = action
            break
    assert subparsers_action is not None
    rec_parser = subparsers_action.choices.get("recommend")
    assert rec_parser is not None

    opts = {opt for act in rec_parser._actions for opt in act.option_strings}
    assert "--text" in opts
    assert "--input" in opts
    assert "--mode" in opts
    assert "--profile" in opts
    assert "--assessments" in opts
    assert "--candidates" in opts
    assert "--preferences" in opts
    assert "--prepare-only" in opts


def test_cli_recommend_mutually_exclusive_and_missing_input(temp_dir: Path, capsys):
    """Mutually exclusive check: both --text and --input or neither specified."""
    # Both specified -> exit code 2
    f = temp_dir / "draft.txt"
    f.write_text("Hello", encoding="utf-8")

    code1 = run_journal_cli([
        "recommend",
        "--text", "Hello text",
        "--input", str(f),
        "--json",
    ])
    assert code1 == 2
    out1 = json.loads(capsys.readouterr().out)
    assert out1["status"] == "error"
    assert out1["error_code"] == "INVALID_INPUT"

    # Neither specified -> exit code 2
    code2 = run_journal_cli(["recommend", "--json"])
    assert code2 == 2
    out2 = json.loads(capsys.readouterr().out)
    assert out2["status"] == "error"
    assert out2["error_code"] == "INVALID_INPUT"


def test_cli_recommend_prepare_only(capsys):
    """--prepare-only triggers prepare_manuscript and outputs result."""
    with patch(
        "paperflow.cli.journal_cli.JournalFinder.prepare_manuscript",
        create=True,
    ) as mock_prep:
        mock_prep.return_value = {
            "status": "success",
            "data": {
                "input_id": "inp_cli_1",
                "extracted_text": "Prepared content",
            },
            "warnings": [],
        }

        code = run_journal_cli([
            "recommend",
            "--text", "My research abstract",
            "--mode", "idea",
            "--prepare-only",
            "--json",
        ])
        assert code == 0
        mock_prep.assert_called_once_with(
            text="My research abstract",
            file_path="",
            mode="idea",
            max_chars=60000,
        )
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "success"
        assert out["data"]["input_id"] == "inp_cli_1"


def test_cli_recommend_json_bounded_reads_and_type_checks(temp_dir: Path, capsys):
    """Verify profile/preferences must be dict, assessments/candidates must be list-of-dicts (limit 200)."""
    # 1. Profile is array (not dict) -> exit code 2
    p_bad = temp_dir / "profile_bad.json"
    p_bad.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    code = run_journal_cli([
        "recommend",
        "--text", "My abstract",
        "--profile", str(p_bad),
        "--json",
    ])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "必须为 JSON 对象 (dict)" in out["message"]

    # 2. Preferences is array (not dict) -> exit code 2
    pref_bad = temp_dir / "pref_bad.json"
    pref_bad.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    code = run_journal_cli([
        "recommend",
        "--text", "My abstract",
        "--preferences", str(pref_bad),
        "--json",
    ])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "必须为 JSON 对象 (dict)" in out["message"]

    # 3. Assessments is dict (not list) -> exit code 2
    ass_bad = temp_dir / "ass_bad.json"
    ass_bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    code = run_journal_cli([
        "recommend",
        "--text", "My abstract",
        "--assessments", str(ass_bad),
        "--json",
    ])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "必须为 JSON 数组 (list)" in out["message"]

    # 4. Candidates list exceeding 200 items -> exit code 2
    cand_over = temp_dir / "cand_over.json"
    cand_over.write_text(json.dumps([{"id": f"j_{i}"} for i in range(201)]), encoding="utf-8")
    code = run_journal_cli([
        "recommend",
        "--text", "My abstract",
        "--candidates", str(cand_over),
        "--json",
    ])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "不能超过 200 条" in out["message"]

    # 5. Assessments element is not dict -> exit code 2
    ass_item_bad = temp_dir / "ass_item_bad.json"
    ass_item_bad.write_text(json.dumps(["string_item"]), encoding="utf-8")
    code = run_journal_cli([
        "recommend",
        "--text", "My abstract",
        "--assessments", str(ass_item_bad),
        "--json",
    ])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "每个元素必须为 JSON 对象 (dict)" in out["message"]


def test_cli_recommend_preferences_flag(temp_dir: Path, capsys):
    """--preferences loads and forwards preferences JSON."""
    pref_file = temp_dir / "pref.json"
    pref_file.write_text(json.dumps({"max_budget": 1500, "currency": "CNY"}), encoding="utf-8")

    with patch(
        "paperflow.cli.journal_cli.JournalFinder.recommend",
        create=True,
    ) as mock_rec:
        mock_rec.return_value = {
            "status": "success",
            "data": {"needs_agent_assessment": True},
            "warnings": [],
        }

        code1 = run_journal_cli([
            "recommend",
            "--text", "Paper draft",
            "--preferences", str(pref_file),
            "--json",
        ])
        assert code1 == 0
        call_pref1 = mock_rec.call_args[1]["preferences"]
        assert call_pref1["max_budget"] == 1500
        assert call_pref1["currency"] == "CNY"


def test_cli_recommend_full_arguments_forwarding(temp_dir: Path, capsys):
    """Verify all files and parameters correctly loaded and forwarded to recommend."""
    prof_file = temp_dir / "profile.json"
    prof_file.write_text(json.dumps({"topics": ["AI", "NLP"]}), encoding="utf-8")

    ass_file = temp_dir / "assessments.json"
    ass_file.write_text(json.dumps([{"journal_id": "j1", "score": 90}]), encoding="utf-8")

    cand_file = temp_dir / "candidates.json"
    cand_file.write_text(json.dumps([{"journal_id": "j1", "title": "NLP Journal"}]), encoding="utf-8")

    with patch(
        "paperflow.cli.journal_cli.JournalFinder.recommend",
        create=True,
    ) as mock_rec:
        mock_rec.return_value = {
            "status": "success",
            "data": {
                "status": "needs_agent_assessment",
                "recommendations": [],
            },
            "warnings": [],
        }

        code = run_journal_cli([
            "recommend",
            "--text", "NLP text",
            "--mode", "manuscript",
            "--profile", str(prof_file),
            "--assessments", str(ass_file),
            "--candidates", str(cand_file),
            "--json",
        ])
        assert code == 0
        mock_rec.assert_called_once_with(
            text="NLP text",
            file_path="",
            mode="manuscript",
            profile={"topics": ["AI", "NLP"]},
            assessments=[{"journal_id": "j1", "score": 90}],
            candidate_records=[{"journal_id": "j1", "title": "NLP Journal"}],
            preferences=None,
        )
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "success"


def test_cli_recommend_error_codes_mapping(capsys):
    """Verify error codes: input/validation error gives 2, source/runtime error gives 3."""
    # Clear any previous captured output
    capsys.readouterr()

    # INVALID_INPUT gives code 2
    with patch(
        "paperflow.cli.journal_cli.JournalFinder.recommend",
        create=True,
        side_effect=JournalError("INVALID_INPUT", "测试无效输入"),
    ):
        code = run_journal_cli(["recommend", "--text", "sample", "--json"])
        assert code == 2
        capsys.readouterr()

    # SOURCE_UNAVAILABLE gives code 3
    with patch(
        "paperflow.cli.journal_cli.JournalFinder.recommend",
        create=True,
        side_effect=JournalError("SOURCE_UNAVAILABLE", "数据源不可用"),
    ):
        code = run_journal_cli(["recommend", "--text", "sample", "--json"])
        assert code == 3
        capsys.readouterr()

    # Generic unexpected exception gives code 3 and no leaked raw traceback
    with patch(
        "paperflow.cli.journal_cli.JournalFinder.recommend",
        create=True,
        side_effect=RuntimeError("Secret database password db_pass_123 failed"),
    ):
        code = run_journal_cli(["recommend", "--text", "sample", "--json"])
        assert code == 3
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "db_pass_123" not in json.dumps(out)


def test_cli_recommend_real_service_empty_store_needs_candidate_evidence(temp_dir: Path, capsys):
    """Real JournalFinder call without mock on empty store:
    Returns exit code 0, status success/partial, data.stage needs_candidate_evidence, and does not write to store.
    """
    empty_db = temp_dir / "empty_journals_db"
    empty_db.mkdir(parents=True, exist_ok=True)

    code = run_journal_cli([
        "--data-dir", str(empty_db),
        "--json",
        "recommend",
        "--text", "This is a real study on distributed database query optimization and consistency protocols.",
        "--no-builtin",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in ("success", "partial")
    data = payload["data"]
    assert data["stage"] == "needs_candidate_evidence"
    assert "context_id" in data
    assert "groups" in data
    assert "efficiency" in data["groups"]
    assert "balanced" in data["groups"]
    assert "stretch" in data["groups"]
    assert set(data["groups"].keys()) == {"efficiency", "balanced", "stretch"}

    # Verify no persistent DB table / records were written to disk
    store_file = empty_db / "journals.db"
    assert not store_file.exists() or store_file.stat().st_size == 0


def test_cli_recommend_real_service_with_candidates_no_assessment_needs_agent_assessment(temp_dir: Path, capsys):
    """Real JournalFinder call without mock with candidates JSON but no assessment:
    Returns exit code 0, status success/partial, data.stage needs_agent_assessment, and no LLM called.
    """
    empty_db = temp_dir / "empty_db_for_cand"
    empty_db.mkdir(parents=True, exist_ok=True)

    candidate_file = temp_dir / "sample_candidates.json"
    candidate_file.write_text(json.dumps([
        {
            "title": "Journal of Systems Architecture",
            "issns": ["1383-7621"],
            "kind": "journal",
        }
    ]), encoding="utf-8")

    code = run_journal_cli([
        "--data-dir", str(empty_db),
        "--json",
        "recommend",
        "--text", "This is a real study on distributed database query optimization and consistency protocols.",
        "--candidates", str(candidate_file),
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in ("success", "partial")
    data = payload["data"]
    assert data["stage"] == "needs_agent_assessment"
    assert "context_id" in data
    assert "groups" in data


def test_cli_recommend_human_readable_output_no_profile_and_unknown_fee(temp_dir: Path, capsys):
    """Test human readable output formatting without --json flag:
    Verifies stage notification, group titles, provisional notices, unknown fees, and no raw JSON dump.
    """
    empty_db = temp_dir / "empty_db_hr"
    empty_db.mkdir(parents=True, exist_ok=True)

    candidate_file = temp_dir / "cand_hr.json"
    candidate_file.write_text(json.dumps([
        {
            "title": "IEEE Transactions on Neural Networks",
            "issns": ["2162-237X"],
            "kind": "journal",
            "editorial_profiles": [
                {
                    "provenance": {"source_url": "https://ieee.org/tnn", "authority": "official", "freshness": "fresh", "observed_at": "2026-01-01T00:00:00Z"},
                    "scope_summary": "Neural networks and deep learning systems.",
                    "positioning": "field_leading",
                    "positioning_basis": "official_rank",
                }
            ],
            "publication_fees": [
                {
                    "route": "open_access",
                    "amount": None,
                    "kind": "apc",
                    "unit": "per_article",
                    "currency": "USD",
                }
            ],
        }
    ]), encoding="utf-8")

    code = run_journal_cli([
        "--data-dir", str(empty_db),
        "recommend",
        "--text", "A paper investigating deep neural network convergence behaviors in vision tasks.",
        "--candidates", str(candidate_file),
    ])
    assert code == 0
    captured = capsys.readouterr().out
    assert "=== 期刊推荐结果 (阶段: needs_agent_assessment) ===" in captured
    assert "CLI 本身不包含任何大语言模型" in captured
    assert "【冲刺／领域顶刊档】" in captured
    assert "待核验梯队" in captured
    assert "[待核验]" in captured
    assert "未评估" in captured
    assert "费用: open_access 未知" in captured
    assert "0" not in captured.split("费用:")[1].split("|")[0]  # Unknown cost must not show 0


def test_cli_recommend_human_readable_assessed_and_json_intact(temp_dir: Path, capsys):
    """Test human-readable output with simulated assessments and verify --json remains raw complete envelope."""
    mock_payload = {
        "status": "success",
        "data": {
            "stage": "scored",
            "context_id": "ctx_999",
            "input_id": "inp_888",
            "groups": {
                "efficiency": {
                    "label": "效率先锋",
                    "recommended": [
                        {
                            "journal_id": "j_eff",
                            "title": "Applied Computing Letters",
                            "issns": ["1111-2222"],
                            "score": {"value": 86.5, "evidence_completeness": 90.0},
                            "rankings": [{"system": "cas", "year": 2024, "category_name": "工程技术", "quartile": 2}],
                            "cost": {"selected": {"route": "open_access", "estimated_total": 1200.0, "currency": "USD"}},
                            "risk": {"overall_risk_level": "low", "conclusion": "clean", "needs_verification": False},
                            "rationale": "Very strong alignment with the authors computational methods.",
                            "improvements": ["Add comparison with benchmark models"],
                            "missing": [],
                        }
                    ],
                    "provisional": [],
                },
                "balanced": {"label": "均衡稳妥", "recommended": [], "provisional": []},
                "stretch": {"label": "冲刺突破", "recommended": [], "provisional": []},
            },
            "excluded": [
                {"title": "Unrelated Biology Journal", "reasons": ["研究主题与征稿范围适配不足"]}
            ],
        },
        "warnings": ["推荐分不是录用概率"],
    }

    with patch("paperflow.cli.journal_cli.JournalFinder.recommend", return_value=mock_payload):
        # 1. Human readable
        code_hr = run_journal_cli(["recommend", "--text", "Sample manuscript"])
        assert code_hr == 0
        out_hr = capsys.readouterr().out
        assert "【稳妥／效率档】" in out_hr
        assert "推荐梯队 (基于所给证据)" in out_hr
        assert "Applied Computing Letters [ISSN: 1111-2222]" in out_hr
        assert "推荐分: 86.5 (证据完整度: 90%)" in out_hr
        assert "USD 1200" in out_hr
        assert "Very strong alignment" in out_hr
        assert "【已排除期刊 (1 种)】" in out_hr
        assert "Unrelated Biology Journal: 研究主题与征稿范围适配不足" in out_hr

        # 2. JSON mode remains intact and identical to mock_payload
        code_json = run_journal_cli(["recommend", "--text", "Sample manuscript", "--json"])
        assert code_json == 0
        out_json = capsys.readouterr().out
        parsed = json.loads(out_json)
        assert parsed == mock_payload


def test_cli_recommend_extended_error_codes_mapping():
    """Verify INVALID_ASSESSMENT, STALE_ASSESSMENT, INPUT_TOO_LARGE and file input errors all map to exit code 2."""
    error_codes_for_2 = [
        "INVALID_ASSESSMENT",
        "STALE_ASSESSMENT",
        "INPUT_TOO_LARGE",
        "INVALID_PATH",
        "NETWORK_PATH_REJECTED",
        "FILE_NOT_FOUND",
        "FILE_TOO_LARGE",
        "EMPTY_INPUT",
        "INVALID_ARCHIVE",
        "DANGEROUS_ARCHIVE",
        "ZIP_BOMB_DETECTED",
        "INVALID_DOCX",
        "INVALID_PDF",
        "PDF_RESOURCE_LIMIT",
        "PDF_ENCRYPTED",
        "OCR_REQUIRED",
    ]

    for err_code in error_codes_for_2:
        with patch(
            "paperflow.cli.journal_cli.JournalFinder.recommend",
            create=True,
            side_effect=JournalError(err_code, f"Error {err_code}"),
        ):
            code = run_journal_cli(["recommend", "--text", "sample", "--json"])
            assert code == 2, f"Expected code 2 for {err_code}, got {code}"


