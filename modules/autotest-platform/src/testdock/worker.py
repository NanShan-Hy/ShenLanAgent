import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .core import Database, Settings, encode, private_write
from .services import new_run, next_due


from .scheduling import scheduler_tick
from .notifications import finish_run


class Worker:
    def __init__(self, settings=None):
        self.settings = settings or Settings.load()
        self.db = Database(self.settings)
        self.db.initialize()

    def heartbeat(self):
        private_write(self.settings.data_dir / "worker-heartbeat", str(time.time()))

    def recover(self):
        with self.db.transaction() as conn:
            interrupted=conn.execute("SELECT id FROM runs WHERE status='running'").fetchall()
            conn.execute("UPDATE runs SET status='error',finished_at=?,error='执行器中断，需手动重新运行' WHERE status='running'", (time.time(),))
            for row in interrupted:finish_run(self.db,conn,row['id'])

    def claim(self):
        with self.db.transaction() as conn:
            job = conn.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not job:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=? AND status='queued'", (time.time(), job["id"]))
            return dict(job)

    def run_once(self):
        self.heartbeat()
        scheduler_tick(self.db)
        job = self.claim()
        if not job:
            return False
        run_dir = self.settings.data_dir / "runs" / job["id"]
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        error, forced_status = "", None
        result = {}
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_bytes:
            forced_status, error = "error", "磁盘可用空间不足3GB，未启动执行"
        else:
            log_file = run_dir / "executor.log"
            fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as log:
                process = subprocess.Popen([sys.executable, "-m", "testdock.execute", job["id"]],
                                           stdout=log, stderr=log, start_new_session=True,
                                           env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]) +
                                                (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')})
                try:
                    began = time.monotonic()
                    while process.poll() is None:
                        self.heartbeat()
                        current = self.db.one("SELECT cancel_requested FROM runs WHERE id=?", (job["id"],))
                        if current["cancel_requested"] or time.monotonic() - began > self.settings.run_timeout:
                            forced_status = "cancelled" if current["cancel_requested"] else "error"
                            error = "用户取消" if forced_status == "cancelled" else "执行超过总时间限制"
                            try:
                                os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait()
                            break
                        scheduler_tick(self.db)
                        time.sleep(0.25)
                finally:
                    # Also clean up on database/scheduler errors while a child is alive.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                result_file = run_dir / "result.json"
                if result_file.exists():
                    try:
                        result = json.loads(result_file.read_text())
                    except (ValueError, OSError):
                        pass
                if not forced_status and (process.returncode != 0 or not result.get("status")):
                    forced_status, error = "error", "执行器异常退出，请检查服务日志"
        status = forced_status or result.get("status", "error")
        rows = result.get("results", [])
        summary = {state: sum(row["status"] == state for row in rows) for state in ("passed", "failed", "error", "cancelled")}
        summary["total"] = len(rows)
        with self.db.transaction() as conn:
            latest = conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (job["id"],)).fetchone()
            if latest["cancel_requested"]:
                status = "cancelled"
            for row in rows:
                conn.execute("INSERT INTO case_results(run_id,case_id,name,status,duration,steps) VALUES(?,?,?,?,?,?)",
                             (job["id"], row["case_id"], row["name"], row["status"], row["duration"], encode(row["steps"])))
            conn.execute("UPDATE runs SET status=?,finished_at=?,error=?,summary=? WHERE id=?",
                         (status, time.time(), error, encode(summary), job["id"]))
            finish_run(self.db,conn,job["id"])
        self.heartbeat()
        return True

    def run(self):
        os.umask(0o077)
        lock_path = self.settings.data_dir / "worker.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("已有执行器正在运行")
            self.recover()
            while True:
                try:
                    if not self.run_once():
                        time.sleep(1)
                except Exception:
                    # Keep the queue alive; startup recovery never replays side effects.
                    import traceback
                    traceback.print_exc()
                    self.recover()
                    time.sleep(2)


if __name__ == "__main__":
    Worker().run()
