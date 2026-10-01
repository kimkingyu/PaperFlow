import { useId } from 'react';
import type { Data, Schema } from '../types';
import { Button, Check, Icon, stateLabels } from './ui';
export const labels: Record<string,string> = {
  title:'标题',description:'说明',query:'检索词 / 名称',text:'正文 / 研究材料',goal:'研究目标',focus:'研究重点',constraints:'约束条件',resources:'资源',decisions:'方案决策',open_questions:'待解决问题',start_date:'开始日期',target_date:'目标日期',name:'名称',notes:'备注',source_ref:'来源引用（不会自动访问）',profile:'项目画像',plan:'研究规划',questions:'研究问题',experiments:'实验',evidence:'证据',tasks:'任务',id:'标识',question:'研究问题',hypothesis:'研究假设',baseline:'对照基线',minimum_change:'最小改动',continue_condition:'继续条件',stop_condition:'停止条件',status:'状态',evidence_ids:'证据标识',assessment_note:'结论评估说明',question_id:'关联研究问题',comparisons:'比较方案',metrics:'评价指标',protocol:'实验步骤',acceptance_condition:'验收条件',result_summary:'结果摘要',kind:'类型',summary:'摘要 / 解读',verification:'核验状态',verification_note:'核验说明',verified_by:'核验人',stage:'阶段',completion_condition:'完成条件',depends_on:'前置任务标识',experiment_id:'实验标识',due_date:'截止日期',artifact_refs:'产物引用',completion_note:'完成记录',blocked_reason:'受阻原因',project_id:'项目标识',task_id:'任务标识',updates:'任务更新',expected_revision:'当前项目版本',revision:'读取版本',change_note:'本次修订说明',experiment_ids:'关联实验',max_chars:'文字上限',limit:'返回数量',offset:'偏移',evidence_offset:'证据偏移',evidence_limit:'证据数量',research_question:'核心研究问题',context:'研究背景',language:'语言',sub_questions:'子问题',keywords:'关键词',own_materials:'实际用户材料',queries:'检索计划',purpose:'目的',question_ids:'关联问题标识',sources:'检索来源',year_from:'起始年份',year_to:'截止年份',paper_ids:'文献标识',source_text:'原始用户材料',input_id:'准备接口输入标识',per_query_limit:'每条检索数量',assessments:'适配 / 筛选判断',paper_id:'文献标识',metadata_sha256:'元数据指纹',relevance:'相关程度',reason:'判断理由',basis:'判断依据',limitations:'局限',selected_paper_ids:'选中文献标识',draft:'稿件',outline:'大纲',sections:'章节',level:'标题级别',paragraphs:'段落',citation_ids:'真实引用标识',own_material_ids:'实际材料标识',page_number:'物理页码',page_count:'读取页数',reading:'解读卡',claims:'论述',section:'论述章节',quote:'原文引用',fragment_id:'原文片段标识',file_sha256:'正文文件指纹',reading_id:'解读卡标识',asset_id:'已上传文件',asset_ids:'已上传文件',previous_asset_id:'前次事件快照文件',source_id:'数据源标识',dataset_id:'数据集 / 期刊标识',dry_run:'仅预览（不写入）',force:'允许明确指定的缩减',data_year:'数据年份',encoding:'编码',source_version:'来源版本',allow_shrink:'允许缩减快照',mode:'处理模式',preferences:'选刊偏好',candidate_records:'真实期刊候选',filters:'检索筛选',sort_by:'排序',journal_ids:'期刊标识',rank_system:'分区体系',rank_year:'分区年份',profile_id:'学校政策标识',warning_years:'预警年份',references:'参考文献',journal:'期刊名称',venue:'发表载体',issn:'ISSN',doi:'DOI',year:'年份',json_text:'投稿系统导出的事件 JSON',provider:'提供方',include_title:'显示稿件标题',category:'学术标准类别',standard_id:'格式标准',abstract:'摘要',is_chinese:'中文文稿',headers:'表头',rows:'表格行',caption:'表题',tables:'三线表',position:'写入位置',document_id:'已锁定文档',selection_fingerprint:'当前选区指纹',content:'内容',new_text:'替换文本',comment_text:'批注',author:'批注人',enable:'启用修订',heading_level:'标题级别',budget:'执行预算',max_read_papers:'最多阅读文献',batch_size:'每批篇数',max_rounds:'最多轮数',max_pages:'最多物理页',max_text_chars:'最多提取字符',max_search_calls:'检索次数（可为零）',max_download_attempts:'下载次数（可为零）',max_read_steps:'阅读步骤上限',max_gui_model_calls:'GUI 模型额度（可为零）',expected_loop_revision:'当前补读版本',expected_project_revision:'当前写作版本',action:'操作',request:'补读目标',review:'证据评审',context_fingerprint:'评审上下文指纹',action_id:'待执行动作标识',feedback:'阶段反馈',dimensions:'评审维度',dimension:'维度',gaps:'证据缺口',section_ids:'章节标识',recommendation:'建议',stop_reason:'停止原因',evidence_sufficient:'证据是否充分',material_ids:'材料标识',query_ids:'检索标识',note:'说明'
};
export function resolve(schema: Schema, root: Schema): Schema {
  if (schema.$ref) { const value = schema.$ref.split('/').slice(1).reduce<any>((acc,key) => acc?.[key], root); return value ? { ...value, ...schema, $ref: undefined } : schema; }
  return schema;
}
function variants(schema: Schema, root: Schema) { return (schema.anyOf || schema.oneOf || []).map(s => resolve(s,root)).filter(s => s.type !== 'null'); }
export function initialValue(schema: Schema, root: Schema = schema, depth = 0): any {
  const s = resolve(schema,root);
  if (s.default !== undefined && s.default !== null) return structuredClone(s.default);
  if (s.const !== undefined) return s.const;
  if (depth > 8) return undefined;
  const options = variants(s,root); if (options.length) return initialValue(options[0],root,depth + 1);
  if (s.type === 'object' || s.properties) return Object.fromEntries(Object.entries(s.properties || {}).filter(([key,value]) => { const field = resolve(value,root); return s.required?.includes(key) || (field.default !== undefined && field.default !== null) || (field.type === 'array' && field.default !== null && !field.minItems); }).map(([key,value]) => [key, initialValue(value,root,depth + 1)]));
  if (s.type === 'array') return Array.from({length:s.minItems || 0},() => initialValue(Array.isArray(s.items) ? s.items[0] : s.items || {},root,depth + 1));
  if (s.type === 'boolean') return false;
  if (s.type === 'integer' || s.type === 'number') return s.minimum ?? 0;
  if (s.enum) return s.enum[0];
  return '';
}
export function validate(schema: Schema, value: any, root: Schema = schema, path = ''): string[] {
  const s = resolve(schema,root); const errors: string[] = [];
  const options = variants(s,root);
  if (options.length) return options.map(option => validate(option,value,root,path)).find(e => !e.length) || options.flatMap(option => validate(option,value,root,path)).slice(0,5);
  if (s.type === 'object' || s.properties) {
    for (const key of s.required || []) if (value?.[key] === undefined || value?.[key] === null) errors.push(`${labels[key] || key}不能为空`);
    for (const [key,item] of Object.entries(value || {})) if (s.properties?.[key]) errors.push(...validate(s.properties[key],item,root,labels[key] || key));
  }
  if (typeof value === 'string') {
    if (s.minLength && value.trim().length < s.minLength) errors.push(`${path}至少需要 ${s.minLength} 个字符`);
    if (s.maxLength && value.length > s.maxLength) errors.push(`${path}超过文字上限`);
    if (s.pattern) { try { if (!(new RegExp(s.pattern)).test(value)) errors.push(`${path}格式不符合要求，请使用服务返回的标识或指纹`); } catch { /* Backend remains authoritative. */ } }
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (s.type === 'integer' && !Number.isInteger(value))) errors.push(`${path}必须是有效整数`);
    if (s.minimum !== undefined && value < s.minimum) errors.push(`${path}不能小于 ${s.minimum}`);
    if (s.maximum !== undefined && value > s.maximum) errors.push(`${path}不能大于 ${s.maximum}`);
  }
  if (Array.isArray(value)) {
    if (s.minItems && value.length < s.minItems) errors.push(`${path}至少需要 ${s.minItems} 项`);
    if (s.maxItems && value.length > s.maxItems) errors.push(`${path}最多 ${s.maxItems} 项`);
    value.forEach(item => errors.push(...validate(Array.isArray(s.items) ? s.items[0] : s.items || {},item,root,path)));
  }
  return errors;
}
export function SchemaEditor({ schema, root = schema, value, onChange, name = '', disabled = false, depth = 0, hidden = [], controlId }: { schema: Schema; root?: Schema; value: any; onChange: (v: any) => void; name?: string; disabled?: boolean; depth?: number; hidden?: string[]; controlId?: string }) {
  const uid = useId(); const inputId = controlId || uid; let s = resolve(schema,root);
  const options = variants(s,root);
  if (options.length) {
    const index = Math.max(0, options.findIndex(option => option.properties && Object.entries(option.properties).every(([key,field]) => field.const === undefined || field.const === value?.[key])));
    s = options[index] || options[0];
    if (options.length > 1 && depth > 0) return <div className="schema-group"><label htmlFor={inputId}>内容类型</label><select id={inputId} disabled={disabled} value={index} onChange={e => onChange(initialValue(options[Number(e.target.value)],root))}>{options.map((item,i) => <option key={i} value={i}>{labels[item.title || ''] || item.title || `类型 ${i + 1}`}</option>)}</select><SchemaEditor schema={s} root={root} value={value} onChange={onChange} name={name} disabled={disabled} depth={depth + 1}/></div>;
  }
  if (depth > 12) return <p className="muted">此嵌套结构过深，暂不可编辑。</p>;
  if (s.type === 'object' || s.properties) return <div className={depth ? 'schema-group' : 'schema-fields'}>{Object.entries(s.properties || {}).filter(([key]) => !hidden.includes(key)).map(([key,child]) => {
    const required = s.required?.includes(key); const enabled = value?.[key] !== undefined && value?.[key] !== null;
    const resolved = resolve(child,root); const effective = variants(resolved,root)[0] || resolved;
    const group = effective.type === 'object' || Boolean(effective.properties) || effective.type === 'array';
    const Wrapper = group ? 'fieldset' : 'div'; const Caption = group ? 'legend' : 'div'; const childId = uid + '-' + key;
    const caption = <>{labels[key] || child.title || key}{required && <span className="required"> *</span>}</>;
    return <Wrapper className="schema-field" key={key}><Caption className="field-caption">{group || !(required || enabled) ? <span>{caption}</span> : <label htmlFor={childId}>{caption}</label>}{!required && <button type="button" className="text-button" aria-label={`${enabled ? '不填写' : '添加'}${labels[key] || key}字段`} disabled={disabled} onClick={() => { const next = {...(value || {})}; if (enabled) delete next[key]; else next[key] = initialValue(child,root); onChange(next); }}>{enabled ? '不填写' : '添加'}</button>}</Caption>{(required || enabled) && <SchemaEditor schema={child} root={root} value={value?.[key]} onChange={v => onChange({...value,[key]:v})} name={key} controlId={childId} disabled={disabled} depth={depth + 1}/ >}{child.description && <small>{child.description}</small>}</Wrapper>;
  })}</div>;
  if (s.type === 'array') {
    const itemSchema = Array.isArray(s.items) ? s.items[0] : s.items || {};
    const array = Array.isArray(value) ? value : [];
    return <div className="schema-array">{array.map((item,i) => <div className="array-item" key={i}><div className="array-head"><span>第 {i + 1} 项</span><button type="button" className="icon-button" aria-label={`移除第 ${i + 1} 项`} disabled={disabled} onClick={() => onChange(array.filter((_,j) => i !== j))}><Icon name="close" size={15}/></button></div><SchemaEditor schema={itemSchema} root={root} value={item} onChange={v => onChange(array.map((old,j) => i === j ? v : old))} name={name} disabled={disabled} depth={depth + 1}/></div>)}<Button type="button" variant="secondary" disabled={disabled || (s.maxItems !== undefined && array.length >= s.maxItems)} onClick={() => onChange([...array,initialValue(itemSchema,root)])}><Icon name="plus" size={15}/>添加{labels[name] || '条目'}</Button></div>;
  }
  if (s.const !== undefined) return <input id={inputId} aria-label={labels[name] || name} value={String(s.const)} readOnly/>;
  if (s.enum) return <select id={inputId} aria-label={labels[name] || name} disabled={disabled} value={value ?? s.enum[0]} onChange={e => onChange(typeof s.enum![0] === 'number' ? Number(e.target.value) : e.target.value)}>{s.enum.map(item => <option key={String(item)} value={item}>{stateLabels[item] || labels[item] || item}</option>)}</select>;
  if (s.type === 'boolean') return <Check id={inputId} disabled={disabled} label={labels[name] || '启用'} checked={Boolean(value)} onChange={onChange}/>;
  if (s.type === 'integer' || s.type === 'number') return <input id={inputId} type="number" aria-label={labels[name] || name} disabled={disabled} min={s.minimum} max={s.maximum} step={s.type === 'integer' ? 1 : 'any'} value={value ?? ''} onChange={e => onChange(e.target.value === '' ? undefined : Number(e.target.value))}/>;
  const isText = ['text','summary','question','hypothesis','goal','focus','protocol','research_question','context','reason','content','new_text','comment_text','source_text','json_text','request','assessment_note','result_summary','completion_note','verification_note'].includes(name) || (s.maxLength || 0) > 3000;
  const inputProps = { id: inputId, 'aria-label': labels[name] || name, disabled, value: value ?? '', maxLength: s.maxLength, onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(e.target.value) };
  if (isText) return <textarea {...inputProps} rows={name === 'json_text' ? 7 : 3}/>;
  return <input {...inputProps} type={name.endsWith('_date') ? 'date' : 'text'}/>;
}
export function ObjectEditor({ schema, value, onChange }: { schema: Schema; value: Data; onChange: (v: Data) => void }) { return <SchemaEditor schema={schema} value={value} onChange={onChange}/>; }
