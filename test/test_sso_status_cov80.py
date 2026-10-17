"""Coverage for ``kiro_crew.sso_status`` — the OSS no-op SSO stubs.

The public distribution ships inert stubs so dashboard/Slack callers keep
importing and awaiting the same symbols. These tests pin the stub CONTRACT
(``available`` is False, the display line is empty and never leaks the prefix),
because a companion package swapping in a real provider must not change the
shape these callers depend on.
"""

from __future__ import annotations

import inspect

import pytest

from kiro_crew import sso_status as mod

pytestmark = pytest.mark.asyncio


async def test_sso_status_reports_unavailable() -> None:
    assert mod.sso_status() == {"available": False}


async def test_sso_status_async_matches_the_sync_stub() -> None:
    assert inspect.iscoroutinefunction(mod.sso_status_async)
    assert await mod.sso_status_async() == mod.sso_status()


async def test_status_line_is_empty_and_ignores_the_prefix() -> None:
    """OSS shows no SSO line at all, so the prefix must not appear in output —
    a caller concatenating the result gets nothing, not a dangling label."""
    assert await mod.get_sso_status_line() == ""
    assert await mod.get_sso_status_line(prefix="*NONSENSE-PFX:*") == ""
