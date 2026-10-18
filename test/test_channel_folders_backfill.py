"""The explicit backfill of channel conversations into their folder.

The backfill guard (unlike automatic filing it ignores ``channel_origin``),
the pass that files every eligible conversation and reports what it moved,
and its yielding to a conversation deleted while it runs. Shared helpers live
in ``channel_folders_helpers.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from channel_folders_helpers import (  # noqa: F401 -- the fixture is requested by name
    _FakeLog,
    _write_config,
    dashboard_state_fixture,
)

from kiro_crew.dashboard import channel_folders, channel_slots


class _ModifiedLog(_FakeLog):
    """:class:`_FakeLog` whose sessions carry distinct mtimes and memory modes.

    The base fake reports every session as ``modified: 0.0``, which cannot
    distinguish "newest first" from "in whatever order the store listed them" --
    the two agree on a tie. It also has no ``memory_mode``, which the ephemeral
    skip reads.
    """

    def __init__(
        self,
        keys: list[str],
        meta: dict[str, Any] | None = None,
        modified: dict[str, float] | None = None,
        modes: dict[str, str] | None = None,
    ) -> None:
        super().__init__(keys, meta)
        self._modified = dict(modified or {})
        self._modes = dict(modes or {})

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for k in self._keys:
            row: dict[str, Any] = {
                "key": k,
                "title": "",
                "modified": self._modified.get(k, 0.0),
            }
            if k in self._modes:
                row["memory_mode"] = self._modes[k]
            out.append(row)
        return out


class TestNeedsBackfillFiling:
    """The guard, which is the whole of the behaviour change.

    Its one difference from :func:`needs_default_filing` is that it ignores
    ``channel_origin``, and that difference is what makes an existing
    conversation reachable at all.
    """

    def test_a_conversation_that_only_predates_the_setting_is_eligible(self) -> None:
        # The target population: surfaced and saved while filing was off, so it
        # carries the provenance flag and nothing else. Automatic filing refuses
        # this record; an explicit click must not.
        assert channel_slots.needs_backfill_filing({"channel_origin": True}) is True
        assert channel_slots.needs_default_filing({"channel_origin": True}) is False

    def test_a_never_surfaced_conversation_is_eligible(self) -> None:
        assert channel_slots.needs_backfill_filing({}) is True

    def test_a_conversation_in_a_folder_is_refused(self) -> None:
        assert channel_slots.needs_backfill_filing({"folder_id": "user-choice"}) is False

    def test_a_conversation_filed_then_moved_to_the_top_level_is_refused(self) -> None:
        """The marker with no folder beside it IS the record of a user move."""
        assert (
            channel_slots.needs_backfill_filing(
                {"channel_folder_filed": True, "channel_origin": True}
            )
            is False
        )


class TestBackfillChannelFolder:
    @pytest.fixture(autouse=True)
    def _quiet_push(self, dashboard_state: Any) -> None:
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

    def _folder(self, state: Any, ns: str = "discord", name: str = "Discord") -> str:
        _write_config(ns, name)
        return asyncio.run(channel_folders.ensure_channel_folder(state, ns, name))

    def test_files_a_conversation_that_predates_the_setting(self, dashboard_state: Any) -> None:
        """The defect this fixes: switching the setting on left these behind."""
        fid = self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "title": "Standup"}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == ""
        assert [m["key"] for m in report["moved"]] == [key]
        # Reported by name, because there is no bulk undo and the list is the
        # only record of what moved.
        assert report["moved"][0]["title"] == "Standup"
        # Persisted, not just reported: the marker is what stops the background
        # pass filing it a second time after the user moves it.
        assert log.get_metadata(key)["folder_id"] == fid
        assert log.get_metadata(key)["channel_folder_filed"] is True

    def test_leaves_a_conversation_the_user_filed_where_it_is(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"folder_id": "user-choice"}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert log.get_metadata(key)["folder_id"] == "user-choice"

    def test_leaves_a_conversation_moved_to_the_top_level_at_the_top_level(
        self, dashboard_state: Any
    ) -> None:
        """Filed once, then dragged out. The click must not drag it back."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_folder_filed": True}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert "folder_id" not in log.get_metadata(key)

    def test_an_ephemeral_conversation_is_never_given_a_folder(self, dashboard_state: Any) -> None:
        """Incognito asked for no trace; a durable placement contradicts that."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {}}, modes={key: "incognito"})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert "folder_id" not in log.get_metadata(key)

    def test_another_channels_conversations_are_untouched(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        mine = "discord:kirocrew:direct:U1"
        theirs = "telegram:kirocrew:direct:U2"
        log = _ModifiedLog([mine, theirs], {mine: {}, theirs: {}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [mine]
        assert "folder_id" not in log.get_metadata(theirs)

    def test_the_setting_being_off_is_reported_as_such(self, dashboard_state: Any) -> None:
        """Distinguishable from "nothing to do", which the panel words differently."""
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _ModifiedLog([key], {key: {}})

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == "not_configured"
        assert report["moved"] == []

    def test_a_configured_but_absent_folder_is_reported_as_such(self, dashboard_state: Any) -> None:
        _write_config("discord", "Discord")
        key = "discord:kirocrew:direct:U1"
        dashboard_state.conversation_log = _ModifiedLog([key], {key: {}})

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["reason"] == "folder_missing"
        assert report["folder_name"] == "Discord"
        # Nothing is created from this path: that would put an fsync on the loop.
        assert dashboard_state._folders == []

    def test_an_unknown_namespace_files_nothing(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = _ModifiedLog([])
        report = asyncio.run(
            channel_slots.backfill_channel_folder(dashboard_state, "not-a-channel")
        )
        assert report["reason"] == "not_configured"

    def test_one_click_is_bounded_and_says_how_many_remain(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """The cap is reportable rather than silent, so a second click continues."""
        monkeypatch.setattr(channel_slots, "BACKFILL_MOVE_LIMIT", 2)
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(5)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert len(report["moved"]) == 2
        assert report["remaining"] == 3
        # A capped run failed at nothing. This is the half that makes `failed`
        # worth reporting: without it, "3 still unfiled" reads the same here as
        # it does on a pass whose every write blew up.
        assert report["failed"] == 0
        # Idempotent, so the next click picks up exactly what this one did not.
        again = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        assert len(again["moved"]) == 2
        assert again["remaining"] == 1
        assert again["failed"] == 0

    def test_a_capped_click_takes_the_newest_conversations_first(
        self, dashboard_state: Any, monkeypatch: Any
    ) -> None:
        """A capped run must file what the user is most likely looking for."""
        monkeypatch.setattr(channel_slots, "BACKFILL_MOVE_LIMIT", 1)
        self._folder(dashboard_state)
        old = "discord:kirocrew:direct:OLD"
        new = "discord:kirocrew:direct:NEW"
        log = _ModifiedLog([old, new], {old: {}, new: {}}, modified={old: 100.0, new: 900.0})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [new]

    def test_an_open_tab_is_re_placed_without_a_restart(self, dashboard_state: Any) -> None:
        """Otherwise the button looks inert for the conversations on screen."""
        fid = self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None and slot.folder_id == ""
        pushes: list[int] = []
        dashboard_state.push_slots_update = lambda: pushes.append(1)  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert slot.folder_id == fid
        assert slot._channel_folder_filed is True
        assert pushes, "the sidebar is not told the tab moved"

    def test_a_folder_the_user_just_dragged_a_tab_into_wins(self, dashboard_state: Any) -> None:
        """The in-memory placement can be AHEAD of disk.

        A drag sets the slot immediately and saves asynchronously, so the record
        the guard reads still says unfiled. Reading only the record would move a
        conversation the user placed a second earlier.
        """
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None
        slot.folder_id = "dragged-here"

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert slot.folder_id == "dragged-here"
        assert "folder_id" not in log.get_metadata(key)

    def test_a_placement_landing_mid_pass_is_not_overwritten(self, dashboard_state: Any) -> None:
        """The guard re-decides under the store's lock, not at scan time."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        original = log.update_metadata_if

        def _place_first(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            # Stand in for the user's own move committing while this pass queued
            # behind the cross-process lock.
            log.update_metadata(k, {"folder_id": "user-choice"})
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _place_first  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        assert log.get_metadata(key)["folder_id"] == "user-choice"

    def test_a_filed_conversation_inherits_the_folders_tags(self, dashboard_state: Any) -> None:
        """Same creation-time inheritance the automatic filing path applies."""
        fid = self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(lambda fs: (True, fs[0].__setitem__("tags", ["t1"])))
        )
        dashboard_state._tags = [{"id": "t1", "label": "t1"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        stored = log.get_metadata(key)
        assert stored["folder_id"] == fid
        assert stored.get("tags") == ["t1"]

    def test_a_reported_title_is_redacted(self, dashboard_state: Any) -> None:
        """Titles are generated from channel content, so they are untrusted here."""
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        secret = "ghp_" + "a" * 36
        log = _ModifiedLog([key], {key: {"channel_origin": True, "title": secret}})
        dashboard_state.conversation_log = log

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert secret not in report["moved"][0]["title"]

    def test_no_conversation_log_is_reported_not_raised(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = None
        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        assert report["reason"] == "unavailable"

    def test_one_failing_conversation_does_not_abandon_the_rest(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        bad = "discord:kirocrew:direct:BAD"
        good = "discord:kirocrew:direct:GOOD"
        log = _ModifiedLog([bad, good], {bad: {}, good: {}}, modified={bad: 900.0, good: 100.0})
        dashboard_state.conversation_log = log
        original = log.update_metadata_if

        def _explode(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            if k == bad:
                raise OSError("disk went away")
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _explode  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [good]

    def test_a_folder_deleted_mid_pass_strands_nothing(self, dashboard_state: Any) -> None:
        """A dead ``folder_id`` beside the filing marker has NO recovery.

        The marker is what :func:`needs_backfill_filing` refuses on, so a
        conversation stamped with a folder that is gone ends up in no folder AND
        permanently ineligible for a later click. Empty tags cannot carry the
        difference between "this folder has no tags" and "this folder is gone",
        which is why the read reports absence explicitly.
        """
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(3)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log
        # Delete the folder the moment the first write is attempted.
        original = log.update_metadata_if
        deleted: list[int] = []

        def _delete_then_write(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            if not deleted:
                deleted.append(1)
                dashboard_state._folders.clear()
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _delete_then_write  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        # Whatever it managed before the deletion is reported; nothing after it
        # is stamped with the dead id. Its OWN reason: a folder DELETED mid-pass
        # needs the opposite sentence from one that was never created, and the
        # panel must not have to infer which it had from the counts.
        assert report["reason"] == "folder_gone"
        stranded = [
            k
            for k in keys
            if log.get_metadata(k).get("channel_folder_filed")
            and not any(
                f.get("id") == log.get_metadata(k).get("folder_id")
                for f in dashboard_state._folders
            )
        ]
        assert len(stranded) <= 1, stranded
        assert len(report["moved"]) == len(stranded)

    def test_a_drag_during_the_write_is_not_reverted_in_memory(self, dashboard_state: Any) -> None:
        """The mirror re-reads the slot, so a placement made mid-write survives.

        The write is an await. A drag landing during it sets the slot's folder in
        memory before its own save lands, so mirroring the value read BEFORE that
        await would revert the user's move -- and the guard on the write cannot
        catch it, because it reads the record their save has not reached yet.
        """
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        slot = channel_slots.surface_channel_session(
            dashboard_state, {"key": key, "title": "", "modified": 0.0}, {}, []
        )
        assert slot is not None
        original = log.update_metadata_if

        def _drag_then_write(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            # Stand in for the user dragging the open tab while the write runs:
            # in memory now, on disk only after their save.
            slot.folder_id = "dragged-mid-write"
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _drag_then_write  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert slot.folder_id == "dragged-mid-write"

    def test_writes_that_all_fail_are_not_reported_as_nothing_to_do(
        self, dashboard_state: Any
    ) -> None:
        """A swallowed failure must not render as "nothing needed moving"."""
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(2)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        def _explode(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            raise OSError("disk went away")

        log.update_metadata_if = _explode  # type: ignore[method-assign]

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert report["moved"] == []
        # Its OWN reason, not `unavailable`. That value means the store could not
        # be READ, and here every read succeeded and every WRITE failed. Sharing
        # one value made the panel state the wrong cause, and because the panel
        # short-circuits on an empty `moved` it also dropped the count below --
        # the only number that tells the user whether clicking again can help.
        assert report["reason"] == "all_failed"
        # Still unfiled, so they belong in the count that invites another click.
        assert report["remaining"] == 2
        # And every one of them is there because a write FAILED, not because the
        # run hit its cap, so the copy can say so instead of inviting a click
        # that will fail identically.
        assert report["failed"] == 2

    def test_the_receipt_names_the_folder_the_sessions_were_actually_filed_into(
        self, dashboard_state: Any
    ) -> None:
        """One config read, so the reported name and the written id cannot disagree.

        ``report["folder_name"]`` is what the panel shows and ``folder_id`` is what
        every write stamps. Both are derived from a SINGLE config read. With two
        independent reads -- one here and one inside ``lookup_channel_folder`` -- a
        settings save moving this channel from folder A to folder B and committing
        between them yields A's name beside B's id, so the user is told A while
        every session lands in B.

        Asserted by COUNTING the config reads rather than by comparing the strings.
        The property is that a second read does not happen; a comparison alone also
        passes whenever the race does not fire, which is almost always.
        """
        self._folder(dashboard_state, name="Alpha")
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {}})
        dashboard_state.conversation_log = log

        reads: list[str] = []
        real = channel_folders.configured_folder_name

        def _counting_read(ns: str) -> str:
            reads.append(ns)
            # A second read would see the reconfigured value, which is exactly the
            # divergence this pins shut.
            if len(reads) > 1:
                return "Beta"
            return real(ns)

        monkey = channel_slots.configured_folder_name
        channel_slots.configured_folder_name = _counting_read  # type: ignore[assignment]
        channel_folders.configured_folder_name = _counting_read  # type: ignore[assignment]
        try:
            report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))
        finally:
            channel_slots.configured_folder_name = monkey  # type: ignore[assignment]
            channel_folders.configured_folder_name = real  # type: ignore[assignment]

        assert reads == ["discord"], reads
        assert report["folder_name"] == "Alpha"
        # And the session really is in Alpha, not in the folder a second read
        # would have named.
        alpha = next(f for f in dashboard_state._folders if f.get("name") == "Alpha")
        assert log.get_metadata(key).get("folder_id") == alpha.get("id")

    def test_a_deleted_folder_and_one_never_created_report_different_reasons(
        self, dashboard_state: Any
    ) -> None:
        """The two situations are named by the SERVER, not inferred by the panel.

        ``lookup_channel_folder`` answering nothing means no folder ever existed,
        and saving the settings creates it. The per-write re-read finding the
        folder gone means it existed a moment ago, and anything already filed into
        it is stranded on a dead id -- recreating mints a fresh one. Opposite
        remedies, so opposite values.

        An earlier revision reported both as ``folder_missing`` and left the panel
        proving "the folder existed" from ``failed > 0``, a claim about this
        function's control flow made a layer away from it.
        """
        # Deleted WHILE the pass runs, with a write failing on the way.
        self._folder(dashboard_state)
        keys = [f"discord:kirocrew:direct:U{i}" for i in range(3)]
        log = _ModifiedLog(keys, {k: {} for k in keys})
        dashboard_state.conversation_log = log

        def _fail_then_delete(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            dashboard_state._folders.clear()
            raise OSError("disk went away")

        log.update_metadata_if = _fail_then_delete  # type: ignore[method-assign]

        gone = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert gone["reason"] == "folder_gone"
        assert gone["moved"] == []
        assert gone["failed"] >= 1

        # Never created: configured, but no folder answers to the name.
        _write_config("discord", "Discord")
        dashboard_state._folders.clear()
        fresh = [f"discord:kirocrew:direct:N{i}" for i in range(2)]
        dashboard_state.conversation_log = _ModifiedLog(fresh, {k: {} for k in fresh})

        never = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert never["reason"] == "folder_missing"
        assert never["moved"] == []
        # The two are distinguishable from the report alone, which is the point.
        assert never["reason"] != gone["reason"]

    def test_a_history_only_conversations_own_tags_survive_filing(
        self, dashboard_state: Any
    ) -> None:
        """The user's tags are unioned with the folder's, never replaced.

        The store merges with ``metadata.update(fields)``, so a bare list under
        ``tags`` overwrites the whole key. This path reaches records the automatic
        one never does: a conversation surfaced while filing was off, tagged by the
        user and never filed, with its tab CLOSED. It passes the guard (which reads
        placement, not tags) and has no live slot to union the tags back through,
        so replacing would destroy them with nothing recording what they were.
        """
        fid = self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda fs: (True, fs[0].__setitem__("tags", ["folder-tag"]))
            )
        )
        dashboard_state._tags = [
            {"id": "folder-tag", "label": "folder-tag"},
            {"id": "important", "label": "important"},
        ]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "tags": ["important"]}})
        dashboard_state.conversation_log = log
        # No live slot: History-only is the case with no in-memory union to fall
        # back on, which is what makes the loss unrecoverable.
        assert channel_slots.channel_slot_name(key) not in dashboard_state._slots

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [key]
        stored = log.get_metadata(key)
        assert stored["folder_id"] == fid
        assert stored["tags"] == ["important", "folder-tag"], stored["tags"]

    def test_an_unreadable_record_does_not_get_its_tags_touched(self, dashboard_state: Any) -> None:
        """Fails closed: inheriting is a convenience, losing tags is permanent."""
        self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda fs: (True, fs[0].__setitem__("tags", ["folder-tag"]))
            )
        )
        dashboard_state._tags = [{"id": "folder-tag", "label": "folder-tag"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        calls: list[str] = []
        original = log.get_metadata

        def _explode_on_reread(k: str) -> dict[str, Any]:
            calls.append(k)
            # Exactly the SECOND read: the first is the pre-scan snapshot and the
            # third is the guard's own read inside update_metadata_if. Failing
            # every read after the first would break the write itself and the test
            # would pass for the wrong reason -- it did, before this was scoped.
            if len(calls) == 2:
                raise OSError("record unreadable")
            return original(k)

        log.get_metadata = _explode_on_reread  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        log.get_metadata = original  # type: ignore[method-assign]
        stored = log.get_metadata(key)
        # Filed, but the tags key was never written.
        assert stored.get("channel_folder_filed") is True
        assert "tags" not in stored, stored

    def test_the_folder_adding_nothing_new_writes_no_tags_key(self, dashboard_state: Any) -> None:
        """No write on `tags` at all when the union would not change it."""
        self._folder(dashboard_state)
        asyncio.run(
            dashboard_state.mutate_folders(lambda fs: (True, fs[0].__setitem__("tags", ["shared"])))
        )
        dashboard_state._tags = [{"id": "shared", "label": "shared"}]
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True, "tags": ["shared"]}})
        dashboard_state.conversation_log = log
        written: list[dict[str, Any]] = []
        original = log.update_metadata_if

        def _record(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            written.append(dict(fields))
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _record  # type: ignore[method-assign]

        asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert written and "tags" not in written[0], written
        assert log.get_metadata(key)["tags"] == ["shared"]


class TestBackfillYieldsToADeletion:
    """A conversation deleted while the pass runs must stay deleted.

    The pass reads its candidate list, then takes one lock per write. That gap is
    the widest in the feature -- up to ``BACKFILL_MOVE_LIMIT`` lock acquisitions
    and awaits -- and the store's metadata read reports an ABSENT session as an
    empty-but-readable record, which is the same value a session with no metadata
    line has. So the placement guard cannot refuse the deletion:
    ``needs_backfill_filing({})`` is True by design, because at SCAN time an
    empty record means "nothing has placed this". Existence is asked separately,
    inside the write's own lock.

    What that prevents is specific and unrecoverable: the merge upserts, so the
    deleted conversation comes back as a metadata line carrying the folder id and
    the filing marker, with no transcript behind it -- a sidebar row that opens
    onto nothing, stamped ineligible for any later pass, and this button has no
    undo to walk it back.
    """

    @pytest.fixture(autouse=True)
    def _quiet_push(self, dashboard_state: Any) -> None:
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

    def _folder(self, state: Any, ns: str = "discord", name: str = "Discord") -> str:
        _write_config(ns, name)
        return asyncio.run(channel_folders.ensure_channel_folder(state, ns, name))

    @staticmethod
    def _delete_at_write(log: Any, victim: str) -> None:
        """Make *victim*'s deletion land just before its own write is attempted.

        Deleting from ``_keys`` AND ``_meta`` is what a real deletion does: the
        transcript goes, and with it the metadata line. Doing it here rather than
        before the pass is the whole point -- the candidate has to be present for
        the scan and absent for the write, which is the window under test.
        """
        original = log.update_metadata_if

        def _deleting(k: str, fields: dict[str, Any], guard: Any, **kwargs: Any) -> bool:
            if k == victim:
                log._keys = [x for x in log._keys if x != victim]
                log._meta.pop(victim, None)
            return original(k, fields, guard, **kwargs)

        log.update_metadata_if = _deleting  # type: ignore[method-assign]

    def test_a_conversation_deleted_mid_pass_is_not_recreated(self, dashboard_state: Any) -> None:
        self._folder(dashboard_state)
        key = "discord:kirocrew:direct:U1"
        log = _ModifiedLog([key], {key: {"channel_origin": True}})
        dashboard_state.conversation_log = log
        self._delete_at_write(log, key)

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        # The record stays gone. A `folder_id` here would be the stub: filed into
        # the folder with nothing behind it.
        assert log.get_metadata(key) == {}
        assert key not in log._meta
        # And it is not claimed as moved. The receipt is the only record of what
        # this button did, so naming a conversation it did not move is worse than
        # naming none.
        assert report["moved"] == []

    def test_a_deletion_does_not_stop_the_conversations_after_it(
        self, dashboard_state: Any
    ) -> None:
        """A refusal is a decision about one conversation, not about the pass."""
        fid = self._folder(dashboard_state)
        gone = "discord:kirocrew:direct:GONE"
        kept = "discord:kirocrew:direct:KEPT"
        log = _ModifiedLog(
            [gone, kept],
            {gone: {"channel_origin": True}, kept: {"channel_origin": True}},
            modified={gone: 900.0, kept: 100.0},
        )
        dashboard_state.conversation_log = log
        self._delete_at_write(log, gone)

        report = asyncio.run(channel_slots.backfill_channel_folder(dashboard_state, "discord"))

        assert [m["key"] for m in report["moved"]] == [kept]
        assert log.get_metadata(kept)["folder_id"] == fid
        assert log.get_metadata(gone) == {}
        # Not a failure: no write was attempted, so inviting another click
        # against a conversation that is gone would be a dead end.
        assert report["failed"] == 0
        assert report["reason"] == ""

    def test_the_scan_filter_still_accepts_an_empty_record(self) -> None:
        """The two questions have opposite answers for the same dict.

        Pinned together so a later tidy-up cannot collapse them: moving existence
        into the placement guard would refuse the feature's own population, and
        that is why the flag exists instead.
        """
        assert channel_slots.needs_backfill_filing({}) is True
