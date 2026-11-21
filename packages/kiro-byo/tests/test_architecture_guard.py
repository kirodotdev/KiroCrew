"""Prevent silent growth of legacy monoliths while behavior is being preserved."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_ratchet_keeps_hotspots_bounded() -> None:
    report = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "architecture_guard.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert report.returncode == 0, report.stdout + "\n" + report.stderr
    assert "architecture budget: PASS" in report.stdout


def test_overall_bloat_and_host_modification_budget_are_bounded() -> None:
    budget = json.loads((ROOT / "config" / "architecture_budget.json").read_text(encoding="utf-8"))
    # 0.4 close-out: +100 net lines are the required cancel/steer rollback,
    # MCP race, and sandbox-escape safety fixes plus the restored native_tools
    # re-export, not bloat. Real slimming is a dedicated later round.
    # +11 more: retired-seam inspect fix (a retired file with no transform rule
    # and no BYO trace now reads as clean official source, not foreign/partial).
    assert budget["core_python_lines"] <= 46466
    assert budget["crew_host_modified_files"] <= 20


def test_architecture_budget_explicitly_targets_core_execution_and_acp() -> None:
    budget = json.loads((ROOT / "config" / "architecture_budget.json").read_text(encoding="utf-8"))
    assert budget["functions_over_100_lines"] <= 54
    assert budget["functions_over_200_lines"] <= 13
    assert "src/kiro_byo/turn_runner.py" in budget["hotspot_file_lines"]
    assert "src/kiro_byo/acp_server.py" in budget["hotspot_file_lines"]
    assert budget["hotspot_function_lines"]["src/kiro_byo/integration_manager.py::install_from_app"] <= 198


def test_v4_prompt_route_boundary_is_a_separately_bounded_module() -> None:
    budget = json.loads((ROOT / "config" / "architecture_budget.json").read_text(encoding="utf-8"))
    assert budget["hotspot_file_lines"]["src/kiro_byo/prompt_routing.py"] <= 240
    assert budget["hotspot_function_lines"]["src/kiro_byo/turn_runner.py::_handle_prompt_request"] <= 92
    assert budget["hotspot_function_lines"]["src/kiro_byo/turn_runner.py::_execute_prompt_request_locked"] <= 162


def test_execution_seams_are_inherited_not_copied_back_into_controllers() -> None:
    from kiro_byo.acp_server import AcpServer, SessionBusyError
    from kiro_byo.errors import SessionBusyError as SharedSessionBusyError
    from kiro_byo.session_store import SessionStoreMixin
    from kiro_byo.subagent_execution import SubagentExecutionMixin
    from kiro_byo.turn_runner import TurnRunnerMixin

    assert AcpServer._persist_session is SessionStoreMixin._persist_session
    assert AcpServer._load_persisted_record is SessionStoreMixin._load_persisted_record
    assert TurnRunnerMixin._run_isolated_subagent is SubagentExecutionMixin._run_isolated_subagent
    assert TurnRunnerMixin._resume_agent_task is SubagentExecutionMixin._resume_agent_task
    assert SessionBusyError is SharedSessionBusyError


def test_architecture_budget_is_versioned_without_exposing_private_config() -> None:
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
    assert 'config/*.json' in ignore
    assert '!config/architecture_budget.json' in ignore
    assert (ROOT / 'config/architecture_budget.json').is_file()


def test_stage3_acp_lifecycle_and_recovery_are_real_inherited_handlers() -> None:
    """Dispatch and release audit must follow declared seams, not stale text."""
    from kiro_byo.acp_server import AcpServer
    from kiro_byo.run_recovery import RunRecoveryMixin
    from kiro_byo.session_lifecycle import SessionLifecycleMixin
    from scripts.acp_capability_audit import _dispatch_names

    assert AcpServer._put_session is SessionLifecycleMixin._put_session
    assert AcpServer._signal_cancel is SessionLifecycleMixin._signal_cancel
    assert AcpServer._handle_session_delete_request is SessionLifecycleMixin._handle_session_delete_request
    assert AcpServer._handle_run_resume_request is RunRecoveryMixin._handle_run_resume_request
    assert AcpServer._run_recovery_payload is RunRecoveryMixin._run_recovery_payload
    assert {
        'session/close', 'session/delete', '_kiro/run/resume', '_kiro/run/retry',
        '_kiro/run/replan', '_kiro/work/list',
    } <= _dispatch_names(ROOT / 'src/kiro_byo/acp_server.py')

    budget = json.loads((ROOT / 'config/architecture_budget.json').read_text(encoding='utf-8'))
    for module in ('session_lifecycle', 'run_recovery'):
        assert f'src/kiro_byo/{module}.py' in budget['hotspot_file_lines']


def test_stage4_tool_bridge_is_inherited_without_second_permission_path() -> None:
    """One ACP request boundary, not two competing consent implementations."""
    from kiro_byo.acp_server import AcpServer
    from kiro_byo.acp_tool_bridge import AcpToolBridgeMixin
    from kiro_byo.acp_session_state import Session

    for name in (
        '_tool_call_payload', '_emit_tool_call', '_emit_tool_status',
        '_permission_consent_meta', '_request_tool_permission_choice',
        '_request_tool_permission',
    ):
        assert getattr(AcpServer, name) is getattr(AcpToolBridgeMixin, name)

    server = object.__new__(AcpServer)
    session = Session('s', '.', 'model')
    server._emit_tool_call = lambda *_a, **_k: {'toolCallId': 'call-1'}
    responses = [
        {'outcome': {'outcome': 'cancelled', 'optionId': 'allow_always'}},
        {'outcome': {'outcome': 'selected', 'optionId': 'reject_once'}},
        {'outcome': {'outcome': 'selected', 'optionId': 'allow_once'}},
    ]
    server._request_client = lambda *_a, **_k: responses.pop(0)
    # Tool identity/consent metadata alone cannot authorize execution.
    assert server._request_tool_permission_choice(session, 'call-1', 'lookup', {}) == ''
    assert server._request_tool_permission_choice(session, 'call-1', 'lookup', {}) == ''
    assert server._request_tool_permission_choice(session, 'call-1', 'lookup', {}) == 'allow_once'
    assert not responses

    budget = json.loads((ROOT / 'config/architecture_budget.json').read_text(encoding='utf-8'))
    assert budget['hotspot_file_lines']['src/kiro_byo/acp_server.py'] <= 2741
    assert budget['hotspot_file_lines']['src/kiro_byo/acp_tool_bridge.py'] <= 211


def test_stage5_tool_batch_and_terminal_projection_have_single_owners() -> None:
    """New boundaries cannot silently regrow inside the turn coordinator."""
    from kiro_byo.turn_runner import TurnRunnerMixin
    from kiro_byo.tool_batch_execution import ToolBatchExecutionMixin
    from kiro_byo.provider_loop_terminal import ProviderLoopTerminalMixin
    from scripts.boundary_guard import check

    assert TurnRunnerMixin._execute_tool_batch_impl is ToolBatchExecutionMixin._execute_tool_batch_impl
    assert TurnRunnerMixin._observe_tool_execution_guard is ToolBatchExecutionMixin._observe_tool_execution_guard
    assert TurnRunnerMixin._record_provider_turn_telemetry is ProviderLoopTerminalMixin._record_provider_turn_telemetry
    assert '_execute_tool_batch_impl' not in TurnRunnerMixin.__dict__
    assert '_record_provider_turn_telemetry' not in TurnRunnerMixin.__dict__
    assert TurnRunnerMixin._finish_interrupted_provider_round is ProviderLoopTerminalMixin._finish_interrupted_provider_round
    assert '_finish_interrupted_provider_round' not in TurnRunnerMixin.__dict__
    assert check(ROOT) == []

    budget = json.loads((ROOT / 'config/architecture_budget.json').read_text(encoding='utf-8'))
    assert budget['hotspot_file_lines']['src/kiro_byo/turn_runner.py'] <= 3218
    assert budget['hotspot_file_lines']['src/kiro_byo/tool_batch_execution.py'] <= 281
    # Interruption finalization was moved from the controller into the existing
    # terminal owner. Bound the combined footprint, not the old file split.
    assert budget['hotspot_file_lines']['src/kiro_byo/provider_loop_terminal.py'] <= 289
    assert (budget['hotspot_file_lines']['src/kiro_byo/turn_runner.py']
            + budget['hotspot_file_lines']['src/kiro_byo/provider_loop_terminal.py']) <= 3488
