"""Dashboard knowledge-search ACL wiring seam.

The dashboard handler resolves the query-time AccessContext from the request and
pulls an optional RevalidationHook off the app, so a shared/multi-tenant
deployment can plug in the provider identity resolver + revalidation hook
without touching the call sites. These tests exercise those two seams directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.handlers.knowledge import (
    _knowledge_access_context,
    _knowledge_revalidator,
)
from kiro_crew.knowledge.acl import LOCAL_LIBRARY, AccessContext


def _request(app: dict) -> MagicMock:
    req = MagicMock()
    req.app = app
    return req


def test_access_context_defaults_to_local_library_when_no_resolver():
    ctx = _knowledge_access_context(_request({}))
    assert ctx is LOCAL_LIBRARY


def test_access_context_uses_installed_resolver():
    resolved = AccessContext(subject="alice", tenant="acme")
    req = _request({"knowledge_identity_resolver": lambda r: resolved})
    assert _knowledge_access_context(req) is resolved


def test_access_context_falls_back_when_resolver_returns_none():
    req = _request({"knowledge_identity_resolver": lambda r: None})
    assert _knowledge_access_context(req) is LOCAL_LIBRARY


def test_access_context_falls_back_when_resolver_raises():
    def _boom(r):
        raise RuntimeError("resolver down")

    req = _request({"knowledge_identity_resolver": _boom})
    # Fail-closed to the local single-user context (managed items stay denied),
    # never an exception out of the handler.
    assert _knowledge_access_context(req) is LOCAL_LIBRARY


def test_revalidator_is_none_when_unwired():
    assert _knowledge_revalidator(_request({})) is None


def test_revalidator_returns_installed_hook():
    hook = object()
    assert _knowledge_revalidator(_request({"knowledge_revalidator": hook})) is hook
