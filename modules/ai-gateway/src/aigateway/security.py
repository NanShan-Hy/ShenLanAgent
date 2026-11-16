from __future__ import annotations

import ipaddress
import json
import time

from .core import digest


class GatewayError(Exception):
    def __init__(self, status, code, message, request_id=None):
        self.status, self.code, self.message, self.request_id = status, code, message, request_id
        super().__init__(message)


def fail(status, code, message):
    raise GatewayError(status, code, message)


def actor(db, request):
    row = db.one('''SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                   WHERE s.hash=? AND s.expires_at>? AND u.active=1''',
                 (digest(request.cookies.get('gateway_session', '')), time.time()))
    if not row:
        fail(401, 'authentication_required', '请先登录')
    return row


def admin(db, request):
    user = actor(db, request)
    if not user['is_admin']:
        fail(403, 'admin_required', '需要运营管理员权限')
    return user


def tenant_access(c, user_id, tenant_id, required='member'):
    row = c.execute('''SELECT t.*,m.role FROM tenants t JOIN members m ON m.tenant_id=t.id
                       JOIN users u ON u.id=m.user_id WHERE t.id=? AND m.user_id=? AND t.active=1 AND u.active=1''',
                    (tenant_id, user_id)).fetchone()
    if not row or {'member': 0, 'admin': 1, 'owner': 2}.get(row['role'], -1) < {'member': 0, 'admin': 1, 'owner': 2}[required]:
        fail(403, 'tenant_forbidden', '没有此空间的操作权限')
    return dict(row)


def public_user(user):
    return {k: user[k] for k in ('id', 'username', 'display_name', 'is_admin', 'active')}


def key_from_token(c, settings, token, address):
    if not token.startswith('sk-gw-') or len(token) > 200:
        fail(401, 'invalid_api_key', '平台Key无效或已撤销')
    row = c.execute('''SELECT k.* FROM api_keys k JOIN users u ON u.id=k.user_id
      JOIN tenants t ON t.id=k.tenant_id JOIN members m ON m.user_id=k.user_id AND m.tenant_id=k.tenant_id
      JOIN applications a ON a.id=k.application_id AND a.tenant_id=k.tenant_id
      WHERE k.hash=? AND k.active=1 AND u.active=1 AND t.active=1 AND a.active=1
      AND (k.expires_at IS NULL OR k.expires_at>?)''', (settings.key_hash(token), time.time())).fetchone()
    if not row:
        fail(401, 'invalid_api_key', '平台Key无效或已撤销')
    result = dict(row)
    if json.loads(result['ips']):
        try:
            ip = ipaddress.ip_address(address)
            allowed = any(ip in ipaddress.ip_network(item, strict=False) for item in json.loads(result['ips']))
        except ValueError:
            allowed = False
        if not allowed:
            fail(403, 'ip_forbidden', '请求IP不在此Key的允许范围')
    return result


def bearer(request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        fail(401, 'invalid_api_key', '请使用Bearer平台Key')
    return auth[7:]


def model_allowed(c, key, model):
    selected = json.loads(key['models'])
    if '*' not in selected and model['alias'] not in selected:
        fail(403, 'model_forbidden', '此Key没有该模型的使用权限')
    pool = c.execute('SELECT * FROM pools WHERE id=?', (model['pool_id'],)).fetchone()
    if not pool or not pool['enabled'] or pool['approval_status'] != 'ready':
        fail(503, 'pool_unavailable', '资源池未启用或尚未完成接入')
    if pool['visibility'] == 'tenant' and pool['tenant_id'] != key['tenant_id']:
        fail(403, 'model_forbidden', '此空间没有该资源池的使用权限')
    if pool['kind'] != 'api':
        fail(410, 'legacy_pool_retired', '旧账号接入已移除，请使用GPT账号池')
    return dict(pool)
