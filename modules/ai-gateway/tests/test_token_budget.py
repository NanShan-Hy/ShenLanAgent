"""Large Codex context must use token budgets without weakening financial holds."""
import json

import httpx
import pytest

from aigateway.billing import cost, ledger_check
from aigateway.routing import validate_payload
from aigateway import token_budget
from conftest import configured
from test_clients import response_stream


@pytest.fixture
def token_gateway(gateway):
    g = configured(gateway)
    # The common fixture uses a generic compatible provider, which deliberately
    # retains byte accounting. Exercise the OpenAI branch against a synthetic vendor.
    g['db'].execute("UPDATE upstreams SET provider='openai',base_url='https://api.openai.com/v1' WHERE id=?", (g['upstream'],))
    g['db'].execute("UPDATE models SET provider='openai' WHERE id=?", (g['model'],))
    return g


def large_codex_body(model, raw_budget, lite):
    tools = [{'type': 'function', 'name': 'local_test_' + str(n),
              'description': 'A synthetic client tool. Read local fixtures and report test results. ' * 3,
              'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}
             for n in range(80)]
    body = {'model': model['alias'], 'stream': True, 'max_output_tokens': 8192,
            'input': [{'type': 'message', 'role': 'user', 'content': 'PRIVATE_INPUT_QUESTION: 现在时间是？'}],
            'instructions': ''}
    if lite:
        body['input'].insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': tools})
    else:
        body['tools'] = tools
    payload, _, output = validate_payload(body, 'responses', model)
    remaining = raw_budget - token_budget.reservation_input_bound(payload) - output
    assert remaining > 0
    seed = 'Synthetic system instructions for local development, client tools and isolated tests. '
    body['instructions'] = (seed * (remaining // len(seed) + 1))[:remaining]
    return body


@pytest.mark.parametrize('raw_budget', [121765, 155000])
@pytest.mark.parametrize('lite', [False, True])
def test_codex_byte_overestimate_passes_unchanged_limits_and_preserves_full_context(token_gateway, raw_budget, lite):
    g = token_gateway
    limits = {'rpm': 20, 'tpm': 100000, 'concurrency': 2, 'daily_tokens': 200000}
    assert g['client'].put('/api/v1/admin/policies/user/' + g['user'], json=limits).status_code == 200
    model = g['db'].one('SELECT * FROM models WHERE id=?', (g['model'],))
    body = large_codex_body(model, raw_budget, lite)
    payload, estimated, output = validate_payload(body, 'responses', model)
    byte_bound = token_budget.reservation_input_bound(payload)
    assert byte_bound + output == raw_budget
    assert estimated + output < 100000
    seen = []
    def vendor(request):
        seen.append(json.loads(request.content))
        return response_stream('TOKEN_BUDGET_OK')
    g['app'].state.upstream_transport = httpx.MockTransport(vendor)
    response = g['client'].post('/v1/responses', json=body, headers=g['headers'])
    assert response.status_code == 200 and 'TOKEN_BUDGET_OK' in response.text, response.text
    assert len(seen) == 1 and seen[0] == payload
    row = g['db'].one('SELECT * FROM requests')
    price = json.loads(row['price'])
    assert row['estimate_tokens'] == estimated + output
    assert row['reserved_micro'] == max(cost(price, byte_bound, output),
                                        cost(price, byte_bound, output, byte_bound))
    assert row['reserved_micro'] > row['charged_micro']
    assert row['status'] == 'succeeded' and row['charged_micro'] > 0
    assert 'PRIVATE_INPUT_QUESTION' not in g['db'].path.read_bytes().decode('utf-8', 'ignore')
    assert ledger_check(g['db'])['ok']


def test_true_token_overflow_is_rejected_before_dispatch(token_gateway):
    g = token_gateway
    calls = []
    g['app'].state.upstream_transport = httpx.MockTransport(lambda request: calls.append(request) or response_stream('UNEXPECTED'))
    response = g['client'].post('/v1/responses', json={
        'model': 'qa-model', 'input': '\U0010ffff' * 35000}, headers=g['headers'])
    assert response.status_code == 400 and response.json()['error']['code'] == 'context_limit'
    assert not calls and g['db'].one('SELECT count(*) n FROM requests')['n'] == 0
    assert ledger_check(g['db'])['ok']


@pytest.mark.parametrize('provider', ['deepseek', 'compatible'])
def test_unknown_vendor_tokenizers_keep_byte_bound(provider):
    payload = {'input': '中英文 synthetic context ' * 2000}
    assert token_budget.estimate_input(payload, provider) == token_budget.reservation_input_bound(payload)


def test_bundled_vocabularies_load_offline_and_special_looking_text_is_ordinary(monkeypatch):
    import socket
    import tiktoken.load
    def forbidden(*args, **kwargs):
        raise AssertionError('Tokenizer must never fetch resources during a request')
    token_budget.encoding.cache_clear()
    with monkeypatch.context() as isolated:
        isolated.setattr(socket, 'create_connection', forbidden)
        isolated.setattr(tiktoken.load, 'read_file', forbidden)
        payload = {'input': '<|endoftext|> 中文客户端工具 🧪 synthetic context ' * 100}
        for provider in ('openai', 'account_pool'):
            assert 2048 < token_budget.estimate_input(payload, provider) < token_budget.reservation_input_bound(payload)


@pytest.mark.parametrize('corrupt', [False, True])
def test_missing_or_corrupt_resources_fail_closed_without_charges(token_gateway, monkeypatch, tmp_path, corrupt):
    g = token_gateway
    if corrupt:
        (tmp_path / 'cl100k_base.tiktoken').write_bytes(b'not the official vocabulary')
    token_budget.encoding.cache_clear()
    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(token_budget, 'DATA', tmp_path)
            response = g['client'].post('/v1/responses', json={'model': 'qa-model', 'input': 'Synthetic'}, headers=g['headers'])
        assert response.status_code == 503 and response.json()['error']['code'] == 'tokenizer_unavailable'
        assert g['db'].one('SELECT count(*) n FROM requests')['n'] == 0
        assert g['db'].one('SELECT count(*) n FROM upstream_attempts')['n'] == 0
        assert ledger_check(g['db'])['ok']
    finally:
        token_budget.encoding.cache_clear()
