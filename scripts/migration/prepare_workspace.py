#!/usr/bin/env python3
"""Prepare source/app directories only; default is a read-only plan."""
import argparse
import json
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path('/home/nanshan/workspace'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    workspace = args.workspace.resolve()
    modules = ['ai-gateway', 'ai-workbench', 'autotest-platform']
    destinations = [workspace / 'projects' / name for name in modules]
    destinations += [workspace / 'services' / name / 'app' for name in modules]
    conflicts = [str(p) for p in destinations if p.exists() or p.is_symlink()]
    print(json.dumps({'apply': args.apply, 'workspace': str(workspace),
                      'source_and_app_targets': [str(p) for p in destinations],
                      'existing_targets': conflicts,
                      'path_note': 'Existing module code and unit templates use /home/nanshan/workspace; review absolute paths if changed.'},
                     ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if conflicts:
        parser.error('existing targets must be reviewed; this script never overwrites them')
    # Check every destination before making any changes.
    for name in modules:
        if not (repo / 'modules' / name / 'requirements.lock').is_file():
            parser.error(f'missing module: {name}')
    os.umask(0o027)
    for folder in ['projects', 'services', 'tools', 'data', 'downloads/installers', 'archives']:
        (workspace / folder).mkdir(parents=True, exist_ok=True, mode=0o750)
    for folder in ['tmp', 'backups']:
        p = workspace / folder
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in modules:
        source = repo / 'modules' / name
        shutil.copytree(source, workspace / 'projects' / name,
                        ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.pytest_cache'))
        shutil.copytree(source, workspace / 'services' / name / 'app',
                        ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.pytest_cache'))
    for name in [*modules, 'gpt-account-pool']:
        for folder in ['app', 'logs', 'config', 'data']:
            p = workspace / 'services' / name / folder
            p.mkdir(parents=True, exist_ok=True, mode=0o700 if folder in ['config', 'data'] else 0o750)
            if folder in ['config', 'data']:
                p.chmod(0o700)
        p = workspace / 'tmp' / name
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    print('Prepared sources and app directories. Restore private config/data and dependencies before installing or starting units.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
