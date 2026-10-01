import { cloneElement, isValidElement, useId, useState, type ReactElement, type ReactNode } from 'react';
import { download, errorText } from '../api';
import type { Artifact } from '../types';

export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string,string> = {
    home: 'M3 10 12 3l9 7v10H3Z M9 20v-7h6v7', agent: 'M5 6h14v13H5Z M9 10v2 M15 10v2 M9 16h6 M12 3v3 M2 10v5 M22 10v5',
    planning: 'M5 3h14v18H5Z M8 7h8 M8 11h8 M8 15h5', papers: 'M3 5h8l1 2 1-2h8v15h-8l-1 1-1-1H3Z M12 7v14',
    writing: 'm4 17-1 4 4-1L20 7l-3-3Z M14 7l3 3 M3 3h8 M3 7h5', journals: 'M4 3h16v18H4Z M8 7h8 M8 11h8 M8 15h3',
    submission: 'M3 12h18 M14 5l7 7-7 7 M3 5v14', documents: 'M5 3h10l4 4v14H5Z M15 3v5h4 M8 12h8 M8 16h6',
    tasks: 'M3 5h18v16H3Z M7 9l2 2 4-4 M7 16h10', settings: 'M12 3v3 M12 18v3 M3 12h3 M18 12h3 M6 6l2 2 M16 16l2 2 M6 18l2-2 M16 8l2-2 M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
    plus: 'M12 5v14 M5 12h14', arrow: 'M5 12h14 M13 6l6 6-6 6', close: 'M6 6l12 12 M6 18 18 6', menu: 'M4 6h16 M4 12h16 M4 18h16',
    refresh: 'M20 7v5h-5 M20 12a8 8 0 1 0-2 6', check: 'm5 12 4 4 10-10', folder: 'M3 6h7l2 3h9v11H3Z', shield: 'm12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6Z M8 12l3 3 5-5',
    chevron: 'm9 5 7 7-7 7', download: 'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5', search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0 M15 15l6 6',
    bolt: 'm13 2-9 12h7l-1 8 10-12h-7Z', info: 'M12 8h.01 M12 11v6 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0', upload: 'M12 16V3 M7 8l5-5 5 5 M4 17v4h16v-4'
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.info}/></svg>;
}
export function Button({ children, variant = 'primary', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary'|'secondary'|'ghost'|'danger' }) {
  return <button {...props} className={`button ${variant} ${props.className || ''}`}>{children}</button>;
}
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function Card({ children, title, description, action, className = '' }: { children: ReactNode; title?: string; description?: string; action?: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{title && <div className="card-heading"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action}</div>}{children}</section>;
}
export function PageHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow || 'RESEARCH WORKSPACE'}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>;
}
export function Empty({ title = '这里还没有内容', text, action }: { title?: string; text?: string; action?: ReactNode }) {
  return <div className="empty"><div className="empty-icon"><Icon name="folder" size={26}/></div><h3>{title}</h3>{text && <p>{text}</p>}{action}</div>;
}
export function Notice({ children, danger = false }: { children: ReactNode; danger?: boolean }) { return <div className={`notice ${danger ? 'error' : ''}`} role={danger ? 'alert' : 'status'}><Icon name={danger ? 'info' : 'shield'}/><div>{children}</div></div>; }
export function Loading() { return <div className="loading" role="status"><span className="spinner"/>正在读取真实数据…</div>; }
export function Tabs({ items, active, onChange }: { items: { id: string; title: string }[]; active: string; onChange: (id: string) => void }) {
  return <div className="tabs" role="tablist">{items.map(item => <button key={item.id} role="tab" aria-selected={active === item.id} className={active === item.id ? 'active' : ''} onClick={() => onChange(item.id)}>{item.title}</button>)}</div>;
}
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { const id = useId(); return <div className="field"><label htmlFor={id}>{label}</label>{isValidElement(children) ? cloneElement(children as ReactElement<any>, { id, 'aria-describedby': hint ? id + '-hint' : undefined }) : children}{hint && <small id={id + '-hint'}>{hint}</small>}</div>; }
export function Check({ label, checked, onChange, id, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; id?: string; disabled?: boolean }) { return <label className="checkbox" htmlFor={id}><input id={id} disabled={disabled} type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)}/><span>{label}</span></label>; }
export function RawData({ data }: { data: unknown }) { return <details className="raw-data"><summary>高级：查看原始响应</summary><pre>{JSON.stringify(data, (key,value) => /api_key|authorization|(^|_)token$|secret|password|reasoning|thinking|chain_of_thought|file_path|output_path|local_path|internal_path/i.test(key) ? '[已隐藏]' : value, 2)}</pre></details>; }
export function ArtifactLink({ artifact }: { artifact: Artifact }) {
  const [error,setError] = useState(''); const [busy,setBusy] = useState(false);
  const id = artifact.artifact_id || artifact.id;
  return <div className="artifact"><span className="file-icon"><Icon name="documents"/></span><div><strong>{artifact.title || artifact.filename || '科研产物'}</strong><small>{artifact.media_type || '受管文件'}{artifact.size ? ` · ${(artifact.size / 1024).toFixed(1)} KB` : ''}</small>{error && <small className="error-text">{error}</small>}</div><Button variant="ghost" disabled={!id || busy} aria-label={`下载 ${artifact.filename || '产物'}`} onClick={async () => { if (!id) return; setBusy(true); setError(''); try { await download(id,artifact.filename); } catch(e) { setError(errorText(e)); } finally { setBusy(false); } }}><Icon name="download"/></Button></div>;
}
export const stateLabels: Record<string,string> = { success:'成功',partial:'部分返回',error:'失败',unknown:'未知',needs_agent_assessment:'需要相关性判断',needs_agent_plan:'待制定方案',needs_search_plan:'需要检索计划',needs_reading:'需要正文阅读',evidence_ready:'证据已准备',draft_ready:'草稿已保存',openai_compatible:'OpenAI 兼容协议',openai_responses:'OpenAI Responses',anthropic:'Anthropic Messages',nativeAgent:'内置 Agent',calling_agent:'调用方记录',gui_model:'GUI 模型',start:'启动',pause:'暂停',resume:'恢复',stop:'停止',update_budget:'更新预算',auto:'自动识别',idea:'研究想法',manuscript:'稿件',cursor:'当前光标',end:'文档末尾',author_claim:'作者陈述',agent_inference:'解读推断',research_question:'研究问题',method:'方法',assumptions:'假设',baselines_experiments:'基线与实验',results:'结果',limitations:'局限',relevance:'相关性',reusable_parts:'可复用部分',code_data:'代码与数据',sub_questions:'子问题覆盖',method_baselines:'方法基线',contrary_findings:'相反证据',draft_support:'段落支撑',todo:'待开始',doing:'进行中',done:'已完成',blocked:'受阻',queued:'排队中',running:'执行中',awaiting_approval:'等待审批',paused:'已暂停',completed:'已完成',failed:'失败',cancelled:'已停止',interrupted:'已中断',cancelling:'正在停止',idle:'未启动',awaiting_review:'等待评审',awaiting_assessment:'等待筛选',awaiting_interpretation:'等待解读',awaiting_revision:'等待修订',ready:'待执行',executing:'执行中',stopped:'已停止',available:'可用',unavailable:'不可用',unverified:'未核实',verified:'已核实',retracted:'已撤回',core:'核心',background:'背景',marginal:'边缘',irrelevant:'不相关',metadata:'元数据',abstract:'摘要',fulltext:'正文',placeholder:'缺失材料占位',literature_summary:'文献归纳',literature_inference:'文献推断',author_proposal:'作者提案',user_material:'用户材料',planning:'科研规划',writing:'文献写作',paper:'文献',zh:'中文',en:'英文',crossref:'Crossref',arxiv:'arXiv',measurement:'测量',simulation:'仿真',literature:'文献',note:'笔记',artifact:'产物',proposed:'拟议',confirmed:'已确认',supported:'支持',refuted:'否定',inconclusive:'尚无定论',testing:'验证中',planned:'已规划',sufficient:'充分',gap:'存在缺口',uncertain:'不确定',not_applicable:'不适用'};
export function Status({ status }: { status: string }) { return <Badge tone={['failed','blocked','interrupted'].includes(status) ? 'red' : ['completed','done','verified'].includes(status) ? 'green' : ['running','doing','awaiting_approval'].includes(status) ? 'violet' : 'neutral'}>{stateLabels[status] || status}</Badge>; }
