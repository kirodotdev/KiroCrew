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
    req.headers = {}
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
    # No installed resolver AND no middleware-established identity -> LOCAL.
    req = MagicMock()
    req.app = {}
    req.headers = {}
    req.__getitem__.side_effect = {}.__getitem__
    req.__contains__.side_effect = {}.__contains__
    assert _knowledge_query_principal(req) is LOCAL_PRINCIPAL


def test_query_principal_uses_installed_resolver():
    p = QueryPrincipal(principal_id="alice")
    req = _request({"knowledge_query_principal": lambda r: p})
    assert _knowledge_query_principal(req) is p


def test_query_principal_falls_back_when_resolver_raises():
    def _boom(r):
        raise RuntimeError("down")
    # Resolver raises -> fall back; with NO middleware identity -> LOCAL.
    req = MagicMock()
    req.app = {"knowledge_query_principal": _boom}
    req.headers = {}
    req.__getitem__.side_effect = {}.__getitem__
    req.__contains__.side_effect = {}.__contains__
    assert _knowledge_query_principal(req) is LOCAL_PRINCIPAL


# ── query principal DERIVED from the middleware-established identity ─────────
# No installed hook: the principal is built ONLY from the keys the auth
# middleware sets post-validation (request["app"] / ["user"] /
# ["is_dashboard_user"]) -- never from a caller-supplied header. The full
# real-middleware path is covered in test_knowledge_query_principal_authpath.py;
# these unit-level tests pin the key-reading contract.

def _authed_request(store: dict) -> MagicMock:
    """A request whose item-storage holds the given middleware-set keys."""
    req = MagicMock()
    req.app = {}
    req.headers = {"X-Session-Key": "session:should-be-ignored"}
    req.__getitem__.side_effect = store.__getitem__
    req.__contains__.side_effect = store.__contains__
    return req


def test_query_principal_no_established_identity_does_not_mint():
    # None of the middleware keys present -> do not mint an authorization
    # subject; the X-Session-Key header is ignored -> LOCAL_PRINCIPAL (unverified).
    p = _knowledge_query_principal(_authed_request({}))
    assert p is LOCAL_PRINCIPAL
    assert p.verified is False


def test_query_principal_dashboard_user_is_verified_owner():
    store = {"app": "", "user": "alice", "is_dashboard_user": True}
    p = _knowledge_query_principal(_authed_request(store))
    assert isinstance(p, QueryPrincipal)
    assert p.principal_id == "user:alice"
    assert p.verified is True
    assert p.local_library is True
    assert p is not LOCAL_PRINCIPAL


def test_query_principal_authenticated_app_is_verified():
    store = {"app": "mochi", "user": "local-app", "is_dashboard_user": False}
    p = _knowledge_query_principal(_authed_request(store))
    assert isinstance(p, QueryPrincipal)
    assert p.principal_id == "app:mochi"
    assert p.verified is True
    assert p.local_library is True


def test_query_principal_ignores_arbitrary_header_when_no_authed_identity():
    # Only a caller-supplied header, no middleware identity -> not minted,
    # and the result is UNVERIFIED (the resolver would deny it).
    req = MagicMock()
    req.app = {}
    req.headers = {"X-Session-Key": "session:attacker"}
    req.__getitem__.side_effect = {}.__getitem__
    req.__contains__.side_effect = {}.__contains__
    p = _knowledge_query_principal(req)
    assert p is LOCAL_PRINCIPAL
    assert p.verified is False


def test_binding_resolver_none_when_unwired():
    assert _knowledge_binding_resolver(_request({})) is None


def test_binding_resolver_bridges_installed_accessgrant_resolver():
    # The handler wraps the installed W01 resolver through the bridge so its
    # AccessGrant (no subject_ids) is mapped to a real AccessContext.
    import dataclasses

    from kiro_crew.knowledge.acl import AccessContext, QueryPrincipal

    @dataclasses.dataclass(frozen=True)
    class _Grant:  # mirrors W01 AccessGrant: no subject_ids
        subject: str
        tenant: str
        groups: frozenset = dataclasses.field(default_factory=frozenset)
        bypass_acl: bool = False

    class _Inner:
        def resolve(self, principal, provider, account):
            return _Grant(subject="sp-alice", tenant="ms-A")

    bridged = _knowledge_binding_resolver(_request({"knowledge_binding_resolver": _Inner()}))
    out = bridged.resolve(QueryPrincipal("alice"), "sharepoint", "tenant-A")
    assert isinstance(out, AccessContext)
    assert out.subject == "sp-alice" and out.tenant == "ms-A"
    assert out.subject_ids == frozenset({"sp-alice"})


def test_binding_resolver_bridge_passes_through_accesscontext():
    from kiro_crew.knowledge.acl import AccessContext, QueryPrincipal

    ctx = AccessContext(subject="sp-bob", tenant="ms-B")

    class _Inner:
        def resolve(self, principal, provider, account):
            return ctx

    bridged = _knowledge_binding_resolver(_request({"knowledge_binding_resolver": _Inner()}))
    assert bridged.resolve(QueryPrincipal("bob"), "sharepoint", "acct") is ctx


# ── optional vendor-connector registration seam ────────────────────────────
# The structured vendor connectors (GitHub/Google/Salesforce) each land on main
# through their own PR. Each takes its live-read dependency as an OPTIONAL
# injected arg and self-enforces fail-closed when it is absent (GitHub refuses
# at fetch, Google/SF at validate). The shared handler registers each connector
# whenever its module imports (its documented registered+editable contract), and
# injects the runner factory into the constructor when the host installs one.
# Absent module => not registered (fail-closed, never public).

from kiro_crew.dashboard.handlers.knowledge import _register_optional_connector  # noqa: E402


class _FakeConnector:
    """Mirrors the real vendor contract: OPTIONAL injected factory (None default),
    injected BY KEYWORD (here ``operations_factory``, as Google's is)."""

    def __init__(self, operations_factory=None):
        self._factory = operations_factory

    def source_type(self) -> str:
        return "fake_vendor"


class _SFShapeConnector:
    """Mirrors Salesforce's constructor: a positional ``call_runner`` and a
    keyword-only ``runner_factory``. A positional inject would WRONGLY land the
    per-source factory in ``call_runner``; keyword inject must target
    ``runner_factory``."""

    def __init__(self, call_runner=None, *, runner_factory=None):
        self.call_runner = call_runner
        self.runner_factory = runner_factory

    def source_type(self) -> str:
        return "salesforce"


def _install_fake_module(monkeypatch, mod_name: str, cls):
    import sys
    import types

    mod = types.ModuleType(mod_name)
    setattr(mod, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, mod_name, mod)
    return mod_name


def test_optional_connector_injects_by_keyword(monkeypatch):
    mod_name = _install_fake_module(monkeypatch, "kiro_crew._fake_vendor_mod", _FakeConnector)
    runner = object()  # stands in for the host-installed per-source factory
    connectors: dict = {}
    ok = _register_optional_connector(
        connectors, mod_name, "_FakeConnector",
        runner_factory=runner, inject_kw="operations_factory",
    )
    assert ok is True
    assert connectors["fake_vendor"]._factory is runner


def test_optional_connector_sf_shape_factory_lands_in_runner_factory(monkeypatch):
    # The mis-wire Root warned about: a positional inject would put the factory
    # in call_runner. Keyword inject targets runner_factory; call_runner stays None.
    mod_name = _install_fake_module(monkeypatch, "kiro_crew._sf_shape_mod", _SFShapeConnector)
    factory = object()
    connectors: dict = {}
    ok = _register_optional_connector(
        connectors, mod_name, "_SFShapeConnector",
        runner_factory=factory, inject_kw="runner_factory",
    )
    assert ok is True
    conn = connectors["salesforce"]
    assert conn.runner_factory is factory
    assert conn.call_runner is None


def test_optional_connector_registers_without_runner_using_default(monkeypatch):
    # Module present but NO runner factory installed -> STILL registered, built
    # with the connector's own default. The connector self-enforces fail-closed
    # live reads (registered + edition-overridable is its documented contract);
    # it is not a dead entry.
    mod_name = _install_fake_module(monkeypatch, "kiro_crew._fake_vendor_mod2", _FakeConnector)
    connectors: dict = {}
    ok = _register_optional_connector(
        connectors, mod_name, "_FakeConnector", runner_factory=None,
        inject_kw="operations_factory",
    )
    assert ok is True
    assert "fake_vendor" in connectors
    assert connectors["fake_vendor"]._factory is None


def test_optional_connector_absent_module_is_failclosed():
    connectors: dict = {}
    ok = _register_optional_connector(
        connectors,
        "kiro_crew.knowledge.connectors._not_landed_yet",
        "Nope",
        runner_factory=object(),
    )
    assert ok is False
    assert connectors == {}  # source_type simply not present -> add_source rejects


def test_optional_connector_broken_module_is_skipped(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("kiro_crew._broken_vendor_mod")

    class _Broken:
        def __init__(self, runner_factory=None):
            raise RuntimeError("vendor ctor blew up")

    mod._Broken = _Broken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._broken_vendor_mod", mod)

    connectors: dict = {"local_folder": object()}
    ok = _register_optional_connector(
        connectors, "kiro_crew._broken_vendor_mod", "_Broken",
        runner_factory=object(), inject_kw="runner_factory",
    )
    assert ok is False
    # Built-ins untouched by a broken vendor module.
    assert "local_folder" in connectors
    assert len(connectors) == 1
