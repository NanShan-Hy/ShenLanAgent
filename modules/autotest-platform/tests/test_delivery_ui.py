import json
import os

from playwright.sync_api import expect, sync_playwright

from conftest import serve, BROWSER_LIBS, PASSWORD
from testdock.worker import Worker


def test_import_matrix_history_rerun_and_export_in_browser(targets, tmp_path):
    c, db, settings, api = targets
    errors = []
    document = {'openapi': '3.0.3', 'paths': {'/api/info': {'get': {'responses': {'200': {}}}}}}
    with serve(api) as base, sync_playwright() as p:
        browser = p.chromium.launch(env={**os.environ, 'LD_LIBRARY_PATH': BROWSER_LIBS})
        page = browser.new_page(viewport={'width': 1440, 'height': 1050}, accept_downloads=True)
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(base)
        page.get_by_label('账号', exact=True).fill('admin')
        page.get_by_label('密码', exact=True).fill(PASSWORD)
        page.get_by_role('button', name='登录工作台').click()
        expect(page.get_by_role('heading', name='测试工作台')).to_be_visible()
        page.get_by_role('button', name='测试用例', exact=False).first.click()
        page.get_by_role('button', name='导入', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('JSON 文档').fill(json.dumps(document))
        dialog.get_by_label('名称前缀').fill('迁入 · ')
        dialog.get_by_role('button', name='解析文档').click()
        expect(dialog.get_by_label('GET /api/info', exact=True)).to_be_checked()
        dialog.get_by_role('button', name='预检选中项').click()
        dialog.get_by_role('button', name='确认导入').click()
        expect(page.get_by_role('dialog', name='导入完成')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_text('迁入 · GET /api/info', exact=True)).to_be_visible()
        with page.expect_download() as download_info:
            page.get_by_role('button', name='导出', exact=True).click()
        path = tmp_path / 'assets.json'
        download_info.value.save_as(path)
        assert 'demo-pass' not in path.read_text()
        assert len(json.loads(path.read_text())['cases']) == 3
        page.get_by_role('row').filter(has_text='迁入 · GET /api/info').get_by_role('button', name='运行', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('测试演示', exact=True).check()
        dialog.get_by_role('button', name='提交执行').click()
        expect(page.get_by_role('dialog', name='执行报告')).to_be_visible()
        assert len(db.all("SELECT id FROM runs WHERE status='queued'")) == 2
        worker = Worker(settings)
        worker.run_once(); worker.run_once()
        expect(page.get_by_role('dialog').get_by_text('已通过', exact=True).first).to_be_visible(timeout=10000)
        page.get_by_role('dialog').get_by_role('button', name='重跑原快照').click()
        expect(page.get_by_role('dialog').get_by_text('排队中', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        page.get_by_label('执行状态').select_option('queued')
        page.get_by_role('button', name='查询', exact=True).click()
        expect(page.get_by_role('heading', name='执行历史 · 1 条')).to_be_visible()
        worker.run_once()
        page.get_by_label('执行状态').select_option('passed')
        page.get_by_label('环境筛选').fill('测试演示')
        page.get_by_role('button', name='查询', exact=True).click()
        expect(page.get_by_role('heading', name='执行历史 · 1 条')).to_be_visible()
        page.screenshot(path=str(tmp_path / 'history.png'), full_page=True)
        page.get_by_role('button', name='接入指南', exact=True).click()
        expect(page.get_by_role('heading', name='项目接入指南')).to_be_visible()
        with page.expect_download() as template_download:
            page.get_by_role('link', name='下载销售流程示例').click()
        template_download.value.save_as(tmp_path / 'sales.json')
        assert len(json.loads((tmp_path / 'sales.json').read_text())['cases']) == 2
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.get_by_role('button', name='展开菜单').click()
        page.get_by_role('button', name='测试用例', exact=False).first.click()
        expect(page.get_by_role('button', name='导入', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        browser.close()
    assert not errors, errors
