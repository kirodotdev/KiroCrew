"""GET /api/connections — the read model behind the connection surfaces."""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.dashboard.handlers_system import _collect_connections
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


def _by_id(payload):
    return {c["id"]: c for c in payload["connections"]}


class TestShape:
    def test_every_builtin_transport_reports_its_default_connection(self):
        _install(None)
        rows = _by_id(_collect_connections())
        for transport in ("slack", "telegram", "discord", "webex", "wecom", "teams", "weixin"):
            assert f"{transport}/default" in rows
        row = rows["telegram/default"]
        assert row["transport"] == "telegram"
        assert row["name"] == "default"

    def test_ungoverned_default_is_enrolled_permitted_and_unpinned(self):
        _install(None)
        row = _by_id(_collect_connections())["telegram/default"]
        assert row["enrolled"] is True
        assert row["permitted"] is True
        assert row["senders_pinned"] is False


class TestRosterState:
    def test_a_readable_roster_reports_loaded(self):
        _install(None)
        assert _collect_connections()["roster"]["loaded"] is True

    def test_an_absent_roster_is_reported_as_unloaded_not_as_trusting_nobody(
        self, _seed_channel_trust_roster
    ):
        # The UI must be able to say "the roster could not be read" rather than
        # rendering a fail-closed instance as an operator who enrolled no one.
        _seed_channel_trust_roster.unlink()
        _install(None)
        payload = _collect_connections()
        assert payload["roster"]["loaded"] is False
        assert payload["roster"]["error"] == "absent"
        assert all(c["enrolled"] is False for c in payload["connections"])

    def test_the_roster_path_is_reported_so_the_ui_can_name_it(self):
        _install(None)
        assert _collect_connections()["roster"]["path"].endswith("channel_trust.json")


class TestEnrolment:
    def test_only_enrolled_connections_are_marked_enrolled(self, _seed_channel_trust_roster):
        _seed_channel_trust_roster.write_text(
            json.dumps({"version": 1, "connections": ["telegram/default"]}), encoding="utf-8"
        )
        _install(None)
        rows = _by_id(_collect_connections())
        assert rows["telegram/default"]["enrolled"] is True
        assert rows["discord/default"]["enrolled"] is False


class TestCeiling:
    def test_a_denied_connection_reports_permitted_false(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram"]}},
            }
        )
        rows = _by_id(_collect_connections())
        assert rows["telegram/default"]["permitted"] is True
        assert rows["discord/default"]["permitted"] is False

    def test_a_pinned_sender_leaf_is_reported(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {
                    "members": {"mode": "allow", "allow": ["telegram", "discord"]},
                    "posture": {
                        "telegram": {"senders": {"mode": "allow", "allow": ["u_9931"]}}
                    },
                },
            }
        )
        rows = _by_id(_collect_connections())
        assert rows["telegram/default"]["senders_pinned"] is True
        # A transport with no posture leaf is NOT reported as pinned — otherwise
        # every connection would read as restricted the moment one of them is.
        assert rows["discord/default"]["senders_pinned"] is False

    def test_the_deciding_layer_is_reported(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram"]}},
            }
        )
        assert _by_id(_collect_connections())["telegram/default"]["layer"] == "policy"
