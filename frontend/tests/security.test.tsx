import { expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ResultView } from '../src/components/Results';
import { RawData } from '../src/components/ui';
import { publicMessage } from '../src/pages/Agent';
it('正文与模型响应只能作为React安全文本，不执行原始HTML',() => {const hostile = '<img src=x onerror="window.hacked=true"><script>alert(1)</script>';const {container} = render(<ResultView data={{text:hostile}}/>);expect(screen.getByText(hostile)).toBeInTheDocument();expect(container.querySelector('script')).toBeNull();expect(container.querySelector('img')).toBeNull();});
it('结果不显示密钥、任意文件路径与模型隐藏推理字段',() => {render(<ResultView data={{title:'真实产物',api_key:'private-model-key',file_path:'private-document-path',thinking:'hidden-chain',reasoning:'hidden-chain',text:'公开执行摘要'}}/>);expect(screen.queryByText('private-model-key')).not.toBeInTheDocument();expect(screen.queryByText('private-document-path')).not.toBeInTheDocument();expect(screen.queryByText('hidden-chain')).not.toBeInTheDocument();expect(screen.getByText('公开执行摘要')).toBeInTheDocument();});
it('高级响应查看也不会回显模型Key',() => {const {container} = render(<RawData data={{api_key:'private-model-key',token:'private-token'}}/>);expect(container.textContent).not.toContain('private-model-key');expect(container.textContent).not.toContain('private-token');});
it('助手只显示公开文字，过滤显式隐藏推理片段',() => {expect(publicMessage({role:'assistant',text:'<think>秘密思维链</think>已完成实际工具调用。'})).toBe('已完成实际工具调用。');expect(publicMessage({role:'assistant',reasoning:'不得展示'})).toBe('');expect(publicMessage({role:'user',text:'<think>用户引用原文</think>'})).toContain('用户引用原文');});
