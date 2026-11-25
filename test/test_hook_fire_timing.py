"""A slow script-hook fire logs one warning naming its phases; a fast one logs nothing.

Each slow step advances a fake clock from inside itself, so host load cannot change the result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import hook_fire_timing
from kiro_crew.hook_fire_timing import FirePhase
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHookStore

_SLOW_SECS = 2.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _slow_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.getMessage().startswith("Slow hook fire")
    ]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(hook_fire_timing, "time", SimpleNamespace(perf_counter=fake))
    return fake


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScriptHookStore:
    # Run the hook unsandboxed so hosts without a sandbox still reach its exit code.
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))
    hook_store = ScriptHookStore(tmp_path)
    hook_store.create({"name": "guard", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "exit 0"})
    return hook_store


def _slow_capability(monkeypatch, clock, store):
    import kiro_crew.hooks as hooks_mod

    def slow(_session_key):
        clock.now += _SLOW_SECS
        return None

    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", slow)


def _slow_hook_policy(monkeypatch, clock, store):
    def slow(_session_key, _hook):
        clock.now += _SLOW_SECS
        return None

    monkeypatch.setattr("kiro_crew.hook_event_governance.hook_refusal", slow)


def _slow_deny_list(monkeypatch, clock, store):
    import kiro_crew.hooks as hooks_mod

    def slow():
        clock.now += _SLOW_SECS
        return []

    monkeypatch.setattr(hooks_mod, "effective_denied_regexes_from_config", slow)


def _slow_prepare(monkeypatch, clock, store):
    def slow(argv, **kwargs):
        clock.now += _SLOW_SECS
        return list(argv), kwargs.get("env"), None

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", slow)


def _slow_persist(monkeypatch, clock, store):
    real = store._persist_current

    def slow():
        clock.now += _SLOW_SECS
        real()

    monkeypatch.setattr(store, "_persist_current", slow)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase, make_slow",
    [
        ("gov_capability", _slow_capability),
        ("gov_hook", _slow_hook_policy),
        ("deny_list", _slow_deny_list),
        ("prepare", _slow_prepare),
        ("persist", _slow_persist),
    ],
)
async def test_a_slow_phase_logs_one_warning_naming_it(
    phase, make_slow, clock, store, monkeypatch, caplog
):
    make_slow(monkeypatch, clock, store)
    caplog.set_level(logging.INFO)

    results = await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert [r.exit_code for r in results] == [0]
    (record,) = _slow_warnings(caplog)
    assert f"{phase}=2000ms (wait 0, run 2000, resume 0)" in record.getMessage()


@pytest.mark.asyncio
async def test_a_fast_fire_logs_no_warning(clock, store, caplog):
    caplog.set_level(logging.INFO)

    results = await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert [r.exit_code for r in results] == [0]
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "kiro_crew.hooks"
    ]


def test_a_hop_splits_pool_wait_from_work_and_loop_resume():
    phase = FirePhase("gov_hook")
    phase.started = 10.0
    phase.worker_started = 13.0
    phase.worker_ended = 13.05
    phase.ended = 13.1

    assert phase.describe(now=99.0) == "gov_hook=3100ms (wait 3000, run 50, resume 50)"


@pytest.mark.asyncio
@pytest.mark.parametrize("secs, warnings", [(1.0, 1), (0.999, 0)])
async def test_the_threshold_is_inclusive(secs, warnings, clock, store, monkeypatch, caplog):
    def slow(_session_key, _hook):
        clock.now += secs
        return None

    monkeypatch.setattr("kiro_crew.hook_event_governance.hook_refusal", slow)
    caplog.set_level(logging.INFO)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert len(_slow_warnings(caplog)) == warnings


@pytest.mark.asyncio
async def test_a_slow_matcher_that_does_not_match_still_logs(clock, tmp_path, monkeypatch, caplog):
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT

    def slow_no_match(*_args):
        clock.now += _SLOW_SECS
        return False

    monkeypatch.setattr(hooks_mod, "_context_matches", slow_no_match)
    hook_store = ScriptHookStore(tmp_path)
    hook_store.create(
        {
            "name": "prompt-guard",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "command": "exit 0",
            "matcher": "never",
        }
    )
    caplog.set_level(logging.INFO)

    assert await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "a prompt") == []
    (record,) = _slow_warnings(caplog)
    assert "matcher=2000ms (wait 0, run 2000, resume 0)" in record.getMessage()


@pytest.mark.asyncio
async def test_a_fire_leaves_the_gc_callbacks_unchanged(store):
    import gc

    before = list(gc.callbacks)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert gc.callbacks == before
    assert not [
        cb for cb in gc.callbacks if getattr(cb, "__module__", "") == hook_fire_timing.__name__
    ]


@pytest.mark.asyncio
async def test_a_slow_fire_cancelled_mid_hook_still_logs(clock, store, monkeypatch, caplog):
    import asyncio
    import threading

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def slow(_session_key, _hook):
        clock.now += _SLOW_SECS
        loop.call_soon_threadsafe(started.set)
        release.wait(30)
        return None

    monkeypatch.setattr("kiro_crew.hook_event_governance.hook_refusal", slow)
    caplog.set_level(logging.INFO)
    fire = asyncio.ensure_future(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write"))
    try:
        await asyncio.wait_for(started.wait(), 30)
        fire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fire
    finally:
        release.set()

    (record,) = _slow_warnings(caplog)
    assert "gov_hook=2000ms" in record.getMessage()


def _event_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.getMessage().startswith("Slow hook event")
    ]


def _add_guards(hook_store: ScriptHookStore, count: int) -> None:
    for n in range(2, count + 2):
        hook_store.create(
            {"name": f"guard-{n}", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "exit 0"}
        )


def _each_hook_takes(monkeypatch: pytest.MonkeyPatch, clock: _Clock, secs: float) -> None:
    def slow(_session_key, _hook):
        clock.now += secs
        return None

    monkeypatch.setattr("kiro_crew.hook_event_governance.hook_refusal", slow)


@pytest.mark.asyncio
async def test_hooks_each_under_the_threshold_log_one_event_warning(
    clock, store, monkeypatch, caplog
):
    _add_guards(store, 2)
    _each_hook_takes(monkeypatch, clock, 0.5)
    caplog.set_level(logging.INFO)

    results = await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert [r.exit_code for r in results] == [0, 0, 0]
    assert _slow_warnings(caplog) == []
    (record,) = _event_warnings(caplog)
    message = record.getMessage()
    assert f"Slow hook event: {HOOK_EVENT_PRE_TOOL_USE} took 1500ms; hooks=3: " in message
    assert "guard=500ms, guard-2=500ms, guard-3=500ms; other=0ms" in message


@pytest.mark.asyncio
async def test_a_fast_event_logs_no_event_warning(clock, store, monkeypatch, caplog):
    _add_guards(store, 2)
    _each_hook_takes(monkeypatch, clock, 0.3)
    caplog.set_level(logging.INFO)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert _event_warnings(caplog) == []
    assert _slow_warnings(caplog) == []


@pytest.mark.asyncio
async def test_one_slow_hook_logs_both_the_hook_and_the_event_line(
    clock, store, monkeypatch, caplog
):
    _each_hook_takes(monkeypatch, clock, _SLOW_SECS)
    caplog.set_level(logging.INFO)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    (hook_line,) = _slow_warnings(caplog)
    assert "gov_hook=2000ms" in hook_line.getMessage()
    (event_line,) = _event_warnings(caplog)
    assert "took 2000ms; hooks=1: guard=2000ms; other=0ms" in event_line.getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("secs_per_hook, warnings", [(0.5, 1), (0.4995, 0)])
async def test_the_event_threshold_is_inclusive(
    secs_per_hook, warnings, clock, store, monkeypatch, caplog
):
    _add_guards(store, 1)
    _each_hook_takes(monkeypatch, clock, secs_per_hook)
    caplog.set_level(logging.INFO)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    assert len(_event_warnings(caplog)) == warnings
    assert _slow_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_slow_status_write_counts_as_time_outside_the_hooks(
    clock, store, monkeypatch, caplog
):
    _slow_persist(monkeypatch, clock, store)
    caplog.set_level(logging.INFO)

    await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")

    (event_line,) = _event_warnings(caplog)
    assert "took 2000ms; hooks=1: guard=0ms; other=2000ms" in event_line.getMessage()


@pytest.mark.asyncio
async def test_an_event_whose_hook_did_not_match_logs_no_event_line(
    clock, tmp_path, monkeypatch, caplog
):
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT

    def slow_no_match(*_args):
        clock.now += _SLOW_SECS
        return False

    monkeypatch.setattr(hooks_mod, "_context_matches", slow_no_match)
    hook_store = ScriptHookStore(tmp_path)
    hook_store.create(
        {
            "name": "prompt-guard",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "command": "exit 0",
            "matcher": "never",
        }
    )
    caplog.set_level(logging.INFO)

    await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "a prompt")

    assert len(_slow_warnings(caplog)) == 1
    assert _event_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_slow_event_cancelled_mid_hook_still_logs_the_event_line(
    clock, store, monkeypatch, caplog
):
    import asyncio
    import threading

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def slow(_session_key, _hook):
        clock.now += _SLOW_SECS
        loop.call_soon_threadsafe(started.set)
        release.wait(30)
        return None

    monkeypatch.setattr("kiro_crew.hook_event_governance.hook_refusal", slow)
    caplog.set_level(logging.INFO)
    fire = asyncio.ensure_future(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write"))
    try:
        await asyncio.wait_for(started.wait(), 30)
        fire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fire
    finally:
        release.set()

    (event_line,) = _event_warnings(caplog)
    assert "took 2000ms; hooks=1: guard=2000ms" in event_line.getMessage()
