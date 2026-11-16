"""Bounded encrypted objects for replayable contexts and attachments; no plaintext files."""
import base64
import json
import shutil

from .core import uid
from .security import fail

TURN_RESERVE = 40 * 1024**2


def space(db, c, extra=0, reserve=0):
    row=c.execute('SELECT * FROM managed_settings WHERE id=1').fetchone()
    if row['used_bytes']+row['reserved_bytes']+extra+reserve>row['max_bytes']:
        fail(507,'managed_storage_full','会话与附件容量不足，请手动清理后继续')
    if shutil.disk_usage(db.settings.data_dir).free<db.settings.min_free_bytes+max(0,extra+reserve):
        fail(507,'managed_disk_full','可用磁盘空间不足，尚未派发模型请求')


def put(db, value, kind='context', *, reserved=False):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    return put_bytes(db,raw,kind,reserved=reserved)


def put_bytes(db, raw, kind, *, reserved=False):
    pk=uid();chunks=[]
    start=0;seq=0
    while start<len(raw):
        end=min(start+32768,len(raw))
        if kind!='file':
            while end<len(raw) and raw[end]&192==128:end-=1
        cipher=db.settings.seal({'id':pk,'seq':seq,'data':base64.b64encode(raw[start:end]).decode()})
        chunks.append((pk,seq,cipher,len(cipher)))
        start=end;seq+=1
    size=sum(r[3] for r in chunks)
    with db.transaction() as c:
        if reserved:
            row=c.execute('SELECT * FROM managed_settings WHERE id=1').fetchone()
            if row['reserved_bytes']<size:fail(507,'managed_storage_reservation','托管内容超过预留容量')
            c.execute('UPDATE managed_settings SET reserved_bytes=reserved_bytes-? WHERE id=1',(size,))
        space(db,c,size)
        c.execute('INSERT INTO managed_objects VALUES(?,?,?)',(pk,kind,size))
        c.executemany('INSERT INTO managed_chunks VALUES(?,?,?,?)',chunks)
        c.execute('UPDATE managed_settings SET used_bytes=used_bytes+? WHERE id=1',(size,))
    return pk,size


def get_bytes(db, pk):
    if not pk:return b''
    chunks=db.all('SELECT * FROM managed_chunks WHERE object_id=? ORDER BY seq',(pk,))
    if not db.one('SELECT id FROM managed_objects WHERE id=?',(pk,)):
        fail(409,'managed_content_missing','会话所需内容已清理，请新建会话或补充资料')
    data=[]
    for row in chunks:
        value=db.settings.unseal(row['ciphertext'])
        if value.get('id')!=pk or value.get('seq')!=row['seq']:raise ValueError('managed object binding mismatch')
        data.append(base64.b64decode(value['data'],validate=True))
    return b''.join(data)


def get(db, pk):
    return json.loads(get_bytes(db,pk)) if pk else None


def remove(c, pk):
    if not pk:return
    row=c.execute('SELECT bytes FROM managed_objects WHERE id=?',(pk,)).fetchone()
    if row:
        c.execute('DELETE FROM managed_chunks WHERE object_id=?',(pk,))
        c.execute('DELETE FROM managed_objects WHERE id=?',(pk,))
        c.execute('UPDATE managed_settings SET used_bytes=used_bytes-? WHERE id=1',(row['bytes'],))


def verify(db):
    for row in db.all('SELECT id,bytes FROM managed_objects'):
        get_bytes(db,row['id'])
        total=db.one('SELECT COALESCE(sum(bytes),0) n FROM managed_chunks WHERE object_id=?',(row['id'],))['n']
        if total!=row['bytes']:raise ValueError('managed object size mismatch')
    for row in db.all('SELECT account_id FROM account_bindings'):
        if not isinstance(db.settings.unseal(row['account_id']).get('id'),str):raise ValueError('account binding unreadable')
    actual=db.one('SELECT COALESCE(sum(bytes),0) n FROM managed_chunks')['n']
    if actual!=db.one('SELECT used_bytes FROM managed_settings')['used_bytes']:raise ValueError('managed storage accounting mismatch')
    return db.one('SELECT count(*) n FROM managed_chunks')['n']
