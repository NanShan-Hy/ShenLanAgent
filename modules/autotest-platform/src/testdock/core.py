from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from contextvars import ContextVar
from pathlib import Path

from cryptography.fernet import Fernet


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 600_000)
    return salt + "$" + base64.b64encode(digest).decode()


def password_matches(value: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 600_000)
        return hmac.compare_digest(base64.b64encode(actual).decode(), digest)
    except (ValueError, TypeError):
        return False


def private_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


@dataclass
class Settings:
    data_dir: Path
    key_file: Path
    secure_cookie: bool = False
    run_timeout: int = 900
    step_timeout: int = 15
    min_free_bytes: int = 3 * 1024**3
    browser_library_path: str = ""
    report_base_url: str = "http://172.16.61.128:8088"

    def __post_init__(self):
        from urllib.parse import urlparse
        url=urlparse(self.report_base_url)
        if len(self.report_base_url)>2048 or url.scheme not in ('http','https') or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError('报告基础地址必须是无嵌入凭据和查询参数的HTTP/HTTPS地址')
        self.report_base_url=self.report_base_url.rstrip('/')

    @classmethod
    def load(cls):
        config_path = Path(os.environ.get("TESTDOCK_CONFIG", "/home/nanshan/workspace/services/autotest-platform/config/settings.json"))
        raw = json.loads(config_path.read_text())
        return cls(data_dir=Path(raw["data_dir"]), key_file=Path(raw["key_file"]),
                   secure_cookie=raw.get("secure_cookie", False),
                   run_timeout=raw.get("run_timeout", 900),
                   step_timeout=raw.get("step_timeout", 15),
                   min_free_bytes=raw.get("min_free_bytes", 3 * 1024**3),
                   browser_library_path=raw.get("browser_library_path", ""),
                   report_base_url=raw.get("report_base_url", "http://172.16.61.128:8088").rstrip("/"))

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        (self.data_dir / "runs").mkdir(exist_ok=True, mode=0o700)
        if not self.key_file.exists():
            if (self.data_dir / 'testdock.sqlite3').exists() and (self.data_dir / 'testdock.sqlite3').stat().st_size:
                raise RuntimeError('已有数据库缺少配套主密钥，请从已验证备份恢复密钥')
            private_write(self.key_file, Fernet.generate_key().decode())

    @property
    def cipher(self):
        return Fernet(self.key_file.read_bytes().strip())

    def seal(self, value):
        return self.cipher.encrypt(encode(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(
 address TEXT PRIMARY KEY, attempts INTEGER NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 created_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members(
 project_id INTEGER NOT NULL REFERENCES projects(id), user_id INTEGER NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('admin','tester','viewer')), PRIMARY KEY(project_id,user_id));
CREATE TABLE IF NOT EXISTS environments(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, web_base_url TEXT NOT NULL DEFAULT '', api_base_url TEXT NOT NULL DEFAULT '',
 variables TEXT NOT NULL DEFAULT '{}', secrets TEXT NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(project_id,name));
CREATE TABLE IF NOT EXISTS cases(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
 steps TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS suites(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', case_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), suite_id INTEGER,
 name TEXT NOT NULL, environment_name TEXT NOT NULL, snapshot TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', trigger TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id), token_id INTEGER,
 idempotency_key TEXT, request_hash TEXT, schedule_id INTEGER,
 created_at REAL NOT NULL, started_at REAL, finished_at REAL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 summary TEXT NOT NULL DEFAULT '{}',
 UNIQUE(token_id,idempotency_key));
CREATE INDEX IF NOT EXISTS run_queue ON runs(status,created_at);
CREATE INDEX IF NOT EXISTS project_runs ON runs(project_id,created_at);
CREATE TABLE IF NOT EXISTS case_results(
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), case_id INTEGER NOT NULL,
 name TEXT NOT NULL, status TEXT NOT NULL, duration REAL NOT NULL, steps TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 suite_id INTEGER NOT NULL REFERENCES suites(id), environment_id INTEGER NOT NULL REFERENCES environments(id),
 cron TEXT NOT NULL, timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 next_run_at REAL NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS schedule_fires(
 schedule_id INTEGER NOT NULL, scheduled_for REAL NOT NULL, run_id TEXT NOT NULL,
 PRIMARY KEY(schedule_id,scheduled_for));
CREATE TABLE IF NOT EXISTS ci_tokens(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(
 id INTEGER PRIMARY KEY, project_id INTEGER, user_id INTEGER, action TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL);
"""


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "testdock.sqlite3"
        self._transaction = ContextVar('testdock_transaction_' + str(id(self)), default=None)

    @contextmanager
    def connect(self):
        active = self._transaction.get()
        if active is not None:
            yield active
            return
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        if self._transaction.get() is not None:
            yield self._transaction.get()
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = self._transaction.set(conn)
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._transaction.reset(marker)

    def initialize(self, upgrade=False):
        had_database = self.path.exists() and self.path.stat().st_size > 0
        self.settings.prepare()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            from .migrations import migrate
            migrate(conn)
            version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
            if version == 2 and had_database and self.settings.data_dir.resolve() == Path('/home/nanshan/workspace/services/autotest-platform/data') and not upgrade:
                raise RuntimeError('生产schema2需备份后显式initialize(upgrade=True)迁移')
            from .ai_store import migrate as migrate_ai
            migrate_ai(conn)
        self.path.chmod(0o600)

    def one(self, sql, args=()):
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.transaction() as conn:
            return conn.execute(sql, args).lastrowid

    def audit(self, user_id, project_id, action, detail=""):
        self.execute("INSERT INTO audit_logs(project_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                     (project_id, user_id, action, str(detail)[:1000], time.time()))
