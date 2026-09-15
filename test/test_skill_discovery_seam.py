"""The ``skill_discovery`` seam: edition-contributed skill discovery providers.

Pins the contract of ``SkillDiscoveryProvider.skill_providers()`` as consumed by
``handlers/discover._build_registry``: the public default contributes nothing;
a contributed provider is registered AFTER the built-in one and gated per
provider by the same ``external_access.admits_registry`` decision (so a managed
allowlist applies uniformly); registration is ADD-only and de-duped by name
(the built-in wins a collision); and the read is fail-closed — a raising
adapter yields no extra providers without breaking the built-in catalog.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultSkillDiscoveryProvider


class _FakeProvider:
    """Minimal structural match for the ``SkillProvider`` protocol."""

    def __init__(self, name: str = "edition-fake", api_base: str = "https://catalog.example.test"):
        self._name = name
        self.api_base = api_base

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return "Edition Fake"

    async def search(self, query: str, *, limit: int = 20):
        return []

    async def fetch_skill_content(self, skill_id: str):
        return None

    def is_available(self) -> bool:
        return True


class _NoBaseProvider(_FakeProvider):
    """A provider that exposes no ``api_base`` — gated on an empty base."""

    def __init__(self) -> None:
        super().__init__(name="edition-nobase")
        del self.api_base


class _Source:
    def __init__(self, providers):
        self._providers = list(providers)

    def skill_providers(self):
        return list(self._providers)


class _BoomSource:
    def skill_providers(self):
        raise RuntimeError("adapter exploded")


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


@pytest.fixture
def _reset_registry():
    """The discover provider registry is a module-level singleton."""
    from kiro_crew.dashboard.handlers import discover

    discover._registry = None
    yield
    discover._registry = None


def _with_context(monkeypatch, **overrides):
    base = build_default_context(KiroCrewConfig())
    monkeypatch.setattr(ctx_mod, "current_context", lambda: dataclasses.replace(base, **overrides))


def test_default_contributes_nothing() -> None:
    assert DefaultSkillDiscoveryProvider().skill_providers() == []


def test_default_context_registers_builtin_only(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover

    _with_context(monkeypatch)
    assert discover._build_registry().provider_names == ["skillsh"]


def test_contributed_provider_is_registered_after_builtin(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover

    fake = _FakeProvider()
    _with_context(monkeypatch, skill_discovery=_Source([fake]))
    reg = discover._build_registry()
    assert reg.provider_names == ["skillsh", "edition-fake"]
    assert reg.get("edition-fake") is fake


def test_contributed_provider_passes_the_same_policy_gate(monkeypatch, _reset_registry) -> None:
    """An allowlist admitting only the edition base keeps the built-in out too."""
    from kiro_crew.dashboard.handlers import discover

    policy = _AllowOnly("https://catalog.example.test")
    _with_context(
        monkeypatch,
        skill_discovery=_Source([_FakeProvider()]),
        external_access=policy,
    )
    reg = discover._build_registry()
    assert reg.provider_names == ["edition-fake"]
    # The gate saw the provider's own identity, not a core-supplied literal.
    assert any(
        kind == "skill" and name == "edition-fake" and base == "https://catalog.example.test"
        for kind, name, base in policy.seen
    )


def test_denied_contributed_provider_is_not_registered(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover

    _with_context(
        monkeypatch,
        skill_discovery=_Source([_FakeProvider()]),
        external_access=_DenyAll(),
    )
    assert discover._build_registry().provider_names == []


def test_provider_without_api_base_is_gated_on_empty_base(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover

    policy = _AllowOnly("https://")  # any non-empty https base passes; "" does not
    _with_context(
        monkeypatch,
        skill_discovery=_Source([_NoBaseProvider()]),
        external_access=policy,
    )
    reg = discover._build_registry()
    assert "edition-nobase" not in reg.provider_names
    assert ("skill", "edition-nobase", "") in policy.seen


def test_name_collision_keeps_the_builtin(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover
    from kiro_crew.skill_providers.skillsh import SkillsShProvider

    _with_context(monkeypatch, skill_discovery=_Source([_FakeProvider(name="skillsh")]))
    reg = discover._build_registry()
    assert reg.provider_names == ["skillsh"]
    assert isinstance(reg.get("skillsh"), SkillsShProvider)


def test_raising_adapter_fails_closed_to_builtin_only(monkeypatch, _reset_registry) -> None:
    from kiro_crew.dashboard.handlers import discover

    _with_context(monkeypatch, skill_discovery=_BoomSource())
    assert discover._build_registry().provider_names == ["skillsh"]


def test_path_like_provider_name_is_rejected(monkeypatch, _reset_registry) -> None:
    """A provider name becomes the first segment of an installed skill's
    on-disk key, so a separator-carrying or absolute name must never register."""
    from kiro_crew.dashboard.handlers import discover

    bad = [
        _FakeProvider(name="/writable/path"),
        _FakeProvider(name="a/b"),
        _FakeProvider(name=".."),
        _FakeProvider(name=""),
    ]
    _with_context(monkeypatch, skill_discovery=_Source(bad))
    assert discover._build_registry().provider_names == ["skillsh"]


def test_non_protocol_object_is_rejected(monkeypatch, _reset_registry) -> None:
    """An object missing part of the protocol (here ``is_available``) must be
    refused at registration, not crash the aggregate search later."""
    from kiro_crew.dashboard.handlers import discover

    class _Shapeless:
        name = "edition-shapeless"
        display_name = "Shapeless"
        api_base = "https://catalog.example.test"

        async def search(self, query, *, limit=20):
            return []

        async def fetch_skill_content(self, skill_id):
            return None

    _with_context(monkeypatch, skill_discovery=_Source([_Shapeless()]))
    assert discover._build_registry().provider_names == ["skillsh"]


def test_provider_raising_during_protocol_check_does_not_crash(
    monkeypatch, _reset_registry
) -> None:
    """On Python 3.10/3.11 a runtime-checkable ``isinstance`` EXECUTES
    properties, so a raising member surfaces inside the protocol check there
    (3.12 inspects statically and does not call it). Either way, registry
    construction must complete and the built-in catalog must survive."""
    from kiro_crew.dashboard.handlers import discover

    class _RaisingMember(_FakeProvider):
        @property
        def fetch_skill_content(self):
            raise RuntimeError("member exploded")

    _with_context(monkeypatch, skill_discovery=_Source([_RaisingMember(name="edition-raising")]))
    reg = discover._build_registry()
    assert "skillsh" in reg.provider_names


def test_registration_uses_the_gate_checked_identity(monkeypatch, _reset_registry) -> None:
    """``name`` is a property; a second read could differ from the vetted one.
    Registration must key on the value the policy gate actually checked."""
    from kiro_crew.dashboard.handlers import discover

    class _ShiftyName(_FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self._reads = 0

        @property
        def name(self) -> str:
            self._reads += 1
            return "edition-first" if self._reads == 1 else "skillsh"

    _with_context(monkeypatch, skill_discovery=_Source([_ShiftyName()]))
    reg = discover._build_registry()
    assert "edition-first" in reg.provider_names
    assert isinstance(reg.get("edition-first"), _ShiftyName)


def test_raising_is_available_reads_as_unavailable() -> None:
    """One broken provider must not take the aggregate search down."""
    from kiro_crew.skill_providers.base import ProviderRegistry

    class _BrokenAvail(_FakeProvider):
        def is_available(self) -> bool:
            raise RuntimeError("probe exploded")

    reg = ProviderRegistry()
    good = _FakeProvider(name="edition-good")
    reg.register(good, name="edition-good")
    reg.register(_BrokenAvail(name="edition-broken"), name="edition-broken")
    assert reg.available_providers == [good]
    assert reg.available_provider_names == ["edition-good"]


def test_display_name_is_scrubbed_and_falls_back(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import discover
    from kiro_crew.skill_providers.base import ProviderRegistry

    class _RaisingLabel(_FakeProvider):
        @property
        def display_name(self) -> str:
            raise RuntimeError("label exploded")

    class _SecretLabel(_FakeProvider):
        @property
        def display_name(self) -> str:
            return "Catalog key=AKIAIOSFODNN7EXAMPLE"

    reg = ProviderRegistry()
    reg.register(_RaisingLabel(name="edition-raises"), name="edition-raises")
    reg.register(_SecretLabel(name="edition-secret"), name="edition-secret")
    assert discover._display_name(reg, "edition-raises") == "edition-raises"
    assert "AKIAIOSFODNN7EXAMPLE" not in discover._display_name(reg, "edition-secret")
    assert discover._display_name(reg, "unregistered") == "unregistered"


@pytest.mark.asyncio
async def test_result_provenance_is_stamped_from_the_registration_key(
    monkeypatch, _reset_registry
) -> None:
    """A result row's ``provider`` field is provider-authored data: the registry
    re-stamps every row with the registration key of the provider that actually
    produced it, so a row can neither impersonate another registered catalog
    ("skillsh") nor smuggle an arbitrary provenance string."""
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import discover
    from kiro_crew.skill_providers.base import SkillSearchResult

    class _LyingProvider(_FakeProvider):
        async def search(self, query, *, limit=20):
            return [
                SkillSearchResult(id="a", name="A", description="", provider="skillsh"),
                SkillSearchResult(
                    id="b", name="B", description="", provider="https://evil.example/?k=v"
                ),
            ]

    # Admit only the fake provider's base: keeps the built-in provider out of
    # the registry so the fan-out search never makes a real network call.
    _with_context(
        monkeypatch,
        skill_discovery=_Source([_LyingProvider()]),
        external_access=_AllowOnly("https://catalog.example.test"),
    )

    state = MagicMock()
    monkeypatch.setattr(discover, "_get_skills", lambda _s: MagicMock(list_skills=lambda: []))
    monkeypatch.setattr(discover, "_sel", lambda: MagicMock())
    app = web.Application()
    app["state"] = state
    request = make_mocked_request("GET", "/api/skills/-/discover?q=x", app=app)
    body = await discover.api_skills_discover(request)
    import json

    payload = json.loads(body.body.decode("utf-8"))
    assert [row["provider"] for row in payload["results"]] == ["edition-fake", "edition-fake"]
    assert "skillsh" not in [row["provider"] for row in payload["results"]]
    assert "evil.example" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_install_with_raising_availability_probe_is_404_not_500(
    monkeypatch, _reset_registry
) -> None:
    """install/preview probe a single provider directly; a raising
    ``is_available`` must read as unavailable (404), never crash (500)."""
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import discover

    class _BrokenAvail(_FakeProvider):
        def is_available(self) -> bool:
            raise RuntimeError("probe exploded")

    _with_context(
        monkeypatch,
        skill_discovery=_Source([_BrokenAvail(name="edition-broken")]),
        external_access=_AllowOnly("https://catalog.example.test"),
    )
    app = web.Application()
    app["state"] = MagicMock()
    request = make_mocked_request("POST", "/api/skills/-/discover/install", app=app)
    from unittest.mock import AsyncMock

    request.json = AsyncMock(  # type: ignore[method-assign]
        return_value={"provider": "edition-broken", "skill_id": "x"}
    )
    response = await discover.api_skills_discover_install(request)
    assert response.status == 404
