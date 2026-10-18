"""Pins that a dashboard slot save loses no acknowledged write, its own or another's.

``_save_slot_to_history`` rewrites a session JSONL from the slot's frozen prefix and
window. These tests pin that the save holds the per-session ``_locked``, so an append
another thread makes mid-write survives; that it merges an append committed to the
file before the save runs; that every frozen-prefix fast-path save re-emits a
preserved foreign line exactly once; and that it carries ``rotation_generation``
forward. The fold in ``_frozen_prefix_and_foreign_appends`` is pinned on both
identity tiers: the id-less timestamp-first ladder with its count-bounded body
tiebreak and the drops it surfaces for archiving, and the ``meta.mid`` tier (pass 0)
with its corroboration rules. The fold logs one warning when it keeps lines it has
not reported yet, naming the chat and never the content. ``save_slot_off_loop``
marks the slot dirty when a best-effort save fails, so the periodic flush retries
it. The ``ConversationLog`` lock these saves rely on is pinned in
``test_history_locking_remediation.py`` and
``test_history_locking_remediation_locked_section.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

from kiro_crew.history import ConversationLog


# ── Bug 7: dashboard persistence save must hold _locked vs. append_off_loop ──
class TestDashboardSaveHoldsLock:
    """``_save_slot_to_history`` (dashboard persistence) does a
    read-modify-``atomic_write`` of the session JSONL. If it does NOT hold the
    same per-session ``_locked`` as :meth:`ConversationLog.append`, a concurrent
    ``append_off_loop`` — e.g. a workflow result appended to the originating
    dashboard session — can land AFTER the save snapshots the file but BEFORE it
    replaces the file, so the ``atomic_write`` silently deletes the just-appended
    (already acknowledged) message.

    This reproduces that interleaving deterministically: a competitor thread
    appends a message during the save's ``atomic_write``. With the lock the
    append serializes behind the save and survives; without it the save's
    file-replace clobbers the append.
    """

    def _make_state(self, tmp_path: Path):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_concurrent_append_off_loop_survives_save(self, tmp_path, monkeypatch):
        import threading

        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("locktest")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window.
        _save_slot_to_history(state, slot, force=True)

        # A workflow-result durable copy (exactly what inject_workflow_result
        # does via append_off_loop) appended while the NEXT save is mid-write.
        appended = "workflow-result-m3"
        go = threading.Event()
        done = threading.Event()

        def _competitor() -> None:
            # Off the event loop → append takes the patient _locked acquire path.
            go.wait(5)
            state.conversation_log.append(history_key, "assistant", appended)
            done.set()

        competitor = threading.Thread(target=_competitor)
        competitor.start()

        real_atomic_write = chat_persistence.atomic_write

        def _slow_atomic_write(path, data, **kwargs):
            # We are now INSIDE the save's critical section. If the save holds
            # _locked, the competitor's append blocks here until the save's
            # with-block exits; if it does NOT, the append lands during this
            # sleep and the real write below clobbers it.
            go.set()
            # flake-ok: pre-existing 500 ms hold inside the stubbed atomic_write that widens the race window the competitor's append aims at, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
            time.sleep(0.5)
            return real_atomic_write(path, data, **kwargs)

        monkeypatch.setattr(chat_persistence, "atomic_write", _slow_atomic_write)

        # Window is unchanged ([m1, m2]) so a naive save re-writes only those two
        # lines — the appended m3 exists ONLY on disk and is the loss candidate.
        _save_slot_to_history(state, slot, force=True)

        assert done.wait(10), "competitor append never completed (deadlock?)"
        competitor.join(10)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert appended in contents, (
            "dashboard save clobbered a concurrent append_off_loop message "
            f"(got {contents!r}) — _save_slot_to_history must hold _locked"
        )
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1

    def test_cross_process_append_before_save_survives(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:793): the ``window`` snapshot is
        captured BEFORE ``_save_slot_to_history`` takes ``_locked``. A writer in
        ANOTHER process can fully append + release the lock in that gap; the save
        then acquires the lock and rewrites ``meta + frozen + window`` from the
        stale snapshot, permanently deleting the acknowledged append.

        This reproduces it WITHOUT threads: the cross-process append is committed
        to the file first, then the slot (whose in-memory window never saw it) is
        saved. The save must merge the on-disk append rather than clobber it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("staleoverwrite")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window ([m1, m2]).
        _save_slot_to_history(state, slot, force=True)

        # A DIFFERENT process appends an acknowledged message straight to the
        # file (append holds the same cross-process ``_locked``). This slot's
        # in-memory window never learns about it (no ``slot.append`` here), so it
        # is exactly the "landed between snapshot and lock" loss candidate.
        state.conversation_log.append(history_key, "assistant", "cross-proc-m3")

        # Next slot save: window is still [m1, m2]; a bare meta+frozen+window
        # replace would drop cross-proc-m3.
        _save_slot_to_history(state, slot, force=True)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert "cross-proc-m3" in contents, (
            "slot save deleted a cross-process append that landed between the "
            f"window snapshot and the file replace (got {contents!r})"
        )
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1
        assert contents.count("cross-proc-m3") == 1

    def test_repeated_save_after_foreign_append_keeps_it(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:746): the frozen-prefix fast path.

        After a save preserves a cross-process append, it records the resulting
        file's ``(mtime, size, disk_older)`` in ``slot._frozen_prefix_cache``.
        The very NEXT save (window and disk both unchanged) then matches that
        cache and takes the O(window) fast path. If the fast path returns EMPTY
        foreign lines, the rebuilt ``meta + frozen + window`` payload drops the
        already-preserved append — a cron/workflow result followed by two
        dashboard saves silently loses the transcript line.

        Reproduces the sequence save -> foreign-append -> save -> save -> save
        with NO writer between the saves, so saves 2..4 all hit the fast path,
        and asserts the foreign line survives every one exactly once.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("fastpathloss")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window ([m1, m2]).
        _save_slot_to_history(state, slot, force=True)

        # A cron/workflow result lands via the cross-process append path; the
        # slot's in-memory window never learns about it.
        state.conversation_log.append(history_key, "assistant", "cron-m3")

        # Save #1: slow path (disk changed since our seed write) detects and
        # preserves cron-m3, then caches the resulting file metadata + the
        # preserved foreign line.
        _save_slot_to_history(state, slot, force=True)

        # Saves #2, #3, #4: disk is byte-identical to save #1's output and the
        # window is still [m1, m2], so each hits the frozen-prefix fast path.
        # Before the fix these dropped cron-m3; now the cached foreign line is
        # re-emitted verbatim on every fast-path save.
        for _ in range(3):
            _save_slot_to_history(state, slot, force=True)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert "cron-m3" in contents, (
            "fast-path save dropped a previously-preserved cross-process append "
            f"(got {contents!r}) — cached foreign lines must be re-emitted"
        )
        assert (
            contents.count("cron-m3") == 1
        ), f"cron-m3 duplicated across repeated fast-path saves (got {contents!r})"
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1

    def test_slot_save_preserves_rotation_generation(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:793, second half): the rewritten
        metadata reconstructed a subset and omitted ``rotation_generation``, so a
        slot save AFTER a rotation reset the generation to 0 — letting a
        concurrent consolidation apply a stale offset and mark never-consolidated
        retained messages as done (undoing the rotation-generation fix).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("rotgen")
        slot.append("user", "hello")
        slot.drain()
        history_key = _history_key_for(slot.key)
        _save_slot_to_history(state, slot, force=True)

        # Simulate a rotation having bumped the generation counter in metadata.
        state.conversation_log.update_metadata(history_key, {"rotation_generation": 7})
        assert state.conversation_log.rotation_generation(history_key) == 7

        # A subsequent slot save must carry the generation forward, not reset it.
        slot.append("assistant", "world")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.rotation_generation(history_key) == 7, (
            "slot save dropped rotation_generation — a stale consolidation "
            "offset could then be applied against post-rotation messages"
        )

    def test_foreign_append_content_identity_dedup_semantics(self, tmp_path, monkeypatch):
        """Pin the LEGACY id-less fallback: the narrowed, timestamp-first
        foreign-append identity (GPT 5.6 HIGH + arbiter long-term item 2).

        Since the ``meta.mid`` tier landed, this ladder is the fallback
        for disk lines that carry NO stable id — pre-id transcripts and writers
        that persist id-less durable copies. Every disk line and window entry
        here is deliberately id-less, so the input must reproduce the pre-id
        behaviour EXACTLY (pass 0 never fires; nothing about tiers a-c moved).

        For id-less lines ``_frozen_prefix_and_foreign_appends`` classifies a
        disk window-region line as "ours" (drops it — the window re-serializes
        it) when EITHER its ``ts`` matches a window entry (in-place edit) OR —
        as a COUNT-BOUNDED tiebreak — its ``(role, content)`` matches an
        as-yet-unconsumed window entry (a same-process ``append_if_absent`` copy
        persisted with a fresh ``ts``). Because the tiebreak is bounded, each
        window entry absorbs AT MOST one disk copy: a second same-content line
        with a DISTINCT ts (a genuinely distinct event from another process,
        e.g. a repeated identical cron/workflow result) is PRESERVED as foreign
        rather than collapsed. This verifies both no-loss (the distinct event
        survives) and no-duplication (the append_if_absent fresh-ts copy is
        folded once, not re-appended). Stamped lines resolve exactly in the id
        tier instead — see ``TestForeignFoldMidIdentity`` and
        docs/system-specs/modules/history.md.
        """
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import (
            _frozen_prefix_and_foreign_appends,
        )
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("dedupsem")
        history_key = _history_key_for(slot.key)
        path = state.conversation_log._path(history_key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # On-disk: metadata line + window-region message lines.
        disk_lines = [
            json.dumps({"_type": "metadata", "created": "2026-01-01T00:00:00Z"}),
            # ts-match to a window entry (edited in place) → collapsed.
            json.dumps({"role": "user", "content": "same-ts", "ts": "T1"}),
            # The window's OWN persisted copy of "windowed" (ts-match) → dropped;
            # the window re-serializes it.
            json.dumps({"role": "assistant", "content": "windowed", "ts": "T2"}),
            # A genuinely DISTINCT event with identical (role, content) but a
            # different ts — the GPT 5.6 HIGH data-loss case. With the window's
            # single "windowed" budget already spent by the ts-match above, this
            # MUST be preserved as foreign, not collapsed.
            json.dumps({"role": "assistant", "content": "windowed", "ts": "TX"}),
            # An append_if_absent copy of a window message persisted with a fresh
            # ts (the window's own copy is NOT on disk at this ts) → folded into
            # the window by the bounded content tiebreak (no duplication).
            json.dumps({"role": "assistant", "content": "aif-copy", "ts": "TY"}),
            # Genuinely distinct content → preserved as foreign.
            json.dumps({"role": "assistant", "content": "distinct", "ts": "T3"}),
        ]
        path.write_text("\n".join(disk_lines) + "\n", encoding="utf-8")

        window_entries = [
            {"role": "user", "content": "same-ts", "ts": "T1"},
            {"role": "assistant", "content": "windowed", "ts": "T2"},
            {"role": "assistant", "content": "aif-copy", "ts": "TZ"},
        ]
        slot._frozen_prefix_cache = None

        _prefix, foreign, dedup_dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries
        )
        foreign_contents = [json.loads(ln)["content"] for ln in foreign]

        # ts-match collapsed (window wins on in-place edits / its own copies).
        assert "same-ts" not in foreign_contents
        # No-loss: a distinct same-content event with a fresh ts, beyond the
        # window's single budget for that content, is preserved as foreign.
        assert foreign_contents.count("windowed") == 1, (
            "distinct same-content foreign event collapsed — the (role, content) "
            "dedup must be count-bounded/timestamp-first so two real events are "
            "not folded into one (GPT 5.6 HIGH data-loss finding)"
        )
        # No-duplication: the append_if_absent fresh-ts copy is folded once into
        # the window (its own budget absorbs it) and NOT re-appended as foreign.
        assert "aif-copy" not in foreign_contents, (
            "append_if_absent fresh-ts copy duplicated — the bounded tiebreak "
            "must fold the window's own copy"
        )
        # A genuinely-distinct foreign line is preserved (no data loss).
        assert "distinct" in foreign_contents
        # The folded fresh-ts copy is routed through the archive (item 2b) so the
        # residual ambiguous case loses no data permanently.
        dropped_contents = [json.loads(ln)["content"] for ln in dedup_dropped]
        assert dropped_contents == ["aif-copy"], (
            "fresh-ts content-tiebreak drops must be surfaced for archiving "
            f"(got {dropped_contents!r})"
        )


class TestForeignFoldMidIdentity:
    """Pin the ``meta.mid`` tier (pass 0) of the save-side foreign-merge fold.

    Every window append mints a stable per-message id (``meta.mid``), a save
    persists it, and the durable-copy writers (workflow/cron injectors, CLI)
    carry the window row's id onto their copy — so the id is on
    BOTH sides of the fold's comparison. Pass 0 uses it: an id match IS the
    same message (folded silently, never archived); an id-carrying disk line
    whose id matches NO window entry is foreign regardless of body equality
    (two genuinely distinct identical-content messages carry distinct ids);
    id-less lines keep the legacy timestamp-first ladder byte-identically
    (pinned by ``test_foreign_append_content_identity_dedup_semantics``).
    """

    def _make_state(self, tmp_path: Path):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def _fold(self, tmp_path, monkeypatch, slot_name, disk_entries, window_entries):
        """Write *disk_entries* (after a metadata line) and run the fold."""
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import (
            _frozen_prefix_and_foreign_appends,
        )
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot(slot_name)
        history_key = _history_key_for(slot.key)
        path = state.conversation_log._path(history_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"_type": "metadata", "created": "2026-01-01T00:00:00Z"})]
        lines.extend(json.dumps(e) for e in disk_entries)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        slot._frozen_prefix_cache = None
        return _frozen_prefix_and_foreign_appends(slot, path, 0, window_entries)

    def test_id_matched_durable_copy_folds_without_archive_churn(self, tmp_path, monkeypatch):
        """An injector's durable copy (fresh ts, SAME ``meta.mid`` as its window
        row) folds in pass 0 with an EMPTY ``dedup_dropped``: the ids matching
        exactly makes it unambiguous, so it must not be routed to the
        ``foreign-dedup`` archive the way the id-less fresh-ts tiebreak is
        (consequence 1 — archive churn on every injection+save).
        """
        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midfold",
            disk_entries=[
                # Durable copy: fresh ts TY (window's in-memory ts is TZ), same id.
                {
                    "role": "assistant",
                    "content": "inj",
                    "ts": "TY",
                    "meta": {"mid": "m-inj"},
                }
            ],
            window_entries=[
                {
                    "role": "assistant",
                    "content": "inj",
                    "ts": "TZ",
                    "meta": {"mid": "m-inj"},
                }
            ],
        )
        assert foreign == [], "id-matched durable copy must fold into the window"
        assert dedup_dropped == [], (
            "an id-matched fold is exact, not ambiguous — it must not churn "
            f"the foreign-dedup archive (got {dedup_dropped!r})"
        )

    def test_distinct_ids_with_identical_content_stay_distinct(self, tmp_path, monkeypatch):
        """Two identical-content rows with DISTINCT ids resolve exactly: the
        window row's copy (same id) folds silently, the genuinely distinct row
        (different id) is preserved as foreign (consequence 2 — the
        residual ambiguity the count-bounded tiebreak could only bound).
        """
        import json

        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "mid2",
            disk_entries=[
                # The window row's own durable copy (fresh ts, same id) → folded.
                {
                    "role": "assistant",
                    "content": "status ok",
                    "ts": "TA",
                    "meta": {"mid": "m-ours"},
                },
                # A distinct cron result with the SAME text but its OWN id →
                # foreign, even though the body tiebreak budget is exhausted
                # only after the fold above.
                {
                    "role": "assistant",
                    "content": "status ok",
                    "ts": "TB",
                    "meta": {"mid": "m-theirs"},
                },
            ],
            window_entries=[
                {
                    "role": "assistant",
                    "content": "status ok",
                    "ts": "TW",
                    "meta": {"mid": "m-ours"},
                }
            ],
        )
        foreign_mids = [json.loads(ln)["meta"]["mid"] for ln in foreign]
        assert foreign_mids == ["m-theirs"], (
            "the distinct-id row must be preserved as foreign and the same-id "
            f"copy folded (got foreign mids {foreign_mids!r})"
        )
        assert dedup_dropped == [], "id-tier resolutions must not archive anything"

    def test_unmatched_id_beats_body_tiebreak(self, tmp_path, monkeypatch):
        """An id-carrying disk line whose id is absent from the window is
        FOREIGN even though its body matches an unconsumed window entry — the
        distinct id is authoritative, so the tier-(c) body tiebreak must not
        absorb it (and must not archive it as an ambiguous drop).
        """
        import json

        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midforeign",
            disk_entries=[
                {
                    "role": "assistant",
                    "content": "same text",
                    "ts": "TB",
                    "meta": {"mid": "m-other"},
                }
            ],
            window_entries=[
                # Unconsumed window entry with matching body but a different id:
                # its (role, content) budget is AVAILABLE, and must still not
                # absorb the distinct-id line.
                {
                    "role": "assistant",
                    "content": "same text",
                    "ts": "TW",
                    "meta": {"mid": "m-mine"},
                }
            ],
        )
        assert [json.loads(ln)["meta"]["mid"] for ln in foreign] == ["m-other"], (
            "a disk line whose id matches no window entry must be preserved as "
            "foreign regardless of body equality"
        )
        assert dedup_dropped == [], "an id-settled foreign line is not an ambiguous drop"

    def test_legacy_line_still_folds_into_id_carrying_window_entry(self, tmp_path, monkeypatch):
        """A restored legacy transcript: the on-disk line is id-less, but the
        restore minted the window row a fresh id. The exact (ts, role, content)
        tier must still fold the line — an id-carrying window entry stays
        indexed in the legacy tiers, or every save after a restart would
        duplicate the whole pre-id transcript.
        """
        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midlegacy",
            disk_entries=[
                {"role": "assistant", "content": "old row", "ts": "T1"},
            ],
            window_entries=[
                # The same row post-restore: identical triple, restore-minted id.
                {
                    "role": "assistant",
                    "content": "old row",
                    "ts": "T1",
                    "meta": {"mid": "m-minted"},
                }
            ],
        )
        assert foreign == [], (
            "an id-less legacy line must still exact-fold into its restored "
            "window row (else every post-restart save duplicates the transcript)"
        )
        assert dedup_dropped == [], "an exact fold is silent"

    def test_mixed_id_and_idless_lines_in_one_ts_group_lose_nothing(self, tmp_path, monkeypatch):
        """Opus 5 review HIGH: an id-foreign line must keep its ``ts`` group
        AMBIGUOUS. If it were excluded from the disk-side counter, a group
        holding an id-carrying foreign line PLUS an id-less foreign line and
        exactly one unmatched window entry with the same ``ts`` (coarse-clock
        collision) would read as an unambiguous 1:1, and the ts-only tier
        would silently DROP the id-less acknowledged append — the exact
        data-loss class the ambiguity gate exists to prevent. Both lines must
        be preserved as foreign, matching the pre-id-tier behaviour.
        """
        import json

        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midmixts",
            disk_entries=[
                # Id-carrying foreign line (unknown id) colliding on ts T.
                {
                    "role": "assistant",
                    "content": "x",
                    "ts": "T",
                    "meta": {"mid": "m-2"},
                },
                # Id-less foreign line sharing the same coarse-clock ts T.
                {"role": "assistant", "content": "y", "ts": "T"},
            ],
            window_entries=[
                {
                    "role": "assistant",
                    "content": "a",
                    "ts": "T",
                    "meta": {"mid": "m-1"},
                }
            ],
        )
        foreign_contents = [json.loads(ln)["content"] for ln in foreign]
        assert foreign_contents == ["x", "y"], (
            "both foreign lines in the contested ts group must survive — the "
            "id-foreign line may not convert the group into a silent 1:1 fold "
            f"(got {foreign_contents!r})"
        )
        assert dedup_dropped == []

    def test_reused_mid_with_distinct_body_falls_back_conservatively(self, tmp_path, monkeypatch):
        """GPT 5.6 review HIGH: ``meta.mid`` is caller-suppliable
        (``_ChatSlot.append`` preserves a pre-existing id, and ``/api/chat``
        meta rides through), so bare id equality may pair two genuinely
        distinct messages. An id match corroborated by NEITHER body nor ``ts``
        must not consume the window entry: the line falls back to the legacy
        ladder (preserved as foreign here), and a later id-less exact copy of
        the entry still folds — the outcome cannot depend on the reused-id
        line stealing the entry's budget.
        """
        import json

        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midreuse",
            disk_entries=[
                # Reused id, but different body AND different ts: a genuinely
                # distinct message that happens to carry the window row's id.
                {
                    "role": "assistant",
                    "content": "distinct msg",
                    "ts": "T2",
                    "meta": {"mid": "m-reused"},
                },
                # The window row's own id-less exact persisted copy — must
                # still fold into the entry the reused-id line did not steal.
                {"role": "assistant", "content": "a", "ts": "T1"},
            ],
            window_entries=[
                {
                    "role": "assistant",
                    "content": "a",
                    "ts": "T1",
                    "meta": {"mid": "m-reused"},
                }
            ],
        )
        foreign_contents = [json.loads(ln)["content"] for ln in foreign]
        assert foreign_contents == ["distinct msg"], (
            "an uncorroborated id match must not silently drop a distinct "
            f"message (got {foreign_contents!r})"
        )
        assert (
            dedup_dropped == []
        ), "the exact copy folds via the exact tier, not the archive tiebreak"

    def test_id_match_corroborated_by_ts_folds_silently(self, tmp_path, monkeypatch):
        """The ``ts`` half of the corroboration rule: an id-matched line whose
        ``ts`` equals the entry's (an in-place edit of a durably-copied row —
        same id, same ts, superseded body) is the entry's own persisted copy
        and folds silently, window version winning, no archive churn.
        """
        _prefix, foreign, dedup_dropped = self._fold(
            tmp_path,
            monkeypatch,
            "midtscorr",
            disk_entries=[
                {
                    "role": "assistant",
                    "content": "OLD body",
                    "ts": "T1",
                    "meta": {"mid": "m-edit"},
                }
            ],
            window_entries=[
                # Same id, same ts, edited content: the window wins.
                {
                    "role": "assistant",
                    "content": "NEW body",
                    "ts": "T1",
                    "meta": {"mid": "m-edit"},
                }
            ],
        )
        assert foreign == [], "the stale persisted copy must fold, window wins"
        assert dedup_dropped == [], "an id+ts corroborated fold is silent"

    def test_id_match_corroborated_by_a_preserved_image_folds(self, tmp_path, monkeypatch):
        """Same id, fresh ts, bodies differing ONLY in an image destination the
        durable copy preserved (the window entry's own rewrite failed open because
        the agent's scratch file was already gone): pass 0 folds it. Without the
        image allowance the id match has no corroboration, the line falls through
        the id-less tiers, and the transcript keeps two copies of one message,
        one naming a dead file.
        """
        import json

        from kiro_crew.chat_attachments import attachments_dir
        from kiro_crew.dashboard.chat_persistence import _frozen_prefix_and_foreign_appends
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("imgfold")
        path = state.conversation_log._path(_history_key_for(slot.key))
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = attachments_dir(path.parent, path.stem) / "0123456789abcdef-shot.png"
        scratch = tmp_path / "scratch" / "shot.png"  # never created: it is gone
        disk_entries = [
            {
                "role": "assistant",
                "content": f"see ![shot]({stored})",
                "ts": "TY",
                "meta": {"mid": "m-img"},
            }
        ]
        lines = [json.dumps({"_type": "metadata", "created": "2026-01-01T00:00:00Z"})]
        lines.extend(json.dumps(e) for e in disk_entries)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        slot._frozen_prefix_cache = None
        window_entries = [
            {
                "role": "assistant",
                "content": f"see ![shot]({scratch})",
                "ts": "TZ",
                "meta": {"mid": "m-img"},
            }
        ]

        _prefix, foreign, dedup_dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries
        )
        assert foreign == [], "the preserved-image durable copy must fold into the window"
        assert dedup_dropped == []

        # The allowance is for preserved images only: a same-id line whose TEXT
        # differs is still uncorroborated and still preserved.
        slot._frozen_prefix_cache = None
        window_entries[0]["content"] = f"look ![shot]({scratch})"
        _prefix, foreign, _dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries
        )
        assert len(foreign) == 1

    def test_a_save_that_keeps_foreign_lines_logs_one_warning(self, tmp_path, monkeypatch, caplog):
        """A save that keeps another writer's lines warns once per chat: a
        re-scan after a trim is quiet, a swapped line warns again, and a save
        with nothing foreign is quiet.
        """
        import json
        import logging

        from kiro_crew.dashboard.chat_persistence import _frozen_prefix_and_foreign_appends

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        caplog.set_level(logging.WARNING, logger="kiro_crew.dashboard.chat_persistence")
        slot = self._make_state(tmp_path).get_or_create_slot("kc_warn_foreign")
        path = tmp_path / "kc_warn_foreign.jsonl"
        mine = {"role": "user", "content": "mine", "ts": "T1", "meta": {"mid": "m-1"}}

        def scan(*disk):
            lines = [json.dumps({"_type": "metadata", "created": "2026-01-01T00:00:00Z"})]
            path.write_text("\n".join(lines + [json.dumps(e) for e in disk]) + "\n")
            slot._frozen_prefix_cache = None  # what a _MAX_SLOT_MESSAGES trim does
            caplog.clear()
            _p, foreign, _d = _frozen_prefix_and_foreign_appends(slot, path, 0, [mine])
            got = [r.getMessage() for r in caplog.records]
            return foreign, [m for m in got if "another writer appended" in m]

        other = {"role": "assistant", "content": "theirs", "ts": "T2", "meta": {"mid": "m-2"}}
        foreign, warned = scan(mine, other)
        assert len(foreign) == 1 and len(warned) == 1
        assert " found 1 new line(s) " in warned[0] and "kc_warn_foreign" in warned[0]
        assert "theirs" not in warned[0], "no message content in the log"

        foreign, warned = scan(mine, other)  # same kept line, cache dropped
        assert len(foreign) == 1 and warned == []

        swapped = {**other, "ts": "T3", "meta": {"mid": "m-3"}}
        foreign, warned = scan(mine, swapped)  # same count, different line
        assert len(foreign) == 1 and len(warned) == 1

        foreign, warned = scan(mine)
        assert foreign == [] and warned == []


class TestBestEffortSaveMarksDirty:
    """GPT 5.6 HIGH (chat_persistence.py:1087): metadata mutation endpoints
    (pin / folder / tag / mode) call ``save_slot_off_loop(..., force=True)`` with
    the default ``best_effort=True``. A lock timeout / I/O error was swallowed and
    the slot was NOT marked dirty, so the periodic flush never retried — the
    endpoint returned success but the acknowledged change was lost after restart.

    The fix marks the slot dirty on a swallowed best-effort failure so the 5s
    flush retries. ``save_slot_off_loop`` is a coroutine, so its body always runs
    under a running loop and takes the offloaded (executor) branch — that is the
    path the dashboard metadata endpoints exercise.
    """

    def _make_state(self, tmp_path: Path):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_offloaded_best_effort_failure_marks_dirty(self, tmp_path, monkeypatch):
        import asyncio

        from kiro_crew.dashboard import chat_persistence

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("beffail")
        slot.append("user", "m1")
        slot.drain()
        slot._dirty = False

        def _boom(*_a, **_k):
            raise RuntimeError("simulated lock timeout / io error")

        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _boom)

        async def _run() -> None:
            # On a running loop → offloaded (executor) branch.
            await chat_persistence.save_slot_off_loop(state, slot, force=True)

        asyncio.run(_run())

        assert slot._dirty is True, (
            "a swallowed best-effort save failure must mark the slot dirty so the "
            "periodic flush retries — otherwise the metadata change is lost"
        )
