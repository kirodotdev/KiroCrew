"""note_tab_id updates the tab_id chain index in place without hiding sibling keys.

The slot-save path used to call invalidate_tab_id_cache() on every save, which threw the whole
tab_id -> [keys] index away and made the next chained read re-glob the session directory and
re-open every dashboard_chat-*.jsonl to rebuild a mapping a content-only save never changed.
note_tab_id updates just the affected entry instead.

The hazard it has to avoid is appending onto an entry that is present but empty. That forges a
one-key index entry which reads as authoritative, so the chained read takes the warm path and
never rescans -- hiding every sibling key under that tab_id. Hence `not keys` rather than
`keys is None`.

On this tree an empty-list value is not reachable from production code: _rebuild_tab_id_index
only ever setdefault()s a list it immediately appends to, and read_messages_chained deliberately
plants no [] sentinel (it uses index.get(tid, []) without storing). The empty-entry case below is
therefore a defensive pin on the guard's form, not a reproduction of a live defect. The absent-tid
case IS reachable and is what makes the guard load-bearing today.
"""

from __future__ import annotations

from kiro_crew.history import (
    _TAB_ID_INDEX_GLOB,
    ConversationLog,
    can_hold_tab_id_index_entry,
    transcript_stem,
)


class TestTabIdIndexSentinel:
    def test_note_tab_id_on_empty_entry_keeps_sibling_keys_reachable(self, tmp_path):
        """An entry that is present but empty must invalidate, never be appended to."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log.append("dashboard:chat-2", "user", "from two", tab_id="T")

        # Present-and-empty, not absent -- the state a `keys is None` guard would walk straight past.
        log._tab_id_index = {"T": []}

        log.note_tab_id("dashboard:chat-1", "T")

        contents = [m["content"] for m in log.read_messages_chained("dashboard:chat-1")]
        # Without the fix the empty entry was appended to, so the chained read took the warm
        # one-key path and never rescanned for the sibling: contents would be ["from one"].
        assert "from one" in contents
        assert "from two" in contents

    def test_note_tab_id_invalidates_when_tab_id_absent_from_index(self, tmp_path):
        """Reachable case: a save creating a tab_id's first file predates the built index."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log.append("dashboard:chat-2", "user", "from two", tab_id="U")
        # An authoritative index built before the "U" file existed knows nothing of "U".
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-2", "U")

        assert log._tab_id_index is None

    def test_absent_tab_id_invalidation_makes_the_sibling_reachable(self, tmp_path):
        """The consequence of that invalidation, asserted on its own so it can fail on its own.

        Kept separate from the assertion above because the index-state check would always fail
        first, leaving this one unreached and therefore unproven.
        """
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log.append("dashboard:chat-2", "user", "from two", tab_id="U")
        log.append("dashboard:chat-3", "user", "from three", tab_id="U")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-2", "U")

        contents = [m["content"] for m in log.read_messages_chained("dashboard:chat-2")]
        # Left un-invalidated, "U" stays absent from the index, the chained read falls back to
        # reading chat-2's own file, and chat-3 is silently unreachable.
        assert "from three" in contents

    def test_note_tab_id_still_warms_a_populated_entry(self, tmp_path):
        """The optimisation itself: a populated entry is appended to, not thrown away."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log.append("dashboard:chat-2", "user", "from two", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-2", "T")

        # Appended in place rather than invalidated, so the entry is still warm and now complete.
        assert log._tab_id_index == {"T": ["dashboard:chat-1", "dashboard:chat-2"]}

    def test_note_tab_id_leaves_a_stale_index_stale(self, tmp_path):
        """A None index means "rebuild on next read"; warming it here would skip the rebuild."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log._tab_id_index = None

        log.note_tab_id("dashboard:chat-1", "T")

        assert log._tab_id_index is None

    def test_note_tab_id_is_idempotent_for_an_already_indexed_key(self, tmp_path):
        """Repeated saves of one slot must not grow its chain entry with duplicates."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-1", "T")
        log.note_tab_id("dashboard:chat-1", "T")

        assert log._tab_id_index == {"T": ["dashboard:chat-1"]}

    def test_note_tab_id_without_a_tab_id_invalidates(self, tmp_path):
        """No tab_id to index against, so keep the old unconditional invalidation."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-1", None)

        assert log._tab_id_index is None


class TestChannelKeyedSaveIsNoOp:
    """A save the index rebuild never scans must not invalidate the warm index.

    _rebuild_tab_id_index only globs dashboard_chat-*.jsonl, so a channel-keyed transcript
    (slack:<ts> writes slack_<ts>.jsonl) can never hold an index entry. Its tab_id is therefore
    always absent, which without the guard sent every channel-tab flush down the
    invalidate_tab_id_cache() arm and restored the per-save rescan this change removes.

    Each consequence is asserted in its own test: the runner stops at the first failing
    assertion, so siblings sharing a test body would go unproven.
    """

    def test_channel_keyed_save_leaves_warm_index_warm(self, tmp_path):
        """The index must still be a live mapping, not the None staleness sentinel."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("slack:1786000000.1", "CHANNELTAB")

        assert log._tab_id_index is not None

    def test_channel_keyed_save_leaves_index_contents_untouched(self, tmp_path):
        """Separate assertion: the mapping is not merely non-None but unchanged."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("slack:1786000000.1", "CHANNELTAB")

        assert log._tab_id_index == {"T": ["dashboard:chat-1"]}

    def test_dashboard_keyed_save_still_appends(self, tmp_path):
        """Positive control: the guard must not swallow the case the method exists for."""
        log = ConversationLog(tmp_path)
        log.append("dashboard:chat-1", "user", "from one", tab_id="T")
        log.append("dashboard:chat-2", "user", "from two", tab_id="T")
        log._tab_id_index = {"T": ["dashboard:chat-1"]}

        log.note_tab_id("dashboard:chat-2", "T")

        assert log._tab_id_index == {"T": ["dashboard:chat-1", "dashboard:chat-2"]}

    def test_guard_admits_a_key_the_rebuild_glob_matches(self):
        """Pin the guard to the glob so the two cannot drift apart silently."""
        prefix = _TAB_ID_INDEX_GLOB.split("*", 1)[0]

        assert transcript_stem("dashboard:chat-1").startswith(prefix)

    def test_guard_admits_dashboard_keys(self):
        assert can_hold_tab_id_index_entry("dashboard:chat-1") is True

    def test_guard_rejects_channel_keys(self):
        assert can_hold_tab_id_index_entry("slack:1786000000.1") is False
