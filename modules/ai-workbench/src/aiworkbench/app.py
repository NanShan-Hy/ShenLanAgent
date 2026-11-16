from __future__ import annotations
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException,Request,UploadFile,File,Form,Query,Body
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from fastapi.responses import JSONResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from .core import Database,Settings,dump,digest,password_matches,password_hash,write_private
from .models import (Login,UserInput,ProjectInput,MemberInput,RecordInput,SearchInput,TaskInput,ModelInput,EditInput,ReviewInput,ShareInput,BindInput,IngestInput,RunInput)
from .access import Access,Bridge,create_record
from .retrieval import search,index_record
from .worker import queue,notify,import_report,sync_testdock
from .documents import approved_path,ALLOWED,MAX_FILE


def create_app(settings=None):
    settings=settings or Settings.load();db=Database(settings);db.initialize()
    api=FastAPI(title='AI Workbench',version='0.2.0',docs_url='/api/docs',openapi_url='/api/openapi.json')
    api.state.db=db
    @api.exception_handler(ValidationError)
    async def invalid_model(request,exc):return JSONResponse({'detail':'集成输入字段不符合格式'},status_code=422)
    @api.exception_handler(RequestValidationError)
    async def invalid(request,exc):return JSONResponse({'detail':[{'loc':e['loc'],'msg':e['msg']} for e in exc.errors()]},status_code=422)
    @api.exception_handler(sqlite3.IntegrityError)
    async def integrity(request,exc):return JSONResponse({'detail':'名称冲突或资源仍被引用'},status_code=409)
    @api.middleware('http')
    async def guard(request,call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            if not request.url.path.startswith('/api/integrations/testdock/') and request.headers.get('x-requested-with')!='AIWorkbench':return JSONResponse({'detail':'缺少请求标识'},status_code=403)
            origin=request.headers.get('origin')
            if origin and urlparse(origin).netloc!=request.headers.get('host'):return JSONResponse({'detail':'来源不匹配'},status_code=403)
            limit=21*1024**2 if request.url.path.endswith('/uploads') else 2*1024**2
            data=bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data)>limit:return JSONResponse({'detail':'请求体积超过上限'},status_code=413)
            request._body=bytes(data)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';response.headers['X-Frame-Options']='DENY'
        return response

    def actor(request):
        user=db.one('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',(digest(request.cookies.get('ai_session','')),time.time()))
        if not user:raise HTTPException(401,'请先登录')
        return user
    def public(user):return {k:user[k] for k in ('id','username','display_name','is_admin','active')}
    def admin(request):
        user=actor(request)
        if not user['is_admin']:raise HTTPException(403,'需要工作区管理员')
        return user
    def access(request):return Access(db,actor(request))
    def editable(a,r):
        if r['visibility']=='workspace' and a.user['is_admin']:return
        if r['owner_id']==a.user['id']:
            if r['project_id'] is not None:a.project(r['project_id'],'editor')
        elif r['project_id'] is not None:a.project(r['project_id'],'admin')
        else:raise HTTPException(403,'仅作者可操作此内容')
    def task_view(a,t):
        if t['owner_id']!=a.user['id']:raise HTTPException(404,'任务不存在')
        payload=settings.unseal(t['payload'])
        for pid in payload.get('project_ids',[]):a.project(pid)
        for rid in payload.get('source_ids',[]):a.record(rid)
        result=settings.unseal(t['result']) if t['result']!='{}' else {}
        for cite in result.get('citations',[]):a.record(cite['record_id'])
        if result.get('record_id'):a.record(result['record_id'])
        return {k:t[k] for k in ('id','kind','status','progress','error','created_at','started_at','finished_at','used_tokens')}|{'result':result}
    def record_out(a,r):
        item=a.record(r['id']);item['role']=a.roles.get(item['project_id'])
        evidence=db.one('SELECT citations FROM record_evidence WHERE record_id=?',(r['id'],))
        if evidence:item['citations']=json.loads(evidence['citations'])
        return item

    @api.get('/api/v1/health')
    def health():
        heartbeat=settings.data_dir/'worker-heartbeat'
        return {'status':'ok','worker_online':heartbeat.exists() and time.time()-heartbeat.stat().st_mtime<15}
    @api.post('/api/v1/auth/login')
    def login(body:Login,request:Request):
        address=request.client.host if request.client else 'unknown';now=time.time()
        attempt=db.one('SELECT * FROM login_attempts WHERE address=?',(address,))
        if attempt and attempt['n']>=10 and now-attempt['since']<300:raise HTTPException(429,'请5分钟后重试')
        user=db.one('SELECT * FROM users WHERE username=? AND active=1',(body.username,))
        if not user or not password_matches(body.password,user['password_hash']):
            n=attempt['n']+1 if attempt and now-attempt['since']<300 else 1
            since=attempt['since'] if n>1 else now
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(address,n,since));raise HTTPException(401,'账号或密码不正确')
        db.execute('DELETE FROM login_attempts WHERE address=?',(address,));token=secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),user['id'],now+43200))
        response=JSONResponse(public(user));response.set_cookie('ai_session',token,httponly=True,samesite='lax',max_age=43200)
        return response
    @api.post('/api/v1/auth/logout')
    def logout(request:Request):
        db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get('ai_session','')),))
        response=JSONResponse({'ok':True});response.delete_cookie('ai_session');return response
    @api.get('/api/v1/me')
    def me(request:Request):
        user=actor(request);binding=db.one('SELECT external_user_id FROM bindings WHERE user_id=?',(user['id'],))
        return public(user)|{'testdock_bound':bool(binding)}
    @api.post('/api/v1/me/password')
    def change_password(request:Request,body:dict=Body(...)):
        user=actor(request);new=body.get('new_password')
        if not isinstance(new,str) or not 10<=len(new)<=256:raise HTTPException(400,'密码需10至256位')
        if not password_matches(body.get('old_password'),user['password_hash']):raise HTTPException(400,'原密码不正确')
        with db.transaction() as c:
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(new),user['id']));c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        return {'ok':True}
    @api.get('/api/v1/users')
    def users(request:Request):admin(request);return [public(u) for u in db.all('SELECT * FROM users')]
    @api.post('/api/v1/users',status_code=201)
    def add_user(body:UserInput,request:Request):
        user=admin(request)
        pk=db.execute('INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',(body.username,body.display_name,password_hash(body.password),body.is_admin,time.time()))
        db.audit(user['id'],'user.create',str(pk));return public(db.one('SELECT * FROM users WHERE id=?',(pk,)))
    @api.patch('/api/v1/users/{pk}')
    def update_user(pk:int,request:Request,body:dict=Body(...)):
        user=admin(request);target=db.one('SELECT * FROM users WHERE id=?',(pk,))
        if not target:raise HTTPException(404,'用户不存在')
        if set(body)-{'active','password'}:raise HTTPException(400,'不支持的字段')
        active=body.get('active',bool(target['active']))
        if type(active) is not bool:raise HTTPException(400,'active需布尔值')
        if not active and target['is_admin'] and db.one('SELECT count(*) n FROM users WHERE is_admin=1 AND active=1')['n']<=1:raise HTTPException(409,'需保留有效管理员')
        if not active:
            for owned in db.all("SELECT m.project_id FROM members m JOIN projects p ON p.id=m.project_id WHERE m.user_id=? AND m.role='admin' AND p.external_id IS NULL",(pk,)):
                if db.one("SELECT count(*) n FROM members m JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND m.role='admin' AND u.active=1",(owned['project_id'],))['n']<=1:raise HTTPException(409,'请先移交此账号管理的项目')
        hashed=target['password_hash']
        if 'password' in body:
            if not isinstance(body['password'],str) or not 10<=len(body['password'])<=256:raise HTTPException(400,'密码长度无效')
            hashed=password_hash(body['password'])
        with db.transaction() as c:
            c.execute('UPDATE users SET active=?,password_hash=? WHERE id=?',(active,hashed,pk))
            if not active or 'password' in body:c.execute('DELETE FROM sessions WHERE user_id=?',(pk,))
        db.audit(user['id'],'user.update',str(pk));return {'ok':True}
    @api.get('/api/v1/projects')
    def projects(request:Request):
        a=access(request)
        return {'items':[db.one('SELECT * FROM projects WHERE id=?',(pk,))|{'role':role} for pk,role in a.roles.items()], 'source_unavailable':a.upstream_unavailable}
    @api.post('/api/v1/projects',status_code=201)
    def create_project(body:ProjectInput,request:Request):
        user=admin(request)
        with db.transaction() as c:
            pk=c.execute('INSERT INTO projects(name,description,created_at) VALUES(?,?,?)',(body.name,body.description,time.time())).lastrowid
            c.execute('INSERT INTO members VALUES(?,?,?)',(pk,user['id'],'admin'))
        return {'id':pk,**body.model_dump(),'role':'admin'}
    @api.get('/api/v1/projects/{pk}/members')
    def members(pk:int,request:Request):
        a=access(request);p=a.project(pk)
        if p['external_id'] is not None:
            items=[]
            for u in db.all('SELECT u.* FROM users u JOIN bindings b ON b.user_id=u.id WHERE u.active=1'):
                candidate=Access(db,u)
                if pk in candidate.roles:items.append({'id':u['id'],'username':u['username'],'display_name':u['display_name'],'role':candidate.roles[pk]})
            return {'external':True,'items':items}

        return {'external':False,'items':db.all('SELECT u.id,u.username,u.display_name,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE m.project_id=?',(pk,))}
    @api.get('/api/v1/projects/{pk}/available-users')
    def available_users(pk:int,request:Request):
        access(request).project(pk,'admin')
        return [public(u) for u in db.all('SELECT * FROM users WHERE active=1')]
    @api.put('/api/v1/projects/{pk}/members')
    def member(pk:int,body:MemberInput,request:Request):
        a=access(request);p=a.project(pk,'admin')
        if p['external_id'] is not None:raise HTTPException(400,'来源项目成员请在TestDock管理')
        if not db.one('SELECT id FROM users WHERE id=? AND active=1',(body.user_id,)):raise HTTPException(404,'用户不存在')
        with db.transaction() as conn:
            old=conn.execute('SELECT role FROM members WHERE project_id=? AND user_id=?',(pk,body.user_id)).fetchone()
            count=conn.execute("SELECT count(*) FROM members m JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND m.role='admin' AND u.active=1",(pk,)).fetchone()[0]
            if old and old['role']=='admin' and body.role!='admin' and count<=1:raise HTTPException(409,'需保留项目管理员')
            conn.execute('INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role',(pk,body.user_id,body.role))
        db.audit(a.user['id'],'member.update',str(pk),str(body.user_id)+':'+body.role);return {'ok':True}
    @api.delete('/api/v1/projects/{pk}/members/{uid}')
    def remove_member(pk:int,uid:int,request:Request):
        a=access(request);p=a.project(pk,'admin')
        if p['external_id'] is not None:raise HTTPException(400,'请在来源平台管理成员')
        with db.transaction() as conn:
            old=conn.execute('SELECT role FROM members WHERE project_id=? AND user_id=?',(pk,uid)).fetchone()
            count=conn.execute("SELECT count(*) FROM members m JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND m.role='admin' AND u.active=1",(pk,)).fetchone()[0]
            if old and old['role']=='admin' and count<=1:raise HTTPException(409,'需保留项目管理员')
            conn.execute('DELETE FROM members WHERE project_id=? AND user_id=?',(pk,uid))
        db.audit(a.user['id'],'member.remove',str(pk),str(uid));return {'ok':True}
    @api.put('/api/v1/projects/{pk}/policy')
    def policy(pk:int,request:Request,body:dict=Body(...)):
        a=access(request);a.project(pk,'admin')
        if body.get('egress') not in ('local','external'):raise HTTPException(400,'无效外发策略')
        db.execute('UPDATE projects SET egress=? WHERE id=?',(body['egress'],pk));db.audit(a.user['id'],'policy.update',str(pk),body['egress']);return {'ok':True}
    @api.get('/api/v1/projects/{pk}/subscription')
    def subscription(pk:int,request:Request):
        a=access(request);a.project(pk)
        row=db.one('SELECT enabled FROM subscriptions WHERE user_id=? AND project_id=?',(a.user['id'],pk))
        auto=db.one('SELECT enabled,model_id FROM automations WHERE project_id=?',(pk,))
        return {'enabled':bool(row and row['enabled']),'automation':auto}
    @api.put('/api/v1/projects/{pk}/subscription')
    def subscribe(pk:int,request:Request,body:dict=Body(...)):
        a=access(request);project=a.project(pk)
        if not project['external_id'] or not a.external_user:raise HTTPException(400,'仅支持绑定的TestDock项目')
        if type(body.get('enabled')) is not bool:raise HTTPException(400,'需要布尔值')
        db.execute('INSERT INTO subscriptions VALUES(?,?,?,?) ON CONFLICT(user_id,project_id) DO UPDATE SET enabled=excluded.enabled,last_checked=excluded.last_checked',(a.user['id'],pk,body['enabled'],time.time()))
        return {'ok':True}
    @api.put('/api/v1/projects/{pk}/automation')
    def automation(pk:int,request:Request,body:dict=Body(...)):
        a=access(request);project=a.project(pk,'admin')
        if not project['external_id'] or not a.external_user:raise HTTPException(400,'请选择绑定的TestDock项目')
        enabled=body.get('enabled')
        if type(enabled) is not bool:raise HTTPException(400,'需要布尔值')
        mid=body.get('model_id')
        model=db.one('SELECT * FROM models WHERE id=? AND enabled=1',(mid,))
        if enabled and not model:raise HTTPException(400,'需启用可用模型')
        if enabled and not model['internal'] and project['egress']!='external':raise HTTPException(400,'此项目尚未授权外部模型处理')
        db.execute('INSERT INTO automations VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET owner_id=excluded.owner_id,model_id=excluded.model_id,enabled=excluded.enabled,last_checked=excluded.last_checked',(pk,a.user['id'],mid,enabled,time.time()))
        db.audit(a.user['id'],'automation.configure',str(pk),str(enabled));return {'ok':True}
    @api.post('/api/v1/testdock/bind')
    def bind(body:BindInput,request:Request):
        user=actor(request);result=Bridge(settings).call('bind',{'code':body.code})
        if db.one('SELECT 1 FROM bindings WHERE external_user_id=? AND user_id!=?',(result['user_id'],user['id'])):raise HTTPException(409,'来源账号已经绑定其他工作台账号')
        db.execute('INSERT INTO bindings(user_id,external_user_id,created_at,secret,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET external_user_id=excluded.external_user_id,created_at=excluded.created_at,secret=excluded.secret,expires_at=excluded.expires_at',(user['id'],result['user_id'],time.time(),settings.seal({'delegation_token':result['delegation_token']}),result['expires_at']))
        db.execute('UPDATE td_identities SET user_id=? WHERE external_user_id=?',(user['id'],result['user_id']))
        db.execute('DELETE FROM td_sessions WHERE external_user_id=?',(result['user_id'],))
        db.audit(user['id'],'testdock.bind',str(result['user_id']));return {'ok':True}
    @api.post('/api/v1/testdock/sync',status_code=201)
    def sync_sources(request:Request):
        a=access(request)
        if not a.external_user:raise HTTPException(400,'请先绑定来源账号')
        pk=queue(db,a.user,'sync',{'project_ids':[p for p in a.roles if db.one('SELECT external_id FROM projects WHERE id=?',(p,))['external_id'] is not None],'source_ids':[]})
        return {'id':pk}
    @api.post('/api/v1/testdock/evidence',status_code=201)
    def evidence(body:RunInput,request:Request):
        a=access(request)
        if not a.external_user:raise HTTPException(400,'请先绑定TestDock账号')
        run=a.bridge.call('evidence',{'user_id':a.external_user,'run_id':body.run_id})
        rid=import_report(db,a.user,a,run)
        return {'id':rid,'project_id':db.one('SELECT project_id FROM records WHERE id=?',(rid,))['project_id']}
    @api.get('/api/v1/records')
    def records(request:Request,kind:str='',project_id:int|None=None,page:int=Query(1,ge=1)):
        a=access(request)
        if project_id is not None:a.project(project_id)
        items=[]
        kinds={'document','case','suite','environment','run_index','report'} if kind=='knowledge' else {kind}
        for row in db.all('SELECT id,kind,project_id,updated_at FROM records WHERE deleted=0 ORDER BY updated_at DESC'):
            if (not kind or row['kind'] in kinds) and (project_id is None or row['project_id']==project_id) and a.can_see(row['id']):
                item=record_out(a,row);item['body']=item['body'][:400];items.append(item)
        return {'items':items[(page-1)*30:page*30],'total':len(items)}
    @api.post('/api/v1/records',status_code=201)
    def add_record(body:RecordInput,request:Request):
        a=access(request);payload=body.model_dump(exclude_none=True)
        if body.thread_id and body.kind!='reply':
            if body.kind!='chat_user' or not db.one('SELECT 1 FROM conversations WHERE id=? AND owner_id=?',(body.thread_id,a.user['id'])):raise HTTPException(403,'不能向他人会话写入内容')
        if body.kind=='chat_user' and body.visibility!='private':raise HTTPException(400,'对话消息先保持私有，请通过显式共享发布')
        if body.kind=='reply':
            if not body.thread_id:raise HTTPException(400,'回复需要主题')
            thread=a.record(body.thread_id)
            if thread['kind'] not in ('discussion','reply') or thread['project_id']!=body.project_id:raise HTTPException(400,'回复主题不匹配')
            payload['sources'].append({'id':thread['id'],'version':thread['version']})
        pk=create_record(db,a.user,payload,a);index_record(db,pk)
        recipients=set(body.mentions)
        if body.kind=='reply':recipients.add(thread['owner_id'])
        for uid in recipients:
            if uid!=a.user['id']:notify(db,uid,pk,'新的项目消息：'+body.title)
        if body.kind=='memory' and body.project_id:
            for uid in db.all("SELECT user_id FROM members WHERE project_id=? AND role='admin'",(body.project_id,)):notify(db,uid['user_id'],pk,'记忆待审核')
        return {'id':pk}
    @api.get('/api/v1/records/{pk}')
    def get_record(pk:str,request:Request):return record_out(access(request),{'id':pk})
    @api.get('/api/v1/records/{pk}/replies')
    def replies(pk:str,request:Request):
        a=access(request);a.record(pk)
        return [record_out(a,r) for r in db.all('SELECT id FROM records WHERE thread_id=? ORDER BY created_at',(pk,)) if a.can_see(r['id'])]
    @api.put('/api/v1/records/{pk}')
    def edit_record(pk:str,body:EditInput,request:Request):
        a=access(request);r=a.record(pk);editable(a,r)
        if r['kind'] in ('report','case','suite','environment','run_index'):raise HTTPException(400,'来源证据只读，请同步或重新导入当前内容')
        if r['version']!=body.version:raise HTTPException(409,'版本已变化，请刷新')
        with db.transaction() as c:
            c.execute('INSERT INTO versions VALUES(?,?,?,?,?)',(pk,r['version'],r['body'],r['title'],time.time()))
            c.execute('UPDATE records SET title=?,body=?,version=version+1,status=?,updated_at=?,reviewed_by=NULL WHERE id=?',
                      (body.title,body.body,'pending' if r['kind']=='memory' else 'ready',time.time(),pk))
        index_record(db,pk);db.audit(a.user['id'],'record.edit',pk);return {'ok':True}
    @api.delete('/api/v1/records/{pk}')
    def delete_record(pk:str,request:Request):
        a=access(request);r=a.record(pk);editable(a,r)
        with db.transaction() as c:
            c.execute("UPDATE records SET deleted=1,body='',updated_at=? WHERE id=?",(time.time(),pk))
            c.execute('DELETE FROM versions WHERE record_id=?',(pk,))
            c.execute('DELETE FROM record_evidence WHERE record_id=?',(pk,))
            c.execute('DELETE FROM embeddings WHERE chunk_id IN(SELECT rowid FROM chunks WHERE record_id=?)',(pk,))
            c.execute('DELETE FROM chunks WHERE record_id=?',(pk,))
        for file in (settings.data_dir/'files').glob(pk+'.*'):file.unlink()
        db.audit(a.user['id'],'record.delete',pk);return {'ok':True}
    @api.put('/api/v1/records/{pk}/policy')
    def record_policy(pk:str,request:Request,body:dict=Body(...)):
        a=access(request);r=a.record(pk)
        if r['project_id'] is not None:a.project(r['project_id'],'admin')
        elif r['owner_id']!=a.user['id'] and not (r['visibility']=='workspace' and a.user['is_admin']):raise HTTPException(403,'仅作者可修改私有资料策略')
        if r['visibility']=='workspace' and not a.user['is_admin']:raise HTTPException(403,'需管理员')
        if body.get('egress') not in ('local','external'):raise HTTPException(400,'策略无效')
        db.execute('UPDATE records SET egress=?,updated_at=? WHERE id=?',(body['egress'],time.time(),pk))
        db.audit(a.user['id'],'record.policy',pk,body['egress']);return {'ok':True}
    @api.post('/api/v1/records/{pk}/review')
    def review(pk:str,body:ReviewInput,request:Request):
        a=access(request);r=a.record(pk)
        if r['project_id'] is not None:a.project(r['project_id'],'admin')
        elif r['visibility']=='workspace':
            if not a.user['is_admin']:raise HTTPException(403,'需要工作区管理员审核')
        elif r['owner_id']!=a.user['id']:raise HTTPException(403,'没有审核权限')
        if r['kind']!='memory' and r['status']!='pending':raise HTTPException(400,'此内容不需要审核')
        if r['stale']:raise HTTPException(409,'来源已变化，请重新整理记忆')
        status=('approved' if r['kind']=='memory' else 'ready') if body.action=='approve' else 'rejected'
        db.execute('UPDATE records SET status=?,reviewed_by=?,review_at=?,updated_at=? WHERE id=?',(status,a.user['id'],time.time()+90*86400 if r['kind']=='memory' else None,time.time(),pk))
        db.audit(a.user['id'],'record.review',pk,body.action);notify(db,r['owner_id'],pk,'内容审核完成');return {'status':status}
    @api.post('/api/v1/records/{pk}/share')
    def share(pk:str,body:ShareInput,request:Request):
        a=access(request);r=a.record(pk);a.project(body.project_id,'editor')
        if r['owner_id']!=a.user['id']:raise HTTPException(403,'仅作者可以共享私有内容')
        if r['visibility']!='private':raise HTTPException(400,'内容已共享')
        for src in r['sources']:
            if a.record(src['id'])['visibility']=='private':raise HTTPException(400,'请先共享私有来源或另行发布人工审核的摘录')
        deps=set(r['deps'])|{body.project_id}
        db.execute("UPDATE records SET visibility='project',project_id=?,deps=?,status=?,reviewed_by=NULL,updated_at=? WHERE id=?",(body.project_id,dump(sorted(deps)),'pending' if r['kind']=='memory' else r['status'],time.time(),pk))
        db.audit(a.user['id'],'record.share',pk);return {'ok':True}
    @api.get('/api/v1/records/{pk}/file')
    def download(pk:str,request:Request,version:int|None=None):
        a=access(request);record=a.record(pk)
        row=db.one('SELECT filename FROM file_versions WHERE record_id=? AND version=?',(pk,version or record['version']))
        if not row:raise HTTPException(404,'此版本无原文件')
        path=settings.data_dir/'files'/row['filename']
        return FileResponse(path,filename=path.name)
    @api.post('/api/v1/search')
    def query(body:SearchInput,request:Request):return search(db,access(request),body.query,body.project_ids,body.limit,body.semantic)
    @api.post('/api/v1/uploads',status_code=201)
    async def upload(request:Request,file:UploadFile=File(...),project_id:int=Form(...),egress:str=Form('local')):
        a=access(request);a.project(project_id,'editor')
        if egress not in ('local','external'):raise HTTPException(400,'无效策略')
        ext=Path(file.filename or '').suffix.lower()
        if ext not in ALLOWED:raise HTTPException(400,'不支持的文件类型')
        if shutil.disk_usage(settings.data_dir).free<settings.min_free_bytes:raise HTTPException(507,'可用磁盘不足3GiB')
        raw=await file.read(MAX_FILE+1)
        if len(raw)>MAX_FILE:raise HTTPException(413,'文件超过20MiB')
        pk=create_record(db,a.user,{'kind':'document','title':Path(file.filename).name[:200],'project_id':project_id,'visibility':'project','status':'pending','egress':egress},a)
        dest=settings.data_dir/'files'/(pk+ext)
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw)
        db.execute('INSERT INTO file_versions VALUES(?,?,?,?)',(pk,1,dest.name,digest(raw)))
        task=queue(db,a.user,'ingest',{'record_id':pk,'filename':dest.name,'version':1,'project_ids':[project_id],'source_ids':[pk]})
        return {'id':pk,'task_id':task}
    @api.post('/api/v1/sources/ingest',status_code=201)
    def ingest_path(body:IngestInput,request:Request):
        a=access(request);admin(request);a.project(body.project_id,'editor')
        try:path=approved_path(body.path,settings.roots)
        except ValueError as exc:raise HTTPException(400,str(exc))
        pk=create_record(db,a.user,{'kind':'document','title':path.name,'project_id':body.project_id,'visibility':'project','status':'pending','egress':body.egress,'locator':str(path)},a)
        dest=settings.data_dir/'files'/(pk+path.suffix.lower());shutil.copyfile(path,dest);dest.chmod(0o600)
        db.execute('INSERT INTO file_versions VALUES(?,?,?,?)',(pk,1,dest.name,digest(dest.read_bytes())))
        db.execute('INSERT INTO source_files VALUES(?,?,?,?)',(pk,str(path),digest(path.read_bytes()),time.time()))
        task=queue(db,a.user,'ingest',{'record_id':pk,'filename':dest.name,'version':1,'project_ids':[body.project_id],'source_ids':[pk]})
        return {'id':pk,'task_id':task}
    @api.get('/api/v1/notifications')
    def notifications(request:Request):
        a=access(request);rows=db.all('SELECT * FROM notifications WHERE recipient_id=? ORDER BY id DESC',(a.user['id'],))
        rows=[r for r in rows if a.can_see(r['record_id'])]
        return {'items':rows[:100],'unread':sum(r['read_at'] is None for r in rows)}
    @api.post('/api/v1/notifications/{pk}/read')
    def read_notification(pk:int,request:Request):
        a=access(request);row=db.one('SELECT * FROM notifications WHERE id=? AND recipient_id=?',(pk,a.user['id']))
        if not row or not a.can_see(row['record_id']):raise HTTPException(404,'通知不存在')
        db.execute('UPDATE notifications SET read_at=? WHERE id=?',(time.time(),pk));return {'ok':True}
    @api.post('/api/v1/conversations',status_code=201)
    def conversation(request:Request,body:dict=Body(...)):
        user=actor(request);title=body.get('title','新对话')
        if not isinstance(title,str) or not 1<=len(title)<=200:raise HTTPException(400,'标题无效')
        pk=uuid.uuid4().hex;db.execute('INSERT INTO conversations VALUES(?,?,?,?)',(pk,user['id'],title,time.time()));return {'id':pk}
    @api.get('/api/v1/conversations')
    def conversations(request:Request):return db.all('SELECT * FROM conversations WHERE owner_id=? ORDER BY created_at DESC',(actor(request)['id'],))
    @api.get('/api/v1/conversations/{pk}')
    def conversation_messages(pk:str,request:Request):
        a=access(request)
        if not db.one('SELECT 1 FROM conversations WHERE id=? AND owner_id=?',(pk,a.user['id'])):raise HTTPException(404,'会话不存在')
        return [record_out(a,r) for r in db.all('SELECT id FROM records WHERE thread_id=? ORDER BY created_at',(pk,)) if a.can_see(r['id'])]
    @api.post('/api/v1/tasks',status_code=201)
    def create_task(body:TaskInput,request:Request):
        a=access(request)
        for pid in body.project_ids:a.project(pid)
        for rid in body.source_ids:a.record(rid)
        if body.conversation_id and not db.one('SELECT 1 FROM conversations WHERE id=? AND owner_id=?',(body.conversation_id,a.user['id'])):raise HTTPException(404,'会话不存在')
        if not db.one('SELECT id FROM models WHERE id=? AND enabled=1',(body.model_id,)):raise HTTPException(409,'尚未启用模型，请先配置密钥；本地检索仍可使用')
        if body.conversation_id:
            rid=create_record(db,a.user,{'kind':'chat_user','title':body.query[:100],'body':body.query,'visibility':'private',
                'deps':body.project_ids,'sources':[{'id':pk,'version':a.record(pk)['version']} for pk in body.source_ids], 'thread_id':body.conversation_id},a)
            index_record(db,rid)
        payload=body.model_dump()
        if body.conversation_id:payload['current_message_id']=rid
        pk=queue(db,a.user,body.kind,payload)
        return {'id':pk,'status':'queued'}
    @api.get('/api/v1/tasks')
    def tasks(request:Request):
        a=access(request);out=[]
        for t in db.all('SELECT * FROM tasks WHERE owner_id=? ORDER BY created_at DESC LIMIT 100',(a.user['id'],)):
            try:out.append(task_view(a,t))
            except HTTPException:continue
        return out
    @api.get('/api/v1/tasks/{pk}')
    def get_task(pk:str,request:Request):
        a=access(request);t=db.one('SELECT * FROM tasks WHERE id=?',(pk,))
        if not t:raise HTTPException(404,'任务不存在')
        return task_view(a,t)
    @api.post('/api/v1/tasks/{pk}/cancel')
    def cancel_task(pk:str,request:Request):
        a=access(request);t=db.one('SELECT * FROM tasks WHERE id=?',(pk,))
        if not t:raise HTTPException(404,'任务不存在')
        task_view(a,t);db.execute("UPDATE tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",(pk,));return {'ok':True}
    @api.post('/api/v1/tasks/{pk}/retry',status_code=201)
    def retry_task(pk:str,request:Request):
        a=access(request);t=db.one('SELECT * FROM tasks WHERE id=?',(pk,))
        if not t or t['status'] not in ('failed','cancelled'):raise HTTPException(409,'只能重试失败或已取消任务')
        task_view(a,t)
        new=queue(db,a.user,t['kind'],settings.unseal(t['payload']))
        db.audit(a.user['id'],'task.retry',pk+' -> '+new)
        return {'id':new}
    @api.post('/api/v1/tasks/{pk}/apply')
    def apply_draft(pk:str,request:Request,body:dict=Body(...)):
        a=access(request);t=db.one('SELECT * FROM tasks WHERE id=?',(pk,))
        if not t or t['kind']!='draft' or t['status']!='completed':raise HTTPException(404,'已完成草案不存在')
        result=task_view(a,t)['result'];project=a.project(body.get('project_id'),'editor')
        if not project['external_id'] or not a.external_user:raise HTTPException(400,'请选择绑定的TestDock项目')
        draft=a.record(result['record_id'])
        if set(draft['deps'])-{project['id']}:raise HTTPException(400,'草案含其他项目依据，请仅使用目标项目资料重新生成')
        result=a.bridge.call('apply',{'user_id':a.external_user,'project_id':project['external_id'],'bundle':result['bundle'],'prefix':str(body.get('prefix','AI · '))[:40]})
        db.audit(a.user['id'],'draft.apply',pk);return result
    @api.get('/api/v1/models')
    def models(request:Request):
        actor(request);return db.all('SELECT id,name,base_url,model,enabled,internal FROM models ORDER BY id')
    @api.post('/api/v1/models',status_code=201)
    def add_model(body:ModelInput,request:Request):
        user=admin(request)
        if body.enabled and not body.api_key:raise HTTPException(400,'启用模型需填写密钥')
        pk=db.execute('INSERT INTO models(name,base_url,model,secret,enabled,internal,created_at) VALUES(?,?,?,?,?,?,?)',
                      (body.name,body.base_url,body.model,settings.seal({'api_key':body.api_key}),body.enabled,body.internal,time.time()))
        db.audit(user['id'],'model.create',str(pk));return {'id':pk}
    @api.put('/api/v1/models/{pk}')
    def update_model(pk:int,body:ModelInput,request:Request):
        user=admin(request);old=db.one('SELECT * FROM models WHERE id=?',(pk,))
        if not old:raise HTTPException(404,'模型不存在')
        secret=body.api_key or settings.unseal(old['secret']).get('api_key','')
        if body.enabled and not secret:raise HTTPException(400,'请填写密钥')
        db.execute('UPDATE models SET name=?,base_url=?,model=?,secret=?,enabled=?,internal=? WHERE id=?',
                   (body.name,body.base_url,body.model,settings.seal({'api_key':secret}),body.enabled,body.internal,pk))
        db.audit(user['id'],'model.update',str(pk));return {'ok':True}
    @api.get('/api/v1/usage')
    def usage(request:Request):
        user=admin(request);day=int(time.time()//86400)*86400
        row=db.one('SELECT COALESCE(sum(used_tokens),0) used,COALESCE(sum(max(used_tokens,reserved_tokens)),0) budgeted FROM tasks WHERE created_at>=?',(day,))
        return row|{'daily_limit':settings.daily_tokens,'pricing_configured':False}
    @api.get('/api/v1/audit')
    def audit(request:Request):
        user=admin(request);return db.all('SELECT * FROM audit ORDER BY id DESC LIMIT 200')
    from .testdock_api import attach_testdock
    attach_testdock(api,db)
    api.mount('/',StaticFiles(directory=Path(__file__).parent/'static',html=True),name='web')
    return api


def app():return create_app()
