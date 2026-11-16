import hashlib
import hmac
import json
import secrets
import time
import uuid
import httpx
from fastapi import HTTPException
from .core import dump, digest

RANK={'viewer':0,'editor':1,'admin':2}


class Bridge:
    def __init__(self, settings): self.settings=settings;self.delegation_token=None
    def call(self, action, payload):
        path='/api/integrations/ai/'+action
        key_path=self.settings.bridge_key_file
        if not key_path or not key_path.is_file(): raise HTTPException(503,'TestDock授权连接尚未配置')
        if action!='bind':payload={**payload,'delegation_token':self.delegation_token}
        raw=dump(payload).encode();ts=str(time.time());nonce=secrets.token_hex(16)
        message=ts+'\n'+nonce+'\n'+path+'\n'+digest(raw)
        sig=hmac.new(key_path.read_bytes().strip(),message.encode(),hashlib.sha256).hexdigest()
        try:
            with httpx.Client(trust_env=False,timeout=8) as client:
                with client.stream('POST',self.settings.testdock_url.rstrip('/')+path,content=raw,headers={
                    'Content-Type':'application/json','X-AI-Timestamp':ts,'X-AI-Nonce':nonce,'X-AI-Signature':sig}) as response:
                    if response.status_code>=400:raise HTTPException(403 if response.status_code in (401,403,404) else 503,'来源授权已失效或服务不可用')
                    parts=[];size=0
                    for chunk in response.iter_bytes():
                        size+=len(chunk)
                        if size>4*1024**2:raise ValueError('来源响应超过4MiB')
                        parts.append(chunk)
            return json.loads(b''.join(parts))
        except (httpx.HTTPError,ValueError):raise HTTPException(503,'无法实时验证来源权限')


class Access:
    def __init__(self,db,user,bridge=None,binding=None):
        self.db,self.user=db,user
        self.bridge=bridge or Bridge(db.settings)
        self.roles={}
        self.external_user=None
        for m in db.all('SELECT m.project_id,m.role FROM members m JOIN projects p ON p.id=m.project_id WHERE m.user_id=? AND p.external_id IS NULL',(user['id'],)):
            self.roles[m['project_id']]=m['role']
        binding=binding or db.one('SELECT * FROM bindings WHERE user_id=?',(user['id'],))
        self.upstream_unavailable=False
        if binding:
            self.external_user=binding['external_user_id']
            if binding.get('secret'):self.bridge.delegation_token=db.settings.unseal(binding['secret']).get('delegation_token')
            try:
                context=self.bridge.call('context',{'user_id':self.external_user})
            except HTTPException:
                self.upstream_unavailable=True
                context={'projects':[]}
            for p in context['projects']:
                local=db.one('SELECT * FROM projects WHERE external_id=?',(p['id'],))
                if not local:
                    try:
                        pk=db.execute('INSERT INTO projects(name,description,external_id,created_at) VALUES(?,?,?,?)',(p['name'],p['description'],p['id'],time.time()))
                    except Exception:
                        local=db.one('SELECT * FROM projects WHERE external_id=?',(p['id'],))
                        if not local:raise
                        pk=local['id']
                else:
                    pk=local['id']
                    if local['name']!=p['name'] or local['description']!=p['description']:
                        db.execute('UPDATE projects SET name=?,description=? WHERE id=?',(p['name'],p['description'],pk))
                role={'tester':'editor','viewer':'viewer','admin':'admin'}[p['role']]
                self.roles[pk]=role
        self.meta={r['id']:r for r in db.all('SELECT id,project_id,owner_id,visibility,deps,sources,status,deleted,version,review_at FROM records')}
        self._visible={}

    def project(self,pk,required='viewer'):
        if pk not in self.roles or RANK[self.roles[pk]]<RANK[required]:raise HTTPException(403,'没有此项目的操作权限')
        return self.db.one('SELECT * FROM projects WHERE id=?',(pk,))

    def can_see(self,record_id,stack=None):
        if record_id in self._visible:return self._visible[record_id]
        r=self.meta.get(record_id)
        if not r or r['deleted'] or r['status']=='source_removed':return False
        stack=set() if stack is None else set(stack)
        if record_id in stack:return False
        stack.add(record_id)
        deps=set(json.loads(r['deps']))
        if r['project_id'] is not None:deps.add(r['project_id'])
        visible=deps.issubset(self.roles)
        if r['visibility']=='private':visible=visible and r['owner_id']==self.user['id']
        elif r['visibility']=='project':visible=visible and r['project_id'] in self.roles
        elif r['visibility']!='workspace':visible=False
        if r['status'] in ('pending','rejected') and r['owner_id']!=self.user['id']:
            reviewer=(r['project_id'] in self.roles and self.roles[r['project_id']]=='admin') or (r['visibility']=='workspace' and self.user['is_admin'])
            visible=visible and reviewer
        for source in json.loads(r['sources']):
            if not self.can_see(source['id'],stack):visible=False;break
        self._visible[record_id]=visible
        return visible

    def is_current(self,pk,stack=None):
        r=self.meta.get(pk)
        if not r or not self.can_see(pk) or r['status'] not in ('ready','approved'):return False
        if r['review_at'] and r['review_at']<time.time():return False
        stack=set() if stack is None else set(stack)
        if pk in stack:return False
        stack.add(pk)
        return all(self.meta.get(s['id'],{}).get('version')==s['version'] and self.is_current(s['id'],stack) for s in json.loads(r['sources']))

    def record(self,pk):
        if not self.can_see(pk):raise HTTPException(404,'内容不存在或当前不可见')
        r=self.db.one('SELECT * FROM records WHERE id=?',(pk,))
        r['stale']=any(self.meta.get(s['id'],{}).get('version')!=s['version'] or not self.is_current(s['id']) for s in json.loads(r['sources']))
        if r['kind']=='memory' and r['review_at'] and r['review_at']<time.time():r['stale']=True
        for name in ('deps','sources'):r[name]=json.loads(r[name])
        return r

    def visible_ids(self,search=False):
        result=[]
        for pk in self.meta:
            if not self.can_see(pk):continue
            r=self.meta[pk]
            if search and not self.is_current(pk):continue
            result.append(pk)
        return result


def create_record(db,user,body,access=None):
    access=access or Access(db,user)
    project=body.get('project_id');visibility=body.get('visibility','private')
    if project is not None:access.project(project,'editor')
    if visibility=='project' and project is None:raise HTTPException(400,'项目内容需要选择项目')
    if visibility=='workspace' and not user['is_admin']:raise HTTPException(403,'只有工作区管理员可以发布共享知识')
    deps=set(body.get('deps',[]))
    if project is not None:deps.add(project)
    sources=[]
    for item in body.get('sources',[]):
        src=access.record(item['id'])
        if item.get('version',src['version'])!=src['version']:raise HTTPException(409,'来源版本已变化，请重新生成')
        deps.update(src['deps'])
        # Private originals may not be published implicitly through derived content.
        if src['visibility']=='private' and visibility!='private':raise HTTPException(400,'私有来源须先由作者明确共享，再发布派生内容')
        sources.append({'id':src['id'],'version':src['version']})
    if not deps.issubset(access.roles):raise HTTPException(403,'包含未授权项目')
    pk=uuid.uuid4().hex;now=time.time()
    db.execute('''INSERT INTO records(id,kind,title,body,project_id,owner_id,visibility,deps,sources,thread_id,status,locator,external_run_id,egress,created_at,updated_at,review_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (pk,body['kind'],body['title'],body.get('body',''),project,user['id'],visibility,dump(sorted(deps)),dump(sources),body.get('thread_id'),
         body.get('status','pending' if body['kind']=='memory' else 'ready'),body.get('locator',''),body.get('external_run_id'),body.get('egress','local'),now,now,
         now+90*86400 if body['kind']=='memory' else None))
    db.audit(user['id'],'record.create',pk,body['kind'])
    return pk
