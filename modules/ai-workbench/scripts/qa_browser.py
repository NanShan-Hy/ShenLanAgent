"""Runs against the isolated development service on 8093, never production data."""
import json
import os
import re
import subprocess
import time
STAMP=str(int(time.time()))
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
BASE=Path('/home/nanshan/workspace');PROJECT=BASE/'projects/ai-workbench';QA=BASE/'data/ai-workbench/dev'
password=re.search(r'密码：([^\n]+)',(QA/'config/initial-admin.txt').read_text()).group(1)
errors=[]
with sync_playwright() as p:
 b=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':str(BASE/'tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu')})
 page=b.new_page(viewport={'width':1440,'height':1050});page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:8093');page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(password);page.get_by_role('button',name='登录工作台',exact=True).click()
 expect(page.get_by_role('heading',name='工作区概览')).to_be_visible()
 page.get_by_role('button',name='项目与成员',exact=True).click();page.get_by_role('button',name='新建本地项目').click()
 dialog=page.get_by_role('dialog');dialog.get_by_label('项目名称').fill('浏览器验收项目 '+STAMP);dialog.get_by_label('描述').fill('仅开发数据，验证完整操作流程。');dialog.get_by_role('button',name='保存',exact=True).click()
 page.get_by_role('article').filter(has=page.get_by_role('heading',name='浏览器验收项目 '+STAMP,exact=True)).get_by_role('button',name='进入项目').click();expect(page.get_by_role('heading',name='知识库',exact=True)).to_be_visible()
 page.get_by_role('button',name='添加文本',exact=True).click();dialog=page.get_by_role('dialog');dialog.get_by_label('标题',exact=True).fill('页面验收 '+STAMP+' · 报价规则');dialog.get_by_label('内容',exact=True).fill('工业乙醇单价690分，采购2公斤，订单报价1380分。');dialog.get_by_label('可见范围').select_option('project');dialog.get_by_role('button',name='保存',exact=True).click()
 expect(page.get_by_role('heading',name='页面验收 '+STAMP+' · 报价规则')).to_be_visible()
 page.get_by_role('button',name='AI 对话与检索',exact=True).click();page.get_by_label('问题或任务').fill('采购两公斤乙醇报价多少');page.get_by_role('button',name='本地检索',exact=True).click();expect(page.get_by_role('heading',name='页面验收 '+STAMP+' · 报价规则')).to_be_visible(timeout=30000)
 page.screenshot(path=str(QA/'search-ui.png'),full_page=True)
 page.get_by_role('button',name='讨论与消息',exact=True).click();page.get_by_role('button',name='发起讨论',exact=True).click()
 dialog=page.get_by_role('dialog');dialog.get_by_label('标题',exact=True).fill('页面验收 '+STAMP+' · 讨论');dialog.get_by_label('内容',exact=True).fill('请核对订单金额断言。');dialog.get_by_role('button',name='保存',exact=True).click()
 page.get_by_role('button',name='查看详情').first.click();page.get_by_role('dialog').get_by_role('button',name='回复',exact=True).click();page.get_by_label('回复内容').fill('已核对订单金额1380分。');page.get_by_role('dialog').get_by_role('button',name='保存',exact=True).click();page.get_by_role('button',name='查看详情').first.click();expect(page.get_by_role('dialog').get_by_text('已核对订单金额1380分。',exact=True)).to_be_visible();page.keyboard.press('Escape')
 page.get_by_role('button',name='记忆中心',exact=True).click();page.get_by_role('button',name='新建记忆').click();dialog=page.get_by_role('dialog');dialog.get_by_label('标题',exact=True).fill('页面验收 '+STAMP+' · 长期经验');dialog.get_by_label('内容',exact=True).fill('金额应采用分为单位断言。');dialog.get_by_role('button',name='保存',exact=True).click()
 page.get_by_role('button',name='查看详情').first.click();page.get_by_role('dialog').get_by_role('button',name='确认生效').click();expect(page.get_by_text('已确认',exact=True)).to_be_visible()
 page.get_by_role('button',name='知识库',exact=True).click();page.get_by_role('button',name='上传资料').click();dialog=page.get_by_role('dialog');dialog.get_by_label('文件',exact=True).set_input_files({'name':'浏览器上传.txt','mimeType':'text/plain','buffer':'接口401需要检查登录令牌是否过期。'.encode()});dialog.get_by_role('button',name='保存',exact=True).click();expect(page.get_by_role('heading',name='任务与草案')).to_be_visible()
 subprocess.run([str(PROJECT/'.venv/bin/python'),'-c','from aiworkbench.worker import Worker;Worker().run_once()'],env={**os.environ,'PYTHONPATH':str(PROJECT/'src'),'AIWORKBENCH_CONFIG':str(QA/'config/settings.json'),'OPENBLAS_NUM_THREADS':'1'},check=True,timeout=120)
 page.get_by_role('button',name='刷新',exact=True).click();expect(page.get_by_text('已完成',exact=True)).to_be_visible()
 page.get_by_role('button',name='模型与设置',exact=True).click();expect(page.get_by_text('未启用',exact=False).first).to_be_visible();page.get_by_role('button',name='配置',exact=True).first.click();expect(page.get_by_label('API 密钥')).to_have_value('');page.keyboard.press('Escape')
 page.get_by_role('button',name='工作台',exact=True).click();page.screenshot(path=str(QA/'home-ui.png'),full_page=True)
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path=str(QA/'mobile-ui.png'),full_page=True)
 assert not errors,errors;b.close()
for file in QA.glob('*-ui.png'):file.chmod(0o600)
print('浏览器验收通过：登录、项目、知识文本、混合检索、讨论回复、记忆审核、文件处理、空密钥设置与390px布局；页面异常0。')
