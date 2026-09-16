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
    _knowledge_binding_resolver,
    _knowledge_query_principal,
    _knowledge_revalidator,
)
from kiro_crew.knowledge.acl import LOCAL_LIBRARY, LOCAL_PRINCIPAL, AccessContext, QueryPrincipal


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


def test_query_principal_defaults_to_local():
    assert _knowledge_query_principal(_request({})) is LOCAL_PRINCIPAL


def test_query_principal_uses_installed_resolver():
    p = QueryPrincipal(principal_id="alice")
    req = _request({"knowledge_query_principal": lambda r: p})
    assert _knowledge_query_principal(req) is p


def test_query_principal_falls_back_when_resolver_raises():
    def _boom(r):
        raise RuntimeError("down")
    req = _request({"knowledge_query_principal": _boom})
    assert _knowledge_query_principal(req) is LOCAL_PRINCIPAL


def test_binding_resolver_none_when_unwired():
    assert _knowledge_binding_resolver(_request({})) is None


def test_binding_resolver_returns_installed():
    resolver = object()
    assert _knowledge_binding_resolver(_request({"knowledge_binding_resolver": resolver})) is resolver


# ── optional vendor-connector registration seam ────────────────────────────
# The structured vendor connectors (GitHub/Google/Salesforce) each land on main
# through their own PR. The shared handler registers each ONLY when its module
# is importable, keyed by the connector's own source_type. Absent module => not
# registered => add_source rejects it (fail-closed, never public).

from kiro_crew.dashboard.handlers.knowledge import _register_optional_connector  # noqa: E402


class _FakeConnector:
    def source_type(self) -> str:
        return "fake_vendor"


def test_optional_connector_registers_when_module_present(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("kiro_crew._fake_vendor_mod")
    mod.FakeConnector = _FakeConnector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._fake_vendor_mod", mod)

    connectors: dict = {}
    ok = _register_optional_connector(
        connectors, "kiro_crew._fake_vendor_mod", "FakeConnector"
    )
    assert ok is True
    # Keyed by the connector's OWN source_type, not the module/class name.
    assert "fake_vendor" in connectors
    assert isinstance(connectors["fake_vendor"], _FakeConnector)


def test_optional_connector_absent_module_is_failclosed():
    connectors: dict = {}
    ok = _register_optional_connector(
        connectors, "kiro_crew.knowledge.connectors._not_landed_yet", "Nope"
    )
    assert ok is False
    assert connectors == {}  # source_type simply not present -> add_source rejects


def test_optional_connector_broken_module_is_skipped(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("kiro_crew._broken_vendor_mod")

    class _Broken:
        def __init__(self):
            raise RuntimeError("vendor ctor blew up")

    mod._Broken = _Broken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._broken_vendor_mod", mod)

    connectors: dict = {"local_folder": object()}
    ok = _register_optional_connector(
        connectors, "kiro_crew._broken_vendor_mod", "_Broken"
    )
    assert ok is False
    # Built-ins untouched by a broken vendor module.
    assert "local_folder" in connectors
    assert len(connectors) == 1
