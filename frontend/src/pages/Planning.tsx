import { useCallback, useEffect, useState } from 'react';
import { useWorkbench } from '../context';
import { errorText, records } from '../api';
import type { Data } from '../types';
import { initialValue, SchemaEditor, validate } from '../components/SchemaForm';
import { ActionResult, ResultView } from '../components/Results';
import { ToolPanel } from '../components/ToolPanel';
import { Badge, Button, Card, Empty, Field, Icon, Notice, PageHeading, Status, Tabs } from '../components/ui';
export default function Planning() {
  const {execute,tools,workspace,link,askAgent} = useWorkbench();const [projects,setProjects] = useState<Data[]>([]);const [id,setId] = useState('');const [snapshot,setSnapshot] = useState<Data>();const [plan,setPlan] = useState<Data>();
  const [tab,setTab] = useState('plan');const [error,setError] = useState('');const [busy,setBusy] = useState(false);const [note,setNote] = useState('更新研究规划');const [taskId,setTaskId] = useState('');
  const saveTool = tools.find(tool => tool.name === 'planning_save');const planSchema = saveTool?.input_schema.properties?.plan;
  const list = useCallback(async () => {try {setProjects(records(await execute('planning_list',{limit:30,offset:0})));}catch(e){setError(errorText(e));}},[execute]);
  useEffect(() => {if (tools.some(t => t.name === 'planning_list')) void list();},[list,tools]);
  useEffect(() => {setId('');setSnapshot(undefined);setPlan(undefined);},[workspace?.id]);
  async function load(target = id) {if (!target) return;setError('');try {const data = await execute('planning_get',{project_id:target});const snap = data.snapshot || data;setSnapshot(snap);setPlan(structuredClone(snap.plan));setId(target);}catch(e){setError(errorText(e));}}
  async function created(data: Data) {const snap = data.snapshot || data;const target = snap.project_id;if (target) {await link('planning',target).catch(e => setError(errorText(e)));await list();await load(target);setTab('plan');}}
  async function save() {if (!snapshot || !plan || !planSchema || !saveTool) return;const errors = validate(planSchema,plan,saveTool.input_schema);if(errors.length){setError(errors.slice(0,8).join('；'));return;}setBusy(true);setError('');try {await execute('planning_save',{project_id:id,expected_revision:snapshot.revision,plan,change_note:note});await load();await list();}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  return <>
    <PageHeading eyebrow="PLAN → EXPERIMENT → EVIDENCE" title="科研规划" description="把假设变成可检验的实验，把推进变成可核验的任务。研究结论由实际证据支撑。" action={<Button variant="secondary" onClick={() => askAgent(`请协助科研规划${id ? ` ${id}，当前版本 ${snapshot?.revision}` : ''}。先检查实际材料和缺口，不要编造实验结果。`)}><Icon name="agent" size={17}/>与 Agent 一起规划</Button>}/>
    {error && <Notice danger>{error}</Notice>}
    <div className="project-toolbar"><Icon name="folder"/><select aria-label="科研规划项目" value={id} onChange={e => void load(e.target.value)}><option value="">选择本地科研规划</option>{projects.map(project => <option key={project.project_id} value={project.project_id}>{project.title || project.profile?.title || project.project_id} · v{project.revision}</option>)}</select><Button variant="ghost" onClick={() => void list()}><Icon name="refresh" size={16}/>刷新列表</Button>{snapshot && <Badge tone="violet">版本 {snapshot.revision}</Badge>}</div>
    <Tabs active={tab} onChange={setTab} items={[{id:'plan',title:'研究规划'},{id:'create',title:'新建 / 准备'},{id:'tasks',title:'阶段任务'},{id:'evidence',title:'证据记录'},{id:'export',title:'历史与导出'}]}/>
    {tab === 'create' && <div className="two-columns"><ToolPanel name="planning_prepare" title="整理研究材料" description="读取用户实际材料，返回规划契约；不会自动推断已完成实验。"/><ToolPanel name="planning_create" title="创建科研规划" description="填写项目画像。问题、实验和依赖可以在创建后继续编辑。" onResult={data => void created(data)}/></div>}
    {tab === 'plan' && (!plan || !snapshot ? <Empty title="选择或创建一份研究规划" text="完整编辑研究问题、假设、实验、资源、任务依赖与证据矩阵。" action={<Button onClick={() => setTab('create')}><Icon name="plus" size={16}/>新建科研规划</Button>}/> : <>
      <div className="metric-grid">{[{label:'研究问题',value:plan.questions?.length || 0},{label:'实验方案',value:plan.experiments?.length || 0},{label:'已记录证据',value:plan.evidence?.length || 0}].map(item => <div className="metric" key={item.label}><small>{item.label}</small><strong>{item.value}</strong><span>来自当前版本</span></div>)}</div>
      <Card title="结构化规划编辑" description="保存时提交完整版本快照；跨会话修改会触发版本冲突，不会静默覆盖。" action={<Badge>v{snapshot.revision}</Badge>}>
        {planSchema && saveTool ? <SchemaEditor schema={planSchema} root={saveTool.input_schema} value={plan} onChange={setPlan} disabled={busy}/> : <Notice>服务缺少规划编辑契约。</Notice>}
        <div className="save-bar"><Field label="修订说明"><input value={note} onChange={e => setNote(e.target.value)} maxLength={1000}/></Field><Button disabled={busy || !note.trim()} onClick={() => void save()}>{busy ? '正在保存…' : '保存规划版本'}</Button><Button variant="secondary" onClick={() => void load()}>重新读取最新版本</Button></div>
      </Card><Card title="问题—实验—证据矩阵" description="结构检查不等于科学结论认证"><ResultView data={snapshot.overview || {questions:plan.questions,experiments:plan.experiments,evidence:plan.evidence}}/></Card>
    </>)}
    {tab === 'tasks' && (!snapshot ? <Empty title="先选择科研规划"/> : <div className="two-columns"><Card title="阶段任务与依赖">{snapshot.plan?.tasks?.length ? snapshot.plan.tasks.map((task:Data) => <button className={`task-row ${taskId === task.id ? 'selected' : ''}`} key={task.id} onClick={() => setTaskId(task.id)}><Status status={task.status}/><div><strong>{task.text}</strong><small>完成条件：{task.completion_condition}</small><small>{task.depends_on?.length ? `前置任务：${task.depends_on.join('、')}` : '无前置依赖'}</small></div></button>) : <Empty title="尚无任务" text="在研究规划编辑器中添加任务、验收条件和依赖。"/>}</Card>{taskId ? <ToolPanel key={taskId} name="planning_task" title="更新阶段任务" preset={{project_id:id,task_id:taskId,expected_revision:snapshot.revision}} onResult={() => void load()}/> : <Empty title="选择任务后编辑" text="完成任务需提供完成记录；阻塞任务需说明原因。"/>}</div>)}
    {tab === 'evidence' && (!snapshot ? <Empty title="先选择科研规划"/> : <div className="two-columns"><ToolPanel name="planning_evidence" title="记录有来源的证据" description="区分测量、仿真、文献、笔记与产物；来源只保存引用，不会自动访问。" preset={{project_id:id,expected_revision:snapshot.revision}} onResult={() => void load()}/><Card title="当前证据矩阵">{snapshot.plan?.evidence?.length ? <ResultView data={snapshot.plan.evidence}/> : <Empty title="尚无证据" text="没有真实数据时，保留未核实状态。"/>}</Card></div>)}
    {tab === 'export' && (!snapshot ? <Empty title="先选择科研规划"/> : <div className="two-columns"><ToolPanel name="planning_export" title="导出独立规划 DOCX" description="规划文档不是已完成实验的论文；生成新的可下载产物。" preset={{project_id:id}}/><ToolPanel name="planning_get" title="读取历史版本" preset={{project_id:id}}/></div>)}
  </>;
}
