"""The connection-governance kill switch.

Two independent switches with different owners: the operator's config field, and
an enterprise ceiling's un-liftable pin. Either being off makes the whole feature
inert, which is how a fleet whose per-surface scoping will arrive as crew members
declines this surface instead of half-adopting it.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers_system import _collect_connections
from kiro_crew.messaging import trust
from kiro_crew.messaging.connections import make_connection
from kiro_crew.messaging.identity import _channel_inbound_permitted_sync
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


def _install(policy_body):
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    ctx_mod.set_context(None)


def _pin_off():
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "capabilities": {"channel_connections": {"enabled": False}},
    }


class TestTheConfigSwitch:
    def test_default_is_on(self):
        from kiro_crew.config.loader import KiroCrewConfig

        assert KiroCrewConfig.load().messaging.connection_governance is True

    def test_off_by_config_disables_the_feature(self):
        cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=False))
        assert trust.feature_enabled(cfg) is False

    def test_on_by_config_keeps_the_feature(self):
        cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=True))
        assert trust.feature_enabled(cfg) is True


class TestTheFleetPin:
    def test_a_policy_pin_disables_the_feature(self):
        _install(_pin_off())
        cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=True))
        assert trust.feature_enabled(cfg) is False

    def test_no_policy_leaves_the_feature_on(self):
        _install(None)
        cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=True))
        assert trust.feature_enabled(cfg) is True

    def test_a_profile_denial_is_not_a_pin(self):
        # This is a process-wide feature switch, not a per-surface permission, so
        # only the POLICY layer may turn it off — the same rule the telemetry pin
        # follows. A profile-shaped denial must not silently disable a host-wide
        # feature for one surface.
        from kiro_crew.platform.governance import parse_profile

        base_cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=True))
        from kiro_crew.config.loader import KiroCrewConfig

        base = build_default_context(KiroCrewConfig.load())
        profile = parse_profile(
            {"name": "surface-x", "capabilities": {"channel_connections": {"enabled": False}}}
        )
        ctx_mod.set_context(dataclasses.replace(base, governance=None))
        # A profile alone (no policy) must not read as pinned.
        assert profile.get("capabilities.channel_connections") is not None
        assert trust.feature_enabled(base_cfg) is True


class TestDisabledIsInert:
    def test_an_unenrolled_connection_still_attaches_when_disabled(
        self, monkeypatch, _seed_channel_trust_roster
    ):
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": []}), encoding="utf-8"
        )
        _install(_pin_off())
        from kiro_crew.slack import gateway as gw

        # Enrolment is not consulted, so the pre-roster behaviour is restored: the
        # `channels` ceiling alone decides, and it permits by default.
        assert gw._channel_transport_permitted("telegram") is True

    def test_inbound_is_not_gated_on_enrolment_when_disabled(
        self, _seed_channel_trust_roster
    ):
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": []}), encoding="utf-8"
        )
        _install(_pin_off())
        assert _channel_inbound_permitted_sync("telegram") is True

    def test_the_sender_leaf_is_not_consulted_when_disabled(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"channel_connections": {"enabled": False}},
                "channels": {
                    "members": {"mode": "allow", "allow": ["telegram"]},
                    "posture": {
                        "telegram": {"senders": {"mode": "allow", "allow": ["u_9931"]}}
                    },
                },
            }
        )
        assert _channel_inbound_permitted_sync("telegram", "u_stranger") is True

    def test_the_seed_writes_nothing_when_disabled(self, _seed_channel_trust_roster):
        # The file's PRESENCE is the "intended open" signal, so a disabled fleet
        # must not be left one to misread later.
        _seed_channel_trust_roster.unlink()
        (_seed_channel_trust_roster.parent / ".migrations" / "channel_trust_seeded").unlink()
        _install(_pin_off())
        assert trust.seed_roster([make_connection("telegram")]) is False
        assert _seed_channel_trust_roster.exists() is False

    def test_the_read_model_reports_disabled(self):
        _install(_pin_off())
        assert _collect_connections()["enabled"] is False

    def test_the_read_model_reports_enabled_by_default(self):
        _install(None)
        assert _collect_connections()["enabled"] is True


class TestTheSwitchItselfIsNotFailClosed:
    def test_an_unevaluable_ceiling_leaves_the_feature_on(self, monkeypatch):
        # Every GATE this feature adds fails closed, because refusing on doubt
        # costs one blocked message. This switch must not: refusing on doubt would
        # stop EVERY channel on a host that never opted out, turning an unrelated
        # governance hiccup into a total outage.
        import kiro_crew.messaging.trust as trust_mod

        def _boom(*_a, **_k):
            raise RuntimeError("ceiling unevaluable")

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits", _boom
        )
        cfg = SimpleNamespace(messaging=SimpleNamespace(connection_governance=True))
        assert trust_mod.feature_enabled(cfg) is True
