"""A hook Crew refused to run says so, instead of reading as a bare exit 2.

``run_script_hook`` refuses a command the shell deny set or the governance gate
rejects, and returns exit 2 so a PreToolUse gate still blocks. The block reason
the log, the model's notice and the activity row carry must name that refusal
and its rule, and must not echo the hook command, which can hold paths or tokens.
A hook that really ran and exited 2 keeps its own stderr as the reason.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import kiro_crew.hooks as hooks_mod
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
    run_script_hook,
)

_REFUSAL = "Blocked by security policy: ^git\\s+push\nan operator note"


def _result(**kw) -> ScriptHookResult:
    return ScriptHookResult(hook_id="h", hook_name="telemetry", event=HOOK_EVENT_PRE_TOOL_USE, **kw)


def test_a_policy_refusal_names_itself_and_its_rule():
    r = _result(exit_code=2, error=_REFUSAL)
    assert r.refused_by_policy
    detail = r.block_detail("exit 2")
    assert (
        detail
        == "refused by Kiro Crew policy, hook not run: Blocked by security policy: ^git\\s+push"
    )
    assert "operator note" not in detail


def test_a_hook_that_ran_and_exited_2_keeps_its_own_reason():
    ran = _result(exit_code=2, stderr="not on a weekend")
    assert not ran.refused_by_policy
    assert ran.block_detail("exit 2") == "not on a weekend"
    silent = _result(exit_code=2)
    assert not silent.refused_by_policy
    assert silent.block_detail("exit 2") == "exit 2"


def test_a_timeout_is_not_read_as_a_policy_refusal():
    assert not _result(exit_code=-1, error="Timed out after 5s").refused_by_policy


@pytest.mark.asyncio
async def test_a_denylisted_hook_reports_the_rule_not_the_command(tmp_path: Path):
    marker = tmp_path / "secret-looking-path"
    hook = ScriptHook(
        id="deny-1",
        name="telemetry",
        event=HOOK_EVENT_PRE_TOOL_USE,
        command=f"cat {marker} >/dev/null && git push origin main",
        timeout=30,
        enabled=True,
    )
    result = await run_script_hook(hook, "ctx")
    assert result.refused_by_policy
    detail = result.block_detail("exit 2")
    assert detail.startswith(
        "refused by Kiro Crew policy, hook not run: Blocked by security policy:"
    )
    assert str(marker) not in detail


def _gate(tmp_path, monkeypatch, result: ScriptHookResult) -> str | None:
    async def fake_run(hook, context="", hook_event=None, cwd=None):
        return result

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    spec = ScriptHook(
        id="spec:a1:PreToolUse:0", event=HOOK_EVENT_PRE_TOOL_USE, matcher="*", command="g"
    )
    store = ScriptHookStore(config_dir=tmp_path)
    return asyncio.run(hooks_mod.permission_pre_tool_block(store, [spec], None, "ReadFile"))


def test_the_permission_gate_carries_the_refusal_into_its_reason(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.hook_runtime.hook_dispatch"):
        reason = _gate(tmp_path, monkeypatch, _result(exit_code=2, error=_REFUSAL))
    assert reason is not None
    assert "refused by Kiro Crew policy, hook not run" in reason
    assert "Blocked by security policy: ^git\\s+push" in reason
    assert any("refused by Kiro Crew policy" in rec.getMessage() for rec in caplog.records)


def test_the_permission_gate_keeps_a_real_exit_2_reason(tmp_path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, _result(exit_code=2)) == "telemetry: hook denied"
    reason = _gate(tmp_path, monkeypatch, _result(exit_code=2, stderr="nope"))
    assert reason == "telemetry: nope"


def _turn_fire(result: ScriptHookResult, caplog) -> list[str]:
    from types import SimpleNamespace

    # The chat runner composes its owners: ``_fire`` runs on the runner's globals
    # and logs through the runner's logger.
    from kiro_crew.dashboard import chat_runner

    class _Store:
        async def fire(self, *a, **kw):
            return [result]

    turn = SimpleNamespace(
        session_key="dashboard:1",
        slot=SimpleNamespace(key="1"),
        state=SimpleNamespace(_hook_store=_Store(), broadcast_ws=lambda *a, **k: None),
        client=SimpleNamespace(session_id="s"),
        _spec_hooks=[],
        _spec_hooks_unreadable=False,
        _spec_hooks_cwd=None,
    )
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
        return asyncio.run(chat_runner._fire(turn, HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile"))


def test_the_chat_turn_logs_and_injects_the_refusal(caplog):
    injected = _turn_fire(_result(exit_code=2, error=_REFUSAL), caplog)
    assert injected == [
        "BLOCKED:telemetry:refused by Kiro Crew policy, hook not run: "
        "Blocked by security policy: ^git\\s+push"
    ]
    assert any("refused by Kiro Crew policy" in rec.getMessage() for rec in caplog.records)


def test_the_chat_turn_keeps_a_real_exit_2_unchanged(caplog):
    injected = _turn_fire(_result(exit_code=2), caplog)
    assert injected == ["BLOCKED:telemetry:hook denied"]
    assert any(rec.getMessage() == "Hook telemetry blocked tool: exit 2" for rec in caplog.records)


def test_a_hook_result_that_is_not_a_script_hook_result_still_reads():
    from types import SimpleNamespace

    from kiro_crew.hooks import hook_block_detail

    ran = SimpleNamespace(exit_code=2, stdout="", stderr="blocked by rule X", error="")
    assert hook_block_detail(ran, "hook denied") == "blocked by rule X"
    refused = SimpleNamespace(exit_code=2, stdout="", stderr="", error="Blocked: rule Y\nmore")
    assert hook_block_detail(refused, "exit 2").startswith("refused by Kiro Crew policy")
    assert hook_block_detail(SimpleNamespace(exit_code=2), "exit 2") == "exit 2"
