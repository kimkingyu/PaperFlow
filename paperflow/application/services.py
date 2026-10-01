"""Shared web/native-agent business services. No model, shell, or MCP execution."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from pydantic import ValidationError
from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.models import JournalError, response
from paperflow.engine.literature.service import LiteratureService
from paperflow.engine.literature.models import PaperRecord
from paperflow.engine.literature.writing_service import PaperWritingService
from paperflow.engine.literature.reading_loop_service import ReadingLoopService
from paperflow.engine.planning.service import ResearchPlanner
from paperflow.engine.planning.models import PlanningError
from .schemas import contracts
from .storage import ManagedFiles, WorkspaceStore, ident, MIMES
from .execution import ExecutionCoordinator

DESCRIPTIONS = {
 'overview':'查看期刊数据覆盖与快照', 'search':'检索本地期刊', 'details':'期刊详情', 'check':'检查预警与学校政策',
 'compare':'对比期刊', 'prepare':'提取选刊材料及画像契约', 'recommend':'依据材料与真实来源推荐期刊', 'read_project':'读取上传项目说明',
 'papers_search':'联网检索真实文献', 'papers_details':'文献详情及获取状态', 'papers_download':'获取合法公开全文', 'papers_read':'分页提取正文证据',
 'papers_notes':'严格校验并保存解读卡', 'papers_import':'导入上传的合法 PDF', 'papers_list':'列出工作区文献',
 'writing_prepare':'准备文献写作研究契约', 'writing_create':'创建文献写作项目', 'writing_search':'执行项目多查询', 'writing_assess':'保存候选相关度判断',
 'writing_get':'读取项目与证据矩阵', 'writing_list':'列出写作项目', 'writing_materials':'取得有界正文与草稿契约', 'writing_draft':'严格校验并保存文献支持草稿', 'writing_export':'复验引用后导出独立 DOCX',
 'loop_get':'补读状态', 'loop_control':'控制补读及预算', 'loop_prepare_review':'准备补读评审材料', 'loop_submit_review':'提交来源明确的评审', 'loop_step':'执行指定补读步骤', 'loop_feedback':'保存补读反馈',
 'planning_prepare':'准备研究规划契约', 'planning_create':'创建科研规划', 'planning_get':'读取科研规划版本', 'planning_list':'列出科研规划', 'planning_save':'校验保存完整规划版本', 'planning_task':'更新任务及依赖', 'planning_evidence':'记录来源证据', 'planning_export':'导出研究规划 DOCX',
 'journal_sources':'数据源及能力状态', 'journal_build':'预览或构建期刊数据库', 'journal_import':'预览或导入上传数据', 'journal_refresh':'预览或刷新支持的数据源', 'journal_peers':'从参考文献分析候选期刊', 'submission_track':'离线解析投稿事件和快照差异',
 'documents_standards':'列出学术标准', 'documents_audit':'审计上传 DOCX 格式', 'documents_normalize':'生成格式规范化副本', 'documents_generate':'生成离线学术 DOCX', 'documents_language_check':'检查学术语言风格',
 'word_status':'检测运行中的 Word/WPS', 'word_targets':'枚举可锁定的桌面文档', 'word_select':'锁定明确目标文档', 'word_selection':'读取目标选区及校验指纹', 'word_insert':'向锁定文档插入文本', 'word_replace':'替换已确认选区', 'word_comment':'添加原生批注', 'word_track':'切换修订', 'word_table':'插入学术三线表', 'word_style':'应用明确的学术样式',
}
READ_ONLY = {'overview','search','details','check','compare','prepare','recommend','read_project','papers_details','papers_list','writing_prepare','writing_get','writing_list','writing_materials','loop_get','loop_prepare_review','planning_prepare','planning_get','planning_list','journal_sources','journal_peers','submission_track','documents_standards','documents_audit','documents_language_check','word_status','word_targets','word_selection'}
NETWORK = {'papers_search','papers_download','writing_search','journal_refresh'}
DESKTOP_WRITES = {'word_select','word_insert','word_replace','word_comment','word_track','word_table','word_style'}


def sanitize(value, roots=()):
    if isinstance(value, dict):
        return {k: sanitize(v, roots) for k,v in value.items() if k not in {'path','paths','file_path','output_path','pdf_path','directory','database_path','document_path','target_path'} and not k.endswith('_path')}
    if isinstance(value, list):
        return [sanitize(v, roots) for v in value]
    if isinstance(value, str):
        for root in roots:
            value = value.replace(str(root), '[managed]').replace(str(root).replace('\\','/'), '[managed]')
    return value


class ApplicationServices:
    def __init__(self, finder=None, literature=None, writing=None, loop=None, data_dir=None):
        self.finder = finder if finder is not None else JournalFinder(data_dir=str(data_dir) if data_dir else None)
        self.directory = Path(data_dir).expanduser().resolve() if data_dir else self.finder.store.directory / 'application'
        self.directory.mkdir(parents=True, exist_ok=True)
        from paperflow.engine.journals.store import default_data_dir
        isolated = bool(data_dir) or self.finder.store.directory.resolve() != default_data_dir().resolve()
        business_root = Path(data_dir).expanduser().resolve() if data_dir else self.finder.store.directory
        paper_dir = business_root / 'papers' if isolated else Path(os.environ.get('PAPERFLOW_PAPER_HOME') or str(self.finder.store.directory / 'papers'))
        writing = writing if writing is not None else getattr(loop,'writing',None)
        literature = literature if literature is not None else getattr(writing,'literature',None)
        self.literature = literature if literature is not None else LiteratureService(data_dir=str(paper_dir))
        self.writing = writing if writing is not None else PaperWritingService(data_dir=str(paper_dir), literature=self.literature)
        self.loop = loop if loop is not None else ReadingLoopService(data_dir=str(paper_dir), writing=self.writing)
        self.planning = ResearchPlanner(data_dir=str(business_root / 'research') if isolated else None)
        self.workspaces = WorkspaceStore(self.directory, self._validate_target)
        self.execution = ExecutionCoordinator(self.workspaces)
        self._agent_context = threading.local()
        self._operation_context = threading.local()
        self.workspaces.mutation_guard = lambda w: self.execution.check(w, getattr(self._operation_context,'owner',None))
        self.assets = ManagedFiles(self.workspaces)
        self.assets.provenance_provider = lambda w: getattr(self._operation_context,'provenance',None) or {'source_tool':'upload','origin':'web','run_id':None,'workspace_id':w}
        self.files = self.assets
        self.artifacts = self.assets
        self._schemas = contracts()
        self._word_bridge = None
        self._word_lock = threading.RLock()
        self._word_targets = {}
        self._word_selected = None

    def acquire_execution(self, project_id, owner):
        # The injected loop service is authoritative for its own execution target.
        # The production loop.get delegates to writing.get, preserving real-ID checks.
        result = self.loop.get(ident(project_id))
        if result.get('status') == 'error' or not isinstance(result.get('data'),dict) or result['data'].get('project_id') != project_id:
            raise JournalError(result.get('error_code') or 'PROJECT_NOT_FOUND',result.get('message') or '执行项目不存在')
        with self.workspaces.connect() as db:
            workspaces = [row[0] for row in db.execute("SELECT workspace_id FROM links WHERE kind='writing' AND target_id=?",(project_id,))]
        for workspace in workspaces:
            self.execution.check(workspace,owner)
        return response(self.execution.acquire(project_id,owner))

    def ensure_execution(self, project_id, owner):
        import time
        with self.workspaces.connect() as db:
            row = db.execute('SELECT owner,expires_at FROM execution_leases WHERE project_id=?',(ident(project_id),)).fetchone()
        if not row or row['owner'] != owner or row['expires_at'] <= time.time():
            raise JournalError('EXECUTION_LOST','项目执行权已丢失，执行器必须停止')
        return self.acquire_execution(project_id,owner)

    def release_execution(self, project_id, owner):
        with self.workspaces.connect() as db:
            db.execute('DELETE FROM execution_leases WHERE project_id=? AND owner=?',(ident(project_id),owner))
        return response({'project_id':project_id,'released':True})

    def _scope_snapshot(self, workspace_id):
        workspace = self.workspaces.get(workspace_id)
        def files(kind):
            return sorted([{kind+'_id':item[kind+'_id'],'sha256':item['sha256']} for item in self.assets.list(workspace_id,kind)],key=lambda item:item[kind+'_id'])
        materials = {}
        for link in workspace['links']:
            kind, target = link['kind'], link['target_id']
            data = self._validate_target(kind,target)['data']
            if kind == 'paper':
                materials[(kind,target)] = self._paper_material(data)
            else:
                materials[(kind,target)] = self._project_material(kind,data)
                if kind == 'writing':
                    for candidate in data.get('candidates',[]):
                        paper_id = candidate['paper_id']
                        materials[('paper',paper_id)] = self._paper_material(self.literature.get(paper_id)['data'])
        return {'workspace_id':workspace_id,'links':workspace['links'],'assets':files('asset'),'artifacts':files('artifact'),'materials':[materials[key] for key in sorted(materials)]}

    @staticmethod
    def _hash(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')).hexdigest()

    def _project_material(self,kind,data):
        payload = data['plan'] if kind=='planning' else {key:data.get(key) for key in ('profile','source_text','queries','assessments','selected_paper_ids','draft','draft_metadata_sha256')}
        return {'kind':kind,'target_id':data['project_id'],'revision':data['revision'],'sha256':self._hash(payload),'paper_ids':sorted(c['paper_id'] for c in data.get('candidates',[]))}

    def _card_material(self,card):
        return {'reading_id':card['reading_id'],'origin':card['origin'],'created_at':card['created_at'],'sha256':self._hash(card['card'])}

    def _paper_material(self,data):
        metadata={key:value for key,value in data.items() if key in PaperRecord.model_fields}
        return {'kind':'paper','target_id':data['paper_id'],'metadata_sha256':self._hash(metadata),'identity_sha256':self._hash({k:v for k,v in metadata.items() if k!='fulltext_locations'}),'file_sha256':(data.get('acquisition') or {}).get('file_sha256'),'reading_cards':sorted([self._card_material(c) for c in data.get('reading_cards',[])],key=lambda c:c['reading_id'])}

    def _check_scope(self,workspace_id,run_id):
        expected=self.execution.scope(run_id)
        if expected is None or self._scope_snapshot(workspace_id)!=expected:
            raise JournalError('SCOPE_CHANGED','工作区材料或解读卡在运行外发生变化；需重新授权')
        return expected

    def _receipt_nodes(self,value):
        if isinstance(value,dict):
            yield value
            for key,item in value.items():
                if key not in {'sources','related_identifiers','agent_contract','profile','draft','plan'}:
                    yield from self._receipt_nodes(item)
        elif isinstance(value,list):
            for item in value:
                yield from self._receipt_nodes(item)

    def _accept_actor_scope(self,name,params,result,workspace_id,run_id,before,step_context=None):
        import copy
        expected=copy.deepcopy(before)
        material={(row['kind'],row['target_id']):row for row in expected['materials']}
        data=result.get('data') or {}
        successful=result.get('status')!='error'
        nodes=list(self._receipt_nodes(data))
        introduced=set()
        if successful and name in {'planning_create','writing_create'}:
            introduced.add((name.split('_')[0],data['project_id']))
        if successful and name in {'papers_search','papers_import','writing_search','loop_step'}:
            for node in nodes:
                if {'paper_id','source_id','source_record','title'}<=node.keys():
                    introduced.add(('paper',node['paper_id']))
        expected['links']=sorted(expected['links']+[{'kind':kind,'target_id':target} for kind,target in introduced if {'kind':kind,'target_id':target} not in expected['links']],key=lambda link:(link['kind'],link['target_id']))
        if successful and name.startswith('planning_') and name not in READ_ONLY and 'plan' in data:
            material[('planning',data['project_id'])]=self._project_material('planning',data)
        project=data.get('project') if name.startswith('loop_') else data
        if successful and name.startswith(('writing_','loop_')) and name not in READ_ONLY and isinstance(project,dict) and 'profile' in project and 'project_id' in project:
            material[('writing',project['project_id'])]=self._project_material('writing',project)
        metadata_allowed=name in {'papers_search','papers_import','writing_search'} or name=='loop_step' and (step_context or {}).get('kind')=='search'
        for node in nodes:
            if {'paper_id','source_id','source_record','title'}<=node.keys() and metadata_allowed and successful:
                key=('paper',node['paper_id'])
                original=material.get(key,{'kind':'paper','target_id':node['paper_id'],'file_sha256':None,'reading_cards':[]})
                projected=self._paper_material(node)
                original['metadata_sha256']=projected['metadata_sha256']
                original['identity_sha256']=projected['identity_sha256']
                if name=='papers_import':
                    original['file_sha256']=(node.get('acquisition') or {}).get('file_sha256')
                material[key]=original
        if successful and name=='papers_download':
            key=('paper',params['paper_id'])
            receipt=self._paper_material(data)
            if receipt['identity_sha256']!=material[key]['identity_sha256']:
                raise JournalError('SCOPE_CHANGED','下载期间文献身份或正文之外的材料发生改变')
            material[key]['metadata_sha256']=receipt['metadata_sha256']
            material[key]['file_sha256']=(data.get('acquisition') or {}).get('file_sha256')
        if successful and name=='loop_step' and (step_context or {}).get('kind')=='download':
            receipt=data.get('step_result') or {}
            target=(step_context.get('payload') or {}).get('paper_id')
            if receipt.get('downloaded') and 'file_sha256' not in receipt:
                raise JournalError('NEEDS_RECONCILIATION','下载动作缺少核心文件哈希收据，未授权盲刷新')
            if 'file_sha256' in receipt:
                material[('paper',target)]['file_sha256']=receipt['file_sha256']
            downloaded_paper=receipt.get('download_receipt')
            if isinstance(downloaded_paper,dict) and downloaded_paper.get('paper_id')==target:
                metadata=self._paper_material(downloaded_paper)
                if metadata['identity_sha256']!=material[('paper',target)]['identity_sha256']:
                    raise JournalError('SCOPE_CHANGED','补读下载期间文献身份改变')
                material[('paper',target)]['metadata_sha256']=metadata['metadata_sha256']
        cards=[data] if name=='papers_notes' else [data['saved_reading']] if name=='loop_feedback' and data.get('saved_reading') else []
        for card in cards if successful else []:
            if not {'reading_id','origin','card','created_at'}<=card.keys() or not isinstance(card['card'],dict):
                raise JournalError('NEEDS_RECONCILIATION','解读动作缺少真实保存收据')
            paper_id=card['card'].get('paper_id')
            if name=='papers_notes' and paper_id!=params['paper_id']:
                raise JournalError('SCOPE_DENIED','真实解读收据与本次动作目标不符')
            key=('paper',paper_id)
            if key not in material:
                raise JournalError('SCOPE_DENIED','真实解读收据指向未授权文献')
            entries={item['reading_id']:item for item in material[key]['reading_cards']}
            entries[card['reading_id']]=self._card_material(card)
            material[key]['reading_cards']=sorted(sorted(entries.values(),key=lambda item:item['created_at'],reverse=True)[:20],key=lambda item:item['reading_id'])
        for node in nodes:
            if 'artifact_id' in node and 'sha256' in node:
                entry={'artifact_id':node['artifact_id'],'sha256':node['sha256']}
                if entry not in expected['artifacts']:
                    expected['artifacts'].append(entry)
        expected['artifacts']=sorted(expected['artifacts'],key=lambda item:item['artifact_id'])
        expected['materials']=[material[key] for key in sorted(material)]
        actual=self._scope_snapshot(workspace_id)
        if expected!=actual:
            raise JournalError('SCOPE_CHANGED','动作后检测到无法归属该动作的材料变化，未扩大授权范围')
        self.execution.save_scope(run_id,workspace_id,expected)
        return expected

    def agent_start(self, workspace_id, run_id, scope=None):
        owner = 'agent:'+ident(run_id)
        self.workspaces.get(workspace_id)
        self.execution.acquire(workspace_id,owner)
        try:
            snapshot = self._scope_snapshot(workspace_id)
            previous = self.execution.scope(run_id)
            if (scope is not None and snapshot != scope) or (previous is not None and snapshot != previous):
                raise JournalError('SCOPE_CHANGED','工作区材料授权范围已改变，请重新授权后开始新运行')
            self.execution.save_scope(run_id,workspace_id,snapshot)
            for item in snapshot['links']:
                if item['kind']=='writing':
                    self.acquire_execution(item['target_id'],owner)
            self._agent_context.run_id = run_id
            self._agent_context.workspace_id = workspace_id
            return response(snapshot)
        except Exception:
            self.execution.release(owner)
            raise

    def agent_scope_snapshot(self, workspace_id, run_id):
        self.execution.ensure(workspace_id,'agent:'+ident(run_id))
        return response(self._check_scope(workspace_id,run_id))

    def agent_recover(self, recovered_run_ids):
        if not isinstance(recovered_run_ids,list) or len(recovered_run_ids)>1000:
            raise JournalError('INVALID_INPUT','恢复运行编号列表无效')
        reports=self.execution.recover([ident(value) for value in recovered_run_ids])
        return response({'recovered':reports,'needs_reconciliation':any(row['needs_reconciliation'] for row in reports)})

    def agent_before_model(self, workspace_id, run_id):
        self.execution.ensure(workspace_id,'agent:'+ident(run_id))
        self._check_scope(workspace_id,run_id)
        self._agent_context.run_id = run_id
        self._agent_context.workspace_id = workspace_id
        if self.execution.pending(run_id):
            raise JournalError('NEEDS_RECONCILIATION','已有未结算模型预留，不能重发真实请求')
        plans = []
        for project_id in self.execution.engaged(run_id):
            self.workspaces.require(workspace_id,'writing',project_id)
            self.ensure_execution(project_id,'agent:'+run_id)
            state=self.loop.get(project_id)
            if state.get('status')=='error':
                raise JournalError(state.get('error_code') or 'LOOP_ERROR',state.get('message') or '补读状态无效')
            data=state['data']
            loop=data['loop']
            action=loop.get('next_action') or {}
            if loop.get('status') not in ('awaiting_review','awaiting_assessment','awaiting_interpretation','awaiting_revision','ready') or action.get('kind') not in ('review','assessment','interpretation','revision'):
                continue
            budget,usage,reserved=loop['budget'],loop['usage'],loop['reserved']
            if reserved['gui_model_calls']:
                raise JournalError('NEEDS_RECONCILIATION','补读已有未结算请求，不能自动再次发送模型请求')
            if usage['gui_model_calls']+reserved['gui_model_calls']>=budget['max_gui_model_calls']:
                raise JournalError('BUDGET_EXHAUSTED','补读模型调用预算已耗尽，未发送模型请求')
            plans.append((data,action))
        reservations=[]
        try:
            for data,action in plans:
                # Recheck both revisions before delegating to the existing engine ticket ledger.
                current=self.loop.get(data['project_id'])['data']
                if current['project_revision']!=data['project_revision'] or current['loop_revision']!=data['loop_revision']:
                    raise JournalError('REVISION_CONFLICT','补读双版本已改变，未发送模型请求')
                result=self.loop.reserve_model_call(data['project_id'],action['action_id'],data['project_revision'],data['loop_revision'])
                if result.get('status')=='error':
                    raise JournalError(result.get('error_code') or 'MODEL_BUDGET_ERROR',result.get('message') or '模型预算预留失败')
                reservations.append({'project_id':data['project_id'],'action_id':action['action_id'],'model_ticket':result['data']['model_ticket']})
                self.execution.reserve(run_id,result,action['action_id'])
                if result['data']['project_revision'] != data['project_revision']:
                    raise JournalError('REVISION_CONFLICT','项目版本在预留中改变，未发送模型请求')
        except Exception:
            rollback = getattr(self.loop,'release_unissued_model_call',None)
            for reservation in reservations:
                if rollback is None:
                    raise JournalError('NEEDS_RECONCILIATION','未发出请求的预留未能回退；核心缺少取消预留能力') from None
                released = rollback(reservation['project_id'],reservation['action_id'],reservation['model_ticket'])
                if released.get('status')=='error':
                    raise JournalError('NEEDS_RECONCILIATION','未发出请求的模型预留回退失败，需要核对') from None
                self.execution.release_unissued(reservation['model_ticket'],{'provider_dispatched':False,'released':True})
            raise
        return response({'reservations':reservations,'budget_accounting':'补读 legacy gui_model_calls 为同一次真实 provider 请求的项目内预算镜像，不是额外模型调用或费用'})

    def agent_after_model(self, run_id, success):
        if type(success) is not bool:
            raise JournalError('INVALID_INPUT','模型结算必须提供真实布尔成功状态')
        results=[]
        for pending in self.execution.pending(ident(run_id)):
            result=self.loop.settle_model_call(pending['project_id'],pending['action_id'],pending['ticket'],success)
            if result.get('status')=='error':
                raise JournalError(result.get('error_code') or 'NEEDS_RECONCILIATION',result.get('message') or '补读请求结算失败')
            self.execution.settled(pending['ticket'],{'success':success,'project_id':pending['project_id'],'loop_revision':result['data']['loop_revision']})
            results.append(sanitize(result))
        return response({'settled':results,'provider_request_success':success})

    def agent_release(self, run_id):
        pending=self.execution.pending(ident(run_id))
        self.execution.release('agent:'+run_id)
        if getattr(self._agent_context,'run_id',None)==run_id:
            self._agent_context.run_id=None
            self._agent_context.workspace_id=None
        if pending:
            return response({'pending_count':len(pending)},status='error',error_code='NEEDS_RECONCILIATION',message='真实请求状态仍需核对；未自动结算或重发')
        return response({'released':True})

    def _validate_target(self, kind, target_id):
        try:
            result = {'planning':self.planning.get,'writing':self.writing.get,'paper':self.literature.get}[kind](target_id)
        except PlanningError as exc:
            raise JournalError(exc.code,str(exc)) from None
        if result.get('status') == 'error' or result.get('data') is None:
            raise JournalError(result.get('error_code') or 'NOT_FOUND', result.get('message') or '关联对象不存在')
        return result

    def _word_available(self):
        if sys.platform != 'win32':
            return False, 'Word/WPS COM 仅支持 Windows'
        try:
            if importlib.util.find_spec('win32com') is None or importlib.util.find_spec('pythoncom') is None:
                return False, '尚未安装 pywin32'
        except (ImportError, ValueError):
            return False, '尚未安装 pywin32'
        try:
            if self._word_bridge is None:
                from paperflow.engine.word_live_bridge import WordLiveBridge
                self._word_bridge = WordLiveBridge()
            connected, message = self._word_bridge.connect()
            return bool(connected), '' if connected else message
        except Exception:
            return False, 'Word/WPS COM 检测失败或桌面不可访问'

    def describe_tools(self):
        desktop, reason = self._word_available()
        result = []
        for name, model in self._schemas.items():
            category = ('journals' if name in {'overview','search','details','check','compare','prepare','recommend','read_project'} or name.startswith('journal_') else name.split('_')[0])
            result.append({'name':name,'description':DESCRIPTIONS[name],'input_schema':model.model_json_schema(), 'category':category,
                           'mutates':name not in READ_ONLY,'network':name in NETWORK or name == 'loop_step', 'desktop':name.startswith('word_'),
                           'approval_required':name in DESKTOP_WRITES or name in {'journal_build','journal_import','journal_refresh'},
                           'available':desktop if name.startswith('word_') else importlib.util.find_spec('pypdf') is not None if name=='papers_read' else importlib.util.find_spec('docx') is not None if name in {'planning_export','writing_export','documents_audit','documents_normalize','documents_generate'} else True,
                           'unavailable_reason':reason if name.startswith('word_') else '未安装 PDF 解析依赖' if name=='papers_read' and importlib.util.find_spec('pypdf') is None else '未安装 python-docx' if name in {'planning_export','writing_export','documents_audit','documents_normalize','documents_generate'} and importlib.util.find_spec('docx') is None else '',
                           'network_conditional':name=='loop_step'})
        return result

    def capabilities(self):
        available, reason = self._word_available()
        return response({'tools':self.describe_tools(),'platform':sys.platform,'desktop':{'available':available,'unavailable_reason':reason},'backend_calls_llm':False,'managed_upload_limit':32*1024*1024,'managed_upload_types':{extension:sorted(types) for extension,types in MIMES.items()},'environment':{'journals_initialized':self.finder.store.path.exists(),'planning_initialized':self.planning.store.path.exists(),'literature_initialized':self.literature.store.path.exists(),'pdf_parser_available':importlib.util.find_spec('pypdf') is not None}})

    def execute(self, name, arguments, *, workspace_id=None, origin='web', allow_network=False, approved=False):
        if name not in self._schemas:
            raise JournalError('UNKNOWN_TOOL', '未知或禁用的业务工具')
        if origin not in ('web','nativeAgent','native_agent','agent','calling_agent','gui_model') or type(allow_network) is not bool or type(approved) is not bool:
            raise JournalError('INVALID_INPUT', '执行上下文无效')
        if not isinstance(arguments, dict):
            raise JournalError('INVALID_INPUT', '工具参数必须为对象')
        try:
            encoded = json.dumps(arguments, ensure_ascii=False,allow_nan=False)
            if len(encoded.encode('utf-8')) > 2*1024*1024:
                raise JournalError('INPUT_TOO_LARGE', '请求超过 2 MiB')
            params = self._schemas[name].model_validate_json(encoded, strict=True).model_dump(mode='json',exclude_unset=True)
        except (ValidationError,TypeError,ValueError,RecursionError) as exc:
            if isinstance(exc,JournalError):
                raise
            raise JournalError('INVALID_INPUT','工具参数不符合能力目录 schema') from None
        discover = params.pop('discover',False) if name in {'papers_list','writing_list','planning_list'} else False
        if discover and origin != 'web':
            raise JournalError('SCOPE_DENIED','旧对象发现仅限人工网页操作；Agent 只能读取当前工作区')
        if workspace_id:
            self.workspaces.get(workspace_id)
        elif not discover and name not in {'overview','search','details','check','compare','journal_sources','documents_standards','word_status','word_targets'}:
            raise JournalError('WORKSPACE_REQUIRED','请先选择工作区')
        if discover:
            return self._discover(name,params)
        project_id = params.get('project_id')
        if project_id:
            self.workspaces.require(workspace_id,'planning' if name.startswith('planning_') else 'writing',project_id)
        if 'paper_id' in params:
            self.workspaces.require(workspace_id,'paper',params['paper_id'])
        for field in ('paper_ids','selected_paper_ids'):
            for paper_id in params.get(field) or []:
                self.workspaces.require(workspace_id,'paper',paper_id)
        for item in params.get('assessments') or []:
            if 'paper_id' in item:
                self.workspaces.require(workspace_id,'paper',item['paper_id'])
        needs_network = name in NETWORK
        if name == 'loop_step':
            current = self.loop.get(project_id)
            data = current.get('data') or {}
            next_action = data.get('next_action') or (data.get('loop') or {}).get('next_action') or {}
            needs_network = next_action.get('kind') in {'search','download'}
        if needs_network and not allow_network:
            raise JournalError('NETWORK_NOT_AUTHORIZED','本次操作需要明确联网授权')
        if (name in DESKTOP_WRITES or name in {'journal_build','journal_import','journal_refresh'} and params.get('dry_run',True) is False) and not approved:
            raise JournalError('APPROVAL_REQUIRED','请确认该动作的修改范围')
        source = 'nativeAgent' if origin in ('nativeAgent','native_agent','agent') else 'gui_model' if origin == 'gui_model' else 'calling_agent'
        previous_owner = getattr(self._operation_context,'owner',None)
        previous_provenance = getattr(self._operation_context,'provenance',None)
        run_id = getattr(self._agent_context,'run_id',None) if source == 'nativeAgent' else None
        owner = 'agent:'+run_id if run_id else None
        self._operation_context.owner = owner
        self._operation_context.provenance = {'source_tool':name,'origin':source if owner else origin,'run_id':run_id,'workspace_id':workspace_id,'project_id':project_id,'project_revision':params.get('revision',params.get('expected_revision',params.get('expected_project_revision'))),'input_asset_ids':([params['asset_id']] if params.get('asset_id') else [])+list(params.get('asset_ids') or [])}
        try:
            self._operation_context.provenance['input_asset_refs'] = [{'asset_id':asset_id,'sha256':self.assets.get(asset_id,workspace_id,'asset')['sha256']} for asset_id in self._operation_context.provenance['input_asset_ids']]
            if owner and workspace_id != getattr(self._agent_context,'workspace_id',None):
                raise JournalError('SCOPE_DENIED','原生运行不能切换到其他工作区')
            before_scope = None
            step_context = None
            if owner and workspace_id:
                self.execution.ensure(workspace_id,owner)
                before_scope = self._check_scope(workspace_id,run_id)
                if name=='loop_step':
                    step_context=((self.loop.get(project_id).get('data') or {}).get('loop') or {}).get('next_action')
            if workspace_id and name not in READ_ONLY:
                self.execution.check(workspace_id,owner)
                if owner:
                    self.execution.acquire(workspace_id,owner)
            if project_id and name.startswith(('writing_','loop_')) and name not in READ_ONLY:
                self.execution.check(project_id,owner)
                if owner:
                    self.acquire_execution(project_id,owner)
            if owner and name.startswith('loop_'):
                self.execution.engage(run_id,project_id)
            result = self._execute(name, params, workspace_id, source)
            if not isinstance(result,dict) or 'status' not in result:
                result = response(result)
            if workspace_id and result.get('status') != 'error':
                if name in {'planning_create','writing_create'}:
                    self.workspaces.link(workspace_id,name.split('_')[0],result['data']['project_id'])
                    if owner and name == 'writing_create':
                        self.acquire_execution(result['data']['project_id'],owner)
                if name in {'papers_search','papers_import','writing_search','loop_step'}:
                    self._bind_papers(result.get('data'),workspace_id)
            if owner:
                self._accept_actor_scope(name,params,result,workspace_id,run_id,before_scope,step_context)
            return sanitize(result,(self.directory,self.finder.store.directory,*[v['identity'] for v in self._word_targets.values()]))
        except PlanningError as exc:
            raise JournalError(exc.code,str(exc)) from None
        finally:
            self._operation_context.owner = previous_owner
            self._operation_context.provenance = previous_provenance

    def _bind_papers(self,value,workspace_id):
        if isinstance(value,dict):
            paper_id = value.get('paper_id')
            if isinstance(paper_id,str) and ({'source_id','source_record','title'}<=value.keys() or 'metadata_sha256' in value):
                self.workspaces.link(workspace_id,'paper',paper_id)
            for key,item in value.items():
                if key not in {'sources','related_identifiers','agent_contract','profile','draft','plan'}:
                    self._bind_papers(item,workspace_id)
        elif isinstance(value,list):
            for item in value:
                self._bind_papers(item,workspace_id)

    def _scoped_list(self, kind, workspace_id, params):
        links = [r['target_id'] for r in self.workspaces.get(workspace_id)['links'] if r['kind']==kind]
        offset,limit = params.get('offset',0),params.get('limit',20)
        # Fetch only linked identities, never a global list subsequently trimmed.
        items = [self._summary(kind,self._validate_target(kind,i)['data']) for i in links[offset:offset+limit]]
        return response({'papers' if kind=='paper' else 'projects':items,'total':len(links),'limit':limit,'offset':offset},coverage={'workspace_scoped':True})

    @staticmethod
    def _summary(kind,item):
        allowed = {'paper_id','project_id','revision','title','stage','created_at','updated_at','venue','year','source_id','doi','arxiv_id','publication_type'}
        result = {key:value for key,value in item.items() if key in allowed}
        if kind != 'paper':
            result['title'] = (item.get('profile') or (item.get('plan') or {}).get('profile') or {}).get('title',result.get('title',''))
        return result

    def _discover(self,name,params):
        kind = {'planning_list':'planning','writing_list':'writing','papers_list':'paper'}[name]
        result = {'planning':self.planning.list_projects,'writing':self.writing.list,'paper':self.literature.list}[kind](**params)
        key = 'papers' if kind=='paper' else 'projects'
        data = result['data']
        data[key] = [self._summary(kind,item) for item in data.get(key,[])]
        return sanitize(result,(self.directory,self.finder.store.directory,self.planning.store.directory))

    def _asset_params(self, params, workspace_id, extensions=None):
        p = dict(params)
        asset_id = p.pop('asset_id',None)
        if asset_id:
            p['file_path'] = str(self.assets.resolve(asset_id,workspace_id,extensions=extensions))
        return p

    def _export(self, name, params, workspace_id):
        filename = 'research-plan.docx' if name == 'planning_export' else 'literature-draft.docx'
        file_id, path = self.assets.allocate(workspace_id,filename)
        try:
            fn = self.planning.export if name == 'planning_export' else self.writing.export_docx
            result = fn(**params,output_path=str(path),overwrite=False)
            if result.get('status') == 'error':
                return result
            artifact = self.assets.register(workspace_id,file_id,filename,'application/vnd.openxmlformats-officedocument.wordprocessingml.document','artifact',metadata={'project_id':result['data']['project_id'],'project_revision':result['data']['revision']})
            result['data'].update(artifact)
            return result
        finally:
            # Unregistered partial output is not a downloadable artifact.
            with self.workspaces.connect() as db:
                registered = db.execute('SELECT 1 FROM files WHERE id=?',(file_id,)).fetchone()
            if not registered:
                path.unlink(missing_ok=True)

    def _execute(self,name,p,w,origin):
        if name in ('planning_export','writing_export'):
            return self._export(name,p,w)
        if name in ('papers_list','planning_list','writing_list'):
            return self._scoped_list({'papers_list':'paper','planning_list':'planning','writing_list':'writing'}[name],w,p)
        if name.startswith('planning_'):
            p = self._asset_params(p,w,{'.txt','.md','.docx','.pdf'})
            methods = {'prepare':'prepare','create':'create','get':'get','save':'save','task':'update_task','evidence':'record_evidence'}
            return getattr(self.planning,methods[name[9:]])(**p)
        if name == 'papers_import':
            asset = self.assets.get(p['asset_id'],w,'asset')
            params = self._asset_params(p,w,{'.pdf'})
            params['title'] = params.get('title') or asset['filename']
            return self.literature.import_pdf(**params)
        if name.startswith('papers_'):
            method = {'search':'search','details':'get','download':'download','read':'read','notes':'save_reading'}[name[7:]]
            if name == 'papers_notes':
                p = {**p,'origin':origin,'strict':True}
            return getattr(self.literature,method)(**p)
        if name.startswith('writing_'):
            method = {'prepare':'prepare','create':'create','search':'search','assess':'assess','get':'get','materials':'prepare_writing','draft':'save_draft'}[name[8:]]
            if name == 'writing_assess':
                p = {**p,'origin':origin}
            return getattr(self.writing,method)(**p)
        if name.startswith('loop_'):
            method = {'get':'get','control':'control','prepare_review':'prepare_review','submit_review':'submit_review','step':'step','feedback':'apply_feedback'}[name[5:]]
            if name in ('loop_submit_review','loop_feedback'):
                p = {**p,'origin':origin}
            return getattr(self.loop,method)(**p)
        if name.startswith('word_'):
            return self._desktop(name,p,w)
        if name.startswith('documents_'):
            return self._document(name,p,w)
        if name == 'journal_sources':
            return self.finder.list_sources(**p)
        if name == 'journal_build':
            asset_ids = p.pop('asset_ids',None)
            if asset_ids is not None:
                p['input_paths'] = [str(self.assets.resolve(i,w,extensions={'.json','.csv','.db','.sqlite','.sqlite3'})) for i in asset_ids]
            return self.finder.build_database(**p)
        if name == 'journal_import':
            return self.finder.import_data(**self._asset_params(p,w,{'.json','.csv','.db','.sqlite','.sqlite3','.txt','.md'}))
        if name == 'journal_refresh':
            return self.finder.refresh(**p)
        if name == 'journal_peers':
            return self.finder.peers(**self._asset_params(p,w,{'.json'}))
        if name == 'submission_track':
            text = p.pop('json_text','')
            if text and p.get('asset_id'):
                raise JournalError('INVALID_INPUT','投稿 JSON 文本与文件二选一')
            p = self._asset_params(p,w,{'.json'})
            previous = p.pop('previous_asset_id',None)
            if previous:
                p['previous_file_path'] = str(self.assets.resolve(previous,w,extensions={'.json'}))
            if text:
                try:
                    p['data'] = json.loads(text,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except ValueError:
                    raise JournalError('INVALID_INPUT','投稿事件 JSON 无效') from None
            return self.finder.tracker(**p)
        if name in {'prepare','recommend','read_project'}:
            p = self._asset_params(p,w,{'.txt','.md','.docx','.pdf'})
            if p.get('text') and p.get('file_path'):
                raise JournalError('INVALID_INPUT','材料文本与文件二选一')
            return (self.finder.recommend if name=='recommend' else self.finder.prepare_manuscript)(**p)
        from paperflow.gui.actions import dispatch
        return dispatch(self.finder,name,p,literature=self.literature,writing=self.writing,loop=self.loop)

    def _document(self,name,p,w):
        from paperflow.engine.standards_manager import list_standards, get_standard_by_id
        if name == 'documents_standards':
            return response(list_standards(**p))
        if name == 'documents_language_check':
            from paperflow.engine.anti_ai_cleaner import AntiAICleaner
            return response(AntiAICleaner().analyze(p['text']))
        if 'standard_id' in p and get_standard_by_id(p['standard_id']) is None:
            raise JournalError('INVALID_INPUT','未知学术标准')
        if name == 'documents_audit':
            from paperflow.engine.formatter import PaperFormatAuditor
            return response(PaperFormatAuditor.audit_docx(str(self.assets.resolve(p['asset_id'],w,extensions={'.docx'})),standard_id=p.get('standard_id','chinese_thesis_standard')))
        file_id,path = self.assets.allocate(w,'academic-document.docx' if name == 'documents_generate' else 'normalized-document.docx')
        filename = 'academic-document.docx' if name == 'documents_generate' else 'normalized-document.docx'
        try:
            if name == 'documents_normalize':
                from paperflow.engine.formatter import PaperFormatNormalizer
                result = PaperFormatNormalizer.normalize_docx(str(self.assets.resolve(p['asset_id'],w,extensions={'.docx'})),str(path),standard_id=p.get('standard_id','chinese_thesis_standard'))
                if result.get('success') is False:
                    raise JournalError('DOCUMENT_ERROR','文档规范化未完成')
            else:
                from paperflow.engine.docx_builder import AcademicDocxBuilder
                builder = AcademicDocxBuilder(is_chinese=p.get('is_chinese',True))
                builder.add_title(p['title'])
                builder.add_abstract(p.get('abstract',''),p.get('keywords',[]))
                for section in p['sections']:
                    builder.add_heading(section['title'],section.get('level',1))
                    for text in section.get('paragraphs',[]):
                        builder.add_paragraph_with_citations(text)
                    for table in section.get('tables',[]):
                        self._validate_table(table)
                        builder.add_three_line_table(table['headers'],table['rows'],table.get('caption',''))
                builder.save(str(path))
                result = {'document_type':'offline_academic_document'}
            artifact = self.assets.register(w,file_id,filename,'application/vnd.openxmlformats-officedocument.wordprocessingml.document','artifact')
            return response({**sanitize(result),**artifact})
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_table(p):
        if not 1 <= len(p['headers']) <= 30 or len(p['rows']) > 500 or any(len(row)!=len(p['headers']) for row in p['rows']):
            raise JournalError('INVALID_INPUT','表格行宽须与表头一致且数量有界')

    def _bridge(self):
        available,reason = self._word_available()
        if not available:
            raise JournalError('DESKTOP_UNAVAILABLE',reason)
        if self._word_bridge is None:
            from paperflow.engine.word_live_bridge import WordLiveBridge
            self._word_bridge = WordLiveBridge()
        return self._word_bridge

    def _desktop(self,name,p,w):
        with self._word_lock:
            bridge = self._bridge()
            if name == 'word_status':
                ok,message = bridge.connect()
                if not ok:
                    raise JournalError('DESKTOP_UNAVAILABLE',message)
                return response({'connected':True,'app_type':bridge._app_type})
            if name == 'word_targets':
                def enumerate_docs():
                    ok,msg = bridge._connect_on_com_thread()
                    if not ok:
                        raise JournalError('DESKTOP_UNAVAILABLE',msg)
                    targets=[]
                    for i in range(1,bridge._word_app.Documents.Count+1):
                        doc=bridge._word_app.Documents(i)
                        identity=str(doc.FullName)
                        key=next((k for k,v in self._word_targets.items() if v['identity']==identity),None) or 'document-'+uuid.uuid4().hex
                        self._word_targets.setdefault(key,{'identity':identity,'workspace_id':None})
                        targets.append({'document_id':key,'name':str(doc.Name)})
                    return response(targets)
                return bridge._call(enumerate_docs)
            if name == 'word_select':
                if bool(p.get('document_id')) == bool(p.get('asset_id')):
                    raise JournalError('INVALID_INPUT','请选择一个桌面文档或上传文档')
                if p.get('asset_id'):
                    # Never alter uploaded source: open a separately managed editable copy.
                    import shutil
                    asset=self.assets.get(p['asset_id'],w,'asset')
                    source=self.assets.resolve(p['asset_id'],w,extensions={'.docx'})
                    key,path=self.assets.allocate(w,'desktop-copy.docx')
                    shutil.copyfile(source,path)
                    artifact=self.assets.register(w,key,'desktop-copy.docx',asset['media_type'],'artifact')
                    document_id='document-'+uuid.uuid4().hex
                    self._word_targets[document_id]={'identity':str(path),'workspace_id':w}
                    bridge.select_document(str(path))
                    self._guard_desktop(bridge,document_id,w,select=True)
                else:
                    document_id=p['document_id']
                    target=self._word_targets.get(document_id)
                    if not target:
                        raise JournalError('TARGET_NOT_FOUND','请重新枚举桌面文档')
                    if target['workspace_id'] not in (None,w):
                        raise JournalError('SCOPE_DENIED','该桌面目标已属于其他工作区')
                    self._guard_desktop(bridge,document_id,w,select=True)
                    self._word_targets[document_id]['workspace_id']=w
                    artifact=None
                self._word_selected=document_id
                return response({'document_id':document_id,'artifact':artifact,'selected':True})
            document_id=p['document_id']
            if self._word_selected != document_id:
                raise JournalError('TARGET_NOT_LOCKED','目标未锁定或已被其他操作切换')
            def operation():
                doc=self._guard_desktop(bridge,document_id,w)
                selection=bridge._word_app.Selection
                fingerprint=hashlib.sha256(json.dumps([document_id,int(selection.Start),int(selection.End),str(selection.Text)],ensure_ascii=False).encode()).hexdigest()
                if name != 'word_selection' and fingerprint != p['selection_fingerprint']:
                    raise JournalError('SELECTION_CHANGED','选区已改变，请重新读取并审批')
                if name == 'word_selection':
                    return response({'document_id':document_id,'selection_fingerprint':fingerprint,'has_selection':selection.End>selection.Start,'text':bridge._clean(selection.Text),'start':selection.Start,'end':selection.End})
                if name in ('word_replace','word_comment') and selection.End<=selection.Start:
                    raise JournalError('NO_SELECTION','需要明确选中的文本')
                # Public bridge methods must execute in the existing COM thread, atomically
                # with identity/fingerprint verification; do not submit recursively.
                original_call=bridge._call
                original_active=getattr(bridge,'_active_doc',None)
                bridge._call=lambda fn: fn()
                # The legacy bridge can fall back to a duplicate basename. Reuse its
                # formatting functions, but never its permissive target resolver.
                bridge._active_doc=lambda: self._guard_desktop(bridge,document_id,w)
                try:
                    if name != 'word_track':
                        doc.TrackRevisions=True
                    args={k:v for k,v in p.items() if k not in {'document_id','selection_fingerprint'}}
                    if name == 'word_insert':
                        args['text']=args.pop('content')
                    if name == 'word_table':
                        self._validate_table(args)
                    fn={'word_insert':'insert_text','word_replace':'replace_selection','word_comment':'add_comment','word_track':'set_track_revisions','word_table':'insert_academic_table','word_style':'apply_academic_preset'}[name]
                    result=getattr(bridge,fn)(**args)
                    return response(result)
                finally:
                    bridge._call=original_call
                    if original_active is None:
                        del bridge._active_doc
                    else:
                        bridge._active_doc=original_active
            return bridge._call(operation)

    def _guard_desktop(self,bridge,document_id,workspace_id,select=False):
        target=self._word_targets.get(document_id)
        if not target or target['workspace_id'] not in (None,workspace_id) or (not select and target['workspace_id'] != workspace_id):
            raise JournalError('SCOPE_DENIED','桌面目标不属于当前工作区')
        def check():
            if bridge._word_app is None:
                ok,msg=bridge._connect_on_com_thread()
                if not ok:
                    raise JournalError('DESKTOP_UNAVAILABLE',msg)
            matches=[bridge._word_app.Documents(i) for i in range(1,bridge._word_app.Documents.Count+1) if str(bridge._word_app.Documents(i).FullName)==target['identity']]
            if len(matches)!=1:
                raise JournalError('TARGET_CHANGED','目标已关闭或重命名；不会按同名文件回退')
            doc=matches[0]
            if select:
                doc.Activate()
                bridge._target_doc_path=target['identity']
            elif str(bridge._word_app.ActiveDocument.FullName)!=target['identity'] or bridge._target_doc_path!=target['identity']:
                raise JournalError('TARGET_CHANGED','Word 活动目标已切换；请重新锁定')
            return doc
        return bridge._call(check) if select else check()
