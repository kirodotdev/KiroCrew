"""MCP discovery: a remote server's redirect names its target on the row.

The probe never follows a redirect, but kiro-cli does, so a server whose url needs a
trailing slash works in a session while its row reports the redirect. The row says
where the redirect points (scheme, host, port and path only), resolved against the
configured url, and never contacts that target.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

# Bound at import, before the autouse fixture swaps the module attribute out, so the
# live loopback case can put the real connector back.
from kiro_crew.link_unfurl import pinned_connector as _real_pinned_connector
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache, _probe_remote, _redirect_error

_BASE = "https://mcp.example.com/mcp"


class TestRedirectError:
    @pytest.mark.parametrize(
        "status,location,expected",
        [
            pytest.param(
                307,
                "/mcp/",
                "HTTP 307, redirects to https://mcp.example.com/mcp/",
                id="a-relative-trailing-slash-redirect-names-the-url-to-configure",
            ),
            pytest.param(
                308,
                "http://other.example:8000/v1/mcp",
                "HTTP 308, redirects to http://other.example:8000/v1/mcp",
                id="an-absolute-target-keeps-its-host-and-port",
            ),
            pytest.param(
                301,
                "http://[::1]:3000/mcp/",
                "HTTP 301, redirects to http://[::1]:3000/mcp/",
                id="an-ipv6-host-keeps-its-brackets",
            ),
        ],
    )
    def test_the_redirect_names_the_url_to_configure(self, status, location, expected) -> None:
        assert _redirect_error(status, {"Location": location}, _BASE) == expected

    def test_userinfo_query_and_fragment_never_reach_the_row(self) -> None:
        """The target is resolved against the configured url, so a credential in its
        userinfo or query, or one the server echoed into the Location, would be shown."""
        error = _redirect_error(
            307,
            {"Location": "https://user:s3cret@mcp.example.com/mcp/?api_key=s3cret#frag"},
            "https://user:s3cret@mcp.example.com/mcp?api_key=s3cret",
        )
        assert error == "HTTP 307, redirects to https://mcp.example.com/mcp/"
        assert "s3cret" not in error

    @pytest.mark.parametrize(
        ("status", "headers"),
        [
            (307, {}),
            (307, {"Location": "   "}),
            (302, {"Location": "javascript:alert(1)"}),
            (302, {"Location": "http://mcp.example.com:notaport/mcp"}),
            (307, {"Location": "https://mcp.example.com/" + "a" * 300}),
            (500, {"Location": "/mcp/"}),
            (404, {}),
        ],
        ids=["no-location", "blank", "not-http", "bad-port", "overlong", "not-3xx", "plain-404"],
    )
    def test_anything_else_reports_the_status_alone(self, status, headers) -> None:
        assert _redirect_error(status, headers, _BASE) == f"HTTP {status}"


async def _start_loopback_stub(handler) -> tuple[web.AppRunner, int]:
    """One real HTTP stub on ``127.0.0.1``, on a port the OS picks."""
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, runner.addresses[0][1]


class TestProbeRemoteRedirectAgainstALoopbackStub:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_a_trailing_slash_redirect_names_its_target_and_is_not_followed(
        self, monkeypatch
    ) -> None:
        """Starlette answers ``POST /mcp`` with a 307 to ``/mcp/``. The probe, through
        the real vet and pinned connector, sends exactly one request and names the
        slashed url on the row."""
        from kiro_crew import link_unfurl as lu

        monkeypatch.setattr(lu, "pinned_connector", _real_pinned_connector)
        reached: list[str] = []

        async def _starlette_like(request: web.Request) -> web.Response:
            reached.append(request.path)
            if request.path == "/mcp":
                raise web.HTTPTemporaryRedirect("/mcp/")
            return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

        runner, port = await _start_loopback_stub(_starlette_like)
        try:
            result = await _probe_remote(
                McpServerInfo(name="fastmcp", url=f"http://127.0.0.1:{port}/mcp")
            )
        finally:
            await runner.cleanup()

        assert reached == ["/mcp"]
        assert result.status == "error"
        assert result.error == f"HTTP 307, redirects to http://127.0.0.1:{port}/mcp/"
        assert result.tools == []
