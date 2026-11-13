"""``mcp_gateway.keep_alive_servers``: a named server's idle shared backend is kept.

A server that holds its OAuth sign-in only in memory signs in again each time its
backend is drained. Naming it keeps its idle backend for the daemon's run; every
other backend keeps the idle drain, and the default keeps nothing.
"""

from __future__ import annotations

import time

import pytest
from test_mcp_gateway_bluegreen import _make_mock_backend, _make_pool_key

from kiro_crew.config.section_builders import _build_mcp_gateway_config
from kiro_crew.mcp_gateway.daemon.cli import _build_argparser
from kiro_crew.mcp_gateway.pool import BackendPool


async def _idle_pool(*servers: str) -> BackendPool:
    pool = BackendPool(max_backends=10)
    for name in servers:
        key = _make_pool_key(name)
        backend = _make_mock_backend(key)
        backend.last_used_at = time.monotonic() - 3600
        await pool.add(key, backend)
    return pool


@pytest.mark.asyncio
async def test_named_server_survives_the_idle_sweep_and_others_drain() -> None:
    pool = await _idle_pool("oauth-server", "other")
    evicted = await pool.evict_idle(300, keep_alive_servers=frozenset({"oauth-server"}))
    assert evicted == 1
    assert len(pool) == 1


@pytest.mark.asyncio
async def test_default_keeps_nothing_alive() -> None:
    pool = await _idle_pool("oauth-server", "other")
    assert await pool.evict_idle(300) == 2
    assert len(pool) == 0


@pytest.mark.asyncio
async def test_credential_drain_still_reclaims_a_kept_backend() -> None:
    pool = await _idle_pool("oauth-server")
    # The credential-rotation drain does not pass the keep-alive set.
    assert await pool.evict_idle(0.0, include_pinned=True) == 1


def test_daemon_flag_is_repeatable_and_empty_by_default() -> None:
    p = _build_argparser()
    assert p.parse_args([]).keep_alive_servers == []
    args = p.parse_args(["--keep-alive-server", "a", "--keep-alive-server", "b"])
    assert args.keep_alive_servers == ["a", "b"]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, []),
        (["oauth-server", " ", 3, " other "], ["oauth-server", "other"]),
        ("oauth-server", []),
        ({"oauth-server": True}, []),
    ],
    ids=["absent", "list", "string", "object"],
)
def test_config_reads_only_a_list_of_names(stored, expected) -> None:
    data = {} if stored is None else {"keep_alive_servers": stored}
    assert _build_mcp_gateway_config(data).keep_alive_servers == expected
