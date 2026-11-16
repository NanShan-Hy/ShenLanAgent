"""Payment boundary. No online provider is enabled without a reviewed implementation."""
from typing import Protocol

from .security import fail


class PaymentAdapter(Protocol):
    def checkout(self, order: dict) -> dict: ...
    def verify_event(self, headers: dict, body: bytes) -> dict: ...
    def refund(self, order: dict, amount_fen: int, request_id: str) -> dict: ...


def providers():
    return [{'id': 'manual', 'name': '人工核款', 'enabled': True},
            {'id': 'wechat', 'name': '微信支付', 'enabled': False},
            {'id': 'alipay', 'name': '支付宝', 'enabled': False},
            {'id': 'stripe', 'name': 'Stripe', 'enabled': False}]


def checkout_for(provider, order):
    if provider != 'manual':
        fail(501, 'payment_provider_disabled', '该在线支付渠道尚未接入；请使用人工核款流程')
    return {'provider': 'manual', 'order_id': order['id'], 'amount_fen': order['amount_fen'],
            'status': 'awaiting_verification', 'instructions': '联系运营管理员确认线下付款方式，付款时备注订单编号；到账核验后入账。'}
