"""The ``external_access`` seam: which external services this deployment may use.

Three surfaces the core offers unconditionally: skill discovery (skills.sh), MCP
server discovery (the official registry), and cloud deployment (real AWS
infrastructure in the operator's account). These tests pin that a composed policy
can refuse each one, that refusing a registry means the provider is never
registered rather than failing per request, that refusing cloud deployment makes
the deploy surface report itself disabled AND refuse mutations, and that the
public default still admits everything.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import admits_registry as _admits
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultExternalAccessPolicy


def _base_context():
    return build_default_context(KiroCrewConfig())


class _FakeRequest:
    """Minimal stand-in — the gated handlers read nothing off the request."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.match_info: dict[str, str] = {}


class _DenyAll:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return False

    def admits_cloud_deployment(self, target: str) -> bool:
        return False


class _AllowOnly:
    """Allowlist by URL, the way a managed deployment would."""

    def __init__(self, allowed: str) -> None:
        self.allowed = allowed
        self.seen: list[tuple[str, str, str]] = []

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        self.seen.append((kind, name, api_base))
        return api_base.startswith(self.allowed)

    def admits_cloud_deployment(self, target: str) -> bool:
        return True


class _NoCloud:
    """Registries fine, cloud deployment withheld — the internal-edition shape."""

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return True

    def admits_cloud_deployment(self, target: str) -> bool:
        return False


class _Boom:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        raise RuntimeError("adapter exploded")

    def admits_cloud_deployment(self, target: str) -> bool:
        raise RuntimeError("adapter exploded")


@pytest.fixture
def _reset_registries():
    """Both provider registries are module-level singletons."""
    from kiro_crew.dashboard.handlers import discover, mcp_discover

    discover._registry = None
    mcp_discover._registry = None
    yield
    discover._registry = None
    mcp_discover._registry = None


def _with_policy(monkeypatch, policy):
    base = _base_context()
    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: dataclasses.replace(base, external_access=policy)
    )


class TestDefaultAdmitsEverything:
    def test_public_default_admits_any_registry(self):
        pol = DefaultExternalAccessPolicy()
        assert pol.admits_registry("skill", "skillsh", "https://api.skills.sh")
        assert pol.admits_registry("mcp", "official", "https://registry.modelcontextprotocol.io")

    def test_default_context_carries_the_slot(self):
        assert isinstance(_base_context().external_access, DefaultExternalAccessPolicy)


class TestSkillProviderGating:
    def test_denied_provider_is_not_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import discover

        _with_policy(monkeypatch, _DenyAll())
        assert discover._build_registry().provider_names == []

    def test_admitted_provider_is_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import discover

        _with_policy(monkeypatch, DefaultExternalAccessPolicy())
        assert "skillsh" in discover._build_registry().provider_names


class TestMcpProviderGating:
    def test_denied_official_registry_is_not_registered(self, monkeypatch, _reset_registries):
        from kiro_crew.dashboard.handlers import mcp_discover

        _with_policy(monkeypatch, _DenyAll())
        assert "official" not in mcp_discover._build_registry().provider_names

    def test_allowlisting_by_url_keeps_an_internal_registry_only(
        self, monkeypatch, _reset_registries
    ):
        """The shape a managed deployment actually wants.

        Allowing only an internal base URL must drop the public MCP registry while
        the decision is made on the URL, not the provider's self-chosen name.
        """
        from kiro_crew.dashboard.handlers import mcp_discover

        pol = _AllowOnly("https://internal.example.invalid/")
        _with_policy(monkeypatch, pol)

        names = mcp_discover._build_registry().provider_names

        assert "official" not in names
        assert ("mcp", "official", "https://registry.modelcontextprotocol.io") in pol.seen


class TestCloudDeploymentGating:
    def test_default_admits_cloud_deployment(self):
        assert DefaultExternalAccessPolicy().admits_cloud_deployment("aws") is True

    def test_denied_policy_refuses(self, monkeypatch):
        from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

        _with_policy(monkeypatch, _NoCloud())
        assert admits_cloud_deployment("aws") is False

    def test_config_reports_the_disabled_state(self, monkeypatch):
        """The frontend needs this to hide the surface rather than 403 every button."""
        import asyncio as _asyncio

        from kiro_crew.deploy import handlers as dh

        _with_policy(monkeypatch, _NoCloud())
        monkeypatch.setattr(dh, "_load_config", lambda: {"profile": "p", "region": "r"})

        resp = _asyncio.run(dh._handle_get_config(_FakeRequest()))

        assert resp.status == 200
        assert json.loads(resp.text)["cloudDeploymentEnabled"] is False

    def test_config_read_is_not_itself_gated(self, monkeypatch):
        """A 403 here would leave the page unable to explain why deploy is gone."""
        import asyncio as _asyncio

        from kiro_crew.deploy import handlers as dh

        _with_policy(monkeypatch, _NoCloud())
        monkeypatch.setattr(dh, "_load_config", lambda: {"profile": "p"})

        assert _asyncio.run(dh._handle_get_config(_FakeRequest())).status == 200

    def test_provisioning_routes_are_gated_but_withdrawal_is_not(self, monkeypatch):
        """Asserted against the REAL route table, not a hand-kept list.

        Two properties at once. A new PROVISIONING endpoint added without the guard
        fails here rather than shipping an ungated path into a cloud account. And
        withdrawal must stay ungated: gating recall/destroy/teardown would strand
        exposure created while deployment was still permitted, leaving an operator
        unable to take a live public URL down through the supported API.
        """
        from aiohttp import web as _web

        from kiro_crew.deploy import handlers as dh

        app = _web.Application()
        dh.register_routes(app)

        gated, ungated = set(), set()
        for r in app.router.routes():
            if not r.resource or r.method not in {"POST", "PUT", "DELETE"}:
                continue
            target = gated if getattr(r.handler, "_cloud_gated", False) else ungated
            target.add(r.resource.canonical)

        # Anti-vacuity: the surface really does carry several mutating routes.
        assert len(gated) + len(ungated) >= 8

        must_be_gated = {"/api/deploy/deploy", "/api/deploy/verify", "/api/deploy/config"}
        assert must_be_gated <= gated, f"provisioning left ungated: {must_be_gated - gated}"

        withdrawal = {
            "/api/deploy/recall",
            "/api/deploy/destroy",
            "/api/deploy/teardown/{slug}",
        }
        assert withdrawal <= ungated, f"withdrawal wrongly gated: {withdrawal & gated}"

    def test_a_gated_route_returns_403_with_a_machine_readable_code(self, monkeypatch):
        import asyncio as _asyncio

        from kiro_crew.deploy import handlers as dh

        _with_policy(monkeypatch, _NoCloud())

        async def _never(_request):  # pragma: no cover - must not run
            raise AssertionError("handler ran despite a denied policy")

        resp = _asyncio.run(dh._cloud_gated(_never)(_FakeRequest()))

        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "cloud_deployment_denied"

    def test_a_raising_policy_denies_deployment(self, monkeypatch):
        from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

        _with_policy(monkeypatch, _Boom())
        assert admits_cloud_deployment("aws") is False


class TestAdmissionIsAudited:
    """GPT review: an admission decision that is not logged cannot be proven."""

    def _events(self, monkeypatch):
        from kiro_crew.dashboard.handlers import _shared

        seen: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                seen.append(kw)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
        return seen, _shared

    def test_denial_is_audited(self, monkeypatch):
        seen, _shared = self._events(monkeypatch)
        _with_policy(monkeypatch, _NoCloud())

        _shared.admits_cloud_deployment("aws")

        assert [e["outcome"] for e in seen] == ["denied"]
        assert seen[0]["operation"] == "external_access:cloud_deployment"

    def test_admission_is_audited_too(self, monkeypatch):
        """Both outcomes: a denial-only log cannot show the permitted path was used."""
        seen, _shared = self._events(monkeypatch)
        _with_policy(monkeypatch, DefaultExternalAccessPolicy())

        _shared.admits_cloud_deployment("aws")

        assert [e["outcome"] for e in seen] == ["allowed"]

    def test_registry_admission_is_audited(self, monkeypatch):
        seen, _shared = self._events(monkeypatch)
        _with_policy(monkeypatch, _NoCloud())

        _shared.admits_registry("mcp", "official", "https://example.invalid")

        assert seen[0]["operation"] == "external_access:registry:mcp"
        assert seen[0]["resources"] == "https://example.invalid"

    def test_a_failed_audit_denies_rather_than_proceeding_unlogged(self, monkeypatch):
        """An access grant that cannot be recorded is an unaccountable grant.

        This asserts the OPPOSITE of an earlier revision, which treated auditing as
        best-effort so a broken sink could never turn a working install into a
        failing one. That reasoning was wrong for this seam specifically: the whole
        point is that an operator who restricted these surfaces can prove
        afterwards what was reached, and a silent unlogged grant destroys exactly
        that. Denying is the conservative direction — the operator loses a registry
        browser or a deploy button and gets a logged error, instead of silently
        gaining unaudited egress.
        """
        from kiro_crew.dashboard.handlers import _shared

        def _boom():
            raise RuntimeError("sel key unwritable")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)
        _with_policy(monkeypatch, DefaultExternalAccessPolicy())

        assert _shared.admits_cloud_deployment("aws") is False
        assert _shared.admits_registry("mcp", "official", "https://example.invalid") is False


class TestFailClosed:
    def test_a_raising_policy_denies_rather_than_admits(self, monkeypatch, _reset_registries):
        """A composed policy that throws must not hand back public egress.

        Reaching this path means a managed deployment intended to restrict
        something, so admitting on error would restore the exact fetch the
        operator disabled.
        """
        _with_policy(monkeypatch, _Boom())
        assert _admits("mcp", "official", "https://example.invalid") is False

    def test_composition_error_still_propagates(self, monkeypatch):
        """The CPP invariant: a composition failure aborts, it does not degrade."""

        def boom():
            raise ctx_mod.PlatformCompositionError("no companion")

        monkeypatch.setattr(ctx_mod, "current_context", boom)
        with pytest.raises(ctx_mod.PlatformCompositionError):
            _admits("skill", "skills.sh", "https://api.skills.sh")
