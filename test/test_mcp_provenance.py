"""The authorship marker: shape, decision matrix, and lifecycle.

Ownership of an entry in a shared MCP config file is "carries our marker", not
"the name is in the store". These tests pin the predicate and the three decisions
built on it; the end-to-end behaviour at each write surface lives in
``test_mcp_sync_agent.py``.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.mcp_provenance import (
    ABSENT,
    MARKER_KEY,
    is_marked,
    resolve_write,
    stamp,
    without_marker,
)


class TestMarkerShape:
    """Only the exact shape counts as ours.

    The predicate decides whether we may overwrite a file we do not own, so
    every ambiguous value has to fall on the "leave it alone" side. A marker we
    cannot read is a marker we did not write.
    """

    @pytest.mark.parametrize(
        "entry",
        [
            {"url": "https://x"},
            {"url": "https://x", MARKER_KEY: None},
            {"url": "https://x", MARKER_KEY: True},
            {"url": "https://x", MARKER_KEY: "managed"},
            {"url": "https://x", MARKER_KEY: []},
            {"url": "https://x", MARKER_KEY: {}},
            {"url": "https://x", MARKER_KEY: {"managed": "yes"}},
            {"url": "https://x", MARKER_KEY: {"managed": 1}},
            {"url": "https://x", MARKER_KEY: {"managed": False}},
            "not-a-dict",
            None,
        ],
    )
    def test_anything_but_the_exact_shape_reads_as_the_users(self, entry):
        assert is_marked(entry) is False

    def test_the_exact_shape_reads_as_ours(self):
        assert is_marked(stamp({"url": "https://x"})) is True

    def test_stamping_does_not_mutate_its_input(self):
        """Callers pass the entry they are still comparing against."""
        original = {"url": "https://x"}
        stamped = stamp(original)
        assert original == {"url": "https://x"}
        assert is_marked(stamped)

    def test_stamping_replaces_a_malformed_marker(self):
        """The written marker is always the exact shape.

        A candidate is built by copying the on-disk entry, so an unreadable
        marker value can ride along into a write. Stamping normalizes it rather
        than preserving it, so what lands on disk is always readable back.
        """
        assert is_marked(stamp({"url": "https://x", MARKER_KEY: "garbage"}))

    @pytest.mark.parametrize("entry", ["not-a-dict", None, 7, []])
    def test_without_marker_tolerates_a_non_dict(self, entry):
        """Comparisons run before any shape re-check, so this cannot raise."""
        assert without_marker(entry) == {}

    def test_without_marker_keeps_everything_else(self):
        entry = stamp({"url": "https://x", "headers": {"A": "b"}})
        assert without_marker(entry) == {"url": "https://x", "headers": {"A": "b"}}


class TestResolveWriteDecisions:
    """The three outcomes, plus the states that must fall into decline."""

    _CANDIDATE = {"url": "https://new.example.net/mcp"}

    def _resolve(self, on_disk, *, store_managed=True, candidate=None):
        return resolve_write(
            name="srv",
            on_disk=on_disk,
            candidate=dict(candidate if candidate is not None else self._CANDIDATE),
            store_managed=store_managed,
            surface="~/.kiro/settings/mcp.json",
        )

    def test_a_create_for_a_managed_name_is_stamped(self):
        """We are authoring the entry, so the record says so."""
        resolved = self._resolve(ABSENT)
        assert resolved is not None and is_marked(resolved)
        assert without_marker(resolved) == self._CANDIDATE

    def test_a_create_for_an_unmanaged_name_is_not_stamped(self):
        """A marker on a name we do not manage would claim an entry no later
        write is allowed to touch anyway, so it is not written."""
        resolved = self._resolve(ABSENT, store_managed=False)
        assert resolved == self._CANDIDATE
        assert not is_marked(resolved)

    @pytest.mark.parametrize("on_disk", ["not-a-dict", None, [], 7])
    def test_a_present_but_unreadable_entry_is_declined(self, on_disk, caplog):
        """Present-and-malformed is not absent, and cannot be ours.

        A string, ``null`` or a list cannot carry the marker, so by the invariant
        it reads as unmarked -- the user's. Only true absence is a create: a value
        we cannot parse is a value we cannot prove we wrote, and overwriting it
        would destroy whatever the user meant by it. ``None`` is in here on
        purpose -- a JSON ``null`` under the name is a value the user typed, which
        is why absence needs its own signal rather than borrowing ``None``.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_provenance"):
            assert self._resolve(on_disk) is None
        assert "srv" in caplog.text

    @pytest.mark.parametrize("on_disk", ["not-a-dict", None, [], 7])
    def test_an_unmanaged_name_with_an_unreadable_entry_is_left_alone(self, on_disk):
        """The store-side gate answers first, so nothing is written either way."""
        assert self._resolve(on_disk, store_managed=False) is None

    def test_an_unmanaged_name_with_an_existing_entry_is_declined(self):
        """The store-side half stays a necessary precondition."""
        assert self._resolve({"url": "https://theirs"}, store_managed=False) is None

    def test_a_marked_entry_is_rewritten_and_stays_marked(self):
        """The propagation the marker exists to make safe."""
        resolved = self._resolve(stamp({"url": "https://old.example/mcp"}))
        assert resolved is not None and is_marked(resolved)
        assert without_marker(resolved) == self._CANDIDATE

    def test_a_stripped_marker_is_not_re_stamped(self):
        """Reclamation has to survive the next sync.

        Stripping the marker off an entry we wrote leaves EXACTLY our emit, so
        "unmarked and byte-equal to our candidate" is the same disk state as
        "written before the marker existed". Stamping on that match would migrate
        the second and silently take back the first, and no content test
        separates them -- so neither is written. The trade is documented in the
        module docstring and the design note.
        """
        resolved = self._resolve(dict(self._CANDIDATE))
        assert resolved is None

    def test_an_entry_carrying_a_malformed_marker_is_declined(self):
        """A marker we cannot read is a marker we did not write.

        Previously this stamped, because the content beside the malformed key
        matched our emit. It is unmarked, so it is the user's.
        """
        assert self._resolve({**self._CANDIDATE, MARKER_KEY: "garbage"}) is None

    def test_an_unmarked_divergent_entry_is_declined_and_logged(self, caplog):
        """The case the overridden finding described.

        Nothing proves we wrote it and the sync would change it, so it is the
        user's. The log line is the only trace, and it names the server so a
        silent divergence is diagnosable.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_provenance"):
            assert self._resolve({"url": "https://theirs.example.com/mcp"}) is None
        assert "srv" in caplog.text
        assert "~/.kiro/settings/mcp.json" in caplog.text

    def test_content_equality_does_not_widen_the_decline(self):
        """One property, stated once: presence + unmarked is enough to decline.

        Key order, nesting depth and exact byte-equality all used to steer a
        stamping branch. With that branch gone there is nothing for them to
        steer, and this pins that no future comparison re-enters through them.
        """
        for on_disk in (
            {"headers": {"A": "b"}, "url": "https://u"},
            {"url": "https://u", "headers": {"A": "b"}},
            {"url": "https://u", "headers": {"Authorization": "Bearer theirs"}},
        ):
            candidate = {"url": "https://u", "headers": {"A": "b"}}
            assert self._resolve(on_disk, candidate=candidate) is None


class TestOwnershipIsPerFileNotPerName:
    """A name-set cannot answer for two surfaces at once.

    ``kirocrew_managed_names`` stays name-only on purpose: the same managed name
    can be ours in the kiro-global file and the user's in the sidecar, so folding
    marker state into the set would make it answer for whichever surface happened
    to be checked last.
    """

    def test_the_same_name_resolves_differently_per_surface(self):
        candidate = {"url": "https://new.example.net/mcp"}
        marked_surface = resolve_write(
            name="srv",
            on_disk=stamp({"url": "https://old.example/mcp"}),
            candidate=dict(candidate),
            store_managed=True,
            surface="~/.kiro/settings/mcp.json",
        )
        unmarked_surface = resolve_write(
            name="srv",
            on_disk={"url": "https://old.example/mcp"},
            candidate=dict(candidate),
            store_managed=True,
            surface="~/.mcp.json",
        )
        assert marked_surface is not None, "ours here"
        assert unmarked_surface is None, "theirs there, same name and same store"

    def test_the_store_side_predicate_keeps_its_name_only_contract(self):
        """PR3 consumes this signature; the marker did not change it."""
        from unittest.mock import patch

        from kiro_crew.mcp_discovery import SCOPE_KIROCREW, kirocrew_managed_names

        with patch(
            "kiro_crew.mcp_discovery._load_mcp_json_by_source",
            return_value={
                SCOPE_KIROCREW: {
                    "plain": {"url": "https://x"},
                    "marked": stamp({"url": "https://y"}),
                    "bad": "not-a-dict",
                }
            },
        ):
            assert kirocrew_managed_names() == {"plain", "marked"}
