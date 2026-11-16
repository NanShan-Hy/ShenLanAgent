import asyncio
import hashlib
from pathlib import Path
import tempfile
import time

from fastapi import Body,Query,Request
from fastapi.responses import Response

from .core import BASE,audit,uid,digest,dumps
from . import managed_store as store
from .media import EXTENSIONS,file_for,identity,parse_file,subject
from .security import fail,admin


def purge(db,row,operator,tenant=None):
    with db.transaction() as c:
        c.execute("UPDATE gateway_files SET state='deleted',parsed_id=NULL WHERE id=?",(row['id'],))
        for object_id in (row['object_id'],row['parsed_id']):
            if not object_id:continue
            size=c.execute('SELECT bytes FROM managed_objects WHERE id=?',(object_id,)).fetchone()
            if not size:continue
            c.execute('DELETE FROM managed_chunks WHERE object_id=?',(object_id,))
            c.execute('UPDATE managed_objects SET bytes=0 WHERE id=?',(object_id,))
            c.execute('UPDATE managed_settings SET used_bytes=used_bytes-? WHERE id=1',(size['bytes'],))
        audit(c,operator,tenant,'file.delete',row['id'])
    return {'id':row['id'],'object':'file','deleted':True,'financial_records_preserved':True}


def attach_files(api,db):
    directory=BASE/'tmp/ai-gateway/uploads';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    tempfile.tempdir=str(directory)
    slot=asyncio.Semaphore(1)

    def authorized(request):
        key=identity(db,request);return key,subject(db,key,request.headers.get('x-gateway-subject'),request.headers.get('x-gateway-authorization-version','1'))

    def info(row):
        return {'id':row['id'],'object':'file','filename':row['name'],'bytes':row['bytes'],'created_at':int(row['created_at']),
                'purpose':'user_data','status':row['state'],'sha256':row['sha256']}

    @api.post('/v1/files',status_code=201)
    async def upload(request:Request):
        key,sub=authorized(request)
        if request.headers.get('content-type','').split(';')[0]!='multipart/form-data':fail(415,'multipart_required','请使用multipart上传文件')
        async with slot:
            async with request.form(max_files=1,max_fields=3,max_part_size=20*1024**2) as form:
                file=form.get('file')
                if not hasattr(file,'read') or form.get('purpose','user_data')!='user_data':fail(400,'invalid_file','需要file和purpose=user_data')
                name=Path(file.filename or '').name
                if not name or len(name)>160 or Path(name).suffix.lower() not in EXTENSIONS:fail(400,'unsupported_file','文件类型不在支持范围')
                raw=await file.read(20*1024**2+1)
                if not raw or len(raw)>20*1024**2:fail(413,'file_size_limit','单文件需为1字节至20MiB')
                # No partial object publication: parsing errors are explicit, original stays private for section selection.
                parsed=await asyncio.to_thread(parse_file,raw,name)
                if parsed.get('error') not in (None,'selection_required','section_too_large'):fail(400,'attachment_'+parsed['error'],'文件无法安全解析')
                original,_=store.put_bytes(db,raw,'file');parsed_id=None;pk='file-'+uid()
                try:
                    parsed_id,_=store.put(db,parsed,'parsed')
                    with db.transaction() as c:
                        c.execute('INSERT INTO gateway_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (pk,key['user_id'],key['application_id'],sub,name,(file.content_type or 'application/octet-stream')[:100],len(raw),hashlib.sha256(raw).hexdigest(),original,parsed_id,'needs_selection' if parsed.get('error') else 'ready',time.time()))
                        audit(c,key['user_id'],key['tenant_id'],'file.upload',pk)
                except BaseException:
                    with db.transaction() as c:store.remove(c,parsed_id);store.remove(c,original)
                    raise
        return info(file_for(db,key,sub,pk))

    @api.get('/v1/files')
    def listing(request:Request):
        key,sub=authorized(request)
        rows=db.all("SELECT * FROM gateway_files WHERE user_id=? AND application_id=? AND subject_hash=? AND state!='deleted' ORDER BY created_at DESC LIMIT 100",(key['user_id'],key['application_id'],sub))
        return {'object':'list','data':[info(r) for r in rows]}

    @api.get('/v1/files/{pk}')
    def detail(pk:str,request:Request):
        key,sub=authorized(request);row=file_for(db,key,sub,pk);value=info(row)
        parsed=store.get(db,row['parsed_id']);value['sections']=[{'label':p['label'],'characters':len(p.get('text','')),'image':bool(p.get('image'))} for p in (parsed or {}).get('sections',[])]
        for name in ('available_sections','available_sheets','page_count','selection_format'):
            if name in (parsed or {}):value[name]=parsed[name]
        return value

    @api.get('/v1/files/{pk}/content')
    def contents(pk:str,request:Request):
        key,sub=authorized(request);row=file_for(db,key,sub,pk)
        with db.transaction() as c:audit(c,key['user_id'],key['tenant_id'],'file.read',pk)
        return Response(store.get_bytes(db,row['object_id']),media_type='application/octet-stream',headers={'Content-Disposition':'attachment'})

    @api.delete('/v1/files/{pk}')
    def delete(pk:str,request:Request):
        key,sub=authorized(request);row=file_for(db,key,sub,pk)
        return purge(db,row,key['user_id'],key['tenant_id'])

    @api.get('/api/v1/admin/gateway-files')
    def admin_files(request:Request,page:int=Query(1,ge=1,le=100000)):
        admin(db,request)
        base="FROM gateway_files f JOIN users u ON u.id=f.user_id JOIN applications a ON a.id=f.application_id WHERE f.state!='deleted'"
        return {'items':db.all('SELECT f.id,f.name,f.bytes,f.state,f.created_at,u.display_name,a.name application_name '+base+' ORDER BY f.created_at DESC LIMIT 20 OFFSET ?',((page-1)*20,)),
                'page':page,'total':db.one('SELECT count(*) n '+base)['n']}

    def file_preview(pk):
        row=db.one("SELECT * FROM gateway_files WHERE id=? AND state!='deleted'",(pk,))
        if not row:fail(404,'file_not_found','文件不存在或已清理')
        sizes=[db.one('SELECT id,bytes FROM managed_objects WHERE id=?',(object_id,)) for object_id in (row['object_id'],row['parsed_id']) if object_id]
        sizes=[x for x in sizes if x]
        return row,{'bytes':sum(x['bytes'] for x in sizes),'selection_hash':digest(dumps(sizes)),'filename':row['name']}

    @api.post('/api/v1/admin/gateway-files/{pk}/cleanup/preview')
    def preview(pk:str,request:Request):
        admin(db,request);return file_preview(pk)[1]

    @api.post('/api/v1/admin/gateway-files/{pk}/cleanup')
    def clear(pk:str,request:Request,body:dict=Body(...)):
        user=admin(db,request);row,preview=file_preview(pk)
        if body.get('selection_hash')!=preview['selection_hash']:fail(409,'cleanup_changed','请重新预览附件清理范围')
        return purge(db,row,user['id'])
