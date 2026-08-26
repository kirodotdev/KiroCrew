"""Tests for v1c-B -- temporary/incognito flags persisted on the session.

The flags were previously in-memory only (a bounded LRU), so they were lost
on gateway restart. v1c-B write-throughs them to the canonical ``SessionMap``
and re-hydrates the in-memory caches on session load, while preserving the
existing in-memory behavior for contexts without a ``SessionMap`` (tests,
the golden harness).
"""

from __future__ import annotations

import pytest

from kiro_crew.session_map import SessionMap
from kiro_crew.slack import handler as h


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    # Isolate the module-global LRUs between tests.
    h._thread_temporary.clear()
    h._thread_incognito.clear()
    yield tmp_path
    h._thread_temporary.clear()
    h._thread_incognito.clear()


class _Sessions:
    """Minimal SessionManager stand-in exposing a real SessionMap."""

    def __init__(self, sm: SessionMap) -> None:
        self._session_map = sm


class TestConvFlagPersistence:
    def test_conv_state_map_none_without_session_map(self, patched):
        assert h._conv_state_map(object()) is None  # test double -> in-memory only

    def test_hydrate_restores_temporary_from_session_map(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        assert h.is_thread_temporary("slack:1.2") is False  # not yet hydrated
        h._hydrate_conv_flags(_Sessions(sm), "slack:1.2")
        assert h.is_thread_temporary("slack:1.2") is True

    def test_hydrate_restores_incognito_from_session_map(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:9.9", "incognito", True)
        h._hydrate_conv_flags(_Sessions(sm), "slack:9.9")
        assert h.is_thread_incognito("slack:9.9") is True

    def test_hydrate_noop_without_session_map(self, patched):
        # No _session_map -> no crash, no hydration (in-memory stays empty).
        h._hydrate_conv_flags(object(), "slack:1.2")
        assert h.is_thread_temporary("slack:1.2") is False

    def test_flag_survives_fresh_session_map_instance(self, patched):
        # Write via one SessionMap, read via a fresh instance == restart.
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        h._hydrate_conv_flags(_Sessions(SessionMap()), "slack:1.2")
        assert h.is_thread_temporary("slack:1.2") is True
