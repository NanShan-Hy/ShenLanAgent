import os
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from conftest import serve,PASSWORD,BROWSER_LIBS
from test_ai import generation,connected,policy
from testdock.ai_runtime import AIRuntime


def test_assistant_browser_actions_attachment_history_and_mobile(generation,tmp_path):
    connected,gateway=generation;c,db,s,app,wb,context=connected;policy(c,context);errors=[]
    gateway.outputs=[[{'type':'function_call','name':'prepare_action','call_id':'ui-write','arguments':json.dumps({'operation':'case.create','project_id':1,'arguments':{'name':'浏览器AI用例','steps':[{'action':'http','url':'/api/info'}]}})}]]
    with serve(app) as url,sync_playwright() as p:
        browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':BROWSER_LIBS})
        page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(url);page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(PASSWORD);page.get_by_role('button',name='登录工作台').click()
        page.locator('.nav-link').filter(has_text='AI 助手').click();expect(page.get_by_role('heading',name='全局 AI 助手')).to_be_visible()
        page.get_by_label('AI 问题').fill('请新增一个接口测试用例，先让我审核')
        page.get_by_role('button',name='发送',exact=True).click()
        expect(page.get_by_text('排队中',exact=True)).to_be_visible();AIRuntime(db).run_once()
        expect(page.get_by_role('button',name='审核并确认')).to_be_visible(timeout=15000)
        assert not db.one("SELECT id FROM cases WHERE name='浏览器AI用例'")
        page.get_by_role('button',name='审核并确认').click();expect(page.get_by_role('dialog',name='审核操作 · 新增用例')).to_be_visible()
        page.get_by_role('button',name='确认执行',exact=True).click()
        expect(page.get_by_text('已执行',exact=True)).to_be_visible()
        assert db.one("SELECT count(*) n FROM cases WHERE name='浏览器AI用例'")['n']==1
        AIRuntime(db).run_once();expect(page.get_by_text('连接成功。',exact=True)).to_be_visible(timeout=15000)
        page.get_by_role('button',name='添加附件',exact=True).click();page.get_by_label('附件文件').set_input_files({'name':'qa.txt','mimeType':'text/plain','buffer':b'BROWSER_ATTACHMENT_SENTINEL'})
        page.get_by_label('同意将此附件发送给外部模型',exact=True).check();page.get_by_role('dialog').get_by_role('button',name='保存',exact=True).click()
        expect(page.get_by_text('qa.txt · 1 KiB',exact=True)).to_be_visible()
        gateway.outputs=[[{'type':'message','content':[{'type':'output_text','text':'已读取附件内容 [C1]。'}]}]]
        page.get_by_label('AI 问题').fill('检查附件');page.get_by_role('button',name='发送',exact=True).click();expect(page.get_by_text('排队中',exact=True)).to_be_visible()
        AIRuntime(db).run_once();expect(page.get_by_text('已读取附件内容 [C1]。',exact=True)).to_be_visible(timeout=15000)
        page.reload();page.locator('.nav-link').filter(has_text='AI 助手').click();expect(page.get_by_text('已读取附件内容 [C1]。',exact=True)).to_be_visible()
        page.screenshot(path=str(tmp_path/'ai-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(tmp_path/'ai-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),page.evaluate('[document.documentElement.scrollWidth,innerWidth]')
        page.locator('.topbar').get_by_role('button',name='AI 助手',exact=True).click();expect(page.get_by_role('dialog',name='AI 快捷助手')).to_be_visible()
        page.get_by_role('dialog',name='AI 快捷助手').get_by_role('button',name='AI 设置',exact=True).click();expect(page.get_by_role('dialog',name='AI 连接设置')).to_be_visible()
        assert page.get_by_label('专用应用 Key').input_value()==''
        page.keyboard.press('Escape');page.get_by_role('dialog',name='AI 快捷助手').get_by_role('button',name='关闭',exact=True).click()
        assert not errors,errors
        browser.close()
