import hashlib
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import HTTPException

from .core import encode

TERMINAL = {"passed", "failed", "error", "cancelled"}


def next_due(expression, timezone, after=None):
    base = datetime.fromtimestamp(after if after is not None else time.time(), ZoneInfo(timezone))
    return croniter(expression, base).get_next(datetime).timestamp()


def new_run(db, conn, project_id, payload, user_id, trigger="manual", token_id=None,
            idempotency_key=None, schedule_id=None, batch_id=None):
    request_hash = hashlib.sha256(encode(payload).encode()).hexdigest()
    if token_id and idempotency_key:
        existing = conn.execute("SELECT id,request_hash FROM runs WHERE token_id=? AND idempotency_key=?",
                                (token_id, idempotency_key)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "幂等键已用于不同的执行参数")
            return existing["id"]
    env = conn.execute("SELECT * FROM environments WHERE id=? AND project_id=?",
                       (payload["environment_id"], project_id)).fetchone()
    if not env:
        raise HTTPException(404, "环境不存在")
    suite_id = payload.get("suite_id")
    if suite_id:
        suite = conn.execute("SELECT * FROM suites WHERE id=? AND project_id=?", (suite_id, project_id)).fetchone()
        if not suite:
            raise HTTPException(404, "套件不存在")
        case_ids, name = json.loads(suite["case_ids"]), suite["name"]
    else:
        case_ids, name = payload["case_ids"], "用例执行"
    cases = []
    for case_id in dict.fromkeys(case_ids):
        item = conn.execute("SELECT * FROM cases WHERE id=? AND project_id=?", (case_id, project_id)).fetchone()
        if not item:
            raise HTTPException(404, "用例不存在或不属于该项目")
        item = dict(item)
        item["steps"] = json.loads(item["steps"])
        cases.append(item)
    if not cases:
        raise HTTPException(400, "没有可运行的用例")
    environment = dict(env)
    environment["variables"] = json.loads(env["variables"])
    environment["secrets"] = db.settings.unseal(env["secrets"])
    run_id = uuid.uuid4().hex
    snapshot = db.settings.seal({"environment": environment, "cases": cases})
    conn.execute("""INSERT INTO runs(id,project_id,suite_id,name,environment_name,snapshot,
     trigger,user_id,token_id,idempotency_key,request_hash,schedule_id,created_at)
     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (run_id, project_id, suite_id, name, env["name"], snapshot, trigger, user_id, token_id,
                  idempotency_key, request_hash, schedule_id, time.time()))
    from .notifications import targets
    selected,mode=targets(conn,project_id,trigger) if not batch_id else ([], 'all')
    conn.execute('UPDATE runs SET batch_id=?,notify_targets=?,notify_mode=? WHERE id=?',(batch_id,encode(selected),mode,run_id))
    return run_id


RUN_COLUMNS = "id,project_id,suite_id,name,environment_name,status,trigger,user_id,created_at,started_at,finished_at,cancel_requested,error,summary"


def serialize_run(row):
    item = dict(row)
    item["summary"] = json.loads(item["summary"])
    return item


def serialize_environment(settings, row):
    item = dict(row)
    item["variables"] = json.loads(item["variables"])
    item["secret_keys"] = sorted(settings.unseal(item.pop("secrets")))
    return item


def serialize_case(row):
    item = dict(row)
    item["steps"] = json.loads(item["steps"])
    item["tags"] = json.loads(item["tags"])
    item["kind"] = "api" if all(s["action"] == "http" for s in item["steps"]) else "web"
    return item
