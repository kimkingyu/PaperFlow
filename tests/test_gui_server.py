"""Local GUI server: token, Host check, CSP, path rejection, inbox and model config."""
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from paperflow.engine.journal_finder import JournalFinder
from paperflow.gui import server as gui_server
from paperflow.gui.secrets import SecretStore

TOKEN = "t" * 43
PORT = 5123
IDEA = "We plan underwater robotics and embedded computing experiments."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_server, "load_page", lambda: "<!DOCTYPE html><html><body>studio</body></html>")
    finder = JournalFinder(str(tmp_path / "home"))
    store = SecretStore()
    app = gui_server.create_app(TOKEN, PORT, finder=finder, store=store)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    return client, finder, store


def post(client, path, payload, token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-PaperFlow-Token"] = token
    return client.post(path, content=json.dumps(payload), headers=headers)


def test_page_has_strict_csp_and_no_token_needed(setup):
    client, _, _ = setup
    res = client.get("/")
    assert res.status_code == 200
    csp = res.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert res.headers["x-frame-options"] == "DENY"


def test_api_requires_token(setup):
    client, _, _ = setup
    assert post(client, "/api/action", {"action": "overview"}, token=None).status_code == 403
    assert post(client, "/api/action", {"action": "overview"}, token="wrong").status_code == 403
    assert client.get("/api/inbox").status_code == 403
    assert post(client, "/api/action", {"action": "overview"}).status_code == 200


def test_foreign_host_is_rejected(setup):
    client, _, _ = setup
    res = client.post("/api/action", content=json.dumps({"action": "overview"}),
                      headers={"Content-Type": "application/json", "X-PaperFlow-Token": TOKEN,
                               "Host": "evil.example:5123"})
    assert res.status_code == 403 and res.json()["error_code"] == "FORBIDDEN_HOST"
    assert client.get("/", headers={"Host": "attacker.test"}).status_code == 403


def test_non_json_body_and_file_paths_rejected(setup):
    client, _, _ = setup
    res = client.post("/api/action", content="action=overview",
                      headers={"Content-Type": "application/x-www-form-urlencoded", "X-PaperFlow-Token": TOKEN})
    assert res.status_code == 400
    res = post(client, "/api/action", {"action": "recommend", "params": {"text": IDEA, "file_path": "C:/secret.txt"}})
    assert res.status_code == 400 and res.json()["error_code"] == "INVALID_INPUT"


def test_recommend_action_and_inbox_roundtrip(setup):
    client, finder, _ = setup
    res = post(client, "/api/action", {"action": "recommend", "params": {"text": IDEA}})
    assert res.status_code == 200 and res.json()["data"]["candidate_count"] >= 30
    published = finder.recommend(text=IDEA, publish_to_gui=True)["data"]["gui_inbox_id"]
    items = client.get("/api/inbox", headers={"X-PaperFlow-Token": TOKEN}).json()["items"]
    assert items[0]["id"] == published
    item = client.get(f"/api/inbox/{published}", headers={"X-PaperFlow-Token": TOKEN}).json()
    assert item["result"]["data"]["context_id"]
    bad = client.get("/api/inbox/..%2F..%2Fjournals", headers={"X-PaperFlow-Token": TOKEN})
    assert bad.status_code in (400, 404)


def test_model_config_validates_endpoint_and_never_echoes_key(setup):
    client, _, store = setup
    secret = "sk-test-SECRET-123456789"
    res = post(client, "/api/model/config", {"provider": "openai_compatible", "base_url": "http://api.remote.example/v1",
                                             "model": "m", "api_key": secret})
    assert res.status_code == 400  # remote http refused
    res = post(client, "/api/model/config", {"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
                                             "model": "demo-model", "api_key": secret})
    assert res.status_code == 200 and secret not in res.text
    assert res.json()["configured"] is True and res.json()["consented"] is False
    status = client.get("/api/model/status", headers={"X-PaperFlow-Token": TOKEN})
    assert secret not in status.text
    # local http endpoint is allowed (Ollama / LM Studio)
    ok = post(client, "/api/model/config", {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
                                            "model": "qwen", "api_key": ""})
    assert ok.status_code == 200


def test_model_score_requires_consent(setup):
    client, _, _ = setup
    post(client, "/api/model/config", {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
                                       "model": "qwen", "api_key": "k"})
    res = post(client, "/api/model/score", {"text": IDEA})
    assert res.status_code == 400 and res.json()["error_code"] == "CONSENT_REQUIRED"
    assert post(client, "/api/model/consent", {"consent": True}).json()["consented"] is True


def test_studio_uses_one_line_choices_and_labels_currency_conversion():
    html = (Path(__file__).parents[1] / "paperflow/gui/static/journal_studio.html").read_text(encoding="utf-8")
    assert 'id="rec-filter-cas" class="score-sort-group"' in html
    assert 'id="rec-filter-jcr" class="score-sort-group"' in html
    assert 'id="rec-filter-if" class="score-sort-group"' in html
    assert 'id="rec-filter-percent" class="score-sort-group"' in html
    assert 'id="rec-goal-choices"' in html
    assert "效率优先 · 好投在前" in html
    assert 'id="rec-pref-currency-choices"' in html
    assert 'CHF: 0.82775' in html
    assert "瑞士法郎" in html
    assert "参考汇率" in html
    assert "该刊为混合出版模式" in html
    assert "开放获取（OA）路线" in html
    assert "价格未知 ≠ 免费" in html
