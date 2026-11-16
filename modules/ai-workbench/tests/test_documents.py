import io
import json
from pathlib import Path
import pytest
from aiworkbench.documents import extract,approved_path
from aiworkbench.worker import Worker
from aiworkbench.retrieval import index_vectors


def test_documents_upload_and_normal_formats(env,tmp_path):
 db,s,client,app=env;c=client()
 text='目录与配置规范\n源码放 projects，配置放服务config，真实密钥不要入库。'
 r=c.post('/api/v1/uploads',files={'file':('规范.txt',text.encode(),'text/plain')},data={'project_id':'1'})
 assert r.status_code==201,r.text
 task=r.json()['task_id'];rid=r.json()['id'];Worker(s).run_once()
 detail=c.get('/api/v1/tasks/'+task).json()
 assert detail['status']=='completed',detail
 assert '源码放' in c.get('/api/v1/records/'+rid).json()['body']
 assert c.post('/api/v1/search',json={'query':'配置规范','semantic':False}).json()['items']
 from docx import Document
 d=Document();d.add_paragraph('商品报价规则');table=d.add_table(rows=1,cols=2);table.cell(0,0).text='SKU';table.cell(0,1).text='ETH-99';p=tmp_path/'test.docx';d.save(p)
 pages,review=extract(p);assert any('ETH-99' in text for _,text in pages)
 import openpyxl
 w=openpyxl.Workbook();sheet=w.active;sheet.title='订单';sheet.append(['SKU','金额']);sheet.append(['ETH-99',1380]);p=tmp_path/'test.xlsx';w.save(p)
 pages,review=extract(p);assert any('1380' in text and '表头' in text for _,text in pages)


def test_real_chinese_ocr_and_scanned_pdf(env,tmp_path):
 from PIL import Image,ImageDraw,ImageFont
 image=Image.new('RGB',(1100,280),'white');font=ImageFont.truetype('/home/nanshan/workspace/tools/fonts/noto-cjk/20230817/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',40)
 d=ImageDraw.Draw(image);d.text((40,40),'工业乙醇 99% 订单金额 1380 分',font=font,fill='black');d.text((40,120),'项目权限测试 API 401',font=font,fill='black')
 p=tmp_path/'image.png';image.save(p)
 pages,review=extract(p);text=''.join(t for _,t in pages)
 assert all(x in text for x in ['工业乙醇','1380','401']),text
 p=tmp_path/'scan.pdf';image.save(p,'PDF',resolution=120)
 pages,review=extract(p);assert '1380' in pages[0][1],pages


def test_path_allowlist_and_symlink_escape(env,tmp_path):
 db,s,client,app=env
 allowed=tmp_path/'allowed';allowed.mkdir();outside=tmp_path/'secret.txt';outside.write_text('secret')
 path=allowed/'doc.txt';path.write_text('公开内容')
 assert approved_path(path,[str(allowed)])==path
 (allowed/'link.txt').symlink_to(outside)
 with pytest.raises(ValueError):approved_path(allowed/'link.txt',[str(allowed)])
 secret=allowed/'config';secret.mkdir();(secret/'settings.json').write_text('{}')
 with pytest.raises(ValueError):approved_path(secret/'settings.json',[str(allowed)])
 c=client('bob')
 assert c.post('/api/v1/uploads',files={'file':('a.txt',b'x')},data={'project_id':'1'}).status_code==403


def test_registered_source_versions_and_unavailable_ancestor(env,tmp_path):
 from aiworkbench.access import Access
 db,s,client,app=env;c=client();root=tmp_path/'source-root';root.mkdir();s.roots=(str(root),)
 path=root/'rules.txt';path.write_text('订单金额按690分每公斤计算。')
 r=c.post('/api/v1/sources/ingest',json={'path':str(path),'project_id':1});assert r.status_code==201,r.text
 rid=r.json()['id'];worker=Worker(s);worker.run_once()
 memory=c.post('/api/v1/records',json={'kind':'memory','title':'价格规则','body':'价格690分','project_id':1,'visibility':'project','sources':[{'id':rid,'version':1}]}).json()['id']
 c.post('/api/v1/records/'+memory+'/review',json={'action':'approve'})
 path.write_text('订单价格变更为700分每公斤。');db.execute('UPDATE source_files SET last_checked=0')
 worker.run_once()
 assert c.get('/api/v1/records/'+rid).json()['version']==2
 assert c.get('/api/v1/records/'+memory).json()['stale']
 assert c.get('/api/v1/records/'+rid+'/file?version=1').content.decode().find('690')>=0
 assert c.get('/api/v1/records/'+rid+'/file?version=2').content.decode().find('700')>=0
 path.unlink();db.execute('UPDATE source_files SET last_checked=0');worker.sync_sources()
 user=db.one('SELECT * FROM users WHERE id=1');assert not Access(db,user).is_current(rid)
