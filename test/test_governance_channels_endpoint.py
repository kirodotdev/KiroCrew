"""Tests for the read-only ``GET /api/governance/channels`` surface.

The endpoint returns the effective per-channel ``channels`` policy decision
(``{channel_type: bool}``) that the Settings UI uses to grey out / disable a
policy-denied channel tab ("Off by admin"). Byte-identical default: with NO
policy governing ``channels`` (the standard OSS build) every member is
``true`` and the UI is unchanged from today.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.dashboard import handlers_system
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _install(policy_body):
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


# The canonical members, derived from the transports' channel_type attrs.
EXPECTED_MEMBERS = {
    "slack",
    "discord",
    "telegram",
    "webex",
    "wecom",
    "teams",
    "weixin",
    "imessage",
    "whatsapp",
}


class TestChannelMembers:
    def test_members_match_transport_channel_types(self):
        # Derived from the transport class attrs — never a hardcoded divergent list.
        assert set(handlers_system._channel_members()) == EXPECTED_MEMBERS


class TestNoPolicyDefault:
    def test_all_true_when_ungoverned(self):
        # Byte-identical default: no policy → every channel permitted (all-true),
        # so the UI is unchanged (every tab fully enabled).
        _install(None)
        result = handlers_system._collect_channel_governance()
        assert result == {m: True for m in EXPECTED_MEMBERS}
        assert all(result.values())

    def test_all_true_when_policy_omits_channels(self):
        # A policy that governs other scopes but NOT channels also leaves every
        # channel permitted (no channels ScopedMap → ungoverned member).
        _install(
            {"version": 1, "boot": {"fail_closed": True}, "apps": {"mode": "allow", "allow": ["x"]}}
        )
        result = handlers_system._collect_channel_governance()
        assert result == {m: True for m in EXPECTED_MEMBERS}


class TestPolicyDenies:
    def test_allow_only_slack_denies_others(self):
        # allow=[slack] → slack true, every other channel denied (false).
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
        result = handlers_system._collect_channel_governance()
        assert result["slack"] is True
        assert result["discord"] is False
        assert result["telegram"] is False
        assert result["webex"] is False
        assert result["wecom"] is False

    def test_deny_specific_channel(self):
        # deny-mode: only the listed channel is denied; the rest stay permitted.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "deny", "deny": ["discord"]}},
            }
        )
        result = handlers_system._collect_channel_governance()
        assert result["discord"] is False
        assert result["slack"] is True
        assert result["telegram"] is True
        assert result["webex"] is True
        assert result["wecom"] is True

    def test_eval_error_is_null_not_denied(self, monkeypatch):
        # MEDIUM (GPT round-6 pass 3): a transient governance-EVALUATION error must
        # surface as null ("unavailable"), NOT False ("Off by admin") — mislabeling
        # a transient failure as an explicit admin denial is misleading. The
        # fail-closed degrade Decision carries rule="default" + a
        # GOVERNANCE_ERROR_REASON reason, which the collector maps to null. Build the
        # reason from the shared constant so this test can't drift from the prose the
        # evaluator actually emits (the whole point of exporting the constant).
        from kiro_crew.platform.governance import Decision
        from kiro_crew.platform.governance_profiles import GOVERNANCE_ERROR_REASON

        def _degraded(scope, item, *, session_key="", fail_closed=False, **kw):
            # Mirrors governance_permits' fail-closed degrade Decision.
            return Decision(
                False, f"{GOVERNANCE_ERROR_REASON}; denied (fail-closed)", rule="default"
            )

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _degraded)
        result = handlers_system._collect_channel_governance()
        # Every member reports null (unavailable), never False (a real policy deny).
        assert all(v is None for v in result.values()), result
        assert not any(v is False for v in result.values())


class TestSessionKey:
    def test_uses_host_session_key(self, monkeypatch):
        # Every member must be evaluated with session_key=HOST_SESSION_KEY (the
        # host surface, matching the messaging chokepoint + app-activation gate).
        from kiro_crew.platform.governance import Decision

        seen: list[str] = []

        def _spy(scope, item, *, session_key="", **kw):
            seen.append(session_key)
            return Decision(True, "spy", rule="default")

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _spy)
        handlers_system._collect_channel_governance()
        assert seen  # called at least once
        assert all(sk == gp.HOST_SESSION_KEY for sk in seen)
        assert len(seen) == len(EXPECTED_MEMBERS)
