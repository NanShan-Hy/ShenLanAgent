from __future__ import annotations

import json
import time

from fastapi import Body, Query, Request

from .billing import active_subscription, plan_available, wallet
from .core import audit, dumps, uid
from .models import OrderInput
from .security import fail, tenant_access


def attach_commerce(api, db, access):
    from .app import page_query

    @api.get('/api/v1/wallet')
    def get_wallet(request: Request, tenant_id: str | None = None, page: int = Query(1, ge=1)):
        user, tenant = access(request, tenant_id, 'admin')
        with db.connect() as c:
            result = wallet(c, tenant['id'])
            result['subscription'] = active_subscription(c, tenant['id'])
            if result['subscription']:
                result['subscription']['snapshot'] = json.loads(result['subscription']['snapshot'])
        result['ledger'] = page_query(db, 'SELECT * FROM ledger WHERE tenant_id=? ORDER BY id DESC', (tenant['id'],), page)
        result['billing_mode'] = tenant['billing_mode']
        result['wallet_overage'] = bool(tenant['wallet_overage'])
        result['unit'] = 'micro_cny'
        return result

    @api.get('/api/v1/plans')
    def plans(request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id)
        rows = db.all('SELECT * FROM plans WHERE published=1 ORDER BY price_fen,id')
        for row in rows:
            row['models'], row['limits'] = json.loads(row['models']), json.loads(row['limits'])
        with db.connect() as c:
            return [row for row in rows if plan_available(c, row, tenant['id'])]

    @api.get('/api/v1/orders')
    def orders(request: Request, tenant_id: str | None = None, page: int = Query(1, ge=1)):
        user, tenant = access(request, tenant_id, 'admin')
        return page_query(db, 'SELECT * FROM orders WHERE tenant_id=? ORDER BY created_at DESC', (tenant['id'],), page)

    @api.post('/api/v1/orders', status_code=201)
    def add_order(body: OrderInput, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'admin')
        pk = uid()
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'admin')
            previous = c.execute('SELECT * FROM orders WHERE tenant_id=? AND client_ref=?', (tenant['id'], body.client_ref)).fetchone()
            if previous:
                old = json.loads(previous['snapshot'])
                if (previous['kind'] != body.kind or (body.kind == 'topup' and previous['amount_fen'] != body.amount_fen)
                        or (body.kind == 'plan' and old.get('id') != body.plan_id)):
                    fail(409, 'idempotency_conflict', '订单幂等编号对应不同请求')
                return dict(previous)
            snapshot = {}
            if body.kind == 'plan':
                plan = c.execute('SELECT * FROM plans WHERE id=? AND published=1', (body.plan_id,)).fetchone()
                if not plan:
                    fail(404, 'plan_not_found', '套餐不存在或已下架')
                if active_subscription(c, tenant['id']):
                    fail(409, 'active_plan_exists', '当前已有有效套餐')
                snapshot = dict(plan)
                snapshot['limits'], snapshot['models'] = json.loads(plan['limits']), json.loads(plan['models'])
                if not plan_available(c, snapshot, tenant['id']):
                    fail(403, 'plan_model_forbidden', '当前空间没有套餐模型的使用权限')
                amount = plan['price_fen']
            else:
                if body.amount_fen < 1:
                    fail(400, 'invalid_amount', '充值金额至少为1分')
                if body.plan_id is not None:
                    fail(400, 'invalid_order', '充值订单不接受套餐ID')
                amount = body.amount_fen
            c.execute('''INSERT INTO orders(id,tenant_id,user_id,kind,amount_fen,client_ref,snapshot,created_at)
                         VALUES(?,?,?,?,?,?,?,?)''',
                      (pk, tenant['id'], user['id'], body.kind, amount, body.client_ref, dumps(snapshot), time.time()))
            audit(c, user['id'], tenant['id'], 'order.create', pk)
            return dict(c.execute('SELECT * FROM orders WHERE id=?', (pk,)).fetchone())

    @api.post('/api/v1/orders/{pk}/cancel')
    def cancel_order(pk: str, request: Request, tenant_id: str | None = None):
        user, tenant = access(request, tenant_id, 'admin')
        with db.transaction() as c:
            tenant_access(c, user['id'], tenant['id'], 'admin')
            row = c.execute('SELECT * FROM orders WHERE id=? AND tenant_id=?', (pk, tenant['id'])).fetchone()
            if not row:
                fail(404, 'order_not_found', '订单不存在')
            if row['status'] != 'pending':
                fail(409, 'order_state', '仅待核款订单可取消')
            c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (pk,))
            audit(c, user['id'], tenant['id'], 'order.cancel', pk)
        return {'ok': True}

    @api.get('/api/v1/payments/providers')
    def payment_providers(request: Request):
        from .payments import providers
        access(request)
        return providers()

    @api.post('/api/v1/payments/{provider}/checkout')
    def checkout(provider: str, request: Request, body: dict = Body(...), tenant_id: str | None = None):
        from .payments import checkout_for
        user, tenant = access(request, tenant_id, 'admin')
        row = db.one('SELECT * FROM orders WHERE id=? AND tenant_id=?', (body.get('order_id'), tenant['id']))
        if not row or row['status'] != 'pending':
            fail(409, 'order_state', '需要本空间的待核款订单')
        return checkout_for(provider, row)

