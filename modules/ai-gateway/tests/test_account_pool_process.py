"""Exercise the actual pinned backend in an isolated empty service, never real accounts."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import httpx
import pytest

from aigateway.account_pool_api import PREFIX
from aigateway.core import BASE,private_write
from scripts.manage_account_pool import prepare_service

BINARY=BASE/'tools/cliproxyapi/8.0.72/cli-proxy-api'


@contextmanager
def real_backend(tmp_path,*,extra_config=None,restart_callback=None):
    if not BINARY.is_file():pytest.skip('pinned CLIProxyAPI binary not installed')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    service=tmp_path/'isolated-pool'
    connector=prepare_service(BINARY,service=service,port=port)
    if extra_config:
        config=service/'config/config.yaml';raw=json.loads(config.read_text());raw.update(extra_config)
        private_write(config,json.dumps(raw))
    values=json.loads(connector.read_text())
    process=subprocess.Popen([str(BINARY),'--config',str(service/'config/config.yaml'),'--local-model'],
        cwd=service/'app',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        with httpx.Client(trust_env=False,timeout=2) as client:
            for _ in range(60):
                if process.poll() is not None:raise AssertionError('isolated backend exited: '+str(process.returncode))
                try:
                    response=client.get(values['base_url']+'/models',headers={'Authorization':'Bearer '+values['client_key']})
                    if response.status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise AssertionError('isolated account backend did not become ready')
        def restart():
            nonlocal process
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=8)
            process=subprocess.Popen([str(BINARY),'--config',str(service/'config/config.yaml'),'--local-model'],
                cwd=service/'app',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            with httpx.Client(trust_env=False,timeout=2) as client:
                for _ in range(60):
                    if process.poll() is not None:raise AssertionError('restarted isolated backend exited')
                    try:
                        if client.get(values['base_url']+'/models',headers={'Authorization':'Bearer '+values['client_key']}).status_code==200:return
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                raise AssertionError('restarted isolated backend did not become ready')
        if restart_callback:restart_callback(restart)
        yield service,connector,values
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)


def test_actual_empty_backend_facade_oauth_url_and_no_request_body_persistence(gateway,tmp_path):
    g=gateway
    with real_backend(tmp_path) as (service,connector,values):
        g['settings'].account_pool_config=connector;g['settings'].allow_private_upstreams=False
        g['app'].state.upstream_transport=None
        status=g['client'].get(PREFIX+'/status');assert status.status_code==200,status.text
        assert status.json()['online'] and status.json()['accounts']==[] and not status.json()['ready']
        bootstrap=g['client'].post(PREFIX+'/bootstrap',json={'tenant_id':g['tenant'],'sharing_consent':True})
        assert bootstrap.status_code==201,bootstrap.text
        start=g['client'].post(PREFIX+'/authorize',json={'sharing_consent':True})
        assert start.status_code==200,start.text
        flow=start.json()
        assert flow['authorization_url'].startswith('https://auth.openai.com/oauth/authorize?')
        assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()['status']=='wait'
        assert g['client'].delete(PREFIX+'/authorizations/'+flow['id']).status_code==200
        marker='SYNTHETIC_QUESTION_MUST_NOT_BE_PERSISTED_2026'
        with httpx.Client(trust_env=False,timeout=3) as client:
            assert client.get(values['base_url']+'/models').status_code in (401,403)
            root=values['base_url'].removesuffix('/v1')
            assert client.get(root+'/v8/management/credentials',headers={'Authorization':'Bearer '+values['client_key']}).status_code in (401,403)
            config=client.get(root+'/v8/management/config',headers={'Authorization':'Bearer '+values['management_key']}).json()
            assert config['server']['host']=='127.0.0.1' and config['server']['commercial-mode'] is True
            assert config['routing']['retry']=={'request-retry':0,'max-retry-credentials':1,'max-retry-interval':0}
            assert config['management']['disable-control-panel'] is True
            assert config['plugins']['enabled'] is False
            result=client.post(values['base_url']+'/responses',headers={'Authorization':'Bearer '+values['client_key']},json={'model':'no-configured-model','input':marker,'store':False})
            assert result.status_code>=400
        time.sleep(.15)
        for path in service.rglob('*'):
            if path.is_file():assert marker.encode() not in path.read_bytes()
        assert not list((service/'data/credentials').glob('*.json'))
        private_write(tmp_path/'backend-verification.json',json.dumps({'actual_pinned_binary':True,'isolated_empty_pool':True,
            'management_and_client_keys_separated':True,'facade_status':True,'actual_oauth_url_generated_then_cancelled':True,
            'no_external_signin':True,'real_provider_calls':0,'request_body_marker_persisted':False}))
