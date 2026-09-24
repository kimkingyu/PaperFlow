"""Tests for the integrated STEM narrative principles, reviewer deduction checks, and academic common sense guardrails."""
import json
import pytest

from paperflow.engine.anti_ai_cleaner import anti_ai_engine
from paperflow.engine.formatter import PaperFormatAuditor, IssueSeverity
from paperflow.engine.journals.models import JournalRecord, EditorialProfile, Provenance
from paperflow.engine.journals.recommendation import recommend_records
from paperflow.engine.journals.recommendation_models import (
    ResearchProfile, RecommendationPreferences,
)


def test_anti_ai_detects_textbook_theory_padding():
    sample_text = (
        "如图所示，卷积神经网络包含卷积层、池化层与全连接层，通过反向传播算法调整权重。"
        "概括而言，本文所提方法在未来的工作中我们将进一步研究，具有广阔的应用前景。"
    )
    result = anti_ai_engine.analyze(sample_text)
    matched_rules = {f.get("rule_type") for f in result["findings"]}
    assert "textbook_padding" in matched_rules
    assert "hollow_conclusion" in matched_rules
    # Ensure actionable suggestion is provided
    textbook_finding = next(f for f in result["findings"] if f.get("rule_type") == "textbook_padding")
    assert "业务背景" in textbook_finding["suggestion"]
    assert "量身定制" in textbook_finding["suggestion"]


def test_anti_ai_detects_english_textbook_theory_padding():
    sample_text = (
        "As shown in Figure 2, the convolutional neural network consists of convolutional layers and pooling layers. "
        "In summary, it is evident that our approach achieves high accuracy. Future research will focus on further optimization."
    )
    result = anti_ai_engine.analyze(sample_text)
    matched_rules = {f.get("rule_type") for f in result["findings"]}
    assert "textbook_padding" in matched_rules
    assert "hollow_conclusion" in matched_rules


def test_auditor_flags_vague_title():
    auditor = PaperFormatAuditor()
    assert auditor._check_vague_title("基于深度学习的系统研究与实现") is not None
    assert auditor._check_vague_title("Research on Bearing Fault Diagnosis Based on Deep Learning") is not None
    # Concrete mechanism / trade-off / object titles should pass without issue
    assert auditor._check_vague_title("考虑时变摩擦与热固耦合的轴承微弱故障瞬态冲击检测方法") is None


def test_auditor_flags_undefined_abbreviations():
    auditor = PaperFormatAuditor()
    text_without_def = [("正文第1段", "本文提出了一种新型网络，通过引入 CBAM 模块与 LSTM 单元提升精度。")]
    issues = auditor._check_abbreviation_definitions(text_without_def)
    flagged_msgs = " ".join(i.message for i in issues)
    assert "CBAM" in flagged_msgs and "LSTM" in flagged_msgs

    text_with_def = [("正文第1段", "本文提出结合卷积注意力模块 (Convolutional Block Attention Module, CBAM) 与长短期记忆网络 (Long Short-Term Memory, LSTM) 的方法。")]
    issues_clean = auditor._check_abbreviation_definitions(text_with_def)
    assert len(issues_clean) == 0


def test_auditor_flags_abstract_conclusion_repetition():
    auditor = PaperFormatAuditor()
    abs_text = "针对传统方法在强噪声下提取轴承故障特征不明显的问题，本文提出了一种基于多尺度残差注意力网络的解耦方法。实验结果表明，该方法在测试集上的平均准确率达到 98.5%，相较于基准模型提升了 6.2%。"
    conc_text = "针对传统方法在强噪声下提取轴承故障特征不明显的问题，本文提出了一种基于多尺度残差注意力网络的解耦方法。实验结果表明，该方法在测试集上的平均准确率达到 98.5%，相较于基准模型提升了 6.2%，具有良好效果。"
    paragraphs = [
        ("摘要", True),
        (abs_text, False),
        ("1 引言", True),
        ("轴承是关键部件...", False),
        ("5 结论", True),
        (conc_text, False),
    ]
    issues = auditor._check_abstract_conclusion_repetition(paragraphs)
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.WARNING
    assert "高度重叠" in issues[0].message


def test_recommendation_flags_small_sample_deep_learning_sanity_risk():
    prepared = {
        "input_id": "0" * 64,
        "text": "小样本表格数据只有数百条样本，直接使用 50 万参数的深度学习全连接网络与 CNN 训练分类器，准确率宣称 98%。",
        "mode": "manuscript",
        "truncated": False,
        "analysis_limits": [],
    }
    profile = ResearchProfile(
        input_id="0" * 64,
        mode="manuscript",
        summary="小样本结构化表格数据使用高参数深度神经网络分类",
        keywords=["小样本", "深度学习"],
        article_type="research",
        data_scale="small_sample",
        model_paradigm="deep_learning",
        data_openness="private_domain",
        readiness="unknown",
    )
    prefs = RecommendationPreferences()
    envelope = recommend_records(prepared, [], profile, [], prefs, {})
    res = envelope["data"]
    warnings = envelope["warnings"]

    # Check that the sanity guardrail warning is triggered
    assert any("一眼假" in w or "学术常识" in w for w in warnings)
    # Check that non-standard private domain narrative route guidance is provided
    assert any("非标/私有数据叙事路线" in g for g in res.get("narrative_guidance", []))
    assert any("Special Issue" in g for g in res.get("narrative_guidance", []))


def test_recommendation_guides_public_benchmark_narrative():
    prepared = {
        "input_id": "1" * 64,
        "text": "在 ImageNet 和 CIFAR-10 公开数据集上验证消融实验与精度。",
        "mode": "idea",
        "truncated": False,
        "analysis_limits": [],
    }
    profile = ResearchProfile(
        input_id="1" * 64,
        mode="idea",
        summary="公开 Benchmark 数据集上的神经网络结构改进与消融实验",
        keywords=["benchmark", "cifar"],
        article_type="research",
        data_openness="public_benchmark",
        readiness="planned",
    )
    prefs = RecommendationPreferences()
    res = recommend_records(prepared, [], profile, [], prefs, {})["data"]
    assert any("公开 Benchmark 叙事路线" in g for g in res.get("narrative_guidance", []))


def test_recommendation_cards_include_special_issue_tactical_tips():
    now_iso = "2026-09-24T00:00:00Z"
    record = JournalRecord(
        journal_id="j_app_01",
        title="Journal of Industrial Engineering Applications",
        issns=["1234-5679"],
        oa_mode="closed",
        editorial_profiles=[
            EditorialProfile(
                scope_summary="Industrial fault diagnosis and engineering applications.",
                positioning="application",
                positioning_basis="official scope",
                provenance=Provenance(source_url="https://example.com", authority="official", observed_at=now_iso),
            )
        ],
    )
    prepared = {
        "input_id": "2" * 64,
        "text": "Industrial fault diagnostics on rotating machinery.",
        "mode": "idea",
        "truncated": False,
        "analysis_limits": [],
    }
    profile = ResearchProfile(
        input_id="2" * 64,
        mode="idea",
        summary="Fault diagnostics on industrial machinery",
        keywords=["machinery"],
        article_type="research",
        readiness="planned",
    )
    prefs = RecommendationPreferences()
    res = recommend_records(prepared, [record], profile, [], prefs, {})["data"]
    efficiency_cards = res["groups"]["efficiency"]["provisional"]
    assert len(efficiency_cards) >= 1
    assert "special_issue_tip" in efficiency_cards[0]
    assert "Special Issue" in efficiency_cards[0]["special_issue_tip"]
