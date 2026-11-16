"""Local operator-owned bridge configuration; never exposed through the portal."""
import json
from pathlib import Path
from urllib.parse import urlparse

VERSION = '8.0.72'
ARCHIVE = 'CLIProxyAPI_8.0.72_linux_amd64_no-plugin.tar.gz'
ARCHIVE_SHA256 = '12ff0146cc2f0b663088d7a8b597cef1b96767e7ac6c8844e3cd21669c2693c3'
RELEASE_URL = 'https://github.com/router-for-me/CLIProxyAPI/releases/download/v'+VERSION+'/'


def load_connector(settings):
    path = settings.account_pool_config
    if path is None:
        return None
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise ValueError('账号池连接配置需为私有绝对路径文件，权限600')
    if path.stat().st_size > 16384:
        raise ValueError('账号池连接配置过大')
    raw = json.loads(path.read_text())
    if not isinstance(raw,dict) or not isinstance(raw.get('base_url'),str):
        raise ValueError('账号池连接配置格式无效')
    url = urlparse(raw.get('base_url',''))
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path != '/v1' or url.query or url.fragment):
        raise ValueError('本机账号池必须使用127.0.0.1独立端口的/v1地址')
    if url.port == settings.port:
        raise ValueError('账号池不能指向网关自身')
    for name in ('client_key','management_key'):
        value = raw.get(name)
        if not isinstance(value,str) or not 32 <= len(value) <= 256 or any(not 33<=ord(c)<=126 for c in value):
            raise ValueError('账号池内部凭据格式无效')
    return raw


def render_backend(service, *, port, client_key, management_key):
    """JSON is a YAML subset. No shell or third-party config parser is needed."""
    return {
        'config-version':8,
        'server':{'host':'127.0.0.1','port':port,'max-request-body-bytes':4*1024**2,
            'commercial-mode':True,'trusted-proxies':[],'shutdown-timeout':20,
            'response-compression':{'enabled':False},'discovery':{'enabled':False},
            'kv-store':{'disabled':True}},
        'management':{'allow-remote':False,'secret-key':management_key,
            'disable-control-panel':True,'disable-auto-update-panel':True},
        'access':{'api-keys':[client_key]},
        'oauth':{'auth-dir':str(service/'data/credentials'),'auth-auto-refresh-workers':2},
        'routing':{'strategy':'round-robin','session-affinity':True,
            'session-affinity-rebind-on':'quota','session-affinity-ttl':'1h',
            'retry':{'request-retry':0,'max-retry-credentials':1,'max-retry-interval':0}},
        'requests':{'streaming':{'bootstrap-retries':0,'keepalive-seconds':0}},
        'observability':{'logs':{'debug':False,'logging-to-file':False,'request-log':False,
                'logs-max-total-size-mb':0,'error-logs-max-files':0},
            'usage':{'usage-statistics-enabled':False},'pprof':{'enable':False}},
        'plugins':{'enabled':False},
    }
