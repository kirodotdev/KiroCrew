"""The enrol / revoke write surface for the channel trust roster.

The roster is a keystone file the AGENT cannot touch; these endpoints are how the
OPERATOR edits it without hand-writing JSON. Same shape as the denied-commands
surface next door: refuse to overwrite a corrupt file, idempotent, audited.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard.handlers.channel_trust import (
    RosterCorruptError,
    _entry_id,
    _read_for_mutation,
    _write_roster,
)
from kiro_crew.messaging import trust
from kiro_crew.messaging.connections import make_connection


@pytest.fixture
def roster(_seed_channel_trust_roster):
    return _seed_channel_trust_roster


def _write(path, connections):
    path.write_text(
        json.dumps({"version": 1, "connections": connections}, indent=2) + "\n",
        encoding="utf-8",
    )


def _enrol(item, note=""):
    def _mutate(doc):
        entries = doc.get("connections") or []
        if any(_entry_id(e) == item for e in entries):
            return
        entries.append({"id": item, "note": note} if note else {"id": item})
        doc["connections"] = entries

    return _mutate


def _revoke(item):
    from kiro_crew.messaging.connections import parse_item

    def _mutate(doc):
        kept = []
        for entry in doc.get("connections") or []:
            raw = _entry_id(entry)
            normalized = parse_item(raw).governance_item() if raw else ""
            if normalized != item:
                kept.append(entry)
        doc["connections"] = kept

    return _mutate


class TestEnrol:
    @pytest.mark.asyncio
    async def test_enrolling_lets_the_connection_attach(self, roster):
        _write(roster, [])
        assert trust.load_roster().admits(make_connection("telegram")) is False
        await _write_roster(_enrol("telegram/default"))
        # No restart: the gates read the roster per decision, so the write IS the
        # control taking effect.
        assert trust.load_roster().admits(make_connection("telegram")) is True

    @pytest.mark.asyncio
    async def test_enrolling_twice_does_not_duplicate(self, roster):
        _write(roster, [])
        await _write_roster(_enrol("telegram/default"))
        await _write_roster(_enrol("telegram/default"))
        doc = _read_for_mutation()
        assert [_entry_id(e) for e in doc["connections"]] == ["telegram/default"]

    @pytest.mark.asyncio
    async def test_an_operator_note_is_preserved(self, roster):
        _write(roster, [])
        await _write_roster(_enrol("telegram/default", "ops bot, read-only"))
        doc = _read_for_mutation()
        assert doc["connections"][0]["note"] == "ops bot, read-only"

    @pytest.mark.asyncio
    async def test_the_file_keeps_its_schema_version(self, roster):
        roster.unlink()
        await _write_roster(_enrol("telegram/default"))
        assert _read_for_mutation()["version"] == trust.ROSTER_VERSION
        assert trust.load_roster().loaded is True


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoking_stops_the_connection_attaching(self, roster):
        _write(roster, ["telegram/default"])
        await _write_roster(_revoke("telegram/default"))
        assert trust.load_roster().admits(make_connection("telegram")) is False

    @pytest.mark.asyncio
    async def test_revoking_matches_the_terse_spelling_too(self, roster):
        # An operator may have hand-written the bare transport. Revoking
        # `telegram/default` must still remove it, or the control silently no-ops
        # against a roster written in the other spelling.
        _write(roster, ["telegram"])
        await _write_roster(_revoke("telegram/default"))
        assert trust.load_roster().admits(make_connection("telegram")) is False

    @pytest.mark.asyncio
    async def test_revoking_leaves_other_connections_alone(self, roster):
        _write(roster, ["telegram/default", "slack/default"])
        await _write_roster(_revoke("telegram/default"))
        loaded = trust.load_roster()
        assert loaded.admits(make_connection("slack")) is True
        assert loaded.admits(make_connection("telegram")) is False

    @pytest.mark.asyncio
    async def test_revoking_something_absent_is_a_no_op(self, roster):
        _write(roster, ["slack/default"])
        await _write_roster(_revoke("telegram/default"))
        assert trust.load_roster().admits(make_connection("slack")) is True


class TestCorruptRosterIsNotOverwritten:
    def test_reading_a_corrupt_roster_raises(self, roster):
        roster.write_text("{not json", encoding="utf-8")
        with pytest.raises(RosterCorruptError):
            _read_for_mutation()

    @pytest.mark.asyncio
    async def test_a_corrupt_roster_is_left_byte_for_byte(self, roster):
        # Rewriting a file we could not read would discard whatever an operator or
        # a fleet push had put there, so the write refuses and the human resolves.
        original = "{not json"
        roster.write_text(original, encoding="utf-8")
        with pytest.raises(RosterCorruptError):
            await _write_roster(_enrol("telegram/default"))
        assert roster.read_text(encoding="utf-8") == original

    def test_a_non_object_root_is_corrupt(self, roster):
        roster.write_text("[]", encoding="utf-8")
        with pytest.raises(RosterCorruptError):
            _read_for_mutation()


class TestTheFileStaysAKeystone:
    @pytest.mark.asyncio
    async def test_the_written_file_is_owner_only(self, roster):
        import os
        import stat

        _write(roster, [])
        await _write_roster(_enrol("telegram/default"))
        mode = stat.S_IMODE(os.stat(roster).st_mode)
        # 0600, like the other keystone secrets: owner-only does not isolate a
        # same-UID process, but it is the floor every credential store here holds.
        assert mode == 0o600

    def test_the_path_is_still_agent_fenced(self):
        from kiro_crew import security

        assert security.is_sensitive_path(str(trust.roster_path())) is True
