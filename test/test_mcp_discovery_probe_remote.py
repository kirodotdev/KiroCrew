"""MCP discovery: probing a remote (HTTP) server.

``_probe_remote`` reports a handshake, an HTTP error or a needs-auth challenge with the
OAuth evidence it learned; ``_read_jsonrpc_response`` takes the reply from a JSON body, or
from the last ``data:`` line of an SSE stream that carries an id; an ordinary header
reference (``${VAR}``, ``${env:VAR}``) resolves before the probe sends it, a
credential-filtered or unset one stays literal, and a resolved secret is scrubbed from
what the probe reports; the protocol version steps down once when a server refuses the
newest; and the remote timeout comes from ``_get_probe_timeout``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)
from stray_line_helpers import STRAY_LINES

# Bound at import, before the autouse fixture swaps the module attribute out, so the
# live loopback cases can put the real connector back.
from kiro_crew.link_unfurl import pinned_connector as _real_pinned_connector
from kiro_crew.mcp_discovery import (
    MCP_CLIENT_PROTOCOL_VERSION,
    MCP_REDACTED_HEADER_VALUE,
    McpServerInfo,
    _probe_cache,
    _probe_remote,
    _read_jsonrpc_response,
    downgrade_protocol_version,
    probe_metadata,
    probe_server,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT


class TestReadJsonrpcResponse:
    @pytest.mark.asyncio
    async def test_json_content_type(self) -> None:
        resp = MagicMock()
        resp.content_type = "application/json"
        resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        result = await _read_jsonrpc_response(resp)
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {}}

    @pytest.mark.asyncio
    async def test_sse_content_type(self) -> None:
        sse_body = (
            "event: message\n" 'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n' "\n"
        )
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value=sse_body)
        result = await _read_jsonrpc_response(resp)
        assert result["id"] == 1
        assert result["result"] == {"tools": []}

    @pytest.mark.asyncio
    async def test_sse_picks_last_response(self) -> None:
        """Multiple data lines — picks the last one with an id."""
        sse_body = (
            'data: {"jsonrpc": "2.0", "method": "log"}\n'
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}\n'
        )
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value=sse_body)
        result = await _read_jsonrpc_response(resp)
        assert result["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_sse_a_stray_data_line_is_skipped(self) -> None:
        """``RecursionError`` is not a ``JSONDecodeError``: unlisted, one data
        line nested past the decoder failed the whole probe."""
        strays = "".join(
            "data: " + make().decode("utf-8", "replace") for make in STRAY_LINES.values()
        )
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(
            return_value=strays + 'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}\n'
        )
        result = await _read_jsonrpc_response(resp)
        assert result["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_sse_empty_returns_empty_dict(self) -> None:
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value="")
        result = await _read_jsonrpc_response(resp)
        assert result == {}


class TestProbeRemote:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_probe_remote_ok(self) -> None:
        """Successful HTTP probe returns ok status and tools."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        init_resp = MagicMock()
        init_resp.status = 200
        init_resp.content_type = "application/json"
        init_resp.headers = {}
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

        # notifications/initialized gets a body-less accept.
        notif_resp = MagicMock()
        notif_resp.status = 202
        notif_resp.__aenter__ = AsyncMock(return_value=notif_resp)
        notif_resp.__aexit__ = AsyncMock(return_value=False)

        tools_resp = MagicMock()
        tools_resp.status = 200
        tools_resp.content_type = "application/json"
        tools_resp.json = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "search"}, {"name": "read"}]},
            }
        )
        tools_resp.__aenter__ = AsyncMock(return_value=tools_resp)
        tools_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[init_resp, notif_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search", "read"]
        # Lifecycle order: initialize, notifications/initialized, tools/list.
        methods = [c.kwargs["json"].get("method") for c in mock_session.post.call_args_list]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    @pytest.mark.asyncio
    async def test_probe_remote_carries_the_session_id(self) -> None:
        """A stateful server's Mcp-Session-Id must ride every follow-up request,
        or a HEALTHY server renders errored when it rejects the sessionless
        tools/list."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        init_resp = MagicMock()
        init_resp.status = 200
        init_resp.content_type = "application/json"
        init_resp.headers = {"Mcp-Session-Id": "sess-42"}
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

        notif_resp = MagicMock()
        notif_resp.status = 202
        notif_resp.__aenter__ = AsyncMock(return_value=notif_resp)
        notif_resp.__aexit__ = AsyncMock(return_value=False)

        tools_resp = MagicMock()
        tools_resp.status = 200
        tools_resp.content_type = "application/json"
        tools_resp.json = AsyncMock(
            return_value={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
        )
        tools_resp.__aenter__ = AsyncMock(return_value=tools_resp)
        tools_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[init_resp, notif_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        for call in mock_session.post.call_args_list[1:]:
            assert call.kwargs["headers"].get("Mcp-Session-Id") == "sess-42"

    @pytest.mark.asyncio
    async def test_probe_remote_http_error(self) -> None:
        """Non-200 response sets error status."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 500
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_probe_remote_401_reports_needs_auth(self) -> None:
        """A tokenless 401 from a remote OAuth server is needs_auth, not error.

        The runtime holds the OAuth token and calls the server fine; the probe
        never sees that token, so 401 means "authenticate", not "broken".
        """
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_probe_remote_403_with_challenge_reports_needs_auth(self) -> None:
        """A 403 carrying a WWW-Authenticate challenge is also needs_auth."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 403
        resp.headers = {
            "WWW-Authenticate": 'Bearer resource_metadata="https://example.com/.well-known"'
        }
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_probe_remote_403_without_challenge_is_error(self) -> None:
        """A plain 403 (no challenge) is a real error, not needs_auth."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 403
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "403" in result.error

    @pytest.mark.asyncio
    async def test_probe_remote_401_with_static_auth_header_is_error(self) -> None:
        """A 401 despite a configured Authorization header is a real error.

        The caller supplied a credential and it was rejected — that is a
        genuine failure, so it must not be masked as needs_auth.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer stale-token"},
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "401" in result.error

    @pytest.mark.asyncio
    async def test_probe_remote_secret_header_is_refused_without_contact(self) -> None:
        """A ``secret://`` header is reported by name and the server is not called.

        Only a server's env is resolved against the vault, so the header would
        reach the server as the literal reference.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"x-api-key": "secret://MY_TOKEN", "X-Other": "plain"},
        )

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert "'x-api-key'" in result.error
        assert "X-Other" not in result.error
        assert "${env:NAME}" in result.error
        assert "MY_TOKEN" not in result.error
        assert "MY_TOKEN" not in json.dumps(result.to_dict())

    def test_needs_authorization_predicate(self) -> None:
        """Unit-level truth table for _needs_authorization."""
        from kiro_crew.mcp_discovery import _needs_authorization

        # Tokenless 401 → authenticate.
        assert _needs_authorization(401, {}, {}) is True
        # 403 with a challenge → authenticate.
        assert _needs_authorization(403, {"WWW-Authenticate": "Bearer"}, {}) is True
        # Header lookups are case-insensitive.
        assert _needs_authorization(403, {"www-authenticate": "Bearer"}, {}) is True
        # 403 without a challenge → not an auth prompt.
        assert _needs_authorization(403, {}, {}) is False
        # A rejected static credential is a real error, never needs_auth.
        assert _needs_authorization(401, {}, {"Authorization": "Bearer x"}) is False
        assert _needs_authorization(401, {}, {"authorization": "Bearer x"}) is False
        # Other statuses are never needs_auth.
        assert _needs_authorization(500, {}, {}) is False

    def test_a_real_oauth_challenge_is_recognised(self) -> None:
        """The two pieces of evidence that make a 401 an OAuth challenge."""
        from kiro_crew.mcp_discovery import _is_bearer_challenge

        assert _is_bearer_challenge(
            'Bearer resource_metadata="https://mcp.example.ai/.well-known/'
            'oauth-protected-resource/mcp", scope="openid email offline_access"'
        )
        # Either piece alone is enough.
        assert _is_bearer_challenge('Bearer resource_metadata="https://x.example/.well-known/y"')
        assert _is_bearer_challenge('Bearer scope="openid"')

    @pytest.mark.parametrize(
        "challenge",
        [
            pytest.param("", id="empty"),
            pytest.param('Basic realm="x"', id="another-scheme-entirely"),
            pytest.param('BearerToken scope="openid"', id="bearer-prefix-is-another-scheme"),
            pytest.param(
                'bearerish resource_metadata="https://x.example/y"',
                id="bearer-word-prefix-is-another-scheme",
            ),
            pytest.param("Bearer", id="bearer-with-no-evidence"),
            pytest.param('Bearer scope=""', id="empty-scope"),
            pytest.param(
                'Bearer resource_metadata="http://insecure.example/x"', id="http-metadata"
            ),
            pytest.param(
                'Bearer resource_metadata="javascript:alert(1)"', id="non-http-scheme-metadata"
            ),
            pytest.param("Bearer resource_metadata=" + "x" * 4096, id="over-length"),
            pytest.param(None, id="not-a-string-at-all"),
        ],
    )
    def test_anything_it_cannot_vouch_for_is_not_a_challenge(self, challenge) -> None:
        """Unrecognised or unsafe evidence reads as "no challenge", and never raises.

        An http or javascript metadata URL from an unauthenticated endpoint is not
        evidence of anything, so it does not count toward recognition.
        """
        from kiro_crew.mcp_discovery import _is_bearer_challenge

        assert _is_bearer_challenge(challenge) is False

    @pytest.mark.asyncio
    async def test_a_tokenless_401_records_the_challenge_and_an_absent_grant(self) -> None:
        """needs_auth carries the evidence that separates it from 'signed in already'."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {
            "WWW-Authenticate": 'Bearer resource_metadata="https://example.com/.well-known/x",'
            ' scope="openid email"'
        }
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session),
            patch("kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=False)),
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is True
        assert result.auth_grant_present is False
        payload = result.to_dict()
        assert payload["authChallenge"] is True
        assert payload["authGrantPresent"] is False

    @pytest.mark.asyncio
    async def test_an_existing_runtime_grant_is_reported_alongside_needs_auth(self) -> None:
        """A held grant is what makes 'cannot verify' the honest wording rather than 'sign in'."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session),
            patch("kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=True)),
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_grant_present is True

    @pytest.mark.asyncio
    async def test_a_rejected_static_credential_still_records_the_oauth_challenge(self) -> None:
        """The error branch keeps the challenge — and looks up no grant.

        A pasted token against an OAuth-only server is a real error — but the
        useful thing to say is that the server wants a sign-in, not that it
        answered 401.

        The GRANT is the half that stops here. Its only reader gates on
        ``needs_auth``, so a lookup on this branch would run a stat, and let
        ``grant_observed`` write a critical SEL event, for an observation nothing
        renders.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer pasted-placeholder"},
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid offline_access"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        grant_lookup = AsyncMock(return_value=False)
        with (
            patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session),
            patch("kiro_crew.mcp_discovery._runtime_grant_present", grant_lookup),
        ):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "401" in result.error
        assert result.auth_challenge is True
        grant_lookup.assert_not_awaited()
        assert result.auth_grant_present is None
        assert "authGrantPresent" not in result.to_dict()

    def test_a_probe_that_learned_nothing_emits_no_auth_keys(self) -> None:
        """An absent key is what lets a client tell 'unknown' from 'no grant'."""
        payload = McpServerInfo(name="remote", url="https://example.com/mcp").to_dict()

        assert "authChallenge" not in payload
        assert "authGrantPresent" not in payload

    def test_the_probe_cache_round_trips_the_authorization_evidence(self) -> None:
        """The panel is served from this cache, so the evidence has to survive it."""
        from kiro_crew.mcp_discovery import _cache_probe, probe_metadata

        server = McpServerInfo(
            name="cached-remote",
            url="https://example.com/mcp",
            status="needs_auth",
            auth_challenge=True,
            auth_grant_present=True,
        )
        _cache_probe(server)

        cached = probe_metadata("cached-remote")
        assert cached is not None
        assert cached.auth_challenge is True
        assert cached.auth_grant_present is True

    @pytest.mark.asyncio
    async def test_an_unobservable_grant_is_omitted_rather_than_reported_absent(self) -> None:
        """ "Could not observe" must not reach the client as "no grant".

        The sign-in wording is gated on an explicit ``false``, so emitting ``false``
        when the lookup merely failed would tell the owner of an already-authorized
        server to sign in again — the exact harm the gate exists to prevent. An
        unreadable cache home lands here.
        """
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session),
            patch("kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=None)),
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is True
        assert result.auth_grant_present is None
        payload = result.to_dict()
        assert payload["authChallenge"] is True
        assert "authGrantPresent" not in payload

    @pytest.mark.asyncio
    async def test_the_probe_asks_for_the_absent_grant_to_be_audited(self) -> None:
        """The probe acts on absence, so it must not inherit the mint's poll default.

        ``grant_observed`` records only the positive unless a caller opts in,
        because the mint polls and would otherwise write a critical SEL event per
        iteration. The probe is the opposite shape: one read, and the NEGATIVE is
        what turns a row into "Sign-in required". Without the opt-in that acted-on
        access leaves no trail at all.
        """
        from kiro_crew.mcp_discovery import _runtime_grant_present

        lookup = AsyncMock(return_value=False)
        with patch("kiro_crew.mcp_discovery.grant_observed", lookup):
            assert await _runtime_grant_present("https://example.com/mcp", "remote") is False

        lookup.assert_awaited_once_with("https://example.com/mcp", audit_absence=True)

    def test_importing_this_module_does_not_pull_in_the_mint_engine(self) -> None:
        """The grant helpers are shared through a leaf module, not through the mint.

        This module is reachable from the handlers package the gateway imports at
        boot, and ``connections.mint`` reaches the agent and ACP layers -- which
        ``test_the_handlers_package_does_not_import_the_mint_engine`` refuses to
        have loaded at boot. Sharing via ``mcp_grant`` is what lets the grant
        lookup be an ordinary module-scope import instead of a runtime one, so
        that separation is the thing worth pinning.

        Run in a subprocess: this test module already imports the mint, so an
        in-process ``sys.modules`` check would always find it.
        """
        probe = (
            "import sys; import kiro_crew.mcp_discovery;"
            " print('MINT' if 'kiro_crew.connections.mint' in sys.modules else 'CLEAN')"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, timeout=180, **UTF8_TEXT
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip().endswith("CLEAN"), out.stdout

    @pytest.mark.asyncio
    async def test_a_re_probe_does_not_inherit_the_previous_endpoints_challenge(self) -> None:
        """Authorization evidence is per-probe, never carried forward.

        The probe cache is keyed by NAME and ``list_servers`` rehydrates these
        fields onto the row before a re-probe, so a server whose url was edited
        arrives carrying the OLD endpoint's verdict. A probe that only ever SETS
        the flags would keep reporting "Sign-in required" for an endpoint that
        never asked for one.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            auth_challenge=True,
            auth_grant_present=True,
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}  # a bare 401: no challenge this time
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is False
        assert result.auth_grant_present is None
        assert "authChallenge" not in result.to_dict()

    @pytest.mark.asyncio
    async def test_a_credential_bearing_url_never_reaches_the_log(self, caplog) -> None:
        """The grant lookup runs for ANY url a user typed, so it must not log one.

        A custom endpoint can carry a credential in its userinfo or query string.
        The probe's own failure path therefore names the server, never the url —
        this line lands in gateway.log, which is not a credential store.
        """
        from kiro_crew.mcp_discovery import _runtime_grant_present

        secret_url = "https://user:sup3r-secret@mcp.example.com/mcp?token=abcd1234"

        # ``None`` is what an unreadable cache home resolves to: ``grant_presence``
        # classifies the failed stat itself, so nothing raises out to this caller.
        with patch("kiro_crew.mcp_discovery.grant_observed", AsyncMock(return_value=None)):
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_discovery"):
                present = await _runtime_grant_present(secret_url, "higgsfield")

        # None, not False: the lookup could not answer, and the payload must not
        # report that as "no grant" -- that would name an action on no evidence.
        assert present is None
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "sup3r-secret" not in blob
        assert "abcd1234" not in blob
        assert "mcp.example.com" not in blob
        # The server name is what makes the line diagnosable at all.
        assert "higgsfield" in blob

    @pytest.mark.asyncio
    async def test_probe_remote_connection_error(self) -> None:
        """Connection failure sets error status."""
        server = McpServerInfo(name="remote", url="https://unreachable.example.com/mcp")

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=ConnectionError("refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_probe_dispatches_to_remote(self) -> None:
        """probe_server dispatches to _probe_remote for url-based servers."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            mock_remote.return_value = server
            result = await probe_server(server)

        mock_remote.assert_awaited_once_with(server)
        assert result is server

    @pytest.mark.asyncio
    async def test_probe_local_not_dispatched_to_remote(self) -> None:
        """probe_server does NOT dispatch to _probe_remote for command-based servers."""
        server = McpServerInfo(name="local", command="nonexistent-cmd-xyz")

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            result = await probe_server(server)

        mock_remote.assert_not_awaited()
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_directory_qualified_command_reports_no_search_path(self) -> None:
        """A directory-qualified command is looked up directly, not PATH-searched.

        ``shutil.which`` returns before it reads ``path=`` when the command
        carries a directory component, so it checks exactly the one location
        named. Reporting a declared search path for it would tell the reader it
        "searched N directories" that were never consulted -- the exact
        not-installed vs installed-elsewhere confusion this avoids, stated
        backwards. The error for such a command must be the bare
        ``command not found: <cmd>`` with no directory list.
        """
        abs_missing = os.path.join(os.sep, "opt", "vendor", "bin", "ghost-mcp")
        server = McpServerInfo(name="dirq", command=abs_missing)
        result = await probe_server(server)
        assert result.status == "error"
        assert f"command not found: {abs_missing}" in result.error
        assert "directories" not in result.error  # nothing was searched
        assert "empty PATH" not in result.error

    @pytest.mark.asyncio
    async def test_bare_command_still_reports_the_search_path(self) -> None:
        """The bare-command path is unchanged: it IS PATH-searched, so say so."""
        server = McpServerInfo(name="bare", command="ghost-mcp-xyz")
        result = await probe_server(server)
        assert result.status == "error"
        assert "command not found: ghost-mcp-xyz" in result.error
        assert "directories" in result.error  # PATH was searched, report it


class TestProbeRemoteTimeout:
    """Test that _probe_remote uses _get_probe_timeout() for HTTP timeout."""

    @pytest.mark.asyncio
    async def test_probe_remote_timeout_uses_config(self) -> None:
        """Remote probe uses _get_probe_timeout() for aiohttp timeout."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        with (
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
            patch("aiohttp.ClientSession") as mock_session_cls,
        ):
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 60
            mock_cls.load.return_value = mock_cfg

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(side_effect=asyncio.TimeoutError)
            mock_session_cls.return_value = mock_session

            result = await _probe_remote(server)

        assert result.status == "error"
        assert result.error == "timeout"
        timeout_used = mock_session_cls.call_args.kwargs.get("timeout")
        assert timeout_used is not None
        assert timeout_used.total == 60


class TestProbeHeaderReferenceExpansion:
    """The remote probe resolves ``${VAR}``/``${env:VAR}`` header references.

    A static header whose value carries a runtime reference is a documented
    form (docs/reference/kiro-cli/mcp/configuration.md) that kiro-cli expands
    at session runtime. If the probe sent the reference as literal text, a
    working configuration would render as Error / HTTP 401 with advice to
    delete the header. The probe resolves references through
    the gateway rewriter's declared-env expander — same regex, same
    credential-filtered source view, same "unresolved stays literal" rule.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @staticmethod
    def _session_returning(*responses: MagicMock) -> MagicMock:
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=list(responses))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @staticmethod
    def _resp(status: int, *, body: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.content_type = "application/json"
        resp.headers = {}
        if body is not None:
            resp.json = AsyncMock(return_value=body)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    @pytest.mark.asyncio
    async def test_an_ordinary_reference_resolves_and_the_probe_sends_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reference naming an ordinary variable is sent RESOLVED — and a 401
        against that resolved credential stays a real error, because a
        credential genuinely was supplied and rejected."""
        monkeypatch.setenv("KC_PROBE_TEST_TOKEN", "kc-9206-resolved-token")
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={
                "Authorization": "Bearer ${env:KC_PROBE_TEST_TOKEN}",
                "X-Api-Key": "${KC_PROBE_TEST_TOKEN}",
            },
        )

        mock_session = self._session_returning(self._resp(401))
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        sent = mock_session.post.call_args_list[0].kwargs["headers"]
        assert sent["Authorization"] == "Bearer kc-9206-resolved-token"
        assert sent["X-Api-Key"] == "kc-9206-resolved-token"
        assert result.status == "error"
        assert "401" in result.error

    @pytest.mark.asyncio
    async def test_a_credential_filtered_reference_stays_literal_and_is_not_a_rejected_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reference naming a credential-filtered variable is a MISS: the
        secret value must not ride out in a probe request, and the 401 that
        follows must not read as "your credential was rejected" — nothing was
        supplied, so the row reports the sign-in state instead of an error
        whose remediation advice would break a working configuration."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "kc-9206-filtered-secret")
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer ${env:AWS_SECRET_ACCESS_KEY}"},
        )

        mock_session = self._session_returning(self._resp(401))
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        sent = mock_session.post.call_args_list[0].kwargs["headers"]
        assert "kc-9206-filtered-secret" not in sent["Authorization"]
        assert "${" in sent["Authorization"]
        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_a_missing_variable_reference_stays_literal_and_reports_needs_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KC_PROBE_UNSET_VAR", raising=False)
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer ${KC_PROBE_UNSET_VAR}"},
        )

        mock_session = self._session_returning(self._resp(401))
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        sent = mock_session.post.call_args_list[0].kwargs["headers"]
        assert sent["Authorization"] == "Bearer ${KC_PROBE_UNSET_VAR}"
        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_an_error_echoing_the_resolved_secret_is_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """redact_mcp_error's layer-2 scrubber keys on EXACT header values, so
        it must be handed the values the probe actually sent. Handing it the
        unexpanded config map would leave the resolved secret matching nothing
        in the scrub set, in both serialized output (to_dict) and the probe
        cache (_cache_probe). The synthetic token is chosen to survive the
        layer-1 generic scanners, so this test isolates the layer-2 threading.
        """
        secret = "kc-9206-resolved-token"
        monkeypatch.setenv("KC_PROBE_TEST_TOKEN", secret)
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer ${env:KC_PROBE_TEST_TOKEN}"},
        )

        init_resp = self._resp(
            200,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"message": f"upstream rejected {secret} as expired"},
            },
        )
        mock_session = self._session_returning(init_resp)
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        serialized = result.to_dict()["error"]
        assert secret not in serialized
        assert MCP_REDACTED_HEADER_VALUE in serialized
        cached = probe_metadata("remote")
        assert cached is not None
        assert secret not in cached.error
        assert MCP_REDACTED_HEADER_VALUE in cached.error

    @pytest.mark.asyncio
    async def test_a_partially_expanded_header_still_scrubs_the_resolved_fragment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`${TOKEN}${MISSING}` expands to `<resolved>${MISSING}`: the FULL sent
        value and the Authorization suffix both carry the literal tail, so a
        server echoing only the resolved token would slip past a scrub set
        keyed on whole values. Each individually resolved placeholder value
        must be in the scrub set on its own."""
        secret = "kc-9206-resolved-token"
        monkeypatch.setenv("KC_PROBE_TEST_TOKEN", secret)
        monkeypatch.delenv("KC_PROBE_MISSING", raising=False)
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer ${env:KC_PROBE_TEST_TOKEN}${env:KC_PROBE_MISSING}"},
        )

        init_resp = self._resp(
            200,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"message": f"upstream rejected {secret} as expired"},
            },
        )
        mock_session = self._session_returning(init_resp)
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        serialized = result.to_dict()["error"]
        assert secret not in serialized
        assert MCP_REDACTED_HEADER_VALUE in serialized
        cached = probe_metadata("remote")
        assert cached is not None
        assert secret not in cached.error

    @pytest.mark.asyncio
    async def test_a_tiny_resolved_fragment_does_not_corrupt_ordinary_prose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved placeholder value below the credential minimum length
        must NOT join the scrub set as a bare substring: no boundary rule
        separates a one- or two-character value from prose words, and masking
        it would corrupt unrelated error text."""
        monkeypatch.setenv("KC_PROBE_TINY", "ab")
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"X-Key": "x-${env:KC_PROBE_TINY}-y"},
        )

        init_resp = self._resp(
            200,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                # "ab" appears both STANDALONE (a boundary-anchored pattern
                # would mask it — the case only the minimum-length skip
                # protects) and embedded in a longer word.
                "error": {"message": "got ab grade abnormal response"},
            },
        )
        mock_session = self._session_returning(init_resp)
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        serialized = result.to_dict()["error"]
        assert "got ab grade" in serialized
        assert "abnormal" in serialized

    def test_needs_authorization_ignores_an_unresolved_reference(self) -> None:
        """An Authorization value still carrying ``${VAR}`` is not a supplied
        credential: the premise behind "any auth key present means a credential
        was rejected" does not hold, so a 401 is an authenticate prompt."""
        from kiro_crew.mcp_discovery import _needs_authorization

        assert _needs_authorization(401, {}, {"Authorization": "Bearer ${TOKEN}"}) is True
        assert (
            _needs_authorization(
                403,
                {"WWW-Authenticate": "Bearer"},
                {"Authorization": "${env:TOKEN}"},
            )
            is True
        )
        # A resolved (reference-free) credential still suppresses needs_auth.
        assert _needs_authorization(401, {}, {"Authorization": "Bearer abc"}) is False
        # Only 401/403-with-challenge become needs_auth even when unresolved.
        assert _needs_authorization(500, {}, {"Authorization": "Bearer ${TOKEN}"}) is False


def _refusal(supported: list[str]) -> dict:
    """The -32602 reply a server gives an initialize offering a version it dropped."""
    data = {"supported": supported, "requested": MCP_CLIENT_PROTOCOL_VERSION}
    error = {"code": -32602, "message": "Unsupported protocol version", "data": data}
    return {"jsonrpc": "2.0", "id": 1, "error": error}


def _http_reply(status: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.content_type = "application/json"
    resp.headers = {}
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestProtocolVersionNegotiation:
    """Offer the newest revision; step down once when a server refuses it."""

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    def test_downgrade_picks_the_newest_supported_version(self) -> None:
        reply = _refusal(["2024-11-05", "2025-03-26"])
        assert downgrade_protocol_version(reply, MCP_CLIENT_PROTOCOL_VERSION) == "2025-03-26"

    def test_downgrade_only_steps_down_from_the_first_offer(self) -> None:
        assert downgrade_protocol_version(_refusal(["2024-11-05"]), "2025-03-26") is None

    def test_other_errors_do_not_downgrade(self) -> None:
        unknown = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        no_list = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}
        only_ours = _refusal([MCP_CLIENT_PROTOCOL_VERSION])
        only_newer = _refusal(["2099-01-01", "draft", "1.0"])
        success = {"jsonrpc": "2.0", "id": 1, "result": {}}
        replies = (unknown, no_list, only_ours, only_newer, success)
        for reply in replies:
            assert downgrade_protocol_version(reply, MCP_CLIENT_PROTOCOL_VERSION) is None

    def _session(self, replies: list[MagicMock]) -> MagicMock:
        session = MagicMock()
        session.post = MagicMock(side_effect=replies)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_http_probe_retries_once_with_the_supported_version(self) -> None:
        server = McpServerInfo(name="remote", url="https://example.com/mcp")
        ok = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        tools = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}
        replies = [_http_reply(200, _refusal(["2025-03-26", "2024-11-05"])), _http_reply(200, ok)]
        session = self._session([*replies, _http_reply(202), _http_reply(200, tools)])

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        calls = session.post.call_args_list
        offered = [c.kwargs["json"]["params"]["protocolVersion"] for c in calls[:2]]
        assert offered == [MCP_CLIENT_PROTOCOL_VERSION, "2025-03-26"]
        assert all("MCP-Protocol-Version" not in c.kwargs["headers"] for c in calls[:2])
        assert all(c.kwargs["headers"]["MCP-Protocol-Version"] == "2025-03-26" for c in calls[2:])

    @pytest.mark.asyncio
    async def test_http_probe_does_not_retry_an_unknown_error(self) -> None:
        server = McpServerInfo(name="remote", url="https://example.com/mcp")
        boom = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        session = self._session([_http_reply(200, boom)])

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session):
            result = await _probe_remote(server)

        assert (result.status, result.error) == ("error", "boom")
        assert session.post.call_count == 1


_REFUSED_ADDRESS = "URL host is a link-local or metadata address, or does not resolve"


def _ok_session() -> MagicMock:
    """A session that answers initialize, the notification and tools/list."""

    def _resp(status: int, body: dict | None = None) -> MagicMock:
        r = MagicMock()
        r.status = status
        r.content_type = "application/json"
        r.headers = {}
        if body is not None:
            r.json = AsyncMock(return_value=body)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    session = MagicMock()
    session.post = MagicMock(
        side_effect=[
            _resp(200, {"jsonrpc": "2.0", "id": 1, "result": {}}),
            _resp(202),
            _resp(200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}),
        ]
    )
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestProbeRemoteRejectsNonPublicUrls:
    """The remote probe SSRF-vets its url before it POSTs.

    ``server.url`` is attacker-reachable -- a sandboxed process can append an
    entry to ``~/.kiro/crew/mcp.json`` -- so a url naming the cloud metadata
    endpoint, or anything else link-local, must be refused and never contacted.
    Otherwise the gateway POSTs the handshake there and reflects the reply onto
    the dashboard row. Every other target must still probe: a public server on
    any port, and a loopback or private one (an app backend on ``127.0.0.1``, a
    LAN dev server), which kiro-cli itself connects to.

    The autouse ``_stub_remote_probe_ssrf_vet`` fixture in ``mcp_discovery_helpers``
    answers the vet's DNS seam from one public address, so a NAME is approved
    without a real lookup while an IP LITERAL still runs the real address checks.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
            "http://169.254.170.2/v2/credentials",  # container metadata (link-local)
            "http://[fd00:ec2::254]/latest/meta-data/",  # IPv6 cloud metadata (not link-local)
            "http://[fe80::1]/mcp",  # IPv6 link-local
            "http://[::ffff:169.254.169.254]/mcp",  # IPv4-mapped spelling
            "http://[2002:a9fe:a9fe::1]/mcp",  # 6to4 spelling
            "http://[fd00:ec2::254%0]/latest/meta-data/",  # scoped IPv6 metadata (zone id)
            "http://[fe80::1%0]/mcp",  # scoped IPv6 link-local
            "http://2852039166/mcp",  # decimal spelling the OS resolver accepts
            "http://0xa9fea9fe/mcp",  # hex spelling the OS resolver accepts
        ],
    )
    @pytest.mark.asyncio
    async def test_a_metadata_or_link_local_literal_is_refused_without_contact(
        self, url: str
    ) -> None:
        server = McpServerInfo(name="remote", url=url)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == _REFUSED_ADDRESS
        # A refused probe exposes no handshake details and lists no tools.
        assert result.sent_headers is None
        assert result.tools == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9100/mcp",  # an app backend's loopback MCP server
            "http://[::1]:3000/mcp",  # IPv6 loopback
            "http://0x7f000001:3000/mcp",  # alternate loopback encoding
            "http://10.0.0.5:8000/mcp",  # RFC1918 private
            "http://192.168.1.20/mcp",  # RFC1918 private
            "http://100.64.0.7:8443/mcp",  # shared address space (a tailnet)
        ],
    )
    @pytest.mark.asyncio
    async def test_a_loopback_or_private_server_is_still_probed(self, url: str) -> None:
        """No regression: a local or LAN server the operator configured -- or an
        app backend registered on loopback -- probes as it did before the vet."""
        server = McpServerInfo(name="local", url=url)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=_ok_session()):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search"]

    @pytest.mark.asyncio
    async def test_a_name_resolving_to_a_metadata_address_is_refused(self, monkeypatch) -> None:
        """DNS-rebinding guard: the vet checks EVERY resolved address, so a name
        that answers with a public AND a metadata address is refused. Drives the
        REAL vet with its resolver pointed at that answer."""
        from kiro_crew import link_unfurl as lu

        monkeypatch.setattr(
            lu, "_default_resolve", lambda _h, _p: ["93.184.216.34", "169.254.169.254"]
        )
        server = McpServerInfo(name="remote", url="https://rebind.example/mcp")

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == _REFUSED_ADDRESS

    @pytest.mark.parametrize("host", ["devbox.internal.example", "printer.local"])
    @pytest.mark.asyncio
    async def test_a_name_resolving_to_a_private_address_is_probed(
        self, monkeypatch, host: str
    ) -> None:
        """A LAN name (an mDNS ``.local`` one included) that answers with a
        private address is probed: only link-local and metadata answers refuse."""
        from kiro_crew import link_unfurl as lu

        monkeypatch.setattr(lu, "_default_resolve", lambda _h, _p: ["10.1.2.3"])
        server = McpServerInfo(name="lan", url=f"http://{host}:8000/mcp")

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=_ok_session()):
            result = await _probe_remote(server)

        assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_an_unresolvable_host_is_refused_without_contact(self, monkeypatch) -> None:
        """The vet fails closed on a host it could not resolve: no address was
        checked, so none may be dialed, and the row says why."""
        from kiro_crew import link_unfurl as lu

        def _nxdomain(_host: str, _port: int) -> list[str]:
            raise OSError("Name or service not known")

        monkeypatch.setattr(lu, "_default_resolve", _nxdomain)
        server = McpServerInfo(name="typo", url="https://mcp.exmaple.invalid/mcp")

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == _REFUSED_ADDRESS

    @pytest.mark.asyncio
    async def test_a_stalled_dns_lookup_is_bounded_by_the_probe_timeout(self, monkeypatch) -> None:
        """The vet's lookup runs inside the probe timeout, as aiohttp's own did: a
        resolver that never answers reports ``timeout`` and frees the probe's slot
        instead of holding it until the OS gives up."""
        import threading

        from kiro_crew import link_unfurl as lu
        from kiro_crew import mcp_discovery

        release = threading.Event()

        def _stalled_vet(_url: str, **_kw: object) -> lu.VettedUrl:
            # Bounded so a broken run cannot strand the worker thread; the
            # finally below releases it as soon as the probe has returned.
            release.wait(timeout=30)
            raise lu.UnfurlRejected("blocked_url")

        monkeypatch.setattr(lu, "vet_unfurl_url", _stalled_vet)
        monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 0.05)
        server = McpServerInfo(name="slow-dns", url="https://slow.example/mcp")
        try:
            with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
                result = await _probe_remote(server)
        finally:
            release.set()

        session_cls.assert_not_called()
        assert (result.status, result.error) == ("error", "timeout")

    @pytest.mark.asyncio
    async def test_a_url_carrying_credentials_is_refused_as_malformed(self) -> None:
        """The vet refuses userinfo (aiohttp would turn it into a Basic header);
        the row names that cause rather than an address one."""
        server = McpServerInfo(name="remote", url="https://user:pw@mcp.example.com/mcp")

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == "URL is malformed or carries credentials"

    @pytest.mark.asyncio
    async def test_the_vet_runs_before_a_secretless_server_is_contacted(self) -> None:
        """A plain (no ``secret://`` header) server still reaches the vet: the
        header pre-check passes, and the metadata url is refused before any
        POST -- so the fix does not depend on the header refusal firing first."""
        server = McpServerInfo(
            name="remote",
            url="http://169.254.169.254/latest/meta-data/",
            headers={"Authorization": "Bearer plain-token"},
        )

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == _REFUSED_ADDRESS

    @pytest.mark.asyncio
    async def test_a_public_url_still_connects(self) -> None:
        """No regression: a public remote server is vetted, pinned, and probed OK."""
        server = McpServerInfo(name="remote", url="https://mcp.example.com/mcp")

        init_resp = MagicMock()
        init_resp.status = 200
        init_resp.content_type = "application/json"
        init_resp.headers = {}
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

        notif_resp = MagicMock()
        notif_resp.status = 202
        notif_resp.__aenter__ = AsyncMock(return_value=notif_resp)
        notif_resp.__aexit__ = AsyncMock(return_value=False)

        tools_resp = MagicMock()
        tools_resp.status = 200
        tools_resp.content_type = "application/json"
        tools_resp.json = AsyncMock(
            return_value={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}
        )
        tools_resp.__aenter__ = AsyncMock(return_value=tools_resp)
        tools_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[init_resp, notif_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search"]

    @pytest.mark.parametrize("port", [8443, 8000, 3000])
    @pytest.mark.asyncio
    async def test_a_public_url_on_a_nonstandard_port_connects(self, port: int) -> None:
        """Regression: MCP servers commonly use non-web ports. A PUBLIC host on
        :8443 / :8000 / :3000 must probe, not be refused by the 80/443 allowlist
        -- the probe lifts it with ``allow_any_port`` while the address check
        (what actually gates SSRF) is unchanged."""
        server = McpServerInfo(name="remote", url=f"https://mcp.example.com:{port}/mcp")

        def _resp(status: int, body: dict | None = None) -> MagicMock:
            r = MagicMock()
            r.status = status
            r.content_type = "application/json"
            r.headers = {}
            if body is not None:
                r.json = AsyncMock(return_value=body)
            r.__aenter__ = AsyncMock(return_value=r)
            r.__aexit__ = AsyncMock(return_value=False)
            return r

        mock_session = MagicMock()
        mock_session.post = MagicMock(
            side_effect=[
                _resp(200, {"jsonrpc": "2.0", "id": 1, "result": {}}),
                _resp(202),
                _resp(200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}),
            ]
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search"]

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254:8443/latest/meta-data/",  # metadata, nonstandard port
            "http://[fd00:ec2::254]:8000/latest/meta-data/",  # IPv6 metadata, nonstandard port
            "http://[fe80::1]:3000/mcp",  # IPv6 link-local, nonstandard port
        ],
    )
    @pytest.mark.asyncio
    async def test_a_metadata_url_on_a_nonstandard_port_is_still_refused(self, url: str) -> None:
        """Lifting the port allowlist must NOT weaken the address check: a
        link-local or metadata target is refused on any port."""
        server = McpServerInfo(name="remote", url=url)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as session_cls:
            result = await _probe_remote(server)

        session_cls.assert_not_called()
        assert result.status == "error"
        assert result.error == _REFUSED_ADDRESS


async def _start_loopback_stub(handler) -> tuple[web.AppRunner, int]:
    """One real HTTP stub on ``127.0.0.1``, on a port the OS picks."""
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, runner.addresses[0][1]


class TestProbeRemoteAgainstLoopbackStubs:
    """The probe end to end, through the REAL vet and the REAL pinned connector.

    The module fixture stubs ``pinned_connector`` out for the mocked-session
    cases; these restore it, so the socket really is opened by the connector the
    vet's answer pins. Only ``127.0.0.1`` stubs are dialed.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_an_app_backend_on_loopback_probes_ok(self, monkeypatch) -> None:
        """An app's MCP backend is registered at ``http://127.0.0.1:<port>/mcp``
        (``apps/bridges_mcp._resolve_live_mcp_url``); the probe must list its tools."""
        from kiro_crew import link_unfurl as lu

        monkeypatch.setattr(lu, "pinned_connector", _real_pinned_connector)

        async def _mcp(request: web.Request) -> web.Response:
            body = await request.json()
            if body.get("method") == "initialize":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                    }
                )
            if body.get("method") == "tools/list":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"tools": [{"name": "app_tool"}]},
                    }
                )
            return web.Response(status=202)

        runner, port = await _start_loopback_stub(_mcp)
        try:
            result = await _probe_remote(
                McpServerInfo(name="myapp:backend", url=f"http://127.0.0.1:{port}/mcp")
            )
        finally:
            await runner.cleanup()

        assert result.status == "ok", result.error
        assert result.tools == ["app_tool"]

    @pytest.mark.asyncio
    async def test_a_redirect_is_reported_and_its_target_is_never_contacted(
        self, monkeypatch
    ) -> None:
        """A vetted server's redirect is never followed. aiohttp follows a 307/308
        by default, re-sending the POST body, and dials an IP-literal ``Location``
        without asking the pinned resolver, so a followed redirect would reach an
        address the vet never checked. The first stub answers every POST with a
        307 to the second, which must never be contacted."""
        from kiro_crew import link_unfurl as lu

        monkeypatch.setattr(lu, "pinned_connector", _real_pinned_connector)
        reached: list[str] = []

        async def _target(request: web.Request) -> web.Response:
            reached.append(request.path)
            return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

        target_runner, target_port = await _start_loopback_stub(_target)

        async def _redirect(request: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(f"http://127.0.0.1:{target_port}/mcp")

        redirect_runner, redirect_port = await _start_loopback_stub(_redirect)
        try:
            result = await _probe_remote(
                McpServerInfo(name="redirector", url=f"http://127.0.0.1:{redirect_port}/mcp")
            )
        finally:
            await redirect_runner.cleanup()
            await target_runner.cleanup()

        assert reached == []
        assert result.status == "error"
        assert result.error == f"HTTP 307, redirects to http://127.0.0.1:{target_port}/mcp"
        assert result.tools == []
