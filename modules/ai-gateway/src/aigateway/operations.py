from __future__ import annotations

import time
import math
from email.utils import parsedate_to_datetime

from .core import uid
from .security import fail

ACTIVE_ATTEMPTS = ('leased', 'sending')
THRESHOLD = 3
BASE_BACKOFF = 30
MAX_BACKOFF = 300


def ensure_runtime(c, upstream):
    row = c.execute('SELECT * FROM upstream_runtime WHERE upstream_id=?', (upstream['id'],)).fetchone()
    if not row or row['revision'] != upstream['revision']:
        c.execute('''INSERT INTO upstream_runtime(upstream_id,revision) VALUES(?,?)
            ON CONFLICT(upstream_id) DO UPDATE SET revision=excluded.revision,state='closed',failures=0,
            retry_at=0,half_open_attempt=NULL,last_error='',last_http_status=NULL,last_latency_ms=NULL,
            last_event_at=NULL,last_success_at=NULL,probe_id=NULL,probe_until=0''',
                  (upstream['id'], upstream['revision']))
        row = c.execute('SELECT * FROM upstream_runtime WHERE upstream_id=?', (upstream['id'],)).fetchone()
    return dict(row)


def eligible(c, upstream, now):
    runtime = ensure_runtime(c, upstream)
    if runtime['state'] == 'blocked':
        return False
    if runtime['state'] == 'open' and runtime['retry_at'] > now:
        return False
    if runtime['half_open_attempt']:
        leased = c.execute("SELECT 1 FROM upstream_attempts WHERE id=? AND status IN ('leased','sending')",
                           (runtime['half_open_attempt'],)).fetchone()
        if leased:
            return False
    return True


def lease_attempt(c, request_id, upstream, group, now=None):
    now = time.time() if now is None else now
    runtime = ensure_runtime(c, upstream)
    if not eligible(c, upstream, now):
        fail(503, 'upstream_circuit_open', '上游暂时熔断或等待配置修复')
    number = c.execute('SELECT COALESCE(max(attempt_no),0)+1 FROM upstream_attempts WHERE request_id=?', (request_id,)).fetchone()[0]
    pk = uid()
    c.execute('''INSERT INTO upstream_attempts(id,request_id,upstream_id,revision,attempt_no,quota_group,status,created_at)
                 VALUES(?,?,?,?,?,?,'leased',?)''',
              (pk, request_id, upstream['id'], upstream['revision'], number, group, now))
    if runtime['state'] in ('open', 'half_open'):
        c.execute("UPDATE upstream_runtime SET state='half_open',half_open_attempt=? WHERE upstream_id=?",
                  (pk, upstream['id']))
    return pk


def begin_send(db, attempt_id):
    with db.transaction() as c:
        row = c.execute('''SELECT a.*,r.state FROM upstream_attempts a JOIN upstream_runtime r ON r.upstream_id=a.upstream_id
                           WHERE a.id=? AND a.revision=r.revision''', (attempt_id,)).fetchone()
        if not row or row['status'] != 'leased' or row['state'] == 'blocked':
            fail(503, 'upstream_changed', '发送前上游状态已变化，请重新提交')
        if row['state'] == 'open':
            fail(503, 'upstream_circuit_open', '发送前上游已熔断，请稍后重试')
        c.execute("UPDATE upstream_attempts SET status='sending',dispatched_at=? WHERE id=?", (time.time(), attempt_id))


def close_attempt(c, attempt_id, status, *, error='', http_status=None, provider_id='', now=None):
    now = time.time() if now is None else now
    row = c.execute('SELECT * FROM upstream_attempts WHERE id=?', (attempt_id,)).fetchone()
    if not row or row['status'] not in ACTIVE_ATTEMPTS:
        return False
    dispatched = row['dispatched_at'] is not None
    latency = max(0, int((now - row['created_at']) * 1000))
    actual_status = status if dispatched or error == 'upstream_dns_failed' else 'cancelled'
    c.execute('''UPDATE upstream_attempts SET status=?,error_code=?,http_status=?,provider_id=?,
                 finished_at=?,latency_ms=? WHERE id=?''',
              (actual_status, error, http_status, provider_id, now, latency, attempt_id))
    runtime = c.execute('SELECT * FROM upstream_runtime WHERE upstream_id=? AND revision=?',
                        (row['upstream_id'], row['revision'])).fetchone()
    if not runtime:
        return True
    # Local cancellation/configuration changes are not evidence of provider failure.
    auth = dispatched and http_status in (401, 403)
    if auth:
        provider=c.execute('SELECT provider FROM upstreams WHERE id=?',(row['upstream_id'],)).fetchone()
        # A broker owns each account's authentication state. One expired
        # account must not disable the broker's other authorized accounts.
        auth=not provider or provider['provider']!='account_pool'
    bad = ((dispatched and ((http_status is not None and http_status >= 500)
           or error in ('upstream_connection_failed', 'upstream_dns_failed', 'upstream_outcome_unknown',
                        'upstream_usage_missing', 'upstream_deadline_exceeded', 'stream_interrupted')))
           or error == 'upstream_dns_failed')
    success = dispatched and status == 'succeeded'
    responding = dispatched and http_status is not None and 400 <= http_status < 500 and not auth
    if auth:
        state, failures, retry = 'blocked', runtime['failures'] + 1, 0
    elif runtime['state'] == 'blocked':
        state, failures, retry = 'blocked', runtime['failures'], runtime['retry_at']
    elif bad:
        failures = runtime['failures'] + 1
        state = 'open' if failures >= THRESHOLD or runtime['state'] == 'half_open' else 'closed'
        retry = now + min(MAX_BACKOFF, BASE_BACKOFF * 2 ** min(4, max(0, failures - THRESHOLD))) if state == 'open' else 0
    elif success or responding:
        state, failures, retry = 'closed', 0, 0
    elif runtime['half_open_attempt'] == attempt_id:
        state, failures, retry = 'open', runtime['failures'], now + BASE_BACKOFF
    else:
        state, failures, retry = runtime['state'], runtime['failures'], runtime['retry_at']
    half = None if runtime['half_open_attempt'] == attempt_id or state != 'half_open' else runtime['half_open_attempt']
    c.execute('''UPDATE upstream_runtime SET state=?,failures=?,retry_at=?,half_open_attempt=?,
                 last_error=?,last_http_status=?,last_latency_ms=?,last_event_at=?,
                 last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE upstream_id=? AND revision=?''',
              (state, failures, retry, half, error, http_status, latency, now, success, now, row['upstream_id'], row['revision']))
    return True


def close_request(c, request_id, status, error='', http_status=None, provider_id=''):
    rows = c.execute("SELECT id FROM upstream_attempts WHERE request_id=? AND status IN ('leased','sending')", (request_id,)).fetchall()
    for row in rows:
        close_attempt(c, row['id'], status, error=error, http_status=http_status, provider_id=provider_id)


def finish_connect_failure(db, attempt_id, code):
    with db.transaction() as c:
        close_attempt(c, attempt_id, 'failed', error=code)


def group_limits(c, group, now):
    row = c.execute('SELECT max(cooldown_until),min(rpm),min(tpm),min(concurrency) FROM pools WHERE quota_group=?',
                    (group,)).fetchone()
    if row[0] > now:
        fail(429, 'upstream_cooling_down', '上游限流分组冷却中，请稍后重试')
    attempts = c.execute('SELECT count(*) FROM upstream_attempts WHERE quota_group=? AND created_at>=?', (group, now - 60)).fetchone()[0]
    probes = c.execute('SELECT count(*) FROM upstream_probes WHERE quota_group=? AND created_at>=?', (group, now - 60)).fetchone()[0]
    if attempts + probes >= row[1]:
        fail(429, 'upstream_rate_limit', '上游共享分组已达到每分钟请求上限')
    return {'tpm': row[2], 'concurrency': row[3]}


def recover(c, now, restart=False):
    if restart:
        rows = c.execute("SELECT id FROM upstream_attempts WHERE status IN ('leased','sending')").fetchall()
        for row in rows:
            close_attempt(c, row['id'], 'unknown', error='service_restarted', now=now)
    else:
        rows = c.execute('''SELECT a.id FROM upstream_attempts a JOIN requests r ON r.id=a.request_id
                            WHERE a.status IN ('leased','sending') AND (r.status!='running' OR r.heartbeat<?)''', (now - 360,)).fetchall()
        for row in rows:
            close_attempt(c, row['id'], 'unknown', error='request_heartbeat_lost', now=now)
    probes = c.execute("SELECT id FROM upstream_probes WHERE status='running' AND (? OR created_at<?)", (restart, now - 30)).fetchall()
    for probe in probes:
        c.execute("UPDATE upstream_probes SET status='cancelled',error_code='probe_interrupted',finished_at=? WHERE id=?",
                  (now, probe['id']))
        c.execute('UPDATE upstream_runtime SET probe_id=NULL,probe_until=0 WHERE probe_id=?', (probe['id'],))


def reset_runtime(c, upstream, actor_id):
    from .core import audit
    ensure_runtime(c, upstream)
    c.execute("""UPDATE upstream_runtime SET state='closed',failures=0,retry_at=0,half_open_attempt=NULL,
                 last_error='' WHERE upstream_id=?""", (upstream['id'],))
    audit(c, actor_id, None, 'upstream.circuit_reset', upstream['id'])


def retry_delay(value):
    if not value:
        return 60
    try:
        delay = float(value)
    except (ValueError, TypeError, OverflowError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 60
    return max(6, delay) if math.isfinite(delay) else 60
