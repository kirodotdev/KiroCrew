"""Shared fixtures and builders for the per-app WebSocket event scope suites.

``test_ws_event_scoping.py`` and its ``test_ws_event_scoping_<topic>.py`` siblings import these
by name: the two autouse resets of ``ws_event_scope``'s module caches, and the slot, state and
declaration builders the tests share.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Clear ws_event_scope module-level caches between tests.

    The exposeToApps TTL cache and the SEL audit dedup cache persist across
    test invocations at module scope; without an autouse fixture, a stale
    entry from an earlier test can suppress the behaviour under test in a
    later one (especially the cross-app tests that patch ``get_app_manifest``).
    """
    from kiro_crew.dashboard import ws_event_scope as _wes

    _wes._exposeto_cache.clear()
    _wes._sel_last_audit.clear()
    yield
    _wes._exposeto_cache.clear()
    _wes._sel_last_audit.clear()


def _make_slot(*, owner_app: str = "", origin: str = SlotOrigin.USER, key: str = "s1") -> MagicMock:
    slot = MagicMock()
    slot._app = owner_app
    slot._origin = origin
    slot.key = key
    return slot


def _make_state(slots: dict[str, Any] | None = None) -> MagicMock:
    state = MagicMock()
    state._slots = slots or {}
    return state


def _allowed(*decls: str) -> frozenset[str]:
    return build_allowed_event_set(list(decls))


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the test_ws_event_scoping suites' autouse pin; none of them calls monkeypatch.undo()
def _clear_ws_scope_module_caches(monkeypatch):
    """Isolate the module-level caches between tests.

    ``ws_event_scope`` keeps process-wide dicts (declaration refresh, exposeToApps,
    SEL dedup) keyed by app NAME. Tests share fixture app names, so one test
    seeding a narrowed declaration for ``mochi-pet`` would otherwise silently
    narrow every later test that uses the same name.

    Also pins ``is_app_enabled`` to True as the DEFAULT world, because that is what
    production looks like for any app able to reach the gate: a token is only
    minted for an installed app, so ``installed.json`` has an enabled entry. The
    suite's app names are synthetic and absent from the real ``installed.json``,
    which without this would read as REVOKED and withhold even the own-slot
    default. Tests that exercise revocation override this explicitly (patch it
    False, or seed ``_declared_cache`` with a disabled entry).
    """
    from kiro_crew.dashboard import ws_event_scope as mod

    monkeypatch.setattr(mod, "is_app_enabled", lambda _name: True)
    for cache in (mod._declared_cache, mod._exposeto_cache, mod._sel_last_audit):
        cache.clear()
    for pending in (mod._declared_refreshing, mod._exposeto_refreshing):
        pending.clear()
    yield
    for cache in (mod._declared_cache, mod._exposeto_cache, mod._sel_last_audit):
        cache.clear()
    for pending in (mod._declared_refreshing, mod._exposeto_refreshing):
        pending.clear()
