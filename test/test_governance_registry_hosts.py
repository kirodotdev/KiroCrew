"""The ``external_access.registry_hosts`` governance key.

RFC crew-lts-edition, appendix row "Extension sources". The key lists the
hosts of the skill and MCP discovery registries the Discover surfaces may query.
``_shared_access.admits_registry`` asks it before the edition's
``external_access`` policy, so the stricter of the two wins. An unnamed key
admits every registry, as before, in ``oss`` and LTS alike; only a policy that
names it narrows. A governance error admits too, logged and audited.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from kiro_crew import extension_governance as eg
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import admits_registry
from kiro_crew.platform import bootstrap as bootstrap_mod
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultExternalAccessPolicy
from kiro_crew.platform.governance import ScopedRuleset, parse_policy

SKILLSH = "https://skills.sh/api"
GITHUB = "https://api.github.com"
OFFICIAL_MCP = "https://registry.modelcontextprotocol.io"
_NO_KEY = object()


class _DenyAll:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return False

    def admits_cloud_deployment(self, target: str) -> bool:
        return False


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


@pytest.fixture(scope="module")
def base_ctx():
    """Built once with no security policy loaded; each test installs its own ceiling.

    Module scope runs ahead of the function-scoped no-fetch floor, so the policy
    loader is stubbed here: building the context never reads or fetches a policy.
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(bootstrap_mod, "load_security_policy", lambda: None)
        return build_default_context(KiroCrewConfig())


@pytest.fixture
def lts(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture
def install(base_ctx, monkeypatch):
    """Install a context whose ceiling carries *rule* for the key.

    ``None`` installs no ceiling at all; ``_NO_KEY`` a policy that does not name it.
    """

    def _install(rule, *, edition_policy=None) -> None:
        if rule is None:
            ceiling = None
        elif rule is _NO_KEY:
            ceiling = parse_policy({"version": 1, "boot": {}})
        else:
            ceiling = parse_policy({"version": 1, "boot": {}, eg.REGISTRY_HOSTS_SCOPE: rule})
        ctx = dataclasses.replace(
            base_ctx,
            governance=ceiling,
            external_access=edition_policy or DefaultExternalAccessPolicy(),
        )
        monkeypatch.setattr(ctx_mod, "current_context", lambda: ctx)

    return _install


def test_a_policy_carrying_the_key_loads_with_the_host_matcher() -> None:
    for body in (
        {eg.REGISTRY_HOSTS_SCOPE: {"mode": "allow", "allow": ["skills.sh"]}},
        {"external_access": {"registry_hosts": {"mode": "deny", "deny": ["skills.sh"]}}},
    ):
        ceiling = parse_policy({"version": 1, "boot": {}, **body})
        rule = ceiling.get(eg.REGISTRY_HOSTS_SCOPE)
        assert isinstance(rule, ScopedRuleset) and rule.matcher == "host"


@pytest.mark.parametrize("rule", [None, _NO_KEY], ids=["no-ceiling", "policy-without-key"])
def test_oss_without_the_key_admits_every_registry(isolated_home, fake_sel, install, rule) -> None:
    install(rule)
    assert admits_registry("skill", "skillsh", SKILLSH)
    assert admits_registry("skill", "github", GITHUB)
    assert admits_registry("mcp", "official", OFFICIAL_MCP)
    assert admits_registry("skill", "edition", "")
    assert eg.registry_host_denied("skill", SKILLSH) is None
    # Nothing governs the key, so nothing new is written to the SEL.
    fake_sel.log_governance_decision.assert_not_called()


def test_an_allow_list_admits_only_the_hosts_it_names(isolated_home, fake_sel, install) -> None:
    install({"mode": "allow", "allow": ["skills.sh", "*.github.com"]})
    assert admits_registry("skill", "skillsh", SKILLSH)
    assert admits_registry("skill", "github", GITHUB)
    # One list covers both catalogs: naming only skill hosts drops the MCP registry.
    assert not admits_registry("mcp", "official", OFFICIAL_MCP)
    # A provider with no host is judged as "", which no named host matches.
    assert not admits_registry("skill", "edition", "")
    decisions = [c.kwargs for c in fake_sel.log_governance_decision.call_args_list]
    assert {d["item"] for d in decisions if d["outcome"] == "denied"} == {
        "registry.modelcontextprotocol.io",
        "",
    }


def test_a_catch_all_admits_a_provider_with_no_host(isolated_home, fake_sel, install) -> None:
    install({"mode": "allow", "allow": ["*"]})
    assert admits_registry("skill", "edition", "")


def test_a_deny_list_refuses_the_hosts_it_names(isolated_home, fake_sel, install) -> None:
    install({"mode": "deny", "deny": ["skills.sh"]})
    assert not admits_registry("skill", "skillsh", SKILLSH)
    assert admits_registry("mcp", "official", OFFICIAL_MCP)


def test_the_host_is_matched_never_the_url_or_the_provider_name(
    isolated_home, fake_sel, install
) -> None:
    # The provider's own name is not what the list is judged on.
    install({"mode": "allow", "allow": ["skillsh"]})
    assert not admits_registry("skill", "skillsh", SKILLSH)
    # Case-insensitive, and the scheme, port and path take no part.
    install({"mode": "allow", "allow": ["SKILLS.SH"]})
    assert admits_registry("skill", "renamed", "https://skills.sh:443/v2/api")
    # A provider that repoints at another host loses the admission its name had.
    assert not admits_registry("skill", "skillsh", "https://skills.example.net/api")


def test_the_stricter_of_edition_and_ceiling_wins(isolated_home, fake_sel, install) -> None:
    install({"mode": "allow", "allow": ["skills.sh"]}, edition_policy=_DenyAll())
    assert not admits_registry("skill", "skillsh", SKILLSH)


@pytest.mark.parametrize("rule", [None, _NO_KEY], ids=["no-ceiling", "policy-without-key"])
def test_lts_unnamed_key_admits_every_registry(lts, isolated_home, fake_sel, install, rule) -> None:
    install(rule)
    assert admits_registry("skill", "skillsh", SKILLSH)
    assert admits_registry("skill", "github", GITHUB)
    assert admits_registry("mcp", "official", OFFICIAL_MCP)
    assert eg.registry_host_denied("skill", SKILLSH) is None
    # Nothing governs the key, so nothing new is written to the SEL.
    fake_sel.log_governance_decision.assert_not_called()


def test_lts_policy_allow_list_narrows(lts, isolated_home, fake_sel, install) -> None:
    install({"mode": "allow", "allow": ["registry.modelcontextprotocol.io"]})
    assert admits_registry("mcp", "official", OFFICIAL_MCP)
    assert not admits_registry("skill", "skillsh", SKILLSH)


def test_lts_policy_deny_list_narrows(lts, isolated_home, fake_sel, install) -> None:
    install({"mode": "deny", "deny": ["skills.sh"]})
    assert not admits_registry("skill", "skillsh", SKILLSH)
    assert admits_registry("mcp", "official", OFFICIAL_MCP)


@pytest.fixture
def degraded(monkeypatch):
    """Record each ``audit_governance_degraded`` call (the log and SEL record)."""
    calls: list[tuple[str, dict]] = []

    def _record(chokepoint, **kwargs) -> None:
        calls.append((chokepoint, kwargs))

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.audit_governance_degraded", _record)
    return calls


@pytest.mark.parametrize("edition_name", ["oss", "lts"])
def test_a_governance_error_is_logged_and_admits(
    isolated_home, fake_sel, install, monkeypatch, degraded, edition_name
) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", edition_name)
    install({"mode": "allow", "allow": ["api.github.com"]})

    def _boom(*_a, **_k):
        raise RuntimeError("profile store unreadable")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _boom)
    assert admits_registry("skill", "skillsh", SKILLSH)
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    assert degraded == [
        ("registry:skill", {"session_key": HOST_SESSION_KEY, "scope": eg.REGISTRY_HOSTS_SCOPE})
    ]


def test_an_error_inside_the_evaluation_is_logged_and_admits(
    isolated_home, fake_sel, install, monkeypatch, degraded
) -> None:
    install({"mode": "allow", "allow": ["api.github.com"]})

    def _boom(*_a, **_k):
        raise RuntimeError("profile store unreadable")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.resolve_active_scope", _boom)
    assert admits_registry("skill", "skillsh", SKILLSH)
    assert [(c, k.get("scope"), k.get("failed_closed")) for c, k in degraded] == [
        ("governance_permits", eg.REGISTRY_HOSTS_SCOPE, False)
    ]


def test_a_composition_error_still_propagates(isolated_home, install, monkeypatch) -> None:
    install(None)

    def _boom(*_a, **_k):
        raise ctx_mod.PlatformCompositionError("not composed")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _boom)
    with pytest.raises(ctx_mod.PlatformCompositionError):
        eg.registry_host_denied("skill", SKILLSH)


def test_a_denied_registry_is_never_registered(
    isolated_home, fake_sel, install, monkeypatch
) -> None:
    from kiro_crew.dashboard.handlers import discover, mcp_discover

    monkeypatch.setattr(discover, "_registry", None)
    monkeypatch.setattr(mcp_discover, "_registry", None)
    install({"mode": "allow", "allow": ["api.github.com"]})
    assert "skillsh" not in discover._build_registry().provider_names
    assert "official" not in mcp_discover._build_registry().provider_names
