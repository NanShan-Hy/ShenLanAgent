import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi.testclient import TestClient

from testdock.app import create_app
from testdock.core import Database, Settings, password_hash, private_write
from testdock.cli import seed_demo
from testdock.demo import app as demo_app

BROWSER_LIBS = "/home/nanshan/workspace/tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu"
PASSWORD = "test-password-123"


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture
def platform(tmp_path, monkeypatch):
    config = tmp_path / "config/settings.json"
    private_write(config, json.dumps({"data_dir": str(tmp_path / "data"),
        "key_file": str(tmp_path / "config/master.key"), "browser_library_path": BROWSER_LIBS,
        "min_free_bytes": 0, "run_timeout": 30}))
    monkeypatch.setenv("TESTDOCK_CONFIG", str(config))
    settings = Settings.load()
    db = Database(settings)
    db.initialize()
    db.execute("INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
               ("admin", "管理员", password_hash(PASSWORD), 1, time.time()))
    seed_demo(db)
    api = create_app(settings)
    with TestClient(api, headers={"X-Requested-With": "TestDock"}) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
        yield client, db, settings, api


@pytest.fixture
def targets(platform, monkeypatch):
    client, db, settings, api = platform
    with serve(demo_app()) as first:
        monkeypatch.setenv("TESTDOCK_DEMO_LABEL", "测试演示")
        with serve(demo_app()) as second:
            for pk, url, label in [(1, first, "开发演示"), (2, second, "测试演示")]:
                db.execute("UPDATE environments SET web_base_url=?,api_base_url=?,variables=? WHERE id=?",
                           (url, url, json.dumps({"username": "tester", "environment": label,
                            "greeting": label + "，欢迎 tester"}), pk))
            yield platform
