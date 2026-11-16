#!/usr/bin/env python3
"""Run a suite from CI; token is read only from TESTDOCK_CI_TOKEN."""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--project', type=int, required=True)
    parser.add_argument('--suite', type=int, required=True)
    parser.add_argument('--environment', type=int, required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--junit', type=Path, default=Path('junit.xml'))
    parser.add_argument('--idempotency-key', default=os.environ.get('CI_BUILD_ID') or str(uuid.uuid4()))
    parser.add_argument('--trust-proxy-env', action='store_true', help='显式使用系统代理；内网默认直连')
    args = parser.parse_args()
    token = os.environ.get('TESTDOCK_CI_TOKEN')
    if not token:
        parser.error('请通过 CI 凭据注入 TESTDOCK_CI_TOKEN')
    base = args.url.rstrip('/')
    opener = build_opener() if args.trust_proxy_env else build_opener(ProxyHandler({}))

    def call(path, body=None, raw=False, key=None):
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with opener.open(req, timeout=30) as response:
            data = response.read()
            return data if raw else json.loads(data)

    try:
        run = call(f'/api/ci/projects/{args.project}/runs',
                   {'suite_id': args.suite, 'environment_id': args.environment}, key=args.idempotency_key)
        run_id = run['id']
        print('执行编号：' + run_id, flush=True)
        deadline = time.monotonic() + args.timeout
        while run['status'] in ('queued', 'running'):
            if time.monotonic() >= deadline:
                call(f'/api/ci/runs/{run_id}/cancel', {})
                print('等待超时，已请求取消；请在平台确认终态。', file=sys.stderr)
                return 2
            time.sleep(2)
            run = call('/api/ci/runs/' + run_id)
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        args.junit.write_bytes(call(f'/api/runs/{run_id}/junit.xml', raw=True))
        print('结果：' + run['status'] + '；JUnit：' + str(args.junit))
        return 0 if run['status'] == 'passed' else 1
    except HTTPError as exc:
        print(f'平台请求失败：HTTP {exc.code}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print('无法完成 CI 执行：' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
