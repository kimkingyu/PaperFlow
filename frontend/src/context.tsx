import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { action, api, errorText, records, post } from './api';
import type { Data, Tool, Workspace } from './types';
interface Result { action: string; data: any; at: string; workspace_id: string; }
interface Workbench {
  tools: Tool[]; capabilities: Data; workspaces: Workspace[]; workspace?: Workspace; loading: boolean; error: string;
  mode: 'standalone'|'mcp'; setMode: (mode: 'standalone'|'mcp') => void;
  selectWorkspace: (id: string) => void; refresh: () => Promise<void>; refreshWorkspaces: () => Promise<void>;
  execute: (name: string, params?: Data, network?: boolean, approved?: boolean) => Promise<any>;
  results: Result[]; publish: (name: string, data: any, workspaceId?: string) => void; link: (kind: string,targetId: string) => Promise<void>;
  agentPrompt: string; askAgent: (prompt: string) => void; clearAgentPrompt: () => void;
}
const Context = createContext<Workbench | null>(null);
export function WorkbenchProvider({ children }: { children: ReactNode }) {
  const [tools,setTools] = useState<Tool[]>([]); const [capabilities,setCapabilities] = useState<Data>({});
  const [workspaces,setWorkspaces] = useState<Workspace[]>([]); const [workspaceId,setWorkspaceId] = useState('');
  const [loading,setLoading] = useState(true); const [error,setError] = useState(''); const [mode,setMode] = useState<'standalone'|'mcp'>('standalone');
  const [results,setResults] = useState<Result[]>([]); const [agentPrompt,setAgentPrompt] = useState('');
  const workspaceRef = useRef(workspaceId); workspaceRef.current = workspaceId;
  const refreshWorkspaces = useCallback(async () => {
    const data = await api('/api/workbench/workspaces'); const items = records(data,'workspaces') as Workspace[]; setWorkspaces(items);
    setWorkspaceId(old => items.some(item => item.id === old) ? old : items[0]?.id || '');
  },[]);
  const refresh = useCallback(async () => {
    setLoading(true); setError('');
    const responses = await Promise.allSettled([api('/api/workbench/capabilities'),refreshWorkspaces()]);
    if (responses[0].status === 'fulfilled') { setCapabilities(responses[0].value); setTools(records(responses[0].value,'tools') as Tool[]); }
    setError(responses.filter((item): item is PromiseRejectedResult => item.status === 'rejected').map(item => errorText(item.reason)).join(' ')); setLoading(false);
  },[refreshWorkspaces]);
  useEffect(() => { void refresh(); },[refresh]);
  const publish = useCallback((name: string,data: any,scopeId?: string) => setResults(old => [{action:name,data,at:new Date().toISOString(),workspace_id:scopeId ?? workspaceRef.current},...old].slice(0,40)),[]);
  const execute = useCallback(async (name: string,params: Data = {},network = false,approved = false) => { const data = await action(name,params,workspaceId,network,approved); publish(name,data,workspaceId); return data; },[workspaceId,publish]);
  const link = async (kind: string,target_id: string) => { if (!workspaceId) throw new Error('请先选择工作区。'); await post(`/api/workbench/workspaces/${encodeURIComponent(workspaceId)}/links`,{kind,target_id}); await refreshWorkspaces(); };
  return <Context.Provider value={{tools,capabilities,workspaces,workspace:workspaces.find(item => item.id === workspaceId),loading,error,mode,setMode,selectWorkspace:setWorkspaceId,refresh,refreshWorkspaces,execute,results:results.filter(item => item.workspace_id === workspaceId),publish,link,agentPrompt,askAgent:prompt => {setAgentPrompt(prompt); window.location.hash = '/agent';},clearAgentPrompt:() => setAgentPrompt('')}}>{children}</Context.Provider>;
}
export function useWorkbench() { const context = useContext(Context); if (!context) throw new Error('工作台上下文不可用'); return context; }
