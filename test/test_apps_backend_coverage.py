"""Additional unit coverage for :mod:`kiro_crew.apps.backend`.

Complements ``test_app_backend.py`` (port allocation, spawn survival, dispatch)
and ``test_app_backend_stale_reap.py`` (pidfile reap safety) by exercising the
branches those files leave untouched. This module covers starting a backend:

* which functions may mutate the process table, the port probes and PID ancestry,
* which PIDs may be attributed to an app (only a live child of this gateway),
* ``start_app_backend``'s coordination and who publishes a spawned process,
* the spawn body's port resolution and the adopt-an-already-healthy-instance path.

The rest lives in siblings named ``test_apps_backend_coverage_<topic>.py``:
``_dependency_install``, ``_dispatch``, ``_stop``, ``_supervision`` and ``_boot``.
The doubles and fixtures the family shares live in ``apps_backend_coverage_helpers.py``.

Everything here is hermetic and order-independent: no real process is spawned,
no socket is bound, no network request is made, and no wall-clock duration is
asserted. ``subprocess.Popen`` / ``subprocess.run``, the ``socket`` module,
and ``loopback_urlopen`` are stubbed, and the spawn body is frozen at the
``Popen`` seam with a sentinel exception.
"""

from __future__ import annotations

import ast
import logging
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import (
    _capture_popen,
    _fake_proc,
    _FakeProc,
    _FakeResp,
    _install_fake_socket,
    _manifest,
    _StopSpawn,
)

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess


def test_lifecycle_mutation_is_confined_to_declared_owners() -> None:
    # The facade and every owner it composes: the rule is about which FUNCTIONS
    # mutate the process table, wherever each one lives.
    sources = [Path(bmod.__file__)] + [
        Path(sys.modules[name].__file__) for name in bmod._PART_MODULES
    ]
    module_functions = [
        node
        for source_path in sources
        for node in ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        ).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert len(module_functions) > 80
    mutations: dict[str, set[str]] = {
        "_processes": set(),
        "_lifecycle_generation": set(),
    }
    lifecycle_callers: set[str] = set()
    mutating_methods = {"pop", "setdefault", "update", "clear"}

    def _subscripted_table(target: ast.AST) -> str | None:
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            return target.value.id
        return None

    for owner in module_functions:
        for node in ast.walk(owner):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = node.targets
            for target in targets:
                table = _subscripted_table(target)
                if table in mutations:
                    mutations[table].add(owner.name)

            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in mutations
                    and func.attr in mutating_methods
                ):
                    mutations[func.value.id].add(owner.name)
                if isinstance(func, ast.Name) and func.id == "_advance_lifecycle_locked":
                    lifecycle_callers.add(owner.name)

    rule = (
        "Lifecycle ownership changed; see docs/system-specs/modules/"
        "app-kit-platform.md and _advance_lifecycle_locked"
    )
    assert mutations["_lifecycle_generation"] == frozenset({"_advance_lifecycle_locked"}), rule
    assert lifecycle_callers == frozenset({"start_app_backend", "stop_app_backend"}), rule
    assert mutations["_processes"] == frozenset(
        {
            "_await_inflight_spawn",
            "_clear_failed_spawn_state",
            "_restart_exited_backend",
            "_settle_superseding_start",
            "_start_app_backend",
            "_start_app_backend_body",
            "stop_app_backend",
        }
    ), rule


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


# A lost-run ceiling for the threaded spawn-ownership tests, never their race window:
# every wait below ends on an event the test or the spawn path sets, and once the
# gates are released the workers depend on nothing the test still holds. The bound
# only turns a broken handoff into a named failure instead of a hung worker.
# Measured from the windows-pytest-progress artifacts: a passing call takes 0.16-0.17 s,
# and a contended runner has held the post-gate publication work (real pidfile and log
# writes) past 2 s, so this is more than 10x that, and half the suite's --timeout=120.
_SPAWN_RACE_CEILING_SECS = 30.0


def _join_started(*threads: threading.Thread) -> float:
    """Join every worker that was started, so none outlives its test's monkeypatches.

    A thread that was never started (because an earlier coordination assert failed)
    cannot be joined, so it is skipped. The started ones share one lost-run ceiling,
    so a broken run fails after one ceiling, not one per thread. Returns the seconds
    spent joining, for the caller's failure message.
    """
    started = time.monotonic()
    deadline = started + _SPAWN_RACE_CEILING_SECS
    for thread in threads:
        if thread.ident is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
    return time.monotonic() - started


def _stub_listeners(monkeypatch: pytest.MonkeyPatch, listeners: list[Any]) -> None:
    """Pin the port->PID lookup the adoption path reads its owners from.

    ``find_port_listeners`` never raises and folds every failure (tool absent,
    wedged probe) into ``[]``, so an empty stub covers the unavailable case too.
    """

    monkeypatch.setattr(bmod.platform_compat, "find_port_listeners", lambda _port: listeners)


# ---------------------------------------------------------------------------
# Port + listener probes
# ---------------------------------------------------------------------------


class TestPortProbes:
    def test_exhausted_range_raises_rather_than_returning_a_taken_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handing back an already-reserved port would crash-loop the loser."""

        monkeypatch.setattr(bmod, "_MIN_PORT", 9100)
        monkeypatch.setattr(bmod, "_MAX_PORT", 9103)
        with bmod._lock:
            bmod._allocated_ports.update({"a": 9100, "b": 9101, "c": 9102})
        with pytest.raises(RuntimeError, match="No free ports"):
            bmod._find_free_port()

    def test_port_is_listening_true_when_connect_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The True branch, without a real host socket.

        An earlier revision bound a real ephemeral loopback listener here. Even
        on port 0 that is a host-level side effect outside ``tmp_path``, and it
        can fail outright on a locked-down runner -- so the success path is
        faked the same way the refusal path below already is. ``_port_is_listening``
        only uses the connection as a context manager and discards it, so a
        minimal stub is a faithful double.
        """

        class _FakeConn:
            def __enter__(self) -> _FakeConn:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        seen: dict[str, object] = {}

        def _connect(address: tuple[str, int], **kwargs: object) -> _FakeConn:
            seen["address"] = address
            seen["timeout"] = kwargs.get("timeout")
            return _FakeConn()

        monkeypatch.setattr(bmod.socket, "create_connection", _connect)
        assert bmod._port_is_listening(9100) is True
        # Probing anything but loopback would reach off-box.
        assert seen["address"] == ("127.0.0.1", 9100)
        assert seen["timeout"] == bmod._PORT_PROBE_TIMEOUT

    def test_port_is_listening_false_when_connect_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bmod.socket,
            "create_connection",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("refused")),
        )
        assert bmod._port_is_listening(9100) is False

    def test_listening_pids_passes_through_the_platform_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.platform_compat, "find_listening_pids", lambda _p: [7, 9])
        assert bmod._listening_pids(9100) == [7, 9]

    def test_listening_pids_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A probe failure must degrade to 'unknown', not fail the spawn."""

        def _boom(_port: int) -> list[int]:
            raise RuntimeError("lsof exploded")

        monkeypatch.setattr(bmod.platform_compat, "find_listening_pids", _boom)
        assert bmod._listening_pids(9100) == []


class TestPidAncestry:
    def test_same_pid_is_its_own_ancestor(self) -> None:
        assert bmod._pid_is_self_or_descendant_of(11, 11) is True

    def test_direct_child_is_a_descendant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda _p: 11)
        assert bmod._pid_is_self_or_descendant_of(22, 11) is True

    def test_ppid_probe_failure_denies_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_pid: int) -> int:
            raise OSError("no /proc")

        monkeypatch.setattr(bmod.platform_compat, "get_ppid", _boom)
        assert bmod._pid_is_self_or_descendant_of(22, 11) is False

    def test_reaching_pid_0_denies_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda _p: 0)
        assert bmod._pid_is_self_or_descendant_of(22, 11) is False

    def test_walk_is_depth_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unbounded walk on a pathological parent map must not hang."""

        seen: list[int] = []

        def _ppid(pid: int) -> int:
            seen.append(pid)
            return pid + 1  # never reaches the ancestor

        monkeypatch.setattr(bmod.platform_compat, "get_ppid", _ppid)
        assert bmod._pid_is_self_or_descendant_of(100, 11) is False
        assert len(seen) == bmod._PID_ANCESTRY_MAX_DEPTH

    def test_spawn_owns_listener_combines_probe_and_ancestry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_listening_pids", lambda _p: [55])
        monkeypatch.setattr(
            bmod, "_pid_is_self_or_descendant_of", lambda pid, anc: pid == 55 and anc == 42
        )
        assert bmod._spawn_owns_listener(9100, 42) is True
        assert bmod._spawn_owns_listener(9100, 43) is False


class TestSpawnedBackendOwnsPid:
    """Only a LIVE child of this gateway may be attributed to an app."""

    @staticmethod
    def _live(pid: int, name: str = "app") -> AppProcess:
        return AppProcess(
            app_name=name,
            pid=pid,
            proc=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        )

    def test_the_spawn_root_itself_is_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_processes", {"app": self._live(11)})
        assert bmod.spawned_backend_owns_pid(11) is True

    def test_the_owner_is_named_by_its_registry_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The attribution the token mint audits is the app the backend runs under."""
        monkeypatch.setattr(
            bmod,
            "_processes",
            {"first": self._live(11, "first"), "second": self._live(33, "second")},
        )
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda pid: {44: 33}.get(pid, 1))
        assert bmod.spawned_backend_owner(11) == "first"
        assert bmod.spawned_backend_owner(44) == "second"
        assert bmod.spawned_backend_owner(22) is None

    def test_no_owner_is_named_for_a_dead_or_adopted_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The name form answers on exactly the records the boolean form trusts."""
        dead = AppProcess(
            app_name="dead",
            pid=11,
            proc=SimpleNamespace(poll=lambda: 0),  # type: ignore[arg-type]
        )
        adopted = AppProcess(app_name="adopted", pid=22, proc=None, adopted_pids=[22])
        monkeypatch.setattr(bmod, "_processes", {"dead": dead, "adopted": adopted})
        assert bmod.spawned_backend_owner(11) is None
        assert bmod.spawned_backend_owner(22) is None

    def test_a_launcher_descendant_is_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``wrap_argv`` puts a sandbox launcher between us and the real server."""
        monkeypatch.setattr(bmod, "_processes", {"app": self._live(11)})
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda _p: 11)
        assert bmod.spawned_backend_owns_pid(22) is True

    def test_an_unrelated_pid_is_not_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_processes", {"app": self._live(11)})
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda _p: 1)
        assert bmod.spawned_backend_owns_pid(22) is False

    def test_an_adopted_backend_is_never_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An adopted instance's pid belongs to a supervisor that is not us."""
        adopted = AppProcess(app_name="app", pid=11, proc=None, adopted_pids=[11])
        monkeypatch.setattr(bmod, "_processes", {"app": adopted})
        assert bmod.spawned_backend_owns_pid(11) is False

    def test_an_exited_child_is_never_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once a child is reaped its pid is free for any process to take."""
        dead = AppProcess(
            app_name="app",
            pid=11,
            proc=SimpleNamespace(poll=lambda: 0),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(bmod, "_processes", {"app": dead})
        assert bmod.spawned_backend_owns_pid(11) is False

    def test_a_record_with_no_pid_matches_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the guard, pid 0 would be its own ancestor and match."""
        blank = AppProcess(
            app_name="app",
            pid=0,
            proc=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(bmod, "_processes", {"app": blank})
        assert bmod.spawned_backend_owns_pid(0) is False

    def test_the_ancestry_walk_runs_outside_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``/proc`` read per candidate must not be serialised behind spawns."""
        free: list[bool] = []

        def _probe(pid: int, ancestor: int) -> bool:
            # Non-reentrant lock, so a successful acquire proves it was released.
            acquired = bmod._lock.acquire(blocking=False)
            free.append(acquired)
            if acquired:
                bmod._lock.release()
            return pid == ancestor

        monkeypatch.setattr(bmod, "_processes", {"app": self._live(11)})
        monkeypatch.setattr(bmod, "_pid_is_self_or_descendant_of", _probe)
        assert bmod.spawned_backend_owns_pid(11) is True
        assert free == [True]


# ---------------------------------------------------------------------------
# start_app_backend coordination
# ---------------------------------------------------------------------------


class TestStartCoordination:
    def test_no_manifest_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _n: None)
        assert bmod.start_app_backend("ghost") is None

    def test_a_live_spawned_process_is_reused_not_respawned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _n: _manifest("server.py"))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend_body",
            lambda *_a: pytest.fail("respawned an already-running backend"),
        )
        existing = AppProcess(app_name="live", port=9100, pid=1, proc=_fake_proc())
        with bmod._lock:
            bmod._processes["live"] = existing
        assert bmod.start_app_backend("live") is existing

    def test_an_adopted_instance_is_reused_not_respawned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _n: _manifest("server.py"))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend_body",
            lambda *_a: pytest.fail("respawned over an adopted instance"),
        )
        existing = AppProcess(
            app_name="adopted", port=9100, pid=0, proc=None, adopted_pids=[123], healthy=True
        )
        with bmod._lock:
            bmod._processes["adopted"] = existing
        assert bmod.start_app_backend("adopted") is existing

    def test_a_raising_spawn_body_clears_the_starting_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the app is wedged in 'starting' until a gateway restart."""

        monkeypatch.setattr(bmod, "get_app_manifest", lambda _n: _manifest("server.py"))

        def _boom(_name: str, _manifest: Any) -> None:
            raise RuntimeError("sandbox unavailable")

        monkeypatch.setattr(bmod, "_start_app_backend_body", _boom)
        with pytest.raises(RuntimeError, match="sandbox unavailable"):
            bmod.start_app_backend("boomer")
        assert "boomer" not in bmod._processes
        assert "boomer" not in bmod._allocated_ports

    def test_failed_spawn_releases_boot_preclaimed_fixed_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure before `_claim_port` releases the boot-time reservation."""

        fixed_port = bmod._MIN_PORT + 17
        manifest = _manifest("server.py", port=str(fixed_port))
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr(bmod, "_start_app_backend_body", lambda *_args: None)

        bmod._preclaim_fixed_ports(["doomed"])
        assert bmod._allocated_ports == {"doomed": fixed_port}
        assert bmod._start_app_backend("doomed") is None

        assert "doomed" not in bmod._processes
        assert "doomed" not in bmod._allocated_ports
        bmod._claim_port("later", fixed_port)
        assert bmod._allocated_ports["later"] == fixed_port


class TestAwaitInflightSpawn:
    def test_a_cleared_placeholder_resolves_to_none(self) -> None:
        assert bmod._await_inflight_spawn("nobody", timeout=0.2) is None

    def test_a_resolved_process_is_returned(self) -> None:
        ap = AppProcess(app_name="done", port=9100, pid=7)
        with bmod._lock:
            bmod._processes["done"] = ap
        assert bmod._await_inflight_spawn("done", timeout=0.2) is ap

    def test_a_process_that_resolved_at_the_deadline_is_still_returned(self) -> None:
        """The post-deadline recheck must not throw away a real started process."""

        ap = AppProcess(app_name="late", port=9100, pid=7)
        with bmod._lock:
            bmod._processes["late"] = ap
        # timeout=0 skips the poll loop entirely and goes straight to the recheck.
        assert bmod._await_inflight_spawn("late", timeout=0.0) is ap

    def test_an_absent_entry_at_the_deadline_resolves_to_none(self) -> None:
        assert bmod._await_inflight_spawn("absent", timeout=0.0) is None


class TestSpawnPublicationOwnership:
    @pytest.mark.parametrize("port", ["auto", str(bmod._MIN_PORT)])
    def test_spawn_retired_before_reservation_claims_no_port(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, port: str
    ) -> None:
        """Ownership validation and reservation are one atomic transition."""

        (spawn_root / "server.py").write_text("x = 1\n")
        retired = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", starting=True)
        with bmod._lock:
            bmod._processes["app"] = successor

        monkeypatch.setattr(
            bmod,
            "popen_limited",
            lambda *_args, **_kwargs: pytest.fail("retired spawn reached Popen"),
        )
        previous_owner = bmod._spawn_publication_owner.get()
        bmod._spawn_publication_owner.set(retired)
        try:
            assert bmod._start_app_backend_body("app", _manifest("server.py", port=port)) is None
        finally:
            bmod._spawn_publication_owner.set(previous_owner)

        assert bmod._processes["app"] is successor
        assert "app" not in bmod._allocated_ports

    def test_retired_release_preserves_successor_claim_and_frees_its_port(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retired cleanup cannot touch a successor's slot or reservation."""

        monkeypatch.setattr(bmod, "_MIN_PORT", 9100)
        monkeypatch.setattr(bmod, "_MAX_PORT", 9102)
        retired = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", starting=True)
        with bmod._lock:
            bmod._processes["app"] = retired
        previous_owner = bmod._spawn_publication_owner.get()
        bmod._spawn_publication_owner.set(retired)
        try:
            retired_port = bmod._reserve_free_port("app")
        finally:
            bmod._spawn_publication_owner.set(previous_owner)
        assert retired_port == 9100

        with bmod._lock:
            bmod._processes["app"] = successor
            bmod._allocated_ports["app"] = 9101
        bmod._clear_failed_spawn_state("app", retired)

        assert bmod._processes["app"] is successor
        assert bmod._allocated_ports["app"] == 9101
        bmod._spawn_publication_owner.set(successor)
        try:
            assert bmod._reserve_free_port("app") == retired_port
        finally:
            bmod._spawn_publication_owner.set(previous_owner)

    def test_retired_cleanup_precedes_successor_reservation(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lifecycle flock is released only after retired-state cleanup."""

        monkeypatch.setattr(bmod, "_MIN_PORT", 9100)
        monkeypatch.setattr(bmod, "_MAX_PORT", 9101)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        lifecycle_lock = threading.Lock()
        flock_calls_lock = threading.Lock()
        first_reserved = threading.Event()
        release_first = threading.Event()
        cleanup_complete = threading.Event()
        successor_waiting = threading.Event()
        successor_reserved = threading.Event()
        body_calls = 0
        flock_calls = 0
        first_owner: list[AppProcess] = []
        cleanup_before_successor: list[bool] = []

        @bmod.contextlib.contextmanager
        def _flock(_name: str) -> Any:
            nonlocal flock_calls
            with flock_calls_lock:
                flock_calls += 1
                call_number = flock_calls
                if call_number == 2:
                    successor_waiting.set()
            with lifecycle_lock:
                yield
            if call_number == 1:
                assert successor_reserved.wait(_SPAWN_RACE_CEILING_SECS)

        def _body(name: str, _manifest: Any) -> AppProcess | None:
            nonlocal body_calls
            body_calls += 1
            owner = bmod._spawn_publication_owner.get()
            assert owner is not None
            if body_calls == 1:
                first_owner.append(owner)
                assert bmod._reserve_free_port(name) == 9100
                first_reserved.set()
                assert release_first.wait(_SPAWN_RACE_CEILING_SECS)
                return None
            assert cleanup_complete.is_set()
            port = bmod._reserve_free_port(name)
            successor = AppProcess(app_name=name, port=port, pid=704, proc=_fake_proc(pid=704))
            with bmod._lock:
                bmod._processes[name] = successor
                bmod._allocated_ports[name] = port
            successor_reserved.set()
            return successor

        real_clear = bmod._clear_failed_spawn_state

        def _clear(name: str, placeholder: AppProcess) -> None:
            if first_owner and placeholder is first_owner[0]:
                cleanup_before_successor.append(not successor_reserved.is_set())
            real_clear(name, placeholder)
            if first_owner and placeholder is first_owner[0]:
                cleanup_complete.set()

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _flock)
        monkeypatch.setattr(bmod, "_start_app_backend_body", _body)
        monkeypatch.setattr(bmod, "_clear_failed_spawn_state", _clear)
        first_results: list[AppProcess | None] = []
        successor_results: list[AppProcess | None] = []
        errors: list[BaseException] = []

        def _run(target: Any, results: list[AppProcess | None]) -> None:
            try:
                results.append(target("app"))
            except BaseException as exc:
                errors.append(exc)

        first_thread = threading.Thread(target=_run, args=(bmod._start_app_backend, first_results))
        successor_thread = threading.Thread(
            target=_run, args=(bmod.start_app_backend, successor_results)
        )
        # A failed coordination assert must not leave workers running past teardown
        # (they would see monkeypatches restored under them), so every gate is
        # released and every started thread joined on the way out.
        first_thread.start()
        try:
            assert first_reserved.wait(_SPAWN_RACE_CEILING_SECS)
            assert bmod.stop_app_backend("app") is True

            successor_thread.start()
            assert successor_waiting.wait(_SPAWN_RACE_CEILING_SECS)
        finally:
            release_first.set()
            joined_after = _join_started(first_thread, successor_thread)

        assert not first_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert not successor_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert errors == []
        assert first_results == [None]
        assert len(successor_results) == 1
        successor = successor_results[0]
        assert successor is not None
        assert cleanup_before_successor == [True]
        assert bmod._processes["app"] is successor
        assert bmod._allocated_ports["app"] == 9100

    def test_restart_spawn_retired_by_stop_start_never_publishes_or_leaks(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later public START owns publication and the retired child is reaped."""

        (spawn_root / "server.py").write_text("x = 1\n")
        manifest = _manifest("server.py")
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_start_health_supervisor", lambda *_args: None)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"start-{pid}")

        old = AppProcess(
            app_name="app",
            port=9136,
            pid=700,
            proc=_fake_proc(pid=700, returncode=-15),
        )
        with bmod._lock:
            bmod._processes["app"] = old
            bmod._allocated_ports["app"] = old.port

        spawned = [_FakeProc(pid=701), _FakeProc(pid=702)]
        popen_calls: list[int] = []

        def _popen(*_args: Any, **_kwargs: Any) -> _FakeProc:
            proc = spawned[len(popen_calls)]
            popen_calls.append(proc.pid)
            return proc

        monkeypatch.setattr(bmod, "popen_limited", _popen)

        restart_child_spawned = threading.Event()
        allow_restart_publication = threading.Event()

        def _survived(proc: _FakeProc, _port: int | None = None) -> bool:
            if proc.pid == 701:
                restart_child_spawned.set()
                assert allow_restart_publication.wait(_SPAWN_RACE_CEILING_SECS)
            return True

        monkeypatch.setattr(bmod, "_survived_spawn", _survived)

        flock = threading.Lock()
        flock_calls_lock = threading.Lock()
        public_start_waiting = threading.Event()
        flock_calls = 0

        @bmod.contextlib.contextmanager
        def _flock(_name: str) -> Any:
            nonlocal flock_calls
            with flock_calls_lock:
                flock_calls += 1
                if flock_calls == 2:
                    public_start_waiting.set()
            with flock:
                yield

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _flock)
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, sig: kills.append((pid, sig)),
        )

        restart_results: list[bool] = []
        start_results: list[AppProcess | None] = []
        errors: list[BaseException] = []

        def _restart() -> None:
            try:
                restart_results.append(bmod._restart_exited_backend(old, -15))
            except BaseException as exc:
                errors.append(exc)

        def _public_start() -> None:
            try:
                start_results.append(bmod.start_app_backend("app"))
            except BaseException as exc:
                errors.append(exc)

        restart_thread = threading.Thread(target=_restart)
        start_thread = threading.Thread(target=_public_start)
        restart_thread.start()
        try:
            assert restart_child_spawned.wait(_SPAWN_RACE_CEILING_SECS)
            assert bmod.stop_app_backend("app") is True

            start_thread.start()
            assert public_start_waiting.wait(_SPAWN_RACE_CEILING_SECS)
        finally:
            allow_restart_publication.set()
            joined_after = _join_started(restart_thread, start_thread)

        assert not restart_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert not start_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert errors == []
        assert restart_results == [True]
        assert len(start_results) == 1
        successor = start_results[0]
        assert successor is not None
        assert successor.pid == 702
        assert bmod._processes == {"app": successor}
        assert popen_calls == [701, 702]
        assert kills == [(701, bmod.platform_compat.SIGTERM)]
        row = bmod._read_pidfile()["app"]
        # Exact key set, not a projection: an extra key must fail here, which is
        # what makes this a ratchet on the persisted row rather than a spot check.
        # The value itself cannot be pinned (a fresh uuid per spawn), so only its
        # presence and non-emptiness are asserted.
        assert set(row) == {"pid", "start_time", "port", "spawn_instance"}
        assert (row["pid"], row["start_time"], row["port"]) == (702, "start-702", successor.port)
        # The successor's own incarnation token, which the startup reap needs to
        # vouch its process group once the leader is gone.
        assert row["spawn_instance"]

    def test_restart_joins_a_public_start_already_in_flight(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restart-side internal start awaits, rather than spawning a child."""

        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_start_health_supervisor", lambda *_args: None)
        child = _FakeProc(pid=703)
        popen_calls: list[int] = []

        def _popen(*_args: Any, **_kwargs: Any) -> _FakeProc:
            popen_calls.append(child.pid)
            return child

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        child_spawned = threading.Event()
        allow_publication = threading.Event()

        def _survived(_proc: _FakeProc, _port: int | None = None) -> bool:
            child_spawned.set()
            assert allow_publication.wait(_SPAWN_RACE_CEILING_SECS)
            return True

        monkeypatch.setattr(bmod, "_survived_spawn", _survived)
        await_called = threading.Event()
        real_await = bmod._await_inflight_spawn

        def _await(name: str, timeout: float = 20.0) -> AppProcess | None:
            await_called.set()
            return real_await(name, timeout)

        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        public_results: list[AppProcess | None] = []
        restart_results: list[AppProcess | None] = []
        errors: list[BaseException] = []

        def _run(target: Any, results: list[AppProcess | None]) -> None:
            try:
                results.append(target("app"))
            except BaseException as exc:
                errors.append(exc)

        public_thread = threading.Thread(target=_run, args=(bmod.start_app_backend, public_results))
        restart_thread = threading.Thread(
            target=_run, args=(bmod._start_app_backend, restart_results)
        )
        public_thread.start()
        try:
            assert child_spawned.wait(_SPAWN_RACE_CEILING_SECS)

            restart_thread.start()
            assert await_called.wait(_SPAWN_RACE_CEILING_SECS)
        finally:
            allow_publication.set()
            joined_after = _join_started(public_thread, restart_thread)

        assert not public_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert not restart_thread.is_alive(), f"alive after {joined_after:.1f} s of joining"
        assert errors == []
        assert len(public_results) == 1
        assert restart_results == public_results
        assert bmod._processes["app"] is public_results[0]
        assert popen_calls == [703]


# ---------------------------------------------------------------------------
# Spawn body: port resolution
# ---------------------------------------------------------------------------


class TestSpawnPortResolution:
    def test_fixed_port_outside_the_allowed_range_is_refused(
        self, spawn_root: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        with caplog.at_level(logging.ERROR):
            assert bmod._start_app_backend_body("ranged", _manifest("server.py", port="1")) is None
        assert any("outside allowed range" in r.message for r in caplog.records)

    def test_fixed_port_held_by_another_app_is_refused(
        self, spawn_root: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Double-booking a port is the EADDRINUSE crash this refusal prevents."""

        taken = bmod._MIN_PORT + 4
        bmod._claim_port("incumbent", taken)
        (spawn_root / "server.py").write_text("x = 1\n")
        with caplog.at_level(logging.ERROR):
            result = bmod._start_app_backend_body(
                "latecomer", _manifest("server.py", port=str(taken))
            )
        assert result is None
        assert any("cannot start" in r.message for r in caplog.records)
        assert "latecomer" not in bmod._allocated_ports

    def test_a_non_numeric_port_falls_back_to_auto_allocation(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("fuzzy", _manifest("server.py", port="not-a-number"))
        assert bmod._MIN_PORT <= bmod._allocated_ports["fuzzy"] <= bmod._MAX_PORT


# ---------------------------------------------------------------------------
# Spawn body: adopting an existing instance on a fixed port
# ---------------------------------------------------------------------------


class TestAdoptExistingInstance:
    @pytest.fixture()
    def occupied(self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        (spawn_root / "server.py").write_text("x = 1\n")
        _install_fake_socket(monkeypatch, connect_exc=None)  # port answers => occupied
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        # The adoption path registers through the serialized transition, which is gated
        # on the app being enabled. "adoptee" is fabricated and so is not in
        # installed.json; in production start_app_backend only ever runs for an enabled
        # app. The gate itself is pinned by TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        return spawn_root

    def _run(self, port: int) -> AppProcess | None:
        return bmod._start_app_backend_body("adoptee", _manifest("server.py", port=str(port)))

    def test_a_healthy_instance_is_adopted_with_its_listening_pids(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = bmod._MIN_PORT + 6
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        _stub_listeners(
            monkeypatch,
            [
                # Pre-fork workers legitimately share the listening socket.
                bmod.platform_compat.PortListener(111, "127.0.0.1", "4"),
                bmod.platform_compat.PortListener(222, "127.0.0.1", "4"),
            ],
        )
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        ap = self._run(port)
        assert ap is not None
        assert ap.proc is None
        assert ap.healthy is True
        assert ap.adopted_pids == [111, 222]
        # Start-time identity is captured per owner so stop can refuse a
        # recycled PID later.
        assert ap.adopted_start_times == {111: "st-111", 222: "st-222"}
        assert bmod._processes["adoptee"] is ap
        assert bmod._allocated_ports["adoptee"] == port

    def test_adoption_records_only_the_probed_address_owner(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership is claimed per address, not per port.

        Two processes legally share a port on different local addresses; only
        the one covering the health-checked 127.0.0.1 was ever validated, so
        recording the other would hand stop_app_backend an unrelated process
        to signal.
        """

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(
            monkeypatch,
            [
                bmod.platform_compat.PortListener(111, "127.0.0.1", "4"),
                bmod.platform_compat.PortListener(999, "192.168.1.5", "4"),
            ],
        )
        ap = self._run(bmod._MIN_PORT + 16)
        assert ap is not None
        assert ap.adopted_pids == [111]

    def test_adoption_excludes_a_v6only_wildcard_beside_the_v4_owner(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lsof spells both wildcard binds ``*`` — only the family separates a
        v4 owner from an unrelated IPV6_V6ONLY listener sharing its port, and
        the latter never saw the 127.0.0.1 health probe."""

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(
            monkeypatch,
            [
                bmod.platform_compat.PortListener(111, "*", "4"),
                bmod.platform_compat.PortListener(999, "*", "6"),
            ],
        )
        ap = self._run(bmod._MIN_PORT + 17)
        assert ap is not None
        assert ap.adopted_pids == [111]

    def test_adoption_survives_an_audit_sink_failure(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken audit sink must not cost us a healthy running backend."""

        def _boom() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _boom)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(333, "127.0.0.1", "4")])
        ap = self._run(bmod._MIN_PORT + 7)
        assert ap is not None
        assert ap.adopted_pids == [333]

    def test_adoption_is_refused_when_no_owning_pid_can_be_recorded(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Adopting without PIDs would leave a backend we can never stop."""

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        _stub_listeners(monkeypatch, [])
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 8) is None
        assert any("cannot record owning PIDs" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_adoption_is_refused_when_only_other_addresses_listen(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No loopback-covering owner means the probed backend cannot be
        attributed — adopting the other-address listener would be adopting a
        process that never answered the health check."""

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(999, "192.168.1.5", "4")])
        assert self._run(bmod._MIN_PORT + 9) is None
        assert "adoptee" not in bmod._processes

    def test_adoption_is_refused_when_an_owner_identity_is_unreadable(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An owner that cannot be positively named can never be signalled
        later: stop and uninstall would skip it, leaving a third-party backend
        running after its trust was revoked. Refuse the adoption instead."""

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(111, "127.0.0.1", "4")])
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 20) is None
        assert any("refusing adoption" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_adoption_is_refused_when_owners_change_mid_capture(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The consistency sandwich: if the responder exits between the health
        probe and the owner capture, the lookup would attribute ownership to a
        bystander (e.g. a coexisting v6-only wildcard) — the re-read owner set
        differs, so adoption is refused instead of recording the bystander."""

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        seqs = [
            [bmod.platform_compat.PortListener(111, "127.0.0.1", "4")],
            [bmod.platform_compat.PortListener(999, "*", "6")],
        ]
        monkeypatch.setattr(bmod.platform_compat, "find_port_listeners", lambda _port: seqs.pop(0))
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 18) is None
        assert any("owners changed" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_adoption_is_refused_when_health_lapses_mid_capture(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The re-probe half of the sandwich: a backend that stops answering
        its health check while ownership is being recorded is not a stable
        adoptee — whatever the owner lookup returned may describe a corpse or
        a bystander."""

        calls = {"n": 0}

        def _urlopen(*_a: Any, **_k: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResp(200)
            raise urllib.error.URLError("gone mid-capture")

        monkeypatch.setattr(bmod, "loopback_urlopen", _urlopen)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(111, "127.0.0.1", "4")])
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 19) is None
        assert any("stopped answering" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_an_unhealthy_occupant_blocks_the_spawn_instead_of_colliding(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _refused(*_a: Any, **_k: Any) -> Any:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(bmod, "loopback_urlopen", _refused)
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 10) is None
        assert any("occupied by unhealthy process" in r.message for r in caplog.records)

    def test_an_error_status_counts_as_unhealthy(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(503))
        assert self._run(bmod._MIN_PORT + 11) is None
