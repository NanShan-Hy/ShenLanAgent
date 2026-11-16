import hashlib
import json
import threading
import time
import sqlite3
import os
import subprocess
import pytest
from pathlib import Path
from xml.etree import ElementTree
from cryptography.fernet import Fernet
from conftest import serve

from fastapi.testclient import TestClient

from testdock.cli import backup
from testdock.core import password_hash
from testdock.services import next_due
from testdock.worker import Worker, scheduler_tick


def submit(client, env=1, **kw):
    response = client.post('/api/projects/1/runs', json={"environment_id": env, **(kw or {"suite_id": 1})})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_auth_validation_and_password(platform):
    client, db, settings, api = platform
    with TestClient(api) as anonymous:
        assert anonymous.get('/api/projects').status_code == 401
        assert anonymous.post('/api/auth/login', json={"username": "admin", "password": "bad"}).status_code == 403
        assert anonymous.post('/api/auth/login', headers={"X-Requested-With": "TestDock", "Origin": "http://elsewhere"},
                              json={"username": "admin", "password": "bad"}).status_code == 403
    result = client.post('/api/projects/1/cases', json={"name": "invalid", "steps": [{"action": "http", "url": "/",
        "body": {"password": "NeverEchoThisSecret"}, "assertions": [{"source": "json", "expression": "["}]}]})
    assert result.status_code == 422
    assert "NeverEchoThisSecret" not in result.text
    assert client.post('/api/projects/1/schedules', json={"name": "x", "suite_id": 1, "environment_id": 1,
        "cron": "* * * * *", "timezone": "bad/zone"}).status_code == 422
    assert client.patch('/api/users/1', json={"active": False}).status_code == 409
    assert client.post('/api/me/password', json={"old_password": "test-password-123", "new_password": "changed-password"}).status_code == 200
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/auth/login', json={"username": "admin", "password": "changed-password"}).status_code == 200


def test_missing_master_key_is_not_recreated(platform):
    client, db, settings, api = platform
    settings.key_file.unlink()
    with pytest.raises(RuntimeError, match='配套主密钥'):
        settings.prepare()
    assert not settings.key_file.exists()


def test_roles_cross_project_tokens_and_artifacts(platform):
    client, db, settings, api = platform
    user = client.post('/api/users', json={"username": "viewer", "display_name": "只读", "password": "viewer-password"}).json()
    assert client.put('/api/projects/1/members', json={"user_id": user['id'], "role": "viewer"}).status_code == 200
    second = client.post('/api/projects', json={"name": "隔离项目"}).json()['id']
    run_id = submit(client)
    assert client.get(f'/api/runs/{run_id}/junit.xml').status_code == 409
    folder = settings.data_dir / 'runs' / run_id
    folder.mkdir()
    (folder / 'test.png').write_bytes(b'png')
    token = client.post('/api/projects/1/tokens', json={"name": "CI"}).json()
    headers = {"Authorization": "Bearer " + token['token']}
    with TestClient(api, headers={"X-Requested-With": "TestDock"}) as viewer:
        viewer.post('/api/auth/login', json={"username": "viewer", "password": "viewer-password"})
        assert len(viewer.get('/api/projects').json()) == 1
        assert viewer.get(f'/api/projects/{second}/cases').status_code == 403
        assert viewer.post('/api/projects/1/runs', json={"suite_id": 1, "environment_id": 1}).status_code == 403
        assert viewer.get(f'/api/runs/{run_id}/artifacts/test.png').status_code == 200
        assert viewer.get('/api/projects/1/tokens').status_code == 403
        client.delete(f"/api/projects/1/members/{user['id']}")
        assert viewer.get(f'/api/runs/{run_id}').status_code == 403
        assert viewer.get(f'/api/runs/{run_id}/artifacts/test.png').status_code == 403
    assert client.get(f'/api/runs/{run_id}/artifacts/test.png', headers=headers).status_code == 200
    assert client.post(f'/api/ci/projects/{second}/runs', headers=headers, json={"suite_id": 1, "environment_id": 1}).status_code == 403
    client.delete(f"/api/projects/1/tokens/{token['id']}")
    assert client.get(f'/api/ci/runs/{run_id}', headers=headers).status_code == 401


def test_encrypted_snapshot_and_ci_idempotency(platform):
    client, db, settings, api = platform
    assert 'demo-pass' not in client.get('/api/projects/1/environments').text
    assert b'demo-pass' not in db.path.read_bytes()
    run_id = submit(client)
    original = settings.unseal(db.one('SELECT snapshot FROM runs WHERE id=?', (run_id,))['snapshot'])
    env = client.get('/api/projects/1/environments').json()[0]
    client.put('/api/projects/1/environments/1', json={"name": env['name'], "variables": {"changed": True}, "secrets": {"password": "new-secret"}})
    assert settings.unseal(db.one('SELECT snapshot FROM runs WHERE id=?', (run_id,))['snapshot']) == original
    assert original['environment']['secrets']['password'] == 'demo-pass'
    token = client.post('/api/projects/1/tokens', json={"name": "CI"}).json()
    headers = {"Authorization": "Bearer " + token['token'], "Idempotency-Key": "build-42"}
    a = client.post('/api/ci/projects/1/runs', json={"suite_id": 1, "environment_id": 2}, headers=headers)
    b = client.post('/api/ci/projects/1/runs', json={"suite_id": 1, "environment_id": 2}, headers=headers)
    assert a.status_code == b.status_code == 201 and a.json()['id'] == b.json()['id']
    assert client.post('/api/ci/projects/1/runs', json={"suite_id": 1, "environment_id": 1}, headers=headers).status_code == 409
    assert client.post(f"/api/ci/runs/{a.json()['id']}/cancel", headers=headers).status_code == 200
    assert client.get(f"/api/ci/runs/{a.json()['id']}", headers=headers).json()['status'] == 'cancelled'


def test_real_web_api_two_environments_and_backup(targets, tmp_path):
    client, db, settings, api = targets
    for env in (1, 2):
        run_id = submit(client, env)
        assert Worker(settings).run_once()
        detail = client.get('/api/runs/' + run_id).json()
        assert detail['status'] == 'passed', detail
        assert len(detail['results']) == 2
        assert detail['summary']['passed'] == 2
        assert 'demo-pass' not in json.dumps(detail) and 'demo-token-' not in json.dumps(detail)
        assert len(ElementTree.fromstring(client.get(f'/api/runs/{run_id}/junit.xml').text)) == 2
    target = backup(db, root=tmp_path / 'backups')
    manifest = json.loads((target / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    restored = sqlite3.connect(target / 'testdock.sqlite3')
    assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert restored.execute("SELECT count(*) FROM runs WHERE status='passed'").fetchone()[0] == 2
    sealed = restored.execute('SELECT snapshot FROM runs LIMIT 1').fetchone()[0]
    assert json.loads(Fernet((target / 'master.key').read_bytes()).decrypt(sealed.encode()))['cases']
    restored.close()


def test_ci_script_real_execution(targets, tmp_path):
    client, db, settings, api = targets
    suite = client.post('/api/projects/1/suites', json={"name": "CI 接口", "case_ids": [2]}).json()['id']
    token = client.post('/api/projects/1/tokens', json={"name": "CI 脚本验收"}).json()['token']
    with serve(api) as base:
        process = subprocess.Popen([os.sys.executable, 'scripts/ci_run.py', '--url', base, '--project', '1',
             '--suite', str(suite), '--environment', '2', '--junit', str(tmp_path / 'ci-junit.xml')],
             env={**os.environ, 'TESTDOCK_CI_TOKEN': token}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not db.one("SELECT id FROM runs WHERE status='queued'") and time.monotonic() < deadline:
            time.sleep(.05)
        Worker(settings).run_once()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert token not in stdout + stderr
        assert ElementTree.parse(tmp_path / 'ci-junit.xml').getroot().attrib['failures'] == '0'


def test_failure_artifacts_and_path_protection(targets):
    client, db, settings, api = targets
    case = client.post('/api/projects/1/cases', json={"name": "失败附件验证", "steps": [
        {"action": "goto", "url": "/"}, {"action": "assert_text", "locator": "h1", "value": "不存在的文字", "timeout": 1}]}).json()
    run_id = submit(client, case_ids=[case['id']])
    Worker(settings).run_once()
    detail = client.get('/api/runs/' + run_id).json()
    assert detail['status'] == 'failed', detail
    assert any(p.endswith('failure.png') for p in detail['artifacts'])
    assert any(p.endswith('trace.zip') for p in detail['artifacts'])
    for name in detail['artifacts']:
        assert client.get(f'/api/runs/{run_id}/artifacts/{name}').status_code == 200
    assert client.get(f'/api/runs/{run_id}/artifacts/%2e%2e%2f%2e%2e%2fmaster.key').status_code == 404
    assert 'failure' in client.get(f'/api/runs/{run_id}/junit.xml').text


def test_scheduler_dedup_overdue_and_roles(platform):
    client, db, settings, api = platform
    response = client.post('/api/projects/1/schedules', json={"name": "每分钟", "suite_id": 1,
        "environment_id": 1, "cron": "* * * * *", "timezone": "Asia/Shanghai"})
    sid = response.json()['id']
    now = time.time()
    db.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (now - 1, sid))
    scheduler_tick(db, now)
    scheduler_tick(db, now)
    assert db.one('SELECT count(*) AS n FROM schedule_fires')['n'] == 1
    db.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (now + 60, sid))
    scheduler_tick(db, now + 61)
    assert db.one('SELECT count(*) AS n FROM schedule_fires')['n'] == 1  # no overlap
    db.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (now - 500, sid))
    scheduler_tick(db, now)
    assert '跳过' in db.one('SELECT last_error FROM schedules')['last_error']
    db.execute('UPDATE users SET active=0 WHERE id=1')
    db.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (now, sid))
    scheduler_tick(db, now)
    assert db.one('SELECT enabled FROM schedules')['enabled'] == 0
    assert next_due('0 9 * * *', 'Asia/Shanghai', 0) == 3600


def test_cancel_timeout_and_restart_recovery(targets):
    client, db, settings, api = targets
    case = client.post('/api/projects/1/cases', json={"name": "等待用例", "steps": [
        {"action": "goto", "url": "/"}, {"action": "wait_for", "locator": "#never", "timeout": 20}]}).json()
    run_id = submit(client, case_ids=[case['id']])
    worker = Worker(settings)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    deadline = time.monotonic() + 10
    while not (settings.data_dir / 'runs' / run_id / 'progress.json').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    client.post(f'/api/runs/{run_id}/cancel')
    thread.join(15)
    assert not thread.is_alive()
    assert client.get('/api/runs/' + run_id).json()['status'] == 'cancelled'
    timeout_id = submit(client, case_ids=[case['id']])
    worker.settings.run_timeout = .5
    worker.run_once()
    detail = client.get('/api/runs/' + timeout_id).json()
    assert detail['status'] == 'error' and '总时间' in detail['error']
    recovery_id = submit(client)
    worker.claim()
    worker.recover()
    assert client.get('/api/runs/' + recovery_id).json()['status'] == 'error'
    assert not worker.claim()
