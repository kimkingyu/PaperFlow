"""Executable browser checks for adaptive reading loop DOM, JS syntax, and state transitions."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
PAGE = ROOT / "paperflow/gui/static/journal_studio.html"


def page_script():
    text = PAGE.read_text(encoding="utf-8")
    return text, re.search(r"<script[^>]*>([\s\S]*?)</script>", text).group(1)


def script_function(script, name):
    match = re.search(r"^      (?:async )?function " + name + r"\([^\n]*", script, re.M)
    assert match, name
    following = re.search(r"\n      (?:async )?function |\n      // |\n      document\.getElementById", script[match.end():])
    assert following, name
    return script[match.start():match.end() + following.start()]


def node_run(source, args=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed; executable front-end checks unavailable")
    result = subprocess.run([node, *args], input=source, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_whole_studio_javascript_syntax_and_loop_ids():
    html, script = page_script()
    node_run(script, ("--check",))
    ids = re.findall(r'\bid="([^"\s]+)"', html)
    assert len(ids) == len(set(ids)), f"Duplicate IDs found in HTML: {[x for x in ids if ids.count(x) > 1]}"

    expected_ids = [
        "tab-writing", "panel-writing", "writing-loop-card",
        "loop-status-tag", "loop-worker-indicator", "loop-round-info", "loop-stop-reason", "loop-stop-detail",
        "loop-next-action-container", "loop-action-kind", "loop-action-id", "loop-action-reason",
        "loop-metrics-table", "loop-user-request", "loop-add-more-papers", "loop-budget-details",
        "budget-max-read-papers", "budget-batch-size", "budget-max-rounds", "budget-max-pages", "budget-max-text-chars",
        "budget-max-search-calls", "budget-max-download-attempts", "budget-max-read-steps", "budget-max-gui-model-calls",
        "loop-update-budget", "loop-btn-start", "loop-btn-step", "loop-btn-pause", "loop-btn-resume", "loop-btn-stop", "loop-btn-refresh"
    ]
    for ident in expected_ids:
        assert ident in ids, f"Expected ID {ident} missing from journal_studio.html"

    # Verify no XSS sinks in writing and reading loop block
    block = script.split("// --- Writing:", 1)[1].split("// --- Literature:", 1)[0]
    assert "innerHTML" not in block
    assert "srcdoc" not in block
    assert "insertAdjacentHTML" not in block


def test_browser_reading_loop_harness_execution():
    _, script = page_script()
    functions = "\n".join(script_function(script, name) for name in [
        "writingSetBusy", "writingRun", "writingRequireModelConsent", "writingLoopReadBudget",
        "writingLoopUpdateControls", "writingLoopRender", "writingLoopStartAuto", "writingLoopStep",
        "writingLoopControl", "writingLoopAddMorePapers",
    ])
    harness = r'''
const assert = require("assert");
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.style = {};
    this.value = ""; this._text = ""; this.disabled = false; this.checked = false; this.hidden = false;
    this.className = ""; this.classList = {add: c => {this.className += " " + c;}};
  }
  set textContent(value) {this._text = String(value); this.children = [];}
  get textContent() {return this._text + this.children.map(n => n.textContent).join("");}
  setAttribute(key, value) {this.attrs[key] = String(value); if (key === "value") this.value = String(value);}
  appendChild(node) {this.children.push(node); return node;}
}
const elements = {};
function getEl(id) {return elements[id] || (elements[id] = new Node("div"));}
const document = {getElementById:getEl};
function el(tag, attrs, children) {
  const node = new Node(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  });
  (children || []).forEach(child => node.appendChild(typeof child === "string" ? {textContent:child} : child));
  return node;
}
function clearEl(node) {node.children = []; node._text = "";}
const notices = [], fetches = [];
const showToast = (...args) => notices.push(args);
const writingLoopStartPoll = () => {}, writingLoopStopPoll = () => {};
let transportMode = "local", localToken = "offline-token", conflict = false;
const writingState = {
  project:{project_id:"test-proj-01", revision:5}, epoch:0, busy:false, stale:false,
  evidenceIds:new Set(), evidenceOffset:0, nextEvidenceOffset:null,
  loop:null, workerRunning:false, loopPollTimer:null,
};
const defaults = {
  max_read_papers:12, batch_size:3, max_rounds:4, max_pages:80, max_text_chars:180000,
  max_search_calls:24, max_download_attempts:24, max_read_steps:120, max_gui_model_calls:40,
};
const fieldIds = {
  max_read_papers:"budget-max-read-papers", batch_size:"budget-batch-size", max_rounds:"budget-max-rounds",
  max_pages:"budget-max-pages", max_text_chars:"budget-max-text-chars", max_search_calls:"budget-max-search-calls",
  max_download_attempts:"budget-max-download-attempts", max_read_steps:"budget-max-read-steps",
  max_gui_model_calls:"budget-max-gui-model-calls",
};
Object.entries(defaults).forEach(([key, value]) => {getEl(fieldIds[key]).value = String(value);});
let backendLoop = {
  loop_revision:12, status:"stopped", stop_reason:"evidence_sufficient", round_index:1,
  budget:{...defaults, max_read_papers:1, batch_size:1, max_pages:8, max_download_attempts:2},
  metrics:{usage:{read_papers:1, pages:2, text_chars:8875, rounds:1, search_calls:0,
    download_attempts:1, read_steps:2, gui_model_calls:0}, inherited_baseline:{}},
};
let running = false;
function state() {return {project_id:writingState.project.project_id, loop:JSON.parse(JSON.stringify(backendLoop)), worker_running:running};}
const fetch = async (url, options) => {
  const payload = JSON.parse(options.body);
  fetches.push({url, payload, headers:options.headers});
  if (conflict) return {ok:false, json:async () => ({status:"error", error_code:"REVISION_CONFLICT", message:"stale loop"})};
  if (url.endsWith("loop-control")) {
    if (payload.action === "update_budget") {
      backendLoop.budget = {...backendLoop.budget, ...payload.budget};
      backendLoop.request = payload.request; backendLoop.status = "awaiting_review"; backendLoop.stop_reason = null;
    } else if (payload.action === "pause") {backendLoop.status = "paused"; running = false;}
    else if (payload.action === "resume") {backendLoop.status = "awaiting_review"; running = true;}
  } else if (url.endsWith("loop-start-auto")) {running = true; backendLoop.status = "awaiting_review";}
  backendLoop.loop_revision += 1;
  return {ok:true, json:async () => ({status:"success", data:state()})};
};
__FUNCTIONS__
(async () => {
  assert.deepStrictEqual(writingLoopReadBudget(), defaults);
  getEl("budget-max-pages").value = "0";
  assert.throws(() => writingLoopReadBudget(), error => error.code === "INVALID_INPUT");
  getEl("budget-max-pages").value = String(defaults.max_pages);

  await writingRun("updating loop", async () => {
    writingLoopRender(state());
    assert.strictEqual(getEl("loop-btn-start").disabled, true);
  });
  assert.strictEqual(writingState.busy, false);
  assert.strictEqual(getEl("loop-btn-start").disabled, false);
  assert.strictEqual(getEl("loop-btn-resume").disabled, false);
  assert.strictEqual(getEl("loop-add-more-papers").disabled, false);

  const beforeBudget = {...backendLoop.budget}, beforeUsage = {...backendLoop.metrics.usage};
  getEl("budget-max-pages").value = "999";
  getEl("budget-max-download-attempts").value = "888";
  await writingLoopAddMorePapers();
  const more = fetches[fetches.length - 1];
  assert.deepStrictEqual(more.payload.budget, {max_read_papers:4});
  assert.deepStrictEqual(backendLoop.budget, {...beforeBudget, max_read_papers:4});
  assert.deepStrictEqual(backendLoop.metrics.usage, beforeUsage);
  assert.strictEqual(writingState.loop.status, "awaiting_review");
  assert.strictEqual(writingState.loop.stop_reason, null);
  assert(more.payload.request.includes("先重新评审具体缺口"));
  assert.strictEqual(getEl("loop-btn-start").disabled, false);
  assert(getEl("writing-status").textContent.includes("循环已更新"));

  const beforeConsent = fetches.length;
  await writingLoopStartAuto();
  assert.strictEqual(fetches.length, beforeConsent);
  assert(getEl("writing-status").textContent.includes("CONSENT_REQUIRED"));
  await writingLoopStep();
  assert.strictEqual(fetches.length, beforeConsent);
  assert(getEl("writing-status").textContent.includes("CONSENT_REQUIRED"));
  backendLoop.status = "paused"; writingLoopRender(state());
  await writingLoopControl("resume");
  assert.strictEqual(fetches.length, beforeConsent);
  assert(getEl("writing-status").textContent.includes("CONSENT_REQUIRED"));
  getEl("writing-model-consent").checked = true;
  transportMode = "mcp";
  await writingLoopStartAuto();
  assert.strictEqual(fetches.length, beforeConsent);
  assert(getEl("writing-status").textContent.includes("MODEL_NOT_CONFIGURED"));
  transportMode = "local";

  await writingLoopStartAuto();
  const start = fetches[fetches.length - 1];
  assert.strictEqual(start.payload.consent, true);
  assert.strictEqual(start.payload.expected_project_revision, 5);
  assert.strictEqual(start.payload.expected_loop_revision, more.payload.expected_loop_revision + 1);
  assert.strictEqual(getEl("loop-btn-start").disabled, true);
  assert.strictEqual(getEl("loop-btn-pause").disabled, false);
  getEl("writing-model-consent").checked = false;
  await writingLoopControl("pause");
  assert.strictEqual(writingState.loop.status, "paused");
  assert.strictEqual(getEl("loop-btn-resume").disabled, false);
  const beforeResume = fetches.length;
  await writingLoopControl("resume");
  assert.strictEqual(fetches.length, beforeResume);
  getEl("writing-model-consent").checked = true;
  await writingLoopControl("resume");
  assert.strictEqual(fetches[fetches.length - 1].payload.consent, true);
  assert.strictEqual(getEl("loop-btn-resume").disabled, true);

  running = false; backendLoop.status = "awaiting_review"; writingLoopRender(state());
  await writingLoopStep();
  const step = fetches[fetches.length - 1];
  assert.strictEqual(step.payload.consent, true);
  assert.strictEqual(step.payload.expected_project_revision, 5);
  assert.strictEqual(step.headers["X-PaperFlow-Token"], "offline-token");
  assert.strictEqual(getEl("loop-btn-step").disabled, false);

  conflict = true;
  getEl("budget-max-pages").value = "321";
  getEl("loop-user-request").value = "keep my unsaved requirement";
  await writingLoopControl("update_budget");
  assert.strictEqual(writingState.busy, false);
  assert.strictEqual(writingState.stale, true);
  assert.strictEqual(getEl("writing-reload").hidden, false);
  assert.strictEqual(getEl("budget-max-pages").value, "321");
  assert.strictEqual(getEl("loop-user-request").value, "keep my unsaved requirement");
  assert.strictEqual(getEl("loop-btn-start").disabled, true);
  assert.strictEqual(getEl("loop-btn-refresh").disabled, false);
  console.log("Real loop controls, consent, additive budget and conflict input recovery passed.");
})().catch(error => {console.error(error); process.exit(1);});
'''
    output = node_run(harness.replace("__FUNCTIONS__", functions))
    assert "conflict input recovery passed" in output
