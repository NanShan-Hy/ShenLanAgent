from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Model):
    username: str = Field(min_length=3, max_length=80, pattern=r'^[a-zA-Z0-9_.@-]+$')
    password: str = Field(min_length=10, max_length=256)


class UserInput(Login):
    display_name: str = Field(min_length=1, max_length=80)
    invitation: str = Field(default='', max_length=200)


class TenantInput(Model):
    name: str = Field(min_length=1, max_length=80)


class BillingInput(Model):
    billing_mode: Literal['prepaid', 'package', 'internal']
    wallet_overage: bool = False


class TenantAdminUpdate(Model):
    active: bool | None = None
    billing_mode: Literal['prepaid', 'package', 'internal'] | None = None
    wallet_overage: bool | None = None
    @model_validator(mode='after')
    def not_empty(self):
        if self.active is None and self.billing_mode is None and self.wallet_overage is None:
            raise ValueError('至少提供一项空间设置')
        return self


class ApplicationInput(TenantInput):
    active: bool = True


class Limits(Model):
    rpm: int | None = Field(default=None, ge=0, le=100000)
    tpm: int | None = Field(default=None, ge=0, le=1000000000)
    concurrency: int | None = Field(default=None, ge=0, le=1000)
    daily_tokens: int | None = Field(default=None, ge=0, le=10000000000)
    monthly_tokens: int | None = Field(default=None, ge=0, le=100000000000)
    daily_cost: int | None = Field(default=None, ge=0, le=10**15)
    monthly_cost: int | None = Field(default=None, ge=0, le=10**15)
    per_request_cost: int | None = Field(default=None, ge=0, le=10**15)
    max_input: int | None = Field(default=None, ge=0, le=10000000)
    max_output: int | None = Field(default=None, ge=0, le=1000000)
    timeout: int | None = Field(default=None, ge=1, le=300)


class KeyInput(TenantInput):
    application_id: str = Field(min_length=1, max_length=40)
    models: list[str] = Field(default_factory=lambda: ['*'], max_length=100)
    ips: list[str] = Field(default_factory=list, max_length=20)
    expires_at: int | None = Field(default=None, ge=0)
    limits: Limits = Field(default_factory=Limits)
    @field_validator('ips')
    @classmethod
    def validate_ips(cls, value):
        for item in value:
            ipaddress.ip_network(item, strict=False)
        return value


class PoolInput(TenantInput):
    kind: Literal['api'] = 'api'
    visibility: Literal['platform', 'tenant'] = 'platform'
    tenant_id: str | None = None
    quota_group: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    enabled: bool = False
    rpm: int = Field(default=60, ge=1, le=100000)
    tpm: int = Field(default=300000, ge=1, le=10**9)
    concurrency: int = Field(default=10, ge=1, le=1000)
    @model_validator(mode='after')
    def scope(self):
        if self.visibility == 'tenant' and not self.tenant_id:
            raise ValueError('租户专用池必须选择租户')
        if self.visibility == 'platform':
            self.tenant_id = None
        return self


class UpstreamInput(TenantInput):
    pool_id: str
    provider: Literal['openai', 'deepseek', 'compatible'] = 'openai'
    base_url: str = 'https://api.openai.com/v1'
    api_key: str = Field(default='', max_length=4096)
    enabled: bool = False
    weight: int = Field(default=1, ge=1, le=100)
    concurrency: int = Field(default=2, ge=1, le=100)
    @field_validator('base_url')
    @classmethod
    def base(cls, value):
        url = urlparse(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or re.search(r'[\s\\\x00-\x1f]', value)):
            raise ValueError('上游地址格式无效')
        return value.rstrip('/')


class ModelInput(Model):
    alias: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_.:/-]+$')
    pool_id: str
    provider: Literal['openai', 'deepseek', 'compatible', 'account_pool'] = 'openai'
    upstream_model: str = Field(min_length=1, max_length=120)
    protocols: list[Literal['chat', 'responses', 'compact']] = Field(default_factory=lambda: ['chat'], min_length=1, max_length=3)
    enabled: bool = False
    context_limit: int = Field(default=131072, ge=2048, le=2000000)
    max_output: int = Field(default=8192, ge=1, le=100000)
    input_rate: int | None = Field(default=None, ge=0, le=10**12)
    cache_rate: int | None = Field(default=None, ge=0, le=10**12)
    output_rate: int | None = Field(default=None, ge=0, le=10**12)
    @model_validator(mode='after')
    def valid(self):
        if self.enabled and (any(x is None for x in (self.input_rate, self.cache_rate, self.output_rate))
                             or self.input_rate + self.output_rate <= 0):
            raise ValueError('启用前需完整配置收费资费')
        if self.provider == 'deepseek' and set(self.protocols) != {'chat'}:
            raise ValueError('首期DeepSeek仅开放Chat Completions')
        if 'compact' in self.protocols and 'responses' not in self.protocols:
            raise ValueError('compact需同时支持responses')
        return self


class PlanInput(TenantInput):
    price_fen: int = Field(ge=1, le=100000000)
    credit_micro: int = Field(ge=1, le=10**15)
    days: int = Field(default=30, ge=1, le=366)
    models: list[str] = Field(min_length=1, max_length=100)
    limits: Limits = Field(default_factory=Limits)
    published: bool = False


class OrderInput(Model):
    kind: Literal['topup', 'plan']
    amount_fen: int = Field(default=0, ge=0, le=100000000)
    plan_id: str | None = None
    client_ref: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class Receipt(Model):
    reference: str = Field(min_length=5, max_length=500)


class RefundInput(Receipt):
    amount_fen: int = Field(ge=1, le=100000000)
    client_ref: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class GrantInput(Model):
    tenant_id: str
    amount_micro: int = Field(ge=1, le=10**15)
    note: str = Field(min_length=5, max_length=500)
    client_ref: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    days: int = Field(default=30, ge=1, le=366)


class ReconcileInput(Receipt):
    input_tokens: int = Field(ge=0, le=10000000)
    output_tokens: int = Field(ge=0, le=10000000)
    cached_tokens: int = Field(default=0, ge=0, le=10000000)
