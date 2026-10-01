"""Authenticated bounded HTTP transport for the shared application services."""
from __future__ import annotations

import inspect
import json
import re
from urllib.parse import unquote
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse
from starlette.routing import Route
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Annotated, Optional
from paperflow.engine.journals.models import JournalError, response
from paperflow.application.storage import MAX_ASSET_BYTES, MIMES

MAX_JSON_BYTES = 2 * 1024 * 1024
DOWNLOAD_HEADERS = {'X-Content-Type-Options':'nosniff','Cache-Control':'no-store','Content-Security-Policy':"sandbox; default-src 'none'",'Referrer-Policy':'no-referrer'}

class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)

class CreateWorkspace(RequestModel):
    title: Annotated[str,Field(min_length=1,max_length=300)]
    description: Annotated[str,Field(max_length=4000)] = ''

class LinkWorkspace(RequestModel):
    kind: str
    target_id: str

class Action(RequestModel):
    action: Annotated[str,Field(min_length=1,max_length=100)]
    params: dict = Field(default_factory=dict)
    workspace_id: Optional[str] = None
    allow_network: bool = False
    approved: bool = False


def build_workbench_routes(services, guard, failure, ok) -> list[Route]:
    async def authorize(request):
        blocked = guard(request)
        if inspect.isawaitable(blocked):
            blocked = await blocked
        return blocked

    async def bounded_stream(request, maximum):
        raw_length=request.headers.get('content-length')
        if raw_length is not None:
            if not raw_length.isdigit():
                raise JournalError('INVALID_INPUT','Content-Length 无效')
            if int(raw_length)>maximum:
                raise JournalError('INPUT_TOO_LARGE','请求超过大小上限')
        count=0
        async for chunk in request.stream():
            count+=len(chunk)
            if count>maximum:
                raise JournalError('INPUT_TOO_LARGE','请求超过大小上限')
            yield chunk

    async def body(request, model):
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
            raise JournalError('INVALID_INPUT','请求必须为 application/json')
        chunks=[]
        async for chunk in bounded_stream(request,MAX_JSON_BYTES):
            chunks.append(chunk)
        def parse():
            def object_pairs(pairs):
                result={}
                for key,value in pairs:
                    if key in result:
                        raise ValueError('duplicate key')
                    result[key]=value
                return result
            try:
                payload=json.loads(b''.join(chunks).decode('utf-8'),object_pairs_hook=object_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                return model.model_validate(payload).model_dump(exclude_unset=True)
            except (ValueError,TypeError,UnicodeError,RecursionError,ValidationError):
                raise JournalError('INVALID_INPUT','请求 JSON 或参数结构无效') from None
        return await run_in_threadpool(parse)

    def query_workspace(request):
        if set(request.query_params)-{'workspace_id'} or len(request.query_params.getlist('workspace_id'))!=1:
            raise JournalError('INVALID_INPUT','需要唯一 workspace_id 查询参数')
        workspace_id=request.query_params.get('workspace_id')
        services.workspaces.get(workspace_id)
        return workspace_id

    async def capabilities(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            return ok(await run_in_threadpool(services.capabilities))
        except Exception as exc:
            return failure(exc)

    async def workspaces(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            if request.method=='GET':
                result=await run_in_threadpool(services.workspaces.list)
            else:
                p=await body(request,CreateWorkspace)
                result=await run_in_threadpool(services.workspaces.create,**p)
            return ok(response(result))
        except Exception as exc:
            return failure(exc)

    async def workspace(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            return ok(response(await run_in_threadpool(services.workspaces.get,request.path_params['workspace_id'])))
        except Exception as exc:
            return failure(exc)

    async def link(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            p=await body(request,LinkWorkspace)
            return ok(response(await run_in_threadpool(services.workspaces.link,request.path_params['workspace_id'],**p)))
        except Exception as exc:
            return failure(exc)

    async def action(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            p=await body(request,Action)
            name=p.pop('action')
            args=p.pop('params',{})
            return ok(await run_in_threadpool(services.execute,name,args,origin='web',**p))
        except Exception as exc:
            return failure(exc)

    async def assets(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        path=None
        try:
            w=await run_in_threadpool(query_workspace,request)
            if request.method=='GET':
                return ok(response(await run_in_threadpool(services.assets.list,w,'asset')))
            encoded=request.headers.get('x-file-name','')
            if len(encoded)>4096 or not encoded or any(ord(c)<32 for c in encoded) or re.search(r'%(?![0-9a-fA-F]{2})',encoded):
                raise JournalError('INVALID_FILENAME','缺少或无效 X-File-Name')
            try:
                filename=unquote(encoded,encoding='utf-8',errors='strict')
            except UnicodeError:
                raise JournalError('INVALID_FILENAME','文件名编码无效') from None
            filename=services.assets.filename(filename)
            media_type=request.headers.get('content-type','').split(';')[0].strip().lower()
            from pathlib import Path
            if media_type not in MIMES.get(Path(filename).suffix.lower(),set()):
                raise JournalError('UNSUPPORTED_FILE','媒体类型与文件扩展名不匹配')
            file_id,path=await run_in_threadpool(services.assets.allocate,w,filename,'asset')
            stream=await run_in_threadpool(path.open,'xb')
            try:
                async for chunk in bounded_stream(request,MAX_ASSET_BYTES):
                    await run_in_threadpool(stream.write,chunk)
            finally:
                await run_in_threadpool(stream.close)
            result=await run_in_threadpool(services.assets.register,w,file_id,filename,media_type,'asset')
            path=None
            return ok(response(result))
        except Exception as exc:
            if path is not None:
                await run_in_threadpool(path.unlink,missing_ok=True)
            return failure(exc)

    async def artifacts(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            w=await run_in_threadpool(query_workspace,request)
            return ok(response(await run_in_threadpool(services.assets.list,w,'artifact')))
        except Exception as exc:
            return failure(exc)

    async def download(request):
        blocked=await authorize(request)
        if blocked is not None:
            return blocked
        try:
            kind='artifact' if 'artifact_id' in request.path_params else 'asset'
            file_id=request.path_params[kind+'_id']
            w=request.query_params.get('workspace_id')
            if set(request.query_params)-{'workspace_id'} or len(request.query_params.getlist('workspace_id'))>1:
                raise JournalError('INVALID_INPUT','下载查询参数无效')
            data=await run_in_threadpool(services.assets.get,file_id,w,kind)
            path=services.assets.path(file_id,data['filename'])
            return FileResponse(path,media_type=data['media_type'],filename=data['filename'],headers=DOWNLOAD_HEADERS,content_disposition_type='attachment')
        except Exception as exc:
            return failure(exc)

    return [Route('/api/workbench/capabilities',capabilities,methods=['GET']),
            Route('/api/workbench/workspaces',workspaces,methods=['GET','POST']),
            Route('/api/workbench/workspaces/{workspace_id}',workspace,methods=['GET']),
            Route('/api/workbench/workspaces/{workspace_id}/links',link,methods=['POST']),
            Route('/api/workbench/action',action,methods=['POST']),
            Route('/api/workbench/assets',assets,methods=['GET','POST']),
            Route('/api/workbench/assets/{asset_id}/download',download,methods=['GET']),
            Route('/api/workbench/artifacts',artifacts,methods=['GET']),
            Route('/api/workbench/artifacts/{artifact_id}/download',download,methods=['GET'])]
