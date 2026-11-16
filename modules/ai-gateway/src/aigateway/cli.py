from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .billing import ledger_check, maintenance, settle
from .core import BASE, DEFAULT_CONFIG, Database, Settings, create_user, digest, password_hash, private_write


def initialize(config=DEFAULT_CONFIG):
    config = Path(config)
    if not config.exists():
        service = config.parent.parent
        for name in ('config', 'data', 'logs'):
            (service / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            (service / name).chmod(0o700)
        settings = Settings(service / 'data', config.parent / 'master.key')
        raw = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(settings).items()}
        private_write(config, json.dumps(raw, ensure_ascii=False, indent=2), exclusive=True)
    settings = Settings.load(config)
    db = Database(settings)
    db.initialize()
    if not db.one('SELECT 1 FROM users'):
        password = secrets.token_urlsafe(20)
        with db.transaction() as c:
            create_user(c, 'admin', '运营管理员', password, admin=True)
        private_write(config.parent / 'initial-admin.txt',
                      'AI Gateway 初始管理员\n账号：admin\n密码：' + password +
                      '\n首次登录后修改密码。本文件仅当前用户可读，不提交源码。\n', exclusive=True)
    return db


def copy_source(source, target):
    target.mkdir(mode=0o700)
    for name in ('src', 'docs', 'scripts', 'config', 'deploy', 'examples', 'tests','bridge'):
        path = source / name
        if path.is_dir():
            if any(p.is_symlink() for p in path.rglob('*')):
                raise ValueError('打包来源包含符号链接，请明确存储边界')
            shutil.copytree(path, target / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.key', '*.pem',
                            '.env', 'settings.json', 'initial-admin.txt', 'recovery-admin.txt'))
    for name in ('README.md', 'AGENTS.md', 'pyproject.toml', 'requirements.in', 'requirements.lock'):
        if (source / name).exists():
            shutil.copy2(source / name, target / name)


def secure_tree(path):
    for p in [path, *path.rglob('*')]:
        if p.is_symlink():
            raise ValueError('敏感快照不允许符号链接')
        p.chmod(0o700 if p.is_dir() else 0o600)


def backup(db, *, source=None, root=None):
    if shutil.disk_usage(db.settings.data_dir).free < max(db.settings.min_free_bytes, 50 * 1024**2):
        raise RuntimeError('可用空间不足，未创建备份')
    source = Path(source or Path(__file__).resolve().parents[2])
    root = Path(root or BASE / 'backups/ai-gateway')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3))
    target.mkdir(mode=0o700)
    (target / 'data').mkdir(mode=0o700)
    with db.connect() as current, sqlite3.connect(target / 'data/gateway.sqlite3') as output:
        current.backup(output)
    (target / 'config').mkdir(mode=0o700)
    shutil.copy2(db.settings.key_file, target / 'config/master.key')
    raw = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(db.settings).items()}
    private_write(target / 'config/settings.json', json.dumps(raw, ensure_ascii=False, indent=2))
    installed_unit = Path.home() / '.config/systemd/user/ai-gateway.service'
    if installed_unit.exists():
        shutil.copy2(installed_unit, target / 'config/ai-gateway.service')
    copy_source(source, target / 'source')
    if (source / '.git').is_dir():
        shutil.copytree(source / '.git', target / 'git')
    hashes = {str(p.relative_to(target)): digest(p.read_bytes()) for p in target.rglob('*') if p.is_file()}
    private_write(target / 'manifest.json', json.dumps({'version': __version__, 'database_schema':db.one('SELECT version FROM schema_version')['version'], 'created_at': time.time(),
                  'source': str(source), 'sha256': hashes}, ensure_ascii=False, indent=2))
    secure_tree(target)
    return target


def verify(snapshot, output):
    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('恢复目标已存在，不覆盖')
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    for relative, expected in manifest['sha256'].items():
        path = (snapshot / relative).resolve()
        if not path.is_relative_to(snapshot) or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('快照校验失败：' + relative)
    output.mkdir(parents=True, mode=0o700)
    for name in ('data', 'config', 'source'):
        shutil.copytree(snapshot / name, output / name)
    raw = json.loads((output / 'config/settings.json').read_text())
    raw.update(data_dir=str(output / 'data'), key_file=str(output / 'config/master.key'),
               public_url='http://127.0.0.1:8096', bind='127.0.0.1', port=8096,
               public_registration=False, allow_private_upstreams=False)
    raw['account_pool_config'] = None
    private_write(output / 'config/settings.json', json.dumps(raw, ensure_ascii=False, indent=2))
    settings = Settings.load(output / 'config/settings.json')
    db = Database(settings)
    source_schema = db.one('SELECT version FROM schema_version')['version']
    db.initialize()
    encrypted_records = 0
    with db.connect() as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite完整性校验失败')
        for row in c.execute('SELECT secret FROM upstreams'):
            settings.unseal(row['secret'])
            encrypted_records += 1
        capture_chunks = 0
        for row in c.execute('SELECT audit_id,section,seq,ciphertext FROM request_capture_chunks'):
            value = settings.unseal(row['ciphertext'])
            if value.get('audit_id') != row['audit_id'] or value.get('section') != row['section'] or value.get('seq') != row['seq']:
                raise ValueError('请求正文密文归属校验失败')
            capture_chunks += 1
        stored = c.execute('SELECT COALESCE(sum(byte_size),0) FROM request_capture_chunks').fetchone()[0]
        if stored != c.execute('SELECT used_bytes FROM request_recording_settings WHERE id=1').fetchone()[0]:
            raise ValueError('请求正文容量记录不一致')
    before = ledger_check(db)
    from .managed_store import verify as verify_managed
    managed_chunks=verify_managed(db)
    if not before['ok']:
        raise ValueError('恢复前账本不一致')
    for row in db.all("SELECT id FROM requests WHERE status IN ('running','unknown')"):
        settle(db, row['id'], status='released', error='recovery_safety_release')
    maintenance(db, restart=True)
    from .request_audit import recover
    recover(db)
    from .conversations import recover as recover_conversations
    recover_conversations(db)
    recovery_password = secrets.token_urlsafe(20)
    with db.transaction() as c:
        c.execute('DELETE FROM sessions')
        c.execute('DELETE FROM invitations')
        c.execute('UPDATE api_keys SET active=0')
        c.execute('UPDATE upstreams SET enabled=0')
        c.execute('UPDATE pools SET enabled=0')
        c.execute('UPDATE models SET enabled=0')
        first = c.execute('SELECT id,username FROM users WHERE is_admin=1 ORDER BY created_at LIMIT 1').fetchone()
        if not first:
            raise ValueError('恢复副本缺少管理员')
        c.execute('UPDATE users SET active=0')
        c.execute('UPDATE users SET active=1,password_hash=? WHERE id=?', (password_hash(recovery_password), first['id']))
    private_write(output / 'config/recovery-admin.txt', '账号：' + first['username'] + '\n密码：' + recovery_password + '\n')
    after = ledger_check(db)
    result = {'sha256_verified': len(manifest['sha256']), 'integrity': 'ok', 'credentials_decrypted': True,
              'source_schema':source_schema,'restored_schema':db.one('SELECT version FROM schema_version')['version'],
              'encrypted_records_verified': encrypted_records,
              'capture_chunks_decrypted': capture_chunks,'managed_chunks_decrypted':managed_chunks,
              'ledger_before': before, 'ledger_after': after,
              'guards': 'sessions/invitations removed; keys/upstreams/pools/models disabled; unfinished holds released; recovery admin only'}
    private_write(output / 'verification.json', json.dumps(result, ensure_ascii=False, indent=2))
    secure_tree(output)
    return result


def main():
    parser = argparse.ArgumentParser(description='Independent AI Gateway')
    parser.add_argument('--config', default=os.environ.get('AI_GATEWAY_CONFIG', str(DEFAULT_CONFIG)))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('serve')
    backup_parser = sub.add_parser('backup')
    backup_parser.add_argument('--root')
    sub.add_parser('check-ledger')
    recovery = sub.add_parser('verify')
    recovery.add_argument('--snapshot', required=True)
    recovery.add_argument('--output', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'init':
        initialize(args.config)
        print('初始化完成；初始凭据保存在配置目录的initial-admin.txt，未显示密钥或密码。')
    elif args.command == 'verify':
        print(json.dumps(verify(args.snapshot, args.output), ensure_ascii=False))
    else:
        settings = Settings.load(args.config)
        db = Database(settings)
        if args.command == 'backup':
            print(backup(db, root=args.root))
        elif args.command == 'check-ledger':
            print(json.dumps(ledger_check(db), ensure_ascii=False))
        else:
            import uvicorn
            from .app import create_app
            uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, workers=1,
                        access_log=False, proxy_headers=False, log_level='warning', timeout_graceful_shutdown=20,
                        limit_concurrency=40, backlog=128)


if __name__ == '__main__':
    main()
