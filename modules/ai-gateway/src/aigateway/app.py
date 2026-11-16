from __future__ import annotations

import asyncio
import fcntl
import json
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .billing import maintenance
from .core import Database, Settings, audit, create_user, digest, dumps, password_hash, password_matches, uid
from .models import ApplicationInput, BillingInput, KeyInput, Limits, Login, TenantInput, UserInput
from .relay import perform
from .http_pool import close_clients
from .migrations import SCHEMA_VERSION
from .security import GatewayError, actor, admin, bearer, fail, key_from_token, model_allowed, public_user, tenant_access

STATIC = Path(__file__).parent / 'static'


def page_query(db, sql, args=(), page=1):
    return {'items': db.all(sql + ' LIMIT 20 OFFSET ?', (*args, (page - 1) * 20)), 'page': page,
            'total': db.one('SELECT count(*) n FROM (' + sql + ')', args)['n'], 'page_size': 20}


def create_app(settings=None, *, upstream_transport=None):
    settings = settings or Settings.load()
    db = Database(settings)
    db.initialize()

    @asynccontextmanager
    async def lifespan(api):
        from .token_budget import warm_tokenizers
        warm_tokenizers()
        lock = (settings.data_dir / 'instance.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError('同一数据库仅允许一个网关实例')
        (settings.data_dir / 'instance.lock').chmod(0o600)
        maintenance(db, restart=True)
        from .request_audit import recover
        recover(db)
        from .conversations import recover as recover_conversations
        recover_conversations(db)
        api.state.maintenance_last = time.time()
        api.state.maintenance_error = False
        async def tick():
            while True:
                await asyncio.sleep(5)
                try:
                    maintenance(db)
                    api.state.maintenance_last = time.time()
                    api.state.maintenance_error = False
                except sqlite3.Error:
                    api.state.maintenance_error = True
        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                maintenance(db, restart=True)
            finally:
                try:
                    await close_clients(api)
                finally:
                    lock.close()

    api = FastAPI(title='AI Gateway', version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url='/api/openapi.json')
    api.state.db = db
    api.state.upstream_transport = upstream_transport
    api.state.http_clients = {}
    api.state.maintenance_last = 0
    api.state.maintenance_error = False

    @api.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        context = getattr(request.state, 'request_audit', None)
        if context:
            context.error(exc.code)
            if exc.request_id and not context.request_id:
                context.link(exc.request_id, False)
        headers={'X-Request-Id':exc.request_id} if exc.request_id else {}
        turn=getattr(request.state,'managed_turn',None)
        if turn:headers.update(turn.headers())
        return JSONResponse({'error': {'type': 'gateway_error', 'code': exc.code, 'message': exc.message,
                                      'request_id': exc.request_id}, 'detail': exc.message}, status_code=exc.status,
                            headers=headers)

    @api.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        detail = '; '.join('.'.join(map(str, e['loc'])) + ': ' + e['msg'] for e in exc.errors())
        return JSONResponse({'detail': detail, 'error': {'code': 'invalid_request', 'message': detail}}, status_code=422)

    @api.exception_handler(sqlite3.IntegrityError)
    async def integrity(request, exc):
        return JSONResponse({'detail': '名称冲突或资源状态不允许此操作', 'error': {'code': 'resource_conflict', 'message': '名称冲突或资源状态不允许此操作'}}, status_code=409)

    @api.middleware('http')
    async def guard(request, call_next):
        if request.url.path.startswith(('/api/', '/v1/')) and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.url.path.startswith('/api/') and not (request.url.path.startswith('/api/v1/conversations') and request.headers.get('authorization','').startswith('Bearer ')):
                origin = request.headers.get('origin')
                if (request.headers.get('x-requested-with') != 'AIGateway'
                        or (origin and urlparse(origin).netloc != request.headers.get('host'))):
                    return JSONResponse({'detail': '请求来源校验失败'}, status_code=403)
            if request.headers.get('content-encoding', 'identity') != 'identity':
                context = getattr(request.state, 'request_audit', None)
                if context:context.error('unsupported_content_encoding')
                return JSONResponse({'detail': '请发送未压缩JSON请求'}, status_code=415)
            body = bytearray()
            if request.url.path=='/v1/files' and request.method=='POST':
                try:content_length=int(request.headers.get('content-length','0') or 0)
                except ValueError:return JSONResponse({'detail':'Content-Length无效'},status_code=400)
                if content_length>21*1024**2:return JSONResponse({'detail':'文件上传超过20MiB'},status_code=413)
                original_receive=request._receive
                received=0
                async def bounded_receive():
                    nonlocal received
                    message=await original_receive();received+=len(message.get('body',b''))
                    if received>21*1024**2:raise GatewayError(413,'file_size_limit','文件上传超过20MiB')
                    return message
                request._receive=bounded_receive
            else:
              async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4 * 1024**2:
                    context = getattr(request.state, 'request_audit', None)
                    if context:context.error('request_too_large')
                    return JSONResponse({'detail': '请求超过4MiB'}, status_code=413)
              request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    def access(request, tenant_id=None, required='member'):
        user = actor(db, request)
        if not tenant_id:
            first = db.one('''SELECT m.tenant_id FROM members m JOIN tenants t ON t.id=m.tenant_id
                              WHERE m.user_id=? AND t.active=1 ORDER BY t.created_at LIMIT 1''', (user['id'],))
            if not first:
                fail(403, 'tenant_forbidden', '当前没有有效空间')
            tenant_id = first['tenant_id']
        with db.connect() as c:
            tenant = tenant_access(c, user['id'], tenant_id, required)
        return user, tenant

    api.state.access = access

    @api.get('/api/v1/health')
    def health():
        maintenance_online = time.time() - api.state.maintenance_last < 20 and not api.state.maintenance_error
        try:
            database_online = db.one('SELECT version FROM schema_version')['version'] == SCHEMA_VERSION
        except sqlite3.Error:
            database_online = False
        return {'status': 'ok' if maintenance_online and database_online else 'degraded', 'version': __version__,
                'service': 'ai-gateway', 'max_concurrency': settings.max_concurrency,
                'maintenance_online': maintenance_online, 'database_online': database_online}

    @api.get('/api/docs', include_in_schema=False)
    def docs():
        return RedirectResponse('/#docs')

    @api.post('/api/v1/auth/login')
    def login(body: Login, request: Request):
        address, now = request.client.host if request.client else 'unknown', time.time()
        attempt = db.one('SELECT * FROM login_attempts WHERE address=?', (address,))
        if attempt and attempt['n'] >= 10 and now - attempt['since'] < 300:
            fail(429, 'login_rate_limit', '请5分钟后重试')
        user = db.one('SELECT * FROM users WHERE username=? AND active=1', (body.username,))
        if not user or not password_matches(body.password, user['password_hash']):
            with db.transaction() as c:
                old = c.execute('SELECT * FROM login_attempts WHERE address=?', (address,)).fetchone()
                count, since = (old['n'] + 1, old['since']) if old and now - old['since'] < 300 else (1, now)
                c.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)', (address, count, since))
            fail(401, 'invalid_login', '账号或密码错误')
        token = secrets.token_urlsafe(32)
        with db.transaction() as c:
            c.execute('DELETE FROM sessions WHERE hash=?', (digest(request.cookies.get('gateway_session','')),))
            c.execute('DELETE FROM login_attempts WHERE address=?', (address,))
            c.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), user['id'], now + 43200))
        response = JSONResponse(public_user(user))
        response.set_cookie('gateway_session', token, httponly=True, secure=settings.public_url.startswith('https:'),
                            samesite='strict', max_age=43200)
        return response

    @api.post('/api/v1/auth/logout')
    def logout(request: Request):
        with db.transaction() as c:
            c.execute('DELETE FROM sessions WHERE hash=?', (digest(request.cookies.get('gateway_session','')),))
        response = JSONResponse({'ok': True})
        response.delete_cookie('gateway_session')
        return response

    @api.get('/api/v1/auth/options')
    def options():
        return {'public_registration': settings.public_registration, 'invitation_supported': True,
                'base_url': settings.public_url.rstrip('/') + '/v1'}

    @api.post('/api/v1/auth/register', status_code=201)
    def register(body: UserInput):
        with db.transaction() as c:
            invite = c.execute('SELECT * FROM invitations WHERE hash=? AND used_by IS NULL AND expires_at>?',
                               (digest(body.invitation), time.time())).fetchone() if body.invitation else None
            if not settings.public_registration and not invite:
                fail(403, 'invitation_required', '当前采用邀请注册，请输入有效邀请码')
            if body.invitation and not invite:
                fail(403, 'invalid_invitation', '邀请码失效或已使用')
            if c.execute('SELECT count(*) FROM users').fetchone()[0] >= settings.max_users:
                fail(409, 'user_capacity', '已达到当前试运营账号容量')
            user_id, tenant_id, app_id = create_user(c, body.username, body.display_name, body.password)
            if invite:
                if not c.execute('SELECT 1 FROM tenants WHERE id=? AND active=1', (invite['tenant_id'],)).fetchone():
                    fail(403, 'invalid_invitation', '邀请空间已停用')
                c.execute('INSERT INTO members VALUES(?,?,?)', (invite['tenant_id'], user_id, invite['role']))
                c.execute('UPDATE invitations SET used_by=? WHERE hash=?', (user_id, invite['hash']))
            return {'id': user_id}

    @api.get('/api/v1/me')
    def me(request: Request):
        user = actor(db, request)
        tenants = db.all('SELECT t.*,m.role FROM tenants t JOIN members m ON m.tenant_id=t.id WHERE m.user_id=? AND t.active=1 ORDER BY t.created_at', (user['id'],))
        return {**public_user(user), 'tenants': tenants, 'base_url': settings.public_url.rstrip('/') + '/v1'}

    @api.post('/api/v1/auth/password')
    def change_password(request: Request, body: dict = Body(...)):
        user = actor(db, request)
        if set(body) != {'old_password', 'new_password'} or not isinstance(body['new_password'], str) or not 10 <= len(body['new_password']) <= 256:
            fail(400, 'invalid_password', '新密码需要10至256位')
        if not password_matches(body['old_password'], user['password_hash']):
            fail(403, 'invalid_password', '原密码不正确')
        with db.transaction() as c:
            c.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash(body['new_password']), user['id']))
            c.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            audit(c, user['id'], None, 'password.change', user['id'])
        return {'ok': True}

    @api.post('/api/v1/tenants', status_code=201)
    def add_tenant(body: TenantInput, request: Request):
        user, pk = actor(db, request), uid()
        with db.transaction() as c:
            c.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)', (pk, body.name, time.time()))
            c.execute('INSERT INTO members VALUES(?,?,?)', (pk, user['id'], 'owner'))
            c.execute('INSERT INTO applications VALUES(?,?,?,1,?)', (uid(), pk, '默认应用', time.time()))
            audit(c, user['id'], pk, 'tenant.create', pk)
        return {'id': pk}

    @api.put('/api/v1/tenants/{tenant_id}/billing')
    def tenant_billing(tenant_id: str, body: BillingInput, request: Request):
        user = actor(db, request)
        with db.transaction() as c:
            tenant = tenant_access(c, user['id'], tenant_id, 'owner')
            if body.billing_mode == 'internal' and not user['is_admin']:
                fail(403, 'admin_required', '内部额度模式由运营管理员配置')
            c.execute('UPDATE tenants SET billing_mode=?,wallet_overage=? WHERE id=?',
                      (body.billing_mode, body.wallet_overage, tenant_id))
            audit(c, user['id'], tenant_id, 'billing.mode', tenant_id, body.billing_mode)
        return {'ok': True}

    @api.get('/api/v1/members')
    def members(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        return db.all('SELECT u.id,u.username,u.display_name,u.active,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE m.tenant_id=? ORDER BY u.created_at', (tenant['id'],))

    @api.post('/api/v1/invitations')
    def invite(request: Request, body: dict = Body(...), tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'owner')
        role = body.get('role', 'member')
        if set(body) - {'role'} or role not in ('member', 'admin'):
            fail(400, 'invalid_role', '可邀请成员或空间管理员')
        token, expires = secrets.token_urlsafe(32), time.time() + 48 * 3600
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'owner')
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,NULL)', (digest(token), tenant['id'], role, user['id'], expires))
            audit(c, user['id'], tenant['id'], 'invitation.create')
        return {'invitation': token, 'expires_at': expires}

    @api.post('/api/v1/invitations/accept')
    def accept_invite(request: Request, body: dict = Body(...)):
        user = actor(db, request)
        token = body.get('invitation')
        if not isinstance(token, str) or len(token) > 200:
            fail(400, 'invalid_invitation', '邀请码无效')
        with db.transaction() as c:
            invitation = c.execute('SELECT i.* FROM invitations i JOIN tenants t ON t.id=i.tenant_id WHERE i.hash=? AND i.used_by IS NULL AND i.expires_at>? AND t.active=1', (digest(token), time.time())).fetchone()
            if not invitation:
                fail(403, 'invalid_invitation', '邀请码失效或已使用')
            c.execute('INSERT INTO members VALUES(?,?,?)', (invitation['tenant_id'], user['id'], invitation['role']))
            c.execute('UPDATE invitations SET used_by=? WHERE hash=?', (user['id'], invitation['hash']))
            audit(c, user['id'], invitation['tenant_id'], 'invitation.accept')
        return {'ok': True}

    @api.delete('/api/v1/members/{member_id}')
    def remove_member(member_id: str, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'owner')
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'owner')
            target = c.execute('SELECT * FROM members WHERE tenant_id=? AND user_id=?', (tenant['id'], member_id)).fetchone()
            if not target:
                fail(404, 'member_not_found', '成员不存在')
            if target['role'] == 'owner':
                fail(409, 'owner_required', '空间所有者不能移除')
            c.execute('DELETE FROM members WHERE tenant_id=? AND user_id=?', (tenant['id'], member_id))
            c.execute('UPDATE api_keys SET active=0 WHERE tenant_id=? AND user_id=?', (tenant['id'], member_id))
            audit(c, user['id'], tenant['id'], 'member.remove', member_id)
        return {'ok': True}

    @api.get('/api/v1/applications')
    def applications(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        rows = db.all('SELECT a.*,p.limits FROM applications a LEFT JOIN policies p ON p.scope=\'application\' AND p.scope_id=a.id WHERE a.tenant_id=? ORDER BY a.created_at', (tenant['id'],))
        for row in rows:
            row['limits'] = json.loads(row['limits']) if row['limits'] else {}
        return rows

    @api.post('/api/v1/applications', status_code=201)
    def add_application(body: ApplicationInput, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'admin')
        pk = uid()
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'admin')
            c.execute('INSERT INTO applications VALUES(?,?,?,?,?)', (pk, tenant['id'], body.name, body.active, time.time()))
            audit(c, user['id'], tenant['id'], 'application.create', pk)
        return {'id': pk}

    @api.put('/api/v1/applications/{pk}')
    def update_application(pk: str, body: ApplicationInput, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'admin')
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'admin')
            if not c.execute('SELECT 1 FROM applications WHERE id=? AND tenant_id=?', (pk, tenant['id'])).fetchone():
                fail(404, 'application_not_found', '应用不存在')
            c.execute('UPDATE applications SET name=?,active=? WHERE id=?', (body.name, body.active, pk))
            audit(c, user['id'], tenant['id'], 'application.update', pk)
        return {'ok': True}

    @api.get('/api/v1/keys')
    def keys(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        sql = 'SELECT k.id,k.name,k.application_id,k.user_id,k.prefix,k.suffix,k.active,k.expires_at,k.created_at,k.models,k.ips FROM api_keys k WHERE tenant_id=?'
        args = [tenant['id']]
        if tenant['role'] == 'member':
            sql += ' AND user_id=?'
            args.append(user['id'])
        rows = db.all(sql + ' ORDER BY created_at DESC', args)
        for row in rows:
            row['models'], row['ips'] = json.loads(row['models']), json.loads(row['ips'])
            policy = db.one("SELECT limits FROM policies WHERE scope='key' AND scope_id=?", (row['id'],))
            row['limits'] = json.loads(policy['limits']) if policy else {}
        return rows

    @api.post('/api/v1/keys', status_code=201)
    def add_key(body: KeyInput, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        pk, token = uid(), 'sk-gw-' + secrets.token_urlsafe(32)
        if body.expires_at is not None and body.expires_at <= time.time():
            fail(400, 'key_expired', '有效期必须晚于当前时间')
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'])
            if not c.execute('SELECT 1 FROM applications WHERE id=? AND tenant_id=? AND active=1', (body.application_id, tenant['id'])).fetchone():
                fail(404, 'application_not_found', '应用不存在或已停用')
            c.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)',
                      (pk, tenant['id'], user['id'], body.application_id, body.name, settings.key_hash(token),
                       token[:14], token[-4:], dumps(body.models), dumps(body.ips), body.expires_at, time.time()))
            c.execute('INSERT INTO policies VALUES(?,?,?)', ('key', pk, dumps(body.limits.model_dump(exclude_none=True))))
            audit(c, user['id'], tenant['id'], 'key.create', pk)
        return {'id': pk, 'key': token, 'base_url': settings.public_url.rstrip('/') + '/v1', 'shown_once': True}

    def key_permission(c, user, tenant, pk, *, owner_only=False):
        tenant_access(c, user['id'], tenant['id'])
        row = c.execute('SELECT * FROM api_keys WHERE id=? AND tenant_id=?', (pk, tenant['id'])).fetchone()
        if not row or (row['user_id'] != user['id'] and (owner_only or tenant['role'] == 'member')):
            fail(404, 'key_not_found', 'Key不存在或无操作权限')
        return row

    @api.put('/api/v1/keys/{pk}')
    def update_key(pk: str, body: KeyInput, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        with db.transaction() as c:
            row = key_permission(c, user, tenant, pk, owner_only=True)
            if not c.execute('SELECT 1 FROM applications WHERE id=? AND tenant_id=? AND active=1', (body.application_id, tenant['id'])).fetchone():
                fail(404, 'application_not_found', '应用不存在')
            c.execute('UPDATE api_keys SET name=?,application_id=?,models=?,ips=?,expires_at=? WHERE id=?',
                      (body.name, body.application_id, dumps(body.models), dumps(body.ips), body.expires_at, pk))
            c.execute("INSERT INTO policies VALUES('key',?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET limits=excluded.limits",
                      (pk, dumps(body.limits.model_dump(exclude_none=True))))
            audit(c, user['id'], tenant['id'], 'key.update', pk)
        return {'ok': True}

    @api.delete('/api/v1/keys/{pk}')
    def revoke_key(pk: str, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        with db.transaction() as c:
            key_permission(c, user, tenant, pk)
            c.execute('UPDATE api_keys SET active=0 WHERE id=?', (pk,))
            audit(c, user['id'], tenant['id'], 'key.revoke', pk)
        return {'ok': True}

    @api.post('/api/v1/keys/{pk}/rotate')
    def rotate_key(pk: str, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        token = 'sk-gw-' + secrets.token_urlsafe(32)
        with db.transaction() as c:
            row = key_permission(c, user, tenant, pk, owner_only=True)
            if not row['active'] or (row['expires_at'] is not None and row['expires_at'] <= time.time()):
                fail(409, 'key_inactive', '撤销或过期的Key请重新创建')
            c.execute('UPDATE api_keys SET hash=?,prefix=?,suffix=? WHERE id=?', (settings.key_hash(token), token[:14], token[-4:], pk))
            audit(c, user['id'], tenant['id'], 'key.rotate', pk)
        return {'id': pk, 'key': token, 'shown_once': True}

    @api.get('/api/v1/catalog')
    def catalog(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        rows = db.all('''SELECT m.* FROM models m JOIN pools p ON p.id=m.pool_id WHERE m.enabled=1 AND p.enabled=1 AND p.kind='api'
                       AND p.approval_status='ready' AND (p.visibility='platform' OR p.tenant_id=?) ORDER BY m.alias''', (tenant['id'],))
        return [{k: json.loads(r[k]) if k == 'protocols' else r[k] for k in ('id','alias','protocols','context_limit','max_output','input_rate','cache_rate','output_rate','price_version')} for r in rows]

    @api.get('/api/v1/usage')
    def usage(request: Request, tenant_id: str | None = None, page: int = Query(1, ge=1),
              application_id: str | None = None, status: str | None = None):
        user, tenant = access(request, tenant_id)
        predicate, args = 'r.tenant_id=?', [tenant['id']]
        if tenant['role'] == 'member':
            predicate += ' AND r.user_id=?'
            args.append(user['id'])
        if application_id:
            predicate += ' AND r.application_id=?'
            args.append(application_id)
        if status:
            predicate += ' AND r.status=?'
            args.append(status)
        sql = '''SELECT r.id,r.application_id,a.name application_name,r.user_id,r.key_id,COALESCE(json_extract(r.price,'$.model_alias'),m.alias) model,r.endpoint,
          r.status,r.input_tokens,r.output_tokens,r.cached_tokens,r.charged_micro,r.reserved_micro,r.error_code,
          r.created_at,r.finished_at,r.provider_id FROM requests r JOIN applications a ON a.id=r.application_id
          JOIN models m ON m.id=r.model_id WHERE ''' + predicate + ' ORDER BY r.created_at DESC'
        result = page_query(db, sql, args, page)
        result['summary'] = db.one('''SELECT count(*) requests,COALESCE(sum(input_tokens+output_tokens),0) tokens,
          COALESCE(sum(charged_micro),0) charged_micro,sum(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) succeeded,
          sum(CASE WHEN status='unknown' THEN 1 ELSE 0 END) unknown FROM requests r WHERE ''' + predicate, args)
        return result

    @api.get('/api/v1/policies')
    def policies(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        row = db.one("SELECT limits FROM policies WHERE scope='tenant' AND scope_id=?", (tenant['id'],))
        own = db.one("SELECT limits FROM policies WHERE scope='user' AND scope_id=?", (user['id'],))
        return {'tenant': json.loads(row['limits']) if row else {}, 'user': json.loads(own['limits']) if own else {},
                'global_concurrency': settings.max_concurrency}

    @api.put('/api/v1/policies/{scope}/{pk}')
    def policy(scope: str, pk: str, body: Limits, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'admin')
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'admin')
            if scope == 'tenant':
                valid = pk == tenant['id']
            elif scope == 'application':
                valid = c.execute('SELECT 1 FROM applications WHERE id=? AND tenant_id=?', (pk, tenant['id'])).fetchone()
            else:
                valid = False
            if not valid:
                fail(403, 'policy_forbidden', '没有此配额策略的管理权限')
            c.execute('INSERT INTO policies VALUES(?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET limits=excluded.limits',
                      (scope, pk, dumps(body.model_dump(exclude_none=True))))
            audit(c, user['id'], tenant['id'], 'policy.update', scope + ':' + pk)
        return {'ok': True}

    @api.get('/v1/models')
    def model_list(request: Request):
        with db.connect() as c:
            key = key_from_token(c, settings, bearer(request), request.client.host if request.client else '')
            rows = c.execute('SELECT * FROM models WHERE enabled=1 ORDER BY alias').fetchall()
            items = []
            for model in rows:
                try:
                    model_allowed(c, key, model)
                    items.append({'id': model['alias'], 'object': 'model', 'created': int(model['created_at']), 'owned_by': 'ai-gateway'})
                except GatewayError:
                    pass
        return {'object': 'list', 'data': items}

    @api.post('/v1/chat/completions')
    async def chat(request: Request):
        return await perform(request, 'chat', '/chat/completions')

    @api.post('/v1/responses')
    async def responses(request: Request):
        return await perform(request, 'responses', '/responses')

    @api.post('/v1/responses/compact')
    async def compact(request: Request):
        return await perform(request, 'compact', '/responses/compact')

    from .commerce_api import attach_commerce
    from .admin_api import attach_admin
    from .account_pool_api import attach_account_pool
    attach_commerce(api, db, access)
    attach_admin(api, db)
    attach_account_pool(api, db)
    from .request_audit_api import attach_request_audit
    from .request_audit import RequestAuditMiddleware
    attach_request_audit(api, db)
    from .conversation_api import attach_conversations
    from .files_api import attach_files
    attach_conversations(api,db)
    attach_files(api,db)
    api.add_middleware(RequestAuditMiddleware, db=db)
    api.mount('/static', StaticFiles(directory=STATIC), name='static')

    @api.get('/', include_in_schema=False)
    def index():
        return FileResponse(STATIC / 'index.html')

    return api
