import { expect, test, type Page } from '@playwright/test';
import { Buffer } from 'node:buffer';
// No HTTP mocking here: isolated ApplicationServices, real validation and managed files.
const token = 'isolated-real-backend-token';
async function actualAction(page:Page,workspaceId:string,action:string,params:Record<string,unknown>) {
  const result = await page.request.post('/api/workbench/action',{headers:{'X-PaperFlow-Token':token},data:{action,params,workspace_id:workspaceId,allow_network:false,approved:false}});
  const envelope = await result.json();expect(result.ok(),JSON.stringify(envelope)).toBe(true);expect(envelope.status,JSON.stringify(envelope)).not.toBe('error');return envelope.data;
}
function isolatedPdf() {
  const text = 'BT /F1 12 Tf 50 740 Td (This is an isolated test paper. It contains no research results.) Tj ET';
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>','<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>','<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',`<< /Length ${Buffer.byteLength(text)} >>\nstream\n${text}\nendstream`];
  let pdf = '%PDF-1.4\n';const offsets:number[] = [];
  objects.forEach((object,i) => {offsets.push(Buffer.byteLength(pdf));pdf += `${i + 1} 0 obj\n${object}\nendobj\n`;});
  const xref = Buffer.byteLength(pdf);pdf += 'xref\n0 6\n0000000000 65535 f \n' + offsets.map(offset => String(offset).padStart(10,'0') + ' 00000 n \n').join('') + `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}
async function createWorkspace(page:Page,title:string) {
  await page.goto('/?token=' + token + '#/home');
  await page.getByLabel('工作区名称').fill(title);
  await page.getByLabel('研究方向 / 说明').fill('端到端隔离验收，仅使用测试输入，不读取用户资料。');
  await page.getByRole('button',{name:'创建工作区',exact:true}).click();
  await expect(page.getByRole('heading',{name:title + '，从这里继续'})).toBeVisible();
}
test('真实规划：创建、结构化任务、revision保护、证据与DOCX导出',async ({page}) => {
  await createWorkspace(page,'真实服务规划验收');
  await page.getByRole('navigation').getByRole('link',{name:'科研规划',exact:true}).click();
  await page.getByRole('tab',{name:'新建 / 准备'}).click();
  await page.getByLabel('标题',{exact:true}).fill('真实服务研究规划');
  await page.getByLabel('研究目标',{exact:true}).fill('验证结构化规划与实际DOCX导出，不产生实验结果。');
  await page.getByRole('button',{name:'创建科研规划',exact:true}).click();
  await expect(page.getByRole('heading',{name:'结构化规划编辑'})).toBeVisible();
  await page.getByRole('button',{name:'添加任务',exact:true}).click();
  await page.getByLabel('标识',{exact:true}).fill('task-browser-test');
  await page.getByLabel('正文 / 研究材料',{exact:true}).fill('核对研究方案');
  await page.getByLabel('完成条件',{exact:true}).fill('保留核对记录，未经实验不宣称结果。');
  await page.getByRole('button',{name:'保存规划版本'}).click();
  await expect(page.locator('.project-toolbar').getByText('版本 2',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'证据记录'}).click();
  await page.getByLabel('标识',{exact:true}).fill('evidence-browser-test');
  await page.getByLabel('类型',{exact:true}).selectOption('note');
  await page.getByLabel('来源引用（不会自动访问）',{exact:true}).fill('用户提供的测试记录');
  await page.getByLabel('摘要 / 解读',{exact:true}).fill('这是测试笔记，不是测量数据或已经验证的结论。');
  await page.getByRole('button',{name:'记录有来源的证据'}).click();
  await expect(page.locator('.project-toolbar').getByText('版本 3',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'历史与导出'}).click();
  await page.getByRole('button',{name:'导出独立规划 DOCX',exact:true}).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button',{name:/下载 .*\.docx/}).click();
  const download = await downloadPromise;expect(download.suggestedFilename()).toMatch(/\.docx$/);
  await expect(page.getByRole('alert')).toHaveCount(0);
});
test('真实写作：实际PDF解读、引用段落、版本保存与独立导出',async ({page}) => {
  await createWorkspace(page,'真实服务写作验收');
  const workspaceId = await page.getByLabel('当前工作区').inputValue();
  const uploaded = await page.request.post('/api/workbench/assets?workspace_id=' + workspaceId,{headers:{'X-PaperFlow-Token':token,'X-File-Name':'isolated-test.pdf','Content-Type':'application/pdf'},data:isolatedPdf()});
  expect(uploaded.ok()).toBe(true);const asset = (await uploaded.json()).data;
  const imported = await actualAction(page,workspaceId,'papers_import',{asset_id:asset.asset_id,title:'仅测试的真实PDF文献'});const paperId = imported.paper?.paper_id || imported.paper_id;
  const reading = await actualAction(page,workspaceId,'papers_read',{paper_id:paperId,page_number:1,page_count:1,offset:0,max_chars:20000});
  const fragment = reading.fragments[0];expect(fragment.text).toContain('no research results');
  await actualAction(page,workspaceId,'papers_notes',{paper_id:paperId,reading:{paper_id:paperId,file_sha256:reading.file_sha256,summary:'仅用于测试的实际原文解读。',claims:[{section:'relevance',kind:'author_claim',text:'文件声明自身是隔离测试文献，未提供研究结果。',evidence:[{fragment_id:fragment.fragment_id,page_number:fragment.page_number,quote:fragment.text.slice(0,200),match_method:'exact'}]}]}});
  const project = await actualAction(page,workspaceId,'writing_create',{profile:{title:'仅测试的研究提案',research_question:'如何验证真实PDF证据与写作引用边界？'},paper_ids:[paperId]});
  await page.getByRole('navigation').getByRole('link',{name:'文献写作',exact:true}).click();await page.getByRole('button',{name:'刷新列表'}).click();await page.getByLabel('文献写作项目').selectOption(project.project_id);
  await expect(page.locator('.project-toolbar').getByText('版本 1',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'候选筛选'}).click();await page.getByLabel(/^相关性 /).selectOption('core');await page.getByLabel('筛选理由').fill('此文献用于核对测试原文的实际引用链，不作为科学结论。');await page.getByLabel('判断依据').selectOption('metadata');await page.getByRole('button',{name:'保存筛选与选中文献'}).click();await expect(page.locator('.project-toolbar').getByText('版本 2',{exact:true})).toBeVisible();
  const evidence = await actualAction(page,workspaceId,'writing_get',{project_id:project.project_id});const citationId = evidence.evidence_matrix[0].citation_id;expect(citationId).toMatch(/^cite-/);
  await page.getByRole('tab',{name:'大纲与草稿'}).click();
  await page.getByRole('button',{name:'添加章节',exact:true}).click();await page.getByLabel('标识',{exact:true}).fill('section-browser-test');await page.getByLabel('标题',{exact:true}).nth(1).fill('测试原文边界');
  await page.getByRole('button',{name:'添加段落',exact:true}).click();await page.getByLabel('类型',{exact:true}).selectOption('literature_summary');await page.getByLabel('正文 / 研究材料',{exact:true}).fill('该文件明确说明其为隔离测试文献，未提供研究结果。');await page.getByRole('button',{name:'添加真实引用标识',exact:true}).click();await page.getByLabel('真实引用标识',{exact:true}).fill(citationId);
  await page.getByRole('button',{name:'保存新草稿版本'}).click();await expect(page.locator('.project-toolbar').getByText('版本 3',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'修订与导出'}).click();
  await page.getByRole('button',{name:'导出有出处的 DOCX'}).click();
  const downloadPromise = page.waitForEvent('download');await page.getByRole('button',{name:/下载 .*\.docx/}).click();
  expect((await downloadPromise).suggestedFilename()).toMatch(/\.docx$/);
  await expect(page.getByRole('alert')).toHaveCount(0);
});
test('真实文档：生成、上传原件、格式审计、规范化新副本',async ({page},testInfo) => {
  await createWorkspace(page,'真实服务文档验收');
  await page.getByRole('navigation').getByRole('link',{name:'文档与格式',exact:true}).click();
  await page.getByRole('tab',{name:'生成文稿 / 三线表'}).click();
  await page.getByLabel('标题',{exact:true}).fill('格式验收独立文稿');
  await page.getByRole('button',{name:'添加章节',exact:true}).click();
  await page.getByLabel('标题',{exact:true}).nth(1).fill('研究背景');
  await page.getByRole('button',{name:'添加段落',exact:true}).click();
  await page.getByLabel('段落',{exact:true}).fill('这是用户提供的隔离测试文字，不是真实实验结论。');
  await page.getByRole('button',{name:'生成独立学术 DOCX',exact:true}).click();
  const downloadPromise = page.waitForEvent('download');await page.getByRole('button',{name:/下载 .*\.docx/}).click();
  const download = await downloadPromise;const file = testInfo.outputPath('generated.docx');await download.saveAs(file);
  await page.getByRole('tab',{name:'格式检查与副本'}).click();
  await page.locator('input[type=file]').setInputFiles(file);
  await expect(page.getByText(/已上传：/)).toBeVisible();
  await page.getByRole('button',{name:'检查论文格式',exact:true}).click();
  await expect(page.locator('.action-result').first()).toBeVisible();
  await page.getByRole('button',{name:'生成格式规范化副本',exact:true}).click();
  const normalizedPromise = page.waitForEvent('download');await page.getByRole('button',{name:/下载 normalized-document\.docx/}).click();
  expect((await normalizedPromise).suggestedFilename()).toBe('normalized-document.docx');
  await expect(page.getByRole('alert')).toHaveCount(0);
});
