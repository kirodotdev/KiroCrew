"""How CLI commands find, verify and call the running gateway.

The client port lookup (flag, ``KIROCREW_PORT``, ``dashboard.url``, the run marker), the identity
gate that guards the local secret, the loopback address secret-bearing requests use, and the
commands that call the gateway with it: ``logout``, ``status``, ``spawn`` and ``artifact``.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)


class TestLogout:
    """Tests for _logout CLI function."""

    @pytest.fixture(autouse=True)
    def _posix_ownership(self, _floor_monkeypatch):
        # Pin the POSIX branch so the connection-bound ownership proof takes its
        # uid path on every runner; on Windows it would otherwise take the SID
        # path and refuse first. Route the owner-verified secret transport
        # through the stubbed loopback_urlopen so the send works without a real
        # unix socket; the transport's own proof is covered in
        # test_cli_server_more_coverage_client.
        _floor_monkeypatch.setattr("kiro_crew.cli_server.platform_compat.IS_POSIX", True)
        import kiro_crew.cli_server as _cs

        _floor_monkeypatch.setattr(
            _cs,
            "_owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": _cs.loopback_urlopen(req, timeout=timeout),
        )

    def test_logout_success(self, tmp_path, monkeypatch, capsys):
        """Successful logout POSTs the secret to /api/logout and prints success."""
        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp) as opener:
            _logout(5476)
        req = opener.call_args.args[0]
        assert req.get_method() == "POST"
        assert req.full_url == "http://127.0.0.1:5476/api/logout"
        assert req.get_header("X-local-secret") == "test-secret"
        assert capsys.readouterr().out == "✅ All dashboard sessions revoked.\n"

    def test_logout_gateway_not_running(self, tmp_path, monkeypatch):
        """Missing secret file means gateway not running."""
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "")

        from kiro_crew.cli_server import _logout

        try:
            _logout(5476)
            assert False, "should have exited"
        except SystemExit as e:
            assert e.code == 1

    def test_logout_http_error(self, tmp_path, monkeypatch):
        """HTTP error from gateway is handled."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )

        from kiro_crew.cli_server import _logout

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(None, 403, "Forbidden", {}, None),
        ):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_connection_error(self, tmp_path, monkeypatch):
        """Connection error means gateway not running."""
        secret_file = tmp_path / ".kirocrew" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )

        from kiro_crew.cli_server import _logout

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_error_response(self, tmp_path, monkeypatch):
        """Error response from gateway is handled."""
        secret_file = tmp_path / ".kirocrew" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": false, "error": "test error"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1


class TestStatus:
    """Tests for _status() HTTP error handling."""

    def _make_args(self, port=5476):
        return argparse.Namespace(port=port)

    def test_status_auth_required(self, capsys):
        """401/403 should report gateway as running with token auth."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:5476/api/status", 403, "Forbidden", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "token auth" in out

    def test_status_other_http_error(self, capsys):
        """Non-auth HTTP errors should report gateway as running with code."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:5476/api/status", 500, "Internal Server Error", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "HTTP 500" in out

    def test_status_connection_refused(self, capsys):
        """Connection refused should report gateway as not running."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "not running" in out

    def test_status_timeout_reports_busy_not_down(self, capsys):
        """A URLError-wrapped timeout reads as busy through the shared classifier."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.URLError(TimeoutError("timed out")),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "busy" in out
        assert "not running" not in out

    def test_status_read_timeout_reports_busy_not_down(self, capsys):
        """A bare TimeoutError (socket read timeout) also reads as busy."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "busy" in out
        assert "not running" not in out

    def test_status_success(self, capsys):
        """200 OK should display stats."""
        from kiro_crew.cli_server import _status

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "uptime": "1h 0m",
                "sessions": 2,
                "messages": 10,
                "tool_calls": 5,
                "subagents": 0,
                "crons": 1,
                "lessons": 3,
            }
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "1h 0m" in out
        assert "Sessions" in out or "sessions" in out.lower()

    def test_status_unexpected_exception(self, capsys):
        """Non-network exceptions should report gateway as running with unexpected response."""
        from kiro_crew.cli_server import _status

        with patch("kiro_crew.cli_server.loopback_urlopen", side_effect=RuntimeError("unexpected")):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "unexpected response" in out


class TestStatusAgainstAClosedPort:
    """``kirocrew status`` against a port nothing listens on, with a REAL probe.

    ``_status`` dials with a 3 s deadline through ``loopback_urlopen`` and routes
    its failure through the shared ``loopback_http.deadline_expired`` classifier,
    which reads a ``URLError`` wrapping a ``TimeoutError`` as busy. A closed
    loopback port must refuse well inside that 3 s deadline so the command says
    not running, not busy. On native Windows a closed-port connect SYN-retries
    (~2 s) before ``WSAECONNREFUSED``; this test runs the real probe on every
    platform, so the Windows CI shards show whether that refusal lands before the
    3 s deadline or is misread as busy.
    """

    def _closed_port(self) -> int:
        """A loopback port bound then closed, so a connect to it is refused."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def test_stopped_gateway_reads_not_running(self, capsys):
        from kiro_crew.cli_server import _status

        port = self._closed_port()
        _status(argparse.Namespace(port=port))
        out = capsys.readouterr().out
        assert "Kiro Crew gateway is not running." in out
        assert "busy" not in out


class TestResolveClientPort:
    """Tests for `resolve_client_port` — the port-resolution order used by
    `kirocrew token` / `status` / `logout` / `stop` to find the gateway.

    Resolution order (see cli.resolve_client_port):
      1. explicit --port CLI arg (cli_port != None)
      2. KIROCREW_PORT env var
      3. port explicitly named in dashboard.url in config
      4. the sole live gateway run-marker (see TestResolveClientPortRunMarker)
      5. default 5476
    """

    def test_cli_flag_wins(self, monkeypatch, tmp_path):
        """An explicit --port flag must override env and config."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "9999")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(12345) == 12345

    def test_env_var_used_when_no_cli(self, monkeypatch):
        """KIROCREW_PORT env var wins over config when no --port passed."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 6777

    def test_invalid_env_var_falls_through_to_config(self, monkeypatch):
        """A garbage KIROCREW_PORT must not crash; the helper falls through."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "not-a-number")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_used_when_no_cli_no_env(self, monkeypatch):
        """The port in dashboard.url must be honoured when env is unset."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_hostname_only_falls_through_to_default(self, monkeypatch):
        """A dashboard.url without an explicit port must fall through to 5476."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://my.host.example"
        with (
            patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg),
            # No gateway advertising itself — isolate from the dev box's markers.
            patch("kiro_crew.cli_server.run_marker.marker_ports", return_value=[]),
        ):
            # A portless URL is not a port choice, so we continue past it; with no
            # live run-marker either, we land on the documented default.
            assert resolve_client_port(None) == 5476

    def test_empty_config_falls_through_to_default(self, monkeypatch):
        """No env, empty dashboard.url → 5476."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = ""
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 5476

    def test_config_load_failure_falls_through_to_default(self, monkeypatch):
        """If config loading raises, the helper must still return a usable port."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", side_effect=RuntimeError("boom")):
            assert resolve_client_port(None) == 5476

    def test_cli_flag_zero_is_respected(self, monkeypatch):
        """Port 0 is weird but valid; it must not be coerced to None/default."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "9999")
        # cli_port=0 is explicit; the helper uses 'is not None' not truthiness.
        assert resolve_client_port(0) == 0


class TestResolveClientPortRunMarker:
    """The run-marker fallback in `resolve_client_port` (step 4).

    A gateway on a non-default port advertises itself by writing
    `<data-home>/run/gateway-<port>.bin`. With nothing configured, a client must
    read that marker instead of assuming 5476 — but only when a verified
    Kiro Crew gateway process holds the port, and only when exactly one does.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of this class calls monkeypatch.undo()
    def _no_env(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

    def _cfg(self, url):
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = url
        return patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg)

    def _markers(self, ports):
        return patch("kiro_crew.cli_server.run_marker.marker_ports", return_value=ports)

    def _owned(self, ports):
        """Pretend a verified Kiro Crew gateway listens on each of *ports*."""
        return patch(
            "kiro_crew.cli_server._gateway_owns_port", side_effect=lambda p: p in set(ports)
        )

    def test_sole_owned_marker_used_when_nothing_configured(self):
        """The bug: one gateway on 6776, no config → must not return 5476."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776

    def test_portless_config_url_falls_through_to_marker(self):
        """`dashboard.url` without a port is not a port choice — the marker wins.

        parse_dashboard_url() substitutes 5476 for a portless URL, which would
        otherwise short-circuit discovery with a value the user never wrote down.
        """
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://my.host.example"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776

    def test_explicit_config_port_beats_marker(self):
        """An explicitly configured port is a user decision; discovery yields."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://localhost:7778"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 7778

    def test_env_var_beats_marker(self, monkeypatch):
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        with self._cfg(""), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6777

    def test_cli_flag_beats_marker(self):
        from kiro_crew.cli_server import resolve_client_port

        with self._markers([6776]) as markers:
            assert resolve_client_port(12345) == 12345
            markers.assert_not_called()  # no lookup cost when the port is explicit

    def test_non_string_config_url_does_not_crash(self):
        """`dashboard.url: 123` must degrade, not raise.

        Core installs may lack jsonschema, so the field can hold any JSON type.
        urlparse raises TypeError (NOT ValueError) on a non-str, which would
        otherwise escape and kill every client command.
        """
        from kiro_crew.cli_server import resolve_client_port

        for bad in (123, ["http://localhost:7778"], {"url": 1}, True):
            with self._cfg(bad), self._markers([6776]), self._owned([6776]):
                assert resolve_client_port(None) == 6776
        # ...and with no marker to fall back on, still the documented default.
        with self._cfg(123), self._markers([]):
            assert resolve_client_port(None) == 5476

    def test_no_marker_falls_through_to_default(self):
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([]):
            assert resolve_client_port(None) == 5476

    def test_stale_marker_not_owned_by_gateway_is_ignored(self):
        """A crashed gateway leaves its marker behind, and an unrelated process
        may since have bound that port. Trusting it would hand the local secret
        to that process, so the port must be discarded."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776]), self._owned([]):
            assert resolve_client_port(None) == 5476

    def test_multiple_owned_markers_refuses_to_guess(self, capsys):
        """Two gateways up → no basis to pick; fall back to the documented
        default and tell the user how to disambiguate."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776, 6777]), self._owned([6776, 6777]):
            assert resolve_client_port(None) == 5476
        err = capsys.readouterr().err
        assert "6776" in err and "6777" in err
        assert "--port" in err

    def test_discovery_failure_falls_through_to_default(self):
        """A broken data home must not break client commands."""
        from kiro_crew.cli_server import resolve_client_port

        with (
            self._cfg(""),
            patch(
                "kiro_crew.cli_server.run_marker.marker_ports",
                side_effect=OSError("boom"),
            ),
        ):
            assert resolve_client_port(None) == 5476

    def test_malformed_config_url_still_reaches_marker(self):
        """A typo'd `dashboard.url` must not swallow the discovery step."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://[::1"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776


class TestGatewayOwnsPort:
    """`_gateway_owns_port` — the identity gate protecting the local secret.

    Three parts, none sufficient alone: the pid recorded in the owner-only
    sidecar, that pid holding the port, and that pid being owned by the calling
    user. An argv-only check would be spoofable by launching a listener as
    `/tmp/kirocrew gateway`.
    """

    def _sidecar(self, pid):
        return patch("kiro_crew.cli_server.run_marker.read_pid", return_value=pid)

    def _listeners(self, pids):
        return patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=pids)

    def _owner(self, uid):
        return patch("kiro_crew.cli_server.platform_compat.process_owner_uid", return_value=uid)

    def _me(self):
        return os.getuid() if hasattr(os, "getuid") else 0

    def _posix(self, value=True):
        return patch("kiro_crew.cli_server.platform_compat.IS_POSIX", value)

    def test_true_when_recorded_pid_holds_the_port_and_is_ours(self):
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._posix(),
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server.os.getuid", return_value=self._me(), create=True),
        ):
            assert _gateway_owns_port(6776) is True

    def test_denies_on_non_posix(self):
        """Windows cannot report a process owner, and a home writable by another
        user would let them forge both the marker and the sidecar — so the step
        is skipped rather than trusted on partial evidence. Windows keeps --port
        / KIROCREW_PORT, which is where it was before this fallback existed.
        """
        from kiro_crew.cli_server import _gateway_owns_port

        # Every OTHER precondition is satisfied, so the non-POSIX guard is the
        # only thing that can deny — otherwise this test would pass for the
        # wrong reason (e.g. an unresolvable owner for a fabricated pid).
        with (
            self._posix(False),
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server.os.getuid", return_value=self._me(), create=True),
        ):
            assert _gateway_owns_port(6776) is False

    def test_false_when_listener_is_not_the_recorded_pid(self):
        """The spoofing case: an unrelated process holds the port and can name
        itself anything, but it is not the pid our own sidecar records."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([9999]),
            self._owner(self._me()),
            # Even if its argv is a perfect forgery of a gateway command line.
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uid gate only")
    def test_false_when_pid_is_owned_by_another_user(self):
        """Pid recycling into a *foreign* user's process must not be trusted,
        even though that pid does hold the port."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(os.getuid() + 1),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uid gate only")
    def test_false_when_owner_uid_is_unknown_on_posix(self):
        """Cannot determine ownership → deny (fail closed), do not assume ours."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(None),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    def test_false_when_no_pid_recorded(self):
        """No sidecar / unparseable pid → nothing to prove identity with."""
        from kiro_crew.cli_server import _gateway_owns_port

        with self._sidecar(None), self._listeners([4242]):
            assert _gateway_owns_port(6776) is False

    def test_false_when_pid_is_not_a_kirocrew_process(self):
        """Defense in depth kept as the last step, never as the only one."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=False),
        ):
            assert _gateway_owns_port(6776) is False

    def test_fails_closed_when_no_listener_or_lookup_tool(self):
        """find_listening_pids folds a missing lsof/netstat into an empty list,
        so both 'nothing there' and 'cannot tell' must deny."""
        from kiro_crew.cli_server import _gateway_owns_port

        with self._sidecar(4242), self._listeners([]):
            assert _gateway_owns_port(6776) is False

    def test_fails_closed_when_lookup_raises(self):
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                side_effect=OSError("lsof exploded"),
            ),
        ):
            assert _gateway_owns_port(6776) is False


class TestCliLoopbackAddress:
    """Secret-bearing CLI requests must be pinned to the verified endpoint.

    `localhost` can resolve to `::1` first on a dual-stack host, and the listener
    verification cannot distinguish an IPv6 squatter from the real IPv4 gateway,
    so a name-based URL could deliver `X-Local-Secret` to another local user's
    socket.
    """

    def test_cli_requests_use_the_ipv4_literal(self):
        import inspect

        from kiro_crew import cli_server

        assert cli_server._CLI_LOOPBACK == "127.0.0.1"
        src = "".join(
            inspect.getsource(sys.modules[m]) for m in ("kiro_crew.cli_server", *cli_server._OWNERS)
        )
        # No CLI->gateway request may be built from the hostname.
        assert "http://localhost:{port}" not in src
        for fn in (cli_server._token, cli_server._logout, cli_server._print_token_url):
            body = inspect.getsource(fn)
            if "http://" in body:
                assert "_CLI_LOOPBACK" in body, fn.__name__

    def test_printed_browser_url_still_uses_the_canonical_host(self):
        """The URL handed to the browser must NOT be switched to 127.0.0.1 —
        the SPA's per-origin localStorage is keyed on `localhost`."""
        import inspect

        from kiro_crew import cli_server

        # The URL printing lives in _emit_session_urls, which _token calls. Inspect the
        # function that actually owns the invariant, and assert the call still happens,
        # so this stays a real check rather than passing on an empty search.
        body = inspect.getsource(cli_server._emit_session_urls)
        assert "resolve_dashboard_host(local_only=True)" in body
        assert 'print(f"http://{host}:{port}?token={token}")' in body
        assert "_emit_session_urls(" in inspect.getsource(cli_server._token)


class TestSpawnCliAuth:
    """``kirocrew spawn`` attaches X-Internal-Secret on every gateway call.

    The CLI helpers in ``cli_commands_gateway_api.py`` attach the per-session IPC secret
    on every ``/api/spawn`` call. Without it the call gets a 403, which
    reads ``"gateway not running"`` when ``dashboard.url`` is
    a non-loopback host (token_auth_middleware then requires either
    a session cookie or the secret header on every request).
    """

    def test_internal_secret_reads_local_secret_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "abc123"
        )

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret(5476) == "abc123"

    def test_internal_secret_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "")

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret(5476) == ""

    def test_spawn_list_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "test-secret-xyz"
        )

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"agents": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, *_args: object) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("kiro_crew.cli_commands._gateway_send", fake_urlopen)

        from kiro_crew.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=5476)
        _spawn(args)

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://127.0.0.1:5476/api/spawn?queued=1"
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "test-secret-xyz"

    def test_spawn_run_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "run-secret-abc"
        )

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "agent-1", "task": "hi"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, *_args: object) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("kiro_crew.cli_commands._gateway_send", fake_urlopen)

        from kiro_crew.cli_commands import _spawn_run

        args = argparse.Namespace(task="do thing", fire_and_forget=True, port=5476)
        _spawn_run(args, "http://localhost:5476")

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://localhost:5476/api/spawn"
        assert req.data == b'{"task": "do thing"}'
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "run-secret-abc"
        assert headers_lower["content-type"] == "application/json"

    def test_spawn_list_403_prints_token_required(self, tmp_path, monkeypatch, capsys):
        """A bare 403 from the gateway is reported, not masked as 'not running'."""
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "")

        def fake_urlopen(*_args: object, **_kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "http://localhost:5476/api/spawn",
                403,
                "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr("kiro_crew.cli_commands._gateway_send", fake_urlopen)

        from kiro_crew.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=5476)
        with pytest.raises(SystemExit) as excinfo:
            _spawn(args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Error" in out
        assert "gateway not running" not in out

    def test_spawn_list_timeout_reports_busy(self, monkeypatch, capsys):
        """A read timeout on spawn list reads as busy, not 'not running'."""
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "s")
        monkeypatch.setattr(
            "kiro_crew.cli_commands._gateway_send",
            lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        from kiro_crew.cli_commands import _spawn

        with pytest.raises(SystemExit) as excinfo:
            _spawn(argparse.Namespace(spawn_action="list", port=5476))
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "busy" in out
        assert "gateway not running" not in out

    def test_spawn_run_timeout_reports_busy_and_points_at_list(self, monkeypatch, capsys):
        """A spawn-run timeout reports busy and points at `spawn list`, not a blind re-run."""
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "s")
        monkeypatch.setattr(
            "kiro_crew.cli_commands._gateway_send",
            lambda *_a, **_k: (_ for _ in ()).throw(
                urllib.error.URLError(TimeoutError("timed out"))
            ),
        )

        from kiro_crew.cli_commands import _spawn_run

        args = argparse.Namespace(task="do thing", fire_and_forget=True, port=5476)
        with pytest.raises(SystemExit) as excinfo:
            _spawn_run(args, "http://localhost:5476")
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "busy" in out
        assert "spawn list" in out
        assert "gateway not running" not in out

    def _list_rows(self, monkeypatch, capsys, payload: dict) -> str:
        """Run ``spawn list`` against a canned gateway answer and return stdout."""
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port, **_kw: "s")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr("kiro_crew.cli_commands._gateway_send", lambda _req, *_a: mock_resp)

        from kiro_crew.cli_commands import _spawn

        _spawn(argparse.Namespace(spawn_action="list", port=5476))
        return capsys.readouterr().out

    def test_spawn_list_queued_row_prints_no_terminal_controls(self, monkeypatch, capsys):
        """A queued spawn's id and task are agent-authored: no live escape, no forged row."""
        esc = "\x1b"
        out = self._list_rows(
            monkeypatch,
            capsys,
            {
                "agents": [],
                "queued": [
                    {
                        "id": f"q{esc}[2Jid",
                        "task": f"do it{esc}]0;pwned\x07\n  ✅ forged-row  done",
                    }
                ],
            },
        )
        assert esc not in out
        assert "\x07" not in out
        assert len(out.strip().splitlines()) == 1
        assert "forged-row" in out  # kept as visible text, on the SAME line

    def test_spawn_list_running_row_prints_no_terminal_controls(self, monkeypatch, capsys):
        """The running-agent rows share the queued rows' rule."""
        esc = "\x1b"
        out = self._list_rows(
            monkeypatch,
            capsys,
            {
                "agents": [
                    {"id": f"a{esc}[31m1", "task": f"t{esc}[2K\r\n  ✅ forged  done", "done": False}
                ],
                "queued": [],
            },
        )
        assert esc not in out
        assert "\r" not in out
        assert len(out.strip().splitlines()) == 1


class TestArtifactCli:
    """CLI-side coverage for security-critical paths in `_artifact`.

    The bulk of artifact behavior is exercised via the HTTP handler tests; this
    class focuses on the CLI's own gates (e.g. `is_sensitive_path()` refusal on
    `--content-file`).
    """

    def test_save_parses_an_explicit_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A subparser that never declared the flag exits at parse time, so this
        # pins the whole argv -> namespace -> command hop rather than the
        # forwarding alone.
        seen: dict[str, object] = {}
        from kiro_crew import cli

        monkeypatch.setattr(
            "kiro_crew.cli_commands._artifact",
            lambda args: seen.update(slug=args.slug, name=args.name),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "kirocrew",
                "artifact",
                "save",
                "--name",
                "Run Summary",
                "--slug",
                "chosen-by-hand",
                "--content",
                "body",
            ],
        )
        cli.main()
        assert seen == {"slug": "chosen-by-hand", "name": "Run Summary"}

    def test_save_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AUTOSDE security-controls: --content-file must be gated by
        # is_sensitive_path() before Path.read_text(encoding="utf-8") so a user (or script)
        # cannot exfiltrate ~/.aws/credentials by piping it into an artifact.
        from kiro_crew.cli_commands import _artifact

        monkeypatch.setattr("kiro_crew.cli_commands.is_sensitive_path", lambda _p: True)
        # Surface any HTTP call as a fatal so we can prove the function exited
        # at the security check, not at the network layer.
        monkeypatch.setattr(
            "kiro_crew.cli_commands._gateway_send",
            lambda *_a, **_kw: pytest.fail("_artifact must refuse before opening any HTTP request"),
        )

        args = argparse.Namespace(
            artifact_action="save",
            name="x",
            kind="widget",
            content=None,
            content_file="/tmp/should-be-refused",
            description="",
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err

    def test_update_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.cli_commands import _artifact

        monkeypatch.setattr("kiro_crew.cli_commands.is_sensitive_path", lambda _p: True)
        monkeypatch.setattr(
            "kiro_crew.cli_commands._gateway_send",
            lambda *_a, **_kw: pytest.fail("_artifact must refuse before opening any HTTP request"),
        )

        args = argparse.Namespace(
            artifact_action="update",
            slug="x",
            content=None,
            content_file="/tmp/should-be-refused",
            name=None,
            description=None,
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err
