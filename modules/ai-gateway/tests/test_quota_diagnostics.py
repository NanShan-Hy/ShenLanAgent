import logging
import re

import httpx
import pytest

from aigateway.billing import ledger_check
from conftest import chat_result, configured


@pytest.mark.parametrize('dimension', ['tpm', 'daily_tokens'])
def test_large_context_quota_rejection_is_safe_and_does_not_dispatch(gateway, caplog, dimension):
    g = configured(gateway)
    g['db'].execute("UPDATE upstreams SET provider='openai',base_url='https://api.openai.com/v1' WHERE id=?", (g['upstream'],))
    g['db'].execute("UPDATE models SET provider='openai' WHERE id=?", (g['model'],))
    calls = []
    g['app'].state.upstream_transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=chat_result()))
    policy = {'rpm': 20, 'tpm': 200000, 'daily_tokens': 200000, 'concurrency': 2}
    policy[dimension] = 100000
    assert g['client'].put('/api/v1/admin/policies/user/' + g['user'], json=policy).status_code == 200
    body = {'model': 'qa-model', 'max_tokens': 8192, 'messages': [
        {'role': 'system', 'content': 'PRIVATE_SYNTHETIC_CONTEXT_' + '\U0010ffff' * 27500},
        {'role': 'user', 'content': '现在时间是？'}]}
    before = [dict(row) for row in g['db'].all('SELECT * FROM ledger ORDER BY id')]
    with caplog.at_level(logging.WARNING, logger='aigateway.quota'):
        response = g['client'].post('/v1/chat/completions', json=body, headers=g['headers'])
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'quota_exceeded'
    assert calls == []
    assert g['db'].one('SELECT count(*) n FROM requests')['n'] == 0
    assert g['db'].one('SELECT count(*) n FROM upstream_attempts')['n'] == 0
    assert before == [dict(row) for row in g['db'].all('SELECT * FROM ledger ORDER BY id')]
    records = [r.getMessage() for r in caplog.records if r.name == 'aigateway.quota']
    assert len(records) == 1
    match = re.fullmatch(r'quota_rejected scope=user dimension=' + dimension
                         + r' used=0 requested=(\d+) limit=100000', records[0])
    assert match and 100000 < int(match[1]) < 131072
    for private in ('PRIVATE_SYNTHETIC_CONTEXT_', '现在时间是', g['token'], g['user'], g['tenant']):
        assert private not in records[0]
    assert 'PRIVATE_SYNTHETIC_CONTEXT_' not in response.text

    # The same request succeeds only after an explicit policy update in this isolated fixture.
    policy[dimension] = 200000
    assert g['client'].put('/api/v1/admin/policies/user/' + g['user'], json=policy).status_code == 200
    caplog.clear()
    assert g['client'].post('/v1/chat/completions', json=body, headers=g['headers']).status_code == 200
    assert len(calls) == 1
    assert not [r for r in caplog.records if r.name == 'aigateway.quota']
    assert ledger_check(g['db'])['ok']
