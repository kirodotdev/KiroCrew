"""Persistent retry accounting for history consolidation.

A consolidation pass spends a billed LLM turn before it can write the durable
``last_consolidated`` marker, so a failure in between leaves the span
unconsolidated. Without durable accounting every entry point then re-spends that
turn indefinitely — the 60s idle sweep on every tick, session-expiry sweeps on
every expiry, and every gateway restart (the in-memory throttle is memory-only).
These tests pin the attempt counter, the exponential backoff, the abandon cap,
and that no entry point bypasses them.
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import history as history_mod
from kiro_crew import platform_compat
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    _SESSION_MAX_BYTES,
    ConversationLog,
    HistoryConsolidator,
)

KEY = "dashboard:chat-retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_consolidation_captures_execution_off_loop(tmp_path, monkeypatch, mode):
    from kiro_crew import execution_context
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    captured = execution_context.ExecutionContext(
        None, execution_context.MemoryStoreRef("default"), "template", "kirocrew", mode
    )
    loop_thread = threading.get_ident()
    reads = []

    def read(key):
        reads.append((threading.get_ident(), key))
        return captured

    monkeypatch.setattr(execution_context, "read_session_execution", read)
    consolidator = _make_consolidator(_seed_log(tmp_path, count=0))
    consolidator._call_llm = AsyncMock()
    result = await asyncio.wait_for(consolidator._consolidate(KEY), 10)
    assert result is (None if mode == "persistent" else _CONSOLIDATION_REFUSED)
    consolidator._call_llm.assert_not_awaited()
    assert len(reads) == 1
    assert reads[0][0] != loop_thread
    assert reads[0][1] == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["temporary", "incognito", "persistent"])
async def test_consolidate_honors_the_transcript_header_mode(tmp_path, caplog, mode):
    """The memory-mode choke point every entry point inherits.

    A session with no execution record still carries its mode in the transcript
    header. ``_consolidate`` must refuse a temporary or incognito header before
    the model is called or any offset moves -- and say so at debug, so a skip is
    distinguishable from a pass -- while a persistent header consolidates exactly
    as before.
    """
    import logging

    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": mode})
    c = _make_consolidator(log)
    c._call_llm = AsyncMock(return_value={"history_entry": "x"})

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.history"):
        result = await asyncio.wait_for(c._consolidate(KEY, include_history=True), 10)

    if mode == "persistent":
        assert result is None
        c._call_llm.assert_awaited_once()
        assert log.unconsolidated_count(KEY) == 0
        return
    assert result is _CONSOLIDATION_REFUSED
    c._call_llm.assert_not_awaited()
    assert log.unconsolidated_count(KEY) == 3
    assert log.get_metadata(KEY).get("last_consolidated", 0) == 0
    assert KEY not in c._running
    skipped = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "consolidation skipped" in r.getMessage()
    ]
    assert skipped, f"a {mode} refusal left no debug trace:\n{caplog.text}"
    assert KEY in skipped[0].getMessage() and mode in skipped[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("named_by", ["live_key", "stem"])
async def test_consolidate_honors_a_channel_threads_session_map_flag(tmp_path, caplog, named_by):
    """The third memory-mode source: a channel thread's durable ``!incognito`` flag.

    A Slack thread marked ``!incognito`` keeps that mode ONLY in the session map
    (``privacy_mode._persist`` -> ``SessionMap.set_flag``): no channel path writes
    ``memory_mode`` into its transcript header or binds an execution record, so
    the execution-record and header reads see a persistent session, and the
    background paths -- the idle sweep and the channel's session-end hook, both
    of which schedule ``_consolidate`` through ``consolidate_session`` -- would
    consolidate the thread's pre-flag transcript into durable memory. The flag is
    seeded in a real ``SessionMap`` with the process-local trackers emptied (what
    a gateway restart leaves behind), and the thread is named both ways a caller
    names it: the live ``slack:<ts>`` key the channel passes, and the transcript
    stem ``slack_<ts>`` the dashboard trigger and the CLI pass, which only the
    map can unfold.
    """
    import logging
    from types import SimpleNamespace

    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    live_key = "slack:1785861252.833429"
    key = live_key if named_by == "live_key" else "slack_1785861252.833429"
    # The channel wrote the transcript under its live key; both names read it.
    log = _seed_log(tmp_path, key=live_key)
    assert "memory_mode" not in (log.get_metadata(key) or {}), "premise: header carries no mode"
    sm = SessionMap()
    sm.set_flag(live_key, "incognito", True)
    sm.flush()
    privacy_mode.reset()
    assert privacy_mode.is_incognito(live_key) is False, "premise: trackers start empty"
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    c = _make_consolidator(log, sessions=sessions)
    c._call_llm = AsyncMock(return_value={"history_entry": "x"})

    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.history"):
            c.consolidate_session(key)
            tasks = list(c._tasks)
            assert len(tasks) == 1, "premise: the session-end hook scheduled one pass"
            result = await asyncio.wait_for(tasks[0], 10)
    finally:
        privacy_mode.reset()

    assert (
        c._call_llm.await_count == 0
    ), "an incognito-flagged channel thread was consolidated into durable memory"
    assert result is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(key) == 3
    assert log.get_metadata(key).get("last_consolidated", 0) == 0
    assert key not in c._running
    skipped = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "consolidation skipped" in r.getMessage()
    ]
    assert skipped, f"the session-map refusal left no debug trace:\n{caplog.text}"
    message = skipped[0].getMessage()
    assert key in message and "incognito" in message and "(session map)" in message


@pytest.mark.asyncio
async def test_consolidate_refuses_a_channel_thread_whose_map_entry_was_pruned(
    tmp_path, monkeypatch, caplog
):
    """The transcript header is the record a memory reader consults.

    ``privacy_mode.apply_mode`` stamps ``memory_mode`` into the header, the source
    ``_consolidate`` reads before the map, so a transcript read never depends on
    the session map being loaded. Driven the way production runs it: the real
    modifier path over a real ``SessionMap``, then a consolidator that holds NO
    session map at all (a header-only reader), with the process-local trackers
    emptied (the restart), through the session-end hook's entry point.
    """
    import logging
    from types import SimpleNamespace

    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_default_log(live_key)  # three turns before the modifier
    sm = SessionMap()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    privacy_mode.reset()
    try:
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            live_key,
            source="telegram",
            sessions=sessions,
        )
        sm.flush()
        assert log.get_metadata(live_key).get("memory_mode") == "incognito", "premise: stamped"
        privacy_mode.reset()  # the fresh gateway's trackers start empty
        c = _make_consolidator(log, sessions=None)  # no map: the header must answer
        c._call_llm = AsyncMock(return_value={"history_entry": "x"})
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.history"):
            c.consolidate_session(live_key)
            tasks = list(c._tasks)
            assert len(tasks) == 1, "premise: the session-end hook scheduled one pass"
            result = await asyncio.wait_for(tasks[0], 10)
    finally:
        privacy_mode.reset()

    assert (
        c._call_llm.await_count == 0
    ), "a channel thread marked incognito was consolidated without its session map"
    assert result is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(live_key) == 3
    assert log.get_metadata(live_key).get("last_consolidated", 0) == 0
    skipped = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "consolidation skipped" in r.getMessage()
    ]
    assert skipped, f"the refusal left no debug trace:\n{caplog.text}"
    assert "(transcript header)" in skipped[0].getMessage()


@pytest.mark.asyncio
async def test_a_flag_set_during_the_resolvers_header_read_is_seen_before_the_verdict(
    tmp_path, monkeypatch
):
    """The modifier lands while the resolver's awaited header read is in flight.

    ``apply_mode`` marks the tracker and flags the map synchronously and awaits
    its header write after them, so a modifier landing during that read leaves
    a header that predates the mode beside a flag that already records it. The
    resolver read the flag BEFORE the await; read once, it would return
    unrestricted on the stale header. So the flag is read again after the
    await, and its restricted answer wins. Injected at the awaited reader: the
    read returns the header as it was (no mode) and sets the map flag on its
    way out, exactly the interleaving. Mutation: drop the post-await re-check
    -- ``restricted is None``.
    """
    from types import SimpleNamespace

    from kiro_crew.history import ConversationLog
    from kiro_crew.history_consolidation import (
        TARGET_SOURCE_SESSION_MAP,
        RestrictedTarget,
        resolve_consolidation_target,
    )
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_log(tmp_path, key=live_key)  # header without a mode
    sm = SessionMap()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    real_get = ConversationLog.get_metadata

    def _read_then_modifier(self, key):
        stale = real_get(self, key)
        # The modifier lands while this read is in flight: the map flag is
        # written now, the header write has not landed yet.
        sm.set_flag(live_key, "incognito", True)
        return stale

    monkeypatch.setattr(ConversationLog, "get_metadata", _read_then_modifier)
    privacy_mode.reset()
    try:
        target = await resolve_consolidation_target(live_key, log=log, sessions=sessions)
    finally:
        privacy_mode.reset()
    assert "memory_mode" not in target.metadata, "premise: the header read predates the mode"
    assert target.restricted is not None, "a flag set during the header read was missed"
    assert target.restricted == RestrictedTarget("incognito", TARGET_SOURCE_SESSION_MAP)


@pytest.mark.asyncio
async def test_a_flag_set_during_a_boundary_header_read_stops_the_write(tmp_path, monkeypatch):
    """The same window at a write boundary: the write gate's ``boundary`` asks the
    same resolver before it dispatches a write, so the re-read after the awaited
    header read is what stops the write there too. The pass resolves clean before
    its snapshot; the modifier lands during the header read of the resolution
    ahead of the history write. Mutation: drop the post-await re-check --
    ``append_history`` is called.
    """
    from types import SimpleNamespace

    from kiro_crew.history import ConversationLog
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_log(tmp_path, key=live_key)
    sm = SessionMap()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    c = _make_consolidator(log, sessions=sessions)
    armed = False

    async def _llm(prompt, **kw):
        nonlocal armed
        armed = True  # the next header read is the boundary check's
        return {"history_entry": "must not be written"}

    c._call_llm = _llm
    real_get = ConversationLog.get_metadata

    def _read_then_modifier(self, key):
        stale = real_get(self, key)
        if armed and key == live_key:
            sm.set_flag(live_key, "temporary", True)
        return stale

    monkeypatch.setattr(ConversationLog, "get_metadata", _read_then_modifier)
    privacy_mode.reset()
    try:
        outcome = await asyncio.wait_for(c._consolidate(live_key), 10)
    finally:
        privacy_mode.reset()
    c._memory.append_history.assert_not_called()
    assert outcome is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(live_key) == 3
    assert [e["resources"] for e in events] == [f"restricted_target_session:temporary:{live_key}"]
    assert c._restricted_refused == {live_key: "temporary"}


@pytest.mark.asyncio
async def test_a_legacy_privacy_flag_refuses_without_opening_the_transcript_and_the_startup_stamp_records_it(
    tmp_path, monkeypatch, caplog
):
    """A thread flagged before the header carried the mode: the map entry is all it has.

    Pre-upgrade installs hold private threads whose flag was written straight to
    the session map, with no header stamp, and ``apply_mode`` returns early for a
    mode already marked, so nothing re-stamps them. Three facts pinned in one
    lifecycle: the startup ``prune()`` keeps such an entry (clearing only its
    dead ``sid``) and never reads a transcript for it; the consolidator refuses
    it from the map WITHOUT opening the transcript -- not even its header -- the
    same contract ``test_restricted_consolidation_never_reads_transcript_or_opens_memory``
    pins for a known execution record; and the startup path's off-loop step then
    copies the mode into the existing transcript's header while the row STAYS
    (it is what the channel gate hydrates from), after which a header-only
    reader refuses on its own as well.
    """
    import logging
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.history import ConversationLog
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_default_log(live_key)
    assert "memory_mode" not in log.get_metadata(live_key), "premise: legacy header, no mode"
    sm = SessionMap()
    sm.set(live_key, "sid-reclaimed-by-kiro-cli")  # the provider session, since reclaimed
    sm.set_flag(live_key, "incognito", True)  # the legacy flag: map only, no header stamp
    sm.flush()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    privacy_mode.reset()
    loop_thread = threading.current_thread()
    header_reads: list[threading.Thread] = []
    real_get_metadata = ConversationLog.get_metadata

    def _recording_get_metadata(self, key):
        header_reads.append(threading.current_thread())
        return real_get_metadata(self, key)

    monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get_metadata)
    try:
        # The restart: startup prunes stale entries on the loop -- no transcript read.
        pruned = sm.prune()
        sm.flush()
        assert pruned == 0
        assert sm.get_flag(live_key, "incognito") is True
        assert header_reads == [], "prune read a transcript header on the loop"

        # The sweep: refused from the map, the transcript never opened.
        c = _make_consolidator(log, sessions=sessions)
        c._call_llm = AsyncMock(return_value={"history_entry": "x"})
        c._log.get_metadata = MagicMock(side_effect=AssertionError("the transcript was opened"))
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.history"):
            c.consolidate_session(live_key)
            tasks = list(c._tasks)
            assert len(tasks) == 1, "premise: the session-end hook scheduled one pass"
            result = await asyncio.wait_for(tasks[0], 10)
        assert result is _CONSOLIDATION_REFUSED
        assert c._call_llm.await_count == 0, (
            "a legacy incognito flag was not honoured and the thread was consolidated "
            "into durable memory"
        )
        skipped = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "consolidation skipped" in r.getMessage()
        ]
        assert skipped and "(session map)" in skipped[0].getMessage()
        assert header_reads == [], "a known-restricted refusal opened the transcript"

        # The startup path's off-loop step: the mode moves into the header, the
        # row stays, and neither happened on the loop thread.
        assert await sm.stamp_privacy_headers() == 1
        assert header_reads, "premise: the header was probed"
        assert [t for t in header_reads if t is loop_thread] == []
        assert sm.get_flag(live_key, "incognito") is True
        assert real_get_metadata(log, live_key).get("memory_mode") == "incognito"
        assert log.unconsolidated_count(live_key) == 3

        # From here the header answers on its own too: a consolidator with no
        # map refuses (the guard against opening the transcript comes off -- the
        # header IS what this reader is meant to open).
        del c._log.get_metadata
        privacy_mode.reset()
        header_only = _make_consolidator(log, sessions=None)
        header_only._call_llm = AsyncMock(return_value={"history_entry": "x"})
        assert (
            await asyncio.wait_for(header_only._consolidate(live_key), 10) is _CONSOLIDATION_REFUSED
        )
        header_only._call_llm.assert_not_awaited()
    finally:
        privacy_mode.reset()


@pytest.mark.asyncio
async def test_a_header_spelled_temporary_in_mixed_case_refuses_as_temporary(tmp_path, monkeypatch):
    """The resolver names the mode a ``Temporary`` header IS, not the string it holds.

    The shared predicate already reads the header case-insensitively; the mode it
    reports must be the normalized one too, because that string is what the SEL
    denial carries (``restricted_target_session:<mode>:<key>``), what the memo
    stores, what the route's 403 body names and what the tab's tally reads.
    Mutation: report ``str(metadata.get("memory_mode"))`` -- ``Temporary`` in
    the verdict and in the audit record.
    """
    from kiro_crew.history_consolidation import (
        _CONSOLIDATION_REFUSED,
        TARGET_SOURCE_HEADER,
        RestrictedTarget,
        resolve_consolidation_target,
    )

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": "Temporary"})
    target = await resolve_consolidation_target(KEY, log=log, sessions=None)
    assert target.restricted == RestrictedTarget("temporary", TARGET_SOURCE_HEADER)

    c = _make_consolidator(log)
    c._call_llm = AsyncMock()
    assert await asyncio.wait_for(c._consolidate(KEY), 10) is _CONSOLIDATION_REFUSED
    c._call_llm.assert_not_awaited()
    assert [e["resources"] for e in events] == [f"restricted_target_session:temporary:{KEY}"]
    assert c._restricted_refused == {KEY: "temporary"}


@pytest.mark.asyncio
async def test_every_restricted_refusal_is_its_own_sel_record(tmp_path, monkeypatch):
    """The background refusal writes the denial the dashboard route writes, every time.

    Same operation and outcome, ``restricted_target_session:<mode>`` plus the
    target key in ``resources``, ``source="background"`` because no request
    carried it. Two refusals of the same key are two records: an audit event
    that is sometimes withheld, whatever a later record claims to fold in, is a
    gap the reader cannot see. A tightened mode is a new fact and reads as such.
    Mutation: drop the ``log_api_access`` call -- red; window or dedupe the
    record per key -- the second assertion goes red.
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": "incognito"})
    c = _make_consolidator(log)
    c._call_llm = AsyncMock()

    assert await asyncio.wait_for(c._consolidate(KEY), 10) is _CONSOLIDATION_REFUSED
    assert events == [
        {
            "caller": "history_consolidator",
            "operation": "memory.consolidate",
            "outcome": "denied",
            "source": "background",
            "resources": f"restricted_target_session:incognito:{KEY}",
        }
    ]
    assert await asyncio.wait_for(c._consolidate(KEY), 10) is _CONSOLIDATION_REFUSED
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": "temporary"})
    assert await asyncio.wait_for(c._consolidate(KEY), 10) is _CONSOLIDATION_REFUSED
    assert [e["resources"] for e in events] == [
        f"restricted_target_session:incognito:{KEY}",
        f"restricted_target_session:incognito:{KEY}",
        f"restricted_target_session:temporary:{KEY}",
    ], "a repeated refusal was windowed, deduplicated or folded instead of recorded"
    c._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_idle_sweep_does_not_re_attempt_a_key_it_refused_as_restricted(
    tmp_path, monkeypatch
):
    """The volume is solved at its source, not in the audit trail.

    A refusal is not a completed pass, so it sets no throttle, and without a
    memo the idle sweep would schedule the same refused key on every 60 s tick
    -- one audit denial a minute per quiet private thread. After one refusal
    the sweep skips the key without calling ``_consolidate`` (nothing scheduled,
    no record); an EXPLICIT trigger aimed at the key is still attempted and
    still audited. Mutation: drop the memo check in ``check_idle_sessions`` --
    the second tick schedules again and the first assertion goes red.
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": "incognito"})
    c = _make_consolidator(log)
    c._call_llm = AsyncMock()
    attempts: list[str] = []
    real_consolidate = c._consolidate

    async def _counting(key, include_history=True):
        attempts.append(key)
        return await real_consolidate(key, include_history=include_history)

    monkeypatch.setattr(c, "_consolidate", _counting)
    c._last_activity[KEY] = time.time() - 10

    c.check_idle_sessions()
    assert c._tasks, "premise: the first tick schedules the (refused) pass"
    await asyncio.gather(*list(c._tasks))
    assert attempts == [KEY]
    assert len(events) == 1
    assert c._restricted_refused == {KEY: "incognito"}

    c._tasks.clear()
    c.check_idle_sessions()
    assert not c._tasks, "the idle sweep re-attempted a key it had already refused as restricted"
    assert attempts == [KEY]
    assert len(events) == 1, "a skipped (never attempted) key must not audit a denial"

    # The explicit trigger ignores the memo: attempted, refused, audited.
    assert await asyncio.wait_for(c.consolidate_now(KEY), 10) is False
    assert attempts == [KEY, KEY]
    assert [e["resources"] for e in events] == [
        f"restricted_target_session:incognito:{KEY}",
        f"restricted_target_session:incognito:{KEY}",
    ]
    assert await asyncio.wait_for(real_consolidate(KEY), 10) is _CONSOLIDATION_REFUSED
    c._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_per_turn_trigger_does_not_re_attempt_a_refused_key(tmp_path, monkeypatch):
    """``maybe_consolidate`` runs on every user turn and a refusal advances no
    offset, so past the threshold a refused private thread would schedule a
    refusal per turn. It reads the same memo as the idle sweep."""
    from kiro_crew.history_consolidation import _CONSOLIDATION_THRESHOLD

    monkeypatch.setattr(history_mod, "sel", lambda: MagicMock())
    log = _seed_log(tmp_path, count=_CONSOLIDATION_THRESHOLD + 1)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"memory_mode": "temporary"})
    c = _make_consolidator(log)
    c._call_llm = AsyncMock()

    c.maybe_consolidate(KEY)
    assert c._tasks, "premise: over the threshold, the first turn schedules the (refused) pass"
    await asyncio.gather(*list(c._tasks))
    assert c._restricted_refused == {KEY: "temporary"}
    c._tasks.clear()
    c.maybe_consolidate(KEY)
    assert not c._tasks, "the per-turn trigger re-attempted a key already refused as restricted"
    c._call_llm.assert_not_awaited()


def test_the_refusal_memo_is_bounded_oldest_out(monkeypatch, tmp_path):
    """The memo holds at most ``_RESTRICTED_REFUSAL_MEMO_MAX`` keys.

    Past the bound the OLDEST refused key is evicted and the newest kept; a key
    refused again moves to the newest end. An evicted key is merely eligible for
    the sweep again (one more refused attempt, one more SEL row), so eviction is
    safe. Mutation: drop the eviction loop -- the memo grows past the bound and
    the first assertion goes red.
    """
    import kiro_crew.history_consolidation as hc

    monkeypatch.setattr(history_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(hc, "_RESTRICTED_REFUSAL_MEMO_MAX", 3)
    c = _make_consolidator(_seed_log(tmp_path, count=0))
    for i in range(5):
        c._refuse_restricted(f"dashboard:chat-{i}", "incognito", "test")
    assert list(c._restricted_refused) == [
        "dashboard:chat-2",
        "dashboard:chat-3",
        "dashboard:chat-4",
    ]
    # Refusing an existing key again makes it the newest, not a second entry.
    c._refuse_restricted("dashboard:chat-2", "incognito", "test")
    assert list(c._restricted_refused) == [
        "dashboard:chat-3",
        "dashboard:chat-4",
        "dashboard:chat-2",
    ]
    # The evicted key is eligible for the sweep again: the documented cost.
    assert "dashboard:chat-0" not in c._restricted_refused


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["consolidate_now", "idle_sweep", "per_turn"])
async def test_a_mode_tightened_during_the_pass_aborts_before_any_durable_write(
    tmp_path, monkeypatch, entry_point
):
    """``!incognito`` lands while a pass is in flight: nothing is written.

    The pass resolved the mode once before its snapshot; the modifier lands
    during the model call (the ordinary sequence: a turn-triggered pass
    overlapping the user's next message). Every durable write is dispatched
    through the write gate, which resolves the mode again first: the pass
    aborts with the same SEL denial and memo entry as a refusal before the
    snapshot, the store receives no write and the offset does not advance. All
    three entry points cross the same gate because all three run
    ``_consolidate``. Mutation: dispatch the write without the gate -- the store
    receives the history entry and the offset advances (red:
    ``append_history`` called, ``unconsolidated_count == 0``).
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED, _CONSOLIDATION_THRESHOLD

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    count = _CONSOLIDATION_THRESHOLD + 1 if entry_point == "per_turn" else 3
    log = _seed_log(tmp_path, count=count)
    c = _make_consolidator(log)

    async def _llm_then_modifier(prompt, **kw):
        # The modifier's durable record lands while the model is thinking.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": "incognito"})
        return {"history_entry": "a summary that must not be written", "lessons": []}

    c._call_llm = _llm_then_modifier
    if entry_point == "consolidate_now":
        outcome = await asyncio.wait_for(c.consolidate_now(KEY), 10)
        results = [_CONSOLIDATION_REFUSED if outcome is False else None]
    elif entry_point == "idle_sweep":
        c._last_activity[KEY] = time.time() - 10
        c.check_idle_sessions()
        assert c._tasks, "premise: the sweep scheduled the pass"
        results = await asyncio.gather(*list(c._tasks))
    else:
        c.maybe_consolidate(KEY)
        assert c._tasks, "premise: the per-turn trigger scheduled the pass"
        results = await asyncio.gather(*list(c._tasks))

    # The leak, named first: the store must not receive the thread's content.
    c._memory.append_history.assert_not_called()
    assert log.unconsolidated_count(KEY) == count, "the offset advanced for a refused pass"
    assert results == [_CONSOLIDATION_REFUSED]
    assert [e["resources"] for e in events] == [f"restricted_target_session:incognito:{KEY}"]
    assert c._restricted_refused == {KEY: "incognito"}


@pytest.mark.asyncio
async def test_a_mode_tightened_after_a_write_is_not_retroactive_but_stops_the_offset(
    tmp_path, monkeypatch
):
    """The modifier lands between the history write and the offset advance.

    The write that already completed stands -- a switch after a completed write
    is not retroactive, nothing purges (the existing design line) -- but the
    offset advance is a durable write too and crosses its own boundary: the
    window stays unconsolidated, the pass is refused and audited, and the key is
    memoed so the sweep does not retry a session that is now restricted.
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._call_llm = AsyncMock(return_value={"history_entry": "written before the switch"})

    def _write_then_modifier(entry, *, admit=None):
        # The mock store behaves like a real one: it asks the gate's hook under
        # its lock ahead of the write, then the modifier lands inside the write.
        if admit is not None:
            admit()
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": "temporary"})

    c._memory.append_history.side_effect = _write_then_modifier
    outcome = await asyncio.wait_for(c._consolidate(KEY), 10)
    # Not retroactive: the completed write stands. Not advanced: the window stays.
    c._memory.append_history.assert_called_once()
    assert log.unconsolidated_count(KEY) == 3, "the offset advanced after the mode tightened"
    assert outcome is _CONSOLIDATION_REFUSED
    assert [e["resources"] for e in events] == [f"restricted_target_session:temporary:{KEY}"]
    assert c._restricted_refused == {KEY: "temporary"}


def _plausible_markdown(header: str) -> str:
    """A whole-file memory value ``_is_plausible_memory_file`` accepts."""
    return f"{header}\n\n- a line the model kept\n- a line the model added this pass\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", ["history-then-structured", "preferences-then-projects"])
async def test_a_mode_tightened_inside_one_write_stops_the_next_one(tmp_path, monkeypatch, pair):
    """The modifier lands INSIDE an awaited write; the write that follows it must not happen.

    Every awaited durable write of ``_consolidate`` is dispatched through the
    write gate, which resolves the mode again immediately before it, with no
    await in between. A single check ahead of a GROUP of awaited writes guards
    only the first: a modifier landing while that first write is in flight --
    ``append_history`` takes a cross-process file lock and can wait; a Markdown
    write is a whole-file rewrite -- would still have the next write in the
    group land the thread's content. Two pairs that once shared one check: the
    history write and the structured-memory write (the V1 vector path), and the
    preferences file and the projects file (the legacy Markdown path). Mutation:
    dispatch the pair under one resolution -- the second write of the pair is
    called.
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)

    def _tighten(*_a, **_kw):
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": "incognito"})
        return True

    if pair == "history-then-structured":
        vectors = MagicMock()
        vectors.algorithm_version = "v1"
        vectors.get_all_semantic.return_value = []
        c = _make_consolidator(log, vector_store=vectors)
        c._call_llm = AsyncMock(
            return_value={
                "history_entry": "written while the modifier landed",
                "semantic": [{"key": "pref.editor", "value": "vim", "confidence": 1.0}],
            }
        )
        first, second = c._memory.append_history, MagicMock(name="_write_structured_memory")
        first.side_effect = _tighten
        monkeypatch.setattr(c, "_write_structured_memory", second)
    else:
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        c = HistoryConsolidator(
            log=log, memory=memory, migrated=False, history_idle_secs=0, sessions=None
        )
        c._call_llm = AsyncMock(
            return_value={
                "history_entry": "x",
                "preferences_update": _plausible_markdown("# User Preferences"),
                "projects_update": _plausible_markdown("# Active Projects"),
            }
        )
        first, second = memory.write_preferences, memory.write_projects
        first.side_effect = _tighten

    outcome = await asyncio.wait_for(c._consolidate(KEY), 10)
    # The leak, named first: the second write of the pair must not receive the
    # thread's content once the first one carried the modifier in.
    second.assert_not_called()
    first.assert_called_once()  # not retroactive: the write in flight completes
    assert outcome is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(KEY) == 3, "the offset advanced after the mode tightened"
    assert [e["resources"] for e in events] == [f"restricted_target_session:incognito:{KEY}"]
    assert c._restricted_refused == {KEY: "incognito"}


@pytest.mark.asyncio
async def test_a_mode_tightened_during_the_skill_generation_stops_the_skill_write(
    tmp_path, monkeypatch
):
    """The skill pass is generation THEN write, minutes apart: the write is gated on its own.

    The skill -- distilled from this thread's transcript and published
    install-wide -- is written after the generation's await, so a modifier
    landing during the generation must be seen by the write gate that dispatches
    ``_process_auto_skills``, which resolves the mode again immediately before
    it. Mutation: dispatch the skill write without the gate -- the skill is
    staged.
    """
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)

    log = _seed_log(tmp_path)
    loader = MagicMock()
    c = _make_consolidator(log, skills_loader=loader, auto_skills_enabled=True)
    c._auto_min_tool_calls = 0
    staged = MagicMock(name="_process_auto_skills")
    monkeypatch.setattr(c, "_process_auto_skills", staged)

    async def _llm(prompt, **kw):
        if prompt.startswith("You are a skill-extraction agent."):
            # The modifier lands while the skill model is generating.
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"memory_mode": "temporary"})
            return {"new_skill": {"slug": "must-not-land", "procedure_md": "..."}}
        return {"history_entry": "written before the skill pass"}

    c._call_llm = _llm
    outcome = await asyncio.wait_for(c._consolidate(KEY), 10)
    staged.assert_not_called()
    c._memory.append_history.assert_called_once()  # the earlier write stands
    assert outcome is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(KEY) == 3, "the offset advanced after the mode tightened"
    assert [e["resources"] for e in events] == [f"restricted_target_session:temporary:{KEY}"]
    assert c._restricted_refused == {KEY: "temporary"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "seed_source", "assistant_value"),
    (
        (1, "user_explicit", "old@example.com"),
        (1, f"consolidation:{KEY}", "new@example.com"),
        (2, "user_explicit", "new@example.com"),
    ),
    ids=("v1-owner", "v1-automatic", "v2-verified-correction"),
)
@pytest.mark.parametrize("mutation", ("edit", "delete", "withdraw", "assistant"))
async def test_pending_extraction_rechecks_its_actual_transcript(
    tmp_path, monkeypatch, version, seed_source, assistant_value, mutation
):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.memory_stores import memory_store_dir_for, provision_member_memory
    from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

    store_name = None
    directory = tmp_path / "global"
    if version == 2:
        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig()
        store_name = provision_member_memory(cfg, "writer")
        cfg.save()
        directory = memory_store_dir_for(store_name)
    if version == 2:
        vectors = open_member_database(
            directory / "memory.db", member_id=cfg.agents["writer"].member_id, store_id=store_name
        )
    else:
        vectors = VectorMemoryStore(db_path=directory / "memory.db")
        vectors.init()
    try:
        assert vectors.set_semantic("user.email", "old@example.com", 1, seed_source) is None
        log = _seed_log(tmp_path, count=0)
        quote = "Please replace old@example.com with new@example.com."
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", quote)
        c = _make_consolidator(log, vector_store=vectors)
        monkeypatch.setattr("kiro_crew.context.store_of_session", lambda *_: store_name)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda *a, **kw: c._memory)
        monkeypatch.setattr(ContextBuilder, "get_lessons_for", lambda *a, **kw: None)

        async def response(_prompt, **_kwargs):
            with history_mod.allow_on_loop_persist():
                if mutation == "edit":
                    rows = log.read_messages(KEY)
                    rows[-1]["content"] = "That replacement was only a hypothetical example."
                    log.rewrite_session(KEY, rows)
                elif mutation == "delete":
                    assert log.delete_session(KEY)
                elif mutation == "withdraw":
                    log.append(KEY, "user", "Do not change my email after all.")
                else:
                    log.append(KEY, "assistant", "Understood.")
            return {
                "history_entry": "The user changed their email.",
                "semantic": [
                    {
                        "key": "user.email",
                        "value": "new@example.com",
                        "confidence": 1,
                        "correction_quote": quote,
                    }
                ],
            }

        with patch.object(c, "_call_llm", AsyncMock(side_effect=response)):
            outcome = await c._consolidate(KEY, include_history=True)
        row = vectors.get_semantic("user.email")
        assert row is not None
        if mutation == "assistant":
            assert outcome is None
            # An assistant append preserves eligibility, not extra write authority.
            assert json.loads(row["value_json"]) == assistant_value
            if version == 2:
                assert "changed their email" in vectors.read_editable_history()
                c._memory.append_history.assert_not_called()
            else:
                c._memory.append_history.assert_called_once()
            assert log.consolidation_counts(KEY)[1] == 1
        else:
            assert outcome is _CONSOLIDATION_REFUSED
            assert json.loads(row["value_json"]) == "old@example.com"
            c._memory.append_history.assert_not_called()
            assert int(log.get_metadata(KEY).get("last_consolidated", 0)) == 0
    finally:
        vectors.close()


# A row big enough that five of them exceed the rotation byte budget whatever it
# is set to. Rotation on a handful of huge rows is driven by the byte-shrink loop,
# not the line cap, so the workload has to be sized off the budget: a hardcoded
# byte figure silently stops rotating when the budget is raised, and these tests
# assert on a rotation having fired.
_OVER_CAP_ROW_CHARS = _SESSION_MAX_BYTES // 3


def _seed_log(tmp_path, key: str = KEY, count: int = 3) -> ConversationLog:
    """A real transcript with *count* unconsolidated messages."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    # These tests run on the event loop; the mutations under test are the
    # production ones (offloaded inside _consolidate), not this fixture setup.
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _seed_default_log(key: str, count: int = 3) -> ConversationLog:
    """Like :func:`_seed_log`, in the DEFAULT sessions directory.

    For a test that drives ``privacy_mode.apply_mode``: its header stamp goes
    through a default ``ConversationLog``, so the transcript under test has to
    live where that instance writes (the conftest pins the directory per test).
    """
    log = ConversationLog()
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _total(log: ConversationLog, key: str = KEY) -> int:
    """The transcript's current message total, as an entry point would supply it."""
    return log.consolidation_counts(key)[0]


def _span(
    log: ConversationLog, total: int | None = None, key: str = KEY
) -> history_mod.AttemptedSpan:
    """The span identity a turn over the CURRENT transcript would attempt.

    Mirrors what ``_consolidate`` freezes from its pre-turn snapshot, so a test
    charging a failure by hand stamps the same identity production would.
    """
    meta = log.get_metadata(key)
    return history_mod.AttemptedSpan(
        total=_total(log, key) if total is None else total,
        generation=int(meta.get("rotation_generation", 0) or 0),
        offset=int(meta.get("last_consolidated", 0) or 0),
    )


def _eligible(c: HistoryConsolidator, log: ConversationLog, now=None) -> bool:
    """retry_eligible with the count its callers already hold."""
    return c.retry_eligible(KEY, now, message_count=_total(log))


def _dashboard_state(log: ConversationLog) -> DashboardState:
    """A DashboardState wired to *log* — enough for the slot rehydrate/save paths."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        conversation_log=log,
        start_time=0.0,
    )


def _plant_raw_meta(log: ConversationLog, key: str, raw_fields: str) -> None:
    """Splice RAW JSON text into the metadata line.

    Goes around ``json.dumps`` on purpose: the hostile inputs are literals a
    serializer will not emit (``1e309``, a bare ``NaN``, ``"NaN"`` as a string),
    and they are exactly what a hand-edited or foreign-written transcript can
    carry.
    """
    path = log._path(key)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta_txt = lines[0].strip()
    assert meta_txt.endswith("}")
    lines[0] = f"{meta_txt[:-1]},{raw_fields}}}\n"
    path.write_text("".join(lines), encoding="utf-8")
    log._invalidate_cache(key)


class _FakeRequest(dict):
    """Minimal aiohttp request stand-in for the manual consolidate handler."""

    def __init__(self, state: Any, body: dict) -> None:
        super().__init__()
        self.app = {"state": state}
        # The owner's dashboard claims: the manual trigger is an owner-gated write.
        # The subject is ``state.owner_id`` when one is configured, else the signed
        # local bootstrap subject.
        self["app"] = ""
        self["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        # The handler's session-recognition gate refuses a request with no
        # X-Session-Key; the browser UI's static key is the recognised caller.
        self.headers: dict[str, str] = {"X-Session-Key": "dashboard:ui"}
        # ``read_bounded_json`` reads both: whether a body is there, and whether
        # it declares JSON (415 when it does not). A real request always has them.
        self.can_read_body = True
        self.content_type = "application/json"
        self._body = body

    async def json(self) -> dict:
        return self._body


class TestFailureAfterTheBilledCall:
    @pytest.mark.asyncio
    async def test_exception_after_llm_call_does_not_retry_on_the_next_tick(self, tmp_path):
        """A raise between the LLM call and the marker must arm backoff.

        The idle sweep's done-callback only sets its in-memory throttle when the
        task ends WITHOUT an exception, so on a raise all of check_idle_sessions'
        skip conditions are false again 60s later — a fresh billed turn per tick,
        forever. The durable counter is the only thing that stops it.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        with (
            patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})),
            patch.object(log, "mark_consolidated", side_effect=RuntimeError("disk full")),
        ):
            c.check_idle_sessions()
            assert c._tasks, "idle sweep did not schedule a consolidation"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1
        assert retry_at > time.time()
        # The span is still unconsolidated, and the throttle was never set —
        # backoff is the sole remaining gate.
        assert log.unconsolidated_count(KEY) == 3
        assert KEY not in c._history_consolidated

        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks, "consolidation re-fired while inside the backoff window"

    @pytest.mark.asyncio
    async def test_a_failure_before_the_llm_call_does_not_consume_budget(self, tmp_path):
        """Nothing was billed yet, so the attempt is free."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(log, "snapshot_for_consolidation", side_effect=RuntimeError("io error")):
            with pytest.raises(RuntimeError):
                await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY) == (0, 0.0)

    @pytest.mark.asyncio
    async def test_none_result_consumes_an_attempt(self, tmp_path):
        """_call_llm swallows every exception and returns None.

        _consolidate's bare ``return`` on a falsy result happens BEFORE the
        marker write, so the task looks successful (throttle set) while the
        durable count still says unconsolidated. Treat it as a failed attempt.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_backoff_doubles_per_attempt(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
            first = log.consolidation_retry_state(KEY)[1] - time.time()
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)
            second = log.consolidation_retry_state(KEY)[1] - time.time()

        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        assert second == pytest.approx(2 * _CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)


class TestAttemptCap:
    @pytest.mark.asyncio
    async def test_cap_abandons_the_span_with_the_durable_marker(self, tmp_path):
        """At the cap the marker is written anyway, ending the spend."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0, (
            "abandoned span left unmarked — it will re-bill a turn on the next "
            "tick and after every restart"
        )
        # The marker releases the budget in the same write, so the NEXT span is
        # not charged for this one's failures.
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        c._last_activity[KEY] = time.time() - 10
        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks

    @pytest.mark.asyncio
    async def test_cap_without_a_marker_write_stays_ineligible(self, tmp_path):
        """If even the abandon write fails, the span must stop spending."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": 0.0,
                },
            )

        assert c.retry_eligible(KEY) is False


class TestSuccessClearsTheAccounting:
    @pytest.mark.asyncio
    async def test_marking_a_span_releases_its_retry_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        assert log.consolidation_retry_state(KEY)[0] == 1

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "ok"})):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0
        assert log.consolidation_retry_state(KEY) == (0, 0.0)


class TestAccountingSurvivesARestart:
    @pytest.mark.asyncio
    async def test_a_fresh_consolidator_reads_the_persisted_backoff(self, tmp_path):
        """The in-memory throttle is lost on restart; the counter is not."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        assert log.consolidation_retry_state(KEY)[0] == 1

        # Simulate a gateway restart: brand-new log + consolidator over the same
        # session directory, with every in-memory dict empty.
        fresh_log = ConversationLog(base_dir=tmp_path / "sessions")
        fresh = _make_consolidator(fresh_log)
        fresh._last_activity[KEY] = time.time() - 10
        assert not fresh._history_consolidated

        assert fresh.retry_eligible(KEY) is False
        fresh.check_idle_sessions()
        assert not fresh._tasks, "restart re-billed a turn for a backed-off span"


class TestTheAccountingIsReadUncached:
    """The accounting is cross-process, and its writers preserve the file mtime.

    A gateway sweep, the CLI and a subagent all record failures for the same
    session. Every writer of these fields restores the pre-write mtime so
    housekeeping does not reorder ``list_sessions`` — which means the mtime-keyed
    metadata cache cannot notice another process's write.
    """

    @pytest.mark.asyncio
    async def test_a_second_writers_count_is_not_hidden_by_a_warm_cache(self, tmp_path):
        log = _seed_log(tmp_path)
        # A separate ConversationLog over the same directory stands in for the
        # other process — its own caches, its own view of the file.
        other = ConversationLog(base_dir=tmp_path / "sessions")

        path = log._path(KEY)
        # Warm this process's metadata cache the way the idle sweep does: it calls
        # unconsolidated_count (which caches the metadata line) immediately before
        # consulting the backoff.
        log.unconsolidated_count(KEY)
        assert log._meta_cache.get(KEY) is not None
        mtime_before = path.stat().st_mtime

        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, _span(other))

        assert path.stat().st_mtime == mtime_before, (
            "premise broken: the write advanced the mtime, so the cache would "
            "have self-invalidated and this test proves nothing"
        )

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1, "a stale cached count hid the other process's failure"
        assert retry_at > time.time()

    @pytest.mark.asyncio
    async def test_the_increment_builds_on_the_other_writers_count(self, tmp_path):
        """A stale read would overwrite the durable count with a lower one."""
        log = _seed_log(tmp_path)
        other = ConversationLog(base_dir=tmp_path / "sessions")
        # Frozen before the deliberate cache warm below, so reading the span does
        # not itself touch this process's caches.
        span = _span(log)

        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, span)
            other.record_consolidation_failure(KEY, 900.0, 86400.0, span)
            log.unconsolidated_count(KEY)  # warm this process's cache
            attempts, _ = log.record_consolidation_failure(KEY, 900.0, 86400.0, span)

        assert attempts == 3
        assert other.consolidation_retry_state(KEY)[0] == 3

    @pytest.mark.asyncio
    async def test_a_backed_off_span_is_not_re_fired_from_a_warm_cache(self, tmp_path):
        """End to end: the idle sweep must see the other process's backoff."""
        log = _seed_log(tmp_path)
        other = ConversationLog(base_dir=tmp_path / "sessions")
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        log.unconsolidated_count(KEY)
        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, _span(other))

        c.check_idle_sessions()
        assert not c._tasks, (
            "the sweep billed an LLM turn for a span another process had just " "put into backoff"
        )


class TestHostileMetadataDoesNotBreakTheGate:
    """Metadata is caller-supplied JSON; the conversions must fail safe."""

    @pytest.mark.asyncio
    async def test_an_overflowing_attempt_count_reads_as_zero(self, tmp_path):
        """``1e309`` parses to ``inf``, and ``int(inf)`` raises OverflowError."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 1e309')

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_a_huge_integer_deadline_reads_as_zero(self, tmp_path):
        """An integer too large for a float raises OverflowError from float()."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_retry_at": ' + "9" * 400)

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_an_infinite_deadline_reads_as_zero(self, tmp_path):
        """``1e309`` parses straight to ``inf``: no raise, but never expires."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_retry_at": 1e309')

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_a_nan_deadline_does_not_disable_consolidation_forever(self, tmp_path):
        """Every ``now >= nan`` is false, so a NaN deadline never expires."""
        raws = ('"consolidation_retry_at": NaN', '"consolidation_retry_at": "NaN"')
        for i, raw in enumerate(raws):
            log = _seed_log(tmp_path / f"case{i}")
            _plant_raw_meta(log, KEY, raw)

            assert log.consolidation_retry_state(KEY)[1] == 0.0
            assert (
                _make_consolidator(log).retry_eligible(KEY) is True
            ), f"{raw} permanently disabled consolidation for the session"

    @pytest.mark.asyncio
    async def test_a_non_numeric_value_reads_as_zero(self, tmp_path):
        log = _seed_log(tmp_path)
        _plant_raw_meta(
            log,
            KEY,
            '"consolidation_attempts": "lots", "consolidation_retry_at": {"a": 1}',
        )

        assert log.consolidation_retry_state(KEY) == (0, 0.0)

    @pytest.mark.asyncio
    async def test_the_manual_trigger_does_not_500_on_hostile_metadata(self, tmp_path):
        """The gate runs inside a request handler — a raise there is a 500."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 1e309')
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_absurd_stored_count_does_not_explode_the_backoff_shift(self, tmp_path):
        """The exponent is attacker-influenced; ``2 ** n`` must stay bounded."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 100000000')

        # Offloaded rather than wrapped in ``allow_on_loop_persist``: this write ends
        # in ``replace_with_retry``, which by design refuses to retry a Windows
        # sharing violation while ON the event loop, because retrying sleeps. A
        # concurrent reader holding the destination is routine there, so an on-loop
        # persist turns an ordinary contended rename into a hard failure -- this line
        # is what Windows CI failed on. Production offloads this write, and
        # ``on_event_loop`` states the rule: a caller earns the retry by offloading,
        # not by declaring. Doing the same here tests the path production runs.
        attempts, retry_at = await asyncio.to_thread(
            log.record_consolidation_failure,
            KEY,
            _CONSOLIDATION_BACKOFF_BASE_SECS,
            86400.0,
            _span(log),
        )

        assert attempts == 100000001
        assert retry_at == pytest.approx(time.time() + 86400.0, abs=5)


class TestAContendedRenameNeedsTheWriteOffTheLoop:
    """The metadata write ends in a rename Windows can refuse; offloading retries it.

    ``_update_metadata_locked`` finishes on ``replace_with_retry``, which absorbs the
    ``PermissionError`` a concurrent transcript READER causes on Windows -- but only
    off the event loop, because retrying sleeps and sleeping on the loop would stall
    every other session. Production earns the retry by offloading every session
    mutator, exactly as ``on_event_loop`` documents. A test that instead drives the
    mutator ON the loop under ``allow_on_loop_persist`` opts out of the retry, so one
    routine contended rename fails it outright -- which is what Windows CI hit here.
    """

    @pytest.fixture
    def _windows(self, monkeypatch):
        """A Windows rename gate, with the bounded backoff made instant."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)

    @pytest.mark.asyncio
    async def test_offloaded_the_contended_write_still_lands(self, tmp_path, _windows):
        """One faulted rename, then one that succeeds: the charge survives."""
        log = _seed_log(tmp_path)
        span = _span(log)

        # Entered AFTER seeding so the simulator faults the write under test rather
        # than the fixture's own appends.
        with replace_sharing_violation(match="-retry.jsonl", times=1) as state:
            attempts, _retry_at = await asyncio.to_thread(
                log.record_consolidation_failure,
                KEY,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                86400.0,
                span,
            )

        assert attempts == 1
        assert state["n"] == 2, (
            "the simulator must have FAULTED the metadata rename and been retried -- "
            "n < 2 means the retry never ran on the path under test"
        )
        assert log.get_metadata(KEY).get("consolidation_attempts") == 1

    @pytest.mark.asyncio
    async def test_on_the_loop_the_same_contention_is_fatal(self, tmp_path, _windows):
        """Why the sibling offloads: on the loop the retry is refused, by design."""
        log = _seed_log(tmp_path)
        span = _span(log)

        with replace_sharing_violation(match="-retry.jsonl", times=1):
            with pytest.raises(PermissionError):
                with history_mod.allow_on_loop_persist():
                    log.record_consolidation_failure(
                        KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, 86400.0, span
                    )


class TestOnlyASentTurnConsumesTheCap:
    """The cap abandons a span, so only real spend may advance it.

    A pre-dispatch failure (no session manager, kiro-cli missing / not logged in /
    failing to start) costs nothing. Charging it would let a handful of environment
    failures write the durable marker over messages no LLM has ever read — the
    exact false abandonment this accounting exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_memory_binding_failure_arms_durable_backoff_without_spending(self, tmp_path):
        from kiro_crew.memory_stores import UnknownMemoryStore

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with (
            patch("kiro_crew.context.store_of_session", side_effect=UnknownMemoryStore("missing")),
            patch.object(c, "_call_llm", AsyncMock()) as call,
            pytest.raises(UnknownMemoryStore, match="missing"),
        ):
            await c._consolidate(KEY, include_history=True)
        call.assert_not_called()
        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 0
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3
        restarted = _make_consolidator(log)
        assert restarted.retry_eligible(KEY) is False

    @pytest.mark.asyncio
    async def test_a_pre_dispatch_failure_does_not_consume_an_attempt(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            await c._consolidate(KEY, include_history=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 0, "an unsent turn consumed the abandon budget"
        # The backoff is still armed, so a broken host does not re-attempt on the
        # next 60s tick.
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_repeated_pre_dispatch_failures_never_abandon_the_span(self, tmp_path):
        """Past the cap count, the span must still be unmarked and retryable."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS + 3):
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 0
        assert (
            log.unconsolidated_count(KEY) == 3
        ), "a broken environment abandoned a span without one billed turn"
        # Still eligible once the deadline passes: the messages are not lost.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
        assert c.retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_the_environment_backoff_widens_per_failure(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            await c._consolidate(KEY, include_history=True)
            first = log.consolidation_retry_state(KEY)[1] - time.time()
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)
            second = log.consolidation_retry_state(KEY)[1] - time.time()

        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        assert second == pytest.approx(2 * _CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)

    @pytest.mark.asyncio
    async def test_a_post_dispatch_empty_result_still_consumes_an_attempt(self, tmp_path):
        """The other half of the contract: a sent turn is still charged."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 1

    @pytest.mark.asyncio
    async def test_no_session_manager_is_reported_as_not_dispatched(self, tmp_path):
        """_call_llm's own contract, not the caller's handling of it."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        assert c._sessions is None

        with pytest.raises(history_mod._ConsolidationNotDispatched):
            await c._call_llm("prompt")

    @pytest.mark.asyncio
    async def test_a_failure_acquiring_the_session_is_not_dispatched(self, tmp_path):
        """kiro-cli failing to start must not look like a spent turn."""
        log = _seed_log(tmp_path)
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("cli not found"))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()
        c = _make_consolidator(log, sessions=sessions)

        with pytest.raises(history_mod._ConsolidationNotDispatched):
            await c._call_llm("prompt")

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_turn_is_charged_as_dispatched(self, tmp_path):
        """Once the prompt is sent it may have been billed, so it returns None."""
        log = _seed_log(tmp_path)
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()
        c = _make_consolidator(log, sessions=sessions)

        with patch.object(
            history_mod,
            "stream_and_collect_json",
            AsyncMock(side_effect=RuntimeError("stream died")),
        ):
            assert await c._call_llm("prompt") is None


class TestAccountingNeverResurrectsADeletedSession:
    @pytest.mark.asyncio
    async def test_recording_a_failure_for_a_deleted_session_is_a_no_op(self, tmp_path):
        """_update_metadata_locked upserts, so a blind write recreates the file."""
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        # The span a turn would have attempted, frozen while the file still
        # exists — the pre-turn snapshot production would be holding here.
        span = _span(log)
        path.unlink()

        with history_mod.allow_on_loop_persist():
            attempts, retry_at = log.record_consolidation_failure(KEY, 900.0, 86400.0, span)

        assert (attempts, retry_at) == (0, 0.0)
        assert not path.exists(), "a deleted session was resurrected as empty history"

    @pytest.mark.asyncio
    async def test_recording_an_environment_failure_for_a_deleted_session_is_a_no_op(
        self, tmp_path
    ):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        path.unlink()

        with history_mod.allow_on_loop_persist():
            log.record_consolidation_environment_failure(KEY, 900.0, 86400.0)

        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_session_deleted_mid_consolidation_leaves_no_file(self, tmp_path):
        """End to end: the delete lands while the LLM turn is in flight."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        path = log._path(KEY)

        async def _delete_then_fail(_prompt, *, memory_store: str = "", session_key: str = ""):
            path.unlink()
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_delete_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert not path.exists()


class TestRotationDoesNotClearACappedBudget:
    """mark_consolidated is also the abandon path, and a rotation resets to 0.

    When the offset is not applied the span stays unconsolidated, so the write
    must not drop the accounting: clearing it there would hand the span a fresh
    budget every time a rewrite raced the marker, and the cap would never hold.
    Whether a LATER read still counts those attempts is a separate question,
    answered by span identity (see TestRotationReleasesTheBudgetForNewContent) —
    these tests pin the durable write.
    """

    @pytest.mark.asyncio
    async def test_a_generation_change_retains_the_capped_state(self, tmp_path):
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "rotation_generation": 4,
                },
            )
            # The caller's snapshot generation (2) does not match, so
            # mark_consolidated resets the offset to 0 instead of applying it.
            log.mark_consolidated(KEY, 3, 2)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 0
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "an unapplied offset cleared the cap on an unmarked span, buying "
            "another billed attempt"
        )
        assert meta.get("consolidation_retry_at")

    @pytest.mark.asyncio
    async def test_an_offset_beyond_the_message_count_retains_the_capped_state(self, tmp_path):
        """The count fallback also resets to 0 without advancing the marker."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )
            log.mark_consolidated(KEY, 999, 0)

        assert log.get_metadata(KEY)["last_consolidated"] == 0
        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_an_applied_offset_still_releases_the_budget(self, tmp_path):
        """The success path is unchanged: a marked span drops its accounting."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_env_failures": 4,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            log.mark_consolidated(KEY, 3, 0)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 3
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        for stale in (
            "consolidation_env_failures",
            "consolidation_attempts_generation",
            "consolidation_attempts_offset",
        ):
            assert stale not in meta, f"{stale} outlived the span it described"


class TestRotationReleasesTheBudgetForNewContent:
    """The cap abandons ONE span, so it must not outlive that span.

    A rotation archives the messages the failures were charged against and resets
    the marker to 0. Carrying a capped count onto the retained tail would silence
    consolidation for the session permanently — every message written afterwards
    stays ineligible forever. The counter is therefore bound to the
    ``(rotation_generation, last_consolidated)`` pair it was charged against: the
    same span keeps its cap, a genuinely new one gets a fresh bounded budget.
    """

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_span_it_belongs_to(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1
        assert meta["consolidation_attempts_generation"] == 0
        assert meta["consolidation_attempts_offset"] == 0

    @pytest.mark.asyncio
    async def test_a_new_generation_gets_a_fresh_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == 0, (
            "a capped count from an archived span disabled consolidation for "
            "every message written after the rotation"
        )
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_the_same_span_stays_capped(self, tmp_path):
        """No free attempt while the span is unchanged."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS
        assert (
            _make_consolidator(log).retry_eligible(KEY) is False
        ), "the same failing span bought another billed attempt"

    @pytest.mark.asyncio
    async def test_a_fresh_budget_still_waits_out_the_backoff(self, tmp_path):
        """A new span is not a free immediate turn on a host that keeps failing."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == 0
        assert _make_consolidator(log).retry_eligible(KEY) is False

    @pytest.mark.asyncio
    async def test_unstamped_accounting_keeps_the_cap(self, tmp_path):
        """Unknown provenance must fail closed, not grant unbounded retries."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "rotation_generation": 7,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_content_written_after_a_real_rotation_consolidates(self, tmp_path):
        """End to end through the real rotation path, not a planted generation.

        The capped state is planted rather than accrued: when the abandon path's
        marker write SUCCEEDS it clears the accounting itself, so the state that
        survives a rotation is the one whose marker write was refused.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            assert c.retry_eligible(KEY) is False

            # Blow the byte budget so _maybe_rotate archives the failing messages
            # and bumps the generation itself.
            for i in range(5):
                log.append(KEY, "user", f"{i}" * _OVER_CAP_ROW_CHARS)
        assert log.get_metadata(KEY)["rotation_generation"] >= 1, "no rotation fired"

        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
        assert (
            c.retry_eligible(KEY) is True
        ), "a rotation left the session permanently unable to consolidate"

        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "after rotation"})
        ):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0, "post-rotation content never consolidated"
        assert "consolidation_attempts" not in log.get_metadata(KEY)


class TestARotationDuringTheTurnStampsTheAttemptedSpan:
    """The charge must describe what the turn attempted, not what it returns to.

    The billed call is the whole point of the accounting, and the transcript is
    live while it is in flight — a rotation can land between the pre-turn
    snapshot and the failure charge. Re-reading the metadata line at charge time
    stamps the counter with the NEW generation, so the counter claims to have
    measured content no LLM has seen. At the cap that content is refused with its
    own identity already on the stamp, and nothing later can release it.
    """

    @staticmethod
    def _rotating_failure(log):
        """An LLM turn that rotates the transcript, then fails."""

        async def _turn(*_a, **_kw):
            with history_mod.allow_on_loop_persist():
                # Blow the byte budget so the real _maybe_rotate archives the
                # attempted messages and bumps the generation mid-turn.
                for i in range(5):
                    log.append(KEY, "user", f"{i}" * _OVER_CAP_ROW_CHARS)
            assert (
                log.get_metadata(KEY)["rotation_generation"] >= 1
            ), "premise broken: no rotation fired during the turn"
            return None

        return AsyncMock(side_effect=_turn)

    @pytest.mark.asyncio
    async def test_the_charge_carries_the_pre_turn_generation(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert meta["rotation_generation"] >= 1, "premise broken: no rotation landed"
        assert meta["consolidation_attempts_generation"] == 0, (
            "the charge was stamped with the generation the rotation produced, "
            "so it claims to have measured content the turn never sent"
        )

    @pytest.mark.asyncio
    async def test_the_rotated_span_does_not_inherit_the_charge(self, tmp_path):
        """The consequence: retained messages must start with their own budget.

        A charge stamped with the post-rotation identity reads back as belonging
        to the retained content, so that content is short a turn of budget before
        it has been attempted even once — and the cap abandons it early.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        total = _total(log)
        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert log.unconsolidated_count(KEY) > 0, (
            "premise broken: nothing is left pending after the rotation, so "
            "there is no span to strand"
        )
        assert total <= meta["consolidation_attempts_count"], (
            "premise broken: the transcript grew past the attempted extent, so "
            "the growth test would release the charge on its own and this would "
            "pass with the generation stamp wrong"
        )
        assert log.consolidation_retry_state(KEY, total)[0] == 0, (
            "the post-rotation messages carry a charge that was never spent on "
            "them, so their own budget is short and the cap abandons them early"
        )


class TestForeignWritersCannotEraseTheAccounting:
    """The accounting shares the metadata line with other layers' writers.

    A writer that REBUILDS that line from its own state deletes every field it
    does not enumerate. Two already did, and each silently reset the backoff so
    billed retries resumed. Preservation is now the default: a rebuilder names the
    keys it owns and carries the rest through.
    """

    @pytest.mark.asyncio
    async def test_a_compaction_preserves_the_retry_accounting(self, tmp_path):
        log = _seed_log(tmp_path)
        deadline = time.time() + 3600
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 3,
                    "consolidation_retry_at": deadline,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "title": "kept",
                },
            )
            log.rewrite_session(KEY, log._read_messages(KEY)[-1:])

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 3, "a compaction reset the backoff and re-billed the span"
        assert retry_at == pytest.approx(deadline, abs=1)
        assert log.get_metadata(KEY)["title"] == "kept"

    def test_a_dashboard_slot_save_preserves_the_retry_accounting(self, tmp_path, monkeypatch):
        """The save rebuilds the whole metadata line from the slot's own state."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        for i in range(3):
            log.append("dashboard:chat1", "user", f"m{i}")
        deadline = time.time() + 3600
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": 4,
                "consolidation_retry_at": deadline,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "rotation_generation": 2,
            },
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert meta.get("consolidation_attempts") == 4, (
            "a dashboard slot save erased the retry accounting, resuming billed "
            "retries on a span that already spent its budget"
        )
        assert meta.get("consolidation_retry_at") == pytest.approx(deadline, abs=1)
        assert meta.get("consolidation_attempts_generation") == 0
        assert meta.get("rotation_generation") == 2

    def test_a_slot_owned_field_is_still_cleared_by_omission(self, tmp_path, monkeypatch):
        """Preserving unowned keys must not make the slot's own state unclearable."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "m0")
        log.update_metadata("dashboard:chat1", {"pinned": True, "consolidation_attempts": 1})

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot.pinned = False
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert "pinned" not in meta, "an un-pinned slot could not clear its pin"
        assert meta.get("consolidation_attempts") == 1

    def test_the_helper_never_shadows_an_owned_key(self):
        rebuilt = {"title": "new"}
        existing = {"title": "old", "consolidation_attempts": 2, "rotation_generation": 1}
        out = history_mod.carry_unowned_metadata(rebuilt, existing, frozenset({"title"}))
        assert out == {
            "title": "new",
            "consolidation_attempts": 2,
            "rotation_generation": 1,
        }


class TestAnEditedTranscriptEarnsAFreshBudget:
    """An edit swaps in content no consolidation turn read, so it advances the
    session's content identity — and that ONE counter carries both guarantees.

    Preserving the accounting across a REBUILD is right; letting it (or a marker
    written by a turn already in flight) apply to EDITED content is not. A
    regenerate replaces the assistant tail with a reply the failing turns never
    saw, and it lands at the same message count, the same marker and the same
    extent — so nothing but the rotation generation distinguishes it, and without
    the bump a capped span would strand brand-new content forever while an
    in-flight attempt would mark it consolidated unread.
    """

    def _plant_capped_slot(
        self, tmp_path, monkeypatch, retry_in: float = 3600.0
    ) -> ConversationLog:
        """A two-message dashboard transcript whose span sits at the cap.

        *retry_in* places the armed backoff deadline relative to now — negative
        for a span whose wait has already elapsed (so eligibility turns purely on
        the budget), positive for one still serving it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "ask")
        log.append("dashboard:chat1", "assistant", "first answer")
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                "consolidation_retry_at": time.time() + retry_in,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "consolidation_attempts_count": log.consolidation_counts("dashboard:chat1")[0],
            },
        )
        return log

    def _regenerate(self, state, slot) -> None:
        """What the regenerate path does: swap the assistant tail for a different
        reply and persist that window as an explicit snapshot (a rewrite)."""
        snapshot = list(slot.messages)
        snapshot[-1] = {**snapshot[-1], "content": "regenerated answer"}
        slot.messages[:] = snapshot
        slot._dirty = True
        _save_slot_to_history(state, slot, snapshot)

    def test_a_regenerated_reply_is_not_held_by_the_old_spans_cap(self, tmp_path, monkeypatch):
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        before = log.consolidation_counts("dashboard:chat1")[0]
        c = _make_consolidator(log)
        assert not c.retry_eligible("dashboard:chat1", message_count=before), (
            "premise broken: the span is not capped, so the edit has nothing to "
            "release and this would pass without the fix"
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        after = log.consolidation_counts("dashboard:chat1")[0]
        meta = log.get_metadata("dashboard:chat1")
        assert after == before, (
            "premise broken: the rewrite changed the message total, so the growth "
            "test would release the charge on its own"
        )
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "premise broken: the rewrite moved the marker, which releases the " "charge on its own"
        )
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "premise broken: the accounting was dropped outright, so this passes "
            "without the span identity having moved"
        )
        assert "regenerated answer" in log._path("dashboard:chat1").read_text(
            encoding="utf-8"
        ), "premise broken: the replacement reply never reached disk"

        assert log.consolidation_retry_state("dashboard:chat1", after)[0] == 0, (
            "the replacement reply inherited the exhausted budget of the span it " "replaced"
        )
        assert c.retry_eligible("dashboard:chat1", message_count=after), (
            "a reply no consolidation turn has ever read is permanently "
            "ineligible for consolidation"
        )

    def test_the_edit_advances_the_sessions_content_identity(self, tmp_path, monkeypatch):
        """The release above is the span-identity semantics, not a special case."""
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        before = log.rotation_generation("dashboard:chat1")
        self._regenerate(state, slot)
        assert log.rotation_generation("dashboard:chat1") == before + 1, (
            "the edit left the content identity untouched, so a consolidation "
            "holding the pre-edit generation cannot tell its span was replaced"
        )

    def test_an_edit_does_not_buy_a_free_billed_turn(self, tmp_path, monkeypatch):
        """A fresh budget is not an immediate turn — same as a rotation.

        Releasing the deadline too would let a user hammering regenerate re-bill a
        failing consolidation on every gesture, which is exactly what the backoff
        exists to stop.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=3600.0)
        armed = log.get_metadata("dashboard:chat1")["consolidation_retry_at"]
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        attempts, retry_at = log.consolidation_retry_state("dashboard:chat1", count)
        assert attempts == 0, "the edit did not release the exhausted budget"
        assert retry_at == pytest.approx(
            armed, abs=1
        ), "the edit discarded the armed backoff deadline"
        assert not _make_consolidator(log).retry_eligible(
            "dashboard:chat1", message_count=count
        ), "an edit let the session skip a backoff it had not served"

    def test_an_edit_invalidates_an_attempt_already_in_flight(self, tmp_path, monkeypatch):
        """The completion write of a turn that snapshotted the PRE-edit span must
        not mark the replacement tail consolidated.

        The turn read the original reply, the user regenerated it, and only then
        did the turn finish. Its offset still fits the file — the count, the
        marker and the extent are all unchanged — so nothing but the advanced
        generation stops ``mark_consolidated`` from marking a reply no LLM has
        ever seen as already extracted.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        key = "dashboard:chat1"
        # The consolidation turn starts: one atomic pre-turn snapshot.
        _msgs, total, generation = log.snapshot_for_consolidation(key)
        assert total == 2 and generation == 0

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)
        assert log.consolidation_counts(key)[0] == total, (
            "premise broken: the edit changed the message count, so the offset "
            "fallback in mark_consolidated would catch this without the fix"
        )

        # The turn now completes and writes the marker for the span it read.
        log.mark_consolidated(key, total, generation)

        meta = log.get_metadata(key)
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "the regenerated reply was marked consolidated without ever being "
            "extracted — silent memory loss"
        )
        assert (
            log.consolidation_counts(key)[1] == total
        ), "the replacement content is not queued for consolidation"

    def test_a_steady_flush_leaves_the_content_identity_alone(self, tmp_path, monkeypatch):
        """Only an EDIT advances it; a re-serialization of the same window is not
        evidence about content, or every flush would release the cap."""
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        assert log.rotation_generation("dashboard:chat1") == 0
        assert (
            log.consolidation_retry_state("dashboard:chat1", count)[0]
            == _CONSOLIDATION_MAX_ATTEMPTS
        ), "a steady flush released the cap, so a failing span retries forever"

    def test_the_helper_preserves_a_foreign_field_unconditionally(self):
        existing = {
            "consolidation_attempts": 3,
            "consolidation_retry_at": 123.0,
            "rotation_generation": 1,
            "title": "kept",
        }
        assert history_mod.carry_unowned_metadata({}, existing, frozenset()) == existing


class TestTheCapDoesNotOutliveTheSpanItMeasured:
    """The cap is reachable only when the abandon-marker write itself failed.

    Refusing that span is correct; refusing the SESSION is not. Appended messages
    leave the generation and the marker untouched, so without the span's extent
    one transient write failure would reject the transcript forever and the
    session's history would never be consolidated again.
    """

    def _plant_capped(self, log, count: int | None = None, retry_at: float = -1.0):
        """A span charged to the cap whose abandon-marker write did not land."""
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": (
                        time.time() + retry_at if retry_at > 0 else time.time() - 1
                    ),
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "consolidation_attempts_count": (_total(log) if count is None else count),
                },
            )

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_spans_extent(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3

    @pytest.mark.asyncio
    async def test_the_extent_is_the_attempted_count_not_the_current_size(self, tmp_path):
        """A message arriving DURING the failing turn must not be swallowed.

        It was never sent to the provider, so recording the post-turn size would
        bury it inside the charged extent and the growth test would never fire for
        it — at the cap it would be excluded from consolidation permanently.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        async def _append_then_fail(_prompt, *, memory_store: str = "", session_key: str = ""):
            with history_mod.allow_on_loop_persist():
                log.append(KEY, "user", "arrived mid-turn")
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_append_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3, (
            "the mid-turn message was recorded as attempted, so growth can never " "release it"
        )
        # It reads as growth, so the counter does not describe this span. (The
        # armed deadline still applies — a fresh budget is not a free turn.)
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _total(log) == 4

    @pytest.mark.asyncio
    async def test_a_capped_span_with_no_growth_stays_ineligible(self, tmp_path):
        """An unchanged failing span cannot burn forever."""
        log = _seed_log(tmp_path)
        self._plant_capped(log)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_transcript_growth_releases_the_cap(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        assert _eligible(c, log) is False

        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0, (
            "one failed abandon write refused every message the session will " "ever write again"
        )
        assert _eligible(c, log) is True

    @pytest.mark.asyncio
    async def test_growth_still_waits_out_the_armed_backoff(self, tmp_path):
        """A fresh budget is not a free immediate turn."""
        log = _seed_log(tmp_path)
        self._plant_capped(log, retry_at=3600.0)
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_the_grown_span_re_arms_the_cap(self, tmp_path):
        """Growth buys ONE bounded budget, not an escape from the cap."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")
        assert _eligible(c, log) is True

        with history_mod.allow_on_loop_persist():
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS):
                log.record_consolidation_failure(KEY, 900.0, 86400.0, _span(log))
                log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == _CONSOLIDATION_MAX_ATTEMPTS
        assert meta["consolidation_attempts_count"] == 4, (
            "the cap re-armed against the OLD extent, so the same growth would " "release it again"
        )
        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(c, log) is False, "the original failing prefix can re-bill indefinitely"

    @pytest.mark.asyncio
    async def test_a_shrink_alone_does_not_release_the_cap(self, tmp_path):
        """Growth is `>`, not `!=` — a rewrite that only drops content adds none."""
        log = _seed_log(tmp_path)
        self._plant_capped(log, count=99)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_eligibility_never_reads_the_transcript(self, tmp_path):
        """It runs on the gateway loop; a full-file read there stalls everything."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        total = _total(log)

        reads: list[str] = []
        real_read_text = Path.read_text

        def _counting_read_text(self_path, *a, **kw):
            if self_path == log._path(KEY):
                reads.append(str(self_path))
            return real_read_text(self_path, *a, **kw)

        with patch.object(Path, "read_text", _counting_read_text):
            c.retry_eligible(KEY, message_count=total)
            log.consolidation_retry_state(KEY, total)

        assert reads == [], "the eligibility check read the whole transcript on the event loop"

    @pytest.mark.asyncio
    async def test_content_written_after_a_failed_abandon_consolidates(self, tmp_path):
        """End to end through a real entry point, which is what consults the gate.

        Driving ``_consolidate`` directly would prove nothing here: the cap lives
        in ``retry_eligible``, so only a caller that passes through it can show the
        session recovering.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)

        c.consolidate_session(KEY)
        assert not c._tasks, "a capped span with no growth still fired a turn"

        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "recovered"})):
            c.consolidate_session(KEY)
            assert c._tasks, (
                "one failed abandon write left the session permanently unable to "
                "consolidate anything it writes from now on"
            )
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        assert log.unconsolidated_count(KEY) == 0
        assert "consolidation_attempts" not in log.get_metadata(KEY)


class TestEveryEntryPointRespectsTheAccounting:
    @pytest.mark.asyncio
    async def test_consolidate_session_honors_the_backoff(self, tmp_path):
        """The expiry path consults no time throttle of its own at all."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 1,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        c.consolidate_session(KEY)
        assert not c._tasks, "session expiry re-fired a backed-off consolidation"

        # Past the deadline the same call proceeds, so backoff delays rather than
        # disables the path.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            c.consolidate_session(KEY)
            assert c._tasks
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_manual_dashboard_trigger_refuses_inside_the_backoff(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert not c._tasks, "manual trigger bypassed the backoff"

        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)


class TestTheManualTriggerAnswersAnUnreadableRecord:
    """The route reads the target's record twice -- ``store_of_session`` first,
    then ``resolve_consolidation_target`` after the awaited live-mode read. The
    first read answers an unreadable record with a coded 503; a record that
    turns unreadable BETWEEN the two reads (a writer lands a first line the
    reader cannot decode) must get the same 503 from the second, never an
    uncaught ``UnknownMemoryStore`` -- a 500 -- and must claim and dispatch
    nothing. Mutation: let the resolver's raise escape (the r35 shape) -- red:
    the handler raises instead of answering.
    """

    @pytest.mark.asyncio
    async def test_a_record_that_turns_unreadable_between_the_two_reads_is_a_503(self, tmp_path):
        from kiro_crew.dashboard.handlers import memory as mem_mod
        from kiro_crew.memory_stores import UnknownMemoryStore

        # The default sessions directory (the conftest pins it per test): the
        # resolver's own reader, ``read_session_execution``, opens a default
        # ``ConversationLog``, so the record under test has to live there.
        log = _seed_default_log(KEY)
        c = _make_consolidator(log)
        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state.sessions = None
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})
        path = log._path(KEY)
        real_resolve = mem_mod.resolve_session_memory_mode

        async def _corrupt_then_resolve(st, key):
            # The await between the two reads: the first line becomes bytes no
            # UTF-8 reader decodes, so the record is unreadable from here on.
            data = path.read_bytes()
            path.write_bytes(b'{"memory_mode": "\xff\xfe"}' + data[data.index(b"\n") :])
            return await real_resolve(st, key)

        with (
            patch.object(mem_mod, "resolve_session_memory_mode", _corrupt_then_resolve),
            patch.object(c, "_consolidate", new_callable=AsyncMock),
        ):
            try:
                resp = await mem_mod.api_memory_consolidate(request)  # type: ignore[arg-type]
            except UnknownMemoryStore as exc:
                pytest.fail(
                    "the record turning unreadable between the two reads escaped the "
                    f"handler as a 500: {exc}"
                )
            c._consolidate.assert_not_called()
        assert resp.status == 503, (resp.status, resp.text)
        body = json.loads(resp.text)
        assert body["code"] == "store_unavailable"
        assert "unreadable" in body["error"], body
        assert not c._tasks


class TestTheManualTriggerClaimsAtomically:
    """Two concurrent POSTs must not both dispatch a billed turn.

    The eligibility probe awaits an off-loop transcript read, so a membership
    test taken before that yield and acted on after it is a check-then-act race.
    """

    @pytest.mark.asyncio
    async def test_concurrent_triggers_dispatch_only_once(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}

        with patch.object(c, "_consolidate", new_callable=AsyncMock) as spy:
            # Both requests are in flight across the handler's await, which is
            # exactly the window a check-then-act guard leaves open.
            responses = await asyncio.gather(
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
            )
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        statuses = sorted(r.status for r in responses)
        assert statuses == [200, 409], f"expected one winner and one refusal, got {statuses}"
        assert spy.await_count == 1, (
            f"consolidation dispatched {spy.await_count} times for one span — "
            "the claim is not atomic across the handler's await"
        )

    @pytest.mark.asyncio
    async def test_a_refused_trigger_releases_its_claim(self, tmp_path):
        """A 429 must not leave the key claimed, or the span is wedged forever."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert KEY not in c._running, "the backoff refusal leaked its claim"

        # Once the backoff expires the same key is dispatchable again, which it
        # would not be if the refusal above had left the claim in place.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)


class TestConsolidateIsTheEligibilityChokePoint:
    """_consolidate() enforces retry eligibility itself.

    The caller pre-checks are scheduling short-circuits and UX; the gate inside
    the operation is what guarantees an entry point without one (the
    preferences/projects path, or a future caller) cannot re-bill a backed-off
    span.
    """

    def _arm_backoff(self, log: ConversationLog, attempts: int = 1) -> None:
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": attempts,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

    @pytest.mark.asyncio
    async def test_a_direct_call_inside_the_backoff_bills_nothing(self, tmp_path):
        """No caller pre-check at all — the inner gate alone must refuse."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            await c._consolidate(KEY, include_history=True)

        spy.assert_not_awaited()
        # The refusal is free: nothing reached the provider, so nothing is
        # charged and the span's accounting is untouched.
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 1
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_a_direct_call_on_a_fresh_span_still_consolidates(self, tmp_path):
        """The eval runner calls _consolidate directly to bypass the message
        threshold; a fresh span carries no backoff, so the gate lets it pass."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            await c._consolidate(KEY, include_history=True)

        spy.assert_awaited_once()
        assert log.unconsolidated_count(KEY) == 0

    @pytest.mark.asyncio
    async def test_maybe_consolidate_is_refused_inside_the_backoff(self, tmp_path):
        """The prefs/projects path carries the same cheap pre-check as the
        other automatic entry points, so a backed-off span never even
        schedules a task (this path runs on every user turn)."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            c.maybe_consolidate(KEY)
            assert not c._tasks, "a backed-off span still scheduled a task"

        spy.assert_not_awaited()
        assert KEY not in c._running
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 1

    @pytest.mark.asyncio
    async def test_the_inner_gate_backstops_a_caller_without_a_pre_check(self, tmp_path):
        """A caller whose pre-check misses — a future entry point without one,
        or a pre-check that raced the backoff being recorded — still cannot
        bill the span: the gate inside _consolidate is the enforcement, the
        pre-checks are scheduling short-circuits. The first retry_eligible
        answer (the pre-check) lies True; the second (the inner gate) tells
        the truth."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as spy,
            patch.object(c, "retry_eligible", side_effect=[True, False]),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_not_awaited()
        assert KEY not in c._running, "the refusal leaked the running claim"
        assert KEY not in c._prefs_offset, "a refused pass advanced the prefs offset"

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_strand_the_key_in_running(self, tmp_path):
        """Callers add the key to _running before the task and rely on
        done-callbacks to release it; a refusal path that skipped the release
        would wedge consolidation for the session permanently. The pre-check
        is bypassed (first retry_eligible answer lies True) so the inner
        gate's refusal path actually runs."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock),
            patch.object(c, "retry_eligible", side_effect=[True, False]),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        assert KEY not in c._running, "the refusal leaked the running claim"

        # Once the backoff expires the same key is dispatchable again, which
        # it would not be if the refusal above had left the claim in place.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            c.consolidate_session(KEY)
            assert c._tasks, "a released key was still refused after the backoff"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_maybe_consolidate_proceeds_outside_the_backoff(self, tmp_path):
        """The gate delays rather than disables the prefs/projects path."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the threshold did not schedule a task"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_awaited_once()
        assert c._prefs_offset.get(KEY) == history_mod._CONSOLIDATION_THRESHOLD
        assert KEY not in c._running

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_advance_the_prefs_offset(self, tmp_path):
        """Advancing the offset on a refusal marks the window consolidated with
        no pass run — once the backoff expires the threshold test would skip it
        until a whole new threshold of messages accumulates, silently dropping
        its preference/project extraction. The pre-check is bypassed (first
        retry_eligible answer lies True) so the inner gate's refusal path
        actually runs."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as spy,
            patch.object(c, "retry_eligible", side_effect=[True, False]),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_not_awaited()
        assert KEY not in c._prefs_offset, "a refused pass advanced the prefs offset"

        # Once the backoff expires the SAME window is retried — the refusal
        # delayed the extraction rather than dropping it.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
            c.maybe_consolidate(KEY)
            assert c._tasks, "the un-advanced offset did not re-arm the threshold"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_awaited_once()
        assert c._prefs_offset.get(KEY) == history_mod._CONSOLIDATION_THRESHOLD

    @pytest.mark.asyncio
    async def test_consolidate_now_reports_a_refusal(self, tmp_path):
        """consolidate_now's only caller is the CLI, which would otherwise print
        'done ✓' unconditionally; the returned False is what lets it report
        the backoff skip instead of a false success."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            assert await c.consolidate_now(KEY) is False

        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consolidate_now_reports_success_outside_the_backoff(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            assert await c.consolidate_now(KEY) is True

        spy.assert_awaited_once()
        assert log.unconsolidated_count(KEY) == 0
