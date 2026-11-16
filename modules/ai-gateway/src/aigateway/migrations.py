"""Additive, transactional operational metadata. Financial rows are never rewritten."""
import uuid
SCHEMA_VERSION = 6
SQL = '''
CREATE TABLE upstream_runtime(
 upstream_id TEXT PRIMARY KEY REFERENCES upstreams(id),revision INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'closed',failures INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0,half_open_attempt TEXT,
 last_error TEXT NOT NULL DEFAULT '',last_http_status INTEGER,last_latency_ms INTEGER,
 last_event_at REAL,last_success_at REAL,probe_id TEXT,probe_until REAL NOT NULL DEFAULT 0);
CREATE TABLE upstream_attempts(
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES requests(id),upstream_id TEXT NOT NULL REFERENCES upstreams(id),
 revision INTEGER,attempt_no INTEGER NOT NULL,quota_group TEXT NOT NULL,status TEXT NOT NULL,
 error_code TEXT NOT NULL DEFAULT '',http_status INTEGER,provider_id TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL,dispatched_at REAL,finished_at REAL,latency_ms INTEGER,
 UNIQUE(request_id,attempt_no));
CREATE INDEX attempt_group_time ON upstream_attempts(quota_group,created_at);
CREATE INDEX attempt_upstream_time ON upstream_attempts(upstream_id,created_at);
CREATE INDEX attempt_request_state ON upstream_attempts(request_id,status);
CREATE TABLE upstream_probes(
 id TEXT PRIMARY KEY,upstream_id TEXT NOT NULL REFERENCES upstreams(id),revision INTEGER NOT NULL,
 quota_group TEXT NOT NULL,actor_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL,
 error_code TEXT NOT NULL DEFAULT '',http_status INTEGER,model_count INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,finished_at REAL,latency_ms INTEGER);
CREATE INDEX probe_upstream_time ON upstream_probes(upstream_id,created_at);
CREATE INDEX probe_group_time ON upstream_probes(quota_group,created_at);
CREATE TABLE upstream_catalog(
 upstream_id TEXT NOT NULL REFERENCES upstreams(id),revision INTEGER NOT NULL,model_id TEXT NOT NULL,
 discovered_at REAL NOT NULL,PRIMARY KEY(upstream_id,revision,model_id));
'''

SQL_V3 = '''
CREATE TABLE chatgpt_integration(
 id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 0,
 client_id TEXT NOT NULL DEFAULT '',redirect_uri TEXT NOT NULL DEFAULT '',
 auth_method TEXT NOT NULL DEFAULT 'none',secret TEXT NOT NULL DEFAULT '',
 host_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL DEFAULT 0);
CREATE TABLE chatgpt_accounts(
 id TEXT PRIMARY KEY,upstream_id TEXT NOT NULL UNIQUE REFERENCES upstreams(id),
 owner_id TEXT NOT NULL REFERENCES users(id),label TEXT NOT NULL,
 sharing TEXT NOT NULL DEFAULT 'owner',tenant_id TEXT REFERENCES tenants(id),
 issuer TEXT NOT NULL DEFAULT 'https://auth.openai.com',subject TEXT NOT NULL DEFAULT '',
 client_id TEXT NOT NULL DEFAULT '',email TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending_signin',scopes TEXT NOT NULL DEFAULT '[]',
 expires_at REAL NOT NULL DEFAULT 0,earliest_refresh_at REAL NOT NULL DEFAULT 0,
 token_version INTEGER NOT NULL DEFAULT 0,integration_revision INTEGER NOT NULL DEFAULT 0,
 refresh_lease TEXT,refresh_until REAL NOT NULL DEFAULT 0,refresh_dispatched INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE UNIQUE INDEX chatgpt_registration ON chatgpt_accounts(issuer,client_id,subject) WHERE subject!='';
CREATE INDEX chatgpt_owner ON chatgpt_accounts(owner_id);
CREATE TABLE chatgpt_oauth_attempts(
 id TEXT PRIMARY KEY,state_hash TEXT NOT NULL UNIQUE,account_id TEXT NOT NULL REFERENCES chatgpt_accounts(id),
 owner_id TEXT NOT NULL REFERENCES users(id),session_hash TEXT NOT NULL,
 secret TEXT NOT NULL,client_id TEXT NOT NULL,redirect_uri TEXT NOT NULL,integration_revision INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',error_code TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL,expires_at REAL NOT NULL,finished_at REAL);
CREATE INDEX chatgpt_oauth_owner ON chatgpt_oauth_attempts(owner_id,created_at);
CREATE TABLE chatgpt_usage_state(
 scope_hash TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'unknown',cooldown_until REAL NOT NULL DEFAULT 0,
 reset_at REAL,last_error TEXT NOT NULL DEFAULT '',observed_at REAL);
'''


SQL_V5 = '''
CREATE TABLE request_audits(
 id TEXT PRIMARY KEY,request_id TEXT REFERENCES requests(id),execution_owner INTEGER NOT NULL DEFAULT 0,
 user_id TEXT REFERENCES users(id),tenant_id TEXT REFERENCES tenants(id),application_id TEXT REFERENCES applications(id),
 key_id TEXT REFERENCES api_keys(id),endpoint TEXT NOT NULL,protocol TEXT NOT NULL,model TEXT NOT NULL DEFAULT '',
 source_ip TEXT NOT NULL DEFAULT '',stream INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'running',
 stage TEXT NOT NULL DEFAULT 'received',http_status INTEGER,error_code TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL,finished_at REAL,dispatched_at REAL,first_byte_at REAL,timeline TEXT NOT NULL DEFAULT '{}',
 capture_status TEXT NOT NULL DEFAULT 'not_captured',capture_reason TEXT NOT NULL DEFAULT '',capture_bytes INTEGER NOT NULL DEFAULT 0);
CREATE INDEX audit_user_time ON request_audits(user_id,created_at,id);
CREATE INDEX audit_tenant_time ON request_audits(tenant_id,created_at,id);
CREATE INDEX audit_status_time ON request_audits(status,created_at,id);
CREATE INDEX audit_request ON request_audits(request_id);
CREATE TABLE request_capture_chunks(
 audit_id TEXT NOT NULL REFERENCES request_audits(id),section TEXT NOT NULL,seq INTEGER NOT NULL,
 ciphertext TEXT NOT NULL,byte_size INTEGER NOT NULL,PRIMARY KEY(audit_id,section,seq));
CREATE TABLE request_recording_settings(
 id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 1,
 max_bytes INTEGER NOT NULL DEFAULT 1073741824,used_bytes INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL DEFAULT 0);
INSERT INTO request_recording_settings(id) VALUES(1);
INSERT INTO request_audits(id,request_id,execution_owner,user_id,tenant_id,application_id,key_id,endpoint,protocol,model,
 status,stage,created_at,finished_at,capture_status,capture_reason)
 SELECT 'legacy-'||r.id,r.id,1,r.user_id,r.tenant_id,r.application_id,r.key_id,r.endpoint,
 CASE WHEN r.endpoint='/chat/completions' THEN 'chat' WHEN r.endpoint='/responses/compact' THEN 'compact' ELSE 'responses' END,
 COALESCE(json_extract(r.price,'$.model_alias'),m.alias),r.status,'historical',r.created_at,r.finished_at,'historical','history_not_recorded'
 FROM requests r JOIN models m ON m.id=r.model_id;
'''


SQL_V6 = '''
CREATE TABLE managed_settings(id INTEGER PRIMARY KEY CHECK(id=1),max_bytes INTEGER NOT NULL DEFAULT 1073741824,
 used_bytes INTEGER NOT NULL DEFAULT 0,reserved_bytes INTEGER NOT NULL DEFAULT 0);
INSERT INTO managed_settings(id) VALUES(1);
CREATE TABLE managed_objects(id TEXT PRIMARY KEY,kind TEXT NOT NULL,bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE managed_chunks(object_id TEXT NOT NULL REFERENCES managed_objects(id),seq INTEGER NOT NULL,
 ciphertext TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(object_id,seq));
CREATE TABLE conversation_policies(application_id TEXT NOT NULL DEFAULT '',task_type TEXT NOT NULL DEFAULT '',
 policy TEXT NOT NULL DEFAULT 'continuity',identity_mode TEXT NOT NULL DEFAULT 'key_owner',
 PRIMARY KEY(application_id,task_type));
INSERT INTO conversation_policies VALUES('','','continuity','key_owner');
CREATE TABLE model_media(model_id TEXT PRIMARY KEY REFERENCES models(id),vision INTEGER NOT NULL DEFAULT 0,
 image_tokens INTEGER NOT NULL DEFAULT 0,max_images INTEGER NOT NULL DEFAULT 4,verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE conversations(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),tenant_id TEXT NOT NULL REFERENCES tenants(id),
 application_id TEXT NOT NULL REFERENCES applications(id),subject_hash TEXT NOT NULL,task_type TEXT NOT NULL,model TEXT NOT NULL,
 protocol TEXT NOT NULL,policy TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',revision INTEGER NOT NULL DEFAULT 0,
 pending_calls TEXT NOT NULL DEFAULT '[]',summary_id TEXT,summary_until INTEGER NOT NULL DEFAULT 0,
 lease TEXT,lease_until REAL,gateway_upstream TEXT REFERENCES upstreams(id),created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX conversation_owner ON conversations(user_id,application_id,subject_hash,updated_at);
CREATE TABLE conversation_current(user_id TEXT NOT NULL,application_id TEXT NOT NULL,subject_hash TEXT NOT NULL,
 task_type TEXT NOT NULL,conversation_id TEXT NOT NULL REFERENCES conversations(id),PRIMARY KEY(user_id,application_id,subject_hash,task_type));
CREATE TABLE conversation_turns(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),
 request_id TEXT REFERENCES requests(id),key_id TEXT NOT NULL REFERENCES api_keys(id),revision INTEGER NOT NULL,
 idempotency_key TEXT NOT NULL,input_id TEXT REFERENCES managed_objects(id),output_id TEXT REFERENCES managed_objects(id),
 status TEXT NOT NULL DEFAULT 'running',error_code TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'answer',account_ref TEXT,
 created_at REAL NOT NULL,finished_at REAL,UNIQUE(conversation_id,idempotency_key));
CREATE INDEX conversation_history ON conversation_turns(conversation_id,revision,created_at);
CREATE TABLE account_bindings(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,policy TEXT NOT NULL,
 excluded TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL DEFAULT 'bound',updated_at REAL NOT NULL,account_ref TEXT);
CREATE INDEX binding_account_ref ON account_bindings(account_ref);
CREATE TABLE binding_events(id INTEGER PRIMARY KEY,binding_id TEXT NOT NULL,old_account TEXT,new_account TEXT,
 reason TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE gateway_files(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),application_id TEXT NOT NULL REFERENCES applications(id),
 subject_hash TEXT NOT NULL,name TEXT NOT NULL,mime TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES managed_objects(id),parsed_id TEXT REFERENCES managed_objects(id),
 state TEXT NOT NULL DEFAULT 'processing',created_at REAL NOT NULL);
ALTER TABLE request_audits ADD COLUMN conversation_id TEXT;
ALTER TABLE request_audits ADD COLUMN turn_id TEXT;
'''


def migrate(connection):
    version = connection.execute('SELECT version FROM schema_version').fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version not in (1, 2, 3, 4, 5):
        raise RuntimeError('数据库版本不兼容')
    connection.execute('BEGIN IMMEDIATE')
    try:
        if version == 1:
            for statement in SQL.split(';'):
                if statement.strip():
                    connection.execute(statement)
            # Preserve old observations without inventing a dispatch/configuration version.
            connection.execute('''INSERT INTO upstream_attempts(id,request_id,upstream_id,revision,attempt_no,
            quota_group,status,error_code,provider_id,created_at,finished_at,latency_ms)
            SELECT 'legacy-'||r.id,r.id,r.upstream_id,NULL,1,p.quota_group,'history',r.error_code,r.provider_id,
            r.created_at,r.finished_at,CASE WHEN r.finished_at IS NOT NULL
            THEN max(0,CAST((r.finished_at-r.created_at)*1000 AS INTEGER)) ELSE NULL END
            FROM requests r JOIN pools p ON p.id=r.pool_id''')
        if version < 3:
            for statement in SQL_V3.split(';'):
                if statement.strip():
                    connection.execute(statement)
            connection.execute('INSERT INTO chatgpt_integration(id,host_id) VALUES(1,?)', ('urn:uuid:' + str(uuid.uuid4()),))
        if version < 4:
            # Retain identifiers/financial history; retired resources cannot dispatch.
            connection.execute("UPDATE models SET enabled=0 WHERE pool_id IN (SELECT id FROM pools WHERE kind='chatgpt')")
            connection.execute("UPDATE upstreams SET enabled=0 WHERE pool_id IN (SELECT id FROM pools WHERE kind='chatgpt')")
            connection.execute("UPDATE pools SET enabled=0,approval_status='retired' WHERE kind='chatgpt'")
            for table in ('chatgpt_integration','chatgpt_accounts','chatgpt_oauth_attempts','chatgpt_usage_state'):
                for action in ('INSERT','UPDATE','DELETE'):
                    connection.execute(f"CREATE TRIGGER {table}_frozen_{action.lower()} BEFORE {action} ON {table} "
                                       "BEGIN SELECT RAISE(ABORT,'legacy integration is read-only'); END")
        if version < 5:
            for statement in SQL_V5.split(';'):
                if statement.strip():
                    connection.execute(statement)
        for statement in SQL_V6.split(';'):
            if statement.strip():
                connection.execute(statement)
        connection.execute('UPDATE schema_version SET version=?', (SCHEMA_VERSION,))
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
