"""Stage a verified release, drain requests, back up, and install the independent user unit."""
import json
import argparse
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from aigateway import __version__
from aigateway.cli import backup, copy_source, initialize
from aigateway.core import BASE, Database, Settings, digest, private_write

ROOT = Path(__file__).resolve().parents[1]
SERVICE = BASE / 'services/ai-gateway'


def upgrade_account_adapter(db,binary):
    from aigateway.account_pool_config import load_connector
    from aigateway.account_pool_backup import snapshot
    if __package__:
        from .manage_account_pool import service_unit
    else:
        from manage_account_pool import service_unit
    binary=binary.resolve();manifest=json.loads((binary.parent/'release.json').read_text())
    if (not binary.is_relative_to(BASE/'tools/cliproxyapi') or manifest.get('pinning_contract')!=1
            or digest(binary.read_bytes())!=manifest.get('binary_sha256')
            or digest((ROOT/'bridge/account-binding.patch').read_bytes())!=manifest.get('patch_sha256')):
        raise ValueError('账号绑定适配器校验失败')
    config=load_connector(db.settings);connector=db.settings.account_pool_config
    service=connector.parent.parent;unit=Path.home()/'.config/systemd/user/gpt-account-pool.service'
    old_connector=connector.read_text();old_unit=unit.read_text();old_release=(service/'app/release.json').read_text()
    was_active=subprocess.run(['systemctl','--user','is-active','--quiet','gpt-account-pool.service'],capture_output=True).returncode==0
    subprocess.run(['systemctl','--user','stop','gpt-account-pool.service'],check=True,capture_output=True)
    try:
        saved=snapshot(service,BASE/'backups/gpt-account-pool',unit=unit)
        private_write(unit,service_unit(binary,service))
        config.update(pinning_supported=True,adapter_version=manifest['version'])
        private_write(connector,json.dumps(config,indent=2))
        private_write(service/'app/release.json',json.dumps({**manifest,'binary':str(binary),'backup':str(saved)},indent=2))
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','start','gpt-account-pool.service'],check=True)
        with httpx.Client(trust_env=False,timeout=2) as client:
            for _ in range(40):
                try:
                    r=client.get(config['base_url'].removesuffix('/v1')+'/v8/management/gateway-accounts',headers={'Authorization':'Bearer '+config['management_key']})
                    if r.status_code==200 and r.json().get('contract')==1:return saved
                except (httpx.HTTPError,ValueError):pass
                time.sleep(.25)
        raise RuntimeError('账号池绑定接口未通过健康检查')
    except BaseException:
        subprocess.run(['systemctl','--user','stop','gpt-account-pool.service'],check=False,capture_output=True)
        private_write(unit,old_unit);private_write(connector,old_connector);private_write(service/'app/release.json',old_release)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        if was_active:subprocess.run(['systemctl','--user','start','gpt-account-pool.service'],check=False)
        # OAuth files may have refreshed after startup; they are never rolled backwards.
        raise


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-pool-config',type=Path)
    parser.add_argument('--account-pool-adapter',type=Path)
    args=parser.parse_args()
    # Validate bundled tokenizers offline before draining or stopping the service.
    from aigateway.token_budget import warm_tokenizers
    warm_tokenizers()
    config = SERVICE / 'config/settings.json'
    db = Database(Settings.load(config)) if config.exists() else initialize(config)
    previous_config=config.read_text()
    if args.account_pool_config:
        from aigateway.account_pool_config import load_connector
        load_connector(replace(db.settings,account_pool_config=args.account_pool_config))
    temporary = BASE / 'tmp/ai-gateway'
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = Path(tempfile.mkdtemp(prefix='deploy-', dir=temporary))
    staged = parent / 'app'
    copy_source(ROOT, staged)
    hashes = {str(p.relative_to(staged)): digest(p.read_bytes()) for p in staged.rglob('*') if p.is_file()}
    for relative, expected in hashes.items():
        assert digest((ROOT / relative).read_bytes()) == expected, relative
    flag = db.settings.data_dir / 'maintenance'
    was_active = subprocess.run(['systemctl','--user','is-active','--quiet','ai-gateway.service'],capture_output=True).returncode == 0
    stopped = False
    switched = False
    snapshot=None;archive=None;adapter_snapshot=None
    private_write(flag, 'deployment in progress')
    try:
        deadline = time.monotonic() + 25
        while db.one("SELECT count(*) n FROM requests WHERE status='running'")['n']:
            if time.monotonic() > deadline:
                raise RuntimeError('仍有请求运行，部署已停止，原服务保持运行')
            time.sleep(.5)
        stop = subprocess.run(['systemctl', '--user', 'stop', 'ai-gateway.service'], capture_output=True)
        if stop.returncode and was_active:
            raise RuntimeError('服务停止失败，未备份或迁移数据库')
        if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'ai-gateway.service'], capture_output=True).returncode == 0:
            raise RuntimeError('服务仍在运行，未备份或迁移数据库')
        stopped = True
        snapshot = backup(db, source=ROOT)
        if args.account_pool_adapter:adapter_snapshot=upgrade_account_adapter(db,args.account_pool_adapter)
        if args.account_pool_config:
            raw=json.loads(previous_config);raw['account_pool_config']=str(args.account_pool_config)
            private_write(config,json.dumps(raw,ensure_ascii=False,indent=2))
            db=Database(Settings.load(config))
        db.initialize()
        try:
            commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        except subprocess.CalledProcessError:
            commit = None
        dirty = subprocess.run(['git','-C',str(ROOT),'status','--porcelain'],capture_output=True,text=True).stdout.strip()
        private_write(staged / 'release.json', json.dumps({'version':__version__, 'source_dirty':bool(dirty), 'deployed_at': datetime.now(timezone.utc).isoformat(),
                      'source': str(ROOT), 'commit': commit, 'backup': str(snapshot),'account_pool_backup':str(adapter_snapshot) if adapter_snapshot else None, 'sha256': hashes}, ensure_ascii=False, indent=2))
        deployed = SERVICE / 'app'
        if deployed.exists():
            archive = BASE / 'archives/ai-gateway' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if archive.exists():
                raise RuntimeError('同名归档已存在，不覆盖')
            deployed.rename(archive)
        staged.rename(deployed)
        switched = True
        for path in [deployed, *deployed.rglob('*')]:
            path.chmod(0o750 if path.is_dir() else 0o640)
        units = Path.home() / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'deploy/ai-gateway.service', units / 'ai-gateway.service')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    finally:
        flag.unlink(missing_ok=True)
        if was_active and stopped and not switched:
            # No service has reopened the database: restoring this pre-migration copy
            # cannot remove post-release calls. Never do this after a switched start.
            if snapshot:
                with sqlite3.connect(snapshot/'data/gateway.sqlite3') as old,sqlite3.connect(db.path) as current:old.backup(current)
            if archive and not (SERVICE/'app').exists():archive.rename(SERVICE/'app')
            private_write(config,previous_config)
            subprocess.run(['systemctl','--user','start','ai-gateway.service'],check=False)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'ai-gateway.service'], check=True)
    with httpx.Client(trust_env=False, timeout=2) as client:
        for _ in range(30):
            try:
                response = client.get('http://127.0.0.1:' + str(db.settings.port) + '/api/v1/health')
                response.raise_for_status()
                if response.json()['status'] == 'ok':
                    print('独立网关部署完成；校验文件 ' + str(len(hashes)) + '；前置备份：' + str(snapshot))
                    return
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            time.sleep(.5)
    raise RuntimeError('部署后健康检查失败；配置与数据已保留，请检查journal')


if __name__ == '__main__':
    main()
