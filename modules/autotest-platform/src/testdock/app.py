from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import Body, FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from .core import Database, Settings, encode, password_hash, password_matches, token_hash
from .models import (CaseInput, EnvironmentInput, Login, MemberInput, ProjectInput,
                     RunInput, ScheduleInput, SuiteInput, TokenInput, UserInput,
                     MatrixInput, ImportInput, OpenAPIInput, PasswordInput)
from .transfer import export_assets, import_assets, openapi_preview
from .services import (RUN_COLUMNS, TERMINAL, new_run, next_due, serialize_case,
                       serialize_environment, serialize_run)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    db = Database(settings)
    db.initialize()
    api = FastAPI(title="TestDock API", version="1.3.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    api.state.db = db
    api.state.settings = settings

    @api.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]}, status_code=422)

    @api.exception_handler(ValidationError)
    async def invalid_model(request,exc):
        return JSONResponse({'detail':'输入字段或工具参数不符合平台格式'},status_code=422)

    @api.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"detail": "名称已存在，或该资源仍被其他内容引用"}, status_code=409)

    @api.middleware("http")
    async def browser_guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not request.url.path.startswith(("/api/ci/", "/api/integrations/ai/")):
                origin = request.headers.get("origin")
                if origin and urlparse(origin).netloc != request.headers.get("host"):
                    return JSONResponse({"detail": "来源不匹配"}, status_code=403)
                if request.headers.get("x-requested-with") != "TestDock":
                    return JSONResponse({"detail": "缺少请求标识"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度"}, status_code=400)
            limit = 21 * 1024 * 1024 if request.url.path.startswith('/api/ai/conversations/') and request.url.path.endswith('/files') else 2 * 1024 * 1024
            if length > limit:
                return JSONResponse({"detail": "请求内容超过2MB"}, status_code=413)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > limit:
                    return JSONResponse({"detail": "请求内容超过2MB"}, status_code=413)
            request._body = bytes(data)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def public_user(user):
        return {key: user[key] for key in ("id", "username", "display_name", "is_admin", "active")}

    def actor(request, ci=False):
        if ci:
            value = request.headers.get("authorization", "")
            if not value.startswith("Bearer "):
                raise HTTPException(401, "需要CI Bearer令牌")
            token = db.one("SELECT * FROM ci_tokens WHERE token_hash=? AND expires_at>?",
                           (token_hash(value[7:]), time.time()))
            if not token:
                raise HTTPException(401, "CI令牌无效或已失效")
            user = db.one("SELECT * FROM users WHERE id=? AND active=1", (token["user_id"],))
            if not user:
                raise HTTPException(401, "账号已停用")
            return user, token
        value = request.cookies.get("testdock_session", "")
        user = db.one("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                        WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
                      (token_hash(value), time.time()))
        if not user:
            raise HTTPException(401, "请先登录")
        return user

    def project_access(user, project_id, required="viewer"):
        if not db.one("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise HTTPException(404, "项目不存在")
        if user["is_admin"]:
            return "admin"
        member = db.one("SELECT role FROM members WHERE project_id=? AND user_id=?", (project_id, user["id"]))
        ranks = {"viewer": 0, "tester": 1, "admin": 2}
        if not member or ranks[member["role"]] < ranks[required]:
            raise HTTPException(403, "没有该项目的操作权限")
        return member["role"]

    def scoped(request, project_id, required="viewer"):
        user = actor(request)
        project_access(user, project_id, required)
        return user

    def resource(table, resource_id, project_id):
        row = db.one(f"SELECT * FROM {table} WHERE id=? AND project_id=?", (resource_id, project_id))
        if not row:
            raise HTTPException(404, "资源不存在")
        return row

    def ci_access(request, project_id, required="viewer"):
        user, token = actor(request, ci=True)
        if token["project_id"] != project_id:
            raise HTTPException(403, "令牌不属于该项目")
        project_access(user, project_id, required)
        return user, token

    @api.get("/api/health")
    def health():
        db.one("SELECT 1")
        heartbeat = settings.data_dir / "worker-heartbeat"
        notifier=settings.data_dir / 'notifier-heartbeat'
        return {"status": "ok", "worker_online": heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 10,
                "notifier_online":notifier.exists() and time.time()-notifier.stat().st_mtime<10}

    @api.post("/api/auth/login")
    def login(body: Login, request: Request):
        address = request.client.host if request.client else "unknown"
        attempts = db.one("SELECT * FROM login_attempts WHERE address=?", (address,))
        if attempts and attempts["attempts"] >= 10 and time.time() - attempts["since"] < 300:
            raise HTTPException(429, "登录失败次数较多，请5分钟后再试")
        user = db.one("SELECT * FROM users WHERE username=? AND active=1", (body.username,))
        if not user or not password_matches(body.password, user["password_hash"]):
            since = attempts["since"] if attempts and time.time() - attempts["since"] < 300 else time.time()
            count = attempts["attempts"] + 1 if attempts and time.time() - attempts["since"] < 300 else 1
            db.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)", (address, count, since))
            raise HTTPException(401, "账号或密码不正确")
        db.execute("DELETE FROM login_attempts WHERE address=?", (address,))
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), user["id"], time.time() + 43200))
        response = JSONResponse(public_user(user))
        response.set_cookie("testdock_session", token, httponly=True, secure=settings.secure_cookie,
                            samesite="lax", max_age=43200, path="/")
        return response

    @api.post("/api/auth/logout")
    def logout(request: Request):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get("testdock_session", "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("testdock_session")
        return response

    @api.get("/api/me")
    def me(request: Request):
        return public_user(actor(request))

    @api.post("/api/me/password")
    def change_password(request: Request, body: PasswordInput):
        user = actor(request)
        if not password_matches(body.old_password, user["password_hash"]):
            raise HTTPException(400, "原密码不正确")
        password = body.new_password
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise HTTPException(400, "新密码长度为10至256位")
        with db.transaction() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), user["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        return {"ok": True}

    @api.get("/api/users")
    def list_users(request: Request):
        if not actor(request)["is_admin"]:
            raise HTTPException(403, "需要系统管理员")
        return [public_user(u) for u in db.all("SELECT * FROM users ORDER BY id")]

    @api.post("/api/users", status_code=201)
    def create_user(body: UserInput, request: Request):
        user = actor(request)
        if not user["is_admin"]:
            raise HTTPException(403, "需要系统管理员")
        pk = db.execute("INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
                        (body.username, body.display_name, password_hash(body.password), int(body.is_admin), time.time()))
        db.audit(user["id"], None, "user.create", body.username)
        return public_user(db.one("SELECT * FROM users WHERE id=?", (pk,)))

    @api.patch("/api/users/{user_id}")
    def update_user(user_id: int, request: Request, body: dict = Body(...)):
        admin = actor(request)
        if not admin["is_admin"]:
            raise HTTPException(403, "需要系统管理员")
        user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            raise HTTPException(404, "用户不存在")
        allowed = {"active", "display_name", "password", "is_admin"}
        if set(body) - allowed:
            raise HTTPException(400, "不支持的用户字段")
        if any(key in body and type(body[key]) is not bool for key in ('active', 'is_admin')):
            raise HTTPException(400, '启用和管理员字段必须为布尔值')
        active, is_admin = bool(body.get("active", user["active"])), bool(body.get("is_admin", user["is_admin"]))
        with db.transaction() as conn:
            if user["is_admin"] and user["active"] and not (active and is_admin):
                if conn.execute("SELECT count(*) FROM users WHERE is_admin=1 AND active=1").fetchone()[0] <= 1:
                    raise HTTPException(409, "至少保留一名有效系统管理员")
            new_hash = user["password_hash"]
            if "password" in body:
                if not isinstance(body["password"], str) or not 10 <= len(body["password"]) <= 256:
                    raise HTTPException(400, "密码长度为10至256位")
                new_hash = password_hash(body["password"])
            display_name = str(body.get("display_name", user["display_name"])).strip()
            if not 1 <= len(display_name) <= 80:
                raise HTTPException(400, "姓名长度为1至80位")
            conn.execute("UPDATE users SET active=?,is_admin=?,display_name=?,password_hash=? WHERE id=?",
                         (active, is_admin, display_name, new_hash, user_id))
            if not active or "password" in body:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.audit(admin["id"], None, "user.update", str(user_id))
        return public_user(db.one("SELECT * FROM users WHERE id=?", (user_id,)))

    @api.get("/api/projects")
    def projects(request: Request):
        user = actor(request)
        if user["is_admin"]:
            rows = db.all("SELECT p.*, 'admin' AS role FROM projects p ORDER BY p.id")
        else:
            rows = db.all("SELECT p.*,m.role FROM projects p JOIN members m ON p.id=m.project_id WHERE m.user_id=? ORDER BY p.id", (user["id"],))
        return rows

    @api.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput, request: Request):
        user = actor(request)
        if not user["is_admin"]:
            raise HTTPException(403, "需要系统管理员创建项目")
        with db.transaction() as conn:
            pk = conn.execute("INSERT INTO projects(name,description,created_by,created_at) VALUES(?,?,?,?)",
                              (body.name, body.description, user["id"], time.time())).lastrowid
            conn.execute("INSERT INTO members VALUES(?,?,?)", (pk, user["id"], "admin"))
        db.audit(user["id"], pk, "project.create", body.name)
        return {"id": pk, **body.model_dump(), "role": "admin"}

    @api.put("/api/projects/{project_id}")
    def update_project(project_id: int, body: ProjectInput, request: Request):
        scoped(request, project_id, "admin")
        db.execute("UPDATE projects SET name=?,description=? WHERE id=?", (body.name, body.description, project_id))
        return {"id": project_id, **body.model_dump()}

    @api.get("/api/projects/{project_id}/members")
    def members(project_id: int, request: Request):
        scoped(request, project_id)
        return db.all("SELECT u.id,u.username,u.display_name,u.active,m.role FROM users u JOIN members m ON u.id=m.user_id WHERE m.project_id=?", (project_id,))

    @api.get("/api/projects/{project_id}/available-users")
    def available_users(project_id: int, request: Request):
        scoped(request, project_id, "admin")
        return [public_user(u) for u in db.all("SELECT * FROM users WHERE active=1 ORDER BY id")]

    @api.put("/api/projects/{project_id}/members")
    def put_member(project_id: int, body: MemberInput, request: Request):
        user = scoped(request, project_id, "admin")
        if not db.one("SELECT id FROM users WHERE id=? AND active=1", (body.user_id,)):
            raise HTTPException(404, "用户不存在")
        with db.transaction() as conn:
            old = conn.execute("SELECT role FROM members WHERE project_id=? AND user_id=?", (project_id, body.user_id)).fetchone()
            if old and old["role"] == "admin" and body.role != "admin":
                if conn.execute("SELECT count(*) FROM members WHERE project_id=? AND role='admin'", (project_id,)).fetchone()[0] <= 1:
                    raise HTTPException(409, "至少保留一名项目管理员")
            conn.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role",
                         (project_id, body.user_id, body.role))
        db.audit(user["id"], project_id, "member.update", str(body.user_id) + ":" + body.role)
        return {"ok": True}

    @api.delete("/api/projects/{project_id}/members/{user_id}")
    def remove_member(project_id: int, user_id: int, request: Request):
        scoped(request, project_id, "admin")
        with db.transaction() as conn:
            old = conn.execute("SELECT role FROM members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
            if old and old["role"] == "admin" and conn.execute("SELECT count(*) FROM members WHERE project_id=? AND role='admin'", (project_id,)).fetchone()[0] <= 1:
                raise HTTPException(409, "至少保留一名项目管理员")
            conn.execute("DELETE FROM members WHERE project_id=? AND user_id=?", (project_id, user_id))
        return {"ok": True}

    @api.get("/api/projects/{project_id}/environments")
    def environments(project_id: int, request: Request):
        scoped(request, project_id)
        return [serialize_environment(settings, row) for row in db.all("SELECT * FROM environments WHERE project_id=? ORDER BY id", (project_id,))]

    def write_environment(project_id, body, old=None):
        hidden = settings.unseal(old["secrets"]) if old else {}
        for key in body.remove_secrets:
            hidden.pop(key, None)
        hidden.update(body.secrets)
        if set(hidden) & set(body.variables):
            raise HTTPException(400, "敏感变量与普通变量不能重名")
        return (body.name, body.web_base_url, body.api_base_url, encode(body.variables),
                settings.seal(hidden), time.time())

    @api.post("/api/projects/{project_id}/environments", status_code=201)
    def create_environment(project_id: int, body: EnvironmentInput, request: Request):
        user = scoped(request, project_id, "tester")
        pk = db.execute("INSERT INTO environments(name,web_base_url,api_base_url,variables,secrets,updated_at,project_id) VALUES(?,?,?,?,?,?,?)",
                        (*write_environment(project_id, body), project_id))
        db.audit(user["id"], project_id, "environment.create", str(pk))
        return serialize_environment(settings, resource("environments", pk, project_id))

    @api.put("/api/projects/{project_id}/environments/{pk}")
    def update_environment(project_id: int, pk: int, body: EnvironmentInput, request: Request):
        user = scoped(request, project_id, "tester")
        old = resource("environments", pk, project_id)
        db.execute("UPDATE environments SET name=?,web_base_url=?,api_base_url=?,variables=?,secrets=?,updated_at=? WHERE id=?",
                   (*write_environment(project_id, body, old), pk))
        db.audit(user["id"], project_id, "environment.update", str(pk))
        return serialize_environment(settings, resource("environments", pk, project_id))

    @api.delete("/api/projects/{project_id}/environments/{pk}")
    def delete_environment(project_id: int, pk: int, request: Request):
        scoped(request, project_id, "tester")
        resource("environments", pk, project_id)
        db.execute("DELETE FROM environments WHERE id=?", (pk,))
        return {"ok": True}

    @api.get("/api/projects/{project_id}/cases")
    def cases(project_id: int, request: Request):
        scoped(request, project_id)
        return [serialize_case(c) for c in db.all("SELECT * FROM cases WHERE project_id=? ORDER BY updated_at DESC", (project_id,))]

    @api.post("/api/projects/{project_id}/cases", status_code=201)
    def create_case(project_id: int, body: CaseInput, request: Request):
        user = scoped(request, project_id, "tester")
        pk = db.execute("INSERT INTO cases(project_id,name,description,tags,steps,updated_at) VALUES(?,?,?,?,?,?)",
                        (project_id, body.name, body.description, encode(body.tags), encode([s.model_dump() for s in body.steps]), time.time()))
        db.audit(user["id"], project_id, "case.create", str(pk))
        return serialize_case(resource("cases", pk, project_id))

    @api.put("/api/projects/{project_id}/cases/{pk}")
    def update_case(project_id: int, pk: int, body: CaseInput, request: Request):
        user = scoped(request, project_id, "tester")
        resource("cases", pk, project_id)
        db.execute("UPDATE cases SET name=?,description=?,tags=?,steps=?,revision=revision+1,updated_at=? WHERE id=?",
                   (body.name, body.description, encode(body.tags), encode([s.model_dump() for s in body.steps]), time.time(), pk))
        db.audit(user["id"], project_id, "case.update", str(pk))
        return serialize_case(resource("cases", pk, project_id))

    @api.delete("/api/projects/{project_id}/cases/{pk}")
    def delete_case(project_id: int, pk: int, request: Request):
        scoped(request, project_id, "tester")
        resource("cases", pk, project_id)
        if any(pk in json.loads(s["case_ids"]) for s in db.all("SELECT case_ids FROM suites WHERE project_id=?", (project_id,))):
            raise HTTPException(409, "请先从套件中移除该用例")
        db.execute("DELETE FROM cases WHERE id=?", (pk,))
        return {"ok": True}

    @api.get("/api/projects/{project_id}/suites")
    def suites(project_id: int, request: Request):
        scoped(request, project_id)
        return [{**s, "case_ids": json.loads(s["case_ids"])} for s in db.all("SELECT * FROM suites WHERE project_id=? ORDER BY id", (project_id,))]

    def checked_cases(project_id, case_ids):
        for pk in dict.fromkeys(case_ids):
            resource("cases", pk, project_id)
        return encode(list(dict.fromkeys(case_ids)))

    @api.post("/api/projects/{project_id}/suites", status_code=201)
    def create_suite(project_id: int, body: SuiteInput, request: Request):
        scoped(request, project_id, "tester")
        pk = db.execute("INSERT INTO suites(project_id,name,description,case_ids) VALUES(?,?,?,?)",
                        (project_id, body.name, body.description, checked_cases(project_id, body.case_ids)))
        return {"id": pk, **body.model_dump()}

    @api.put("/api/projects/{project_id}/suites/{pk}")
    def update_suite(project_id: int, pk: int, body: SuiteInput, request: Request):
        scoped(request, project_id, "tester")
        resource("suites", pk, project_id)
        db.execute("UPDATE suites SET name=?,description=?,case_ids=? WHERE id=?",
                   (body.name, body.description, checked_cases(project_id, body.case_ids), pk))
        return {"id": pk, **body.model_dump()}

    @api.delete("/api/projects/{project_id}/suites/{pk}")
    def delete_suite(project_id: int, pk: int, request: Request):
        scoped(request, project_id, "tester")
        resource("suites", pk, project_id)
        db.execute("DELETE FROM suites WHERE id=?", (pk,))
        return {"ok": True}

    @api.post("/api/projects/{project_id}/runs", status_code=201)
    def submit_run(project_id: int, body: RunInput, request: Request):
        user = scoped(request, project_id, "tester")
        with db.transaction() as conn:
            run_id = new_run(db, conn, project_id, body.model_dump(), user["id"])
        db.audit(user["id"], project_id, "run.create", run_id)
        return {"id": run_id, "status": "queued"}

    @api.post("/api/ci/projects/{project_id}/runs", status_code=201)
    def ci_submit(project_id: int, body: RunInput, request: Request):
        user, token = ci_access(request, project_id, "tester")
        key = request.headers.get("idempotency-key")
        if key is not None and not 1 <= len(key.strip()) <= 128:
            raise HTTPException(400, "幂等键长度为1至128")
        with db.transaction() as conn:
            run_id = new_run(db, conn, project_id, body.model_dump(), user["id"], "ci", token["id"], key)
        return serialize_run(db.one(f"SELECT {RUN_COLUMNS} FROM runs WHERE id=?", (run_id,)))

    @api.get("/api/projects/{project_id}/runs")
    def list_runs(project_id: int, request: Request):
        scoped(request, project_id)
        return [serialize_run(row) for row in db.all(f"SELECT {RUN_COLUMNS} FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 200", (project_id,))]

    @api.get('/api/projects/{project_id}/assets/export')
    def asset_export(project_id: int, request: Request):
        user = scoped(request, project_id, 'tester')
        try:
            result = export_assets(db, project_id)
        except ValueError:
            raise HTTPException(413, '资产超过单包上限，请按业务拆分项目后导出')
        if len(encode(result).encode()) > 1900 * 1024:
            raise HTTPException(413, '导出包超过可再次导入的大小上限，请按业务拆分项目')
        db.audit(user['id'], project_id, 'assets.export')
        return JSONResponse(result, headers={'Content-Disposition': 'attachment; filename="testdock-assets.json"'})

    @api.get('/api/projects/{project_id}/examples/sales')
    def sales_template(project_id: int, request: Request):
        scoped(request, project_id)
        return FileResponse(Path(__file__).resolve().parents[2] / 'examples/chemical-sales.json',
                            filename='chemical-sales.json', headers={'Cache-Control': 'no-store'})

    @api.post('/api/projects/{project_id}/assets/import')
    def asset_import(project_id: int, body: ImportInput, request: Request):
        user = scoped(request, project_id, 'tester')
        result = import_assets(db, project_id, body)
        if not body.dry_run:
            db.audit(user['id'], project_id, 'assets.import', result['counts'])
        return result

    @api.post('/api/projects/{project_id}/openapi/preview')
    def preview_openapi(project_id: int, body: OpenAPIInput, request: Request):
        scoped(request, project_id, 'tester')
        try:
            return openapi_preview(body.document)
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            raise HTTPException(400, '无法解析文档，需要 OpenAPI 3.0/3.1 JSON、有效路径及本地引用')

    @api.post('/api/projects/{project_id}/runs/matrix', status_code=201)
    def matrix(project_id: int, body: MatrixInput, request: Request):
        user = scoped(request, project_id, 'tester')
        with db.transaction() as conn:
            ids = [new_run(db, conn, project_id, {'environment_id': e, 'suite_id': body.suite_id, 'case_ids': body.case_ids}, user['id'])
                   for e in dict.fromkeys(body.environment_ids)]
        db.audit(user['id'], project_id, 'run.matrix', ','.join(ids))
        return {'runs': [{'id': i, 'status': 'queued'} for i in ids]}

    @api.post('/api/runs/{run_id}/rerun', status_code=201)
    def rerun(run_id: str, request: Request, failed_only: bool = True, use_latest: bool = False):
        old = db.one('SELECT * FROM runs WHERE id=?', (run_id,))
        if not old: raise HTTPException(404, '执行不存在')
        user = scoped(request, old['project_id'], 'tester')
        if old['status'] not in TERMINAL: raise HTTPException(409, '请等待原执行结束')
        snapshot = settings.unseal(old['snapshot'])
        if failed_only:
            passed = {r['case_id'] for r in db.all("SELECT case_id FROM case_results WHERE run_id=? AND status='passed'", (run_id,))}
            snapshot['cases'] = [c for c in snapshot['cases'] if c['id'] not in passed]
        if not snapshot['cases']: raise HTTPException(409, '没有需要重跑的用例')
        if use_latest:
            with db.transaction() as conn:
                pk = new_run(db, conn, old['project_id'], {'environment_id': snapshot['environment']['id'],
                    'case_ids': [c['id'] for c in snapshot['cases']]}, user['id'])
                conn.execute('UPDATE runs SET name=? WHERE id=?', ('最新配置重跑 #' + run_id[:8], pk))
            db.audit(user['id'], old['project_id'], 'run.rerun_latest', run_id + ' -> ' + pk)
            return {'id': pk, 'status': 'queued'}
        pk = uuid.uuid4().hex
        from .notifications import targets
        with db.transaction() as conn:
            selected,mode=targets(conn,old['project_id'],'manual')
            conn.execute('INSERT INTO runs(id,project_id,suite_id,name,environment_name,snapshot,trigger,user_id,created_at,notify_targets,notify_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (pk,old['project_id'],old['suite_id'],('重跑 #'+run_id[:8]+' · '+old['name'])[:200],old['environment_name'],
                 settings.seal(snapshot),'manual',user['id'],time.time(),encode(selected),mode))
        db.audit(user['id'], old['project_id'], 'run.rerun', run_id + ' -> ' + pk)
        return {'id': pk, 'status': 'queued'}

    @api.get('/api/projects/{project_id}/runs/search')
    def search_runs(project_id: int, request: Request, q: str = Query('', max_length=200),
                    status: str = '', environment: str = '', trigger: str = '',
                    since: float | None = None, until: float | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        scoped(request, project_id)
        if status and status not in TERMINAL | {'queued', 'running'}: raise HTTPException(400, '状态无效')
        if trigger and trigger not in ('manual', 'schedule', 'ci'): raise HTTPException(400, '触发方式无效')
        if since is not None and until is not None and since > until: raise HTTPException(400, '时间区间无效')
        clauses, args = ['project_id=?'], [project_id]
        for col, val in [('status', status), ('environment_name', environment), ('trigger', trigger)]:
            if val: clauses.append(col + '=?'); args.append(val)
        if q:
            clauses.append('(instr(name,?)>0 OR instr(id,?)>0)'); args.extend([q, q])
        if since is not None: clauses.append('created_at>=?'); args.append(since)
        if until is not None: clauses.append('created_at<=?'); args.append(until)
        where = ' AND '.join(clauses)
        with db.connect() as conn:
            counts = {r['status']: r['n'] for r in conn.execute(f'SELECT status,count(*) n FROM runs WHERE {where} GROUP BY status', args)}
            rows = conn.execute(f'SELECT {RUN_COLUMNS} FROM runs WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*args, page_size, (page - 1) * page_size)).fetchall()
        return {'items': [serialize_run(r) for r in rows], 'total': sum(counts.values()), 'counts': counts, 'page': page, 'page_size': page_size}

    def run_detail(run):
        result = serialize_run({key: run[key] for key in RUN_COLUMNS.split(",")})
        result["results"] = [{**c, "steps": json.loads(c["steps"])} for c in db.all("SELECT * FROM case_results WHERE run_id=? ORDER BY id", (run["id"],))]
        run_dir = settings.data_dir / "runs" / run["id"]
        result["artifacts"] = [str(p.relative_to(run_dir)) for p in sorted(run_dir.rglob("*")) if p.is_file() and p.suffix in (".png", ".zip")]
        progress = run_dir / "progress.json"
        if progress.exists():
            try:
                result["progress"] = json.loads(progress.read_text())
            except (ValueError, OSError):
                pass
        snap = settings.unseal(run["snapshot"])
        result["case_revisions"] = [{"id": c["id"], "name": c["name"], "revision": c["revision"]} for c in snap["cases"]]
        return result

    @api.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise HTTPException(404, "执行不存在")
        scoped(request, run["project_id"])
        return run_detail(run)

    @api.get("/api/ci/runs/{run_id}")
    def ci_get_run(run_id: str, request: Request):
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise HTTPException(404, "执行不存在")
        ci_access(request, run["project_id"])
        return run_detail(run)

    @api.post("/api/runs/{run_id}/cancel")
    @api.post("/api/ci/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request):
        with db.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, "执行不存在")
            if request.url.path.startswith("/api/ci/"):
                ci_access(request, run["project_id"], "tester")
            else:
                scoped(request, run["project_id"], "tester")
            if run["status"] not in TERMINAL:
                if run["status"] == "queued":
                    conn.execute("UPDATE runs SET status='cancelled',cancel_requested=1,finished_at=? WHERE id=?", (time.time(), run_id))
                    from .notifications import finish_run
                    finish_run(db,conn,run_id)
                else:
                    conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        return {"ok": True}

    @api.get("/api/runs/{run_id}/artifacts/{filename:path}")
    def artifact(run_id: str, filename: str, request: Request):
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise HTTPException(404, "执行不存在")
        if request.headers.get("authorization", "").startswith("Bearer "):
            ci_access(request, run["project_id"])
        else:
            scoped(request, run["project_id"])
        base = (settings.data_dir / "runs" / run_id).resolve()
        path = (base / filename).resolve()
        if not path.is_relative_to(base) or path.suffix not in (".png", ".zip") or not path.is_file():
            raise HTTPException(404, "附件不存在")
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    @api.get("/api/runs/{run_id}/junit.xml")
    def junit(run_id: str, request: Request):
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise HTTPException(404, "执行不存在")
        if request.headers.get("authorization", "").startswith("Bearer "):
            ci_access(request, run["project_id"])
        else:
            scoped(request, run["project_id"])
        if run["status"] not in TERMINAL:
            raise HTTPException(409, "执行尚未结束，暂不能导出JUnit")
        rows = db.all("SELECT * FROM case_results WHERE run_id=?", (run_id,))
        root = Element("testsuite", name=run["name"], tests=str(len(rows)))
        for item in rows:
            element = SubElement(root, "testcase", name=item["name"], time=str(item["duration"]))
            if item["status"] != "passed":
                tag = "failure" if item["status"] == "failed" else "skipped" if item["status"] in ("skipped", "cancelled") else "error"
                SubElement(element, tag).text = encode(json.loads(item["steps"]))
        if run["status"] in ("error", "cancelled") and not any(r['status'] in ('error', 'cancelled') for r in rows):
            element = SubElement(root, "testcase", name="执行器")
            SubElement(element, "error").text = run["error"] or run["status"]
        root.set("tests", str(len(root)))
        for tag, attribute in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
            root.set(attribute, str(len(root.findall('./testcase/' + tag))))
        return Response(tostring(root, encoding="unicode"), media_type="application/xml")

    from .notification_api import attach_notifications
    attach_notifications(api,db,scoped,resource,project_access,actor)

    @api.get("/api/projects/{project_id}/tokens")
    def tokens(project_id: int, request: Request):
        scoped(request, project_id, "admin")
        return db.all("SELECT id,name,user_id,created_at,expires_at FROM ci_tokens WHERE project_id=?", (project_id,))

    @api.post("/api/projects/{project_id}/tokens", status_code=201)
    def create_token(project_id: int, body: TokenInput, request: Request):
        user = scoped(request, project_id, "admin")
        value = "td_" + secrets.token_urlsafe(32)
        pk = db.execute("INSERT INTO ci_tokens(project_id,user_id,name,token_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                        (project_id, user["id"], body.name, token_hash(value), time.time(), time.time() + body.expires_days * 86400))
        db.audit(user["id"], project_id, "token.create", str(pk))
        return {"id": pk, "token": value}

    @api.delete("/api/projects/{project_id}/tokens/{pk}")
    def revoke_token(project_id: int, pk: int, request: Request):
        user = scoped(request, project_id, "admin")
        resource("ci_tokens", pk, project_id)
        db.execute("DELETE FROM ci_tokens WHERE id=?", (pk,))
        db.audit(user["id"], project_id, "token.revoke", str(pk))
        return {"ok": True}

    @api.get("/api/projects/{project_id}/audit")
    def audit(project_id: int, request: Request):
        scoped(request, project_id, "admin")
        return db.all("SELECT a.*,u.username FROM audit_logs a LEFT JOIN users u ON a.user_id=u.id WHERE project_id=? ORDER BY a.id DESC LIMIT 100", (project_id,))

    from .ai_bridge import attach_bridge
    attach_bridge(api, db, actor, project_access, run_detail)
    from .ai_api import attach_assistant
    attach_assistant(api,db,actor,project_access)
    api.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return api


def app():
    return create_app()
