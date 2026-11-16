from __future__ import annotations

import json
import time

from fastapi import Body, Query, Request

from .billing import approve_order, ledger_check, new_bucket, refund_order, settle
from .core import audit, create_user, dumps, password_hash, uid
from .models import GrantInput, Limits, ModelInput, PlanInput, PoolInput, Receipt, ReconcileInput, RefundInput, TenantAdminUpdate, UpstreamInput, UserInput
from .routing import validate_upstream
from .security import admin, fail, public_user


def attach_admin(api, db):
    from .app import page_query

    def pool_check(c, pk, *, api_only=False):
        row = c.execute('SELECT * FROM pools WHERE id=?', (pk,)).fetchone()
        if not row:
            fail(404, 'pool_not_found', '资源池不存在')
        if api_only and row['kind'] != 'api':
            fail(409, 'legacy_pool_retired', '旧账号接入已移除，请使用GPT账号池')
        if row['approval_status'] != 'ready':
            fail(409, 'pool_pending', '旧资源已停用，请使用GPT账号池')
        return row

    @api.get('/api/v1/admin/overview')
    def overview(request: Request):
        admin(db, request)
        return {'users': db.one('SELECT count(*) n FROM users')['n'],
                'tenants': db.one('SELECT count(*) n FROM tenants')['n'],
                'requests': db.one('SELECT count(*) n FROM requests')['n'],
                'running': db.one("SELECT count(*) n FROM requests WHERE status='running'")['n'],
                'unknown': db.one("SELECT count(*) n FROM requests WHERE status='unknown'")['n'],
                'pending_orders': db.one("SELECT count(*) n FROM orders WHERE status='pending'")['n'],
                'ledger_check': ledger_check(db), 'account_pool_configured': db.settings.account_pool_config is not None, 'online_payments': False,
                'max_users': db.settings.max_users, 'max_concurrency': db.settings.max_concurrency}

    @api.get('/api/v1/admin/users')
    def users(request: Request):
        admin(db, request)
        return [public_user(row) for row in db.all('SELECT * FROM users ORDER BY created_at')]

    @api.post('/api/v1/admin/users', status_code=201)
    def add_user(body: UserInput, request: Request):
        user = admin(db, request)
        with db.transaction() as c:
            if c.execute('SELECT count(*) FROM users').fetchone()[0] >= db.settings.max_users:
                fail(409, 'user_capacity', '已达到账号容量')
            user_id, tenant_id, app_id = create_user(c, body.username, body.display_name, body.password)
            audit(c, user['id'], tenant_id, 'operator.user_create', user_id)
        return {'id': user_id, 'tenant_id': tenant_id, 'application_id': app_id}

    @api.patch('/api/v1/admin/users/{pk}')
    def update_user(pk: str, request: Request, body: dict = Body(...)):
        user = admin(db, request)
        if set(body) - {'active', 'password'} or ('active' in body and type(body['active']) is not bool):
            fail(400, 'invalid_request', '仅支持active布尔值或重置password')
        if 'password' in body and (not isinstance(body['password'], str) or not 10 <= len(body['password']) <= 256):
            fail(400, 'invalid_password', '密码需10至256位')
        with db.transaction() as c:
            old = c.execute('SELECT * FROM users WHERE id=?', (pk,)).fetchone()
            if not old:
                fail(404, 'user_not_found', '账号不存在')
            active = body.get('active', bool(old['active']))
            if not active and old['is_admin'] and c.execute('SELECT count(*) FROM users WHERE is_admin=1 AND active=1').fetchone()[0] <= 1:
                fail(409, 'last_admin', '需保留有效运营管理员')
            hashed = password_hash(body['password']) if 'password' in body else old['password_hash']
            c.execute('UPDATE users SET active=?,password_hash=? WHERE id=?', (active, hashed, pk))
            if not active or 'password' in body:
                c.execute('DELETE FROM sessions WHERE user_id=?', (pk,))
                c.execute('UPDATE api_keys SET active=0 WHERE user_id=?', (pk,))
            audit(c, user['id'], None, 'operator.user_update', pk)
        return {'ok': True}

    @api.get('/api/v1/admin/tenants')
    def tenants(request: Request):
        admin(db, request)
        return db.all('SELECT * FROM tenants ORDER BY created_at')

    @api.patch('/api/v1/admin/tenants/{pk}')
    def update_tenant(pk: str, body: TenantAdminUpdate, request: Request):
        user = admin(db, request)
        values = body.model_dump(exclude_none=True)
        with db.transaction() as c:
            if not c.execute('SELECT 1 FROM tenants WHERE id=?', (pk,)).fetchone():
                fail(404, 'tenant_not_found', '空间不存在')
            if body.active is False and c.execute('SELECT 1 FROM members WHERE tenant_id=? AND user_id=?', (pk, user['id'])).fetchone():
                if c.execute('SELECT count(*) FROM members m JOIN tenants t ON t.id=m.tenant_id WHERE m.user_id=? AND t.active=1', (user['id'],)).fetchone()[0] <= 1:
                    fail(409, 'operator_workspace_required', '请为当前运营管理员保留一个有效空间')
            c.execute('UPDATE tenants SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), pk))
            audit(c, user['id'], pk, 'operator.tenant_update', pk)
        return {'ok': True}

    @api.get('/api/v1/admin/pools')
    def pools(request: Request):
        admin(db, request)
        return db.all('SELECT * FROM pools ORDER BY created_at')

    def save_pool(body, user, pk=None):
        with db.transaction() as c:
            if body.tenant_id and not c.execute('SELECT 1 FROM tenants WHERE id=?', (body.tenant_id,)).fetchone():
                fail(400, 'invalid_tenant', '目标空间不存在')
            values = body.model_dump()
            values['approval_status'] = 'ready'
            if pk:
                old = c.execute('SELECT * FROM pools WHERE id=?', (pk,)).fetchone()
                if not old:
                    fail(404, 'pool_not_found', '资源池不存在')
                if old['kind'] != body.kind:
                    fail(409, 'immutable_pool_kind', '资源池类型创建后不可修改')
                c.execute('UPDATE pools SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), pk))
            else:
                pk = uid()
                values = {'id': pk, **values, 'created_at': time.time()}
                c.execute('INSERT INTO pools(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            audit(c, user['id'], None, 'pool.save', pk)
        return {'id': pk}

    @api.post('/api/v1/admin/pools', status_code=201)
    def add_pool(body: PoolInput, request: Request):
        return save_pool(body, admin(db, request))

    @api.put('/api/v1/admin/pools/{pk}')
    def update_pool(pk: str, body: PoolInput, request: Request):
        return save_pool(body, admin(db, request), pk)

    @api.get('/api/v1/admin/upstreams')
    def upstreams(request: Request):
        admin(db, request)
        rows = db.all('SELECT id,pool_id,name,provider,base_url,enabled,weight,concurrency,revision,created_at,secret FROM upstreams ORDER BY created_at')
        for row in rows:
            row['credential_configured'] = bool(db.settings.unseal(row.pop('secret')).get('api_key'))
            from .diagnostics import runtime_summary
            with db.connect() as c:
                row['runtime'] = runtime_summary(c, row, time.time())
        return rows

    def save_upstream(body, user, pk=None):
        validate_upstream(db.settings, body.provider, body.base_url, resolve=True)
        with db.transaction() as c:
            pool_check(c, body.pool_id, api_only=True)
            old = c.execute('SELECT * FROM upstreams WHERE id=?', (pk,)).fetchone() if pk else None
            if pk and not old:
                fail(404, 'upstream_not_found', '上游不存在')
            if old and old['provider']=='account_pool':
                fail(409,'account_pool_managed','本机账号池连接由独立服务配置管理，请使用GPT账号池页面')
            value = body.api_key or (db.settings.unseal(old['secret'])['api_key'] if old else '')
            if body.enabled and not value:
                fail(400, 'credential_required', '启用前需填写API凭据')
            values = body.model_dump(exclude={'api_key'})
            values['secret'] = db.settings.seal({'api_key': value})
            values['revision'] = old['revision'] + 1 if old else 1
            if pk:
                c.execute('UPDATE upstreams SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), pk))
                if any(old[k]!=values[k] for k in ('provider','base_url','pool_id')):
                    c.execute('UPDATE model_media SET vision=0,verified=0 WHERE model_id IN (SELECT id FROM models WHERE pool_id IN (?,?))',(old['pool_id'],values['pool_id']))
            else:
                pk = uid()
                values = {'id': pk, **values, 'created_at': time.time()}
                c.execute('INSERT INTO upstreams(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            audit(c, user['id'], None, 'upstream.save', pk)
            from .operations import ensure_runtime
            ensure_runtime(c, c.execute('SELECT * FROM upstreams WHERE id=?', (pk,)).fetchone())
        return {'id': pk}

    @api.post('/api/v1/admin/upstreams', status_code=201)
    def add_upstream(body: UpstreamInput, request: Request):
        return save_upstream(body, admin(db, request))

    @api.put('/api/v1/admin/upstreams/{pk}')
    def update_upstream(pk: str, body: UpstreamInput, request: Request):
        return save_upstream(body, admin(db, request), pk)

    @api.get('/api/v1/admin/models')
    def models(request: Request):
        admin(db, request)
        rows = db.all('SELECT * FROM models ORDER BY alias')
        for row in rows:
            row['protocols'] = json.loads(row['protocols'])
        return rows

    def save_model(body, user, pk=None):
        with db.transaction() as c:
            pool = pool_check(c, body.pool_id)
            managed=c.execute("SELECT 1 FROM upstreams WHERE pool_id=? AND provider='account_pool'",(body.pool_id,)).fetchone()
            if bool(managed)!=(body.provider=='account_pool'):
                fail(400,'account_pool_model_provider','本机GPT账号池的模型必须选择对应的账号池提供商')
            values = body.model_dump()
            values['protocols'] = dumps(body.protocols)
            old = c.execute('SELECT * FROM models WHERE id=?', (pk,)).fetchone() if pk else None
            if pk and not old:
                fail(404, 'model_not_found', '模型路由不存在')
            version = old['price_version'] if old else 1
            if old and any(old[k] != values[k] for k in ('input_rate','cache_rate','output_rate')):
                version += 1
            values['price_version'] = version
            if pk:
                c.execute('UPDATE models SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), pk))
                if any(old[k]!=values[k] for k in ('provider','upstream_model','pool_id')):
                    c.execute('UPDATE model_media SET vision=0,verified=0 WHERE model_id=?',(pk,))
            else:
                pk = uid()
                values = {'id': pk, **values, 'created_at': time.time()}
                c.execute('INSERT INTO models(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            audit(c, user['id'], None, 'model.save', pk)
        return {'id': pk}

    @api.post('/api/v1/admin/models', status_code=201)
    def add_model(body: ModelInput, request: Request):
        return save_model(body, admin(db, request))

    @api.put('/api/v1/admin/models/{pk}')
    def update_model(pk: str, body: ModelInput, request: Request):
        return save_model(body, admin(db, request), pk)

    @api.get('/api/v1/admin/plans')
    def plans(request: Request):
        admin(db, request)
        rows = db.all('SELECT * FROM plans ORDER BY created_at')
        for row in rows:
            row['models'], row['limits'] = json.loads(row['models']), json.loads(row['limits'])
        return rows

    def save_plan(body, user, pk=None):
        with db.transaction() as c:
            if body.published:
                for alias in body.models:
                    if alias != '*' and not c.execute('SELECT 1 FROM models WHERE alias=? AND enabled=1', (alias,)).fetchone():
                        fail(400, 'invalid_plan_model', '发布套餐需选择已启用的模型')
                if not c.execute('SELECT 1 FROM models WHERE enabled=1').fetchone():
                    fail(400, 'pricing_missing', '发布套餐前需启用已定价的模型')
            values = body.model_dump()
            values['models'], values['limits'] = dumps(body.models), dumps(body.limits.model_dump(exclude_none=True))
            old = c.execute('SELECT * FROM plans WHERE id=?', (pk,)).fetchone() if pk else None
            if pk and not old:
                fail(404, 'plan_not_found', '套餐不存在')
            values['version'] = old['version'] + 1 if old else 1
            if pk:
                c.execute('UPDATE plans SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), pk))
            else:
                pk = uid()
                values = {'id': pk, **values, 'created_at': time.time()}
                c.execute('INSERT INTO plans(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            audit(c, user['id'], None, 'plan.save', pk)
        return {'id': pk}

    @api.post('/api/v1/admin/plans', status_code=201)
    def add_plan(body: PlanInput, request: Request):
        return save_plan(body, admin(db, request))

    @api.put('/api/v1/admin/plans/{pk}')
    def update_plan(pk: str, body: PlanInput, request: Request):
        return save_plan(body, admin(db, request), pk)

    @api.get('/api/v1/admin/policies')
    def policies(request: Request):
        admin(db, request)
        return [{**row, 'limits': json.loads(row['limits'])} for row in db.all('SELECT * FROM policies ORDER BY scope,scope_id')]

    @api.put('/api/v1/admin/policies/{scope}/{pk}')
    def policy(scope: str, pk: str, body: Limits, request: Request):
        user = admin(db, request)
        tables = {'tenant': 'tenants', 'user': 'users', 'application': 'applications', 'key': 'api_keys'}
        with db.transaction() as c:
            if not ((scope == 'platform' and pk == 'global') or (scope in tables and c.execute('SELECT 1 FROM ' + tables[scope] + ' WHERE id=?', (pk,)).fetchone())):
                fail(400, 'invalid_policy_scope', '配额目标不存在')
            c.execute('INSERT INTO policies VALUES(?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET limits=excluded.limits',
                      (scope, pk, dumps(body.model_dump(exclude_none=True))))
            audit(c, user['id'], None, 'operator.policy_update', scope + ':' + pk)
        return {'ok': True}

    @api.get('/api/v1/admin/orders')
    def orders(request: Request, page: int = Query(1, ge=1), status: str | None = None):
        admin(db, request)
        sql = 'SELECT o.*,t.name tenant_name FROM orders o JOIN tenants t ON t.id=o.tenant_id'
        args = ()
        if status:
            sql += ' WHERE o.status=?'
            args = (status,)
        return page_query(db, sql + ' ORDER BY o.created_at DESC', args, page)

    @api.post('/api/v1/admin/orders/{pk}/approve')
    def approve(pk: str, body: Receipt, request: Request):
        return approve_order(db, pk, admin(db, request)['id'], body.reference)

    @api.post('/api/v1/admin/orders/{pk}/refund')
    def refund(pk: str, body: RefundInput, request: Request):
        return refund_order(db, pk, admin(db, request)['id'], body)

    @api.post('/api/v1/admin/grants', status_code=201)
    def grant(body: GrantInput, request: Request):
        user = admin(db, request)
        with db.transaction() as c:
            previous = c.execute("SELECT * FROM buckets WHERE kind='grant' AND source_id=?", (body.client_ref,)).fetchone()
            if previous:
                if previous['tenant_id'] != body.tenant_id or previous['original'] != body.amount_micro:
                    fail(409, 'idempotency_conflict', '赠送编号对应不同请求')
                return {'id': previous['id'], 'already_granted': True}
            if not c.execute('SELECT 1 FROM tenants WHERE id=? AND active=1', (body.tenant_id,)).fetchone():
                fail(404, 'tenant_not_found', '空间不存在或已停用')
            pk = new_bucket(c, body.tenant_id, 'grant', body.client_ref, body.amount_micro, actor=user['id'],
                            expires=time.time() + body.days * 86400, note=body.note)
            audit(c, user['id'], body.tenant_id, 'grant.create', pk, body.note)
        return {'id': pk}

    @api.get('/api/v1/admin/requests')
    def requests(request: Request, page: int = Query(1, ge=1), status: str | None = None):
        admin(db, request)
        sql = "SELECT r.id,r.tenant_id,r.user_id,r.application_id,r.key_id,COALESCE(json_extract(r.price,'$.model_alias'),m.alias) model,r.pool_id,r.upstream_id,r.endpoint,r.status,r.input_tokens,r.output_tokens,r.cached_tokens,r.charged_micro,r.computed_micro,r.reserved_micro,r.error_code,r.provider_id,r.created_at,r.finished_at FROM requests r JOIN models m ON m.id=r.model_id"
        args = ()
        if status:
            sql += ' WHERE r.status=?'
            args = (status,)
        return page_query(db, sql + ' ORDER BY r.created_at DESC', args, page)

    @api.post('/api/v1/admin/requests/{pk}/reconcile')
    def reconcile(pk: str, body: ReconcileInput, request: Request):
        user = admin(db, request)
        row = db.one('SELECT * FROM requests WHERE id=?', (pk,))
        if not row or row['status'] != 'unknown':
            fail(409, 'request_state', '仅待对账请求可补充供应商用量')
        if body.cached_tokens > body.input_tokens:
            fail(400, 'invalid_usage', '缓存输入不能超过总输入')
        done = settle(db, pk, (body.input_tokens, body.output_tokens, body.cached_tokens),
                      status='succeeded', actor=user['id'], note=body.reference)
        if not done:
            fail(409, 'request_state', '请求已结算或释放')
        return {'ok': True}

    @api.get('/api/v1/admin/audit')
    def audit_log(request: Request, page: int = Query(1, ge=1)):
        admin(db, request)
        return page_query(db, 'SELECT * FROM audit ORDER BY id DESC', page=page)

    from .diagnostics import attach_diagnostics
    attach_diagnostics(api, db)
