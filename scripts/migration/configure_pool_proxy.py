#!/usr/bin/env python3
"""Change only the private account-pool proxy URL; default is a dry run."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--config', type=Path, default=Path('/home/nanshan/workspace/services/gpt-account-pool/config/config.yaml'))
    parser.add_argument('--backup-root', type=Path, default=Path('/home/nanshan/workspace/backups/gpt-account-pool'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme not in ['http', 'https'] or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error('use an HTTP/HTTPS proxy URL without embedded credentials or query parameters')
    config = args.config.resolve()
    repo = Path(__file__).resolve().parents[2]
    if config.is_relative_to(repo):
        parser.error('use a private runtime configuration outside the source repository')
    data = json.loads(config.read_text())
    print(json.dumps({'apply': args.apply, 'config': str(config),
                      'field': 'requests.proxy-url', 'new_url': args.url}, ensure_ascii=False))
    if not args.apply:
        return 0
    backup_root = args.backup_root.resolve()
    if backup_root.is_relative_to(repo):
        parser.error('backups must remain outside the source repository')
    if shutil.disk_usage(config.parent).free < 3 * 1024 ** 3:
        parser.error('less than 3 GiB free; handle storage before changing configuration')
    os.umask(0o077)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = 'proxy-change-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
    snapshot = Path(tempfile.mkdtemp(prefix=prefix, dir=backup_root))
    shutil.copyfile(config, snapshot / 'config.yaml')
    (snapshot / 'config.yaml').chmod(0o600)
    data.setdefault('requests', {})['proxy-url'] = args.url
    fd, filename = tempfile.mkstemp(prefix='.proxy-update-', dir=config.parent)
    with os.fdopen(fd, 'w') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    Path(filename).chmod(0o600)
    os.replace(filename, config)
    print(json.dumps({'updated': True, 'private_backup': str(snapshot),
                      'restart_required': 'gpt-account-pool.service'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
