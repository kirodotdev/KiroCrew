"""The ``Connectivity`` gateway row of ``kirocrew doctor``.

``_doctor_gateway_reachability`` probes ``GET /api/status`` on loopback with a 2 s
deadline. A bare read ``TimeoutError`` means the connection was accepted and the
gateway then stalled, so the row reads busy; a connect-phase failure (a refusal, or
a ``URLError`` wrapping a timeout) reads not running. The 2 s deadline is short
enough that on native Windows a closed-port connect can time out inside the SYN-retry
window, so this row counts only a bare read timeout as busy rather than routing the
connect timeout through the shared ``loopback_http.deadline_expired``.
"""

from __future__ import annotations

import urllib.error

import pytest

from kiro_crew.doctor_checks import services
from kiro_crew.doctor_checks.services import DashboardBinding


def _binding(port: int = 5476) -> DashboardBinding:
    return DashboardBinding(host="127.0.0.1", port=port, creds={}, has_slack=False, local=True)


class TestGatewayRowBusyVsDown:
    def test_read_timeout_reads_busy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare read timeout (connection accepted, gateway stalled) reads busy."""

        def _times_out(*_a: object, **_k: object) -> None:
            raise TimeoutError("read timed out")

        monkeypatch.setattr(services.urllib.request, "urlopen", _times_out)
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "gateway:     ⏳ busy (up but did not answer)" in out
        assert "not running" not in out
        assert issues == []

    def test_connection_refused_reads_not_running(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refused connection reads not running, keeping the restart remedy."""

        def _refused(*_a: object, **_k: object) -> None:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        monkeypatch.setattr(services.urllib.request, "urlopen", _refused)
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "gateway:     ⏹  not running" in out
        assert "busy" not in out

    def test_connect_phase_timeout_reads_not_running(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A connect-phase timeout (URLError wrapping one) is the Windows closed-port
        shape at this 2 s deadline, so the row reads not running, not busy."""

        def _connect_timeout(*_a: object, **_k: object) -> None:
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        monkeypatch.setattr(services.urllib.request, "urlopen", _connect_timeout)
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "gateway:     ⏹  not running" in out
        assert "busy" not in out
