import { expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ArtifactProvenance } from '../src/components/ArtifactProvenance';
it('任务成果显示真实metadata来源、执行者、项目版本和输入资产引用',() => {
  render(<ArtifactProvenance artifact={{id:'artifact-test',filename:'plan.docx',created_at:'2026-10-01',metadata:{source_action:'planning_export',origin:'web',project_id:'planning-test',project_revision:3,input_asset_refs:[{asset_id:'asset-test',sha256:'a'.repeat(64)}]}}}/>);
  expect(screen.getByText('来源动作：planning_export')).toBeInTheDocument();expect(screen.getByText('执行来源：网页操作')).toBeInTheDocument();expect(screen.getByText('项目：planning-test · 版本 3')).toBeInTheDocument();expect(screen.getByText(/asset-test/)).toHaveAttribute('title','a'.repeat(64));expect(screen.queryByText('来源未提供')).not.toBeInTheDocument();
});
it('兼容后端source_tool字段，不将有来源记录降级为未提供',() => {render(<ArtifactProvenance artifact={{metadata:{source_tool:'documents_normalize',origin:'nativeAgent'}}}/>);expect(screen.getByText('来源动作：documents_normalize')).toBeInTheDocument();expect(screen.getByText('执行来源：内置 Agent')).toBeInTheDocument();});
