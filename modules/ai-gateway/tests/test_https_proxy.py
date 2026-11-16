import asyncio
import html
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import time
from urllib.parse import parse_qs,urlparse,urlencode

import httpx
from playwright.sync_api import expect,sync_playwright
import pytest

from aigateway.billing import ledger_check
from aigateway.core import private_write
from live import BROWSER_LIBS,serve
from scripts.render_https_proxy import render_config
from test_account_pool import pool
from test_clients import response_stream
from conftest import configured
from conftest import PASSWORD

NGINX=Path(os.environ.get('AI_GATEWAY_TEST_NGINX','/home/nanshan/workspace/tools/nginx/1.24.0-2ubuntu7.19/usr/sbin/nginx'))


def test_real_tls_proxy_login_callback_streaming_and_no_sensitive_temp_files(pool,tmp_path):
    if not NGINX.is_file():pytest.skip('Set AI_GATEWAY_TEST_NGINX to run real Nginx TLS acceptance')
    pool['settings'].allow_private_upstreams=True
    g=configured(pool);run=tmp_path/'proxy';run.mkdir(mode=0o700)
    certificate,key=run/'test-cert.pem',run/'test-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=gateway.test','-addext','subjectAltName=DNS:gateway.test,IP:127.0.0.1',
        '-keyout',str(key),'-out',str(certificate)],check=True,capture_output=True)
    key.chmod(0o600);certificate.chmod(0o640)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    public='https://gateway.test:'+str(port)
    marker='PROXY_CALLBACK_SECRET_MUST_NOT_PERSIST'
    question='PROXY_QUESTION_BODY_PRIVATE'
    release=__import__('threading').Event()
    class HeldStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data=response_stream('ACCOUNT_POOL_OK').content;first,rest=data.split(b'\n\n',1)
            yield first+b'\n\n'
            if not await asyncio.to_thread(release.wait,8):raise TimeoutError('SSE buffering stalled')
            yield rest
    original_handler=g['app'].state.upstream_transport.handler
    g['control']['callback_code']=marker
    def vendor(request):
        if request.method=='GET' or request.url.path.startswith('/v8/management'):
            response=original_handler(request)
            if request.url.path.endswith('/oauth/callback'):g['control']['status']='ok'
            return response
        return httpx.Response(200,stream=HeldStream(),headers={'content-type':'text/event-stream'})
    g['app'].state.upstream_transport=httpx.MockTransport(vendor)
    g['db'].execute('UPDATE models SET context_limit=2000000 WHERE id=?',(g['model'],))
    g['db'].execute('UPDATE api_keys SET ips=? WHERE id=?',(json.dumps(['127.0.0.1']),g['key_id']))
    with serve(g['app'],lifespan='off') as backend:
        config=render_config(domain='gateway.test',certificate=certificate,private_key=key,
            run_dir=run,backend_port=urlparse(backend).port,tls_port=port)
        path=run/'nginx.conf';private_write(path,config)
        args=[str(NGINX),'-p',str(run)+'/', '-c',str(path),'-e',str(run/'bootstrap.log')]
        syntax=subprocess.run(args+['-t'],capture_output=True,text=True)
        assert syntax.returncode==0,syntax.stderr
        stderr=run/'process.stderr';private_write(stderr,'')
        context=ssl.create_default_context(cafile=str(certificate))
        with stderr.open('w') as err:
            process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=err,start_new_session=True)
            try:
                with httpx.Client(base_url='https://127.0.0.1:'+str(port),verify=context,trust_env=False,
                                  headers={'Host':'gateway.test:'+str(port)},timeout=3) as client:
                    for _ in range(100):
                        assert process.poll() is None,stderr.read_text()
                        try:
                            if client.get('/api/v1/health').json()['status']=='ok':break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.02)
                    else:raise AssertionError('TLS proxy did not become healthy')
                    bad=client.post('/api/v1/auth/login',headers={'X-Requested-With':'AIGateway','Origin':'https://attacker.test'},json={'username':'admin','password':PASSWORD})
                    assert bad.status_code==403
                    with pytest.raises(httpx.RemoteProtocolError):
                        client.get('/api/v1/health',headers={'Host':'untrusted.test'})
                    g['settings'].public_url=public
                    callbacks=[];page_errors=[];callback_cookies=[]
                    with sync_playwright() as p:
                        browser_args=['--host-resolver-rules=MAP gateway.test 127.0.0.1']
                        browser_env={k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
                        browser_env['LD_LIBRARY_PATH']=BROWSER_LIBS
                        browser=p.chromium.launch(env=browser_env,args=browser_args,
                            proxy={'server':'http://127.0.0.1:9','bypass':'gateway.test,127.0.0.1,localhost'})
                        browser_context=browser.new_context(ignore_https_errors=True)
                        page=browser_context.new_page()
                        page.on('pageerror',lambda error:page_errors.append(str(error)))
                        def callback_response(response):
                            if urlparse(response.url).path=='/auth/callback':
                                callbacks.append(response.headers)
                                raw=response.request.all_headers().get('cookie','')
                                callback_cookies.append({'portal':'gateway_session=' in raw})
                        page.on('response',callback_response)
                        def authorize(route):
                            params=parse_qs(urlparse(route.request.url).query)
                            target=public+'/auth/callback?'+urlencode({'state':params['state'][0],'code':marker})
                            route.fulfill(status=200,content_type='text/html',body='<h1>Local consent fixture</h1><a href="'+html.escape(target,quote=True)+'">Complete simulated authorization</a>')
                        browser_context.route('https://auth.openai.com/oauth/authorize**',authorize)
                        page.goto(public)
                        page.get_by_label('账号',exact=True).fill('admin')
                        page.get_by_label('密码',exact=True).fill(PASSWORD)
                        page.get_by_role('button',name='登录控制台',exact=True).click()
                        expect(page.get_by_role('heading',name='工作区概览',exact=True)).to_be_visible()
                        cookie=next(c for c in browser_context.cookies() if c['name']=='gateway_session')
                        assert cookie['secure'] and cookie['httpOnly'] and cookie['sameSite']=='Strict'
                        page.get_by_role('button',name='GPT账号池',exact=True).click()
                        page.get_by_role('button',name='添加GPT账号',exact=True).click()
                        dialog=page.get_by_role('dialog')
                        dialog.get_by_label('同意新增GPT账号供当前账号池承接项目任务',exact=True).check()
                        dialog.get_by_role('button',name='生成登录链接',exact=True).click()
                        with page.expect_popup() as opened:
                            dialog.get_by_role('link',name='前往GPT登录',exact=True).click()
                        popup=opened.value
                        popup.on('response',callback_response)
                        expect(popup.get_by_role('heading',name='Local consent fixture')).to_be_visible()
                        popup.get_by_role('link',name='Complete simulated authorization').click()
                        popup.wait_for_url('**/auth/callback/result**')
                        expect(page.get_by_role('dialog')).to_have_count(0,timeout=10000)
                        assert callback_cookies==[{'portal':False}]
                        assert callbacks[-1]['referrer-policy']=='no-referrer'
                        assert callbacks[-1]['cache-control']=='no-store'
                        assert marker not in popup.url
                        page.screenshot(path=str(tmp_path/'https-proxy-account.png'),full_page=True)
                        assert not page_errors,page_errors
                        browser.close()
                    body={'model':'qa-model','input':question+'x'*(256*1024),'stream':True}
                    try:
                        with client.stream('POST','/v1/responses',json=body,headers={
                            'Authorization':'Bearer '+g['token'],'X-Forwarded-For':'203.0.113.99','Forwarded':'for=203.0.113.99'}) as response:
                            assert response.status_code==200
                            lines=response.iter_lines()
                            first=next(lines)
                            assert 'response.created' in first
                            # The provider's terminal event is withheld until the first
                            # event crosses TLS/proxy/application without buffering.
                            release.set();rest='\n'.join(lines)
                            assert 'response.completed' in rest and 'ACCOUNT_POOL_OK' in rest
                    finally:release.set()
                    assert g['db'].one('SELECT status FROM requests')['status']=='succeeded'
                    assert ledger_check(g['db'])['ok']
                assert not list((run/'client_body').glob('**/*'))
                assert not list((run/'proxy').glob('**/*'))
            finally:
                release.set()
                if process.poll() is None:
                    process.send_signal(signal.SIGQUIT)
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
            assert process.returncode==0,stderr.read_text()
    forbidden=(marker.encode(),question.encode(),g['token'].encode(),b'ACCOUNT_POOL_OK')
    for p in run.rglob('*'):
        if p.is_file():assert not any(value in p.read_bytes() for value in forbidden),p.name
    private_write(tmp_path/'https-verification.json',json.dumps({'nginx_syntax':True,'certificate':'self-signed local QA only',
        'login_secure_cookie':True,'oauth_callback':True,'cross_site_document_committed':True,
        'portal_strict_cookie_not_sent_to_callback':True,'callback_bound_to_active_portal_session_without_cookie':True,
        'sse_first_event_before_terminal_release':True,
        'large_body_bytes':256*1024,'request_body_temp_files':0,'sensitive_proxy_log_matches':0,
        'host_spoof_rejected':True,'forged_origin_rejected':True,'forwarded_client_ip_ignored':True,
        'real_provider':False,'public_https_verified':False},indent=2))
