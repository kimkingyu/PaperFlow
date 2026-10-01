import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorText, post, records, streamEvents } from '../api';
import { useWorkbench } from '../context';
import type { Data, Run, RunEvent, Session } from '../types';
import { ResultView } from '../components/Results';
import { Badge, Button, Card, Check, Empty, Field, Icon, Notice, Status } from '../components/ui';
import { McpGuide } from './Settings';
const activeStatuses = ['queued','running','awaiting_approval','paused','interrupted'];
const allowedEvents = new Set(['status','usage','model_request','assistant_message','tool_started','tool_result','tool_reused','approval_required','approval','artifact']);
export function publicMessage(message: Data): string {
  const text = typeof message.text === 'string' ? message.text : typeof message.content === 'string' ? message.content : '';
  return message.role === 'assistant' ? text.replace(/<(think|analysis|reasoning)>[\s\S]*?<\/\1>/gi,'').trim() : text;
}
export default function Agent() {
  const {workspace,mode,agentPrompt,clearAgentPrompt,refreshWorkspaces,publish} = useWorkbench();
  const [sessions,setSessions] = useState<Session[]>([]);const [session,setSession] = useState<Session>();const [run,setRun] = useState<Run>();const [events,setEvents] = useState<RunEvent[]>([]);
  const [message,setMessage] = useState('');const [title,setTitle] = useState('');const [error,setError] = useState('');const [busy,setBusy] = useState(false);const [network,setNetwork] = useState(false);const [writes,setWrites] = useState(false);const [consent,setConsent] = useState(false);const [budget,setBudget] = useState({model_calls:40,tool_calls:80,seconds:1800});
  const [fallback,setFallback] = useState(false);const [reconnect,setReconnect] = useState(0);const [settling,setSettling] = useState(false);const [model,setModel] = useState<Data>();
  const cursors = useRef<Record<string,number>>({});const cache = useRef<Record<string,RunEvent[]>>({});const transcriptEnd = useRef<HTMLDivElement>(null);
  const list = useCallback(async () => {if(!workspace || mode === 'mcp')return;try{const found = records(await api('/api/agent/sessions?workspace_id=' + encodeURIComponent(workspace.id))) as Session[];setSessions(found);const wanted = new URLSearchParams(location.hash.split('?')[1] || '').get('session');if(wanted && found.some(item => item.id === wanted))await open(wanted);}catch(e){setError(errorText(e));}},[workspace?.id,mode]);
  useEffect(() => {void list();},[list]);
  useEffect(() => {setSession(undefined);setRun(undefined);setEvents([]);setConsent(false);setNetwork(false);setWrites(false);setError('');},[workspace?.id,mode]);
  useEffect(() => {if(agentPrompt){setMessage(agentPrompt);clearAgentPrompt();}},[agentPrompt,clearAgentPrompt]);
  useEffect(() => {if(mode === 'standalone')void api('/api/model/status').then(setModel).catch(e => setError(errorText(e)));},[mode]);
  async function open(id:string) {setError('');try {const data = await api<Session>('/api/agent/sessions/' + encodeURIComponent(id));setSession(data);setRun(data.runs?.at(-1));}catch(e){setError(errorText(e));}}
  async function create(): Promise<Session | undefined> {if(!workspace)return;try {const data = await post<Session>('/api/agent/sessions',{workspace_id:workspace.id,title:title.trim() || '新研究会话'});setSession(data);setRun(undefined);setTitle('');await list();return data;}catch(e){setError(errorText(e));}}
  async function send() {if(!message.trim() || !workspace || !consent)return;setBusy(true);setError('');try {
    const target = session || await create();if(!target)return;
    const request_id = crypto.randomUUID();
    const data = await post<Run>(`/api/agent/sessions/${encodeURIComponent(target.id)}/messages`,{message,request_id,allow_network:network,allow_writes:writes,consent,budget});
    setRun(data);setMessage('');setSettling(false);await open(target.id);
  }catch(e){setError(errorText(e) + ' 发送失败或响应不确定时，请先刷新会话核对，不要重复发送同一任务。');}finally{setBusy(false);}}
  useEffect(() => {
    if(!run || mode === 'mcp')return;const id = run.id;const sid = run.session_id;const controller = new AbortController();let disposed = false;let polling = false;
    setEvents(cache.current[id] || []);setFallback(false);
    const accept = (event:RunEvent) => {
      if(disposed || event.seq <= (cursors.current[id] || 0))return;cursors.current[id] = event.seq;
      if(!allowedEvents.has(event.type))return;
      const all = [...(cache.current[id] || []),event].slice(-300);cache.current[id] = all;setEvents(all);
      if(event.type === 'status'){setRun(old => old?.id === id ? {...old,status:event.data.status,error:event.data.error || old.error} : old);if(event.data.settling_in_flight)setSettling(true);}
      if(event.type === 'usage')setRun(old => old?.id === id ? {...old,usage:event.data} : old);
      if(event.type === 'approval_required')setRun(old => old?.id === id ? {...old,pending_approval:event.data as Run['pending_approval'],status:'awaiting_approval'} : old);
      if(event.type === 'tool_result')publish('agent:' + (event.data.tool_name || 'tool'),event.data.result,run.workspace_id || workspace?.id);
    };
    void streamEvents(id,cursors.current[id] || 0,controller.signal,accept).then(() => {if(!disposed)setFallback(true);}).catch(e => {if(!disposed && (e as Error).name !== 'AbortError'){setFallback(true);if((e as any).code === 'AUTH_REQUIRED')setError(errorText(e));}});
    const poll = async () => {if(disposed || polling)return;polling = true;try {
      const [latest,current] = await Promise.all([api<Run>('/api/agent/runs/' + encodeURIComponent(id),{signal:controller.signal}),api<Session>('/api/agent/sessions/' + encodeURIComponent(sid),{signal:controller.signal})]);
      if(!disposed){setRun(latest);setSession(old => old?.id === sid ? current : old);if(['completed','failed'].includes(latest.status))setSettling(false);}
    }catch(e){if(!disposed && (e as Error).name !== 'AbortError')setError(errorText(e));}finally{polling = false;}};
    void poll();const timer = window.setInterval(() => void poll(),2500);
    return () => {disposed = true;controller.abort();window.clearInterval(timer);};
  },[run?.id,mode,reconnect,publish]);
  useEffect(() => {transcriptEnd.current?.scrollIntoView?.({behavior:'smooth',block:'end'});},[session?.messages?.length,events.length]);
  async function command(command:string,body:Data = {}) {if(!run)return;setBusy(true);setError('');try {const data = await post<Run>(`/api/agent/runs/${encodeURIComponent(run.id)}/${command}`,body);setRun(data);if(command === 'cancel')setSettling(true);await refreshWorkspaces();}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  if(mode === 'mcp')return <><div className="page-heading"><div><div className="eyebrow">EXTERNAL AGENT MODE</div><h1>外部 MCP 执行</h1><p>当前模式不调用内置 Agent，不要求本地模型凭证。</p></div></div><McpGuide/></>;
  const messages = (session?.messages || []).filter(item => ['user','assistant','tool'].includes(item.role));
  const tools = events.filter(event => ['tool_started','tool_result','tool_reused'].includes(event.type));
  const approval = run?.pending_approval;
  const toolCards = tools.filter(event => event.type !== 'tool_result').map(event => ({start:event,result:tools.find(e => e.type === 'tool_result' && e.data.call_id === event.data.call_id)}));
  return <div className="agent-workspace">
    <aside className="session-sidebar"><div className="session-heading"><div className="eyebrow">AGENT SESSIONS</div><h2>研究会话</h2><p>{workspace?.title || '请先选择工作区'}</p></div><Field label="新会话名称"><input value={title} onChange={e => setTitle(e.target.value)} maxLength={200} placeholder="为这次研究起个名字"/></Field><Button variant="secondary" disabled={!workspace || busy} onClick={() => void create()}><Icon name="plus" size={16}/>新建会话</Button><div className="session-list">{sessions.length ? sessions.map(item => <button className={`session-item ${session?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => void open(item.id)}><Icon name="agent" size={17}/><div><strong>{item.title}</strong><small>绑定当前工作区</small></div></button>) : <p className="muted">尚无会话。发送目标时可自动新建。</p>}</div><Notice>只展示执行摘要和实际工具结果，不展示模型隐藏推理。</Notice></aside>
    <section className="conversation"><div className="conversation-heading"><div><Badge tone="violet">独立 Agent</Badge><h1>{session?.title || '开始一次研究协作'}</h1></div><div className="row-actions">{run && <Status status={run.status}/>}<Button variant="ghost" onClick={() => session ? void open(session.id) : void list()} aria-label="刷新 Agent 会话"><Icon name="refresh" size={17}/></Button><a className="button ghost" href="#/home">回到工作台</a></div></div>
      {error && <Notice danger>{error}</Notice>}
      {model?.configured === false && <Notice>还没有配置模型。<a href="#/settings">打开模型设置</a>；手动科研业务仍可使用。</Notice>}{model?.configured && model.consented === false && <Notice>模型配置尚未授权材料外发。请先在<a href="#/settings">模型设置</a>明确开启，再授权本次任务。</Notice>}
      <div className="transcript" aria-live="polite">{!messages.length ? <div className="agent-welcome"><span className="welcome-mark"><Icon name="agent" size={42}/></span><h2>从问题出发，以证据落地</h2><p>描述目标，明确材料与权限。Agent 只使用已注册的科研业务工具，每一步都留有实际结果。</p><div className="prompt-suggestions">{['整理我的研究问题与可验证假设','检查当前写作项目的正文证据缺口','基于选中文献组织有出处的大纲'].map(text => <button key={text} onClick={() => setMessage(text)}>{text}<Icon name="arrow" size={15}/></button>)}</div></div> : messages.map((item,index) => item.role === 'tool' ? <details className="transcript-tool" key={index}><summary><Icon name="bolt" size={15}/>{item.tool_name || '业务工具'}<Badge tone={item.ok ? 'green' : 'red'}>{item.ok ? '实际完成' : '操作未完成'}</Badge></summary><ResultView data={item.result}/></details> : publicMessage(item) ? <article className={`message ${item.role}`} key={index}><div className="message-avatar">{item.role === 'user' ? '你' : <Icon name="agent" size={19}/>}</div><div><strong>{item.role === 'user' ? '你' : 'PaperFlow Agent'}</strong><p>{publicMessage(item)}</p></div></article> : null)}<div ref={transcriptEnd}/></div>
      {approval && <div className="approval-card"><div><Icon name="shield"/><strong>这一步需要你的批准</strong><Badge tone="violet">{approval.tool_name}</Badge></div><p>{approval.reason}</p><ResultView data={approval.arguments}/><div className="row-actions"><Button disabled={busy} onClick={() => void command('approve',{approval_id:approval.approval_id,approved:true})}>核对参数并批准</Button><Button variant="secondary" disabled={busy} onClick={() => void command('approve',{approval_id:approval.approval_id,approved:false})}>拒绝本次操作</Button></div></div>}
      <div className="agent-composer"><textarea aria-label="Agent 消息" value={message} onChange={e => setMessage(e.target.value)} maxLength={24000} rows={3} placeholder="描述你的研究目标、选中的材料与期望产物…" onKeyDown={e => {if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)){e.preventDefault();if(!busy && !activeStatuses.includes(run?.status || ''))void send();}}}/><div className="composer-authorizations"><Check label="允许本次联网" checked={network} onChange={setNetwork}/><Check label="允许工作区内业务写入" checked={writes} onChange={setWrites}/><Check label="授权本次消息与工作区结果发送模型" checked={consent} onChange={setConsent}/></div><div className="composer-footer"><small>仅当前工作区 · Ctrl / ⌘ + Enter 发送</small><div className="row-actions">{run && ['paused','interrupted'].includes(run.status) && <Button variant="secondary" disabled={busy || !consent} onClick={() => void command('resume',{consent:true})}>明确授权并恢复</Button>}{run && activeStatuses.includes(run.status) ? <Button variant="danger" disabled={busy} onClick={() => void command('cancel')}>停止后续操作</Button> : <Button disabled={busy || !workspace || !message.trim() || !consent} onClick={() => void send()}><Icon name="arrow" size={17}/>{busy ? '提交中…' : '开始执行'}</Button>}</div></div>{settling && <small className="settling-message">已阻止后续动作；可能仍有在途模型 / 工具请求需要结算或超时。停止不会撤回已完成操作。</small>}</div>
    </section>
    <aside className="execution-sidebar"><div className="execution-heading"><h2>执行与证据</h2><Badge>{fallback ? '状态轮询' : run ? '鉴权事件流' : '等待任务'}</Badge></div><Card title="本次执行预算" description="达到任一额度会在下一项副作用前停止。"><div className="budget-fields">{[{key:'model_calls',label:'模型请求',max:40},{key:'tool_calls',label:'工具执行',max:80},{key:'seconds',label:'运行秒数',max:1800}].map(item => <Field key={item.key} label={item.label}><input type="number" min={1} max={item.max} disabled={Boolean(run && activeStatuses.includes(run.status))} value={budget[item.key as keyof typeof budget]} onChange={e => setBudget({...budget,[item.key]:Number(e.target.value)})}/></Field>)}</div></Card>
      {run && <Card title="实际用量"><div className="usage-grid">{[{key:'model_calls',label:'模型请求'},{key:'tool_calls',label:'工具执行'},{key:'input_tokens',label:'输入 token'},{key:'output_tokens',label:'输出 token'},{key:'elapsed_seconds',label:'执行秒数'}].map(item => <div key={item.key}><span>{item.label}</span><strong>{run.usage?.[item.key] == null ? '未知' : typeof run.usage[item.key] === 'number' ? Math.round(run.usage[item.key] * 10) / 10 : String(run.usage[item.key])}</strong></div>)}</div>{run.error && <Notice danger>{typeof run.error === 'string' ? run.error : run.error.message || '执行失败'}</Notice>}<Button variant="ghost" onClick={() => setReconnect(old => old + 1)}>按事件游标重新连接</Button></Card>}
      <h3 className="section-label">实际工具执行</h3>{toolCards.length ? toolCards.map(({start,result}) => <details className="execution-card" key={start.seq} open={!result}><summary><span className={`execution-dot ${result?.data.ok ? 'success' : ''}`}/><strong>{start.data.tool_name}</strong><Badge tone={result ? result.data.ok ? 'green' : 'red' : 'violet'}>{start.type === 'tool_reused' ? '已记录结果复用' : result ? result.data.ok ? '完成' : '失败' : '执行中'}</Badge></summary><ResultView data={start.data.arguments}/>{result && <ResultView data={result.data.result}/>}</details>) : <Empty title="没有工具执行记录" text="只有实际调用过的工具才显示在这里。"/>}<a className="button secondary" href="#/tasks"><Icon name="documents" size={16}/>查看工作区成果</a>
    </aside>
  </div>;
}
