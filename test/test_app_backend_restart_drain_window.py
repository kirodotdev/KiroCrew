"""A restart keeps the app's slot reserved while it drains the dead root's tree.

``_restart_exited_backend`` drains the exited root's process tree before it spawns
the replacement, and that drain takes seconds (a SIGTERM grace, then SIGKILL). If
the dead record were simply removed first, the app would read as free to start for
that whole span: a concurrent start (enable, update, the boot pass, or any internal
starter) would begin its own spawn, and on a fixed port it could adopt the forked
server the drain is about to end, leaving a dead adopted record nothing restarts.

The restart therefore swaps the dead record for a STARTING reservation and holds
the app's lifecycle flock across the forget + drain. These tests hold the drain
open at the ``_drain_exited_root_tree`` seam and run a second start against it;
nothing here spawns a process or binds a port.
"""

from __future__ import annotations

import logging
import os
import threading
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc, _manifest

import kiro_crew.apps.backend as bmod
import kiro_crew.apps.backend_runtime.restart as rmod
from kiro_crew import platform_compat
from kiro_crew.apps.backend import AppProcess
from kiro_crew.config.loader import config_dir

_APP = "app"
_WAIT = 5.0


class _HeldDrain:
    """Stands in for ``_drain_exited_root_tree``; the first call blocks until released."""

    def __init__(self, during: Callable[[], None] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._during = during

    def __call__(self, *_args: Any) -> bool:
        self.calls += 1
        if self.calls > 1:
            return True
        self.entered.set()
        if self._during is not None:
            self._during()
        assert self.release.wait(_WAIT), "the test never released the drain"
        return True


class _Body:
    """Stands in for the spawn body: adopts while the drain is open, spawns after."""

    def __init__(self, drain: _HeldDrain) -> None:
        self._drain = drain
        self.adopted = threading.Event()
        self.spawned: list[AppProcess] = []

    def __call__(self, name: str, _manifest: Any) -> AppProcess:
        if self._drain.entered.is_set() and not self._drain.release.is_set():
            # The shape the issue reports: a fixed-port probe finds the dead
            # launcher's forked server still answering and adopts it.
            record = AppProcess(app_name=name, port=9136, adopted_pids=[9999])
            self.adopted.set()
        else:
            record = AppProcess(app_name=name, port=9136, pid=618, proc=_fake_proc(pid=618))
            self.spawned.append(record)
        with bmod._lock:
            bmod._processes[name] = record
            bmod._allocated_ports[name] = record.port
        return record


@pytest.fixture()
def restart_env(monkeypatch: pytest.MonkeyPatch) -> AppProcess:
    """An exited, tracked backend whose restart is permitted and runs at once."""
    monkeypatch.setattr(
        bmod,
        "shutdown_event",
        SimpleNamespace(is_set=lambda: False, wait=lambda _delay: False),
    )
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
    monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
    monkeypatch.setattr(bmod, "_forget_app_pid", lambda *_args: None)
    monkeypatch.setattr(bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict())
    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
    ap = AppProcess(
        app_name=_APP,
        port=9136,
        pid=617,
        proc=_fake_proc(pid=617, returncode=1),
        healthy=False,
        mcp_healthy=False,
    )
    with bmod._lock:
        bmod._processes[_APP] = ap
        bmod._allocated_ports[_APP] = ap.port
    return ap


def _run(target: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def _go() -> None:
        try:
            out.append(target())
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below
            out.append(exc)

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    return thread, out


def _flock_is_free() -> bool:
    """Probe the app's lifecycle flock from a separate descriptor, as a waiter does."""
    lock_dir = config_dir() / "app_backend_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / f"{_APP}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        return bool(platform_compat.try_acquire_lock(fd, exclusive=True))
    finally:
        os.close(fd)


class _AwaitSignal(logging.Handler):
    """Sets ``reached`` once a start logs that it is awaiting an in-flight spawn."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.reached = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if "awaiting the in-flight spawn" in record.getMessage():
            self.reached.set()


@pytest.fixture()
def await_signal() -> Iterator[_AwaitSignal]:
    handler = _AwaitSignal()
    logger = logging.getLogger(bmod._FACADE)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.mark.parametrize(
    "starter",
    ["start_app_backend", "_start_app_backend"],
    ids=["public-start", "internal-start"],
)
def test_a_start_during_the_drain_waits_instead_of_adopting(
    restart_env: AppProcess,
    monkeypatch: pytest.MonkeyPatch,
    await_signal: _AwaitSignal,
    starter: str,
) -> None:
    drain = _HeldDrain()
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    restart, restart_out = _run(lambda: bmod._restart_exited_backend(restart_env, 1))
    start: threading.Thread | None = None
    try:
        assert drain.entered.wait(_WAIT)
        with bmod._lock:
            held = bmod._processes.get(_APP)
        assert held is not None and held.starting is True, "the slot must stay reserved"

        start, start_out = _run(lambda: getattr(bmod, starter)(_APP))
        # Positive evidence of the overlap: the start is waiting on the reservation
        # while the drain is still held open.
        assert await_signal.reached.wait(_WAIT), "the start did not wait on the reservation"
        assert not body.adopted.is_set(), "a start during the drain adopted the draining server"
        with bmod._lock:
            assert bmod._processes.get(_APP) is held
    finally:
        drain.release.set()
        restart.join(_WAIT)
        if start is not None:
            start.join(_WAIT + 25.0)
    assert not restart.is_alive() and start is not None and not start.is_alive()
    assert restart_out == [True]
    assert not body.adopted.is_set()
    assert len(body.spawned) == 1
    replacement = body.spawned[0]
    assert bmod._processes[_APP] is replacement
    assert start_out in ([None], [replacement])


def test_the_reservation_is_published_with_the_flock_already_held(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No gap between the reservation and the flock for a stop+start to slip into."""
    seen: list[tuple[bool, bool]] = []

    def _enabled(_name: str) -> bool:
        with bmod._lock:
            current = bmod._processes.get(_APP)
        seen.append((current is restart_env, _flock_is_free()))
        return True

    monkeypatch.setattr(bmod, "_app_enabled_state", _enabled)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a: True)
    monkeypatch.setattr(
        bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict(denied="policy")
    )
    assert bmod._restart_exited_backend(restart_env, 1) is False
    # The pre-wait check runs without the flock; the post-wait check that swaps in
    # the reservation runs with it held.
    assert seen[0] == (True, True)
    assert seen[1] == (True, False)


def test_a_stop_then_start_during_the_drain_cannot_adopt_the_draining_server(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[threading.Thread] = []

    def _during() -> None:
        assert bmod.stop_app_backend(_APP) is True
        thread, _out = _run(lambda: bmod.start_app_backend(_APP))
        started.append(thread)
        # The start's spawn needs the flock the drain holds, so it cannot run (and
        # cannot probe and adopt) until the drain is over. Positive evidence that it
        # got that far: its own STARTING placeholder is the record.
        deadline = threading.Event()
        for _ in range(int(_WAIT / 0.01)):
            with bmod._lock:
                current = bmod._processes.get(_APP)
            if current is not None and current.starting:
                break
            deadline.wait(0.01)  # flake-ok: bounded poll for a positive condition
        assert current is not None and current.starting, "the start never reserved"
        thread.join(0.5)  # flake-ok: must stay blocked on the held flock; can only pass falsely
        assert thread.is_alive()
        assert not body.adopted.is_set()
        drain.release.set()

    drain = _HeldDrain(during=_during)
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)
    try:
        assert bmod._restart_exited_backend(restart_env, 1) is True
    finally:
        drain.release.set()
        for thread in started:
            thread.join(_WAIT + 25.0)
    assert not body.adopted.is_set()
    assert len(body.spawned) == 1
    assert bmod._processes[_APP] is body.spawned[0]


def test_a_lifecycle_lock_failure_is_retried_on_the_cadence(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(
        bmod,
        "shutdown_event",
        SimpleNamespace(is_set=lambda: False, wait=lambda d: delays.append(d) or False),
    )
    real = rmod.app_backend_lifecycle_flock
    calls: list[int] = []

    def _flaky(name: str) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("lock directory unavailable")
        return real(name)

    monkeypatch.setattr(rmod, "app_backend_lifecycle_flock", _flaky)
    drain = _HeldDrain()
    drain.release.set()
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    assert bmod._restart_exited_backend(restart_env, 1) is True
    assert delays == [0.0, 1.0]
    assert bmod._restart_attempts[_APP] == 2
    assert len(body.spawned) == 1 and bmod._processes[_APP] is body.spawned[0]


def test_a_waiter_that_outlasts_its_window_leaves_the_reservation_in_place(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flock held across the drain tells a timed-out waiter the owner is alive."""
    waited: list[Any] = []

    def _during() -> None:
        waited.append(bmod._await_inflight_spawn(_APP, timeout=0.2))
        with bmod._lock:
            waited.append(bmod._processes.get(_APP))
        drain.release.set()

    drain = _HeldDrain(during=_during)
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    assert bmod._restart_exited_backend(restart_env, 1) is True
    assert waited[0] is None
    assert waited[1] is not None and waited[1].starting is True
    assert len(body.spawned) == 1 and bmod._processes[_APP] is body.spawned[0]


def test_a_policy_denial_leaves_no_reservation_and_frees_the_flock(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a: True)
    monkeypatch.setattr(
        bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict(denied="policy")
    )
    monkeypatch.setattr(
        bmod, "_start_app_backend_body", lambda *_a: pytest.fail("a denied restart spawned")
    )
    assert bmod._restart_exited_backend(restart_env, 1) is False
    assert _APP not in bmod._processes
    with bmod.app_backend_lifecycle_flock(_APP):
        pass


def test_a_transient_denial_restores_the_dead_record_and_retries(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdicts = iter(
        [
            bmod.ActivationVerdict(denied="governance evaluation error: x", transient=True),
            bmod.ActivationVerdict(),
        ]
    )
    drain = _HeldDrain()
    drain.release.set()
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_activation_denied", lambda _n, _a: next(verdicts))
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    assert bmod._restart_exited_backend(restart_env, 1) is True
    assert bmod._restart_attempts[_APP] == 2
    assert len(body.spawned) == 1 and bmod._processes[_APP] is body.spawned[0]


def test_a_drain_that_raises_leaves_no_reservation(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any) -> bool:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(bmod, "_drain_exited_root_tree", _boom)
    with pytest.raises(RuntimeError, match="drain failed"):
        bmod._restart_exited_backend(restart_env, 1)
    assert _APP not in bmod._processes
    with bmod.app_backend_lifecycle_flock(_APP):
        pass


def test_a_stop_during_the_drain_wins_and_the_replacement_is_stopped(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[Any] = []

    def _during() -> None:
        stopped.append(bmod.stop_app_backend(_APP))
        with bmod._lock:
            stopped.append(bmod._processes.get(_APP))
        drain.release.set()

    drain = _HeldDrain(during=_during)
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)
    monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bmod.platform_compat, "kill_process_tree_pinned", lambda *_a, **_k: True, raising=False
    )

    assert bmod._restart_exited_backend(restart_env, 1) is False
    assert stopped[1] is None, "the stop removed the reservation"
    assert bmod._lifecycle_generation[_APP][1] == bmod._LIFECYCLE_STOP
    assert _APP not in bmod._processes


def test_the_reservation_cleanup_leaves_a_record_that_replaced_it_in_place(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``finally`` drops the restart's own reservation only, by identity.

    A stop then start during the drain puts the start's own placeholder in the slot.
    If the cleanup removed whatever record it found, it would take that placeholder
    with it, and the app would stay down with nothing left to restart it.
    """
    successor: list[AppProcess] = []

    def _during() -> None:
        # Stands in for a stop+start during the drain: a different record now owns
        # the slot. The denial below keeps the restart from spawning over it.
        record = bmod._starting_reservation(_APP)
        with bmod._lock:
            assert bmod._processes.get(_APP) is not record
            bmod._processes[_APP] = record
        successor.append(record)
        drain.release.set()

    drain = _HeldDrain(during=_during)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(
        bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict(denied="policy")
    )
    monkeypatch.setattr(
        bmod, "_start_app_backend_body", lambda *_a: pytest.fail("a denied restart spawned")
    )

    assert bmod._restart_exited_backend(restart_env, 1) is False
    assert len(successor) == 1
    with bmod._lock:
        assert bmod._processes.get(_APP) is successor[0], "the cleanup removed the successor"
