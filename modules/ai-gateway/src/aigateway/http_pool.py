"""Application-owned TCP pooling; authentication and cookies never become shared state."""
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx


class RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def client_for(api):
    transport = api.state.upstream_transport
    key = id(transport) if transport is not None else None
    clients = api.state.http_clients
    if key not in clients:
        clients[key] = httpx.AsyncClient(
            timeout=120, trust_env=False, follow_redirects=False, transport=transport,
            cookies=CookieJar(policy=RejectCookies()),
            limits=httpx.Limits(max_connections=api.state.db.settings.max_concurrency + 4,
                               max_keepalive_connections=api.state.db.settings.max_concurrency,
                               keepalive_expiry=30))
    return clients[key]


async def close_clients(api):
    for client in api.state.http_clients.values():
        await client.aclose()
    api.state.http_clients.clear()
