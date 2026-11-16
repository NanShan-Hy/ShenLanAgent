from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .core import audit, dumps, uid
from .security import fail, model_allowed, GatewayError

ACTIVE = ('running', 'unknown')
QUOTA_LOG = logging.getLogger('aigateway.quota')


def ledger(c, tenant, bucket, event, kind, delta=0, reserve_delta=0,
           request=None, order=None, actor=None, note=''):
    c.execute('''INSERT INTO ledger(tenant_id,bucket_id,event,kind,delta,reserve_delta,request_id,
                 order_id,actor_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (tenant, bucket, event, kind, delta, reserve_delta, request, order, actor, note, time.time()))


def new_bucket(c, tenant, kind, source, amount, *, actor=None, order=None, expires=None, models=None, note=''):
    pk = uid()
    c.execute('INSERT INTO buckets VALUES(?,?,?,?,?,?,0,?,?,?)',
              (pk, tenant, kind, source, amount, amount, dumps(models or ['*']), expires, time.time()))
    ledger(c, tenant, pk, 'credit:' + kind + ':' + source, 'credit', delta=amount, actor=actor, order=order, note=note)
    return pk


def wallet(c, tenant):
    now = time.time()
    rows = [dict(r) for r in c.execute('SELECT * FROM buckets WHERE tenant_id=? ORDER BY created_at DESC', (tenant,))]
    totals = {kind: {'remaining': 0, 'reserved': 0, 'available': 0} for kind in ('cash', 'package', 'grant')}
    for r in rows:
        expired = r['expires_at'] is not None and r['expires_at'] <= now
        r['expired'] = expired
        r['available'] = 0 if expired else r['remaining'] - r['reserved']
        item = totals[r['kind']]
        item['remaining'] += 0 if expired else r['remaining']
        item['reserved'] += r['reserved']
        item['available'] += r['available']
        r['models'] = json.loads(r['models'])
    return {'totals': totals, 'buckets': rows}


def active_subscription(c, tenant):
    row = c.execute("SELECT * FROM subscriptions WHERE tenant_id=? AND status='active' AND expires_at>? ORDER BY starts_at DESC LIMIT 1",
                    (tenant, time.time())).fetchone()
    return dict(row) if row else None


def plan_available(c, snapshot, tenant_id):
    aliases = snapshot['models']
    key = {'tenant_id': tenant_id, 'models': '["*"]'}
    models = c.execute('SELECT * FROM models WHERE enabled=1').fetchall()
    available = set()
    for model in models:
        try:
            model_allowed(c, key, model)
            available.add(model['alias'])
        except GatewayError:
            pass
    return bool(available) if '*' in aliases else bool(aliases) and set(aliases).issubset(available)


def approve_order(db, order_id, actor_id, reference):
    with db.transaction() as c:
        row = c.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not row:
            fail(404, 'order_not_found', '订单不存在')
        if row['status'] == 'paid':
            return {'ok': True, 'already_paid': True}
        if row['status'] != 'pending':
            fail(409, 'order_state', '订单状态不允许入账')
        tenant = c.execute('SELECT * FROM tenants WHERE id=? AND active=1', (row['tenant_id'],)).fetchone()
        if not tenant:
            fail(409, 'tenant_inactive', '空间已停用')
        if row['kind'] == 'topup':
            new_bucket(c, row['tenant_id'], 'cash', row['id'], row['amount_fen'] * 10000,
                       actor=actor_id, order=row['id'], note=reference)
        else:
            if active_subscription(c, row['tenant_id']):
                fail(409, 'active_plan_exists', '当前已有有效套餐，请到期后再购买')
            snapshot, now = json.loads(row['snapshot']), time.time()
            if not plan_available(c, snapshot, row['tenant_id']):
                fail(409, 'plan_models_unavailable', '当前空间无法使用套餐模型，请先核对资源授权')
            expires = now + snapshot['days'] * 86400
            c.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?)',
                      (uid(), row['tenant_id'], row['id'], row['snapshot'], now, expires, 'active'))
            new_bucket(c, row['tenant_id'], 'package', row['id'], snapshot['credit_micro'],
                       actor=actor_id, order=row['id'], expires=expires, models=snapshot['models'], note=reference)
            c.execute("UPDATE tenants SET billing_mode='package' WHERE id=?", (row['tenant_id'],))
        c.execute("UPDATE orders SET status='paid',reference=?,approved_by=?,approved_at=? WHERE id=?",
                  (reference, actor_id, time.time(), row['id']))
        audit(c, actor_id, row['tenant_id'], 'order.approve', row['id'])
        return {'ok': True, 'already_paid': False}


def refund_order(db, order_id, actor_id, body):
    with db.transaction() as c:
        existing = c.execute('SELECT * FROM refunds WHERE client_ref=?', (body.client_ref,)).fetchone()
        if existing:
            if (existing['order_id'], existing['amount_fen'], existing['reference']) != (order_id, body.amount_fen, body.reference):
                fail(409, 'idempotency_conflict', '退款幂等编号对应不同请求')
            return dict(existing)
        row = c.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not row or row['status'] not in ('paid', 'partially_refunded'):
            fail(409, 'order_state', '原订单不可退款')
        bucket = c.execute('SELECT * FROM buckets WHERE source_id=? AND kind=?',
                           (row['id'], 'cash' if row['kind'] == 'topup' else 'package')).fetchone()
        refunded = c.execute('SELECT COALESCE(sum(amount_fen),0) FROM refunds WHERE order_id=?', (row['id'],)).fetchone()[0]
        if body.amount_fen + refunded > row['amount_fen']:
            fail(409, 'refund_exceeds_payment', '退款超过原实收金额')
        if row['kind'] == 'plan':
            if (body.amount_fen != row['amount_fen'] or bucket['reserved'] or bucket['remaining'] != bucket['original']
                    or bucket['expires_at'] <= time.time()):
                fail(409, 'plan_refund_unavailable', '套餐仅支持有效期内完全未使用时全额退款')
            amount = bucket['remaining']
            c.execute("UPDATE subscriptions SET status='refunded' WHERE order_id=?", (row['id'],))
        else:
            amount = body.amount_fen * 10000
        if amount > bucket['remaining'] - bucket['reserved']:
            fail(409, 'refund_exceeds_available', '余额已消费或冻结，不能退还')
        c.execute('UPDATE buckets SET remaining=remaining-? WHERE id=?', (amount, bucket['id']))
        pk = uid()
        c.execute('INSERT INTO refunds VALUES(?,?,?,?,?,?,?,?)',
                  (pk, row['id'], row['tenant_id'], body.amount_fen, body.reference, actor_id, body.client_ref, time.time()))
        ledger(c, row['tenant_id'], bucket['id'], 'refund:' + pk, 'refund', delta=-amount,
               order=row['id'], actor=actor_id, note=body.reference)
        status = 'refunded' if refunded + body.amount_fen == row['amount_fen'] else 'partially_refunded'
        c.execute('UPDATE orders SET status=? WHERE id=?', (status, row['id']))
        audit(c, actor_id, row['tenant_id'], 'order.refund', row['id'], str(body.amount_fen))
        return dict(c.execute('SELECT * FROM refunds WHERE id=?', (pk,)).fetchone())


def cost(price, input_tokens, output_tokens, cached_tokens=0):
    numerator = ((input_tokens - cached_tokens) * price['input_rate']
                 + cached_tokens * price['cache_rate'] + output_tokens * price['output_rate'])
    return (numerator + 999999) // 1000000


def periods(now):
    current = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
    return (current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
            current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def check_limits(c, predicate, args, limits, now, estimate, charge, input_estimate, max_output, *, scope='unspecified'):
    def maximum(key, used, extra=0):
        if limits.get(key) is not None and used + extra > limits[key]:
            # Only controlled scope names and numeric budgets; never request content,
            # account identifiers, credentials, or the SQL predicate/arguments.
            QUOTA_LOG.warning('quota_rejected scope=%s dimension=%s used=%d requested=%d limit=%d',
                              scope, key, used, extra, limits[key])
            fail(429, 'quota_exceeded', '已达到' + {'rpm':'每分钟请求', 'tpm':'每分钟Token',
                 'concurrency':'并发', 'daily_tokens':'每日Token', 'monthly_tokens':'每月Token',
                 'daily_cost':'每日费用', 'monthly_cost':'每月费用', 'per_request_cost':'单次费用',
                 'max_input':'输入长度', 'max_output':'最大输出'}[key] + '上限')
    maximum('per_request_cost', 0, charge)
    maximum('max_input', 0, input_estimate)
    maximum('max_output', 0, max_output)
    if limits.get('concurrency') is not None:
        n = c.execute('SELECT count(*) FROM requests r WHERE ' + predicate + " AND status='running'", args).fetchone()[0]
        maximum('concurrency', n, 1)
    day, month = periods(now)
    for since, token_name, cost_name in ((now - 60, 'tpm', None), (day, 'daily_tokens', 'daily_cost'), (month, 'monthly_tokens', 'monthly_cost')):
        if limits.get(token_name) is None and (not cost_name or limits.get(cost_name) is None) and not (token_name == 'tpm' and limits.get('rpm') is not None):
            continue
        row = c.execute('''SELECT count(*),COALESCE(sum(CASE WHEN status IN ('running','unknown')
          THEN estimate_tokens ELSE input_tokens+output_tokens END),0),COALESCE(sum(CASE WHEN status IN ('running','unknown')
          THEN reserved_micro ELSE charged_micro END),0) FROM requests r WHERE ''' + predicate + ' AND created_at>=?', (*args, since)).fetchone()
        if token_name == 'tpm':
            maximum('rpm', row[0], 1)
        maximum(token_name, row[1], estimate)
        if cost_name:
            maximum(cost_name, row[2], charge)


def reserve_buckets(c, request, tenant, alias, amount, subscription):
    now = time.time()
    mode, overage = tenant['billing_mode'], tenant['wallet_overage']
    if mode == 'package' and not subscription:
        fail(402, 'package_expired', '套餐未生效或已到期，请购买套餐或切换预付余额模式')
    candidates = [dict(r) for r in c.execute('''SELECT * FROM buckets WHERE tenant_id=? AND remaining>reserved
                    AND (expires_at IS NULL OR expires_at>?) ORDER BY
                    CASE kind WHEN 'package' THEN 0 WHEN 'grant' THEN 1 ELSE 2 END,
                    COALESCE(expires_at,1e20),created_at,id''', (tenant['id'], now))]
    left = amount
    for bucket in candidates:
        if mode == 'internal' and bucket['kind'] != 'grant':
            continue
        if mode == 'prepaid' and bucket['kind'] == 'package':
            continue
        if mode == 'package' and bucket['kind'] == 'cash' and not overage:
            continue
        allowed = json.loads(bucket['models'])
        if '*' not in allowed and alias not in allowed:
            continue
        take = min(left, bucket['remaining'] - bucket['reserved'])
        if take:
            c.execute('UPDATE buckets SET reserved=reserved+? WHERE id=?', (take, bucket['id']))
            c.execute('INSERT INTO allocations VALUES(?,?,?)', (request, bucket['id'], take))
            ledger(c, tenant['id'], bucket['id'], 'hold:' + request + ':' + bucket['id'], 'hold',
                   reserve_delta=take, request=request)
            left -= take
        if not left:
            return
    if left:
        fail(402, 'insufficient_quota', '可用余额或套餐额度不足以覆盖本次最大消费；可减少最大输出')


def settle(db, request_id, usage=None, *, status='succeeded', error='', provider_id='', actor=None, note='', http_status=None):
    with db.transaction() as c:
        row = c.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if not row or row['status'] not in ACTIVE:
            return False
        from .operations import close_request
        close_request(c, request_id, status, error, http_status, provider_id)
        if usage is None:
            used_input = used_output = cached = actual = charge = 0
        else:
            used_input, used_output, cached = usage
            if min(usage) < 0 or cached > used_input or max(usage) > 100000000:
                raise ValueError('invalid usage')
            actual = cost(json.loads(row['price']), used_input, used_output, cached)
            charge = min(actual, row['reserved_micro'])
        remaining = charge
        allocations = c.execute('''SELECT a.*,b.created_at,b.kind,b.expires_at FROM allocations a JOIN buckets b ON b.id=a.bucket_id
                                WHERE a.request_id=? ORDER BY CASE b.kind WHEN 'package' THEN 0 WHEN 'grant' THEN 1 ELSE 2 END,
                                COALESCE(b.expires_at,1e20),b.created_at,b.id''', (request_id,)).fetchall()
        for item in allocations:
            take = min(remaining, item['amount'])
            c.execute('UPDATE buckets SET reserved=reserved-?,remaining=remaining-? WHERE id=?',
                      (item['amount'], take, item['bucket_id']))
            ledger(c, row['tenant_id'], item['bucket_id'], 'settle:' + request_id + ':' + item['bucket_id'],
                   'settle', delta=-take, reserve_delta=-item['amount'], request=request_id, actor=actor, note=note)
            remaining -= take
        if remaining:
            raise RuntimeError('allocation inconsistency')
        c.execute('''UPDATE requests SET status=?,input_tokens=?,output_tokens=?,cached_tokens=?,charged_micro=?,
                   computed_micro=?,provider_id=?,error_code=?,finished_at=?,heartbeat=? WHERE id=?''',
                  (status, used_input, used_output, cached, charge, actual, provider_id[:160], error,
                   time.time(), time.time(), request_id))
        if actual > row['reserved_micro']:
            audit(c, None, row['tenant_id'], 'billing.reservation_exceeded', request_id, str(actual - charge))
        if actor:
            audit(c, actor, row['tenant_id'], 'request.reconcile', request_id, note)
        return True


def mark_unknown(db, request_id, code='upstream_outcome_unknown', *, http_status=None, provider_id=''):
    with db.transaction() as c:
        from .operations import close_request
        close_request(c, request_id, 'unknown', code, http_status, provider_id)
        c.execute("UPDATE requests SET status='unknown',error_code=?,finished_at=?,heartbeat=? WHERE id=? AND status='running'",
                  (code, time.time(), time.time(), request_id))


def maintenance(db, *, restart=False, now=None):
    now = time.time() if now is None else now
    with db.transaction() as c:
        from .operations import recover
        recover(c, now, restart)
        if restart:
            c.execute("UPDATE requests SET status='unknown',error_code='service_restarted',finished_at=?,heartbeat=? WHERE status='running'", (now, now))
        else:
            c.execute("UPDATE requests SET status='unknown',error_code='request_heartbeat_lost',finished_at=? WHERE status='running' AND heartbeat<?", (now, now - 360))
        rows = c.execute("SELECT id FROM requests WHERE status='unknown' AND finished_at<=?", (now - 86400,)).fetchall()
    for row in rows:
        settle(db, row['id'], status='released', error='reconciliation_expired')


def ledger_check(db):
    with db.connect() as c:
        rows = c.execute('''SELECT b.id,b.remaining,b.reserved,COALESCE(sum(l.delta),0) balance,
                   COALESCE(sum(l.reserve_delta),0) holds FROM buckets b LEFT JOIN ledger l ON l.bucket_id=b.id GROUP BY b.id''').fetchall()
        bad = [r['id'] for r in rows if r['remaining'] != r['balance'] or r['reserved'] != r['holds']]
        orphan = c.execute('''SELECT b.id FROM buckets b WHERE b.reserved != COALESCE((SELECT sum(a.amount)
                  FROM allocations a JOIN requests r ON r.id=a.request_id WHERE a.bucket_id=b.id AND r.status IN ('running','unknown')),0)''').fetchall()
        return {'ok': not bad and not orphan, 'buckets': len(rows), 'mismatches': bad, 'reservation_mismatches': [r['id'] for r in orphan]}
