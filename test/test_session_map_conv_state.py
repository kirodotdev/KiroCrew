"""Tests for v1c-B -- per-conversation state on the session entry.

Covers the additive ``SessionMap`` backing store that will replace the
``slack/handler.py`` module-global thread-state dicts:

* boolean flags (``temporary`` / ``incognito``)
* agent + project overrides

Key properties asserted:
* state survives a reload (it is the point of moving off in-memory globals);
* setting per-conversation state never clobbers ``sid`` / Slack-link fields;
* bare ``thread_ts`` and ``slack:`` keys resolve to the SAME entry
  (canonicalization), so a not-yet-migrated caller and a migrated one agree.
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

    The flag is the record the channel's inbound gate hydrates from
    (``privacy_mode.hydrate`` reads the session map alone, never the header), so
    a row removed for any reason leaves that gate reading the thread as
    persistent after the next restart. ``prune()`` and the per-read repair keep
    such an entry WITHOUT reading a transcript (they run under the map lock, on
    the loop); ``stamp_privacy_headers()`` probes -- and for a legacy or
    tightened row, stamps -- the header on a worker thread and removes nothing.
    Immortality of durable settings stays opt-in; a privacy row is kept for a
    stated reason with a stated end (the gate reading the header, a separate
    change). Mutations: make ``_header_records_privacy_mode`` return False
    without writing -- the ``stamped`` cases go red; remove a row whose header
    records the mode -- every ``kept`` case goes red.
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
        """The thread's default-directory transcript: seeded with a turn, optionally stamped."""
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        key = key or TestPrivacyFlagsAndPrune.KEY
        log = ConversationLog()
        log.init()
        if seed:
            with history_mod.allow_on_loop_persist():
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
        self._log(seed=True, mode=flag)
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
        log = self._log(seed=True)
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
        log = self._log(seed=True, mode="incognito")
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

        log = self._log(seed=True)  # legacy header: no mode
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

        log = self._log(seed=True, mode="Temporary")
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
        """Unreadable is not "recorded": the count says so, and the row stays."""
        from kiro_crew.history import ConversationLog

        self._log(seed=True, mode="incognito")
        monkeypatch.setattr(
            ConversationLog,
            "update_metadata_if",
            lambda self, *a, **kw: (_ for _ in ()).throw(OSError("read-only")),
        )
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
        log = self._log(seed=True)
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
        log = self._log(seed=True, key=bound)
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

        self._log(seed=True, mode="incognito")
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

        The worker stamped the header with the flags it saw, so the header holds
        the pre-tightening mode -- never a looser one than before, the stamp is
        tighten-only -- and the row, which no pass removes, carries both flags:
        the channel gate reads ``temporary`` off the map at once, and the next
        pass reads the current flags and re-stamps the tightened mode. Mutation:
        make the stamp read the header without writing -- the second pass leaves
        ``incognito`` in the header.
        """
        from types import SimpleNamespace

        from kiro_crew import session_map as session_map_mod
        from kiro_crew.messaging import privacy_mode

        log = self._log(seed=True, mode="incognito")
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

        from kiro_crew import history as history_mod
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.session import SessionManager

        key = TestPrivacyFlagsAndPrune.KEY
        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(key, "user", "a pre-modifier turn")
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
            m.runtime_info = lambda: (None, None)
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

    The sweep's cost scales with the retained privacy-flagged rows -- a
    cross-process lock and two header reads each -- and the non-blocking start
    is what the live-config appliers and the dashboard's background-session
    restart call: they must not wait on housekeeping. The blocking path still
    awaits the sweep in place. Asserted as an ORDER of recorded events with a
    hooked sweep, never by timing.
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

        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        key = TestPrivacyFlagsAndPrune.KEY
        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(key, "user", "a pre-modifier turn")
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
    m.runtime_info = lambda: (None, None)
    m.stream_command = MagicMock()
    return m


class TestPrivacyRowsAcrossARestart:
    """A privacy-flagged channel row across a restart: kept, and the thread
    restricted for every reader, in all six cells.

    Two row shapes. ``!incognito`` as a thread's first message writes the map
    flag (and, since the modifier stamps headers, a metadata-only transcript
    header) before the thread has run a turn: no ``sid``, no ``discarded_sid``.
    A thread that ran turns and whose provider session was since reclaimed
    carries a ``sid`` with no session file behind it. Each in three transcript states:
    (a) no transcript on disk, (b) a transcript whose header lacks the mode, (c)
    a transcript whose header carries it. The restart runs the real startup
    path -- ``SessionManager.start_pool()``: ``prune()`` on the loop,
    ``stamp_privacy_headers()`` off it -- and the question is what the thread's
    readers resolve AFTERWARDS.

    Two readers, two records. The channel's inbound gate
    (``upload_gate.session_is_restricted`` for a non-``dashboard:`` key -- the
    same ``privacy_mode.hydrate`` the Slack and Telegram handlers call per
    message, and ``_is_restricted_session`` behind every memory-mutation route)
    reads the SESSION MAP through the process-local trackers and never the
    header. ``execution_context.capture_session_execution`` (the hooks handler,
    the MCP control tool, the task runner, workflow memory) and the consolidation
    resolver read the HEADER when the map has nothing. So the row must stay in
    every cell; a header is ensured wherever a transcript exists and never
    created where none does. Observed before the fix, ``sid``-bearing rows in
    (b) and (c): the startup step stamped the header and removed the row, the
    header and the consolidator read ``incognito``, and the channel gate read
    the thread as PERSISTENT after the restart -- turns persisted, agent memory
    writes admitted. Mutation: remove a row once its header records the mode --
    those two cells go red on the gate assertion with
    ``channel_gate_restricted: False``.
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
        from kiro_crew import history as history_mod
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.history_consolidation import resolve_consolidation_target
        from kiro_crew.messaging import privacy_mode, upload_gate
        from kiro_crew.session import SessionManager

        key = self.KEY
        log = ConversationLog()
        log.init()
        if state != "no-transcript":
            with history_mod.allow_on_loop_persist():
                log.append(key, "user", "a turn written before the modifier")
        if state == "header-carries-mode":
            log.update_metadata_if(key, {"memory_mode": "incognito"}, lambda _m: True)
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
            target = await resolve_consolidation_target(key, log=ConversationLog(), sessions=mgr)
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
                # The memory readers' verdict.
                "consolidation": (
                    None
                    if target.restricted is None
                    else (target.restricted.mode, target.restricted.source)
                ),
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
        # the memory readers off the map or the header.
        assert observed["channel_gate_restricted"] is True, observed
        assert observed["consolidation"] is not None, observed
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
