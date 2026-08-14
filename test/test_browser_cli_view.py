"""Supervision of ``playwright-cli show``: loopback bind, health, lifecycle."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod


class FakeProc:
    """Stand-in for the supervised child; never touches a real process."""

    def __init__(self, alive: bool = True, pid: int = 424242) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 1
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._alive = False


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Clear the module singleton and neutralize real process signalling."""
    signalled: list[int] = []
    monkeypatch.setattr(mod, "_proc", None)
    monkeypatch.setattr(mod, "_info", None)
    monkeypatch.setattr(
        platform_compat,
        "kill_process_tree",
        lambda pid, sig=platform_compat.SIGTERM: signalled.append(pid) or True,
    )
    yield signalled
    mod._proc = None
    mod._info = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Answers ``/`` with 302, exactly as the real dashboard does."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self.send_response(302)
        self.send_header("Location", "/dashboard")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def redirecting_server() -> Iterator[int]:
    """A loopback HTTP server whose root answers 302."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_show_argv_binds_explicit_loopback_host() -> None:
    """The default listener is IPv6-only, so ``--host 127.0.0.1`` must be passed."""
    argv = mod._show_argv("/n/playwright-cli", 45613)

    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "45613"
    assert argv[:2] == ["/n/playwright-cli", "show"]


def test_show_argv_never_binds_a_routable_address() -> None:
    """A non-loopback bind would publish remote browser input to the network."""
    argv = mod._show_argv("/n/playwright-cli", 45613)

    assert "0.0.0.0" not in argv
    assert "::" not in argv
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


def test_health_accepts_a_302(redirecting_server: int) -> None:
    """``/`` answers 302; a health check that demanded 200 would report dead."""
    assert mod._healthy(redirecting_server) is True


def test_health_false_when_nothing_is_listening() -> None:
    assert mod._healthy(_free_port()) is False


def test_free_port_is_bindable_loopback() -> None:
    port = mod._free_port()

    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_free_port_is_not_hardcoded() -> None:
    """A fixed port would collide with whatever else the operator runs."""
    assert mod._free_port() != mod._free_port()


def test_ensure_running_returns_none_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawned.append([cli]) or FakeProc())

    assert mod.ensure_running() is None
    assert spawned == []


def test_ensure_running_spawns_with_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real argv reaching the child carries the explicit loopback bind."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)

    def fake_spawn(cli: str, port: int) -> FakeProc:
        recorded.append(mod._show_argv(cli, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running()

    assert info is not None
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert info.url == f"http://127.0.0.1:{info.port}"
    assert argv[argv.index("--port") + 1] == str(info.port)


def test_ensure_running_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy server is reused, not duplicated by a second panel mount."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc()
    )

    first = mod.ensure_running()
    second = mod.ensure_running()

    assert first == second
    assert len(spawns) == 1


def test_ensure_running_replaces_a_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing a corpse would leave the panel permanently blank."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc()
    )

    mod.ensure_running()
    mod._proc = FakeProc(alive=False)

    assert mod.ensure_running() is not None
    assert len(spawns) == 2


def test_ensure_running_respawns_when_process_stops_answering(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """Alive but unresponsive is still unusable, and the stale child is reaped."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    healthy = {"value": True}
    monkeypatch.setattr(mod, "_healthy", lambda port: healthy["value"])
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc(pid=len(spawns))
    )

    mod.ensure_running()
    healthy["value"] = False
    # Startup gate cannot pass while unhealthy, so this reports failure...
    assert mod.ensure_running() is None
    # ...and the stale child was signalled rather than left holding the port.
    assert 1 in reset_state


def test_ensure_running_gives_up_when_child_exits_during_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: False)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(alive=False))

    assert mod.ensure_running() is None
    assert mod.status()["status"] == "stopped"


def test_ensure_running_returns_none_when_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: None)

    assert mod.ensure_running() is None


def test_stop_reaps_child_without_a_global_kill(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """``stop()`` reaps only the child it spawned.

    A global ``show --kill`` would stop the daemon, but it stops EVERY session
    with it, including one the operator launched independently — and unsaved work
    goes with it. The child is spawned into its own session, so reaping its
    process group covers the server and the browser it started.
    """
    runs: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=777))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, **kw: runs.append(list(argv)) or None,
    )

    mod.ensure_running()
    mod.stop()

    assert runs == [], runs
    assert 777 in reset_state
    assert mod.status()["status"] == "stopped"


def test_stop_reaps_child_even_when_kill_command_fails(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """A failed daemon kill must not leave the child holding the port."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=888))

    def boom(argv: list[str], **kw: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(mod.subprocess, "run", boom)

    mod.ensure_running()
    mod.stop()

    assert 888 in reset_state
    assert mod._proc is None


def test_status_unavailable_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable is distinct from stopped: starting the server cannot fix it."""
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    st = mod.status()

    assert st["status"] == "unavailable"
    assert st["url"] is None
    assert st["port"] is None
    assert st["reason"]


def test_status_running_reports_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())

    info = mod.ensure_running()
    st = mod.status()

    assert info is not None
    assert st["status"] == "running"
    assert st["url"] == info.url
    assert st["port"] == info.port


def test_status_does_not_start_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc()
    )

    assert mod.status()["status"] == "stopped"
    assert spawns == []
