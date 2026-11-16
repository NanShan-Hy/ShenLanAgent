"""Independent single assistant worker. Interrupted provider calls are never replayed."""
import fcntl
import os
import threading
import time
from .core import Database,Settings,private_write
from .ai_runtime import AIRuntime


def main():
    os.umask(0o077);db=Database(Settings.load());db.initialize()
    with (db.settings.data_dir/'ai-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db.execute("UPDATE ai_tasks SET status='unknown',error='AI执行器中断，请查询状态后恢复；原请求未重放' WHERE status='running'")
        def heartbeat():
            while True:private_write(db.settings.data_dir/'ai-heartbeat',str(time.time()));time.sleep(2)
        threading.Thread(target=heartbeat,daemon=True).start()
        worker=AIRuntime(db)
        while True:
            if not worker.run_once():time.sleep(1)


if __name__=='__main__':main()
