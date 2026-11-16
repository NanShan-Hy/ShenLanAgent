"""Additive schema 1 -> 2 migration. Call only after an upgrade backup."""
SQL = '''
CREATE TABLE IF NOT EXISTS schedule_batches(
 id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), schedule_id INTEGER NOT NULL,
 name TEXT NOT NULL, scheduled_for REAL NOT NULL, created_at REAL NOT NULL, finished_at REAL,
 status TEXT NOT NULL DEFAULT 'running', reason TEXT NOT NULL DEFAULT '',
 notify_targets TEXT NOT NULL DEFAULT '[]', notify_mode TEXT NOT NULL DEFAULT 'all',
 UNIQUE(schedule_id,scheduled_for));
CREATE TABLE IF NOT EXISTS notification_channels(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 kind TEXT NOT NULL, secret TEXT NOT NULL, target_hash TEXT NOT NULL,
 owner_id INTEGER NOT NULL REFERENCES users(id), enabled INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_rules(
 project_id INTEGER PRIMARY KEY REFERENCES projects(id), channel_ids TEXT NOT NULL DEFAULT '[]',
 triggers TEXT NOT NULL DEFAULT '["schedule"]', mode TEXT NOT NULL DEFAULT 'all');
CREATE TABLE IF NOT EXISTS notification_deliveries(
 id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 channel_id INTEGER NOT NULL REFERENCES notification_channels(id), channel_revision INTEGER NOT NULL,
 event_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, error TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL DEFAULT '',
 UNIQUE(event_key,channel_id));
CREATE INDEX IF NOT EXISTS notification_pending ON notification_deliveries(status,next_attempt);
CREATE INDEX IF NOT EXISTS notification_project ON notification_deliveries(project_id,created_at);
CREATE TABLE IF NOT EXISTS notification_attempts(
 id INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES notification_deliveries(id),
 target_hash TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
'''

def migrate(conn):
    version=conn.execute('SELECT version FROM schema_version').fetchone()[0]
    if version==3:return
    if version not in (1,2):raise RuntimeError('数据库版本不兼容')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for table,cols in {'schedules': [('environment_ids',"TEXT NOT NULL DEFAULT '[]'"),('notification_channel_ids','TEXT'),('notify_mode','TEXT')],
                           'runs':[('batch_id','TEXT'),('notify_targets',"TEXT NOT NULL DEFAULT '[]'"),('notify_mode',"TEXT NOT NULL DEFAULT 'all'")]}.items():
            present={r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
            for name,definition in cols:
                if name not in present:conn.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
        for statement in SQL.split(';'):
            if statement.strip():conn.execute(statement)
        conn.execute("UPDATE schedules SET environment_ids='['||environment_id||']' WHERE environment_ids='[]'")
        conn.execute('CREATE INDEX IF NOT EXISTS runs_batch ON runs(batch_id)')
        conn.execute('UPDATE schema_version SET version=2')
        conn.commit()
    except BaseException:
        conn.rollback();raise
