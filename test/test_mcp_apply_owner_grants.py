"""``POST /api/mcp/apply`` forgets an uninstalled owner server once the MCP lock is released.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _read_global, _request, _write_global

from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers import mcp as mcp_mod


@pytest.mark.asyncio
@pytest.mark.parametrize("uninstall_first", [True, False], ids=["reached", "swept"])
async def test_an_uninstall_is_forgotten_after_the_lock_when_another_change_fails(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, uninstall_first: bool
) -> None:
    """The uninstall the loop reached before the failing change, and the one after it
    that only the crash-window sweep removed, are both forgotten, never under the lock:
    forgetting can wait on a revocation endpoint."""
    _write_global(sandbox, {"gone": {"command": "y"}, "a": {"command": "x"}})
    held: list[bool] = []

    class _Lock:
        async def __aenter__(self) -> None:
            held.append(True)

        async def __aexit__(self, *a: object) -> None:
            held.pop()

    monkeypatch.setattr(mcp_mod, "_get_mcp_lock", _Lock)
    monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", contextlib.nullcontext)
    monkeypatch.setattr(
        _shared, "_capability_manager", lambda: MagicMock(**{"available.return_value": False})
    )

    def _boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mcp_mod, "_set_kirocrew_entry", _boom)
    under_lock: list[bool] = []
    forget = AsyncMock(side_effect=lambda names: under_lock.append(bool(held)))
    monkeypatch.setattr(mcp_mod, "_forget_owner_servers", forget)

    changes = [{"name": "gone", "uninstall": True}, {"name": "a", "kirocrew": True}]
    with pytest.raises(OSError):
        await mcp_mod.api_mcp_apply(
            _request({"changes": changes if uninstall_first else changes[::-1]})
        )

    assert "gone" not in _read_global(sandbox)
    forget.assert_awaited_once_with(["gone"])
    assert under_lock == [False]
