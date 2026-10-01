import { useState } from 'react';
import { useWorkbench } from '../context';
import { errorText, records } from '../api';
import type { Data } from '../types';
import { ToolPanel, UploadField } from '../components/ToolPanel';
import { ResultView } from '../components/Results';
import { Badge, Button, Card, Empty, Icon, Notice, PageHeading, Tabs } from '../components/ui';
export default function Documents() {
  const {execute,tools} = useWorkbench();const [tab,setTab] = useState('format');const [asset,setAsset] = useState<Data>();const [word,setWord] = useState<Data>();const [targets,setTargets] = useState<Data[]>([]);const [target,setTarget] = useState('');const [selection,setSelection] = useState<Data>();const [error,setError] = useState('');const [operation,setOperation] = useState('word_insert');
  async function inspect() {setError('');try{const data = await execute('word_status');setWord(data);if(data.available !== false){setTargets(records(await execute('word_targets'),'targets','documents'));}}catch(e){setError(errorText(e));}}
  async function lock() {setError('');try {const selected = await execute('word_select',{document_id:target},false,true);const id = selected.document_id || selected.target?.document_id || target;setTarget(id);setSelection(await execute('word_selection',{document_id:id}));}catch(e){setError(errorText(e));}}
  async function refreshSelection() {if(!target)return;try{setSelection(await execute('word_selection',{document_id:target}));}catch(e){setError(errorText(e));}}
  const fingerprint = selection?.selection_fingerprint || selection?.fingerprint;
  const wordAvailable = tools.find(t => t.name === 'word_insert')?.available;
  return <>
    <PageHeading eyebrow="DOCUMENT QUALITY, LOCAL FIRST" title="文档与格式" description="检查格式、生成独立 DOCX，或在具备真实能力的 Windows 环境里受控操作 Word / WPS。"/>
    {error && <Notice danger>{error}</Notice>}
    <Tabs active={tab} onChange={setTab} items={[{id:'format',title:'格式检查与副本'},{id:'generate',title:'生成文稿 / 三线表'},{id:'language',title:'语言风格'},{id:'standards',title:'学术标准'},{id:'word',title:'Word / WPS 桌面'}]}/>
    {tab === 'format' && <><Card title="上传待检查的 DOCX" description="只处理选中的受管文件，不提供任意系统路径访问。"><UploadField accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" title="选择学术文稿" onUploaded={setAsset}/></Card>{asset ? <div className="two-columns"><ToolPanel name="documents_audit" title="检查论文格式" description="检查真实标题层级、缩进、三线表、引用和标点缺陷。" preset={{asset_id:asset.asset_id}}/><ToolPanel name="documents_normalize" title="生成格式规范化副本" description="生成新文件，不覆盖上传原件。官方期刊 / 学校模板请先检查再决定。" preset={{asset_id:asset.asset_id}}/></div> : <Empty title="上传后开始格式检查" text="原件保留，规范化结果以新产物下载。"/>}</>}
    {tab === 'generate' && <><Notice>生成的是你填写的离线文稿，不会凭空补出实验结果。引用界面仅承诺现有字段生成和检查能力，不声称 Zotero 双向同步。</Notice><ToolPanel name="documents_generate" title="生成独立学术 DOCX" description="完整填写标题、摘要、关键词、章节、段落与三线表。所有表格使用结构化行列编辑。"/></>}
    {tab === 'language' && <ToolPanel name="documents_language_check" title="检查学术语言风格" description="识别套话、机械过渡和风格风险；不把风格扫描当作学术事实认证。"/>}
    {tab === 'standards' && <ToolPanel name="documents_standards" title="查看支持的学术标准" description="选择参考文献、结构、排版或国际标准分类。学校与期刊自己的模板优先。"/>}
    {tab === 'word' && <>
      <Card title="真实桌面能力" description="锁定明确文档与选区指纹，避免跟随窗口焦点误改活动稿件。" action={<Button variant="secondary" onClick={() => void inspect()}><Icon name="refresh" size={16}/>检查 Office 环境</Button>}>
        {!word ? <Empty title="尚未检测 Word / WPS" text="没有 Windows / Office 能力时，仍可使用上面的离线 DOCX 功能。"/> : <ResultView data={word}/>}
        {wordAvailable === false && <Notice>{tools.find(t => t.name === 'word_insert')?.unavailable_reason || '此环境没有可用的 Word / WPS 桌面能力。'}</Notice>}
        {!!targets.length && <div className="inline-form"><select aria-label="选择后端枚举的桌面文档" value={target} onChange={e => {setTarget(e.target.value);setSelection(undefined);}}><option value="">选择实际已打开的文档</option>{targets.map(document => <option key={document.document_id || document.id} value={document.document_id || document.id}>{document.title || document.name || document.document_id}</option>)}</select><Button disabled={!target} variant="secondary" onClick={() => void lock()}>锁定目标并读取选区</Button></div>}
      </Card>
      {wordAvailable && <Card title="打开受管文稿"><UploadField accept=".docx" title="上传要在桌面打开的文档" onUploaded={setAsset}/>{asset && <ToolPanel name="word_select" title="锁定上传文档" preset={{asset_id:asset.asset_id}} onResult={async data => {setTarget(data.document_id);setSelection(await execute('word_selection',{document_id:data.document_id}));}}/>}</Card>}
      {selection && fingerprint && <>
        <div className="two-columns"><Card title="已锁定目标与真实选区" action={<Badge tone="green">作用域已锁定</Badge>}><ResultView data={selection}/><Button variant="secondary" onClick={() => void refreshSelection()}>重新读取当前选区</Button></Card><Notice>每次桌面修改都需明确批准。若切换文档、选区或外部程序修改内容，旧指纹失效，服务会拒绝执行。默认建议先开启修订。</Notice></div>
        <Tabs active={operation} onChange={setOperation} items={[{id:'word_insert',title:'插入正文 / 标题'},{id:'word_replace',title:'替换选区'},{id:'word_comment',title:'添加批注'},{id:'word_track',title:'修订模式'},{id:'word_table',title:'三线表'},{id:'word_style',title:'正文样式'}]}/>
        <ToolPanel key={operation} name={operation} title={{word_insert:'插入学术内容',word_replace:'替换已核对的选区',word_comment:'添加原生审阅批注',word_track:'切换修订模式',word_table:'插入学术三线表',word_style:'应用正文样式'}[operation] || '操作文档'} preset={{document_id:target,selection_fingerprint:fingerprint}} onResult={() => void refreshSelection()}/>
      </>}
    </>}
  </>;
}
