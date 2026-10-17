"""Doubles and fixtures shared by the ``test_apps_backend_coverage*.py`` modules.

``_isolated_module_state`` is autouse, so every module imports it by name. The fixtures
a test requests by parameter are declared ``@pytest.fixture(name=...)`` and imported
under their ``*_fixture`` function names, so the parameter does not shadow the import.
The doubles stand in at the seams the backend module reaches out through:
``popen_limited`` / ``run_limited`` (and the nvm probe's plain ``subprocess.run``), the
``socket`` module and the ``loopback_urlopen`` response.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.apps.backend as bmod

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the family's clean process table and pidfile, moved verbatim; tracked for a de-flake pass
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


@pytest.fixture(name="spawn_root")
def spawn_root_fixture(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
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
