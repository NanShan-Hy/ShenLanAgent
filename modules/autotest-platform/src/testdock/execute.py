import os
import signal
import sys
import threading
import time
from .core import Database, Settings
from .engine import execute_snapshot


def interrupt(signum, frame):
    raise KeyboardInterrupt()


def main():
    os.umask(0o077)
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
    threading.Thread(target=watch_parent, daemon=True).start()
    signal.signal(signal.SIGTERM, interrupt)
    settings = Settings.load()
    db = Database(settings)
    run = db.one("SELECT snapshot FROM runs WHERE id=?", (sys.argv[1],))
    execute_snapshot(settings.unseal(run["snapshot"]), settings.data_dir / "runs" / sys.argv[1], settings.step_timeout, settings.browser_library_path)


if __name__ == "__main__":
    main()
