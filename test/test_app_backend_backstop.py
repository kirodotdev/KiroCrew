"""The level-triggered backstop: a backend the gateway wants up but has no record for.

The exit-driven restart acts only on a tracked record, so a public start whose spawn
failed leaves an enabled app with a START lifecycle generation, no record and nothing
driving it back up. ``revive_lost_app_backends`` is the sweep that finds and starts
those, and the gateway runs it on the restart loop's steady cadence. Nothing here
spawns a process: the spawn seam is ``backend._start_app_backend``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc, _manifest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess
from kiro_crew.dashboard import server
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_APP = "app"
_REAL_START = bmod._start_app_backend


class _Spawn:
    """Stands in for ``_start_app_backend``: publishes a live record and returns it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, name: str, **_kw: Any) -> AppProcess:
        self.calls.append(name)
        record = AppProcess(app_name=name, port=9136, pid=618, proc=_fake_proc(pid=618))
        with bmod._lock:
            bmod._processes[name] = record
        return record


@pytest.fixture(name="audit")
def _audit_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The ``(outcome, error)`` of every ``app_backend_restart`` row the sweep writes."""

    rows: list[tuple[str, str]] = []

    def _log(**row: Any) -> None:
        assert row["operation"] == "app_backend_restart"
        assert row["resources"] == f"{_APP} backstop"
        rows.append((row["outcome"], row["error"]))

    monkeypatch.setattr(bmod, "sel", lambda: SimpleNamespace(log_api_access=_log))
    return rows


@pytest.fixture(name="spawn")
def _spawn_fixture(monkeypatch: pytest.MonkeyPatch) -> _Spawn:
    """An admitted, enabled app with a backend, whose first public start failed."""

    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
    monkeypatch.setattr(bmod, "_activation_denied", lambda *_a: bmod.ActivationVerdict())
    monkeypatch.setattr(bmod, "exec_backend_platform_refusal", lambda _name: None)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda *_a, **_kw: None)
    monkeypatch.setattr(bmod, "_start_app_backend", lambda _name: None)
    assert bmod.start_app_backend(_APP) is None
    spawn = _Spawn()
    monkeypatch.setattr(bmod, "_start_app_backend", spawn)
    return spawn


def test_a_wanted_backend_with_no_record_is_started_again(
    spawn: _Spawn, audit: list[tuple[str, str]]
) -> None:
    bmod.revive_lost_app_backends()

    assert spawn.calls == [_APP]
    assert audit == [("allowed", "")]
    # Tracked now, so the next sweep leaves it to its own health watch.
    bmod.revive_lost_app_backends()
    assert spawn.calls == [_APP]


@pytest.mark.parametrize(
    "unwanted",
    ["stopped", "never_started", "tracked", "disabled", "no_entry_point"],
)
def test_the_backstop_starts_nothing_the_gateway_does_not_want(
    spawn: _Spawn, monkeypatch: pytest.MonkeyPatch, unwanted: str
) -> None:
    if unwanted == "stopped":
        bmod.stop_app_backend(_APP)
    elif unwanted == "never_started":
        with bmod._lock:
            bmod._lifecycle_generation.clear()
    elif unwanted == "tracked":
        with bmod._lock:
            bmod._processes[_APP] = bmod._starting_reservation(_APP)
    elif unwanted == "disabled":
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: False)
    else:
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest(""))

    bmod.revive_lost_app_backends()

    assert spawn.calls == []


_TRANSIENT = "governance evaluation error: x"


@pytest.mark.parametrize(
    ("refused_by", "rows"),
    [
        ("policy", [("denied", "not permitted by policy")]),
        ("platform", [("denied", "exec backends need POSIX")]),
        ("execution_ceiling", [("denied", "third-party execution is disabled")]),
        ("evaluator_error", [("error", _TRANSIENT), ("error", _TRANSIENT)]),
    ],
)
def test_a_refusal_that_cannot_clear_is_asked_once(
    spawn: _Spawn,
    audit: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    refused_by: str,
    rows: list[tuple[str, str]],
) -> None:
    """A durable refusal keeps the app down; a governance evaluator error is retried."""

    if refused_by == "policy":
        verdict = bmod.ActivationVerdict(denied="not permitted by policy")
        monkeypatch.setattr(bmod, "_activation_denied", lambda *_a: verdict)
    elif refused_by == "evaluator_error":
        verdict = bmod.ActivationVerdict(denied=_TRANSIENT, transient=True)
        monkeypatch.setattr(bmod, "_activation_denied", lambda *_a: verdict)
    elif refused_by == "platform":
        monkeypatch.setattr(
            bmod, "exec_backend_platform_refusal", lambda _name: "exec backends need POSIX"
        )
    else:
        monkeypatch.setattr(
            bmod, "app_execution_denied", lambda *_a, **_kw: "third-party execution is disabled"
        )

    bmod.revive_lost_app_backends()
    bmod.revive_lost_app_backends()

    assert spawn.calls == []
    assert audit == rows


def test_a_disable_during_the_spawn_stops_the_replacement(
    spawn: _Spawn, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _spawn_then_disable(name: str, **_kw: Any) -> AppProcess:
        record = spawn(name)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: False)
        return record

    monkeypatch.setattr(bmod, "_start_app_backend", _spawn_then_disable)
    stopped: list[Any] = []
    monkeypatch.setattr(
        bmod, "stop_app_backend", lambda name, **kw: stopped.append((name, kw["_expected"]))
    )

    bmod.revive_lost_app_backends()

    assert stopped == [(_APP, bmod._processes[_APP])]


def test_a_record_published_after_the_snapshot_is_left_to_its_owner(
    spawn: _Spawn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-enable during the sweep owns the app: its exited record is not replaced."""

    later = "later"
    with bmod._lock:
        bmod._advance_lifecycle_locked(later, bmod._LIFECYCLE_START)
    original = AppProcess(app_name=later, port=9137, pid=101, proc=_fake_proc(101, 0))
    bodies: list[str] = []

    def _body(name: str, _manifest: Any) -> AppProcess:
        bodies.append(name)
        if name == _APP:
            with bmod._health_reconcile_lock, bmod._lock:
                bmod._advance_lifecycle_locked(later, bmod._LIFECYCLE_START)
                bmod._processes[later] = original
        return spawn(name)

    monkeypatch.setattr(bmod, "_start_app_backend", _REAL_START)
    monkeypatch.setattr(bmod, "_start_app_backend_body", _body)

    bmod.revive_lost_app_backends()

    assert bodies == [_APP]
    assert bmod._processes[later] is original


@pytest.mark.asyncio
async def test_the_gateway_runs_the_sweep_on_the_restart_cadence_until_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    sweeps: list[int] = []

    def _sweep() -> None:
        sweeps.append(1)
        if len(sweeps) == 2:
            loop.call_soon_threadsafe(stop.set)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(server, "_RESTART_STEADY_INTERVAL", 0)
    monkeypatch.setattr(server, "shutdown_event", stop)
    monkeypatch.setattr(server, "revive_lost_app_backends", _sweep)
    state = SimpleNamespace(_background_tasks=set())

    server._kick_app_backend_backstop(state)
    (task,) = state._background_tasks
    await asyncio.wait_for(task, timeout=FALLBACK_TIMEOUT_SECS)

    assert sweeps == [1, 1]
    assert not state._background_tasks
