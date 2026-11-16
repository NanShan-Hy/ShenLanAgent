"""Additive schema3 for session-bound, least-privilege TestDock integration."""
def migrate(conn):
    version=conn.execute('SELECT version FROM schema_version').fetchone()[0]
    if version==3:return
    if version!=2:raise RuntimeError('集成迁移要求schema2')
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('CREATE TABLE td_identities(subject TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),external_user_id INTEGER NOT NULL)')
        conn.execute('CREATE TABLE td_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,external_user_id INTEGER NOT NULL,secret TEXT NOT NULL,expires_at REAL NOT NULL)')
        conn.execute('UPDATE schema_version SET version=3')
        conn.commit()
    except BaseException:
        conn.rollback();raise
