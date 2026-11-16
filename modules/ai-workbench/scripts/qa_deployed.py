"""Explicit deployment smoke check: bind bootstrap admins, sync read-only assets.
Creates a private copy of one existing report; never queues or edits a test.
Uses credential files locally and saves no credentials in the result.
"""
import json
import re
import time
from pathlib import Path
import httpx
BASE=Path('/home/nanshan/workspace')
QA=BASE/'data/ai-workbench/dev'

def credential(service):
 return re.search(r'密码：([^\n]+)',(BASE/'services'/service/'config/initial-admin.txt').read_text()).group(1)

def checked(response):
 response.raise_for_status()
 return response.json()

with httpx.Client(base_url='http://127.0.0.1:8088',headers={'X-Requested-With':'TestDock'},trust_env=False,timeout=30) as td,httpx.Client(base_url='http://127.0.0.1:8090/api/v1',headers={'X-Requested-With':'AIWorkbench'},trust_env=False,timeout=60) as ai:
 checked(td.post('/api/auth/login',json={'username':'admin','password':credential('autotest-platform')}))
 checked(ai.post('/auth/login',json={'username':'admin','password':credential('ai-workbench')}))
 code=checked(td.post('/api/ai/binding-code',json={}))['code']
 checked(ai.post('/testdock/bind',json={'code':code}))
 task=checked(ai.post('/testdock/sync',json={}))['id']
 deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  result=checked(ai.get('/tasks/'+task))
  if result['status'] in ('completed','failed','cancelled'):break
  time.sleep(.5)
 assert result['status']=='completed' and result['result']['errors']==[],result['status']
 projects=checked(ai.get('/projects'))['items']
 source_projects=checked(td.get('/api/projects'))
 assert sorted(p['external_id'] for p in projects if p['external_id'])==sorted(p['id'] for p in source_projects)
 records=checked(ai.get('/records',params={'kind':'knowledge'}))
 kinds={k:sum(r['kind']==k for r in records['items']) for k in ('document','case','suite','environment','run_index')}
 assert all(kinds[k]>0 for k in kinds),kinds
 runs=checked(td.get('/api/projects/'+str(source_projects[0]['id'])+'/runs'))
 assert runs and all(r['status']=='passed' for r in runs)
 rid=runs[0]['id'];evidence=checked(ai.post('/testdock/evidence',json={'run_id':rid}))
 report=checked(ai.get('/records/'+evidence['id']))
 assert report['kind']=='report' and report['visibility']=='private'
 search=checked(ai.post('/search',json={'query':'源码配置存储目录规则','project_ids':[]}))
 assert search['mode']=='local-hybrid' and search['items']
 models=checked(ai.get('/models'))
 assert {m['name'] for m in models}=={'OpenAI','DeepSeek'} and all(not m['enabled'] for m in models)
 usage=checked(ai.get('/usage'));assert usage['used']==0
 health=checked(ai.get('/health'));assert health['worker_online']
 delivery={'checked_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'health':health,'source_project_count':len(source_projects),'visible_projects':[{k:p[k] for k in ('id','name','external_id','role')} for p in projects],'knowledge_counts':kinds,'sync_status':result['status'],'source_run_id':rid,'private_report_id':evidence['id'],'search_mode':search['mode'],'models':[{'name':m['name'],'enabled':bool(m['enabled'])} for m in models],'real_model_tokens':usage['used']}
 target=QA/'delivery.json';target.write_text(json.dumps(delivery,ensure_ascii=False,indent=2));target.chmod(0o600)
 print(json.dumps({k:delivery[k] for k in ('source_project_count','knowledge_counts','sync_status','search_mode','models','real_model_tokens')},ensure_ascii=False))
