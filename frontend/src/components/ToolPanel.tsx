import { useEffect, useMemo, useState } from 'react';
import { useWorkbench } from '../context';
import { errorText, upload } from '../api';
import type { Data } from '../types';
import { initialValue, SchemaEditor, validate } from './SchemaForm';
import { ActionResult } from './Results';
import { Badge, Button, Card, Check, Icon, Notice } from './ui';
export function ToolPanel({ name, title, description, preset = {}, hidden = [], onResult, compact = false, result = true }: { name: string; title: string; description?: string; preset?: Data; hidden?: string[]; onResult?: (data: any, params: Data) => void | Promise<void>; compact?: boolean; result?: boolean }) {
  const { tools,execute,loading } = useWorkbench();
  const tool = tools.find(item => item.name === name);
  const initial = useMemo(() => tool ? {...initialValue(tool.input_schema),...preset} : {...preset},[tool,JSON.stringify(preset)]);
  const [value,setValue] = useState<Data>(initial); const [busy,setBusy] = useState(false); const [network,setNetwork] = useState(false); const [approved,setApproved] = useState(false);
  const [error,setError] = useState(''); const [data,setData] = useState<any>();
  useEffect(() => { setValue(initial); setApproved(false); },[initial]);
  const submit = async () => {
    if (!tool) return; const params = {...value,...preset};
    const errors = validate(tool.input_schema,params);
    if (errors.length) { setError([...new Set(errors)].slice(0,8).join('；')); return; }
    setBusy(true);setError('');
    try { const response = await execute(name,params,network,approved); setData(response); await onResult?.(response,params); }
    catch(error) { setError(errorText(error)); } finally { setBusy(false); }
  };
  return <Card title={title} description={description} className={compact ? 'compact-tool' : ''} action={tool && <Badge tone={tool.available ? 'green' : 'neutral'}>{tool.available ? (tool.network ? '联网工具' : '本地工具') : '环境不可用'}</Badge>}>
    {!tool ? <Notice>{loading ? '正在加载服务能力…' : '当前服务未提供此能力。请更新 PaperFlow 或查看环境设置。'}</Notice> : !tool.available ? <Notice>{tool.unavailable_reason || '当前环境无法执行此工具。'}</Notice> : <form onSubmit={e => {e.preventDefault();void submit();}}>
      <SchemaEditor schema={tool.input_schema} value={value} onChange={next => {setValue(next);setApproved(false);}} hidden={[...Object.keys(preset),...hidden]} disabled={busy}/>
      {(tool.network || tool.approval_required || tool.desktop || tool.mutates) && <div className="authorization">
        {tool.network && <Check label="允许本次操作联网（检索词或选中材料会发往相应来源）" checked={network} onChange={setNetwork}/>}
        {(tool.approval_required || tool.desktop) && <Check label={tool.desktop ? '确认操作当前锁定文档；可能修改桌面内容' : '已核对本次操作范围，批准执行'} checked={approved} onChange={setApproved}/>}
        {tool.mutates && <small>将保存或更新真实项目数据。版本冲突不会覆盖现有记录。</small>}
      </div>}
      {error && <Notice danger>{error}</Notice>}
      <div className="form-actions"><Button type="submit" disabled={busy || (tool.network && !network) || ((tool.approval_required || tool.desktop) && !approved)}>{busy ? <><span className="spinner"/>执行中…</> : <><Icon name="arrow" size={16}/>{title}</>}</Button></div>
    </form>}
    {result && <ActionResult data={data}/ >}
  </Card>;
}
export function UploadField({ accept, onUploaded, title = '上传研究材料' }: { accept: string; onUploaded?: (data: Data) => void; title?: string }) {
  const { workspace,publish } = useWorkbench(); const [data,setData] = useState<Data>(); const [busy,setBusy] = useState(false); const [error,setError] = useState('');
  return <div className="upload-area"><Icon name="upload" size={25}/><strong>{title}</strong><p>文件保存在本地受管目录，只通过资产标识使用。</p><label className="button secondary upload-button">{busy ? '正在上传…' : '选择文件'}<input type="file" accept={accept} disabled={busy} onChange={async e => { const file = e.target.files?.[0]; if (!file) return; setBusy(true);setError('');try { const result = await upload(file,workspace?.id);setData(result);publish('asset_upload',result,workspace?.id);onUploaded?.(result); } catch(error) {setError(errorText(error));} finally {setBusy(false);e.target.value = '';} }}/></label>{data && <small className="success-text">已上传：{data.filename} · {data.asset_id}</small>}{error && <Notice danger>{error}</Notice>}</div>;
}
