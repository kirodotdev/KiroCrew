"""``kirocrew token``, the post-restart token URL, and the transport secret-bearing sends travel on.

The token mint, the post-restart token URL and ``update``'s approve send prefer the gateway's unix
socket and otherwise go over ``tcp_verified_urlopen``, sending only when the connected peer is
proven to be this account's recorded gateway.
"""

import argparse
import contextlib
import http.server
import json
import socketserver
import threading
import types
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew import platform_compat
from kiro_crew.cli_server import (
    _owner_verified_secret_urlopen as _REAL_OWNER_VERIFIED_SECRET_URLOPEN,
)


def _route_tcp_send_through_verdict(monkeypatch, verdict):
    """Drive the REAL ``_owner_verified_secret_urlopen`` down its TCP branch.

    No unix socket resolves, so the send rides ``tcp_verified_urlopen``. The fake
    opener mirrors its contract -- run ``verify_peer`` on the connected socket
    BEFORE any byte -- and records a send only if the verifier did not raise.
    ``_connected_peer_verdict`` is pinned to *verdict*, and SEL is captured so
    no audit record escapes the test. Returns ``(sends, sel_calls)``.
    """
    from kiro_crew import cli_server

    # The token / restart-poll test classes route the helper to a plain stub via
    # an autouse fixture; this driver asserts on the REAL helper, so restore it.
    monkeypatch.setattr(
        cli_server, "_owner_verified_secret_urlopen", _REAL_OWNER_VERIFIED_SECRET_URLOPEN
    )
    monkeypatch.setattr(
        "kiro_crew.cli_server.dashboard_socket_path",
        lambda _port: (_ for _ in ()).throw(RuntimeError("no socket")),
    )
    monkeypatch.setattr(cli_server, "_connected_peer_verdict", lambda _sock: verdict)
    sel_calls: list[dict] = []
    monkeypatch.setattr(
        cli_server,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: sel_calls.append(kw)),
    )
    sends: list[object] = []

    def fake_tcp(req, timeout, *, verify_peer):
        verify_peer(object())
        sends.append((req, timeout))
        resp = MagicMock()
        resp.read.return_value = b'{"token": "tok"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
    monkeypatch.setattr(
        cli_server,
        "unix_socket_urlopen",
        lambda *a, **k: pytest.fail("no unix socket exists; must not be dialed"),
    )
    return sends, sel_calls


class TestPrintTokenUrl:
    """Tests for _print_token_url (auto-token after restart)."""

    @pytest.fixture(autouse=True)
    def _route_secret_transport(self, _floor_monkeypatch):
        # Route the owner-verified secret transport through the stubbed
        # loopback_urlopen so the post-restart token poll works without a real
        # unix socket; the transport's own proof is covered elsewhere.
        import kiro_crew.cli_server as _cs

        _floor_monkeypatch.setattr("kiro_crew.cli_server.platform_compat.IS_POSIX", True)
        _floor_monkeypatch.setattr(
            _cs,
            "_owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": _cs.loopback_urlopen(req, timeout=timeout),
        )

    def test_prints_token_on_success(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        # Mock resolve_dashboard_host to an obviously-fake sentinel host (RFC 2606
        # .invalid, never resolvable) so the assertion proves _print_token_url builds
        # the URL from resolve_dashboard_host's output rather than hardcoding a host.
        # A sentinel distinct from the real default ("localhost") is deliberate: it
        # would catch a regression that hardcodes "localhost" back into the URL.
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "abc123"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server._owner_verified_secret_urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://canonical-host.invalid:7777?token=abc123" in out

    def test_prints_custom_origin(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="http://kirocrew.dev:7777")),
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_origin", lambda u: "http://kirocrew.dev:7777"
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "xyz789"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server._owner_verified_secret_urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://kirocrew.dev:7777/?token=xyz789" in out

    def test_fallback_on_timeout(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_TOKEN_WAIT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kirocrew token" in out

    def test_token_wait_keeps_its_15s_budget_whatever_the_readiness_override(
        self, capsys, monkeypatch
    ):
        """The readiness override is fork-path only; both paths' token wait stays at 15s."""
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_RESTART_READY_TIMEOUT", "180")
        secret_reads: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret",
            lambda port, **_kw: secret_reads.append(port) or "",
        )
        remaining = [0.0, 14.0, 16.0, 200.0]

        def clock() -> float:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        fake_time = types.SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)
        with patch.object(cli_server, "time", fake_time):
            cli_server._print_token_url(7777)

        assert cli_server._RESTART_TOKEN_WAIT == 15
        # One read at 14s; the 16s check is past the 15s deadline. A 180s
        # budget would have read again at 16s.
        assert secret_reads == [7777]
        out = capsys.readouterr().out
        assert "kirocrew token" in out
        assert "Still starting" not in out

    def test_fallback_on_no_secret(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "")
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_TOKEN_WAIT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kirocrew token" in out

    def test_peer_refusal_falls_into_the_nonfatal_fallback(self, capsys, monkeypatch):
        # A TCP-only gateway whose run-marker write has not landed yet while the
        # port already serves yields a MISMATCH during the restart window ->
        # _GatewayPeerRefused. It is not an OSError, so it must be caught by the
        # retry loop and hit the non-fatal fallback instead of escaping as a
        # traceback. One loop pass runs the refusing send, then the deadline
        # passes so the loop prints the fallback.
        import kiro_crew.cli_server as _cs
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        # Positive wait so the while-guard admits one pass; monotonic steps past
        # the deadline right after that pass, and sleep is a no-op so the test
        # does not block on the loop's trailing sleep(1).
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_TOKEN_WAIT", 10)
        # First two monotonic() reads (deadline anchor + first while-guard) sit
        # before the deadline so one pass runs; every read after jumps past it,
        # so the loop exits after that single swallowed refusal. A plain counter
        # (not an exhaustible iterator) tolerates extra reads without raising.
        ticks = {"n": 0}

        def _clock():
            ticks["n"] += 1
            return 0.0 if ticks["n"] <= 2 else 100.0

        # Rebind cli_server's own ``time`` reference to a shim that fakes only
        # monotonic/sleep and delegates every other name to the real module.
        # Patching ``cli_server.time.monotonic`` directly would mutate the
        # shared stdlib ``time`` object and freeze the clock for the whole test
        # worker; this confines the fakes to cli_server's view and undoes
        # cleanly at teardown.
        import time as _real_time

        class _FakeTime:
            monotonic = staticmethod(_clock)

            @staticmethod
            def sleep(_s):
                return None

            def __getattr__(self, name):
                return getattr(_real_time, name)

        monkeypatch.setattr(_cs, "time", _FakeTime())

        calls = {"n": 0}

        def _refuse(*_a, **_k):
            calls["n"] += 1
            raise _cs._GatewayPeerRefused("peer not proven")

        # Overrides the class autouse routing fixture for this test.
        monkeypatch.setattr(_cs, "_owner_verified_secret_urlopen", _refuse)

        _print_token_url(7777)  # must NOT raise -- the refusal is swallowed

        # The refusing send ran (branch actually exercised: if the retry handler
        # did not name _GatewayPeerRefused this call would have propagated).
        assert calls["n"] == 1
        out = capsys.readouterr().out
        assert "kirocrew token" in out

    @pytest.mark.parametrize("verdict_name", ["MISMATCH"])
    def test_minting_secret_is_withheld_from_an_unproven_listener(
        self, capsys, monkeypatch, verdict_name
    ):
        """A secret is available but the connected peer is NOT proven ours.

        /api/token/local mints owner tokens, so handing the X-Local-Secret to
        whatever answers 127.0.0.1 would let a foreign local process act as the
        operator. With no unix socket to prefer, the send rides
        ``tcp_verified_urlopen`` whose connect-time verifier refuses on anything
        that is not a positively proven MATCH -- a resolved foreign peer and an
        unresolvable one are one MISMATCH. The refusal is a ``_GatewayPeerRefused``
        the restart loop absorbs, so the arm ends with the non-fatal
        ``kirocrew token`` remedy and nothing is sent.

        A fake clock gives the loop exactly one real iteration (a positive wait,
        not ``_RESTART_TOKEN_WAIT=0`` which would skip the body entirely and make
        the no-send assertion vacuous), so the gate is genuinely exercised: the
        secret IS read, and the connection-bound verifier alone stops the send.
        """
        from kiro_crew import cli_server

        secret_reads: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret",
            lambda port, **_kw: secret_reads.append(port) or "test-secret",
        )
        sends, _sel_calls = _route_tcp_send_through_verdict(
            monkeypatch, getattr(cli_server._ConnectedPeerVerdict, verdict_name)
        )

        # Clock: start=0, one in-loop check at 1s (< 15s deadline), then 16s to
        # cross the deadline after the swallowed refusal. The body runs once.
        remaining = [0.0, 1.0, 16.0]
        clock = types.SimpleNamespace(
            monotonic=lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0],
            sleep=lambda _s: None,
        )
        with patch.object(cli_server, "time", clock):
            cli_server._print_token_url(7777)  # must NOT raise

        assert secret_reads == [7777], "the loop body must run once and read the secret"
        assert sends == [], "the minting secret must not be sent to an unproven listener"
        out = capsys.readouterr().out
        assert "kirocrew token" in out, "the refusal falls into the non-fatal remedy"


class TestTokenCommand:
    """Tests for the ``kirocrew token`` command handler (``_token``)."""

    @pytest.fixture(autouse=True)
    def _posix_ownership(self, _floor_monkeypatch):
        # Pin the POSIX branch so the connection-bound ownership proof takes its
        # uid path; the non-POSIX branch would take the SID path and refuse
        # first. Route the owner-verified secret transport through the stubbed
        # loopback_urlopen so the send works without a real unix socket.
        _floor_monkeypatch.setattr("kiro_crew.cli_server.platform_compat.IS_POSIX", True)
        import kiro_crew.cli_server as _cs

        _floor_monkeypatch.setattr(
            _cs,
            "_owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": _cs.loopback_urlopen(req, timeout=timeout),
        )

    def _mock_token_response(self, token: str) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.read.return_value = f'{{"token": "{token}"}}'.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_prints_loopback_only(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        monkeypatch.setattr("kiro_crew.cli_server.dashboard_origin", lambda u: "")
        # Sentinel canonical host (RFC 2606 .invalid) — proves the URL is built
        # from resolve_dashboard_host's output, not a hardcoded host.
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        args = argparse.Namespace(ttl="1h", port=7777)
        with patch(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            return_value=self._mock_token_response("abc123"),
        ):
            _token(args)

        out = capsys.readouterr().out
        assert "http://canonical-host.invalid:7777?token=abc123" in out
        # No custom-domain URL, so no separating blank line is emitted. The
        # loopback URL has no '/' before '?token=...', so '/?token=...' would
        # only appear if the custom-origin URL had been printed.
        assert "/?token=abc123" not in out

    def test_separates_custom_origin_with_blank_line(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        monkeypatch.setattr(
            "kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: "test-secret"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="https://kirocrew.dev:7777")),
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_origin", lambda u: "https://kirocrew.dev:7777"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        args = argparse.Namespace(ttl="1h", port=7777)
        with patch(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            return_value=self._mock_token_response("xyz789"),
        ):
            _token(args)

        out = capsys.readouterr().out
        loopback_line = "http://canonical-host.invalid:7777?token=xyz789"
        custom_line = "https://kirocrew.dev:7777/?token=xyz789"
        assert loopback_line in out
        assert custom_line in out
        # The two URLs must be separated by a blank line.
        assert f"{loopback_line}\n\n{custom_line}" in out

    # ── stdout is a machine interface: failures go to stderr ─────────────────
    #
    # `_token`'s stdout is regex-parsed by the remote-mint path
    # (kiro_crew.instances.token_mint.mint_remote_token) over SSH. Error prose on
    # stdout both breaks the Unix convention and hides the reason from a caller
    # that captures stderr — which is how a failed remote mint surfaces as
    # a bare "<no stderr>".

    def _stub_token_env(self, tmp_path, monkeypatch, *, secret: bool = True) -> None:
        value = "test-secret" if secret else ""
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port, **_kw: value)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        monkeypatch.setattr("kiro_crew.cli_server.dashboard_origin", lambda u: "")
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

    def test_invalid_ttl_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as excinfo:
            _token(argparse.Namespace(ttl="banana", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid TTL" in captured.err
        assert captured.out == ""

    def test_missing_secret_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch, secret=False)
        with pytest.raises(SystemExit) as excinfo:
            _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Gateway not running" in captured.err
        assert captured.out == ""

    def test_unreachable_gateway_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            with pytest.raises(SystemExit) as excinfo:
                _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Could not reach gateway on port 7777" in captured.err
        assert captured.out == ""

    def test_empty_token_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            return_value=self._mock_token_response(""),
        ):
            with pytest.raises(SystemExit) as excinfo:
                _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "empty token" in captured.err
        assert captured.out == ""

    def test_success_stdout_carries_only_urls(self, tmp_path, capsys, monkeypatch):
        """Every stdout line on the success path must be a parseable URL.

        The docstring's "stdout carries only the URL(s)" is a contract the remote
        mint depends on, and prose is not enforcement: any future preflight that
        writes to stdout — a warning, or an `input()` prompt, whose prompt goes to
        stdout — would silently corrupt the stream that mint_remote_token regexes
        over SSH. This pins the contract to an assertion instead.
        """
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            return_value=self._mock_token_response("eyJa.b"),
        ):
            _token(argparse.Namespace(ttl="1h", port=7777))
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines, "success path printed nothing to stdout"
        for line in lines:
            assert line.lstrip().startswith("http"), f"non-URL text on stdout: {line!r}"
        assert "token=eyJa.b" in captured.out

    @pytest.mark.parametrize(
        ("verdict_name", "reason"),
        [
            ("MISMATCH", "could not be proven to be this install's gateway"),
            ("MISMATCH", "install lsof"),
        ],
    )
    def test_minting_secret_is_withheld_from_an_unproven_listener(
        self, tmp_path, capsys, monkeypatch, verdict_name, reason
    ):
        """A secret is available but the connected peer is not proven ours.

        ``/api/token/local`` mints owner tokens, so the request must not be sent
        to a peer whose gateway-ownership is unproven. With no unix socket to
        prefer, the send rides ``tcp_verified_urlopen``; its connect-time verifier
        raises ``_GatewayPeerRefused`` on anything that is not a positively proven
        MATCH -- a resolved foreign peer (the TOCTOU/squatter case) and an
        unresolvable one are one MISMATCH. ``_token`` reports the refusal -- not
        "could not reach gateway" -- exits 1 with the reason on stderr (the single
        message names both the ownership proof and the install-lsof remedy), and
        never sends.
        """
        from kiro_crew import cli_server
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        sends, sel_calls = _route_tcp_send_through_verdict(
            monkeypatch, getattr(cli_server._ConnectedPeerVerdict, verdict_name)
        )

        with pytest.raises(SystemExit) as excinfo:
            _token(argparse.Namespace(ttl="1h", port=7777))

        assert excinfo.value.code == 1
        assert sends == [], "the minting secret was sent to an unproven listener"
        captured = capsys.readouterr()
        assert "ownership could not be verified" in captured.err
        assert reason.lower() in captured.err.lower()
        assert "Could not reach gateway" not in captured.err
        assert captured.out == ""
        denied = [c for c in sel_calls if c.get("outcome") == "denied"]
        assert denied and denied[-1]["operation"] == "gateway_token"


class TestOwnerVerifiedSecretTransport:
    """The owner-minting / approve sends bind their ownership proof to the
    connected socket on every path (``_token``, ``_print_token_url`` and
    ``_update_approve`` all route through ``_owner_verified_secret_urlopen``).

    With the gateway's own unix socket present the send goes over that socket
    with a ``verify_peer`` hook that refuses unless the kernel confirms the peer
    is this account (``check_peer_is_self``) AND the gateway recorded for the
    port (``_connected_unix_peer_is_recorded_gateway``). With no AF_UNIX socket
    -- a TCP-only gateway or native Windows -- the send rides
    ``tcp_verified_urlopen`` whose ``verify_peer`` resolves the far end of THIS
    4-tuple (``_connected_peer_verdict``): only MATCH sends; anything else is a
    MISMATCH -- a squatter that won a check-to-connect gap and an unresolvable
    peer are one fail-closed decision -- raising ``_GatewayPeerRefused``, which
    is NOT an OSError so it survives urllib's URLError wrap. Covers the three
    secret-bearing sends (``_token``, ``_print_token_url``, ``_update_approve``).
    """

    @pytest.fixture(autouse=True)
    def _sel_calls(self, _floor_monkeypatch):
        from kiro_crew import cli_server

        calls: list[dict] = []
        _floor_monkeypatch.setattr(
            cli_server,
            "sel",
            lambda: types.SimpleNamespace(log_api_access=lambda **kw: calls.append(kw)),
        )
        return calls

    @staticmethod
    def _resp(payload: bytes = b'{"ok": true}') -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _capture_unix_verifier(self, monkeypatch, tmp_path, operation="gateway_token"):
        """Run the real helper down its unix branch; return the verify_peer it built."""
        from kiro_crew import cli_server

        sock_file = tmp_path / "gw.sock"
        sock_file.write_bytes(b"")  # the file must EXIST to be preferred
        monkeypatch.setattr("kiro_crew.cli_server.dashboard_socket_path", lambda _p: sock_file)
        holder: dict = {}

        def fake_unix(req, timeout, *, socket_path, verify_peer):
            holder["socket_path"] = socket_path
            holder["verify_peer"] = verify_peer
            return self._resp()

        monkeypatch.setattr(cli_server, "unix_socket_urlopen", fake_unix)
        cli_server._owner_verified_secret_urlopen(MagicMock(), 7777, 5, operation=operation)
        return holder

    def _capture_tcp_verifier(self, monkeypatch, operation="gateway_token"):
        """Run the real helper down its TCP branch; return the verify_peer it built."""
        from kiro_crew import cli_server

        holder: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            holder["verify_peer"] = verify_peer
            return self._resp()

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
        monkeypatch.setattr(
            cli_server,
            "unix_socket_urlopen",
            lambda *a, **k: pytest.fail("no unix socket to prefer; must not be dialed"),
        )
        cli_server._owner_verified_secret_urlopen(MagicMock(), 7777, 5, operation=operation)
        return holder["verify_peer"]

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="asserts the AF_UNIX socket is preferred over TCP; AF_UNIX-only",
    )
    def test_prefers_the_unix_socket_with_a_peer_verifier_when_it_exists(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import cli_server

        tcp_calls: list[object] = []
        monkeypatch.setattr(
            cli_server, "tcp_verified_urlopen", lambda *a, **k: tcp_calls.append((a, k))
        )
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: tcp_calls.append(a))
        holder = self._capture_unix_verifier(monkeypatch, tmp_path)

        assert holder["socket_path"] == str(tmp_path / "gw.sock")
        assert callable(holder["verify_peer"]), "the unix send must carry a peer verifier"
        assert tcp_calls == [], "TCP must stay untouched when the owner-only socket exists"

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="drives the AF_UNIX verify_peer refusal; AF_UNIX-only",
    )
    def test_unix_verify_peer_requires_this_account_and_the_recorded_gateway(
        self, monkeypatch, tmp_path, _sel_calls
    ):
        from kiro_crew import cli_server
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        # cli_server binds check_peer_is_self at module import, so patch the name
        # there; the verifier closure reads it at call time.
        monkeypatch.setattr(cli_server, "check_peer_is_self", lambda _s: PeerCredResult.MISMATCH)
        verify = self._capture_unix_verifier(monkeypatch, tmp_path)["verify_peer"]
        with pytest.raises(cli_server._GatewayPeerRefused):
            verify(object())

        # Same-uid peer that is NOT this install's recorded gateway (an impostor
        # that rebound the socket path) still refuses -- uid alone is not enough.
        monkeypatch.setattr(cli_server, "check_peer_is_self", lambda _s: PeerCredResult.MATCH)
        monkeypatch.setattr(
            cli_server, "_connected_unix_peer_is_recorded_gateway", lambda _s, _p: False
        )
        verify = self._capture_unix_verifier(monkeypatch, tmp_path)["verify_peer"]
        with pytest.raises(cli_server._GatewayPeerRefused):
            verify(object())

        # Same-uid peer that IS the recorded gateway proceeds.
        monkeypatch.setattr(
            cli_server, "_connected_unix_peer_is_recorded_gateway", lambda _s, _p: True
        )
        verify = self._capture_unix_verifier(monkeypatch, tmp_path)["verify_peer"]
        verify(object())  # must not raise

        outcomes = [c["outcome"] for c in _sel_calls]
        assert outcomes == ["denied", "denied", "allowed"]
        assert all(c["operation"] == "gateway_token" for c in _sel_calls)

    def test_refusal_is_not_an_oserror(self):
        # urllib's do_open wraps an OSError into URLError, which callers report
        # as "gateway not running"; the refusal must escape that wrap.
        from kiro_crew import cli_server

        assert not issubclass(cli_server._GatewayPeerRefused, OSError)

    def test_without_a_unix_socket_sends_over_tcp_verified_only_on_match(
        self, monkeypatch, tmp_path, _sel_calls
    ):
        from kiro_crew import cli_server

        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        verify = self._capture_tcp_verifier(monkeypatch, operation="gateway_update_approve")

        monkeypatch.setattr(
            cli_server, "_connected_peer_verdict", lambda _s: cli_server._ConnectedPeerVerdict.MATCH
        )
        verify(object())  # MATCH: the credential header is written

        # MISMATCH is the TOCTOU/squatter case (something else accepted THIS
        # connection after the gateway's port was freed) AND the unresolvable-
        # peer case (lsof-less macOS) folded into one fail-closed decision; the
        # single refusal message names the install-lsof / owner-only-socket
        # remedy.
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        with pytest.raises(cli_server._GatewayPeerRefused, match="install lsof"):
            verify(object())

        assert [c["outcome"] for c in _sel_calls] == ["allowed", "denied"]
        assert all(c["operation"] == "gateway_update_approve" for c in _sel_calls)
        assert "connected_peer_verified" in _sel_calls[0]["resources"]
        assert "connected_peer_unverified" in _sel_calls[1]["resources"]

    def test_windows_binds_the_proof_to_the_connected_tcp_socket(self, monkeypatch):
        # Native Windows has no AF_UNIX, so the send rides TCP -- but the proof
        # is bound to the connected socket via tcp_verified_urlopen's verifier.
        from kiro_crew import cli_server

        monkeypatch.delattr(cli_server.socket, "AF_UNIX", raising=False)
        verify = self._capture_tcp_verifier(monkeypatch)

        monkeypatch.setattr(
            cli_server, "_connected_peer_verdict", lambda _s: cli_server._ConnectedPeerVerdict.MATCH
        )
        verify(object())  # must not raise
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        with pytest.raises(cli_server._GatewayPeerRefused):
            verify(object())

    def test_connected_peer_identity_proof(self, monkeypatch):
        # The PEER PID of THIS established 4-tuple (get_tcp_peer_pid, not a port
        # snapshot) must equal the recorded gateway pid, with a matching start
        # token, owned by this account -- uid on POSIX, owner SID on Windows.
        from kiro_crew import cli_server
        from kiro_crew.instances import run_marker

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 7777)

        verdict = cli_server._ConnectedPeerVerdict
        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: (4242, "tok"))
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok")

        # --- POSIX: ownership by uid.
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 4242)
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MATCH

        # A different pid accepted this connection -- MISMATCH.
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 9999)
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH

        # Recorded pid but a stale/forged start token -- MISMATCH.
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 4242)
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok-other")
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok")

        # Owned by a different uid, or owner unresolvable -- MISMATCH.
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 65534)
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: None)
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH

        # No recorded gateway for the port -- MISMATCH.
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)
        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: None)
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH
        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: (4242, "tok"))

        # --- Windows: ownership by access-token owner SID (no uid there).
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-me")
        monkeypatch.setattr(platform_compat, "process_owner_sid", lambda _pid: "S-1-5-21-me")
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MATCH
        monkeypatch.setattr(platform_compat, "process_owner_sid", lambda _pid: "S-1-5-21-attacker")
        assert cli_server._connected_peer_verdict(_Sock()) is verdict.MISMATCH

    def test_connected_peer_proof_passes_the_gateway_endpoint_as_client(self, monkeypatch):
        # The gateway is the far end: its endpoint (our getpeername) is the
        # client_endpoint and ours (getsockname) the server_endpoint, so
        # get_tcp_peer_pid resolves the GATEWAY's pid, not our own.
        from kiro_crew import cli_server
        from kiro_crew.instances import run_marker

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 7777)

        seen: dict = {}

        def fake_peer_pid(*, server_endpoint, client_endpoint):
            seen["server"] = server_endpoint
            seen["client"] = client_endpoint
            return 4242

        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", fake_peer_pid)
        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: (4242, "tok"))
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok")
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)
        assert cli_server._connected_peer_verdict(_Sock()) is cli_server._ConnectedPeerVerdict.MATCH
        assert seen["client"] == ("127.0.0.1", 7777)  # gateway endpoint
        assert seen["server"] == ("127.0.0.1", 54321)  # our endpoint

    def test_unresolvable_peer_is_a_fail_closed_mismatch(self, monkeypatch):
        from kiro_crew import cli_server

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 7777)

        # A peer that cannot be resolved -- a resolver that returns nothing, or a
        # host with no trusted lsof -- is one fail-closed MISMATCH: an owner-
        # minting secret is never written to a peer that cannot be proven. The
        # bounded retry exhausts (sleep stubbed) and still fails closed. Patch
        # cli_server's own ``time`` binding with a local namespace rather than
        # mutating the shared stdlib module, so no other worker-wide caller sees
        # the fakes.
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: None)
        clock = iter([0.0, 0.1, 2.0])
        fake_time = types.SimpleNamespace(
            sleep=lambda _s: None,
            monotonic=lambda: next(clock, 2.0),
        )
        monkeypatch.setattr(cli_server, "time", fake_time)
        assert (
            cli_server._connected_peer_verdict(_Sock()) is cli_server._ConnectedPeerVerdict.MISMATCH
        )

    def test_transient_none_peer_pid_is_retried_before_failing_closed(self, monkeypatch):
        """A connection still in the gateway's accept queue shows inode 0, so the

        peer resolver is momentarily None against a healthy TCP-only gateway slow
        to ``accept()``. The verdict retries the resolve for a bounded window and
        resolves MATCH once the pid appears, instead of refusing on the first miss.
        """
        from kiro_crew import cli_server

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 7777)

        # None on the first poll (connection not yet accept()ed), then the real
        # recorded pid on the next -- the accept-queue race the retry closes.
        polls = [None, 4242]
        monkeypatch.setattr(
            platform_compat, "get_tcp_peer_pid", lambda **_kw: polls.pop(0) if polls else 4242
        )
        # Patch cli_server's own ``time`` binding (not the shared stdlib module):
        # monotonic stays under the retry deadline so the one extra poll happens.
        fake_time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0)
        monkeypatch.setattr(cli_server, "time", fake_time)
        monkeypatch.setattr(cli_server, "_recorded_gateway_pid", lambda _port: 4242)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)
        assert cli_server._connected_peer_verdict(_Sock()) is cli_server._ConnectedPeerVerdict.MATCH
        assert polls == [], "the retry must poll again after the first None"

    def test_connected_unix_peer_is_recorded_gateway_requires_pid_and_token(self, monkeypatch):
        # The uid check proves only same-account; a same-uid impostor that
        # rebound the socket path passes it. The pid + start-token bind defeats it.
        from kiro_crew import cli_server

        monkeypatch.setattr(cli_server.run_marker, "pid_start_token", lambda _pid: "tok-real")

        def record(pid, token):
            monkeypatch.setattr(
                cli_server.run_marker, "read_pid_record_path", lambda _p: (pid, token)
            )

        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        record(4242, "tok-real")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 7777) is True

        # Same-uid impostor on a DIFFERENT pid.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 9999)
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 7777) is False

        # Recorded pid but a stale start token (recycled pid / forged record).
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        record(4242, "tok-stale")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 7777) is False

        # Peer pid unreadable -- fail closed.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: None)
        record(4242, "tok-real")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 7777) is False

        # No recorded gateway for the port -- fail closed.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        monkeypatch.setattr(cli_server.run_marker, "read_pid_record_path", lambda _p: None)
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 7777) is False

    # ── _update_approve shares the same send helper ─────────────────────────

    def test_the_duplicate_helper_is_gone(self):
        from kiro_crew import cli_server

        assert not hasattr(cli_server, "_authenticated_internal_urlopen"), (
            "the duplicate approve-path helper must be deleted; _update_approve "
            "shares _owner_verified_secret_urlopen"
        )

    @staticmethod
    def _arm_pending_update(monkeypatch):
        from kiro_crew import cli_server
        from kiro_crew.platform import update_capability, update_stepup

        pending = types.SimpleNamespace(
            version="9.9.9", channel="stable", nonce="nonce-xyz", expires_in=120
        )
        monkeypatch.setattr(
            update_capability,
            "derive_capability",
            lambda: types.SimpleNamespace(managed_by="pip"),
        )
        monkeypatch.setattr(update_stepup, "read_pending", lambda: pending)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 7777)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda *a, **k: "sek", raising=False)

    def test_update_approve_sends_through_the_single_helper(self, monkeypatch):
        from kiro_crew import cli_server

        self._arm_pending_update(monkeypatch)
        sent: list[object] = []

        def _fake_send(req, port, timeout, *, operation=""):
            sent.append((port, operation, req.get_header("X-internal-secret")))
            return self._resp(json.dumps({"version": "9.9.9"}).encode("utf-8"))

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _fake_send)

        cli_server._update_approve()

        assert sent == [(7777, "gateway_update_approve", "sek")]

    def test_update_approve_surfaces_the_peer_refusal(self, monkeypatch, capsys):
        """A live gateway whose connected peer is refused is NOT "not running"."""
        from kiro_crew import cli_server

        self._arm_pending_update(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_socket_path",
            lambda _port: (_ for _ in ()).throw(RuntimeError("no socket")),
        )
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        sends: list[object] = []

        def fake_tcp(req, timeout, *, verify_peer):
            verify_peer(object())
            sends.append(req)
            return self._resp()

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)

        with pytest.raises(SystemExit) as exc:
            cli_server._update_approve()

        assert exc.value.code == 1
        assert sends == [], "the nonce + secret must not reach an unproven peer"
        out = capsys.readouterr().out
        assert "ownership could not be verified" in out
        assert "not running" not in out


class _NoFqdnHTTPServer(http.server.HTTPServer):
    # http.server resolves socket.getfqdn(host) on bind, which can stall on some
    # hosts while the socket is bound but not listening; this loopback test
    # server is reached by port, never by name, so skip the lookup.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class TestTcpVerifiedUrlopen:
    """``tcp_verified_urlopen`` drives a real loopback TCP connection and runs
    its ``verify_peer`` hook on the connected socket BEFORE the first byte.

    Exercises the transport end to end against a tiny loopback HTTP server: a
    hook that passes lets the request reach the server; a hook that raises closes
    the connection so the server never receives the request line, and the raised
    (non-OSError) exception propagates unwrapped.
    """

    @staticmethod
    @contextlib.contextmanager
    def _server():

        received: list[str] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- stdlib contract
                received.append(self.path)
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):  # silence the test log
                return

        srv = _NoFqdnHTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield srv.server_address[1], received
        finally:
            # flake-ok: pre-existing shutdown of the in-test loopback server, moved verbatim; it returns once serve_forever's poll loop sees the shutdown request
            srv.shutdown()
            srv.server_close()
            # flake-ok: pre-existing 5 s join ceiling on the loopback server thread, moved verbatim; tracked for a de-flake pass
            t.join(timeout=5)

    def test_verify_peer_passes_sends_request(self):
        from kiro_crew.loopback_http import tcp_verified_urlopen

        with self._server() as (port, received):
            seen_sock: list[object] = []

            def verify(sock):
                seen_sock.append(sock)  # ran on the connected socket, no raise

            # flake-ok: pre-existing 5 s socket ceiling on a loopback request to the in-test server, moved verbatim; tracked for a de-flake pass
            with tcp_verified_urlopen(
                f"http://127.0.0.1:{port}/api/token/local", timeout=5, verify_peer=verify
            ) as resp:
                assert json.loads(resp.read()) == {"ok": True}
            assert received == ["/api/token/local"], "the request must reach the server"
            assert seen_sock, "verify_peer must run on the connected socket"

    def test_verify_peer_raises_sends_nothing(self):
        from kiro_crew.loopback_http import tcp_verified_urlopen

        class _Refused(Exception):
            """A NON-OSError refusal, so it propagates unwrapped (not URLError)."""

        with self._server() as (port, received):

            def verify(_sock):
                raise _Refused("peer not proven")

            with pytest.raises(_Refused):
                # flake-ok: pre-existing 5 s socket ceiling on a loopback request to the in-test server, moved verbatim; tracked for a de-flake pass
                tcp_verified_urlopen(
                    f"http://127.0.0.1:{port}/api/token/local", timeout=5, verify_peer=verify
                )
            assert received == [], "a refused peer must never receive the request"

    def test_no_verifier_sends_request(self):
        from kiro_crew.loopback_http import tcp_verified_urlopen

        with self._server() as (port, received):
            # flake-ok: pre-existing 5 s socket ceiling on a loopback request to the in-test server, moved verbatim; tracked for a de-flake pass
            with tcp_verified_urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                assert resp.status == 200
            assert received == ["/health"]
