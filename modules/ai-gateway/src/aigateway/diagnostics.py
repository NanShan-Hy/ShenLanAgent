"""Explicit model-catalog diagnostics. No generation, pricing inference, or automatic polling."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Request

from .core import audit, uid
from .http_pool import client_for
from .operations import ensure_runtime, group_limits, reset_runtime, retry_delay
from .routing import cooldown, validate_upstream
from .security import GatewayError, admin, fail

CATALOG_LIMIT = 1024**2
MODEL_LIMIT = 1000
PROBE_TIMEOUT = 10


def current_probe(c, upstream):
    row = c.execute('''SELECT id,status,error_code,http_status,model_count,created_at,finished_at,latency_ms
                       FROM upstream_probes WHERE upstream_id=? AND revision=? ORDER BY created_at DESC LIMIT 1''',
                    (upstream['id'], upstream['revision'])).fetchone()
    return dict(row) if row else None


def runtime_summary(c, upstream, now):
    runtime = c.execute('SELECT * FROM upstream_runtime WHERE upstream_id=? AND revision=?',
                        (upstream['id'], upstream['revision'])).fetchone()
    result = dict(runtime) if runtime else {'state':'closed','failures':0,'retry_at':0,
                'last_error':'','last_http_status':None,'last_latency_ms':None,'last_event_at':None,'last_success_at':None}
    for key in ('probe_id', 'probe_until', 'half_open_attempt', 'upstream_id', 'revision'):
        result.pop(key, None)
    result['last_probe'] = current_probe(c, upstream)
    result['attempts_24h'] = dict(c.execute('''SELECT count(*) total,
      COALESCE(sum(status='succeeded'),0) succeeded,COALESCE(sum(status IN ('failed','unknown')),0) failed,
      CAST(avg(latency_ms) AS INTEGER) avg_latency_ms FROM upstream_attempts
      WHERE upstream_id=? AND dispatched_at IS NOT NULL AND created_at>=?''', (upstream['id'], now - 86400)).fetchone())
    result['running'] = c.execute("SELECT count(*) FROM requests WHERE upstream_id=? AND status='running'", (upstream['id'],)).fetchone()[0]
    return result


def reserve_probe(db, pk, user_id):
    with db.transaction() as c:
        upstream = c.execute('SELECT * FROM upstreams WHERE id=?', (pk,)).fetchone()
        if not upstream:
            fail(404, 'upstream_not_found', '上游不存在')
        pool = c.execute("SELECT * FROM pools WHERE id=? AND kind='api' AND approval_status='ready'", (upstream['pool_id'],)).fetchone()
        if not pool:
            fail(409, 'pool_pending', '该资源尚未支持连接测试')
        try:
            if not db.settings.unseal(upstream['secret']).get('api_key'):
                fail(400, 'credential_required', '请先保存API凭据')
        except (InvalidToken, ValueError, KeyError):
            fail(409, 'credential_unavailable', '上游凭据无法解密，请核对主密钥与配置')
        now = time.time()
        state = ensure_runtime(c, upstream)
        if state['probe_id'] and state['probe_until'] > now:
            fail(409, 'probe_in_progress', '该上游已有连接测试进行中')
        latest = c.execute('SELECT max(created_at) FROM upstream_probes WHERE upstream_id=?', (pk,)).fetchone()[0]
        if latest is not None and latest > now - 10:
            fail(429, 'probe_rate_limit', '连接测试至少间隔10秒')
        if c.execute("SELECT count(*) FROM upstream_probes WHERE status='running' AND created_at>?", (now - 30,)).fetchone()[0] >= 2:
            fail(429, 'probe_capacity', '已有两个连接测试进行中，请稍后重试')
        group_limits(c, pool['quota_group'], now)
        probe_id = uid()
        c.execute('''INSERT INTO upstream_probes(id,upstream_id,revision,quota_group,actor_id,status,created_at)
                     VALUES(?,?,?,?,?,'running',?)''',
                  (probe_id, pk, upstream['revision'], pool['quota_group'], user_id, now))
        c.execute('UPDATE upstream_runtime SET probe_id=?,probe_until=? WHERE upstream_id=?',
                  (probe_id, now + 30, pk))
        audit(c, user_id, None, 'upstream.probe', pk)
        return dict(upstream), dict(pool), probe_id


def probe_authorized(db, upstream, user_id):
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM users WHERE id=? AND active=1 AND is_admin=1', (user_id,)).fetchone():
            fail(403, 'authorization_changed', '连接测试期间账号权限已变化')
        row = c.execute('SELECT revision,pool_id FROM upstreams WHERE id=?', (upstream['id'],)).fetchone()
        if not row or row['revision'] != upstream['revision'] or row['pool_id'] != upstream['pool_id']:
            fail(409, 'configuration_changed', '连接测试期间上游配置已变化，请重新测试')
        if not c.execute("SELECT 1 FROM pools WHERE id=? AND kind='api' AND approval_status='ready'", (row['pool_id'],)).fetchone():
            fail(409, 'configuration_changed', '连接测试期间资源池状态已变化')


def finish_probe(db, upstream, probe_id, user_id, *, status, code='', http_status=None, models=(), elapsed=0):
    with db.transaction() as c:
        # A late response cannot validate or publish an old credential revision.
        live = c.execute('''SELECT u.revision,u.pool_id,p.kind,p.approval_status FROM upstreams u
                            JOIN pools p ON p.id=u.pool_id WHERE u.id=?''', (upstream['id'],)).fetchone()
        active = c.execute('SELECT 1 FROM users WHERE id=? AND active=1 AND is_admin=1', (user_id,)).fetchone()
        if (not live or live['revision'] != upstream['revision'] or live['pool_id'] != upstream['pool_id']
                or live['kind'] != 'api' or live['approval_status'] != 'ready' or not active):
            status, code, models = 'superseded', 'configuration_or_authorization_changed', ()
        event = c.execute('SELECT status FROM upstream_probes WHERE id=?', (probe_id,)).fetchone()
        if not event or event['status'] != 'running':
            fail(409, 'probe_interrupted', '连接测试已中断，请重新测试')
        now = time.time()
        c.execute('''UPDATE upstream_probes SET status=?,error_code=?,http_status=?,model_count=?,
                     finished_at=?,latency_ms=? WHERE id=?''',
                  (status, code, http_status, len(models), now, max(0, int(elapsed * 1000)), probe_id))
        if status == 'ok':
            c.execute('DELETE FROM upstream_catalog WHERE upstream_id=? AND revision=?', (upstream['id'], upstream['revision']))
            c.executemany('INSERT INTO upstream_catalog VALUES(?,?,?,?)',
                          [(upstream['id'], upstream['revision'], model, now) for model in models])
        c.execute('UPDATE upstream_runtime SET probe_id=NULL,probe_until=0 WHERE upstream_id=? AND probe_id=?',
                  (upstream['id'], probe_id))
        result = dict(c.execute('SELECT id,status,error_code,http_status,model_count,created_at,finished_at,latency_ms FROM upstream_probes WHERE id=?',
                                (probe_id,)).fetchone())
        result['models'] = list(models) if status == 'ok' else []
        return result


def catalog_models(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('data'), list):
        raise ValueError('invalid catalog')
    if len(raw['data']) > MODEL_LIMIT:
        raise ValueError('too many models')
    result = set()
    for entry in raw['data']:
        model = entry.get('id') if isinstance(entry, dict) else None
        if (not isinstance(model, str) or not 1 <= len(model) <= 120
                or not model.isprintable() or any(char.isspace() for char in model)):
            raise ValueError('invalid model identifier')
        result.add(model)
    return sorted(result)


async def probe_upstream(api, pk, user_id):
    db = api.state.db
    upstream, pool, probe_id = reserve_probe(db, pk, user_id)
    response = None
    started = time.monotonic()
    status, code, models, http_status = 'failed', '', [], None
    permission_error = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            await asyncio.to_thread(validate_upstream, db.settings, upstream['provider'], upstream['base_url'], resolve=True)
            probe_authorized(db, upstream, user_id)
            token = db.settings.unseal(upstream['secret'])['api_key']
            client = client_for(api)
            outbound = client.build_request('GET', upstream['base_url'].rstrip('/') + '/models',
                       headers={'Authorization':'Bearer ' + token, 'Accept':'application/json'},
                       timeout=httpx.Timeout(PROBE_TIMEOUT, connect=5))
            response = await client.send(outbound, stream=True)
            http_status = response.status_code
            if http_status == 429:
                cooldown(db, pool['id'], retry_delay(response.headers.get('retry-after')), group=pool['quota_group'])
            if http_status != 200:
                code = 'catalog_http_' + str(http_status)
            else:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > CATALOG_LIMIT:
                        raise ValueError('oversized catalog')
                    chunks.append(chunk)
                models = catalog_models(json.loads(b''.join(chunks)))
                status = 'ok'
            probe_authorized(db, upstream, user_id)
    except GatewayError as error:
        code = error.code
        if error.status in (403, 409):
            permission_error = error
    except (httpx.ConnectError, httpx.ConnectTimeout):
        code = 'catalog_connection_failed'
    except (TimeoutError, httpx.TimeoutException):
        code = 'catalog_timeout'
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidToken):
        code = 'catalog_response_invalid'
    except asyncio.CancelledError:
        finish_probe(db, upstream, probe_id, user_id, status='cancelled', code='probe_cancelled',
                     elapsed=time.monotonic() - started)
        raise
    finally:
        if response:
            await response.aclose()
    result = finish_probe(db, upstream, probe_id, user_id, status=status, code=code,
                          http_status=http_status, models=models, elapsed=time.monotonic() - started)
    if permission_error:
        raise permission_error
    return result


def attach_diagnostics(api, db):
    @api.post('/api/v1/admin/upstreams/{pk}/probe')
    async def probe(pk: str, request: Request):
        user = admin(db, request)
        return await probe_upstream(api, pk, user['id'])

    @api.get('/api/v1/admin/upstreams/{pk}/catalog')
    def catalog(pk: str, request: Request):
        admin(db, request)
        with db.connect() as c:
            upstream = c.execute('SELECT * FROM upstreams WHERE id=?', (pk,)).fetchone()
            if not upstream:
                fail(404, 'upstream_not_found', '上游不存在')
            latest = current_probe(c, upstream)
            models = [r['model_id'] for r in c.execute('SELECT model_id FROM upstream_catalog WHERE upstream_id=? AND revision=? ORDER BY model_id',
                       (pk, upstream['revision']))] if latest and latest['status'] == 'ok' else []
            return {'upstream_id':pk,'revision':upstream['revision'],'last_probe':latest,'models':models}

    @api.post('/api/v1/admin/upstreams/{pk}/reset-circuit')
    def reset(pk: str, request: Request):
        user = admin(db, request)
        with db.transaction() as c:
            upstream = c.execute('SELECT * FROM upstreams WHERE id=?', (pk,)).fetchone()
            if not upstream:
                fail(404, 'upstream_not_found', '上游不存在')
            reset_runtime(c, upstream, user['id'])
        return {'ok':True,'group_cooldown_unchanged':True}

    @api.get('/api/v1/admin/pool-status')
    def pool_status(request: Request):
        admin(db, request)
        with db.connect() as c:
            now = time.time()
            upstreams = []
            for row in c.execute('SELECT id,name,pool_id,provider,revision,enabled,concurrency FROM upstreams ORDER BY created_at'):
                upstream = dict(row)
                upstream['runtime'] = runtime_summary(c, upstream, now)
                upstreams.append(upstream)
            pools = []
            for row in c.execute('SELECT * FROM pools ORDER BY created_at'):
                pool = dict(row)
                children = [u for u in upstreams if u['pool_id'] == pool['id']]
                group = c.execute('SELECT max(cooldown_until),min(concurrency),min(rpm),min(tpm) FROM pools WHERE quota_group=?',
                                  (pool['quota_group'],)).fetchone()
                pool.update(upstreams=len(children),enabled_upstreams=sum(bool(u['enabled']) for u in children),
                            running=sum(u['runtime']['running'] for u in children),
                            circuit_limited=sum(u['runtime']['state'] != 'closed' for u in children),
                            group_cooldown_until=group[0],group_concurrency=group[1],group_rpm=group[2],group_tpm=group[3])
                pools.append(pool)
        return {'generated_at':now,'window_hours':24,'pools':pools,'upstreams':upstreams}

    @api.get('/api/v1/admin/requests/{pk}/attempts')
    def attempts(pk: str, request: Request):
        admin(db, request)
        if not db.one('SELECT 1 FROM requests WHERE id=?', (pk,)):
            fail(404, 'request_not_found', '请求不存在')
        return db.all('''SELECT a.id,a.upstream_id,u.name upstream_name,a.revision,a.attempt_no,a.status,a.error_code,
            a.http_status,a.provider_id,a.created_at,a.dispatched_at,a.finished_at,a.latency_ms FROM upstream_attempts a
            JOIN upstreams u ON u.id=a.upstream_id WHERE request_id=? ORDER BY attempt_no''', (pk,))
