#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from aiworkbench.core import BASE,Database,Settings,write_private
from aiworkbench.cli import backup
ROOT=Path(__file__).resolve().parents[1]
SERVICE=BASE/'services/ai-workbench'
UNITS=Path.home()/'.config/systemd/user'

def main():
 os.umask(0o077)
 db=Database(Settings.load())
 subprocess.run(['systemctl','--user','stop','ai-workbench.service','ai-workbench-worker.service'],check=True)
 snapshot=backup(db);db.initialize(upgrade=True)
 stage=Path(tempfile.mkdtemp(prefix='deploy-',dir=BASE/'tmp/ai-workbench'))
 for name in ('src','docs','scripts','config','deploy'):
  if (ROOT/name).is_dir():shutil.copytree(ROOT/name,stage/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.env','*.key'))
 for name in ('README.md','requirements.in','requirements-dev.in','requirements.lock','pyproject.toml','LICENSE','THIRD_PARTY.md','AGENTS.md'):
  if (ROOT/name).is_file():shutil.copy2(ROOT/name,stage/name)
 hashes={str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.rglob('*') if p.is_file()}
 for name,sha in hashes.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha
 write_private(stage/'release.json',json.dumps({'deployed_at':datetime.now(timezone.utc).isoformat(),'source':str(ROOT),'backup':str(snapshot),'sha256':hashes},ensure_ascii=False,indent=2))
 subprocess.run(['systemctl','--user','stop','ai-workbench.service','ai-workbench-worker.service'],check=False)
 current=SERVICE/'app'
 if any(current.iterdir()):
  archive=BASE/'archives/ai-workbench'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');archive.parent.mkdir(parents=True,exist_ok=True);current.rename(archive)
 else:current.rmdir()
 stage.rename(current);UNITS.mkdir(parents=True,exist_ok=True)
 for p in (ROOT/'deploy').iterdir():
  if p.suffix in ('.service','.slice'):shutil.copy2(p,UNITS/p.name)
 subprocess.run(['systemctl','--user','daemon-reload'],check=True)
 subprocess.run(['systemctl','--user','enable','--now','ai-workbench.service','ai-workbench-worker.service'],check=True)
 opener=build_opener(ProxyHandler({}));deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  try:
   with opener.open('http://127.0.0.1:8090/api/v1/health',timeout=2) as r:result=json.load(r)
   if result['status']=='ok' and result['worker_online']:break
  except (OSError,ValueError):pass
  time.sleep(.5)
 else:raise SystemExit('服务健康未通过，请检查用户journal；旧app与数据备份已保留')
 print('AI工作台部署完成，程序SHA-256验证通过；前置备份：'+str(snapshot))
if __name__=='__main__':main()
