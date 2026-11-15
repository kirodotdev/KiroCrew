"""The deny reason ``permission_pre_tool_block`` returns for an exit-2 hook.

Subagent, task runner, cron and channel permission requests read the reason
this gate returns, so a hook's recovery instruction reaches the model through
it whole, up to ``HOOK_DENY_REASON_MAX_CHARS``.
"""

from __future__ import annotations

import asyncio

import kiro_crew.hooks as hooks_mod
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
)

#: ``HOOK_DENY_REASON_MAX_CHARS``, written out so these tests still collect, and
#: fail on their assertions, when the limit is missing.
_DENY_REASON_LIMIT = 2000


def _gate_reason_for_stderr(tmp_path, monkeypatch, stderr: str) -> str | None:
    """The gate's block reason when a spec hook exits 2 with *stderr*."""

    async def fake_run(hook, context="", hook_event=None, cwd=None):
        return ScriptHookResult(
            hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=2, stderr=stderr
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    spec_hook = ScriptHook(
        id="spec:a1:PreToolUse:0",
        event=HOOK_EVENT_PRE_TOOL_USE,
        matcher="Read*",
        command="g",
        spec_origin="user",
    )
    store = ScriptHookStore(config_dir=tmp_path)
    return asyncio.run(
        hooks_mod.permission_pre_tool_block(store, [spec_hook], "/w", "Running: ReadFile")
    )


def test_a_multi_sentence_recovery_instruction_reaches_the_gate_whole(tmp_path, monkeypatch):
    stderr = "Write the plan file before editing. " + "x" * 450 + " END"
    reason = _gate_reason_for_stderr(tmp_path, monkeypatch, stderr)
    assert reason is not None and reason.endswith(stderr)


def test_a_deny_reason_past_the_limit_reaches_the_gate_cut_at_its_head(tmp_path, monkeypatch):
    head = "a" * _DENY_REASON_LIMIT
    reason = _gate_reason_for_stderr(tmp_path, monkeypatch, head + "TAIL-PAST-THE-LIMIT")
    assert reason is not None and reason.endswith(head)
