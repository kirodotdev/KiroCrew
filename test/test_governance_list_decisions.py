"""``capabilities.decisions`` ``models`` / ``points`` / ``endpoints``.

RFC crew-lts-edition, appendix rows "Decision models", "Decision kinds" and
"Decision endpoints". The keys' parse and evaluator contract is in
``test_governance_list_keys.py``; this file covers host readings and refused
shapes. The call site, ``decide``, is pinned in ``test_decisions_gate.py``
(``TestDecisionLists``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.governance import parse_policy, resolve


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
