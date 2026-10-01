import { useState } from 'react';
import type { Data } from '../types';
import { labels } from './SchemaForm';
import { ArtifactLink, Badge, Empty, RawData, Status, stateLabels } from './ui';
const privateKey = /^(api_key|token|authorization|secret|password|file_path|output_path|local_path|internal_path|reasoning|thinking|chain_of_thought)$/i;
export function ResultView({ data, depth = 0 }: { data: any; depth?: number }) {
  if (data == null) return <span className="muted">未提供</span>;
  if (typeof data === 'boolean') return <Badge tone={data ? 'green' : 'neutral'}>{data ? '是' : '否'}</Badge>;
  if (typeof data === 'string' || typeof data === 'number') { const text = String(data); return <span className="result-text">{stateLabels[text] || text.slice(0,60000)}{text.length > 60000 && <small className="muted">\n仅展示前 60,000 字符，是部分内容；请按服务游标分页继续读取。</small>}</span>; }
  if (depth > 7) return <span className="muted">嵌套内容请展开原始响应查看</span>;
  if (Array.isArray(data)) {
    if (!data.length) return <p className="muted">暂无记录</p>;
    return <div className="result-list">{data.slice(0,80).map((item,i) => <div className="result-item" key={i}><ResultView data={item} depth={depth + 1}/></div>)}{data.length > 80 && <p className="muted">仅展示前 80 条，请使用分页继续读取。</p>}</div>;
  }
  if (data.artifact_id && (data.filename || data.media_type)) return <ArtifactLink artifact={data}/>;
  return <dl className="result-properties">{Object.entries(data).filter(([key,value]) => !privateKey.test(key) && value !== undefined && value !== null).map(([key,value]) => <div key={key}><dt>{labels[key] || resultLabels[key] || key}</dt><dd>{key === 'status' && typeof value === 'string' ? <Status status={value}/> : <ResultView data={value} depth={depth + 1}/>}</dd></div>)}</dl>;
}
export const resultLabels: Record<string,string> = {
  configured:'配置可用',consented:'材料发送已授权',consent:'本次材料授权',base_url:'模型端点',model:'模型名称',remembered:'凭证已记住',keyring_available:'系统密钥环可用',connected:'实际连接状态',platform:'操作系统',backend_calls_llm:'服务后端自行调用模型',managed_upload_limit:'上传字节上限',origin:'记录来源',total_tokens:'总 token',unknown_requests:'用量未知请求',needs_reconciliation:'需要人工核对',next_evidence_offset:'下一批证据偏移',evidence_total:'全部证据数量',evidence_returned:'本批证据数量',evidence_truncated:'证据是否仅部分返回',papers:'文献',projects:'项目',items:'记录',records:'记录',results:'结果',count:'数量',total:'总数',created_at:'创建时间',updated_at:'更新时间',tools:'工具',available:'可用性',unavailable_reason:'不可用原因',source_statuses:'来源状态',search_statuses:'检索状态',coverage:'覆盖与限制',capabilities:'实际能力',acquisition:'合法全文获取状态',authors:'作者',publication_date:'发表日期',source_record:'来源记录',landing_url:'原始来源',arxiv_id:'arXiv 标识',publication_type:'发表类型',version:'版本',pages:'物理页',fragments:'原文片段',next_cursor:'继续读取游标',reading_cards:'解读卡',file_sha256:'当前 PDF 指纹',extraction:'文字提取状态',fulltext:'正文',agent_contract:'证据保存契约',snapshot:'项目快照',gaps:'缺口',matrix:'证据矩阵',evidence_matrix:'正文证据矩阵',candidates:'真实候选',selected_paper_ids:'选中文献',draft_history:'草稿修订历史',history:'历史版本',timeline:'事件时间线',diff:'快照差异',added:'新增',removed:'移除',changed:'变化',standards:'学术标准',issues:'问题定位',warnings:'提醒',defects:'格式问题',artifacts:'可下载产物',artifact_id:'产物标识',filename:'文件名',media_type:'文件类型',size:'字节数',asset_id:'上传资产标识',state:'状态',loop_revision:'补读版本',project_revision:'写作版本',usage:'实际用量',budget:'预算',stop_reason:'停止原因',recommended_action:'推荐下一步',pending_actions:'待执行动作',action_id:'动作标识',message:'服务说明',model_calls:'模型请求',tool_calls:'工具执行',elapsed_seconds:'执行秒数',input_tokens:'输入 token',output_tokens:'输出 token',data_year:'数据年份',source_version:'来源版本',ranking:'分区',impact_factor:'影响因子',cas:'中科院分区',jcr:'JCR 分区',apc:'APC',risk:'风险',indexing:'收录状态',fit_score:'适配分',notices:'能力说明',documents:'文档',targets:'可选择文档',selection:'当前选区',fingerprint:'选区指纹',selected_text:'选中文本'
};
export function ActionResult({ data, title = '操作结果' }: { data: Data | any; title?: string }) {
  const [open,setOpen] = useState(true);
  if (data === undefined) return null;
  return <section className="action-result"><button className="result-toggle" onClick={() => setOpen(!open)}><span className="success-dot"/>{title}<span>{open ? '收起' : '展开'}</span></button>{open && <><ResultView data={data}/><RawData data={data}/></>}</section>;
}
export function ResultEmpty() { return <Empty title="等待真实结果" text="完成左侧操作后，来源、记录和限制会显示在这里。"/>; }
