"""0.2.5: session-scoped planning, no Crew authorization/scheduler bypass."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from kiro_byo.native_tools import NativeToolRuntime, ToolValidationError, PermissionDecision
from kiro_byo.tool_center import tool_center_report


def test_plan_replaces_and_reads_without_persisting(tmp_path):
    a=NativeToolRuntime(tmp_path)
    b=NativeToolRuntime(tmp_path)
    try:
        assert a.has_tool('update_plan')
        incoming={"operation":"replace", "steps":[
            {"id":"one", "title":"Inspect current tools", "status":"in_progress"},
            {"id":"two", "title":"Verify changes", "status":"pending"}]}
        call=a.validate('update_plan',incoming)
        assert a.permission_engine.evaluate(call) == PermissionDecision.HOST_GATE
        response=json.loads(a.execute(call).text)
        assert response['durable'] is False
        assert len(response['steps'])==2
        assert json.loads(a.execute(a.validate('update_plan',{'operation':'get'})).text)['steps'][0]['id']=='one'
        assert json.loads(b.execute(b.validate('update_plan',{'operation':'get'})).text)['steps']==[]
        assert a.execute(a.validate('update_plan',{'operation':'replace','steps':[]})).text
        assert list(Path(tmp_path).glob('**/*plan*')) == []
    finally:
        a.close();b.close()

@pytest.mark.parametrize('args',[
    {},{'operation':'other'}, {'operation':'get','steps':[]},
    {'operation':'replace','steps':[{'id':'a','title':'one','status':'pending'}, {'id':'a','title':'two','status':'pending'}]},
    {'operation':'replace','steps':[{'id':'bad/path','title':'x','status':'pending'}]},
    {'operation':'replace','steps':[{'id':'a','title':'x','status':'unknown'}]},
    {'operation':'replace','steps':[{'id':'a','title':'x','status':'pending','secret':'surprise'}]},
    {'operation':'replace','steps':[{'id':'a','title':'x','status':'in_progress'}, {'id':'b','title':'x','status':'in_progress'}]},
    {'operation':'replace','steps':[{'id':str(i),'title':'s','status':'pending'} for i in range(21)]},
])
def test_plan_rejects_invalid_and_oversized_inputs(tmp_path,args):
    runtime=NativeToolRuntime(tmp_path)
    try:
        with pytest.raises(ToolValidationError):runtime.validate('update_plan',args)
    finally:runtime.close()

def test_tool_center_never_upgrades_history_into_live_permission():
    payload=[{'event':'tool.dispatch.blocked','session_id':'s','timestamp':2,
             'data':{'kind':'mcp','tool':'mcp__kirocrew-core__learn_add',
                     'reason':'crew_binding_unverified','arguments':{'secret':'hidden'}}}]
    status=tool_center_report(['read','update_plan'],payload)
    assert status['read_only'] and status['live_permission_state'] is False
    assert status['recent_blocks'][0]['reason']=='crew_binding_unverified'
    assert 'hidden' not in str(status)
