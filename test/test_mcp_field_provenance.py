"""The command-derivation record: what the rebuild computed, and from what.

The agent spec is both this rebuild's output and an input to its next run, so the
resolved absolute ``command`` in it must not read back as one the user wrote. These
tests pin the record's shape, the ownership guard that keeps it from reverting a
hand edit, and the restore that lets a computed value be derived again.

The record applies only to a server no other config source declares. The
end-to-end consequences of that boundary live in
``test_mcp_rebuild_reconsumption.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_provenance import (
    DERIVED_KEY,
    MARKER_KEY,
    command_is_ours,
    record_derived,
    source_view,
)


def _rec(source: str | None, emitted: str) -> dict:
    return {DERIVED_KEY: {"from": source, "emitted": emitted}}


class TestRecordShape:
    """What gets written, and what declining looks like."""

    def test_a_recorded_source_round_trips(self) -> None:
        entry = record_derived({"command": "/abs/npx"}, ("npx", "/abs/npx"))
        assert entry[DERIVED_KEY] == {"from": "npx", "emitted": "/abs/npx"}
        # The computed value stays in the entry -- the record describes it, it does
        # not replace it. kiro-cli reads ``command``, not the record.
        assert entry["command"] == "/abs/npx"

    def test_none_writes_no_record(self) -> None:
        """How a caller declines to claim a field it did not author."""
        assert DERIVED_KEY not in record_derived({"command": "/abs/npx"}, None)

    def test_recording_replaces_a_previous_record(self) -> None:
        """Each emission describes itself; a stale record must not accumulate."""
        first = record_derived({"command": "/a"}, ("one", "/a"))
        second = record_derived(first, ("two", "/b"))
        assert second[DERIVED_KEY] == {"from": "two", "emitted": "/b"}

    def test_the_record_is_not_the_authorship_marker(self) -> None:
        """Two keys, two questions. The marker's invariant excludes this file."""
        assert DERIVED_KEY != MARKER_KEY
        assert MARKER_KEY not in record_derived({"command": "/a"}, ("npx", "/a"))


class TestSourceViewRestores:
    """Reading back through the view yields the source, not the emission."""

    def test_a_computed_command_is_restored(self) -> None:
        emitted = record_derived({"command": "/abs/npx"}, ("npx", "/abs/npx"))
        assert source_view(emitted)["command"] == "npx"

    def test_the_record_is_stripped_from_the_view(self) -> None:
        emitted = record_derived({"command": "/a"}, ("npx", "/a"))
        assert DERIVED_KEY not in source_view(emitted)

    def test_a_user_field_is_untouched(self) -> None:
        """The record covers our field only; everything else survives verbatim."""
        emitted = record_derived(
            {"command": "/abs/npx", "args": ["--x"], "env": {"TOKEN": "t"}, "disabled": True},
            ("npx", "/abs/npx"),
        )
        view = source_view(emitted)
        assert view["args"] == ["--x"]
        assert view["env"] == {"TOKEN": "t"}
        assert view["disabled"] is True


class TestOwnershipGuard:
    """The field is ours only while it still holds what we emitted.

    Without this the restore would revert hand edits, which is worse than the bug it
    fixes. Same rule the entry-level marker applies, at field granularity.
    """

    def test_an_edited_command_is_left_alone(self) -> None:
        entry = {"command": "/user/choice"}
        entry.update(_rec("npx", "/abs/npx"))
        assert source_view(entry)["command"] == "/user/choice"

    def test_a_removed_field_is_not_reinstated(self) -> None:
        """The user deleting a field we wrote is an edit like any other."""
        entry: dict = {}
        entry.update(_rec("npx", "/abs/npx"))
        assert "command" not in source_view(entry)

    def test_command_is_ours_judges_the_same_way(self) -> None:
        ours = record_derived({"command": "/abs/npx"}, ("npx", "/abs/npx"))
        assert command_is_ours(ours) is True
        edited = {"command": "/user/choice"}
        edited.update(_rec("npx", "/abs/npx"))
        assert command_is_ours(edited) is False

    def test_command_is_ours_needs_a_record(self) -> None:
        assert command_is_ours({"command": "/abs/npx"}) is False
        assert command_is_ours("nope") is False


class TestARestoreNeverRemovesTheCommand:
    """The reader's contract is fail-open, so it must never cost a server.

    A restore that deletes or empties the command leaves nothing to resolve, and the
    rebuild then drops the server from the emitted config. So a record whose source
    is blank or absent is not "a record meaning remove" -- it is unreadable, and an
    unreadable record degrades to the behaviour that predates the key.
    """

    @pytest.mark.parametrize(
        "field",
        [
            {"emitted": "/abs/npx"},  # no ``from`` at all
            {"from": None, "emitted": "/abs/npx"},
            {"from": "", "emitted": "/abs/npx"},
            {"from": "npx", "emitted": ""},  # no usable ownership proof either
        ],
    )
    def test_a_blank_or_absent_source_is_unreadable(self, field: dict) -> None:
        entry = {"command": "/abs/npx", DERIVED_KEY: field}
        view = source_view(entry)
        assert view["command"] == "/abs/npx"
        assert command_is_ours(entry) is False

    def test_a_readable_record_always_yields_a_command(self) -> None:
        emitted = record_derived({"command": "/abs/npx"}, ("npx", "/abs/npx"))
        assert source_view(emitted)["command"]


class TestFailsOpenToPreviousBehaviour:
    """No readable record means "consume the stored value", exactly as before.

    A config written by an older build carries no record, and a user can mangle one.
    Neither may cost the user a server, so every unreadable shape degrades to the
    behaviour that predates the record rather than to a removed field.
    """

    @pytest.mark.parametrize(
        "record",
        [
            None,
            True,
            "command",
            [],
            {},
            None,
            7,
            "npx",
            # No ``emitted``: nothing proves the field is ours, so it is not touched.
            {"from": "npx"},
            {"from": "npx", "emitted": 7},
            {"from": ["npx"], "emitted": "/abs/npx"},
            # The old multi-field shape: unreadable now, and must not crash.
            # Earlier nested shapes: unreadable now, and must not crash.
            {"fields": {"command": {"from": "npx", "emitted": "/abs/npx"}}},
            {"command": {"from": "npx", "emitted": "/abs/npx"}},
        ],
    )
    def test_an_unreadable_record_leaves_the_stored_value(self, record: object) -> None:
        view = source_view({"command": "/abs/npx", DERIVED_KEY: record})
        assert view["command"] == "/abs/npx"
        assert DERIVED_KEY not in view

    def test_an_entry_with_no_record_passes_through(self) -> None:
        assert source_view({"command": "/abs/npx"}) == {"command": "/abs/npx"}

    def test_a_non_dict_entry_is_tolerated(self) -> None:
        assert source_view("nope") == {}
