"""Tests for dashboard origin helpers."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.origin import (
    bind_address_for,
    build_allowed_origins,
    build_dashboard_url,
    check_origin,
    dashboard_origin,
    format_dashboard_urls,
    parse_dashboard_url,
    resolve_dashboard_host,
    should_canonicalize_host,
)


class TestBindAddressFor:
    """KIROCREW_BIND overrides ONLY the TCP bind address (container support).

    The override must never touch local_only semantics (URLs, CSRF origin
    set, canonicalization) — those are covered by the existing suites; here
    we pin the bind resolution itself plus the fail-narrow validation rule:
    a malformed value falls back to loopback (can only narrow exposure,
    never widen it).
    """

    def test_default_is_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        assert bind_address_for(True) == "127.0.0.1"

    def test_local_only_false_binds_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        assert bind_address_for(False) == "0.0.0.0"

    def test_env_override_binds_all_interfaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert bind_address_for(True) == "0.0.0.0"

    def test_env_override_specific_interface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "10.0.0.7")
        assert bind_address_for(True) == "10.0.0.7"

    def test_env_override_ipv6_any(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "::")
        assert bind_address_for(True) == "::"

    def test_env_override_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", " 0.0.0.0 ")
        assert bind_address_for(True) == "0.0.0.0"

    def test_invalid_value_falls_back_to_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fail-narrow: a typo must degrade to loopback, never widen exposure.
        monkeypatch.setenv("KIROCREW_BIND", "all-interfaces-please")
        assert bind_address_for(True) == "127.0.0.1"

    def test_hostname_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only IP literals: a hostname would resolve at bind time with
        # environment-dependent results; reject rather than guess.
        monkeypatch.setenv("KIROCREW_BIND", "eth0.local")
        assert bind_address_for(True) == "127.0.0.1"

    def test_empty_value_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "")
        assert bind_address_for(True) == "127.0.0.1"

    def test_override_does_not_touch_local_only_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The override changes the TCP bind ONLY: URL building keeps
        # local_only semantics (no token requirement change here — auth
        # middleware is mounted unconditionally by the server paths).
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert resolve_dashboard_host(True) == "localhost"
        origins = build_allowed_origins(5476, True)
        assert "http://localhost:5476" in origins


class TestProbePaths:
    """PROBE_PATHS is the host_validation exemption set for orchestrator
    health probes (kubelet / Docker HEALTHCHECK address pods by IP). Pin the
    exact membership: adding a path here widens the unauthenticated,
    Host-check-free surface and must be a deliberate, reviewed act."""

    def test_exact_membership(self) -> None:
        from kiro_crew.dashboard.origin import PROBE_PATHS

        assert PROBE_PATHS == {"/api/health", "/api/live", "/api/ready"}


class TestBuildAllowedOrigins:
    def test_default_origins(self) -> None:
        origins = build_allowed_origins(5476, local_only=True)
        assert "http://127.0.0.1:5476" in origins
        assert "http://localhost:5476" in origins
        assert "http://kirocrew.localhost:5476" in origins

    def test_configured_host_adds_http_with_port(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, configured_host="myhost")
        assert "http://myhost:5476" in origins

    def test_dashboard_url_empty_no_extra_origin(self) -> None:
        baseline = build_allowed_origins(5476, local_only=True)
        with_empty = build_allowed_origins(5476, local_only=True, dashboard_url="")
        assert baseline == with_empty

    def test_dashboard_url_https_adds_origin(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kirocrew.local"
        )
        assert "https://kirocrew.local" in origins

    def test_dashboard_url_http_with_port(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, dashboard_url="http://myhost:8080")
        assert "http://myhost:8080" in origins

    def test_dashboard_url_no_scheme_normalized(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, dashboard_url="myhost:8080")
        assert "http://myhost:8080" in origins

    def test_dashboard_url_preserves_existing_origins(self) -> None:
        origins = build_allowed_origins(
            5476,
            local_only=True,
            configured_host="myhost",
            dashboard_url="https://kirocrew.local",
        )
        assert "http://myhost:5476" in origins
        assert "https://kirocrew.local" in origins
        assert "http://localhost:5476" in origins

    def test_dashboard_url_strips_default_https_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kirocrew.local:443"
        )
        assert "https://kirocrew.local" in origins
        assert "https://kirocrew.local:443" not in origins

    def test_dashboard_url_strips_default_http_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="http://kirocrew.local:80"
        )
        assert "http://kirocrew.local" in origins
        assert "http://kirocrew.local:80" not in origins

    def test_dashboard_url_keeps_non_default_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kirocrew.local:8443"
        )
        assert "https://kirocrew.local:8443" in origins

    def test_dashboard_url_malformed_port_ignored(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, dashboard_url="https://host:abc")
        assert len([o for o in origins if "host:abc" in o]) == 0


class TestDashboardOrigin:
    def test_empty_returns_empty(self) -> None:
        assert dashboard_origin("") == ""

    def test_none_returns_empty(self) -> None:
        # None-safety guard: an explicit "url": null in config must be treated
        # as "no origin", not crash the caller.
        assert dashboard_origin(None) == ""  # type: ignore[arg-type]

    def test_https_url(self) -> None:
        assert dashboard_origin("https://kirocrew.local") == "https://kirocrew.local"

    def test_bare_host_defaults_to_http(self) -> None:
        assert dashboard_origin("myhost:8080") == "http://myhost:8080"

    def test_strips_default_https_port(self) -> None:
        assert dashboard_origin("https://host:443") == "https://host"

    def test_malformed_port_returns_empty(self) -> None:
        assert dashboard_origin("https://host:abc") == ""

    def test_ipv6_brackets_preserved(self) -> None:
        assert dashboard_origin("http://[::1]:8080") == "http://[::1]:8080"

    def test_ipv6_no_port(self) -> None:
        assert dashboard_origin("http://[::1]") == "http://[::1]"

    def test_ftp_scheme_rejected(self) -> None:
        assert dashboard_origin("ftp://host") == ""

    def test_file_scheme_rejected(self) -> None:
        assert dashboard_origin("file:///etc/passwd") == ""


class TestSchemeAgreement:
    """Verify parse_dashboard_url and dashboard_origin agree on scheme for bare hostnames."""

    def test_bare_hostname_gets_http(self) -> None:
        host, _ = parse_dashboard_url("myhost:9090")
        origin = dashboard_origin("myhost:9090")
        assert origin == f"http://{host}:9090"


class TestParseDashboardUrlMalformed:
    """A typo in the user-editable dashboard.url config field must not abort
    gateway startup. parse_dashboard_url is called unguarded during boot, and
    urlparse()/.port raise ValueError on a malformed IPv6 literal or a
    non-integer port — those must degrade to defaults, not propagate."""

    @pytest.fixture(autouse=True)
    def _clean_port_env(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

    def test_malformed_ipv6_falls_back_to_defaults(self) -> None:
        # urlparse("http://[::1") raises ValueError('Invalid IPv6 URL').
        host, port = parse_dashboard_url("http://[::1")
        assert host == ""
        assert port == 5476  # _DEFAULT_PORT

    def test_non_integer_port_falls_back_to_defaults(self) -> None:
        # parsed.port raises ValueError on a non-numeric port.
        host, port = parse_dashboard_url("http://myhost:notaport")
        assert host == ""
        assert port == 5476

    def test_valid_url_still_parses(self) -> None:
        host, port = parse_dashboard_url("http://myhost:9090")
        assert host == "myhost"
        assert port == 9090


# Patch target for TestFormatDashboardUrls below. format_dashboard_urls lives in
# dashboard.urls (the stdlib-only leaf module the CLI imports); dashboard.origin
# only re-exports it for back-compat. Collaborators are resolved in the defining
# module's namespace, so patching "...origin.machine_hostname" is INERT here — it
# rebinds origin's copy while format_dashboard_urls keeps calling urls'. Point at
# the defining module so the patches actually take effect.
_MOD = "kiro_crew.dashboard.urls"


class TestBuildDashboardUrl:
    def test_token_appended(self) -> None:
        assert (
            build_dashboard_url("http://localhost:5476", "abc") == "http://localhost:5476?token=abc"
        )

    def test_empty_token_returns_bare_url(self) -> None:
        assert build_dashboard_url("http://localhost:5476") == "http://localhost:5476"

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:5476", "", local_only=False)

    def test_local_without_token_ok(self) -> None:
        assert (
            build_dashboard_url("http://localhost:5476", "", local_only=True)
            == "http://localhost:5476"
        )

    def test_not_local_with_token_ok(self) -> None:
        url = build_dashboard_url("http://host:5476", "tok", local_only=False)
        assert url == "http://host:5476?token=tok"

    def test_special_chars_in_token_are_encoded(self) -> None:
        url = build_dashboard_url("http://localhost:5476", "a&b=c#d")
        assert url == "http://localhost:5476?token=a%26b%3Dc%23d"

    def test_truthy_non_bool_local_only_still_requires_token(self) -> None:
        """review-bot hardening: 'local_only is not True' catches truthy non-booleans."""
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:5476", "", local_only="yes")  # type: ignore[arg-type]


class TestFormatDashboardUrls:
    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_local_direct_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476)
        assert len(lines) == 2
        assert lines[0] == "👻 Dashboard:"
        assert "http://localhost:5476" in lines[1]

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_in_url_shown(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476?token=abc", port=5476)
        assert "token=abc" in lines[1]

    @patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 1234 5.6.7.8 5678"}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost")
    @patch(f"{_MOD}.socket.gethostbyname", side_effect=socket.gaierror)
    def test_remote_ssh_tunnel_instructions(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476?token=t", port=5476)
        assert any("ssh -NL 5476:localhost:5476 myhost" in ln for ln in lines)
        assert any("http://localhost:5476?token=t" in ln for ln in lines)
        assert any("systemd" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.internal.example.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_local_with_resolvable_host_adds_remote_hint(
        self, _dns: object, _mh: object, _dp: object
    ) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, local_only=True)
        assert any("Remote" in ln and "ssh -NL" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.internal.example.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_custom_host_suppresses_remote_hint(
        self, _dns: object, _mh: object, _dp: object
    ) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, has_custom_host=True)
        assert not any("Remote" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.example.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_shown_when_not_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:5476?token=t", port=5476, local_only=False)
        assert any("Proxy" in ln and "proxy.devspaces" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.example.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_hidden_when_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, local_only=True)
        assert not any("Proxy" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.example.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_propagated_to_proxy_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:5476?token=abc", port=5476, local_only=False)
        proxy_line = [ln for ln in lines if "Proxy" in ln][0]
        assert "proxy.devspaces.example.com?token=abc" in proxy_line

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476", port=5476, local_only=False)

    def test_not_local_with_non_token_query_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476?debug=1", port=5476, local_only=False)

    def test_truthy_non_bool_local_only_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476", port=5476, local_only="yes")  # type: ignore[arg-type]


class TestCheckOriginLoopbackTrust:
    """check_origin tightened (CSE SEC-016): only the bound port and explicitly
    opted-in loopback ports are trusted — not every loopback port."""

    def _make_request(
        self, origin: str, remote: str = "127.0.0.1", allowed=None, host: str = ""
    ) -> object:
        """Create a minimal mock request with Origin header and allowed_origins.

        *host* sets the request ``Host`` header (used by the same-origin
        loopback fallback).
        """
        from unittest.mock import MagicMock

        request = MagicMock()
        headers = {}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        request.headers = headers
        request.remote = remote
        # Only allow port 5476 — simulates the default config
        if allowed is None:
            allowed = {"http://localhost:5476", "http://127.0.0.1:5476"}
        request.app = {"allowed_origins": allowed}
        return request

    def test_localhost_different_port_rejected_by_default(self) -> None:
        """A loopback origin on a non-bound port is NOT trusted by default (CSRF guard)."""
        request = self._make_request("http://localhost:8777")
        assert check_origin(request) is False

    def test_127_0_0_1_different_port_rejected_by_default(self) -> None:
        request = self._make_request("http://127.0.0.1:9999")
        assert check_origin(request) is False

    def test_opted_in_loopback_port_trusted(self) -> None:
        """A loopback port the operator added (via KIROCREW_ALLOWED_LOOPBACK_PORTS,
        folded into allowed_origins) is accepted — SSH-tunnel support, opt-in."""
        allowed = {
            "http://localhost:5476",
            "http://127.0.0.1:5476",
            "http://localhost:8777",
            "http://127.0.0.1:8777",
        }
        request = self._make_request("http://localhost:8777", allowed=allowed)
        assert check_origin(request) is True

    def test_exact_match_still_works(self) -> None:
        """Standard case: origin matches allowed set exactly."""
        request = self._make_request("http://localhost:5476")
        assert check_origin(request) is True

    def test_non_loopback_origin_rejected(self) -> None:
        """Remote origin not in allowed set should be rejected."""
        request = self._make_request("http://evil.com:5476")
        assert check_origin(request) is False

    def test_no_origin_loopback_remote_trusted(self) -> None:
        """No Origin header from loopback remote (local process) is trusted."""
        request = self._make_request("", remote="127.0.0.1")
        assert check_origin(request) is True

    def test_no_origin_non_loopback_remote_rejected(self) -> None:
        """No Origin header from non-loopback remote is rejected."""
        request = self._make_request("", remote="10.0.0.5")
        assert check_origin(request) is False

    # --- same-origin loopback fallback (embedded multi-instance iframe) ---

    def test_same_origin_loopback_port_trusted(self) -> None:
        """The embedded instance iframe is served at <host>:<tunnelPort> and opens
        its WS to that same location.host, so Origin == Host. Trust it even though
        the port is not in allowed_origins."""
        request = self._make_request(
            "http://kirocrew.localhost:7779",
            host="kirocrew.localhost:7779",
        )
        assert check_origin(request) is True

    def test_same_origin_127_loopback_port_trusted(self) -> None:
        request = self._make_request("http://127.0.0.1:8777", host="127.0.0.1:8777")
        assert check_origin(request) is True

    def test_origin_host_mismatch_rejected(self) -> None:
        """SEC-016 boundary preserved: a malicious local page on an arbitrary port
        sends its own Origin while the Host is the gateway's — they differ, so the
        same-origin fallback must NOT trust it."""
        request = self._make_request("http://localhost:9999", host="kirocrew.localhost:7779")
        assert check_origin(request) is False

    def test_same_origin_non_loopback_not_trusted_by_fallback(self) -> None:
        """The fallback is loopback-only: a public host with Origin == Host must
        still go through the allowlist (not auto-trusted)."""
        request = self._make_request("http://evil.com:7779", host="evil.com:7779")
        assert check_origin(request) is False


class TestAllowedLoopbackPortsEnv:
    """KIROCREW_ALLOWED_LOOPBACK_PORTS opts specific loopback ports into the allowed set."""

    @patch.dict("os.environ", {"KIROCREW_ALLOWED_LOOPBACK_PORTS": "8777,9000"}, clear=True)
    def test_env_ports_added(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert "http://localhost:8777" in origins
        assert "http://127.0.0.1:8777" in origins
        assert "http://[::1]:8777" in origins
        assert "http://localhost:9000" in origins

    @patch.dict("os.environ", {"KIROCREW_ALLOWED_LOOPBACK_PORTS": "notaport"}, clear=True)
    def test_non_numeric_ignored(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert not any(":notaport" in o for o in origins)

    @patch.dict("os.environ", {}, clear=True)
    def test_no_env_only_bound_port(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert "http://localhost:7777" in origins
        assert "http://localhost:8777" not in origins


class TestShouldCanonicalizeHost:
    """Loopback host canonicalization for the SPA's per-origin localStorage."""

    def test_redirects_localhost_to_canonical_document_nav(self) -> None:
        assert should_canonicalize_host(
            "localhost:7777",
            "kirocrew.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_redirects_127_to_canonical(self) -> None:
        assert should_canonicalize_host(
            "127.0.0.1:7777", "localhost", method="GET", sec_fetch_dest="document"
        )

    def test_no_redirect_when_already_canonical(self) -> None:
        assert not should_canonicalize_host(
            "kirocrew.localhost:7777",
            "kirocrew.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_no_redirect_for_non_document_dest(self) -> None:
        # XHR / fetch / websocket / sub-resource requests must never be redirected.
        for dest in ("empty", "websocket", "script", "style", "image", None):
            assert not should_canonicalize_host(
                "localhost:7777",
                "kirocrew.localhost",
                method="GET",
                sec_fetch_dest=dest,
            )

    def test_no_redirect_for_mutating_methods(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert not should_canonicalize_host(
                "localhost:7777",
                "kirocrew.localhost",
                method=method,
                sec_fetch_dest="document",
            )

    def test_no_redirect_for_non_loopback_request_host(self) -> None:
        # A real hostname / reverse-proxy vhost is never canonicalized.
        assert not should_canonicalize_host(
            "kirocrew.example.com:7777",
            "kirocrew.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_no_redirect_when_canonical_not_loopback(self) -> None:
        assert not should_canonicalize_host(
            "localhost:7777",
            "kirocrew.example.com",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_host_without_port(self) -> None:
        assert should_canonicalize_host(
            "localhost", "kirocrew.localhost", method="GET", sec_fetch_dest="document"
        )

    def test_ipv6_loopback_bracket_host_redirected(self) -> None:
        # [::1]:7777 must parse to ::1 (not "[") and converge like other loopbacks.
        assert should_canonicalize_host(
            "[::1]:7777",
            "kirocrew.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_ipv6_loopback_bracket_host_without_port(self) -> None:
        assert should_canonicalize_host(
            "[::1]", "kirocrew.localhost", method="GET", sec_fetch_dest="document"
        )


class TestResolveDashboardHost:
    """Canonical loopback host must be plain ``localhost`` (resolves everywhere,
    including Safari / SSH tunnels — unlike ``*.localhost``)."""

    def test_local_only_returns_localhost(self) -> None:
        assert resolve_dashboard_host(local_only=True) == "localhost"

    def test_configured_host_wins(self) -> None:
        assert (
            resolve_dashboard_host(local_only=True, configured_host="myhost.example")
            == "myhost.example"
        )


class TestBuildHostCanonicalRedirect:
    """End-to-end tests for the extracted 302 middleware (runtime behavior)."""

    @pytest.mark.asyncio
    async def test_document_nav_302s_preserving_port_path_query(self) -> None:
        from urllib.parse import urlsplit

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("kirocrew.localhost")])
        app.router.add_get("/chat", _ok)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/chat",
                params={"token": "abc123"},
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert resp.status == 302
            loc = urlsplit(resp.headers["Location"])
            assert loc.hostname == "kirocrew.localhost"  # host converged
            assert loc.port == 7777  # port preserved
            assert loc.path == "/chat"  # path preserved
            assert "token=abc123" in loc.query  # ?token= preserved

    @pytest.mark.asyncio
    async def test_xhr_and_post_not_redirected(self) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("kirocrew.localhost")])
        app.router.add_get("/api/x", _ok)
        app.router.add_post("/api/x", _ok)

        async with TestClient(TestServer(app)) as client:
            # XHR/fetch (Sec-Fetch-Dest: empty) on a loopback alias is NOT redirected.
            xhr = await client.get(
                "/api/x",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "empty"},
                allow_redirects=False,
            )
            assert xhr.status == 200
            # A mutating method is never redirected, even as a document nav.
            post = await client.post(
                "/api/x",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert post.status == 200

    @pytest.mark.asyncio
    async def test_empty_canonical_host_is_noop(self) -> None:
        # local_only=False passes canonical_host="" -> middleware never redirects
        # (reverse-proxy / remote-host deployments are untouched).
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("")])
        app.router.add_get("/chat", _ok)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/chat",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert resp.status == 200


class TestBuildAllowedHosts:
    """build_allowed_hosts derives the DNS-rebinding Host allowlist from the
    same allowed_origins set the CSRF check uses."""

    def test_loopback_floor_always_present(self) -> None:
        from kiro_crew.dashboard.origin import build_allowed_hosts

        hosts = build_allowed_hosts(set())
        assert {"localhost", "127.0.0.1", "::1", "kirocrew.localhost"} <= hosts

    def test_hostnames_extracted_port_stripped(self) -> None:
        from kiro_crew.dashboard.origin import build_allowed_hosts

        hosts = build_allowed_hosts({"http://myhost:8080", "https://kirocrew.example.com"})
        # Exact set-membership assertion (build_allowed_hosts returns a set of
        # bare hostnames, never host:port). Compare the whole derived set rather
        # than `<literal> in hosts` so the check is unambiguously exact — this
        # also avoids CodeQL py/incomplete-url-substring-sanitization, which
        # cannot tell that `hosts` is a set (exact) rather than a URL string.
        assert hosts == {
            "localhost",
            "127.0.0.1",
            "::1",
            "kirocrew.localhost",
            "myhost",  # port dropped from myhost:8080
            "kirocrew.example.com",
        }

    def test_ipv6_bracket_stripped(self) -> None:
        from kiro_crew.dashboard.origin import build_allowed_hosts

        hosts = build_allowed_hosts({"http://[::1]:8777"})
        assert "::1" in hosts


class TestCheckHost:
    """check_host is an independent DNS-rebinding barrier: it runs for every
    method and does NOT trust loopback remote."""

    def _make_request(self, host: str, allowed=None, remote: str = "127.0.0.1"):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"Host": host} if host is not None else {}
        request.remote = remote
        if allowed is None:
            allowed = {"http://localhost:5476", "http://127.0.0.1:5476"}
        # dict .get mirrors aiohttp app mapping access used by check_host
        request.app = {"allowed_origins": allowed}
        return request

    def test_spoofed_host_rejected(self) -> None:
        from kiro_crew.dashboard.origin import check_host

        req = self._make_request("evil.rebind.attacker.com")
        assert check_host(req) is False

    def test_spoofed_host_rejected_even_from_loopback_remote(self) -> None:
        """The rebinding connection IS loopback while Host is forged — loopback
        remote must NOT bypass the Host check (unlike check_origin)."""
        from kiro_crew.dashboard.origin import check_host

        req = self._make_request("attacker.com", remote="127.0.0.1")
        assert check_host(req) is False

    def test_localhost_host_accepted(self) -> None:
        from kiro_crew.dashboard.origin import check_host

        assert check_host(self._make_request("localhost:5476")) is True

    def test_loopback_ip_host_accepted(self) -> None:
        from kiro_crew.dashboard.origin import check_host

        assert check_host(self._make_request("127.0.0.1:5476")) is True

    def test_tunnel_port_host_accepted_port_independent(self) -> None:
        """SSH-tunnel local port (localhost:8777) still matches 'localhost'."""
        from kiro_crew.dashboard.origin import check_host

        assert check_host(self._make_request("localhost:8777")) is True

    def test_configured_remote_host_accepted(self) -> None:
        from kiro_crew.dashboard.origin import check_host

        allowed = {"http://localhost:5476", "https://kirocrew.example.com"}
        req = self._make_request("kirocrew.example.com", allowed=allowed)
        assert check_host(req) is True

    def test_missing_host_allowed_from_loopback(self) -> None:
        """No Host header from a loopback remote is local IPC (mcp-core, doctor)
        and is allowed; browsers always send Host so this is not a rebinding gap."""
        from kiro_crew.dashboard.origin import check_host

        assert check_host(self._make_request(None, remote="127.0.0.1")) is True

    def test_missing_host_denied_from_non_loopback(self) -> None:
        """Deny-by-default: a headerless request from a non-loopback remote is
        rejected rather than blanket-allowed (review-bot security-controls)."""
        from kiro_crew.dashboard.origin import check_host

        assert check_host(self._make_request(None, remote="10.0.0.5")) is False

    def test_empty_allowlist_denied(self) -> None:
        """Deny-by-default: a missing/empty allowed_origins must NOT bypass the
        Host check (review-bot security-controls, fail-open guard)."""
        from kiro_crew.dashboard.origin import check_host

        req = self._make_request("evil.com", allowed=set())
        assert check_host(req) is False

    def test_missing_allowed_origins_denied(self) -> None:
        """None allowlist (key never set / race) is a denial, not fail-open."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.origin import check_host

        request = MagicMock()
        request.headers = {"Host": "localhost:5476"}
        request.app = {}  # dict.get("allowed_origins") -> None
        assert check_host(request) is False


class TestFrameAncestorsValue:
    """``'self'`` plus the ancestors ``'self'`` cannot express.

    Both halves are load-bearing and each covers a case the other gets wrong, so
    both directions are pinned here — a regression in either one blanks every
    widget frame on one of the two access paths while leaving the other working,
    which is exactly how it shipped past a first round of verification.
    """

    def test_self_is_always_present(self) -> None:
        """The serving origin must be named by ``'self'``, not derived server-side.

        ``'self'`` is resolved by the BROWSER against the frame's real URL, so it is
        correct behind a TLS-terminating tunnel that rewrites ``Host`` to the
        loopback backend and may not forward ``X-Forwarded-Proto``. A header built
        from ``Host`` instead names ``http://localhost:<port>`` while the browser is
        on ``https://<tunnel-host>``, matches no ancestor, and refuses the frame —
        a regression invisible on a direct loopback connection.
        """
        from kiro_crew.dashboard.origin import frame_ancestors_value

        assert frame_ancestors_value() == "'self'"

    def test_an_embedding_ancestor_is_added(self) -> None:
        """The grandparent origin is what ``'self'`` alone leaves out.

        ``frame-ancestors`` is matched against EVERY ancestor, so a widget nested in
        the Instances embed (local dashboard -> embedded dashboard -> widget) is
        refused on ``'self'`` alone even though its immediate parent matches.
        """
        from kiro_crew.dashboard.origin import frame_ancestors_value

        assert frame_ancestors_value(["http://localhost:5476"]) == "'self' http://localhost:5476"

    def test_several_ancestors_keep_their_order(self) -> None:
        from kiro_crew.dashboard.origin import frame_ancestors_value

        value = frame_ancestors_value(["http://127.0.0.1:5476", "http://localhost:5476"])
        assert value == "'self' http://127.0.0.1:5476 http://localhost:5476"

    def test_a_duplicate_ancestor_is_not_repeated(self) -> None:
        from kiro_crew.dashboard.origin import frame_ancestors_value

        value = frame_ancestors_value(["http://localhost:5476", "http://localhost:5476"])
        assert value == "'self' http://localhost:5476"

    @pytest.mark.parametrize(
        "bad",
        [
            "http://[::1]:5476",
            "http://localhost:5476/path",
            "http://localhost:5476; script-src *",
            "http://localhost:5476 http://evil.example",
            "'self'",
            "*",
            "",
        ],
    )
    def test_an_inexpressible_or_injecting_ancestor_is_dropped(self, bad: str) -> None:
        """The producer is not trusted to have made the value header-safe.

        A bracketed IPv6 literal is not expressible as a CSP host-source (the
        browser refuses the source expression and drops it), a value carrying a
        space would smuggle a second source, and one carrying a semicolon would
        smuggle a whole directive. Each is dropped here rather than at the producer,
        so no future ancestor source can reintroduce the class.
        """
        from kiro_crew.dashboard.origin import frame_ancestors_value

        assert frame_ancestors_value([bad]) == "'self'"
