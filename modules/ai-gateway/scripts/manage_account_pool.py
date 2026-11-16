"""Install a pinned, loopback-only account backend. Never signs in or calls a model."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.request import urlopen

import httpx

from aigateway.account_pool_config import VERSION, ARCHIVE, ARCHIVE_SHA256, RELEASE_URL, render_backend
from aigateway.core import BASE, private_write


def install_tool():
    if shutil.disk_usage(BASE).free < 3*1024**3:
        raise RuntimeError('可用空间低于3GiB，未下载安装包')
    downloads=BASE/'downloads/installers/cliproxyapi'/VERSION
    downloads.mkdir(parents=True,exist_ok=True,mode=0o750)
    for p in (downloads.parent,downloads):p.chmod(0o750)
    archive=downloads/ARCHIVE
    if not archive.exists():
        descriptor, temporary=tempfile.mkstemp(prefix='download-',dir=downloads)
        try:
            with os.fdopen(descriptor,'wb') as output,urlopen(RELEASE_URL+ARCHIVE,timeout=30) as source:
                total=0
                while chunk:=source.read(1024**2):
                    total+=len(chunk)
                    if total>64*1024**2:raise RuntimeError('安装包超过64MiB')
                    output.write(chunk)
            if hashlib.sha256(Path(temporary).read_bytes()).hexdigest()!=ARCHIVE_SHA256:
                raise RuntimeError('安装包SHA-256不匹配；临时副本保留供检查')
            # Hard link provides exclusive publication instead of replacing an existing archive.
            os.link(temporary,archive);Path(temporary).unlink();archive.chmod(0o640)
        except BaseException:
            raise
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=ARCHIVE_SHA256:
        raise RuntimeError('已有同名安装包哈希不匹配，不覆盖')
    target=BASE/'tools/cliproxyapi'/VERSION
    if target.exists():
        manifest=json.loads((target/'release.json').read_text())
        binary=target/'cli-proxy-api'
        if manifest['archive_sha256']!=ARCHIVE_SHA256 or hashlib.sha256(binary.read_bytes()).hexdigest()!=manifest['binary_sha256']:
            raise RuntimeError('已安装工具校验失败，不覆盖')
        return binary
    target.mkdir(parents=True,mode=0o750);target.parent.chmod(0o750)
    allowed={'cli-proxy-api','LICENSE','README.md','README_CN.md'}
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            name=Path(member.name).name
            if name not in allowed or not member.isfile():continue
            destination=target/name
            if destination.exists():raise RuntimeError('安装包存在重名文件')
            with package.extractfile(member) as source,destination.open('xb') as output:
                shutil.copyfileobj(source,output)
            destination.chmod(0o750 if name=='cli-proxy-api' else 0o640)
    binary=target/'cli-proxy-api'
    if not binary.is_file() or not (target/'LICENSE').is_file():raise RuntimeError('安装包缺少程序或许可证')
    manifest={'version':VERSION,'source':RELEASE_URL+ARCHIVE,'archive_sha256':ARCHIVE_SHA256,
        'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
    (target/'release.json').write_text(json.dumps(manifest,indent=2));(target/'release.json').chmod(0o640)
    return binary


def prepare_service(binary, *, service=None, port=8095):
    service=Path(service or BASE/'services/gpt-account-pool')
    connector=service/'config/connector.json'
    if connector.exists():
        raw=json.loads(connector.read_text())
        if raw['base_url']!='http://127.0.0.1:'+str(port)+'/v1':raise RuntimeError('已有配置端口不同，不覆盖')
        return connector
    for name in ('app','config','data','data/credentials','logs'):
        directory=service/name;directory.mkdir(parents=True,mode=0o700,exist_ok=True);directory.chmod(0o700)
    service.chmod(0o750)
    client_key='pool_'+secrets.token_urlsafe(32);management_key=secrets.token_urlsafe(40)
    private_write(connector,json.dumps({'version':VERSION,'base_url':'http://127.0.0.1:'+str(port)+'/v1',
        'client_key':client_key,'management_key':management_key},indent=2),exclusive=True)
    private_write(service/'config/config.yaml',json.dumps(render_backend(service,port=port,client_key=client_key,management_key=management_key),indent=2),exclusive=True)
    private_write(service/'app/release.json',json.dumps({'version':VERSION,'binary':str(binary),
        'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'source':'router-for-me/CLIProxyAPI'},indent=2),exclusive=True)
    return connector


def service_unit(binary,service):
    return f'''[Unit]
Description=Local GPT account pool adapter (CLIProxyAPI {VERSION})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={service}/app
ExecStart={binary} --config {service}/config/config.yaml --local-model
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
UMask=0077
MemoryMax=256M
Nice=5
NoNewPrivileges=true
# Upstream errors may contain request material. Portal diagnostics expose only safe fields.
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
'''


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['install','status','backup','verify'])
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    service=BASE/'services/gpt-account-pool'
    if args.action=='verify':
        if not args.snapshot or not args.output:parser.error('verify需要--snapshot与全新--output')
        from aigateway.account_pool_backup import verify
        print(json.dumps(verify(args.snapshot,args.output),ensure_ascii=False));return
    if args.action=='backup':
        from aigateway.account_pool_backup import snapshot
        from aigateway.core import Database,Settings
        db=Database(Settings.load());flag=db.settings.data_dir/'maintenance'
        private_write(flag,'account pool backup',exclusive=True)
        was_active=subprocess.run(['systemctl','--user','is-active','--quiet','gpt-account-pool.service']).returncode==0
        try:
            deadline=time.monotonic()+25
            while db.one("SELECT count(*) n FROM requests WHERE status='running'")['n']:
                if time.monotonic()>deadline:raise RuntimeError('仍有模型请求运行，未停止账号池')
                time.sleep(.25)
            subprocess.run(['systemctl','--user','stop','gpt-account-pool.service'],check=True)
            destination=snapshot(service,BASE/'backups/gpt-account-pool',unit=Path.home()/'.config/systemd/user/gpt-account-pool.service')
        finally:
            try:
                if was_active:subprocess.run(['systemctl','--user','start','gpt-account-pool.service'],check=True)
            finally:flag.unlink(missing_ok=True)
        print(str(destination));return
    if args.action=='install':
        binary=install_tool();connector=prepare_service(binary,service=service)
        target=Path.home()/'.config/systemd/user/gpt-account-pool.service'
        unit=service_unit(binary,service)
        if target.exists() and target.read_text()!=unit:raise RuntimeError('已有不同服务单元，不覆盖')
        if not target.exists():private_write(target,unit,exclusive=True)
        subprocess.run(['systemd-analyze','--user','verify',str(target)],check=True,capture_output=True)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','enable','--now','gpt-account-pool.service'],check=True,capture_output=True)
    connector=service/'config/connector.json';raw=json.loads(connector.read_text())
    with httpx.Client(trust_env=False,timeout=3) as client:
        for _ in range(40):
            try:
                response=client.get(raw['base_url'].removesuffix('/v1')+'/v8/management/credentials',headers={'Authorization':'Bearer '+raw['management_key']})
                response.raise_for_status();files=response.json()['files'];break
            except (httpx.HTTPError,ValueError,KeyError):time.sleep(.25)
        else:raise RuntimeError('账号池健康检查未通过；内部凭据未输出')
        unauthorized=client.get(raw['base_url']+'/models');assert unauthorized.status_code in (401,403)
        models=client.get(raw['base_url']+'/models',headers={'Authorization':'Bearer '+raw['client_key']});models.raise_for_status()
    print(json.dumps({'version':VERSION,'base_url':raw['base_url'],'credential_records':len(files),
        'models':len(models.json().get('data',[])),'unauthenticated_rejected':True,'connector_path':str(connector),
        'new_authorization_started':False,'model_generation_requests':0},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('账号池操作未完成：'+type(error).__name__+'；请检查本地配置与安装校验，不输出凭据。')
