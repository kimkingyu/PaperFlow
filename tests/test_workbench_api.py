"""Authenticated API contract, real managed streaming uploads and offline artifacts."""
import json
from urllib.parse import quote
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from paperflow.application import ApplicationServices
from paperflow.gui.workbench_routes import build_workbench_routes

@pytest.fixture
def api(tmp_path):
    services=ApplicationServices(data_dir=tmp_path)
    def guard(request):
        if request.headers.get('x-paperflow-token')!='test-only-token':
            return JSONResponse({'status':'error','error_code':'FORBIDDEN'},status_code=403)
    def failure(exc):
        return JSONResponse({'status':'error','error_code':getattr(exc,'code','INVALID_INPUT'),'message':str(exc),'data':None},status_code=400)
    app=Starlette(routes=build_workbench_routes(services,guard,failure,JSONResponse))
    with TestClient(app) as client:
        client.headers.update({'x-paperflow-token':'test-only-token'})
        yield client,services


def create_workspace(client):
    return client.post('/api/workbench/workspaces',json={'title':'课题项目'}).json()['data']['id']


def test_authentication_covers_every_route(api):
    client,_=api
    for method,path in [('GET','/api/workbench/capabilities'),('GET','/api/workbench/workspaces'),('POST','/api/workbench/workspaces'),('GET','/api/workbench/workspaces/any'),('POST','/api/workbench/workspaces/any/links'),('POST','/api/workbench/action'),('GET','/api/workbench/assets?workspace_id=any'),('POST','/api/workbench/assets?workspace_id=any'),('GET','/api/workbench/artifacts?workspace_id=any'),('GET','/api/workbench/artifacts/any/download'),('GET','/api/workbench/assets/any/download')]:
        response=client.request(method,path,headers={'x-paperflow-token':''})
        assert response.status_code==403,(method,path,response.text)


def test_capabilities_and_workspace_contract(api):
    client,_=api
    result=client.get('/api/workbench/capabilities').json()
    assert result['status']=='success'
    assert len(result['data']['tools'])==59
    assert result['data']['backend_calls_llm'] is False
    workspace=create_workspace(client)
    assert client.get('/api/workbench/workspaces/'+workspace).json()['data']['title']=='课题项目'
    assert client.get('/api/workbench/workspaces').json()['data'][0]['id']==workspace
    nonexistent=client.post('/api/workbench/workspaces/'+workspace+'/links',json={'kind':'writing','target_id':'writing-'+('0'*24)})
    assert nonexistent.status_code==400


@pytest.mark.parametrize('content,content_type',[
    ('{"title":"x","title":"y"}','application/json'),('{"title":"x","extra":true}','application/json'),
    ('[]','application/json'),('{"title":null}','application/json'),('{"title":"x"}','text/plain'),
    ('{"title":NaN}','application/json'),('not JSON','application/json')])
def test_strict_body_and_content_type(api,content,content_type):
    client,_=api
    result=client.post('/api/workbench/workspaces',content=content,headers={'content-type':content_type})
    assert result.status_code==400
    assert result.json()['error_code']=='INVALID_INPUT'


def test_actual_planning_action_scope_and_export_download(api):
    client,services=api
    w=create_workspace(client)
    created=client.post('/api/workbench/action',json={'action':'planning_create','params':{'profile':{'title':'研究计划','goal':'准备实验'}},'workspace_id':w}).json()['data']
    other=services.workspaces.create('other')['id']
    denied=client.post('/api/workbench/action',json={'action':'planning_get','params':{'project_id':created['project_id']},'workspace_id':other})
    assert denied.status_code==400
    assert denied.json()['error_code']=='SCOPE_DENIED'
    link=client.post('/api/workbench/workspaces/'+other+'/links',json={'kind':'planning','target_id':created['project_id']})
    assert link.status_code==200
    exported=client.post('/api/workbench/action',json={'action':'planning_export','params':{'project_id':created['project_id']},'workspace_id':w}).json()
    assert exported['status']=='success',exported
    artifact=exported['data']
    assert 'output_path' not in artifact
    assert str(services.directory) not in json.dumps(exported)
    download=client.get(artifact['download_url'])
    assert download.status_code==200
    assert download.content.startswith(b'PK')
    assert download.headers['x-content-type-options']=='nosniff'
    assert download.headers['content-disposition'].startswith('attachment')
    assert client.get(artifact['download_url']+'?workspace_id='+other).json()['error_code']=='SCOPE_DENIED'
    assert client.get('/api/workbench/artifacts?workspace_id='+w).json()['data'][0]['artifact_id']==artifact['artifact_id']


def test_raw_upload_streaming_metadata_and_download(api):
    client,services=api
    w=create_workspace(client)
    name='材料说明.txt'
    uploaded=client.post('/api/workbench/assets?workspace_id='+w,content='实际研究材料'.encode(),headers={'x-file-name':quote(name),'content-type':'text/plain'}).json()['data']
    assert uploaded['filename']==name
    assert uploaded['size']==len('实际研究材料'.encode())
    assert uploaded['asset_id'].startswith('asset-')
    assert client.get(uploaded['download_url']).content=='实际研究材料'.encode()
    assert client.get('/api/workbench/assets?workspace_id='+w).json()['data'][0]['asset_id']==uploaded['asset_id']
    prepared=client.post('/api/workbench/action',json={'action':'planning_prepare','params':{'asset_id':uploaded['asset_id']},'workspace_id':w}).json()
    assert prepared['status']=='success'
    assert str(services.directory) not in json.dumps(prepared)


@pytest.mark.parametrize('filename,content_type,content',[
    ('../secret.txt','text/plain',b'text'),('bad\\secret.txt','text/plain',b'text'),
    ('bad.html','text/html',b'<script>bad</script>'),('bad.pdf','application/pdf',b'not pdf'),
    ('bad.txt','text/plain',b'<iframe>bad</iframe>'),('bad.json','application/json',b'bad JSON')])
def test_upload_boundaries_and_partial_cleanup(api,filename,content_type,content):
    client,services=api
    w=create_workspace(client)
    result=client.post('/api/workbench/assets?workspace_id='+w,content=content,headers={'x-file-name':quote(filename),'content-type':content_type})
    assert result.status_code==400
    assert services.assets.list(w,'asset')==[]
    assert list(services.assets.root.iterdir())==[]


def test_streaming_size_limits_without_trusting_content_length(api):
    client,services=api
    w=create_workspace(client)
    oversized=client.post('/api/workbench/assets?workspace_id='+w,content=(b'x'*1024*1024 for _ in range(33)),headers={'x-file-name':'large.txt','content-type':'text/plain'})
    assert oversized.status_code==400
    assert oversized.json()['error_code']=='INPUT_TOO_LARGE'
    assert services.assets.list(w,'asset')==[]
    assert list(services.assets.root.iterdir())==[]
    length=client.post('/api/workbench/workspaces',content=b'{}',headers={'content-type':'application/json','content-length':str(3*1024*1024)})
    assert length.json()['error_code']=='INPUT_TOO_LARGE'
    bogus=client.post('/api/workbench/workspaces',content=b'{}',headers={'content-type':'application/json','content-length':'-1'})
    assert bogus.json()['error_code']=='INVALID_INPUT'


@pytest.mark.parametrize('payload',[
    {'action':'overview','extra':1},{'action':'overview','allow_network':'true'},
    {'action':'overview','approved':1},{'action':'overview','params':{'file_path':'/etc/passwd'}},
    {'action':'search_reviews','params':{'query':'journal'}},
    {'action':'read_project','params':{'source_type':'local','path':'C:/private'}}])
def test_no_unknown_actions_auth_spoof_or_arbitrary_paths(api,payload):
    client,_=api
    result=client.post('/api/workbench/action',json=payload)
    assert result.status_code==400


def test_workspace_query_is_required_unique_and_closed(api):
    client,_=api
    w=create_workspace(client)
    for query in ['', '?workspace_id='+w+'&workspace_id='+w, '?workspace_id='+w+'&file_path=/etc/passwd']:
        assert client.get('/api/workbench/assets'+query).status_code==400
