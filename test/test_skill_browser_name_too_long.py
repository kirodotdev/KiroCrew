"""Pins the name-too-long refusal on the skill browser's tree and file routes.

``_resolve_skill_root`` joins the URL name onto a skills root and stats it. A
POSIX host refuses a too-long component with ``ENAMETOOLONG``, which
``Path.is_dir()`` re-raises, so ``/-/tree`` and ``/-/file`` answered an uncoded
500. They now answer the coded 400 ``GET /api/skills/{name}`` already returns, and
leave every other ``OSError`` as it was.
"""

from __future__ import annotations

import errno
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from skill_browser_helpers import (  # noqa: F401 -- the fixture is requested by name
    _make_app,
    fake_home_fixture,
)

import kiro_crew.dashboard.handlers.prompts_skill_catalog as catalog_mod

ROUTES = (
    ("/-/tree", "api_skill_tree"),
    ("/-/file?path=SKILL.md", "api_skill_file"),
)


def _state() -> MagicMock:
    return MagicMock(_slots={}, context_builder=None)


def _raise(exc: OSError):
    def _resolve(*_a, **_k):
        raise exc

    return _resolve


@pytest.fixture
def sel(monkeypatch) -> MagicMock:
    m = MagicMock()
    monkeypatch.setattr(catalog_mod, "_sel", lambda: m)
    return m


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, tool_name", ROUTES)
@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.ENAMETOOLONG, "File name too long"),
        # CPython files a Windows too-long path under ENOENT; only winerror says so.
        pytest.param(
            type("WinErr", (OSError,), {"winerror": 206})(errno.ENOENT, "winerror 206"),
            id="winerror-206",
        ),
    ],
)
async def test_a_too_long_name_is_a_coded_400(fake_home, monkeypatch, sel, suffix, tool_name, exc):
    monkeypatch.setattr(catalog_mod, "_resolve_skill_root", _raise(exc))
    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.get(f"/api/skills/{'a' * 300}{suffix}")
        assert resp.status == 400
        assert (await resp.json())["code"] == "name_too_long"
    (call,) = sel.log_tool_invocation.call_args_list
    assert call.kwargs["tool_name"] == tool_name
    assert call.kwargs["outcome"] == "rejected"
    assert call.kwargs["metadata"]["reason"] == "name_too_long"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, _tool_name", ROUTES)
async def test_any_other_oserror_is_left_alone(fake_home, monkeypatch, sel, suffix, _tool_name):
    monkeypatch.setattr(
        catalog_mod, "_resolve_skill_root", _raise(OSError(errno.EACCES, "Permission denied"))
    )
    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.get(f"/api/skills/demo{suffix}")
        assert resp.status == 500
    sel.log_tool_invocation.assert_not_called()


def _is_dir_raises_on_a_long_name(home) -> bool:
    try:
        (home / ("a" * 300)).is_dir()
    except OSError:
        return True
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, _tool_name", ROUTES)
@pytest.mark.parametrize("prefix", ["", "kiro-user/"])
async def test_the_real_resolver_refuses_a_too_long_name(
    fake_home, sel, suffix, _tool_name, prefix
):
    """No fake: the resolver's own ``is_dir()`` raises on a 300-byte component."""
    if not _is_dir_raises_on_a_long_name(fake_home):
        # Windows reports a missing path, and a pathlib that swallows every
        # OSError in is_dir() never reaches the handler with one.
        pytest.skip("this host's Path.is_dir() does not raise on a too-long name")
    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.get(f"/api/skills/{prefix}{'a' * 300}{suffix}")
        assert resp.status == 400
        assert (await resp.json())["code"] == "name_too_long"
