"""Operator-only, bounded encrypted transcripts, separate from financial requests."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import re
import shutil
import sqlite3
import time

from .core import dumps, uid
from .security import GatewayError, key_from_token

LOG = logging.getLogger('aigateway.capture')
CHUNK = 65536
PATHS = {'/v1/chat/completions': 'chat', '/v1/responses': 'responses', '/v1/responses/compact': 'compact', '/v1/models': 'catalog'}
SENSITIVE = r'(?:(?:x[-_])?api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|client[_-]?secret|password|passwd|authorization|cookie|secret(?:[_-]?key)?|private[_-]?key|aws_secret_access_key|aws_access_key_id)'
FIELD = re.compile(r'(?i)([\"\x27]?'+SENSITIVE+r'[\"\x27]?\s*[:=]\s*)(?:\"[^\"]*\"|\x27[^\x27]*\x27|[^\s,;}]+)')
TOKENS = re.compile(r'(?i)\bsk-[a-z0-9_-]{8,}|\bBearer\s+[a-z0-9_.~+/-]+=*|\beyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+')
PRIVATE = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
OPEN_FIELD = re.compile(r'(?i)[\"\x27]?'+SENSITIVE+r'[\"\x27]?\s*[:=]\s*([\"\x27])')


@contextmanager
def capture_transaction(db):
    # A busy capture writer must not hold up inference/financial transactions.
    with db.connect() as c:
        c.execute('PRAGMA busy_timeout=100')
        c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 8]

    def text(self, text):
        for value in self.secrets:
            text = text.replace(value, '[REDACTED]')
        text = PRIVATE.sub('[REDACTED PRIVATE KEY]', text)
        text = FIELD.sub(lambda m: m[1] + '"[REDACTED]"', text)
        text = re.sub(r'(?i)([?&](?:code|state|access_token|refresh_token|client_secret)=)[^&\s\"\x27]+', r'\1[REDACTED]', text)
        return TOKENS.sub('[REDACTED]', text)

    def value(self, value):
        if isinstance(value, dict):
            return {self.text(k): '[REDACTED]' if re.fullmatch(SENSITIVE, k, re.I) else self.value(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v) for v in value]
        return self.text(value) if isinstance(value, str) else value

    def cut(self, text, force=False):
        if force:
            return len(text)
        if len(text) < CHUNK + 4096:
            return 0
        cut = min(CHUNK, len(text) - 4096)
        spans = [m.span() for pattern in (FIELD, TOKENS, PRIVATE) for m in pattern.finditer(text)]
        for secret in self.secrets:
            start = text.find(secret)
            while start >= 0:
                spans.append((start, start + len(secret)))
                start = text.find(secret, start + len(secret))
        for start, end in sorted(spans, reverse=True):
            if start < cut < end or (end == len(text) and start < cut):
                cut = min(cut, start)
        # Hold incomplete quoted credentials until their closing quote arrives.
        for m in OPEN_FIELD.finditer(text):
            if text.find(m[1], m.end()) < 0:
                cut = min(cut, m.start())
        start = text.find('-----BEGIN ')
        if start >= 0 and 'PRIVATE KEY-----' in text[start:] and '-----END ' not in text[start:]:
            cut = min(cut, start)
        return cut


def current_input(body, protocol):
    items = body.get('messages' if protocol == 'chat' else 'input')
    if isinstance(items, str):
        return [{'role': 'user', 'content': items}], []
    if not isinstance(items, list):
        return [], []
    users = [i for i, item in enumerate(items) if isinstance(item, dict) and item.get('role') == 'user'
             and item.get('type') not in ('additional_tools', 'tool_search_output', 'tool_search_call')]
    start = users[-1] if users else len(items)
    while start > 0 and isinstance(items[start - 1], dict) and items[start - 1].get('role') == 'user':
        start -= 1
    selected = items[start:] if users else items
    messages, tools = [], []
    for item in selected:
        if not isinstance(item, dict):
            continue
        kind = item.get('type', '')
        if item.get('role') == 'user' and kind not in ('additional_tools', 'tool_search_output', 'tool_search_call'):
            content = item.get('content', '')
            if isinstance(content, list):
                parts=[]
                for p in content:
                    if not isinstance(p,dict):continue
                    if p.get('type') in ('text','input_text','output_text'):parts.append(p)
                    elif p.get('type') in ('input_file','file'):
                        f=p.get('file',p);parts.append({'type':'attachment_reference',**{k:f[k] for k in ('file_id','filename','selected_sections') if k in f}})
                    elif p.get('type') in ('input_image','image_url'):parts.append({'type':'image_reference','source':'client_image'})
                content=parts
            messages.append({'role': 'user', 'content': content})
        elif item.get('role') == 'tool' or kind in ('function_call_output', 'custom_tool_call_output', 'tool_search_output'):
            tools.append({'origin': 'client_return', **{k: item[k] for k in ('type', 'call_id', 'tool_call_id', 'name', 'output', 'content') if k in item}})
        elif kind in ('function_call', 'custom_tool_call'):
            tools.append({'origin': 'client_history_model_request', **{k: item[k] for k in ('type', 'call_id', 'name', 'namespace', 'arguments', 'input') if k in item}})
        elif item.get('role') == 'assistant':
            for call in item.get('tool_calls', []):
                function=call.get('function', {})
                tools.append({'origin':'client_history_model_request','type':'function_call','call_id':call.get('id'),
                              'name':function.get('name'),'arguments':function.get('arguments')})
    return messages, tools


class AuditContext:
    def __init__(self, db, scope):
        self.db, self.id = db, uid()
        self.created = time.time()
        self.phase = 'received'
        self.timeline = {'received': self.created}
        self.request_id, self.owner, self.error_code = None, False, ''
        self.http_status, self.finished = None, False
        self.identity, self.authorized, self.enabled = {}, False, False
        self.content_status, self.reason, self.exists = 'not_captured', 'unauthenticated', False
        self.output_pending, self.saw_delta = '', False
        self.tool_names, self.tool_done = {}, set()
        self.pending_tool_bytes = 0
        self.protocol = PATHS[scope['path']]
        headers = dict(scope.get('headers', []))
        auth = headers.get(b'authorization', b'').decode('latin1')
        token = auth[7:] if auth.startswith('Bearer ') and len(auth) <= 207 else ''
        self.redactor = Redactor([token])
        address = (scope.get('client') or ('', 0))[0]
        try:
            with db.connect() as c:
                if token:
                    row = c.execute('SELECT user_id,tenant_id,application_id,id key_id FROM api_keys WHERE hash=?', (db.settings.key_hash(token),)).fetchone()
                    if row:
                        self.identity = dict(row)
                    try:
                        key_from_token(c, db.settings, token, address)
                        self.authorized = True
                    except GatewayError:
                        pass
                setting = c.execute('SELECT enabled FROM request_recording_settings WHERE id=1').fetchone()
                self.enabled = bool(setting and setting['enabled'] and self.authorized and self.protocol != 'catalog')
            self.content_status = 'recording' if self.enabled else ('disabled' if self.authorized else 'not_captured')
            self.reason = '' if self.enabled else ('recording_disabled' if self.authorized else 'unauthenticated')
            if self.protocol == 'catalog':
                self.content_status,self.reason='not_captured','metadata_only'
            with capture_transaction(db) as c:
                c.execute('''INSERT INTO request_audits(id,user_id,tenant_id,application_id,key_id,endpoint,protocol,source_ip,
                    created_at,timeline,capture_status,capture_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (self.id, self.identity.get('user_id'), self.identity.get('tenant_id'), self.identity.get('application_id'),
                     self.identity.get('key_id'), scope['path'][3:], self.protocol, address[:80], self.created,
                     dumps(self.timeline), self.content_status, self.reason))
            self.exists = True
        except (sqlite3.Error, OSError, ValueError):
            self.problem('audit_storage_unavailable')

    def problem(self, reason):
        if self.content_status not in ('cleared', 'historical'):
            self.content_status = 'partial' if self.enabled else 'not_captured'
            self.reason = reason
        LOG.warning('capture_unavailable reason=%s', reason)

    def phase_at(self, phase):
        self.phase = phase
        self.timeline.setdefault(phase, time.time())
        if self.exists:
            try:
                with capture_transaction(self.db) as c:
                    c.execute('UPDATE request_audits SET stage=?,timeline=?,request_id=?,execution_owner=?,dispatched_at=?,first_byte_at=? WHERE id=?',
                              (phase,dumps(self.timeline),self.request_id,self.owner,self.timeline.get('dispatched'),self.timeline.get('first_byte'),self.id))
            except (sqlite3.Error, OSError):
                self.problem('audit_stage_write_failed')

    def safe(self, method, *args):
        try:
            return getattr(self, method)(*args)
        except Exception:
            self.problem('capture_processing_failed')

    def link(self, request_id, owner=True):
        self.request_id, self.owner = request_id, owner
        self.phase_at('reserved' if owner else 'idempotency')

    def associate(self,conversation_id,turn_id=None):
        if self.exists:
            with capture_transaction(self.db) as c:
                c.execute('UPDATE request_audits SET conversation_id=?,turn_id=? WHERE id=?',(conversation_id,turn_id,self.id))

    def error(self, code):
        self.error_code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else 'request_failed'

    def _write(self, section, text):
        if not self.enabled or not self.exists or self.reason:
            return
        try:
            if shutil.disk_usage(self.db.settings.data_dir).free < self.db.settings.min_free_bytes:
                self.problem('disk_space_low')
                return
            # Split Unicode characters, so every page independently decodes correctly.
            for offset in range(0, len(text), CHUNK // 4):
                piece = text[offset:offset + CHUNK // 4]
                with capture_transaction(self.db) as c:
                    current = c.execute('SELECT capture_status FROM request_audits WHERE id=?', (self.id,)).fetchone()
                    if not current or current['capture_status'] == 'cleared':
                        self.enabled = False
                        return
                    settings = c.execute('SELECT * FROM request_recording_settings WHERE id=1').fetchone()
                    if not settings['enabled']:
                        self.problem('recording_disabled_during_request')
                        return
                    seq = c.execute('SELECT COALESCE(max(seq),-1)+1 FROM request_capture_chunks WHERE audit_id=? AND section=?', (self.id, section)).fetchone()[0]
                    cipher = self.db.settings.seal({'audit_id': self.id, 'section': section, 'seq': seq, 'text': piece})
                    size = len(cipher.encode('ascii'))
                    if settings['used_bytes'] + size > settings['max_bytes']:
                        self.problem('storage_limit')
                        return
                    c.execute('INSERT INTO request_capture_chunks VALUES(?,?,?,?,?)', (self.id, section, seq, cipher, size))
                    c.execute('UPDATE request_recording_settings SET used_bytes=used_bytes+? WHERE id=1', (size,))
                    c.execute('UPDATE request_audits SET capture_bytes=capture_bytes+? WHERE id=?', (size, self.id))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.problem('capture_write_failed')

    def record(self, section, value):
        if self.enabled and not self.reason:
            value = self.redactor.value(value)
            self._write(section, json.dumps(value, ensure_ascii=False, indent=2) + '\n\n')

    def input(self, body):
        if not isinstance(body, dict):
            return
        try:
            messages, tools = current_input(body, self.protocol)
            if messages:
                self.record('input', messages)
            for item in tools:
                self.record('tools', item)
            model = self.redactor.text(body.get('model', ''))[:120] if isinstance(body.get('model'), str) else ''
            if self.exists:
                with capture_transaction(self.db) as c:
                    c.execute('UPDATE request_audits SET model=?,stream=? WHERE id=?', (model, body.get('stream') is True, self.id))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.problem('input_capture_failed')

    def text(self, value, final=False):
        if not self.enabled or self.reason:
            return
        if isinstance(value, str):
            self.output_pending += value
        while self.output_pending:
            cut = self.redactor.cut(self.output_pending, final)
            if not cut:
                if len(self.output_pending) > 2 * CHUNK:
                    self.problem('redaction_buffer_limit')
                    self.output_pending = ''
                break
            piece, self.output_pending = self.output_pending[:cut], self.output_pending[cut:]
            self._write('output', self.redactor.text(piece))

    def tool(self, item):
        if item.get('type') not in ('function_call', 'custom_tool_call', 'tool_search_call'):
            return
        ident = item.get('id') or item.get('call_id') or ''
        self.tool_names[ident] = {k: item[k] for k in ('type', 'id', 'name', 'namespace', 'call_id') if k in item}
        args = item.get('arguments', item.get('input'))
        if args not in (None, '') and ident not in self.tool_done:
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                pass
            self.record('tools', {'origin': 'model_request', **self.tool_names[ident], 'arguments': args})
            self.tool_done.add(ident)

    def output(self, raw):
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get('choices'), list):
            for choice in raw['choices']:
                message = choice.get('message', {})
                self.text(message.get('content', ''))
                for call in message.get('tool_calls', []):
                    f = call.get('function', {})
                    self.tool({'type': 'function_call', 'id': call.get('id'), 'name': f.get('name'), 'arguments': f.get('arguments')})
        for item in raw.get('output', []):
            if not isinstance(item, dict):
                continue
            self.tool(item)
            if item.get('type')=='compaction':
                self.text('【供应商返回加密压缩状态，无可读回答正文】')
            for summary in item.get('summary', []):
                if isinstance(summary,dict) and summary.get('type')=='summary_text':
                    self.text('\n【模型公开摘要】\n'+summary.get('text',''))
            if not self.saw_delta:
                for content in item.get('content', []):
                    if isinstance(content, dict) and content.get('type') in ('output_text', 'text'):
                        self.text(content.get('text', ''))
        self.text('', final=True)

    def event(self, raw):
        if not isinstance(raw, dict):
            return
        kind = raw.get('type', '')
        if kind == 'response.output_text.delta':
            self.saw_delta = True
            self.text(raw.get('delta', ''))
        elif kind in ('response.output_item.added', 'response.output_item.done'):
            self.tool(raw.get('item', {}))
        elif kind == 'response.function_call_arguments.done':
            ident = raw.get('item_id', '')
            self.tool({**self.tool_names.get(ident, {}), 'id': ident, 'type': 'function_call', 'arguments': raw.get('arguments', '')})
        elif kind in ('response.completed', 'response.failed', 'response.incomplete'):
            self.output(raw.get('response', {}))
        if self.protocol == 'chat':
            for choice in raw.get('choices', []):
                delta = choice.get('delta', {})
                self.text(delta.get('content', ''))
                # Argument fragments are retained in memory only until a complete JSON argument is available.
                for call in delta.get('tool_calls', []):
                    ident = 'chat-' + str(call.get('index', 0))
                    pending = self.tool_names.setdefault(ident, {'type': 'function_call', 'id': ident, 'arguments': ''})
                    function = call.get('function', {})
                    pending['name'] = function.get('name') or pending.get('name', '')
                    fragment=function.get('arguments', '')
                    self.pending_tool_bytes += len(fragment.encode('utf-8'))
                    pending['arguments'] += fragment
                    if self.pending_tool_bytes > 2 * 1024**2:
                        pending['arguments'] = ''
                        self.problem('tool_arguments_limit')
                        self.tool_names.clear()
                if choice.get('finish_reason'):
                    for item in list(self.tool_names.values()):
                        self.tool(item)

    def finish(self, status=None):
        if self.finished:
            return
        self.finished = True
        try:
            for ident, item in self.tool_names.items():
                if ident not in self.tool_done:
                    self.record('tools', {'origin': 'model_request', **{k:v for k,v in item.items() if k!='arguments'}, 'state': 'arguments_not_completed'})
            self.text('', final=True)
        except Exception:
            self.problem('capture_finalize_failed')
        try:
            if self.request_id is None:
                linked=self.db.one('SELECT request_id,execution_owner FROM request_audits WHERE id=?',(self.id,))
                if linked and linked['request_id']:
                    self.request_id,self.owner=linked['request_id'],bool(linked['execution_owner'])
            row = self.db.one('SELECT status,error_code FROM requests WHERE id=?', (self.request_id,)) if self.request_id else None
            status = row['status'] if row and self.owner else (status or ('rejected' if (self.http_status or 500) >= 400 else 'succeeded'))
            if row and row['error_code'] and self.owner:
                self.error(row['error_code'])
            if (self.http_status or 500)>=400 and not self.error_code:
                self.error('http_'+str(self.http_status or 500))
            if self.enabled and not self.reason:
                self.content_status = 'complete' if status == 'succeeded' else 'partial'
                self.reason = '' if status == 'succeeded' else 'request_not_completed'
            if status=='succeeded':
                self.phase='finished'
            self.timeline.setdefault('finished', time.time())
            with capture_transaction(self.db) as c:
                c.execute('''UPDATE request_audits SET request_id=?,execution_owner=?,status=?,stage=?,http_status=?,error_code=?,
                    finished_at=?,dispatched_at=?,first_byte_at=?,timeline=?,capture_status=CASE WHEN capture_status='cleared' THEN capture_status ELSE ? END,
                    capture_reason=CASE WHEN capture_status='cleared' THEN capture_reason ELSE ? END WHERE id=?''',
                    (self.request_id, self.owner, status, self.phase, self.http_status or 500, self.error_code,
                     time.time(), self.timeline.get('dispatched'), self.timeline.get('first_byte'), dumps(self.timeline),
                     self.content_status, self.reason, self.id))
        except (sqlite3.Error, OSError, ValueError):
            self.problem('audit_finalize_failed')
        self.output_pending = ''
        self.tool_names.clear()


class RequestAuditMiddleware:
    def __init__(self, app, db):
        self.app, self.db = app, db

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') not in PATHS:
            return await self.app(scope, receive, send)
        context = AuditContext(self.db, scope)
        scope.setdefault('state', {})['request_audit'] = context
        async def observed(message):
            if message['type'] == 'http.response.start':
                context.http_status = message['status']
                message = {**message, 'headers': list(message.get('headers', [])) + [(b'x-gateway-trace-id', context.id.encode())]}
            await send(message)
        try:
            await self.app(scope, receive, observed)
        except Exception as exc:
            context.error(context.error_code or 'request_interrupted')
            if context.http_status is None:
                from fastapi.responses import JSONResponse
                LOG.error('gateway_unhandled_exception type=%s', type(exc).__name__)
                await JSONResponse({'error':{'type':'gateway_error','code':'gateway_internal_error','message':'网关处理异常，请凭追踪编号查看记录'}},status_code=500)(scope,receive,observed)
            else:
                raise
        finally:
            context.safe('finish')


def recover(db):
    with capture_transaction(db) as c:
        c.execute('''UPDATE request_audits SET status=COALESCE((SELECT status FROM requests WHERE id=request_id),'unknown'),
           capture_status=CASE WHEN capture_status='recording' THEN 'partial' ELSE capture_status END,
           capture_reason=CASE WHEN capture_status='recording' THEN 'service_restarted' ELSE capture_reason END,
           finished_at=COALESCE(finished_at,?),error_code=CASE WHEN error_code='' THEN 'service_restarted' ELSE error_code END
           WHERE status='running' ''', (time.time(),))
