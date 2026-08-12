"""The inbound gate's three questions: enrolment, member, sender.

The sender leaf is the ceiling counterpart to the per-transport
``allowed_user_ids`` in ``config.json``: config stays in force and the two
intersect, but only the posture lives somewhere the agent cannot rewrite.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

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


def _policy(channels):
    return {"version": 1, "boot": {"fail_closed": True}, "channels": channels}


class TestUngovernedDefault:
    def test_no_policy_permits_an_enrolled_connection(self):
        _install(None)
        assert _channel_inbound_permitted_sync("telegram") is True

    def test_no_policy_still_permits_when_a_sender_is_supplied(self):
        # A build with no `channels` policy must not start rejecting senders just
        # because the caller began passing one.
        _install(None)
        assert _channel_inbound_permitted_sync("telegram", "u_9931") is True


class TestEnrolment:
    def test_an_unenrolled_connection_is_refused_before_policy(
        self, _seed_channel_trust_roster
    ):
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": ["discord/default"]}), encoding="utf-8"
        )
        # Permissive ceiling: only enrolment can be doing the refusing here.
        _install(_policy({"members": {"mode": "deny", "deny": []}}))
        assert _channel_inbound_permitted_sync("telegram") is False
        assert _channel_inbound_permitted_sync("discord") is True

    def test_revoking_enrolment_takes_effect_without_a_restart(
        self, _seed_channel_trust_roster
    ):
        _install(None)
        assert _channel_inbound_permitted_sync("telegram") is True
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": []}), encoding="utf-8"
        )
        assert _channel_inbound_permitted_sync("telegram") is False


class TestSenderPosture:
    def test_posture_allowlist_admits_only_its_senders(self):
        _install(
            _policy(
                {
                    "members": {"mode": "allow", "allow": ["telegram"]},
                    "posture": {
                        "telegram": {
                            "senders": {"mode": "allow", "allow": ["u_9931", "u_4402"]}
                        }
                    },
                }
            )
        )
        assert _channel_inbound_permitted_sync("telegram", "u_9931") is True
        assert _channel_inbound_permitted_sync("telegram", "u_4402") is True
        assert _channel_inbound_permitted_sync("telegram", "u_stranger") is False

    def test_a_caller_that_supplies_no_sender_is_not_blocked_by_the_leaf(self):
        # Not every inbound path knows the sender (a button callback, a channel
        # event). Those keep flowing through the member decision rather than
        # failing a leaf they cannot answer.
        _install(
            _policy(
                {
                    "members": {"mode": "allow", "allow": ["telegram"]},
                    "posture": {
                        "telegram": {"senders": {"mode": "allow", "allow": ["u_9931"]}}
                    },
                }
            )
        )
        assert _channel_inbound_permitted_sync("telegram") is True

    def test_a_denied_member_is_refused_regardless_of_sender(self):
        _install(
            _policy(
                {
                    "members": {"mode": "allow", "allow": ["discord"]},
                    "posture": {
                        "discord": {"senders": {"mode": "allow", "allow": ["u_9931"]}}
                    },
                }
            )
        )
        assert _channel_inbound_permitted_sync("telegram", "u_9931") is False

    def test_no_sender_leaf_means_the_member_decision_stands(self):
        _install(_policy({"members": {"mode": "allow", "allow": ["telegram"]}}))
        assert _channel_inbound_permitted_sync("telegram", "anyone") is True


class TestAudit:
    def test_a_sender_refusal_names_the_posture_leaf(self, monkeypatch):
        import kiro_crew.messaging.identity as ident

        seen: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kw):
                seen.append(kw)

        monkeypatch.setattr(ident, "sel", lambda: _Sel())
        _install(
            _policy(
                {
                    "members": {"mode": "allow", "allow": ["telegram"]},
                    "posture": {
                        "telegram": {"senders": {"mode": "allow", "allow": ["u_9931"]}}
                    },
                }
            )
        )
        assert _channel_inbound_permitted_sync("telegram", "u_stranger") is False
        denials = [r for r in seen if r["outcome"] == "denied"]
        assert len(denials) == 1
        # The item has to name the leaf, or a reader cannot tell a refused SENDER
        # from a refused connection.
        assert denials[0]["item"].endswith("#senders")
        assert "sender" in denials[0]["reason"]

    def test_an_enrolment_refusal_is_attributed_to_the_operator_layer(
        self, monkeypatch, _seed_channel_trust_roster
    ):
        import kiro_crew.messaging.identity as ident

        seen: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kw):
                seen.append(kw)

        monkeypatch.setattr(ident, "sel", lambda: _Sel())
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": []}), encoding="utf-8"
        )
        _install(None)
        assert _channel_inbound_permitted_sync("telegram") is False
        assert len(seen) == 1
        assert seen[0]["rule"] == "trust-roster"
        assert seen[0]["layer"] == "operator"
