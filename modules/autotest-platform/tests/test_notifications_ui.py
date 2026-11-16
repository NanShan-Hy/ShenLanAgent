import json
import os
import time
import httpx
from playwright.sync_api import sync_playwright,expect
from conftest import serve,PASSWORD,BROWSER_LIBS
from testdock.worker import Worker
from testdock.notifier import Notifier,Provider
from test_notifications import KEY


def test_daily_notification_browser_flow(targets,tmp_path):
 client,db,settings,api=targets;errors=[];sent=[]
 with serve(api) as url,sync_playwright() as p:
  browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':BROWSER_LIBS})
  page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(url);page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(PASSWORD);page.get_by_role('button',name='登录工作台',exact=True).click()
  page.get_by_role('button',name='通知推送',exact=True).click();page.get_by_role('button',name='新增通知渠道',exact=True).first.click()
  dialog=page.get_by_role('dialog');dialog.get_by_label('渠道名称',exact=True).fill('每日微信');dialog.get_by_label('接入方式').select_option('serverchan');dialog.get_by_label('SendKey',exact=True).fill(KEY);dialog.get_by_label('启用结果推送').check();dialog.get_by_role('button',name='保存',exact=True).click()
  expect(page.get_by_role('heading',name='每日微信',exact=True)).to_be_visible()
  page.get_by_role('button',name='编辑渠道',exact=True).click();expect(page.get_by_label('SendKey',exact=True)).to_have_value('');page.keyboard.press('Escape')
  page.get_by_label('每日微信',exact=True).check();page.get_by_role('button',name='保存推送规则').click()
  page.get_by_role('button',name='定时任务',exact=True).click();page.get_by_role('button',name='新建定时任务',exact=True).first.click()
  dialog=page.get_by_role('dialog');dialog.get_by_label('任务名称',exact=True).fill('每日双环境UI验收');dialog.get_by_label('测试演示',exact=True).check();dialog.get_by_label('执行频率').select_option('daily');dialog.get_by_label('执行时间',exact=True).fill('09:00')
  expect(dialog.get_by_label('Cron',exact=True)).to_have_value('0 9 * * *');expect(dialog.get_by_label('启用任务')).not_to_be_checked()
  dialog.get_by_role('button',name='预览执行时间').click();expect(dialog.get_by_text('Asia/Shanghai',exact=False).last).to_be_visible();dialog.get_by_role('button',name='保存',exact=True).click()
  expect(page.get_by_text('每日双环境UI验收',exact=True)).to_be_visible()
  sid=db.one('SELECT id FROM schedules WHERE name=?',('每日双环境UI验收',))['id']
  page.get_by_role('button',name='启用',exact=True).click();expect(page.get_by_role('button',name='暂停',exact=True)).to_be_visible();assert db.one('SELECT enabled FROM schedules WHERE id=?',(sid,))['enabled']==1
  db.execute('UPDATE schedules SET next_run_at=? WHERE id=?',(time.time(),sid));w=Worker(settings);assert w.run_once();assert w.run_once()
  batch=db.one('SELECT id FROM schedule_batches')['id']
  def transport(request):
   sent.append(json.loads(request.content));return httpx.Response(200,json={'code':0,'data':{'pushid':'browser-contract-receipt'}})
  notifier=Notifier(settings,Provider(httpx.MockTransport(transport)));assert notifier.run_once()
  page.get_by_role('button',name='通知推送',exact=True).click();expect(page.get_by_text('渠道已受理',exact=True)).to_be_visible();assert len(sent)==1
  page.get_by_role('button',name='查看批次',exact=True).click();expect(page.get_by_role('dialog').get_by_text('开发演示',exact=True)).to_be_visible();expect(page.get_by_role('dialog').get_by_text('测试演示',exact=True)).to_be_visible();page.keyboard.press('Escape')
  page.evaluate('scrollTo(0,0)');page.screenshot(path=str(tmp_path/'notifications.png'),full_page=True)
  page.set_viewport_size({'width':390,'height':844});page.evaluate('scrollTo(0,0)');page.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().right<=1");assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path=str(tmp_path/'notifications-mobile.png'),full_page=True)
  page.goto(url+'/#batch='+batch);page.reload();expect(page.get_by_role('dialog',name='多环境执行批次')).to_be_visible();page.keyboard.press('Escape')
  page.goto(url+'/#run='+db.one('SELECT id FROM runs')['id']);expect(page.get_by_role('dialog',name='执行报告')).to_be_visible();page.keyboard.press('Escape')
  # A fresh browser login must also preserve the report target.
  other=browser.new_page();other.goto(url+'/#batch='+batch);other.get_by_label('账号',exact=True).fill('admin');other.get_by_label('密码',exact=True).fill(PASSWORD);other.get_by_role('button',name='登录工作台',exact=True).click();expect(other.get_by_role('dialog',name='多环境执行批次')).to_be_visible()
  assert not errors,errors;browser.close()
