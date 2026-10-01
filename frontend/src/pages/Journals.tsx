import { useState } from 'react';
import { useWorkbench } from '../context';
import { errorText, records } from '../api';
import type { Data } from '../types';
import { ToolPanel, UploadField } from '../components/ToolPanel';
import { ActionResult, ResultView } from '../components/Results';
import { Badge, Button, Card, Check, Empty, Icon, Notice, PageHeading, Tabs } from '../components/ui';
function PreviewTool({ name,title,preset = {} }: { name:string; title:string; preset?:Data }) {
  const {execute} = useWorkbench();const [preview,setPreview] = useState<{params:Data;data:Data}>();const [confirm,setConfirm] = useState(false);const [data,setData] = useState<Data>();const [error,setError] = useState('');const [busy,setBusy] = useState(false);
  return <div><ToolPanel name={name} title={`预览${title}`} preset={{...preset,dry_run:true}} onResult={(data,params) => {setPreview({data,params});setConfirm(false);}}/>{preview && <Card title={`确认${title}`} description="仅提交下方上次预览的范围。修改输入后请重新预览。"><ResultView data={preview.params}/><Check label="已核对预览结果，允许写入本地数据库" checked={confirm} onChange={setConfirm}/><Button disabled={!confirm || busy} onClick={async () => {setBusy(true);setError('');try{setData(await execute(name,{...preview.params,dry_run:false},name === 'journal_refresh',true));setPreview(undefined);setConfirm(false);}catch(e){setError(errorText(e));}finally{setBusy(false);}}}>{busy ? '正在写入…' : `确认${title}`}</Button>{error && <Notice danger>{error}</Notice>}</Card>}<ActionResult data={data}/></div>;
}
export default function Journals() {
  const {execute,askAgent} = useWorkbench();const [tab,setTab] = useState('search');const [found,setFound] = useState<Data>();const [selected,setSelected] = useState<Data>();const [checked,setChecked] = useState<Data>();const [compare,setCompare] = useState<string[]>([]);const [asset,setAsset] = useState<Data>();const [manuscript,setManuscript] = useState<Data>();const [error,setError] = useState('');
  const journals = records(found,'journals');
  return <>
    <PageHeading eyebrow="JOURNAL FIT, WITH REAL SOURCES" title="选刊与数据源" description="一起看适配度、年份、费用与风险。来源快照不是实时收录或安全保证，推荐分也不是录用概率。" action={<Button variant="secondary" onClick={() => askAgent('请基于当前工作区实际研究材料做分层选刊：解释主题适配、分区年份、费用与来源局限；不要把推荐分说成录用概率。')}><Icon name="agent" size={17}/>与 Agent 一起选刊</Button>}/>
    {error && <Notice danger>{error}</Notice>}
    <Tabs active={tab} onChange={setTab} items={[{id:'search',title:'检索与风险'},{id:'compare',title:'并排对比'},{id:'recommend',title:'画像与推荐'},{id:'sources',title:'数据源与导入'}]}/>
    {tab === 'search' && <><ToolPanel name="search" title="检索期刊" description="按期刊、ISSN、分区、费用等实际字段筛选。" onResult={setFound} result={false}/><div className="two-columns"><Card title="期刊结果" action={<Badge>{journals.length} 条</Badge>}>{!journals.length ? <Empty title="尚无期刊结果" text="输入检索条件；没有记录时不会展示示例期刊。"/> : journals.map(journal => {
      const id = journal.journal_id || journal.id; return <article className="journal-card" key={id || journal.issn}><div><h3>{journal.title || journal.name}</h3><p>{journal.issn || journal.issn_print} · {journal.publisher || '未提供出版社'}</p></div><div className="row-actions"><Button variant="secondary" onClick={async () => {try{setSelected(await execute('details',{query:id || journal.issn || journal.title}));setChecked(await execute('check',{query:id || journal.issn || journal.title}));}catch(e){setError(errorText(e));}}}>详情 / 风险</Button><Button variant="ghost" disabled={!id || compare.includes(id)} onClick={() => setCompare([...compare,id])}>加入对比</Button></div></article>;
    })}{found && <ActionResult data={found} title="检索来源与完整响应"/>}</Card><Card title="详情与风险证据">{selected ? <><ResultView data={selected}/><ActionResult data={checked} title="预警与政策核验"/></> : <Empty title="选择一份期刊记录" text="查看实际指标、来源年份和可核验的风险记录。"/>}</Card></div></>}
    {tab === 'compare' && <><Card title="已选对比期刊" description="最多十本期刊，仅比较真实记录">{!compare.length ? <Empty title="尚未选择期刊" text="从检索结果加入对比，或在下方填写已知的实际期刊标识。"/> : <div className="chips">{compare.map(id => <button className="chip" key={id} onClick={() => setCompare(compare.filter(item => item !== id))}>{id}<Icon name="close" size={14}/></button>)}</div>}</Card><ToolPanel name="compare" title="比较期刊" preset={compare.length ? {journal_ids:compare} : {}}/></>}
    {tab === 'recommend' && <><Card title="上传研究画像材料" description="选择稿件、研究笔记或项目说明；只读取受管文件，不访问任意目录。"><UploadField accept=".txt,.md,.docx,.pdf" title="上传稿件 / 研究说明" onUploaded={setManuscript}/>{manuscript && <ToolPanel name="read_project" title="读取上传项目材料" preset={{asset_id:manuscript.asset_id}}/>}</Card><div className="two-columns"><ToolPanel name="prepare" title="整理稿件画像" description="只处理输入材料，不自动声称已经理解完整稿件。" preset={manuscript ? {asset_id:manuscript.asset_id} : {}}/><ToolPanel name="recommend" title="推荐目标期刊" description="结构化填写研究画像、实际候选与适配判断。没有判断时服务会指出缺口。" preset={manuscript ? {asset_id:manuscript.asset_id} : {}}/></div></>}
    {tab === 'sources' && <>
      <Notice>本地快照和人工证据不是实时安全保证。数据库构建、导入与刷新先预览，再按明确范围确认写入。不提供合成网络评论检索。</Notice>
      <div className="two-columns"><ToolPanel name="journal_sources" title="检查数据源状态" description="显示来源目录、版本年份与实际覆盖。"/><Card title="上传支持的数据文件"><UploadField accept=".json,.csv,.sqlite,.db" title="上传期刊数据快照" onUploaded={setAsset}/>{asset && <PreviewTool name="journal_import" title="导入期刊数据" preset={{asset_id:asset.asset_id}}/>}</Card><PreviewTool name="journal_build" title="构建本地数据库"/><PreviewTool name="journal_refresh" title="刷新支持的数据源"/></div>
    </>}
  </>;
}
