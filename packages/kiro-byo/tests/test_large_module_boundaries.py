"""R5: extracted types and upstream source-contract checks preserve old public paths."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import inspect

from kiro_byo import acp_server, acp_session_state, turn_runner, turn_loop_types
from kiro_byo import integration_manager, crew_contract_checks


def test_session_is_one_type_across_acp_import_boundaries():
    assert acp_server.Session is acp_session_state.Session
    assert acp_server.PendingClientRequest is acp_session_state.PendingClientRequest
    assert "agent" in {f.name for f in fields(acp_server.Session)}
    assert "host_work_link" in {f.name for f in fields(acp_server.Session)}
    assert "turn_lock" in {f.name for f in fields(acp_server.Session)}


def test_session_runtime_mutables_are_never_shared():
    first = acp_server.Session("first", ".", "m")
    second = acp_server.Session("second", ".", "m")
    first.host_work_link["task_id"] = "first-task"
    first.messages.append({"role": "user", "content": "x"})
    first.steer_queue.append("message")
    assert second.host_work_link == {}
    assert second.messages == []
    assert list(second.steer_queue) == []
    assert first.turn_lock is not second.turn_lock
    assert first.cancel_event is not second.cancel_event


def test_turn_loop_directives_and_mutable_state_keep_identity():
    assert turn_runner._ProviderLoopState is turn_loop_types._ProviderLoopState
    assert turn_runner._LoopDirective is turn_loop_types._LoopDirective
    assert turn_runner.DurableCheckpointError is turn_loop_types.DurableCheckpointError
    assert turn_runner._ToolBatchOutcome is turn_loop_types._ToolBatchOutcome
    a = turn_runner._ProviderLoopState(None, None, [], None, "", "", "r1", "t1")
    b = turn_runner._ProviderLoopState(None, None, [], None, "", "", "r2", "t2")
    a.execution_guard_halted = True
    a.round_text_parts.append("stream")
    assert not b.execution_guard_halted
    assert b.round_text_parts == []
    assert a.execution_guard is not b.execution_guard


def test_crew_contract_checks_are_shared_not_duplicated(tmp_path: Path):
    assert integration_manager.compatibility_contract_issues is crew_contract_checks.compatibility_contract_issues
    assert integration_manager._top_level_bound_names is crew_contract_checks._top_level_bound_names
    assert integration_manager._normalized_text is crew_contract_checks._normalized_text
    assert integration_manager._PREFIX is crew_contract_checks._PREFIX
    file = tmp_path / "module.py"
    # newline="" stops the platform text layer from re-translating \n to \r\n
    # on Windows (which would turn the intended \r\n into \r\r\n on disk).
    file.write_text("def probe():\r\n    return True\r\n", encoding="utf-8", newline="")
    assert integration_manager._normalized_text(file) == "def probe():\n    return True\n"
    assert "probe" in integration_manager._top_level_bound_names(file.read_text())


def test_crew_contract_checks_fail_closed_on_missing_upstream_source(tmp_path: Path):
    compat = {"required_contract_symbols": {"src/kiro_crew/missing.py": ["expected"]}}
    issues = integration_manager.compatibility_contract_issues(compat, tmp_path)
    assert issues and any("missing" in str(issue) for issue in issues)
    bad_root = {"required_contract_symbols": {"../outside.py": ["expected"]}}
    assert integration_manager.compatibility_contract_issues(bad_root, tmp_path)


def test_usage_aggregation_across_lead_child_and_review():
    """All execution roles must contribute to one accurate final token ledger."""
    from kiro_byo.providers.base import ChatTurn, UsageInfo

    state = turn_loop_types._ProviderLoopState(None, None, [], None, "", "", "run", "trace")
    turn_runner.TurnRunnerMixin._apply_provider_round_usage(
        state,
        ChatTurn(
            text="", served_model="served",
            usage=UsageInfo(
                input_tokens=11, output_tokens=7, cached_tokens=2,
                cache_write_tokens=4, reasoning_tokens=3,
                cached_tokens_reported=True, cache_write_tokens_reported=True,
            ),
        ),
        lead=True,
    )
    assert state.served_model_id == "served"
    state.add_usage({
        "input_tokens": 5, "output_tokens": 3, "cached_tokens": 1,
        "cache_write_tokens": 9, "reasoning_tokens": 2,
        "reasoning_tokens_reported": True,
    })
    # A review with missing fields must not clear evidence from earlier turns.
    state.add_usage(UsageInfo(input_tokens=2, output_tokens=1))
    assert (state.input_tokens, state.output_tokens) == (18, 11)
    assert (state.cached_tokens, state.cache_write_tokens, state.reasoning_tokens) == (3, 13, 5)
    assert (state.cache_usage_reported, state.cache_write_usage_reported, state.reasoning_usage_reported) == (True, True, True)
    assert state.served_model_id == "served"
