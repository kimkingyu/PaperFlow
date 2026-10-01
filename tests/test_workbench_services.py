"""Real isolated service calls and security boundaries for the shared workbench."""
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from paperflow.application import ApplicationServices
from paperflow.engine.journals.models import JournalError

DOCX_MIME='application/vnd.openxmlformats-officedocument.wordprocessingml.document'

@pytest.fixture
def services(tmp_path):
    return ApplicationServices(data_dir=tmp_path)

@pytest.fixture
def workspace(services):
    return services.workspaces.create('研究工作区')['id']

def execute(services,workspace,name,**params):
    return services.execute(name,params,workspace_id=workspace)


def test_catalog_has_real_nested_contracts_and_no_fake_reviews(services):
    tools={t['name']:t for t in services.describe_tools()}
    assert len(tools)==59
    assert 'search_reviews' not in tools
    assert tools['writing_create']['input_schema']['properties']['profile']['$ref'].endswith('WritingProfile')
    assert tools['planning_save']['input_schema']['properties']['plan']['$ref'].endswith('ResearchPlan')
    for tool in tools.values():
        assert {'category','mutates','network','desktop','approval_required','available','unavailable_reason'}<=tool.keys()
        assert tool['input_schema']['additionalProperties'] is False
    assert tools['loop_submit_review']['input_schema']['required']==['project_id','expected_loop_revision','expected_project_revision','review','context_fingerprint']


def test_workspace_persistence_link_validation_and_cross_scope(services,workspace):
    created=execute(services,workspace,'planning_create',profile={'title':'课题','goal':'验证可重复性'})['data']
    assert services.workspaces.get(workspace)['links']==[{'kind':'planning','target_id':created['project_id']}]
    other=services.workspaces.create('其他项目')['id']
    with pytest.raises(JournalError,match='未关联'):
        execute(services,other,'planning_get',project_id=created['project_id'])
    services.workspaces.link(other,'planning',created['project_id'])
    assert execute(services,other,'planning_get',project_id=created['project_id'])['data']['revision']==1
    with pytest.raises(Exception):
        services.workspaces.link(other,'planning','rp_nonexistent')
    reopened=ApplicationServices(data_dir=services.directory)
    assert reopened.workspaces.get(workspace)['title']=='研究工作区'
    assert len(reopened.workspaces.list())==2


def test_planning_real_versions_evidence_tasks_and_export(services,workspace):
    created=execute(services,workspace,'planning_create',profile={'title':'规划','goal':'研究验证'})['data']
    pid=created['project_id']
    plan=created['plan']
    plan['questions']=[{'id':'Q1','question':'是否可以复现'}]
    plan['experiments']=[{'id':'E1','question_id':'Q1','title':'基线实验'}]
    plan['tasks']=[{'id':'T1','text':'准备材料','completion_condition':'保存材料记录'}]
    saved=execute(services,workspace,'planning_save',project_id=pid,plan=plan,expected_revision=1)['data']
    assert saved['revision']==2
    with pytest.raises(Exception,match='新版本'):
        execute(services,workspace,'planning_save',project_id=pid,plan=plan,expected_revision=1)
    evidence=execute(services,workspace,'planning_evidence',project_id=pid,expected_revision=2,evidence={'id':'EV1','kind':'note','source_ref':'user supplied note','summary':'准备记录'},experiment_ids=['E1'])['data']
    assert evidence['revision']==3
    task=execute(services,workspace,'planning_task',project_id=pid,task_id='T1',expected_revision=3,updates={'status':'done','completion_note':'已存材料记录'})['data']
    assert task['revision']==4
    exported=execute(services,workspace,'planning_export',project_id=pid)['data']
    assert exported['artifact_id'].startswith('artifact-')
    assert 'output_path' not in exported
    assert services.assets.resolve(exported['artifact_id'],workspace,'artifact',{'.docx'}).is_file()
    assert len(services.assets.list(workspace,'artifact'))==1


def test_writing_and_loop_real_calls_preserve_dual_revision(services,workspace):
    prepared=execute(services,workspace,'writing_prepare',text='研究学习方法的可重复性')
    assert prepared['data']['draft_schema'] if 'draft_schema' in prepared['data'] else prepared['data']['profile_schema']
    created=execute(services,workspace,'writing_create',profile={'title':'相关研究','research_question':'是否可以复现'})['data']
    pid=created['project_id']
    assert execute(services,workspace,'writing_get',project_id=pid)['data']['revision']==1
    assert execute(services,workspace,'loop_get',project_id=pid)['data']['loop_revision']==0
    start=execute(services,workspace,'loop_control',project_id=pid,action='start',expected_loop_revision=0,expected_project_revision=1,budget={'max_read_papers':1,'max_search_calls':0,'max_download_attempts':0,'max_gui_model_calls':0})
    assert start['status']=='success'
    stale=execute(services,workspace,'loop_control',project_id=pid,action='pause',expected_loop_revision=0,expected_project_revision=1)
    assert stale['status']=='error'
    assert stale['error_code']=='REVISION_CONFLICT'
    assert execute(services,workspace,'writing_list')['data']['projects'][0]['project_id']==pid


@pytest.mark.parametrize('name,params',[
    ('papers_search',{'query':'test'}),('papers_download',{'paper_id':'paper-'+('0'*24)}),
    ('writing_search',{'project_id':'writing-'+('0'*24),'expected_revision':1}),
    ('journal_refresh',{'source_id':'crossref','dataset_id':'test'})])
def test_network_is_explicit_and_scope_checked_first(services,workspace,name,params):
    services.literature.search=Mock(side_effect=AssertionError('must not call network'))
    services.literature.download=Mock(side_effect=AssertionError('must not call network'))
    with pytest.raises(JournalError):
        execute(services,workspace,name,**params)
    assert not services.literature.search.called
    assert not services.literature.download.called


def test_journal_mutation_needs_approval_but_preview_is_real(services,workspace):
    preview=execute(services,workspace,'journal_build',dry_run=True)
    assert preview['status']=='success'
    assert not services.finder.store.path.exists()
    with pytest.raises(JournalError,match='确认'):
        execute(services,workspace,'journal_build',dry_run=False)
    assert not services.finder.store.path.exists()
    overview=execute(services,workspace,'overview')
    assert 'database_path' not in overview['data']
    assert execute(services,workspace,'journal_sources')['status']=='success'


@pytest.mark.parametrize('name,params',[
    ('search',{'file_path':'C:/secret'}),('read_project',{'path':'C:/secret','source_type':'local'}),
    ('recommend',{'text':'abc','github_repo':'owner/repo'}),('planning_create',{'profile':{'title':'x','goal':'y','unexpected':'z'}}),
    ('planning_save',{'project_id':'rp_test','plan':{},'expected_revision':True}),
    ('papers_search',{'query':'abc','limit':'10'}),('papers_notes',{'paper_id':'paper-'+('0'*24),'reading':{}}),
    ('word_insert',{'document_id':'document-test','content':'abc','approved':True}),
    ('search_reviews',{'query':'journal'}),('writing_assess',{'project_id':'writing-'+('0'*24),'expected_revision':1,'assessments':[{}]})])
def test_invalid_parameters_do_not_reach_engine(services,workspace,name,params):
    with pytest.raises(JournalError):
        execute(services,workspace,name,**params)


def test_assets_scope_type_integrity_and_source_not_overwritten(services,workspace):
    uploaded=services.assets.upload(workspace,'研究.json','application/json',b'{"hello":"world"}')
    assert uploaded['asset_id'].startswith('asset-')
    assert 'path' not in uploaded
    other=services.workspaces.create('second')['id']
    with pytest.raises(JournalError):
        services.assets.resolve(uploaded['asset_id'],other)
    path=services.assets.resolve(uploaded['asset_id'],workspace)
    path.write_bytes(b'changed')
    with pytest.raises(JournalError,match='修改'):
        services.assets.get(uploaded['asset_id'])


@pytest.mark.parametrize('filename,media_type,content',[
    ('../bad.txt','text/plain',b'text'),('a\\b.txt','text/plain',b'text'),('x:y.txt','text/plain',b'text'),
    ('CON.txt','text/plain',b'text'),('bad.pdf','application/pdf',b'not-pdf'),
    ('bad.json','application/json',b'{"a":NaN}'),('bad.txt','text/plain',b'<script>alert(1)</script>'),
    ('bad.html','text/html',b'<html></html>'),('bad.txt','application/pdf',b'text')])
def test_unsafe_uploads_rejected(services,workspace,filename,media_type,content):
    with pytest.raises(JournalError):
        services.assets.upload(workspace,filename,media_type,content)
    assert services.assets.list(workspace,'asset')==[]


def test_docx_zip_bomb_active_content_and_traversal_rejected(services,workspace):
    for name,content in [('word/document.xml',b'x'*1000000),('../escape',b'x'),('word/vbaProject.bin',b'x'),('word/_rels/document.xml.rels',b'<Relationship TargetMode="External"/>')]:
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml',b'<Types/>')
            archive.writestr('word/document.xml',content if name=='word/document.xml' else b'<document/>')
            if name!='word/document.xml':
                archive.writestr(name,content)
        with pytest.raises(JournalError):
            services.assets.upload(workspace,'unsafe.docx',DOCX_MIME,raw.getvalue())


def test_real_offline_docx_generate_audit_normalize_and_language(services,workspace):
    doc=execute(services,workspace,'documents_generate',title='测试论文',abstract='摘要',keywords=['研究'],sections=[{'title':'1 引言','paragraphs':['待完成的研究计划。'],'tables':[{'headers':['项目','值'],'rows':[['样本','待测']]}]}])['data']
    path=services.assets.resolve(doc['artifact_id'],workspace,'artifact')
    source=services.assets.upload(workspace,'source.docx',DOCX_MIME,path.read_bytes())
    original=services.assets.resolve(source['asset_id'],workspace).read_bytes()
    audited=execute(services,workspace,'documents_audit',asset_id=source['asset_id'])
    assert audited['status']=='success'
    normalized=execute(services,workspace,'documents_normalize',asset_id=source['asset_id'])
    assert normalized['status']=='success'
    assert normalized['data']['artifact_id']!=doc['artifact_id']
    assert services.assets.resolve(source['asset_id'],workspace).read_bytes()==original
    language=execute(services,workspace,'documents_language_check',text='不可否认的是，我们深入探讨研究。')
    assert language['status']=='success'
    assert language['data']


def test_managed_asset_symlink_is_rejected(services,workspace,tmp_path):
    uploaded=services.assets.upload(workspace,'safe.txt','text/plain',b'content')
    path=services.assets.resolve(uploaded['asset_id'],workspace)
    outside=tmp_path/'outside.txt'
    outside.write_bytes(b'content')
    path.unlink()
    try:
        path.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip('OS does not permit symlinks')
    with pytest.raises(JournalError,match='不安全'):
        services.assets.resolve(uploaded['asset_id'],workspace)


def test_submission_parser_is_offline_and_managed(services,workspace):
    result=execute(services,workspace,'submission_track',json_text=json.dumps({'ReviewEvents':[]}))
    assert result['status']=='success'
    assert not result.get('coverage',{}).get('network_access',False)


def test_word_unavailable_is_error_not_fake_success(services,workspace,monkeypatch):
    monkeypatch.setattr(services,'_word_available',lambda:(False,'no COM'))
    with pytest.raises(JournalError,match='no COM'):
        execute(services,workspace,'word_status')
    assert all(not t['available'] for t in services.describe_tools() if t['desktop'])


def test_word_exact_target_and_selection_are_checked_on_com_thread(services,workspace,monkeypatch):
    class Documents:
        Count=1
        def __call__(self,i):
            return doc
    doc=SimpleNamespace(FullName='C:/controlled.docx',Name='controlled.docx',TrackRevisions=False)
    app=SimpleNamespace(Documents=Documents(),ActiveDocument=doc,Selection=SimpleNamespace(Start=1,End=4,Text='abc'))
    doc.Activate=lambda:None
    calls=[]
    bridge=SimpleNamespace(_word_app=app,_target_doc_path=None,_call=lambda fn:fn(),_clean=lambda text:text)
    bridge.replace_selection=lambda **p:calls.append(p) or {'success':True}
    monkeypatch.setattr(services,'_bridge',lambda:bridge)
    services._word_targets['document-test']={'identity':doc.FullName,'workspace_id':None}
    services.execute('word_select',{'document_id':'document-test'},workspace_id=workspace,approved=True)
    selection=execute(services,workspace,'word_selection',document_id='document-test')['data']
    params={'document_id':'document-test','selection_fingerprint':selection['selection_fingerprint'],'new_text':'new'}
    with pytest.raises(JournalError,match='确认'):
        services.execute('word_replace',params,workspace_id=workspace)
    app.Selection.Text='changed'
    with pytest.raises(JournalError,match='选区'):
        services.execute('word_replace',params,workspace_id=workspace,approved=True)
    assert not calls
    app.Selection.Text='abc'
    services.execute('word_replace',params,workspace_id=workspace,approved=True)
    assert calls==[{'new_text':'new'}]
    assert doc.TrackRevisions
    app.ActiveDocument=SimpleNamespace(FullName='C:/other.docx')
    with pytest.raises(JournalError,match='切换'):
        services.execute('word_replace',params,workspace_id=workspace,approved=True)
    assert len(calls)==1


def test_real_managed_pdf_reading_draft_export_and_strict_citations(services,workspace):
    from test_literature_service import pdf_bytes, card_for
    raw=pdf_bytes(['A reproducible method is described in this paper.'])
    asset=services.assets.upload(workspace,'method.pdf','application/pdf',raw)
    imported=execute(services,workspace,'papers_import',asset_id=asset['asset_id'])['data']
    pid=imported['paper_id']
    assert imported['title']=='method.pdf'
    assert {'kind':'paper','target_id':pid} in services.workspaces.get(workspace)['links']
    read=execute(services,workspace,'papers_read',paper_id=pid)
    assert read['data']['fragments']
    card=card_for(read)
    execute(services,workspace,'papers_notes',paper_id=pid,reading=card)
    created=execute(services,workspace,'writing_create',profile={'title':'Evidence-supported draft','research_question':'Which methods can be reproduced?','language':'en'},paper_ids=[pid])['data']
    candidate=created['candidates'][0]
    assessed=execute(services,workspace,'writing_assess',project_id=created['project_id'],expected_revision=1,assessments=[{'paper_id':pid,'metadata_sha256':candidate['metadata_sha256'],'relevance':'core','reason':'The stated method addresses reproducibility.','question_ids':['RQ1'],'basis':'metadata'}])['data']
    citation=assessed['evidence_matrix'][0]['citation_id']
    draft={'title':'Evidence-supported literature draft','language':'en','outline':[{'id':'related','title':'Related work','purpose':'Compare the stated method','level':1,'evidence_ids':[citation]}],'sections':[{'id':'related','title':'Related work','level':1,'paragraphs':[{'kind':'literature_summary','text':'The referenced paper describes a reproducible method.','citation_ids':[citation],'own_material_ids':[]}]}]}
    saved=execute(services,workspace,'writing_draft',project_id=created['project_id'],expected_revision=2,draft=draft)['data']
    assert saved['revision']==3
    exported=execute(services,workspace,'writing_export',project_id=created['project_id'])['data']
    assert exported['artifact_id'].startswith('artifact-')
    with zipfile.ZipFile(services.assets.resolve(exported['artifact_id'],workspace,'artifact')) as archive:
        assert b'reproducible method' in archive.read('word/document.xml')
    card['claims'][0]['evidence'][0]['quote']='fabricated claim not present in source'
    with pytest.raises(JournalError) as error:
        execute(services,workspace,'papers_notes',paper_id=pid,reading=card)
    assert error.value.code=='EVIDENCE_MISMATCH'
    cache=services.literature.store.pdf_path(read['data']['file_sha256'])
    assert cache.is_file()
    cache.write_bytes(b'%PDF-modified')
    with pytest.raises(JournalError):
        execute(services,workspace,'writing_export',project_id=created['project_id'])
    assert len(services.assets.list(workspace,'artifact'))==1


def test_global_discovery_is_manual_summary_only_and_scoped_lists_stay_closed(services,workspace):
    created=execute(services,workspace,'planning_create',profile={'title':'Private title','goal':'Private detailed material'})['data']
    other=services.workspaces.create('other')['id']
    assert execute(services,other,'planning_list')['data']['projects']==[]
    discovered=services.execute('planning_list',{'discover':True})['data']['projects']
    assert discovered[0]['project_id']==created['project_id']
    assert 'plan' not in discovered[0] and 'goal' not in discovered[0]
    with pytest.raises(JournalError,match='人工'):
        services.execute('planning_list',{'discover':True},workspace_id=other,origin='native_agent')


def test_custom_finder_keeps_all_uninjected_services_out_of_real_home(tmp_path,monkeypatch):
    from paperflow.engine.journal_finder import JournalFinder
    monkeypatch.setenv('PAPERFLOW_RESEARCH_HOME',str(tmp_path/'must-not-touch-research'))
    monkeypatch.setenv('PAPERFLOW_PAPER_HOME',str(tmp_path/'must-not-touch-papers'))
    app=ApplicationServices(finder=JournalFinder(data_dir=str(tmp_path/'isolated-journals')))
    assert app.planning.store.directory==tmp_path/'isolated-journals'/'research'
    assert app.literature.store.directory==tmp_path/'isolated-journals'/'papers'
    w=app.workspaces.create('isolated')['id']
    execute(app,w,'planning_create',profile={'title':'Research','goal':'Goal'})
    assert not (tmp_path/'must-not-touch-research').exists()
    assert not (tmp_path/'must-not-touch-papers').exists()


def test_cross_instance_leases_require_exact_owner_and_do_not_reacquire_lost(services,workspace):
    pid=execute(services,workspace,'writing_create',profile={'title':'x','research_question':'y'})['data']['project_id']
    second=ApplicationServices(data_dir=services.directory)
    services.acquire_execution(pid,'gui:test')
    with pytest.raises(JournalError) as error:
        second.acquire_execution(pid,'agent:other')
    assert error.value.code=='EXECUTION_BUSY'
    second.release_execution(pid,'agent:other')
    assert services.ensure_execution(pid,'gui:test')['status']=='success'
    services.release_execution(pid,'gui:test')
    with pytest.raises(JournalError) as error:
        services.ensure_execution(pid,'gui:test')
    assert error.value.code=='EXECUTION_LOST'


def test_agent_material_scope_freezes_external_link_upload_and_reauths_on_resume(services,workspace):
    pid=execute(services,workspace,'writing_create',profile={'title':'x','research_question':'y'})['data']['project_id']
    scope=services.agent_start(workspace,'run-scope')['data']
    second=ApplicationServices(data_dir=services.directory)
    with pytest.raises(JournalError,match='执行'):
        second.workspaces.link(workspace,'writing',pid)
    with pytest.raises(JournalError,match='执行'):
        second.assets.upload(workspace,'new.txt','text/plain',b'new material')
    with pytest.raises(JournalError):
        second.acquire_execution(pid,'gui:other')
    native=services.execute('planning_create',{'profile':{'title':'native plan','goal':'goal'}},workspace_id=workspace,origin='native_agent')
    assert native['status']=='success'
    refreshed=services.agent_scope_snapshot(workspace,'run-scope')['data']
    assert refreshed!=scope
    assert services.agent_release('run-scope')['status']=='success'
    second.assets.upload(workspace,'new.txt','text/plain',b'new material')
    with pytest.raises(JournalError) as error:
        services.agent_start(workspace,'run-scope',scope=refreshed)
    assert error.value.code=='SCOPE_CHANGED'
    services.agent_start(workspace,'run-new')
    other=second.workspaces.create('other')['id']
    with pytest.raises(JournalError,match='切换'):
        services.execute('planning_list',{},workspace_id=other,origin='native_agent')
    services.agent_release('run-new')


def test_native_loop_requests_mirror_actual_budget_only_when_engaged(services,workspace):
    pid=execute(services,workspace,'writing_create',profile={'title':'x','research_question':'y'})['data']['project_id']
    execute(services,workspace,'loop_control',project_id=pid,action='start',expected_loop_revision=0,expected_project_revision=1,budget={'max_gui_model_calls':1})
    services.agent_start(workspace,'run-budget')
    assert services.agent_before_model(workspace,'run-budget')['data']['reservations']==[]
    assert services.loop.get(pid)['data']['loop']['usage']['gui_model_calls']==0
    services.execute('loop_get',{'project_id':pid},workspace_id=workspace,origin='native_agent')
    reserved=services.agent_before_model(workspace,'run-budget')
    assert len(reserved['data']['reservations'])==1
    assert services.loop.get(pid)['data']['loop']['reserved']['gui_model_calls']==1
    with pytest.raises(JournalError) as error:
        services.agent_before_model(workspace,'run-budget')
    assert error.value.code=='NEEDS_RECONCILIATION'
    services.agent_after_model('run-budget',success=False)
    state=services.loop.get(pid)['data']
    assert state['loop']['usage']['gui_model_calls']==1
    assert state['loop']['reserved']['gui_model_calls']==0
    with pytest.raises(JournalError) as error:
        services.agent_before_model(workspace,'run-budget')
    assert error.value.code=='BUDGET_EXHAUSTED'
    services.agent_release('run-budget')


def test_zero_gui_budget_does_not_block_unrelated_native_model_classification(services,workspace):
    pid=execute(services,workspace,'writing_create',profile={'title':'x','research_question':'y'})['data']['project_id']
    execute(services,workspace,'loop_control',project_id=pid,action='start',expected_loop_revision=0,expected_project_revision=1,budget={'max_gui_model_calls':0})
    services.agent_start(workspace,'run-classify')
    assert services.agent_before_model(workspace,'run-classify')['status']=='success'
    services.execute('loop_get',{'project_id':pid},workspace_id=workspace,origin='native_agent')
    with pytest.raises(JournalError) as error:
        services.agent_before_model(workspace,'run-classify')
    assert error.value.code=='BUDGET_EXHAUSTED'
    services.agent_release('run-classify')


def test_managed_sqlite_upload_validation_is_read_only(services,workspace,tmp_path):
    import sqlite3
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE records (title TEXT)')
        db.execute('INSERT INTO records VALUES (?)',('known input',))
    asset=services.assets.upload(workspace,'dataset.sqlite','application/vnd.sqlite3',path.read_bytes())
    assert services.assets.resolve(asset['asset_id'],workspace).read_bytes()==path.read_bytes()
    with pytest.raises(JournalError):
        services.assets.upload(workspace,'bad.db','application/octet-stream',b'not sqlite')


def test_complete_native_source_and_artifact_provenance_persist(services,workspace):
    from test_literature_service import pdf_bytes, card_for
    raw=pdf_bytes(['A reproducible method is described in this paper.'])
    asset=services.assets.upload(workspace,'native.pdf','application/pdf',raw)
    services.agent_start(workspace,'run-native-full')
    def native(name,**params):
        return services.execute(name,params,workspace_id=workspace,origin='native_agent')
    pid=native('papers_import',asset_id=asset['asset_id'])['data']['paper_id']
    card=card_for(native('papers_read',paper_id=pid))
    saved_card=native('papers_notes',paper_id=pid,reading=card)['data']
    assert saved_card['origin']=='nativeAgent'
    created=native('writing_create',profile={'title':'Native draft','research_question':'Can the method be reproduced?','language':'en'},paper_ids=[pid])['data']
    candidate=created['candidates'][0]
    assessed=native('writing_assess',project_id=created['project_id'],expected_revision=1,assessments=[{'paper_id':pid,'metadata_sha256':candidate['metadata_sha256'],'relevance':'core','reason':'The stated method addresses the research question.','question_ids':['RQ1'],'basis':'metadata'}])['data']
    citation=assessed['evidence_matrix'][0]['citation_id']
    draft={'title':'Native literature-supported draft','language':'en','outline':[{'id':'related','title':'Related work','purpose':'Compare the method','level':1,'evidence_ids':[citation]}],'sections':[{'id':'related','title':'Related work','level':1,'paragraphs':[{'kind':'literature_summary','text':'The paper describes a reproducible method.','citation_ids':[citation],'own_material_ids':[]}]}]}
    native('writing_draft',project_id=created['project_id'],draft=draft,expected_revision=2)
    exported=native('writing_export',project_id=created['project_id'])['data']
    assert exported['metadata']['run_id']=='run-native-full'
    assert exported['metadata']['source_tool']=='writing_export'
    assert exported['metadata']['origin']=='nativeAgent'
    assert exported['metadata']['project_id']==created['project_id']
    assert exported['metadata']['project_revision']==3
    reopened=ApplicationServices(data_dir=services.directory)
    assert reopened.assets.get(exported['artifact_id'])['metadata']==exported['metadata']
    services.agent_scope_snapshot(workspace,'run-native-full')
    services.agent_release('run-native-full')
    normalized_asset=services.assets.upload(workspace,'normalize.docx',DOCX_MIME,services.assets.resolve(exported['artifact_id'],workspace,'artifact').read_bytes())
    normalized=execute(services,workspace,'documents_normalize',asset_id=normalized_asset['asset_id'])['data']
    assert normalized['metadata']['source_tool']=='documents_normalize'
    assert normalized['metadata']['input_asset_refs']==[{'asset_id':normalized_asset['asset_id'],'sha256':normalized_asset['sha256']}]


def test_foreign_reading_card_is_not_silently_added_to_existing_paper_scope(services,workspace):
    from test_literature_service import pdf_bytes, card_for
    asset=services.assets.upload(workspace,'scope.pdf','application/pdf',pdf_bytes(['A real source method.']))
    pid=execute(services,workspace,'papers_import',asset_id=asset['asset_id'])['data']['paper_id']
    card=card_for(execute(services,workspace,'papers_read',paper_id=pid))
    services.agent_start(workspace,'run-card-scope')
    # Simulate a genuine external MCP/direct-core writer bypassing the app lease.
    services.literature.save_reading(pid,card,origin='calling_agent')
    for operation in (lambda:services.agent_before_model(workspace,'run-card-scope'),lambda:services.execute('papers_details',{'paper_id':pid},workspace_id=workspace,origin='native_agent'),lambda:services.agent_scope_snapshot(workspace,'run-card-scope')):
        with pytest.raises(JournalError) as error:
            operation()
        assert error.value.code=='SCOPE_CHANGED'
    services.agent_release('run-card-scope')


def test_actor_delta_does_not_authorize_unrelated_core_write_after_action(services,workspace,monkeypatch):
    unrelated=execute(services,workspace,'planning_create',profile={'title':'Unrelated','goal':'Original material'})['data']
    services.agent_start(workspace,'run-actor')
    original=services._execute
    def foreign_write(name,params,w,origin):
        result=original(name,params,w,origin)
        foreign=services.planning.get(unrelated['project_id'])['data']
        foreign['plan']['profile']['goal']='Foreign new outbound material'
        services.planning.save(unrelated['project_id'],foreign['plan'],foreign['revision'])
        return result
    monkeypatch.setattr(services,'_execute',foreign_write)
    with pytest.raises(JournalError) as error:
        services.execute('planning_create',{'profile':{'title':'Owned','goal':'Own authorized material'}},workspace_id=workspace,origin='native_agent')
    assert error.value.code=='SCOPE_CHANGED'
    with pytest.raises(JournalError):
        services.agent_scope_snapshot(workspace,'run-actor')
    services.agent_release('run-actor')


def test_recovery_releases_only_recovered_owner_and_retains_pending_models(services,workspace):
    pid=execute(services,workspace,'writing_create',profile={'title':'x','research_question':'y'})['data']['project_id']
    execute(services,workspace,'loop_control',project_id=pid,action='start',expected_loop_revision=0,expected_project_revision=1)
    services.agent_start(workspace,'run-recover')
    services.execute('loop_get',{'project_id':pid},workspace_id=workspace,origin='native_agent')
    services.agent_before_model(workspace,'run-recover')
    another=services.workspaces.create('Other workspace')['id']
    services.execution.acquire(another,'agent:run-live')
    report=services.agent_recover(['run-recover'])['data']
    assert report['needs_reconciliation']
    assert report['recovered'][0]['pending_model_reservations']==1
    assert services.execution.pending('run-recover')
    assert services.loop.get(pid)['data']['loop']['usage']['gui_model_calls']==0
    assert services.loop.get(pid)['data']['loop']['reserved']['gui_model_calls']==1
    assert services.execution.ensure(another,'agent:run-live')
    services.execution.release('agent:run-live')


def test_multi_project_reservation_failure_refunds_only_unissued_budget(services,workspace,monkeypatch):
    projects=[]
    for title in ('first','second'):
        pid=execute(services,workspace,'writing_create',profile={'title':title,'research_question':'y'})['data']['project_id']
        execute(services,workspace,'loop_control',project_id=pid,action='start',expected_loop_revision=0,expected_project_revision=1)
        projects.append(pid)
    services.agent_start(workspace,'run-refund')
    for pid in projects:
        services.execute('loop_get',{'project_id':pid},workspace_id=workspace,origin='native_agent')
    original=services.loop.reserve_model_call
    count=0
    def conflict(*args,**kwargs):
        nonlocal count
        count+=1
        if count==2:
            return {'status':'error','error_code':'REVISION_CONFLICT','message':'controlled conflict','data':None}
        return original(*args,**kwargs)
    monkeypatch.setattr(services.loop,'reserve_model_call',conflict)
    with pytest.raises(JournalError) as error:
        services.agent_before_model(workspace,'run-refund')
    assert error.value.code=='REVISION_CONFLICT'
    assert services.execution.pending('run-refund')==[]
    for pid in projects:
        state=services.loop.get(pid)['data']['loop']
        assert state['reserved']['gui_model_calls']==0
        assert state['usage']['gui_model_calls']==0
    services.agent_release('run-refund')


def test_json_calendar_dates_and_booleans_use_correct_strict_types(services,workspace):
    created=execute(services,workspace,'planning_create',profile={'title':'dates','goal':'goal','start_date':'2026-10-01','target_date':'2026-10-02'})['data']
    assert created['plan']['profile']['start_date']=='2026-10-01'
    plan=created['plan']
    plan['tasks']=[{'id':'T1','text':'Task','completion_condition':'Record exists'}]
    saved=execute(services,workspace,'planning_save',project_id=created['project_id'],expected_revision=1,plan=plan)['data']
    updated=execute(services,workspace,'planning_task',project_id=created['project_id'],task_id='T1',expected_revision=saved['revision'],updates={'due_date':'2026-10-02'})['data']
    assert updated['plan']['tasks'][0]['due_date']=='2026-10-02'
    with pytest.raises(JournalError):
        execute(services,workspace,'planning_task',project_id=created['project_id'],task_id='T1',expected_revision=True,updates={'status':'doing'})


def test_native_real_search_adapter_scopes_provider_records_and_project_candidates(tmp_path):
    from test_literature_service import FakeProviders
    from paperflow.engine.literature.service import LiteratureService
    literature=LiteratureService(str(tmp_path/'papers'),providers=FakeProviders())
    services=ApplicationServices(data_dir=tmp_path,literature=literature)
    workspace=services.workspaces.create('Provider fixtures')['id']
    services.agent_start(workspace,'run-provider-fixture')
    searched=services.execute('papers_search',{'query':'reproducibility'},workspace_id=workspace,origin='native_agent',allow_network=True)
    assert searched['status']=='success'
    assert literature.providers.calls
    services.agent_scope_snapshot(workspace,'run-provider-fixture')
    created=services.execute('writing_create',{'profile':{'title':'x','research_question':'y'},'queries':[{'id':'Q1','query':'reproducibility','purpose':'Relevant methods','question_ids':['RQ1']}]},workspace_id=workspace,origin='native_agent')['data']
    result=services.execute('writing_search',{'project_id':created['project_id'],'expected_revision':1},workspace_id=workspace,origin='native_agent',allow_network=True)
    assert result['status']=='success'
    services.agent_scope_snapshot(workspace,'run-provider-fixture')
    services.agent_release('run-provider-fixture')


def test_native_asset_scope_failure_restores_actor_context(services,workspace):
    other=services.workspaces.create('other')['id']
    foreign=services.assets.upload(other,'foreign.txt','text/plain',b'private')
    services.agent_start(workspace,'run-restore-context')
    with pytest.raises(JournalError):
        services.execute('planning_prepare',{'asset_id':foreign['asset_id']},workspace_id=workspace,origin='native_agent')
    assert getattr(services._operation_context,'owner',None) is None
    with pytest.raises(JournalError):
        services.assets.upload(workspace,'must-not-enter.txt','text/plain',b'new material')
    services.agent_release('run-restore-context')


def test_desktop_write_overrides_legacy_duplicate_basename_resolver(services,workspace,monkeypatch):
    doc=SimpleNamespace(FullName='C:/selected/same.docx',Name='same.docx',TrackRevisions=False)
    wrong=SimpleNamespace(FullName='C:/other/same.docx',Name='same.docx')
    class Documents:
        Count=2
        def __call__(self,index):
            return wrong if index==1 else doc
    app=SimpleNamespace(Documents=Documents(),ActiveDocument=doc,Selection=SimpleNamespace(Start=1,End=4,Text='abc'))
    doc.Activate=lambda:None
    bridge=SimpleNamespace(_word_app=app,_target_doc_path=doc.FullName,_call=lambda fn:fn(),_clean=lambda text:text,_active_doc=lambda:wrong)
    original_active=bridge._active_doc
    touched=[]
    bridge.replace_selection=lambda **args:touched.append(bridge._active_doc()) or {'success':True}
    monkeypatch.setattr(services,'_bridge',lambda:bridge)
    services._word_targets['document-duplicate']={'identity':doc.FullName,'workspace_id':workspace}
    services._word_selected='document-duplicate'
    selected=execute(services,workspace,'word_selection',document_id='document-duplicate')['data']
    services.execute('word_replace',{'document_id':'document-duplicate','selection_fingerprint':selected['selection_fingerprint'],'new_text':'approved'},workspace_id=workspace,approved=True)
    assert touched==[doc]
    assert bridge._active_doc is original_active


@pytest.mark.parametrize('resolve_oa_metadata',[False,True])
def test_native_loop_real_review_download_read_and_interpretation_receipts(services,workspace,tmp_path,monkeypatch,resolve_oa_metadata):
    from test_reading_loop_core import create_sample_project
    from test_literature_service import card_for
    project_id,papers=create_sample_project(services.writing,tmp_path,count=2)
    services.workspaces.link(workspace,'writing',project_id)
    services.agent_start(workspace,'run-loop-receipts')
    def native(name,**params):
        result=services.execute(name,params,workspace_id=workspace,origin='native_agent',allow_network=True)
        assert result['status']!='error',result
        return result['data']
    current=native('loop_get',project_id=project_id)
    native('loop_control',project_id=project_id,action='start',expected_loop_revision=current['loop_revision'],expected_project_revision=current['project_revision'])
    prepared=native('loop_prepare_review',project_id=project_id)
    current=native('loop_get',project_id=project_id)
    citation=prepared['evidence_matrix'][0]['citation_id']
    review={'dimensions':[{'dimension':dimension,'status':'gap' if dimension in {'sub_questions','draft_support'} else 'not_applicable','reason':'A targeted comparison is required.','question_ids':['RQ1'] if dimension=='sub_questions' else [],'section_ids':['sec-intro'] if dimension=='draft_support' else []} for dimension in ('sub_questions','method_baselines','contrary_findings','draft_support')],'gaps':[{'id':'G1','category':'literature','priority':'high','question_ids':['RQ1'],'section_ids':['sec-intro'],'reason':'The second architecture needs reading.','expected_information':'A reproducible comparison.','read_paper_ids':[papers[1]]}],'decision':'continue','reason':'Read the second source.','examined_citation_ids':[citation],'examined_section_ids':['sec-intro']}
    assessed=native('loop_submit_review',project_id=project_id,review=review,context_fingerprint=prepared['context_fingerprint'],expected_project_revision=current['project_revision'],expected_loop_revision=current['loop_revision'])
    candidate=next(c for c in assessed['project']['candidates'] if c['paper_id']==papers[1])
    feedback={'kind':'assessment','assessments':[{'paper_id':papers[1],'metadata_sha256':candidate['metadata_sha256'],'relevance':'core','reason':'An actual comparison source.','question_ids':['RQ1'],'basis':'metadata'}],'selected_paper_ids':papers}
    state=native('loop_feedback',project_id=project_id,action_id=assessed['loop']['next_action']['action_id'],feedback=feedback,expected_project_revision=assessed['project_revision'],expected_loop_revision=assessed['loop_revision'])
    assert state['loop']['next_action']['kind']=='download'
    if resolve_oa_metadata:
        from paperflow.engine.literature.models import PaperRecord
        original_download=services.literature.download
        def download_with_resolved_metadata(paper_id):
            downloaded=original_download(paper_id)
            record={k:v for k,v in downloaded['data'].items() if k in PaperRecord.model_fields}
            record['fulltext_locations']=[{'url':'https://example.org/isolated-open-paper.pdf','is_open':True,'access_evidence':'Controlled test resolver; no network request.'}]
            services.literature.store.put(record)
            return services.literature.get(paper_id=paper_id)
        monkeypatch.setattr(services.literature,'download',download_with_resolved_metadata)
    state=native('loop_step',project_id=project_id,action_id=state['loop']['next_action']['action_id'],expected_project_revision=state['project_revision'],expected_loop_revision=state['loop_revision'])
    assert state['step_result']['file_sha256']==services.literature.get(papers[1])['data']['acquisition']['file_sha256']
    assert state['step_result']['download_receipt']['paper_id']==papers[1]
    if resolve_oa_metadata:
        assert state['step_result']['download_receipt']['fulltext_locations']
    assert state['loop']['next_action']['kind']=='read'
    state=native('loop_step',project_id=project_id,action_id=state['loop']['next_action']['action_id'],expected_project_revision=state['project_revision'],expected_loop_revision=state['loop_revision'])
    assert state['loop']['next_action']['kind']=='interpretation'
    card=card_for({'data':state['loop']['next_action']['material']})
    interpreted=native('loop_feedback',project_id=project_id,action_id=state['loop']['next_action']['action_id'],feedback={'kind':'interpretation','reading':card,'read_more':False},expected_project_revision=state['project_revision'],expected_loop_revision=state['loop_revision'])
    assert interpreted['saved_reading']['origin']=='nativeAgent'
    assert interpreted['saved_reading']['card']['paper_id']==papers[1]
    services.agent_scope_snapshot(workspace,'run-loop-receipts')
    services.agent_release('run-loop-receipts')


@pytest.mark.parametrize('part,body',[
    ('word/_rels/document.xml.rels',b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="x" Target="https://example.invalid" TargetMode="&#69;xternal"/></Relationships>'),
    ('word/document.xml',b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DD</w:instrText></w:r><w:r><w:instrText>EAUTO command</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:body></w:document>'),
    ('word/document.xml',b'<!DOCTYPE root [<!ENTITY x "expand">]><document>&x;</document>')])
def test_docx_encoded_external_relationships_split_dde_and_entities_rejected(services,workspace,part,body):
    from docx import Document
    buffer=io.BytesIO()
    Document().save(buffer)
    original=zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w') as archive:
        for entry in original.infolist():
            if entry.filename!=part:
                archive.writestr(entry,original.read(entry.filename))
        archive.writestr(part,body)
    original.close()
    with pytest.raises(JournalError):
        services.assets.upload(workspace,'active.docx',DOCX_MIME,result.getvalue())
