"""HTTP-only delegated knowledge access and native evidence manifests."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import httpx
from fastapi import HTTPException

from .core import encode, token_hash
from .ai_store import config


def subject(db, uid):
    row=db.one('SELECT subject FROM ai_subjects WHERE user_id=?',(uid,))
    if not row:
        db.execute('INSERT OR IGNORE INTO ai_subjects(user_id,subject) VALUES(?,?)',(uid,secrets.token_hex(16)))
        row=db.one('SELECT subject FROM ai_subjects WHERE user_id=?',(uid,))
    installation=db.one("SELECT value FROM ai_meta WHERE name='installation'")['value']
    return installation+':'+row['subject']


def live_session(db,uid,session_hash):
    row=db.one('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE u.id=? AND u.active=1 AND s.token_hash=? AND s.expires_at>?',(uid,session_hash,time.time()))
    if not row:raise HTTPException(401,'登录已退出、过期或账号已停用')
    if db.one('SELECT revoked FROM ai_subjects WHERE user_id=?',(uid,)) and db.one('SELECT revoked FROM ai_subjects WHERE user_id=?',(uid,))['revoked']:
        raise HTTPException(403,'AI委托已撤销，请明确重新连接')
    return row


class Knowledge:
    def __init__(self,db,uid,session_hash):self.db,self.uid,self.session_hash=db,uid,session_hash

    def signed(self,action,body,token=None):
        key=Path(os.environ.get('TESTDOCK_AI_BRIDGE_KEY_FILE','/home/nanshan/workspace/services/autotest-platform/config/ai-bridge.key'))
        if not key.is_file():raise HTTPException(503,'工作台授权连接尚未配置')
        path='/api/integrations/testdock/'+action
        raw=encode(body).encode();ts=str(time.time());nonce=secrets.token_hex(16)
        signature=hmac.new(key.read_bytes().strip(),(ts+'\n'+nonce+'\n'+path+'\n'+hashlib.sha256(raw).hexdigest()).encode(),hashlib.sha256).hexdigest()
        headers={'Content-Type':'application/json','X-AI-Timestamp':ts,'X-AI-Nonce':nonce,'X-AI-Signature':signature}
        if token:headers['Authorization']='Bearer '+token
        try:
            with httpx.Client(trust_env=False,timeout=15,follow_redirects=False) as client:
                with client.stream('POST',config(self.db)['workbench_url'].rstrip('/')+path,content=raw,headers=headers) as r:
                    if r.status_code>=400:
                        raise HTTPException(r.status_code if r.status_code in (401,403,404,409,422,507) else 503,'知识工作台拒绝请求或权限已失效')
                    chunks=[];size=0
                    for part in r.iter_bytes():
                        size+=len(part)
                        if size>4*1024**2:raise HTTPException(413,'工作台响应过大，请缩小范围')
                        chunks.append(part)
            return json.loads(b''.join(chunks))
        except (httpx.HTTPError,ValueError):raise HTTPException(503,'知识工作台暂不可用')

    def token(self):
        live_session(self.db,self.uid,self.session_hash)
        row=self.db.one('SELECT * FROM ai_connections WHERE session_hash=? AND user_id=? AND expires_at>?',(self.session_hash,self.uid,time.time()+30))
        if row:return self.db.settings.unseal(row['secret'])['token']
        subject(self.db,self.uid)
        ticket=secrets.token_urlsafe(40)
        self.db.execute('INSERT INTO ai_tickets VALUES(?,?,?,?,0)',(token_hash(ticket),self.uid,self.session_hash,time.time()+60))
        data=self.signed('session',{'ticket':ticket})
        self.db.execute('INSERT INTO ai_connections VALUES(?,?,?,?) ON CONFLICT(session_hash) DO UPDATE SET secret=excluded.secret,expires_at=excluded.expires_at',
                        (self.session_hash,self.uid,self.db.settings.seal({'token':data['token']}),data['expires_at']))
        return data['token']

    def call(self,action,body=None):
        try:return self.signed(action,body or {},self.token())
        except HTTPException as exc:
            if exc.status_code!=401:raise
            self.db.execute('DELETE FROM ai_connections WHERE session_hash=?',(self.session_hash,))
            return self.signed(action,body or {},self.token())


def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


def native_record(db,uid,ref,access):
    table=ref.get('kind');pk=ref.get('id')
    if table not in ('cases','suites','environments','runs','schedules'):raise HTTPException(422,'来源类型无效')
    row=db.one('SELECT * FROM '+table+' WHERE id=?',(pk,))
    if not row:raise HTTPException(404,'测试来源已移除')
    access(db.one('SELECT * FROM users WHERE id=?',(uid,)),row['project_id'])
    from .transfer import portable
    from .services import serialize_case,serialize_environment,RUN_COLUMNS,serialize_run
    if table=='cases':value=serialize_case(row)
    elif table=='environments':value=serialize_environment(db.settings,row)
    elif table=='runs':
        value=serialize_run({k:v for k,v in row.items() if k!='snapshot'})
        value['results']=db.all('SELECT case_id,name,status,duration,steps FROM case_results WHERE run_id=?',(pk,))
    else:
        value=dict(row)
        for key in ('case_ids','environment_ids','notification_channel_ids'):
            if key in value and isinstance(value[key],str):value[key]=json.loads(value[key])
        for key in ('next_run_at','last_error'):value.pop(key,None)
    value=portable(value)
    return {'system':'testdock','kind':table,'id':pk,'project_id':row['project_id'],'version':digest(value),
            'title':row.get('name',str(pk)),'locator':table+' #'+str(pk),'text':encode(value),'data':value}
