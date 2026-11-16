"""Read-only production UI/health checks; creates and logs out only its own login.

Never enables models, sends generation requests, edits policies or runs tests.
"""
import json
import os
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

BASE=Path('/home/nanshan/workspace')
OUT=BASE/'data/autotest-platform/dev/ai-v13'


def main():
    OUT.mkdir(parents=True,exist_ok=True,mode=0o700)
    password=re.search(r'密码：([^\n]+)',(BASE/'services/autotest-platform/config/initial-admin.txt').read_text()).group(1)
    errors=[];server_errors=[];stamp=str(time.time_ns())
    report={'checks':[],'real_generation':False,'business_mutations':False,'screenshots':[]}
    with sync_playwright() as p:
        browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':str(BASE/'tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu')})
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:server_errors.append({'url':r.url,'status':r.status}) if r.status>=500 else None)
        page.goto('http://127.0.0.1:8088')
        page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(password)
        page.get_by_role('button',name='登录工作台').click()
        expect(page.get_by_role('heading',name='测试工作台')).to_be_visible()
        page.locator('.nav-link').filter(has_text='AI 助手').click()
        expect(page.get_by_role('heading',name='全局 AI 助手')).to_be_visible()
        status=page.request.get('http://127.0.0.1:8088/api/ai/status').json()
        assert status['worker_online'] and not status['enabled'] and not status['configured'],status
        report['status']=status;report['checks'].append('native assistant, disabled model and live AI worker')
        page.get_by_role('button',name='资料范围',exact=True).click()
        expect(page.get_by_role('dialog',name='资料范围与外发')).to_be_visible()
        context=page.request.get('http://127.0.0.1:8088/api/ai/context').json()
        assert len(context['projects'])>=2
        report['authorized_projects']=len(context['projects']);report['checks'].append('session-bound knowledge delegation with existing identity')
        page.keyboard.press('Escape')
        page.get_by_role('button',name='AI 设置',exact=True).click()
        expect(page.get_by_role('dialog',name='AI 连接设置')).to_be_visible()
        assert page.get_by_label('专用应用 Key').input_value()==''
        assert page.get_by_label('网关 Base URL').input_value()=='http://127.0.0.1:8094/v1'
        page.keyboard.press('Escape')
        for name,viewport in [('desktop',{'width':1440,'height':1000}),('mobile',{'width':390,'height':844})]:
            page.set_viewport_size(viewport)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            image=OUT/(stamp+'-production-'+name+'.png');page.screenshot(path=str(image),full_page=True);image.chmod(0o600);report['screenshots'].append(str(image))
        report['checks'].append('desktop and 390px responsive layout')
        page.set_viewport_size({'width':1440,'height':1000})
        page.get_by_role('button',name='执行记录',exact=True).click()
        page.get_by_role('button',name='查看报告',exact=True).first.click()
        page.get_by_role('button',name='AI 分析',exact=True).click()
        expect(page.get_by_role('dialog',name='AI 快捷助手')).to_be_visible()
        expect(page.get_by_text('已附加 1 份报告或明确来源。',exact=True)).to_be_visible()
        assert not page.get_by_role('dialog',name='AI 快捷助手').get_by_role('button',name='发送',exact=True).is_enabled()
        report['checks'].append('report opens native drawer with evidence and no generation')
        page.get_by_role('dialog',name='AI 快捷助手').get_by_role('button',name='关闭',exact=True).click()
        page.request.post('http://127.0.0.1:8088/api/auth/logout',headers={'X-Requested-With':'TestDock'},data='{}')
        browser.close()
    assert not errors,errors
    assert not server_errors,server_errors
    report['script_errors']=errors;report['server_errors']=server_errors;report['passed']=True
    target=OUT/'production.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2));target.chmod(0o600)
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
