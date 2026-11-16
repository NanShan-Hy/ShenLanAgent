"""Same-origin assistant API. Business mutations require explicit proposal confirmation."""
import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Body,HTTPException,Request,UploadFile,File,Form
from fastapi.responses import Response,StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .core import encode,token_hash
from .models import Model
from .ai_store import config,event
from .ai_sources import Knowledge,live_session,native_record,subject
from .ai_gateway import Gateway,GatewayFailure,validate_url
from .ai_runtime import check_sources,source_in_scope
from .ai_actions import prepare,confirm,proposal_view

EXTENSIONS={'.png','.jpg','.jpeg','.webp','.pdf','.docx','.xlsx','.csv','.tsv','.txt','.md','.json','.py','.js','.ts','.html','.css','.yaml','.yml','.sql','.xml','.log','.toml','.ini','.sh','.go','.rs','.java','.c','.cpp','.h'}
ACTIVE=('queued','running','waiting_confirmation','unknown')


class SettingsInput(Model):
    enabled:bool=False
    base_url:str='http://127.0.0.1:8094/v1'
    api_key:str=Field(default='',max_length=1024)
    model:str=Field(default='',max_length=120)
    vision_models:list[str]=Field(default_factory=list,max_length=50)
    workbench_url:str='http://127.0.0.1:8090'
    max_output_tokens:int=Field(default=4096,ge=128,le=8192)
    max_rounds:int=Field(default=8,ge=1,le=8)


class ConversationInput(Model):
    title:str=Field(default='新对话',min_length=1,max_length=200)
    mode:Literal['all','project','selected']='all'
    project_id:int|None=None
    workbench_project_ids:list[int]=Field(default_factory=list,max_length=100)


class MessageInput(Model):
    query:str=Field(min_length=1,max_length=12000)
    sources:list[dict]=Field(default_factory=list,max_length=20)
    file_ids:list[str]=Field(default_factory=list,max_length=4)


class ProposalInput(Model):
    operation:str
    project_id:int
    target_id:int|str|None=None
    arguments:dict
    sources:list[dict]=Field(default_factory=list,max_length=50)


def attach_assistant(api,db,actor,project_access):
    def title(row):
        try:return db.settings.unseal(row['title'])['title']
        except Exception:return row['title']
    def session(request):
        user=actor(request);sh=token_hash(request.cookies.get('testdock_session',''))
        return user,sh,Knowledge(db,user['id'],sh)

    def admin(request):
        user=actor(request)
        if not user['is_admin']:raise HTTPException(403,'需要系统管理员')
        return user

    def owned(request,pk):
        user,sh,k=session(request)
        row=db.one('SELECT * FROM ai_conversations WHERE id=? AND owner_id=? AND deleted=0',(pk,user['id']))
        if not row:raise HTTPException(404,'会话不存在')
        scope=json.loads(row['scope'])
        if scope['mode']=='project':project_access(user,scope['project_id'])
        return row,user,sh,k

    def task_view(request,pk):
        user,sh,k=session(request)
        t=db.one('SELECT * FROM ai_tasks WHERE id=? AND owner_id=?',(pk,user['id']))
        if not t:raise HTTPException(404,'任务不存在')
        owned(request,t['conversation_id'])
        payload=db.settings.unseal(t['secret'])
        check_sources(db,user,k,payload.get('sources',[]))
        return {n:t[n] for n in ('id','conversation_id','status','error','created_at','updated_at')}|{'usage':payload.get('usage',{}),'request_id':payload.get('request_id'),'memory_candidates':payload.get('memory_candidates',[])}

    def public_settings():
        c=config(db);c['key_configured']=bool(c.pop('api_key'))
        return c

    @api.get('/api/ai/status')
    def status(request:Request):
        user,sh,k=session(request);c=config(db);hb=db.settings.data_dir/'ai-heartbeat'
        return {'enabled':c['enabled'],'configured':bool(c['api_key'] and c['model']),'model':c['model'],
                'worker_online':hb.exists() and time.time()-hb.stat().st_mtime<15,
                'revoked':bool((db.one('SELECT revoked FROM ai_subjects WHERE user_id=?',(user['id'],)) or {}).get('revoked')),
                'vision':c['model'] in c['vision_models'],'is_admin':bool(user['is_admin'])}

    @api.get('/api/ai/settings')
    def settings_view(request:Request):admin(request);return public_settings()

    @api.put('/api/ai/settings')
    def settings_save(body:SettingsInput,request:Request):
        user=admin(request);data=body.model_dump();data['base_url']=validate_url(data['base_url']);data['workbench_url']=validate_url(data['workbench_url'],True)
        data['api_key']=data['api_key'] or config(db)['api_key']
        if data['enabled'] and (not data['api_key'] or not data['model']):raise HTTPException(422,'启用前需填写专用应用Key及模型')
        db.execute('INSERT INTO ai_settings VALUES(1,?,1) ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,revision=revision+1',(db.settings.seal(data),))
        db.audit(user['id'],None,'ai.settings.update');return public_settings()

    @api.get('/api/ai/models')
    def models(request:Request):
        user=actor(request)
        if not config(db)['api_key']:return {'data':[],'configured':False}
        try:return Gateway(db,user['id']).request('GET','/v1/models')
        except GatewayFailure as exc:raise HTTPException(503,str(exc))

    @api.post('/api/ai/reconnect')
    def reconnect(request:Request):
        user,sh,k=session(request);subject(db,user['id'])
        db.execute('UPDATE ai_subjects SET revoked=0 WHERE user_id=?',(user['id'],))
        data=k.call('context');db.audit(user['id'],None,'ai.reconnect');return data

    @api.get('/api/ai/context')
    def context(request:Request):
        user,sh,k=session(request);return k.call('context')

    @api.post('/api/ai/search')
    def search(request:Request,body:dict=Body(...)):
        user,sh,k=session(request)
        return k.call('search',{'query':str(body.get('query',''))[:2000],'project_ids':body.get('project_ids',[]),'limit':12,'semantic':True,'external':False})

    @api.post('/api/ai/source')
    def source(request:Request,body:dict=Body(...)):
        user,sh,k=session(request)
        if body.get('system')=='testdock':return native_record(db,user['id'],body,project_access)
        if body.get('system')=='attachment':
            row,user,meta=get_file(request,body['id'])
            return {'title':row['name'],'system':'attachment','text':'附件原文可从附件详情下载；分段：'+', '.join(meta.get('selected_sections',[]))}
        return k.call('validate',{'sources':[body],'external':False})['items'][0]

    @api.put('/api/ai/source-policy')
    def source_policy(request:Request,body:dict=Body(...)):
        user,sh,k=session(request);return k.call('record-policy',body)

    @api.put('/api/ai/project-policy')
    def policy(request:Request,body:dict=Body(...)):
        user,sh,k=session(request)
        return k.call('policy',body)

    @api.post('/api/ai/memory')
    def memory(request:Request,body:dict=Body(...)):
        user,sh,k=session(request);return k.call('memory',body)

    @api.get('/api/ai/conversations')
    def conversations(request:Request):
        user=actor(request)
        return [{k:r[k] for k in ('id','created_at','updated_at')}|{'title':title(r),'scope':json.loads(r['scope'])} for r in db.all('SELECT * FROM ai_conversations WHERE owner_id=? AND deleted=0 ORDER BY updated_at DESC',(user['id'],))]

    @api.post('/api/ai/conversations',status_code=201)
    def create_conversation(body:ConversationInput,request:Request):
        user=actor(request)
        if body.mode=='project':
            if body.project_id is None:raise HTTPException(422,'请选择项目')
            project_access(user,body.project_id)
        if body.mode=='selected':
            if not body.workbench_project_ids:raise HTTPException(422,'请选择知识项目')
            _,_,k=session(request);allowed={p['id'] for p in k.call('context')['projects']}
            if set(body.workbench_project_ids)-allowed:raise HTTPException(403,'知识项目不可访问')
        pk=uuid.uuid4().hex;now=time.time()
        db.execute('INSERT INTO ai_conversations(id,owner_id,title,scope,secret,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (pk,user['id'],db.settings.seal({'title':body.title}),encode({'mode':body.mode,'project_id':body.project_id,'workbench_project_ids':body.workbench_project_ids}),db.settings.seal({'sources':[]}),now,now))
        return {'id':pk}

    @api.get('/api/ai/conversations/{pk}')
    def conversation(pk:str,request:Request):
        row,user,sh,k=owned(request,pk);messages=[];hidden=0
        for m in db.all('SELECT * FROM ai_messages WHERE conversation_id=? ORDER BY created_at,id',(pk,)):
            value=db.settings.unseal(m['secret'])
            try:check_sources(db,user,k,value.get('sources',[]))
            except HTTPException:hidden+=1;continue
            messages.append({'id':m['id'],'role':m['role'],'created_at':m['created_at'],**value})
        tasks=[]
        for t in db.all('SELECT id FROM ai_tasks WHERE conversation_id=? ORDER BY created_at DESC LIMIT 20',(pk,)):
            try:tasks.append(task_view(request,t['id']))
            except HTTPException:continue
        proposals=[]
        for p in db.all('SELECT * FROM ai_proposals WHERE conversation_id=? ORDER BY created_at',(pk,)):
            try:
                project_access(user,p['project_id']);data=db.settings.unseal(p['secret']);check_sources(db,user,k,data.get('sources',[]))
            except HTTPException:
                if p['status']!='applied':continue
                try:project_access(user,p['project_id'])
                except HTTPException:continue
                value=proposal_view(db,p);value.pop('sources',None);value['before']=None;proposals.append(value)
                continue
            proposals.append(proposal_view(db,p))
        files=[{'id':f['id'],'name':f['name'],'bytes':f['bytes'],'selected_sections':db.settings.unseal(f['secret']).get('selected_sections',[])} for f in db.all('SELECT * FROM ai_files WHERE conversation_id=? AND owner_id=? AND deleted=0',(pk,user['id'])) if file_allowed(f,user)]
        return {'id':pk,'title':title(row),'scope':json.loads(row['scope']),'messages':messages,'tasks':tasks,'proposals':proposals,'files':files,'hidden_messages':hidden}

    def file_allowed(f,user):
        try:
            meta=db.settings.unseal(f['secret']);project_access(user,meta['project_id']);return True
        except HTTPException:return False

    @api.post('/api/ai/conversations/{pk}/messages',status_code=201)
    def send(pk:str,body:MessageInput,request:Request):
        row,user,sh,k=owned(request,pk);live_session(db,user['id'],sh)
        c=config(db)
        if not c['enabled'] or not c['api_key'] or not c['model']:raise HTTPException(409,'AI接入尚未启用，请管理员先配置；本地检索仍可使用')
        refs=[]
        for ref in body.sources:
            if ref.get('system')=='testdock':refs.append(native_record(db,user['id'],ref,project_access))
            elif ref.get('system')=='workbench':refs+=k.call('validate',{'sources':[ref]})['items']
            else:raise HTTPException(422,'来源系统无效')
        for fid in body.file_ids:
            f=db.one('SELECT * FROM ai_files WHERE id=? AND owner_id=? AND conversation_id=? AND deleted=0',(fid,user['id'],pk))
            if not f or not file_allowed(f,user):raise HTTPException(404,'附件不存在或不可访问')
        if refs:
            scope=json.loads(row['scope']);context=k.call('context')
            if any(not source_in_scope(ref,scope,context) for ref in refs):raise HTTPException(403,'来源超出固定会话范围，请新建对话选择合适范围')
        now=time.time();tid=uuid.uuid4().hex
        with db.transaction():
            if db.one("SELECT id FROM ai_tasks WHERE conversation_id=? AND status IN ('queued','running','waiting_confirmation','unknown')",(pk,)):raise HTTPException(409,'当前会话有任务待完成、确认或恢复')
            payload={'query':body.query,'explicit_sources':refs,'file_ids':list(dict.fromkeys(body.file_ids))}
            db.execute('INSERT INTO ai_tasks(id,conversation_id,owner_id,session_hash,secret,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(tid,pk,user['id'],sh,db.settings.seal(payload),now,now))
            db.execute('INSERT INTO ai_messages VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,pk,tid,'user',db.settings.seal({'text':body.query,'sources':refs,'file_ids':body.file_ids}),now))
            display=body.query[:80] if title(row)=='新对话' else title(row)
            db.execute('UPDATE ai_conversations SET title=?,updated_at=? WHERE id=?',(db.settings.seal({'title':display}),now,pk))
        event(db,tid,'queued');return {'id':tid,'status':'queued'}

    @api.post('/api/ai/test',status_code=201)
    def smoke(request:Request):
        admin(request);conv=create_conversation(ConversationInput(title='显式模型联调'),request)
        result=send(conv['id'],MessageInput(query='请用中文回复：连接成功。不要读取资料或调用工具。'),request)
        row=db.one('SELECT secret FROM ai_tasks WHERE id=?',(result['id'],));payload=db.settings.unseal(row['secret']);payload['max_output_tokens']=128
        db.execute('UPDATE ai_tasks SET secret=? WHERE id=?',(db.settings.seal(payload),result['id']))
        return {**result,'conversation_id':conv['id']}

    @api.get('/api/ai/tasks/{pk}')
    def task(pk:str,request:Request):return task_view(request,pk)

    @api.get('/api/ai/tasks/{pk}/events')
    async def events(pk:str,request:Request):
        await run_in_threadpool(task_view,request,pk)
        try:cursor=max(0,int(request.headers.get('last-event-id',request.query_params.get('cursor','0'))))
        except ValueError:raise HTTPException(422,'无效事件游标')
        async def stream():
            nonlocal cursor
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                if await request.is_disconnected():return
                try:view=await run_in_threadpool(task_view,request,pk)
                except HTTPException:
                    yield 'event: revoked\ndata: {"type":"revoked","text":"来源权限或登录已失效"}\n\n';return
                rows=db.all('SELECT id,secret FROM ai_events WHERE task_id=? AND id>? ORDER BY id LIMIT 100',(pk,cursor))
                for row in rows:
                    cursor=row['id'];value=db.settings.unseal(row['secret'])
                    yield 'id: '+str(cursor)+'\ndata: '+encode(value)+'\n\n'
                if view['status'] not in ('queued','running'):
                    yield 'event: state\ndata: '+encode({'status':view['status']})+'\n\n';return
                yield ': keepalive\n\n';await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @api.post('/api/ai/tasks/{pk}/cancel')
    def cancel(pk:str,request:Request):
        view=task_view(request,pk)
        with db.transaction():
            db.execute("UPDATE ai_tasks SET cancel_requested=1,status=CASE WHEN status IN ('queued','waiting_confirmation') THEN 'cancelled' ELSE status END WHERE id=?",(pk,))
            db.execute("UPDATE ai_proposals SET status='rejected' WHERE task_id=? AND status='pending'",(pk,))
        return {'ok':True}

    @api.post('/api/ai/conversations/{pk}/recover')
    def recover(pk:str,request:Request,body:dict=Body(...)):
        row,user,sh,k=owned(request,pk)
        if body.get('acknowledge_unknown') is not True:raise HTTPException(422,'请明确确认未知结果不会重放')
        state=db.settings.unseal(row['secret']);gateway=Gateway(db,user['id'],row['epoch'])
        if state.get('gateway'):
            try:
                current=gateway.request('GET','/api/v1/conversations/'+state['gateway']['id'])
                if current['state']=='needs_recovery':current=gateway.request('POST','/api/v1/conversations/'+current['id']+'/recover',{'acknowledge_unknown':True})
                if current.get('pending_calls'):gateway.request('POST','/api/v1/conversations/'+state['gateway']['id']+'/cancel-tools',{'acknowledge_cancellation':True})
            except GatewayFailure as exc:raise HTTPException(409,str(exc))
        state.pop('gateway',None)
        db.execute('UPDATE ai_conversations SET epoch=epoch+1,secret=? WHERE id=?',(db.settings.seal(state),pk))
        db.execute("UPDATE ai_tasks SET status='cancelled',error='用户确认未知状态后新建上下文；原调用未重放' WHERE conversation_id=? AND status IN ('unknown','waiting_confirmation')",(pk,))
        db.execute("UPDATE ai_proposals SET status='rejected' WHERE conversation_id=? AND status='pending'",(pk,))
        return {'ok':True,'replayed':False}

    @api.post('/api/ai/conversations/{pk}/proposals',status_code=201)
    def proposal(pk:str,body:ProposalInput,request:Request):
        row,user,sh,k=owned(request,pk);check_sources(db,user,k,body.sources,target=body.project_id)
        return prepare(db,user,project_access,pk,body.operation,body.project_id,body.target_id,body.arguments,body.sources)

    @api.post('/api/ai/proposals/{pk}/confirm')
    def apply(pk:str,request:Request):
        user,sh,k=session(request);live_session(db,user['id'],sh)
        p=db.one('SELECT * FROM ai_proposals WHERE id=? AND owner_id=?',(pk,user['id']))
        if not p:raise HTTPException(404,'提案不存在')
        owned(request,p['conversation_id'])
        result=confirm(api,db,request,user,project_access,pk,lambda refs,target:check_sources(db,user,k,refs,external=bool(p['task_id']),target=target))
        resume(p,sh);return result

    def resume(p,sh):
        if p['task_id'] and not db.one("SELECT 1 FROM ai_proposals WHERE task_id=? AND status='pending'",(p['task_id'],)):
            db.execute("UPDATE ai_tasks SET status='queued',session_hash=?,updated_at=? WHERE id=? AND status='waiting_confirmation'",(sh,time.time(),p['task_id']))

    @api.post('/api/ai/proposals/{pk}/reject')
    def reject(pk:str,request:Request):
        user,sh,k=session(request);p=db.one('SELECT * FROM ai_proposals WHERE id=? AND owner_id=?',(pk,user['id']))
        if not p:raise HTTPException(404,'提案不存在')
        owned(request,p['conversation_id'])
        if p['status']!='pending':raise HTTPException(409,'该提案已处理，不能撤销已发生的操作')
        db.execute("UPDATE ai_proposals SET status='rejected' WHERE id=? AND status='pending'",(pk,));resume(p,sh)
        return {'ok':True}

    def store_file(row,user,filename,raw,pid,consent,source=None):
        project_access(user,pid)
        filename=Path(filename or 'attachment.txt').name
        if not filename or len(filename)>160 or Path(filename).suffix.lower() not in EXTENSIONS:raise HTTPException(422,'不支持的附件类型或名称')
        if not raw or len(raw)>20*1024**2:raise HTTPException(413,'附件需为1字节至20MiB')
        if shutil.disk_usage(db.settings.data_dir).free<db.settings.min_free_bytes+len(raw)*2:raise HTTPException(507,'可用空间不足，暂停附件写入')
        directory=db.settings.data_dir/'ai';directory.mkdir(mode=0o700,exist_ok=True);directory.chmod(0o700)
        fid=uuid.uuid4().hex;path=directory/fid
        with path.open('xb') as f:f.write(db.settings.cipher.encrypt(raw))
        path.chmod(0o600)
        try:
            db.execute('INSERT INTO ai_files(id,owner_id,conversation_id,name,bytes,sha256,secret,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (fid,user['id'],row['id'],filename,len(raw),hashlib.sha256(raw).hexdigest(),db.settings.seal({'project_id':pid,'consent':consent,'source':source}),time.time()))
        except BaseException:path.unlink();raise
        return {'id':fid,'name':filename,'bytes':len(raw)}

    @api.post('/api/ai/conversations/{pk}/files',status_code=201)
    async def upload(pk:str,request:Request,file:UploadFile=File(...),project_id:int=Form(...),external_consent:bool=Form(False)):
        row,user,sh,k=await run_in_threadpool(owned,request,pk);raw=await file.read(20*1024**2+1)
        return await run_in_threadpool(store_file,row,user,file.filename,raw,project_id,external_consent)

    @api.post('/api/ai/conversations/{pk}/screenshot',status_code=201)
    def screenshot(pk:str,request:Request,body:dict=Body(...)):
        row,user,sh,k=owned(request,pk);run=db.one('SELECT * FROM runs WHERE id=?',(body.get('run_id'),))
        if not run:raise HTTPException(404,'报告不存在')
        project_access(user,run['project_id'])
        root=(db.settings.data_dir/'runs'/run['id']).resolve();path=(root/str(body.get('filename',''))).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp') or not path.is_file():raise HTTPException(404,'截图不存在')
        if path.stat().st_size>20*1024**2:raise HTTPException(413,'截图超过20MiB')
        source=native_record(db,user['id'],{'kind':'runs','id':run['id']},project_access)
        return store_file(row,user,path.name,path.read_bytes(),run['project_id'],body.get('external_consent') is True,source)

    def get_file(request,pk):
        user=actor(request);row=db.one('SELECT * FROM ai_files WHERE id=? AND owner_id=? AND deleted=0',(pk,user['id']))
        if not row or not file_allowed(row,user):raise HTTPException(404,'附件不存在')
        owned(request,row['conversation_id']);return row,user,db.settings.unseal(row['secret'])

    @api.get('/api/ai/files/{pk}')
    def file_info(pk:str,request:Request):
        row,user,meta=get_file(request,pk);info={}
        for remote in meta.get('gateway',[])[-1:]:
            try:info=Gateway(db,user['id'],remote['epoch']).request('GET','/v1/files/'+remote['id'])
            except GatewayFailure as exc:info={'error':str(exc)}
        return {'id':pk,'name':row['name'],'bytes':row['bytes'],'selected_sections':meta.get('selected_sections',[]),'gateway':info,'consent':meta['consent']}

    @api.put('/api/ai/files/{pk}')
    def select_file(pk:str,request:Request,body:dict=Body(...)):
        row,user,meta=get_file(request,pk);sections=body.get('selected_sections',[])
        if not isinstance(sections,list) or len(sections)>100 or any(not isinstance(s,str) or len(s)>100 for s in sections):raise HTTPException(422,'附件分段无效')
        if 'external_consent' in body and type(body['external_consent']) is not bool:raise HTTPException(422,'附件外发授权无效')
        meta['selected_sections']=sections;meta['consent']=body.get('external_consent',meta['consent'])
        db.execute('UPDATE ai_files SET secret=? WHERE id=?',(db.settings.seal(meta),pk));return {'ok':True}

    @api.get('/api/ai/files/{pk}/content')
    def download(pk:str,request:Request):
        row,user,meta=get_file(request,pk)
        return Response(db.settings.cipher.decrypt((db.settings.data_dir/'ai'/pk).read_bytes()),media_type='application/octet-stream',headers={'Content-Disposition':'attachment'})

    def purge_file(row,user):
        meta=db.settings.unseal(row['secret'])
        for remote in meta.get('gateway',[]):
            try:Gateway(db,user['id'],remote['epoch']).request('DELETE','/v1/files/'+remote['id'])
            except GatewayFailure as exc:raise HTTPException(409,'网关附件清理未完成，请重试：'+str(exc))
        db.execute('UPDATE ai_files SET deleted=1 WHERE id=?',(row['id'],));(db.settings.data_dir/'ai'/row['id']).unlink(missing_ok=True)

    @api.delete('/api/ai/files/{pk}')
    def delete_file(pk:str,request:Request):
        row,user,meta=get_file(request,pk);purge_file(row,user);return {'ok':True}

    @api.delete('/api/ai/conversations/{pk}')
    def delete_conversation(pk:str,request:Request):
        row,user,sh,k=owned(request,pk)
        if db.one("SELECT 1 FROM ai_tasks WHERE conversation_id=? AND status IN ('queued','running')",(pk,)):raise HTTPException(409,'请先取消运行中的任务')
        state=db.settings.unseal(row['secret'])
        for remote in state.get('gateway_sessions',[]):
            try:Gateway(db,user['id'],remote['epoch']).request('DELETE','/api/v1/conversations/'+remote['id'])
            except GatewayFailure as exc:raise HTTPException(409,'网关会话清理未完成，请重试：'+str(exc))
        for f in db.all('SELECT * FROM ai_files WHERE conversation_id=? AND deleted=0',(pk,)):purge_file(f,user)
        with db.transaction():
            db.execute("UPDATE ai_proposals SET status='rejected',secret=? WHERE conversation_id=?",(db.settings.seal({}),pk))
            db.execute("UPDATE ai_tasks SET status='cancelled',secret=? WHERE conversation_id=?",(db.settings.seal({}),pk))
            db.execute('DELETE FROM ai_events WHERE task_id IN(SELECT id FROM ai_tasks WHERE conversation_id=?)',(pk,))
            db.execute('DELETE FROM ai_messages WHERE conversation_id=?',(pk,))
            db.execute('UPDATE ai_conversations SET deleted=1,secret=?,title=? WHERE id=?',(db.settings.seal({}),'已清理',pk))
        db.audit(user['id'],None,'ai.conversation.delete',pk);return {'ok':True,'financial_records_preserved':True}
