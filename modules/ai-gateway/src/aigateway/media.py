"""Private attachments resolved before budget/admission. No remote URL fetching."""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import managed_store as store
from .security import bearer,fail,key_from_token
from .core import digest

EXTENSIONS={'.png','.jpg','.jpeg','.webp','.pdf','.docx','.xlsx','.csv','.tsv','.txt','.md','.markdown','.json','.yaml','.yml','.xml','.log','.py','.js','.ts','.html','.css','.sql','.java','.go','.c','.cpp','.rs','.toml'}


class MediaPayload(dict):
    media_tokens=0
    media_verified=True
    media_profile=None


def copy_budget(source,target):
    if isinstance(source,MediaPayload):
        result=MediaPayload(target);result.media_tokens=source.media_tokens;result.media_profile=source.media_profile;return result
    return target


def parse_file(raw,name,selection=None):
    try:
        result=subprocess.run([sys.executable,'-m','aigateway.document_worker',name,json.dumps(selection)],input=raw,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=18,
            env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]),'OPENBLAS_NUM_THREADS':'1'})
    except (OSError,subprocess.TimeoutExpired):return {'error':'file_parse_failed'}
    if result.returncode or len(result.stdout)>4*1024**2:return {'error':'file_parse_failed'}
    try:return json.loads(result.stdout)
    except ValueError:return {'error':'file_parse_failed'}


def identity(db,request):
    token=bearer(request)
    with db.connect() as c:return key_from_token(c,db.settings,token,request.client.host if request.client else '')


def subject(db,key,external=None,authorization_version='1'):
    from .account_binding import policy_for
    if not isinstance(authorization_version,str) or not 1<=len(authorization_version)<=100:fail(400,'invalid_authorization_version','外部授权版本无效')
    policy=policy_for(db,key['application_id'])
    if policy and policy['identity_mode']=='application_subject':
        if not isinstance(external,str) or not 1<=len(external)<=160:fail(400,'external_subject_required','此应用需要可信后端传入外部用户标识')
        return digest(external+'\0'+authorization_version)
    if external is not None and (not isinstance(external,str) or len(external)>160):fail(400,'invalid_subject','外部用户标识无效')
    return digest((external or 'key-owner')+'\0'+authorization_version)


def file_for(db,key,subject_hash,pk):
    row=db.one("SELECT * FROM gateway_files WHERE id=? AND user_id=? AND application_id=? AND subject_hash=? AND state!='deleted'",
               (pk,key['user_id'],key['application_id'],subject_hash))
    if not row:fail(404,'file_not_found','文件不存在或无访问权限')
    return row


async def resolve_media(db,request,body,protocol,key=None,subject_hash=None):
    if isinstance(body,MediaPayload):return body
    key=key or identity(db,request);subject_hash=subject_hash or subject(db,key,request.headers.get('x-gateway-subject'),request.headers.get('x-gateway-authorization-version','1'))
    body=copy.deepcopy(body);images=0;files=0;found=False
    def expand(value):
        nonlocal images,files,found
        if isinstance(value,list):
            out=[]
            for item in value:
                replacement=expand(item)
                if isinstance(item,dict) and item.get('type') in ('input_file','file'):out.extend(replacement)
                else:out.append(replacement)
            return out
        if not isinstance(value,dict):return value
        kind=value.get('type')
        if kind in ('input_file','file'):
            found=True;files+=1
            obj=value.get('file',value);pk=obj.get('file_id')
            if not isinstance(pk,str):fail(400,'platform_file_required','请先上传文件并使用平台file_id')
            f=file_for(db,key,subject_hash,pk);selection=obj.get('selected_sections')
            if selection is not None and (not isinstance(selection,list) or not 1<=len(selection)<=100 or any(not isinstance(v,str) or len(v)>100 for v in selection)):
                fail(400,'invalid_selection','文件分段选择无效')
            parsed=parse_file(store.get_bytes(db,f['object_id']),f['name'],selection) if selection else store.get(db,f['parsed_id'])
            if not parsed or parsed.get('error'):fail(400,'attachment_'+(parsed or {}).get('error','selection_required'),'文件需要选择页/工作表/行范围，或格式无法解析')
            parts=[]
            for section in parsed['sections']:
                if section.get('text'):
                    parts.append({'type':'text' if protocol=='chat' else 'input_text','text':'【附件 '+f['name']+' / '+section['label']+' / '+f['sha256'][:12]+'】\n'+section['text']})
                if section.get('image'):
                    images+=1;url='data:image/jpeg;base64,'+section['image']
                    parts.append({'type':'image_url','image_url':{'url':url}} if protocol=='chat' else {'type':'input_image','image_url':url})
            return parts
        if kind in ('input_image','image_url'):
            found=True;images+=1;url=value.get('image_url');url=url.get('url') if isinstance(url,dict) else url
            if not isinstance(url,str) or not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+',url):
                fail(400,'image_upload_required','图片请使用data URL或上传为平台文件；不抓取远程URL')
            try:raw=base64.b64decode(url.split(',',1)[1],validate=True)
            except ValueError:fail(400,'invalid_image','图片编码无效')
            parsed=parse_file(raw,'image.'+url.split(';',1)[0].rsplit('/',1)[1])
            if parsed.get('error'):fail(400,'invalid_image','图片无法解析')
            normalized='data:image/jpeg;base64,'+parsed['sections'][0]['image']
            return {'type':'image_url','image_url':{'url':normalized}} if protocol=='chat' else {'type':'input_image','image_url':normalized}
        return {k:expand(v) for k,v in value.items()}
    # Parse on a worker thread; subprocess isolation bounds CPU/memory and keeps SSE responsive.
    slot=getattr(request.app.state,'media_slot',None)
    if slot is None:request.app.state.media_slot=slot=asyncio.Semaphore(1)
    field='messages' if protocol=='chat' else 'input'
    result=dict(body)
    async with slot:result[field]=await asyncio.to_thread(expand,body.get(field))
    if files>4 or images>4:fail(400,'attachment_count_limit','每轮最多4个附件或视觉页面，请选择需要分析的分段')
    if not found:return body
    model=db.one('SELECT id FROM models WHERE alias=?',(body.get('model'),))
    caps=db.one('SELECT * FROM model_media WHERE model_id=?',(model['id'],)) if model else None
    if images and (not caps or not caps['vision'] or not caps['verified'] or caps['image_tokens']<1024 or images>caps['max_images']):
        fail(400,'vision_not_verified','当前模型尚未验证图片能力及预算，请选择已验证的视觉模型')
    converted=MediaPayload(result);converted.media_tokens=images*(caps['image_tokens'] if caps else 0)
    if images:converted.media_profile=tuple(caps[k] for k in ('model_id','vision','image_tokens','max_images','verified'))
    if len(json.dumps(converted).encode())>4*1024**2:fail(413,'attachment_context_too_large','附件分析内容过大，请选择页或分段')
    return converted
