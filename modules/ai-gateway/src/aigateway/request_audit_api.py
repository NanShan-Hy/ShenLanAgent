"""Global operator access only; financial usage endpoints never expose contents."""
import json
import shutil
import sqlite3
import time

from cryptography.fernet import InvalidToken
from fastapi import Body, Query, Request

from .core import audit, dumps, digest
from .request_audit import capture_transaction
from .security import admin, fail


SELECT = '''SELECT a.*,u.username,u.display_name,t.name tenant_name,p.name application_name,
 k.name key_name,k.prefix key_prefix,k.suffix key_suffix,
 CASE WHEN a.execution_owner=1 THEN COALESCE(r.status,a.status) ELSE a.status END effective_status,
 CASE WHEN a.execution_owner=1 THEN COALESCE(r.input_tokens,0) ELSE 0 END input_tokens,
 CASE WHEN a.execution_owner=1 THEN COALESCE(r.output_tokens,0) ELSE 0 END output_tokens,
 CASE WHEN a.execution_owner=1 THEN COALESCE(r.cached_tokens,0) ELSE 0 END cached_tokens,
 CASE WHEN a.execution_owner=1 THEN COALESCE(r.charged_micro,0) ELSE 0 END charged_micro
 FROM request_audits a LEFT JOIN users u ON u.id=a.user_id LEFT JOIN tenants t ON t.id=a.tenant_id
 LEFT JOIN applications p ON p.id=a.application_id LEFT JOIN api_keys k ON k.id=a.key_id
 LEFT JOIN requests r ON r.id=a.request_id'''


def public_record(row):
    row = dict(row)
    row['status'] = row.pop('effective_status')
    row['timeline'] = json.loads(row['timeline'])
    row['duration_ms'] = round(((row['finished_at'] or time.time()) - row['created_at']) * 1000)
    row['first_byte_ms'] = round((row['first_byte_at'] - row['created_at']) * 1000) if row['first_byte_at'] else None
    return row


def filters(values):
    where, args = ['1=1'], []
    for key in ('user_id', 'tenant_id', 'application_id', 'key_id', 'model', 'request_id', 'stage'):
        if values.get(key):
            if not isinstance(values[key], str) or len(values[key]) > 160:
                fail(400, 'invalid_filter', '筛选值无效')
            where.append('a.' + key + '=?');args.append(values[key])
    if values.get('trace_id'):
        where.append('a.id=?');args.append(values['trace_id'])
    if values.get('status'):
        where.append("CASE WHEN a.execution_owner=1 THEN COALESCE(r.status,a.status) ELSE a.status END=?")
        args.append(values['status'])
    for key, op in (('from_time', '>='), ('to_time', '<=')):
        if values.get(key) is not None:
            value = values[key]
            if not isinstance(value, (int, float)) or not 0 <= value <= 32503680000:
                fail(400, 'invalid_time', '时间范围无效')
            where.append('a.created_at' + op + '?');args.append(value)
    if values.get('from_time') and values.get('to_time') and values['from_time'] > values['to_time']:
        fail(400, 'invalid_time', '开始时间不能晚于结束时间')
    return ' AND '.join(where), args


def attach_request_audit(api, db):
    @api.get('/api/v1/admin/request-audits/options')
    def options(request: Request):
        admin(db, request)
        return {'users': db.all('SELECT id,username,display_name FROM users ORDER BY username'),
                'tenants': db.all('SELECT id,name FROM tenants ORDER BY name'),
                'applications': db.all('SELECT id,name,tenant_id FROM applications ORDER BY name'),
                'keys': db.all('SELECT id,name,suffix,application_id FROM api_keys ORDER BY created_at DESC'),
                'models': db.all('SELECT alias FROM models ORDER BY alias')}

    @api.get('/api/v1/admin/request-audits')
    def listing(request: Request, page: int = Query(1, ge=1, le=1000000), user_id: str | None = None,
                tenant_id: str | None = None, application_id: str | None = None, key_id: str | None = None,
                model: str | None = None, status: str | None = None, request_id: str | None = None,
                trace_id: str | None = None, stage: str | None = None,
                from_time: float | None = None, to_time: float | None = None):
        admin(db, request)
        values = {k: v for k, v in locals().copy().items() if k not in ('request', 'page')}
        where, args = filters(values)
        base = SELECT + ' WHERE ' + where
        total = db.one('SELECT count(*) n FROM (' + base + ')', args)['n']
        rows = db.all(base + ' ORDER BY a.created_at DESC,a.id DESC LIMIT 20 OFFSET ?', (*args, (page - 1) * 20))
        summary = db.one('''SELECT count(*) requests,COALESCE(sum(input_tokens+output_tokens),0) tokens,
           COALESCE(sum(charged_micro),0) charged_micro,COALESCE(sum(effective_status='succeeded'),0) succeeded,
           COALESCE(sum(capture_status='partial'),0) partial_contents FROM (''' + base + ')', args)
        return {'items': [public_record(r) for r in rows], 'page': page, 'page_size': 20, 'total': total, 'summary': summary}

    @api.get('/api/v1/admin/request-audit-settings')
    def settings(request: Request):
        admin(db, request)
        row = db.one('SELECT * FROM request_recording_settings WHERE id=1')
        row.update(retention='manual', failure_policy='continue', mode='current_turn', disk_free_bytes=shutil.disk_usage(db.settings.data_dir).free,
                   partial_contents=db.one("SELECT count(*) n FROM request_audits WHERE capture_status='partial'")['n'])
        return row

    @api.patch('/api/v1/admin/request-audit-settings')
    def update_settings(request: Request, body: dict = Body(...)):
        user = admin(db, request)
        if not body or set(body) - {'enabled', 'max_bytes'}:
            fail(400, 'invalid_settings', '仅支持记录开关和容量')
        if 'enabled' in body and type(body['enabled']) is not bool:
            fail(400, 'invalid_settings', 'enabled需要布尔值')
        if 'max_bytes' in body and (type(body['max_bytes']) is not int or not 1048576 <= body['max_bytes'] <= 2 * 1024**3):
            fail(400, 'invalid_settings', '容量需为1MiB至2GiB')
        with db.transaction() as c:
            c.execute('UPDATE request_recording_settings SET ' + ','.join(k + '=?' for k in body) + ',revision=revision+1,updated_at=? WHERE id=1', (*body.values(), time.time()))
            audit(c, user['id'], None, 'request_recording.settings', 'global', dumps(body))
        return {'ok': True}

    def cleanup_selection(body):
        if set(body) - {'user_id', 'before', 'ids', 'selection_hash'}:
            fail(400, 'invalid_cleanup', '清理条件无效')
        where, args = ["a.capture_bytes>0", "a.status!='running'"], []
        if body.get('user_id'):
            if not isinstance(body['user_id'],str) or len(body['user_id'])>80:
                fail(400,'invalid_cleanup','用户筛选无效')
            where.append('a.user_id=?');args.append(body['user_id'])
        if body.get('before') is not None:
            if type(body['before']) not in (int, float) or not 0 <= body['before'] <= 32503680000:
                fail(400, 'invalid_cleanup', '截止时间无效')
            where.append('a.created_at<=?');args.append(body['before'])
        ids = body.get('ids')
        if ids is not None:
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(x, str) or len(x) > 80 for x in ids):
                fail(400, 'invalid_cleanup', '每批选择1至100条记录')
            where.append('a.id IN (' + ','.join('?' * len(ids)) + ')');args.extend(ids)
        elif not body.get('before'):
            fail(400, 'cleanup_scope_required', '请选择记录或截止时间')
        rows = db.all('SELECT a.id,a.capture_bytes FROM request_audits a WHERE ' + ' AND '.join(where) + ' ORDER BY a.created_at,a.id LIMIT 100', args)
        return rows, digest(dumps(rows))

    @api.post('/api/v1/admin/request-audits/cleanup/preview')
    def preview(request: Request, body: dict = Body(...)):
        admin(db, request)
        rows, checksum = cleanup_selection(body)
        return {'count': len(rows), 'bytes': sum(r['capture_bytes'] for r in rows), 'ids': [r['id'] for r in rows],
                'selection_hash': checksum, 'batch_limit': 100, 'scope': 'contents_only'}

    @api.post('/api/v1/admin/request-audits/cleanup')
    def cleanup(request: Request, body: dict = Body(...)):
        user = admin(db, request)
        rows, checksum = cleanup_selection(body)
        if not body.get('selection_hash') or body['selection_hash'] != checksum:
            fail(409, 'cleanup_selection_changed', '请重新预览清理范围')
        total = 0
        for row in rows:
            with db.transaction() as c:
                c.execute("UPDATE request_audits SET capture_status='cleared',capture_reason='manually_cleared' WHERE id=?", (row['id'],))
            # Small transactions bound contention; a retry resumes the same immutable set.
            while True:
                with db.transaction() as c:
                    chunks = c.execute('SELECT rowid,byte_size FROM request_capture_chunks WHERE audit_id=? LIMIT 128', (row['id'],)).fetchall()
                    if not chunks:
                        c.execute("UPDATE request_audits SET capture_status='cleared',capture_reason='manually_cleared',capture_bytes=0 WHERE id=?", (row['id'],))
                        break
                    size = sum(r['byte_size'] for r in chunks)
                    c.executemany('DELETE FROM request_capture_chunks WHERE rowid=?', [(r['rowid'],) for r in chunks])
                    c.execute('UPDATE request_recording_settings SET used_bytes=used_bytes-? WHERE id=1', (size,))
                    c.execute('UPDATE request_audits SET capture_bytes=capture_bytes-? WHERE id=?', (size, row['id']))
                    total += size
            with db.transaction() as c:
                audit(c, user['id'], None, 'request_contents.clear', row['id'], str(row['capture_bytes']))
        return {'ok': True, 'cleared': len(rows), 'bytes': total, 'financial_records_preserved': True}

    def record(pk):
        row = db.one(SELECT + ' WHERE a.id=?', (pk,))
        if not row:
            fail(404, 'request_audit_not_found', '请求记录不存在')
        return row

    @api.get('/api/v1/admin/request-audits/{pk}')
    def detail(pk: str, request: Request):
        admin(db, request)
        row = record(pk)
        result = public_record(row)
        result['sections'] = db.all('SELECT section,count(*) chunks,sum(byte_size) bytes FROM request_capture_chunks WHERE audit_id=? GROUP BY section', (pk,))
        result['attempts'] = db.all('''SELECT a.attempt_no,a.status,a.error_code,a.http_status,a.latency_ms,a.created_at,a.dispatched_at,
              u.name upstream_name FROM upstream_attempts a JOIN upstreams u ON u.id=a.upstream_id WHERE request_id=? ORDER BY attempt_no''', (row['request_id'],)) if row['request_id'] else []
        return result

    @api.get('/api/v1/admin/request-audits/{pk}/content')
    def contents(pk: str, request: Request, section: str = Query('input', pattern='^(input|output|tools)$'),
                 cursor: int = Query(0, ge=0, le=2147483647)):
        user = admin(db, request)
        row = record(pk)
        with db.transaction() as c:
            audit(c, user['id'], row['tenant_id'], 'request_contents.view', pk, section + ':' + str(cursor))
        chunks = [] if row['capture_status'] == 'cleared' else db.all('SELECT seq,ciphertext FROM request_capture_chunks WHERE audit_id=? AND section=? AND seq>=? ORDER BY seq LIMIT 9', (pk, section, cursor))
        text = []
        try:
            for chunk in chunks[:8]:
                data = db.settings.unseal(chunk['ciphertext'])
                if data.get('audit_id') != pk or data.get('section') != section or data.get('seq') != chunk['seq']:
                    raise ValueError('capture binding mismatch')
                text.append(data['text'])
        except (InvalidToken, ValueError, TypeError, KeyError):
            fail(409, 'capture_unreadable', '内容无法解密或已损坏，请核对备份与主密钥')
        return {'section': section, 'text': ''.join(text), 'next_cursor': chunks[8]['seq'] if len(chunks) > 8 else None,
                'capture_status': row['capture_status'], 'capture_reason': row['capture_reason']}
