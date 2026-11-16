"""Versioned, encrypted assistant persistence. No network or business execution."""
import json
import time
import uuid

SQL = '''
CREATE TABLE ai_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE ai_settings(id INTEGER PRIMARY KEY CHECK(id=1),secret TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE ai_subjects(user_id INTEGER PRIMARY KEY REFERENCES users(id),subject TEXT UNIQUE NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ai_tickets(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,session_hash TEXT NOT NULL,expires_at REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ai_source_grants(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,session_hash TEXT NOT NULL,expires_at REAL NOT NULL);
CREATE TABLE ai_connections(session_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,secret TEXT NOT NULL,expires_at REAL NOT NULL);
CREATE TABLE ai_conversations(id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),title TEXT NOT NULL,scope TEXT NOT NULL,secret TEXT NOT NULL,epoch INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ai_tasks(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),owner_id INTEGER NOT NULL,session_hash TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',secret TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',cancel_requested INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX ai_task_queue ON ai_tasks(status,created_at);
CREATE TABLE ai_messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),task_id TEXT,role TEXT NOT NULL,secret TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE ai_events(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),secret TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX ai_event_task ON ai_events(task_id,id);
CREATE TABLE ai_files(id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),name TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,secret TEXT NOT NULL,created_at REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ai_proposals(id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),task_id TEXT,project_id INTEGER,kind TEXT NOT NULL,secret TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',expires_at REAL NOT NULL,result TEXT,created_at REAL NOT NULL);
'''


def migrate(conn):
    version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
    if version == 3:
        return
    if version != 2:
        raise RuntimeError('AI迁移要求schema2')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for statement in SQL.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.execute('INSERT INTO ai_meta VALUES(?,?)', ('installation', uuid.uuid4().hex))
        conn.execute('UPDATE schema_version SET version=3')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def config(db):
    defaults = {'enabled': False, 'base_url': 'http://127.0.0.1:8094/v1', 'api_key': '',
                'model': '', 'vision_models': [], 'workbench_url': 'http://127.0.0.1:8090',
                'max_output_tokens': 4096, 'max_rounds': 8}
    row = db.one('SELECT secret FROM ai_settings WHERE id=1')
    return defaults | (db.settings.unseal(row['secret']) if row else {})


def event(db, task_id, kind, **data):
    db.execute('INSERT INTO ai_events(task_id,secret,created_at) VALUES(?,?,?)',
               (task_id, db.settings.seal({'type': kind, **data}), time.time()))


def task_payload(db, task):
    return db.settings.unseal(task['secret'])


def save_task(db, task, payload, status=None):
    db.execute('UPDATE ai_tasks SET secret=?,status=COALESCE(?,status),updated_at=? WHERE id=?',
               (db.settings.seal(payload), status, time.time(), task['id']))
