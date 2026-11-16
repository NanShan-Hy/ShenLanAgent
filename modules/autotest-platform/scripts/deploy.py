#!/usr/bin/env python3
"""Back up existing state, install a verified release, and start user units."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from datetime import datetime, timezone
from pathlib import Path

from testdock.cli import backup
from testdock.core import Database, Settings, private_write

ROOT = Path(__file__).resolve().parents[1]
SERVICE = Path('/home/nanshan/workspace/services/autotest-platform')
UNITS = Path.home() / '.config/systemd/user'


def main():
    os.umask(0o077)
    db = Database(Settings.load())
    if db.one("SELECT count(*) n FROM runs WHERE status IN ('queued','running')")['n']:
        raise SystemExit('仍有测试排队或运行；请等待队列清空后部署，未停止服务或迁移数据库')
    temporary = Path('/home/nanshan/workspace/tmp/autotest-platform')
    app = Path(tempfile.mkdtemp(prefix='deploy-', dir=temporary))
    for name in ('src', 'docs', 'scripts', 'config', 'deploy', 'examples'):
        source, target = ROOT / name, app / name
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env', '*.key'))
    for name in ('README.md', 'requirements.in', 'requirements.lock', 'pyproject.toml', 'LICENSE', 'THIRD_PARTY.md', 'AGENTS.md'):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, app / name)
    hashes = {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest() for p in app.rglob('*') if p.is_file()}
    for name, digest in hashes.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    # Close ingress before the final drain check so a concurrent submit cannot be killed.
    subprocess.run(['systemctl','--user','stop','testdock.service'],check=False)
    if db.one("SELECT count(*) n FROM runs WHERE status IN ('queued','running')")['n']:
        subprocess.run(['systemctl','--user','start','testdock.service'],check=False)
        raise SystemExit('停止接入时检测到新任务，已恢复API；请待任务结束后再部署')
    subprocess.run(['systemctl', '--user', 'stop', 'testdock-ai-worker.service', 'testdock-notifier.service', 'testdock-worker.service',
                    'testdock-demo-dev.service', 'testdock-demo-test.service'], check=False)
    snapshot = backup(db)
    db.initialize(upgrade=True)
    private_write(app / 'release.json', json.dumps({'deployed_at': datetime.now(timezone.utc).isoformat(),
        'source': str(ROOT), 'backup': str(snapshot), 'sha256': hashes}, ensure_ascii=False, indent=2))
    deployed = SERVICE / 'app'
    # Unique archive plus directory rename keeps each release exact and reviewable.
    if any(deployed.iterdir()):
        archive = Path('/home/nanshan/workspace/archives/autotest-platform') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive.parent.mkdir(parents=True, exist_ok=True)
        deployed.rename(archive)
    else:
        deployed.rmdir()
    app.rename(deployed)
    UNITS.mkdir(parents=True, exist_ok=True)
    for unit in (ROOT / 'deploy').glob('*.service'):
        shutil.copy2(unit, UNITS / unit.name)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'testdock.service', 'testdock-worker.service', 'testdock-notifier.service', 'testdock-ai-worker.service',
                    'testdock-demo-dev.service', 'testdock-demo-test.service'], check=True)
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            with opener.open('http://127.0.0.1:8088/api/health', timeout=2) as response:
                healthy = json.load(response)
            if healthy.get('status') == 'ok' and healthy.get('worker_online') and healthy.get('notifier_online'):
                for port in (8091, 8092):
                    with opener.open(f'http://127.0.0.1:{port}/api/info', timeout=2) as target:
                        assert json.load(target)['ready']
                break
        except (OSError, ValueError, AssertionError):
            pass
        time.sleep(.5)
    else:
        raise SystemExit('部署后健康检查未通过，请检查用户 journal 并按运行说明从已保留归档回退。数据未覆盖。')
    print('部署完成；程序校验通过；前置备份：' + str(snapshot))


if __name__ == '__main__':
    main()
