#!/usr/bin/env python3
"""Restore a backup into a new temporary directory and validate data/key consistency."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


def verify(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    temporary_root = Path('/home/nanshan/workspace/tmp/autotest-platform').resolve()
    if not output.is_relative_to(temporary_root) or output == temporary_root:
        raise ValueError('恢复验证目标必须位于项目临时目录内的全新子目录')
    if output.exists():
        raise FileExistsError('不覆盖已有恢复目录')
    manifest = json.loads((source / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('备份校验失败：' + name)
    shutil.copytree(source, output)
    for path in [output, *output.rglob('*')]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    cipher = Fernet((output / 'master.key').read_bytes().strip())
    with sqlite3.connect(output / 'testdock.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for row in db.execute('SELECT secrets FROM environments UNION ALL SELECT snapshot FROM runs'):
            json.loads(cipher.decrypt(row[0].encode()))
        counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('users', 'projects', 'environments', 'cases', 'runs')}
        db.execute('UPDATE schedules SET enabled=0')
        db.execute("UPDATE runs SET status='cancelled',cancel_requested=1,error='隔离恢复副本不自动执行测试' WHERE status IN ('queued','running')")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_channels'").fetchone():
            for row in db.execute('SELECT secret FROM notification_channels'):
                json.loads(cipher.decrypt(row[0].encode()))
            for row in db.execute('SELECT payload FROM notification_deliveries'):
                json.loads(cipher.decrypt(row[0].encode()))
            db.execute('UPDATE notification_channels SET enabled=0')
            db.execute("UPDATE notification_deliveries SET status='cancelled',error='恢复副本待人工复核' WHERE status IN ('pending','sending')")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_delegations'").fetchone():
            db.execute('UPDATE ai_delegations SET revoked=1')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_codes'").fetchone():
            db.execute('DELETE FROM ai_codes')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_settings'").fetchone():
            for row in db.execute('SELECT secret FROM ai_settings'):
                value=json.loads(cipher.decrypt(row[0].encode()));value['enabled']=False
                db.execute('UPDATE ai_settings SET secret=?',(cipher.encrypt(json.dumps(value).encode()).decode(),))
            for table in ('ai_conversations','ai_tasks','ai_messages','ai_events','ai_proposals','ai_files','ai_connections'):
                for row in db.execute('SELECT secret FROM '+table):json.loads(cipher.decrypt(row[0].encode()))
            for table in ('ai_tickets','ai_source_grants','ai_connections'):db.execute('DELETE FROM '+table)
            db.execute("UPDATE ai_meta SET value=? WHERE name='installation'",(uuid.uuid4().hex,))
            db.execute('UPDATE ai_subjects SET revoked=1')
            db.execute("UPDATE ai_tasks SET status='cancelled',cancel_requested=1 WHERE status IN ('queued','running','waiting_confirmation','unknown')")
            db.execute("UPDATE ai_proposals SET status='rejected' WHERE status='pending'")
            for row in db.execute('SELECT id,secret FROM ai_conversations').fetchall():
                value=json.loads(cipher.decrypt(row[1].encode()));value.pop('gateway',None);value['gateway_sessions']=[];value['initialized']=False
                db.execute('UPDATE ai_conversations SET secret=? WHERE id=?',(cipher.encrypt(json.dumps(value).encode()).decode(),row[0]))
            for row in db.execute('SELECT id,secret FROM ai_files').fetchall():
                value=json.loads(cipher.decrypt(row[1].encode()));value['gateway']=[]
                db.execute('UPDATE ai_files SET secret=? WHERE id=?',(cipher.encrypt(json.dumps(value).encode()).decode(),row[0]))
            for p in (output/'ai').glob('*'):
                if p.is_file():cipher.decrypt(p.read_bytes())
    # This config is usable for an isolated application launch against the restored database.
    config = output / 'restored-settings.json'
    raw = json.loads((output / 'settings.json').read_text()) if (output / 'settings.json').exists() else {}
    raw.update(data_dir=str(output), key_file=str(output / 'master.key'))
    config.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    config.chmod(0o600)
    report = {'source': str(source), 'restored_to': str(output), 'checksums': 'passed',
              'sqlite_integrity': 'ok', 'encrypted_records': 'decryptable', 'counts': counts,
              'ai_grants': 'revoked in restored copy; rebind after recovery',
              'recovery_guards': 'schedules/channels/AI disabled; grants reset; pending tests, AI calls, proposals and notifications cancelled'}
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (output / 'verification.json').chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    verify(args.snapshot, args.output)
