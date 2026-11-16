import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

from .core import Database, Settings, encode, password_hash, private_write


def initialize(config_path=None):
    config = Path(config_path or os.environ.get("TESTDOCK_CONFIG", "/home/nanshan/workspace/services/autotest-platform/config/settings.json"))
    if not config.exists():
        service = config.parent.parent
        private_write(config, json.dumps({"data_dir": str(service / "data"), "key_file": str(config.parent / "master.key"),
                                         "secure_cookie": False, "run_timeout": 900, "step_timeout": 15}, indent=2))
    os.environ["TESTDOCK_CONFIG"] = str(config)
    settings = Settings.load()
    db = Database(settings)
    db.initialize()
    if not db.one("SELECT id FROM users LIMIT 1"):
        password = secrets.token_urlsafe(16)
        db.execute("INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
                   ("admin", "管理员", password_hash(password), 1, time.time()))
        credential = config.parent / "initial-admin.txt"
        private_write(credential, "TestDock 初始管理员\n账号：admin\n密码：" + password + "\n本机访问：http://127.0.0.1:8088\n首次登录后请在账户设置中修改密码。\n")
        print("初始化完成；初始登录信息保存于 " + str(credential))
    else:
        print("数据库已初始化，保留现有账号")
    return db


def seed_demo(db):
    if db.one("SELECT id FROM projects WHERE name='示例 · Web 与接口回归'"):
        print("演示项目已存在，保留当前内容")
        return
    user = db.one("SELECT id FROM users WHERE is_admin=1 AND active=1 ORDER BY id LIMIT 1")
    now = time.time()
    with db.transaction() as conn:
        project = conn.execute("INSERT INTO projects(name,description,created_by,created_at) VALUES(?,?,?,?)",
                               ("示例 · Web 与接口回归", "两套真实本地演示目标，展示页面登录、接口链路和被测环境切换。", user["id"], now)).lastrowid
        conn.execute("INSERT INTO members VALUES(?,?,?)", (project, user["id"], "admin"))
        for label, port in (("开发演示", 8091), ("测试演示", 8092)):
            conn.execute("INSERT INTO environments(project_id,name,web_base_url,api_base_url,variables,secrets,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (project, label, f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}",
                          encode({"username": "tester", "greeting": label + "，欢迎 tester", "environment": label}),
                          db.settings.seal({"password": "demo-pass"}), now))
        web_steps = [
            {"action": "goto", "name": "打开演示商店", "url": "/"},
            {"action": "fill", "name": "输入用户名", "locator_type": "testid", "locator": "username", "value": "{{username}}"},
            {"action": "fill", "name": "输入密码", "locator_type": "testid", "locator": "password", "value": "{{password}}"},
            {"action": "click", "name": "提交登录", "locator_type": "testid", "locator": "login"},
            {"action": "assert_text", "name": "核对环境和欢迎语", "locator_type": "testid", "locator": "welcome", "value": "{{greeting}}"},
        ]
        api_steps = [
            {"action": "http", "name": "登录并提取会话", "url": "/api/session", "method": "POST",
             "body": {"username": "{{username}}", "password": "{{password}}"}, "assertions": [{"source": "status", "operator": "eq", "expected": 200}],
             "extract": [{"name": "session_token", "source": "json", "expression": "token", "secret": True}]},
            {"action": "http", "name": "验证环境与接口结果", "url": "/api/protected", "headers": {"Authorization": "Bearer {{session_token}}"},
             "assertions": [{"source": "status", "operator": "eq", "expected": 200},
                            {"source": "json", "expression": "environment", "operator": "eq", "expected": "{{environment}}"},
                            {"source": "json", "expression": "items[0].id", "operator": "eq", "expected": 101}]},
        ]
        from .models import CaseInput
        case_ids = []
        for name, steps, tags in (("Web · 登录与环境校验", web_steps, ["web", "smoke"]),
                                  ("API · 登录提取与鉴权链路", api_steps, ["api", "smoke"])):
            case = CaseInput(name=name, steps=steps, tags=tags)
            case_ids.append(conn.execute("INSERT INTO cases(project_id,name,description,tags,steps,updated_at) VALUES(?,?,?,?,?,?)",
                                         (project, name, "可切换开发与测试演示环境", encode(tags), encode([s.model_dump() for s in case.steps]), now)).lastrowid)
        conn.execute("INSERT INTO suites(project_id,name,description,case_ids) VALUES(?,?,?,?)",
                     (project, "核心冒烟回归", "真实浏览器登录 + HTTP 鉴权链路，在两套环境复用。", encode(case_ids)))
    print("演示项目已建立；对应目标监听8091和8092，仅供本机执行器使用")


def backup(db, root=None):
    root = Path(root or "/home/nanshan/workspace/backups/autotest-platform")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    size = sum(p.stat().st_size for p in db.settings.data_dir.rglob('*') if p.is_file())
    if shutil.disk_usage(root).free < 3 * 1024**3 + size * 2:
        raise SystemExit("备份空间不足，未删除任何已有快照")
    target = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3))
    target.mkdir(mode=0o700)
    with sqlite3.connect(db.path) as source, sqlite3.connect(target / "testdock.sqlite3") as destination:
        source.backup(destination)
    shutil.copy2(db.settings.key_file, target / "master.key")
    bridge = Path(os.environ.get('TESTDOCK_AI_BRIDGE_KEY_FILE', '/home/nanshan/workspace/services/autotest-platform/config/ai-bridge.key'))
    if bridge.is_file():
        shutil.copy2(bridge, target / 'ai-bridge.key')
    private_write(target / 'settings.json', json.dumps({k: str(v) if isinstance(v, Path) else v
                  for k, v in asdict(db.settings).items()}, ensure_ascii=False, indent=2))
    source_root = Path(__file__).resolve().parents[2]
    for name in ('src', 'docs', 'scripts', 'deploy', 'config', 'tests', 'examples'):
        if (source_root / name).is_dir():
            shutil.copytree(source_root / name, target / 'source' / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env', '*.key'))
    for name in ('README.md', 'requirements.in', 'requirements.lock', 'pyproject.toml', 'LICENSE', 'THIRD_PARTY.md', 'AGENTS.md'):
        if (source_root / name).is_file():
            shutil.copy2(source_root / name, target / 'source' / name)
    # Preserve only completed runs so attachments cannot change while being copied.
    for run in db.all("SELECT id FROM runs WHERE status IN ('passed','failed','error','cancelled')"):
        source = db.settings.data_dir / "runs" / run["id"]
        if source.is_dir():
            shutil.copytree(source, target / "runs" / run["id"])
    if (db.settings.data_dir/'ai').is_dir():
        shutil.copytree(db.settings.data_dir/'ai',target/'ai')
    manifest = {"created_at": time.time(), "source_database": str(db.path), "source_code": str(source_root),
                "version": "1.3.0", "files": {}}
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
            manifest["files"][str(path.relative_to(target))] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            path.chmod(0o700)
    private_write(target / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print("备份完成：" + str(target))
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "seed-demo", "backup", "migrate"])
    parser.add_argument("--config")
    args = parser.parse_args()
    if args.config:
        os.environ["TESTDOCK_CONFIG"] = str(Path(args.config).resolve())
    os.umask(0o077)
    if args.command == "init":
        initialize(args.config)
    else:
        settings = Settings.load()
        db = Database(settings)
        db.initialize(upgrade=args.command=='migrate')
        if args.command=='migrate':
            print('schema3迁移完成；原业务保留，AI默认关闭')
            return
        if args.command == "seed-demo":
            seed_demo(db)
        else:
            backup(db)


if __name__ == "__main__":
    main()
