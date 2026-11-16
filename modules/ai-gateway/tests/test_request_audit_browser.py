import os

import httpx
from playwright.sync_api import expect,sync_playwright

from conftest import PASSWORD,configured
from live import serve,BROWSER_LIBS


def test_operator_filters_full_contents_settings_and_manual_cleanup(gateway,tmp_path):
    g=configured(gateway)
    private='BROWSER_AUDIT_QUESTION <img src=x onerror="window.auditXss=1">'
    with serve(g['app'],lifespan='off') as url,sync_playwright() as p:
        with httpx.Client(trust_env=False) as client:
            assert client.post(url+'/v1/chat/completions',json={'model':'qa-model','messages':[{'role':'user','content':private}],'max_tokens':64},headers=g['headers']).status_code==200
        browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':BROWSER_LIBS})
        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(url)
        page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(PASSWORD)
        page.get_by_role('button',name='登录控制台',exact=True).click()
        page.get_by_role('button',name='运营管理',exact=True).click()
        page.get_by_role('button',name='账号与配额',exact=True).click()
        page.get_by_role('button',name='查看请求',exact=True).click()
        expect(page.get_by_role('heading',name='用户请求记录',exact=True)).to_be_visible()
        expect(page.get_by_label('请求用户',exact=True)).to_have_value(g['user'])
        page.get_by_role('button',name='查看问答',exact=True).click()
        dialog=page.get_by_role('dialog')
        expect(dialog.get_by_label('完整问题',exact=True)).to_contain_text('BROWSER_AUDIT_QUESTION')
        timeline=dialog.locator('table').first.locator('tbody tr')
        expect(timeline.first.locator('td').first).to_have_text('接收')
        expect(timeline.last.locator('td').first).to_have_text('结束')
        assert not page.evaluate('window.auditXss')
        assert dialog.locator('img').count()==0
        dialog.get_by_role('button',name='模型回答',exact=True).click()
        expect(dialog.get_by_label('完整回答',exact=True)).to_contain_text('本地协议回答')
        dialog.get_by_role('button',name='工具过程',exact=True).click()
        expect(dialog.get_by_label('工具调用明细',exact=True)).to_be_visible()
        page.screenshot(path=str(tmp_path/'request-audit-detail.png'),full_page=True)
        page.keyboard.press('Escape')
        page.get_by_role('button',name='记录设置',exact=True).click()
        expect(page.get_by_role('dialog').get_by_label('正文容量（MiB）',exact=True)).to_have_value('1024')
        page.keyboard.press('Escape')
        page.get_by_role('button',name='清理正文',exact=True).click()
        dialog=page.get_by_role('dialog')
        dialog.get_by_label('清理截止时间',exact=True).fill('2099-01-01T00:00')
        dialog.get_by_role('button',name='预览清理范围',exact=True).click()
        expect(dialog.get_by_text('本批 1 条正文',exact=False)).to_be_visible()
        dialog.get_by_role('button',name='执行清理',exact=True).click()
        expect(page.get_by_text('正文已清理',exact=True)).to_be_visible()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.get_by_role('button',name='查看问答',exact=True).click()
        expect(page.get_by_role('dialog').get_by_text('管理员已手动清理正文',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(tmp_path/'request-audit-mobile.png'),full_page=True)
        assert not errors
        browser.close()
