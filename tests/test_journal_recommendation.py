"""Fictional journals only; exact score, privacy, provenance and route invariants."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from paperflow.engine.journals.models import (
    EditorialProfile, JournalError, JournalRecord, Metric, Provenance,
    PublicationFee, Ranking, RiskEvent, SchoolPolicy,
)
from paperflow.engine.journals.recommendation import combine_candidates, recommend_records
from paperflow.engine.journals.recommendation_models import (
    CurrencyRate, FitAssessment, RecommendationPreferences, ResearchProfile,
)

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
TEXT = "I plan bearing fault diagnostics using vibration data."
PAPER = "Our bearing fault diagnostics were validated in independent facilities. Results generalize across engineering and clinical systems."
COVERAGE = {"snapshots": [{"active": True, "coverage": {"system": "cas_warning", "year": 2026, "complete": True}}]}


def provenance(age=0, url="https://publisher.example/fictional"):
    return Provenance(source_id="fictional", source_url=url, authority="official",
                      observed_at=(NOW - timedelta(days=age)).isoformat())


def journal(position="application", issn="1234-5679", **changes):
    data = dict(title="Fictional " + position, issns=[issn], oa_mode="full", fields=["fault_diagnosis"],
                editorial_profiles=[EditorialProfile(
                    scope_summary="Original research on bearing fault diagnostics using vibration data.",
                    article_types=["research"], article_types_complete=True,
                    positioning=position, positioning_basis="Explicit fictional editorial criteria", provenance=provenance())],
                risks=[RiskEvent(system="clarivate", value="indexed", provenance=provenance(url="https://mjl.clarivate.com/fictional"))])
    data.update(changes)
    return JournalRecord(**data)


def prepared(paper=False):
    return {"input_id": "a" * 64, "text": PAPER if paper else TEXT,
            "mode": "manuscript" if paper else "idea", "truncated": False, "analysis_limits": []}


def portrait(paper=False, **updates):
    data = dict(input_id="a" * 64, mode="manuscript" if paper else "idea",
                summary="Fictional diagnostics research", keywords=["bearing", "vibration"], article_type="research",
                readiness="validated" if paper else "planned")
    if paper:
        data.update(readiness_quotes=["validated in independent facilities"], significance="cross_field",
                    significance_quotes=["generalize across engineering and clinical systems"],
                    independent_validation_quotes=["validated in independent facilities"])
    data.update(updates)
    return ResearchProfile(**data)


def evaluate(record=None, prefs=None, paper=False, assessment_updates=None, coverage=None, policy=None, profile=None):
    records = combine_candidates([], [record or journal()])
    prefs = prefs or RecommendationPreferences()
    profile = profile or portrait(paper)
    coverage = COVERAGE if coverage is None else coverage
    first = recommend_records(prepared(paper), records, profile, [], prefs, coverage, policy, NOW)
    assessment = dict(journal_id=records[0].journal_id, context_id=first["data"]["context_id"],
                      scope_fit=80, manuscript_fit=85, goal_fit=60,
                      evidence=[{"manuscript_quote": "bearing fault diagnostics", "journal_quote": "bearing fault diagnostics"}],
                      rationale="The research topic matches the supplied scope", gaps=["Confirm editorial requirements"])
    assessment.update(assessment_updates or {})
    result = recommend_records(prepared(paper), records, profile, [FitAssessment(**assessment)], prefs, coverage, policy, NOW)
    return result["data"]


def cards(result):
    return [c for g in result["groups"].values() for key in ("recommended", "provisional") for c in g[key]] + result["unclassified"]


def fee(amount=100, route="open_access", currency="CNY", age=0, **changes):
    args = dict(route=route, kind="apc", amount=amount, currency=currency,
                taxes_included=True, provenance=provenance(age))
    args.update(changes)
    return PublicationFee(**args)


def test_idea_score_is_transparent_and_not_acceptance_probability():
    result = evaluate()
    card = cards(result)[0]
    assert card["score"]["value"] == 75
    assert card["score"]["range"] == [75, 75]
    assert card["score"]["evidence_completeness"] == 100
    assert set(card["score"]["components"]) == {"scope", "goal"}
    assert result["groups"]["efficiency"]["recommended"]
    assert result["backend_calls_llm"] is False
    assert "probability" not in card["score"]


def test_unknown_score_does_not_renormalize_into_a_higher_score():
    result = evaluate(assessment_updates={"goal_fit": None})
    card = cards(result)[0]
    assert card["score"]["value"] == 60
    assert card["score"]["range"] == [60, 85]
    assert card["score"]["evidence_completeness"] == 75
    assert result["groups"]["efficiency"]["recommended"] == []


@pytest.mark.parametrize("position,group", [("application", "efficiency"), ("general", "balanced"), ("field_leading", "stretch")])
def test_positioning_is_independent_from_score_and_publisher_name(position, group):
    result = evaluate(journal(position, title="Fictional Nature-like title"))
    assert result["groups"][group]["recommended"]
    assert set(result["groups"]) == {"efficiency", "balanced", "stretch"}


def test_no_profile_or_judgment_never_fakes_semantic_assessment():
    records = combine_candidates([], [journal()])
    result = recommend_records(prepared(), records, None, [], RecommendationPreferences(), COVERAGE, as_of=NOW)["data"]
    assert result["stage"] == "needs_agent_assessment"
    assert all(c["score"] is None for c in cards(result))
    assert result["submission_order"] == []
    assert len(result["assessment_targets"]) == 1


def test_empty_store_has_honest_candidate_evidence_path():
    result = recommend_records(prepared(), [], portrait(), [], RecommendationPreferences(), {}, as_of=NOW)["data"]
    assert result["stage"] == "needs_candidate_evidence"
    assert cards(result) == []


@pytest.mark.parametrize("field,value", [("input_id", "b" * 64), ("mode", "manuscript")])
def test_stale_portrait_rejected(field, value):
    data = portrait().model_dump()
    data[field] = value
    with pytest.raises(JournalError, match="画像"):
        evaluate(profile=ResearchProfile(**data))


def test_fabricated_research_quotes_rejected():
    profile = portrait(True, significance_quotes=["unreported miracle"])
    with pytest.raises(JournalError, match="原文依据"):
        evaluate(paper=True, profile=profile)


def test_fabricated_fit_quote_and_stale_context_rejected():
    with pytest.raises(JournalError, match="当前稿件"):
        evaluate(assessment_updates={"evidence": [{"manuscript_quote": "fabricated evidence", "journal_quote": "bearing fault diagnostics"}]})
    with pytest.raises(JournalError, match="不一致"):
        evaluate(assessment_updates={"context_id": "0" * 64})


def test_known_warning_cannot_be_compensated_with_hundred_points():
    record = journal()
    record.risks.append(RiskEvent(system="cas_warning", value="flagged", year=2026))
    result = evaluate(record, assessment_updates={"scope_fit": 100, "goal_fit": 100})
    assert not cards(result)
    assert any("风险" in reason for item in result["excluded"] for reason in item["reasons"])


def test_ephemeral_facts_do_not_replace_local_warning_or_trust_caller_id():
    local = combine_candidates([], [journal()])[0]
    local.risks.append(RiskEvent(system="cas_warning", value="flagged", year=2026))
    supplied = journal(journal_id="forged-id")
    merged = combine_candidates([local], [supplied])
    assert len(merged) == 1
    assert merged[0].journal_id == local.journal_id
    assert any(r.value == "flagged" for r in merged[0].risks)
    assert supplied.journal_id == "forged-id"  # caller input was not modified


def test_homonymous_journals_do_not_silently_merge():
    a = journal()
    b = journal(issn="2049-3630")
    combined = combine_candidates([], [a, b])
    assert len(combined) == 2
    assert all(r.identity_warnings for r in combined)


def test_three_practical_tiers_only_without_elite_or_cns():
    result = evaluate()
    assert set(result["groups"].keys()) == {"efficiency", "balanced", "stretch"}
    assert "elite" not in result["groups"]


def test_old_scope_does_not_support_current_semantic_score():
    record = journal()
    record.editorial_profiles[0].provenance = provenance(age=800)
    result = evaluate(record)
    assert cards(result)[0]["score"] is None
    assert result["stage"] == "needs_candidate_evidence"
    assert result["evidence_targets"]
    assert result["unclassified"]


def test_partial_article_type_list_is_not_an_explicit_ban():
    record = journal()
    record.editorial_profiles[0].article_types = ["review"]
    record.editorial_profiles[0].article_types_complete = False
    assert cards(evaluate(record))
    record.editorial_profiles[0].article_types_complete = True
    assert not cards(evaluate(record))


def test_missing_cost_never_becomes_zero_or_fully_budget_compliant():
    prefs = RecommendationPreferences(max_budget=500, currency="CNY")
    result = evaluate(journal(publication_fees=[fee(None)]), prefs)
    card = cards(result)[0]
    assert card["cost"]["selected"]["known_charges"] is None
    assert card["cost"]["selected"]["estimated_total"] is None
    assert card["score"]["components"]["budget"]["score"] is None


def test_hybrid_optional_oa_does_not_override_subscription_quote():
    record = journal(oa_mode="hybrid", publication_fees=[fee(0, "subscription"), fee(10000, "open_access", optional=True)])
    prefs = RecommendationPreferences(max_budget=100, currency="CNY")
    card = cards(evaluate(record, prefs))[0]
    assert card["cost"]["selected"]["route"] == "subscription"
    assert card["cost"]["selected"]["estimated_total"] == 0
    assert len(card["cost"]["routes"]) == 2


def test_over_budget_cannot_be_compensated_with_hundred_points():
    record = journal(publication_fees=[fee(900)])
    result = evaluate(record, RecommendationPreferences(max_budget=500, currency="CNY"),
                      assessment_updates={"scope_fit": 100, "goal_fit": 100})
    assert not cards(result)


def test_conflicting_fee_quotes_are_not_cherry_picked():
    record = journal(publication_fees=[fee(100), fee(900)])
    result = evaluate(record, RecommendationPreferences(max_budget=500, currency="CNY"))
    assert not cards(result)


def test_stale_fees_and_missing_tax_information_remain_unknown():
    prefs = RecommendationPreferences(max_budget=500, currency="CNY")
    for quote in (fee(100, age=100), fee(100, taxes_included=None)):
        card = cards(evaluate(journal(publication_fees=[quote]), prefs))[0]
        assert card["score"]["components"]["budget"]["score"] is None


def test_currency_conversion_requires_dated_evidence_and_retains_original():
    record = journal(publication_fees=[fee(50, currency="USD")])
    prefs = RecommendationPreferences(max_budget=500, currency="CNY")
    assert cards(evaluate(record, prefs))[0]["cost"]["selected"]["estimated_total"] is None
    prefs.fx_rates = [CurrencyRate(source_currency="USD", target_currency="CNY", rate=7, provenance=provenance())]
    cost = cards(evaluate(record, prefs))[0]["cost"]
    assert cost["selected"]["estimated_total"] == 350
    assert cost["routes"][0]["quotes"][0]["currency"] == "USD"
    prefs.fx_rates[0].provenance = provenance(age=8)
    assert cards(evaluate(record, prefs))[0]["cost"]["selected"]["estimated_total"] is None


def test_mandatory_per_page_cost_requires_page_count():
    record = journal(publication_fees=[fee(0), fee(30, kind="page", unit="per_page")])
    prefs = RecommendationPreferences(max_budget=500, currency="CNY")
    assert cards(evaluate(record, prefs))[0]["cost"]["selected"]["estimated_total"] is None
    prefs.estimated_pages = 10
    assert cards(evaluate(record, prefs))[0]["cost"]["selected"]["estimated_total"] == 300


@pytest.mark.parametrize("unit,comparator,stage,age", [("", "eq", "first_decision", 0), ("days", "gt", "first_decision", 0), ("days", "eq", "acceptance", 0), ("days", "eq", "first_decision", 100)])
def test_uncalibrated_review_speed_does_not_satisfy_time_goal(unit, comparator, stage, age):
    record = journal(metrics=[Metric(name="duration", stage=stage, unit=unit, value=10, comparator=comparator, provenance=provenance(age))])
    card = cards(evaluate(record, RecommendationPreferences(max_decision_days=45)))[0]
    assert card["score"]["components"]["speed"]["score"] is None


def test_known_review_speed_and_upper_bound():
    record = journal(metrics=[Metric(name="duration", stage="first_decision", unit="weeks", lower=1, upper=2,
                                     comparator="range", provenance=provenance())])
    card = cards(evaluate(record, RecommendationPreferences(max_decision_days=45)))[0]
    assert card["timing"]["upper_days"] == 14
    assert not cards(evaluate(record, RecommendationPreferences(max_decision_days=10)))


def test_rank_filter_does_not_use_a_different_year_or_favourable_category():
    record = journal(rankings=[Ranking(system="cas", year=2025, category="Engineering", quartile=1)])
    prefs = RecommendationPreferences(filters={"rank_system": "cas", "rank_year": 2026, "quartiles": [1, 2, 3]})
    card = cards(evaluate(record, prefs))[0]
    assert card["eligibility"] == "needs_verification"
    record.rankings = [Ranking(system="cas", year=2026, category="Engineering", quartile=1),
                       Ranking(system="cas", year=2026, category="Clinical", quartile=4)]
    mixed = cards(evaluate(record, prefs))[0]
    assert mixed["eligibility"] == "needs_verification"
    assert mixed["score"]["components"]["goal"]["score"] is None
    prefs.filters.category = "Engineering"
    assert cards(evaluate(record, prefs))
    prefs.filters.category = "Clinical"
    assert not cards(evaluate(record, prefs))


def test_school_absent_indexing_is_unknown_not_an_alleged_violation():
    policy = SchoolPolicy(profile_id="fictional", name="Fictional school", source_url="https://school.example/policy", required_indexing=["SCIE"])
    card = cards(evaluate(journal(indexing=[]), policy=policy))[0]
    school = next(c for c in card["risk"]["checks"] if c["system"] == "school")
    assert school["state"] == "unknown"


def test_school_non_oa_rule_checks_article_route_not_hybrid_label():
    policy = SchoolPolicy(profile_id="fictional", name="Fictional school", source_url="https://school.example/policy", allow_oa=False)
    record = journal(oa_mode="hybrid", publication_fees=[fee(0, route="subscription")])
    assert cards(evaluate(record, policy=policy))
    assert not cards(evaluate(journal(oa_mode="diamond"), policy=policy))


def test_preference_and_score_validation_rejects_unsafe_values():
    for prefs in ({"max_budget": 10}, {"max_budget": float("nan"), "currency": "CNY"},
                  {"filters": {"top": True}}, {"filters": {"risk_policy": "include_flagged"}}):
        with pytest.raises(ValueError):
            RecommendationPreferences(**prefs)
    with pytest.raises(ValueError):
        evaluate(assessment_updates={"scope_fit": 101})


def test_explicit_oa_route_cannot_pass_school_non_oa_policy():
    policy = SchoolPolicy(profile_id="fictional", name="Fictional school", source_url="https://school.example/rules", allow_oa=False)
    record = journal(oa_mode="hybrid", publication_fees=[fee(0, "subscription"), fee(100, "open_access")])
    prefs = RecommendationPreferences(publication_route="open_access")
    assert not cards(evaluate(record, prefs, policy=policy))
    chosen = cards(evaluate(record, policy=policy))[0]
    assert chosen["cost"]["selected"]["route"] == "subscription"
    assert chosen["publication_conditions"]


def test_generic_aliases_do_not_poison_distinct_issn_identities():
    a = journal(title="Fictional Alpha", aliases=["Letters"])
    b = journal(title="Fictional Beta", issn="2049-3630", aliases=["Letters"])
    assert all(not r.identity_warnings for r in combine_candidates([], [a, b]))


def test_category_is_exact_not_a_cross_category_substring():
    record = journal(rankings=[Ranking(system="cas", year=2026, category="Engineering", quartile=1),
                               Ranking(system="cas", year=2026, category="Engineering crossover", quartile=4)])
    prefs = RecommendationPreferences(filters={"rank_system": "cas", "rank_year": 2026, "category": "Engineering", "quartiles": [1]})
    assert evaluate(record, prefs)["groups"]["efficiency"]["recommended"]


def test_original_article_and_research_article_are_compatible_labels():
    record = journal()
    record.editorial_profiles[0].article_types = ["Original Article"]
    assert evaluate(record)["groups"]["efficiency"]["recommended"]


def test_metadata_constraints_are_considered_before_candidate_limit():
    bad = journal(title="Fictional bearing vibration alpha", rankings=[Ranking(system="cas", year=2026, quartile=4)])
    good = journal(title="Fictional beta", issn="2049-3630", rankings=[Ranking(system="cas", year=2026, quartile=1)])
    prefs = RecommendationPreferences(candidate_limit=1, filters={"rank_system": "cas", "rank_year": 2026, "quartiles": [1]})
    results = recommend_records(prepared(), combine_candidates([], [bad, good]), portrait(), [], prefs, COVERAGE, as_of=NOW)["data"]
    assert results["assessment_targets"][0]["title"] == "Fictional beta"
