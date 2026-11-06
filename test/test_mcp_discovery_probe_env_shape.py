"""MCP discovery: a probe of a server whose ``env`` is not a map.

``env`` maps variable names to values. A hand-edited entry can hold a list or a
string there, and the spawn reads it by key, so the probe failed with Python's
indexing message. It now refuses before any spawn with one error in the
config's own terms, and does not start the server without its variables.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import McpServerInfo, probe_all, probe_server

#: The words the person reads: the field, and the shape it must have.
ENV_NOT_A_MAP = "env must be an object of variable names to values"


class _NoSpawn(Exception):
    """Raised by the spawn stub, so a test can tell the probe reached it."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        pytest.param(["API_KEY=x"], id="list"),
        pytest.param("API_KEY=x", id="string"),
        pytest.param(5, id="number"),
    ],
)
async def test_a_non_map_env_is_refused_in_its_own_terms_before_any_spawn(env: object) -> None:
    server = McpServerInfo(name="bad-env", command="echo", env=env)  # type: ignore[arg-type]
    with (
        patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", side_effect=_NoSpawn
        ) as spawn,
        patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
    ):
        result = await probe_server(server)
    assert result.status == "error"
    assert result.error.startswith(ENV_NOT_A_MAP)
    assert "indices" not in result.error
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_a_scalar_env_does_not_fail_the_whole_probe_response() -> None:
    # After each probe, ``probe_all`` explains env keys the sandbox dropped. On a
    # scalar env that step iterated the number and raised outside the per-server
    # error handling, so the response failed for every server, not just this one.
    bad = McpServerInfo(name="bad-env", command="echo", env=5)  # type: ignore[arg-type]
    good = McpServerInfo(name="no-command", command="")
    with (
        patch("kiro_crew.mcp_discovery.list_servers", return_value=[bad, good]),
        patch("kiro_crew.mcp_discovery._spawn_excluded", return_value=frozenset()),
        patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", side_effect=_NoSpawn
        ) as spawn,
    ):
        rows = await probe_all()
    by_name = {r.name: r for r in rows}
    assert by_name["bad-env"].error.startswith(ENV_NOT_A_MAP)
    assert by_name["no-command"].error == "no command"
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"API_KEY": "x"}, id="map"),
        # Empty non-maps declare no variables and probe on main: they must keep doing so.
        pytest.param([], id="empty-list"),
        pytest.param("", id="empty-string"),
        pytest.param(None, id="absent"),
    ],
)
async def test_an_env_that_declares_nothing_wrong_still_reaches_the_spawn(env: object) -> None:
    server = McpServerInfo(name="good-env", command="echo", env=env)  # type: ignore[arg-type]
    with (
        patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", side_effect=_NoSpawn
        ) as spawn,
        patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
    ):
        result = await probe_server(server)
    spawn.assert_called_once()
    assert ENV_NOT_A_MAP not in result.error
