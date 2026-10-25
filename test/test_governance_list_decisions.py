"""``capabilities.decisions`` ``models`` / ``points`` / ``endpoints``.

RFC crew-enterprise-edition, appendix rows "Decision models", "Decision kinds" and
"Decision endpoints". Each key parses with the catalog's matcher; a list that names
X permits X and denies Y; a gate pinned off denies every item; no policy, or no
list, permits as before. The call site, ``decide``, is pinned in
``test_decisions_gate.py`` (``TestDecisionLists``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.governance import CapabilityGate, parse_policy, resolve

#: (scope, inner key, matcher, an item the list allows, an item it does not)
_CASES = (
    ("capabilities.decisions", "models", "identifier", "jev-1", "other-model"),
    ("capabilities.decisions", "points", "identifier", "skills.select", "tool.risk"),
    ("capabilities.decisions", "endpoints", "host", "jev.corp.example", "api.example"),
)
_IDS = [f"{scope}.{key}" for scope, key, *_ in _CASES]


def _policy(scope: str, body: dict) -> dict:
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "capabilities": {scope.split(".", 1)[1]: body},
    }


def _allow(scope: str, key: str, allowed: str) -> dict:
    return _policy(scope, {"scopes": {key: {"mode": "allow", "allow": [allowed]}}})


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


def _install_policy(monkeypatch, doc: dict | None) -> None:
    from kiro_crew.metrics import provider
    from kiro_crew.platform import context as pc

    ceiling = parse_policy(doc) if doc is not None else None
    ctx = SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    # ``metrics.provider`` binds the name at import, so it is patched there too.
    monkeypatch.setattr(provider, "current_context", lambda: ctx)


# ── The keys, through the real evaluator ────────────────────────────────


class TestListKeys:
    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_each_key_parses_with_its_catalog_matcher(self, scope, key, matcher, yes, no) -> None:
        gate = parse_policy(_allow(scope, key, yes)).get(scope)
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is True  # every one of these rows defaults on
        assert gate.scopes[key].matcher == matcher

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_the_list_permits_what_it_names_and_denies_the_rest(
        self, scope, key, matcher, yes, no
    ) -> None:
        ceiling = parse_policy(_allow(scope, key, yes))
        assert resolve(ceiling, None, scope, f"{key}:{yes}").permitted is True
        assert resolve(ceiling, None, scope, f"{key}:{no}").permitted is False

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_a_gate_pinned_off_denies_every_item(self, scope, key, matcher, yes, no) -> None:
        ceiling = parse_policy(_policy(scope, {"enabled": False}))
        assert resolve(ceiling, None, scope, f"{key}:{yes}").permitted is False

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_no_policy_or_no_list_permits_as_before(self, scope, key, matcher, yes, no) -> None:
        assert resolve(None, None, scope, f"{key}:{no}").permitted is True
        ceiling = parse_policy(_policy(scope, {"enabled": True}))
        assert resolve(ceiling, None, scope, f"{key}:{no}").permitted is True


class TestHostReadings:
    """An ASCII host with a character a DNS name does not use is still asked as
    read, so a deny glob over its domain refuses it."""

    def test_an_odd_ascii_host_is_asked_as_read(self) -> None:
        from kiro_crew.platform.governance import url_policy_hosts

        hosts = url_policy_hosts("https://clip+one.blocked.example/v1")
        assert "clip+one.blocked.example" in hosts
        assert "" not in hosts

    def test_a_deny_glob_refuses_an_odd_ascii_host(self) -> None:
        from kiro_crew.platform.governance import url_policy_hosts

        scope = "capabilities.decisions"
        doc = _policy(
            scope, {"scopes": {"endpoints": {"mode": "deny", "deny": ["*.blocked.example"]}}}
        )
        ceiling = parse_policy(doc)
        hosts = url_policy_hosts("https://clip+one.blocked.example/v1")
        assert any(not resolve(ceiling, None, scope, f"endpoints:{h}").permitted for h in hosts)


class TestWrongShape:
    @pytest.mark.parametrize("key", ["models", "points", "endpoints"])
    @pytest.mark.parametrize("value", [["jev-1"], "jev-1", 1, None])
    def test_a_list_in_the_wrong_shape_fails_to_load(self, key, value) -> None:
        """A malformed list must not load as no list, which permits every item."""
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match=f"scopes.{key} must be an object"):
            parse_policy(_policy("capabilities.decisions", {"scopes": {key: value}}))
