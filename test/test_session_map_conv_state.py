"""Per-conversation state on the ``SessionMap`` entry: privacy flags and overrides.

State survives a reload, never clobbers ``sid`` / Slack-link fields, and a bare
``thread_ts`` and its ``slack:`` key resolve to the SAME entry.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.session_map import SessionMap


def _make_kiro_session(kiro_dir, sid: str) -> None:
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
    (kiro_dir / f"{sid}.jsonl").write_text('{"x":1}\n{"y":2}\n', encoding="utf-8")


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", kiro)
    return tmp_path, kiro


class TestFlags:
    def test_flag_defaults_false(self, patched):
        sm = SessionMap()
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("missing", "incognito") is False

    def test_set_and_get_flag(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        assert sm.get_flag("slack:1.2", "temporary") is True
        # other flags remain independent
        assert sm.get_flag("slack:1.2", "incognito") is False

    def test_flag_persists_across_reload(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "incognito", True)
        # fresh instance == reload from disk
        assert SessionMap().get_flag("slack:1.2", "incognito") is True

    def test_clear_flag_removes_it(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "temporary", False)
        assert sm.get_flag("slack:1.2", "temporary") is False
        # clearing the last flag drops the sub-dict entirely (no accretion)
        raw = json.loads((patched[0] / "session_map.json").read_text(encoding="utf-8"))
        assert "flags" not in raw["slack:1.2"]

    def test_clear_flag_on_missing_key_creates_no_entry(self, patched):
        # Clearing a flag on a key that was never stored is a no-op and must
        # NOT materialize a blank entry on disk (phantom-entry accretion).
        sm = SessionMap()
        sm.set_flag("slack:never.stored", "temporary", False)
        assert sm.get_flag("slack:never.stored", "temporary") is False
        path = patched[0] / "session_map.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert "slack:never.stored" not in raw

    def test_two_flags_coexist(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "incognito", True)
        sm.set_flag("slack:1.2", "temporary", False)
        # clearing one leaves the other
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("slack:1.2", "incognito") is True

    def test_bare_and_namespaced_key_same_entry(self, patched):
        sm = SessionMap()
        sm.set_flag("1.2", "temporary", True)  # bare thread_ts
        assert sm.get_flag("slack:1.2", "temporary") is True  # namespaced read


class TestPrivacyFlagsAndPrune:
    """A ``temporary`` / ``incognito`` flag keeps its entry through both stale
    paths and NO startup step removes it; the transcript header is ensured off
    the event loop wherever a transcript exists.

    ``privacy_mode.hydrate`` reads the session map alone, so a removed row reads as
    persistent after a restart. ``prune()`` keeps the row without reading a transcript
    (it runs on the loop); ``stamp_privacy_headers()`` stamps on a worker thread.
    """

    KEY = "telegram:kirocrew:direct:4242"

    def test_flag_names_are_the_privacy_mode_names_strictest_first(self):
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import _PRIVACY_STRICTNESS

        assert set(_PRIVACY_STRICTNESS) == {
            privacy_mode.MODE_TEMPORARY,
            privacy_mode.MODE_INCOGNITO,
        }
        # The stamp compares strictness the way ``privacy_mode.strictest`` does.
        assert list(_PRIVACY_STRICTNESS) == [
            m for m in privacy_mode._STRICTNESS if m in _PRIVACY_STRICTNESS
        ]

    @staticmethod
    def _log(*, seed: bool, mode: str | None = None, key: str | None = None):
        """The thread's default-directory transcript: seeded with a turn, optionally stamped.

        Call it off the loop (``await asyncio.to_thread(self._log, ...)``):
        ``replace_with_retry`` retries the Windows sharing violation only when no loop
        runs on the calling thread.
        """
        from kiro_crew.history import ConversationLog

        key = key or TestPrivacyFlagsAndPrune.KEY
        log = ConversationLog()
        log.init()
        if seed:
            log.append(key, "user", "a pre-modifier turn")
        if mode is not None:
            log.update_metadata_if(key, {"memory_mode": mode}, lambda _m: True)
        return log

    @pytest.mark.parametrize("flag", ["temporary", "incognito"])
    def test_startup_prune_keeps_a_stale_flagged_entry_and_reads_no_transcript(
        self, patched, monkeypatch, flag
    ):
        """The loop-side half: sid cleared, flag kept, on disk -- and no header read."""
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(
            ConversationLog,
            "get_metadata",
            lambda self, key: pytest.fail("prune read a transcript"),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")  # no such file under the kiro dir
        sm.set_flag(self.KEY, flag, True)
        assert sm.prune() == 0
        assert sm.get_flag(self.KEY, flag) is True
        assert (sm._data.get(self.KEY) or {}).get("sid") == ""
        assert SessionMap().get_flag(self.KEY, flag) is True
        # Naming the rows whose header must record the mode is entry-state work.
        assert sm.privacy_flagged_entries() == {self.KEY: [flag]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["temporary", "incognito"])
    async def test_a_row_whose_header_already_carries_the_mode_is_kept(self, patched, flag):
        """The header is complete and the row STILL stays: it is what the
        channel gate hydrates from, whatever the header says."""
        await asyncio.to_thread(self._log, seed=True, mode=flag)
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, flag, True)
        assert sm.prune() == 0
        assert await sm.stamp_privacy_headers() == 1
        assert self.KEY in sm._data
        assert sm.get_flag(self.KEY, flag) is True
        sm.flush()
        assert SessionMap().get_flag(self.KEY, flag) is True

    @pytest.mark.asyncio
    async def test_the_off_loop_step_stamps_a_legacy_rows_mode_into_the_header_and_keeps_the_row(
        self, patched
    ):
        """A row flagged before headers were stamped: the mode moves into the
        existing transcript's header -- the record the memory readers refuse on
        -- and the row stays for the channel gate."""
        log = await asyncio.to_thread(self._log, seed=True)
        assert "memory_mode" not in log.get_metadata(self.KEY), "premise: legacy header"
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        assert sm.prune() == 0
        assert await sm.stamp_privacy_headers() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        assert self.KEY in sm._data
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_a_stricter_flag_tightens_the_header(self, patched):
        """``!incognito`` then ``!temporary`` with the header still saying incognito:
        the header is tightened to temporary; the row stays with both flags."""
        log = await asyncio.to_thread(self._log, seed=True, mode="incognito")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.set_flag(self.KEY, "temporary", True)
        sm.prune()
        assert await sm.stamp_privacy_headers() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "temporary"
        assert sm.get_flag(self.KEY, "temporary") is True
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_a_second_boot_writes_no_header_that_already_records_the_mode(
        self, patched, monkeypatch
    ):
        """The startup stamp visits every flagged row on every boot; a header that
        already records the mode must cost no write -- the stamp is tighten-only AND
        write-free at equality (``privacy_mode.needs_tightening``). Mutation: compare
        with plain ``strictest`` -- the second boot rewrites the header again."""
        from kiro_crew.history import ConversationLog

        log = await asyncio.to_thread(self._log, seed=True)  # legacy header: no mode
        writes: list[str] = []
        real_write = ConversationLog._update_metadata_locked

        def _counting(self_log, key, fields):
            writes.append(key)
            return real_write(self_log, key, fields)

        monkeypatch.setattr(ConversationLog, "_update_metadata_locked", _counting)
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.stamp_privacy_headers() == 1  # boot one: the header is stamped
        assert writes == [self.KEY]
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        assert await sm.stamp_privacy_headers() == 1  # boot two: recorded, not rewritten
        assert writes == [self.KEY], "an unchanged header was rewritten by the startup stamp"

    @pytest.mark.asyncio
    async def test_a_header_spelled_temporary_in_mixed_case_is_not_loosened_by_the_stamp(
        self, patched
    ):
        """A header is not bound by the API's validation: ``Temporary`` (a hand edit,
        a foreign writer) is the stricter mode, spelled differently. Compared raw it
        is UNKNOWN to ``strictest`` -- weaker than anything -- so an incognito stamp
        would overwrite it and the thread would read looser than written. The compare
        normalizes first (``transcript_privacy_mode``, the shared predicate's rule):
        the header keeps what it spells and counts as recording the flag. Mutation:
        compare the raw string -- the header reads ``incognito``."""
        from kiro_crew.history import transcript_privacy_mode

        log = await asyncio.to_thread(self._log, seed=True, mode="Temporary")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.stamp_privacy_headers() == 1
        header = log.get_metadata(self.KEY).get("memory_mode")
        assert header == "Temporary", "the incognito stamp overwrote a stricter header"
        assert transcript_privacy_mode(header) == "temporary"

    @pytest.mark.asyncio
    async def test_a_flagged_entry_with_no_transcript_is_kept_and_none_is_created(self, patched):
        """``!incognito`` as the very first message: no sid, no transcript. Nothing to
        stamp (a refusal must never create a transcript), and the row stays."""
        from kiro_crew.history import ConversationLog

        sm = SessionMap()
        sm.set_flag(self.KEY, "incognito", True)
        assert sm.prune() == 0
        assert await sm.stamp_privacy_headers() == 0
        assert sm.get_flag(self.KEY, "incognito") is True
        assert "memory_mode" not in ConversationLog().get_metadata(self.KEY)

    @pytest.mark.asyncio
    async def test_an_unwritable_or_unreadable_header_is_not_reported_as_recorded(
        self, patched, monkeypatch
    ):
        """Unwritable is not "recorded": the sweep raises naming the row, and the row stays."""
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging.privacy_mode import PrivacySweepIncomplete

        await asyncio.to_thread(self._log, seed=True, mode="incognito")
        monkeypatch.setattr(
            ConversationLog,
            "update_metadata_if",
            lambda self, *a, **kw: (_ for _ in ()).throw(OSError("read-only")),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        with pytest.raises(PrivacySweepIncomplete) as incomplete:
            await sm.stamp_privacy_headers()
        assert incomplete.value.keys == [self.KEY]
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_a_message_first_transcript_is_not_a_repair_owed(self, patched):
        """A legacy transcript whose first line is a MESSAGE (valid JSON, not a
        header) is one no line writer will stamp, so the sweep counts it not
        recorded and finishes rather than raising forever. Mutation: raise for
        it like any other existing transcript whose header is too weak -- red."""
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        path = log._path(self.KEY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"role": "user", "content": "hello"}\n', encoding="utf-8")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.stamp_privacy_headers() == 0
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_a_live_rows_header_is_stamped_too_and_its_sid_untouched(self, patched):
        """The header is the record for the THREAD, not for the provider session
        that happens to serve it: a row whose session is live is a candidate as
        well, stamped and left exactly as it was."""
        tmp, kiro = patched
        log = await asyncio.to_thread(self._log, seed=True)
        _make_kiro_session(kiro, "sid-alive")
        sm = SessionMap()
        sm.set(self.KEY, "sid-alive")
        sm.set_flag(self.KEY, "incognito", True)
        assert sm.prune() == 0
        assert sm.privacy_flagged_entries() == {self.KEY: ["incognito"]}
        assert await sm.stamp_privacy_headers() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        assert sm.get_flag(self.KEY, "incognito") is True and sm.get(self.KEY) == "sid-alive"

    @pytest.mark.asyncio
    async def test_a_channel_bound_rows_header_is_stamped_too(self, patched):
        """A Slack-bound private thread: the binding already keeps the row; the
        header is ensured for it like for any other flagged row."""
        bound = "slack:1700000000.000100"
        log = await asyncio.to_thread(self._log, seed=True, key=bound)
        sm = SessionMap()
        sm.set_slack_link(bound, "1700000000.000100", "C0BOUND")
        sm.set_flag(bound, "temporary", True)
        assert sm.prune() == 0
        assert sm.privacy_flagged_entries() == {bound: ["temporary"]}
        assert await sm.stamp_privacy_headers() == 1
        assert log.get_metadata(bound).get("memory_mode") == "temporary"
        assert sm.get_flag(bound, "temporary") is True

    def test_the_per_read_repair_keeps_the_flag_and_reads_no_transcript(self, patched, monkeypatch):
        """``get()`` on a stale sid keeps the entry whatever the header says; the
        header stamp belongs to the startup step, off the loop."""
        from kiro_crew.history import ConversationLog

        self._log(seed=True, mode="temporary")
        monkeypatch.setattr(
            ConversationLog,
            "get_metadata",
            lambda self, key: pytest.fail("the repair read a transcript"),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "temporary", True)
        assert sm.get(self.KEY) is None
        assert sm.get_flag(self.KEY, "temporary") is True

    @pytest.mark.asyncio
    async def test_the_header_probe_never_runs_on_the_loop_thread(self, patched, monkeypatch):
        """The whole point of the split: the transcript I/O is a worker thread's."""
        import threading

        from kiro_crew.history import ConversationLog

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_get = ConversationLog.get_metadata
        real_update = ConversationLog.update_metadata_if

        def _recording_get(self, key):
            seen.append(threading.current_thread())
            return real_get(self, key)

        def _recording_update(self, *a, **kw):
            seen.append(threading.current_thread())
            return real_update(self, *a, **kw)

        await asyncio.to_thread(self._log, seed=True, mode="incognito")
        monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get)
        monkeypatch.setattr(ConversationLog, "update_metadata_if", _recording_update)
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.stamp_privacy_headers() == 1
        assert seen, "premise: the header was probed"
        assert [t for t in seen if t is loop_thread] == []

    @pytest.mark.asyncio
    async def test_a_flag_tightened_during_the_probe_is_re_stamped_by_the_next_pass(
        self, patched, monkeypatch
    ):
        """``!temporary`` lands while the worker probes an ``incognito`` row.

        The header keeps the pre-tightening mode (the stamp is tighten-only), the row
        carries both flags, and the next pass re-stamps the tightened mode.
        """
        from types import SimpleNamespace

        from kiro_crew import session_map as session_map_mod
        from kiro_crew.messaging import privacy_mode

        log = await asyncio.to_thread(self._log, seed=True, mode="incognito")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        real_probe = session_map_mod._header_records_privacy_mode
        seen_by_worker: list[list[str]] = []

        def _probe_then_tighten(key, flagged):
            seen_by_worker.append(list(flagged))
            recorded = real_probe(key, flagged)
            # The modifier arrives while the probe runs off the loop (a live
            # reload, not only boot): the row tightens under the worker.
            sm.set_flag(key, "temporary", True)
            return recorded

        monkeypatch.setattr(session_map_mod, "_header_records_privacy_mode", _probe_then_tighten)
        assert await sm.stamp_privacy_headers() == 1
        assert seen_by_worker == [["incognito"]], "premise: the worker judged the incognito row"
        # The row carries BOTH flags, on disk too.
        assert self.KEY in sm._data
        assert sm.get_flag(self.KEY, "temporary") is True
        assert sm.get_flag(self.KEY, "incognito") is True
        assert SessionMap().get_flag(self.KEY, "temporary") is True
        # The stamp is the mode the worker saw: not loosened, not yet tightened.
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        # The channel gate, hydrating from the map alone, reads the tightened mode.
        privacy_mode.reset()
        try:
            privacy_mode.hydrate(SimpleNamespace(_session_map=sm), self.KEY)
            assert privacy_mode.is_temporary(self.KEY) is True
        finally:
            privacy_mode.reset()
        # The next pass reads the current flags: header tightened, row still there.
        monkeypatch.setattr(session_map_mod, "_header_records_privacy_mode", real_probe)
        assert await sm.stamp_privacy_headers() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "temporary"
        assert self.KEY in sm._data
        assert sm.get_flag(self.KEY, "temporary") is True

    @pytest.mark.asyncio
    async def test_a_reservation_released_after_the_snapshot_is_not_re_stamped_by_the_sweep(
        self, patched, monkeypatch
    ):
        """A live-pool restart's sweep meets a Telegram reservation released after its snapshot.

        The second row is released between snapshot and stamp. Stamping from the snapshot
        would leave ``incognito`` in a header with no row, and nothing loosens it, so the
        sweep re-reads the row under its durable-write lock.
        """
        import threading
        from types import SimpleNamespace

        from kiro_crew import session_map as session_map_mod
        from kiro_crew.history import transcript_privacy_mode
        from kiro_crew.messaging import privacy_mode

        first, second = self.KEY, "telegram:kirocrew:direct:4343"
        log = await asyncio.to_thread(self._log, seed=True, mode="incognito")
        await asyncio.to_thread(self._log, seed=True, key=second)
        sm = SessionMap()
        sm.set_flag(first, "incognito", True)
        sessions = SimpleNamespace(_session_map=sm)
        privacy_mode.reset()
        try:
            reservation = await privacy_mode.reserve(
                "incognito", second, source="telegram", sessions=sessions
            )
            assert sm.get_flag(second, "incognito") is True, "premise: the reservation landed"
            assert log.get_metadata(second).get("memory_mode") == "incognito"
            assert list(sm.privacy_flagged_entries()) == [first, second]

            released = threading.Event()

            async def _release_while_the_sweep_stamps_the_first_row() -> bool:
                try:
                    return await privacy_mode.release(
                        reservation, sessions=sessions, source="telegram"
                    )
                finally:
                    released.set()

            real_probe = session_map_mod._header_records_privacy_mode
            probed: list[tuple[str, list[str], bool]] = []

            def _probe_after_the_release(key, flagged):
                # The worker reaches the first row while the release runs on
                # the loop; it waits for the release to land before probing,
                # so the second row is always reached AFTER its release.
                assert released.wait(timeout=10), "premise: the release landed while the sweep ran"
                probed.append((key, list(flagged), sm.get_flag(key, "incognito")))
                return real_probe(key, flagged)

            monkeypatch.setattr(
                session_map_mod, "_header_records_privacy_mode", _probe_after_the_release
            )
            # Scheduled before the sweep, so it runs at the sweep's first
            # suspension -- after the snapshot, which still names both rows.
            release = asyncio.get_running_loop().create_task(
                _release_while_the_sweep_stamps_the_first_row()
            )
            recorded = await sm.stamp_privacy_headers()
            assert await release is True, "premise: the release landed, not retained"
            assert probed[0] == (first, ["incognito"], True), "premise: the first row was stamped"

            header = log.get_metadata(second).get("memory_mode")
            assert transcript_privacy_mode(header) == "", (
                "the sweep re-stamped a mode the release had taken back: "
                f"header={header!r}, row={sm.get_flag(second, 'incognito')}, "
                f"marked={privacy_mode.is_incognito(second)}, probed={probed!r}"
            )
            assert header == "persistent", "the release's restore target stands"
            assert sm.get_flag(second, "incognito") is False
            assert privacy_mode.is_incognito(second) is False
            assert recorded == 1, "the first row recorded; the released row skipped, not counted"
            assert [key for key, _flags, _row in probed] == [
                first
            ], "a row the re-read finds unflagged is never probed"
            # The first row is untouched by the release next door.
            assert log.get_metadata(first).get("memory_mode") == "incognito"
            assert sm.get_flag(first, "incognito") is True
        finally:
            privacy_mode.reset()

    def test_the_sweep_stamps_under_the_rows_lock_after_a_re_read(self):
        """Structural: the probe runs inside ``async with _serialized(key)``, after ``privacy_flags``.

        Checked over the AST of ``privacy_mode.stamp_flagged_headers``: one ``async with``,
        the re-read inside it, and the probe handed to ``to_thread`` there.
        """
        import ast
        import inspect

        from kiro_crew.messaging import privacy_mode as privacy_mode_mod

        tree = ast.parse(inspect.getsource(privacy_mode_mod))
        sweep = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "stamp_flagged_headers"
        )
        blocks = [node for node in ast.walk(sweep) if isinstance(node, ast.AsyncWith)]
        assert len(blocks) == 1, "one lock block per row"
        context = blocks[0].items[0].context_expr
        assert isinstance(context, ast.Call) and getattr(context.func, "id", "") == "_serialized"

        def _calls(node: ast.AST, name: str) -> list[ast.Call]:
            return [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and name in (getattr(call.func, "attr", ""), getattr(call.func, "id", ""))
            ]

        re_read = _calls(blocks[0], "privacy_flags")
        handed = _calls(blocks[0], "to_thread")
        assert len(re_read) == 1 and len(handed) == 1
        assert re_read[0].lineno < handed[0].lineno, "the row is re-read before the probe"
        assert getattr(handed[0].args[0], "id", "") == "_header_records_privacy_mode"
        # The probe is passed to the worker, never called directly anywhere in the sweep.
        assert _calls(sweep, "_header_records_privacy_mode") == []
        assert _calls(sweep, "to_thread") == handed, "no probe outside the lock block"

    def test_an_unflagged_stale_entry_is_still_collected_by_prune(self, patched):
        """The keep rule is exactly the two privacy flags, not every entry."""
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        assert sm.prune() == 1


class TestStartPoolPrunesOffTheLoop:
    """``start_pool()`` prunes on the loop and awaits the header stamp off it."""

    @pytest.mark.asyncio
    async def test_start_pool_reads_no_transcript_header_on_the_loop_thread(
        self, patched, monkeypatch
    ):
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.session import SessionManager

        key = TestPrivacyFlagsAndPrune.KEY
        await asyncio.to_thread(TestPrivacyFlagsAndPrune._log, seed=True)
        seeded = SessionMap()
        seeded.set(key, "sid-reclaimed-by-kiro-cli")
        seeded.set_flag(key, "incognito", True)  # a legacy row: map flag, no header mode
        seeded.flush()

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_get = ConversationLog.get_metadata

        def _recording_get(self, k):
            seen.append(threading.current_thread())
            return real_get(self, k)

        monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.is_process_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            m.context_usage_unknown = lambda: False
            m.context_window_tokens = lambda: 0
            m.has_active_turn = lambda: False
            m.runtime_abort_target = lambda: None
            m.stream_command = MagicMock()
            return m

        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        mgr = SessionManager(cfg, provider_factory=factory)
        try:
            await mgr.start_pool()
            assert seen, "premise: start_pool probed the flagged row's header"
            assert [t for t in seen if t is loop_thread] == []
            # Stamped, and kept.
            assert real_get(ConversationLog(), key).get("memory_mode") == "incognito"
            assert mgr._session_map.get_flag(key, "incognito") is True
        finally:
            await mgr.close_all()


class TestANonBlockingStartDoesNotWaitForTheSweep:
    """``start_pool(blocking=False)`` returns before the privacy-header sweep runs.

    Live-config appliers and the dashboard restart must not wait on the sweep; the
    blocking path still awaits it. Asserted as an ORDER of events, never by timing.
    """

    @staticmethod
    def _hook_sweep(monkeypatch, events: list[str]):
        from kiro_crew.session_map import SessionMap

        async def _recording_sweep(self) -> int:
            events.append("sweep started")
            # Yield twice so an unawaited task interleaves visibly, then finish.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            events.append("sweep finished")
            return 0

        monkeypatch.setattr(SessionMap, "stamp_privacy_headers", _recording_sweep)

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        return SessionManager(cfg, provider_factory=_provider_factory)

    @pytest.mark.asyncio
    async def test_a_non_blocking_start_returns_before_the_sweep_completes(
        self, patched, monkeypatch
    ):
        events: list[str] = []
        self._hook_sweep(monkeypatch, events)
        mgr = self._manager()
        try:
            await mgr.start_pool(blocking=False)
            events.append("start_pool returned")
            # Let the scheduled task run to completion so the sweep's end is on record.
            for _ in range(10):
                await asyncio.sleep(0)
            assert "sweep finished" in events, f"the non-blocking start skipped the sweep: {events}"
            assert events.index("start_pool returned") < events.index(
                "sweep finished"
            ), f"a non-blocking start waited for the privacy-header sweep: {events}"
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_blocking_start_still_awaits_the_sweep_in_place(self, patched, monkeypatch):
        events: list[str] = []
        self._hook_sweep(monkeypatch, events)
        mgr = self._manager()
        try:
            await mgr.start_pool()
            events.append("start_pool returned")
            assert events == ["sweep started", "sweep finished", "start_pool returned"], events
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_non_blocking_start_still_stamps_the_header_off_the_loop(
        self, patched, monkeypatch
    ):
        """The real sweep, reached through the scheduled task: the legacy row's
        header is stamped on a worker thread, and the row is kept."""
        import threading

        from kiro_crew.history import ConversationLog

        key = TestPrivacyFlagsAndPrune.KEY
        await asyncio.to_thread(TestPrivacyFlagsAndPrune._log, seed=True)
        seeded = SessionMap()
        seeded.set(key, "sid-reclaimed-by-kiro-cli")
        seeded.set_flag(key, "incognito", True)  # a legacy row: map flag, no header mode
        seeded.flush()

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_get = ConversationLog.get_metadata

        def _recording_get(self, k):
            seen.append(threading.current_thread())
            return real_get(self, k)

        monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get)

        mgr = self._manager()
        try:
            await mgr.start_pool(blocking=False)
            assert (
                real_get(ConversationLog(), key).get("memory_mode") is None
            ), "premise: the header is stamped by the scheduled task, not before the return"
            pending = [t for t in mgr._background_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending)
            assert seen, "premise: the scheduled start probed the flagged row's header"
            assert [t for t in seen if t is loop_thread] == []
            assert real_get(ConversationLog(), key).get("memory_mode") == "incognito"
            assert mgr._session_map.get_flag(key, "incognito") is True
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_sweep_runs_on_every_start_and_before_the_background_start(
        self, patched, monkeypatch
    ):
        """A ``start_pool`` re-entry after a reset path runs the sweep AGAIN, before
        the background session.

        A failed release relies on the next sweep to restore the header, and a turn
        must not read a flagged thread as persistent. Asserted as an ORDER of events.
        """
        from kiro_crew.session_pool import WarmSessionPool

        events: list[str] = []

        real_ensure = WarmSessionPool._ensure_background_unless_lazy

        async def _recording_ensure(self) -> None:
            events.append("ensure_background")
            await real_ensure(self)

        async def _recording_sweep(self) -> int:
            events.append("sweep")
            await asyncio.sleep(0)
            return 0

        monkeypatch.setattr(WarmSessionPool, "_ensure_background_unless_lazy", _recording_ensure)
        monkeypatch.setattr(SessionMap, "stamp_privacy_headers", _recording_sweep)

        async def _drain(mgr) -> None:
            for _ in range(20):
                pending = [t for t in mgr._background_tasks if not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending)

        mgr = self._manager()
        try:
            await mgr.start_pool(blocking=False)
            await _drain(mgr)
            # A live-config apply / provider reload resets the gate and re-enters.
            mgr._pool_started = False
            await mgr.start_pool(blocking=False)
            await _drain(mgr)
            assert events.count("sweep") == 2, f"a start_pool re-entry skipped the sweep: {events}"
            assert events.count("ensure_background") == 2, events
            # Each sweep ran before its background start, so no turn can read a
            # flagged row as persistent before its header is stamped.
            assert events == ["sweep", "ensure_background"] * 2, events
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failed_header_stamp_is_retried_by_a_later_start(self, patched, monkeypatch):
        """A failed header stamp logs ``PrivacySweepIncomplete``; the next ``start_pool``
        repairs it.

        Until then header-only readers (``get_chat_session``'s guard) would serve the
        private transcript.
        """
        from kiro_crew.history import ConversationLog

        key = TestPrivacyFlagsAndPrune.KEY
        await asyncio.to_thread(TestPrivacyFlagsAndPrune._log, seed=True)
        seeded = SessionMap()
        seeded.set(key, "sid-reclaimed-by-kiro-cli")
        seeded.set_flag(key, "incognito", True)  # a legacy row: map flag, no header mode
        seeded.flush()

        real_update = ConversationLog.update_metadata_if

        def _failing_update(self, k, *args, **kwargs):
            if k == key:
                raise OSError("simulated header-write failure")
            return real_update(self, k, *args, **kwargs)

        mgr = self._manager()
        try:
            with pytest.MonkeyPatch.context() as failing:
                failing.setattr(ConversationLog, "update_metadata_if", _failing_update)
                await mgr.start_pool()
            assert (
                ConversationLog().get_metadata(key).get("memory_mode") is None
            ), "premise: the failed stamp left the header unrestricted"
            # A live-config apply / provider reload resets the gate and re-enters.
            mgr._pool_started = False
            await mgr.start_pool()
            assert (
                ConversationLog().get_metadata(key).get("memory_mode") == "incognito"
            ), "a failed stamp counted the sweep done, so the retry never repaired the header"
            assert mgr._session_map.get_flag(key, "incognito") is True
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_read_that_fails_then_recovers_is_a_repair_still_owed(
        self, patched, monkeypatch
    ):
        """A deferred write whose read-back shows the weaker header still owes a repair.

        The write defers on ``METADATA_LINE_TRANSIENT``, so the sweep raises
        ``PrivacySweepIncomplete`` naming the row rather than reporting a clean pass.
        """
        from kiro_crew.history import METADATA_LINE_TRANSIENT, ConversationLog
        from kiro_crew.messaging.privacy_mode import PrivacySweepIncomplete

        key = TestPrivacyFlagsAndPrune.KEY
        await asyncio.to_thread(TestPrivacyFlagsAndPrune._log, seed=True)
        sm = SessionMap()
        sm.set(key, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(key, "incognito", True)  # a legacy row: map flag, no header mode
        sm.prune()

        real_status = ConversationLog._read_metadata_status
        real_state = ConversationLog.metadata_line_state
        failing = {"status": 1, "state": 1}

        def _status_fails_once(self, k):
            if k == key and failing["status"]:
                failing["status"] -= 1
                return {}, False
            return real_status(self, k)

        def _state_transient_once(self, k):
            if k == key and failing["state"]:
                failing["state"] -= 1
                return METADATA_LINE_TRANSIENT
            return real_state(self, k)

        with pytest.MonkeyPatch.context() as flaky:
            flaky.setattr(ConversationLog, "_read_metadata_status", _status_fails_once)
            flaky.setattr(ConversationLog, "metadata_line_state", _state_transient_once)
            with pytest.raises(PrivacySweepIncomplete) as incomplete:
                await sm.stamp_privacy_headers()
        assert failing == {"status": 0, "state": 0}, "premise: the write met a failed read"
        assert incomplete.value.keys == [key]
        assert (
            ConversationLog().get_metadata(key).get("memory_mode") is None
        ), "premise: the deferred write left the header unrestricted"
        # The next sweep, with reads healthy again, stamps it.
        assert await sm.stamp_privacy_headers() == 1
        assert ConversationLog().get_metadata(key).get("memory_mode") == "incognito"


def _provider_factory(session_key=None, agent=None, channel_id=None, **kwargs):
    """A ``SessionManager`` provider double that never spawns anything."""
    from unittest.mock import AsyncMock, MagicMock

    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.is_process_alive = lambda: True
    m.context_usage_pct = lambda: 0.0
    m.context_usage_unknown = lambda: False
    m.context_window_tokens = lambda: 0
    m.has_active_turn = lambda: False
    m.runtime_abort_target = lambda: None
    m.stream_command = MagicMock()
    return m


class TestPrivacyRowsAcrossARestart:
    """A privacy-flagged channel row across a restart: kept, and the thread
    restricted for every reader, in all six cells.

    Rows: no ``sid`` yet, or a ``sid`` with no session file; transcripts: none, header
    without the mode, header with it. The channel gate reads the SESSION MAP and the
    memory readers read the HEADER, so the row stays in every cell.
    """

    KEY = "telegram:kirocrew:direct:5150"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "owned_a_sid", [False, True], ids=["never-owned-a-sid", "sid-reclaimed"]
    )
    @pytest.mark.parametrize("state", ["no-transcript", "header-lacks-mode", "header-carries-mode"])
    async def test_the_row_is_kept_and_the_thread_stays_restricted_after_a_restart(
        self, patched, state, owned_a_sid
    ):
        from types import SimpleNamespace

        from kiro_crew import execution_context
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog, transcript_withholds_derivation
        from kiro_crew.messaging import privacy_mode, upload_gate
        from kiro_crew.session import SessionManager

        key = self.KEY

        def _seed_transcript() -> None:
            # Off the loop, like every production header writer: the stamp's
            # rename retries a Windows sharing violation only there.
            log = ConversationLog()
            log.init()
            if state != "no-transcript":
                log.append(key, "user", "a turn written before the modifier")
            if state == "header-carries-mode":
                log.update_metadata_if(key, {"memory_mode": "incognito"}, lambda _m: True)

        await asyncio.to_thread(_seed_transcript)
        seeded = SessionMap()
        if owned_a_sid:
            seeded.set(key, "sid-reclaimed-by-kiro-cli")  # no such file under the kiro dir
        seeded.set_flag(key, "incognito", True)
        seeded.flush()
        entry = SessionMap()._data[key]
        assert bool(entry.get("sid")) is owned_a_sid, "premise: the row shape under test"

        # The restart: empty trackers, then the real startup path.
        privacy_mode.reset()
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        mgr = SessionManager(cfg, provider_factory=_provider_factory)
        try:
            await mgr.start_pool()
            observed = {
                "row_in_map": key in mgr._session_map._data,
                "flag": mgr._session_map.get_flag(key, "incognito"),
                "header_mode": ConversationLog().get_metadata(key).get("memory_mode"),
                # The channel gate, exactly as an inbound message asks it.
                "channel_gate_restricted": await upload_gate.session_is_restricted(
                    SimpleNamespace(sessions=mgr),
                    key,
                    persisted_probe=lambda _k: (False, None),
                ),
                # The header reader (execution_context.py, capture_session_execution).
                "execution_mode": execution_context.capture_session_execution(key).memory_mode,
                # The memory readers' verdict, off the header alone.
                "consolidation_withheld": transcript_withholds_derivation(ConversationLog(), key),
            }
        finally:
            await mgr.close_all()
            privacy_mode.reset()

        # The row's fate: kept, flag intact, on disk -- no restart can undo the
        # modifier for the channel gate.
        assert observed["row_in_map"] is True, observed
        assert observed["flag"] is True, observed
        assert SessionMap().get_flag(key, "incognito") is True, observed
        # The thread's readers after the restart: the channel gate off the map,
        # the memory readers off the header -- withheld wherever a transcript
        # exists (where none does there is nothing to derive from).
        assert observed["channel_gate_restricted"] is True, observed
        assert observed["consolidation_withheld"] is (state != "no-transcript"), observed
        # The header is ensured wherever a transcript exists (the worker stamps a
        # header that lacks the mode), and never created where none does.
        assert observed["header_mode"] == (
            None if state == "no-transcript" else "incognito"
        ), observed
        assert observed["execution_mode"] == (
            "persistent" if state == "no-transcript" else "incognito"
        ), observed


class TestOverrides:
    def test_agent_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        assert sm.get_agent_override("slack:1.2") == "researcher"
        assert SessionMap().get_agent_override("slack:1.2") == "researcher"

    def test_agent_override_clear(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        sm.set_agent_override("slack:1.2", None)
        assert sm.get_agent_override("slack:1.2") is None

    def test_project_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_project_override("slack:1.2", "/home/u/proj")
        assert sm.get_project_override("slack:1.2") == "/home/u/proj"
        assert SessionMap().get_project_override("slack:1.2") == "/home/u/proj"

    def test_missing_override_is_none(self, patched):
        sm = SessionMap()
        assert sm.get_agent_override("nope") is None
        assert sm.get_project_override("nope") is None


class TestNoClobber:
    def test_flag_preserves_sid(self, patched):
        tmp, kiro = patched
        _make_kiro_session(kiro, "sid-abc")
        sm = SessionMap()
        sm.set("slack:1.2", "sid-abc")
        sm.set_flag("slack:1.2", "temporary", True)
        # reload: both the live sid and the flag survive together
        sm2 = SessionMap()
        assert sm2.get("slack:1.2") == "sid-abc"
        assert sm2.get_flag("slack:1.2", "temporary") is True

    def test_flag_preserves_slack_link(self, patched):
        sm = SessionMap()
        sm.set_slack_link("slack:1.2", "1.2", "C1")
        sm.set_agent_override("slack:1.2", "researcher")
        sm2 = SessionMap()
        assert sm2.get_slack_link("slack:1.2") == ("1.2", "C1")
        assert sm2.get_agent_override("slack:1.2") == "researcher"
        # reverse index for challenge-redirect resume is intact
        assert sm2.get_session_for_thread("1.2") == "slack:1.2"


class TestGenerationFloor:
    def test_explicit_generation_survives_reload_and_prune(self, patched):
        tmp, _ = patched
        bucket = "discord:kirocrew:direct:u1"
        sm = SessionMap()

        sm.reserve_generation(f"{bucket}:gen4")

        reloaded = SessionMap()
        assert reloaded.max_generation(bucket) == 4
        assert reloaded.prune() == 0
        assert reloaded.max_generation(bucket) == 4
        raw = json.loads((tmp / "session_map.json").read_text(encoding="utf-8"))
        assert raw[bucket]["generation_floor"] == 4
        assert f"{bucket}:gen4" not in raw

    def test_generation_floor_is_monotonic_and_supports_unified_keys(self, patched):
        sm = SessionMap()
        sm.reserve_generation("discord:kirocrew:direct:u1:gen5")
        sm.reserve_generation("discord:kirocrew:direct:u1:gen2")
        sm.reserve_generation("unified:kirocrew:gen3")

        assert sm.max_generation("discord:kirocrew:direct:u1") == 5
        assert sm.max_generation("unified:kirocrew") == 3

    def test_non_dm_key_is_rejected(self, patched):
        with pytest.raises(ValueError, match="not a canonical DM session key"):
            SessionMap().reserve_generation("dashboard:chat-1")
