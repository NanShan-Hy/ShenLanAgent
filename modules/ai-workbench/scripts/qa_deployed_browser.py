"""Browser deployment acceptance; reads live data and imports one private report."""
import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
BASE=Path('/home/nanshan/workspace');QA=BASE/'data/ai-workbench/dev'
delivery=json.loads((QA/'delivery.json').read_text());errors=[]
def password(service):return re.search(r'密码：([^\n]+)',(BASE/'services'/service/'config/initial-admin.txt').read_text()).group(1)
def login(page,service,port):
 page.goto('http://172.16.61.128:'+str(port))
 page.get_by_label('账号',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(password(service));page.get_by_role('button',name='登录工作台',exact=True).click()
with sync_playwright() as p:
 browser=p.chromium.launch(env={**os.environ,'LD_LIBRARY_PATH':str(BASE/'tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu')})
 context=browser.new_context(viewport={'width':1440,'height':1050});page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
 login(page,'ai-workbench',8090);expect(page.get_by_role('heading',name='工作区概览')).to_be_visible()
 scope=page.get_by_label('检索项目范围');expect(scope).to_have_value('');assert scope.locator('option').count()==4
 page.screenshot(path=str(QA/'production-home.png'),full_page=True)
 page.get_by_role('button',name='知识库',exact=True).click();expect(page.get_by_role('heading',name='知识库',exact=True)).to_be_visible()
 expect(page.get_by_role('heading',name='README.md',exact=True)).to_be_visible()
 page.get_by_role('button',name='AI 对话与检索',exact=True).click();page.get_by_label('问题或任务').fill('开发源码和真实配置应该存放在哪里');page.get_by_role('button',name='本地检索',exact=True).click()
 expect(page.get_by_text('本地关键词 + 语义检索',exact=True)).to_be_visible(timeout=30000)
 page.get_by_role('button',name='查看来源').first.click();expect(page.get_by_role('dialog')).to_be_visible();page.keyboard.press('Escape');page.screenshot(path=str(QA/'production-search.png'),full_page=True)
 page.get_by_role('button',name='模型与设置',exact=True).click();expect(page.get_by_text('已绑定。项目权限会实时复核。',exact=True)).to_be_visible()
 expect(page.get_by_text('DeepSeek',exact=True)).to_be_visible();page.get_by_role('button',name='配置',exact=True).first.click();expect(page.get_by_label('API 密钥')).to_have_value('');page.keyboard.press('Escape')
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path=str(QA/'production-mobile.png'),full_page=True);page.set_viewport_size({'width':1440,'height':1050})
 td=context.new_page();td.on('pageerror',lambda e:errors.append(str(e)));login(td,'autotest-platform',8088)
 td.get_by_title('账户设置',exact=True).click();td.get_by_role('button',name='连接 AI 工作台',exact=True).click();expect(td.get_by_label('绑定码',exact=True)).to_be_visible();assert len(td.get_by_label('绑定码',exact=True).input_value())>40;td.keyboard.press('Escape')
 td.get_by_role('button',name='执行记录',exact=True).click();td.get_by_role('button',name='查看报告',exact=True).first.click()
 expect(td.get_by_role('link',name='AI 分析',exact=True)).to_be_visible()
 link=td.get_by_role('link',name='AI 分析',exact=True).get_attribute('href');assert '#testdock-run=' in link
 with context.expect_page() as popup:td.get_by_role('link',name='AI 分析',exact=True).click()
 page=popup.value;page.on('pageerror',lambda e:errors.append(str(e)))
 expect(page.get_by_role('heading',name='AI 对话与检索',exact=True)).to_be_visible();expect(page.get_by_text('已附加 1 份明确选择的证据。')).to_be_visible()
 page.get_by_role('button',name='本地检索',exact=True).is_visible();assert not errors,errors
 page.screenshot(path=str(QA/'production-report-handoff.png'),full_page=True)
 browser.close()
for path in QA.glob('production-*.png'):path.chmod(0o600)
delivery['browser']={'login_lan':True,'default_scope':'all accessible projects','search':'local-hybrid','model_key_blank':True,'testdock_binding_code_ui':True,'report_handoff':True,'viewport_widths':[1440,390],'page_errors':errors}
(QA/'delivery.json').write_text(json.dumps(delivery,ensure_ascii=False,indent=2))
print('上线浏览器验收通过：局域网登录、全项目范围、知识与来源、本地混合检索、空密钥、390px、TestDock绑定码及报告联动；页面异常0。')
