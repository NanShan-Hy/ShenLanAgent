import socket
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager

import uvicorn

BROWSER_LIBS = '/home/nanshan/workspace/tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu'


@contextmanager
def serve(app, *, lifespan='auto', port=0):
    sock = socket.socket()
    sock.bind(('127.0.0.1', port))
    sock.listen(64)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, proxy_headers=False, lifespan=lifespan))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    if not server.started:
        raise RuntimeError('local QA server did not start')
    try:
        yield 'http://127.0.0.1:' + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(25)
        sock.close()
        assert not thread.is_alive(), 'QA server did not stop'


def run_cli(args,env,tmp_path,stage):
    from types import SimpleNamespace
    # Each acceptance client has an empty native config directory. Do not load
    # the developer's login, provider settings or unrelated MCP connections.
    client_config=tmp_path/(stage+'-codex-config')
    client_config.mkdir(mode=0o700)
    child_env={**env,'CODEX_HOME':str(client_config)}
    out,err=tmp_path/(stage+'.stdout'),tmp_path/(stage+'.stderr')
    for p in (out,err):p.touch(mode=0o600)
    with out.open('w') as stdout,err.open('w') as stderr:
        process=subprocess.Popen(args,env=child_env,stdin=subprocess.DEVNULL,start_new_session=True,
                                 stdout=stdout,stderr=stderr,text=True)
        (tmp_path/(stage+'.process.json')).write_text(json.dumps({'pid':process.pid,'parent':os.getpid(),'pgid':os.getpgid(process.pid)}))
        try:
            result=process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
            raise
    return SimpleNamespace(returncode=result,stderr=err.read_text())
