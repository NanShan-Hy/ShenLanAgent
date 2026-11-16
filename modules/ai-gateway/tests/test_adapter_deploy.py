import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aigateway.core import BASE,private_write
from scripts.manage_account_pool import prepare_service
from scripts import deploy
from aigateway import account_pool_backup


@pytest.mark.parametrize('success',[True,False])
def test_adapter_upgrade_preserves_access_and_rolls_back_program_only(gateway,tmp_path,monkeypatch,success):
    binary=BASE/'tools/cliproxyapi/8.0.72-gateway.2/cli-proxy-api'
    service=tmp_path/'pool';connector=prepare_service(binary,service=service,port=19871);gateway['settings'].account_pool_config=connector
    before=connector.read_text();home=tmp_path/'operator-home';unit=home/'.config/systemd/user/gpt-account-pool.service';private_write(unit,'ORIGINAL_UNIT')
    oauth=service/'data/credentials/fixture.json';private_write(oauth,'ORIGINAL_SYNTHETIC_OAUTH')
    monkeypatch.setattr(Path,'home',lambda:home)
    calls=[]
    monkeypatch.setattr(deploy.subprocess,'run',lambda args,**kw:calls.append(args) or SimpleNamespace(returncode=0))
    real_snapshot=account_pool_backup.snapshot
    monkeypatch.setattr(account_pool_backup,'snapshot',lambda directory,root,**kw:real_snapshot(directory,tmp_path/'backups',**kw))
    real_client=httpx.Client
    def handle(request):
        if not success:private_write(oauth,'ROTATED_SYNTHETIC_OAUTH')
        return httpx.Response(200 if success else 503,json={'contract':1})
    monkeypatch.setattr(deploy.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw))
    monkeypatch.setattr(deploy.time,'sleep',lambda _:None)
    if success:
        snapshot=deploy.upgrade_account_adapter(gateway['db'],binary)
        assert snapshot.is_dir() and json.loads(connector.read_text())['pinning_supported']
        assert oauth.read_text()=='ORIGINAL_SYNTHETIC_OAUTH'
        assert '8.0.72-gateway.2' in unit.read_text()
        assert json.loads(connector.read_text())['client_key']==json.loads(before)['client_key']
    else:
        with pytest.raises(RuntimeError):deploy.upgrade_account_adapter(gateway['db'],binary)
        assert connector.read_text()==before and unit.read_text()=='ORIGINAL_UNIT'
        assert oauth.read_text()=='ROTATED_SYNTHETIC_OAUTH'
    assert any(args[2:]==['stop','gpt-account-pool.service'] for args in calls)
