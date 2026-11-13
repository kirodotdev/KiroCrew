"""Pins that a dashboard slot save loses no acknowledged write, its own or another's.

``_save_slot_to_history`` rewrites a session JSONL from the slot's frozen prefix and
window; the foreign-line fold in ``_frozen_prefix_and_foreign_appends`` is pinned on
both identity tiers. The ``ConversationLog`` lock is pinned in
``test_history_locking_remediation.py`` and
``test_history_locking_remediation_locked_section.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

from kiro_crew.history import ConversationLog


# ── Bug 7: dashboard persistence save must hold _locked vs. append_off_loop ──
class TestDashboardSaveHoldsLock:
    """``_save_slot_to_history`` holds the same ``_locked`` as :meth:`ConversationLog.append`.

    A competitor thread appends during the save's ``atomic_write``; without the lock
    the file replace would delete that acknowledged append.
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
        """The save merges an append another process committed before it took the lock.

        The ``window`` snapshot is taken before ``_locked``, so the append lands in that
        gap; here it is committed to the file first, without threads.
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
        """Every frozen-prefix fast-path save re-emits a preserved foreign line once.

        With no writer between saves 2..4, each matches ``slot._frozen_prefix_cache``;
        a fast path returning no foreign lines would drop the preserved append.
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
        """The id-less fallback: timestamp-first identity with a count-bounded body tiebreak.

        Every line here is id-less, so pass 0 never fires. A disk line is ours when its
        ``ts`` matches a window entry, or its ``(role, content)`` matches an unconsumed
        one (an ``append_if_absent`` copy with a fresh ``ts``). Each entry absorbs at
        most one copy, so a second identical line with a distinct ts stays foreign.
        Id tier: ``TestForeignFoldMidIdentity``, docs/system-specs/modules/history.md.
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
            slot, path, 0, window_entries, archive_dedup=lambda lines: None
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

    Durable-copy writers carry the window row's ``meta.mid``, so an id match is the same
    message (folded, never archived) and an unmatched id is foreign even with an equal
    body. Id-less lines keep the timestamp-first ladder.
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
        return _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries, archive_dedup=lambda lines: None
        )

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
        """An id-foreign line keeps its ``ts`` group ambiguous.

        Left out of the disk-side count, an id-foreign plus an id-less line against one
        same-``ts`` window entry would read as 1:1 and drop the id-less append.
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
        """An id match backed by neither body nor ``ts`` does not consume the window entry.

        ``meta.mid`` is caller-suppliable (``_ChatSlot.append``, ``/api/chat`` meta), so
        the line falls back to the ladder and a later id-less exact copy still folds.
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
            slot, path, 0, window_entries, archive_dedup=lambda lines: None
        )
        assert foreign == [], "the preserved-image durable copy must fold into the window"
        assert dedup_dropped == []

        # The allowance is for preserved images only: a same-id line whose TEXT
        # differs is still uncorroborated and still preserved.
        slot._frozen_prefix_cache = None
        window_entries[0]["content"] = f"look ![shot]({scratch})"
        _prefix, foreign, _dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries, archive_dedup=lambda lines: None
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
            path.write_text(
                "\n".join(lines + [json.dumps(e) for e in disk]) + "\n", encoding="utf-8"
            )
            slot._frozen_prefix_cache = None  # what a _MAX_SLOT_MESSAGES trim does
            caplog.clear()
            _p, foreign, _d = _frozen_prefix_and_foreign_appends(
                slot, path, 0, [mine], archive_dedup=lambda lines: None
            )
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
    """A swallowed best-effort save failure marks the slot dirty so the 5s flush retries.

    Metadata endpoints (pin / folder / tag / mode) save with ``best_effort=True``.
    ``save_slot_off_loop`` always takes the executor branch, as those endpoints do.
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
