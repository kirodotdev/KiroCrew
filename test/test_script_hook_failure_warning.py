"""A failed hook leaves exactly one WARNING, whichever surface fires it.

``ScriptHookStore.fire`` logs it (``Hook <name> (<event>) failed: <detail>``), so a
caller that only reads exit-0 stdout -- a subagent's Stop, a channel turn's Stop --
cannot leave a failure at INFO, and the callers that read the result (the dashboard
turn, a channel turn's prompt hooks) do not warn a second time. The SessionLaneChanged
runner, which skips ``fire``, logs the same line. Real store, real hook subprocesses:
nothing here mocks ``fire`` or ``run_script_hook``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.hooks as hooks_mod
from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookStore,
)

_HOOK = "failing-hook"

pytestmark = pytest.mark.xdist_group(name="script_hook_failure_warning")


@pytest.fixture(autouse=True)
def _passthrough_sandbox(_floor_monkeypatch):
    # These tests exercise the log line, not host sandbox discovery: a host without
    # a sandbox backend (Windows CI) otherwise fails closed with exit -1.
    _floor_monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))


@pytest.fixture(autouse=True)
def _no_slow_fire_warning(_floor_monkeypatch):
    # fire's slow-fire WARNING names the hook too, and fires on wall time alone (a loaded
    # runner); off here, so the counts below see every other WARNING and only those.
    _floor_monkeypatch.setattr("kiro_crew.hook_fire_timing.SLOW_FIRE_WARN_MS", float("inf"))


def _command(tmp_path: Path, exit_code: int, stderr: str = "") -> str:
    """A hook command that runs the same under ``/bin/sh`` and ``cmd /c``."""
    script = tmp_path / f"hook_{exit_code}.py"
    script.write_text(
        f"import sys\nsys.stderr.write({stderr!r})\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _store(tmp_path: Path, event: str, command: str) -> ScriptHookStore:
    store = ScriptHookStore(tmp_path / "hooks")
    store.create({"name": _HOOK, "event": event, "command": command, "enabled": True})
    return store


def _hook_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and _HOOK in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_a_failed_stop_hook_leaves_one_warning_naming_it(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    store = _store(tmp_path, HOOK_EVENT_STOP, _command(tmp_path, 3))
    results = await store.fire(HOOK_EVENT_STOP, "reply")
    assert [r.exit_code for r in results] == [3]
    assert _hook_warnings(caplog) == [f"Hook {_HOOK} (Stop) failed: exited with code 3"]


@pytest.mark.asyncio
async def test_the_warning_carries_the_stderr_tail(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    store = _store(tmp_path, HOOK_EVENT_STOP, _command(tmp_path, 1, "boom-diagnosis"))
    await store.fire(HOOK_EVENT_STOP, "reply")
    (warning,) = _hook_warnings(caplog)
    assert warning.startswith(f"Hook {_HOOK} (Stop) failed: ")
    assert warning.rstrip().endswith("boom-diagnosis")


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 2], ids=["exit0", "exit2"])
async def test_exit_0_and_exit_2_leave_no_warning(tmp_path, caplog, exit_code):
    caplog.set_level(logging.INFO)
    store = _store(tmp_path, HOOK_EVENT_STOP, _command(tmp_path, exit_code, "deny"))
    results = await store.fire(HOOK_EVENT_STOP, "reply")
    assert [r.exit_code for r in results] == [exit_code]
    assert _hook_warnings(caplog) == []
    assert any(_HOOK in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)


@pytest.mark.asyncio
async def test_a_channel_prompt_hook_failure_is_warned_once(tmp_path, caplog, monkeypatch):
    from kiro_crew.messaging.script_hooks import ScriptHookTurn, fire_prompt_submit

    caplog.set_level(logging.INFO)
    command = _command(tmp_path, 1, "boom-diagnosis")
    store = _store(tmp_path, HOOK_EVENT_USER_PROMPT_SUBMIT, command)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", store)
    injected = await fire_prompt_submit(ScriptHookTurn("sk", "prompt"))
    assert injected == []
    assert _hook_warnings(caplog) == [f"Hook {_HOOK} (UserPromptSubmit) failed: boom-diagnosis"]


@pytest.mark.asyncio
async def test_a_dashboard_stop_hook_failure_is_warned_once(tmp_path, caplog):
    import kiro_crew.dashboard.chat_runner  # noqa: F401 - binds the names _fire reads
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire

    caplog.set_level(logging.INFO)
    state = SimpleNamespace(
        _hook_store=_store(tmp_path, HOOK_EVENT_STOP, _command(tmp_path, 1, "boom-diagnosis")),
        broadcast_ws=lambda *_a, **_k: None,
    )
    turn = SimpleNamespace(
        session_key="sk",
        slot=SimpleNamespace(key="slot"),
        state=state,
        _spec_hooks_unreadable=False,
        _spec_hooks=(),
        _spec_hooks_cwd=None,
        client=None,
    )
    assert await _fire(turn, HOOK_EVENT_STOP, "reply") == []
    assert _hook_warnings(caplog) == [f"Hook {_HOOK} (Stop) failed: boom-diagnosis"]


@pytest.mark.asyncio
async def test_a_failed_lane_hook_leaves_one_warning(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    store = _store(tmp_path, HOOK_EVENT_SESSION_LANE_CHANGED, _command(tmp_path, 3))
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", store)
    (hook,) = store.list_all()
    await hooks_mod._run_one_lane_hook(hook, "added:done;", {"hook_event_name": hook.event})
    assert hook.last_status == "error"
    assert _hook_warnings(caplog) == [
        f"Hook {_HOOK} (SessionLaneChanged) failed: exited with code 3"
    ]
