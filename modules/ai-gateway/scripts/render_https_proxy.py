"""Render a reviewable Nginx configuration. Does not change settings or start services."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
from pathlib import Path
import re

from aigateway.core import private_write

ROOT=Path(__file__).resolve().parents[1]


def config_path(value):
    path=Path(value)
    if not path.is_absolute() or any(c in str(path) for c in '\n\r\x00$"\'\\;{}'):
        raise ValueError('配置路径需为绝对路径，不能含换行、引号、变量或指令分隔符')
    return '"'+str(path)+'"'


def render_config(*, domain, certificate, private_key, run_dir, backend_port=8094,
                  tls_port=8443, listen='127.0.0.1', site_only=False):
    domain=domain.lower()
    if (len(domain)>253 or '.' not in domain or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in domain.split('.'))):
        raise ValueError('请提供不含协议、端口或路径的DNS域名；国际域名请使用Punycode')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError('请使用登记回调的DNS域名')
    address=ipaddress.ip_address(listen)
    if type(backend_port) is not int or type(tls_port) is not int or not 1<=backend_port<=65535 or not 1<=tls_port<=65535:
        raise ValueError('端口应为1至65535')
    if backend_port==tls_port:
        raise ValueError('代理与后端端口不能相同')
    run=Path(run_dir)
    values={'DOMAIN':domain,'CERTIFICATE':config_path(certificate),'PRIVATE_KEY':config_path(private_key),
        'TLS_PORT':str(tls_port),'BACKEND_PORT':str(backend_port),
        'LISTEN_ADDRESS':str(address) if address.version==4 else '['+str(address)+']',
        'UPSTREAM_NAME':'ai_gateway_'+hashlib.sha256((domain+':'+str(tls_port)).encode()).hexdigest()[:12]}
    config_path(run)
    site=(ROOT/'deploy/nginx-site.conf.template').read_text()
    for key,value in values.items():site=site.replace('@@'+key+'@@',value)
    if '@@' in site:raise ValueError('模板含未替换字段')
    if site_only:return site
    preamble='''worker_processes 1;
daemon off;
pid %s;
error_log stderr notice;
events { worker_connections 256; }
http {
    default_type application/octet-stream;
    access_log off;
'''%config_path(run/'nginx.pid')
    for name in ('client_body','proxy','fastcgi','uwsgi','scgi'):
        preamble+='    '+name+'_temp_path '+config_path(run/name)+';\n'
    return preamble+site+'\n}\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain',required=True)
    parser.add_argument('--certificate',required=True,type=Path)
    parser.add_argument('--private-key',required=True,type=Path)
    parser.add_argument('--run-dir',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--listen',default='127.0.0.1')
    parser.add_argument('--tls-port',default=8443,type=int)
    parser.add_argument('--backend-port',default=8094,type=int)
    parser.add_argument('--site-only',action='store_true')
    args=parser.parse_args()
    if not args.output.is_absolute():parser.error('输出路径需为绝对路径')
    if not args.certificate.is_file() or not args.private_key.is_file():parser.error('请先准备证书与私钥文件')
    if args.private_key.stat().st_mode & 0o077:parser.error('私钥仅允许持有人访问，请设置600或更严格权限')
    if args.run_dir.exists() and args.run_dir.stat().st_mode & 0o077:
        parser.error('运行目录必须仅由持有人访问，请使用权限700的目录')
    try:
        config=render_config(domain=args.domain,certificate=args.certificate,private_key=args.private_key,
            run_dir=args.run_dir,backend_port=args.backend_port,tls_port=args.tls_port,listen=args.listen,site_only=args.site_only)
        args.run_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
        private_write(args.output,config,exclusive=True)
    except (ValueError,OSError) as error:
        parser.exit(2,str(error)+'\n')
    print('代理配置已生成：'+str(args.output)+'；尚未启动代理或修改平台public_url。')


if __name__=='__main__':main()
