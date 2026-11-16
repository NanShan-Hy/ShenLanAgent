from __future__ import annotations

import ipaddress
import json
import socket
import shutil
import time
from urllib.parse import urlparse

from .billing import QUOTA_LOG, active_subscription, check_limits, cost, reserve_buckets
from .core import audit, digest, dumps, uid
from .operations import eligible, group_limits, lease_attempt
from .security import GatewayError, fail, key_from_token, model_allowed


def validate_upstream(settings, provider, url, *, resolve=False):
    if provider == 'account_pool':
        from .account_pool_config import load_connector
        try:
            config=load_connector(settings)
        except (OSError,ValueError,TypeError):
            fail(503,'account_pool_configuration','本机账号池配置不可用')
        if not config or url!=config['base_url']:
            fail(400,'invalid_account_pool_upstream','本机账号池仅允许已配置的固定回环地址')
        return
    parsed = urlparse(url)
    host = parsed.hostname
    if provider == 'openai' and (host != 'api.openai.com' or parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.path.rstrip('/') != '/v1'):
        fail(400, 'invalid_upstream', 'OpenAI提供商使用https://api.openai.com/v1')
    if provider == 'deepseek' and (host != 'api.deepseek.com' or parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.path.rstrip('/') not in ('', '/v1')):
        fail(400, 'invalid_upstream', 'DeepSeek提供商使用官方HTTPS API地址')
    if parsed.scheme != 'https' and not settings.allow_private_upstreams:
        fail(400, 'insecure_upstream', '生产上游必须使用HTTPS')
    if settings.allow_private_upstreams:
        return
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
        if host in ('localhost', 'metadata.google.internal') or host.endswith(('.local', '.localhost', '.internal')):
            fail(400, 'private_upstream', '不允许内网或元数据服务作为上游')
        if resolve:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)]
            except OSError:
                fail(503, 'upstream_dns_failed', '无法解析上游地址')
    if any(not address.is_global for address in addresses):
        fail(400, 'private_upstream', '不允许内网或元数据服务作为上游')


def validate_payload(body, protocol, model):
    if not isinstance(body, dict):
        fail(400, 'invalid_request', '需要JSON对象')
    common = {'model','stream','store','background','previous_response_id','conversation','tools','tool_choice',
              'parallel_tool_calls','temperature','top_p','metadata','user','safety_identifier','service_tier'}
    specific = ({'messages','max_tokens','max_completion_tokens','stream_options','stop','frequency_penalty',
                 'presence_penalty','logit_bias','logprobs','top_logprobs','n','response_format','reasoning_effort',
                 'verbosity','thinking','reasoning','seed'} if protocol == 'chat' else
                {'input','instructions','max_output_tokens','include','reasoning','text','client_metadata',
                 'prompt_cache_key','max_tool_calls','stream_options'})
    if set(body) - common - specific:
        fail(400, 'unsupported_parameter', '请求含首期尚未开放的参数，请使用文档中的文本与客户端工具接口')
    if body.get('store') not in (None, False) or body.get('background') not in (None, False):
        fail(400, 'unsupported_feature', '首期仅支持store:false及前台调用')
    if any(body.get(key) is not None for key in ('previous_response_id', 'conversation')):
        fail(400, 'unsupported_feature', '请发送完整上下文，不使用服务端会话ID')
    if body.get('service_tier') not in (None, 'auto', 'default') or body.get('n', 1) != 1:
        fail(400, 'unsupported_billing', '此资费仅支持默认服务等级和单份输出')
    stream = body.get('stream', False)
    if not isinstance(stream, bool) or (protocol == 'compact' and stream):
        fail(400, 'invalid_request', 'stream参数无效')
    if protocol == 'chat':
        content = body.get('messages')
        if not isinstance(content, list) or not content:
            fail(400, 'invalid_request', 'messages必须为非空数组')
    else:
        content = body.get('input')
        if not isinstance(content, (str, list)) or not content:
            fail(400, 'invalid_request', 'input必须为文本或非空数组')

    def text_only(value, *, input_item=False):
        if isinstance(value, list):
            for item in value:
                text_only(item)
        elif isinstance(value, dict):
            kind = value.get('type', '')
            if kind == 'additional_tools':
                # Codex Responses Lite sends client declarations as ordered input
                # items instead of top-level tools. Preserve their original scope.
                if not input_item or protocol == 'chat':
                    fail(400, 'invalid_additional_tools', 'additional_tools必须是Responses的独立input项')
                if value.get('role') not in ('developer', 'system'):
                    fail(400, 'invalid_additional_tools', 'additional_tools需要developer或system角色')
                local_tools(value.get('tools'))
                # JSON Schema property names (e.g. file_id/image_url) are not uploads.
                return
            if kind in ('tool_search_call', 'tool_search_output'):
                if (not input_item or protocol == 'chat' or value.get('execution') != 'client'
                        or not isinstance(value.get('call_id'), str) or not value['call_id']):
                    fail(400, 'unsupported_tool', '工具搜索历史仅支持带call_id的客户端执行模式')
                if kind == 'tool_search_output':
                    local_tools(value.get('tools'))
                return
            if (any(key in value for key in ('image_url', 'file_id', 'file_data', 'audio_url', 'video_url'))
                    or kind in ('image_url', 'input_image', 'input_file', 'input_audio', 'audio', 'video', 'file')):
                if not getattr(body,'media_verified',False) or kind not in ('image_url','input_image'):
                    fail(400, 'unsupported_media', '请使用已验证图片或平台附件输入')
            for item in value.values():
                if isinstance(item, (dict, list)):
                    text_only(item)
    def local_tools(tools):
        if not isinstance(tools, list):
            fail(400, 'invalid_tools', 'tools必须为数组')
        for tool in tools:
            if not isinstance(tool, dict) or tool.get('type') not in ('function', 'custom', 'namespace', 'tool_search'):
                fail(400, 'unsupported_tool', '此路由仅支持客户端function/custom工具及客户端工具搜索')
            if tool.get('type') == 'tool_search' and (protocol == 'chat' or tool.get('execution') != 'client'):
                fail(400, 'unsupported_tool', '工具搜索仅支持Responses的execution:client模式')
            if tool.get('type') == 'namespace':
                local_tools(tool.get('tools', []))
    if isinstance(content, list):
        for item in content:
            text_only(item, input_item=True)
    else:
        text_only(content)
    local_tools(body.get('tools', []))
    from .media import copy_budget
    payload = copy_budget(body,dict(body))
    payload['model'] = model['upstream_model']
    if protocol != 'compact':
        payload['store'] = False
    output_fields = ('max_completion_tokens', 'max_tokens') if protocol == 'chat' else ('max_output_tokens',)
    provided = [body[field] for field in output_fields if body.get(field) is not None]
    if len(provided) > 1:
        fail(400, 'invalid_request', '请仅设置一个最大输出参数')
    max_output = provided[0] if provided else model['max_output']
    if type(max_output) is not int or not 1 <= max_output <= model['max_output']:
        fail(400, 'output_limit', '最大输出超出模型路由限制')
    if protocol != 'compact':
        for field in output_fields:
            payload.pop(field, None)
        field = 'max_output_tokens' if protocol == 'responses' else ('max_tokens' if model['provider'] == 'deepseek' else 'max_completion_tokens')
        payload[field] = max_output
    if model['provider'] == 'deepseek':
        payload.pop('store', None)
    if stream and protocol == 'chat':
        options = payload.get('stream_options', {})
        if not isinstance(options, dict):
            fail(400, 'invalid_request', 'stream_options无效')
        payload['stream_options'] = {**options, 'include_usage': True}
    from .token_budget import estimate_input
    input_estimate = estimate_input(payload, model['provider'])
    if input_estimate + max_output > model['context_limit']:
        QUOTA_LOG.warning('context_rejected input=%d output=%d limit=%d',
                          input_estimate, max_output, model['context_limit'])
        fail(400, 'context_limit', '上下文及最大输出超出路由限制，请压缩上下文')
    return payload, input_estimate, max_output


def admit(db, token, address, body, protocol, endpoint, idempotency=None, session=None, *, audit_id=None,managed_turn_id=None):
    now = time.time()
    if (db.settings.data_dir / 'maintenance').exists():
        fail(503, 'maintenance', '网关维护中，请稍后重试')
    if shutil.disk_usage(db.settings.data_dir).free < db.settings.min_free_bytes:
        fail(507, 'storage_capacity', '可用存储空间不足，暂缓新的模型请求')
    if idempotency is not None and (not idempotency or len(idempotency) > 200):
        fail(400, 'invalid_idempotency_key', '幂等键长度应为1至200字符')
    if not isinstance(body, dict) or not isinstance(body.get('model'), str):
        fail(400, 'invalid_request', '请指定model')
    with db.transaction() as c:
        key = key_from_token(c, db.settings, token, address)
        body_hash = digest(dumps({'endpoint': endpoint, 'body': body}))
        if idempotency:
            old = c.execute('SELECT id,status,body_hash FROM requests WHERE tenant_id=? AND key_id=? AND idempotency_key=?',
                            (key['tenant_id'], key['id'], idempotency)).fetchone()
            if old:
                raise GatewayError(409, 'idempotency_conflict' if old['body_hash'] != body_hash else 'request_already_exists',
                                   '此幂等键已提交；不会重复调用和收费。请在用量页查询原请求', old['id'])
        model = c.execute('SELECT * FROM models WHERE alias=? AND enabled=1', (body['model'],)).fetchone()
        if not model:
            fail(404, 'model_not_found', '模型不存在或未启用')
        model = dict(model)
        if getattr(body,'media_tokens',0):
            profile=c.execute('SELECT * FROM model_media WHERE model_id=?',(model['id'],)).fetchone()
            if not profile or tuple(profile[k] for k in ('model_id','vision','image_tokens','max_images','verified'))!=getattr(body,'media_profile',None):
                fail(409,'model_media_changed','图片能力或预算已变更，请重新校验后提交')
        pool = model_allowed(c, key, model)
        if protocol not in json.loads(model['protocols']):
            fail(400, 'protocol_unsupported', '此模型尚未开放该接口协议')
        payload, input_estimate, max_output = validate_payload(body, protocol, model)
        price = {field: model[field] for field in ('input_rate', 'cache_rate', 'output_rate', 'price_version')}
        price.update(model_alias=model['alias'], upstream_model=model['upstream_model'])
        if any(price[field] is None for field in ('input_rate', 'cache_rate', 'output_rate')):
            fail(503, 'pricing_missing', '模型尚未配置完整资费')
        from .token_budget import reservation_input_bound
        input_bound = reservation_input_bound(payload)
        charge = max(cost(price, input_bound, max_output), cost(price, input_bound, max_output, input_bound))
        estimate = input_estimate + max_output
        scopes = [('platform', 'global', '1=1', ()), ('tenant', key['tenant_id'], 'r.tenant_id=?', (key['tenant_id'],)),
                  ('user', key['user_id'], 'r.user_id=?', (key['user_id'],)),
                  ('application', key['application_id'], 'r.application_id=?', (key['application_id'],)),
                  ('key', key['id'], 'r.key_id=?', (key['id'],))]
        timeout = 300
        check_limits(c, '1=1', (), {'concurrency': db.settings.max_concurrency}, now, estimate, charge, input_estimate, max_output, scope='platform')
        for scope, scope_id, predicate, args in scopes:
            row = c.execute('SELECT limits FROM policies WHERE scope=? AND scope_id=?', (scope, scope_id)).fetchone()
            if row:
                limits = json.loads(row['limits'])
                check_limits(c, predicate, args, limits, now, estimate, charge, input_estimate, max_output, scope=scope)
                if limits.get('timeout') is not None:
                    timeout = min(timeout, limits['timeout'])
        tenant = dict(c.execute('SELECT * FROM tenants WHERE id=?', (key['tenant_id'],)).fetchone())
        subscription = active_subscription(c, key['tenant_id'])
        if tenant['billing_mode'] == 'package' and subscription:
            snapshot = json.loads(subscription['snapshot'])
            if '*' not in snapshot['models'] and model['alias'] not in snapshot['models']:
                fail(403, 'plan_model_forbidden', '当前套餐不包含此模型')
            check_limits(c, 'r.tenant_id=?', (key['tenant_id'],), snapshot['limits'], now, estimate, charge, input_estimate, max_output, scope='package')
        group = group_limits(c, pool['quota_group'], now)
        check_limits(c, 'r.pool_id IN (SELECT id FROM pools WHERE quota_group=?)', (pool['quota_group'],),
                     group, now, estimate, charge, input_estimate, max_output, scope='upstream_group')
        upstreams = c.execute('''SELECT u.*,(SELECT count(*) FROM requests r WHERE r.upstream_id=u.id AND r.status='running') load
            FROM upstreams u WHERE pool_id=? AND provider=? AND enabled=1 ORDER BY (load*1.0/weight),
            (SELECT COALESCE(max(a.created_at),0) FROM upstream_attempts a WHERE a.upstream_id=u.id),u.id''',
                              (pool['id'], model['provider'])).fetchall()
        affinity = None
        if managed_turn_id:
            managed_conversation=c.execute('''SELECT v.* FROM conversations v JOIN conversation_turns t ON t.conversation_id=v.id
                 WHERE t.id=? AND t.key_id=? AND v.user_id=? AND v.application_id=?''',(managed_turn_id,key['id'],key['user_id'],key['application_id'])).fetchone()
            if not managed_conversation:fail(409,'managed_turn_unavailable','托管轮次不存在或无权限')
        else:
            managed_conversation=c.execute('''SELECT v.* FROM conversations v JOIN request_audits a ON a.conversation_id=v.id
                 WHERE a.id=? AND v.user_id=? AND v.application_id=?''',(audit_id,key['user_id'],key['application_id'])).fetchone() if audit_id else None
        session_hash = digest(session) if session and protocol != 'chat' and not managed_conversation else None
        if session_hash:
            affinity = c.execute('SELECT upstream_id FROM affinities WHERE key_id=? AND model_id=? AND session_hash=?',
                                 (key['id'], model['id'], session_hash)).fetchone()
        chosen=None
        if managed_conversation and managed_conversation['gateway_upstream']:
            chosen=next((dict(u) for u in upstreams if u['id']==managed_conversation['gateway_upstream'] and u['load']<u['concurrency'] and eligible(c,u,now)),None)
            if chosen is None and managed_conversation['policy']=='strict':fail(503,'session_upstream_unavailable','会话固定上游暂不可用，等待恢复或明确迁移')
        if chosen is None:
            chosen = next((dict(u) for u in upstreams if u['load'] < u['concurrency']
                           and (not affinity or u['id'] == affinity['upstream_id']) and eligible(c, u, now)), None)
        if not chosen:
            fail(503, 'affinity_unavailable' if affinity else 'no_upstream_capacity',
                 '当前会话固定上游不可用，请等待恢复或使用新会话' if affinity else '暂无可调度上游，请检查容量、熔断或凭据状态')
        if managed_conversation:
            c.execute('UPDATE conversations SET gateway_upstream=? WHERE id=?',(chosen['id'],managed_conversation['id']))
            if managed_conversation['gateway_upstream']!=chosen['id']:
                audit(c,key['user_id'],key['tenant_id'],'conversation.gateway_binding',managed_conversation['id'],chosen['id'])
        pk = uid()
        c.execute('''INSERT INTO requests(id,tenant_id,user_id,application_id,key_id,model_id,pool_id,upstream_id,
          endpoint,body_hash,idempotency_key,status,estimate_tokens,reserved_micro,price,created_at,heartbeat)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,'running',?,?,?,?,?)''',
                  (pk, key['tenant_id'], key['user_id'], key['application_id'], key['id'], model['id'], pool['id'], chosen['id'],
                   endpoint, body_hash, idempotency, estimate, charge, dumps(price), now, now))
        reserve_buckets(c, pk, tenant, model['alias'], charge, subscription)
        attempt_id = lease_attempt(c, pk, chosen, pool['quota_group'], now)
        if managed_turn_id:
            c.execute("UPDATE conversation_turns SET request_id=? WHERE id=? AND key_id=? AND request_id IS NULL AND status='running'",(pk,managed_turn_id,key['id']))
        if audit_id:
            # Bind in the same transaction: a crash cannot leave a charged request
            # disconnected from its ingress trace. Body writes stay outside it.
            c.execute('''UPDATE request_audits SET request_id=?,execution_owner=1
                WHERE id=? AND user_id=? AND key_id=? AND request_id IS NULL''',
                (pk,audit_id,key['user_id'],key['id']))
            c.execute('''UPDATE conversation_turns SET request_id=? WHERE id=(SELECT turn_id FROM request_audits WHERE id=?)
                AND key_id=? AND request_id IS NULL AND status='running' ''',(pk,audit_id,key['id']))
        if session_hash:
            c.execute('''INSERT INTO affinities VALUES(?,?,?,?,?,?) ON CONFLICT(key_id,model_id,session_hash)
                         DO UPDATE SET last_used=excluded.last_used''',
                      (key['id'], model['id'], session_hash, chosen['id'], now, now))
        return {'id': pk, 'key': key, 'model': model, 'pool': pool, 'upstream': chosen, 'payload': payload,
                'protocol': protocol, 'timeout': timeout, 'endpoint': endpoint, 'attempt_id': attempt_id,
                'managed_conversation':managed_conversation['id'] if managed_conversation else None}


def safe_fallback(db, admitted, token, address):
    """Only a stateless Chat's known pre-HTTP connection failure can change its lease."""
    if admitted.get('managed_conversation') or admitted['pool']['kind'] != 'api' or admitted['protocol'] != 'chat' or admitted['payload'].get('stream'):
        return False
    with db.transaction() as c:
        key = key_from_token(c, db.settings, token, address)
        row = c.execute('SELECT * FROM requests WHERE id=? AND status=\'running\'', (admitted['id'],)).fetchone()
        if not row:
            return False
        tried = {r['upstream_id'] for r in c.execute('SELECT upstream_id FROM upstream_attempts WHERE request_id=?', (admitted['id'],))}
        if len(tried) >= 2:
            return False
        model = c.execute('SELECT * FROM models WHERE id=? AND enabled=1', (admitted['model']['id'],)).fetchone()
        if not model or any(model[k] != admitted['model'][k] for k in ('pool_id','provider','upstream_model','protocols')):
            fail(409, 'model_changed', '切换前模型配置已变化')
        pool = model_allowed(c, key, model)
        try:
            group_limits(c, pool['quota_group'], time.time())
        except GatewayError as error:
            if error.status == 429:
                return False
            raise
        candidates = c.execute('''SELECT u.*,(SELECT count(*) FROM requests r WHERE r.upstream_id=u.id AND r.status='running') load
            FROM upstreams u WHERE pool_id=? AND provider=? AND enabled=1 ORDER BY load*1.0/weight,
            (SELECT COALESCE(max(a.created_at),0) FROM upstream_attempts a WHERE a.upstream_id=u.id),u.id''',
                               (pool['id'], model['provider'])).fetchall()
        chosen = next((dict(u) for u in candidates if u['id'] not in tried and u['load'] < u['concurrency']
                       and eligible(c, u, time.time())), None)
        if not chosen:
            return False
        c.execute('UPDATE requests SET upstream_id=? WHERE id=?', (chosen['id'], admitted['id']))
        attempt_id = lease_attempt(c, admitted['id'], chosen, pool['quota_group'])
    admitted.update(upstream=chosen, attempt_id=attempt_id, pool=pool)
    return True


def cooldown(db, pool_id, seconds, *, group=None):
    with db.transaction() as c:
        row = c.execute('SELECT quota_group FROM pools WHERE id=?', (pool_id,)).fetchone()
        if row:
            c.execute('UPDATE pools SET cooldown_until=max(cooldown_until,?) WHERE quota_group=?',
                      (time.time() + max(6, seconds), group or row['quota_group']))


def still_authorized(db, token, address, admitted):
    with db.connect() as c:
        key = key_from_token(c, db.settings, token, address)
        model = c.execute('SELECT * FROM models WHERE id=? AND enabled=1', (admitted['model']['id'],)).fetchone()
        if not model:
            fail(403, 'model_changed', '调用前模型已停用')
        model_allowed(c, key, model)
        old = admitted['model']
        if any(model[field] != old[field] for field in ('pool_id', 'provider', 'upstream_model', 'protocols')):
            fail(409, 'model_changed', '调用前模型配置已变化，请重新提交')
        upstream = c.execute('SELECT * FROM upstreams WHERE id=? AND enabled=1', (admitted['upstream']['id'],)).fetchone()
        if not upstream or upstream['revision'] != admitted['upstream']['revision']:
            fail(409, 'upstream_changed', '调用前上游配置已变化，请重新提交')
