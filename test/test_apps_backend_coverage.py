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

import importlib
import json
import logging
import os
import subprocess
import sys
import urllib.error
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

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
    yield
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()


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
    monkeypatch.setattr("kiro_crew.apps.manager._app_activation_denied", lambda _name: None)

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
        skeleton — which the venv-first interpreter policy then prefers while
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
        directory — and for VOLATILE requirements (the only kind that can
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
        the deps_boot shim processes via site.addsitedir — so provisioning
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
        direct references) must disable stamp reuse — the digest cannot
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
        # listing cache has mtime granularity — a module written within the
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
        via a .pth redirect must import under the shim — and the deps
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
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provisioned backend spawns via deps_boot (which addsitedir()s
        the deps dir, processing .pth) rather than a raw interpreter+entry
        argv — and only for the gateway interpreter."""
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
        assert argv[1].endswith("deps_boot.py"), argv
        assert argv[2] == str(deps_dir), argv
        assert argv[3].endswith("server.py"), argv

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
        requirements FILE — handing it the symlink path would resolve
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

    def test_concurrent_provisioning_is_serialized_by_the_deps_lock(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend spawn and a backend-less registration (or two
        registrations) can provision the same app concurrently; unserialized
        they delete each other's staging tree and both fail. The per-app
        file lock admits one transaction at a time — and the stamp check
        runs inside it, so the waiter skips pip on the winner's stamp."""
        import threading
        import time as _time

        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        inside = []
        overlap = []

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
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
            t.join()
        assert errs == ["", ""], errs
        assert not overlap, "two pip transactions ran concurrently"
        from kiro_crew.apps.interpreter import app_deps_dir

        assert (app_deps_dir(spawn_root) / "pkg.py").is_file()

    def test_the_pinned_dir_detects_a_mid_transaction_swap(self, tmp_path: Any) -> None:
        """The link pre-check is a TOCTOU window: a running app can swap
        data/ AFTER validation. The pin holds the directory open and verify()
        refuses once the path names a different inode — the renames go
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

    def test_provisioning_refuses_a_linked_data_directory(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every provisioning operation under data/ would FOLLOW a planted
        link — an app pointing data/ at another app's tree would have the
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
        oversized requirements.txt reads at most cap+1 bytes and refuses —
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
        stamp name must never make the gateway read an arbitrary path — it
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
        requirements off the network — and keeps an OFFLINE restart of a
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
        byte-identical — a requirements-only stamp would skip pip and leave
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
        prior name only. The next start must put it back — and, with a
        matching stamp inside, skip pip — so an offline restart keeps its
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

    def test_a_failed_reinstall_leaves_the_prior_deps_dir_intact(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip fills a staging dir that is swapped in only on success, so a
        failed or interrupted (re)install can never corrupt the live deps dir
        in place — the prior good install keeps serving the spawn."""
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
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip must run as `sys.executable -m pip` — a bare `python3` relies
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
        assert pip_argv[1:3] == ["-m", "pip"], pip_argv
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
        backend to a failed refresh) — but the failure now surfaces as an
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
        PYTHONPATH would SHADOW the gateway's shim — app code running as the
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


@pytest.fixture()
def probe_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the interpreter usability probe without OS confinement.

    The probe's sandbox wrapping is a host capability (absent on Windows CI
    and fail-closed there by design); these tests pin the probe's LOGIC —
    sys.prefix ownership and ABI match against a real venv interpreter — so
    the seams are passed through and the child runs directly.
    """
    from kiro_crew.apps import interpreter as imod

    monkeypatch.setattr(imod.sandbox, "wrap_argv", lambda argv, **_k: (list(argv), None))
    monkeypatch.setattr(imod.sandbox, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(imod.sandbox, "run_limited", lambda argv, **kw: subprocess.run(argv, **kw))


class TestInterpreterResolution:
    def _venv_python(self, root: Any) -> Any:
        from kiro_crew.apps.interpreter import venv_python_path

        return venv_python_path(root)

    def _real_venv(self, root: Any) -> Any:
        """Build a REAL .venv under root (the resolver now probes the
        interpreter, so a stub file no longer passes _venv_is_usable)."""
        import venv as _venv

        _venv.create(root / ".venv", with_pip=False)
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
        and whose pyvenv.cfg names the current minor version — a version
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
        same sanitized environment every app subprocess gets — never the
        gateway's own, which can carry credentials."""
        import venv as _venv

        from kiro_crew.apps import interpreter as imod

        _venv.create(tmp_path / ".venv", with_pip=False)
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
        positive evidence and falls back to sys.executable — the exception
        must never propagate into spawn or registration."""
        import venv as _venv

        from kiro_crew.apps import interpreter as imod

        _venv.create(tmp_path / ".venv", with_pip=False)

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("no sandbox backend")

        monkeypatch.setattr(imod.sandbox, "wrap_argv", _raise)
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_hostile_probe_output_reads_as_not_usable(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        """The probe output is app-controlled: a venv whose interpreter prints
        a null prefix (valid JSON, wrong shape) must read as "not usable" —
        Path(None) raising TypeError out of the probe would break spawn and
        registration."""
        py = self._venv_python(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\necho '[null, %d, %d]'\n" % sys.version_info[:2])
        py.chmod(0o755)
        from kiro_crew.apps import interpreter as imod

        assert imod._venv_is_usable(tmp_path) is False
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_declared_requirements_pin_the_gateway_interpreter(self, tmp_path: Any) -> None:
        """With requirements.txt declared, the deps mechanism owns the
        interpreter: even a probe-passing venv must not be preferred — a
        failed `-m venv` leaves a prefix-valid EMPTY skeleton that passes
        the probe but holds none of the declared dependencies."""
        import sys as _sys
        from unittest import mock

        from kiro_crew.apps import interpreter as imod

        (tmp_path / "requirements.txt").write_bytes(b"uvicorn\n")
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) == _sys.executable
        # without requirements, a usable venv IS preferred
        (tmp_path / "requirements.txt").unlink()
        (tmp_path / ".venv").mkdir()
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) != _sys.executable

    def test_a_deps_dir_console_script_is_resolvable(self, tmp_path: Any) -> None:
        """pip --target puts console scripts in <target>/bin (Scripts on
        Windows); the resolver must find them there now that the gateway never
        creates the venv layout that used to carry them."""
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
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

    def test_a_sniffed_asgi_entry_is_served_by_uvicorn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "backend").mkdir()
        (spawn_root / "backend" / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi", _manifest("backend/app.py"))
        assert seen["argv"][1:3] == ["-m", "uvicorn"]
        assert seen["argv"][3] == "backend.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root)

    def test_a_src_layout_asgi_entry_runs_from_the_src_root(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
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
        assert seen["argv"][3] == "pkg.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root / "src")

    def test_the_app_venv_interpreter_is_preferred_when_present(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        # A REAL venv: resolve_app_python now probes the interpreter (runs it
        # and checks sys.prefix + ABI), so a stub file no longer passes. A
        # bootstrap skeleton or wrong-ABI copy is rejected by that probe;
        # provisioned deps (absent here) would pin sys.executable instead.
        import venv as _venv

        from kiro_crew.apps.interpreter import venv_python_path

        _venv.create(spawn_root / ".venv", with_pip=False)
        venv_py = venv_python_path(spawn_root)
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi-venv", _manifest("app.py"))
        assert seen["argv"][0] == str(venv_py)

    def test_a_module_builtin_never_provisions_or_injects_app_deps(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module-style builtin (entry is None) runs TRUSTED package code.
        A requirements.txt or .kirocrew-deps sitting in its writable app dir
        must NOT be provisioned or prepended to its PYTHONPATH — that would
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
        recorded: list[tuple[str, int, int]] = []
        monkeypatch.setattr(
            bmod,
            "_record_app_pid",
            lambda name, pid, port: (recorded.append((name, pid, port)), True)[1],
        )
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(pid=777))
        ap = bmod._start_app_backend_body("okapp", _manifest("server.py"))
        assert ap is not None
        assert ap.pid == 777
        # Surviving the bind is NOT health: the health loop owns that transition.
        assert ap.healthy is False
        assert bmod._processes["okapp"] is ap
        assert recorded == [("okapp", 777, ap.port)]

    def test_a_child_whose_pid_record_fails_is_torn_down(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unrecorded backend is unstoppable from outside the gateway and
        would survive uninstall — a failed pid-record persist must stop the
        just-spawned backend, not leave it running untracked-on-disk."""
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: True)
        monkeypatch.setattr(bmod, "_record_app_pid", lambda name, pid, port: False)
        stopped: list[str] = []
        monkeypatch.setattr(bmod, "stop_app_backend", lambda name: (stopped.append(name), True)[1])
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(pid=778))
        ap = bmod._start_app_backend_body("recfail", _manifest("server.py"))
        assert ap is None
        assert stopped == ["recfail"]

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
    def _fast_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

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

    def test_only_identity_verified_pids_are_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revalidation is process identity, not a port heuristic: a PID whose
        live start time no longer matches the adoption record was recycled and
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

    def test_an_unreadable_identity_is_never_signalled_and_not_confirmed(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token was recorded at adoption (identity unreadable): the PID can
        no longer be positively named, so it is skipped — fail toward not
        killing, per the process_start_time contract. And because that live
        process might BE the backend, the stop reports UNCONFIRMED (False):
        treating it as success would let an uninstall proceed past a possibly
        live backend on the strength of a stop that touched nothing."""

        self._track(adopted_pids=[111], adopted_start_times={}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-live")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        assert bmod.stop_app_backend("ext") is False
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
            "kiro_crew.apps.manager._app_activation_denied", lambda _n: "not in allowlist"
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
        monkeypatch.setattr("kiro_crew.apps.manager._app_activation_denied", lambda _n: "banned")
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
            "kiro_crew.apps.manager._app_activation_denied", lambda _n: "not allowed"
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
        def _gone(_pid: int, _expected: str, _sig: int) -> bool:
            raise ProcessLookupError

        # Patched at the PINNED entry point, which is what the reap calls now.
        # Patching the inner ``kill_process_tree`` would leave the real handle
        # work in front of it and make the case host-dependent.
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _gone)
        assert bmod._reap_stale_app_backends() == 0
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
            lambda _pid, _expected, sig: bool(signals.append(sig)) or True,
        )
        assert bmod._reap_stale_app_backends() == 1
        assert signals == [
            bmod.platform_compat.SIGTERM,
            bmod.platform_compat.SIGKILL,
        ]

    def test_a_pid_that_exits_before_the_escalation_is_not_an_error(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _kill(_pid: int, _expected: str, sig: int) -> bool:
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _kill)
        assert bmod._reap_stale_app_backends() == 1
        assert bmod._read_pidfile() == {}
