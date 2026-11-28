"""Static ACP request routing table.

Only declared method names are dispatched; the table never selects dynamic
method names from client payloads. Control-plane priority and RPC framing
remain the responsibility of :mod:`acp_server`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# Keep the two special paths (prompt, restore) in AcpServer.handle; they
# have different lock / lifecycle requirements.
ACP_REQUEST_HANDLERS: Mapping[str, str] = MappingProxyType({
    'initialize': '_handle_initialize_request',
    'session/new': '_handle_session_new_request',
    'session/list': '_handle_session_list_request',
    '_session/steer': '_handle_session_steer_request',
    '_kiro/session/compact': '_handle_session_compact_request',
    'session/set_config_option': '_handle_session_set_config_option_request',
    'session/set_model': '_handle_session_set_model_request',
    'session/set_mode': '_handle_session_set_mode_request',
    'session/close': '_handle_session_close_request',
    '_kiro.dev/session/terminate': '_handle_session_close_request',
    'session/delete': '_handle_session_delete_request',
    '_kiro/session/delete': '_handle_session_delete_request',
    '_kiro/agent/list': '_handle_agent_list_request',
    '_kiro/agent/send': '_handle_agent_send_request',
    '_kiro/agent/cancel': '_handle_agent_cancel_request',
    '_kiro/agent/resume': '_handle_agent_resume_request',
    '_kiro/team/list': '_handle_team_list_request',
    '_kiro/team/get': '_handle_team_get_request',
    '_kiro/team/send': '_handle_team_send_request',
    '_kiro/team/spawn': '_handle_team_spawn_request',
    '_kiro/team/board': '_handle_team_board_request',
    '_kiro/team/recycle': '_handle_team_recycle_request',
    '_kiro/workspace/list': '_handle_workspace_list_request',
    '_kiro/workspace/get': '_handle_workspace_get_request',
    '_kiro/workspace/diff': '_handle_workspace_diff_request',
    '_kiro/workspace/check': '_handle_workspace_check_request',
    '_kiro/workspace/compare': '_handle_workspace_compare_request',
    '_kiro/workspace/apply': '_handle_workspace_apply_request',
    '_kiro/workspace/remove': '_handle_workspace_remove_request',
    '_kiro/acp_agents/list': '_handle_acp_agents_list_request',
    '_kiro/acp/conformance': '_handle_acp_conformance_request',
    '_kiro/run/list': '_handle_run_list_request',
    '_kiro/work/list': '_handle_work_list_request',
    '_kiro/work/bind': '_handle_work_bind_request',
    '_kiro/run/get': '_handle_run_get_request',
    '_kiro/run/resume': '_handle_run_resume_request',
    '_kiro/run/retry': '_handle_run_retry_request',
    '_kiro/run/replan': '_handle_run_replan_request',
})
