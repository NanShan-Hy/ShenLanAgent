from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
from cryptography.fernet import InvalidToken
from fastapi.responses import JSONResponse, StreamingResponse

from .billing import mark_unknown, settle
from .core import dumps
from .routing import cooldown, safe_fallback, still_authorized, validate_upstream
from .operations import begin_send, finish_connect_failure, retry_delay
from .http_pool import client_for
from .security import GatewayError, bearer

MAX_RESPONSE = 16 * 1024**2
MAX_EVENT = 2 * 1024**2


def error_body(code, message, request_id):
    return {'error': {'message': message, 'type': 'gateway_error', 'code': code, 'request_id': request_id}}


def receipt(value):
    return value[:160] if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,200}', value) else ''


def usage_of(raw):
    if not isinstance(raw, dict):
        return None
    source = raw.get('usage')
    if not isinstance(source, dict):
        return None
    input_tokens = source.get('input_tokens', source.get('prompt_tokens'))
    output_tokens = source.get('output_tokens', source.get('completion_tokens'))
    details = source.get('input_tokens_details', source.get('prompt_tokens_details')) or {}
    cached = details.get('cached_tokens', source.get('prompt_cache_hit_tokens', 0)) if isinstance(details, dict) else None
    values = (input_tokens, output_tokens, cached)
    if any(type(value) is not int or value < 0 or value > 100000000 for value in values) or cached > input_tokens:
        return None
    return values


class Tracker:
    def __init__(self, protocol):
        self.protocol = protocol
        self.usage = None
        self.provider_id = ''
        self.complete = False
        self.terminal = False
        self.failed = False
        self.error_code = ''

    def accept(self, data):
        if data == '[DONE]':
            if self.protocol == 'chat':
                self.complete = not self.failed
                self.terminal = True
            return
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError('invalid SSE payload')
        if raw.get('error') or raw.get('type') == 'error':
            self.failed = True
            self.terminal = True
        nested = raw.get('response', raw)
        if isinstance(nested, dict):
            self.usage = usage_of(nested) or self.usage
            self.provider_id = receipt(nested.get('id')) or self.provider_id
        if self.protocol != 'chat':
            if raw.get('type') == 'response.completed':
                self.complete = True
                self.terminal = True
            elif raw.get('type') in ('response.failed', 'response.incomplete'):
                self.failed = True
                self.terminal = True


async def heartbeat(db, request_id):
    while True:
        await asyncio.sleep(5)
        db.execute("UPDATE requests SET heartbeat=? WHERE id=? AND status='running'", (time.time(), request_id))


def remaining(admitted):
    return max(0.0, admitted['deadline'] - time.monotonic())


async def perform(request, protocol, endpoint):
    try:
        result=await _perform(request,protocol,endpoint)
        turn=getattr(request.state,'managed_turn',None)
        if turn:
            if not isinstance(result,StreamingResponse):turn.finish()
            result.headers.update(turn.headers())
            if turn.finished and getattr(turn,'final_state',None)=='needs_recovery':
                result.headers['X-Gateway-Context-Warning']='needs_recovery'
        return result
    except BaseException as exc:
        turn=getattr(request.state,'managed_turn',None)
        if turn:
            turn.finish(getattr(exc,'code','request_interrupted'))
        raise


async def _perform(request, protocol, endpoint):
    started = time.monotonic()
    db = request.app.state.db
    context = getattr(request.state, 'request_audit', None)
    if context:context.phase_at('authentication')
    token = bearer(request)
    address = request.client.host if request.client else ''
    if context:context.phase_at('validation')
    try:
        def bad_constant(value):
            raise ValueError('nonfinite JSON')
        raw_body = await request.body()
        body = json.loads(raw_body, parse_constant=bad_constant)
    except (ValueError, UnicodeDecodeError):
        raise GatewayError(400, 'invalid_json', '请发送有效JSON')
    if not isinstance(body,dict):raise GatewayError(400,'invalid_request','需要JSON对象')
    if context:
        context.phase_at('validation')
        context.safe('input', body)
    from .conversations import prepare
    from .media import resolve_media
    body,turn=await prepare(request,body,protocol)
    body=await resolve_media(db,request,body,protocol)
    session = request.headers.get('x-session-id') or request.headers.get('x-gateway-session') or request.headers.get('session_id') or request.headers.get('x-codex-session-id')
    metadata = body.get('client_metadata') if isinstance(body, dict) else None
    if not session and isinstance(metadata, dict):
        session = metadata.get('session_id') or metadata.get('thread_id')
    if not isinstance(session, str) or not 1 <= len(session) <= 200:
        session = None
    caller_session = session
    if turn:caller_session=session=turn.conversation['id']
    if protocol != 'chat' and not session:
        session = 'default-key-model-session'
    from .routing import admit
    admitted = admit(db,token,address,body,protocol,endpoint,request.headers.get('idempotency-key'),session,
                     audit_id=context.id if context and context.exists else None,
                     managed_turn_id=turn.id if turn else getattr(request.state,'summary_turn_id',None))
    if turn:turn.link(admitted['id'])
    if context:context.link(admitted['id'])
    admitted['_audit'] = context
    admitted['_managed_turn'] = turn
    admitted['started_monotonic'] = started
    admitted['deadline'] = admitted['started_monotonic'] + admitted['timeout']
    pk = admitted['id']
    response = None
    pulse = asyncio.create_task(heartbeat(db, pk))
    uncertain = False
    try:
        client = client_for(request.app)
        while True:
            upstream = admitted['upstream']
            try:
                await asyncio.wait_for(asyncio.to_thread(validate_upstream, db.settings, upstream['provider'],
                                      upstream['base_url'], resolve=True), min(10, remaining(admitted)))
            except TimeoutError:
                finish_connect_failure(db, admitted['attempt_id'], 'upstream_dns_failed')
                raise GatewayError(502, 'upstream_dns_failed', '上游解析超过截止时间，本次冻结将释放', pk)
            except GatewayError as error:
                if error.code == 'upstream_dns_failed':
                    finish_connect_failure(db, admitted['attempt_id'], 'upstream_dns_failed')
                raise
            still_authorized(db, token, address, admitted)
            if context:context.phase_at('credentials')
            try:
                secret = db.settings.unseal(upstream['secret'])['api_key']
                if not isinstance(secret, str) or not secret:
                    raise ValueError('empty credential')
            except (InvalidToken, ValueError, KeyError, TypeError):
                settle(db, pk, status='failed', error='upstream_credential_unavailable')
                raise GatewayError(502, 'upstream_credential_unavailable', '上游凭据不可用，请管理员检查配置；本次冻结已释放', pk)
            if context:context.redactor.secrets.append(secret)
            headers = {'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json', 'X-Client-Request-Id': pk}
            if upstream['provider']=='account_pool':
                from .core import digest
                headers['X-Session-ID']='gw-'+digest(admitted['key']['id']+'\0'+admitted['model']['id']+'\0'+(caller_session or pk))
                from .account_binding import bind
                account=await bind(request,admitted,caller_session or pk) if caller_session or turn or getattr(request.state,'binding_conversation',None) else None
                if account:headers['X-Gateway-Account-ID']=account
            for name in ('openai-beta', 'x-codex-turn-state', 'x-codex-turn-metadata'):
                if name in request.headers:
                    headers[name] = request.headers[name]
            outbound = client.build_request('POST', upstream['base_url'] + endpoint, json=admitted['payload'], headers=headers,
                         timeout=httpx.Timeout(max(.001, remaining(admitted)), connect=min(10, max(.001, remaining(admitted)))))
            begin_send(db, admitted['attempt_id'])
            if context:context.phase_at('dispatched')
            uncertain = True
            try:
                response = await asyncio.wait_for(client.send(outbound, stream=True), remaining(admitted))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                uncertain = False
                finish_connect_failure(db, admitted['attempt_id'], 'upstream_connection_failed')
                if remaining(admitted) > 0 and safe_fallback(db, admitted, token, address):
                    continue
                settle(db, pk, status='failed', error='upstream_connection_failed')
                raise GatewayError(502, 'upstream_connection_failed', '连接上游失败，本次冻结已释放', pk)
            except httpx.PoolTimeout:
                uncertain = False
                settle(db, pk, status='failed', error='gateway_connection_capacity')
                raise GatewayError(503, 'gateway_connection_capacity', '连接容量暂不可用，本次冻结已释放', pk)
            break
        provider_id = receipt(response.headers.get('x-request-id', ''))
        if provider_id:
            db.execute('UPDATE requests SET provider_id=? WHERE id=?', (provider_id, pk))
        admitted['http_status'] = response.status_code
        binding=getattr(request.state,'account_binding',None)
        if binding and response.status_code<300 and (response.headers.get('x-gateway-account-id')!=binding[1] or response.headers.get('x-gateway-pinning-version')!='1'):
            raise ValueError('account binding receipt mismatch')
        if context:context.phase_at('upstream_headers')
        if response.status_code >= 300:
            from .account_binding import failed
            failed(db,binding,response.status_code)
            status = response.status_code
            provider_code = ''
            # The dedicated bridge owns per-account quota/cooldown. A single
            # account's 429 must not freeze unrelated subscriptions in that pool.
            if status == 429 and upstream['provider']!='account_pool':
                cooldown(db, admitted['pool']['id'], retry_delay(response.headers.get('retry-after')),
                         group=admitted['pool']['quota_group'])
            if 400 <= status < 500:
                settle(db, pk, status='failed', error='upstream_http_' + str(status), provider_id=provider_id, http_status=status)
            else:
                mark_unknown(db, pk, 'upstream_http_' + str(status), http_status=status, provider_id=provider_id)
            return JSONResponse(error_body(provider_code or 'upstream_http_' + str(status),
                                '上游未完成请求，请凭请求编号查看记录；不会重放可能已发送的请求', pk),
                                status_code=429 if status == 429 else 502, headers={'X-Request-Id': pk})
        if body.get('stream', False):
            held_response, held_pulse = response, pulse
            response = pulse = None
            return StreamingResponse(stream_reply(db, token, address, admitted, held_response, held_pulse),
                                     media_type='text/event-stream', headers={'X-Request-Id': pk, 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        chunks, size = [], 0
        async with asyncio.timeout(remaining(admitted)):
            async for chunk in response.aiter_bytes():
                if context and 'first_byte' not in context.timeline:context.phase_at('first_byte')
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError('oversized upstream response')
                chunks.append(chunk)
        raw = json.loads(b''.join(chunks))
        if not isinstance(raw, dict):
            raise ValueError('invalid upstream response')
        if context:context.safe('output', raw)
        if turn:turn.observe(raw)
        usage = usage_of(raw)
        success = (isinstance(raw.get('choices'), list) and bool(raw['choices'])) if protocol == 'chat' else (
            raw.get('object') == 'response.compaction' if protocol == 'compact' else raw.get('status') == 'completed')
        if not usage or raw.get('error'):
            mark_unknown(db, pk, 'upstream_usage_missing', http_status=200, provider_id=provider_id)
            return JSONResponse(error_body('upstream_usage_missing', '未获得可靠用量，请求已进入待对账；不会自动扣费', pk),
                                status_code=502, headers={'X-Request-Id': pk})
        result = JSONResponse(raw, headers={'X-Request-Id': pk})
        settle(db, pk, usage, status='succeeded' if success else 'failed',
               error='' if success else 'upstream_incomplete', provider_id=provider_id or receipt(raw.get('id')), http_status=200)
        still_authorized(db, token, address, admitted)
        return result
    except GatewayError as error:
        if not uncertain:
            settle(db, pk, status='cancelled', error=error.code)
        else:
            mark_unknown(db, pk, 'authorization_or_configuration_changed')
        error.request_id = error.request_id or pk
        raise
    except TimeoutError:
        if uncertain:
            mark_unknown(db, pk, 'upstream_deadline_exceeded')
        else:
            settle(db, pk, status='failed', error='upstream_deadline_exceeded')
        raise GatewayError(504, 'upstream_deadline_exceeded', '本次请求已达到总超时；已发送的请求不会自动重放', pk)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        if uncertain:
            mark_unknown(db, pk)
        else:
            settle(db, pk, status='failed', error='outbound_request_invalid')
        raise GatewayError(502, 'upstream_outcome_unknown' if uncertain else 'outbound_request_invalid',
                           '请求未完成，请凭请求编号查看状态；不会重复生成', pk)
    except asyncio.CancelledError:
        if uncertain:
            mark_unknown(db, pk, 'client_disconnected')
        else:
            settle(db, pk, status='cancelled', error='client_disconnected')
        raise
    finally:
        if pulse:
            pulse.cancel()
        if response:
            await response.aclose()


async def stream_reply(db, token, address, admitted, response, pulse):
    pk = admitted['id']
    context = admitted.get('_audit')
    turn=admitted.get('_managed_turn')
    tracker = Tracker(admitted.get('upstream_protocol',admitted['protocol']))
    buffer, size = b'', 0
    last_check = time.monotonic()
    completed = False
    try:
        async with asyncio.timeout(remaining(admitted)):
            async for chunk in response.aiter_bytes():
                if context and 'first_byte' not in context.timeline:context.phase_at('first_byte')
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError('oversized stream')
                buffer = (buffer + chunk).replace(b'\r\n', b'\n')
                if time.monotonic() - last_check > 2:
                    still_authorized(db, token, address, admitted)
                    last_check = time.monotonic()
                while b'\n\n' in buffer:
                    frame, buffer = buffer.split(b'\n\n', 1)
                    if len(frame) > MAX_EVENT:
                        raise ValueError('oversized event')
                    lines = frame.decode().split('\n')
                    data = '\n'.join(line[5:].lstrip(' ') for line in lines if line.startswith('data:'))
                    if data:
                        tracker.accept(data)
                        if context and data != '[DONE]':context.safe('event', json.loads(data))
                        if turn and data!='[DONE]':turn.event(json.loads(data))
                    if tracker.failed and not tracker.usage:
                        raise ValueError('upstream failed')
                    if tracker.terminal:
                        if not tracker.usage:
                            raise ValueError('terminal usage missing')
                        settle(db, pk, tracker.usage, status='succeeded' if tracker.complete else 'failed',
                               error='' if tracker.complete else 'upstream_incomplete', provider_id=tracker.provider_id, http_status=200)
                        completed = True
                        still_authorized(db,token,address,admitted)
                        if turn:
                            turn.finish()
                            if getattr(turn,'final_state',None)=='needs_recovery':
                                yield ('data: '+dumps(error_body('context_save_failed','模型调用已结束，但托管历史保存失败；请保存已收到的结果并明确恢复会话，不要重复生成',pk))+'\n\n').encode()
                                return
                    if tracker.failed:
                        safe = json.loads(data)
                        if isinstance(safe.get('response'), dict):
                            safe['response']['error'] = {'code': 'upstream_incomplete', 'message': '上游请求未完整完成'}
                        safe.pop('error', None)
                        frame = ('event: ' + safe.get('type', 'error') + '\ndata: ' + dumps(safe)).encode()
                    yield frame + b'\n\n'
                    if tracker.terminal:
                        return
                if len(buffer) > MAX_EVENT:
                    raise ValueError('oversized event')
        if not completed:
            raise ValueError('stream ended before terminal event')
    except asyncio.CancelledError:
        mark_unknown(db, pk, 'client_disconnected')
        raise
    except (httpx.HTTPError, ValueError, TimeoutError, UnicodeDecodeError, GatewayError):
        mark_unknown(db, pk, 'stream_interrupted')
        body = error_body('stream_interrupted', '流式请求未完整结束，已记录待对账状态', pk)
        yield ('data: ' + dumps(body) + '\n\n').encode()
    finally:
        if not completed:
            mark_unknown(db, pk, 'stream_interrupted')
        pulse.cancel()
        await response.aclose()
        if turn:turn.finish()
