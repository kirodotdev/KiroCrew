"""The restore guard probes the port the client port chain resolves, at call time.

``kirocrew restore`` refuses to run while a gateway listens, unless ``--force``. The
guard that decides "a gateway listens" resolves its port through the chain every client
command uses (``KIROCREW_PORT``, ``KIROCREW_BOUND_PORT``, the port written in
``dashboard.url``, the run marker, then the default), so a gateway on a configured port
is seen without ``KIROCREW_PORT`` exported. These tests pin WHICH port the guard probes
(recording the connect at the socket seam), that the override still decides without a
probe, and what the guard does when the chain itself fails.

Every listener binds port 0 and reports the port it got; nothing here depends on a port
being free on the host.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import port_resolution
from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch data home with no port named anywhere, so each test adds one source."""
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT", "KIROCREW_ASSUME_GATEWAY_RUNNING"):
        monkeypatch.delenv(name, raising=False)
    return d


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """A plain loopback listener standing in for the gateway, on a kernel-chosen port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock
    finally:
        sock.close()


def _port(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])


def _name_port_in_config(home: Path, port: int) -> None:
    (home / "config.json").write_text(
        json.dumps({"dashboard": {"url": f"http://localhost:{port}"}}), encoding="utf-8"
    )


def _record_probes(monkeypatch: pytest.MonkeyPatch, *, connect: bool) -> list[tuple[str, int]]:
    """Record every address the guard connects to; refuse the connect unless *connect*."""
    probed: list[tuple[str, int]] = []
    real = socket.create_connection

    def _connect(address, timeout=None, *args, **kwargs):
        probed.append(address)
        if not connect:
            raise ConnectionRefusedError
        return real(address, timeout, *args, **kwargs)

    monkeypatch.setattr(snap.socket, "create_connection", _connect)
    return probed


class TestWhichPortTheGuardProbes:
    def test_a_gateway_on_the_configured_port_is_seen(
        self, home: Path, listener: socket.socket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``dashboard.url`` names the port; nothing else does."""
        port = _port(listener)
        _name_port_in_config(home, port)
        probed = _record_probes(monkeypatch, connect=True)

        assert snap._is_gateway_running() is True
        assert probed == [("127.0.0.1", port)]

    def test_a_gateway_on_the_bound_port_is_seen(
        self, home: Path, listener: socket.socket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_BOUND_PORT`` names the port and ``dashboard.url`` is empty."""
        port = _port(listener)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(port))
        (home / "config.json").write_text(json.dumps({"dashboard": {"url": ""}}), encoding="utf-8")
        probed = _record_probes(monkeypatch, connect=True)

        assert snap._is_gateway_running() is True
        assert probed == [("127.0.0.1", port)]

    def test_kirocrew_port_still_wins_over_the_configured_port(
        self, home: Path, listener: socket.socket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator override ranks above the config, as it does for every client."""
        env_port = _port(listener)
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            _name_port_in_config(home, _port(other))
        monkeypatch.setenv("KIROCREW_PORT", str(env_port))
        probed = _record_probes(monkeypatch, connect=True)

        assert snap._is_gateway_running() is True
        assert probed == [("127.0.0.1", env_port)]

    def test_nothing_on_the_resolved_port_lets_the_restore_proceed(
        self, home: Path, listener: socket.socket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused connect on the resolved port, and only that port, reads as not running."""
        port = _port(listener)
        _name_port_in_config(home, port)
        probed = _record_probes(monkeypatch, connect=False)

        assert snap._is_gateway_running() is False
        assert probed == [("127.0.0.1", port)]


class TestTheOverrideStillDecides:
    @pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False)])
    def test_kirocrew_assume_gateway_running_probes_nothing(
        self,
        home: Path,
        listener: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        expected: bool,
    ) -> None:
        _name_port_in_config(home, _port(listener))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", value)
        probed = _record_probes(monkeypatch, connect=True)

        assert snap._is_gateway_running() is expected
        assert probed == []


class TestWhenTheChainFails:
    @pytest.fixture
    def failing_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(cli_port):
            raise RuntimeError("marker directory unreadable")

        monkeypatch.setattr(port_resolution, "resolve_client_port_src", _raise)

    def test_the_guard_probes_the_import_time_port_and_says_so(
        self,
        home: Path,
        listener: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
        failing_chain: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The guard protects a restore: it does not crash it, and it does not answer
        "not running" without probing anything."""
        port = _port(listener)
        monkeypatch.setattr(snap, "_DASHBOARD_PORT", port)
        probed = _record_probes(monkeypatch, connect=True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.snapshot"):
            assert snap._is_gateway_running() is True
        assert probed == [("127.0.0.1", port)]
        assert snap._gateway_probe_port() == (port, snap._PROBE_PORT_FALLBACK_SOURCE)
        assert [r for r in caplog.records if str(port) in r.getMessage()], caplog.text

    def test_nothing_on_the_import_time_port_reads_as_not_running(
        self,
        home: Path,
        listener: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
        failing_chain: None,
    ) -> None:
        port = _port(listener)
        monkeypatch.setattr(snap, "_DASHBOARD_PORT", port)
        probed = _record_probes(monkeypatch, connect=False)

        assert snap._is_gateway_running() is False
        assert probed == [("127.0.0.1", port)]


class TestRestoreRefusesOnTheConfiguredPort:
    def test_restore_main_refuses_and_audits_without_kirocrew_port(
        self,
        home: Path,
        listener: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A gateway on the ``dashboard.url`` port refuses the restore before the bundle
        is even looked at, and the refusal is audited."""
        _name_port_in_config(home, _port(listener))
        audited: list[tuple[str, str]] = []
        monkeypatch.setattr(
            snap, "_audit", lambda event, resources: audited.append((event, resources))
        )

        ret = snap.restore_main([str(tmp_path / "missing.tar.gz"), "--mode", "merge"])

        assert ret == 1
        assert "Gateway is running" in capsys.readouterr().out
        assert audited == [("state_restore_rejected", "reason=gateway_running")]
