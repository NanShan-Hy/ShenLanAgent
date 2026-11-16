"""Narrow HMAC + session delegation API; never accepts a caller-supplied user id."""
import hashlib
import hmac
import json
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from .access import Access,Bridge,create_record
from .core import digest,dump,redact
from .models import SearchInput,RecordInput
from .retrieval import search,index_record


def attach_testdock(api,db):
    async def signed(request):
        try:
            ts=request.headers['x-ai-timestamp'];nonce=request.headers['x-ai-nonce']
            if abs(time.time()-float(ts))>30 or not 16<=len(nonce)<=100:raise ValueError()
            key=db.settings.bridge_key_file
            if not key or not key.is_file():raise HTTPException(503,'来源连接未配置')
            raw=await request.body()
            expected=hmac.new(key.read_bytes().strip(),(ts+'\n'+nonce+'\n'+request.url.path+'\n'+digest(raw)).encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected,request.headers.get('x-ai-signature','')):raise ValueError()
            with db.transaction() as c:
                c.execute('DELETE FROM used_nonces WHERE expires_at<?',(time.time(),))
                if c.execute('SELECT 1 FROM used_nonces WHERE nonce=?',(nonce,)).fetchone():raise ValueError()
                c.execute('INSERT INTO used_nonces VALUES(?,?)',(nonce,time.time()+60))
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError()
            return body
        except (ValueError,KeyError,TypeError):raise HTTPException(401,'无效的集成签名或重放')

    def principal(request):
        token=request.headers.get('authorization','').removeprefix('Bearer ')
        row=db.one('SELECT * FROM td_sessions WHERE token_hash=? AND expires_at>?',(digest(token),time.time()))
        if not row:raise HTTPException(401,'集成登录已过期')
        user=db.one('SELECT * FROM users WHERE id=? AND active=1',(row['user_id'],))
        if not user:raise HTTPException(403,'AI账号已停用')
        a=Access(db,user,binding={'external_user_id':row['external_user_id'],'secret':row['secret']})
        if a.upstream_unavailable:raise HTTPException(403,'来源登录或权限已失效')
        return a

    def validate(a,refs,external=False):
        checked=set();out=[]
        def check(pk):
            if pk in checked:return
            checked.add(pk);r=a.record(pk)
            if not a.is_current(pk):raise HTTPException(409,'知识来源已失效')
            if external:
                if r['egress']!='external':raise HTTPException(403,'资料尚未授权外发')
                for pid in r['deps']:
                    if a.project(pid)['egress']!='external':raise HTTPException(403,'项目尚未授权外发')
            for s in r['sources']:check(s['id'])
        for ref in refs:
            r=a.record(ref['id']);check(r['id'])
            if ref.get('version',r['version'])!=r['version']:raise HTTPException(409,'知识来源版本已变化')
            out.append({'system':'workbench','id':r['id'],'project_id':r['project_id'],'deps':r['deps'],'version':r['version'],
                        'title':r['title'],'kind':r['kind'],'locator':r['locator'],'egress':r['egress'],
                        'can_manage_policy':a.roles.get(r['project_id'])=='admin' if r['project_id'] is not None else r['owner_id']==a.user['id'],
                        'truncated':len(redact(r['body']))>16000,'total_characters':len(r['body']),
                        'text':redact(r['body'])[:16000]})
        return out

    @api.post('/api/integrations/testdock/session')
    async def exchange(request:Request):
        body=await signed(request)
        if set(body)!={'ticket'}:raise HTTPException(422,'仅接受登录票据')
        remote=Bridge(db.settings).call('session-exchange',body)
        with db.transaction() as c:
            mapping=c.execute('SELECT user_id FROM td_identities WHERE subject=?',(remote['subject'],)).fetchone()
            legacy=c.execute('SELECT user_id FROM bindings WHERE external_user_id=?',(remote['user_id'],)).fetchone()
            uid=mapping['user_id'] if mapping else legacy['user_id'] if legacy else None
            if uid is None:
                uid=c.execute('INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,0,?)',
                              ('td_'+uuid.uuid4().hex,remote['display_name'],'!source-login-only',time.time())).lastrowid
            if not c.execute('SELECT 1 FROM users WHERE id=? AND active=1',(uid,)).fetchone():raise HTTPException(403,'AI账号已停用')
            c.execute('INSERT OR IGNORE INTO td_identities VALUES(?,?,?)',(remote['subject'],uid,remote['user_id']))
            token=secrets.token_urlsafe(40)
            c.execute('INSERT INTO td_sessions VALUES(?,?,?,?,?)',(digest(token),uid,remote['user_id'],db.settings.seal({'delegation_token':remote['delegation_token']}),remote['expires_at']))
        db.audit(uid,'testdock.session',str(remote['user_id']))
        return {'token':token,'expires_at':remote['expires_at']}

    @api.post('/api/integrations/testdock/context')
    async def context(request:Request):
        await signed(request);a=principal(request)
        return {'projects':[dict(a.project(pid),role=role) for pid,role in a.roles.items()],
                'identity':{'id':a.user['id'],'display_name':a.user['display_name']}}

    @api.post('/api/integrations/testdock/search')
    async def query(request:Request):
        body=await signed(request);a=principal(request)
        external=body.pop('external',False);s=SearchInput.model_validate(body)
        result=search(db,a,s.query,s.project_ids,s.limit,s.semantic);out=[]
        for item in result['items']:
            try:ref=validate(a,[{'id':item['record_id'],'version':item['version']}],external)[0]
            except HTTPException as exc:
                if external and exc.status_code==403:continue
                raise
            out.append({**ref,'text':redact(item['text']),'locator':item['locator']})
        return {'items':out,'mode':result['mode']}

    @api.post('/api/integrations/testdock/validate')
    async def checked(request:Request):
        body=await signed(request);a=principal(request)
        refs=body.get('sources',[])
        if not isinstance(refs,list) or len(refs)>100:raise HTTPException(422,'来源数量过多')
        return {'items':validate(a,refs,bool(body.get('external',False)))}

    @api.post('/api/integrations/testdock/policy')
    async def policy(request:Request):
        body=await signed(request);a=principal(request);pid=body.get('project_id');a.project(pid,'admin')
        if body.get('egress') not in ('local','external'):raise HTTPException(422,'策略无效')
        db.execute('UPDATE projects SET egress=? WHERE id=?',(body['egress'],pid));db.audit(a.user['id'],'project.policy',str(pid),body['egress'])
        return {'ok':True}

    @api.post('/api/integrations/testdock/memory')
    async def memory(request:Request):
        body=await signed(request);a=principal(request)
        if body.get('action')=='view':
            r=a.record(body.get('id'))
            if r['kind']!='memory':raise HTTPException(404,'记忆不存在')
            return {key:r[key] for key in ('id','title','body','status','version','stale')}
        if body.get('action')=='review':
            r=a.record(body.get('id'));expected=body.get('version')
            if r['kind']!='memory' or r['version']!=expected or r['stale']:raise HTTPException(409,'记忆或来源已变化')
            if r['project_id'] is not None:a.project(r['project_id'],'admin')
            elif r['owner_id']!=a.user['id']:raise HTTPException(403,'仅本人可审核私人记忆')
            if body.get('decision') not in ('approve','reject'):raise HTTPException(422,'审核决定无效')
            status='approved' if body['decision']=='approve' else 'rejected'
            db.execute('UPDATE records SET status=?,reviewed_by=?,review_at=?,updated_at=? WHERE id=?',(status,a.user['id'],time.time()+90*86400,time.time(),r['id']))
            db.audit(a.user['id'],'record.review',r['id'],status);return {'id':r['id'],'status':status}
        value=RecordInput.model_validate({**body,'kind':'memory','visibility':'private'})
        rid=create_record(db,a.user,value.model_dump(),a);index_record(db,rid)
        return {'id':rid,'status':'pending','version':1}

    @api.post('/api/integrations/testdock/record-policy')
    async def record_policy(request:Request):
        body=await signed(request);a=principal(request);r=a.record(body.get('id'))
        if r['project_id'] is not None:a.project(r['project_id'],'admin')
        elif r['owner_id']!=a.user['id']:raise HTTPException(403,'仅作者可修改私人资料策略')
        if r['visibility']=='workspace' and not a.user['is_admin']:raise HTTPException(403,'需要工作台管理员')
        if body.get('egress') not in ('local','external'):raise HTTPException(422,'策略无效')
        db.execute('UPDATE records SET egress=?,updated_at=? WHERE id=?',(body['egress'],time.time(),r['id']))
        db.audit(a.user['id'],'record.policy',r['id'],body['egress']);return {'ok':True}
