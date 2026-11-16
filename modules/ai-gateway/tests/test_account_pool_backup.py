import json
from pathlib import Path
import pytest

from aigateway.account_pool_backup import snapshot,verify
from aigateway.core import private_write
from scripts.manage_account_pool import prepare_service


def binary(tmp_path):
    p=tmp_path/'synthetic-backend';p.write_bytes(b'BACKUP-ONLY-DUMMY-BINARY');return p


def test_pool_backup_restores_to_new_keys_and_quarantines_old_authorizations(tmp_path):
    service=tmp_path/'stopped-service'
    original=prepare_service(binary(tmp_path),service=service,port=8095)
    private_write(service/'data/credentials/codex-qa.json',json.dumps({'type':'codex','refresh_token':'SYNTHETIC-ROTATING-REFRESH'}))
    saved=original.read_bytes()
    snap=snapshot(service,tmp_path/'backups')
    restored=tmp_path/'isolated-restore';report=verify(snap,restored)
    assert report['active_credential_files']==0 and report['archived_credential_files']==1
    assert not list((restored/'data/credentials').glob('*.json'))
    assert (restored/'archived-source/data/credentials/codex-qa.json').exists()
    new=json.loads((restored/'config/connector.json').read_text());old=json.loads(saved)
    assert new['client_key']!=old['client_key'] and new['management_key']!=old['management_key']
    assert original.read_bytes()==saved
    assert json.loads((restored/'config/config.yaml').read_text())['oauth']['auth-dir']==str(restored/'data/credentials')
    with pytest.raises(ValueError):verify(snap,restored)
    assert all(p.stat().st_mode&0o777==(0o700 if p.is_dir() else 0o600) for p in restored.rglob('*'))


def test_pool_backup_hash_tampering_and_symlink_are_rejected(tmp_path):
    service=tmp_path/'stopped-service';prepare_service(binary(tmp_path),service=service)
    snapshot_root=tmp_path/'backups';snap=snapshot(service,snapshot_root)
    with (snap/'config/connector.json').open('a') as f:f.write('tampered')
    with pytest.raises(ValueError):verify(snap,tmp_path/'bad-restore')
    (service/'data/unsafe-link').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):snapshot(service,snapshot_root)
