from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

BASE = Path('/home/nanshan/workspace')
DEFAULT_CONFIG = BASE / 'services/ai-gateway/config/settings.json'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def uid():
    return uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000)
    return salt + '$' + base64.b64encode(value).decode()


def password_matches(password, stored):
    try:
        salt, value = stored.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000)
        return hmac.compare_digest(base64.b64encode(actual).decode(), value)
    except (ValueError, AttributeError, TypeError):
        return False


def private_write(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    path.chmod(0o600)


@dataclass
class Settings:
    data_dir: Path
    key_file: Path
    bind: str = '127.0.0.1'
    port: int = 8094
    public_url: str = 'http://127.0.0.1:8094'
    max_concurrency: int = 10
    max_users: int = 50
    allow_private_upstreams: bool = False
    public_registration: bool = False
    min_free_bytes: int = 3 * 1024**3
    account_pool_config: Path | None = None

    @classmethod
    def load(cls, path=None):
        path = Path(path or os.environ.get('AI_GATEWAY_CONFIG', DEFAULT_CONFIG))
        raw = json.loads(path.read_text())
        for field in ('data_dir', 'key_file'):
            raw[field] = Path(raw[field])
        if raw.get('account_pool_config') is not None:
            raw['account_pool_config'] = Path(raw['account_pool_config'])
        return cls(**raw)

    def prepare(self):
        from urllib.parse import urlparse
        url = urlparse(self.public_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('public_url必须是无凭据的HTTP/HTTPS地址')
        if self.public_registration and url.scheme != 'https':
            raise ValueError('公开注册需要HTTPS public_url')
        if not 1 <= self.max_concurrency <= 100 or not 1 <= self.max_users <= 10000:
            raise ValueError('服务容量配置无效')
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key_file.parent.chmod(0o700)
        if not self.key_file.exists():
            private_write(self.key_file, Fernet.generate_key().decode(), exclusive=True)
        self.key_file.chmod(0o600)
        Fernet(self.key_file.read_bytes())

    def seal(self, value):
        return Fernet(self.key_file.read_bytes()).encrypt(dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(Fernet(self.key_file.read_bytes()).decrypt(value.encode()))

    def key_hash(self, value):
        return hmac.new(self.key_file.read_bytes(), value.encode(), hashlib.sha256).hexdigest()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,
 is_admin INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(address TEXT PRIMARY KEY,n INTEGER NOT NULL,since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tenants(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
 billing_mode TEXT NOT NULL DEFAULT 'prepaid',wallet_overage INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members(
 tenant_id TEXT REFERENCES tenants(id),user_id TEXT REFERENCES users(id),role TEXT NOT NULL,
 PRIMARY KEY(tenant_id,user_id));
CREATE TABLE IF NOT EXISTS invitations(
 hash TEXT PRIMARY KEY,tenant_id TEXT REFERENCES tenants(id),role TEXT NOT NULL,created_by TEXT REFERENCES users(id),
 expires_at REAL NOT NULL,used_by TEXT REFERENCES users(id));
CREATE TABLE IF NOT EXISTS applications(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),user_id TEXT NOT NULL REFERENCES users(id),
 application_id TEXT NOT NULL REFERENCES applications(id),name TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,
 prefix TEXT NOT NULL,suffix TEXT NOT NULL,models TEXT NOT NULL DEFAULT '["*"]',ips TEXT NOT NULL DEFAULT '[]',
 active INTEGER NOT NULL DEFAULT 1,expires_at REAL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pools(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'api',visibility TEXT NOT NULL DEFAULT 'platform',
 tenant_id TEXT REFERENCES tenants(id),quota_group TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
 rpm INTEGER NOT NULL DEFAULT 60,tpm INTEGER NOT NULL DEFAULT 300000,concurrency INTEGER NOT NULL DEFAULT 10,
 cooldown_until REAL NOT NULL DEFAULT 0,approval_status TEXT NOT NULL DEFAULT 'ready',created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS upstreams(
 id TEXT PRIMARY KEY,pool_id TEXT NOT NULL REFERENCES pools(id),name TEXT NOT NULL,provider TEXT NOT NULL,
 base_url TEXT NOT NULL,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,weight INTEGER NOT NULL DEFAULT 1,
 concurrency INTEGER NOT NULL DEFAULT 2,revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS models(
 id TEXT PRIMARY KEY,alias TEXT UNIQUE NOT NULL,pool_id TEXT NOT NULL REFERENCES pools(id),provider TEXT NOT NULL,
 upstream_model TEXT NOT NULL,protocols TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
 context_limit INTEGER NOT NULL DEFAULT 131072,max_output INTEGER NOT NULL DEFAULT 8192,
 input_rate INTEGER,cache_rate INTEGER,output_rate INTEGER,price_version INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS policies(scope TEXT NOT NULL,scope_id TEXT NOT NULL,limits TEXT NOT NULL,
 PRIMARY KEY(scope,scope_id));
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,price_fen INTEGER NOT NULL,credit_micro INTEGER NOT NULL,
 days INTEGER NOT NULL DEFAULT 30,models TEXT NOT NULL,limits TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),user_id TEXT NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL,amount_fen INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 client_ref TEXT NOT NULL,snapshot TEXT NOT NULL DEFAULT '{}',reference TEXT NOT NULL DEFAULT '',
 approved_by TEXT REFERENCES users(id),approved_at REAL,created_at REAL NOT NULL,UNIQUE(tenant_id,client_ref));
CREATE TABLE IF NOT EXISTS subscriptions(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),order_id TEXT UNIQUE NOT NULL REFERENCES orders(id),
 snapshot TEXT NOT NULL,starts_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS buckets(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),kind TEXT NOT NULL,source_id TEXT NOT NULL,
 original INTEGER NOT NULL CHECK(original>=0),remaining INTEGER NOT NULL CHECK(remaining>=0),
 reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0 AND reserved<=remaining),
 models TEXT NOT NULL DEFAULT '["*"]',expires_at REAL,created_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS bucket_source ON buckets(kind,source_id);
CREATE TABLE IF NOT EXISTS requests(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),user_id TEXT NOT NULL REFERENCES users(id),
 application_id TEXT NOT NULL REFERENCES applications(id),key_id TEXT NOT NULL REFERENCES api_keys(id),
 model_id TEXT NOT NULL REFERENCES models(id),pool_id TEXT NOT NULL REFERENCES pools(id),
 upstream_id TEXT NOT NULL REFERENCES upstreams(id),endpoint TEXT NOT NULL,body_hash TEXT NOT NULL,idempotency_key TEXT,
 status TEXT NOT NULL,estimate_tokens INTEGER NOT NULL,reserved_micro INTEGER NOT NULL,price TEXT NOT NULL,
 input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,cached_tokens INTEGER NOT NULL DEFAULT 0,
 charged_micro INTEGER NOT NULL DEFAULT 0,computed_micro INTEGER NOT NULL DEFAULT 0,
 provider_id TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,
 heartbeat REAL NOT NULL,finished_at REAL,UNIQUE(tenant_id,key_id,idempotency_key));
CREATE INDEX IF NOT EXISTS request_created ON requests(created_at);
CREATE INDEX IF NOT EXISTS request_owner ON requests(tenant_id,created_at);
CREATE INDEX IF NOT EXISTS request_state ON requests(status,heartbeat);
CREATE INDEX IF NOT EXISTS request_user ON requests(user_id,created_at);
CREATE INDEX IF NOT EXISTS request_application ON requests(application_id,created_at);
CREATE INDEX IF NOT EXISTS request_key ON requests(key_id,created_at);
CREATE INDEX IF NOT EXISTS request_upstream ON requests(upstream_id,status);
CREATE INDEX IF NOT EXISTS request_pool ON requests(pool_id,created_at);
CREATE TABLE IF NOT EXISTS affinities(key_id TEXT REFERENCES api_keys(id),model_id TEXT REFERENCES models(id),
 session_hash TEXT NOT NULL,upstream_id TEXT NOT NULL REFERENCES upstreams(id),created_at REAL NOT NULL,
 last_used REAL NOT NULL,PRIMARY KEY(key_id,model_id,session_hash));
CREATE TABLE IF NOT EXISTS allocations(
 request_id TEXT REFERENCES requests(id),bucket_id TEXT REFERENCES buckets(id),amount INTEGER NOT NULL,
 PRIMARY KEY(request_id,bucket_id));
CREATE TABLE IF NOT EXISTS ledger(
 id INTEGER PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),bucket_id TEXT NOT NULL REFERENCES buckets(id),
 event TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,delta INTEGER NOT NULL,reserve_delta INTEGER NOT NULL DEFAULT 0,
 request_id TEXT REFERENCES requests(id),order_id TEXT REFERENCES orders(id),actor_id TEXT REFERENCES users(id),
 note TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS refunds(
 id TEXT PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(id),tenant_id TEXT NOT NULL REFERENCES tenants(id),
 amount_fen INTEGER NOT NULL,reference TEXT NOT NULL,actor_id TEXT NOT NULL REFERENCES users(id),
 client_ref TEXT NOT NULL UNIQUE,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY,user_id TEXT,tenant_id TEXT,action TEXT NOT NULL,object_id TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
'''


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / 'gateway.sqlite3'

    def initialize(self):
        self.settings.prepare()
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.executescript(SCHEMA)
            from .migrations import migrate
            migrate(connection)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA busy_timeout=10000')
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK')
                raise

    def one(self, sql, args=()):
        with self.connect() as c:
            row = c.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.transaction() as c:
            return c.execute(sql, args).lastrowid


def audit(c, actor, tenant, action, object_id='', detail=''):
    c.execute('INSERT INTO audit(user_id,tenant_id,action,object_id,detail,created_at) VALUES(?,?,?,?,?,?)',
              (actor, tenant, action, str(object_id), detail, time.time()))


def create_user(c, username, name, password, *, admin=False):
    user_id, tenant_id, application_id, now = uid(), uid(), uid(), time.time()
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,1,?)',
              (user_id, username, name, password_hash(password), int(admin), now))
    c.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)', (tenant_id, name + ' 的空间', now))
    c.execute('INSERT INTO members VALUES(?,?,?)', (tenant_id, user_id, 'owner'))
    c.execute('INSERT INTO applications VALUES(?,?,?,1,?)', (application_id, tenant_id, '默认应用', now))
    c.execute('INSERT INTO policies VALUES(?,?,?)', ('user', user_id, dumps({'rpm': 20, 'tpm': 100000,
               'concurrency': 2, 'daily_tokens': 200000, 'timeout': 120})))
    audit(c, user_id, tenant_id, 'user.create', user_id)
    return user_id, tenant_id, application_id
