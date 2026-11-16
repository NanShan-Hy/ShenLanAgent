import copy
import gzip
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import Response

from conftest import serve
from testdock.core import encode
from testdock.engine import execute_snapshot
from testdock.models import ImportInput
from testdock.transfer import import_assets
from testdock.worker import Worker


def test_asset_roundtrip_preview_conflict_and_transaction(platform):
    c, db, settings, _ = platform
    exported = c.get('/api/projects/1/assets/export')
    assert exported.status_code == 200 and 'demo-pass' not in exported.text
    bundle = exported.json()
    assert bundle['environments'][0]['secret_keys'] == ['password']
    assert bundle['cases'][1]['steps'][0]['extract'][0]['secret'] is True
    body = {'bundle': bundle, 'prefix': '副本 · '}
    assert c.post('/api/projects/1/assets/import', json=body).json()['dry_run']
    assert len(c.get('/api/projects/1/cases').json()) == 2
    result = c.post('/api/projects/1/assets/import', json={**body, 'dry_run': False})
    assert result.status_code == 200, result.text
    assert result.json()['counts'] == {'cases': 2, 'suites': 1, 'environments': 2}
    assert result.json()['warnings']
    assert c.post('/api/projects/1/assets/import', json={**body, 'dry_run': False}).status_code == 409
    assert len(c.get('/api/projects/1/cases').json()) == 4
    broken = copy.deepcopy(bundle); broken['suites'][0]['case_ids'] = ['foreign-key']
    assert c.post('/api/projects/1/assets/import', json={'bundle': broken, 'dry_run': False}).status_code == 422
    # A conflict discovered after case checks still leaves no partial additions.
    broken = copy.deepcopy(bundle); broken['cases'][0]['name'] = '不应写入'
    assert c.post('/api/projects/1/assets/import', json={'bundle': broken, 'dry_run': False}).status_code == 409
    assert not db.one("SELECT id FROM cases WHERE name='不应写入'")


def test_openapi_preview_refs_auth_parameters_and_execution(targets):
    c, db, settings, _ = targets
    document = {'openapi': '3.1.0', 'paths': {'/api/info': {'get': {'summary': '环境信息', 'responses': {'200': {}}}},
        '/items/{id}': {'parameters': [{'$ref': '#/components/parameters/id'}], 'post': {
            'security': [{'bearer': []}], 'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Item'}}}},
            'responses': {'201': {}}}}}, 'components': {'parameters': {'id': {'name': 'id', 'in': 'path', 'schema': {'type': 'integer'}}},
        'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}},
        'schemas': {'Item': {'type': 'object', 'properties': {'name': {'type': 'string', 'example': 'test'}, 'password': {'type': 'string', 'example': 'secret-doc-value'}}}}}}
    response = c.post('/api/projects/1/openapi/preview', json={'document': document})
    assert response.status_code == 200, response.text
    preview = response.json(); step = preview['bundle']['cases'][1]['steps'][0]
    assert step['url'] == '/items/{{path_id}}'
    assert step['headers']['Authorization'] == 'Bearer {{auth_bearer}}'
    assert step['body']['password'] == '{{body_password}}'
    assert 'secret-doc-value' not in response.text
    assert preview['required_variables'] == ['auth_bearer', 'body_password', 'path_id']
    chosen = preview['bundle']; chosen['cases'] = chosen['cases'][:1]
    assert c.post('/api/projects/1/assets/import', json={'bundle': chosen, 'dry_run': False}).status_code == 200
    case = db.one("SELECT id FROM cases WHERE name LIKE 'GET /api/info%'")
    run = c.post('/api/projects/1/runs', json={'case_ids': [case['id']], 'environment_id': 1}).json()['id']
    Worker(settings).run_once()
    assert c.get('/api/runs/' + run).json()['status'] == 'passed'
    cyclic = {'openapi': '3.0.3', 'paths': {'/bad': {'$ref': '#/paths/~1bad'}}}
    assert c.post('/api/projects/1/openapi/preview', json={'document': cyclic}).status_code == 400


def test_matrix_atomic_and_sales_business_flow(targets):
    c, db, settings, _ = targets
    bundle = json.loads(Path('examples/chemical-sales.json').read_text()); bundle['environments'] = []
    import_assets(db, 1, ImportInput(bundle=bundle, dry_run=False))
    for pk in (1, 2):
        row = db.one('SELECT variables FROM environments WHERE id=?', (pk,))
        variables = {**json.loads(row['variables']), 'quantity': 2, 'expected_total_cents': 1380, 'customer': '测试客户'}
        db.execute('UPDATE environments SET variables=? WHERE id=?', (encode(variables), pk))
    suite = db.one("SELECT id FROM suites WHERE name='销售核心流程回归'")['id']
    before = db.one('SELECT count(*) n FROM runs')['n']
    assert c.post('/api/projects/1/runs/matrix', json={'suite_id': suite, 'environment_ids': [1, 999]}).status_code == 404
    assert db.one('SELECT count(*) n FROM runs')['n'] == before
    result = c.post('/api/projects/1/runs/matrix', json={'suite_id': suite, 'environment_ids': [1, 2, 1]}).json()
    assert len(result['runs']) == 2
    for run in result['runs']:
        Worker(settings).run_once()
        detail = c.get('/api/runs/' + run['id']).json()
        assert detail['status'] == 'passed', detail
        assert detail['summary']['passed'] == 2


def test_failed_rerun_keeps_original_snapshot(targets):
    c, db, settings, _ = targets
    case = c.post('/api/projects/1/cases', json={'name': '失败验证', 'steps': [{'action': 'http', 'url': '/api/info',
        'assertions': [{'source': 'status', 'expected': 404}]}]}).json()
    original = c.post('/api/projects/1/runs', json={'case_ids': [case['id'], 2], 'environment_id': 1}).json()['id']
    assert c.post('/api/runs/' + original + '/rerun', json={}).status_code == 409
    Worker(settings).run_once()
    c.put('/api/projects/1/cases/' + str(case['id']), json={'name': '已修复', 'steps': [{'action': 'http', 'url': '/api/info', 'assertions': [{'source': 'status', 'expected': 200}]}]})
    rerun = c.post('/api/runs/' + original + '/rerun', json={}).json()['id']
    snap = settings.unseal(db.one('SELECT snapshot FROM runs WHERE id=?', (rerun,))['snapshot'])
    assert len(snap['cases']) == 1 and snap['cases'][0]['revision'] == 1
    assert snap['cases'][0]['steps'][0]['url'] == '/api/info'
    Worker(settings).run_once()
    assert c.get('/api/runs/' + rerun).json()['status'] == 'failed'
    latest = c.post('/api/runs/' + original + '/rerun?use_latest=true', json={}).json()['id']
    Worker(settings).run_once()
    result = c.get('/api/runs/' + latest).json()
    assert result['status'] == 'passed' and result['case_revisions'][0]['revision'] == 2


def test_history_filters_pagination_and_new_feature_permissions(platform):
    c, db, settings, api = platform
    for i in range(23):
        c.post('/api/projects/1/runs', json={'suite_id': 1, 'environment_id': 1})
    # Historical rows beyond the legacy 200-item window must remain queryable.
    with db.transaction() as conn:
        original = dict(conn.execute('SELECT * FROM runs LIMIT 1').fetchone())
        columns = list(original)
        for i in range(200):
            row = {**original, 'id': f'historical-{i:04d}', 'created_at': original['created_at'] - 100 - i}
            conn.execute('INSERT INTO runs(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(row[k] for k in columns))
    first = c.get('/api/projects/1/runs/search?page_size=10').json()
    second = c.get('/api/projects/1/runs/search?page_size=10&page=2').json()
    assert first['total'] == 223 and len(second['items']) == 10
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    assert c.get('/api/projects/1/runs/search?status=passed').json()['total'] == 0
    assert len(c.get('/api/projects/1/runs').json()) == 200
    assert c.get('/api/projects/1/runs/search?q=historical-0199').json()['total'] == 1
    assert c.get('/api/projects/1/runs/search?environment=开发演示&trigger=manual&q=核心&since=0').json()['total'] == 223
    assert c.get('/api/projects/1/runs/search?since=2&until=1').status_code == 400
    viewer = c.post('/api/users', json={'username': 'reader', 'display_name': '只读', 'password': 'reader-password'}).json()['id']
    c.put('/api/projects/1/members', json={'user_id': viewer, 'role': 'viewer'})
    c.post('/api/auth/logout'); c.post('/api/auth/login', json={'username': 'reader', 'password': 'reader-password'})
    for endpoint, body in [('assets/import', {'bundle': {'format': 'testdock-assets-v1'}}),
                           ('openapi/preview', {'document': {}}), ('runs/matrix', {'environment_ids': [1], 'suite_id': 1})]:
        assert c.post('/api/projects/1/' + endpoint, json=body).status_code == 403
    assert c.get('/api/projects/1/assets/export').status_code == 403


def test_actual_request_size_and_decompressed_response_limit(platform, tmp_path):
    c, db, settings, api = platform
    assert c.post('/api/me/password', json={'old_password': 123, 'new_password': 'valid-password'}).status_code == 422
    assert c.patch('/api/users/1', json={'active': 'false'}).status_code == 400
    response = c.post('/api/projects', content=iter([b' ' * (1024 * 1024)] * 3), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    target = FastAPI()

    @target.get('/large')
    def large():
        return Response(gzip.compress(b'x' * (6 * 1024 * 1024)), headers={'Content-Encoding': 'gzip'})

    @target.get('/gzip')
    def compressed_json():
        return Response(gzip.compress(b'{"ready":true}'), headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'})

    with serve(target) as base:
        result = execute_snapshot({'environment': {'web_base_url': '', 'api_base_url': base, 'variables': {}, 'secrets': {}},
            'cases': [{'id': 1, 'name': '大响应', 'steps': [{'action': 'http', 'url': '/large'}]}]}, tmp_path / 'run')
        assert result['status'] == 'error'
        assert '5MiB' in result['results'][0]['steps'][0]['error']
        result = execute_snapshot({'environment': {'web_base_url': '', 'api_base_url': base, 'variables': {}, 'secrets': {}},
            'cases': [{'id': 1, 'name': '正常压缩JSON', 'steps': [{'action': 'http', 'url': '/gzip',
                'assertions': [{'source': 'json', 'expression': 'ready', 'operator': 'eq', 'expected': True},
                               {'source': 'header', 'expression': 'content-encoding', 'operator': 'eq', 'expected': 'gzip'}]}]}]}, tmp_path / 'gzip')
        assert result['status'] == 'passed'
