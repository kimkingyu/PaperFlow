"""Independent executable GUI writing DOM, host handoff and whole-page syntax checks."""
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


def function(script, name):
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


def test_whole_studio_javascript_syntax_and_existing_ids():
    html, script = page_script()
    node_run(script, ("--check",))
    ids = re.findall(r'\bid="([^"\s]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate IDs would detach existing GUI controls"
    for ident in ["tab-overview", "tab-search", "tab-details", "tab-compare", "tab-recommend", "tab-literature", "papers-model-read",
                  "papers-query", "papers-reading-cards", "model-provider", "model-key", "rec-scoring-model", "tab-writing", "panel-writing"]:
        assert ident in ids
    block = script.split("// --- Writing:", 1)[1].split("// --- Literature:", 1)[0]
    assert "innerHTML" not in block and "srcdoc" not in block and "insertAdjacentHTML" not in block
    assert 'postMcpRequest("ui/message"' in block and '"X-PaperFlow-Token":localToken' in block
    assert 'fetch("/api/writing/download"' in block and "response.blob()" in block
    panel = html.split('id="panel-writing"', 1)[1].split("<!-- Literature:", 1)[0]
    assert 'type="file"' not in panel and "高级" in panel and "非主流程" in panel
    assert "提取 ≠ 理解" in panel and "引用校验 ≠ 语义认证" in panel


def test_offline_browser_writing_flow_xss_handoff_download_and_recovery():
    _, script = page_script()
    own_block = script.split("// --- Writing:", 1)[1].split("\n", 1)[1].split('      document.getElementById("tab-writing")', 1)[0]
    helpers = "\n".join(function(script, name) for name in ["el", "clearEl", "handleMcpToolResult", "writingLoopUpdateControls"])
    harness = r'''
const assert = require("assert");
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.style = {}; this.value = ""; this._text = ""; this.events = {}; this.disabled = false; this.checked = false; this.hidden = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  set innerHTML(value) { throw new Error("unsafe HTML sink"); }
  setAttribute(key, value) { this.attrs[key] = String(value); if (key === "value") this.value = String(value); }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
  get firstChild() { return this.children[0]; }
  get options() { return this.children; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { if (this.tagName === "a") downloads.push({href:this.href, filename:this.download}); }
}
const nodes = new Map(), downloads = [], notices = [], hostMessages = [], calls = [], fetches = [], tabs = [];
const document = { body:new Node("body"), createElement:tag => new Node(tag),
  createTextNode:text => {const node = new Node("#text"); node.textContent = text; return node;},
  getElementById:id => {if (!nodes.has(id)) nodes.set(id, new Node("div")); return nodes.get(id);} };
const showToast = (...args) => notices.push(args), switchTab = tab => tabs.push(tab), selectPaper = () => {};
const TAB_LABELS = {"tab-writing":"文献支持写作", "tab-literature":"论文检索与阅读"};
let transportMode = "local", localToken = "offline-token", badModel = false;
const writingState = {project:null, plan:null, assessmentPreview:null, draftPreview:null, queryInputs:[], candidateInputs:[], paragraphInputs:[],
  selected:new Set(), reviewedIds:new Set(), evidenceIds:new Set(), evidenceOffset:0, evidenceLimit:30, evidenceTotal:0, nextEvidenceOffset:null,
  projectsOffset:0, projectsTotal:0, epoch:0, busy:false, inputKey:"", stale:false};
const attack = '<script>alert(1)</script><img src=x onerror="alert(2)">';
const pid = "writing-" + "1".repeat(24), p1 = "paper-" + "a".repeat(24), p2 = "paper-" + "b".repeat(24), cid = "cite-" + "c".repeat(24), sha = "d".repeat(64);
const profile = {title:attack, research_question:"underwater localization?", language:"zh", context:"", sub_questions:[{id:"RQ1",question:"underwater localization?"}], keywords:[], own_materials:[{id:"UM1",text:"actual constraints"}]};
const queries = [{id:"Q1",query:"underwater localization",purpose:"compare constrained methods",question_ids:["RQ1"],sources:["crossref","arxiv"],year_from:null,year_to:null}];
const candidate = ident => ({paper_id:ident, metadata_sha256:sha, paper:{title:attack, abstract:attack, authors:[attack], year:2024}, matched_query_ids:["Q1"]});
const assess = ident => ({paper_id:ident, metadata_sha256:sha, relevance:"core", reason:attack, basis:"abstract", question_ids:["RQ1"], limitations:["metadata only"], evidence_ids:[]});
const matrix = [{citation_id:cid,paper_id:p1,reading_id:"reading-fixture",file_sha256:sha,section:"method",kind:"author_claim",text:attack,
  evidence:[{fragment_id:"frag-fixture",page_number:1,quote:attack}]},
  {citation_id:"cite-unverified",paper_id:p1,file_sha256:sha,section:"results",kind:"unverified",text:attack,evidence:[]}];
const draft = {title:attack,language:"zh",outline:[{id:"related_work",title:attack,level:1,purpose:attack,evidence_ids:[cid]}],
  sections:[{id:"related_work",title:attack,level:1,paragraphs:[{text:attack,kind:"literature_summary",citation_ids:[cid],own_material_ids:[]}]}]};
let backend = {project_id:pid,revision:1,profile,queries,candidates:[],assessments:[],selected_paper_ids:[],draft:null,export_ready:false,
  stage:"needs_agent_assessment",search_statuses:[],evidence_matrix:[],gaps:[],agent_contract:{}};
function result() {const data = JSON.parse(JSON.stringify(backend)); data.evidence_offset = 0; data.evidence_limit = 30; data.evidence_total = data.evidence_matrix.length; data.evidence_next_offset = null; return {status:"success",data,coverage:{backend_calls_llm:false},warnings:[]};}
const window = {call:async (action, params) => {
  calls.push({action,params:JSON.parse(JSON.stringify(params))});
  if (action === "writing_prepare") return {status:"success",data:{input_id:"input-fixture",profile_schema:{},query_schema:{},text:params.text}};
  if (action === "writing_list") return {status:"success",data:{projects:[{project_id:pid,title:attack,revision:backend.revision,stage:backend.stage}],total:1}};
  if (action === "writing_get" || action === "writing_materials") return result();
  if (action === "writing_create") {backend.profile=params.profile; backend.queries=params.queries; return result();}
  if (params.expected_revision !== backend.revision) {const error = new Error("stale revision"); error.code="REVISION_CONFLICT"; throw error;}
  if (action === "writing_search") {backend.revision++;backend.candidates=[candidate(p1),candidate(p2)];backend.search_statuses=[{query_id:"Q1",source_id:"arxiv",status:"error",error_code:attack}];return result();}
  if (action === "writing_assess") {backend.revision++;backend.assessments=params.assessments;backend.selected_paper_ids=params.selected_paper_ids;backend.evidence_matrix=matrix;return result();}
  if (action === "writing_draft") {backend.revision++;backend.draft=params.draft;backend.export_ready=true;backend.stage="draft_ready";return result();}
  throw new Error(action);
}};
const fetch = async (url, options) => {
  const payload = JSON.parse(options.body); fetches.push({url,headers:options.headers,payload});
  if (badModel) return {ok:false,json:async () => ({status:"error",error_code:"MODEL_ERROR",message:"empty model output"})};
  if (url === "/api/writing/download") return {ok:true,blob:async () => ({kind:"docx-bytes"})};
  let data;
  if (url.endsWith("writing-plan")) data = {profile,queries,source_text:payload.text+"\n\n"+payload.own_materials.join("\n\n"),input_id:"input-fixture",preview_only:true};
  if (url.endsWith("writing-assess")) data = {project_id:pid,revision:backend.revision,assessments:payload.candidate_ids.map(assess),preview_only:true};
  if (url.endsWith("writing-draft")) data = {project_id:pid,revision:backend.revision,draft,preview_only:true};
  return {ok:true,json:async () => ({status:"success",data,coverage:{model_calls:1,semantic_correctness_verified:false},warnings:["only selected data"]})};
};
const postMcpRequest = async (method, params) => {hostMessages.push({method,params});return {};};
const objectUrls = []; URL.createObjectURL = blob => {objectUrls.push(blob);return "blob:offline-fixture";}; URL.revokeObjectURL = url => objectUrls.push(url);
__HELPERS__
__WRITING__
function all(node) {return [node,...node.children.flatMap(all)];}
function assertSafe(id) {assert(!all(document.getElementById(id)).some(node => ["script","img","iframe"].includes(node.tagName)));}
(async () => {
  handleMcpToolResult({structuredContent:{data:{studio:"ui://paperflow/studio",tab:"writing"}}});assert(tabs.includes("tab-writing"));
  handleMcpToolResult({structuredContent:{data:{studio:"ui://paperflow/studio",tab:"bad-tab"}}});assert(!tabs.includes("tab-bad-tab"));
  document.getElementById("writing-question").value=profile.research_question;
  document.getElementById("writing-own-materials").value="actual constraints";
  document.getElementById("writing-language").value="zh";
  await writingPlan(true);assert.strictEqual(fetches.length,0);assert.strictEqual(writingState.plan,null);
  await writingPlan(false);assert.strictEqual(fetches.length,0);assert(calls.find(call => call.action === "writing_prepare").params.text.includes("actual constraints"));
  document.getElementById("writing-model-consent").checked=true;
  await writingPlan(true);assert.strictEqual(writingState.project,null);assert.strictEqual(writingState.queryInputs.length,1);
  assert.deepStrictEqual(Object.keys(fetches[0].payload).sort(),["text","own_materials","language","consent"].sort());
  await writingCreateProject();assert.strictEqual(writingState.project.project_id,pid);
  const create = calls.find(call => call.action === "writing_create");assert.strictEqual(create.params.source_text,"underwater localization?\n\nactual constraints");
  await writingSearch();assert.strictEqual(writingState.project.revision,2);assert(document.getElementById("writing-source-statuses").textContent.includes("error"));
  assertSafe("writing-source-statuses");assertSafe("writing-candidates");assert(document.getElementById("writing-candidates").textContent.includes(attack));
  await writingAssess(true);assert.strictEqual(backend.assessments.length,0);assert.strictEqual(writingState.selected.size,2);
  writingState.candidateInputs[1].relevance.value="irrelevant";writingState.candidateInputs[1].reason.value="different domain";writingState.selected.delete(p2);
  await writingAssess(false);assert.strictEqual(writingState.project.revision,3);assert.deepStrictEqual(backend.selected_paper_ids,[p1]);
  await writingMaterials(0);assert.deepStrictEqual(Array.from(writingState.evidenceIds),[cid]);assertSafe("writing-evidence");
  assert(document.getElementById("writing-evidence-page").textContent.includes("总量 2"));
  await writingDraft(true);assert.strictEqual(backend.draft,null);assert.strictEqual(writingState.draftPreview.sections[0].paragraphs[0].text,attack);assertSafe("writing-draft");
  badModel=true;const oldPreview=writingState.draftPreview;await writingDraft(true);assert.strictEqual(writingState.draftPreview,oldPreview);assert(document.getElementById("writing-status").textContent.includes("MODEL_ERROR"));badModel=false;
  writingState.paragraphInputs[0].input.value="edited " + attack;
  await writingDraft(false);assert.strictEqual(backend.revision,4);assert.strictEqual(backend.draft.sections[0].paragraphs[0].text,"edited " + attack);
  await writingDownload();assert.strictEqual(downloads.length,1);assert.strictEqual(downloads[0].filename,"paperflow-writing-r4.docx");
  const download=fetches.find(call => call.url.endsWith("/download"));assert.deepStrictEqual(download.payload,{project_id:pid,revision:4});assert.strictEqual(download.headers["X-PaperFlow-Token"],"offline-token");
  assert.strictEqual(objectUrls[0].kind,"docx-bytes");assert.strictEqual(objectUrls[1],"blob:offline-fixture");
  await writingHandoff("draft");assert.strictEqual(hostMessages.length,0);assert(document.getElementById("writing-agent-status").textContent.includes("普通浏览器"));
  transportMode="mcp";await writingHandoff("draft");assert.strictEqual(hostMessages.length,1);assert.strictEqual(hostMessages[0].method,"ui/message");
  const task=JSON.parse(hostMessages[0].params.content.text);assert.strictEqual(task.project_id,pid);assert.deepStrictEqual(task.citation_ids,[cid]);assert.strictEqual(task.stage,"draft");assert(!("file_path" in task));
  await writingDownload();assert.strictEqual(downloads.length,1);assert.strictEqual(JSON.parse(hostMessages[1].params.content.text).stage,"export");
  assert(document.getElementById("writing-agent-task").value.includes("嵌入页不能下载 bytes"));
  const before=writingState.project.revision;
  handleMcpToolResult({structuredContent:{data:{snapshot:{...result().data,revision:before+1},project_id:"writing-"+"f".repeat(24)}}});assert.strictEqual(writingState.project.revision,before);
  handleMcpToolResult({structuredContent:{data:{snapshot:{...result().data,revision:before+1},project_id:pid}}});assert.strictEqual(writingState.project.revision,before+1);assert(tabs.includes("tab-writing"));
  transportMode="local";await writingSearch();assert.strictEqual(writingState.stale,true);assert.strictEqual(document.getElementById("writing-reload").hidden,false);assert.strictEqual(writingState.project.revision,before+1);
  await writingLoadProject(pid);assert.strictEqual(writingState.stale,false);assert.strictEqual(writingState.project.revision,backend.revision);
  const normalCall=window.call;let resolveSearch;
  window.call = (action,params) => action === "writing_search" ? new Promise(resolve => {resolveSearch=resolve;}) : normalCall(action,params);
  const pending=writingSearch();document.getElementById("writing-question").value="a completely different topic";writingReset(false);resolveSearch(result());await pending;
  assert.strictEqual(writingState.project,null);assert.strictEqual(writingState.draftPreview,null);assert.strictEqual(writingState.selected.size,0);assert.strictEqual(document.getElementById("writing-draft").children.length,0);
  assert.strictEqual(document.getElementById("writing-question").value,"a completely different topic");
  window.call=normalCall;
  console.log("writing workflow, safe text, explicit payloads, bytes download, ui/message, snapshots, stale recovery and topic isolation passed");
})().catch(error => {console.error(error);process.exit(1);});
'''
    output = node_run(harness.replace("__HELPERS__", helpers).replace("__WRITING__", own_block))
    assert "topic isolation passed" in output
