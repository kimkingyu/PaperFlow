import { useCallback, useEffect, useState } from 'react';
import { useWorkbench } from '../context';
import { errorText, records } from '../api';
import type { Data } from '../types';
import { SchemaEditor, validate } from '../components/SchemaForm';
import { ActionResult, ResultView } from '../components/Results';
import { ToolPanel, UploadField } from '../components/ToolPanel';
import { Badge, Button, Card, Empty, Field, Icon, Notice, PageHeading, Tabs } from '../components/ui';
export default function Papers() {
  const {execute,tools,workspace,link,askAgent} = useWorkbench();const [tab,setTab] = useState('library');const [papers,setPapers] = useState<Data[]>([]);const [search,setSearch] = useState<Data>();
  const [selected,setSelected] = useState<Data>();const [detail,setDetail] = useState<Data>();const [read,setRead] = useState<Data>();const [asset,setAsset] = useState<Data>();const [error,setError] = useState('');const [busy,setBusy] = useState(false);const [page,setPage] = useState(1);
  const [card,setCard] = useState<Data>();const [saved,setSaved] = useState<Data>();
  const notesTool = tools.find(tool => tool.name === 'papers_notes');const readingSchema = notesTool?.input_schema.properties?.reading;
  const list = useCallback(async () => {try {setPapers(records(await execute('papers_list',{limit:50,offset:0})));}catch(e){setError(errorText(e));}},[execute]);
  useEffect(() => {if (tools.some(t => t.name === 'papers_list')) void list();},[tools,list]);
  useEffect(() => {setSelected(undefined);setDetail(undefined);setRead(undefined);setCard(undefined);},[workspace?.id]);
  async function choose(paper:Data) {setSelected(paper);setRead(undefined);setCard(undefined);setError('');try {setDetail(await execute('papers_details',{paper_id:paper.paper_id}));}catch(e){setError(errorText(e));}}
  async function readPages(cursor?: Data) {if (!selected) return;setBusy(true);setError('');try {const data = await execute('papers_read',{paper_id:selected.paper_id,page_number:cursor?.page_number || page,page_count:3,offset:cursor?.offset || 0,max_chars:20000});setRead(data);setPage(cursor?.page_number || page);setCard(old => old && old.file_sha256 === data.file_sha256 ? old : {paper_id:selected.paper_id,file_sha256:data.file_sha256,summary:'',claims:[]});}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  async function saveCard() {if (!selected || !card || !readingSchema || !notesTool) return;const errors = validate(readingSchema,card,notesTool.input_schema);if(errors.length){setError(errors.slice(0,8).join('；'));return;}setBusy(true);setError('');try {setSaved(await execute('papers_notes',{paper_id:selected.paper_id,reading:card}));}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  function cite(fragment:Data) {if (!card) return;setCard({...card,claims:[...card.claims,{section:'relevance',kind:'unverified',text:'',evidence:[{fragment_id:fragment.fragment_id,page_number:fragment.page_number,quote:(fragment.text || fragment.quote || '').slice(0,1000),match_method:'exact'}]}]});setTab('reading');}
  const fragments = read?.fragments || read?.pages?.flatMap((p:Data) => p.fragments || []) || [];
  const visible = tab === 'search' ? records(search,'papers') : papers;
  return <>
    <PageHeading eyebrow="LITERATURE & TRACEABLE EVIDENCE" title="文献库" description="检索真实记录，合法获取全文。提取文字、部分阅读与全文理解始终分开呈现。" action={<Button variant="secondary" disabled={!selected} onClick={() => askAgent(`请阅读选中文献 ${selected?.paper_id}，先检查合法全文和原文覆盖，保存带实际片段和物理页码的解读卡。`)}><Icon name="agent" size={17}/>交给 Agent 解读</Button>}/>
    {error && <Notice danger>{error}</Notice>}
    <Tabs active={tab} onChange={setTab} items={[{id:'library',title:'本地文献'},{id:'search',title:'真实检索'},{id:'import',title:'导入 PDF'},{id:'reading',title:'阅读与解读卡'}]}/>
    {tab === 'search' && <ToolPanel name="papers_search" title="检索学术文献" description="按来源显示实际请求状态。检索元数据和摘要不等于理解正文。" onResult={setSearch} result={false}/>}
    {tab === 'import' && <div className="two-columns"><Card title="导入有权使用的 PDF"><UploadField accept=".pdf,application/pdf" title="上传本地论文 PDF" onUploaded={setAsset}/>{asset && <ToolPanel name="papers_import" title="导入到文献库" preset={{asset_id:asset.asset_id}} onResult={data => {void list();const paper = data.paper || data; if(paper.paper_id) {void link('paper',paper.paper_id).catch(e => setError(errorText(e)));void choose(paper);}}}/>}</Card><Notice>仅导入你有权使用的文件。扫描件、公式和图表的读取能力以实际覆盖结果为准，不承诺 OCR 或图表完整理解。</Notice></div>}
    {['library','search'].includes(tab) && <div className="library-layout"><Card title={tab === 'search' ? '检索候选' : '工作区文献'} description="点击文献查看来源与获取状态" action={<Button variant="ghost" onClick={() => void list()}><Icon name="refresh" size={16}/></Button>}>
      {search && tab === 'search' && <div className="source-status"><ResultView data={search.source_statuses || search.search_statuses || search.coverage}/></div>}
      {!visible.length ? <Empty title={tab === 'search' ? '尚无检索记录' : '当前工作区没有文献'} text="先检索或导入 PDF。已有文献可通过任务成果页发现并关联。"/> : <div className="paper-list">{visible.map(paper => <button key={paper.paper_id} onClick={() => void choose(paper)} className={`paper-card ${selected?.paper_id === paper.paper_id ? 'selected' : ''}`}><div><Badge>{paper.source_id || '本地'}</Badge>{paper.year && <span>{paper.year}</span>}</div><h3>{paper.title || '未提供标题'}</h3><p>{paper.authors?.slice(0,3).join('、') || '作者未提供'}</p><small>{paper.venue || paper.doi || paper.arxiv_id || paper.paper_id}</small></button>)}</div>}
    </Card><Card title="文献详情" description="来源、版本与合法全文状态">{!selected ? <Empty title="选择一篇文献" text="详情和正文阅读都从真实文献标识开始。"/> : <><h3 className="paper-title">{selected.title}</h3><div className="row-actions"><Button variant="secondary" onClick={() => {setTab('reading');void readPages();}}>读取正文</Button><Button variant="secondary" disabled={!workspace} onClick={async () => {try {await link('paper',selected.paper_id);await list();}catch(e){setError(errorText(e));}}}>关联到工作区</Button></div><ResultView data={detail}/><ToolPanel name="papers_download" title="尝试合法公开全文获取" description="没有合法公开全文时会明确返回不可获取，不绕过付费墙。" preset={{paper_id:selected.paper_id}} onResult={setDetail}/></>}</Card></div>}
    {tab === 'reading' && (!selected ? <Empty title="先选择文献" text="在本地文献或检索结果中选中一篇，再读取正文。"/> : <>
      <div className="reading-banner"><div><Badge tone="violet">部分分页阅读</Badge><strong>{selected.title}</strong></div><div className="inline-form"><Field label="物理页码"><input aria-label="阅读物理页码" type="number" min={1} max={5000} value={page} onChange={e => setPage(Number(e.target.value))}/></Field><Button disabled={busy} onClick={() => void readPages()}>读取 3 页</Button>{read?.next_cursor && <Button variant="secondary" disabled={busy} onClick={() => void readPages(read.next_cursor)}>继续读取</Button>}</div></div>
      <div className="two-columns reading-columns"><Card title="原文与物理页定位" description="只将实际返回片段作为证据。分页结束不保证图表和扫描页完整。">
        {!read ? <Empty title="尚未读取正文"/> : <><Notice><ResultView data={read.coverage}/></Notice>{fragments.length ? fragments.map((fragment:Data) => <article className="evidence-fragment" key={fragment.fragment_id}><div><Badge>物理页 {fragment.page_number}</Badge><small>{fragment.fragment_id}</small></div><p>{fragment.text || fragment.quote}</p><Button variant="ghost" disabled={!card} onClick={() => cite(fragment)}><Icon name="plus" size={15}/>添加为论述证据</Button></article>) : <ResultView data={read.pages || read}/>}</>}
      </Card><Card title="有证据的解读卡" description="作者陈述、解读推断和未核实项分开。服务严格校验原文出处。">
        {!card || !readingSchema || !notesTool ? <Empty title="读取正文后开始解读"/> : <><SchemaEditor schema={readingSchema} root={notesTool.input_schema} value={card} onChange={setCard}/><Button disabled={busy} onClick={() => void saveCard()}>保存解读卡</Button><ActionResult data={saved}/></>}
      </Card></div>
    </>)}
  </>;
}
