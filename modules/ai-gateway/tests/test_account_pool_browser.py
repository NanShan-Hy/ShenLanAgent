import os
from contextlib import ExitStack
import pytest
from urllib.parse import urlencode

import httpx
from playwright.sync_api import expect,sync_playwright

from conftest import PASSWORD
from live import BROWSER_LIBS,serve
from test_account_pool import pool


@pytest.mark.parametrize('automatic',[False,True])
def test_browser_first_setup_authorization_models_key_and_project_call(pool,tmp_path,automatic):
    g=pool;account=g['control']['files'][0].copy();g['control']['files']=[];errors=[]
    with ExitStack() as stack,serve(g['app'],lifespan='off') as url,sync_playwright() as p:
        if automatic:stack.enter_context(serve(g['app'],lifespan='off',port=1455))
        g['settings'].public_url=url
        browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':BROWSER_LIBS})
        context=browser.new_context(viewport={'width':1440,'height':1000})
        callback='http://localhost:1455/auth/callback?'+urlencode({'state':'QA-STATE','code':'QA-code'})
        context.route('https://auth.openai.com/oauth/authorize**',lambda r:r.fulfill(content_type='text/html',body='<a href="'+callback+'">Complete synthetic sign in</a>'))
        if not automatic:context.route('http://localhost:1455/auth/callback**',lambda r:r.fulfill(content_type='text/html',body='Synthetic callback URL. Return to the gateway.'))
        page=context.new_page();page.on('pageerror',lambda e:errors.append(type(e).__name__))
        page.goto(url)
        page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(PASSWORD)
        page.get_by_role('button',name='登录控制台',exact=True).click()
        page.get_by_role('button',name='GPT账号池',exact=True).click()
        expect(page.get_by_role('heading',name='GPT账号池',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='ChatGPT账号',exact=True)).to_have_count(0)
        expect(page.get_by_text('还没有GPT账号，请点击添加GPT账号',exact=True)).to_be_visible()
        page.get_by_role('button',name='创建账号池连接',exact=True).click()
        dialog=page.get_by_role('dialog')
        dialog.get_by_label('同意账号池在上述范围内承接项目任务',exact=True).check()
        dialog.get_by_role('button',name='创建连接',exact=True).click()
        expect(page.get_by_role('heading',name='模型与项目接入',exact=True)).to_be_visible()
        page.get_by_role('button',name='添加GPT账号',exact=True).click()
        dialog=page.get_by_role('dialog')
        dialog.get_by_label('同意新增GPT账号供当前账号池承接项目任务',exact=True).check()
        dialog.get_by_role('button',name='生成登录链接',exact=True).click()
        with page.expect_popup() as opened:
            dialog.get_by_role('link',name='前往GPT登录',exact=True).click()
        popup=opened.value
        popup.get_by_role('link',name='Complete synthetic sign in',exact=True).click()
        if automatic:
            popup.wait_for_url('http://localhost:1455/auth/callback/result**')
            assert 'QA-code' not in popup.url
            expect(popup.get_by_text('这是结果页，无需复制地址或再次手动提交。回调已接收不代表账号登录已经成功。',exact=True)).to_be_visible()
            dialog.get_by_label('授权后返回的完整地址',exact=True).fill(popup.url)
            dialog.get_by_role('button',name='提交授权回调',exact=True).click()
            expect(dialog.get_by_text('这是自动回调后的结果页地址，无需再次提交。请查看上方最终登录状态，失败时按具体原因处理。',exact=True)).to_be_visible()
            assert g['control']['exchanges']==1
        else:
            popup.wait_for_url('http://localhost:1455/auth/callback**')
            dialog.get_by_label('授权后返回的完整地址',exact=True).fill('http://localhost:1455/auth/callback?state=WRONG&code=QA-code')
            dialog.get_by_role('button',name='提交授权回调',exact=True).click()
            expect(dialog.get_by_label('授权后返回的完整地址',exact=True)).to_have_value('http://localhost:1455/auth/callback?state=WRONG&code=QA-code')
            dialog.get_by_label('授权后返回的完整地址',exact=True).fill(popup.url)
            dialog.get_by_role('button',name='提交授权回调',exact=True).click()
        g['control']['files']=[account];g['control']['status']='ok';popup.close()
        expect(page.get_by_role('dialog')).to_have_count(0,timeout=8000)
        expect(page.get_by_text('QA GPT账号',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='发布模型路由',exact=True)).to_be_enabled()
        page.get_by_role('button',name='发布模型路由',exact=True).click()
        dialog=page.get_by_role('dialog')
        dialog.get_by_label('gpt-qa-pool',exact=True).check()
        dialog.get_by_label('输入价格（元/百万Token）',exact=True).fill('1')
        dialog.get_by_label('缓存输入价格（元/百万Token）',exact=True).fill('0.5')
        dialog.get_by_label('输出价格（元/百万Token）',exact=True).fill('2')
        dialog.get_by_role('button',name='发布所选模型',exact=True).click()
        expect(page.get_by_text('gpt/gpt-qa-pool',exact=True)).to_be_visible()
        desktop=tmp_path/'pool-setup-desktop.png';page.screenshot(path=str(desktop),full_page=True);desktop.chmod(0o600)
        page.get_by_role('button',name='生成项目Key',exact=True).click()
        page.get_by_role('dialog').get_by_role('button',name='创建项目Key',exact=True).click()
        dialog=page.get_by_role('dialog')
        expect(dialog.get_by_role('heading',name='项目接入信息',exact=True)).to_be_visible()
        key=dialog.get_by_label('平台Key',exact=True).input_value()
        assert key.startswith('sk-gw-')
        assert 'AI_GATEWAY_API_KEY' in dialog.inner_text() and 'wire_api = "responses"' in dialog.inner_text()
        order=g['client'].post('/api/v1/orders',json={'kind':'topup','amount_fen':1000,'client_ref':'browser-pool-funding'}).json()
        assert g['client'].post('/api/v1/admin/orders/'+order['id']+'/approve',json={'reference':'本地合成，不是真实收款'}).status_code==200
        with httpx.Client(trust_env=False) as client:
            response=client.post(url+'/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':'gpt/gpt-qa-pool','messages':[{'role':'user','content':'Synthetic browser client'}]})
            assert response.status_code==200
        dialog.get_by_role('button',name='关闭弹窗',exact=True).click()
        page.get_by_label('选择账号 QA GPT账号',exact=True).check()
        page.get_by_role('button',name='停用所选',exact=True).click()
        expect(page.get_by_role('heading',name='账号处理结果',exact=True)).to_be_visible()
        page.get_by_role('dialog').get_by_role('button',name='关闭弹窗',exact=True).click()
        expect(page.get_by_text('已停用',exact=True)).to_be_visible()
        page.set_viewport_size({'width':390,'height':844})
        page.evaluate('scrollTo(0,0)')
        expect(page.locator('.sidebar')).to_have_css('transform','matrix(1, 0, 0, 1, -236, 0)')
        page.get_by_role('button',name='打开菜单',exact=True).click()
        expect(page.locator('.sidebar')).to_have_css('transform','matrix(1, 0, 0, 1, 0, 0)')
        page.get_by_role('button',name='GPT账号池',exact=True).click()
        expect(page.locator('.sidebar')).to_have_css('transform','matrix(1, 0, 0, 1, -236, 0)')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        mobile=tmp_path/'pool-setup-mobile.png';page.screenshot(path=str(mobile),full_page=True,animations='disabled');mobile.chmod(0o600)
        assert not errors
        browser.close()
