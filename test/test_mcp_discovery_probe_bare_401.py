"""MCP discovery: a bare ``401`` that the server's well-known metadata explains.

An OAuth server may answer the probe's tokenless ``initialize`` with a ``401``
and no ``WWW-Authenticate`` header (a gateway in front of it can rename the
header). The probe then reads the protected-resource metadata at its well-known
path, and only a document covering this server marks the row as needing a
sign-in. The metadata fetch (``mcp_oauth_discovery._fetch_json``) is replaced by a
table, so nothing leaves the process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _probe_cache,
    _probe_remote,
    advertises_protected_resource,
)

_URL = "https://mcp.example.test/mcp"
_PATH_DOC = "https://mcp.example.test/.well-known/oauth-protected-resource/mcp"
_ROOT_DOC = "https://mcp.example.test/.well-known/oauth-protected-resource"
_METADATA = {"resource": _URL, "authorization_servers": ["https://auth.example.test/"]}


def _documents(table: dict[str, object]) -> tuple[AsyncMock, list[str]]:
    """A ``_fetch_json`` stand-in serving *table*; any other url fails like a 404."""
    asked: list[str] = []

    async def _fetch(url: str) -> object:
        asked.append(url)
        if url not in table:
            raise ValueError(f"{url} returned HTTP 404, expected 200")
        return table[url]

    return AsyncMock(side_effect=_fetch), asked


async def _probe_bare_401(table: dict[str, object], challenge: str | None = None):
    resp = MagicMock()
    resp.status = 401
    resp.headers = {"WWW-Authenticate": challenge} if challenge else {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    fetch, asked = _documents(table)
    with (
        patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session),
        patch("kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=False)),
        patch("kiro_crew.mcp_oauth_discovery._fetch_json", fetch),
    ):
        row = await _probe_remote(McpServerInfo(name="remote", url=_URL))
    return row, asked


class TestBareUnauthorized:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_path_inserted_metadata_marks_the_row_as_needing_a_sign_in(self) -> None:
        row, asked = await _probe_bare_401({_PATH_DOC: _METADATA})

        assert row.status == "needs_auth"
        assert row.auth_challenge is True
        assert row.auth_grant_present is False
        assert row.to_dict()["authChallenge"] is True
        assert asked == [_PATH_DOC]

    @pytest.mark.asyncio
    async def test_root_metadata_is_the_fallback(self) -> None:
        row, asked = await _probe_bare_401({_ROOT_DOC: _METADATA})

        assert row.auth_challenge is True
        assert asked == [_PATH_DOC, _ROOT_DOC]

    @pytest.mark.asyncio
    async def test_metadata_for_another_server_is_not_evidence(self) -> None:
        other = {**_METADATA, "resource": "https://other.example.test/mcp"}
        row, _ = await _probe_bare_401({_PATH_DOC: other, _ROOT_DOC: other})

        assert row.status == "needs_auth"
        assert row.auth_challenge is False
        assert "authChallenge" not in row.to_dict()

    @pytest.mark.asyncio
    async def test_no_metadata_keeps_the_row_unverified(self) -> None:
        row, asked = await _probe_bare_401({})

        assert row.status == "needs_auth"
        assert row.auth_challenge is False
        assert asked == [_PATH_DOC, _ROOT_DOC]

    @pytest.mark.asyncio
    async def test_a_header_challenge_needs_no_lookup(self) -> None:
        row, asked = await _probe_bare_401({}, challenge='Bearer scope="openid"')

        assert row.auth_challenge is True
        assert asked == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {"resource": _URL},
        {"resource": _URL, "authorization_servers": []},
        {"resource": _URL, "authorization_servers": ["http://auth.example.test/"]},
        {"resource": "http://mcp.example.test/mcp", "authorization_servers": ["https://a.test/"]},
    ],
)
async def test_anything_short_of_metadata_for_this_server_is_not_evidence(document) -> None:
    fetch, _ = _documents({_PATH_DOC: document, _ROOT_DOC: document})
    with patch("kiro_crew.mcp_oauth_discovery._fetch_json", fetch):
        assert await advertises_protected_resource(_URL) is False
