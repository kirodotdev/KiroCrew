"""One slot, one transcript — including the channel slot nobody could bind.

A channel-born tab is normally BOUND to the session the channel runs
(``linked_session_key``), resolved in ``get_or_create_slot`` from the session
map. When the map cannot resolve the stem — it was pruned, or the thread
predates it — the tab is deliberately left UNBOUND rather than guessing, because
a wrong key would route the user's replies to a session the channel never reads.
``surface_channel_session`` documents that as a supported state.

For that unbound slot the old ``effective_session_key`` fallback prefixed
``dashboard:`` and named ``dashboard_slack_<ts>.jsonl`` — a file no restore path
reads and ``migrate_channel_transcripts`` deletes on the next boot (it carries
only ``title``/``folder_id``/``pinned``). Every READ path meanwhile resolved the
same slot through ``slot_transcript_key`` and got the channel transcript.

The user-visible consequence: closing such a tab wrote ``closed`` to the phantom
file, the restore guard read the channel file and never saw it, and the tab came
back on every gateway restart — permanently, because a ``folder_id`` also exempts
it from the recency window.

These tests lock the invariant that reads and writes address ONE file.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    restore_open_slots,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.history import _safe_key

CHANNEL_KEY = "slack:1783733803.877979"
STEM = _safe_key(CHANNEL_KEY)  # "slack_1783733803.877979" — the slot name too
PHANTOM = f"dashboard_{STEM}.jsonl"


def _state(tmp_path, *, resolves: str | None = None):
    """State whose session map resolves STEM to *resolves* (default: unknown).

    The bare ``_make_state`` mock would auto-create a truthy
    ``channel_key_for_stem``, which is precisely the accident the binding guard
    exists to stop — pin it explicitly in every test.
    """
    state = _make_state(tmp_path / "sessions")
    state.sessions.channel_key_for_stem = lambda stem: resolves or ""
    return state


def _seed_channel_transcript(state) -> None:
    """A channel transcript as the channel side actually leaves it.

    The ``tab_id`` matters: without one, ``_rehydrate_slot_from_history``
    backfills it through ``update_metadata_off_loop``, and that BACKGROUND
    rewrite restores the mtime it captured before the write — which can land
    after a later append and drag the file's mtime backwards. A real channel
    transcript already carries a tab_id, so seeding one keeps these tests
    deterministic instead of racing a thread.
    """
    log = state.conversation_log
    assert log is not None
    log.append(CHANNEL_KEY, "user", "hello from the thread")
    log.append(CHANNEL_KEY, "assistant", "hi back")
    log.update_metadata(CHANNEL_KEY, {"tab_id": "aaaabbbbcccc"})


def _restarted(tmp_path, old_state):
    """A second DashboardState over the same sessions dir (a 'gateway restart')."""
    state = _make_state(tmp_path / "sessions")
    state.sessions.channel_key_for_stem = old_state.sessions.channel_key_for_stem
    return state


def _restored_tab(state):
    """The tab as production actually establishes it: via the reconciler.

    ``surface_channel_session`` owns surfacing a channel conversation and is what
    sets ``channel_origin``. A legacy transcript carries no persisted marker, so
    ``restore_open_slots`` deliberately will NOT claim it — using the reconciler
    here models production rather than working around the code.
    """
    log = state.conversation_log
    info = next(s for s in log.list_sessions() if s.get("key") == STEM)
    slot = channel_slots.surface_channel_session(
        state, info, log.get_metadata(STEM), log.read_messages(STEM)
    )
    assert slot is not None
    assert slot.messages, "surfaced slot must carry a window"
    assert slot.channel_origin is True
    return slot


def _close(state, slot, *, closed_at: float) -> None:
    asyncio.run(
        save_slot_off_loop(
            state, slot, closed=True, closed_at=closed_at, best_effort=False
        )
    )


class TestSlotHistoryKey:
    def test_unbound_channel_tab_resolves_to_its_channel_transcript(self, tmp_path):
        """The regression: this used to answer "dashboard:slack_<ts>"."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot(STEM, channel_origin=True)
        assert not slot.linked_session_key
        assert slot_history_key(slot) == STEM

    def test_a_dashboard_slot_merely_NAMED_like_a_channel_keeps_its_own_file(
        self, tmp_path
    ):
        """A filename shape is not provenance -- and must not be read as one.

        ``POST /api/chat/slots`` accepts a client-supplied name, and main
        deliberately supports a dashboard slot a caller happened to name
        ``slack_notes`` as a genuine mirror-out. So the stem cannot be the
        signal: without a persisted marker the slot keeps its own
        ``dashboard:`` transcript rather than adopting a real thread's.
        """
        state = _state(tmp_path)
        _seed_channel_transcript(state)  # the thread it would collide with
        slot = state.get_or_create_slot(STEM)  # no channel_origin

        assert slot.channel_origin is False
        assert slot_history_key(slot) == f"dashboard:{STEM}"

    def test_a_legacy_channel_transcript_is_not_adopted(self, tmp_path):
        """Provenance is persisted-only: no marker means no adoption.

        An empty dashboard tab named for an old channel stem must not inherit
        that thread's history, and file absence cannot tell the two apart. The
        data-loss risk this used to guard against is handled instead by
        ``api_sessions_clear`` protecting BOTH candidate transcripts, so
        deletion no longer depends on provenance resolving correctly.
        """
        state = _state(tmp_path)
        _seed_channel_transcript(state)  # channel transcript, no persisted flag

        slot = _rehydrate_slot_from_history(state, STEM)

        assert slot is not None
        assert slot.channel_origin is False
        assert slot_history_key(slot) == f"dashboard:{STEM}"

    def test_a_slot_with_its_own_dashboard_transcript_is_not_adopted(self, tmp_path):
        """The anti-merge case: a dashboard slot merely NAMED like a channel.

        main supports ``slack_notes`` as a genuine mirror-out, and such a slot
        writes its own ``dashboard:<name>`` transcript. That file existing is
        what distinguishes it from a real thread -- not the name's shape.
        """
        state = _state(tmp_path)
        _seed_channel_transcript(state)  # the thread it would collide with
        # Its OWN transcript, as any dashboard slot's save would create.
        state.conversation_log.append(f"dashboard:{STEM}", "user", "my own chat")

        slot = _rehydrate_slot_from_history(state, STEM)

        assert slot is not None
        assert slot.channel_origin is False
        assert slot_history_key(slot) == f"dashboard:{STEM}"

    def test_channel_origin_is_never_downgraded_by_a_later_plain_call(self, tmp_path):
        """get_or_create_slot also returns EXISTING slots."""
        state = _state(tmp_path)
        state.get_or_create_slot(STEM, channel_origin=True)

        again = state.get_or_create_slot(STEM)

        assert again.channel_origin is True
        assert slot_history_key(again) == STEM

    def test_bound_channel_slot_resolves_to_its_linked_key(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot(STEM, linked_session_key=CHANNEL_KEY)
        assert slot_history_key(slot) == CHANNEL_KEY

    def test_ordinary_dashboard_slot_is_unchanged(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("chat-7-123")
        assert slot_history_key(slot) == "dashboard:chat-7-123"

    def test_restore_of_an_ordinary_session_is_not_channel_origin(self, tmp_path):
        state = _state(tmp_path)
        state.conversation_log.append("dashboard:chat-9-1", "user", "hi")

        slot = _rehydrate_slot_from_history(state, "chat-9-1")

        assert slot is not None
        assert slot.channel_origin is False

    def test_channel_transcript_and_linked_key_are_the_same_file(self, tmp_path):
        """_safe_key folds ':' to '_', so both keys name one .jsonl."""
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        log = state.conversation_log
        assert log._path(CHANNEL_KEY) == log._path(STEM)


class TestProvenanceIsPersisted:
    """The restore must not have to re-derive provenance from the slot name."""

    def test_a_close_writes_channel_origin_into_the_transcript(self, tmp_path):
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        slot = _restored_tab(state)
        assert slot.channel_origin is True

        _close(state, slot, closed_at=time.time())

        meta = state.conversation_log.get_metadata(CHANNEL_KEY)
        assert meta.get("channel_origin") is True

    def test_restore_reads_provenance_back_from_the_transcript(self, tmp_path):
        """Round-trip: persisted flag alone is enough to re-establish the tab."""
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        state.conversation_log.update_metadata(CHANNEL_KEY, {"channel_origin": True})

        slot = _rehydrate_slot_from_history(state, STEM)

        assert slot is not None
        assert slot.channel_origin is True
        assert slot_history_key(slot) == STEM

    def test_an_ordinary_transcript_never_gains_the_flag(self, tmp_path):
        state = _state(tmp_path)
        state.conversation_log.append("dashboard:chat-9-1", "user", "hi")
        slot = _rehydrate_slot_from_history(state, "chat-9-1")
        assert slot is not None

        _close(state, slot, closed_at=time.time())

        meta = state.conversation_log.get_metadata("dashboard:chat-9-1")
        assert "channel_origin" not in meta


class TestCloseLandsOnTheTranscriptTheRestorePathReads:
    def test_close_on_unbound_channel_tab_writes_the_channel_transcript(self, tmp_path):
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        slot = _restored_tab(state)
        assert slot.linked_session_key == "", "map cannot resolve: must be unbound"
        closed_at = time.time()

        _close(state, slot, closed_at=closed_at)

        meta = state.conversation_log.get_metadata(CHANNEL_KEY)
        assert meta.get("closed") is True
        assert float(meta.get("closed_at", 0)) == closed_at

    def test_close_creates_no_phantom_dashboard_transcript(self, tmp_path):
        """The phantom file is what migrate_channel_transcripts later deletes."""
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        slot = _restored_tab(state)

        _close(state, slot, closed_at=time.time())

        assert not (tmp_path / "sessions" / PHANTOM).exists()

    def test_bound_channel_tab_still_writes_the_channel_transcript(self, tmp_path):
        """No regression for the tab the reconciler DID manage to bind."""
        state = _state(tmp_path, resolves=CHANNEL_KEY)
        _seed_channel_transcript(state)
        slot = _restored_tab(state)
        assert slot.linked_session_key == CHANNEL_KEY

        _close(state, slot, closed_at=time.time())

        assert state.conversation_log.get_metadata(CHANNEL_KEY).get("closed") is True
        assert not (tmp_path / "sessions" / PHANTOM).exists()

    def test_ordinary_tab_close_is_unchanged(self, tmp_path):
        state = _state(tmp_path)
        state.conversation_log.append("dashboard:chat-9-1", "user", "hi")
        slot = _rehydrate_slot_from_history(state, "chat-9-1")
        assert slot is not None

        _close(state, slot, closed_at=time.time())

        meta = state.conversation_log.get_metadata("dashboard:chat-9-1")
        assert meta.get("closed") is True


class TestClosedChannelTabStaysClosedAcrossRestart:
    def test_closed_unbound_channel_tab_is_not_restored(self, tmp_path, monkeypatch):
        """End-to-end of the reported symptom: close, restart, stays gone."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        slot = _restored_tab(state)
        _close(state, slot, closed_at=time.time())
        # The tab is gone from the live set, but the snapshot still lists it —
        # exactly the state a gateway restart reads.
        state._slots.pop(STEM, None)
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": [STEM], "ts": time.time()}), encoding="utf-8"
        )

        after_restart = _restarted(tmp_path, state)

        assert restore_open_slots(after_restart) == 0
        assert STEM not in after_restart._slots

    def test_open_channel_tab_is_still_restored(self, tmp_path, monkeypatch):
        """Guard the inverse: an un-closed tab must still come back."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _state(tmp_path)
        _seed_channel_transcript(state)
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": [STEM], "ts": time.time()}), encoding="utf-8"
        )

        after_restart = _restarted(tmp_path, state)

        assert restore_open_slots(after_restart) == 1
        assert STEM in after_restart._slots


class TestClosedChannelTabDoesNotOutrunItsOwnClose:
    """The reconciler is the OTHER path that can resurface the tab.

    ``restore_open_slots`` reads ``meta.closed`` directly, but the reconciler
    asks ``_close_stands``, which compares the transcript's mtime against
    ``closed_at`` — channel activity newer than the close re-qualifies the
    session. Writing the close flag is itself a write to that same shared file,
    so unless the pre-close mtime is restored the close outruns itself and the
    tab comes back on the next pass.

    ``closed_at`` is derived from the file's own mtime rather than ``time.time()``
    on purpose: Linux stamps mtimes from a COARSE clock (roughly millisecond
    granularity), so a file written microseconds after a ``time.time()`` reading
    can carry an mtime slightly BEFORE it. Anchoring to the file's own clock
    removes that ambiguity, so an unrestored mtime always reads as newer.
    """

    def _prepare(self, state):
        """Seed, rehydrate, and pick a close instant just after last activity."""
        _seed_channel_transcript(state)
        slot = _restored_tab(state)
        path = state.conversation_log._path(CHANNEL_KEY)
        pre_close_mtime = path.stat().st_mtime
        return slot, path, pre_close_mtime + 0.001

    def _listing(self, state):
        rows = state.conversation_log.list_sessions()
        row = next(s for s in rows if s.get("key") == STEM)
        return row, state.conversation_log.get_metadata(STEM)

    def test_close_restores_the_pre_close_mtime(self, tmp_path):
        """Exact identity, not "<= closed_at": the write must leave no trace."""
        state = _state(tmp_path)
        slot, path, closed_at = self._prepare(state)
        before = path.stat().st_mtime

        _close(state, slot, closed_at=closed_at)

        assert path.stat().st_mtime == before

    def test_reconciler_does_not_resurface_the_closed_tab(self, tmp_path):
        state = _state(tmp_path)
        slot, _path, closed_at = self._prepare(state)

        _close(state, slot, closed_at=closed_at)

        row, meta = self._listing(state)
        assert float(row["modified"]) <= float(meta["closed_at"])
        eligible = channel_slots.eligible_channel_sessions(
            [row], metadata={STEM: meta}, cutoff=None, mtimes={STEM: row["modified"]}
        )
        assert eligible == []

    def test_a_real_channel_append_after_the_close_still_resurfaces_it(self, tmp_path):
        """Don't over-preserve: the channel moving on must win over the close."""
        state = _state(tmp_path)
        slot, _path, closed_at = self._prepare(state)
        _close(state, slot, closed_at=closed_at)
        row, meta = self._listing(state)

        # The channel kept talking a minute later. Stated explicitly rather than
        # timed, so the assertion is about _close_stands' rule and not the host's
        # filesystem timestamp resolution.
        moved_on = dict(row, modified=float(meta["closed_at"]) + 60.0)

        eligible = channel_slots.eligible_channel_sessions(
            [moved_on],
            metadata={STEM: meta},
            cutoff=None,
            mtimes={STEM: moved_on["modified"]},
        )
        assert [s["key"] for s in eligible] == [STEM]


class TestChannelBindingIsValidated:
    """``get_or_create_slot`` is the one site that adopts a map-resolved key."""

    def test_binds_a_real_channel_key(self, tmp_path):
        state = _state(tmp_path, resolves=CHANNEL_KEY)
        assert state.get_or_create_slot(
            STEM, channel_origin=True
        ).linked_session_key == CHANNEL_KEY

    def test_leaves_it_unbound_when_the_map_cannot_resolve(self, tmp_path):
        state = _state(tmp_path)
        assert state.get_or_create_slot(STEM, channel_origin=True).linked_session_key == ""

    def test_refuses_a_non_channel_resolution(self, tmp_path):
        """A wrong key would route replies to a session the channel never reads."""
        state = _state(tmp_path, resolves="dashboard:chat-1-123")
        assert state.get_or_create_slot(STEM, channel_origin=True).linked_session_key == ""

    def test_refuses_a_non_string_resolution(self, tmp_path):
        """A MagicMock is truthy; a bare truthiness check would bind to it."""
        state = _make_state(tmp_path / "sessions")
        state.sessions.channel_key_for_stem = lambda stem: MagicMock()
        assert state.get_or_create_slot(STEM, channel_origin=True).linked_session_key == ""

    def test_ordinary_slot_name_is_never_bound(self, tmp_path):
        state = _state(tmp_path, resolves=CHANNEL_KEY)
        assert state.get_or_create_slot("chat-9-1").linked_session_key == ""

    def test_rehydrate_prefers_the_persisted_binding_over_the_map(self, tmp_path):
        state = _state(tmp_path, resolves="slack:9999999999.000000")
        _seed_channel_transcript(state)
        state.conversation_log.update_metadata(
            CHANNEL_KEY, {"linked_session_key": CHANNEL_KEY}
        )

        slot = _rehydrate_slot_from_history(state, STEM)

        assert slot is not None
        assert slot.linked_session_key == CHANNEL_KEY

    def test_unbound_channel_tab_still_loads_its_history(self, tmp_path):
        """Unbound is a supported state, not a broken one."""
        state = _state(tmp_path)
        _seed_channel_transcript(state)

        slot = _rehydrate_slot_from_history(state, STEM)

        assert slot is not None
        assert slot.linked_session_key == ""
        assert [m["content"] for m in slot.messages] == [
            "hello from the thread",
            "hi back",
        ]
