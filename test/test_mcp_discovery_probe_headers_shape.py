"""MCP discovery: a probe of a remote server whose ``headers`` is not a map.

``headers`` maps header names to values. A hand-edited entry can hold a string
such as ``"Authorization: Bearer ..."`` or a list there, and the request reads it
by name, so the probe failed with Python's attribute message. It now refuses
before any request with one error in the config's own terms, and an empty value
probes like an entry with no headers, as an empty stdio ``env`` does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import McpServerInfo, probe_server

#: The words the person reads: the field, and the shape it must have.
HEADERS_NOT_A_MAP = "headers must be an object of header names to values"

_URL = "https://mcp.example.com/mcp"


@pytest.fixture(autouse=True)
def _empty_probe_cache():
    # A refusal is cached by name, like any probe verdict: keep it out of other tests.
    _clear_cache()
    yield
    _clear_cache()


class _Stop(Exception):
    """Raised by the request stub, so a test can see what the probe sent."""


def _session() -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(side_effect=_Stop)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


async def _sent_headers(headers: object) -> dict:
    """The headers the probe of an entry declaring *headers* put on its request."""
    session = _session()
    server = McpServerInfo(name="remote", url=_URL, headers=headers)  # type: ignore[arg-type]
    with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session):
        result = await probe_server(server)
    session.post.assert_called_once()
    assert HEADERS_NOT_A_MAP not in result.error
    return session.post.call_args.kwargs["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param("Authorization: Bearer abc", id="string"),
        pytest.param(["Authorization: Bearer abc"], id="list"),
        pytest.param(5, id="number"),
    ],
)
async def test_non_map_headers_are_refused_in_their_own_terms_before_any_request(
    headers: object,
) -> None:
    server = McpServerInfo(name="bad-headers", url=_URL, headers=headers)  # type: ignore[arg-type]
    with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession") as client:
        result = await probe_server(server)
    assert result.status == "error"
    assert result.error.startswith(HEADERS_NOT_A_MAP)
    assert "attribute" not in result.error
    # What the dashboard row and doctor show.
    assert result.to_dict()["error"].startswith(HEADERS_NOT_A_MAP)
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="empty-list"),
        pytest.param("", id="empty-string"),
        pytest.param(None, id="null"),
    ],
)
async def test_an_empty_headers_value_probes_like_no_headers(headers: object) -> None:
    assert await _sent_headers(headers) == await _sent_headers({})


@pytest.mark.asyncio
async def test_map_headers_still_reach_the_request() -> None:
    sent = await _sent_headers({"Authorization": "Bearer abc"})
    assert sent["Authorization"] == "Bearer abc"
