"""The L0 probe's HTTP client accepts a large response header line.

Kept out of ``test_connections_l0_probe.py`` because that file forbids building a
real HTTP client. This one builds the real client against a loopback server, so
the header limit under test is aiohttp's own parser, not a fake. Still offline.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.connections import l0_probe

# Mixpanel's issuer sends a ~12 KB Content-Security-Policy; aiohttp's default
# header line limit is 8190 bytes.
LARGE_HEADER_VALUE = "default-src 'self' " + "https://a.example.com " * 540


async def _metadata(_request: web.Request) -> web.Response:
    return web.json_response(
        {"issuer": "https://auth.example.com"},
        headers={"Content-Security-Policy": LARGE_HEADER_VALUE},
    )


@pytest.mark.asyncio
async def test_a_large_response_header_does_not_hide_the_metadata_document():
    assert 8190 < len(LARGE_HEADER_VALUE) < l0_probe._MAX_HEADER_FIELD_BYTES
    app = web.Application()
    app.router.add_get("/.well-known/oauth-authorization-server", _metadata)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/.well-known/oauth-authorization-server"))
        async with l0_probe.client_session() as session:
            document = await l0_probe._get_json(session, url, 10.0)
    assert document == {"issuer": "https://auth.example.com"}
