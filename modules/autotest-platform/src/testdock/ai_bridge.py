"""Loopback integration with signed requests, one-time account binding and live ACL."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from fastapi import HTTPException, Request
from .models import ImportInput
from .transfer import import_assets


def attach_bridge(api, db, actor, project_access, run_detail):
    db.execute('CREATE TABLE IF NOT EXISTS ai_nonces(nonce TEXT PRIMARY KEY, expires_at REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS ai_codes(code_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS ai_delegations(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)')

    def key():
        path = Path(os.environ.get('TESTDOCK_AI_BRIDGE_KEY_FILE', '/home/nanshan/workspace/services/autotest-platform/config/ai-bridge.key'))
        if not path.is_file(): raise HTTPException(503, 'AI集成尚未配置')
        return path.read_bytes().strip()

    def use_nonce(nonce, expires):
        with db.transaction() as c:
            c.execute('DELETE FROM ai_nonces WHERE expires_at<?', (time.time(),))
            if c.execute('SELECT 1 FROM ai_nonces WHERE nonce=?', (nonce,)).fetchone(): raise HTTPException(401, '集成凭据已使用')
            c.execute('INSERT INTO ai_nonces VALUES(?,?)', (nonce,expires))

    async def verify(request):
        try:
            ts=request.headers['x-ai-timestamp'];nonce=request.headers['x-ai-nonce']
            if abs(time.time()-float(ts))>30 or not 16<=len(nonce)<=100: raise ValueError()
            raw=await request.body()
            message=ts+'\n'+nonce+'\n'+request.url.path+'\n'+hashlib.sha256(raw).hexdigest()
            signature=hmac.new(key(),message.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,request.headers.get('x-ai-signature','')): raise ValueError()
            use_nonce('req:'+nonce,time.time()+60)
            data=json.loads(raw)
            return data
        except (KeyError,ValueError,TypeError): raise HTTPException(401,'无效的集成签名')

    def principal(data):
        pk=data.get('user_id')
        if type(pk) is not int: raise HTTPException(401,'无效账号')
        token=data.get('delegation_token','')
        if not isinstance(token,str):raise HTTPException(401,'缺少用户委托')
        grant=db.one('SELECT 1 FROM ai_delegations WHERE token_hash=? AND user_id=? AND expires_at>? AND revoked=0',(hashlib.sha256(token.encode()).hexdigest(),pk,time.time()))
        if not grant:
            grant=db.one('SELECT 1 FROM ai_source_grants g JOIN sessions s ON s.token_hash=g.session_hash JOIN ai_subjects a ON a.user_id=g.user_id WHERE g.token_hash=? AND g.user_id=? AND g.expires_at>? AND s.expires_at>? AND a.revoked=0',
                         (hashlib.sha256(token.encode()).hexdigest(),pk,time.time(),time.time()))
        if not grant:raise HTTPException(401,'用户委托无效、过期或已撤销')
        user=db.one('SELECT * FROM users WHERE id=? AND active=1',(pk,))
        if not user: raise HTTPException(403,'来源账号已停用')
        return user

    @api.post('/api/ai/binding-code')
    def binding(request: Request):
        user=actor(request)
        code=secrets.token_urlsafe(48)
        with db.transaction() as conn:
            conn.execute('DELETE FROM ai_codes WHERE expires_at<?',(time.time(),))
            conn.execute('INSERT INTO ai_codes VALUES(?,?,?,0)',(hashlib.sha256(code.encode()).hexdigest(),user['id'],time.time()+60))
        from .ai_store import config
        return {'code':code,'expires_in':60,'workbench_url':config(db)['workbench_url']}

    @api.post('/api/ai/revoke')
    def revoke(request: Request):
        user=actor(request)
        db.execute('UPDATE ai_delegations SET revoked=1 WHERE user_id=?',(user['id'],))
        from .ai_sources import subject
        subject(db,user['id'])
        db.execute('UPDATE ai_subjects SET revoked=1 WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM ai_source_grants WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM ai_connections WHERE user_id=?',(user['id'],))
        db.audit(user['id'],None,'ai.delegation.revoke')
        return {'ok':True}

    @api.post('/api/integrations/ai/session-exchange')
    async def session_exchange(request: Request):
        from .ai_sources import subject
        data=await verify(request);code=data.get('ticket','')
        if not isinstance(code,str):raise HTTPException(401,'无效票据')
        now=time.time()
        with db.transaction() as c:
            row=c.execute('SELECT t.* FROM ai_tickets t JOIN sessions s ON s.token_hash=t.session_hash JOIN users u ON u.id=t.user_id JOIN ai_subjects a ON a.user_id=u.id WHERE t.token_hash=? AND t.used=0 AND t.expires_at>? AND s.expires_at>? AND u.active=1 AND a.revoked=0',
                          (hashlib.sha256(code.encode()).hexdigest(),now,now)).fetchone()
            if not row:raise HTTPException(401,'登录票据已过期或使用')
            c.execute('UPDATE ai_tickets SET used=1 WHERE token_hash=?',(row['token_hash'],))
            token=secrets.token_urlsafe(40);expires=now+3600
            c.execute('INSERT INTO ai_source_grants VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['user_id'],row['session_hash'],expires))
            user=db.one('SELECT id,username,display_name FROM users WHERE id=?',(row['user_id'],))
        return {**user,'user_id':user['id'],'subject':subject(db,user['id']),'delegation_token':token,'expires_at':expires}

    @api.post('/api/integrations/ai/bind')
    async def exchange(request: Request):
        data=await verify(request)
        code=data.get('code')
        if not isinstance(code,str):raise HTTPException(401,'绑定码无效')
        with db.transaction() as conn:
            row=conn.execute('SELECT * FROM ai_codes WHERE code_hash=? AND expires_at>? AND used=0',(hashlib.sha256(code.encode()).hexdigest(),time.time())).fetchone()
            if not row:raise HTTPException(401,'绑定码无效、过期或已使用')
            user=conn.execute('SELECT * FROM users WHERE id=? AND active=1',(row['user_id'],)).fetchone()
            if not user:raise HTTPException(403,'来源账号已停用')
            conn.execute('UPDATE ai_codes SET used=1 WHERE code_hash=?',(row['code_hash'],))
            delegation=secrets.token_urlsafe(40);expires=time.time()+30*86400
            conn.execute('UPDATE ai_delegations SET revoked=1 WHERE user_id=?',(user['id'],))
            conn.execute('INSERT INTO ai_delegations VALUES(?,?,?,0)',(hashlib.sha256(delegation.encode()).hexdigest(),user['id'],expires))
        return {'user_id':user['id'],'username':user['username'],'display_name':user['display_name'],'delegation_token':delegation,'expires_at':expires}

    @api.post('/api/integrations/ai/context')
    async def context(request: Request):
        user=principal(await verify(request))
        if user['is_admin']:
            projects=db.all("SELECT id,name,description,'admin' role FROM projects")
        else:
            projects=db.all('SELECT p.id,p.name,p.description,m.role FROM projects p JOIN members m ON p.id=m.project_id WHERE m.user_id=?',(user['id'],))
        return {'user_id':user['id'],'projects':projects}

    @api.post('/api/integrations/ai/assets')
    async def assets(request: Request):
        from .transfer import portable
        from .services import serialize_case,serialize_environment,RUN_COLUMNS,serialize_run
        data=await verify(request);user=principal(data);pid=data.get('project_id');project_access(user,pid)
        kind=data.get('kind','cases');offset=data.get('offset',0)
        if kind not in ('cases','suites','environments','runs') or type(offset) is not int or offset<0:raise HTTPException(400,'无效分页')
        total=db.one(f'SELECT count(*) n FROM {kind} WHERE project_id=?',(pid,))['n']
        rows=db.all(f"SELECT {RUN_COLUMNS if kind=='runs' else '*'} FROM {kind} WHERE project_id=? ORDER BY {'created_at DESC,id' if kind=='runs' else 'id'} LIMIT 20 OFFSET ?",(pid,offset))
        items=[];size=0
        for row in rows:
            if kind=='cases':item=serialize_case(row)
            elif kind=='environments':item=serialize_environment(db.settings,row)
            elif kind=='runs':item=serialize_run(row)
            else:item={**row,'case_ids':json.loads(row['case_ids'])}
            secret_keys=item.get('secret_keys')
            item=portable(item)
            if secret_keys is not None:item['secret_keys']=secret_keys
            length=len(json.dumps(item,ensure_ascii=False).encode())
            if items and size+length>2500*1024:break
            items.append(item);size+=length
        nxt=offset+len(items)
        return {'items':items,'total':total,'next_offset':nxt if nxt<total and items else None}

    @api.post('/api/integrations/ai/failures')
    async def failures(request: Request):
        data=await verify(request);user=principal(data);project=data.get('project_id')
        project_access(user,project)
        since=data.get('since',time.time())
        if not isinstance(since,(int,float)):raise HTTPException(400,'无效时间')
        rows=db.all("SELECT id,finished_at FROM runs WHERE project_id=? AND status IN ('failed','error') AND finished_at>=? ORDER BY finished_at,id LIMIT 50",(project,since))
        return {'runs':rows}

    @api.post('/api/integrations/ai/evidence')
    async def evidence(request: Request):
        data=await verify(request);user=principal(data)
        run=db.one('SELECT * FROM runs WHERE id=?',(data.get('run_id',''),))
        if not run: raise HTTPException(404,'执行不存在')
        project_access(user,run['project_id'])
        return run_detail(run)

    @api.post('/api/integrations/ai/apply')
    async def apply(request: Request):
        data=await verify(request);user=principal(data)
        project_access(user,data.get('project_id'),'tester')
        try:
            body=ImportInput.model_validate({'bundle':data['bundle'],'prefix':data.get('prefix','AI · '),'dry_run':False})
        except (ValueError,KeyError): raise HTTPException(422,'用例草案格式不正确')
        if body.bundle.environments or body.bundle.suites or not body.bundle.cases: raise HTTPException(400,'AI仅可应用用例草案')
        result=import_assets(db,data['project_id'],body)
        db.audit(user['id'],data['project_id'],'ai.draft.apply',result['counts'])
        return result
