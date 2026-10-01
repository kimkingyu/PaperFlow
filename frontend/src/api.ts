import type { Data, RunEvent } from './types';
const TOKEN_KEY = 'paperflow.web-token';
let token = '';
export class ApiError extends Error {
  constructor(message: string, public code = 'REQUEST_FAILED', public status = 0, public details?: unknown) { super(message); }
}
export function initializeToken(): string {
  const url = new URL(window.location.href);
  const incoming = url.searchParams.get('token');
  if (incoming) {
    token = incoming;
    try { sessionStorage.setItem(TOKEN_KEY, incoming); } catch { /* In-memory authentication still works. */ }
    url.searchParams.delete('token');
    history.replaceState(history.state, '', url.pathname + url.search + url.hash);
  } else {
    try { token = sessionStorage.getItem(TOKEN_KEY) || ''; } catch { token = ''; }
  }
  return token;
}
export function hasToken() { return Boolean(token); }
export function authHeaders(extra: HeadersInit = {}): Headers {
  const headers = new Headers(extra);
  if (token) headers.set('X-PaperFlow-Token', token);
  return headers;
}
export async function api<T = Data>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try { response = await fetch(path, { ...options, headers: authHeaders(options.headers), credentials: 'same-origin' }); }
  catch (error) { if ((error as Error).name === 'AbortError') throw error; throw new ApiError('无法连接本地服务。请确认 PaperFlow 网页服务仍在运行。', 'CONNECTION_FAILED'); }
  if (response.status === 403 || response.status === 401) {
    window.dispatchEvent(new Event('paperflow:unauthorized'));
    throw new ApiError('访问令牌已失效。请从终端重新打开带令牌的启动链接。', 'AUTH_REQUIRED', response.status);
  }
  let envelope: any;
  try { envelope = await response.json(); } catch { throw new ApiError('服务返回了无法读取的响应。', 'INVALID_RESPONSE', response.status); }
  if (!response.ok || envelope.error_code || ['error','failed','failure'].includes(envelope.status)) {
    throw new ApiError(envelope.message || '操作未完成，请检查参数或环境能力。', envelope.error_code || 'REQUEST_FAILED', response.status, envelope.data);
  }
  return ('data' in envelope ? envelope.data : envelope) as T;
}
export function post<T = Data>(path: string, body: unknown) {
  return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
export function action<T = Data>(name: string, params: Data, workspace_id?: string, allow_network = false, approved = false) {
  return post<T>('/api/workbench/action', { action: name, params, workspace_id: workspace_id || null, allow_network, approved });
}
export async function upload(file: File, workspaceId?: string) {
  if (!workspaceId) throw new ApiError('请先创建或选择工作区，再上传材料。', 'WORKSPACE_REQUIRED');
  const types: Record<string,string> = { pdf:'application/pdf',docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',json:'application/json',csv:'text/csv',txt:'text/plain',md:'text/markdown',sqlite:'application/vnd.sqlite3',sqlite3:'application/vnd.sqlite3',db:'application/vnd.sqlite3' };
  const extension = file.name.split('.').at(-1)?.toLowerCase() || '';
  return api('/api/workbench/assets?workspace_id=' + encodeURIComponent(workspaceId), {
    method: 'POST', headers: { 'Content-Type': types[extension] || file.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name) }, body: file
  });
}
export async function download(id: string, filename = 'paperflow-artifact') {
  const response = await fetch('/api/workbench/artifacts/' + encodeURIComponent(id) + '/download', { headers: authHeaders() });
  if (!response.ok) {
    if (response.status === 403) window.dispatchEvent(new Event('paperflow:unauthorized'));
    throw new ApiError(response.status === 403 ? '访问令牌已失效，请使用启动链接重新进入。' : '下载失败，产物可能已失效。', 'DOWNLOAD_FAILED', response.status);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Fetch rather than EventSource: every stream request carries the startup token.
export async function streamEvents(runId: string, after: number, signal: AbortSignal, onEvent: (event: RunEvent) => void) {
  const response = await fetch('/api/agent/runs/' + encodeURIComponent(runId) + '/events?after=' + after, { headers: authHeaders({ Accept: 'text/event-stream' }), signal });
  if (response.status === 403) { window.dispatchEvent(new Event('paperflow:unauthorized')); throw new ApiError('运行事件访问令牌失效。', 'AUTH_REQUIRED', 403); }
  if (!response.ok || !response.body) throw new ApiError('事件流不可用，已改用状态轮询。');
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n');
      let end: number;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const text = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (text) { const event = JSON.parse(text) as RunEvent; if (typeof event.seq === 'number' && event.seq > after) { after = event.seq; onEvent(event); } }
      }
      if (buffer.length > 2 * 1024 * 1024) throw new ApiError('事件响应超过界面读取上限。');
      if (done) break;
    }
  } finally { reader.releaseLock(); }
}
export function records(data: any, ...keys: string[]): Data[] {
  if (Array.isArray(data)) return data;
  for (const key of keys.concat(['items','records','projects','papers','sessions','artifacts','workspaces','results'])) if (Array.isArray(data?.[key])) return data[key];
  return [];
}
export function errorText(error: unknown) {
  if (error instanceof ApiError && error.code.includes('REVISION')) return '版本冲突：项目已由其他会话修改。你的编辑仍在此页，请重新读取最新版本后对照合并。';
  return error instanceof Error ? error.message : '操作失败，请重试。';
}
