import os

from playwright.sync_api import expect,sync_playwright

from conftest import configured,PASSWORD
from live import serve,BROWSER_LIBS
from test_conversations import conversation,send


def test_operator_conversations_policies_contents_cleanup_and_mobile(gateway,tmp_path):
    g=configured(gateway);c=conversation(g,task_type='sales');assert send(g,c,'BROWSER_CONTEXT <img src=x onerror="window.badContext=1">').status_code==200
    with serve(g['app'],lifespan='off') as url,sync_playwright() as p:
        browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':BROWSER_LIBS});page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
        page.on('pageerror',lambda e:errors.append(type(e).__name__))
        page.goto(url);page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(PASSWORD)
        page.get_by_role('button',name='登录控制台',exact=True).click();page.get_by_role('button',name='运营管理',exact=True).click()
        page.get_by_role('button',name='会话与调度',exact=True).click();expect(page.get_by_role('heading',name='任务会话',exact=True)).to_be_visible()
        page.get_by_role('button',name='查看会话',exact=True).click();expect(page.get_by_role('dialog').get_by_label('会话摘要',exact=True)).to_be_visible()
        page.get_by_role('button',name='查看输入',exact=True).click();expect(page.get_by_label('托管轮次内容',exact=True)).to_contain_text('BROWSER_CONTEXT')
        assert not page.evaluate('window.badContext')
        page.screenshot(path=str(tmp_path/'managed-context-desktop.png'),full_page=True)
        page.keyboard.press('Escape');page.keyboard.press('Escape')
        page.get_by_role('button',name='新增规则',exact=True).click();dialog=page.get_by_role('dialog')
        dialog.get_by_label('规则应用',exact=True).select_option(g['application']);dialog.get_by_label('账号故障策略',exact=True).select_option('strict')
        dialog.get_by_role('button',name='保存',exact=True).click();expect(dialog).not_to_be_visible()
        assert g['db'].one('SELECT policy FROM conversation_policies WHERE application_id=?',(g['application'],))['policy']=='strict'
        page.get_by_role('button',name='清理会话',exact=True).click();dialog=page.get_by_role('dialog')
        expect(dialog).to_contain_text('请求指标和账本保留');dialog.get_by_role('button',name='确认清理历史',exact=True).click()
        expect(page.get_by_text('正文已清理',exact=False)).to_be_visible()
        page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(tmp_path/'managed-context-mobile.png'),full_page=True)
        assert not errors;browser.close()
