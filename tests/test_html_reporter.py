"""Comprehensive test suite for standalone HTML reporter."""
from __future__ import annotations

import os
from datetime import datetime, timezone
import pytest

from paperflow.engine.journals.html_reporter import (
    clean_safe_url,
    render_html_report,
    write_html_report,
)
from paperflow.engine.journals.models import JournalError


def make_card(
    journal_id: str = "j-001",
    title: str = "Journal of Engineering Testing",
    issns: list = None,
    group: str = "efficiency",
    tier: str = "recommended",
    score_val: float = 85.0,
    indexing: list = None,
    cost: dict = None,
    timing: dict = None,
    risk: dict = None,
    experiences: list = None,
    missing: list = None,
    improvements: list = None,
    metrics: list = None,
    provenance: dict = None,
    metadata_observations: list = None,
    experience_count: int = None,
):
    issns = issns if issns is not None else ["1234-5678"]
    indexing = indexing if indexing is not None else ["SCIE", "EI"]
    missing = missing if missing is not None else []
    improvements = improvements if improvements is not None else ["补充最新实测工况对比数据"]

    if cost is None:
        cost = {
            "routes": [
                {
                    "route": "open_access",
                    "currency": "USD",
                    "known_charges": 2200.0,
                    "estimated_total": 2200.0,
                    "needs_verification": False,
                }
            ],
            "selected": {
                "route": "open_access",
                "currency": "USD",
                "known_charges": 2200.0,
                "estimated_total": 2200.0,
                "needs_verification": False,
                "quotes": [],
            },
        }

    if timing is None:
        timing = {
            "stage": "first_decision",
            "upper_days": 28.0,
            "needs_verification": False,
            "observations": [],
        }

    if risk is None:
        risk = {
            "conclusion": "no_known_flags_in_checked_sources",
            "checks": [],
        }

    card = {
        "journal_id": journal_id,
        "title": title,
        "issns": issns,
        "group": group,
        "eligibility": "supported_by_supplied_evidence" if tier == "recommended" else "needs_verification",
        "score": {
            "value": score_val,
            "range": [score_val, score_val + 5.0],
            "evidence_completeness": 90.0,
            "components": {
                "scope": {"score": 90.0, "weight": 45},
                "manuscript": {"score": 80.0, "weight": 20},
                "goal": {"score": 85.0, "weight": 15},
            },
        },
        "missing": missing,
        "improvements": improvements,
        "rationale": "征稿范围与故障诊断主题高度吻合",
        "indexing": indexing,
        "oa_mode": "full",
        "cost": cost,
        "timing": timing,
        "risk": risk,
        "experiences": experiences if experiences is not None else [],
        "experience_count": experience_count if experience_count is not None else (len(experiences) if experiences else 0),
        "metrics": metrics if metrics is not None else [
            {"name": "impact_factor", "value": 4.5, "year": 2025},
            {"name": "annual_articles", "value": 650, "year": 2025},
        ],
        "provenance": provenance if provenance is not None else {
            "source_id": "clarivate",
            "source_url": "https://mjl.clarivate.com/journal-search",
            "data_year": 2026,
            "authority": "official",
        },
        "metadata_observations": metadata_observations if metadata_observations is not None else [
            {"system": "Clarivate WoS", "value": "SCIE Indexed", "note": "Verified 2026"},
            {"system": "EI Compendex", "value": "Indexed", "note": "Verified 2026"},
        ],
        "editorial_evidence": [
            {
                "scope_summary": "Original research on condition monitoring and diagnostics.",
                "recent_papers": ["Fault detection in rotating machinery", "Deep transfer learning for bearing tests"],
            }
        ],
    }
    return card


def make_15_records_result():
    """Build a complete response containing 15 diverse journal cards across all tiers."""
    efficiency_rec = []
    efficiency_prov = []
    balanced_rec = []
    balanced_prov = []
    stretch_rec = []
    stretch_prov = []
    unclassified = []

    # 1-3: Efficiency Recommended & Provisional
    # Note: eff-1 is a hybrid journal with BOTH 0 APC subscription and $2490 open_access routes
    hybrid_cost = {
        "routes": [
            {"route": "subscription", "currency": "USD", "known_charges": 0.0, "estimated_total": 0.0, "needs_verification": False},
            {"route": "open_access", "currency": "USD", "known_charges": 2490.0, "estimated_total": 2490.0, "needs_verification": False},
        ],
        "selected": {"route": "open_access", "currency": "USD", "known_charges": 2490.0, "estimated_total": 2490.0, "needs_verification": False},
    }
    efficiency_rec.append(make_card("j-eff-1", "IEEE Sensors Journal", ["1530-437X"], "efficiency", "recommended", 88.0, ["SCIE", "EI"],
                                     cost=hybrid_cost,
                                     timing={"stage": "first_decision", "upper_days": 35.0}))
    efficiency_rec.append(make_card("j-eff-2", "Measurement Science and Technology", ["0957-0233"], "efficiency", "recommended", 84.0, ["SCIE"],
                                     cost={"routes": [{"route": "subscription", "currency": "GBP", "known_charges": 0.0, "estimated_total": 0.0}],
                                           "selected": {"route": "subscription", "currency": "GBP", "known_charges": 0.0, "estimated_total": 0.0, "needs_verification": False}},
                                     timing={"stage": "first_decision", "upper_days": 25.0}))
    efficiency_prov.append(make_card("j-eff-3", "Applied Acoustics", ["0003-682X"], "efficiency", "provisional", 78.0, ["SCIE", "EI"],
                                      missing=["版面费税率或附加费待核验"],
                                      cost={"routes": [{"route": "open_access", "currency": "EUR", "known_charges": 2800.0}],
                                            "selected": {"route": "open_access", "currency": "EUR", "known_charges": 2800.0, "estimated_total": None, "needs_verification": True}}))

    # 4-6: Balanced Recommended & Provisional
    balanced_rec.append(make_card("j-bal-1", "Mechanical Systems and Signal Processing", ["0888-3270"], "balanced", "recommended", 92.0, ["SCIE", "EI"],
                                   experiences=[{
                                       "field": "机械故障诊断",
                                       "topic": "轴承振动信号分析",
                                       "summary": "一审约两个月，审稿人专业度极高，重视实验基准消融。",
                                       "sample_size": 24,
                                       "url": "https://pub.example.org/experience/101",
                                       "needs_verification": False,
                                       "provenance": {"data_year": 2025, "observed_at": "2025-10-15T08:00:00Z"},
                                   }]))
    balanced_rec.append(make_card("j-bal-2", "IEEE Transactions on Instrumentation and Measurement", ["0018-9456"], "balanced", "recommended", 89.0, ["SCIE", "EI"],
                                   experiences=[{
                                       "field": "仪器测量",
                                       "topic": "微小故障特征提取",
                                       "summary": "初审速度较快，退修两次后录用。",
                                       "sample_size": None,  # Test unknown sample size
                                       "url": "https://pub.example.org/experience/102",
                                       "needs_verification": True,  # Test needs_verification badge
                                       "provenance": {"data_year": 2024, "observed_at": "2024-02-10T12:00:00Z"},
                                   }],
                                   experience_count=8))
    balanced_prov.append(make_card("j-bal-3", "Journal of Sound and Vibration", ["0022-460X"], "balanced", "provisional", 81.0, ["SCIE"],
                                    missing=["审稿周期历史数据待多源交叉验证"]))

    # 7-9: Stretch Recommended & Provisional
    stretch_rec.append(make_card("j-str-1", "IEEE Transactions on Industrial Electronics", ["0278-0046"], "stretch", "recommended", 94.0, ["SCIE", "EI"],
                                  cost={"selected": {"route": "hybrid", "currency": "USD", "known_charges": 2995.0, "estimated_total": 2995.0, "needs_verification": False}}))
    stretch_prov.append(make_card("j-str-2", "IEEE Transactions on Industrial Informatics", ["1551-3203"], "stretch", "provisional", 86.0, ["SCIE", "EI"],
                                   missing=["学校特类期刊名录核验待更新"]))
    stretch_prov.append(make_card("j-str-3", "Automatica", ["0005-1098"], "stretch", "provisional", 83.0, ["SCIE"],
                                   missing=["需要补充控制理论交叉性论证"]))

    # 10-12: Additional diverse records across groups
    efficiency_rec.append(make_card("j-eff-4", "Sensors and Actuators A: Physical", ["0924-4247"], "efficiency", "recommended", 80.0, ["SCIE"]))
    balanced_rec.append(make_card("j-bal-4", "Smart Materials and Structures", ["0964-1726"], "balanced", "recommended", 82.0, ["SCIE", "EI"]))
    stretch_prov.append(make_card("j-str-4", "Nature Communications", ["2041-1723"], "stretch", "provisional", 88.0, ["SCIE"],
                                   missing=["文章跨学科创新深度与颠覆性依据待评审"]))

    # 13-15: Unclassified and multi-indexing / free / warning records
    unclassified.append(make_card("j-unc-1", "Archive of Mechanical Engineering", ["0004-0738"], "unclassified", "provisional", 72.0, ["EI"],
                                  cost={"routes": [{"route": "diamond", "currency": "EUR", "known_charges": 0.0, "estimated_total": 0.0}],
                                        "selected": {"route": "diamond", "currency": "EUR", "known_charges": 0.0, "estimated_total": 0.0, "needs_verification": False}}))
    unclassified.append(make_card("j-unc-2", "Journal of Machinery Monitoring", ["9999-0001"], "unclassified", "provisional", 68.0, [],
                                  cost={"routes": [{"route": "unknown", "currency": "", "known_charges": None, "estimated_total": None}],
                                        "selected": {"route": "unknown", "currency": "", "known_charges": None, "estimated_total": None, "needs_verification": True}},
                                  risk={"conclusion": "needs_verification", "checks": []}))
    unclassified.append(make_card("j-unc-3", "High-Risk Diagnostic Review", ["9999-0002"], "unclassified", "provisional", 50.0, ["EI"],
                                  risk={"conclusion": "flagged", "checks": [{"system": "cas_warning", "state": "flagged"}]}))

    return {
        "status": "success",
        "data": {
            "mode": "manuscript",
            "rubric_version": "journal-fit-v1",
            "stage": "scored",
            "evaluated_at": "2026-09-24T12:00:00Z",
            "display_summary": {
                "target_minimum": 10,
                "group_capacity": 6,
                "displayed_candidates": 15,
                "supported_recommendations": 7,
                "provisional_candidates": 8,
                "assessed_candidates": 15,
                "shortfall": 0,
            },
            "profile": {
                "title": "基于多通道振动信号的滚动轴承早期微弱故障轻量化诊断",
                "summary": "针对工业台架振动信号在强噪声背景下早期故障微弱特征难以识别的问题，提出轻量化解耦网络与自适应迁移方案。",
                "keywords": ["滚动轴承", "振动分析", "特征解耦", "故障诊断"],
            },
            "groups": {
                "efficiency": {
                    "label": "稳妥／效率档",
                    "recommended": efficiency_rec,
                    "provisional": efficiency_prov,
                },
                "balanced": {
                    "label": "均衡档",
                    "recommended": balanced_rec,
                    "provisional": balanced_prov,
                },
                "stretch": {
                    "label": "冲刺／领域顶刊档",
                    "recommended": stretch_rec,
                    "provisional": stretch_prov,
                },
            },
            "unclassified": unclassified,
            "excluded": [
                {"journal_id": "j-excl-1", "title": "Predatory Open Access Journal", "reasons": ["风险核查明确拦截：中科院预警期刊 (High)", "预算超限"]},
                {"journal_id": "j-excl-2", "title": "Pure Mathematical Analysis", "reasons": ["研究主题与征稿范围完全不符"]},
            ],
            "narrative_guidance": [
                "【学术常识排雷】小样本实验强烈建议补充树模型搭配 SHAP 可解释性分析。",
                "【Special Issue 专刊狙击】优先检索目标刊近期 Special Issue 征稿主题。",
            ],
        },
        "coverage": {"snapshots": []},
        "warnings": [
            "推荐分不是录用概率；稳妥／效率是投稿策略，不是对期刊质量的保证",
            "AI 内容判断与期刊事实分开；仅基于所提供证据，不声称本次已实时核验",
        ],
    }


def test_url_cleaning_security():
    # Only http and https allowed
    assert clean_safe_url("javascript:alert(1)") is None
    assert clean_safe_url("data:text/html;base64,PHNjcmlwdD4=") is None
    assert clean_safe_url("file:///etc/passwd") is None
    assert clean_safe_url("ftp://ftp.example.com/file") is None

    # No credentials allowed
    assert clean_safe_url("https://user:pass@example.com/index") is None

    # Sensitive query parameters removed
    cleaned = clean_safe_url("https://example.com/item?token=secret123&page=2&api_key=456&topic=fault")
    assert cleaned is not None
    assert "token" not in cleaned
    assert "api_key" not in cleaned
    assert "page=2" in cleaned
    assert "topic=fault" in cleaned

    # Valid harmless URL
    assert clean_safe_url("https://ieeexplore.ieee.org/document/123456") == "https://ieeexplore.ieee.org/document/123456"


def test_render_html_report_15_records_and_structure():
    res = make_15_records_result()
    html_content = render_html_report(res)

    # Theme and headers
    assert "基于多通道振动信号的滚动轴承早期微弱故障轻量化诊断" in html_content
    assert "学术期刊智能推荐报告" in html_content or "学术期刊推荐报告" in html_content
    assert "2026-09-24T12:00:00Z" in html_content

    # Exactly 15 cards and 15 table rows rendered
    assert html_content.count('class="journal-card filterable-item"') == 15
    assert html_content.count('<tr class="filterable-item"') == 15

    # Verification and recommendation badges clearly segregated
    assert "有证据推荐" in html_content
    assert "待核验候选" in html_content
    assert "已排除期刊" in html_content

    # Check stats values from display_summary
    assert '<div class="stat-value" id="stat-count-recommended">7</div>' in html_content
    assert '<div class="stat-value" id="stat-count-provisional">8</div>' in html_content
    assert '<div class="stat-value" id="stat-count-unclassified">3</div>' in html_content
    assert '<div class="stat-value" id="stat-count-excluded">2</div>' in html_content

    # Narrative guidance and warnings
    assert "【学术常识排雷】" in html_content
    assert "【Special Issue 专刊狙击】" in html_content

    # Check disclaimer banner
    assert "非录用率保证" in html_content
    assert "稳妥／效率档" in html_content
    assert "用户策略" in html_content


def test_stable_element_ids_for_browser_automation():
    res = make_15_records_result()
    html_content = render_html_report(res)

    # Core toolbar controls
    assert 'id="search-input"' in html_content
    assert 'id="filter-indexing"' in html_content
    assert 'id="filter-group"' in html_content
    assert 'id="filter-cost"' in html_content
    assert 'id="filter-experience"' in html_content
    assert 'id="filter-risk"' in html_content
    assert 'id="btn-reset"' in html_content
    assert 'id="match-count"' in html_content
    assert 'id="total-count"' in html_content
    assert 'id="empty-search-state"' in html_content

    # View tabs and containers
    assert 'id="tab-cards"' in html_content
    assert 'id="tab-table"' in html_content
    assert 'id="cards-view"' in html_content
    assert 'id="table-view"' in html_content
    assert 'id="comparison-table"' in html_content

    # Individual cards and table rows stable IDs
    assert 'id="card-j-eff-1"' in html_content or 'id="card-j_eff_1"' in html_content
    assert 'id="row-j-eff-1"' in html_content or 'id="row-j_eff_1"' in html_content
    assert 'id="details-missing-' in html_content
    assert 'id="details-improv-' in html_content
    assert 'id="details-exp-' in html_content
    assert 'id="details-evidence-' in html_content
    assert 'id="details-metadata-' in html_content


def test_hybrid_journal_costs_matches_both_zero_and_paid_filters():
    # Journal with BOTH subscription 0 APC and open_access $2490
    card = make_card("j-hybrid", "Hybrid Optics", cost={
        "routes": [
            {"route": "subscription", "currency": "USD", "known_charges": 0.0, "estimated_total": 0.0},
            {"route": "open_access", "currency": "USD", "known_charges": 2490.0, "estimated_total": 2490.0},
        ],
        "selected": {"route": "open_access", "currency": "USD", "known_charges": 2490.0, "estimated_total": 2490.0},
    })
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {"recommended": [card], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    # The data-cost must contain both "zero" and "paid" tokens
    assert 'data-cost="zero paid"' in html_content or 'data-cost="paid zero"' in html_content
    assert "含零 APC 路线（仅免出版版面费，非总费用全免）" in html_content


def test_zero_apc_distinguished_from_unknown_cost():
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {
                    "recommended": [
                        make_card("j-zero", "Free Diamond Journal", cost={
                            "routes": [{"route": "diamond", "currency": "EUR", "known_charges": 0.0, "estimated_total": 0.0}],
                            "selected": {
                                "route": "diamond",
                                "currency": "EUR",
                                "known_charges": 0.0,
                                "estimated_total": 0.0,
                                "needs_verification": False,
                            }
                        }),
                        make_card("j-unk", "Unknown Fee Journal", cost={
                            "routes": [{"route": "unknown", "currency": "", "known_charges": None, "estimated_total": None}],
                            "selected": {
                                "route": "unknown",
                                "currency": "",
                                "known_charges": None,
                                "estimated_total": None,
                                "needs_verification": True,
                            }
                        }),
                    ],
                    "provisional": [],
                },
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    # Check 0 APC wording
    assert "含零 APC 路线（仅免出版版面费，非总费用全免" in html_content
    assert 'data-cost="zero"' in html_content

    # Check unknown fee wording
    assert "费用未知 / 待核验" in html_content
    assert 'data-cost="unknown"' in html_content


def test_multi_currency_and_native_currencies_preserved():
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {
                    "recommended": [
                        make_card("j-usd", "US Journal", cost={"selected": {"route": "open_access", "currency": "USD", "known_charges": 2500.0}}),
                        make_card("j-eur", "EU Journal", cost={"selected": {"route": "open_access", "currency": "EUR", "known_charges": 1950.0}}),
                        make_card("j-cny", "CN Journal", cost={"selected": {"route": "open_access", "currency": "CNY", "known_charges": 5000.0}}),
                        make_card("j-gbp", "UK Journal", cost={"selected": {"route": "subscription", "currency": "GBP", "known_charges": 1200.0}}),
                    ],
                    "provisional": [],
                },
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    assert "2,500.00 USD" in html_content or "2500.00 USD" in html_content
    assert "1,950.00 EUR" in html_content or "1950.00 EUR" in html_content
    assert "5,000.00 CNY" in html_content or "5000.00 CNY" in html_content
    assert "1,200.00 GBP" in html_content or "1200.00 GBP" in html_content


def test_experiences_rendering_and_empty_behavior():
    # Card with experience (including unknown sample size and needs_verification)
    card_with_exp = make_card("j-exp", "Exp Journal", experiences=[
        {
            "field": "机械工程",
            "topic": "齿轮箱故障",
            "summary": "初审约3周，两位审稿人意见非常中肯细致。",
            "sample_size": None,  # Must indicate sample size unknown
            "url": "https://pub.example.org/review?id=45",
            "needs_verification": True,
            "provenance": {"data_year": 2026},
        }
    ], experience_count=5)
    # Card with NO experiences
    card_no_exp = make_card("j-no-exp", "No Exp Journal", experiences=[])

    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {
                    "recommended": [card_with_exp, card_no_exp],
                    "provisional": [],
                },
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    # Validated experience
    assert "初审约3周，两位审稿人意见非常中肯细致。" in html_content
    assert "样本量未知" in html_content
    assert "评价待核验 (时间较旧或未知)" in html_content
    assert "共 5 条，展示 1 条" in html_content
    assert 'data-experience="has"' in html_content

    # No experience must state clearly, never fabricate
    assert "暂无公开投稿评价" in html_content
    assert 'data-experience="none"' in html_content


def test_metadata_observations_and_metrics_display():
    card = make_card(
        "j-meta", "Metadata Journal",
        metrics=[{"name": "impact_factor", "value": 7.82, "year": 2025}],
        metadata_observations=[{"system": "ESCI", "value": "Indexed", "note": "Verified 2026"}],
    )
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {"recommended": [card], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    assert "影响因子 (IF): 7.82 (2025)" in html_content
    assert "ESCI" in html_content
    assert "Indexed" in html_content


def test_shortfall_display_when_present():
    res = {
        "status": "success",
        "data": {
            "display_summary": {
                "target_minimum": 10,
                "shortfall": 3,
                "supported_recommendations": 4,
                "provisional_candidates": 3,
            },
            "groups": {
                "efficiency": {"recommended": [make_card("j-1", "J1")], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    assert 'id="stat-shortfall"' in html_content
    assert '<div class="stat-value text-warning" id="stat-count-shortfall">3</div>' in html_content
    assert "目标最少 10 本，保留真实缺口" in html_content


def test_timing_stage_clearly_not_acceptance():
    card = make_card("j-time", "Timing Journal", timing={
        "stage": "first_decision",
        "upper_days": 21.0,
    })
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {"recommended": [card], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    assert "初审周期（含编辑初筛，非外审/录用时间）" in html_content
    assert "≤ 21 天 (3.0 周)" in html_content


def test_score_meaning_and_disclaimer():
    card = make_card("j-score", "Score Journal", score_val=88.5)
    res = {
        "status": "success",
        "data": {
            "groups": {
                "efficiency": {"recommended": [card], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    assert "推荐分 <span class=\"score-disclaimer\">(非录用率)</span>" in html_content
    assert "88.5" in html_content


def test_xss_and_script_injection_prevention():
    malicious_card = make_card(
        journal_id="malicious-01",
        title="<script>alert('xss')</script><b>Injected Title</b>",
        missing=["\"><img src=x onerror=alert(1)>"],
        improvements=["</script><script>alert('pwned')</script>"],
        experiences=[
            {
                "summary": "<iframe src='evil.com'></iframe>恶意反馈",
                "sample_size": None,
                "url": "javascript:alert('xss')",  # Dangerous protocol
                "provenance": {"data_year": 2026},
            }
        ],
    )
    res = {
        "status": "success",
        "data": {
            "profile": {
                "title": "<script>alert('title-xss')</script>恶意主题",
            },
            "groups": {
                "efficiency": {"recommended": [malicious_card], "provisional": []},
                "balanced": {"recommended": [], "provisional": []},
                "stretch": {"recommended": [], "provisional": []},
            },
            "unclassified": [],
        },
    }
    html_content = render_html_report(res)

    # Plain script tags must not appear unescaped in HTML
    assert "<script>alert('xss')</script>" not in html_content
    assert "<script>alert('title-xss')</script>" not in html_content
    assert "<script>alert('pwned')</script>" not in html_content
    assert "<iframe src='evil.com'>" not in html_content
    assert "<img src=x" not in html_content
    assert 'href="javascript:' not in html_content

    # They should be safely escaped
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html_content
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in html_content


def test_empty_result_graceful_handling():
    # Empty dictionary
    html1 = render_html_report({})
    assert "<!DOCTYPE html>" in html1
    assert "暂无推荐期刊" in html1

    # Missing groups and profile
    html2 = render_html_report({"data": {}})
    assert "<!DOCTYPE html>" in html2
    assert "暂无推荐期刊" in html2


def test_write_html_report_file_operations_and_overwrite(tmp_path):
    res = make_15_records_result()
    target_file = tmp_path / "recommendation_report.html"

    # 1. Normal write
    abs_path = write_html_report(res, str(target_file), overwrite=False)
    assert os.path.isabs(abs_path)
    assert os.path.exists(abs_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "基于多通道振动信号的滚动轴承早期微弱故障轻量化诊断" in content

    # 2. Overwrite error when overwrite=False
    with pytest.raises(JournalError) as exc_info:
        write_html_report(res, str(target_file), overwrite=False)
    assert exc_info.value.code == "FILE_EXISTS"

    # 3. Overwrite success when overwrite=True
    abs_path2 = write_html_report(res, str(target_file), overwrite=True)
    assert abs_path2 == abs_path

    # 4. Invalid extension error
    bad_ext_file = tmp_path / "report.pdf"
    with pytest.raises(JournalError) as exc_info:
        write_html_report(res, str(bad_ext_file))
    assert exc_info.value.code == "INVALID_FILE_TYPE"

    # 5. Invalid empty path
    with pytest.raises(JournalError) as exc_info:
        write_html_report(res, "")
    assert exc_info.value.code == "INVALID_PATH"

    # 6. .htm extension is valid
    htm_file = tmp_path / "report_custom.HTM"
    htm_path = write_html_report(res, str(htm_file))
    assert os.path.exists(htm_path)


def test_zero_remote_assets_and_no_dom_innerhtml():
    res = make_15_records_result()
    html_content = render_html_report(res)

    # No external CDN links
    assert "cdnjs.cloudflare.com" not in html_content
    assert "cdn.jsdelivr.net" not in html_content
    assert "unpkg.com" not in html_content
    assert "fonts.googleapis.com" not in html_content

    # Front-end script must not use innerHTML
    assert ".innerHTML" not in html_content
