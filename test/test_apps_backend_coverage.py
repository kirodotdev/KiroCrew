"""Additional unit coverage for :mod:`kiro_crew.apps.backend`.

Complements ``test_app_backend.py`` (port allocation, spawn survival, dispatch)
and ``test_app_backend_stale_reap.py`` (pidfile reap safety) by exercising the
branches those files leave untouched:

* the adopt-an-already-healthy-instance path and its refusals,
* the per-app venv / pip and npm dependency-install branches,
* the Node and ASGI dispatch branches,
* ``stop_app_backend``'s adopted-PID revalidation and SIGKILL escalation,
* the pidfile helpers' error paths and ``_proc_start_time``'s two platforms,
* the boot-time MCP + executable-resource reconcile in
  ``start_enabled_app_backends``.

Everything here is hermetic and order-independent: no real process is spawned,
no socket is bound, no network request is made, and no wall-clock duration is
asserted. ``subprocess.Popen`` / ``subprocess.run``, the ``socket`` module,
and ``urllib.request.urlopen`` are stubbed, and the spawn body is frozen at the
``Popen`` seam with a sentinel exception.
"""
from __future__ import annotations

import json
import logging
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
    """Freeze the spawn at Popen and capture the argv + kwargs it built."""

    seen: dict[str, Any] = {}

    def _popen(argv: Any, **kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        raise _StopSpawn()

    monkeypatch.setattr(bmod.subprocess, "Popen", _popen)
    return seen


def _record_runs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: Any = None,
    exc: BaseException | None = None,
) -> list[list[str]]:
    """Record every ``subprocess.run`` argv, optionally failing the call."""

    calls: list[list[str]] = []

    def _run(argv: Any, **_kwargs: Any) -> Any:
        calls.append(list(argv))
        if exc is not None:
            raise exc
        if result is not None:
            return result
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(bmod.subprocess, "run", _run)
    return calls


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
    monkeypatch.setattr(bmod, "resource_limit_preexec", lambda: None)
    monkeypatch.setattr(bmod, "build_resource_limit_preexec", lambda: None)
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
            assert (
                bmod._start_app_backend_body("ranged", _manifest("server.py", port="1")) is None
            )
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
            bmod.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        return spawn_root

    def _run(self, port: int) -> AppProcess | None:
        return bmod._start_app_backend_body("adoptee", _manifest("server.py", port=str(port)))

    def test_a_healthy_instance_is_adopted_with_its_listening_pids(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = bmod._MIN_PORT + 6
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(200))
        _record_runs(
            monkeypatch,
            # A non-numeric line must be skipped, not abort the adoption.
            result=SimpleNamespace(returncode=0, stdout="111\nbogus\n222\n"),
        )
        ap = self._run(port)
        assert ap is not None
        assert ap.proc is None
        assert ap.healthy is True
        assert ap.adopted_pids == [111, 222]
        assert bmod._processes["adoptee"] is ap
        assert bmod._allocated_ports["adoptee"] == port

    def test_adoption_survives_an_audit_sink_failure(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken audit sink must not cost us a healthy running backend."""

        def _boom() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _boom)
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(200))
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=0, stdout="333\n"))
        ap = self._run(bmod._MIN_PORT + 7)
        assert ap is not None
        assert ap.adopted_pids == [333]

    def test_adoption_is_refused_when_no_owning_pid_can_be_recorded(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Adopting without PIDs would leave a backend we can never stop."""

        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(200))
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=1, stdout=""))
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 8) is None
        assert any("cannot record PIDs" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_adoption_is_refused_when_the_pid_probe_is_unavailable(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(200))
        _record_runs(monkeypatch, exc=OSError("no lsof"))
        assert self._run(bmod._MIN_PORT + 9) is None

    def test_an_unhealthy_occupant_blocks_the_spawn_instead_of_colliding(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _refused(*_a: Any, **_k: Any) -> Any:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(bmod.urllib.request, "urlopen", _refused)
        with caplog.at_level(logging.WARNING):
            assert self._run(bmod._MIN_PORT + 10) is None
        assert any("occupied by unhealthy process" in r.message for r in caplog.records)

    def test_an_error_status_counts_as_unhealthy(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(503))
        assert self._run(bmod._MIN_PORT + 11) is None


# ---------------------------------------------------------------------------
# Spawn body: dependency installation
# ---------------------------------------------------------------------------


class TestDependencyInstall:
    def test_requirements_txt_provisions_a_per_app_venv_then_pip_installs(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_text("requests\n")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        assert any("venv" in argv for argv in runs), runs
        assert any("install" in argv for argv in runs), runs

    def test_a_failed_dependency_install_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deps are best-effort: an offline host must still get its backend tried."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_text("requests\n")
        _record_runs(monkeypatch, exc=RuntimeError("no network"))
        _capture_popen(monkeypatch)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("deps-fail", _manifest("server.py"))
        assert any("Failed to install deps" in r.message for r in caplog.records)


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
            bmod.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned without node")
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
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_bin = spawn_root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").write_text("")
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi-venv", _manifest("app.py"))
        assert seen["argv"][0] == str(venv_bin / "python3")


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
            lambda name, pid, port: recorded.append((name, pid, port)),
        )
        monkeypatch.setattr(bmod.subprocess, "Popen", lambda *_a, **_k: _FakeProc(pid=777))
        ap = bmod._start_app_backend_body("okapp", _manifest("server.py"))
        assert ap is not None
        assert ap.pid == 777
        # Surviving the bind is NOT health: the health loop owns that transition.
        assert ap.healthy is False
        assert bmod._processes["okapp"] is ap
        assert recorded == [("okapp", 777, ap.port)]

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

        monkeypatch.setattr(bmod.subprocess, "Popen", _popen)
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

    def test_a_failing_log_handle_close_is_swallowed(
        self, kills: list[tuple[int, int]]
    ) -> None:
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
        monkeypatch.setattr(
            bmod.platform_compat, "kill_pid", lambda pid, sig: recorded.append((pid, sig))
        )
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

    def test_only_pids_still_listening_on_the_port_are_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revalidation guards against a PID recycled since adoption."""

        self._track(adopted_pids=[111, 222], healthy=True)
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=0, stdout="111\n999\n"))
        assert bmod.stop_app_backend("ext") is True
        assert kills == [(111, bmod.platform_compat.SIGTERM)]

    def test_an_unavailable_pid_probe_falls_back_to_the_adopted_set(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(adopted_pids=[111], healthy=True)
        _record_runs(monkeypatch, exc=OSError("no lsof"))
        assert bmod.stop_app_backend("ext") is True
        assert kills == [(111, bmod.platform_compat.SIGTERM)]

    def test_nonpositive_recorded_pids_are_never_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(adopted_pids=[0, -1], healthy=True)
        _record_runs(monkeypatch, exc=OSError("no lsof"))
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_a_survivor_is_escalated_to_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_pid", lambda pid, sig: recorded.append((pid, sig))
        )
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        self._track(adopted_pids=[111], healthy=True)
        _record_runs(monkeypatch, exc=OSError("no lsof"))
        assert bmod.stop_app_backend("ext") is True
        assert recorded == [
            (111, bmod.platform_compat.SIGTERM),
            (111, bmod.platform_compat.SIGKILL),
        ]

    def test_an_unsignalable_pid_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_pid", _denied)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        self._track(adopted_pids=[111], healthy=True)
        _record_runs(monkeypatch, exc=OSError("no lsof"))
        assert bmod.stop_app_backend("ext") is True

    def test_an_unexpected_stop_failure_restores_tracking(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Losing the record would orphan the backend with no way to retry."""

        def _bad(_pid: int, _sig: int) -> None:
            raise ValueError("bad signal")

        monkeypatch.setattr(bmod.platform_compat, "kill_pid", _bad)
        ap = self._track(adopted_pids=[111], healthy=True)
        _record_runs(monkeypatch, exc=OSError("no lsof"))
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

    def test_an_unsignalable_pid_counts_as_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_an_unwritable_pidfile_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(bmod, "atomic_write", _boom)
        bmod._write_pidfile({"app": {"pid": 1}})  # must not raise

    def test_recording_a_pid_never_breaks_a_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_pid: int) -> str:
            raise RuntimeError("ps exploded")

        monkeypatch.setattr(bmod, "_proc_start_time", _boom)
        bmod._record_app_pid("app", 4321, 9100)  # must not raise
        assert bmod._read_pidfile() == {}

    def test_forgetting_a_pid_never_breaks_a_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> dict[str, dict[str, Any]]:
            raise RuntimeError("disk gone")

        monkeypatch.setattr(bmod, "_read_pidfile", _boom)
        bmod._forget_app_pid("app")  # must not raise


class TestProcStartTime:
    def test_linux_reads_the_starttime_field_past_a_parenthesised_comm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Splitting on the FIRST ')' would mis-index any comm containing one."""

        tail = " ".join(str(i) for i in range(4, 24))
        stat = f"4242 (my (odd) proc) S 1 {tail}"

        class _FakeStatPath:
            def __init__(self, _p: str) -> None:
                pass

            def read_text(self) -> str:
                return stat

        monkeypatch.setattr(bmod.sys, "platform", "linux")
        monkeypatch.setattr(bmod, "Path", _FakeStatPath)
        assert bmod._proc_start_time(4242) == "21"

    def test_a_malformed_stat_line_yields_no_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeStatPath:
            def __init__(self, _p: str) -> None:
                pass

            def read_text(self) -> str:
                return "no closing paren here"

        monkeypatch.setattr(bmod.sys, "platform", "linux")
        monkeypatch.setattr(bmod, "Path", _FakeStatPath)
        assert bmod._proc_start_time(4242) is None

    def test_non_linux_shells_out_to_ps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.sys, "platform", "darwin")
        monkeypatch.setattr(
            bmod.subprocess, "check_output", lambda *_a, **_k: b" Mon Jan  1 00:00:00 2024\n"
        )
        assert bmod._proc_start_time(4242) == "Mon Jan  1 00:00:00 2024"

    def test_empty_ps_output_yields_no_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.sys, "platform", "darwin")
        monkeypatch.setattr(bmod.subprocess, "check_output", lambda *_a, **_k: b"\n")
        assert bmod._proc_start_time(4242) is None

    def test_a_failed_ps_probe_yields_no_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> bytes:
            raise OSError("no ps")

        monkeypatch.setattr(bmod.sys, "platform", "darwin")
        monkeypatch.setattr(bmod.subprocess, "check_output", _boom)
        assert bmod._proc_start_time(4242) is None

    def test_pid_alive_delegates_to_the_platform_shim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        assert bmod._pid_alive(4242) is True


# ---------------------------------------------------------------------------
# Health-gated MCP registration
# ---------------------------------------------------------------------------


class TestGateMcpRegistration:
    def test_a_healthy_backend_is_registered_with_its_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, int]] = []
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.reregister_app_mcp_servers",
            lambda name, live_port: seen.append((name, live_port)),
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

        monkeypatch.setattr(bmod.urllib.request, "urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["sick"] = AppProcess(app_name="sick", port=9134)
        bmod._health_check_loop("sick", 9134, "/health")
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

        monkeypatch.setattr(bmod.urllib.request, "urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["racy"] = AppProcess(app_name="racy", port=9135)
        bmod._health_check_loop("racy", 9135, "/health")
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
        monkeypatch.setattr(
            bmod, "list_apps", lambda: [_app("off", enabled=False, manifest={})]
        )
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
        monkeypatch.setattr(
            "kiro_crew.apps.manager._app_activation_denied", lambda _n: "banned"
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
    def test_an_unreadable_extensionless_entry_is_not_a_shell_launcher(
        self, tmp_path: Any
    ) -> None:
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
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(500))
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
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
            bmod._processes["audit"] = AppProcess(
                app_name="audit", port=9100, pid=600, proc=proc
            )
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

    def test_a_garbled_pid_probe_line_is_skipped_during_an_adopted_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)
        monkeypatch.setattr(
            bmod.platform_compat, "kill_pid", lambda pid, sig: killed.append((pid, sig))
        )
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=0, stdout="111\nnope\n"))
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext", port=9100, pid=0, proc=None, adopted_pids=[111], healthy=True
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
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda *_a: pytest.fail("signalled an unusable pidfile entry"),
        )
        assert bmod._reap_stale_app_backends() == 0
        assert bmod._read_pidfile() == {}

    def test_a_pid_that_exits_before_the_signal_is_dropped(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _gone(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _gone)
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
            bmod.platform_compat, "kill_process_tree", lambda _pid, sig: signals.append(sig)
        )
        assert bmod._reap_stale_app_backends() == 1
        assert signals == [
            bmod.platform_compat.SIGTERM,
            bmod.platform_compat.SIGKILL,
        ]

    def test_a_pid_that_exits_before_the_escalation_is_not_an_error(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _kill(_pid: int, sig: int) -> None:
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _kill)
        assert bmod._reap_stale_app_backends() == 1
        assert bmod._read_pidfile() == {}
