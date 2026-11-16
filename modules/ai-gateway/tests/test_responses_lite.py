"""Codex Responses Lite declarations preserve scope without enabling hosted tools."""
import copy
import json

import httpx
import pytest

from aigateway.billing import ledger_check
from conftest import configured
from test_clients import response_stream


def declarations():
    return {'type':'additional_tools','id':'at-qa','role':'developer','tools':[
        {'type':'namespace','name':'functions','description':'QA local tools','tools':[
            {'type':'function','name':'read_fixture','parameters':{'type':'object','properties':{
                'file_id':{'type':'string'},'image_url':{'type':'string'}},'additionalProperties':False}},
            {'type':'custom','name':'apply_patch','format':{'type':'text'}}]},
        {'type':'tool_search','execution':'client','parameters':{'type':'object'}}]}


def body(item=None):
    return {'model':'qa-model','input':[item if item is not None else declarations(),
        {'type':'message','role':'user','content':[{'type':'input_text','text':'QA_TOOL_BODY_PRIVATE'}]}],
        'stream':True}


def test_lite_declarations_keep_order_and_schema_fields_and_are_not_persisted(gateway):
    g=configured(gateway);sent=[];request=body();original=copy.deepcopy(request)
    def upstream(r):
        sent.append(json.loads(r.content));return response_stream('LITE_OK')
    g['app'].state.upstream_transport=httpx.MockTransport(upstream)
    r=g['client'].post('/v1/responses',json=request,headers=g['headers'])
    assert r.status_code==200 and 'LITE_OK' in r.text
    assert sent[0]['input']==original['input'] and request==original
    assert 'tools' not in sent[0] and sent[0]['model']=='upstream-model'
    stored=g['db'].path.read_bytes()
    assert b'QA_TOOL_BODY_PRIVATE' not in stored and b'read_fixture' not in stored
    assert g['db'].one('SELECT status FROM requests')['status']=='succeeded'
    assert ledger_check(g['db'])['ok']


@pytest.mark.parametrize('tool',[
    {'type':'web_search'}, {'type':'file_search','vector_store_ids':['qa']},
    {'type':'mcp','server_url':'https://hosted.invalid'}, {'type':'code_interpreter'},
    {'type':'tool_search'}, {'type':'tool_search','execution':'server'},
    {'type':'namespace','name':'nested','tools':[{'type':'web_search'}]},
])
@pytest.mark.parametrize('placement',['additional_tools','tool_search_output'])
def test_hosted_tools_cannot_enter_through_inline_or_search_results(gateway,tool,placement):
    g=configured(gateway);calls=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:calls.append(r) or response_stream('UNEXPECTED'))
    item=({'type':'additional_tools','role':'developer','tools':[tool]} if placement=='additional_tools' else
        {'type':'tool_search_output','execution':'client','call_id':'search-qa','tools':[tool]})
    r=g['client'].post('/v1/responses',json=body(item),headers=g['headers'])
    assert r.status_code==400 and r.json()['error']['code']=='unsupported_tool'
    assert not calls and g['db'].one('SELECT count(*) n FROM requests')['n']==0
    assert ledger_check(g['db'])['ok']


@pytest.mark.parametrize('item',[
    {'type':'additional_tools','role':'developer','tools':{}},
    {'type':'additional_tools','role':'user','tools':[]},
    {'type':'message','role':'user','content':[declarations()]},
    {'type':'tool_search_output','execution':'server','call_id':'qa','tools':[]},
])
def test_invalid_inline_placement_and_shapes_are_rejected(gateway,item):
    g=configured(gateway)
    assert g['client'].post('/v1/responses',json=body(item),headers=g['headers']).status_code==400
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==0


def test_lite_request_still_requires_platform_credit(gateway):
    g=configured(gateway,balance_fen=0)
    r=g['client'].post('/v1/responses',json=body(),headers=g['headers'])
    assert r.status_code==402 and r.json()['error']['code']=='insufficient_quota'
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==0


def test_client_tool_search_result_definitions_are_preserved(gateway):
    g=configured(gateway);sent=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:sent.append(json.loads(r.content)) or response_stream('SEARCH_OK'))
    request=body();request['input'][1:1]=[
        {'type':'tool_search_call','execution':'client','call_id':'qa-search','arguments':{'query':'local tools'}},
        {'type':'tool_search_output','execution':'client','call_id':'qa-search','tools':declarations()['tools'][:1]}]
    r=g['client'].post('/v1/responses',json=request,headers=g['headers'])
    assert r.status_code==200 and sent[0]['input']==request['input']
