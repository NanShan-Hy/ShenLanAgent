#!/usr/bin/env python3
"""Read-only local service and health probes; never invokes a model or sends notifications."""
import json
import subprocess
import urllib.request


def main():
    names = ['gpt-account-pool', 'ai-gateway', 'ai-workbench', 'ai-workbench-worker',
             'testdock', 'testdock-worker', 'testdock-notifier', 'testdock-ai-worker',
             'testdock-demo-dev', 'testdock-demo-test']
    results = []
    for name in names:
        p = subprocess.run(['systemctl', '--user', 'is-active', name + '.service'],
                           capture_output=True, text=True, timeout=10)
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', name + '.service'],
                                 capture_output=True, text=True, timeout=10)
        results.append({'unit': name, 'active': p.stdout.strip(),
                        'enabled': enabled.stdout.strip(),
                        'ok': p.returncode == 0 and enabled.returncode == 0})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    probes = [(8088, '/api/health', ['worker_online', 'notifier_online']),
              (8090, '/api/v1/health', ['worker_online']),
              (8094, '/api/v1/health', ['database_online', 'maintenance_online']),
              (8095, '/', []), (8091, '/api/info', ['ready']), (8092, '/api/info', ['ready'])]
    for port, path, fields in probes:
        try:
            with opener.open(f'http://127.0.0.1:{port}{path}', timeout=8) as response:
                body = json.loads(response.read(8192))
                ok = response.status == 200 and all(body.get(key) is True for key in fields)
                if port in [8088, 8090, 8094]:
                    ok = ok and body.get('status') == 'ok'
                results.append({'port': port, 'path': path, 'ok': ok, 'health': body})
        except Exception as error:
            results.append({'port': port, 'path': path, 'ok': False, 'error': str(error)})
    ok = all(row['ok'] for row in results)
    print(json.dumps({'ok': ok, 'checks': results,
                      'not_tested': ['real model generation', 'external Oray mapping',
                                     'notification delivery', 'host reboot or logout']},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
