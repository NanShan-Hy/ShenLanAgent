#!/usr/bin/env python3
"""Verify exported module files without importing or starting any application."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-sources', action='store_true',
                        help='also compare with the original local working trees')
    parser.add_argument('--workspace', type=Path,
                        default=Path('/home/nanshan/workspace'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'source-manifest.json').read_text())
    errors = []
    count = 0
    for module in manifest['modules']:
        folder = root / 'modules' / module['name']
        expected = {entry['path'] for entry in module['files']}
        actual = {p.relative_to(folder).as_posix()
                  for p in folder.rglob('*') if p.is_file() or p.is_symlink()}
        for path in sorted(expected - actual):
            errors.append(f"missing: {module['name']}/{path}")
        for path in sorted(actual - expected):
            errors.append(f"extra: {module['name']}/{path}")
        source = args.workspace / module['source_workspace_path']
        if args.check_sources:
            if not (source / '.git').exists():
                errors.append(f'not a local source repository: {source}')
            else:
                proc = subprocess.run(
                    ['git', '-C', str(source), 'ls-files', '-z',
                     '--cached', '--others', '--exclude-standard'],
                    capture_output=True, check=True)
                source_files = {p.decode('utf-8') for p in proc.stdout.split(b'\0')
                                if p and (source / p.decode('utf-8')).is_file()}
                if source_files != expected:
                    errors.append(f"source file list changed: {module['name']}")
        if module['file_count'] != len(module['files']):
            errors.append(f"manifest count mismatch: {module['name']}")
        if module['bytes'] != sum(e['bytes'] for e in module['files']):
            errors.append(f"manifest size mismatch: {module['name']}")
        for entry in module['files']:
            rel = Path(entry['path'])
            if rel.is_absolute() or '..' in rel.parts:
                errors.append(f'unsafe manifest path: {rel}')
                continue
            paths = [folder / rel]
            if args.check_sources:
                paths.append(source / rel)
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    errors.append(f'not a regular file: {path}')
                    continue
                data = path.read_bytes()
                if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                    errors.append(f'content changed: {path}')
                if bool(path.stat().st_mode & 0o100) != (entry['mode'] == '100755'):
                    errors.append(f'executable mode changed: {path}')
            count += 1
    print(json.dumps({'ok': not errors, 'modules': len(manifest['modules']),
                      'files': count, 'sources_checked': args.check_sources,
                      'errors': errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
