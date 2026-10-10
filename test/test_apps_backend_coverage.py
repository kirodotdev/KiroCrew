"""Additional unit coverage for :mod:`kiro_crew.apps.backend`.

Complements ``test_app_backend.py`` (port allocation, spawn survival, dispatch)
and ``test_app_backend_stale_reap.py`` (pidfile reap safety) by exercising the
branches those files leave untouched:

* the adopt-an-already-healthy-instance path and its refusals,
* the per-app pip --target / npm dependency-install branches,
* the Node and ASGI dispatch branches,
* ``stop_app_backend``'s adopted-PID revalidation and SIGKILL escalation,
* the pidfile helpers' error paths and ``_proc_start_time``'s two platforms,
* the boot-time MCP + executable-resource reconcile in
  ``start_enabled_app_backends``.

Everything here is hermetic and order-independent: no real process is spawned,
no socket is bound, no network request is made, and no wall-clock duration is
asserted. ``subprocess.Popen`` / ``subprocess.run``, the ``socket`` module,
and ``loopback_urlopen`` are stubbed, and the spawn body is frozen at the
``Popen`` seam with a sentinel exception.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

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


class _StopSpawn(Exception):
    """Stands in for ``subprocess.Popen`` so the spawn body freezes there.

    Not an ``OSError``, so it is NOT swallowed by the body's Popen guard — it
    propagates out and the test inspects whatever the dispatch had built.
    """


class _FakeProc:
    """Minimal ``Popen`` stand-in: a pid plus a controllable exit status."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.wait_raises = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_raises:
            raise subprocess.TimeoutExpired(cmd="app", timeout=timeout or 0)
        return self.returncode or 0


def _fake_proc(pid: int = 4242, returncode: int | None = None) -> Any:
    """A ``Popen`` stand-in typed as ``Any`` so ``AppProcess.proc`` accepts it."""

    return _FakeProc(pid=pid, returncode=returncode)


def _join_started(*threads: threading.Thread) -> None:
    """Join every worker that was started, so none outlives its test's monkeypatches.

    A thread that was never started (because an earlier coordination assert failed)
    cannot be joined, so it is skipped; a started one is given the same bound the
    tests use for their own waits.
    """
    for thread in threads:
        if thread.ident is not None:
            thread.join(2)


class _FakeSock:
    """Socket stand-in supporting the two uses in this module: bind and connect."""

    def __init__(self, connect_exc: BaseException | None) -> None:
        self._connect_exc = connect_exc

    def __enter__(self) -> _FakeSock:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def bind(self, _addr: Any) -> None:
        return None

    def connect(self, _addr: Any) -> None:
        if self._connect_exc is not None:
            raise self._connect_exc


class _FakeResp:
    """``urlopen`` stand-in: a status code usable as a context manager."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


def _install_fake_socket(
    monkeypatch: pytest.MonkeyPatch, *, connect_exc: BaseException | None
) -> None:
    """Replace the module's ``socket`` reference with an inert stand-in.

    ``connect_exc=OSError(...)`` models "nothing is on this port" (the normal
    spawn path); ``connect_exc=None`` models an occupied port (the adopt path).
    ``bind`` always succeeds so auto-port selection never touches a real port.
    """

    monkeypatch.setattr(
        bmod,
        "socket",
        SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            socket=lambda *_a, **_k: _FakeSock(connect_exc),
        ),
    )


def _manifest(
    entry_point: str,
    *,
    port: str = "auto",
    health: str = "/health",
    backend_type: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(
            entryPoint=entry_point,
            port=port,
            healthCheck=health,
            type=backend_type,
        )
    )


def _capture_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Freeze the spawn at the limiter and capture the argv + kwargs it built."""

    seen: dict[str, Any] = {}

    def _popen(argv: Any, **kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        raise _StopSpawn()

    monkeypatch.setattr(bmod, "popen_limited", _popen)
    return seen


def _record_runs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: Any = None,
    exc: BaseException | None = None,
) -> list[list[str]]:
    """Record every run argv, optionally failing the call.

    Both entry points, because this module has two: the dependency installers go
    through ``run_limited`` (resource limits applied post-exec), while the nvm
    probe is a plain ``subprocess.run`` -- it carries no resource policy, so
    there was nothing for the limiter to deliver for it.
    """

    calls: list[list[str]] = []

    def _run(argv: Any, **_kwargs: Any) -> Any:
        calls.append(list(argv))
        if exc is not None:
            raise exc
        if result is not None:
            return result
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(bmod, "run_limited", _run)
    monkeypatch.setattr(bmod.subprocess, "run", _run)
    return calls


def _stub_listeners(monkeypatch: pytest.MonkeyPatch, listeners: list[Any]) -> None:
    """Pin the port->PID lookup the adoption path reads its owners from.

    ``find_port_listeners`` never raises and folds every failure (tool absent,
    wedged probe) into ``[]``, so an empty stub covers the unavailable case too.
    """

    monkeypatch.setattr(bmod.platform_compat, "find_port_listeners", lambda _port: listeners)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_module_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Give every test a clean process table, pidfile, and audit sink."""

    home = tmp_path / "kirocrew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(bmod, "_pidfile_path", lambda: tmp_path / "app_backends.pids.json")
    monkeypatch.setattr(bmod, "sel", lambda: MagicMock())
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        bmod._restart_attempts.clear()
        bmod._lifecycle_generation.clear()
    yield
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        bmod._restart_attempts.clear()
        bmod._lifecycle_generation.clear()


@pytest.fixture()
def spawn_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An app root wired so ``_start_app_backend_body`` runs without side effects."""

    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setattr(bmod, "app_dir", lambda _name: root)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **_kw: (list(argv), None))
    monkeypatch.setattr(bmod, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: None)
    _install_fake_socket(monkeypatch, connect_exc=OSError("connection refused"))
    return root


@pytest.fixture()
def boot_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub every collaborator ``start_enabled_app_backends`` reaches out to."""

    calls: dict[str, list[Any]] = {
        "started": [],
        "dereg_mcp": [],
        "dereg_agents": [],
        "dereg_skills": [],
        "register": [],
        "reconcile_skills": [],
    }

    def _start(name: str) -> AppProcess | None:
        calls["started"].append(name)
        return None

    monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: None)
    monkeypatch.setattr(bmod, "shipped_builtin_app_root", lambda _name: None)
    monkeypatch.setattr(bmod, "app_admission_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "start_app_backend", _start)
    monkeypatch.setattr(
        "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
    )

    def _dereg_mcp(name: str) -> int:
        calls["dereg_mcp"].append(name)
        return 1

    def _dereg_agents(name: str) -> int:
        calls["dereg_agents"].append(name)
        return 1

    def _dereg_skills(name: str) -> int:
        calls["dereg_skills"].append(name)
        return 1

    def _register(name: str) -> Any:
        calls["register"].append(name)
        return SimpleNamespace(errors=[])

    def _reconcile(name: str) -> None:
        calls["reconcile_skills"].append(name)

    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", _dereg_mcp)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_agents", _dereg_agents)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_skills", _dereg_skills)
    monkeypatch.setattr("kiro_crew.apps.bridges.register_app", _register)
    monkeypatch.setattr("kiro_crew.apps.bridges.reconcile_app_skills", _reconcile)
    return calls


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
# Node / npm binary resolution
# ---------------------------------------------------------------------------


class TestNvmResolution:
    def test_no_nvm_script_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
        assert bmod._resolve_nvm_path("node") is None

    def _nvm_dir(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """An ``NVM_DIR`` with ``nvm.sh`` present, on the POSIX branch.

        The resolver is POSIX-only: on Windows it returns None before the
        ``nvm.sh`` probe (``test_apps_backend_nvm_windows.py`` covers that
        arm). These tests exercise the shell lookup, so they pin the platform
        flag rather than letting the Windows runner skip the code under test.
        """
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        nvm = tmp_path / "nvm"
        nvm.mkdir()
        (nvm / "nvm.sh").write_text("# nvm\n")
        monkeypatch.setenv("NVM_DIR", str(nvm))
        return nvm

    def test_resolves_sibling_binary_of_the_nvm_node(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        (bin_dir / "npm").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        assert bmod._resolve_nvm_path("npm") == str(bin_dir / "npm")

    def test_missing_sibling_binary_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        assert bmod._resolve_nvm_path("npm") is None

    def test_nonzero_nvm_exit_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=1, stdout=""))
        assert bmod._resolve_nvm_path("node") is None

    def test_nvm_probe_failure_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, exc=OSError("no bash"))
        assert bmod._resolve_nvm_path("node") is None

    # Branch-outcome logging: each exit of the POSIX/nvm branch
    # names itself, so a Windows log shows whether the branch ran or was skipped.
    def test_skipped_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Pin the platform flag off: the resolver is POSIX-only and returns
        # before this probe on Windows, so the branch under test runs on the
        # Windows runner only once the guard is neutralised (sibling tests get
        # this from ``_nvm_dir``; this one points NVM_DIR at an absent dir).
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("node") is None
        assert any("skipping nvm branch" in r.getMessage() for r in caplog.records)

    def test_resolved_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        (bin_dir / "npm").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("npm") == str(bin_dir / "npm")
        assert any("resolved 'npm'" in r.getMessage() for r in caplog.records)

    def test_probe_failure_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, exc=OSError("no bash"))
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("node") is None
        assert any("shell invocation failed" in r.getMessage() for r in caplog.records)

    def test_node_and_npm_prefer_nvm_over_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_resolve_nvm_path", lambda name: f"/nvm/{name}")
        monkeypatch.setattr(
            bmod.shutil, "which", lambda _n: pytest.fail("PATH consulted despite nvm hit")
        )
        assert bmod._find_node_binary() == "/nvm/node"
        assert bmod._find_npm_binary() == "/nvm/npm"

    def test_node_and_npm_fall_back_to_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_resolve_nvm_path", lambda _name: None)
        monkeypatch.setattr(bmod.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert bmod._find_node_binary() == "/usr/bin/node"
        assert bmod._find_npm_binary() == "/usr/bin/npm"


# ---------------------------------------------------------------------------
# Entry-point heuristics
# ---------------------------------------------------------------------------


class TestEntryHeuristics:
    def test_asgi_entry_needs_both_markers(self, tmp_path: Any) -> None:
        both = tmp_path / "asgi.py"
        both.write_text("app = FastAPI()\nimport uvicorn\n")
        assert bmod._is_asgi_entry(both) is True

        only_one = tmp_path / "plain.py"
        only_one.write_text("app = FastAPI()\n")
        assert bmod._is_asgi_entry(only_one) is False

    def test_asgi_entry_unreadable_is_not_asgi(self, tmp_path: Any) -> None:
        assert bmod._is_asgi_entry(tmp_path) is False  # a directory: OSError

    def test_shebang_argv_unreadable_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        assert bmod._shebang_argv(tmp_path) == ["/bin/sh"]

    def test_shebang_argv_non_utf8_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        entry = tmp_path / "weird"
        entry.write_bytes(b"#!\xff\xfe\n")
        assert bmod._shebang_argv(entry) == ["/bin/sh"]

    def test_shebang_argv_empty_interpreter_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        entry = tmp_path / "bare"
        entry.write_bytes(b"#!\n")
        assert bmod._shebang_argv(entry) == ["/bin/sh"]

    def test_shebang_argv_keeps_the_single_kernel_argument(self, tmp_path: Any) -> None:
        entry = tmp_path / "run"
        entry.write_text("#!/usr/bin/env bash\n")
        assert bmod._shebang_argv(entry) == ["/usr/bin/env", "bash"]


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
                assert successor_reserved.wait(2)

        def _body(name: str, _manifest: Any) -> AppProcess | None:
            nonlocal body_calls
            body_calls += 1
            owner = bmod._spawn_publication_owner.get()
            assert owner is not None
            if body_calls == 1:
                first_owner.append(owner)
                assert bmod._reserve_free_port(name) == 9100
                first_reserved.set()
                assert release_first.wait(2)
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
            assert first_reserved.wait(2)
            assert bmod.stop_app_backend("app") is True

            successor_thread.start()
            assert successor_waiting.wait(2)
        finally:
            release_first.set()
            _join_started(first_thread, successor_thread)

        assert not first_thread.is_alive()
        assert not successor_thread.is_alive()
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
                assert allow_restart_publication.wait(2)
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
            assert restart_child_spawned.wait(2)
            assert bmod.stop_app_backend("app") is True

            start_thread.start()
            assert public_start_waiting.wait(2)
        finally:
            allow_restart_publication.set()
            _join_started(restart_thread, start_thread)

        assert not restart_thread.is_alive()
        assert not start_thread.is_alive()
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
            assert allow_publication.wait(2)
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
            assert child_spawned.wait(2)

            restart_thread.start()
            assert await_called.wait(2)
        finally:
            allow_publication.set()
            _join_started(public_thread, restart_thread)

        assert not public_thread.is_alive()
        assert not restart_thread.is_alive()
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
        # Adoption is gated on provenance: only a listener the gateway can attribute
        # to the spawn it recorded for this app is adopted. Every case below is about
        # WHICH owners get recorded, so the record is present here and vouches the
        # listener whatever pid the case stubs. The verdict matrix has its own class.
        bmod._write_pidfile(
            {"adoptee": {"pid": 0, "start_time": None, "port": 0, "spawn_instance": "sp-adoptee"}}
        )
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-adoptee")
        return spawn_root

    def _run(self, port: int) -> Any:
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


# ---------------------------------------------------------------------------
# Spawn body: adoption provenance
# ---------------------------------------------------------------------------


class TestAdoptionProvenance:
    """One verdict per identity case the adoption branch can meet.

    A health answer on the manifest's declared port names no code and no
    process, so on its own it lets a backend that outlived its app's uninstall
    be adopted by the next install that happens to use the same app name. The
    gateway adopts only what it can attribute to the spawn it recorded for that
    app, by the recorded leader's identity or by the spawn instance its whole
    tree carries, and refuses everything else.
    """

    @staticmethod
    def _row(**over: Any) -> dict[str, Any]:
        entry = {"pid": 4242, "start_time": "st-4242", "port": 9000, "spawn_instance": "sp-1"}
        entry.update(over)
        return entry

    def _verdict(self, owners: list[int] | None = None, app: str = "app") -> tuple[bool, str]:
        return bmod._adoption_provenance(app, [111, 222] if owners is None else owners)

    def test_the_recorded_leader_still_holding_the_port_is_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pid plus a start instant names one process for good, on every platform."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111")})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        # No instance route is needed, so this host need not be able to vouch at all.
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        attributed, reason = self._verdict([111])
        assert attributed is True
        assert "every owner [111]" in reason

    def test_a_recycled_leader_pid_is_not_ours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The recorded number came back as an unrelated process: the start instant differs."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111", spawn_instance=None)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-recycled")
        attributed, reason = self._verdict()
        assert attributed is False
        assert "no spawn instance" in reason

    def test_a_revoked_row_is_refused_even_when_the_owner_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An update/uninstall that could not confirm the row's deletion stamps it
        ``revoked``. The surviving row still names the recorded leader AND the spawn
        instance a detached survivor inherited, so without this gate the leader route
        (or the tree route) would adopt an OLD-code listener after a restart. A
        revoked row attributes nothing, regardless of how well an owner matches."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111", revoked=True)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-1")
        attributed, reason = self._verdict([111])
        assert attributed is False
        assert "revoked" in reason

    def test_every_member_of_the_recorded_spawn_is_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leader has exited; pre-fork workers of the same spawn hold the port.

        The whole tree inherits the spawn instance, so the token attributes members
        whose leader's pid names nothing live.
        """

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-1")
        attributed, reason = self._verdict()
        assert attributed is True
        assert "every owner [111, 222]" in reason

    def test_one_foreign_co_owner_refuses_the_whole_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-UID SO_REUSEPORT co-binder shares the winning dispatch tier.

        Every captured owner enters the managed set and is signalled at stop, and
        the start-time token stop re-checks was captured at the same moment, so it
        confirms the co-binder rather than excluding it. Attributing the set from
        one match would hand stop an unrelated process to terminate.
        """

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            bmod, "_env_spawn_instance", lambda pid: "sp-1" if pid == 111 else "sp-stranger"
        )
        attributed, reason = self._verdict()
        assert attributed is False
        assert "[222]" in reason
        assert "not placed by this gateway for this app" in reason

    def test_an_empty_owner_set_is_not_ours(self) -> None:
        bmod._write_pidfile({"app": self._row()})
        attributed, reason = self._verdict([])
        assert attributed is False
        assert reason == "no owning pid to attribute"

    def test_a_survivor_of_a_prior_install_of_the_same_name_is_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect this guard exists for.

        The listener is a real Kiro Crew app backend and it answers on the port
        the manifest declares, but it belongs to a spawn of the app that used
        this name before: its instance is a different one.
        """

        bmod._write_pidfile({"app": self._row(spawn_instance="sp-current")})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-previous-install")
        attributed, reason = self._verdict()
        assert attributed is False
        assert "not placed by this gateway for this app" in reason

    def test_no_recorded_spawn_is_not_ours(self) -> None:
        """Nothing was recorded, so nothing attributes the listener.

        This is the state an uninstall leaves behind: stopping the backend drops
        the app's row, so a survivor that rebinds the port meets an empty record
        on the next install's first start.
        """

        bmod._write_pidfile({"other-app": self._row()})
        attributed, reason = self._verdict()
        assert attributed is False
        assert reason == "no spawn recorded for this app"

    def test_an_unreadable_record_is_not_ours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A record that cannot be read decides nothing, so it decides against adoption."""

        def _boom() -> dict[str, Any]:
            raise OSError("pidfile unreadable")

        monkeypatch.setattr(bmod, "_read_pidfile", _boom)
        attributed, reason = self._verdict()
        assert attributed is False
        assert "unreadable" in reason

    def test_a_malformed_row_is_not_ours(self) -> None:
        bmod._write_pidfile({"app": "not-a-mapping"})
        attributed, reason = self._verdict()
        assert attributed is False
        assert reason == "no spawn recorded for this app"

    def test_a_row_with_no_instance_cannot_vouch_a_tree_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual, pinned at its current answer: a spawn recorded without a
        token has nothing to attribute a surviving member with, so the member is
        refused. Self-healing forward -- the next spawn records a token."""

        bmod._write_pidfile({"app": self._row(spawn_instance=None)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        attributed, reason = self._verdict()
        assert attributed is False
        assert "no spawn instance to vouch its tree with" in reason

    def test_a_host_that_cannot_read_an_instance_refuses_a_tree_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual, pinned at its current answer: the token read is
        ``/proc/<pid>/environ``, which exists on Linux alone. Elsewhere a
        surviving member whose leader is gone cannot be attributed, and the
        reason says so rather than reading as "not ours"."""

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        monkeypatch.setattr(
            bmod,
            "_env_spawn_instance",
            lambda _pid: pytest.fail("read on a host with no oracle"),
        )
        attributed, reason = self._verdict()
        assert attributed is False
        assert "cannot read a process's spawn instance" in reason


# ---------------------------------------------------------------------------
# Spawn body: adoption provenance, through the spawn body
# ---------------------------------------------------------------------------


class TestAdoptionProvenanceAtTheSpawnBody:
    @pytest.fixture()
    def occupied(self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        (spawn_root / "server.py").write_text("x = 1\n")
        _install_fake_socket(monkeypatch, connect_exc=None)
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(555, "127.0.0.1", "4")])
        return spawn_root

    def _run(self, port: int) -> Any:
        return bmod._start_app_backend_body("adoptee", _manifest("server.py", port=str(port)))

    def test_an_unattributed_healthy_listener_is_not_adopted(
        self, occupied: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End to end: healthy, on the declared port, and still refused.

        No record names it, so no process table entry is created -- the app does
        not start on a listener the gateway does not own. The port claim taken
        before the branch is released by the caller's spawn bookkeeping, as it is
        on every other refusal here.
        """

        with caplog.at_level(logging.WARNING):
            refused = self._run(bmod._MIN_PORT + 21)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert any("refusing to adopt" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_the_refusal_is_audited_as_unattributed(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit trail records the verdict reached, not the verdict attempted."""

        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        assert isinstance(self._run(bmod._MIN_PORT + 22), bmod.AdoptionRefused)
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert "refused_unattributed" in outcomes
        assert "adopted" not in outcomes

    def test_an_attributed_listener_is_adopted_and_audited(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bmod._write_pidfile(
            {"adoptee": {"pid": 555, "start_time": "st-555", "port": 0, "spawn_instance": "sp-a"}}
        )
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        ap = self._run(bmod._MIN_PORT + 23)
        assert ap is not None
        assert ap.adopted_pids == [555]
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert "adopted" in outcomes
        assert "refused_unattributed" not in outcomes

    def test_the_refusal_returns_a_reason_naming_the_port(self, occupied: Any) -> None:
        """The body hands its refusal back instead of a bare None, so the caller
        has something to show the user: the port, and the step to take."""

        port = bmod._MIN_PORT + 25
        refused = self._run(port)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert refused.port == port
        assert refused.reason
        assert f"port {port}" in refused.notice
        assert f"Close the program using port {port}" in refused.notice

    def test_a_quarantined_app_is_refused_as_quarantined(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a listener the record attributes is refused while the app is
        quarantined: it may be the old-code survivor an update replaced."""

        bmod._write_pidfile(
            {"adoptee": {"pid": 555, "start_time": "st-555", "port": 0, "spawn_instance": "sp-a"}}
        )
        monkeypatch.setattr(bmod, "is_backend_quarantined", lambda _name: True)
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        port = bmod._MIN_PORT + 26
        refused = self._run(port)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert refused.reason == "quarantined"
        assert f"port {port}" in refused.notice
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert outcomes == ["refused_quarantined"]
        assert "adoptee" not in bmod._processes

    def test_the_refusal_reaches_the_caller_as_a_backend_notice(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the public entry point the refusal is still None to every
        existing caller, and its notice is what the app's responses carry."""

        port = bmod._MIN_PORT + 27
        monkeypatch.setattr(
            bmod, "get_app_manifest", lambda _name: _manifest("server.py", port=str(port))
        )
        bmod.clear_backend_notice("adoptee")
        try:
            assert bmod.start_app_backend("adoptee") is None
            notice = bmod.backend_notice("adoptee")
            assert notice is not None
            assert f"port {port}" in notice
            assert "adoptee" not in bmod._processes
        finally:
            bmod.clear_backend_notice("adoptee")

    def test_a_free_port_never_consults_provenance(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is listening, so there is no listener to attribute and the
        ordinary spawn path is unchanged."""

        (spawn_root / "server.py").write_text("x = 1\n")
        _capture_popen(monkeypatch)
        monkeypatch.setattr(
            bmod,
            "_adoption_provenance",
            lambda *_a, **_k: pytest.fail("provenance consulted with no listener to judge"),
        )
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "freeport", _manifest("server.py", port=str(bmod._MIN_PORT + 24))
            )


# ---------------------------------------------------------------------------
# Recovery: re-binding an adopted backend's owners
# ---------------------------------------------------------------------------


class TestRebindAttributesTheNewOwners:
    """Recovery re-captures owners, so it re-asks whose they are.

    This path runs after an adopted backend stops answering. The set it captures
    can be a different population from the one adoption attributed, so inheriting
    that verdict would let an unrelated listener answering the declared health path
    be written into the owner record and promoted -- the same shape a start refuses,
    reached through recovery.
    """

    @pytest.fixture()
    def adopted(self, monkeypatch: pytest.MonkeyPatch) -> AppProcess:
        ap = AppProcess(
            app_name="rebindee",
            port=bmod._MIN_PORT + 30,
            pid=0,
            proc=None,
            healthy=False,
            started_at=0.0,
            log_path="/dev/null",
            adopted_pids=[111],
            adopted_start_times={111: "st-111"},
            gateway_started=True,
            admitted_builtin=False,
        )
        with bmod._lock:
            bmod._processes["rebindee"] = ap
        monkeypatch.setattr(
            bmod, "_capture_adopted_owners", lambda *_a, **_k: ([777], {777: "st-777"})
        )
        return ap

    def test_an_unattributed_replacement_is_not_bound(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The recorded owners survive untouched, so stop still aims where it did."""

        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (False, "stranger"))
        with caplog.at_level(logging.WARNING):
            assert bmod._rebind_adopted_owners(adopted, "/health") is False
        assert any("refusing to re-bind" in r.message for r in caplog.records)
        assert adopted.adopted_pids == [111]
        assert adopted.adopted_start_times == {111: "st-111"}

    def test_the_refusal_is_audited(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (False, "stranger"))
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        assert bmod._rebind_adopted_owners(adopted, "/health") is False
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert outcomes == ["refused_unattributed"]

    def test_an_attributed_replacement_is_bound(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (True, "ours"))
        assert bmod._rebind_adopted_owners(adopted, "/health") is True
        assert adopted.adopted_pids == [777]
        assert adopted.adopted_start_times == {777: "st-777"}

    def test_the_captured_set_is_what_gets_judged(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the set already on the record -- judging that would re-confirm the
        owners the adoption already cleared and never look at the newcomers."""

        seen: list[tuple[str, list[int]]] = []

        def _judge(app_name: str, owners: list[int]) -> tuple[bool, str]:
            seen.append((app_name, list(owners)))
            return True, "ours"

        monkeypatch.setattr(bmod, "_adoption_provenance", _judge)
        assert bmod._rebind_adopted_owners(adopted, "/health") is True
        assert seen == [("rebindee", [777])]


# ---------------------------------------------------------------------------
# A refused stop leaves the retry able to attribute the listener


class TestARefusedStopRestoresTheProvenanceRecord:
    """The stop drops the app's pidfile row inside its lifecycle transition,
    before it signals anything, and it can then refuse and restore tracking.

    That row is where an adopted backend's provenance is read from, so a refusal
    that leaves it dropped makes the retry's re-bind refuse on "no spawn recorded"
    for good: a withdrawn trust ceiling could never converge on a listener that
    keeps serving, and across a restart the listener stops being managed at all.
    Whatever the transition removed is put back on every refusal.
    """

    @pytest.fixture(autouse=True)
    def _fast_wait(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

    ROW = {"pid": 4242, "start_time": "st-4242", "port": 9000, "spawn_instance": "sp-keep"}

    def _track(self, **kwargs: Any) -> AppProcess:
        ap = AppProcess(app_name="ext", port=bmod._MIN_PORT + 13, pid=0, proc=None, **kwargs)
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        return ap

    def test_a_refusal_with_no_recorded_pids_puts_the_row_back(self) -> None:
        self._track(healthy=True)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_a_refusal_whose_restore_write_fails_quarantines_the_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: when the refusal's durable restore does NOT persist (ENOSPC/EDQUOT
        swallows the write), the row the retry reads after a gateway restart is gone
        and adoption provenance fails closed with no row -- so a backend under a
        withdrawn ceiling could keep serving un-trusted code with nothing left to stop
        it by. The refusal must then quarantine the name: the in-memory backstop
        refuses any later adoption of this generation even when its own marker write
        also fails, rather than silently stranding tracking.
        """
        self._track(healthy=True)
        bmod._quarantined_backends.discard("ext")
        # The restore does not persist (ENOSPC/EDQUOT swallowed the write), so the
        # durable row the retry reads after a restart is gone.
        monkeypatch.setattr(bmod, "_restore_app_pid", lambda *_a, **_k: False)
        try:
            assert bmod.stop_app_backend("ext") is False
            # The durable row did not land, but the fail-closed backstop holds.
            assert bmod.is_backend_quarantined("ext") is True
        finally:
            bmod._quarantined_backends.discard("ext")

    def test_a_refusal_on_a_replacement_still_serving_puts_the_row_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda *_a: True)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_a, **_k: bmod.HealthProbeOutcome.answered(200)
        )
        assert bmod.stop_app_backend("ext", _retry_if_serving="/health") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_a_refusal_on_a_descendant_still_serving_puts_the_row_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exited-root descendant-serving refusal restores the row too.

        A launcher root exited while a forked worker still answers the declared
        port, and a withdrawn trust ceiling calls stop with ``_retry_if_serving``.
        The drain finds the survivor (``_drain_exited_root_tree`` -> False), the
        ceiling refuses -- and that refusal must restore the row it dropped, or the
        un-trusted listener it declined to stop becomes permanently unattributable:
        ``_adoption_provenance`` answers "no spawn recorded" and the reap has no
        pid/start_time to census it from.
        """

        proc = _fake_proc(pid=4242, returncode=-15)  # a spawned root that has exited
        ap = AppProcess(
            app_name="ext",
            port=bmod._MIN_PORT + 13,
            pid=4242,
            pid_start_time="st-4242",
            proc=proc,
            healthy=True,
        )
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        # The drain reports a survivor, so the ceiling refuses outright.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: False)
        assert bmod.stop_app_backend("ext", _retry_if_serving="/health") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_an_ordinary_stop_tolerating_a_survivor_keeps_the_recovery_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: an ORDINARY stop (no withdrawn ceiling, ``_retry_if_serving``
        is None) of a tracked root whose descendant outlived it REPORTS SUCCESS but
        must KEEP the pidfile row.

        A plain disable (or the stop half of a rollback-capable update) tolerates a
        surviving port-holder, but the row is the only handle a later re-enable has
        to re-attribute that listener. Dropping it here -- as the code did before
        this fix -- left the survivor permanently unattributable:
        ``_adoption_provenance`` fails closed with no spawn recorded and the reaper
        has no row to census it from, so the enabled app stays unreachable with no
        self-correcting path. In-memory tracking is still popped (the backend we
        spawned is gone); only the recovery row survives.
        """

        proc = _fake_proc(pid=4242, returncode=-15)  # a spawned root that has exited
        ap = AppProcess(
            app_name="ext",
            port=bmod._MIN_PORT + 13,
            pid=4242,
            pid_start_time="st-4242",
            proc=proc,
            healthy=True,
        )
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        # The drain reports a survivor; with NO _retry_if_serving the stop tolerates
        # it and returns success -- but must retain the recovery row.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: False)
        assert bmod.stop_app_backend("ext") is True
        assert bmod._read_pidfile().get("ext") == self.ROW
        # In-memory tracking is gone -- only the recovery row persists.
        with bmod._lock:
            assert "ext" not in bmod._processes

    def test_a_successful_stop_leaves_the_row_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restore undoes a refusal, never a stop that worked -- a row that
        outlived its backend would attribute a later listener to a dead spawn."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda *_a: True)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        assert bmod.stop_app_backend("ext") is True
        assert "ext" not in bmod._read_pidfile()

    def test_a_newer_spawn_keeps_its_own_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A restore must not overwrite an identity recorded after the removal:
        that would aim the stale-reap and provenance at a process that is gone."""

        newer = {"pid": 5555, "start_time": "st-5555", "port": 9001, "spawn_instance": "sp-new"}
        self._track(healthy=True)
        real_forget = bmod._forget_app_pid

        def _forget_then_respawn(app_name: str) -> dict[str, Any] | None:
            removed = real_forget(app_name)
            bmod._write_pidfile({app_name: dict(newer)})
            return removed

        monkeypatch.setattr(bmod, "_forget_app_pid", _forget_then_respawn)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == newer

    def test_an_untracked_stop_leaves_the_recovery_row_intact(self) -> None:
        """A stop for an app this process never tracked (nothing in ``_processes``)
        must not discard the recorded row. That row is what a later start attributes
        an adopted survivor against; forgetting it here on a stop that knew nothing
        of the backend would strand the survivor, unadoptable."""

        bmod._write_pidfile({"ext": dict(self.ROW)})
        with bmod._lock:
            bmod._processes.pop("ext", None)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_forget_backend_provenance_drops_the_row_and_hands_it_back(self) -> None:
        """An UPDATE invalidates the pre-update row before the restart, so the
        restart cannot re-adopt a survivor running the OLD code as the new version.
        The row is handed back so the update's ROLLBACK can restore it — on a failed
        update the old backend is still the app's and must stay attributable."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        handed_back = bmod.forget_backend_provenance("upd")
        assert handed_back == self.ROW
        assert "upd" not in bmod._read_pidfile()
        # Rollback restores exactly what was dropped.
        bmod._restore_app_pid("upd", handed_back)
        assert bmod._read_pidfile().get("upd") == self.ROW

    def test_forget_backend_provenance_raises_when_the_delete_is_not_confirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swallowed pidfile write (ENOSPC/EDQUOT: the row leaves memory but stays
        on disk) must NOT be reported as a successful delete — a caller that then
        restarts would adopt the stale-row survivor. forget_backend_provenance
        confirms the write landed and raises PidfileDeleteFailed otherwise, so the
        update/uninstall caller aborts the restart."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        # Simulate the write failing to land (atomic_write swallows OSError -> False).
        monkeypatch.setattr(bmod, "_write_pidfile", lambda data: False)
        with pytest.raises(bmod.PidfileDeleteFailed):
            bmod.forget_backend_provenance("upd")

    def test_an_unconfirmed_delete_stamps_the_surviving_row_revoked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the delete does not land, the stale row stays on disk and the
        in-memory quarantine the caller sets is lost on restart -- a SIGTERM-survivor
        forked into the reaped group then makes the reap RETAIN the row, so nothing
        would refuse the old-code survivor after a restart. forget stamps a DURABLE
        ``revoked`` flag onto the surviving row (best-effort: the same disk-full may
        defeat it, which the raise still backstops) so _adoption_provenance refuses
        it across the restart. Here the delete write fails but the revoke stamp
        lands, modelling a disk that frees just enough for the smaller write."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        real_write = bmod._write_pidfile
        calls = {"n": 0}

        def _write(data: dict[str, Any]) -> bool:
            # The first write (the delete) fails; the follow-up revoke stamp lands.
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            return real_write(data)

        monkeypatch.setattr(bmod, "_write_pidfile", _write)
        with pytest.raises(bmod.PidfileDeleteFailed):
            bmod.forget_backend_provenance("upd")
        surviving = bmod._read_pidfile().get("upd")
        assert (
            surviving is not None and surviving.get("revoked") is True
        ), "an unconfirmed delete must leave the row stamped revoked so adoption refuses it"
        attributed, reason = bmod._adoption_provenance("upd", [self.ROW["pid"]])
        assert attributed is False and "revoked" in reason

    def test_forget_backend_provenance_is_a_noop_for_an_absent_row(self) -> None:
        """No row to drop is not a failure: it returns None without raising, so an
        app that never recorded a spawn does not block its own update."""

        with bmod._lock:
            bmod._processes.pop("absent", None)
        bmod._write_pidfile({})
        assert bmod.forget_backend_provenance("absent") is None

    def test_forget_backend_provenance_raises_when_the_store_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read failure must not masquerade as 'no row'. _read_pidfile swallows an
        OSError and returns {}, so data.pop would yield None — indistinguishable from
        an absent row — and the caller would report a confirmed delete that never
        happened and restart onto the survivor the stale row still attributes.
        forget_backend_provenance reads strictly and raises PidfileDeleteFailed."""

        bmod._write_pidfile({"upd": dict(self.ROW)})

        def _boom() -> dict[str, Any]:
            raise OSError("disk went away")

        monkeypatch.setattr(bmod, "_read_pidfile_strict", _boom)
        with pytest.raises(bmod.PidfileDeleteFailed):
            bmod.forget_backend_provenance("upd")

    def test_read_pidfile_strict_raises_on_a_corrupt_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The strict reader surfaces a corrupt file as OSError (so the confirmed-delete
        path aborts) while a MISSING file is still an empty dict — there is simply no
        row to delete. The lenient _read_pidfile keeps masking both as {}."""

        pf = tmp_path / "app_backends.pids.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        # Missing file: both readers agree it is empty.
        assert bmod._read_pidfile_strict() == {}
        # Corrupt file: lenient returns {}, strict raises OSError.
        pf.write_text("{not json")
        assert bmod._read_pidfile() == {}
        with pytest.raises(OSError):
            bmod._read_pidfile_strict()

    def test_revoke_backend_provenance_stamps_confirms_and_hands_back_the_old_row(
        self,
    ) -> None:
        """GPT F2: the DURABLE pre-destruction guard. revoke stamps ``revoked`` on the
        row, confirms it by re-reading, and hands back the PRE-revoke row so an
        update's rollback can restore the un-revoked record. The live row is now
        revoked, so _adoption_provenance refuses it even after a restart (the stamp
        rides on disk, unlike the in-memory quarantine)."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        handed_back = bmod.revoke_backend_provenance("upd")
        assert handed_back == self.ROW, "the pre-revoke row is returned for rollback"
        live = bmod._read_pidfile().get("upd")
        assert live is not None and live.get("revoked") is True
        attributed, reason = bmod._adoption_provenance("upd", [self.ROW["pid"]])
        assert attributed is False and "revoked" in reason
        # Rollback restores the un-revoked row the caller held.
        with bmod._lock:
            bmod._processes.pop("upd", None)
        bmod._write_pidfile({})
        bmod._restore_app_pid("upd", handed_back)
        assert bmod._read_pidfile().get("upd") == self.ROW

    def test_revoke_backend_provenance_raises_when_the_stamp_cannot_be_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F2 core: a swallowed write (ENOSPC/EDQUOT) must NOT be reported as a
        durable revoke. The in-memory quarantine does not survive a restart, so a
        caller that committed the destructive step on an unconfirmed revoke would let
        a same-name reinstall adopt an old-code survivor. revoke raises
        PidfileRevokeFailed so the caller REFUSES the destructive work."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        monkeypatch.setattr(bmod, "_write_pidfile", lambda data: False)
        with pytest.raises(bmod.PidfileRevokeFailed):
            bmod.revoke_backend_provenance("upd")

    def test_revoke_backend_provenance_raises_when_the_stamp_does_not_read_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that reports success but whose stamp does not re-read as revoked (a
        short/partial write) must still fail closed. revoke re-reads to confirm the
        ``revoked`` flag reached disk and raises PidfileRevokeFailed otherwise."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        # Write "succeeds" but the row that reads back is NOT revoked.
        monkeypatch.setattr(bmod, "_write_pidfile", lambda data: True)
        monkeypatch.setattr(bmod, "_read_pidfile_strict", lambda: {"upd": dict(self.ROW)})
        with pytest.raises(bmod.PidfileRevokeFailed):
            bmod.revoke_backend_provenance("upd")

    def test_revoke_backend_provenance_is_a_noop_for_an_absent_row(self) -> None:
        """No row vouches for nothing at revoke time — but leaving the name UNwritten
        is unsafe: a stop racing from another process can restore a row afterwards.
        So revoke writes a BARE revoked tombstone and returns None (nothing to restore
        on rollback). The tombstone makes a later restore a no-op and keeps adoption
        refused."""

        with bmod._lock:
            bmod._processes.pop("absent", None)
        bmod._write_pidfile({})
        assert bmod.revoke_backend_provenance("absent") is None
        live = bmod._read_pidfile().get("absent")
        assert live is not None and live.get("revoked") is True and live.get("tombstone") is True
        # A racing stop's restore of a (would-be) row cannot overwrite the tombstone.
        bmod._restore_app_pid(
            "absent", {"pid": 5, "start_time": "s", "port": 9, "spawn_instance": "i"}
        )
        after = bmod._read_pidfile().get("absent")
        assert (
            after is not None and after.get("revoked") is True
        ), "tombstone must survive a restore"
        attributed, reason = bmod._adoption_provenance("absent", [5])
        assert attributed is False and "revoked" in reason

    def test_unrevoke_deletes_a_bare_tombstone_on_rollback(self) -> None:
        """A failed uninstall that wrote a BARE tombstone (no row at revoke time) must
        delete it on rollback — there was no real row to restore, and leaving the
        tombstone would refuse the still-installed app's next spawn until it records
        its own row."""

        with bmod._lock:
            bmod._processes.pop("absent2", None)
        bmod._write_pidfile({})
        assert bmod.revoke_backend_provenance("absent2") is None
        assert bmod._read_pidfile().get("absent2", {}).get("tombstone") is True
        bmod._unrevoke_app_pid("absent2", None)
        assert (
            "absent2" not in bmod._read_pidfile()
        ), "the bare tombstone must be deleted on rollback"

    def test_unrevoke_restores_the_preserved_row_over_a_bare_tombstone(self) -> None:
        """A failed update whose stop forgot the tracked row before the revoke ran must
        still recover the survivor on rollback.

        The update's stop forgets the spawn row; the pre-replacement revoke then finds
        no row and writes a BARE pid-less tombstone (returning None). But the rollback
        holds the pre-stop row as ``preserved_row`` and passes it to _unrevoke_app_pid
        as a real ``original``. Because the tombstone has no pid/start_time/
        spawn_instance, the same-spawn check can never match it; without the bare-
        tombstone branch the recovery row would be lost and the still-installed app's
        surviving backend would be refused by adoption. The row must be put back."""

        with bmod._lock:
            bmod._processes.pop("upd", None)
        # Stop forgot the row, then the revoke stamped a bare tombstone over the gap.
        bmod._write_pidfile({"upd": {"revoked": True, "tombstone": True}})
        bmod._unrevoke_app_pid("upd", dict(self.ROW))
        restored = bmod._read_pidfile().get("upd")
        assert restored is not None, "the preserved row must be restored on rollback"
        assert restored.get("pid") == self.ROW["pid"]
        assert restored.get("spawn_instance") == self.ROW["spawn_instance"]
        assert not restored.get("revoked"), "the restored row must not carry the stamp"
        assert not restored.get("tombstone")
        # The restored row carries no stamp, so adoption does not refuse it for the
        # ``revoked`` reason.
        _, reason = bmod._adoption_provenance("upd", [self.ROW["pid"]])
        assert "revoked" not in reason

    def test_unrevoke_leaves_a_successors_real_row_over_a_tombstone_restore(self) -> None:
        """The bare-tombstone restore must never clobber a genuine successor. If a fresh
        spawn recorded its own real (non-tombstone) row between the revoke and the
        rollback, _unrevoke_app_pid leaves it untouched — a real spawn records a pid,
        never a tombstone, so it falls through neither the tombstone branch nor the
        same-spawn branch for a different ``original``."""

        successor = {"pid": 7777, "start_time": "st-7777", "port": 9001, "spawn_instance": "sp-new"}
        with bmod._lock:
            bmod._processes.pop("upd", None)
        bmod._write_pidfile({"upd": dict(successor)})
        bmod._unrevoke_app_pid("upd", dict(self.ROW))
        live = bmod._read_pidfile().get("upd")
        assert (
            live is not None and live.get("pid") == successor["pid"]
        ), "a successor's real spawn row must be left untouched"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits")
    def test_the_pidfile_lock_sidecar_is_owner_only(self) -> None:
        """A fresh boot must not leave the interprocess lock sidecar readable by
        other users: it lives in the owner-only data home, so it is created 0o600,
        with no group or other bits set."""

        import stat

        prior_umask = os.umask(0o022)
        try:
            with bmod._pidfile_lock:
                lock = bmod._pidfile_lock._lock_path()
                assert lock.exists(), "the lock sidecar is created on acquisition"
                mode = stat.S_IMODE(lock.stat().st_mode)
                assert mode & 0o077 == 0, f"lock sidecar is group/other-accessible: {oct(mode)}"
        finally:
            os.umask(prior_umask)

    def test_revoke_backend_provenance_is_idempotent_on_an_already_revoked_row(
        self,
    ) -> None:
        """A retry after a landed-but-unconfirmed stamp must not fail: an already
        ``revoked`` row is the confirmed state we want, so revoke returns it."""

        bmod._write_pidfile({"upd": {**self.ROW, "revoked": True}})
        result = bmod.revoke_backend_provenance("upd")
        assert result is not None and result.get("revoked") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX fcntl.flock probe")
    def test_the_pidfile_guard_takes_an_interprocess_flock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GPT F2: the pidfile read-modify-write is serialized across PROCESSES, not
        just threads. ``_pidfile_lock`` is now a composite guard that, inside its
        block, holds an exclusive advisory flock on a sidecar lockfile so the offline
        CLI uninstall cannot interleave its revoke with the gateway's spawn record.
        The flock is best-effort: it never weakens the in-process guarantee."""
        import os as _os

        monkeypatch.setattr(bmod, "config_dir", lambda: tmp_path)
        lock_path = tmp_path / "app_backends.pids.lock"
        with bmod._pidfile_lock:
            # The sidecar lockfile exists and this process holds an exclusive flock on
            # it: a second NON-BLOCKING exclusive acquire from another fd must fail.
            assert lock_path.is_file()
            import fcntl

            other = _os.open(str(lock_path), _os.O_RDWR)
            try:
                raised = False
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(other, fcntl.LOCK_UN)
                except OSError:
                    raised = True
                assert raised, "the guard must hold the sidecar flock exclusively inside its block"
            finally:
                _os.close(other)
        # Released outside the block: a fresh acquire now succeeds.
        after = _os.open(str(lock_path), _os.O_RDWR)
        try:
            import fcntl

            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(after, fcntl.LOCK_UN)
        finally:
            _os.close(after)


class TestBackendQuarantine:
    """GPT F1: an update whose spawn-record deletion could not be CONFIRMED
    (ENOSPC/EDQUOT) leaves a stale row that vouches for an OLD-code survivor, so a
    later enable would adopt it as the new version. The quarantine blocks that later
    start until the record is confirmed gone.
    """

    @pytest.fixture(autouse=True)
    def _clear_memory_quarantine(self):
        # The in-memory backstop is module-global; isolate each test from the others.
        bmod._quarantined_backends.clear()
        yield
        bmod._quarantined_backends.clear()

    def test_quarantine_set_read_and_clear_roundtrip(self, tmp_path: Path, monkeypatch) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        assert bmod.is_backend_quarantined("ext") is False
        bmod.quarantine_backend("ext", "update could not confirm delete")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.clear_backend_quarantine("ext")
        assert bmod.is_backend_quarantined("ext") is False

    def test_clear_is_a_noop_when_not_quarantined(self, tmp_path: Path, monkeypatch) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod.clear_backend_quarantine("never")  # must not raise
        assert bmod.is_backend_quarantined("never") is False

    def test_a_present_but_corrupt_quarantine_file_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F2: a marker that EXISTS but cannot be parsed must be treated as
        quarantined. Reading corrupt JSON as empty would silently defeat the gate and
        let a retained spawn row adopt an old-code survivor."""
        qf = tmp_path / "app_backends.quarantine.json"
        qf.write_text("{not json")
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        assert bmod.is_backend_quarantined("ext") is True

    def test_a_confirmed_forget_clears_the_quarantine(self, tmp_path: Path, monkeypatch) -> None:
        """Once the stale row the quarantine guarded against is confirmed deleted, the
        hold is lifted so the next start/enable can proceed."""
        pf = tmp_path / "app_backends.pids.json"
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._write_pidfile({"ext": {"pid": 7, "start_time": "s", "port": 9000}})
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.forget_backend_provenance("ext")
        assert bmod.is_backend_quarantined("ext") is False

    def test_a_fresh_spawn_record_clears_the_quarantine(self, tmp_path: Path, monkeypatch) -> None:
        """A fresh spawn we launched owns the row, overwriting the stale one, so the
        hold is lifted — a fresh row vouches for the new backend, not the old survivor."""
        pf = tmp_path / "app_backends.pids.json"
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "fresh-st")
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod._record_app_pid("ext", 4242, 9000, spawn_instance="sp-new")
        assert bmod.is_backend_quarantined("ext") is False

    def test_quarantine_holds_in_memory_when_the_marker_write_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F1: the on-disk marker write fails under the SAME ENOSPC that triggers
        the quarantine, so a disk-only marker is absent exactly when needed. The
        in-memory backstop must still refuse adoption for the running generation."""
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")

        def _enospc(*_a, **_k):
            raise OSError("[Errno 28] No space left on device")

        monkeypatch.setattr(bmod, "atomic_write", _enospc)
        # The marker write fails, but quarantine_backend still records in memory...
        assert bmod.quarantine_backend("ext", "update could not confirm delete") is True
        assert not qf.exists()  # nothing landed on disk
        # ...and the guard reads CLOSED from the in-memory backstop.
        assert bmod.is_backend_quarantined("ext") is True
        bmod._quarantined_backends.discard("ext")  # cleanup

    def test_clear_lifts_both_the_memory_and_disk_quarantine(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.clear_backend_quarantine("ext")
        assert bmod.is_backend_quarantined("ext") is False
        assert "ext" not in bmod._quarantined_backends

    def test_a_present_but_unreadable_marker_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F2: a quarantine marker that EXISTS but cannot be read (EACCES/EIO/fd
        exhaustion/corruption) must be treated as quarantined -- reading it as empty
        would let a retained spawn row adopt an old-code survivor past the gate."""
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        qf.write_text('{"ext": "stale"}')  # the marker is PRESENT

        import builtins

        real_open = builtins.open

        def _deny(path, *a, **k):
            if str(path) == str(qf):
                raise PermissionError("[Errno 13] Permission denied")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", _deny)
        # _read_quarantine raises QuarantineUnreadable, and the gate fails CLOSED.
        assert bmod.is_backend_quarantined("ext") is True

    def test_a_truly_absent_marker_reads_as_not_quarantined(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The complement: a MISSING marker (FileNotFoundError -- the ENOSPC
        write-never-landed case) is not quarantined, so the gate does not block
        every app forever when no quarantine was ever written."""
        qf = tmp_path / "does_not_exist.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        assert bmod.is_backend_quarantined("ext") is False


# ---------------------------------------------------------------------------
# Spawn body: dependency installation
# ---------------------------------------------------------------------------


class TestDependencyInstall:
    def test_requirements_txt_provisions_the_deps_dir_without_a_venv(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provisioning is a single `pip install --target` into a staging dir
        that is swapped live on success (with the requirements hash stamped).

        Never `-m venv`: a packaged install's bundled interpreter ships pip
        but no ensurepip, so venv creation dies after building the directory
        skeleton - which the venv-first interpreter policy then prefers while
        it holds none of the app's dependencies."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        assert not any("venv" in argv for argv in runs), runs
        pip_argv = next(argv for argv in runs if "install" in argv)
        target_idx = pip_argv.index("--target")
        # staging names are UNIQUE per transaction (pid+nonce suffix), so a
        # data/ swap can never redirect a fixed name's cleanup or fill
        _target = pip_argv[target_idx + 1]
        assert _target.startswith(str(spawn_root / "data" / bmod._DEPS_STAGING_NAME)), pip_argv
        # Success swapped the staging dir live and stamped the requirements
        # hash, so the next start with an unchanged file can skip pip.
        deps_dir = app_deps_dir(spawn_root)
        assert deps_dir.is_dir()
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))
        assert (deps_dir / bmod._DEPS_STAMP_NAME).read_text() == bmod._deps_digest(b"requests\n")

    def test_an_out_of_root_requirements_symlink_is_refused_without_reading_it(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app dir is app-writable, so requirements.txt can be a planted
        symlink to a file outside the app root. Provisioning must refuse it:
        hashing the target's bytes would turn the on-disk stamp into a
        sha256 content oracle for that file, and pip would install from a
        path the app cannot otherwise reach. The refusal is surfaced as a
        provisioning error, not swallowed."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        outside = tmp_path / "outside-secret.txt"
        outside.write_bytes(b"host-secret==1.0\n")
        (spawn_root / "server.py").write_text("x = 1\n")
        try:
            _os.symlink(outside, spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        # No pip ran, no deps dir was created, and no stamp exists anywhere
        # that could disclose a digest of the outside file's bytes.
        assert not any("install" in argv for argv in runs), runs
        from kiro_crew.apps.interpreter import app_deps_dir

        assert not app_deps_dir(spawn_root).exists()
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))

    def test_an_in_tree_requirements_symlink_provisions_normally(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """requirements.txt -> requirements/prod.txt is legitimate app
        layout: a link whose strict resolution stays inside the app root is
        accepted, read via its resolved target (itself no-follow-bound), and
        provisions normally."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "server.py").write_text("x = 1\n")
        reqs = spawn_root / "requirements"
        reqs.mkdir()
        (reqs / "prod.txt").write_bytes(b"requests\n")
        try:
            _os.symlink(
                _os.path.join("requirements", "prod.txt"),
                spawn_root / "requirements.txt",
            )
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-inlink", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_pip_runs_from_the_app_root_on_the_apps_own_requirements(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip runs with cwd=app root, so a relative reference (`-e ./lib`)
        resolves inside the app dir rather than the gateway's working
        directory - and for VOLATILE requirements (the only kind that can
        carry file references) -r points at the app's OWN requirements.txt,
        so a nested include resolves beside it (a relocated copy would
        break includes)."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "base.txt").write_bytes(b"requests\n")
        (spawn_root / "requirements.txt").write_bytes(b"-r base.txt\n")
        _record_runs(monkeypatch)
        seen: dict[str, Any] = {}

        def _spy_run(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                seen["cwd"] = kwargs.get("cwd")
                seen["r_path"] = argv[list(argv).index("-r") + 1]
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _spy_run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        assert seen["cwd"] == str(spawn_root), seen
        assert seen["r_path"] == str(spawn_root / "requirements.txt"), seen

    def test_an_editable_install_is_retained_and_swapped_live(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editable installs materialise as __editable__*.pth hooks, which
        the deps_boot shim processes via site.addsitedir - so provisioning
        RETAINS them and swaps the tree live (the refusal that predated the
        shim is gone)."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"-e ./lib\n")
        _record_runs(monkeypatch)

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                staging = argv[argv.index("--target") + 1]
                (__import__("pathlib").Path(staging) / "__editable__.lib-1.0.pth").write_text(
                    "hook\n"
                )
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-editable", _manifest("server.py"))
        deps_dir = app_deps_dir(spawn_root)
        assert (deps_dir / "__editable__.lib-1.0.pth").is_file()
        log_path = spawn_root / "data" / "logs" / "backend.log"
        if log_path.exists():
            assert "editable requirements" not in log_path.read_text()

    def test_volatile_requirement_forms_disable_the_stamp(self) -> None:
        """Any line whose resolution can change while the line does not
        (file refs attached or spaced, editables, local paths, VCS/URL,
        direct references) must disable stamp reuse - the digest cannot
        prove the resolved set unchanged for them."""
        vol = bmod._requirements_volatile
        assert vol(b"-rbase.txt\n")
        assert vol(b"-r base.txt\n")
        assert vol(b"-c pins.txt\n")
        assert vol(b"--requirement=base.txt\n")
        assert vol(b"-e ./lib\n")
        assert vol(b"./lib\n")
        assert vol(b"~/wheels/pkg.whl\n")
        assert vol(b"pkg @ https://host/pkg.whl\n")
        assert vol(b"git+https://host/repo.git#egg=pkg\n")
        assert vol(b"wheels/pkg.whl\n")  # bare relative path, no ./ prefix
        assert vol(b"vendor.whl\n")  # bare archive FILENAME, no separator
        assert vol(b"-f wheelhouse\n")  # find-links: local wheel content can change
        assert vol(b"--find-links wheelhouse\n")
        assert vol(b"--no-index\n")  # resolution-location options
        assert vol(b"Vendor-1.0.TAR.GZ\n")  # case-insensitive suffix
        assert not vol(b"requests==2.31.0\n")  # index spec stays stampable
        assert vol(b"wheels\\pkg.whl\n")
        assert not vol(b"requests==2.32.0\n# -r not-a-directive in a comment\n")
        assert not vol(b"")

    def test_deps_boot_pth_imports_resolve_deps_first(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A .pth import line executes DURING site processing and caches its
        resolution in sys.modules - so the deps dir must already sit at its
        final front position when addsitedir runs. A gateway copy of a
        colliding name earlier on sys.path must NOT win (no later reordering
        can evict a wrongly cached module)."""
        from kiro_crew.apps import deps_boot

        gateway = tmp_path / "gateway"
        gateway.mkdir()
        (gateway / "collide_probe_7901.py").write_text("MARKER = 'gateway'\n")
        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "collide_probe_7901.py").write_text("MARKER = 'dep'\n")
        pth_proof = tmp_path / "pth_proof.txt"
        (deps / "hook.pth").write_text(
            "import collide_probe_7901, pathlib; "
            f"pathlib.Path({str(pth_proof)!r}).write_text(collide_probe_7901.MARKER)\n"
        )
        script = tmp_path / "noop.py"
        script.write_text("pass\n")
        patched = [str(gateway), *sys.path]  # gateway-like entry ahead of the shim's work
        monkeypatch.setattr(sys, "path", patched)
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(script)])
        finally:
            sys.modules.pop("collide_probe_7901", None)
        assert pth_proof.read_text() == "dep", pth_proof.read_text()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX arm of the exe gate")
    def test_deps_boot_posix_exe_named_script_keeps_deps(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On POSIX a *.exe name is just a python script (only python targets
        reach the shim): it must take the script dispatch - the embedded-
        launcher arm's execv fallback would restart its shebang interpreter
        WITHOUT the deps path."""
        from kiro_crew.apps import deps_boot

        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "exe_dep_7901.py").write_text("MARKER = 'dep'\n")
        exe_proof = tmp_path / "exe_proof.txt"
        exe = tmp_path / "tool.exe"
        exe.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            "import exe_dep_7901\n"
            f"pathlib.Path({str(exe_proof)!r}).write_text(exe_dep_7901.MARKER)\n"
        )
        execvs: list = []
        monkeypatch.setattr(deps_boot.os, "execv", lambda *a: execvs.append(a))
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(exe)])
        finally:
            sys.modules.pop("exe_dep_7901", None)
        assert execvs == [], "POSIX exe-named script must not be handed to execv"
        assert exe_proof.read_text() == "dep"

    def test_deps_boot_windows_launcher_arm(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pip embedded-launcher arm behind the Windows gate, driven on
        any platform through an os proxy: a zip-appended exe's __main__.py
        stub runs in-process with the deps placed, and a non-zip exe is
        handed to execv exactly as an unshimmed launch would."""
        import zipfile as _zf

        from kiro_crew.apps import deps_boot

        class _ExecvCalled(Exception):
            pass

        class _NtOs:
            name = "nt"

            @staticmethod
            def execv(path: str, args: list) -> None:
                raise _ExecvCalled(path)

            def __getattr__(self, attr: str) -> Any:
                return getattr(os, attr)

        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "zip_dep_7901.py").write_text("MARKER = 'dep'\n")
        proof = tmp_path / "zip_proof.txt"
        exe = tmp_path / "console.exe"
        with _zf.ZipFile(exe, "w") as z:
            z.writestr(
                "__main__.py",
                "import pathlib, sys\n"
                "import zip_dep_7901\n"
                f"pathlib.Path({str(proof)!r}).write_text(zip_dep_7901.MARKER + sys.argv[1])\n",
            )
        monkeypatch.setattr(deps_boot, "os", _NtOs())
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(exe), "zargv"])
        finally:
            sys.modules.pop("zip_dep_7901", None)
        assert proof.read_text() == "depzargv"

        # Not a zip after all (the resolver gates on is_zipfile but the file
        # can change between resolution and launch): handed straight to
        # execv, never dispatched as python.
        native = tmp_path / "native.exe"
        native.write_bytes(b"MZx90")
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        with pytest.raises(_ExecvCalled):
            deps_boot.main([str(deps), str(native), "n"])

    def test_deps_boot_drops_its_own_script_dir(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path-launch shape: CPython puts the shim script's OWN directory at
        sys.path[0], which would let an app import gateway modules by bare
        name - the shim must drop it before anything resolves."""
        from kiro_crew.apps import deps_boot

        own = os.path.dirname(os.path.abspath(deps_boot.__file__))
        deps = tmp_path / "deps"
        deps.mkdir()
        script = tmp_path / "noop2.py"
        script.write_text("pass\n")
        monkeypatch.setattr(sys, "path", [own, *sys.path])
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), str(script)])
        assert sys.path[0] != own

    def test_deps_boot_main_runs_a_script_and_a_module_in_process(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-process coverage of the shim's three arms: script dispatch
        (with the script dir at sys.path[0]), -m module dispatch (module
        resolved through the addsitedir'd deps dir, moved to the front),
        and the usage error."""
        from kiro_crew.apps import deps_boot

        _clean_path = list(sys.path)  # arms mutate the patched list; reset from this
        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "dep_mod.py").write_text("MARKER = 'dep'\n")
        proof = tmp_path / "proof.txt"
        script = tmp_path / "sub" / "entry.py"
        script.parent.mkdir()
        (script.parent / "sib.py").write_text("S = 'sib'\n")
        script.write_text(
            "import pathlib, sys\n"
            "import dep_mod, sib\n"
            f"pathlib.Path({str(proof)!r}).write_text("
            "dep_mod.MARKER + sib.S + sys.argv[1])\n"
        )
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), str(script), "argved"])
        assert proof.read_text() == "depsibargved"
        assert sys.path[0] == str(script.parent)

        mod_proof = tmp_path / "mod_proof.txt"
        (deps / "runnable_mod.py").write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(mod_proof)!r}).write_text('ran' + sys.argv[1])\n"
        )
        # The first arm's import built a FileFinder for the deps dir whose
        # listing cache has mtime granularity - a module written within the
        # same granule is invisible to the second arm without this.
        importlib.invalidate_caches()
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), "-m", "runnable_mod", "x"])
        assert mod_proof.read_text() == "ranx"

        # -c arm: python -c parity (argv[0] is "-c", cwd at sys.path[0]),
        # module resolved through the addsitedir'd deps dir. The arm
        # installs a fresh __main__ module (production runs in a dedicated
        # process); restore the test runner's afterwards.
        c_proof = tmp_path / "c_proof.txt"
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main(
            [
                str(deps),
                "-c",
                "import pathlib, sys\n"
                "import dep_mod\n"
                f"pathlib.Path({str(c_proof)!r}).write_text(dep_mod.MARKER + sys.argv[1])",
                "cargv",
            ]
        )
        assert c_proof.read_text() == "depcargv"

        # PLACEMENT parity: the launch entry precedes the deps entries, so
        # an app-local module colliding with a dependency name resolves to
        # the app's own file exactly like a plain launch
        (deps / "sib.py").write_text("S = 'DEPSIB'\n")  # collides with app sib.py
        order_proof = tmp_path / "order_proof.txt"
        script2 = tmp_path / "sub" / "entry2.py"
        script2.write_text(
            "import pathlib\n"
            "import sib\n"
            f"pathlib.Path({str(order_proof)!r}).write_text(sib.S)\n"
        )
        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        deps_boot.main([str(deps), str(script2)])
        assert order_proof.read_text() == "sib", order_proof.read_text()

        # safe_path (-P/-I): the shim must not restore the launch entry.
        # A proxy, not a bare object: sys.flags is process-global and other
        # code (runpy) reads .verbose etc. during the run.
        _real_flags = sys.flags

        class _Flags:
            safe_path = True

            def __getattr__(self, attr):
                return getattr(_real_flags, attr)

        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        monkeypatch.setattr(deps_boot.sys, "flags", _Flags())
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        sp_proof = tmp_path / "sp_proof.txt"
        deps_boot.main(
            [
                str(deps),
                "-c",
                "import pathlib, sys\n"
                f"pathlib.Path({str(sp_proof)!r}).write_text("
                "'|'.join(sys.path[:2]))",
            ]
        )
        parts = sp_proof.read_text().split("|")
        assert parts[0] == str(deps), parts  # deps first, NO '' cwd entry
        monkeypatch.setattr(deps_boot.sys, "flags", _real_flags)

        # embedded-exe arm: pip's Windows launcher carries the console
        # script as an appended ZIP; the shim extracts __main__.py and
        # dispatches it after addsitedir
        import zipfile as _zipfile

        exe_proof = tmp_path / "exe_proof.txt"
        fake_exe = tmp_path / "embtool.exe"
        with open(fake_exe, "wb") as fh:
            fh.write(b"MZ fake native prefix\n")
            with _zipfile.ZipFile(fh, "a") as zf:
                zf.writestr(
                    "__main__.py",
                    "import pathlib, sys\n"
                    "import dep_mod\n"
                    f"pathlib.Path({str(exe_proof)!r}).write_text(dep_mod.MARKER + sys.argv[1])\n",
                )
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        deps_boot.main([str(deps), str(fake_exe), "exearg"])
        assert exe_proof.read_text() == "depexearg"

        with pytest.raises(SystemExit) as exc1:
            deps_boot.main([])
        assert exc1.value.code == 2
        with pytest.raises(SystemExit) as exc2:
            deps_boot.main([str(deps), "-m"])
        assert exc2.value.code == 2

    def test_the_deps_boot_shim_processes_pth_files_with_deps_first(self, tmp_path: Any) -> None:
        """The whole reason the shim exists: PYTHONPATH never processes .pth
        files, site.addsitedir does. A deps tree whose package arrives ONLY
        via a .pth redirect must import under the shim - and the deps
        entries must sit ahead of the gateway env on sys.path (the
        precedence the PYTHONPATH transport had)."""
        deps = tmp_path / "deps"
        hidden = deps / "hidden"
        hidden.mkdir(parents=True)
        (hidden / "pth_only_pkg.py").write_text("VIA_PTH = True\n")
        (deps / "redirect.pth").write_text("hidden\n")
        subdir = tmp_path / "app"
        subdir.mkdir()
        (subdir / "sibling.py").write_text("S = 1\n")
        script = subdir / "entry.py"
        script.write_text(
            "import json, sys\n"
            "import pth_only_pkg\n"
            "import sibling  # direct-script parity: script dir on sys.path[0]\n"
            "print(json.dumps({'via_pth': pth_only_pkg.VIA_PTH,"
            " 'sibling': sibling.S}))\n"
        )
        proc = subprocess.run(
            [
                sys.executable,
                # Absolute SCRIPT path, not -m: the -m spelling imports the
                # kiro_crew package and Python writes __pycache__ into the
                # CHECKOUT - a test must not leave artifacts outside
                # tmp_path. (Production spells it by path for its own
                # reason: session-cwd shadowing.)
                os.path.abspath(bmod._deps_boot_module.__file__),
                str(deps),
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        assert '"via_pth": true' in proc.stdout, proc.stdout
        assert '"sibling": 1' in proc.stdout, proc.stdout

    def test_a_python_backend_with_deps_launches_through_the_shim(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """A provisioned backend spawns via deps_boot (which addsitedir()s
        the deps dir, processing .pth) rather than a raw interpreter+entry
        argv - and only for the gateway interpreter."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-shim", _manifest("server.py"))
        argv = seen["argv"]
        assert argv[0] == sys.executable, argv
        # absolute-path spelling: an app-root kiro_crew.py must not be able
        # to shadow the shim for -m resolution under cwd=app root
        assert argv[1] == "-s", argv
        assert argv[2].endswith("deps_boot.py"), argv
        assert argv[3] == str(deps_dir), argv
        assert argv[4].endswith("server.py"), argv

    def test_non_volatile_requirements_install_from_a_snapshot(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip re-opens the requirements path; a concurrent rewrite after
        hashing would install replacement contents under the ORIGINAL
        digest's stamp. When the stamp will be trusted, pip installs from an
        immutable snapshot of the very bytes the digest covers."""
        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"requests==2.31.0\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        _record_runs(monkeypatch)
        seen: dict[str, Any] = {}

        def _spy_run(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                r_path = argv[argv.index("-r") + 1]
                seen["r_path"] = r_path
                seen["r_bytes"] = __import__("pathlib").Path(r_path).read_bytes()
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _spy_run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-snap", _manifest("server.py"))
        assert "snapshot" in seen["r_path"], seen
        assert str(spawn_root / "data") in seen["r_path"], seen
        assert seen["r_bytes"] == req, seen

    def test_pip_reads_the_resolved_target_of_an_in_tree_symlink(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip resolves a nested include (`-r base.txt`) relative to the
        requirements FILE - handing it the symlink path would resolve
        includes beside the LINK instead of its target. The -r argument must
        be the validated resolved path."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "server.py").write_text("x = 1\n")
        real = spawn_root / "config"
        real.mkdir()
        (real / "base.txt").write_bytes(b"requests\n")
        # an include makes the requirements VOLATILE, so pip reads the live
        # validated path (where the include resolves) instead of a snapshot
        (real / "requirements.txt").write_bytes(b"-r base.txt\n")
        try:
            _os.symlink(real / "requirements.txt", spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-reqlink", _manifest("server.py"))
        pip_argv = next(argv for argv in runs if "install" in argv)
        r_val = pip_argv[pip_argv.index("-r") + 1]
        assert r_val == str(real / "requirements.txt"), pip_argv

    @pytest.mark.parametrize("existing", [False, True])
    @pytest.mark.parametrize("pinned", [False, True])
    def test_deps_lock_is_created_exclusively_or_opened_without_create(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, existing: bool, pinned: bool
    ) -> None:
        import os

        if pinned and not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("directory-relative opens are unavailable")
        if not pinned:
            monkeypatch.setattr(bmod.pinned_fs, "supports_pinned_walk", lambda: False)
        parent = spawn_root / "data"
        parent.mkdir()
        lock = parent / ".kirocrew-deps.lock"
        (spawn_root / "requirements.txt").write_text("requests\n")
        if existing:
            lock.write_text("lock contents stay intact\n")
        calls = []
        real_open = os.open

        def record_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append((flags, dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        # Wrapping os.open must not turn off the pinned branch under test.
        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {record_open})
        monkeypatch.setattr(os, "open", record_open)
        monkeypatch.setattr(bmod, "_provision_app_deps_locked", lambda *args: "")
        assert bmod.provision_app_deps("deps-create", spawn_root) == ""
        assert len(calls) == (2 if existing else 1)
        assert (calls[0][1] is not None) == pinned
        assert calls[0][0] & os.O_CREAT
        assert calls[0][0] & os.O_EXCL
        for flags, _fd in calls:
            assert flags & os.O_RDWR
            assert not flags & os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
        if existing:
            assert not calls[1][0] & (os.O_CREAT | os.O_EXCL)
            assert calls[1][1] == calls[0][1]
            assert lock.read_text() == "lock contents stay intact\n"

    def test_a_lock_that_disappears_before_reopen_fails_closed(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        (spawn_root / "requirements.txt").write_text("requests\n")
        calls = []
        provisioned = []
        real_open = os.open

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append(flags)
                if len(calls) == 1:
                    # A contender created the file, then removed it before reopen.
                    raise FileExistsError("lock existed at exclusive create")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {race_open})
        monkeypatch.setattr(os, "open", race_open)
        monkeypatch.setattr(
            bmod, "_provision_app_deps_locked", lambda *args: provisioned.append(args)
        )
        error = bmod.provision_app_deps("deps-vanished", spawn_root)
        assert "Failed to serialize dependency provisioning" in error
        assert len(calls) == 2
        assert not calls[1] & (os.O_CREAT | os.O_EXCL)
        assert provisioned == []
        assert not (spawn_root / "data" / ".kirocrew-deps.lock").exists()

    def test_an_existing_lock_symlink_is_not_followed(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("this case exercises O_NOFOLLOW")
        parent = spawn_root / "data"
        parent.mkdir()
        target = spawn_root / "unchanged.txt"
        target.write_text("keep this content", encoding="utf-8")
        lock = parent / ".kirocrew-deps.lock"
        lock.symlink_to(target)
        (spawn_root / "requirements.txt").write_text("requests\n", encoding="utf-8")
        provisioned = []
        monkeypatch.setattr(
            bmod, "_provision_app_deps_locked", lambda *args: provisioned.append(args)
        )
        assert bmod.provision_app_deps("deps-link", spawn_root)
        assert provisioned == []
        assert target.read_text(encoding="utf-8") == "keep this content"
        assert lock.is_symlink()

    def _stub_pip(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record each --target pip is pointed at, and populate it.

        ``wrap_argv`` is stubbed for the same reason the ``spawn_root`` fixture
        stubs it: it fail-closes where no OS sandbox backend is available, which
        is a property of the host rather than of the paths under test.
        """
        targets: list[str] = []
        monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **_kw: (list(argv), None))

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                target = argv[argv.index("--target") + 1]
                targets.append(target)
                (Path(target) / "pkg.py").write_text("x = 1\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        return targets

    def test_provisioning_through_a_linked_home_reaches_the_staging_pin(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole transaction has to survive a linked ancestor, not just the data pin.

        Provisioning pins twice - the data directory and, one level deeper, the
        staging tree pip installs into. Both walks refuse any link they meet, so
        a home reached through one fails at whichever pin the caller did not
        canonicalise for. Driving the real transaction is what covers the second.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        real_home = tmp_path / "real-home"
        app_root = real_home / "crew" / "apps" / "demo"
        app_root.mkdir(parents=True)
        (app_root / "requirements.txt").write_bytes(b"requests\n")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home)
        targets = self._stub_pip(monkeypatch)

        error = bmod.provision_app_deps("demo", linked_home / "crew" / "apps" / "demo")

        assert error == "", error
        assert targets, "pip never ran, so the staging pin was never reached"
        assert (bmod.app_deps_dir(app_root) / "pkg.py").is_file()

    def test_provisioning_refuses_a_link_at_the_app_directory(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A link at the app's own name must be refused, at every pin in the run.

        ``apps/<name>`` is app-writable, so canonicalising it would let a link
        planted there redirect the gateway's own staging writes into another
        app's tree. The refusal has to hold for the staging pin too, which sits
        a level deeper than the data pin and so splits the path differently.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        apps = tmp_path / "crew" / "apps"
        victim = apps / "victim"
        (victim / "data").mkdir(parents=True)
        (victim / "requirements.txt").write_bytes(b"requests\n")
        (apps / "attacker").symlink_to(victim)
        targets = self._stub_pip(monkeypatch)

        error = bmod.provision_app_deps("attacker", apps / "attacker")

        assert error, "provisioning through a linked app directory must be refused"
        assert not targets, "pip ran, so a write was already aimed through the link"
        assert not list(
            (victim / "data").glob(".kirocrew-deps*")
        ), "the refusal came too late: the victim tree already carries staging"

    def test_concurrent_provisioning_is_serialized_by_the_deps_lock(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend spawn and a backend-less registration (or two
        registrations) can provision the same app concurrently; unserialized
        they delete each other's staging tree and both fail. The per-app
        file lock admits one transaction at a time - and the stamp check
        runs inside it, so the waiter skips pip on the winner's stamp."""
        import threading
        import time as _time

        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        inside = []
        overlap = []
        installs = []
        ready = threading.Barrier(2)
        real_open = bmod.os.open

        def concurrent_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock") and flags & bmod.os.O_CREAT:
                ready.wait(timeout=5)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in bmod.os.supports_dir_fd:
            monkeypatch.setattr(
                bmod.os, "supports_dir_fd", bmod.os.supports_dir_fd | {concurrent_open}
            )
        monkeypatch.setattr(bmod.os, "open", concurrent_open)

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                installs.append(True)
                if inside:
                    overlap.append(True)
                inside.append(True)
                _time.sleep(0.15)
                argv = list(argv)
                staging = argv[argv.index("--target") + 1]
                (__import__("pathlib").Path(staging) / "pkg.py").write_text("x = 1\n")
                inside.pop()
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        errs: list[str] = []

        def _call() -> None:
            errs.append(bmod.provision_app_deps("deps-race", spawn_root))

        threads = [threading.Thread(target=_call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads), "dependency provisioning deadlocked"
        assert errs == ["", ""], errs
        assert not overlap, "two pip transactions ran concurrently"
        assert len(installs) == 1, "the waiter must reuse the completed install"
        from kiro_crew.apps.interpreter import app_deps_dir

        assert (app_deps_dir(spawn_root) / "pkg.py").is_file()

    def test_the_pinned_dir_detects_a_mid_transaction_swap(self, tmp_path: Any) -> None:
        """The link pre-check is a TOCTOU window: a running app can swap
        data/ AFTER validation. The pin holds the directory open and verify()
        refuses once the path names a different inode - the renames go
        through the held fd and cannot be redirected at all."""
        if bmod.platform_compat.IS_WINDOWS:
            pytest.skip("dir pinning is POSIX-only (symlink creation is privileged on Windows)")
        real = tmp_path / "data"
        real.mkdir()
        pin = bmod._PinnedDir(real)
        try:
            pin.verify()  # untouched: passes
            real.rename(tmp_path / "moved-away")
            (tmp_path / "other").mkdir()
            (tmp_path / "other").rename(real)
            with pytest.raises(OSError, match="replaced mid-provisioning"):
                pin.verify()
        finally:
            pin.close()

    def test_a_data_dir_under_a_linked_ancestor_is_pinned_not_refused(self, tmp_path: Any) -> None:
        """A home directory reached through a symlink is ordinary, not an attack.

        pin_parent walks every component with O_NOFOLLOW and refuses a link,
        which is why its contract makes the CALLER resolve the path once. An
        ancestor that has always been a link is indistinguishable from a
        swapped one to that walk, so an unresolved path turns every install
        and uninstall into a refusal for anyone whose home contains one.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        real_home = tmp_path / "real-home"
        data = real_home / "crew" / "apps" / "demo" / "data"
        data.mkdir(parents=True)
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home)
        app_root = linked_home / "crew" / "apps" / "demo"

        pin = bmod._PinnedDir(bmod._pinned_ancestors(app_root) / "data")
        try:
            assert pin.fd is not None, "POSIX must pin by descriptor"
            pinned = os.fstat(pin.fd)
            target = os.stat(str(data))
            assert (pinned.st_dev, pinned.st_ino) == (target.st_dev, target.st_ino)
            pin.verify()
        finally:
            pin.close()

    def test_a_data_dir_swapped_after_resolution_is_still_refused(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving on the caller side must not retire the guard.

        The swap is performed at the pin_parent seam - after the path was
        resolved, before the walk reads it - which is the check-to-use window
        made deterministic. O_NOFOLLOW on the final component has to refuse
        it, and the refusal has to say a swap happened, because here one did.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        holder = tmp_path / "holder"
        data = holder / "data"
        data.mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.mkdir()

        real_pin_parent = bmod.pinned_fs.pin_parent

        def swap_then_pin(parent: str, **kwargs: Any) -> int:
            data.rmdir()
            data.symlink_to(victim)
            return real_pin_parent(parent, **kwargs)

        monkeypatch.setattr(bmod.pinned_fs, "pin_parent", swap_then_pin)
        with pytest.raises(OSError, match="became a symbolic link after the path was checked"):
            bmod._PinnedDir(data)

    def test_a_link_at_the_app_directory_itself_is_still_refused(self, tmp_path: Any) -> None:
        """The canonical prefix must stop above the app's own directory.

        ``apps/<name>`` is written by the app, so canonicalising that component
        would hand the walk whatever a link planted there points at - another
        app's tree - and the O_NOFOLLOW refusal would never fire. Only the
        operator-controlled part above it may be made canonical.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        apps = tmp_path / "apps"
        victim = apps / "victim"
        (victim / "data").mkdir(parents=True)
        (apps / "attacker").symlink_to(victim)

        with pytest.raises(OSError):
            bmod._PinnedDir(bmod._pinned_ancestors(apps / "attacker") / "data")

    def test_an_ancestor_cycle_is_refused_as_an_oserror_not_a_runtimeerror(
        self, tmp_path: Any
    ) -> None:
        """Canonicalising the ancestors must not raise past the refusal type.

        Every caller of this class treats OSError as "refused" and lets nothing
        else through, so a helper that raises another type on a hostile tree
        turns a refusal into an unhandled crash. ``Path.resolve`` raises
        RuntimeError on a cycle; realpath hands the unresolved path to the walk,
        which refuses it as the configured OSError like any other link.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        first = tmp_path / "ring-a"
        second = tmp_path / "ring-b"
        first.symlink_to(second)
        second.symlink_to(first)

        with pytest.raises(OSError):
            bmod._PinnedDir(bmod._pinned_ancestors(first / "data"))

    def test_provisioning_refuses_a_linked_data_directory(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every provisioning operation under data/ would FOLLOW a planted
        link - an app pointing data/ at another app's tree would have the
        swap install attacker-chosen dependencies into the victim's dir.
        Refused before anything is touched through it, same shape as the
        uninstall purge's guard."""
        import os as _os
        import shutil as _shutil

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        victim = tmp_path / "victim-data"
        victim.mkdir()
        data = spawn_root / "data"
        if data.exists():
            _shutil.rmtree(data)
        try:
            _os.symlink(victim, data)
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-datalink", spawn_root)
        assert "symlink/junction" in err, err
        assert not any("install" in argv for argv in runs), runs
        assert not any(victim.iterdir()), list(victim.iterdir())

    def test_an_oversized_requirements_file_is_refused_bounded(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gateway buffers app-controlled inputs in its own memory: an
        oversized requirements.txt reads at most cap+1 bytes and refuses -
        it must never be slurped whole or handed to pip."""
        (spawn_root / "requirements.txt").write_bytes(b"#" + b"x" * (bmod._DEPS_REQ_MAX_BYTES + 10))
        runs = _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-huge", spawn_root)
        assert "Refusing requirements.txt" in err, err
        assert not any("install" in argv for argv in runs), runs

    def test_every_provisioning_failure_arm_emits_the_sel_event(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deps_provision_failed is the audit contract for provisioning
        failures; the requirements-read refusal (out-of-root symlink) and
        lock failures go through the same wrapper emit as pip failures."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        outside = spawn_root.parent / "outside-req.txt"
        outside.write_bytes(b"requests\n")
        try:
            _os.symlink(outside, spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        events: list = []
        monkeypatch.setattr(
            bmod,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-selpin", spawn_root)
        assert "Refusing requirements.txt" in err, err
        assert [e.get("outcome") for e in events] == ["deps_provision_failed"], events

    def test_a_planted_stamp_symlink_reads_as_unprovisioned(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamp lives in the app-writable tree, so its read is
        no-follow-bound like the requirements read: a planted symlink at the
        stamp name must never make the gateway read an arbitrary path - it
        reads as "unprovisioned" and pip simply runs (safe direction)."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"requests\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        target = tmp_path / "protected.txt"
        target.write_text(bmod._deps_digest(req))  # even a matching target
        try:
            _os.symlink(target, deps_dir / bmod._DEPS_STAMP_NAME)
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-stamplink", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_include_bearing_requirements_never_skip_pip(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamp digest covers the top-level file's bytes only, so a
        change confined to an included file (-r base.txt) would preserve the
        stamp and serve stale dependencies. Include-bearing requirements
        therefore disable the skip: pip runs even when the stamp matches."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"-r base.txt\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        (spawn_root / "base.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(req))
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-inc", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_an_unchanged_requirements_file_skips_the_pip_call(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip --target cannot answer "already satisfied" the way a venv
        install could, so the stamp is what keeps a restart with unchanged
        requirements off the network - and keeps an OFFLINE restart of a
        healthy backend from raising a false provisioning alarm."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-skip", _manifest("server.py"))
        assert not any("install" in argv for argv in runs), runs

    def test_a_changed_requirements_file_reinstalls(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(
            bmod._deps_digest(b"requests\n")  # stamp of the OLD file
        )
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-changed", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_the_stamp_digest_changes_with_the_interpreter_abi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wheels installed by pip --target are ABI-specific, so a gateway
        Python upgrade must reprovision even when requirements.txt is
        byte-identical - a requirements-only stamp would skip pip and leave
        old-ABI wheels live. A PATCH upgrade must reprovision too:
        requirements can carry python_full_version markers that flip on it."""
        before = bmod._deps_digest(b"requests\n")
        monkeypatch.setattr(
            bmod.sys,
            "implementation",
            SimpleNamespace(
                cache_tag="cpython-399",
                name=sys.implementation.name,
                version=sys.implementation.version,
            ),
        )
        assert bmod._deps_digest(b"requests\n") != before
        monkeypatch.undo()
        monkeypatch.setattr(bmod.platform, "python_version", lambda: "3.99.99")
        assert bmod._deps_digest(b"requests\n") != before

    def test_an_interrupted_swap_is_recovered_on_the_next_start(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash between the two swap renames leaves the good tree under the
        prior name only. The next start must put it back - and, with a
        matching stamp inside, skip pip - so an offline restart keeps its
        dependencies instead of spawning bare."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        prior = spawn_root / "data" / bmod._DEPS_PRIOR_NAME
        prior.mkdir(parents=True)
        (prior / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        (prior / "marker.py").write_text("recovered = True\n")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-recover", _manifest("server.py"))
        deps_dir = app_deps_dir(spawn_root)
        assert (deps_dir / "marker.py").is_file()
        assert not prior.exists()
        assert not any("install" in argv for argv in runs), runs
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]

    @pytest.mark.skipif(sys.platform == "win32", reason="dir_fd staging pin is POSIX-only")
    def test_a_staging_swap_after_pip_cannot_redirect_marker_writes_or_publish(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A build hook that replaces staging with a symlink after pip exits
        must neither have the gateway's marker writes land in its target nor
        get its tree PUBLISHED as the live install: the staging descriptor
        pinned at creation makes the writes un-redirectable and the
        pre-publish identity verify refuses the swapped entry."""
        import os as _os
        from pathlib import Path

        from kiro_crew.apps.interpreter import app_deps_dir

        victim = tmp_path / "victim-outside-app"
        victim.mkdir()
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")

        def swapping_run(cmd: Any, **kwargs: Any) -> Any:
            target = None
            for i, tok in enumerate(cmd):
                if str(tok) == "--target" and i + 1 < len(cmd):
                    target = Path(str(cmd[i + 1]))
                    break
            assert target is not None, cmd
            aside = target.parent / (target.name + "-aside")
            _os.rename(str(target), str(aside))
            _os.symlink(str(victim), str(target))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(bmod, "run_limited", swapping_run)
        err = bmod.provision_app_deps("swap-app", spawn_root)
        assert err, "provisioning must FAIL when staging identity is lost"
        assert not (victim / bmod._DEPS_STAMP_NAME).exists(), "marker escaped!"
        assert not (victim / bmod._DEPS_ABI_NAME).exists(), "marker escaped!"
        deps_dir = app_deps_dir(spawn_root)
        assert not deps_dir.is_symlink(), "the swapped tree must not be published"

    def test_a_failed_reinstall_leaves_the_prior_deps_dir_intact(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip fills a staging dir that is swapped in only on success, so a
        failed or interrupted (re)install can never corrupt the live deps dir
        in place - the prior good install keeps serving the spawn."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")
        deps_dir = app_deps_dir(spawn_root)
        (deps_dir / "requests").mkdir(parents=True)
        (deps_dir / "requests" / "__init__.py").write_text("prior = True\n")
        # A REAL prior good install carries its stamp (written at swap time);
        # activation requires it to name the current interpreter's digest,
        # so the fixture plants it too. A stampless (or stale-ABI) tree is
        # deliberately NOT activated - see _deps_tree_stamp_current.
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests==2.32.0\n"))
        _record_runs(monkeypatch, exc=RuntimeError("no network"))
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-keep", _manifest("server.py"))
        assert (deps_dir / "requests" / "__init__.py").read_text() == "prior = True\n"
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))
        # And the surviving install still reaches the child.
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]

    def test_the_installer_never_shells_out_to_a_bare_interpreter(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        """pip must run as `sys.executable -m pip` - a bare `python3` relies
        on PATH (absent on some hosts, a Store stub on Windows), and any
        venv-relative pip path is POSIX-only."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-argv", _manifest("server.py"))
        pip_argv = next(argv for argv in runs if "install" in argv)
        # Assert on the argv TOKEN, never on a substring of the joined command.
        # sys.executable's own basename is frequently `python3` (any mise- or
        # pyenv-managed interpreter, and /usr/bin/python3 itself), so a
        # substring check matches the correct absolute form and fails on
        # exactly the hosts it is meant to pass on.
        assert pip_argv[0] == sys.executable, pip_argv
        assert pip_argv[0] != "python3", pip_argv
        assert pip_argv[1:4] == ["-s", "-m", "pip"], pip_argv
        # `.venv/bin/pip` is POSIX-only; the interpreter must run pip as a module.
        assert not pip_argv[0].replace("\\", "/").endswith("/bin/pip"), pip_argv

    def test_a_nonzero_pip_exit_is_checked_not_discarded(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pip call passes check=True: a non-zero exit must raise into the
        failure path rather than being silently discarded (the original defect
        left the backend to die on an import error pointing away from
        provisioning)."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        kwargs_seen: list[dict[str, Any]] = []

        def _run(argv: Any, **kwargs: Any) -> Any:
            kwargs_seen.append(kwargs)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-check", _manifest("server.py"))
        assert kwargs_seen and kwargs_seen[0].get("check") is True, kwargs_seen

    def test_a_failed_dependency_install_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The spawn is still attempted (the deps dir may hold a previous
        successful install, and an offline host must not lose a working
        backend to a failed refresh) - but the failure now surfaces as an
        ERROR naming provisioning, not a swallowed warning."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        _record_runs(monkeypatch, exc=RuntimeError("no network"))
        _capture_popen(monkeypatch)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("deps-fail", _manifest("server.py"))
        assert any(
            "Failed to install requirements.txt dependencies" in r.message
            and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_a_provisioning_failure_is_written_into_the_backend_log(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user-visible surface: the backend's own log opens with the
        provisioning failure, so the import error the missing deps produce
        points back at the real cause instead of reading as an app bug."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        err = subprocess.CalledProcessError(
            1, ["pip"], stderr=b"No matching distribution found for requests"
        )
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-log", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert "Failed to install requirements.txt dependencies" in log_text
        assert "No matching distribution found" in log_text

    def test_a_non_ascii_provisioning_failure_is_written_under_a_narrow_locale_codec(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A text handle opened without ``encoding=`` takes the platform default,
        which on Windows is cp1252. The provision-error line can carry a
        Unicode traceback glyph or an accented path; under cp1252 that write
        raises UnicodeEncodeError and the spawn aborts on the branch meant to
        record the failure. The open must name UTF-8 so the line always lands.

        The narrow default is simulated by giving every text-mode open that
        omits ``encoding=`` the cp1252 codec, which is what Windows does."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        glyphs = "\u2192 C:\\Users\\Ren\u00e9\\pkg \u2014 \u2019quoted\u2019"
        err = subprocess.CalledProcessError(
            1, ["pip"], stderr=f"ERROR: build failed {glyphs}".encode("utf-8")
        )
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)

        real_open = builtins.open

        def _windows_default_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if "b" not in mode and "encoding" not in kwargs and len(args) < 4:
                kwargs["encoding"] = "cp1252"
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _windows_default_open)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-utf8", _manifest("server.py"))
        raw = (spawn_root / "data" / "logs" / "backend.log").read_bytes()
        assert b"[kiro-crew] " in raw
        assert glyphs.encode("utf-8") in raw, "the non-ASCII text must round-trip as UTF-8"

    def test_the_crash_tail_decodes_the_childs_bytes_with_replacement(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The child appends raw bytes after the UTF-8 header. A stray byte in
        its output must not turn the whole tail into "(no output)"; the read
        decodes with replacement and the error still names what the child said."""
        (spawn_root / "server.py").write_text("x = 1\n")
        log_path = spawn_root / "data" / "logs" / "backend.log"

        def _popen(*_a: Any, **kwargs: Any) -> Any:
            kwargs["stdout"].write("[kiro-crew] header\n")
            kwargs["stdout"].flush()
            with open(log_path, "ab") as raw:
                raw.write(b"Error: address already in use \xff\xfe on bind\n")
            # A pid no platform allocates (the ``_UNALLOCATABLE_PID`` spelling in
            # test/test_update_provider.py): the crash branch may signal it.
            return SimpleNamespace(pid=99_999_999_999, returncode=1, poll=lambda: 1)

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: False)
        with caplog.at_level(logging.ERROR):
            result = bmod._start_app_backend_body("tail-bytes", _manifest("server.py"))
        assert result is None
        message = next(r.message for r in caplog.records if "exited immediately" in r.message)
        assert "(no output)" not in message
        assert "address already in use" in message
        assert "[PORT COLLISION]" in message

    def test_tokenized_url_query_strings_are_stripped_from_pip_stderr(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """redact_credentials catches user:pass@ forms; a signed or
        tokenized URL carries its secret in the query string, which pip
        echoes verbatim. The strip runs on the FULL stderr before the tail
        cut, so a truncation can never split the URL from its query and let
        the token's tail through."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        token = "X-Amz-Signature=" + "s" * 32
        stderr = (
            "x" * 380 + f"ERROR: fetch https://bucket.example/pkg.whl?{token} failed"
        ).encode()
        err = subprocess.CalledProcessError(1, ["pip"], stderr=stderr)
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-qs", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert token not in log_text
        assert "s" * 32 not in log_text
        # WHICH layer catches it is not the contract (the exfiltration-URL
        # pass now runs first and can swallow the whole URL); the property
        # is that some redaction fired and the secret is gone.
        assert "<redacted-query>" in log_text or "[REDACTED" in log_text

    def test_pip_stderr_is_redacted_before_the_tail_truncation(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redaction runs on the FULL stderr, then the tail is taken: a
        suffix cut applied first can split a credential from the marker the
        redactor matches on, letting the secret's tail reach the logs."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        secret = "AKIA" + "X" * 16
        # Position the credential so a truncate-first implementation would
        # cut through it: padding pushes all but the secret's tail out of the
        # final 400 characters.
        stderr = (
            "x" * 380 + f"ERROR: fetch https://user:{secret}@pypi.example/simple failed"
        ).encode()
        err = subprocess.CalledProcessError(1, ["pip"], stderr=stderr)
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-redact", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert secret not in log_text
        assert "Failed to install requirements.txt dependencies" in log_text

    def test_a_shimmed_child_gets_no_deps_pythonpath(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shim XOR PYTHONPATH: `python -m kiro_crew.apps.deps_boot` resolves
        kiro_crew through sys.path, so a deps-provided kiro_crew copy on
        PYTHONPATH would SHADOW the gateway's shim - app code running as the
        "shim". A shimmed child therefore launches WITHOUT the deps dir on
        PYTHONPATH; addsitedir supplies the deps only after the trusted shim
        has imported. The operator's own PYTHONPATH still passes through."""
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setenv("PYTHONPATH", "/operator/own")
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-env", _manifest("server.py"))
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]
        child_pp = seen["kwargs"]["env"].get("PYTHONPATH", "")
        assert str(deps_dir) not in child_pp.split(os.pathsep), child_pp
        assert "/operator/own" in child_pp.split(os.pathsep), child_pp

    def test_no_deps_dir_means_no_pythonpath_injection(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-none", _manifest("server.py"))
        assert "PYTHONPATH" not in seen["kwargs"]["env"]


# ---------------------------------------------------------------------------
# Shared interpreter resolution (apps/interpreter.py)
# ---------------------------------------------------------------------------


def _write_runnable(path: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def _create_real_venv(root: Any) -> None:
    """Build a REAL ``.venv`` under ``root`` the way ``python -m venv`` does.

    The CLI symlinks the interpreter on POSIX and copies it only on Windows;
    the ``venv.create`` API defaults to copying everywhere. A copied binary
    breaks on a relocatable base interpreter (python-build-standalone, which
    ``uv`` installs): its ``@rpath/libpython`` is resolved relative to the
    binary's own location, so the copy aborts at load and
    ``_venv_is_usable``'s probe reads it as "not a venv". Mirror the CLI so
    the fixture is the venv production expects on every host.
    """
    import venv as _venv

    _venv.create(root / ".venv", with_pip=False, symlinks=not bmod.platform_compat.IS_WINDOWS)


@pytest.fixture()
def probe_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the interpreter usability probe without OS confinement.

    The probe's sandbox wrapping is a host capability (absent on Windows CI
    and fail-closed there by design); these tests pin the probe's LOGIC -
    sys.prefix ownership and ABI match against a real venv interpreter - so
    the seams are passed through and the child runs directly.
    """
    from kiro_crew.apps import interpreter as imod

    monkeypatch.setattr(imod.sandbox, "wrap_argv", lambda argv, **_k: (list(argv), None))
    monkeypatch.setattr(imod.sandbox, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(imod.sandbox, "run_limited", lambda argv, **kw: subprocess.run(argv, **kw))


def _plant_stamp(app_root: Any) -> None:
    """Command selection requires a CURRENT stamp (stale trees crash at
    import); fixtures that expect deps-dir scripts to resolve plant one."""
    from kiro_crew.apps.interpreter import app_deps_dir as _add

    d = _add(app_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))


class TestInterpreterResolution:
    def _venv_python(self, root: Any) -> Any:
        from kiro_crew.apps.interpreter import venv_python_path

        return venv_python_path(root)

    def _real_venv(self, root: Any) -> Any:
        """Build a REAL .venv under root (the resolver now probes the
        interpreter, so a stub file fails _venv_is_usable)."""
        _create_real_venv(root)
        return self._venv_python(root)

    def test_a_real_venv_is_preferred(self, tmp_path: Any, probe_sandbox_passthrough: Any) -> None:
        from kiro_crew.apps.interpreter import resolve_app_python

        py = self._real_venv(tmp_path)
        assert resolve_app_python(tmp_path) == str(py)

    def test_a_bootstrap_skeleton_is_not_preferred(
        self, tmp_path: Any, probe_sandbox_passthrough: Any
    ) -> None:
        """The defect the probe fixes: a failed `python -m venv` leaves a
        skeleton whose bin/python3 is the fully-runnable system interpreter
        and whose pyvenv.cfg names the current minor version - a version
        check ACCEPTS it, but its sys.prefix is not this venv (no working
        environment), so the probe rejects it."""
        from kiro_crew.apps.interpreter import resolve_app_python

        # A stub interpreter that starts but is NOT the venv's own python
        # (its sys.prefix will not point inside <root>/.venv).
        py = self._venv_python(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\nexit 0\n")
        py.chmod(0o755)
        (tmp_path / ".venv" / "pyvenv.cfg").write_text(
            f"version = {sys.version_info[0]}.{sys.version_info[1]}.0\n"
        )
        assert resolve_app_python(tmp_path) == sys.executable

    def test_a_venv_without_an_interpreter_is_not_preferred(self, tmp_path: Any) -> None:
        from kiro_crew.apps.interpreter import resolve_app_python

        (tmp_path / ".venv").mkdir()
        assert resolve_app_python(tmp_path) == sys.executable

    def test_the_probe_runs_with_the_sanitized_app_env(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        """The probe executable is app-controlled, so it must receive the
        same sanitized environment every app subprocess gets - never the
        gateway's own, which can carry credentials."""
        from kiro_crew.apps import interpreter as imod

        _create_real_venv(tmp_path)
        monkeypatch.setenv("GATEWAY_SECRET_CANARY", "leak-me")
        seen: dict[str, Any] = {}
        real_run = imod.sandbox.run_limited

        def _spy(argv: Any, **kwargs: Any) -> Any:
            seen["env"] = kwargs.get("env")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(imod.sandbox, "run_limited", _spy)
        imod._venv_is_usable(tmp_path)
        assert seen["env"] is not None, "probe ran with the inherited gateway env"
        assert "GATEWAY_SECRET_CANARY" not in seen["env"]

    def test_the_probe_fails_closed_when_no_sandbox_is_available(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe executes app-controlled code, so it runs ONLY under the
        OS sandbox; a host where wrap_argv fail-closes (no backend) gets no
        positive evidence and falls back to sys.executable - the exception
        must never propagate into spawn or registration."""
        from kiro_crew.apps import interpreter as imod

        _create_real_venv(tmp_path)

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("no sandbox backend")

        monkeypatch.setattr(imod.sandbox, "wrap_argv", _raise)
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_hostile_probe_output_reads_as_not_usable(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        """The probe output is app-controlled: a venv whose interpreter prints
        a null prefix (valid JSON, wrong shape) must read as "not usable" -
        Path(None) raising TypeError out of the probe would break spawn and
        registration."""
        py = self._venv_python(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\necho '[null, %d, %d]'\n" % sys.version_info[:2])
        py.chmod(0o755)
        from kiro_crew.apps import interpreter as imod

        assert imod._venv_is_usable(tmp_path) is False
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_interpreter_choice_follows_the_active_tree(self, tmp_path: Any) -> None:
        """With an ACTIVE provisioned tree the gateway interpreter owns the
        launch (the wheels are ABI-bound to it). WITHOUT one - never
        provisioned or provisioning failed - a POSITIVELY usable venv is
        preferred even when requirements are declared: the bare gateway
        interpreter holds none of the declared deps either, and a failed
        provisioning must not also take a working legacy venv away."""
        import sys as _sys
        from unittest import mock

        from kiro_crew.apps import interpreter as imod

        (tmp_path / "requirements.txt").write_bytes(b"uvicorn\n")
        # No tree + usable venv: the venv wins (the F2 fallback).
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) != _sys.executable
        # No tree + NO usable venv: the gateway interpreter.
        with mock.patch.object(imod, "_venv_is_usable", return_value=False):
            assert imod.resolve_app_python(tmp_path) == _sys.executable
        # ACTIVE tree (current-interpreter stamp planted): the gateway
        # interpreter reclaims the launch, even with a usable venv present.
        deps = imod.app_deps_dir(tmp_path)
        deps.mkdir(parents=True)
        (deps / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"uvicorn\n"))
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) == _sys.executable
        # STALE tree (no stamp, foreign/absent ABI tag): the spawn would
        # refuse to inject it, so it must not suppress the venv fallback
        # either - a dead tree that delivers nothing cannot also take the
        # working environment away.
        (deps / bmod._DEPS_STAMP_NAME).unlink()
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) != _sys.executable

    @pytest.mark.skipif(sys.platform == "win32", reason="dangling-symlink semantics are POSIX-only")
    def test_a_dangling_requirements_symlink_surfaces_as_a_failure(self, spawn_root: Any) -> None:
        """is_file() answers False for a DANGLING requirements.txt link the
        same as for absence - but a present-yet-unreadable entry must NOT be
        silently skipped, or the backend spawns importing deps that were
        never installed. Absence returns "" (nothing to do); a broken link
        returns a provisioning-failure string."""
        import os as _os

        # Genuine absence: no error.
        assert bmod.provision_app_deps("noreq-app", spawn_root) == ""
        # Present but dangling: a failure string routed through the common
        # failure epilogue (SEL audit + ERROR log), not a silent skip.
        _os.symlink(str(spawn_root / "does-not-exist.txt"), str(spawn_root / "requirements.txt"))
        events: list = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        from unittest.mock import patch

        with patch.object(bmod, "sel", lambda: _Sel()):
            err = bmod.provision_app_deps("dangling-app", spawn_root)
        assert err and "requirements.txt" in err, err
        assert events and events[0]["outcome"] == "deps_provision_failed", events

    def test_the_spill_cap_truncates_a_flooding_child(self, tmp_path: Any) -> None:
        """The output spill is bounded on disk: when it crosses the cap the
        watchdog truncates it back to empty, so a flooding child cannot fill
        the host. Drive the watchdog directly against a real temp file."""
        import tempfile as _tf
        import time as _time

        with _tf.TemporaryFile() as f:
            with bmod._capped_spill(f, hard_cap_bytes=4096, poll_secs=0.05) as tripped:
                f.write(b"x" * (4096 * 4))
                f.flush()
                deadline = _time.monotonic() + 3
                while _time.monotonic() < deadline and not tripped.is_set():
                    _time.sleep(0.05)
            assert tripped.is_set(), "watchdog must trip on a spill over the cap"
            import os as _os

            assert _os.fstat(f.fileno()).st_size <= 4096, "spill must be truncated"

    def test_a_deps_dir_console_script_is_resolvable(self, tmp_path: Any) -> None:
        """pip --target puts console scripts in <target>/bin (Scripts on
        Windows); the resolver must find them there now that the gateway never
        creates the venv layout that carries them on real installs."""
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(script)
        assert venv_provided_command(tmp_path, "my-tool") == str(script)

    def test_provisioned_deps_pin_the_gateway_interpreter(self, tmp_path: Any) -> None:
        """Once the gateway provisioned .kirocrew-deps, those wheels were
        built by sys.executable, so the venv is not consulted at all."""
        from kiro_crew.apps.interpreter import app_deps_dir, resolve_app_python

        self._real_venv(tmp_path)
        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        assert resolve_app_python(tmp_path) == sys.executable

    def test_a_venv_console_script_wins_only_without_provisioned_deps(
        self, tmp_path: Any, probe_sandbox_passthrough: Any
    ) -> None:
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        self._real_venv(tmp_path)
        venv_script = tmp_path / ".venv" / scripts / name
        _write_runnable(venv_script)
        # No deps dir: the usable venv's script is the answer.
        assert venv_provided_command(tmp_path, "my-tool") == str(venv_script)
        # Provisioned deps present: the deps-dir script (shebang:
        # sys.executable, ABI-consistent with the provisioned wheels) wins.
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        deps_script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(deps_script)
        assert venv_provided_command(tmp_path, "my-tool") == str(deps_script)

    def test_a_skeleton_venv_console_script_is_bypassed(self, tmp_path: Any) -> None:
        """A console script under a NON-usable venv (skeleton / no working
        interpreter) is skipped: its shebang interpreter would not carry the
        app's environment. The deps-dir script is used instead."""
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        # venv dir exists but has no runnable interpreter -> not usable.
        _write_runnable(tmp_path / ".venv" / scripts / name)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        deps_script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(deps_script)
        assert venv_provided_command(tmp_path, "my-tool") == str(deps_script)


# ---------------------------------------------------------------------------
# Spawn body: dispatch branches
# ---------------------------------------------------------------------------


class TestNodeDispatch:
    def test_a_node_backend_without_a_node_binary_is_refused(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: None)
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned without node")
        )
        with caplog.at_level(logging.ERROR):
            assert bmod._start_app_backend_body("nodeless", _manifest("server.js")) is None
        assert any("no node binary found" in r.message for r in caplog.records)

    def test_a_js_entry_runs_under_node_and_installs_npm_deps(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp", _manifest("server.js"))
        assert seen["argv"][0] == "/usr/bin/node"
        assert seen["kwargs"]["env"]["NODE_ENV"] == "production"
        assert runs == [["/usr/bin/npm", "install", "--production", "--no-audit", "--no-fund"]]

    def test_npm_install_is_skipped_when_node_modules_is_present(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        (spawn_root / "node_modules").mkdir()
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(
            bmod, "_find_npm_binary", lambda: pytest.fail("npm resolved despite node_modules")
        )
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp2", _manifest("server.js"))
        assert runs == []

    def test_a_failed_npm_install_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        _record_runs(monkeypatch, exc=RuntimeError("registry down"))
        _capture_popen(monkeypatch)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("nodeapp3", _manifest("server.js"))
        assert any("Failed to install npm deps" in r.message for r in caplog.records)

    def test_an_explicit_node_type_wins_over_the_filename_heuristic(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``type: node`` must route a non-.js entry to node, not the Python branch."""

        (spawn_root / "launch.bundle").write_text("// noop\n")
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "nodeapp4", _manifest("launch.bundle", backend_type="node")
            )
        assert seen["argv"][0] == "/usr/bin/node"


class TestAsgiDispatch:
    _ASGI_SRC = "from fastapi import FastAPI\napp = FastAPI()\nimport uvicorn\n"

    def test_ambient_pythonpath_keeps_user_site_for_uvicorn(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """An inherited PYTHONPATH is not proof that this launcher supplied
        uvicorn's import path. A user-site-only uvicorn must remain reachable."""
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        monkeypatch.setenv("PYTHONPATH", "/operator/own")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "asgi-ambient-path", _manifest("app.py", backend_type="asgi")
            )
        assert seen["argv"][:3] == [sys.executable, "-m", "uvicorn"]
        assert seen["kwargs"]["env"]["PYTHONPATH"] == "/operator/own"

    @pytest.mark.parametrize(
        ("entry_name", "backend_type", "contents"),
        [
            pytest.param("app.py", "asgi", _ASGI_SRC, id="asgi"),
            pytest.param("server.py", "", "x = 1\n", id="plain"),
        ],
    )
    def test_bundled_interpreter_disables_user_site_for_python_backends(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        bundled_python_with_user_site,
        entry_name: str,
        backend_type: str,
        contents: str,
    ) -> None:
        (spawn_root / entry_name).write_text(contents)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                f"bundled-{entry_name}",
                _manifest(entry_name, backend_type=backend_type),
            )
        assert seen["argv"][:2] == [sys.executable, "-s"]

    def test_a_sniffed_asgi_entry_is_served_by_uvicorn(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        (spawn_root / "backend").mkdir()
        (spawn_root / "backend" / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi", _manifest("backend/app.py"))
        assert seen["argv"][1:4] == ["-s", "-m", "uvicorn"]
        assert seen["argv"][4] == "backend.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root)

    def test_a_src_layout_asgi_entry_runs_from_the_src_root(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        """Without the src/ rewrite uvicorn cannot import the declared module."""

        pkg = spawn_root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "asgi-src", _manifest("src/pkg/app.py", backend_type="asgi")
            )
        assert seen["argv"][4] == "pkg.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root / "src")

    def test_the_app_venv_interpreter_is_preferred_when_present(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        # A REAL venv: resolve_app_python now probes the interpreter (runs it
        # and checks sys.prefix + ABI), so a stub file fails the probe. A
        # bootstrap skeleton or wrong-ABI copy is rejected by that probe;
        # provisioned deps (absent here) would pin sys.executable instead.
        from kiro_crew.apps.interpreter import venv_python_path

        _create_real_venv(spawn_root)
        venv_py = venv_python_path(spawn_root)
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi-venv", _manifest("app.py"))
        assert seen["argv"][0] == str(venv_py)

    def test_nonbundled_module_builtin_keeps_user_site_and_rejects_app_deps(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """A module-style builtin (entry is None) runs TRUSTED package code.
        A requirements.txt or .kirocrew-deps sitting in its writable app dir
        must NOT be provisioned or prepended to its PYTHONPATH - that would
        let agent-authored wheels load ahead of the trusted module."""
        from kiro_crew.apps.interpreter import app_deps_dir

        # Both attack surfaces present in the writable app dir:
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        app_deps_dir(spawn_root).mkdir(parents=True)
        (app_deps_dir(spawn_root) / "evil.py").write_text("x = 1\n")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        # A module-style entry point: dotted, no separator, no extension, and
        # no such file under the app root.
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("mod-builtin", _manifest("kiro_crew.apps.builtins.demo"))
        assert seen["argv"] == [
            sys.executable,
            "-m",
            "kiro_crew.apps.builtins.demo",
        ]
        # No pip install ran, and the deps dir is NOT on the child PYTHONPATH.
        assert not any("install" in argv for argv in runs), runs
        child_pp = seen["kwargs"]["env"].get("PYTHONPATH", "")
        assert str(app_deps_dir(spawn_root)) not in child_pp, child_pp


# ---------------------------------------------------------------------------
# Spawn body: environment construction and the Popen tail
# ---------------------------------------------------------------------------


class TestSpawnEnvironment:
    def test_platform_overrides_and_the_proxy_secret_reach_the_backend(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """minimal_env() strips these, so the explicit forwards are the contract."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / ".app_secret").write_text("  s3cr3t  \n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", "/checkout")
        monkeypatch.setenv("KIROCREW_EDITION_DIR", "/edition")
        monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/kirocrew")
        monkeypatch.setenv("KIROCREW_DEVFLEET_BIN_GH", "/opt/bin/gh")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("envapp", _manifest("server.py"))
        env = seen["kwargs"]["env"]
        assert env["KIROCREW_PROJECT_DIR"] == "/checkout"
        assert env["KIROCREW_EDITION_DIR"] == "/edition"
        assert env["KIROCREW_DEVFLEET_REPO"] == "/opt/kirocrew"
        assert env["KIROCREW_DEVFLEET_BIN_GH"] == "/opt/bin/gh"
        assert env["KIROCREW_PROXY_SECRET"] == "s3cr3t"
        assert env["KIROCREW_APP_NAME"] == "envapp"

    def test_a_missing_proxy_secret_is_tolerated(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nosecret", _manifest("server.py"))
        assert "KIROCREW_PROXY_SECRET" not in seen["kwargs"]["env"]

    def test_an_audit_sink_failure_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")

        def _boom() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _boom)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("audit-down", _manifest("server.py"))
        assert seen["argv"]


class TestSpawnOutcome:
    def test_a_surviving_child_is_recorded_and_persisted(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: True)
        recorded: list[tuple[str, int, int, str | None]] = []
        monkeypatch.setattr(
            bmod,
            "_record_app_pid",
            lambda name, pid, port, instance=None: recorded.append((name, pid, port, instance)),
        )
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(pid=777))
        ap = bmod._start_app_backend_body("okapp", _manifest("server.py"))
        assert ap is not None
        assert ap.pid == 777
        # Surviving the bind is NOT health: the health loop owns that transition.
        assert ap.healthy is False
        assert bmod._processes["okapp"] is ap
        # Exact arity, not a slice: the unpack fails if the call grows another
        # argument, which is what keeps this a ratchet on the recorded call.
        assert len(recorded) == 1
        name, pid, port, instance = recorded[0]
        assert (name, pid, port) == ("okapp", 777, ap.port)
        # The spawn's incarnation token is persisted WITH the pid: it is the only
        # thing that can vouch this backend's process group after the leader dies,
        # and a row without it costs the startup reap that group entirely. The
        # value is a fresh uuid per spawn, so only its presence is pinned.
        assert instance

    def test_a_child_that_dies_on_its_bind_is_not_reported_as_started(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 'started' record for a dead pid makes the proxy 502 and respawn forever."""

        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: False)

        def _popen(_argv: Any, **kwargs: Any) -> Any:
            # Write the crash reason the real child would have logged.
            kwargs["stdout"].write("OSError: [Errno 98] Address already in use\n")
            kwargs["stdout"].flush()
            return _FakeProc(returncode=1)

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        with caplog.at_level(logging.ERROR):
            assert bmod._start_app_backend_body("dyingapp", _manifest("server.py")) is None
        assert any("PORT COLLISION" in r.getMessage() for r in caplog.records)
        assert "dyingapp" not in bmod._processes


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStopSpawnedBackend:
    @pytest.fixture()
    def kills(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        recorded: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, sig: recorded.append((pid, sig)),
        )
        return recorded

    def test_a_live_child_gets_sigterm(self, kills: list[tuple[int, int]]) -> None:
        proc = _fake_proc(pid=555)
        with bmod._lock:
            bmod._processes["spawned"] = AppProcess(
                app_name="spawned", port=9100, pid=555, proc=proc
            )
        assert bmod.stop_app_backend("spawned") is True
        assert kills == [(555, bmod.platform_compat.SIGTERM)]
        assert "spawned" not in bmod._processes

    def test_a_child_that_ignores_sigterm_is_escalated_to_sigkill(
        self, kills: list[tuple[int, int]]
    ) -> None:
        proc = _fake_proc(pid=556)
        proc.wait_raises = True
        with bmod._lock:
            bmod._processes["stubborn"] = AppProcess(
                app_name="stubborn", port=9100, pid=556, proc=proc
            )
        assert bmod.stop_app_backend("stubborn") is True
        assert kills == [
            (556, bmod.platform_compat.SIGTERM),
            (556, bmod.platform_compat.SIGKILL),
        ]

    def test_a_vanished_child_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _gone(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _gone)
        with bmod._lock:
            bmod._processes["gone"] = AppProcess(
                app_name="gone", port=9100, pid=557, proc=_fake_proc(pid=557)
            )
        assert bmod.stop_app_backend("gone") is True

    def test_a_failing_log_handle_close_is_swallowed(self, kills: list[tuple[int, int]]) -> None:
        handle = MagicMock()
        handle.close.side_effect = OSError("already closed")
        with bmod._lock:
            bmod._processes["loggy"] = AppProcess(
                app_name="loggy", port=9100, pid=558, proc=_fake_proc(pid=558), log_fh=handle
            )
        assert bmod.stop_app_backend("loggy") is True
        handle.close.assert_called_once()


class TestStopAdoptedBackend:
    @pytest.fixture(autouse=True)
    def _fast_wait(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

    @pytest.fixture()
    def kills(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        return recorded

    def _track(self, **kwargs: Any) -> AppProcess:
        ap = AppProcess(app_name="ext", port=bmod._MIN_PORT + 12, pid=0, proc=None, **kwargs)
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        return ap

    def test_an_adopted_backend_with_no_recorded_pids_is_left_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Killing unknown PIDs on a port could take out an unrelated service."""

        ap = self._track(healthy=True)
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is False
        assert any("refusing to kill unknown processes" in r.message for r in caplog.records)
        # Tracking is restored so a retry after re-adoption is possible.
        assert bmod._processes["ext"] is ap
        assert bmod._allocated_ports["ext"] == ap.port

    def test_an_ordinary_adopted_stop_leaving_a_replacement_keeps_the_row(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: an ORDINARY adopted stop (``_retry_if_serving`` is None) signals
        the recorded PIDs and reports success, but the app's own spawn-tree
        supervisor can leave a REPLACEMENT listener on the port. The pidfile row is
        the only handle a later re-enable has to re-attribute that survivor; dropped,
        re-enable refuses the app's own replacement as "no spawn recorded" with no
        self-correcting path. The row must be put back when the port still listens,
        matching the spawned-root ordinary-stop restore.
        """

        row = {
            "pid": 111,
            "start_time": "st-111",
            "port": bmod._MIN_PORT + 12,
            "spawn_instance": "sp-a",
        }
        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        bmod._write_pidfile({"ext": dict(row)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        # The recorded PID is signalled and gone, but a replacement supervisor still
        # answers the port -> ordinary stop tolerates it and returns success.
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: True)
        assert bmod.stop_app_backend("ext") is True
        # The recovery row survives so the replacement stays attributable.
        assert bmod._read_pidfile().get("ext") == row

    def test_an_ordinary_adopted_stop_with_a_clean_port_drops_the_row(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The complement: when the port is silent after an ordinary adopted stop
        (no replacement survived), the row is dropped as before -- re-persisting it
        on every clean stop would let a later start re-adopt a backend that genuinely
        stopped instead of launching fresh."""

        row = {
            "pid": 111,
            "start_time": "st-111",
            "port": bmod._MIN_PORT + 12,
            "spawn_instance": "sp-a",
        }
        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        bmod._write_pidfile({"ext": dict(row)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: False)
        assert bmod.stop_app_backend("ext") is True
        assert "ext" not in bmod._read_pidfile()

    def test_only_identity_verified_pids_are_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revalidation is process identity, not a port heuristic: a PID whose
        live start time does not match the adoption record was recycled and
        must not be signalled, whatever it listens on now."""

        self._track(
            adopted_pids=[111, 222],
            adopted_start_times={111: "st-111", 222: "st-222"},
            healthy=True,
        )
        monkeypatch.setattr(
            bmod, "_proc_start_time", lambda pid: {111: "st-111", 222: "st-999"}.get(pid)
        )
        # 222 stays alive (it is the recycled process we must not touch);
        # 111 dies on SIGTERM so no SIGKILL escalation muddies the record.
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda pid: pid == 222)
        assert bmod.stop_app_backend("ext") is True
        assert kills == [(111, bmod.platform_compat.SIGTERM)]

    def test_a_recycled_pid_is_not_signalled_even_when_it_listens(
        self,
        kills: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exact residual the identity guard closes: the adopted backend
        exits, the OS recycles its PID onto ANOTHER listener of the same port
        (any local address, including a v6-only wildcard) — the start-time
        mismatch keeps it from being signalled."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-old"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-recycled")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is True
        assert kills == []
        assert any("identity does not match" in r.message for r in caplog.records)

    def test_an_exited_backend_signals_nothing(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead PID reads back no start time: nothing to signal, no warning
        spam for a process that simply finished."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_a_pid_recycled_during_the_graceful_wait_is_not_sigkilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escalation re-reads identity: a PID whose start time changed
        during the 2s graceful wait was recycled and must not receive the
        destructive SIGKILL, on any platform."""

        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        # Identity matches for the SIGTERM selection, then flips before the
        # escalation re-read (the PID was recycled during the graceful wait).
        reads = iter(["st", "st-recycled"])
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: next(reads))
        assert bmod.stop_app_backend("ext") is True
        assert recorded == [(111, bmod.platform_compat.SIGTERM)]

    def test_a_pin_refusal_signals_nothing_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """kill_pid_pinned returning False means the process exited between the
        identity check and the pin — there is nothing left to stop, and no
        signal may be sent to whatever holds the PID now."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda _pid, _st, _sig: False)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        with caplog.at_level(logging.INFO):
            assert bmod.stop_app_backend("ext") is True
        assert any("pinned SIGTERM" in r.message for r in caplog.records)

    def test_an_unreadable_identity_is_never_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token was recorded at adoption (identity unreadable): the PID
        cannot be positively named, so it is skipped - fail toward not
        killing, per the process_start_time contract."""

        self._track(adopted_pids=[111], adopted_start_times={}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-live")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_nonpositive_recorded_pids_are_never_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(
            adopted_pids=[0, -1],
            adopted_start_times={0: "st-0", -1: "st-1"},
            healthy=True,
        )
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: {0: "st-0", -1: "st-1"}.get(pid))
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_a_survivor_is_escalated_to_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        assert bmod.stop_app_backend("ext") is True
        assert recorded == [
            (111, bmod.platform_compat.SIGTERM),
            (111, bmod.platform_compat.SIGKILL),
        ]

    def test_an_unsignalable_pid_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(_pid: int, _start_time: str, _sig: int) -> bool:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _denied)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        assert bmod.stop_app_backend("ext") is True

    def test_an_unexpected_stop_failure_restores_tracking(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Losing the record would orphan the backend with no way to retry."""

        def _bad(_pid: int, _start_time: str, _sig: int) -> bool:
            raise ValueError("bad signal")

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _bad)
        ap = self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is False
        assert any("Failed to stop adopted backend" in r.message for r in caplog.records)
        assert bmod._processes["ext"] is ap
        assert bmod._allocated_ports["ext"] == ap.port


class TestWaitForPids:
    def test_polling_stops_as_soon_as_every_pid_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = [bmod.platform_compat.PID_ALIVE, bmod.platform_compat.PID_DEAD]

        def _liveness(_pid: int) -> str:
            return states.pop(0) if states else bmod.platform_compat.PID_DEAD

        monkeypatch.setattr(bmod.platform_compat, "pid_liveness", _liveness)
        monkeypatch.setattr(bmod.time, "sleep", lambda _s: None)
        bmod._wait_for_pids([111], timeout=5.0)
        assert states == []

    def test_an_unsignalable_pid_counts_as_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EPERM means 'not ours' — waiting the full deadline on it is pure latency."""

        monkeypatch.setattr(
            bmod.platform_compat,
            "pid_liveness",
            lambda _pid: bmod.platform_compat.PID_UNSIGNALABLE,
        )
        monkeypatch.setattr(
            bmod.time, "sleep", lambda _s: pytest.fail("slept on an unsignalable pid")
        )
        bmod._wait_for_pids([111], timeout=5.0)

    def test_an_already_elapsed_deadline_polls_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bmod.platform_compat, "pid_liveness", lambda _pid: pytest.fail("polled")
        )
        bmod._wait_for_pids([111], timeout=0.0)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_the_proxy_port_is_only_published_once_the_backend_is_healthy() -> None:
    """Publishing a pre-health port routes user traffic at a dead socket."""

    with bmod._lock:
        bmod._processes["p"] = AppProcess(app_name="p", port=9111, pid=1, healthy=False)
    assert bmod.get_app_backend_port("p") is None
    with bmod._lock:
        bmod._processes["p"].healthy = True
    assert bmod.get_app_backend_port("p") == 9111
    assert bmod.get_app_backend_port("absent") is None


# ---------------------------------------------------------------------------
# Pidfile helpers
# ---------------------------------------------------------------------------


class TestPidfileHelpers:
    def test_a_corrupt_pidfile_reads_as_empty_and_is_reported(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently swallowing it would disable the very reap it exists for."""

        bmod._pidfile_path().write_text("{not json")
        with caplog.at_level(logging.WARNING):
            assert bmod._read_pidfile() == {}
        assert any("pidfile unreadable" in r.message for r in caplog.records)

    def test_a_non_mapping_pidfile_reads_as_empty(self, tmp_path: Any) -> None:
        bmod._pidfile_path().write_text("[1, 2, 3]")
        assert bmod._read_pidfile() == {}

    def test_an_unwritable_pidfile_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(bmod, "atomic_write", _boom)
        bmod._write_pidfile({"app": {"pid": 1}})  # must not raise

    def test_recording_a_pid_never_breaks_a_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_pid: int) -> str:
            raise RuntimeError("ps exploded")

        monkeypatch.setattr(bmod, "_proc_start_time", _boom)
        bmod._record_app_pid("app", 4321, 9100)  # must not raise
        assert bmod._read_pidfile() == {}

    def test_forgetting_a_pid_never_breaks_a_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> dict[str, dict[str, Any]]:
            raise RuntimeError("disk gone")

        monkeypatch.setattr(bmod, "_read_pidfile", _boom)
        bmod._forget_app_pid("app")  # must not raise


class TestProcStartTime:
    """The wrapper must not re-implement the per-platform probe.

    Every platform source (Linux /proc field 22, the Windows creation
    FILETIME, the BSD ``ps`` leg) and its fail-safe behaviour is pinned in
    test_platform_compat::TestProcessStartTime. What matters here is that this
    module reads identity from that shim, because a /proc-or-ps probe answers
    None for every pid on Windows and a recorded None makes the stale-reap
    decline to confirm any backend at all.
    """

    def test_it_delegates_to_the_platform_shim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _probe(pid: int) -> str:
            seen.append(pid)
            return "ST-FROM-SHIM"

        monkeypatch.setattr(bmod.platform_compat, "process_start_time", _probe)
        assert bmod._proc_start_time(4242) == "ST-FROM-SHIM"
        assert seen == [4242]


class TestGateMcpRegistration:
    def test_a_healthy_backend_is_registered_with_its_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, int]] = []
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.reregister_app_mcp_servers",
            lambda name, live_port, io_failures=None: seen.append((name, live_port)),
        )
        bmod._gate_mcp_registration("app", 9133, healthy=True)
        assert seen == [("app", 9133)]

    def test_an_unhealthy_backend_has_its_entries_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dead MCP url breaks every kiro-cli session, not just this app."""

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", lambda _n: 2)
        with caplog.at_level(logging.WARNING):
            bmod._gate_mcp_registration("app", 9133, healthy=False)
        assert any("Scrubbed 2 MCP server(s)" in r.getMessage() for r in caplog.records)

    def test_a_reconcile_failure_never_crashes_the_health_loop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("mcp.json locked")

        monkeypatch.setattr("kiro_crew.apps.bridges.reregister_app_mcp_servers", _boom)
        with caplog.at_level(logging.WARNING):
            bmod._gate_mcp_registration("app", 9133, healthy=True)
        assert any("Health-gated MCP registration failed" in r.message for r in caplog.records)


class TestHealthCheckLoop:
    @pytest.fixture(autouse=True)
    def _instant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)

    def test_an_error_status_is_retried_and_then_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: gate.append((name, port, healthy)),
        )
        attempts = {"n": 0}

        def _urlopen(*_a: Any, **_k: Any) -> Any:
            attempts["n"] += 1
            return _FakeResp(500)

        monkeypatch.setattr(bmod, "loopback_urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["sick"] = AppProcess(app_name="sick", port=9134)
        bmod._health_check_loop(
            bmod._processes.get("sick") or bmod.AppProcess(app_name="sick", port=9134), "/health"
        )
        assert attempts["n"] == 2
        assert gate == [("sick", 9134, False)]

    def test_an_app_stopped_between_the_probe_and_the_commit_is_not_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registering here would write the dead-url entry the gate exists to avoid."""

        gate: list[Any] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: gate.append((name, port, healthy)),
        )

        def _urlopen(*_a: Any, **_k: Any) -> Any:
            # The disable lands after the top-of-loop guard, before the commit.
            with bmod._lock:
                bmod._processes.pop("racy", None)
            return _FakeResp(200)

        monkeypatch.setattr(bmod, "loopback_urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["racy"] = AppProcess(app_name="racy", port=9135)
        bmod._health_check_loop(
            bmod._processes.get("racy") or bmod.AppProcess(app_name="racy", port=9135), "/health"
        )
        assert gate == []


class TestRestartExitedBackend:
    @pytest.fixture(autouse=True)
    def _fast_backoff(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        delays: list[float] = []
        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: delays.append(delay) or False,
            ),
        )
        return delays

    @staticmethod
    def _track(name: str = "app") -> AppProcess:
        ap = AppProcess(
            app_name=name,
            port=9136,
            pid=617,
            proc=_fake_proc(pid=617, returncode=-15),
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes[name] = ap
            bmod._allocated_ports[name] = ap.port
        return ap

    def test_an_exited_tracked_backend_restarts_through_start_path(
        self, monkeypatch: pytest.MonkeyPatch, _fast_backoff: list[float]
    ) -> None:
        ap = self._track()
        started: list[str] = []
        replacement = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod,
            "_demote",
            lambda record, reason: setattr(record, "mcp_healthy", False),
        )

        def _start(name: str) -> AppProcess:
            started.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._watch_backend_health_sweeps(ap, "/health")
        assert started == ["app"]
        assert _fast_backoff[-1:] == [0.0]
        assert bmod._processes["app"] is replacement

    @pytest.mark.parametrize("enabled", [False, None])
    def test_disabled_or_unknown_app_is_not_restarted(
        self, monkeypatch: pytest.MonkeyPatch, enabled: bool | None
    ) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: enabled)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart without confirmed enablement"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._processes["app"] is ap

    def test_restart_revet_reads_origin_without_syncing_installed_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import manager

        stored = manager.InstalledApp(
            name="app",
            version="1.0.0",
            origin="registry",
            lifecycle="app",
        )
        manifest = SimpleNamespace(version="2.0.0", backend=SimpleNamespace(entryPoint="server.py"))
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(bmod, "_read_installed", lambda _name: stored)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr(bmod, "app_admission_denied", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            manager,
            "_write_installed",
            lambda *_args, **_kwargs: pytest.fail("restart re-vet wrote installed metadata"),
        )

        assert bmod._activation_denied("app", "restart") == bmod.ActivationVerdict()
        assert stored.version == "1.0.0"

    def test_restart_stops_when_nonbuiltin_activation_is_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="registry"),
        )
        monkeypatch.setattr(
            bmod,
            "app_admission_denied",
            lambda _name, **_kwargs: "signature required",
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("denied restart reached the spawn seam"),
        )

        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is False

        assert (
            sum(
                "backend not restarted: blocked by activation policy" in record.message
                for record in caplog.records
            )
            == 1
        )
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="signature required",
        )

    def test_admission_revet_error_stops_restart_and_audits_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="registry")
        )

        def _raise_admission(_name: str, **_kwargs: Any) -> str:
            raise RuntimeError("signature store unreadable")

        monkeypatch.setattr(bmod, "app_admission_denied", _raise_admission)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("admission re-vet error reached the spawn seam"),
        )

        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="admission re-vet error: signature store unreadable",
        )

    def test_platform_composition_error_stops_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)

        def _raise_composition(_name: str, **_kwargs: Any) -> str:
            raise bmod.PlatformCompositionError("governance composition failed")

        monkeypatch.setattr(bmod, "_app_activation_denied", _raise_composition)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("composition error reached the spawn seam"),
        )

        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="platform composition error: governance composition failed",
        )

    def test_governance_evaluation_error_refuses_one_attempt_then_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        _fast_backoff: list[float],
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        strict_calls: list[bool] = []

        def _evaluate(*_args: Any, **kwargs: Any) -> Any:
            strict_calls.append(kwargs.get("fail_closed") is True)
            if len(strict_calls) == 1:
                return SimpleNamespace(
                    permitted=False,
                    reason=f"{bmod.GOVERNANCE_ERROR_REASON}; denied (fail-closed)",
                    rule="default",
                    layer="profile",
                )
            return SimpleNamespace(permitted=True)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            _evaluate,
        )
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="builtin")
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "sel", lambda: audit)

        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True

        assert strict_calls == [True, True]
        assert _fast_backoff == [0.0, 1.0]
        assert bmod._restart_attempts["app"] == 2
        assert bmod._processes["app"] is replacement
        assert (
            sum(
                "restart activation evaluation failed" in record.message
                for record in caplog.records
            )
            == 1
        )
        assert audit.log_api_access.call_args_list == [
            call(
                caller="gateway",
                operation="app_backend_restart",
                outcome="error",
                resources="app",
                error=f"{bmod.GOVERNANCE_ERROR_REASON}; denied (fail-closed)",
            ),
            call(
                caller="gateway",
                operation="app_backend_restart",
                outcome="allowed",
                resources="app attempt=2",
            ),
        ]

    def test_builtin_restart_ignores_signature_admission_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="builtin"),
        )
        monkeypatch.setattr(
            bmod,
            "app_admission_denied",
            lambda _name, **_kwargs: pytest.fail("builtin restart reached signature admission"),
        )

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement

    @pytest.mark.parametrize("audit_sink_fails", [False, True])
    def test_permitted_restart_is_audited_before_the_spawn(
        self, monkeypatch: pytest.MonkeyPatch, audit_sink_fails: bool
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        events: list[str] = []
        audit = MagicMock()
        if audit_sink_fails:
            audit.log_api_access.side_effect = OSError("sel sink unavailable")
        else:
            audit.log_api_access.side_effect = lambda **_kw: events.append("audit")
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="registry")
        )
        monkeypatch.setattr(bmod, "app_admission_denied", lambda _name, **_kwargs: None)

        def _start(name: str) -> AppProcess:
            events.append("spawn")
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="allowed",
            resources="app attempt=1",
        )
        assert events == ["spawn"] if audit_sink_fails else ["audit", "spawn"]

    def test_governance_denial_blocks_builtin_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied",
            lambda _name, **_kwargs: "not in apps allowlist",
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="builtin"),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("governance-denied builtin restarted"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_an_intentionally_dropped_record_is_not_restarted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_gateway_shutdown_prevents_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "shutdown_event", SimpleNamespace(is_set=lambda: True))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart during gateway shutdown"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_ramp_exhaustion_enters_steady_state_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        started: list[str] = []
        enabled = True
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: enabled)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _fail_until_disabled(name: str) -> None:
            nonlocal enabled
            started.append(name)
            if len(started) == 5:
                enabled = False
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _fail_until_disabled)
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is False
        assert started == ["app"] * 5
        assert bmod._restart_attempts["app"] == 5
        assert (
            sum(
                "still failing after 2 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )

    def test_shutdown_during_steady_wait_prevents_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        waits: list[float] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 7)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: waits.append(delay) or True,
            ),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("shutdown must prevent the steady-state spawn"),
        )
        with bmod._lock:
            bmod._restart_attempts["app"] = 2

        assert bmod._restart_exited_backend(ap, -15) is False
        assert waits == [7]

    def test_successor_pid_record_survives_restart_handoff_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        ap.pid_start_time = "old-start"
        bmod._write_pidfile(
            {"app": {"pid": ap.pid, "start_time": ap.pid_start_time, "port": ap.port}}
        )
        successor = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            pid_start_time="new-start",
            proc=_fake_proc(pid=618),
        )
        real_forget = bmod._forget_app_pid_if

        def _race_successor_record(name: str, pid: int, start_time: str | None) -> None:
            bmod._write_pidfile(
                {name: {"pid": successor.pid, "start_time": successor.pid_start_time, "port": 9137}}
            )
            real_forget(name, pid, start_time)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", _race_successor_record)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile()["app"] == {
            "pid": 618,
            "start_time": "new-start",
            "port": 9137,
        }

    def test_a_stop_during_a_failed_spawn_prevents_restore_and_further_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        started: list[str] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        with bmod._lock:
            bmod._restart_attempts["app"] = 2

        def _fail_after_stop(name: str) -> None:
            started.append(name)
            assert bmod.stop_app_backend(name) is False
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _fail_after_stop)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert started == ["app"]
        assert bmod._processes == {}
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_STOP

    def test_a_stop_during_restart_publication_stops_the_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _start(name: str) -> AppProcess:
            # STOP lands after the old record was removed but before publication.
            assert bmod.stop_app_backend(name) is False
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._processes == {}
        assert stopped == [(618, bmod.platform_compat.SIGTERM)]

    def test_disable_then_quick_reenable_during_spawn_keeps_the_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _enabled_start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        def _restart_start(name: str) -> AppProcess:
            # Disable records STOP while the restart owns the spawn. The immediate
            # public start records a later START and reuses/publishes the replacement.
            assert bmod.stop_app_backend(name) is False
            monkeypatch.setattr(bmod, "_start_app_backend", _enabled_start)
            assert bmod.start_app_backend(name) is replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _restart_start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_START
        assert stopped == []

    def test_superseding_start_failure_keeps_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess | None:
            starts.append(name)
            if len(starts) == 1:
                # A public START records intent, but its spawn fails and clears its
                # placeholder before the restart supervisor compares generations.
                with bmod._health_reconcile_lock:
                    with bmod._lock:
                        bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                        bmod._processes.pop(name, None)
                return None
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        def _await(name: str) -> None:
            assert bmod._lock.acquire(blocking=False)
            bmod._lock.release()
            awaits.append(name)
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app", "app"]
        assert bmod._processes["app"] is successor
        assert "app" not in bmod._allocated_ports

    def test_superseding_start_warns_once_after_unresolved_awaits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        with bmod._lock:
            bmod._processes["app"] = placeholder
        monkeypatch.setattr(bmod, "_SETTLE_UNRESOLVED_WARN_AFTER", 2)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        awaits = 0

        def _await(name: str) -> None:
            nonlocal awaits
            awaits += 1
            if awaits == 4:
                with bmod._lock:
                    bmod._processes[name] = successor

        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        with caplog.at_level(logging.WARNING):
            assert bmod._settle_superseding_start("app", ap, None) == "return_true"

        assert awaits == 4
        assert (
            sum("superseding start unresolved after" in record.message for record in caplog.records)
            == 1
        )

    def test_superseding_start_success_ends_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        successor = AppProcess(
            app_name="app",
            port=9137,
            proc=_fake_proc(pid=618),
        )
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> None:
            with bmod._health_reconcile_lock:
                with bmod._lock:
                    bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                    bmod._processes[name] = successor
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", lambda _name: successor)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is successor

    def test_superseding_start_then_stop_cancels_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _start(name: str) -> AppProcess:
            with bmod._health_reconcile_lock:
                with bmod._lock:
                    bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                    bmod._processes[name] = replacement
            return replacement

        def _await(name: str) -> None:
            assert bmod.stop_app_backend(name) is True
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_STOP
        assert stopped == [(618, bmod.platform_compat.SIGTERM)]

    def test_start_failure_during_backoff_restores_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        waits: list[float] = []
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        with bmod._lock:
            bmod._restart_attempts["app"] = 1

        def _wait(delay: float) -> bool:
            waits.append(delay)
            if len(waits) == 1:
                with bmod._health_reconcile_lock:
                    with bmod._lock:
                        bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_START)
                        bmod._processes["app"] = placeholder
            return False

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(is_set=lambda: False, wait=_wait),
        )
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app"]
        assert waits == [1.0, 1.0]
        assert bmod._processes["app"] is successor

    def test_start_failure_at_loop_top_restores_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        awaits: list[str] = []
        starts: list[str] = []
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_START)
                bmod._processes["app"] = placeholder
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app"]
        assert bmod._processes["app"] is successor

    def test_start_failure_in_failed_spawn_handoff_restores_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess | None:
            starts.append(name)
            with bmod._lock:
                if len(starts) == 1:
                    bmod._processes[name] = placeholder
                    return None
                bmod._processes[name] = successor
            return successor

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert starts == ["app", "app"]
        assert awaits == ["app"]
        assert bmod._processes["app"] is successor
        assert "app" not in bmod._allocated_ports

    def test_startup_health_gate_exit_continues_the_restart_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        started: list[str] = []
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618),
        )
        with bmod._lock:
            bmod._restart_attempts["app"] = 1
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            started.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._supervise_backend_health(ap, "/health")
        assert started == ["app"]
        assert bmod._restart_attempts["app"] == 2
        assert bmod._processes["app"] is replacement

    def test_startup_health_gate_tracks_live_process_until_later_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _fake_proc(pid=617)
        ap = AppProcess(
            app_name="app",
            port=9136,
            pid=617,
            proc=proc,
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes["app"] = ap
        restart_calls: list[int | None] = []

        def _startup_gate(record: AppProcess, _health_path: str) -> None:
            assert record.proc is not None
            assert record.proc.poll() is None
            return None

        def _exit_after_startup(_delay: float) -> None:
            proc.returncode = -15

        def _restart(record: AppProcess, returncode: int | None) -> bool:
            assert record is ap
            restart_calls.append(returncode)
            return False

        monkeypatch.setattr(bmod, "_health_check_loop", _startup_gate)
        monkeypatch.setattr(bmod.time, "sleep", _exit_after_startup)
        monkeypatch.setattr(bmod, "_restart_exited_backend", _restart)

        bmod._supervise_backend_health(ap, "/health")

        assert restart_calls == [-15]

    def test_startup_exit_retries_an_unlanded_scrub_before_restarting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        ap.mcp_healthy = None
        restarted: list[str] = []
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        gate_calls = 0
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _gate(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal gate_calls
            gate_calls += 1
            return gate_calls == 2

        def _start(name: str) -> AppProcess:
            restarted.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._supervise_backend_health(ap, "/health")
        assert gate_calls == 2
        assert restarted == ["app"]
        assert bmod._processes["app"] is replacement

    def test_startup_health_gate_exhaustion_warns_once_across_generations(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618, returncode=-15),
            mcp_healthy=False,
        )
        starts: list[str] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 1)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True
            bmod._supervise_backend_health(replacement, "/health")
        assert starts == ["app", "app"]
        assert (
            sum(
                "still failing after 1 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )

    def test_health_gate_keeps_the_restart_attempt_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = AppProcess(app_name="app", port=9138, proc=_fake_proc(pid=619), healthy=False)
        with bmod._lock:
            bmod._processes["app"] = ap
            bmod._restart_attempts["app"] = 2
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda *_a, **_kw: True)
        assert bmod._set_backend_health(ap, healthy=True) is True
        assert bmod._restart_attempts["app"] == 2

    def test_sustained_health_resets_the_restart_attempt_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = AppProcess(
            app_name="app",
            port=9138,
            proc=_fake_proc(pid=619),
            healthy=True,
            mcp_healthy=True,
        )
        sweeps = 0
        with bmod._lock:
            bmod._processes["app"] = ap
            bmod._restart_attempts["app"] = 2
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_args: bmod.HealthProbeOutcome.answered(200)
        )

        def _sleep(_delay: float) -> None:
            nonlocal sweeps
            sweeps += 1
            if sweeps > bmod._RESTART_STABLE_SWEEPS:
                with bmod._lock:
                    bmod._processes.pop("app", None)

        monkeypatch.setattr(bmod.time, "sleep", _sleep)
        with caplog.at_level(logging.INFO):
            bmod._watch_backend_health_sweeps(ap, "/health")
        assert sweeps == bmod._RESTART_STABLE_SWEEPS + 1
        assert "app" not in bmod._restart_attempts
        assert sum("backend recovered after restart" in r.message for r in caplog.records) == 1

    def test_steady_state_success_publishes_replacement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618),
        )
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        with bmod._lock:
            bmod._restart_attempts["app"] = 2
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        assert bmod._restart_attempts["app"] == 3
        assert (
            sum(
                "still failing after 2 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )


# ---------------------------------------------------------------------------
# Boot reconcile
# ---------------------------------------------------------------------------


def _app(
    name: str, *, enabled: bool = True, origin: str = "builtin", manifest: Any = None
) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": enabled,
        "origin": origin,
        "manifest": {"backend": {"entryPoint": "server.py"}} if manifest is None else manifest,
    }


class TestRestartKeepsTheSpawnRowUntilTheRespawnDecides:
    """The exited leader's row is the spawn tree's only witness, so the restart keeps it.

    A restart that drops the row before the respawn asks leaves the adopt branch with
    no record to read: a pre-fork worker or a detached child of this SAME app that is
    still holding the declared port cannot be attributed, the respawn refuses it, and
    the app stays down for as long as its own orphan keeps the port. The drop therefore
    happens once the attempt has decided, and only when nothing the row attributes is
    left tracked.
    """

    ROW: dict[str, Any] = {
        "pid": 617,
        "start_time": "st-617",
        "port": 9136,
        "spawn_instance": "sp-tree",
    }

    @pytest.fixture(autouse=True)
    def _fast_backoff(self, _floor_monkeypatch: pytest.MonkeyPatch) -> list[float]:
        delays: list[float] = []
        _floor_monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: delays.append(delay) or False,
            ),
        )
        _floor_monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        bmod._write_pidfile({"app": dict(self.ROW)})
        return delays

    @staticmethod
    def _track(name: str = "app") -> AppProcess:
        ap = AppProcess(
            app_name=name,
            port=9136,
            pid=617,
            pid_start_time="st-617",
            proc=_fake_proc(pid=617, returncode=-15),
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes[name] = ap
            bmod._allocated_ports[name] = ap.port
        return ap

    def test_the_row_is_still_readable_when_the_respawn_asks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering pin: the respawn's own attribution needs the record intact."""

        ap = self._track()
        seen: list[dict[str, Any] | None] = []

        def _start(name: str) -> AppProcess:
            seen.append(bmod._read_pidfile().get(name))
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=618))
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert seen == [self.ROW]

    def test_a_transient_spawn_failure_that_restores_tracking_keeps_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restored retry must not drop the row a later exit would otherwise take.

        The loop commits to superseding (arming the deferred drop), the respawn then
        fails and ``_settle_superseding_start`` restores ``ap`` to tracking and answers
        ``\"continue\"``. ``ap`` is tracked again with a non-None ``proc``, so the
        adopted-instance guard would NOT save it -- the deferred drop must be DISARMED
        on that restore, or the next exit reaches the ``finally`` and deletes the row of
        a leader that is once again live and tracked, orphaning its group.

        Driven by making the spawn fail and the settlement restore ``ap`` then, on the
        re-entered loop, letting a shutdown end it: the row must survive.
        """

        ap = self._track()
        calls = {"settle": 0}

        def _settle(_name: str, _ap: AppProcess, _replacement: Any) -> str:
            calls["settle"] += 1
            if calls["settle"] == 1:
                # Restore ap to tracking exactly as the real settle's "nothing tracked"
                # branch does, and ask the loop to continue.
                with bmod._lock:
                    bmod._processes["app"] = _ap
                return "continue"
            return "return_false"

        # Spawn fails transiently -> replacement is None; the app stays DECLARED
        # (a manifest with a backend entryPoint), so the loop does not early-return
        # True and falls through to the final settlement.
        monkeypatch.setattr(bmod, "_start_app_backend", lambda _name: None)
        monkeypatch.setattr(
            bmod,
            "get_app_manifest",
            lambda _name: SimpleNamespace(backend=SimpleNamespace(entryPoint="app.py")),
        )
        monkeypatch.setattr(bmod, "_settle_superseding_start", _settle)

        # A shutdown that trips only after the first wait, so the first pass proceeds
        # to the failed spawn + settlement-restore, and the restored second pass exits.
        state = {"waits": 0, "down": False}

        def _wait(_delay: float) -> bool:
            state["waits"] += 1
            if state["waits"] >= 2:
                state["down"] = True
            return state["down"]

        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(is_set=lambda: state["down"], wait=_wait),
        )
        bmod._restart_exited_backend(ap, -15)
        # The row survives: the restored, tracked leader still owns it.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_an_adopted_replacement_keeps_the_row_it_was_attributed_by(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its ownership is re-read from this row on every re-bind, so the row stays."""

        ap = self._track()
        adopted = AppProcess(
            app_name="app",
            port=9136,
            proc=None,
            adopted_pids=[9001],
            adopted_start_times={9001: "st-9001"},
        )

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = adopted
            return adopted

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_a_fresh_spawn_keeps_its_own_row_and_not_the_exited_leader_s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drop stays keyed on the exited leader, so a successor's row survives it."""

        ap = self._track()
        successor = {
            "pid": 618,
            "start_time": "st-618",
            "port": 9137,
            "spawn_instance": "sp-new",
        }

        def _start(name: str) -> AppProcess:
            bmod._write_pidfile({name: dict(successor)})
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=618))
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile().get("app") == successor

    def test_a_respawn_that_produces_nothing_drops_the_stale_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is tracked, so the row names a process that is gone and it goes."""

        ap = self._track()
        monkeypatch.setattr(bmod, "_start_app_backend", lambda _name: None)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest(""))
        assert bmod._restart_exited_backend(ap, -15) is True
        assert "app" not in bmod._read_pidfile()

    def test_an_activation_denial_drops_the_row_on_its_early_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removal covers every decided exit, including the ones before the spawn."""

        ap = self._track()
        monkeypatch.setattr(
            bmod,
            "_activation_denied",
            lambda *_args: bmod.ActivationVerdict(denied="not admitted"),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("a denied restart must not spawn"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._read_pidfile()

    def test_every_retry_can_still_read_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retrying loop asks attribution again, so the row outlives each attempt."""

        ap = self._track()
        seen: list[dict[str, Any] | None] = []
        verdicts = [
            bmod.ActivationVerdict(denied="evaluator down", transient=True),
            bmod.ActivationVerdict(denied="not admitted"),
        ]

        def _verdict(*_args: Any) -> Any:
            seen.append(bmod._read_pidfile().get("app"))
            return verdicts[min(len(seen) - 1, len(verdicts) - 1)]

        monkeypatch.setattr(bmod, "_activation_denied", _verdict)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("neither verdict admits a spawn"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert seen == [self.ROW, self.ROW]

    def test_a_dropped_record_leaves_a_successor_s_own_row_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deliberate stop or a successor owns the row; the drop keys on the leader."""

        ap = self._track()
        successor = {"pid": 618, "start_time": "st-618", "port": 9137, "spawn_instance": "sp-2"}
        bmod._write_pidfile({"app": dict(successor)})
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
                bmod._processes["app"] = AppProcess(
                    app_name="app", port=9137, proc=_fake_proc(pid=618)
                )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._read_pidfile().get("app") == successor

    def test_an_identity_moved_stop_leaves_the_row_for_the_winning_operation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity moved (a stop popped the slot), so the restart loop does NOT own
        the row -- the winning stop does.

        The restart loop is the LOSING party once another lifecycle operation has taken
        the slot: a stop that popped it already decided the row's fate (an ordinary stop
        that tolerated a surviving port-holder deliberately RETAINS the row so a later
        re-enable can re-attribute it). The restart loop dropping it here would override
        that decision and strand the survivor unmanageable, so on every identity-moved
        verdict the row is left exactly as the winning operation left it.
        """

        ap = self._track()
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        # The row the stop left in place survives the restart loop -- the loop does not
        # delete what another lifecycle operation owns.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_a_transient_early_return_keeps_the_row_for_the_surviving_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An early return BEFORE the supersede commit must not drop the row.

        The leader has exited but its group can still hold the declared port, and the
        loop returns early -- ``installed.json`` reads transiently unreadable (the
        tri-state ``_app_enabled_state`` is exactly for that), or a shutdown lands --
        before it ever commits to superseding this leader. The base dropped the row
        only AFTER those guards, so dropping it here would leave the surviving group
        the app's port with nothing naming it: the reap has no row to census and the
        next adoption reports no spawn recorded. The row therefore stays.
        """

        ap = self._track()
        # The tracked proc has exited (returncode set), so the loop passes the
        # liveness check and reaches the enabled-state guard, which reads as
        # "transiently unreadable" (None) -> an early ``return False`` before the
        # supersede commit.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: None)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not respawn on a transient early return"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        # The row survives: the surviving group still has a witness for the reap and
        # for a later attribution.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_the_tracking_lock_is_released_before_the_pidfile_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: the row delete goes through ``_forget_app_pid_if`` -> the
        cross-process ``_pidfile_lock`` flock, which waits up to 300s under
        contention. ``_forget_exited_leader_row`` runs on the supervisor THREAD;
        holding the in-process ``_lock`` across that wait stalls every on-loop
        ``list_app_processes()`` for the same window (a watchdog-exit hazard). Assert
        ``_lock`` is NOT held while the pidfile write runs — only the in-memory
        successor check is under it."""
        ap = self._track()
        # Remove the live-handle tracking entry so the drop is NOT short-circuited by
        # the ``proc is None`` successor check — we want the delete path to run.
        with bmod._lock:
            bmod._processes.pop("app", None)

        lock_held_during_write = {"value": None}
        real_forget = bmod._forget_app_pid_if

        def _probe_forget(name, pid, start_time):
            # If ``_lock`` is still held here, a concurrent thread (e.g. the event
            # loop in list_app_processes) could not acquire it. acquire(blocking=False)
            # returns False when the lock is held by anyone, True when it is free.
            got = bmod._lock.acquire(blocking=False)
            if got:
                lock_held_during_write["value"] = False
                bmod._lock.release()
            else:
                lock_held_during_write["value"] = True
            return real_forget(name, pid, start_time)

        monkeypatch.setattr(bmod, "_forget_app_pid_if", _probe_forget)
        bmod._forget_exited_leader_row("app", ap)

        assert lock_held_during_write["value"] is False, (
            "the tracking _lock must be released before the pidfile write, or the "
            "300s flock wait stalls the event loop via list_app_processes()"
        )
        # And the drop still happened (row is gone), so correctness is preserved.
        assert "app" not in bmod._read_pidfile()

    def test_the_check_and_the_delete_run_under_the_lifecycle_flock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: adoption reuses the row it finds, so an adoption landing between
        the tracking check and the delete would lose its provenance to the pid-matched
        pop. Both run inside the lifecycle flock a start holds across its spawn body."""
        import contextlib as _ctxlib

        ap = self._track()
        with bmod._lock:
            bmod._processes.pop("app", None)
        events: list[str] = []

        @_ctxlib.contextmanager
        def _recording_flock(name: str):
            events.append(f"enter:{name}")
            try:
                yield
            finally:
                events.append(f"exit:{name}")

        real_forget = bmod._forget_app_pid_if

        def _probe_forget(name, pid, start_time):
            events.append("delete")
            return real_forget(name, pid, start_time)

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _recording_flock)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", _probe_forget)
        bmod._forget_exited_leader_row("app", ap)

        assert events == ["enter:app", "delete", "exit:app"]
        assert "app" not in bmod._read_pidfile()

    def test_an_adoption_tracked_under_the_flock_keeps_its_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adoption that wins the flock first is what the check sees: it is tracked
        with no handle of ours, so the row it rests on is not deleted."""
        import contextlib as _ctxlib

        ap = self._track()
        adopted = AppProcess(app_name="app", port=9136, pid=0, proc=None, started_at=0.0)

        @_ctxlib.contextmanager
        def _flock_after_an_adoption(name: str):
            # The racing start held the flock first and published its adoption.
            with bmod._lock:
                bmod._processes[name] = adopted
            yield

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _flock_after_an_adoption)
        bmod._forget_exited_leader_row("app", ap)

        assert bmod._read_pidfile().get("app") == self.ROW

    def test_an_unavailable_flock_keeps_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flock's 300s ceiling raises ``OSError``; this runs in the restart's
        ``finally``, so it keeps the row (it names an exited leader) and does not raise."""
        ap = self._track()
        with bmod._lock:
            bmod._processes.pop("app", None)

        class _Unavailable:
            def __enter__(self) -> None:
                raise OSError("lifecycle lock timeout")

            def __exit__(self, *_exc: object) -> None:
                raise AssertionError("an unentered flock was released")

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", lambda _name: _Unavailable())
        bmod._forget_exited_leader_row("app", ap)

        assert bmod._read_pidfile().get("app") == self.ROW


class TestBootMcpReconcile:
    def test_a_disabled_app_with_mcp_servers_is_scrubbed(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its backend is not running, so its HTTP MCP url points at a dead port."""

        monkeypatch.setattr(
            bmod,
            "list_apps",
            lambda: [_app("off", enabled=False, manifest={"mcpServers": {"x": {}}})],
        )
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["dereg_mcp"] == ["off"]

    def test_a_disabled_app_without_mcp_servers_is_left_alone(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("off", enabled=False, manifest={})])
        bmod.start_enabled_app_backends()
        assert boot_env["dereg_mcp"] == []

    def test_a_failing_scrub_does_not_crash_boot(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> int:
            raise RuntimeError("mcp.json locked")

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", _boom)
        monkeypatch.setattr(
            bmod,
            "list_apps",
            lambda: [_app("off", enabled=False, manifest={"mcpServers": {"x": {}}})],
        )
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot MCP reconcile failed" in r.message for r in caplog.records)


class TestBootResourceReconcile:
    def test_an_admitted_app_is_re_registered_and_its_skills_reconciled(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("good")])
        bmod.start_enabled_app_backends()
        assert boot_env["register"] == ["good"]
        assert boot_env["reconcile_skills"] == ["good"]
        assert boot_env["started"] == ["good"]

    def test_registration_errors_are_surfaced_not_swallowed(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.register_app",
            lambda _n: SimpleNamespace(errors=["skill clash"]),
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("noisy")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("completed with errors" in r.message for r in caplog.records)

    def test_a_failing_reconcile_does_not_crash_boot(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> Any:
            raise RuntimeError("registry corrupt")

        monkeypatch.setattr("kiro_crew.apps.bridges.register_app", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("broken")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot resource reconcile failed" in r.message for r in caplog.records)
        # The backend spawn loop is independent of the reconcile outcome.
        assert boot_env["started"] == ["broken"]

    def test_a_denied_app_has_its_derivative_resources_revoked(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A policy tightened after enable must revoke, not merely decline to start."""

        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied",
            lambda _n, **_kwargs: "not in allowlist",
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("banned")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["dereg_agents"] == ["banned"]
        assert boot_env["dereg_skills"] == ["banned"]
        assert boot_env["dereg_mcp"] == ["banned"]
        assert boot_env["register"] == []
        assert boot_env["started"] == []

    def test_a_failed_revocation_is_logged_as_an_error(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> int:
            raise RuntimeError("agents dir read-only")

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_agents", _boom)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _n, **_kwargs: "banned"
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("banned")])
        with caplog.at_level(logging.ERROR):
            bmod.start_enabled_app_backends()
        assert any("FAILED to revoke resources" in r.message for r in caplog.records)

    def test_a_vetting_error_is_treated_as_a_denial(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed: an unverifiable app must not keep its resources."""

        def _boom(_name: str, **_kw: Any) -> str:
            raise RuntimeError("provenance store unreadable")

        monkeypatch.setattr(bmod, "app_execution_denied", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("unknowable")])
        bmod.start_enabled_app_backends()
        assert boot_env["dereg_agents"] == ["unknowable"]
        assert boot_env["register"] == []

    def test_an_unavailable_bridges_module_aborts_only_the_reconcile(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Boot must still start admitted backends when the reconcile cannot load."""

        monkeypatch.setitem(sys.modules, "kiro_crew.apps.bridges", SimpleNamespace())
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("a"), _app("b")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot resource reconcile unavailable" in r.message for r in caplog.records)
        assert sorted(boot_env["started"]) == ["a", "b"]


class TestBootSpawnGating:
    def test_a_governance_denied_app_is_not_spawned(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _n, **_kwargs: "not allowed"
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("blocked")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []

    def test_an_enabled_app_without_a_backend_is_skipped(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("uionly", manifest={})])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []

    def test_an_admission_revet_error_fails_closed(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An app whose admission cannot be confirmed must not boot unchecked."""

        def _boom(_name: str, **_kw: Any) -> str:
            raise RuntimeError("signature store unreadable")

        monkeypatch.setattr(bmod, "app_admission_denied", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("shady", origin="registry")])
        with caplog.at_level(logging.ERROR):
            assert bmod.start_enabled_app_backends() == []
        assert any("treating as denied" in r.message for r in caplog.records)
        assert boot_env["started"] == []

    def test_a_boot_spawn_error_is_isolated_even_if_the_audit_sink_is_down(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _start(name: str) -> AppProcess | None:
            if name == "bad":
                raise RuntimeError("sandbox unavailable")
            boot_env["started"].append(name)
            return None

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "start_app_backend", _start)
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("bad"), _app("ok")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == ["ok"]


class TestPreclaimFixedPorts:
    def test_two_apps_declaring_the_same_fixed_port_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        port = bmod._MIN_PORT + 13
        manifests = {
            "first": _manifest("s.py", port=str(port)),
            "second": _manifest("s.py", port=str(port)),
        }
        monkeypatch.setattr(bmod, "get_app_manifest", lambda n: manifests.get(n))
        with caplog.at_level(logging.WARNING):
            bmod._preclaim_fixed_ports(["first", "second"])
        assert bmod._allocated_ports == {"first": port}
        assert any("fixed-port pre-claim" in r.message for r in caplog.records)

    def test_an_empty_boot_set_short_circuits(self) -> None:
        assert bmod._start_backends_concurrently([]) == []


# ---------------------------------------------------------------------------
# Remaining defensive branches
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    def test_an_unreadable_extensionless_entry_is_not_a_shell_launcher(self, tmp_path: Any) -> None:
        """A directory passes the executable check but cannot be sniffed."""

        candidate = tmp_path / "launcher"
        candidate.mkdir()
        assert bmod._is_shell_entry(candidate) is False

    def test_refusing_an_unhealthy_occupant_survives_an_audit_sink_failure(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        _install_fake_socket(monkeypatch, connect_exc=None)

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(500))
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        result = bmod._start_app_backend_body(
            "occupied", _manifest("server.py", port=str(bmod._MIN_PORT + 14))
        )
        assert result is None

    def test_npm_install_proceeds_when_the_audit_sink_is_down(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeaudit", _manifest("server.js"))
        assert runs and runs[0][0] == "/usr/bin/npm"

    def test_a_missing_npm_binary_does_not_block_the_node_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependencies may already be vendored; a missing npm is not fatal."""

        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: None)
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nonpm", _manifest("server.js"))
        assert runs == []
        assert seen["argv"][0] == "/usr/bin/node"

    def test_stopping_a_spawned_backend_survives_audit_and_signal_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither a broken audit sink nor a pid that vanished may fail the stop."""

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        signals: list[int] = []

        def _kill(_pid: int, sig: int) -> None:
            signals.append(sig)
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _kill)
        proc = _fake_proc(pid=600)
        proc.wait_raises = True
        with bmod._lock:
            bmod._processes["audit"] = AppProcess(app_name="audit", port=9100, pid=600, proc=proc)
        assert bmod.stop_app_backend("audit") is True
        assert signals == [
            bmod.platform_compat.SIGTERM,
            bmod.platform_compat.SIGKILL,
        ]

    def test_refusing_an_adopted_stop_survives_an_audit_sink_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext", port=9100, pid=0, proc=None, healthy=True
            )
        assert bmod.stop_app_backend("ext") is False
        assert "ext" in bmod._processes

    def test_an_adopted_stop_kill_path_survives_an_audit_sink_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext",
                port=9100,
                pid=0,
                proc=None,
                adopted_pids=[111],
                adopted_start_times={111: "st"},
                healthy=True,
            )
        assert bmod.stop_app_backend("ext") is True
        assert killed == [
            (111, bmod.platform_compat.SIGTERM),
            (111, bmod.platform_compat.SIGKILL),
        ]

    def test_a_boot_admission_denial_survives_an_audit_sink_failure(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "app_admission_denied", lambda _n, **_kw: "unsigned")
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("shady", origin="registry")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []


class TestReapDefensiveBranches:
    @pytest.fixture()
    def matched_orphan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recorded pid that is alive and positively identified."""

        bmod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "ST-1")
        monkeypatch.setattr(
            bmod.platform_compat, "pid_liveness", lambda _pid: bmod.platform_compat.PID_ALIVE
        )
        monkeypatch.setattr(bmod, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(bmod, "_REAP_SIGTERM_GRACE", 0.0)
        monkeypatch.setattr(bmod, "_REAP_POLL_INTERVAL", 0.0)

    def test_malformed_and_nonpositive_entries_are_dropped_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bmod._write_pidfile(
            {
                "bad-type": {"pid": "not-an-int", "start_time": "ST", "port": 9100},
                "zero": {"pid": 0, "start_time": "ST", "port": 9101},
            }
        )
        for _name in ("kill_process_tree", "kill_process_tree_pinned"):
            monkeypatch.setattr(
                bmod.platform_compat,
                _name,
                lambda *_a: pytest.fail("signalled an unusable pidfile entry"),
            )
        assert bmod._reap_stale_app_backends() == 0
        assert bmod._read_pidfile() == {}

    def test_a_pid_that_exits_before_the_signal_is_dropped(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def _gone(_pid: int, _expected: str, _sig: int, **kwargs: Any) -> bool:
            raise ProcessLookupError

        # Patched at the PINNED entry point, which is what the reap calls now.
        # Patching the inner ``kill_process_tree`` would leave the real handle
        # work in front of it and make the case host-dependent.
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _gone)
        assert bmod._reap_stale_app_backends() == 0
        if bmod.platform_compat.IS_WINDOWS:
            assert bmod._read_pidfile() == {
                "app": {"pid": 4321, "start_time": "ST-1", "port": 9100}
            }, "a Windows drain exception does not prove the descendant tree absent"
        else:
            assert bmod._read_pidfile() == {}

    def test_the_reap_survives_an_audit_sink_failure_on_both_signals(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        signals: list[int] = []
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda _pid, _expected, sig, **kwargs: bool(signals.append(sig)) or True,
        )
        assert bmod._reap_stale_app_backends() == 1
        expected = [bmod.platform_compat.SIGTERM]
        if not bmod.platform_compat.IS_WINDOWS:
            expected.append(bmod.platform_compat.SIGKILL)
        assert signals == expected

    def test_a_pid_that_exits_before_the_escalation_is_not_an_error(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def _kill(_pid: int, _expected: str, sig: int, **kwargs: Any) -> bool:
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _kill)
        assert bmod._reap_stale_app_backends() == 1
        assert bmod._read_pidfile() == {}
