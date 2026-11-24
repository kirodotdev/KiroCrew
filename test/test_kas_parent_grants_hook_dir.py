# SPDX-License-Identifier: Apache-2.0
"""A founder's PreToolUse hook guards its grants only where the hook runs.

A spec's own hook runs in its own session's directory. A founder whose hook the run
carries hands its grants down only when every session above the run is known to run
in the run's own directory: elsewhere the same entry may not find what it runs, and
a hook that fails to start blocks nothing, so the grant would answer a call the
founder's hook would have stopped.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _BROAD,
    _PLAIN,
    _WIDE,
    _WRITE_COMMAND,
    _answer,
    _child,
    _isolated,
    _own,
    _pre,
    agents,
    one_directory,
    preview,
    sessions,
    stored,
)
from test_kas_parent_limits import _judge, _shell, _stamped, _write

from kiro_crew.acp.kas_permissions import CREW_ANSWERED_BUILTINS_KEY
from kiro_crew.hooks import TOOL_AUTO_APPROVE

_RELATIVE_GUARD = {"matcher": "execute_bash", "command": "./guard.sh"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hooked, where, hands_down",
    [
        (True, "the-same", True),
        (True, "another", False),
        (True, "unknown", False),
        (False, "another", True),
        (False, "unknown", True),
    ],
    ids=[
        "hook-same-directory",
        "hook-another-directory",
        "hook-run-directory-unknown",
        "no-hook-another-directory",
        "no-hook-run-directory-unknown",
    ],
)
async def test_a_founders_hooked_grants_reach_a_run_only_in_the_founders_directory(
    agents, sessions, preview, one_directory, tmp_path, hooked, where, hands_down  # noqa: F811
):
    hooks = _pre(_RELATIVE_GUARD) if hooked else {}
    _write(agents, {**_WIDE, "name": "guarded", "hooks": hooks})
    _write(agents, {**_PLAIN, "name": "carries", "hooks": hooks})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_dir: Path | None = {"the-same": one_directory, "another": elsewhere, "unknown": None}[where]

    batch = await _child(sessions, "guarded", "carries", work_dir=run_dir)

    assert (CREW_ANSWERED_BUILTINS_KEY in _own(batch, "carries")) is hands_down
    assert (_answer(batch, "carries").action == TOOL_AUTO_APPROVE) is hands_down


@pytest.mark.asyncio
async def test_the_hook_a_run_carries_runs_on_the_runs_own_call_and_stops_it(
    agents, sessions, preview, tmp_path, monkeypatch, one_directory  # noqa: F811
):
    """Why the grants may travel: at the hook runner, the run's turn reads the hook
    from its own spec, and its permission request for a call the founder's grant
    would answer is refused by that hook, a real script that exits 2, on the floor
    the sub-agent ladder asks before any grant."""
    import sys

    import kiro_crew.hooks as hooks_mod
    from kiro_crew import tool_permission
    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    script = tmp_path / "deny.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    entry = {"matcher": "execute_bash", "command": f'"{sys.executable}" "{script}"'}
    _write(agents, {**_BROAD, "name": "guarded", "hooks": _pre(entry)})
    _write(agents, {**_PLAIN, "name": "carries", "hooks": _pre(entry)})

    batch = await _child(sessions, "guarded", "carries", work_dir=one_directory)
    request = _stamped(_shell(_WRITE_COMMAND), batch, "carries")
    assert _judge(request).action == TOOL_AUTO_APPROVE

    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    turn = await spec_hooks.turn_spec_hooks(provider, "carries")
    assert turn.gated and not turn.unreadable
    assert [(hook.event, hook.matcher) for hook in turn.hooks] == [("PreToolUse", "execute_bash")]
    floor = tool_permission.SpecHooks(
        turn,
        store=lambda: hooks_mod.ScriptHookStore(tmp_path),
        pre_tool=hooks_mod.permission_pre_tool_block,
    )
    ask = tool_permission.Ask(request, SimpleNamespace(client=None), "subagent:c1", "carries")
    refusal = await floor.refuse(ask)
    assert refusal is not None and refusal.rung == "spec_hook", refusal
    # The control: the same hook letting the call through refuses nothing.
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert await floor.refuse(ask) is None

    # A run that lacks the hook has nothing to stop the call, and is handed no grant.
    lacking = await _child(sessions, "guarded", "plain", work_dir=one_directory)
    assert (await spec_hooks.turn_spec_hooks(provider, "plain")).hooks == []
    assert _answer(lacking, "plain").action != TOOL_AUTO_APPROVE
