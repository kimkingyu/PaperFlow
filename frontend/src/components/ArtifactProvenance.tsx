import type { Artifact } from '../types';
const origins: Record<string,string> = {web:'网页操作',nativeAgent:'内置 Agent',native_agent:'内置 Agent',calling_agent:'调用方记录',gui_model:'GUI 模型'};
export function ArtifactProvenance({ artifact }: { artifact: Artifact }) {
  const metadata = artifact.metadata;
  const source = metadata?.source_action || metadata?.source_tool || artifact.source;
  return <div className="artifact-provenance">
    <small>来源动作：{source || '来源动作未提供'}</small>
    {metadata?.origin && <small>执行来源：{origins[metadata.origin] || metadata.origin}</small>}
    {metadata?.project_id && <small>项目：{metadata.project_id}{metadata.project_revision != null ? ` · 版本 ${metadata.project_revision}` : ''}</small>}
    {!metadata?.project_id && metadata?.project_revision != null && <small>项目版本：{metadata.project_revision}</small>}
    {metadata?.run_id && <small>执行记录：{metadata.run_id}</small>}
    {!!metadata?.input_asset_refs?.length && <div><small>输入资产：</small>{metadata.input_asset_refs.map(asset => <small key={asset.asset_id} className="asset-provenance" title={asset.sha256}> {asset.asset_id}{asset.sha256 ? ` · SHA-256 ${asset.sha256.slice(0,16)}…` : ' · 指纹未提供'}</small>)}</div>}
    <small>创建时间：{artifact.created_at || '时间未提供'}</small>
  </div>;
}
