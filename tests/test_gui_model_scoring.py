"""GUI self-hosted model scoring, exercised through a fake transport (no network)."""
import json
from unittest.mock import patch

import pytest

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError
from paperflow.gui import model_scoring
from paperflow.gui.secrets import ModelConfig, SecretStore, redact

IDEA = "We plan underwater robotics and embedded computing experiments."
OE_SCOPE_QUOTE = "水下机器人与作业母船系统协同"
SECRET = "sk-live-VERYSECRET-0123456789"


class FakeModel:
    """Answers the profile call, then the assessment call, like a chat model would."""

    def __init__(self, fabricate=False, provider="openai_compatible"):
        self.fabricate = fabricate
        self.provider = provider
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        prompt = body["messages"][-1]["content"]
        if "研究画像" in prompt:
            content = json.dumps({"summary": IDEA, "keywords": ["underwater", "robotics"],
                                  "article_type": "research", "readiness": "planned"})
        else:
            context_id = prompt.split("context_id 必须是 ")[1].split("。")[0]
            journals = json.loads(prompt.rsplit("候选期刊：\n", 1)[1])
            oe = next(j for j in journals if j["title"] == "Ocean Engineering")
            other = next(j for j in journals if j["title"] != "Ocean Engineering")
            items = [{"journal_id": oe["journal_id"], "context_id": context_id, "scope_fit": 90, "goal_fit": 70,
                      "evidence": [{"manuscript_quote": "underwater robotics",
                                    "journal_quote": OE_SCOPE_QUOTE}],
                      "rationale": "Scope covers underwater robots"}]
            if self.fabricate:
                items.append({"journal_id": other["journal_id"], "context_id": context_id, "scope_fit": 99, "goal_fit": 99,
                              "evidence": [{"manuscript_quote": "a quantum breakthrough we never wrote",
                                            "journal_quote": "a sentence that is not in the scope"}],
                              "rationale": "fabricated"})
            content = "```json\n" + json.dumps(items, ensure_ascii=False) + "\n```"
        if "anthropic" in self.provider:
            return {"content": [{"type": "text", "text": content}]}
        return {"choices": [{"message": {"content": content}}]}


def config(provider="openai_compatible", base_url="https://api.example.com/v1", consented=True):
    cfg = ModelConfig(provider=provider, base_url=base_url, model="demo", consented=consented)
    cfg._session_key = SECRET
    return cfg


def test_scoring_marks_origin_and_rejects_fabricated_quotes(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    fake = FakeModel(fabricate=True)
    result = model_scoring.score(finder, config(), IDEA, mode="idea", transport=fake)
    data = result["data"]
    assert data["assessment_origin"] == "gui_model" and data["backend_calls_llm"] is True
    assert data["model_scoring"]["assessed"] == 1
    assert len(data["model_scoring"]["rejected_for_unsupported_quotes"]) == 1
    scored = [c for b in data["groups"].values() for k in ("recommended", "provisional") for c in b[k]
              if c["score"] is not None]
    assert [c["title"] for c in scored] == ["Ocean Engineering"]
    assert all(c["assessment_origin"] == "gui_model" for c in scored)
    assert fake.calls[0][1]["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_anthropic_wire_format(tmp_path):
    fake = FakeModel(provider="anthropic")
    model_scoring.score(JournalFinder(str(tmp_path / "home")), config("anthropic", "https://api.anthropic.com"),
                        IDEA, mode="idea", transport=fake)
    url, headers, body = fake.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == SECRET and "anthropic-version" in headers
    assert body["system"] and body["messages"][0]["role"] == "user"


def test_consent_and_endpoint_rules(tmp_path):
    finder = JournalFinder(str(tmp_path / "home"))
    with pytest.raises(JournalError) as caught:
        model_scoring.score(finder, config(consented=False), IDEA, transport=FakeModel())
    assert caught.value.code == "CONSENT_REQUIRED"
    for bad in ("http://api.remote.example/v1", "ftp://x/v1", "https://user:pw@api.example.com/v1"):
        with pytest.raises(JournalError):
            model_scoring.validate_endpoint("openai_compatible", bad, "m")
    model_scoring.validate_endpoint("openai_compatible", "http://localhost:1234/v1", "m")
    with pytest.raises(JournalError):
        model_scoring.validate_endpoint("some_vendor", "https://api.example.com", "m")


def test_model_errors_never_leak_the_key(tmp_path):
    def leaky(url, headers, body):
        raise RuntimeError(f"401 unauthorized for key {SECRET}; header Bearer {SECRET}")
    with pytest.raises(JournalError) as caught:
        model_scoring.score(JournalFinder(str(tmp_path / "home")), config(), IDEA, transport=leaky)
    assert SECRET not in str(caught.value)
    assert redact(f"api_key={SECRET}") == "api_key=***"


def test_session_key_not_persisted_without_keyring(monkeypatch, tmp_path):
    store = SecretStore()
    monkeypatch.setattr("paperflow.gui.secrets._keyring", lambda: None)
    with pytest.raises(RuntimeError):
        store.configure("openai_compatible", "https://api.example.com/v1", "m", SECRET, remember=True)
    public = store.configure("openai_compatible", "https://api.example.com/v1", "m", SECRET, remember=False)
    assert public["configured"] and not public["remembered"] and SECRET not in json.dumps(public)


def test_mcp_surface_still_never_calls_a_model(tmp_path):
    with patch.object(model_scoring, "_http_transport", side_effect=AssertionError("MCP must not call models")):
        data = JournalFinder(str(tmp_path / "home")).recommend(text=IDEA)["data"]
    assert data["backend_calls_llm"] is False
