"""The client side of ``kiro_crew.cli_server``: how the CLI finds and talks to its gateway.

This file pins ``resolve_client_port_ex`` falling through a malformed port variable,
the dashboard health probe warning only on the stale-dashboard marker, ``token``
reporting the reason a gateway refuses with, ``token`` and ``logout`` sending the
owner-minting secret only to this install's own gateway, and the owner-verified secret
transport binding its ownership proof to the connected socket on every path. It is one
of three files on ``cli_server``'s command dispatchers; the family's seams are
described in ``test_cli_server_more_coverage.py``.
"""

import argparse
import os
import sys
import urllib.error
from unittest.mock import MagicMock

import pytest
from cli_server_more_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Resp,
    sel_rec_fixture,
)

from kiro_crew import cli_server, platform_compat
from kiro_crew.config.loader import _DEFAULT_PORT
from kiro_crew.dashboard.handlers.core import DASHBOARD_HTML_NOT_FOUND_MARKER
from kiro_crew.testing.wait import wait_until

# Some classes route the owner-verified transport to a stub via an autouse
# fixture; tests that assert on the real helper restore it from here.
_REAL_OWNER_VERIFIED_SECRET_URLOPEN = cli_server._owner_verified_secret_urlopen


# --------------------------------------------------------------------------
# resolve_client_port_ex — malformed env values must fall through, not raise
# --------------------------------------------------------------------------


class TestResolveClientPortExFallThrough:
    """A non-numeric port env var degrades to the next source, never ValueError."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture moved verbatim and no test in the cli_server coverage files calls monkeypatch.undo()
    def _no_other_sources(self, monkeypatch):
        monkeypatch.setattr(cli_server, "_config_url_port", lambda: None)
        monkeypatch.setattr(cli_server, "_marker_port", lambda: None)

    def test_garbage_kirocrew_port_falls_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PORT", "not-a-number")
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        assert cli_server.resolve_client_port_ex(None) == (_DEFAULT_PORT, False)

    def test_garbage_bound_port_falls_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "12x4")
        assert cli_server.resolve_client_port_ex(None) == (_DEFAULT_PORT, False)

    def test_garbage_kirocrew_port_still_honours_bound_port(self, monkeypatch) -> None:
        """The fall-through lands on the NEXT source, not straight on the default."""
        monkeypatch.setenv("KIROCREW_PORT", "oops")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "9931")
        assert cli_server.resolve_client_port_ex(None) == (9931, True)


# --------------------------------------------------------------------------
# _probe_dashboard_health
# --------------------------------------------------------------------------


class TestProbeDashboardHealth:
    """Warns only when the served body carries the stale-dashboard marker."""

    def test_stale_marker_warns_on_stderr(self, monkeypatch, capsys) -> None:
        body = f"<html>{DASHBOARD_HTML_NOT_FOUND_MARKER}</html>".encode()
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: _Resp(body))
        cli_server._probe_dashboard_health(5476)
        assert "stale dashboard" in capsys.readouterr().err

    def test_healthy_body_is_silent(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b"<html>ok</html>")
        )
        cli_server._probe_dashboard_health(5476)
        assert capsys.readouterr().err == ""

    def test_network_error_is_swallowed(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        cli_server._probe_dashboard_health(5476)  # must not raise
        assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# _token
# --------------------------------------------------------------------------


class TestTokenRefusal:
    """A gateway that ANSWERS with an error is not a gateway that could not be
    reached: the operator needs the reason the gateway gave, not a network hint."""

    @pytest.fixture(autouse=True)
    def _route_secret_transport(self, _floor_monkeypatch):
        # These tests stub loopback_urlopen to simulate the verified gateway's
        # HTTP response. Route the owner-verified secret transport through that
        # stub so they drive the real send path without standing up a unix
        # socket + SO_PEERCRED peer. The transport's own proof is covered by
        # TestOwnerVerifiedSecretTransport.
        _floor_monkeypatch.setattr(
            cli_server,
            "_owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": cli_server.loopback_urlopen(
                req, timeout=timeout
            ),
        )

    def test_403_prints_the_gateway_reason(self, monkeypatch, capsys) -> None:
        import io
        import json

        reason = (
            "The gateway could not verify this process as the local owner. "
            "Open the dashboard using its CLI login link."
        )
        body = json.dumps({"error": reason, "code": "member_owner_token_refused"}).encode()

        def refused(req, port, timeout, *, operation=""):
            raise urllib.error.HTTPError(
                "http://127.0.0.1/api/token/local", 403, "Forbidden", {}, io.BytesIO(body)
            )

        monkeypatch.setattr(cli_server, "run_preflight_checks", lambda: None)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _port: 5476)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        monkeypatch.setattr(cli_server, "_verified_loopback_gateway_pids", lambda _port: [4242])
        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", refused)
        with pytest.raises(SystemExit) as exc:
            cli_server._token(argparse.Namespace(ttl="1h", port=None))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "HTTP 403" in err and "could not verify this process as the local owner" in err
        assert "Could not reach gateway" not in err
        assert "kirocrew service install" not in err

    def test_connection_refused_still_reports_unreachable(self, monkeypatch, capsys) -> None:
        def boom(req, port, timeout, *, operation=""):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(cli_server, "run_preflight_checks", lambda: None)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _port: 5476)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        monkeypatch.setattr(cli_server, "_verified_loopback_gateway_pids", lambda _port: [4242])
        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._token(argparse.Namespace(ttl="1h", port=None))
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert "Could not reach gateway on port 5476" in err
        # Nothing listening is a state the operator can fix, and this line is
        # what a remote mint carries back verbatim: it must name both ways to
        # get a gateway listening on the host.
        assert "kirocrew gateway" in err
        assert "kirocrew service install" in err
        # The stdout contract (cli.md): failure prose never reaches stdout.
        assert out == ""

    @pytest.mark.parametrize(
        "slow",
        [
            TimeoutError("timed out"),
            urllib.error.URLError(TimeoutError("timed out")),
        ],
        ids=["read-timeout", "urlopen-timeout"],
    )
    def test_slow_gateway_reports_busy_not_unreachable(self, monkeypatch, capsys, slow) -> None:
        seen: dict[str, float] = {}

        def stalls(*a, **k):
            seen["timeout"] = k["timeout"]
            raise slow

        monkeypatch.setattr(cli_server, "run_preflight_checks", lambda: None)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _port: 5476)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", stalls)
        with pytest.raises(SystemExit) as exc:
            cli_server._token(argparse.Namespace(ttl="1h", port=None))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Gateway on port 5476 is up but did not answer within 20 s (busy)" in err
        assert "Could not reach gateway" not in err
        assert "kirocrew service install" not in err
        assert "status" not in err
        assert seen["timeout"] >= 15


# --------------------------------------------------------------------------
# _logout
# --------------------------------------------------------------------------


class TestPortOwnershipGuard:
    """token and logout send the owner-minting local secret only to this
    install's own gateway, proven on the live connection in
    _owner_verified_secret_urlopen (_connected_peer_verdict on TCP, SO_PEERCRED
    on the unix socket). The proof writes the secret only on a positively
    confirmed MATCH and refuses (fail closed) on a foreign peer or one it cannot
    resolve; it audits the allowed / denied decision itself, like the
    neighboring _stop / _restart gateway decisions. No pre-flight port snapshot
    precedes it -- the connection-bound proof is the single authoritative gate
    (the end-to-end proof, refusal and audit are covered by
    TestOwnerVerifiedSecretTransport). stop's authenticated-shutdown fallback
    deliberately does NOT gate on ownership: the argv-declined arm already
    carries a positive proof and the empty-lookup arm is an intentionally
    unverified fallback."""

    @pytest.fixture(autouse=True)
    def _route_secret_transport(self, _floor_monkeypatch):
        # The proceed tests reach the send. Route the owner-verified secret
        # transport through each test's loopback_urlopen stub so the proceed path
        # works without a real unix socket. The transport's own connection-bound
        # proof (and its refusals/audit) is covered by
        # TestOwnerVerifiedSecretTransport.
        _floor_monkeypatch.setattr(
            cli_server,
            "_owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": cli_server.loopback_urlopen(
                req, timeout=timeout
            ),
        )

    def _explode_if_consulted(self, _port):  # pragma: no cover - must never run
        raise AssertionError("ownership proof consulted where it cannot succeed")

    def test_token_proceeds_when_the_gateway_is_verified(self, monkeypatch, capsys) -> None:
        # The positive path: a verified POSIX gateway reaches the secret and
        # mints the token.
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        read = MagicMock(return_value="s3cr3t")
        monkeypatch.setattr(cli_server, "run_preflight_checks", lambda: None)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _port: 5476)
        monkeypatch.setattr(cli_server, "read_local_secret", read)
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b'{"token": "tok"}')
        )
        monkeypatch.setattr(
            cli_server,
            "KiroCrewConfig",
            # tailscale.enabled=False keeps _emit_session_urls out of the tailnet
            # branch, which would otherwise shell out to the host `tailscale`
            # binary on a machine that has one -- a real subprocess side effect.
            MagicMock(
                load=lambda: MagicMock(
                    dashboard=MagicMock(url="", tailscale=MagicMock(enabled=False))
                )
            ),
        )
        monkeypatch.setattr(cli_server, "dashboard_origin", lambda _u: "")
        monkeypatch.setattr(
            cli_server, "resolve_dashboard_host", lambda local_only=True: "h.invalid"
        )
        monkeypatch.setattr(cli_server, "_probe_dashboard_health", lambda _p: None)
        cli_server._token(argparse.Namespace(ttl="1h", port=None))
        read.assert_called_once()
        assert "token=tok" in capsys.readouterr().out

    def test_logout_proceeds_when_the_gateway_is_verified(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        read = MagicMock(return_value="s3cr3t")
        monkeypatch.setattr(cli_server, "read_local_secret", read)
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b'{"ok": true}'))
        cli_server._logout(5476)
        read.assert_called_once()
        assert "revoked" in capsys.readouterr().out

    def test_logout_peer_refused_does_not_report_gateway_down(self, monkeypatch, capsys) -> None:
        # Issue 2: a live gateway whose connection-bound ownership proof refused
        # must NOT be mislabelled "Gateway not running". _GatewayPeerRefused is
        # NOT an OSError, so it escapes do_open's OSError->URLError wrap and
        # reaches the dedicated handler instead of the (URLError, OSError) arm.
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")

        def _refuse(*_a, **_k):
            raise cli_server._GatewayPeerRefused("peer not proven")

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _refuse)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "ownership not verified" in out
        assert "Gateway not running" not in out

    def test_shutdown_never_consults_ownership_and_reaches_the_secret(self, monkeypatch) -> None:
        # The authenticated-shutdown fallback applies no port-lookup pre-check
        # (the empty-lookup arm runs exactly when that lookup cannot be
        # trusted); its proof is the connection-bound one inside the send
        # helper, routed to a stub here by the class fixture.
        read = MagicMock(return_value="s3cr3t")
        resp = MagicMock(status=200)
        resp.read.return_value = b'{"ok": true, "shutting_down": true}'
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(
            cli_server, "_verified_loopback_gateway_pids", self._explode_if_consulted
        )
        monkeypatch.setattr(cli_server, "read_local_secret", read)
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: ctx)
        assert cli_server._request_gateway_shutdown(5476) is True
        read.assert_called_once()

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="drives the AF_UNIX peer-verified shutdown send; AF_UNIX-only",
    )
    def test_shutdown_rides_the_peer_verified_socket_when_it_exists(
        self, monkeypatch, tmp_path, sel_rec
    ) -> None:
        # With the owner-only socket present, the shutdown secret must go
        # over it with the connection-bound peer proof -- no port tool needed, so
        # the fallback is not deleted -- and never over plain loopback TCP.
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr(
            cli_server, "_owner_verified_secret_urlopen", _REAL_OWNER_VERIFIED_SECRET_URLOPEN
        )
        sock_file = tmp_path / "gw.sock"
        sock_file.write_bytes(b"")
        monkeypatch.setattr(cli_server, "dashboard_socket_path", lambda _p: sock_file)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: pytest.fail("unverified TCP send")
        )
        monkeypatch.setattr(cli_server, "check_peer_is_self", lambda _s: PeerCredResult.MATCH)
        verdict = {"ours": True}
        monkeypatch.setattr(
            cli_server, "_connected_unix_peer_is_recorded_gateway", lambda _s, _p: verdict["ours"]
        )
        sent: list[tuple[str, str]] = []

        def _unix(req, timeout, *, socket_path, verify_peer):
            verify_peer(object())  # connected-socket proof, before any byte
            sent.append((socket_path, req.headers.get("X-local-secret")))
            return _Resp(b'{"ok": true, "shutting_down": true}')

        monkeypatch.setattr(cli_server, "unix_socket_urlopen", _unix)

        assert cli_server._request_gateway_shutdown(5476) is True
        assert sent == [(str(sock_file), "s3cr3t")]

        # A same-user peer that is not the recorded gateway: refused, nothing
        # sent, and the caller gets False (its no-target diagnostic), not a crash.
        sent.clear()
        verdict["ours"] = False
        assert cli_server._request_gateway_shutdown(5476) is False
        assert sent == []
        denied = [c for c in sel_rec.calls if c.get("outcome") == "denied"]
        assert denied and denied[-1]["operation"] == "gateway_stop"

    def test_shutdown_without_a_socket_needs_the_tcp_peer_proof(
        self, monkeypatch, tmp_path, sel_rec
    ) -> None:
        # No owner-only socket (TCP-only gateway, native Windows): the secret
        # rides loopback TCP only with the connection-bound proof. A listener
        # that is not the recorded gateway -- a co-resident on a stale port --
        # gets nothing, and the caller sees False.
        monkeypatch.setattr(
            cli_server, "_owner_verified_secret_urlopen", _REAL_OWNER_VERIFIED_SECRET_URLOPEN
        )
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: pytest.fail("unverified TCP send")
        )
        verdict = {"v": cli_server._ConnectedPeerVerdict.MISMATCH}
        monkeypatch.setattr(cli_server, "_connected_peer_verdict", lambda _s: verdict["v"])
        sent: list[str] = []

        def _tcp(req, timeout, *, verify_peer):
            verify_peer(object())  # connected-socket proof, before any byte
            sent.append(req.headers.get("X-local-secret"))
            return _Resp(b'{"ok": true, "shutting_down": true}')

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", _tcp)

        assert cli_server._request_gateway_shutdown(5476) is False
        assert sent == []
        denied = [c for c in sel_rec.calls if c.get("outcome") == "denied"]
        assert denied and denied[-1]["operation"] == "gateway_stop"

        verdict["v"] = cli_server._ConnectedPeerVerdict.MATCH
        assert cli_server._request_gateway_shutdown(5476) is True
        assert sent == ["s3cr3t"]

    @pytest.fixture
    def secret_home(self, monkeypatch, tmp_path):
        (tmp_path / ".local_secret").write_text("s3cr3t\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "s3cr3t")
        return tmp_path

    def test_missing_secret_reports_gateway_down(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _port, **_kw: "")
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "Gateway not running" in capsys.readouterr().out

    def test_ok_response_reports_success(self, monkeypatch, secret_home, capsys) -> None:
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b'{"ok": true}'))
        cli_server._logout(5476)
        assert "revoked" in capsys.readouterr().out

    def test_not_ok_response_surfaces_error_field(self, monkeypatch, secret_home, capsys) -> None:
        monkeypatch.setattr(
            cli_server,
            "loopback_urlopen",
            lambda *a, **k: _Resp(b'{"ok": false, "error": "store locked"}'),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "store locked" in capsys.readouterr().out

    def test_http_error_reports_status_code(self, monkeypatch, secret_home, capsys) -> None:
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 403, "no", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "HTTP 403" in capsys.readouterr().out

    def test_connection_refused_reports_gateway_down(
        self, monkeypatch, secret_home, capsys
    ) -> None:
        def boom(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "Gateway not running" in capsys.readouterr().out

    def test_timeout_reports_busy_not_down(self, monkeypatch, secret_home, capsys) -> None:
        def boom(*a, **k):
            raise TimeoutError("timed out")

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "busy" in out
        assert "Gateway not running" not in out


# --------------------------------------------------------------------------
# _owner_verified_secret_urlopen — the secret rides a proof-bound transport
# --------------------------------------------------------------------------


class TestOwnerVerifiedSecretTransport:
    """The secret-bearing send binds its ownership proof to the connected
    socket on every path, verifying the PEER OF THE ESTABLISHED CONNECTION, not
    who owns the port now. With the gateway's own unix socket present (the
    ordinary POSIX case) it goes over that socket (which no other user can
    answer) with a verify_peer hook (SO_PEERCRED via check_peer_is_self) that
    refuses a non-self peer before any byte is written. When no AF_UNIX socket
    exists -- a TCP-only gateway (a supported config) or native Windows (no
    AF_UNIX) -- the secret rides TCP, but the proof is bound to the connection
    via tcp_verified_urlopen's verify_peer, which resolves the pid on the far
    end of this exact 4-tuple connection (get_tcp_peer_pid, not a port snapshot)
    and proves it is this install's gateway owned by this account -- by uid on
    POSIX, by access-token owner SID on Windows. The secret is sent ONLY on a
    positive MATCH: a resolved foreign peer (MISMATCH) and a peer that cannot be
    resolved at all (UNAVAILABLE -- e.g. lsof-less macOS) BOTH refuse (fail
    closed), so the owner-minting secret never rides an unproven connection. The
    POSIX uid proof is what lets a legitimate TCP-only gateway reach MATCH, so a
    supported loopback-TCP config works without the no-proof path failing open."""

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="asserts the AF_UNIX socket is preferred over TCP; AF_UNIX-only",
    )
    def test_prefers_the_unix_socket_with_a_peer_verifier_when_it_exists(
        self, monkeypatch, tmp_path
    ) -> None:
        sock_file = tmp_path / "gw.sock"
        sock_file.write_bytes(b"")  # the file must EXIST to be preferred
        monkeypatch.setattr(cli_server, "dashboard_socket_path", lambda _p: sock_file)
        captured: dict = {}

        def fake_unix(req, timeout, *, socket_path, verify_peer):
            captured["socket_path"] = socket_path
            captured["verify_peer"] = verify_peer
            return _Resp(b'{"ok": true}')

        tcp_called = MagicMock()
        monkeypatch.setattr(cli_server, "unix_socket_urlopen", fake_unix)
        monkeypatch.setattr(cli_server, "loopback_urlopen", tcp_called)
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        with cli_server._owner_verified_secret_urlopen(
            req, 5476, timeout=5, operation="gateway_logout"
        ) as resp:
            assert b"ok" in resp.read()
        assert captured["socket_path"] == str(sock_file)
        assert callable(captured["verify_peer"])
        tcp_called.assert_not_called()

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="drives the AF_UNIX verify_peer refusal; AF_UNIX-only",
    )
    def test_verify_peer_refuses_a_non_self_socket_peer(self, monkeypatch, tmp_path) -> None:
        # The verify_peer handed to the unix opener must raise unless the kernel
        # confirms the connected peer is this principal (MATCH).
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        sock_file = tmp_path / "gw.sock"
        sock_file.write_bytes(b"")
        monkeypatch.setattr(cli_server, "dashboard_socket_path", lambda _p: sock_file)

        def capture_verifier():
            holder: dict = {}

            def fake_unix(req, timeout, *, socket_path, verify_peer):
                holder["verify_peer"] = verify_peer
                return _Resp(b"{}")

            monkeypatch.setattr(cli_server, "unix_socket_urlopen", fake_unix)
            req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
            cli_server._owner_verified_secret_urlopen(
                req, 5476, timeout=5, operation="gateway_logout"
            )
            return holder["verify_peer"]

        # cli_server binds check_peer_is_self at module import, so patch the name
        # on cli_server BEFORE each capture so the closure sees the stubbed verdict.
        monkeypatch.setattr(cli_server, "check_peer_is_self", lambda _s: PeerCredResult.MISMATCH)
        with pytest.raises(cli_server._GatewayPeerRefused):
            capture_verifier()(object())

        # Same-uid peer (MATCH) but NOT this install's recorded gateway (a
        # same-uid impostor that rebound the socket path) must still refuse --
        # the uid alone is insufficient.
        monkeypatch.setattr(cli_server, "check_peer_is_self", lambda _s: PeerCredResult.MATCH)
        monkeypatch.setattr(
            cli_server, "_connected_unix_peer_is_recorded_gateway", lambda _s, _p: False
        )
        with pytest.raises(cli_server._GatewayPeerRefused):
            capture_verifier()(object())

        # Same-uid peer that IS the recorded gateway (pid + start token match)
        # proceeds.
        monkeypatch.setattr(
            cli_server, "_connected_unix_peer_is_recorded_gateway", lambda _s, _p: True
        )
        capture_verifier()(object())  # must not raise

    def test_posix_without_a_unix_socket_sends_over_tcp_with_the_peer_proof(
        self, monkeypatch, tmp_path
    ) -> None:
        # A TCP-only gateway (no AF_UNIX socket) is a supported config: sign-in
        # must still work. The send falls through to the connection-bound TCP
        # proof instead of hard-failing, and a MATCH sends.
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        captured: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            captured["verify_peer"] = verify_peer
            return _Resp(b'{"ok": true}')

        unix = MagicMock()
        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
        monkeypatch.setattr(cli_server, "unix_socket_urlopen", unix)
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        with cli_server._owner_verified_secret_urlopen(
            req, 5476, timeout=5, operation="gateway_logout"
        ) as resp:
            assert b"ok" in resp.read()
        unix.assert_not_called()  # no socket to prefer
        verify_peer = captured["verify_peer"]
        # MATCH: the connected peer is our gateway -> sends.
        monkeypatch.setattr(
            cli_server, "_connected_peer_verdict", lambda _s: cli_server._ConnectedPeerVerdict.MATCH
        )
        verify_peer(object())  # must not raise

    @pytest.mark.parametrize(
        "scenario",
        [
            pytest.param(
                "resolved foreign peer",
                id="test_posix_tcp_fallback_refuses_a_resolved_foreign_peer",
            ),
            pytest.param(
                "unresolvable peer",
                id="test_posix_tcp_fallback_refuses_when_the_peer_is_unresolvable",
            ),
        ],
    )
    def test_posix_tcp_fallback_refuses_a_mismatched_peer(
        self, scenario, monkeypatch, tmp_path
    ) -> None:
        # The co-resident-rival hole stays closed on the TCP fallback: a resolved
        # foreign peer refuses the secret, exactly as the unix path refuses a
        # non-self peer. A peer the host cannot resolve at all (lsof-less macOS)
        # fails closed the same way: _connected_peer_verdict folds both into
        # MISMATCH, which is why both scenarios drive that one verdict here.
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        captured: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            captured["verify_peer"] = verify_peer
            return _Resp(b"{}")

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        cli_server._owner_verified_secret_urlopen(req, 5476, timeout=5, operation="gateway_logout")
        verify_peer = captured["verify_peer"]
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        with pytest.raises(cli_server._GatewayPeerRefused):
            verify_peer(object())

    def test_connection_bound_denial_emits_a_denied_sel_audit_event(
        self, monkeypatch, tmp_path, sel_rec
    ) -> None:
        # Audit integrity: the pre-flight may allow/defer, then the authoritative
        # connection-bound gate still refuses. That denial must leave its own
        # `denied` SEL event -- it must not be silent while the pre-flight's
        # allow stands, or the audit trail asserts a verified allow for an
        # operation that was actually denied.
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        captured: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            captured["verify_peer"] = verify_peer
            return _Resp(b"{}")

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        cli_server._owner_verified_secret_urlopen(req, 5476, timeout=5, operation="gateway_logout")
        with pytest.raises(cli_server._GatewayPeerRefused):
            captured["verify_peer"](object())
        denied = [c for c in sel_rec.calls if c.get("outcome") == "denied"]
        assert denied and denied[-1]["operation"] == "gateway_logout"
        assert "connected_peer_unverified" in denied[-1]["resources"]

    def test_connection_bound_grant_emits_an_allowed_sel_audit_event(
        self, monkeypatch, tmp_path, sel_rec
    ) -> None:
        # Audit integrity, mirror of the denial: when the connected peer verifies
        # (MATCH) the authoritative gate authorizes the owner-minting send. That
        # positive permission decision must leave exactly one `allowed` SEL event
        # naming the verified-peer basis -- the grant cannot be unrecorded while
        # the denial is audited.
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        captured: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            captured["verify_peer"] = verify_peer
            return _Resp(b"{}")

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", fake_tcp)
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MATCH,
        )
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        cli_server._owner_verified_secret_urlopen(req, 5476, timeout=5, operation="gateway_logout")
        captured["verify_peer"](object())  # MATCH -- authorizes the send, no raise
        allowed = [
            c
            for c in sel_rec.calls
            if c.get("outcome") == "allowed" and "connected_peer_verified" in c.get("resources", "")
        ]
        assert len(allowed) == 1
        assert allowed[0]["operation"] == "gateway_logout"
        assert f"port={5476}" in allowed[0]["resources"]

    def test_connected_unix_peer_is_recorded_gateway_requires_pid_and_token(
        self, monkeypatch
    ) -> None:
        # The uid check proves only same-account; a same-uid impostor that
        # rebound the socket path passes it. The pid + start-token bind defeats
        # that: only the recorded gateway's pid, carrying the recorded start
        # token, is this install's gateway.
        monkeypatch.setattr(cli_server.run_marker, "pid_start_token", lambda _pid: "tok-real")

        def record(pid, token):
            monkeypatch.setattr(
                cli_server.run_marker, "read_pid_record_path", lambda _p: (pid, token)
            )

        # Peer pid == recorded pid AND start token matches -> recorded gateway.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        record(4242, "tok-real")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 5476) is True

        # Same-uid impostor on a DIFFERENT pid -> refused.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 9999)
        record(4242, "tok-real")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 5476) is False

        # Recorded pid but a start token that does not match the live one
        # (a recycled pid / forged record) -> refused.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        record(4242, "tok-stale")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 5476) is False

        # Peer pid unreadable -> fail closed.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: None)
        record(4242, "tok-real")
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 5476) is False

        # No recorded gateway for the port -> fail closed.
        monkeypatch.setattr(cli_server, "get_peer_pid", lambda _s: 4242)
        monkeypatch.setattr(cli_server.run_marker, "read_pid_record_path", lambda _p: None)
        assert cli_server._connected_unix_peer_is_recorded_gateway(object(), 5476) is False

    def test_windows_binds_the_proof_to_the_connected_tcp_socket(self, monkeypatch) -> None:
        # Native Windows has no AF_UNIX, so the secret rides TCP -- but the proof
        # is bound to the connected socket: tcp_verified_urlopen gets a
        # verify_peer that passes only when the PEER OF THE ESTABLISHED
        # CONNECTION is our recorded gateway owned by this account.
        import kiro_crew.cli_server as cs

        monkeypatch.delattr(cs.socket, "AF_UNIX", raising=False)
        captured: dict = {}

        def fake_tcp(req, timeout, *, verify_peer):
            captured["verify_peer"] = verify_peer
            return _Resp(b'{"ok": true}')

        monkeypatch.setattr(cs, "tcp_verified_urlopen", fake_tcp)
        unix = MagicMock()
        monkeypatch.setattr(cs, "unix_socket_urlopen", unix)
        req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
        with cs._owner_verified_secret_urlopen(
            req, 5476, timeout=5, operation="gateway_logout"
        ) as resp:
            assert b"ok" in resp.read()
        unix.assert_not_called()
        verify_peer = captured["verify_peer"]
        # verify_peer delegates to the connected-connection peer identity proof.
        # Windows always resolves (owner-PID table), so only MATCH sends and a
        # MISMATCH refuses.
        monkeypatch.setattr(
            cs, "_connected_peer_verdict", lambda _s: cs._ConnectedPeerVerdict.MATCH
        )
        verify_peer(object())  # must not raise
        monkeypatch.setattr(
            cs, "_connected_peer_verdict", lambda _s: cs._ConnectedPeerVerdict.MISMATCH
        )
        with pytest.raises(cs._GatewayPeerRefused):
            verify_peer(object())

    def test_tcp_only_gateway_sends_on_match_and_refuses_otherwise(self, monkeypatch) -> None:
        # FP's single-authoritative-proof contract (and the sole off-POSIX gate
        # now that the record-based pre-flight is gone): on a TCP-only / no
        # unix-socket gateway, the connection-bound verdict is the ONLY thing
        # deciding whether the owner-minting secret is sent. MATCH sends;
        # MISMATCH (a resolved foreign peer or an unresolvable one) refuses --
        # the secret is never written to an unverified peer, so
        # removing the netstat sole-owner pre-flight opens no hole: an unproven
        # peer still cannot receive it.
        import kiro_crew.cli_server as cs

        monkeypatch.delattr(cs.socket, "AF_UNIX", raising=False)
        monkeypatch.setattr(cs, "unix_socket_urlopen", MagicMock())

        for verdict, must_send in (
            (cs._ConnectedPeerVerdict.MATCH, True),
            (cs._ConnectedPeerVerdict.MISMATCH, False),
        ):
            captured: dict = {}

            def fake_tcp(req, timeout, *, verify_peer):
                captured["verify_peer"] = verify_peer
                return _Resp(b'{"ok": true}')

            monkeypatch.setattr(cs, "tcp_verified_urlopen", fake_tcp)
            monkeypatch.setattr(cs, "_connected_peer_verdict", lambda _s, _v=verdict: _v)
            req = urllib.request.Request("http://127.0.0.1:5476/api/logout")
            with cs._owner_verified_secret_urlopen(
                req, 5476, timeout=5, operation="gateway_logout"
            ) as resp:
                assert b"ok" in resp.read()
            verify_peer = captured["verify_peer"]
            if must_send:
                verify_peer(object())  # MATCH: the secret header is written
            else:
                with pytest.raises(cs._GatewayPeerRefused):
                    verify_peer(object())  # unproven peer: refuse, never send

    def test_connected_peer_identity_proof(self, monkeypatch) -> None:
        # The connection-bound proof: the PEER PID of THIS established 4-tuple
        # connection (get_tcp_peer_pid, not a port snapshot) must equal the
        # recorded gateway pid, with a matching token, owned by this account --
        # by uid on POSIX, by access-token owner SID on Windows.
        import kiro_crew.cli_server as cs
        from kiro_crew.instances import run_marker

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 5476)

        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: (4242, "tok"))
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok")

        # --- POSIX: ownership proven by uid (process_owner_uid == os.getuid). A
        # legitimate same-user TCP-only gateway resolves to MATCH -- this is the
        # path that keeps POSIX loopback-TCP working (F2).
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cs.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)

        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 4242)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MATCH

        # A DIFFERENT pid than the record (an attacker that accepted this
        # connection) -- MISMATCH.
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 9999)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

        # Peer pid matches but is owned by a DIFFERENT uid -- MISMATCH.
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 4242)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 65534)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

        # Peer uid unresolvable (process gone) -- MISMATCH, fail closed.
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: None)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

        # Peer identity unresolvable on this exact connection -- MISMATCH, never
        # a port snapshot. Zero the accept-queue retry window so the test does
        # not wait it out.
        monkeypatch.setattr(cs, "_PEER_RESOLVE_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: None)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

        # --- Windows: ownership proven by access-token owner SID (no uid there).
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-me")
        monkeypatch.setattr(platform_compat, "process_owner_sid", lambda _pid: "S-1-5-21-me")
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: 4242)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MATCH
        # Foreign owner SID -- MISMATCH.
        monkeypatch.setattr(platform_compat, "process_owner_sid", lambda _pid: "S-1-5-21-attacker")
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

    def test_connected_peer_proof_passes_the_gateway_endpoint_as_client(self, monkeypatch) -> None:
        # Pin the inversion: the gateway is the far end, so its endpoint (our
        # getpeername) is passed as client_endpoint and ours (getsockname) as
        # server_endpoint, so get_tcp_peer_pid resolves the GATEWAY's pid.
        import kiro_crew.cli_server as cs
        from kiro_crew.instances import run_marker

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 5476)

        seen: dict = {}

        def fake_peer_pid(*, server_endpoint, client_endpoint):
            seen["server"] = server_endpoint
            seen["client"] = client_endpoint
            return 4242

        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", fake_peer_pid)
        monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: (4242, "tok"))
        monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: "tok")
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(cs.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(platform_compat, "process_owner_uid", lambda _pid: 1000)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MATCH
        assert seen["client"] == ("127.0.0.1", 5476)  # gateway endpoint
        assert seen["server"] == ("127.0.0.1", 54321)  # our endpoint

    def test_unresolvable_peer_is_mismatch(self, monkeypatch) -> None:
        # One fail-closed decision: an unresolvable peer (get_tcp_peer_pid ->
        # None across the retry window) is MISMATCH on every host, whether it
        # could resolve peers in principle or not (lsof-less macOS).
        import kiro_crew.cli_server as cs

        class _Sock:
            def getsockname(self):
                return ("127.0.0.1", 54321)

            def getpeername(self):
                return ("127.0.0.1", 5476)

        monkeypatch.setattr(cs, "_PEER_RESOLVE_RETRY_SECONDS", 0.0)
        monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", lambda **_kw: None)
        assert cs._connected_peer_verdict(_Sock()) is cs._ConnectedPeerVerdict.MISMATCH

    def test_connected_peer_verdict_on_a_real_loopback_socket_unmocked(
        self, monkeypatch, tmp_path
    ) -> None:
        # Design blocker: every other verdict test stubs get_tcp_peer_pid or
        # _connected_peer_verdict itself, so none proves the resolver actually
        # names the peer of a live connection. This one opens a REAL loopback TCP
        # connection, writes a REAL pid record for this process, and runs
        # _connected_peer_verdict UNSTUBBED. The kernel 4-tuple resolver
        # (get_tcp_peer_pid: /proc on Linux, GetExtendedTcpTable on Windows) runs
        # for real on every OS this test runs on -- it has no POSIX skip, so it
        # exercises the Windows CI leg too. Both ends live in this process, so the
        # resolved peer pid IS this process's pid and ownership (uid on POSIX, SID
        # on Windows) is this account's -- the steady state a live same-user
        # gateway presents. Proves the MATCH path works end to end, then flips the
        # recorded pid to prove a foreign peer is refused.
        import socket

        import kiro_crew.cli_server as cs
        from kiro_crew.instances import run_marker

        # Real record for THIS process, written by the shipped writer, into a tmp
        # config dir both the writer (run_marker.config_dir) and the reader
        # (cli_server.config_dir, used by the verdict) resolve against.
        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cs, "config_dir", lambda: tmp_path)
        (tmp_path / run_marker.RUN_DIR_NAME).mkdir(parents=True, exist_ok=True)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        gateway_port = server.getsockname()[1]
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        accepted = None
        try:
            client.connect(("127.0.0.1", gateway_port))
            accepted, _ = server.accept()  # the gateway now holds the far fd
            run_marker.write_marker(gateway_port)  # real pid + start-token record

            resolvable = (
                sys.platform == "linux"
                or platform_compat.IS_WINDOWS
                or platform_compat.listening_pid_tool_available()
            )
            if not resolvable:
                # A host that structurally cannot resolve a loopback peer (an
                # lsof-less macOS runner) returns MISMATCH for a real connection
                # -- the fail-closed state the secret-send caller refuses on.
                # Assert that rather than skipping, so the ratchet still
                # tightens on every runner.
                monkeypatch.setattr(cs, "_PEER_RESOLVE_RETRY_SECONDS", 0.0)
                assert cs._connected_peer_verdict(client) is cs._ConnectedPeerVerdict.MISMATCH
                return

            # On a resolvable host (Linux /proc, Windows GetExtendedTcpTable) the
            # resolver needs the accepted connection to be visible in the kernel
            # table. Wait for that fixture readiness first, so the verdict
            # below runs with its shipped resolve budget unchanged.
            cli_end = client.getsockname()[:2]
            gateway_end = client.getpeername()[:2]
            wait_until(
                lambda: platform_compat.get_tcp_peer_pid(
                    server_endpoint=(str(cli_end[0]), int(cli_end[1])),
                    client_endpoint=(str(gateway_end[0]), int(gateway_end[1])),
                )
                is not None,
                describe=lambda: f"kernel table never listed {gateway_end}->{cli_end}",
            )
            assert cs._connected_peer_verdict(client) is cs._ConnectedPeerVerdict.MATCH

            # Record a pid that is not this process: the same live connection,
            # resolving to this pid, fails to match the record -> fail-closed
            # MISMATCH. Still fully unmocked.
            record_path = (
                tmp_path / run_marker.RUN_DIR_NAME / run_marker.pid_file_name(gateway_port)
            )
            foreign_pid = os.getpid() + 1
            record_path.write_text(f"{foreign_pid}\n", encoding="utf-8")
            assert cs._connected_peer_verdict(client) is cs._ConnectedPeerVerdict.MISMATCH
        finally:
            client.close()
            if accepted is not None:
                accepted.close()
            server.close()
