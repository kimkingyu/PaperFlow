"""Public recommendation flow stays local, stateless and model-provider neutral."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError
from paperflow.engine.journals.recommendation_models import ResearchProfile

IDEA = "I plan bearing fault diagnostics using vibration data."


def candidate():
    observed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    provenance = {"source_id": "fictional", "source_url": "https://publisher.example/fictional",
                  "authority": "official", "observed_at": observed}
    return {"title": "Fictional Recommendation Journal", "issns": ["1234-5679"], "oa_mode": "full",
            "editorial_profiles": [{"scope_summary": "Research on bearing fault diagnostics using vibration data.",
                                    "article_types": ["research"], "article_types_complete": True,
                                    "positioning": "application", "positioning_basis": "Fictional application-oriented criteria",
                                    "provenance": provenance}],
            "publication_fees": [{"route": "open_access", "amount": 100, "currency": "CNY",
                                  "taxes_included": True, "provenance": provenance}]}


def packet(service, file_path=""):
    source = {"file_path": file_path} if file_path else {"text": IDEA}
    prepared = service.prepare_manuscript(**source, mode="idea")
    profile = {"input_id": prepared["data"]["input_id"], "mode": "idea", "summary": IDEA,
               "keywords": ["bearing", "vibration"], "article_type": "research", "readiness": "planned"}
    candidates = [candidate()]
    first = service.recommend(**source, mode="idea", profile=profile, candidate_records=candidates)
    assessment = {"journal_id": first["data"]["assessment_targets"][0]["journal_id"],
                  "context_id": first["data"]["context_id"], "scope_fit": 90, "goal_fit": 80,
                  "evidence": [{"manuscript_quote": "bearing fault diagnostics", "journal_quote": "bearing fault diagnostics"}],
                  "rationale": "Matching research scope", "gaps": ["Check official indexing status"]}
    return source, profile, candidates, assessment


def test_two_phase_public_flow_does_not_create_db_or_call_network(tmp_path):
    home = tmp_path / "not-created"
    service = JournalFinder(str(home))
    with patch("socket.create_connection", side_effect=AssertionError("Network is forbidden")):
        source, profile, candidates, assessment = packet(service)
        final = service.recommend(**source, mode="idea", profile=profile,
                                  candidate_records=candidates, assessments=[assessment])
    assert final["data"]["stage"] == "scored"
    assert final["data"]["backend_calls_llm"] is False
    card = final["data"]["groups"]["efficiency"]["provisional"][0]
    assert card["score"]["value"] == 87.5
    assert card["cost"]["selected"]["estimated_total"] == 100
    assert not home.exists()


def test_prepare_exposes_a_model_neutral_contract_not_a_fake_profile(tmp_path):
    data = JournalFinder(str(tmp_path / "empty")).prepare_manuscript(text=IDEA)["data"]
    assert data["agent_contract"]["model_provider"] == "calling_agent"
    assert data["agent_contract"]["backend_calls_llm"] is False
    assert data["agent_contract"]["profile_schema"] == ResearchProfile.model_json_schema()
    assert "profile" not in data


@pytest.mark.parametrize("change", ["preferences", "candidate", "profile"])
def test_changed_request_invalidates_previous_agent_judgment(tmp_path, change):
    service = JournalFinder(str(tmp_path / "empty"))
    source, profile, candidates, assessment = packet(service)
    prefs = None
    if change == "preferences":
        prefs = {"goal": "impact"}
    elif change == "candidate":
        candidates[0]["publication_fees"][0]["amount"] = 200
    else:
        profile["summary"] = "A changed interpretation of the research"
    with pytest.raises(JournalError) as caught:
        service.recommend(**source, mode="idea", profile=profile, candidate_records=candidates,
                          assessments=[assessment], preferences=prefs)
    assert caught.value.code == "STALE_ASSESSMENT"


def test_changed_file_invalidates_previous_portrait(tmp_path):
    path = tmp_path / "draft.txt"
    path.write_text(IDEA, encoding="utf-8")
    service = JournalFinder(str(tmp_path / "empty"))
    source, profile, candidates, assessment = packet(service, str(path))
    path.write_text(IDEA + " The study changed.", encoding="utf-8")
    with pytest.raises(JournalError) as caught:
        service.recommend(**source, mode="idea", profile=profile, candidate_records=candidates, assessments=[assessment])
    assert caught.value.code == "STALE_ASSESSMENT"


@pytest.mark.parametrize("bad", [{"candidate_records": {}}, {"assessments": [{}] * 201},
                                  {"preferences": []}, {"profile": []},
                                  {"candidate_records": ["not an object"]}])
def test_bad_protocol_shapes_fail_before_evaluation(tmp_path, bad):
    with pytest.raises(JournalError):
        JournalFinder(str(tmp_path / "empty")).recommend(text=IDEA, **bad)


def test_extended_journal_facts_survive_existing_import_and_store(tmp_path):
    source = tmp_path / "candidates.json"
    source.write_text(json.dumps([candidate()]), encoding="utf-8")
    service = JournalFinder(str(tmp_path / "data"))
    service.import_data("local", str(source), kind="records", dry_run=False)
    record = service.store.records()[0]
    assert record.editorial_profiles[0].positioning == "application"
    assert record.publication_fees[0].amount == 100
    details = service.details("1234-5679")
    assert details["data"]["journal"]["editorial_profiles"]
    assert details["data"]["journal"]["publication_fees"]


def test_old_journal_payload_remains_compatible(tmp_path):
    source = tmp_path / "old.json"
    source.write_text(json.dumps([{"title": "Old Fictional", "issns": ["1234-5679"]}]), encoding="utf-8")
    service = JournalFinder(str(tmp_path / "data"))
    service.import_data("local", str(source), kind="records", dry_run=False)
    record = service.store.records()[0]
    assert record.editorial_profiles == []
    assert record.publication_fees == []
    result = service.recommend(text=IDEA)
    assert result["data"]["stage"] == "needs_agent_assessment"
    assert result["data"]["unclassified"][0]["score"] is None
