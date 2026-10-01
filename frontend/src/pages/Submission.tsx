import { useState } from 'react';
import type { Data } from '../types';
import { ToolPanel, UploadField } from '../components/ToolPanel';
import { Card, Notice, PageHeading, Tabs } from '../components/ui';
export default function Submission() {
  const [tab,setTab] = useState('tracking');const [asset,setAsset] = useState<Data>();const [previous,setPrevious] = useState<Data>();const [references,setReferences] = useState<Data>();
  return <>
    <PageHeading eyebrow="SUBMISSION & REFERENCE CONTEXT" title="投稿与参考文献" description="看清投稿事件的变化，也从你的引用列表发现同行发表的期刊。只使用你提供的真实数据。"/>
    <Tabs active={tab} onChange={setTab} items={[{id:'tracking',title:'投稿事件与时间线'},{id:'references',title:'参考文献与同行期刊'}]}/>
    {tab === 'tracking' && <><Notice>这是离线 Elsevier 事件解析，不连接出版社实时系统，不要求账号密码。请上传系统导出的 JSON 或粘贴已有事件数据。</Notice><div className="two-columns"><Card title="当前事件快照"><UploadField accept=".json,application/json" title="上传投稿事件 JSON" onUploaded={setAsset}/></Card><Card title="前次快照（可选）"><UploadField accept=".json,application/json" title="上传前次投稿快照" onUploaded={setPrevious}/></Card></div><ToolPanel name="submission_track" title="解析投稿时间线与差异" description="标题默认不显示；实际事件、时间线和快照差异由本地解析器生成。" preset={{...(asset ? {asset_id:asset.asset_id} : {}),...(previous ? {previous_asset_id:previous.asset_id} : {})}}/></>}
    {tab === 'references' && <div className="two-columns"><Card title="导入参考文献列表"><UploadField accept=".json,application/json" title="上传现有参考文献信息" onUploaded={setReferences}/><p className="muted">也可以在右侧逐条填写真实标题、期刊、ISSN、DOI 与年份。不伪造文献，不承诺 Zotero 双向同步。</p></Card><ToolPanel name="journal_peers" title="分析引用中的同行期刊" description="从实际参考信息发现候选，再按选刊偏好筛选。" preset={references ? {asset_id:references.asset_id} : {}}/></div>}
  </>;
}
