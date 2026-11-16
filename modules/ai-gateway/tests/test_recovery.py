from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aigateway.app import create_app
from aigateway.billing import ledger_check
from aigateway.cli import backup, verify
from aigateway.core import Database, Settings
from aigateway.routing import admit
from conftest import configured


def test_backup_decrypt_ledger_and_recovery_guards(gateway,tmp_path):
    g=configured(gateway)
    pending=admit(g['db'],g['token'],'testclient',{'model':'qa-model','messages':[{'role':'user','content':'恢复合成任务'}]},'chat','/chat/completions')
    source=Path(__file__).resolve().parents[1]
    snapshot=backup(g['db'],source=source,root=tmp_path/'backups')
    output=tmp_path/'restored'
    result=verify(snapshot,output)
    assert result['integrity']=='ok' and result['ledger_before']['ok'] and result['ledger_after']['ok']
    assert result['encrypted_records_verified']==1
    assert g['db'].one('SELECT status FROM requests WHERE id=?',(pending['id'],))['status']=='running'
    settings=Settings.load(output/'config/settings.json')
    db=Database(settings)
    assert db.one("SELECT count(*) n FROM api_keys WHERE active=1")['n']==0
    assert db.one("SELECT count(*) n FROM upstreams WHERE enabled=1")['n']==0
    assert db.one('SELECT status FROM requests WHERE id=?',(pending['id'],))['status']=='released'
    assert ledger_check(db)['ok']
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/v1/health').status_code==200
        assert client.get('/v1/models',headers=g['headers']).status_code==401
    with pytest.raises(ValueError):verify(snapshot,output)


def test_cli_environment_config_uses_isolated_service(tmp_path,monkeypatch):
    import os
    import sys
    from aigateway.cli import main
    config=tmp_path/'isolated-service/config/settings.json'
    monkeypatch.setenv('AI_GATEWAY_CONFIG',str(config))
    monkeypatch.setattr(sys,'argv',['aigateway','init'])
    old_umask=os.umask(0o077)
    try:
        main()
    finally:
        os.umask(old_umask)
    assert config.exists()
    settings=Settings.load(config)
    assert settings.data_dir==config.parent.parent/'data'
    assert Database(settings).one('SELECT count(*) n FROM users')['n']==1
