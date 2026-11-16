"""Independent, bounded HTTP sender. API acceptance is not a delivery receipt."""
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
import time
from urllib.parse import urlencode
import httpx
from .core import Database,Settings,private_write
from .notifications import authorized,message


class SendError(Exception):
    def __init__(self,detail,retry=False,unknown=False):
        super().__init__(detail);self.retry=retry;self.unknown=unknown


class Provider:
    def __init__(self,transport=None):self.transport=transport;self.tokens={}
    def request(self,url,headers=None,**kwargs):
        try:
            with httpx.Client(timeout=10,trust_env=False,follow_redirects=False,transport=self.transport) as client:
                with client.stream('POST',url,headers=headers,**kwargs) as response:
                    if response.status_code==429:raise SendError('渠道限流，稍后重试',retry=True)
                    if response.status_code>=500:raise SendError('渠道服务端异常，发送状态未知',unknown=True)
                    if response.status_code>=300:raise SendError('渠道拒绝请求，请检查凭据、权限及目标')
                    data=bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data)>128*1024:raise SendError('渠道响应超限，发送状态未知',unknown=True)
            return json.loads(data)
        except (httpx.ConnectError,httpx.ConnectTimeout):raise SendError('未能连接渠道，稍后重试',retry=True)
        except httpx.HTTPError:raise SendError('请求中断或超时，发送状态未知',unknown=True)
        except (ValueError,TypeError):raise SendError('无法确认渠道响应，发送状态未知',unknown=True)

    def send(self,kind,credentials,payload,before_send=lambda:None):
        title,content=message(payload)
        if kind=='dingtalk_app':
            cache_key=(credentials['app_key'],hashlib.sha256(credentials['app_secret'].encode()).hexdigest())
            cached=self.tokens.get(cache_key)
            if not cached or cached[1]<time.time():
                raw=self.request('https://api.dingtalk.com/v1.0/oauth2/accessToken',json={'appKey':credentials['app_key'],'appSecret':credentials['app_secret']})
                if not raw.get('accessToken'):raise SendError('获取钉钉令牌失败，请检查应用凭据及权限')
                cached=(raw['accessToken'],time.time()+max(1,int(raw.get('expireIn',7200))-60));self.tokens[cache_key]=cached
            before_send()
            body={'robotCode':credentials['robot_code'],'openConversationId':credentials['conversation_id'],
                  'msgKey':'sampleMarkdown','msgParam':json.dumps({'title':title,'text':content},ensure_ascii=False)}
            if credentials.get('cool_app_code'):body['coolAppCode']=credentials['cool_app_code']
            try:
                raw=self.request('https://api.dingtalk.com/v1.0/robot/groupMessages/send',headers={'x-acs-dingtalk-access-token':cached[0]},json=body)
            except SendError:
                self.tokens.pop(cache_key,None)
                raise
            if not raw.get('processQueryKey'):
                self.tokens.pop(cache_key,None)
                if raw.get('code')=='unknown.send.result':raise SendError('钉钉返回未知发送状态',unknown=True)
                raise SendError('钉钉拒绝发送，请检查应用权限、令牌及接收群')
            receipt=str(raw['processQueryKey'])[:256]
        elif kind=='dingtalk_webhook':
            url=credentials['webhook_url']
            if credentials.get('sign_secret'):
                stamp=str(int(time.time()*1000));secret=credentials['sign_secret']
                signature=base64.b64encode(hmac.new(secret.encode(),(stamp+'\n'+secret).encode(),hashlib.sha256).digest()).decode()
                url+='&'+urlencode({'timestamp':stamp,'sign':signature})
            before_send();keyword=credentials.get('keyword','')
            raw=self.request(url,json={'msgtype':'markdown','markdown':{'title':title,'text':keyword+'\n'+content}})
            if raw.get('errcode')!=0:
                if raw.get('errcode') in (130101,90018):raise SendError('钉钉机器人限流，稍后重试',retry=True)
                raise SendError('钉钉拒绝发送，请检查密钥、加签、关键词及IP白名单')
            receipt=''
        else:
            before_send();raw=self.request('https://sctapi.ftqq.com/'+credentials['sendkey']+'.send',json={'title':title,'desp':content})
            if raw.get('code')!=0:raise SendError('Server酱拒绝发送，请检查SendKey、微信通道及每日额度')
            receipt=str((raw.get('data') or {}).get('pushid',''))[:256]
        # Never retain a provider response containing one of the configured secrets.
        if any(v in receipt for v in credentials.values() if len(v)>=4):receipt=''
        return receipt


class Notifier:
    def __init__(self,settings=None,provider=None):
        self.settings=settings or Settings.load();self.db=Database(self.settings);self.db.initialize();self.provider=provider or Provider()
    def heartbeat(self):private_write(self.settings.data_dir/'notifier-heartbeat',str(time.time()))
    def recover(self):
        self.db.execute("UPDATE notification_deliveries SET status='unknown',error='通知执行器中断，发送状态未知；请核对后决定重发',updated_at=? WHERE status='sending'",(time.time(),))
    def check(self,delivery,payload,channel):
        with self.db.connect() as c:
            fresh=c.execute('SELECT * FROM notification_channels WHERE id=?',(channel['id'],)).fetchone()
            if not fresh or fresh['deleted'] or fresh['revision']!=delivery['channel_revision'] or (not fresh['enabled'] and not payload.get('test')):
                raise PermissionError('渠道已关闭、删除或配置变更')
            if not authorized(c,fresh['owner_id'],delivery['project_id']):raise PermissionError('渠道授权人已停用或失去项目管理权限')
            prefix,pk=delivery['event_key'].split(':',1)
            if prefix in ('run','batch'):
                table='runs' if prefix=='run' else 'schedule_batches'
                if not c.execute('SELECT 1 FROM '+table+' WHERE id=? AND project_id=?',(pk,delivery['project_id'])).fetchone():raise PermissionError('来源运行或批次已不存在')
    def run_once(self):
        self.heartbeat();now=time.time()
        with self.db.transaction() as c:
            # Claim a different target when one target is throttled; no head-of-line blocking.
            rows=c.execute("SELECT d.*,c.target_hash FROM notification_deliveries d JOIN notification_channels c ON c.id=d.channel_id WHERE d.status='pending' AND d.next_attempt<=? ORDER BY d.created_at LIMIT 100",(now,)).fetchall()
            row=None
            for candidate in rows:
                last=c.execute('SELECT max(created_at) t FROM notification_attempts WHERE target_hash=?',(candidate['target_hash'],)).fetchone()['t']
                if not last or now-last>=6:row=candidate;break
                c.execute('UPDATE notification_deliveries SET next_attempt=? WHERE id=?',(last+6,candidate['id']))
            if not row:return False
            delivery=dict(row)
            c.execute("UPDATE notification_deliveries SET status='sending',updated_at=? WHERE id=?",(now,delivery['id']))
            channel=dict(c.execute('SELECT * FROM notification_channels WHERE id=?',(delivery['channel_id'],)).fetchone())
        status='accepted';error='';receipt='';attempt_id=None
        try:
            payload=self.settings.unseal(delivery['payload']);self.check(delivery,payload,channel)
            # Record attempts only after permission checks, immediately before network work.
            attempt_id=self.db.execute("INSERT INTO notification_attempts(delivery_id,target_hash,created_at,status) VALUES(?,?,?,'sending')",(delivery['id'],channel['target_hash'],now))
            self.db.execute('UPDATE notification_deliveries SET attempts=attempts+1 WHERE id=?',(delivery['id'],))
            receipt=self.provider.send(channel['kind'],self.settings.unseal(channel['secret']),payload,lambda:self.check(delivery,payload,channel))
        except PermissionError as exc:status='cancelled';error=str(exc)
        except SendError as exc:
            error=str(exc);status='unknown' if exc.unknown else 'failed'
            if exc.retry and delivery['attempts']<3:
                status='pending';delay=(60,300,900)[delivery['attempts']]
                self.db.execute('UPDATE notification_deliveries SET next_attempt=? WHERE id=?',(time.time()+delay,delivery['id']))
        except Exception:status='unknown';error='未能确认发送结果，请检查渠道配置后决定是否重发'
        with self.db.transaction() as c:
            c.execute('UPDATE notification_deliveries SET status=?,error=?,provider_id=?,updated_at=? WHERE id=?',(status,error,receipt,time.time(),delivery['id']))
            if attempt_id:c.execute('UPDATE notification_attempts SET status=?,detail=? WHERE id=?',(status,error,attempt_id))
        return True
    def run(self):
        os.umask(0o077)
        with (self.settings.data_dir/'notifier.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise SystemExit('已有通知执行器正在运行')
            self.recover()
            def beat():
                while True:self.heartbeat();time.sleep(2)
            threading.Thread(target=beat,daemon=True).start()
            while True:
                try:
                    if not self.run_once():time.sleep(1)
                except Exception:
                    import traceback
                    traceback.print_exc();time.sleep(2)
if __name__=='__main__':Notifier().run()
