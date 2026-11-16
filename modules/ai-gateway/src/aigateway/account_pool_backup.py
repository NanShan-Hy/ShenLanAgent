"""Back up the stopped standalone backend; recovered copies require fresh authorizations."""
import hashlib
import json
from pathlib import Path
import secrets
import shutil
from datetime import datetime,timezone

from .account_pool_config import VERSION,render_backend
from .core import private_write


def secure_tree(root):
    for path in [root,*root.rglob('*')]:
        if path.is_symlink():raise ValueError('账号池备份不允许符号链接')
        path.chmod(0o700 if path.is_dir() else 0o600)


def snapshot(service,root,*,unit=None):
    """Caller must stop the backend after draining gateway requests before using this."""
    service,root=Path(service),Path(root)
    if shutil.disk_usage(service).free<3*1024**3:raise RuntimeError('可用空间低于3GiB')
    for path in service.rglob('*'):
        if path.is_symlink():raise ValueError('服务目录包含符号链接，未备份')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3))
    target.mkdir(mode=0o700)
    for name in ('config','data','app'):shutil.copytree(service/name,target/name)
    if unit and Path(unit).is_file():shutil.copy2(unit,target/'gpt-account-pool.service')
    hashes={str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}
    private_write(target/'manifest.json',json.dumps({'version':VERSION,'source':str(service),'sha256':hashes,
        'created_at':datetime.now(timezone.utc).isoformat(),'backend_stopped_before_snapshot':True},indent=2))
    secure_tree(target)
    return target


def verify(snapshot_path,output):
    source=Path(snapshot_path).resolve();output=Path(output).resolve()
    if output.exists():raise ValueError('恢复目标已存在，不覆盖')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('账号池快照不允许符号链接')
    manifest=json.loads((source/'manifest.json').read_text())
    for rel,expected in manifest['sha256'].items():
        path=(source/rel).resolve()
        if not path.is_relative_to(source) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('账号池快照校验失败')
    output.mkdir(parents=True,mode=0o700)
    # The old configuration and credentials are preserved for examination, never loaded by a new process.
    preserved=output/'archived-source'
    for name in ('config','data','app'):shutil.copytree(source/name,preserved/name)
    for name in ('config','data/credentials','logs','app'):(output/name).mkdir(parents=True,mode=0o700,exist_ok=True)
    client='pool_'+secrets.token_urlsafe(32);management=secrets.token_urlsafe(40)
    private_write(output/'config/connector.json',json.dumps({'version':VERSION,'base_url':'http://127.0.0.1:8097/v1',
        'client_key':client,'management_key':management},indent=2),exclusive=True)
    private_write(output/'config/config.yaml',json.dumps(render_backend(output,port=8097,client_key=client,management_key=management),indent=2),exclusive=True)
    result={'files_verified':len(manifest['sha256']),'archived_credential_files':len(list((preserved/'data/credentials').glob('*.json'))),
        'active_credential_files':0,'new_internal_keys':True,'automatic_start':False,'reauthorization_required':True,
        'original_service_unchanged':True}
    private_write(output/'verification.json',json.dumps(result,indent=2));secure_tree(output)
    return result
