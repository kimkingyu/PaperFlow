import { expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ToolPanel } from '../src/components/ToolPanel';
const execute = vi.hoisted(() => vi.fn().mockResolvedValue({project_id:'planning-test',revision:1}));
// The shape matches ApplicationServices' planning_create / Pydantic ProjectProfile contract.
const schema = {
  type:'object',required:['profile'],properties:{profile:{$ref:'#/$defs/ProjectProfile'}},
  $defs:{ProjectProfile:{type:'object',required:['title','goal'],properties:{
    title:{type:'string',minLength:1,maxLength:300},goal:{type:'string',minLength:1,maxLength:4000},focus:{type:'string',default:'',maxLength:4000},
    constraints:{type:'array',items:{type:'string'}},resources:{type:'array',items:{$ref:'#/$defs/ResourceItem'}},decisions:{type:'array',items:{type:'string'}},open_questions:{type:'array',items:{type:'string'}},
    start_date:{anyOf:[{type:'string'},{type:'null'}],default:null},target_date:{anyOf:[{type:'string'},{type:'null'}],default:null}
  }},ResourceItem:{type:'object',required:['name'],properties:{name:{type:'string',minLength:1},status:{type:'string',enum:['confirmed','unverified'],default:'unverified'}}}}
};
const tools = [{name:'planning_create',description:'创建科研规划',input_schema:schema,available:true,network:false,desktop:false,mutates:true,approval_required:false}];
vi.mock('../src/context',() => ({useWorkbench:() => ({loading:false,execute,tools})}));
it('planning_create可用标题/研究目标label准确定位，每个原生控件实际接收关联id',async () => {
  const {container} = render(<ToolPanel name="planning_create" title="创建科研规划"/>);
  const title = screen.getByLabelText('标题') as HTMLInputElement;
  const goal = screen.getByLabelText('研究目标') as HTMLTextAreaElement;
  expect(title.tagName).toBe('INPUT');expect(goal.tagName).toBe('TEXTAREA');
  expect(Array.from(title.labels || []).some(label => label.htmlFor === title.id && label.textContent?.includes('标题'))).toBe(true);
  expect(Array.from(goal.labels || []).some(label => label.htmlFor === goal.id && label.textContent?.includes('研究目标'))).toBe(true);
  expect(screen.getByRole('group',{name:/项目画像/})).toHaveProperty('tagName','FIELDSET');
  fireEvent.change(title,{target:{value:'真实规划标题'}});fireEvent.change(goal,{target:{value:'可检验的研究目标'}});
  fireEvent.click(screen.getByRole('button',{name:'添加资源'}));
  fireEvent.change(screen.getByLabelText('名称'),{target:{value:'实际计算资源'}});
  fireEvent.change(screen.getByLabelText('状态'),{target:{value:'confirmed'}});
  for(const label of container.querySelectorAll<HTMLLabelElement>('label[for]')) {
    const control = document.getElementById(label.htmlFor);expect(control).not.toBeNull();expect(['INPUT','TEXTAREA','SELECT']).toContain(control?.tagName);
  }
  fireEvent.click(screen.getByRole('button',{name:'创建科研规划'}));
  await waitFor(() => expect(execute).toHaveBeenCalledWith('planning_create',{profile:{title:'真实规划标题',goal:'可检验的研究目标',focus:'',constraints:[],resources:[{name:'实际计算资源',status:'confirmed'}],decisions:[],open_questions:[]}},false,false));
});
