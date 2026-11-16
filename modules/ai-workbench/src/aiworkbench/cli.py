import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from .core import Database,Settings,CONFIG,BASE,dump,write_private,password_hash


def initialize(path=None):
    path=Path(path or os.environ.get('AIWORKBENCH_CONFIG',CONFIG))
    if not path.exists():
        root=path.parent.parent
        write_private(path,json.dumps({'data_dir':str(root/'data'),'key_file':str(path.parent/'master.key'),
            'model_dir':str(BASE/'tools/ai-models'),'bridge_key_file':str(path.parent/'testdock-bridge.key'),
            'daily_tokens':200000,'min_free_bytes':3*1024**3,'testdock_url':'http://127.0.0.1:8088'},indent=2))
    os.environ['AIWORKBENCH_CONFIG']=str(path)
    db=Database(Settings.load());db.initialize()
    if not db.one('SELECT id FROM users LIMIT 1'):
        password=secrets.token_urlsafe(18)
        db.execute('INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',('admin','工作区管理员',password_hash(password),1,time.time()))
        write_private(path.parent/'initial-admin.txt','AI工作台初始凭据\n账号：admin\n密码：'+password+'\n本机访问：http://127.0.0.1:8090\n首次登录后请修改密码。\n')
    if not db.one('SELECT id FROM models'):
        for name,url,model in [('OpenAI','https://api.openai.com/v1','gpt-4.1'),('DeepSeek','https://api.deepseek.com','deepseek-flash')]:
            db.execute('INSERT INTO models(name,base_url,model,secret,created_at) VALUES(?,?,?,?,?)',(name,url,model,db.settings.seal({'api_key':''}),time.time()))
    print('初始化完成；凭据文件：'+str(path.parent/'initial-admin.txt')+'；模型未启用。')
    return db


def backup(db,root=None):
    root=Path(root or BASE/'backups/ai-workbench');root.mkdir(parents=True,exist_ok=True,mode=0o700)
    size=sum(p.stat().st_size for p in db.settings.data_dir.rglob('*') if p.is_file())
    if shutil.disk_usage(root).free<3*1024**3+size*2:raise RuntimeError('空间不足，不创建备份或删除历史')
    target=root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3));target.mkdir(mode=0o700)
    with sqlite3.connect(db.path) as src,sqlite3.connect(target/'ai.sqlite3') as dst:src.backup(dst)
    shutil.copy2(db.settings.key_file,target/'master.key')
    write_private(target/'settings.json',json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in asdict(db.settings).items()},ensure_ascii=False,indent=2))
    for name in ('files',):
        if (db.settings.data_dir/name).is_dir():shutil.copytree(db.settings.data_dir/name,target/name)
    source=Path(__file__).resolve().parents[2]
    for name in ('src','docs','scripts','config','deploy','tests'):
        if (source/name).is_dir():shutil.copytree(source/name,target/'source'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.env','*.key'))
    for name in ('README.md','requirements.in','requirements-dev.in','requirements.lock','LICENSE','THIRD_PARTY.md','pyproject.toml','AGENTS.md'):
        if (source/name).is_file():shutil.copy2(source/name,target/'source'/name)
    bridge=db.settings.bridge_key_file
    if bridge and bridge.is_file():shutil.copy2(bridge,target/'testdock-bridge.key')
    manifest={}
    for p in target.rglob('*'):
        p.chmod(0o700 if p.is_dir() else 0o600)
        if p.is_file():manifest[str(p.relative_to(target))]=hashlib.sha256(p.read_bytes()).hexdigest()
    write_private(target/'manifest.json',json.dumps({'created_at':time.time(),'source':str(db.path),'files':manifest},ensure_ascii=False,indent=2))
    print('备份完成：'+str(target));return target


def verify(snapshot,output):
    source=Path(snapshot).resolve();out=Path(output).resolve();allowed=BASE/'tmp/ai-workbench'
    if not out.is_relative_to(allowed) or out==allowed or out.exists():raise ValueError('需要项目tmp下的全新恢复目录')
    manifest=json.loads((source/'manifest.json').read_text())
    for name,sha in manifest['files'].items():
        p=(source/name).resolve()
        if not p.is_relative_to(source) or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError('校验失败：'+name)
    shutil.copytree(source,out)
    with sqlite3.connect(out/'ai.sqlite3') as c:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    data=json.loads((out/'settings.json').read_text());data.update(data_dir=str(out),key_file=str(out/'master.key'))
    if (out/'testdock-bridge.key').is_file():data['bridge_key_file']=str(out/'testdock-bridge.key')
    write_private(out/'restored-settings.json',json.dumps(data,ensure_ascii=False,indent=2))
    os.environ['AIWORKBENCH_CONFIG']=str(out/'restored-settings.json');db=Database(Settings.load());db.initialize()
    for table,column in [('models','secret'),('tasks','payload')]:
        for row in db.all(f'SELECT {column} value FROM {table}'):db.settings.unseal(row['value'])
    for row in db.all("SELECT result value FROM tasks WHERE result!='{}'"):db.settings.unseal(row['value'])
    for row in db.all("SELECT secret value FROM bindings WHERE secret!=''"):db.settings.unseal(row['value'])
    if db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='td_sessions'"):
        for row in db.all('SELECT secret value FROM td_sessions'):db.settings.unseal(row['value'])
    # A restored snapshot must not replay queued paid calls or revive source grants.
    with db.transaction() as c:
        keeper=c.execute('SELECT id FROM users WHERE is_admin=1 AND active=1 ORDER BY id LIMIT 1').fetchone()
        if not keeper:raise ValueError('备份中无有效管理员')
        c.execute('DELETE FROM sessions')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='td_sessions'").fetchone():
            c.execute('DELETE FROM td_sessions')
        c.execute('UPDATE users SET active=CASE WHEN id=? THEN 1 ELSE 0 END',(keeper['id'],))
        c.execute("UPDATE bindings SET secret='',expires_at=0")
        c.execute('UPDATE models SET enabled=0')
        c.execute('UPDATE automations SET enabled=0');c.execute('UPDATE subscriptions SET enabled=0')
        c.execute("UPDATE tasks SET status='cancelled',error='恢复副本需人工复核后重新提交',finished_at=? WHERE status IN ('queued','running')",(time.time(),))
    result={'sha256':'passed','sqlite_integrity':'ok','secrets':'decryptable','restored_to':str(out),
            'recovery_guards':'sessions/grants reset; models/automation disabled; pending calls cancelled; only recovery admin active'}
    write_private(out/'verification.json',json.dumps(result));print(json.dumps(result));return result


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['init','backup','verify','migrate']);p.add_argument('--config');p.add_argument('--snapshot');p.add_argument('--output');args=p.parse_args();os.umask(0o077)
    if args.config:os.environ['AIWORKBENCH_CONFIG']=str(Path(args.config).resolve())
    if args.command=='init':initialize(args.config)
    elif args.command=='backup':backup(Database(Settings.load()))
    elif args.command=='migrate':Database(Settings.load()).initialize(upgrade=True);print('schema3集成迁移完成')
    else:verify(args.snapshot,args.output)
if __name__=='__main__':main()
