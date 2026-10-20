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
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
)
from stray_line_helpers import STRAY_LINES

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
        # Verify the configured timeout was actually used
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
