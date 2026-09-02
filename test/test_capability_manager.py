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
    """``_capability_manager()`` returns the context-provided manager, already
    liveness-bounded (the context wraps it at composition; the accessor just
    passes it through) and delegating reads to the inner."""
    from kiro_crew.platform.capability_bound import BoundedCapabilityManager

    class _Sentinel:
        def available(self) -> bool:
            return True

    # The real context wraps at composition, so hand the accessor an
    # already-bounded manager (what ``current_context().capability_manager``
    # returns) and assert the accessor preserves — not double-wraps — it.
    sentinel = BoundedCapabilityManager(_Sentinel())

    class _Ctx:
        capability_manager = sentinel

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    mgr = agents_handler._capability_manager()
    assert isinstance(mgr, BoundedCapabilityManager)
    assert mgr is sentinel  # not re-wrapped
    # Reads delegate straight through to the context-provided manager.
    assert mgr.available() is True


def test_capability_manager_fails_closed(monkeypatch):
    """A context-lookup failure falls back to an unavailable Default (never
    raises) — and the fallback is bounded too, so the return type is uniform."""
    from kiro_crew.platform.capability_bound import BoundedCapabilityManager

    def _boom():
        raise RuntimeError("no context")

    monkeypatch.setattr(platform_context, "current_context", _boom)
    mgr = agents_handler._capability_manager()
    assert isinstance(mgr, BoundedCapabilityManager)
    assert mgr.available() is False


def test_context_composition_bounds_capability_manager():
    """The LIVENESS bound is applied at CONTEXT COMPOSITION, not just at the
    dashboard accessor — so EVERY reader of ``current_context().capability_manager``
    (including non-dashboard consumers) inherits it (arbiter item 1)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.capability_bound import BoundedCapabilityManager

    ctx = build_default_context(KiroCrewConfig())
    # A direct context read (bypassing the dashboard accessor) is already bounded.
    assert isinstance(ctx.capability_manager, BoundedCapabilityManager)


def test_composition_wrap_is_idempotent():
    """A ``dataclasses.replace`` that carries an already-bounded manager forward
    (the companion's composition path) must not double-wrap it."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.capability_bound import BoundedCapabilityManager

    ctx = build_default_context(KiroCrewConfig())
    inner = ctx.capability_manager
    assert isinstance(inner, BoundedCapabilityManager)
    replaced = dataclasses.replace(ctx)
    # __post_init__ re-runs on replace; idempotent bind must not re-wrap.
    assert replaced.capability_manager is inner
    assert not isinstance(replaced.capability_manager._inner, BoundedCapabilityManager)
