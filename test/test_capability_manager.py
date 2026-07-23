"""Tests for the ``_capability_manager()`` seam accessor.

The dashboard resolves the edition's external capability manager through the
platform context, failing closed to an unavailable ``DefaultCapabilityManager``
so ``/api/capability/*`` degrade to 503 rather than crashing.
"""

from __future__ import annotations

import kiro_crew.platform.context as platform_context
from kiro_crew.dashboard.handlers import agents as agents_handler
from kiro_crew.platform.defaults import DefaultCapabilityManager


def test_default_manager_is_unavailable():
    """The public Default reports unavailable so handlers return 503."""
    assert DefaultCapabilityManager().available() is False


def test_capability_manager_reads_context(monkeypatch):
    """``_capability_manager()`` returns the context-provided manager."""

    sentinel = DefaultCapabilityManager()

    class _Ctx:
        capability_manager = sentinel

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    assert agents_handler._capability_manager() is sentinel


def test_capability_manager_fails_closed(monkeypatch):
    """A context-lookup failure falls back to an unavailable Default (never raises)."""

    def _boom():
        raise RuntimeError("no context")

    monkeypatch.setattr(platform_context, "current_context", _boom)
    mgr = agents_handler._capability_manager()
    assert mgr.available() is False
