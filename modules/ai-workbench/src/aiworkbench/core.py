from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from cryptography.fernet import Fernet

BASE = Path('/home/nanshan/workspace')
CONFIG = BASE / 'services/ai-workbench/config/settings.json'
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|cookie|api.?key', re.I)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(value)
    path.chmod(0o600)


def password_hash(value):
    salt = secrets.token_hex(16)
    return salt + '$' + base64.b64encode(hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 600000)).decode()


def password_matches(value, stored):
    try:
        salt, _ = stored.split('$')
        actual = salt + '$' + base64.b64encode(hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 600000)).decode()
        return hmac.compare_digest(actual, stored)
    except (ValueError, TypeError, AttributeError): return False


def redact(value):
    if isinstance(value, dict):
        return {k: '***' if SENSITIVE.search(k) else redact(v) for k,v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(bearer\s+)[\w.\-+/=]+', r'\1***', value)
        value = re.sub(r"(?i)((?:password|passwd|api[_-]?key|secret|token)[\"']?\s*[=:：]\s*[\"']?)[^\s\"',;，；}]+", r'\1***', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}\b', '***', value)
    return value


def terms(text):
    tokens = re.findall(r'[a-zA-Z0-9_./:-]+', text.lower())
    for group in re.findall(r'[\u3400-\u9fff]+', text):
        tokens.extend(group[i:i+2] for i in range(len(group)-1))
        tokens.extend(group)
    return ' '.join(tokens)


@dataclass
class Settings:
    data_dir: Path
    key_file: Path
    model_dir: Path = BASE / 'tools/ai-models'
    min_free_bytes: int = 3*1024**3
    daily_tokens: int = 200000
    testdock_url: str = 'http://127.0.0.1:8088'
    bridge_key_file: Path | None = None
    roots: tuple = (str(BASE/'docs'), str(BASE/'projects'),str(BASE/'README.md'),'/home/nanshan/AGENTS.md')

    @classmethod
    def load(cls):
        data = json.loads(Path(os.environ.get('AIWORKBENCH_CONFIG', CONFIG)).read_text())
        for name in ('data_dir','key_file','model_dir','bridge_key_file'):
            if data.get(name): data[name] = Path(data[name])
        return cls(**data)

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        for name in ('files','tasks','models'):
            (self.data_dir/name).mkdir(exist_ok=True, mode=0o700)
        if not self.key_file.exists():
            if (self.data_dir/'ai.sqlite3').exists(): raise RuntimeError('数据库已存在，不能重新生成主密钥')
            write_private(self.key_file, Fernet.generate_key().decode())

    def seal(self, value): return Fernet(self.key_file.read_bytes()).encrypt(dump(value).encode()).decode()
    def unseal(self, value): return json.loads(Fernet(self.key_file.read_bytes()).decrypt(value.encode()))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,is_admin INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),expires_at REAL);
CREATE TABLE IF NOT EXISTS login_attempts(address TEXT PRIMARY KEY,n INTEGER NOT NULL,since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',external_id INTEGER UNIQUE,egress TEXT NOT NULL DEFAULT 'local',created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members(project_id INTEGER REFERENCES projects(id),user_id INTEGER REFERENCES users(id),role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),PRIMARY KEY(project_id,user_id));
CREATE TABLE IF NOT EXISTS bindings(user_id INTEGER PRIMARY KEY REFERENCES users(id),external_user_id INTEGER UNIQUE NOT NULL,created_at REAL NOT NULL,secret TEXT NOT NULL DEFAULT '',expires_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,project_id INTEGER REFERENCES projects(id),owner_id INTEGER NOT NULL REFERENCES users(id),visibility TEXT NOT NULL DEFAULT 'private',deps TEXT NOT NULL DEFAULT '[]',sources TEXT NOT NULL DEFAULT '[]',thread_id TEXT,status TEXT NOT NULL DEFAULT 'ready',version INTEGER NOT NULL DEFAULT 1,locator TEXT NOT NULL DEFAULT '',external_run_id TEXT,egress TEXT NOT NULL DEFAULT 'local',created_at REAL NOT NULL,updated_at REAL NOT NULL,review_at REAL,reviewed_by INTEGER,deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS record_project ON records(project_id,kind,status);
CREATE TABLE IF NOT EXISTS record_evidence(record_id TEXT PRIMARY KEY REFERENCES records(id),citations TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(record_id TEXT REFERENCES records(id),version INTEGER,body TEXT NOT NULL,title TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(record_id,version));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(record_id UNINDEXED,version UNINDEXED,locator UNINDEXED,text,search_terms,tokenize='unicode61');
CREATE TABLE IF NOT EXISTS embeddings(chunk_id INTEGER PRIMARY KEY,model TEXT NOT NULL,vector BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,recipient_id INTEGER REFERENCES users(id),record_id TEXT REFERENCES records(id),title TEXT NOT NULL,read_at REAL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mirrors(project_id INTEGER,kind TEXT,external_key TEXT,record_id TEXT REFERENCES records(id),last_hash TEXT,PRIMARY KEY(project_id,kind,external_key));
CREATE TABLE IF NOT EXISTS remote_sync(user_id INTEGER PRIMARY KEY,last_checked REAL NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions(user_id INTEGER REFERENCES users(id),project_id INTEGER REFERENCES projects(id),enabled INTEGER NOT NULL DEFAULT 0,last_checked REAL NOT NULL,PRIMARY KEY(user_id,project_id));
CREATE TABLE IF NOT EXISTS deliveries(user_id INTEGER,run_id TEXT,PRIMARY KEY(user_id,run_id));
CREATE TABLE IF NOT EXISTS automations(project_id INTEGER PRIMARY KEY REFERENCES projects(id),owner_id INTEGER REFERENCES users(id),model_id INTEGER,enabled INTEGER NOT NULL DEFAULT 0,last_checked REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS automation_runs(project_id INTEGER,run_id TEXT,PRIMARY KEY(project_id,run_id));
CREATE TABLE IF NOT EXISTS models(id INTEGER PRIMARY KEY,name TEXT NOT NULL,base_url TEXT NOT NULL,model TEXT NOT NULL,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,internal INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,owner_id INTEGER REFERENCES users(id),kind TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'queued',progress TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',cancel_requested INTEGER NOT NULL DEFAULT 0,reserved_tokens INTEGER NOT NULL DEFAULT 0,used_tokens INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,started_at REAL,finished_at REAL);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,created_at);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT NOT NULL,object_id TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS used_nonces(nonce TEXT PRIMARY KEY,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS file_versions(record_id TEXT REFERENCES records(id),version INTEGER,filename TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(record_id,version));
CREATE TABLE IF NOT EXISTS source_files(record_id TEXT PRIMARY KEY REFERENCES records(id),path TEXT NOT NULL,last_hash TEXT NOT NULL,last_checked REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,owner_id INTEGER REFERENCES users(id),title TEXT NOT NULL,created_at REAL NOT NULL);
'''


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir/'ai.sqlite3'

    def initialize(self, upgrade=False):
        had_database=self.path.exists() and self.path.stat().st_size>0
        self.settings.prepare()
        with self.connect() as c:
            c.executescript(SCHEMA)
            version=c.execute('SELECT version FROM schema_version').fetchone()[0]
            if version==1:
                columns={r['name'] for r in c.execute('PRAGMA table_info(bindings)')}
                if 'secret' not in columns:c.execute("ALTER TABLE bindings ADD COLUMN secret TEXT NOT NULL DEFAULT ''")
                if 'expires_at' not in columns:c.execute('ALTER TABLE bindings ADD COLUMN expires_at REAL NOT NULL DEFAULT 0')
                c.execute('UPDATE schema_version SET version=2')
            elif version not in (2,3):raise RuntimeError('数据库版本不兼容')
            version=c.execute('SELECT version FROM schema_version').fetchone()[0]
            if version==2 and had_database and self.settings.data_dir.resolve()==BASE/'services/ai-workbench/data' and not upgrade:
                raise RuntimeError('生产schema2需备份后显式initialize(upgrade=True)迁移')
            from .integration_store import migrate
            migrate(c)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=15000')
        try: yield c
        finally: c.close()

    @contextmanager
    def transaction(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try: yield c;c.commit()
            except BaseException: c.rollback();raise

    def one(self, sql, args=()):
        with self.connect() as c:
            row=c.execute(sql,args).fetchone();return dict(row) if row else None
    def all(self, sql, args=()):
        with self.connect() as c:return [dict(r) for r in c.execute(sql,args)]
    def execute(self, sql, args=()):
        with self.transaction() as c:return c.execute(sql,args).lastrowid
    def audit(self, user, action, target='', detail=''):
        self.execute('INSERT INTO audit(user_id,action,object_id,detail,created_at) VALUES(?,?,?,?,?)',(user,action,target,str(detail)[:500],time.time()))
