"""Crew trust recovery must go through verified broker and Crew ACP approval."""
from __future__ import annotations

import io
from unittest.mock import patch
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.mcp_client import McpManager
from kiro_byo.mcp_transports import StdioMcpServer
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.providers.base import ToolCall
from kiro_byo.tool_center import tool_center_report

ENTRY = {"type": "stdio", "name": "kirocrew-core", "command": "missing", "args": []}
NAME = "mcp__kirocrew-core__learn_add"
TOOL = {"name":"learn_add", "description":"Write a learning", "inputSchema":{"type":"object"}}


def manager():
    m = McpManager([ENTRY])
    m._ready_event.set()
    return m


def test_missing_crew_tool_refreshes_live_catalogue_and_still_requests_host_approval(tmp_path):
    m=manager()
    host=AcpServer(stdin=io.StringIO(),stdout=io.StringIO())
    host._runtime_event=lambda *a,**k:None
    host._emit_tool_status=lambda *a,**k:None
    approved=[]
    host._request_tool_permission=lambda *a: approved.append(a) or True
    calls=[]
    with (patch.object(StdioMcpServer, 'refresh_tools', autospec=True,
                       side_effect=lambda self: setattr(self,'tools',[TOOL]) or [TOOL]),
          patch.object(m,'call',side_effect=lambda n,arg: calls.append(n) or 'saved')):
        try:
            session=Session('s',str(tmp_path),'model',mcp=m)
            text,_,_=host._execute_mcp_tool(session,ToolCall('c1',NAME,'{}'),{})
            assert text=='saved'
            assert calls==[NAME]
            assert len(approved)==1
            assert m.is_live_binding(NAME)
        finally:
            m.close();host._executor.shutdown(wait=True)


def test_host_denial_still_blocks_tool_after_revalidation(tmp_path):
    m=manager();host=AcpServer(stdin=io.StringIO(),stdout=io.StringIO())
    host._runtime_event=lambda *a,**k:None
    host._emit_tool_status=lambda *a,**k:None
    host._request_tool_permission=lambda *a:False
    calls=[]
    with (patch.object(StdioMcpServer, 'refresh_tools', autospec=True,
                       side_effect=lambda self: setattr(self,'tools',[TOOL]) or [TOOL]),
          patch.object(m,'call',side_effect=lambda n,arg:calls.append(n))):
        try:
            text,_,_=host._execute_mcp_tool(Session('s',str(tmp_path),'model',mcp=m),ToolCall('c1',NAME,'{}'),{})
            assert 'Permission denied' in text and calls==[]
        finally:
            m.close();host._executor.shutdown(wait=True)


def test_missing_unmounted_crew_tool_cannot_be_recovered():
    m=McpManager([])
    try:
        assert not m.revalidate_requested_crew_binding(NAME)
        assert m.bindings=={}
    finally:m.close()


def test_unverified_or_absent_broker_fails_closed():
    m=manager()
    with patch.object(StdioMcpServer,'refresh_tools',side_effect=OSError('offline')):
        try:
            assert not m.revalidate_requested_crew_binding(NAME)
            assert not m.is_live_binding(NAME)
        finally:m.close()


def test_agent_excludes_even_a_newly_verified_crew_tool(tmp_path):
    m=manager();host=AcpServer(stdin=io.StringIO(),stdout=io.StringIO())
    events=[];host._runtime_event=lambda name,session,**k:events.append((name,k.get('data',{})))
    host._emit_tool_status=lambda *a,**k:None
    calls=[];host._request_tool_permission=lambda *a:calls.append('approval') or True
    agent=AgentProjection(agent_id='restricted',tools_declared=True,tools=('@mcp',),excluded_tools=('@kirocrew-core/learn_add',))
    with patch.object(StdioMcpServer,'refresh_tools',autospec=True,side_effect=lambda self:setattr(self,'tools',[TOOL]) or [TOOL]):
        try:
            text,_,_=host._execute_mcp_tool(Session('s',str(tmp_path),'model',mcp=m,agent=agent),ToolCall('c1',NAME,'{}'),{})
            assert 'agent_scope_denied' in text and calls==[]
            assert any(data.get('reason')=='agent_scope_denied' for name,data in events if name=='tool.dispatch.blocked')
        finally:
            m.close();host._executor.shutdown(wait=True)


def test_other_server_cannot_gain_first_party_binding_by_name():
    m=McpManager([{"type":"stdio","name":"untrusted","command":"none","args":[]}]);m._ready_event.set()
    try:
        assert not m.revalidate_requested_crew_binding(NAME)
    finally:m.close()


def test_tool_center_is_historical_and_contains_no_payload():
    events=[{'event':'crew.capabilities.changed','session_id':'s','timestamp':2,
             'data':{'tool_names':[NAME],'tool_count':1,'verified_crew_servers':['kirocrew-core'],
                     'broker_token':'please-do-not-show'}},
            {'event':'tool.dispatch.blocked','session_id':'s','timestamp':3,
             'data':{'kind':'mcp','tool':NAME,'reason':'agent_scope_denied','arguments':{'private':'no'}}}]
    report=tool_center_report(['read'],events)
    assert report['session_snapshots'][0]['tools']==[NAME]
    assert report['recent_blocks'][0]['reason']=='agent_scope_denied'
    assert report['live_permission_state'] is False
    assert 'please-do-not-show' not in str(report)
    assert 'private' not in str(report)


def test_real_acp_permission_request_is_delivered_to_crew_with_mcp_identity(tmp_path):
    """The existing Crew 'Trust' mode answers THIS request; BYO grants nothing."""
    m=manager();m.servers['kirocrew-core'].tools=[TOOL];m._rebuild_bindings();m._live_verified_servers.add('kirocrew-core')
    host=AcpServer(stdin=io.StringIO(),stdout=io.StringIO()); requests=[]
    host._emit_tool_call=lambda *a,**k:{'toolCallId':'c1', 'title':'learn_add'}
    host._request_client=lambda method,params,**kwargs:requests.append((method,params)) or {'outcome':{'outcome':'selected','optionId':'allow_once'}}
    try:
        session=Session('s',str(tmp_path),'model',mcp=m)
        assert host._request_tool_permission(session,'c1',NAME,{'key':'test'}) is True
        method,params=requests[0]
        assert method=='session/request_permission'
        assert params['sessionId']=='s'
        assert params['_meta']['kiro']['mcpTool']['identity']=={'serverName':'kirocrew-core','toolName':'learn_add'}
        assert any(o['optionId']=='reject_once' for o in params['options'])
    finally:
        m.close();host._executor.shutdown(wait=True)


def test_tool_center_endpoint_enforces_token_and_reports_only_recorded_data(tmp_path):
    import json, threading, urllib.error, urllib.request
    from kiro_byo.web_ui import _ManagementServer
    from kiro_byo.runtime_trace import RuntimeTraceStore
    from kiro_byo.credential_store import default_credential_store
    trace=RuntimeTraceStore(tmp_path/'events.jsonl')
    trace.record('crew.capabilities.changed',session_id='session-1',data={
        'tool_count':1,'tool_names':[NAME],'verified_crew_servers':['kirocrew-core']})
    # The handler uses an application token and does not expose session env.
    server=_ManagementServer(('127.0.0.1',0),default_credential_store(),'local-test-token',runtime_trace=trace)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_address[1]}/api/tool-center'
    try:
        try:
            urllib.request.urlopen(url,timeout=3)
            raise AssertionError('unauthenticated tool listing unexpectedly permitted')
        except urllib.error.HTTPError as exc:
            assert exc.code==403
        req=urllib.request.Request(url,headers={'X-Kiro-BYO-Token':'local-test-token'})
        with urllib.request.urlopen(req,timeout=3) as response:
            report=json.load(response)
        assert report['read_only'] is True
        assert report['live_permission_state'] is False
        assert report['session_snapshots'][0]['tools']==[NAME]
        assert 'api_key' not in str(report)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_first_verified_search_waits_for_live_broker_even_with_cached_schema():
    m=manager()
    try:
        m._ready_event.clear()
        m.catalog_cache_hits.add('kirocrew-core')
        m.servers['kirocrew-core'].tools=[TOOL]  # cached-only, not verified
        m._rebuild_bindings()
        waits=[]
        def finish_start():
            waits.append('waited')
            m._live_verified_servers.add('kirocrew-core')
            m._ready_event.set()
        with patch.object(m,'start_background',side_effect=finish_start):
            matches,_=m.search_tools('learn add',verified_only=True)
        assert waits==['waited']
        assert [x.public_name for x in matches]==[NAME]
    finally:m.close()
