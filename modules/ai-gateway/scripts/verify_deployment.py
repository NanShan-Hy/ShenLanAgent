"""Read-only production acceptance. Does not create Keys, model calls, or financial orders."""
import json
import os
import re
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from aigateway.billing import ledger_check
from aigateway.core import BASE, Database, Settings, digest, private_write

QA = BASE / 'data/ai-gateway/dev'
BROWSER_LIBS = str(BASE / 'tools/playwright-libs/1.2.11/usr/lib/x86_64-linux-gnu')


def main():
    settings = Settings.load()
    db = Database(settings)
    password_path = settings.key_file.parent / 'initial-admin.txt'
    password = re.search(r'密码：([^\n]+)', password_path.read_text()).group(1)
    url = 'http://127.0.0.1:' + str(settings.port)
    result = {'checked_at': time.time(), 'real_model_calls': 0, 'real_payments': 0,
              'whole_vm_reboot_tested': False, 'logout_linger_tested': False}
    with httpx.Client(base_url=url, headers={'X-Requested-With': 'AIGateway'}, trust_env=False, timeout=15) as client:
        client.post('/api/v1/auth/login', json={'username': 'admin', 'password': password}).raise_for_status()
        result['health'] = client.get('/api/v1/health').json()
        assert result['health']['status'] == 'ok' and result['health']['maintenance_online']
        assert result['health']['version'] == '0.7.0'
        assert db.one('SELECT version FROM schema_version')['version'] == 6
        user = client.get('/api/v1/me').json()
        assert user['is_admin'] and user['tenants']
        overview = client.get('/api/v1/admin/overview').json()
        assert overview['ledger_check']['ok']
        result['counts'] = {table: db.one('SELECT count(*) n FROM ' + table)['n']
                            for table in ('users', 'tenants', 'applications', 'api_keys', 'pools', 'upstreams', 'models', 'orders', 'requests', 'ledger',
                                          'upstream_attempts', 'upstream_probes', 'upstream_catalog','chatgpt_accounts','chatgpt_oauth_attempts','chatgpt_usage_state',
                                          'request_audits','request_capture_chunks')}
        status = client.get('/api/v1/admin/pool-status')
        status.raise_for_status()
        result['schema_version'] = 6
        recording=client.get('/api/v1/admin/request-audit-settings')
        recording.raise_for_status()
        result['request_recording']={k:recording.json()[k] for k in ('enabled','max_bytes','used_bytes','retention','mode','failure_policy','partial_contents')}
        records=client.get('/api/v1/admin/request-audits')
        records.raise_for_status()
        result['request_audits']={'total':records.json()['total'],'page_size':records.json()['page_size']}
        assert client.get('/api/v1/admin/chatgpt/integration').status_code==404
        assert client.get('/api/v1/chatgpt/accounts').status_code==404
        account_pool=client.get('/api/v1/admin/account-pool/status')
        account_pool.raise_for_status()
        assert account_pool.json()['configured'] and account_pool.json()['online']
        result['account_pool']={k:account_pool.json()[k] for k in ('configured','online','ready')}
        result['account_pool']['accounts']=len(account_pool.json()['accounts'])
        assert not settings.public_registration and not settings.allow_private_upstreams
        assert client.get('/v1/models').status_code == 401
        result['ledger'] = ledger_check(db)
    deployed = BASE / 'services/ai-gateway/app'
    release = json.loads((deployed / 'release.json').read_text())
    for relative, expected in release['sha256'].items():
        assert digest((deployed / relative).read_bytes()) == expected, relative
    result['release'] = {'deployed_at': release['deployed_at'], 'commit': release['commit'],
                         'verified_files': len(release['sha256']), 'backup': release['backup']}
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(env={**os.environ, 'LD_LIBRARY_PATH': BROWSER_LIBS})
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(type(error).__name__))
        page.goto(url)
        page.get_by_label('账号', exact=True).fill('admin')
        page.get_by_label('密码', exact=True).fill(password)
        page.get_by_role('button', name='登录控制台', exact=True).click()
        expect(page.get_by_role('heading', name='工作区概览', exact=True)).to_be_visible()
        page.screenshot(path=str(QA / 'gateway-deployed-overview.png'), full_page=True)
        for name in ('应用与密钥', '用量与请求', '钱包与套餐', '空间与成员', '接入文档'):
            page.get_by_role('button', name=name, exact=True).click()
            expect(page.get_by_role('heading', name=name, exact=True)).to_be_visible()
        page.get_by_role('button', name='运营管理', exact=True).click()
        page.get_by_role('button', name='新增上游', exact=True).click()
        expect(page.get_by_label('上游API密钥', exact=True)).to_have_value('')
        page.keyboard.press('Escape')
        page.get_by_role('button', name='连接池状态', exact=True).click()
        expect(page.get_by_role('heading', name='上游调度状态', exact=True)).to_be_visible()
        page.screenshot(path=str(QA / 'gateway-deployed-pool-status.png'), full_page=True)
        page.get_by_role('button', name='订单与套餐', exact=True).click()
        expect(page.get_by_role('heading', name='人工核款与退款', exact=True)).to_be_visible()
        page.get_by_role('button', name='账号与配额', exact=True).click()
        expect(page.get_by_role('heading', name='平台账号', exact=True)).to_be_visible()
        page.get_by_role('button', name='请求审计', exact=True).click()
        expect(page.get_by_role('heading',name='用户请求记录',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='记录设置',exact=True)).to_be_visible()
        page.screenshot(path=str(QA / 'gateway-deployed-request-audits.png'),full_page=True)
        page.get_by_role('button', name='会话与调度', exact=True).click()
        expect(page.get_by_role('heading',name='任务会话',exact=True)).to_be_visible()
        page.screenshot(path=str(QA / 'gateway-deployed-conversations.png'),full_page=True)
        page.get_by_role('button', name='操作审计', exact=True).click()
        page.get_by_role('button', name='上游与模型', exact=True).click()
        page.screenshot(path=str(QA / 'gateway-deployed-operations.png'), full_page=True)
        page.get_by_role('button', name='GPT账号池', exact=True).click()
        expect(page.get_by_role('heading', name='GPT账号池', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='添加GPT账号', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='ChatGPT账号', exact=True)).to_have_count(0)
        page.screenshot(path=str(QA / 'gateway-deployed-account-pool.png'),full_page=True)
        page.get_by_role('button', name='钱包与套餐', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate('scrollTo(0,0)')
        expect(page.locator('.sidebar')).to_have_css('transform', 'matrix(1, 0, 0, 1, -236, 0)')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(QA / 'gateway-deployed-mobile.png'), full_page=True)
        assert not errors
        browser.close()
    result['browser'] = {'login': True, 'pages': 8, 'operations_tabs': 7, 'mobile_width': 390, 'page_errors': errors}
    baseline = json.loads((QA / 'baseline-services.json').read_text())
    result['existing_services'] = {}
    with httpx.Client(trust_env=False, timeout=10) as client:
        for name, endpoint in (('autotest-platform', 'http://127.0.0.1:8088/api/health'),
                               ('ai-workbench', 'http://127.0.0.1:8090/api/v1/health')):
            health = client.get(endpoint).json()
            assert health['status'] == 'ok' and health['worker_online']
            current = json.loads((BASE / 'services' / name / 'app/release.json').read_text())
            assert current['deployed_at'] == baseline['services'][name]['release_deployed_at']
            result['existing_services'][name] = {'health': health, 'release_unchanged': True}
    for path in QA.glob('gateway-deployed-*.png'):
        path.chmod(0o600)
    private_write(QA / 'delivery-v07.json', json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print browser call logs that could contain the locally loaded initial password.
        raise SystemExit('部署验收未完成：' + type(exc).__name__ + '；请检查本地界面及服务日志。')
