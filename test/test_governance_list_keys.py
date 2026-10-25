"""The ``capabilities.*`` list keys, through the real evaluator.

Each key parses with the catalog's matcher; a list that names X permits X and
denies Y; a gate pinned off denies every item; no policy, or no list, permits as
before. The call sites each key gates are tested in
``test_governance_list_otlp_hosts.py`` and ``test_governance_list_share_sites.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.platform.governance import CapabilityGate, parse_policy, resolve

#: (scope, inner key, matcher, an item the list allows, an item it does not)
_CASES = (
    (
        "capabilities.telemetry",
        "otlp_hosts",
        "host",
        "otel.corp.example",
        "evil.example",
    ),
    ("capabilities.social_share", "sites", "identifier", "linkedin", "x"),
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
