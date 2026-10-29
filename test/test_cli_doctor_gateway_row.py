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


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _gateway_answers(
    monkeypatch: pytest.MonkeyPatch, health: object, probed: list[str] | None = None
) -> None:
    """``/api/status`` refuses the bare request (token auth), as on a real install;
    ``/api/health`` answers ``health`` (bytes, or an exception to raise)."""

    def _urlopen(req: object, timeout: object = None) -> _Response:
        url = getattr(req, "full_url", str(req))
        if probed is not None:
            probed.append(url)
        if url.endswith("/api/status"):
            raise urllib.error.HTTPError(url, 401, "unauthorized", None, None)
        if isinstance(health, BaseException):
            raise health
        assert isinstance(health, bytes)
        return _Response(health)

    monkeypatch.setattr(services.urllib.request, "urlopen", _urlopen)


class TestRunningVersionRow:
    """The ``running:`` row reads the gateway's own version from token-exempt
    ``/api/health`` and compares it with the on-disk ``cli_doctor._mc_version``."""

    def test_match_reads_same_as_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(services.cli_doctor, "_mc_version", "1.2.3")
        probed: list[str] = []
        _gateway_answers(monkeypatch, b'{"ok": true, "version": "1.2.3"}', probed)
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "gateway:     ✅ running (token auth enabled)" in out
        assert "running:     ✅ 1.2.3 (same as on disk)" in out
        assert "http://127.0.0.1:5476/api/health" in probed
        assert issues == []

    def test_mismatch_flags_restart_needed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(services.cli_doctor, "_mc_version", "1.2.4")
        _gateway_answers(monkeypatch, b'{"ok": true, "version": "1.2.3"}')
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "running:     ⚠️  1.2.3, on disk 1.2.4 (restart needed)" in out
        assert "Fix: `kirocrew restart`" in out
        assert issues == ["gateway restart needed (running version differs from on-disk)"]

    @pytest.mark.parametrize(
        "health",
        [
            b'{"ok": true}',
            b'{"ok": true, "version": ""}',
            b'{"ok": true, "version": 7}',
            b"not json",
            urllib.error.URLError("refused"),
        ],
        ids=["no-field", "empty", "not-a-string", "not-json", "unreachable"],
    )
    def test_missing_version_reads_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        health: object,
    ) -> None:
        monkeypatch.setattr(services.cli_doctor, "_mc_version", "1.2.3")
        _gateway_answers(monkeypatch, health)
        issues: list[str] = []
        services._doctor_gateway_reachability(_binding(), issues)
        out = capsys.readouterr().out
        assert "running:     ⚠️  gateway version unknown (on disk 1.2.3)" in out
        assert issues == []

    def test_not_running_prints_no_version_row(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        probed: list[str] = []

        def _refused(req: object, timeout: object = None) -> None:
            probed.append(getattr(req, "full_url", str(req)))
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        monkeypatch.setattr(services.urllib.request, "urlopen", _refused)
        services._doctor_gateway_reachability(_binding(), [])
        out = capsys.readouterr().out
        assert "running:" not in out
        assert not any(url.endswith("/api/health") for url in probed)
