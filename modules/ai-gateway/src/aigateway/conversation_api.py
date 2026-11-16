import json
import re
import time

from fastapi import Body,Query,Request

from . import managed_store as store
from .account_binding import policy_for
from .conversations import access,checkpoint,output_items,call_ids
from .core import audit,digest,dumps,uid
from .media import identity,subject
from .security import admin,fail,model_allowed


def attach_conversations(api,db):
    def owned(request,pk):
        key=identity(db,request);sub=subject(db,key,request.headers.get('x-gateway-subject'),request.headers.get('x-gateway-authorization-version','1'));row=access(db,key,sub,pk)
        allowed=json.loads(key['models'])
        if '*' not in allowed and row['model'] not in allowed:fail(403,'model_forbidden','此Key没有该会话模型的访问权限')
        return key,row

    def public(row):
        value={k:row[k] for k in ('id','user_id','tenant_id','application_id','subject_hash','task_type','model','protocol','policy','state','revision','created_at','updated_at')}
        value['subject_hash']=value['subject_hash'][:12];value['pending_calls']=json.loads(row['pending_calls']);value['busy']=bool(row['lease'])
        value['has_summary']=bool(row['summary_id']);value['binding_state']=(db.one('SELECT state FROM account_bindings WHERE id=?',(row['id'],)) or {}).get('state','unbound')
        return value

    @api.post('/api/v1/conversations/resolve')
    def resolve(request:Request,body:dict=Body(...)):
        if set(body)-{'model','protocol','task_type','external_subject','authorization_version','new_session'}:fail(400,'invalid_conversation','会话参数无效')
        key=identity(db,request);sub=subject(db,key,body.get('external_subject',request.headers.get('x-gateway-subject')),body.get('authorization_version',request.headers.get('x-gateway-authorization-version','1')))
        task=body.get('task_type','general_chat');protocol=body.get('protocol','responses');model=body.get('model')
        if not isinstance(model,str) or not 1<=len(model)<=120:fail(400,'invalid_model','请提供模型别名')
        if not isinstance(task,str) or not re.fullmatch(r'[\w.:-]{1,80}',task) or protocol not in ('chat','responses') or type(body.get('new_session',False)) is not bool:
            fail(400,'invalid_conversation','任务类型、协议或新建参数无效')
        route=db.one('SELECT * FROM models WHERE alias=? AND enabled=1',(model,))
        if not route:fail(404,'model_not_found','模型未启用')
        with db.transaction() as c:
            model_allowed(c,key,route)
            if protocol not in json.loads(route['protocols']):fail(400,'protocol_unsupported','模型未开放此协议')
            row=c.execute('''SELECT c.* FROM conversation_current p JOIN conversations c ON c.id=p.conversation_id
                WHERE p.user_id=? AND p.application_id=? AND p.subject_hash=? AND p.task_type=?''',(key['user_id'],key['application_id'],sub,task)).fetchone()
            if row and not body.get('new_session') and row['state'] not in ('closed','cleared'):
                if row['model']!=model or row['protocol']!=protocol:fail(409,'conversation_model_mismatch','同类任务已有不同模型/协议会话，请明确新建')
                return public(dict(row))
            pk=uid();now=time.time();policy=policy_for(db,key['application_id'],task)['policy']
            c.execute('INSERT INTO conversations(id,user_id,tenant_id,application_id,subject_hash,task_type,model,protocol,policy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (pk,key['user_id'],key['tenant_id'],key['application_id'],sub,task,model,protocol,policy,now,now))
            c.execute('INSERT INTO conversation_current VALUES(?,?,?,?,?) ON CONFLICT(user_id,application_id,subject_hash,task_type) DO UPDATE SET conversation_id=excluded.conversation_id',
                (key['user_id'],key['application_id'],sub,task,pk))
            audit(c,key['user_id'],key['tenant_id'],'conversation.create',pk)
        return public(db.one('SELECT * FROM conversations WHERE id=?',(pk,)))

    @api.get('/api/v1/conversations')
    def listing(request:Request,page:int=Query(1,ge=1,le=100000)):
        key=identity(db,request);sub=subject(db,key,request.headers.get('x-gateway-subject'),request.headers.get('x-gateway-authorization-version','1'))
        rows=db.all('SELECT * FROM conversations WHERE user_id=? AND application_id=? AND subject_hash=? ORDER BY updated_at DESC LIMIT 20 OFFSET ?',
            (key['user_id'],key['application_id'],sub,(page-1)*20))
        allowed=json.loads(key['models']);return {'items':[public(r) for r in rows if '*' in allowed or r['model'] in allowed],'page':page}

    @api.get('/api/v1/conversations/{pk}')
    def detail(pk:str,request:Request):
        key,row=owned(request,pk);value=public(row);value['summary']=checkpoint(db,row)['text']
        with db.transaction() as c:audit(c,key['user_id'],key['tenant_id'],'conversation.read',pk)
        return value

    @api.get('/api/v1/conversations/{pk}/turns')
    def turns(pk:str,request:Request,page:int=Query(1,ge=1,le=100000),operation_id:str|None=Query(None,min_length=1,max_length=160)):
        owned(request,pk)
        where='conversation_id=?';args=[pk]
        if operation_id:where+=' AND idempotency_key=?';args.append(operation_id)
        return {'items':db.all('SELECT id,request_id,idempotency_key,revision,status,error_code,kind,created_at,finished_at FROM conversation_turns WHERE '+where+' ORDER BY created_at,id LIMIT 20 OFFSET ?',
             (*args,(page-1)*20)),'page':page}

    def read_turn(pk,turn,section,cursor):
        row=db.one('SELECT input_id,output_id FROM conversation_turns WHERE id=? AND conversation_id=?',(turn,pk))
        if not row:fail(404,'turn_not_found','轮次不存在')
        object_id=row[section+'_id']
        if not object_id:return {'text':'','next_cursor':None}
        rows=db.all('SELECT * FROM managed_chunks WHERE object_id=? AND seq>=? ORDER BY seq LIMIT 9',(object_id,cursor));text=[]
        import base64
        for chunk in rows[:8]:
            value=db.settings.unseal(chunk['ciphertext'])
            if value.get('id')!=object_id or value.get('seq')!=chunk['seq']:fail(409,'context_unreadable','内容无法解密或归属不一致')
            text.append(base64.b64decode(value['data'],validate=True))
        return {'text':b''.join(text).decode(),'next_cursor':rows[8]['seq'] if len(rows)>8 else None}

    @api.get('/api/v1/conversations/{pk}/turns/{turn}/content')
    def content(pk:str,turn:str,request:Request,section:str=Query('input',pattern='^(input|output)$'),cursor:int=Query(0,ge=0,le=2147483647)):
        key,_=owned(request,pk)
        with db.transaction() as c:audit(c,key['user_id'],key['tenant_id'],'conversation.contents_view',pk,turn+':'+section)
        return read_turn(pk,turn,section,cursor)

    @api.post('/api/v1/conversations/{pk}/close')
    def close(pk:str,request:Request):
        key,row=owned(request,pk)
        if row['lease']:fail(409,'conversation_busy','正在处理的会话不能关闭')
        with db.transaction() as c:
            c.execute("UPDATE conversations SET state='closed',revision=revision+1,updated_at=? WHERE id=?",(time.time(),pk))
            audit(c,key['user_id'],key['tenant_id'],'conversation.close',pk)
        return {'ok':True}

    @api.post('/api/v1/conversations/{pk}/recover')
    def recover(pk:str,request:Request,body:dict=Body(...)):
        key,row=owned(request,pk)
        if row['lease'] or row['state']!='needs_recovery':fail(409,'recovery_not_required','会话不在可恢复状态')
        if body.get('acknowledge_unknown') is not True or set(body)-{'acknowledge_unknown','response'}:fail(400,'acknowledgement_required','需明确确认未知调用不会重放，丢失结果可能需接入方重新提供')
        latest=db.one("SELECT * FROM conversation_turns WHERE conversation_id=? AND kind='answer' ORDER BY created_at DESC LIMIT 1",(pk,))
        restored=body.get('response');pending=json.loads(row['pending_calls']);input_id=None;output_id=None
        if restored is not None:
            if not isinstance(restored,dict) or not isinstance(restored.get('choices' if row['protocol']=='chat' else 'output'),list):fail(400,'invalid_restored_response','恢复结果需要原协议的choices或output数组')
            if not latest or not latest['input_id']:fail(409,'recovery_input_missing','缺少本轮原输入，不能关联恢复结果')
            original=store.get(db,latest['input_id'])
            input_id,_=store.put(db,original,'recovery');output_id,_=store.put(db,restored,'recovery')
            pending=call_ids(output_items(restored,row['protocol']),row['protocol'])
        elif pending:
            latest_output=db.one("SELECT output_id FROM conversation_turns WHERE conversation_id=? AND status IN ('succeeded','client_restored') ORDER BY created_at DESC LIMIT 1",(pk,))
            calls=output_items(store.get(db,latest_output['output_id']),row['protocol']) if latest_output and latest_output['output_id'] else []
            kinds={i.get('call_id') or i.get('id'):i.get('type') for i in calls}
            items=[]
            for call in pending:
                if row['protocol']=='chat':item={'role':'tool','tool_call_id':call,'content':'客户端明确取消待回传状态；实际执行结果未确认。'}
                elif kinds.get(call)=='tool_search_call':item={'type':'tool_search_output','call_id':call,'execution':'client','tools':[]}
                else:item={'type':'custom_tool_call_output' if kinds.get(call)=='custom_tool_call' else 'function_call_output','call_id':call,'output':'客户端明确取消待回传状态；实际执行结果未确认。'}
                items.append(item)
            input_id,_=store.put(db,{'items':items,'options':{}},'recovery')
            output_id,_=store.put(db,{'choices':[]} if row['protocol']=='chat' else {'output':[]},'recovery');pending=[]
        with db.transaction() as c:
            current=c.execute('SELECT state,lease,revision FROM conversations WHERE id=?',(pk,)).fetchone()
            if current['lease'] or current['state']!='needs_recovery' or current['revision']!=row['revision']:fail(409,'context_revision_conflict','会话已变化，请刷新')
            if input_id:
                c.execute("INSERT INTO conversation_turns(id,conversation_id,key_id,revision,idempotency_key,input_id,output_id,status,kind,created_at,finished_at) VALUES(?,?,?,?,?,?,?,'client_restored','recovery',?,?)",
                    (uid(),pk,key['id'],row['revision'],'recovery-'+uid(),input_id,output_id,time.time(),time.time()))
            c.execute("UPDATE conversations SET state=?,pending_calls=?,revision=revision+1,updated_at=? WHERE id=?",('waiting_tools' if pending else 'active',dumps(pending),time.time(),pk))
            audit(c,key['user_id'],key['tenant_id'],'conversation.recover_without_replay',pk,'client_supplied_response' if restored else 'without_response')
        return {'ok':True,'replayed':False,'financial_records_preserved':True,'source':'client_restored' if restored is not None else 'explicit_acknowledgement'}

    @api.post('/api/v1/conversations/{pk}/cancel-tools')
    def cancel_tools(pk:str,request:Request,body:dict=Body(...)):
        key,row=owned(request,pk)
        if row['lease'] or row['state']!='waiting_tools':fail(409,'tools_not_waiting','会话没有可取消的待回传工具')
        if body!={'acknowledge_cancellation':True}:fail(400,'acknowledgement_required','需明确取消待回传工具；不代表外部工具实际未执行')
        # Reuse the same paired cancellation history as interruption recovery.
        with db.transaction() as c:
            c.execute("UPDATE conversations SET state='needs_recovery' WHERE id=? AND lease IS NULL AND revision=?",(pk,row['revision']))
        return recover(pk,request,{'acknowledge_unknown':True})

    def cleanup_rows(pk):
        row=db.one('SELECT * FROM conversations WHERE id=?',(pk,))
        if not row:fail(404,'conversation_not_found','会话不存在')
        if row['lease']:fail(409,'conversation_busy','正在处理的会话不能清理')
        objects={r[k] for r in db.all('SELECT input_id,output_id FROM conversation_turns WHERE conversation_id=?',(pk,)) for k in ('input_id','output_id') if r[k]}
        if row['summary_id']:objects.add(row['summary_id'])
        sizes=[db.one('SELECT id,bytes FROM managed_objects WHERE id=?',(obj,)) for obj in sorted(objects)]
        sizes=[x for x in sizes if x];return row,sizes,digest(dumps(sizes))

    def clear(pk,operator,tenant,checksum=None):
        row,sizes,check=cleanup_rows(pk)
        if checksum is not None and checksum!=check:fail(409,'cleanup_changed','清理范围已变化，请重新预览')
        with db.transaction() as c:
            current=c.execute('SELECT lease FROM conversations WHERE id=?',(pk,)).fetchone()
            if current['lease']:fail(409,'conversation_busy','会话开始处理，请稍后清理')
            c.execute('UPDATE conversation_turns SET input_id=NULL,output_id=NULL WHERE conversation_id=?',(pk,))
            c.execute("UPDATE conversations SET state='cleared',summary_id=NULL,pending_calls='[]',revision=revision+1 WHERE id=?",(pk,))
            for obj in sizes:store.remove(c,obj['id'])
            audit(c,operator,tenant,'conversation.clear',pk,str(sum(x['bytes'] for x in sizes)))
        return {'ok':True,'financial_records_preserved':True,'attachments_preserved':True}

    @api.delete('/api/v1/conversations/{pk}')
    def delete(pk:str,request:Request):
        key,_=owned(request,pk);return clear(pk,key['user_id'],key['tenant_id'])

    @api.get('/api/v1/admin/conversations')
    def admin_list(request:Request,page:int=Query(1,ge=1,le=100000),user_id:str|None=None,application_id:str|None=None,task_type:str|None=None):
        admin(db,request);where=['1=1'];args=[]
        for k,v in (('c.user_id',user_id),('c.application_id',application_id),('c.task_type',task_type)):
            if v:where.append(k+'=?');args.append(v)
        base='FROM conversations c JOIN users u ON u.id=c.user_id JOIN applications a ON a.id=c.application_id WHERE '+' AND '.join(where)
        rows=db.all('SELECT c.*,u.display_name,a.name application_name '+base+' ORDER BY c.updated_at DESC LIMIT 20 OFFSET ?',(*args,(page-1)*20))
        return {'items':[{**public(r),'display_name':r['display_name'],'application_name':r['application_name']} for r in rows],
                'total':db.one('SELECT count(*) n '+base,args)['n'],'page':page}

    @api.get('/api/v1/admin/conversations/{pk}')
    def admin_detail(pk:str,request:Request):
        user=admin(db,request);row=db.one('SELECT * FROM conversations WHERE id=?',(pk,))
        if not row:fail(404,'conversation_not_found','会话不存在')
        value=public(row);value['summary']=checkpoint(db,row)['text']
        value['turns']=db.all('SELECT id,request_id,status,error_code,kind,revision,created_at FROM conversation_turns WHERE conversation_id=? ORDER BY created_at DESC LIMIT 50',(pk,))
        binding=db.one('SELECT * FROM account_bindings WHERE id=?',(pk,))
        value['account_reference']=digest(db.settings.unseal(binding['account_id'])['id'])[:12] if binding else None
        value['binding_events']=[{'reason':r['reason'],'created_at':r['created_at']} for r in db.all('SELECT reason,created_at FROM binding_events WHERE binding_id=? ORDER BY id DESC LIMIT 50',(pk,))]
        with db.transaction() as c:audit(c,user['id'],row['tenant_id'],'conversation.operator_view',pk)
        return value

    @api.get('/api/v1/admin/conversations/{pk}/turns/{turn}/content')
    def admin_content(pk:str,turn:str,request:Request,section:str=Query('input',pattern='^(input|output)$'),cursor:int=Query(0,ge=0,le=2147483647)):
        user=admin(db,request)
        with db.transaction() as c:audit(c,user['id'],None,'conversation.operator_contents_view',pk,turn+':'+section)
        return read_turn(pk,turn,section,cursor)

    @api.post('/api/v1/admin/conversations/{pk}/cleanup/preview')
    def preview(pk:str,request:Request):
        admin(db,request);row,sizes,check=cleanup_rows(pk)
        return {'objects':len(sizes),'bytes':sum(x['bytes'] for x in sizes),'selection_hash':check,'scope':'context_only'}

    @api.post('/api/v1/admin/conversations/{pk}/cleanup')
    def admin_clear(pk:str,request:Request,body:dict=Body(...)):
        user=admin(db,request)
        if not isinstance(body.get('selection_hash'),str):fail(400,'preview_required','请先预览')
        return clear(pk,user['id'],None,body['selection_hash'])

    @api.post('/api/v1/admin/conversations/{pk}/migrate')
    def migrate(pk:str,request:Request,body:dict=Body(...)):
        user=admin(db,request);row=db.one('SELECT * FROM conversations WHERE id=?',(pk,))
        if not row:fail(404,'conversation_not_found','会话不存在')
        if row['lease'] or row['state'] not in ('active',):fail(409,'conversation_busy','只有空闲且无待回传工具的会话可明确迁移')
        if body!={'acknowledge_migration':True}:fail(400,'acknowledgement_required','需明确确认迁移')
        with db.transaction() as c:
            old=c.execute('SELECT account_id FROM account_bindings WHERE id=?',(pk,)).fetchone()
            c.execute('DELETE FROM account_bindings WHERE id=?',(pk,))
            c.execute('INSERT INTO binding_events(binding_id,old_account,reason,created_at) VALUES(?,?,?,?)',(pk,old['account_id'] if old else None,'operator_migration',time.time()))
            c.execute('UPDATE conversations SET revision=revision+1,gateway_upstream=NULL WHERE id=?',(pk,))
            audit(c,user['id'],row['tenant_id'],'conversation.migrate',pk)
        return {'ok':True,'next_request_rebinds':True}

    @api.get('/api/v1/admin/conversation-settings')
    def settings(request:Request):
        admin(db,request)
        return {**db.one('SELECT * FROM managed_settings'),'policies':db.all('SELECT * FROM conversation_policies'),
                'media':db.all('SELECT m.id,m.alias,COALESCE(v.vision,0) vision,COALESCE(v.verified,0) verified,COALESCE(v.image_tokens,0) image_tokens,COALESCE(v.max_images,4) max_images FROM models m LEFT JOIN model_media v ON v.model_id=m.id ORDER BY alias'),
                'applications':db.all('SELECT id,name FROM applications'),'retention':'manual'}

    @api.post('/api/v1/admin/conversations/{pk}/policy')
    def change_policy(pk:str,request:Request,body:dict=Body(...)):
        user=admin(db,request)
        if set(body)!={'policy','acknowledge_policy_change'} or body['policy'] not in ('strict','continuity') or body['acknowledge_policy_change'] is not True:
            fail(400,'invalid_policy','需明确确认本会话的策略调整')
        with db.transaction() as c:
            row=c.execute('SELECT * FROM conversations WHERE id=?',(pk,)).fetchone()
            if not row:fail(404,'conversation_not_found','会话不存在')
            if row['lease'] or row['state'] not in ('active','waiting_tools'):fail(409,'conversation_busy','会话正在处理或不可修改')
            c.execute('UPDATE conversations SET policy=?,revision=revision+1,updated_at=? WHERE id=?',(body['policy'],time.time(),pk))
            c.execute('UPDATE account_bindings SET policy=? WHERE id=?',(body['policy'],pk))
            audit(c,user['id'],row['tenant_id'],'conversation.policy_change',pk,body['policy'])
        return {'ok':True,'immediate_migration':False}

    @api.put('/api/v1/admin/conversation-policies')
    def update_policy(request:Request,body:dict=Body(...)):
        user=admin(db,request)
        if set(body)-{'application_id','task_type','policy','identity_mode'}:fail(400,'invalid_policy','策略字段无效')
        app=body.get('application_id','');task=body.get('task_type','');policy=body.get('policy');mode=body.get('identity_mode','key_owner')
        if not isinstance(app,str) or not isinstance(task,str) or len(task)>80 or policy not in ('strict','continuity') or mode not in ('key_owner','application_subject') or (task and mode!='key_owner'):
            fail(400,'invalid_policy','策略或身份模式无效；外部用户模式配置在应用默认规则')
        if app and not db.one('SELECT id FROM applications WHERE id=?',(app,)):fail(404,'application_not_found','应用不存在')
        with db.transaction() as c:
            c.execute('INSERT INTO conversation_policies VALUES(?,?,?,?) ON CONFLICT(application_id,task_type) DO UPDATE SET policy=excluded.policy,identity_mode=excluded.identity_mode',(app,task,policy,mode))
            audit(c,user['id'],None,'conversation.policy',app,dumps(body))
        return {'ok':True,'existing_sessions_unchanged':True}

    @api.patch('/api/v1/admin/conversation-settings')
    def update_settings(request:Request,body:dict=Body(...)):
        user=admin(db,request);maximum=body.get('max_bytes')
        if set(body)!={'max_bytes'} or type(maximum) is not int or not 1048576<=maximum<=2*1024**3:fail(400,'invalid_settings','容量需为1MiB至2GiB')
        with db.transaction() as c:
            c.execute('UPDATE managed_settings SET max_bytes=? WHERE id=1',(maximum,));audit(c,user['id'],None,'conversation.capacity','global',str(maximum))
        return {'ok':True}

    @api.put('/api/v1/admin/model-media/{pk}')
    def media(pk:str,request:Request,body:dict=Body(...)):
        user=admin(db,request)
        if set(body)!={'vision','verified','image_tokens','max_images'} or type(body['vision']) is not bool or type(body['verified']) is not bool or type(body['image_tokens']) is not int or type(body['max_images']) is not int:
            fail(400,'invalid_media_capability','需提供视觉开关、验证状态、单图Token预算和最大图片数')
        if not 0<=body['image_tokens']<=131072 or not 1<=body['max_images']<=4 or (body['vision'] and (not body['verified'] or body['image_tokens']<1024)):
            fail(400,'invalid_media_capability','视觉模型需验证且单图保守预算至少1024，最多4张')
        if not db.one('SELECT id FROM models WHERE id=?',(pk,)):fail(404,'model_not_found','模型不存在')
        with db.transaction() as c:
            c.execute('INSERT INTO model_media VALUES(?,?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET vision=excluded.vision,image_tokens=excluded.image_tokens,max_images=excluded.max_images,verified=excluded.verified',
                (pk,body['vision'],body['image_tokens'],body['max_images'],body['verified']))
            audit(c,user['id'],None,'model.media',pk,dumps(body))
        return {'ok':True}
