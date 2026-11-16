"""Read-only deployment checks and browser previews; no business tasks or messages."""
import json
import os
import re
import time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright,expect
BASE=Path('/home/nanshan/workspace');QA=BASE/'data/autotest-platform/dev'
def password(service):return re.search(r'密码：([^\n]+)',(BASE/'services'/service/'config/initial-admin.txt').read_text()).group(1)
with httpx.Client(base_url='http://127.0.0.1:8088',headers={'X-Requested-With':'TestDock'},trust_env=False,timeout=30) as c:
 c.post('/api/auth/login',json={'username':'admin','password':password('autotest-platform')}).raise_for_status()
 health=c.get('/api/health').json();assert health['worker_online'] and health['notifier_online']
 projects=c.get('/api/projects').json();assert len(projects)==2
 from testdock.core import Database,Settings
 db=Database(Settings.load());before=json.loads((QA/'pre-v12.json').read_text())
 assert db.one('SELECT version FROM schema_version')['version']==2
 counts={t:db.one('SELECT count(*) n FROM '+t)['n'] for t in before['counts']};assert counts==before['counts']
 assert db.all('SELECT id,status FROM runs ORDER BY id')==before['runs']
 assert not c.get('/api/projects/1/notification-channels').json()
 assert c.get('/api/projects/1/notification-deliveries').json()['total']==0
 preview=c.post('/api/projects/1/schedules/preview',json={'cron':'0 9 * * *','timezone':'Asia/Shanghai'});preview.raise_for_status();assert len(preview.json()['times'])==5
 delivery={'checked_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'health':health,'schema':2,'original_counts_preserved':counts,'original_runs_preserved':True,'real_notification_channels':0,'external_notifications_sent':0}
errors=[]
with sync_playwright() as p:
 b=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':str(BASE/'tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu')})
 page=b.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://172.16.61.128:8088');page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(password('autotest-platform'));page.get_by_role('button',name='登录工作台',exact=True).click()
 page.get_by_role('button',name='定时任务',exact=True).click();page.get_by_role('button',name='新建定时任务',exact=True).first.click()
 dialog=page.get_by_role('dialog');expect(dialog.get_by_label('执行时间',exact=True)).to_have_value('09:00');expect(dialog.get_by_label('启用任务')).not_to_be_checked();dialog.get_by_role('button',name='预览执行时间').click();expect(dialog.get_by_text('Asia/Shanghai',exact=False).last).to_be_visible();page.screenshot(path=str(QA/'daily-plan-v12.png'),full_page=True);page.keyboard.press('Escape')
 page.get_by_role('button',name='通知推送',exact=True).click();expect(page.get_by_text('通知执行器在线；今日渠道受理 0 条。',exact=True)).to_be_visible();page.screenshot(path=str(QA/'notifications-v12.png'),full_page=True)
 page.get_by_role('button',name='新增通知渠道',exact=True).first.click();dialog=page.get_by_role('dialog')
 for kind,label in [('dingtalk_app','AppSecret'),('dingtalk_webhook','Webhook地址'),('serverchan','SendKey')]:
  dialog.get_by_label('接入方式').select_option(kind);expect(dialog.get_by_label(label,exact=True)).to_have_value('')
 page.keyboard.press('Escape');page.set_viewport_size({'width':390,'height':844});page.evaluate('scrollTo(0,0)');page.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().right<=1");assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path=str(QA/'notifications-mobile-v12.png'),full_page=True)
 page.goto('http://172.16.61.128:8088/#run='+before['runs'][0]['id']);expect(page.get_by_role('dialog',name='执行报告')).to_be_visible();assert not errors,errors;b.close()
for path in QA.glob('*v12.png'):path.chmod(0o600)
delivery['browser']={'lan_login':True,'daily_time_preview':True,'channel_forms':3,'mobile_width':390,'historical_report_deep_link':True,'page_errors':errors}
with httpx.Client(base_url='http://127.0.0.1:8090/api/v1',headers={'X-Requested-With':'AIWorkbench'},trust_env=False,timeout=30) as c:
 c.post('/auth/login',json={'username':'admin','password':password('ai-workbench')}).raise_for_status();assert len(c.get('/projects').json()['items'])==3
 delivery['ai_bridge_live_projects']=3
path=QA/'delivery-v12.json';path.write_text(json.dumps(delivery,ensure_ascii=False,indent=2));path.chmod(0o600);print(json.dumps(delivery,ensure_ascii=False))
