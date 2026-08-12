"""The channel trust roster: a credential is not a permission.

Covers the gate that decides whether a chat connection may attach at all —
separate from the ``channels`` ceiling, which decides what it may then do.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.messaging import trust
from kiro_crew.messaging.connections import make_connection


@pytest.fixture
def roster_file(_seed_channel_trust_roster):
    """The autouse-seeded roster path, for tests that rewrite or remove it."""
    return _seed_channel_trust_roster


def _write(path, connections, version=trust.ROSTER_VERSION):
    path.write_text(
        json.dumps({"version": version, "connections": connections}, indent=2) + "\n",
        encoding="utf-8",
    )


class TestLoad:
    def test_seeded_roster_admits_its_listed_connections(self, roster_file):
        _write(roster_file, ["telegram/default", "slack/default"])
        roster = trust.load_roster()
        assert roster.loaded is True
        assert roster.admits(make_connection("telegram")) is True
        assert roster.admits(make_connection("slack")) is True
        assert roster.admits(make_connection("discord")) is False

    def test_absent_roster_admits_nothing_and_says_why(self, roster_file):
        roster_file.unlink()
        roster = trust.load_roster()
        # An empty roster because we FAILED CLOSED must be distinguishable from an
        # operator who trusts nothing, or a status surface lies to a human.
        assert roster.loaded is False
        assert roster.error == "absent"
        assert roster.admits(make_connection("telegram")) is False

    def test_unreadable_roster_fails_closed(self, roster_file):
        roster_file.write_text("{not json", encoding="utf-8")
        roster = trust.load_roster()
        assert roster.loaded is False
        assert roster.error == "unreadable"
        assert roster.admits(make_connection("telegram")) is False

    def test_unknown_schema_version_fails_closed(self, roster_file):
        _write(roster_file, ["telegram/default"], version=999)
        roster = trust.load_roster()
        assert roster.loaded is False
        assert roster.error == "version_mismatch"

    def test_bare_transport_entry_means_the_default_connection(self, roster_file):
        # An operator writing the terse form gets the default connection, not a
        # connection literally named "telegram".
        _write(roster_file, ["telegram"])
        assert trust.load_roster().admits(make_connection("telegram")) is True

    def test_object_entries_may_carry_a_note(self, roster_file):
        _write(roster_file, [{"id": "telegram/default", "note": "ops bot"}])
        assert trust.load_roster().admits(make_connection("telegram")) is True

    def test_one_malformed_entry_does_not_disable_the_rest(self, roster_file):
        # A typo must not take every other connection offline with it.
        _write(roster_file, [{"nope": 1}, "telegram/default", "", "bad/NAME"])
        roster = trust.load_roster()
        assert roster.loaded is True
        assert roster.admits(make_connection("telegram")) is True

    def test_a_named_connection_is_not_admitted_by_its_sibling(self, roster_file):
        _write(roster_file, ["telegram/ops-bot"])
        roster = trust.load_roster()
        assert roster.admits(make_connection("telegram", "ops-bot")) is True
        assert roster.admits(make_connection("telegram", "raymond")) is False
        # The roster is exact per connection: enrolling one bot must not enrol the
        # transport's default connection too.
        assert roster.admits(make_connection("telegram")) is False


class TestSeed:
    def test_seed_writes_the_configured_connections(self, roster_file):
        roster_file.unlink()
        (roster_file.parent / ".migrations" / "channel_trust_seeded").unlink()
        assert trust.seed_roster([make_connection("telegram"), make_connection("slack")]) is True
        roster = trust.load_roster()
        assert roster.admits(make_connection("telegram")) is True
        assert roster.admits(make_connection("slack")) is True
        assert roster.admits(make_connection("discord")) is False

    def test_seed_is_one_shot_and_does_not_resurrect_a_deleted_roster(self, roster_file):
        roster_file.unlink()
        (roster_file.parent / ".migrations" / "channel_trust_seeded").unlink()
        assert trust.seed_roster([make_connection("telegram")]) is True
        roster_file.unlink()
        # A roster deleted AFTER the seed is the tamper signal the loader fails
        # closed on; re-creating it here would erase that signal.
        assert trust.seed_roster([make_connection("telegram")]) is False
        assert roster_file.exists() is False
        assert trust.load_roster().loaded is False

    def test_seed_never_clobbers_an_operator_authored_roster(self, roster_file):
        (roster_file.parent / ".migrations" / "channel_trust_seeded").unlink()
        _write(roster_file, ["slack/default"])
        assert trust.seed_roster([make_connection("telegram")]) is False
        roster = trust.load_roster()
        assert roster.admits(make_connection("slack")) is True
        assert roster.admits(make_connection("telegram")) is False


class TestStartGate:
    def test_an_unenrolled_connection_does_not_start(self, roster_file):
        _write(roster_file, ["discord/default"])
        from kiro_crew.slack.gateway import _channel_transport_permitted

        assert _channel_transport_permitted("telegram") is False
        assert _channel_transport_permitted("discord") is True

    def test_enrolment_refusal_is_audited_as_the_operator_layer(self, roster_file, monkeypatch):
        _write(roster_file, [])
        import kiro_crew.slack.gateway as gw

        seen: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kw):
                seen.append(kw)

        monkeypatch.setattr(gw, "sel", lambda: _Sel())
        assert gw._channel_transport_permitted("telegram") is False
        assert len(seen) == 1
        rec = seen[0]
        # The reason must name enrolment, not policy: an operator reading the trail
        # has to be able to tell "nobody allowed this bot" from "the ceiling
        # refused it".
        assert rec["outcome"] == "denied"
        assert rec["item"] == "telegram/default"
        assert rec["rule"] == "trust-roster"
        assert rec["layer"] == "operator"
        assert "not enrolled" in rec["reason"]

    def test_an_absent_roster_stops_every_transport(self, roster_file):
        roster_file.unlink()
        from kiro_crew.slack.gateway import _channel_transport_permitted

        for member in ("slack", "telegram", "discord", "webex", "wecom", "teams", "weixin"):
            assert _channel_transport_permitted(member) is False


class TestTrustRootIsFenced:
    def test_the_roster_is_on_the_sensitive_path_floor(self):
        # The agent must not be able to read or rewrite the list of principals
        # allowed to talk to it.
        from kiro_crew import security

        assert any(
            entry.endswith("channel_trust.json") for entry in security._SENSITIVE_HOME_DIRS
        )

    def test_the_agent_cannot_write_the_roster(self):
        from kiro_crew import security

        assert security.is_sensitive_path(str(trust.roster_path())) is True
