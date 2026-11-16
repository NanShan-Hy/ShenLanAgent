"""Offline evaluation. Synthetic capacity data is kept outside the production database."""
import argparse
import json
import os
import time
from pathlib import Path
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='1'
from aiworkbench.core import Settings,Database,password_hash,dump,terms
from aiworkbench.access import Access,create_record
from aiworkbench.retrieval import index_record,index_vectors,search

p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--capacity',action='store_true');args=p.parse_args()
root=Path(args.output).resolve();allowed=Path('/home/nanshan/workspace/tmp/ai-workbench')
if not root.is_relative_to(allowed) or root.exists():raise SystemExit('需要项目tmp下的全新目录')
root.mkdir(parents=True,mode=0o700)
s=Settings(root/'data',root/'config/master.key',min_free_bytes=0);db=Database(s);db.initialize();now=time.time()
db.execute('INSERT INTO users(id,username,display_name,password_hash,is_admin,created_at) VALUES(1,?,?,?,?,?)',('eval','评估账号',password_hash('not-a-real-password'),1,now))
for pk in range(1,11):
 db.execute('INSERT INTO projects(id,name,created_at) VALUES(?,?,?)',(pk,'合成评估项目'+str(pk),now));db.execute("INSERT INTO members VALUES(1,1,'admin')" if pk==1 else 'INSERT INTO members VALUES(?,?,?)',() if pk==1 else (pk,1,'admin'))
u=db.one('SELECT * FROM users WHERE id=1')
topics=[
 ('接口鉴权失败401','登录令牌过期会导致API返回401，重新登录提取token后请求应成功。',['接口返回401该检查什么','登录令牌过期怎么处理','鉴权接口失败如何排查','接口身份验证错误原因','API会话失效怎么恢复']),
 ('报价金额与数量','商品按单价乘数量计算报价，工业乙醇690分每公斤，2公斤金额1380分。',['乙醇两公斤报价多少分','报价金额怎么计算','数量变化后应如何验证金额','690分单价购买2公斤多少钱','销售报价金额断言如何配置']),
 ('项目账号权限','只读成员不能修改用例，编辑者可以编辑，项目管理员能配置成员角色。',['只读成员能编辑用例吗','谁可以管理项目成员','编辑者有哪些权限','怎样分配账号项目角色','为什么无法修改项目用例']),
 ('测试失败截图与Trace','页面断言失败保存failure.png与trace.zip，报告可查看失败现场。',['浏览器断言失败在哪里看截图','Trace附件如何定位问题','页面失败现场保存什么','failure.png是什么','Web失败报告有哪些附件']),
 ('源码配置目录归放','源码放workspace/projects，服务配置在services/项目/config，真实配置600，敏感目录700。',['源码应该存哪里','真实配置文件权限是多少','服务配置目录怎么规划','敏感数据目录应设置什么权限','项目源码和配置如何分开存储']),
 ('长期记忆审核','AI经验先生成候选，经人工确认后启用，来源修改后需要重新复审。',['AI记忆什么时候生效','长期经验由谁确认','来源变化后记忆怎么处理','记忆候选和已确认有什么区别','为什么旧经验需要复审']),
 ('OCR扫描识别','扫描PDF先按页渲染再OCR，低置信度文字需人工复核，金额与型号应核对原图。',['扫描PDF怎么入知识库','OCR结果不准确怎么办','扫描图片金额需要核对吗','低置信度文字如何处理','扫描件识别经过哪些步骤']),
 ('CI流水线幂等','CI使用Bearer令牌调用，构建编号作为Idempotency-Key，不同环境使用不同幂等键。',['流水线如何避免重复提交','CI用什么令牌调用测试','多环境构建幂等键如何设置','Idempotency-Key有什么作用','重复构建请求怎么去重']),
 ('备份恢复核验','SQLite通过一致性备份导出，恢复检查SHA256与integrity_check，密钥必须配套。',['数据库恢复要检查什么','为什么备份需要配套密钥','SHA256用于什么验证','SQLite如何一致性备份','恢复完整性如何核对']),
 ('模型调用外发策略','资料本地保存，只有获准脱敏片段可调用外部模型；每日额度限制Token用量。',['哪些资料可以发送外部模型','模型调用如何控制额度','私有资料外发需要什么条件','Token每日用量如何限制','AI可以上传全部知识库吗'])]
expected=[]
for i,(title,body,questions) in enumerate(topics):
 rid=create_record(db,u,{'kind':'document','title':title,'body':body,'project_id':i+1,'visibility':'project'});index_record(db,rid)
 expected.extend((q,rid) for q in questions)
while index_vectors(db):pass
correct=0;details=[];latencies=[]
for q,rid in expected:
 start=time.perf_counter();result=search(db,Access(db,u),q,limit=5);elapsed=time.perf_counter()-start
 ok=rid in [x['record_id'] for x in result['items']];correct+=ok;latencies.append(elapsed);details.append({'query':q,'hit':ok,'mode':result['mode']})
report={'dataset':'50条人工设定中文问题/10份合成资料；非业务生产数据','queries':50,'top5_hits':correct,'top5_hit_rate':correct/50,'p95_seconds':sorted(latencies)[47],'details':details}
if args.capacity:
 import numpy as np
 from aiworkbench.retrieval import embedder,MODEL
 vector=np.asarray(next(embedder(s).embed(['合成容量基准文本'])),dtype=np.float32).tobytes()
 start=time.perf_counter()
 with db.transaction() as c:
  for i in range(10000):
   rid='capacity-'+str(i);text='合成容量资料 doc-'+str(i).zfill(5)+' 项目接口与日志检索基准。';pid=i%10+1
   c.execute('INSERT INTO records(id,kind,title,body,project_id,owner_id,visibility,deps,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?,?)',
             (rid,'document','容量文档'+str(i),text,pid,'project',dump([pid]),now,now))
   for j in range(10):
    rowid=c.execute('INSERT INTO chunks(record_id,version,locator,text,search_terms) VALUES(?,1,?,?,?)',(rid,'合成段落'+str(j),text,terms(text))).lastrowid
    c.execute('INSERT INTO embeddings VALUES(?,?,?)',(rowid,MODEL,vector))
 elapsed=time.perf_counter()-start
 timings=[]
 for _ in range(3):
  start=time.perf_counter();result=search(db,Access(db,u),'doc-09999',limit=5);timings.append(time.perf_counter()-start)
  assert any(x['record_id']=='capacity-9999' for x in result['items'])
 report['capacity']={'synthetic_documents':10000,'synthetic_chunks':100000,'vector_note':'重复基准向量，仅评估容量与检索链路，不用于语义质量结论','load_seconds':elapsed,'query_seconds':timings,'db_bytes':db.path.stat().st_size}
(root/'evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));(root/'evaluation.json').chmod(0o600)
print(json.dumps({k:v for k,v in report.items() if k!='details'},ensure_ascii=False,indent=2),flush=True)
assert report['top5_hit_rate']>=.85
assert all(x['mode']=='local-hybrid' for x in details)
