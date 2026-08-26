"""A flush landing during the fork's threaded read must not drop the tail.

``api_chat_slot_fork`` reads the transcript off the event loop, then gates both
its in-memory reconciliation and its durable save on ``slot._dirty``. The periodic
flush executor does not take ``slot._fork_lock``, so it can complete and clear
``_dirty`` while the fork is suspended in that read -- after which both gates read
False and the messages the read missed are in neither source.
"""

import asyncio
import re
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.mark.asyncio
async def test_fork_sees_the_tail_when_a_flush_clears_dirty_mid_read(tmp_path, monkeypatch):
    """Forking at the tail's index must succeed after a mid-read flush.

    The read is served a pre-flush snapshot, then the racer does what the flush
    does: writes the tail to disk and clears ``_dirty``. If the fork keeps that
    stale snapshot, the tail is absent from its index space and forking at the
    tail's index fails as out of range.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:forkrace"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    slot = state.get_or_create_slot("forkrace")
    slot.append("user", "history-1", "msg msg-u")
    slot.append("assistant", "history-2", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    # The unpersisted tail: in memory, not yet on disk.
    slot.append("user", "tail-prompt", "msg msg-u")
    slot._dirty = True

    pre_flush = list(log.read_messages_chained(key))
    assert len(pre_flush) == 2, f"fixture expected 2 on-disk messages, got {len(pre_flush)}"

    read_entered = threading.Event()
    may_finish = threading.Event()
    calls = {"n": 0}
    original = log.read_messages_chained

    def racing_read(k):
        calls["n"] += 1
        if calls["n"] == 1:
            # First read lands BEFORE the flush: serve the pre-flush snapshot.
            read_entered.set()
            may_finish.wait(timeout=10)
            return list(pre_flush)
        return original(k)

    monkeypatch.setattr(log, "racing_read_marker", True, raising=False)
    monkeypatch.setattr(log, "read_messages_chained", racing_read)

    async def racer():
        for _ in range(200):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
        # Exactly what the flush executor does: persist the tail, then clear _dirty.
        original(key)  # touch, mirroring the flush's own read path
        log.append(key, "user", "tail-prompt")
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots/forkrace/fork",
                json={"at_message_index": 2, "prompt": "forked"},
            )
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post
        payload = await resp.json()

    assert resp.status != 400 or "out of range" not in str(payload), (
        "the fork kept its pre-flush snapshot, so the tail the flush persisted is "
        f"missing from its index space: {payload}"
    )
    assert resp.status == 200, f"fork failed for another reason: {resp.status} {payload}"


@pytest.mark.asyncio
async def test_a_reread_does_not_duplicate_a_tail_the_flush_already_persisted(
    tmp_path, monkeypatch
):
    """An append arriving during the re-read must not duplicate the flushed tail.

    The re-read picks the flushed tail up off disk. If an append lands while it is
    in flight, ``_dirty`` goes back to True and the reconciliation below slices the
    in-memory tail from ``_resumed_count`` -- an offset the flush never advances --
    so the tail the re-read already holds would be appended a second time.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:dupe"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    slot = state.get_or_create_slot("dupe")
    slot.append("user", "history-1", "msg msg-u")
    slot.append("assistant", "history-2", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot.append("user", "tail-prompt", "msg msg-u")
    slot._dirty = True

    pre_flush = list(log.read_messages_chained(key))
    original = log.read_messages_chained
    calls = {"n": 0}
    gates = {n: (threading.Event(), threading.Event()) for n in (1, 2)}

    def racing_read(k):
        calls["n"] += 1
        n = calls["n"]
        if n in gates:
            entered, release = gates[n]
            entered.set()
            release.wait(timeout=10)
        # Read 1 landed before the flush, so it cannot see the tail.
        return list(pre_flush) if n == 1 else original(k)

    monkeypatch.setattr(log, "read_messages_chained", racing_read)

    async def _await_gate(ev):
        for _ in range(300):
            if ev.is_set():
                return True
            await asyncio.sleep(0.01)
        return False

    async def racer():
        # While read 1 is parked: the flush persists the tail and clears _dirty,
        # which is what makes the re-read fire.
        assert await _await_gate(gates[1][0]), "read 1 never entered"
        log.append(key, "user", "tail-prompt")
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        gates[1][1].set()
        # While the re-read is parked: an append arrives, marking the slot dirty
        # again, which re-arms the reconciliation below.
        assert await _await_gate(gates[2][0]), "the re-read never fired"
        slot.append("user", "arrived-late", "msg msg-u")
        slot._dirty = True
        gates[2][1].set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots/dupe/fork",
                json={"at_message_index": 99, "prompt": "forked"},
            )
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        payload = await (await post).json()

    # The out-of-range error reports the visible count: the duplication probe.
    m = re.search(r"have (\d+) visible messages", str(payload.get("error", "")))
    assert m, f"expected the out-of-range error to report a count: {payload}"
    visible = int(m.group(1))
    assert visible == 4, (
        "expected 4 visible messages (2 history + tail-prompt + arrived-late), got "
        f"{visible}: the tail the re-read already held was reconciled in a second time"
    )


@pytest.mark.asyncio
async def test_fork_does_not_duplicate_when_an_append_re_dirties_during_the_read(
    tmp_path, monkeypatch
):
    """A flush then an append, both during the FIRST read, must not duplicate.

    This is the interleaving a boolean ``_dirty`` cannot see. The flush persists
    the tail and clears ``_dirty``; an append then re-marks the slot dirty before
    the read returns. A ``was_dirty and not slot._dirty`` transition test reads
    that as "never flushed", so no correction fires and the tail the read already
    picked up off disk is reconciled in a second time.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:redirty"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    slot = state.get_or_create_slot("redirty")
    slot.append("user", "history-1", "msg msg-u")
    slot.append("assistant", "history-2", "msg msg-a")
    # Both counters as a completed save leaves them: two window messages on disk.
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    # The unpersisted tail: in memory, not yet on disk.
    slot.append("user", "tail-prompt", "msg msg-u")
    slot._dirty = True

    original = log.read_messages_chained
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def racing_read(k):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            release.wait(timeout=10)
        # Read 1 resumes AFTER the racer's flush, so it returns the persisted
        # tail -- which is what makes the slice below double-count it.
        return original(k)

    monkeypatch.setattr(log, "read_messages_chained", racing_read)

    async def _await_gate(ev):
        for _ in range(300):
            if ev.is_set():
                return True
            await asyncio.sleep(0.01)
        return False

    async def racer():
        assert await _await_gate(entered), "read 1 never entered"
        # Exactly what the flush executor does: persist the window tail, advance
        # the persisted boundary, then clear _dirty.
        log.append(key, "user", "tail-prompt")
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        # ...and then an append lands, re-marking the slot dirty. From here a
        # boolean-transition test cannot tell a flush happened at all.
        slot.append("user", "arrived-late", "msg msg-u")
        slot._dirty = True
        release.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots/redirty/fork",
                json={"at_message_index": 99, "prompt": "forked"},
            )
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        payload = await (await post).json()

    assert calls["n"] >= 1, "the fork never read the transcript"
    # The out-of-range error reports the visible count: the duplication probe.
    m = re.search(r"have (\d+) visible messages", str(payload.get("error", "")))
    assert m, f"expected the out-of-range error to report a count: {payload}"
    visible = int(m.group(1))
    assert visible == 4, (
        "expected 4 visible messages (2 history + tail-prompt + arrived-late), got "
        f"{visible}: the flush persisted tail-prompt and the append re-dirtied the "
        "slot, so the boolean transition never fired and the persisted tail was "
        "reconciled in on top of the disk read"
    )


@pytest.mark.asyncio
async def test_a_boundary_ahead_of_the_window_does_not_duplicate_persisted_turns(
    tmp_path, monkeypatch
):
    """A boundary ahead of the resident window must not slice from ``_resumed_count``.

    ``_flush_segment`` reassigns ``slot.messages`` to drop a trailing chunk run
    without adjusting ``_disk_window_len``, so the boundary can exceed the window
    and is then unusable as an index. ``_resumed_count`` is not a substitute: the
    save never advances it -- ``_save_slot_to_history`` only ever READS it, in its
    no-op skip -- and for a slot created in this gateway run it stays 0. Slicing
    from it therefore appends the ENTIRE resident window onto the disk read and
    duplicates every persisted turn. ``session_transfer`` records this exact
    defect at its own merge offset, having shipped it once already.

    The three tests above all set the boundary EQUAL to the window and give
    ``_resumed_count`` a momentarily-correct nonzero value -- the one arrangement
    in which the fallback looks benign. This covers the other side.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:ahead"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    slot = state.get_or_create_slot("ahead")
    slot.append("user", "history-1", "msg msg-u")
    slot.append("assistant", "history-2", "msg msg-a")
    # A slot created in THIS gateway run never has _resumed_count advanced, so it
    # stays 0 however often the slot flushes. Asserted rather than assigned: the
    # fixture's whole point is the value the save leaves alone.
    assert (
        slot._resumed_count == 0
    ), f"fixture expects a fresh slot with _resumed_count == 0, got {slot._resumed_count}"
    # The boundary runs AHEAD of the resident window, as a mid-stream
    # _flush_segment leaves it: recorded over a RAW window that included a
    # streaming chunk row which was then dropped from slot.messages.
    slot._disk_window_len = len(slot.messages) + 1
    slot._dirty = True

    on_disk = list(log.read_messages_chained(key))
    assert len(on_disk) == 2, f"fixture expected 2 on-disk messages, got {len(on_disk)}"

    flushes = {"n": 0}

    async def fake_flush(_state, s, best_effort=True):
        # Exactly what a completed save does to these two counters:
        # chat_persistence sets ``_disk_window_len = len(window)`` and clears
        # ``_dirty``. Stubbed rather than run for real so this test measures the
        # boundary logic and not the save's own disk behaviour -- the same
        # technique the three tests above use for the flush executor.
        flushes["n"] += 1
        s._disk_window_len = len(s.messages)
        s._dirty = False

    monkeypatch.setattr("kiro_crew.dashboard.chat_fork.save_slot_off_loop", fake_flush)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/ahead/fork",
            json={"at_message_index": 99, "prompt": "forked"},
        )
        payload = await resp.json()

    # The out-of-range error reports the visible count: the duplication probe.
    m = re.search(r"have (\d+) visible messages", str(payload.get("error", "")))
    assert m, f"expected the out-of-range error to report a count: {payload}"
    visible = int(m.group(1))
    assert visible == 2, (
        f"expected 2 visible messages (the disk history, once), got {visible}: the "
        "boundary-ahead branch sliced the resident window from _resumed_count -- 0 "
        "for a slot created in this gateway run -- and appended every persisted "
        "turn onto the disk read a second time"
    )


@pytest.mark.asyncio
async def test_a_capped_restore_boundary_is_merged_not_flushed(tmp_path, monkeypatch):
    """A boundary ahead because of a capped restore must NOT trigger a flush.

    The boundary-ahead condition has two causes needing opposite remedies. Here
    disk legitimately holds MORE than memory: a capped restore dropped leading
    messages from the window without bumping ``_disk_older_count``. Flushing is
    destructive in that state -- the frozen prefix is keyed on
    ``_disk_older_count`` (``chat_persistence`` builds it as
    ``body[:disk_older]``), which the cap never moved, so the prefix is empty and
    the save writes ``meta + window`` over the whole file, truncating disk from
    250 rows to 52 and destroying 198 persisted messages.

    The discriminator is the invariant ``channel_slots`` states for these
    counters -- disk holds ``_disk_older_count`` frozen rows then the window -- so
    disk holding more than ``older + window`` means rows exist that the counters
    do not represent. This asserts the resulting transcript AND that disk is
    still intact, because a count alone would not catch the truncation.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "dashboard:capped"

    slot = state.get_or_create_slot("capped")
    for i in range(250):
        slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    on_disk_before = len(state.conversation_log.read_messages_chained(key))
    assert on_disk_before == 250, f"fixture expected 250 on disk, got {on_disk_before}"

    # The capped restore: the real path caps the window and sets _resumed_count to
    # the capped length. It does NOT bump _disk_older_count, which is exactly why
    # the boundary ends up ahead of the window.
    slot.messages = slot.messages[-50:]
    slot._resumed_count = len(slot.messages)
    slot.append("user", "new1", "msg")
    slot.append("assistant", "new2", "msg")
    slot.drain()
    assert slot._dirty is True, "fixture expects a dirty slot"
    assert slot._disk_window_len > len(slot.messages), (
        "fixture expects the boundary AHEAD of the resident window, got "
        f"{slot._disk_window_len} vs {len(slot.messages)}"
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/capped/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"
        data = await resp.json()

    assert data["messages"] == 252, (
        f"expected 252 (250 persisted + the 2 unpersisted appends), got {data['messages']}: "
        "the boundary-ahead branch flushed the smaller resident window over a longer "
        "disk history instead of merging"
    )
    # The data-loss probe: a count of the RESPONSE alone cannot see this, because
    # the response is assembled in memory while the damage happens on disk.
    # Asserted as ">= the original 250, oldest turn intact" rather than "== 250":
    # the property under test is that nothing persisted is DESTROYED, not that the
    # dirty tail stays unwritten. Observed here is 252 -- the tail also reached
    # disk, non-destructively, which is fine. Before the fix this read 52.
    on_disk_after = state.conversation_log.read_messages_chained(key)
    assert len(on_disk_after) >= on_disk_before, (
        f"disk held {on_disk_before} messages before the fork and {len(on_disk_after)} "
        "after: the flush wrote the capped window over the persisted history, and the "
        "frozen prefix was empty because the cap never bumped _disk_older_count"
    )
    disk_visible = [m for m in on_disk_after if m.get("role") in ("user", "assistant")]
    assert disk_visible[0].get("content") == "m0", (
        "the oldest persisted turn is gone from disk: the capped window was written "
        f"over the history, leaving {len(on_disk_after)} rows starting at "
        f"{disk_visible[0].get('content')!r}"
    )
    new_slot = state._slots.get(data["key"])
    visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
    assert visible[0]["content"] == "m0", "the forked transcript lost its oldest turn"
    assert visible[-1]["content"] == "new2", "the forked transcript lost the dirty tail"


@pytest.mark.asyncio
async def test_a_pending_rewrite_does_not_fork_discarded_turns(tmp_path, monkeypatch):
    """A slot mid-rewind must not fork the turns the user discarded.

    ``_pending_rewrite`` means "disk is known stale and still holds the PRE-EDIT
    transcript, because the truncating rewrite has not been written yet". None of
    the four counters this loop captures carries that state: ``chat_rewind`` sets
    ``_dirty``, zeroes ``_resumed_count`` and sets ``_pending_rewrite``, but never
    touches ``_disk_window_len`` -- so the boundary keeps its pre-rewind value and
    can still satisfy ``disk_len_before <= count_before``. The authoritative branch
    is then taken, the disk read returns the pre-rewind transcript, and the
    post-await re-check PASSES because nothing moved during the read: it measures
    stability, not correctness.

    ``session_transfer._guard_snapshot`` refuses on exactly this flag for exactly
    this reason; ``chat_fork`` had no equivalent. The fork lock does not help --
    ``chat_fork.py:147`` is its only acquirer, so no rewind path takes it.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:rewindfork"

    # Disk holds the full PRE-REWIND transcript.
    for i in range(50):
        log.append(key, "user" if i % 2 == 0 else "assistant", f"m{i}")
    assert len(log.read_messages_chained(key)) == 50, "fixture expected 50 on disk"

    slot = state.get_or_create_slot("rewindfork")
    for i in range(50):
        slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
    slot.drain()
    # The boundary sits BELOW the resident window, which is what routes this into
    # the authoritative branch rather than the capped-restore branch.
    slot._disk_window_len = 10
    slot._disk_older_count = 0

    # Exactly what chat_rewind does (chat_rewind.py:205-212): truncate the window,
    # mark dirty, zero _resumed_count, set _pending_rewrite -- and leave
    # _disk_window_len alone.
    del slot.messages[20:]
    slot._dirty = True
    slot._resumed_count = 0
    slot._pending_rewrite = True

    assert slot._disk_window_len <= len(slot.messages), (
        "fixture must land on the AUTHORITATIVE branch: boundary "
        f"{slot._disk_window_len} must be <= window {len(slot.messages)}"
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/rewindfork/fork", json={})
        body = await resp.json()

    # The shipped remedy is save-and-retry, not refusal: a rewrite save clears the
    # flag, so the fork is required to SUCCEED here. Asserting 200 (rather than
    # accepting a 503 as an alternative disposition) is what makes the pre-read
    # guard load-bearing -- with only the post-await check the loop spends all four
    # attempts and falls through to the retryable 503, which would satisfy a
    # weaker assertion and leave that half of the fix unmeasured.
    assert resp.status == 200, (
        f"fork returned {resp.status} instead of succeeding: {body}. The pending "
        "rewrite is recoverable -- a rewrite save clears the flag -- so a refusal "
        "here abandons the fork-must-succeed property this handler documents"
    )
    new_slot = state._slots.get(body["key"])
    assert new_slot is not None, f"no forked slot for {body.get('key')!r}"
    contents = [m.get("content") for m in new_slot.messages]
    discarded = [c for c in contents if c in {f"m{i}" for i in range(20, 50)}]
    assert not discarded, (
        f"the fork carried {len(discarded)} turn(s) the user rewound away "
        f"(e.g. {discarded[:3]}): the authoritative branch trusted a boundary that "
        "the rewind never moved, and the disk read returned the pre-edit transcript"
    )


@pytest.mark.asyncio
async def test_a_pending_rewrite_arriving_during_the_read_forces_a_retry(tmp_path, monkeypatch):
    """The flag must be re-checked AFTER the await, not only before it.

    This is the interleaving the four counters cannot see, and the one the finding
    calls worse because no equality test catches it: a failed in-place rewrite
    leaves ``_pending_rewrite`` set while ``_dirty_gen``, ``len(slot.messages)``,
    ``_disk_window_len`` and ``_disk_older_count`` all sit still. Every counter
    matches across the read, so the loop reads "stable" and would fork from a
    snapshot it has been told is stale.

    The probe is the READ COUNT: with the post-await check the attempt is spent and
    the loop re-reads, so the read runs more than once. A content assertion cannot
    discriminate here, because this fixture's disk and window agree -- the defect is
    that the handler trusts a snapshot it was told not to, not that these particular
    bytes differ.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:midread"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    slot = state.get_or_create_slot("midread")
    slot.append("user", "history-1", "msg msg-u")
    slot.append("assistant", "history-2", "msg msg-a")
    slot.drain()
    slot._disk_window_len = len(slot.messages)
    slot._disk_older_count = 0
    slot._resumed_count = len(slot.messages)
    slot._dirty = False
    assert not slot._pending_rewrite, "fixture must start with a clean flag"

    original = log.read_messages_chained
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def racing_read(k):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            release.wait(timeout=10)
        return original(k)

    monkeypatch.setattr(log, "read_messages_chained", racing_read)

    async def racer():
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "read 1 never entered"
        # A failed in-place rewrite: the flag is set and NOTHING else moves, so the
        # counter equality check below cannot detect it.
        slot._pending_rewrite = True
        release.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(client.post("/api/chat/slots/midread/fork", json={}))
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post
        body = await resp.json()

    assert calls["n"] >= 2, (
        f"the transcript was read {calls['n']} time(s): a rewind marked the slot "
        "stale DURING the read and the handler accepted that snapshot anyway, "
        "because the counter equality check cannot see a flag that moves no counter"
    )
    if resp.status != 200:
        assert body.get("code") == "fork_snapshot_unstable", f"unexpected error: {body}"


def _raw_disk_rows(log, key):
    """Non-metadata rows read straight off the JSONL, BYPASSING ``_msg_cache``.

    The cache is the only thing between this and ``read_messages_chained``, so a
    divergence between the two is proof the cached list object was mutated.
    """
    import json as _json

    return [
        line
        for line in log._path(key).read_text(encoding="utf-8").splitlines()
        if line.strip() and _json.loads(line).get("_type") != "metadata"
    ]


@pytest.mark.asyncio
async def test_the_fork_does_not_mutate_the_shared_message_cache(tmp_path, monkeypatch):
    """The fork must not extend the list ``read_messages_chained`` handed it.

    ``_read_messages`` returns the **shared cached list object by identity** on a
    cache hit and its docstring requires callers to treat it as immutable.
    ``read_messages_chained`` passes that object straight through for a session
    with no ``tab_id`` (and for a tid whose index resolves to no keys, and when
    every chained read comes back empty), so the fork's merge extends the cache
    itself.

    Base mutated it too, but base always took the durable save when dirty, and
    that save invalidates the entry -- so the mutation was transient. The
    capped-restore branch SKIPS the save deliberately, which is what turns a
    self-healing race window into durable corruption: nothing evicts the entry,
    and every later reader of this key sees the unpersisted tail as though it
    were history.

    Asserted against the RAW file rather than a count, because the corruption is
    invisible to any probe that reads through the same cache.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:cachemut"

    slot = state.get_or_create_slot("cachemut")
    for i in range(250):
        slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)

    on_disk_before = len(_raw_disk_rows(log, key))
    assert on_disk_before == 250, f"fixture expected 250 rows on disk, got {on_disk_before}"

    # The capped restore: caps the window and sets _resumed_count, without
    # bumping _disk_older_count -- which is what leaves the boundary ahead and
    # makes the handler take the skip-the-save branch.
    slot.messages = slot.messages[-50:]
    slot._resumed_count = len(slot.messages)
    slot.append("user", "UNPERSISTED-1", "msg")
    slot.append("assistant", "UNPERSISTED-2", "msg")
    slot.drain()
    assert slot._dirty is True, "fixture expects a dirty slot"
    assert slot._disk_window_len > len(slot.messages), (
        "fixture expects the boundary AHEAD of the resident window, got "
        f"{slot._disk_window_len} vs {len(slot.messages)}"
    )

    # ESTABLISH THE CACHE HIT, rather than hoping one happens. Identity sharing is
    # best-effort memoization, NOT a contract this path can be relied on to offer:
    # the single publish site (``history.py`` ~:4843) stores an entry only when the
    # fill held the writer RLock, and ``_cache_fill_lock`` makes exactly ONE
    # non-blocking attempt while on an event loop. A plain reader never holds the
    # cross-process flock, so when that attempt loses, ``flock_witness`` is None,
    # nothing is published, and the caller is handed a private parse -- two reads
    # then come back EQUAL BUT NOT IDENTICAL. An earlier revision of this test
    # asserted identity between two arbitrary reads and failed in CI on both Linux
    # and Windows for exactly that reason.
    #
    # The WARM path (~:4609-4612) is lock-free and unconditional: it returns the
    # cached list by identity whenever the stored mtime and generation both still
    # match. Publishing the entry here makes that hit deterministic, which is what
    # puts the fork on the shared-object path it is being tested against.
    cached_obj = list(log.read_messages_chained(key))
    log._msg_cache[key] = (log._path(key).stat().st_mtime, log._cache_gen(key), cached_obj)
    # FIXTURE GUARD: the hit is live, so a reader really is handed THIS object.
    assert log.read_messages_chained(key) is cached_obj, (
        "fixture failed to establish a cache hit: a read did not return the entry "
        "just published, so nothing is shared and this test would pass with or "
        "without the fix"
    )
    assert len(cached_obj) == on_disk_before, (
        f"fixture expected the cached entry to hold the {on_disk_before} persisted rows, "
        f"got {len(cached_obj)}"
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/cachemut/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"

    # NON-VACUITY GUARD: the entry must still be the object published above. If it
    # were evicted or replaced, the fork's read was served a private parse and the
    # assertions below could not observe the mutation at all.
    entry = log._msg_cache.get(key)
    assert entry is not None and entry[2] is cached_obj, (
        "the cache entry was evicted or replaced during the fork, so this run could "
        "not observe whether the shared object was mutated -- the result below would "
        "be vacuous"
    )

    # THE RESULT ASSERTION. Read through the cache, and compare to the file.
    through_cache = log.read_messages_chained(key)
    raw_after = _raw_disk_rows(log, key)
    phantom = [
        m.get("content")
        for m in through_cache
        if str(m.get("content", "")).startswith("UNPERSISTED-")
    ]
    assert len(through_cache) == len(raw_after), (
        f"a later reader sees {len(through_cache)} messages through the cache while the "
        f"file holds {len(raw_after)}: the fork extended the shared cached list in "
        f"place and the skipped save never evicted it. Phantom rows: {phantom}"
    )
    assert not phantom, (
        f"the unpersisted tail {phantom} is visible to later readers of {key} as though "
        "it were persisted history, because the shared cache entry was mutated"
    )


@pytest.mark.asyncio
async def test_a_tid_session_whose_index_resolves_is_unaffected(tmp_path, monkeypatch):
    """OPPOSITE DIRECTION: the fresh-list path must behave exactly as before.

    A tid whose index resolves to keys makes ``read_messages_chained`` build a NEW
    list, so there was never anything shared to corrupt. Copying before the merge
    must not change what the fork produces here.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:tidfork"

    slot = state.get_or_create_slot("tidfork")
    for i in range(6):
        slot.append("user" if i % 2 == 0 else "assistant", f"t{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    log.update_metadata(key, {"tab_id": "abc123def456"})
    assert log.get_metadata(key).get("tab_id"), "fixture expected a tab_id"
    # Resolve the tid to keys directly. A tab_id alone is NOT enough: when the
    # index resolves to nothing, ``read_messages_chained`` falls back to the
    # shared object, which is the sibling test's path. Injecting the mapping is
    # what puts this test on the fresh-list branch instead.
    log._tab_id_index = {"abc123def456": [key]}
    # The fresh-list path builds a NEW list per call, so non-identity here is
    # structural rather than timing-dependent -- but only while the chained read is
    # NON-EMPTY. An empty result falls back to the shared-object path, so assert
    # the content first; otherwise this guard could trip for the wrong reason.
    assert log.read_messages_chained(key), "fixture expected a non-empty chained read"
    assert log.read_messages_chained(key) is not log.read_messages_chained(key), (
        "fixture expected the tid path to build a NEW list each call; if it shares, "
        "this test is measuring the same path as its sibling"
    )

    slot.append("user", "tail-1", "msg")
    slot.drain()

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/tidfork/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"
        data = await resp.json()

    new_slot = state._slots.get(data["key"])
    visible = [m["content"] for m in new_slot.messages if m["role"] in ("user", "assistant")]
    assert visible[0] == "t0", f"the forked transcript lost its oldest turn: {visible[:3]}"
    assert "tail-1" in visible, f"the forked transcript lost the dirty tail: {visible[-3:]}"


@pytest.mark.asyncio
async def test_a_slot_that_legitimately_saves_still_reaches_disk(tmp_path, monkeypatch):
    """OPPOSITE DIRECTION: the save path must still save.

    Copying before the merge must not disturb the branch where the counters agree
    with disk and the durable write is the correct action. If the tail stops
    reaching disk, this goes red.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:savesfine"

    slot = state.get_or_create_slot("savesfine")
    slot.append("user", "s0", "msg")
    slot.append("assistant", "s1", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    before = len(_raw_disk_rows(log, key))

    slot.append("user", "SAVED-TAIL", "msg")
    slot.drain()
    assert slot._dirty is True, "fixture expects a dirty slot"
    assert slot._disk_window_len <= len(
        slot.messages
    ), "fixture expects the boundary NOT ahead, so the handler takes the save path"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/savesfine/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"

    raw_after = _raw_disk_rows(log, key)
    assert len(raw_after) > before, (
        f"disk held {before} rows and still holds {len(raw_after)}: the legitimate save "
        "path stopped persisting the tail"
    )
    assert any(
        "SAVED-TAIL" in r for r in raw_after
    ), "the dirty tail did not reach disk on the path that is supposed to save it"


@pytest.mark.asyncio
async def test_a_rewind_landing_during_the_pending_rewrite_save_is_not_erased(
    tmp_path, monkeypatch
):
    """A rewind that lands DURING the pending-rewrite save must not be lost.

    The ``_pending_rewrite`` arm saves and retries rather than refusing, because a
    rewrite save clears the flag and the fork is then free to read a fresh disk.
    But ``chat_persistence`` clears that flag UNCONDITIONALLY (``if rewrite:
    slot._pending_rewrite = False``) with no check that the flag it clears is the
    one its own snapshot was taken for. The save is an ``await``, so a rewind can
    land inside it: the write persists the PRE-rewind transcript, the clear erases
    the new rewind's flag, and the next attempt sees ``False`` and falls through to
    a disk read still holding the discarded turns.

    The post-await stability re-check cannot catch it -- it re-baselines its four
    counters on the NEW attempt, i.e. after the flag went missing, so it measures
    the stability of the new attempt rather than the flag that was lost.

    Asserted on the FORKED TRANSCRIPT, not on a status code and not on the flag:
    a fork returning 200, or ``_pending_rewrite`` reading False after the save,
    are both true with or without the fix.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:rewindrace"

    slot = state.get_or_create_slot("rewindrace")
    for i in range(10):
        slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard import chat_fork as chat_fork_mod
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    assert len(_raw_disk_rows(log, key)) == 10, "fixture expected 10 rows persisted"

    # A first rewind already happened and its rewrite has not been written yet.
    slot._pending_rewrite = True
    slot._dirty = True

    real_save = chat_fork_mod.save_slot_off_loop
    landed = {"n": 0}

    async def _save_with_a_rewind_landing_inside(state_, slot_, *a, **kw):
        """Model the real save's interleaving: snapshot, then write, then clear.

        The rewind is landed AFTER the snapshot is taken and BEFORE the write and
        the flag clear -- which is exactly the window the real save leaves open,
        since it snapshots on the loop and clears the flag in the worker thread
        after writing.
        """
        if landed["n"] == 0:
            landed["n"] = 1
            stale_snapshot = list(slot_.messages)
            # The concurrent rewind, matching ``chat_rewind``'s own sequence:
            # truncate, mark dirty, zero _resumed_count, set the flag.
            del slot_.messages[6:]
            slot_._dirty = True
            slot_._resumed_count = 0
            slot_._pending_rewrite = True
            # The save writes the PRE-rewind snapshot it captured, and clears the
            # flag -- including the rewind's, which it never owned.
            await real_save(state_, slot_, stale_snapshot, rewrite=True, best_effort=False)
            slot_._pending_rewrite = False
            return
        await real_save(state_, slot_, *a, **kw)

    monkeypatch.setattr(chat_fork_mod, "save_slot_off_loop", _save_with_a_rewind_landing_inside)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/rewindrace/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"
        data = await resp.json()

    # FIXTURE GUARD: the interleave actually ran, and the flag really was erased.
    assert landed["n"] == 1, "fixture never entered the pending-rewrite save"

    new_slot = state._slots.get(data["key"])
    assert new_slot is not None, f"forked slot {data['key']} not found"
    visible = [m["content"] for m in new_slot.messages if m["role"] in ("user", "assistant")]
    discarded = [c for c in visible if c in {"m6", "m7", "m8", "m9"}]
    assert not discarded, (
        f"the forked transcript contains the turns the rewind discarded: {discarded}. "
        "The rewind landed during the pending-rewrite save, that save persisted the "
        "pre-rewind snapshot and cleared the flag it did not own, and the fork then "
        f"copied the stale disk read. Forked transcript: {visible}"
    )
    assert visible[:6] == [
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
        "m5",
    ], f"the forked transcript lost turns the rewind KEPT: {visible}"


@pytest.mark.asyncio
async def test_a_pending_rewrite_fork_without_a_concurrent_rewind_still_succeeds(
    tmp_path, monkeypatch
):
    """DIRECTION NOT BROKEN: the ordinary pending-rewrite fork must not 503.

    The generation witness must not trip on the save's own bookkeeping. If it did,
    every pending-rewrite fork would spend its attempt budget and refuse -- turning
    the recoverable path into a refusal.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    slot = state.get_or_create_slot("pendok")
    for i in range(8):
        slot.append("user" if i % 2 == 0 else "assistant", f"p{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    slot._pending_rewrite = True
    slot._dirty = True
    gen_before = slot._dirty_gen

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/pendok/fork", json={})
        body = await resp.text()
        assert resp.status == 200, (
            f"an ordinary pending-rewrite fork was refused with {resp.status}: {body}. "
            "The generation witness tripped on the save's own bookkeeping instead of "
            "on a genuine concurrent re-dirty."
        )
        data = await resp.json()

    assert slot._dirty_gen == gen_before, (
        "fixture expected NO genuine re-dirty in this scenario; the generation moved "
        f"from {gen_before} to {slot._dirty_gen}, so this test is not measuring the "
        "quiet path it claims to"
    )
    new_slot = state._slots.get(data["key"])
    visible = [m["content"] for m in new_slot.messages if m["role"] in ("user", "assistant")]
    assert visible[0] == "p0" and visible[-1] == "p7", f"forked transcript wrong: {visible}"


@pytest.mark.asyncio
async def test_a_fork_with_no_pending_rewrite_is_untouched(tmp_path, monkeypatch):
    """DIRECTION NOT BROKEN: the ordinary non-pending path must be unaffected.

    The carry flag starts False and the arm is never entered, so this exercises the
    path the guard must leave completely alone.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    slot = state.get_or_create_slot("nopend")
    for i in range(5):
        slot.append("user" if i % 2 == 0 else "assistant", f"n{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    assert slot._pending_rewrite is False, "fixture expects NO pending rewrite"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/nopend/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"
        data = await resp.json()

    new_slot = state._slots.get(data["key"])
    visible = [m["content"] for m in new_slot.messages if m["role"] in ("user", "assistant")]
    assert visible == ["n0", "n1", "n2", "n3", "n4"], f"forked transcript wrong: {visible}"


def _archived_contents(log):
    """Every message content present in this log's archive segments.

    ``_archive_dropped_lines`` is reached ONLY from the rewrite branch
    (``if rewrite and path.exists()``), so an empty result after a truncating
    save means the save took the plain path and the dropped turns were deleted
    without ever being archived.
    """
    import json as _json

    from kiro_crew.history import _archive_dir

    adir = _archive_dir(log._dir)
    if not adir.exists():
        return []
    found = []
    for seg in sorted(adir.glob("*.jsonl")):
        for line in seg.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = _json.loads(line)
            except ValueError:
                continue
            if row.get("_type") == "archive":
                continue
            if row.get("content") is not None:
                found.append(row["content"])
    return found


@pytest.mark.asyncio
async def test_the_pending_rewrite_retry_still_archives_the_discarded_turns(tmp_path, monkeypatch):
    """The retry save must take the ARCHIVE-SAFE rewrite path, not a plain save.

    ``_save_slot_to_history`` only ever PROMOTES ``rewrite`` to True -- ``if
    messages is not None or slot._pending_rewrite: rewrite = True`` -- and the
    archival is gated behind it (``if rewrite and path.exists():
    _archive_dropped_lines(...)``), as is the ``rotation_generation`` bump.

    On the retry both promotion inputs are absent: no explicit snapshot is passed,
    and ``_pending_rewrite`` was cleared by the first save (that clearing is the
    very race the retry exists to recover from). So the retry would persist the
    rewind's truncation through the PLAIN path, deleting the discarded turns from
    disk with no archive copy -- unrecoverable, where the pre-retry behaviour at
    least left them on disk.

    Asserted on the ARCHIVE, not on the transcript: the sibling test already covers
    the transcript, and it passes whether or not the dropped turns were archived.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:archiverace"

    slot = state.get_or_create_slot("archiverace")
    for i in range(10):
        slot.append("user" if i % 2 == 0 else "assistant", f"a{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard import chat_fork as chat_fork_mod
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    assert len(_raw_disk_rows(log, key)) == 10, "fixture expected 10 rows persisted"
    assert _archived_contents(log) == [], "fixture expected an empty archive to start"

    slot._pending_rewrite = True
    slot._dirty = True

    real_save = chat_fork_mod.save_slot_off_loop
    landed = {"n": 0}

    async def _save_with_a_rewind_landing_inside(state_, slot_, *a, **kw):
        if landed["n"] == 0:
            landed["n"] = 1
            stale_snapshot = list(slot_.messages)
            del slot_.messages[6:]
            slot_._dirty = True
            slot_._resumed_count = 0
            slot_._pending_rewrite = True
            await real_save(state_, slot_, stale_snapshot, rewrite=True, best_effort=False)
            slot_._pending_rewrite = False
            return
        await real_save(state_, slot_, *a, **kw)

    monkeypatch.setattr(chat_fork_mod, "save_slot_off_loop", _save_with_a_rewind_landing_inside)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/archiverace/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"

    assert landed["n"] == 1, "fixture never entered the pending-rewrite save"

    # FIXTURE GUARD: the retry really did truncate disk. Without this a missing
    # archive would be trivially "correct" because nothing was ever dropped.
    on_disk = [
        c
        for c in (_raw_disk_rows(log, key))
        if '"a6"' in c or '"a7"' in c or '"a8"' in c or '"a9"' in c
    ]
    assert not on_disk, (
        "fixture expected the retry to have removed the discarded turns from disk; "
        f"{len(on_disk)} of them are still there, so this test cannot observe whether "
        "they were archived first"
    )

    archived = _archived_contents(log)
    missing = [c for c in ("a6", "a7", "a8", "a9") if c not in archived]
    assert not missing, (
        f"the retry deleted the discarded turns {missing} from disk WITHOUT archiving "
        "them: it took the plain save path because the first save had already cleared "
        "_pending_rewrite and no explicit snapshot was passed, so `rewrite` never got "
        f"promoted and the archive branch was skipped. Archived contents: {archived}"
    )


@pytest.mark.asyncio
async def test_a_first_entry_pending_rewrite_save_archives_as_before(tmp_path, monkeypatch):
    """DIRECTION NOT BROKEN: first-entry archival behaviour is unchanged.

    On the FIRST entry ``_pending_rewrite`` is still set, so
    ``_save_slot_to_history`` promotes ``rewrite`` on its own and the archival
    already happens. This pins that, so passing ``rewrite=True`` explicitly at the
    call site is provably a no-op here rather than a behaviour change.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:firstentry"

    slot = state.get_or_create_slot("firstentry")
    for i in range(8):
        slot.append("user" if i % 2 == 0 else "assistant", f"f{i}", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)
    assert len(_raw_disk_rows(log, key)) == 8, "fixture expected 8 rows persisted"

    # A rewind whose rewrite has NOT been written: exactly the state the arm is for.
    del slot.messages[5:]
    slot._dirty = True
    slot._resumed_count = 0
    slot._pending_rewrite = True

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/firstentry/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"

    archived = _archived_contents(log)
    missing = [c for c in ("f5", "f6", "f7") if c not in archived]
    assert not missing, (
        f"the ordinary first-entry pending-rewrite save stopped archiving {missing}. "
        f"Archived contents: {archived}"
    )


@pytest.mark.asyncio
async def test_a_save_completing_mid_read_does_not_fork_a_superseded_variant(tmp_path, monkeypatch):
    """A save COMPLETING during the read must invalidate the attempt.

    The post-await guard witnesses four counters plus ``_pending_rewrite``. None of
    them can see a save that merely COMPLETES: an in-place content edit (a variant
    switch) leaves ``len(slot.messages)`` and ``_disk_older_count`` alone, the save
    re-assigns ``_disk_window_len`` to the same value because the window length did
    not change, and the ``_dirty`` setter advances ``_dirty_gen`` only on a True
    assignment -- so CLEARING it moves nothing.

    That leaves a window where the threaded read returns pre-save bytes while the
    slot now says everything is persisted. The boundary-derived tail slice is empty,
    so the fork adopts the stale disk read verbatim and the forked session keeps the
    SUPERSEDED content.

    ``_dirty`` is added to the RETRY witness set for this, NOT to the merge decision:
    a mismatch re-reads, and the merge stays keyed on the boundary exactly as before
    (see ``test_fork_sees_the_tail_when_a_flush_clears_dirty_mid_read``, which pins
    that a cleared ``_dirty`` must never skip the reconciliation).
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    slot = state.get_or_create_slot("variantrace")
    for i in range(6):
        slot.append("user" if i % 2 == 0 else "assistant", f"v{i}-OLD", "msg")
    slot.drain()
    from kiro_crew.dashboard.chat import _save_slot_to_history

    _save_slot_to_history(state, slot)

    # Steady state: the boundary is a usable index and claims everything persisted,
    # so the tail slice the fork takes below is empty.
    assert slot._disk_window_len == len(slot.messages), (
        "fixture expects the boundary to equal the resident window, got "
        f"{slot._disk_window_len} vs {len(slot.messages)}"
    )

    # The variant switch: content replaced IN PLACE. No append, no trim.
    target = next(m for m in slot.messages if m.get("content") == "v3-OLD")
    target["content"] = "v3-NEW"
    slot._dirty = True

    before = (
        slot._disk_window_len,
        slot._dirty_gen,
        len(slot.messages),
        slot._disk_older_count,
    )
    real_read = log.read_messages_chained
    landed = {"n": 0}

    def _read_then_let_a_save_complete(*a, **kw):
        """Return pre-save bytes, then let a flush complete -- as the race does."""
        stale = [dict(m) for m in real_read(*a, **kw)]
        if landed["n"] == 0:
            landed["n"] = 1
            # Exactly what ``flush_slot_now`` does: save, then clear ``_dirty`` only
            # if the generation did not move under it.
            gen = slot._dirty_gen
            _save_slot_to_history(state, slot)
            if slot._dirty_gen == gen:
                slot._dirty = False
        return stale

    monkeypatch.setattr(log, "read_messages_chained", _read_then_let_a_save_complete)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/variantrace/fork", json={})
        assert resp.status == 200, f"fork failed: {await resp.text()}"
        data = await resp.json()

    # FIXTURE GUARDS. Without these a pass could come from the existing four checks
    # having caught the change, which would make this test measure nothing new.
    assert landed["n"] == 1, "fixture never let a save complete during the read"
    after = (
        slot._disk_window_len,
        slot._dirty_gen,
        len(slot.messages),
        slot._disk_older_count,
    )
    assert after == before, (
        "fixture expected ALL FOUR witnessed counters to be unchanged by the save -- "
        f"otherwise the existing guard already catches this. before={before} after={after}"
    )
    assert slot._dirty is False, (
        "fixture expected the completing save to have CLEARED _dirty; without that "
        "flip there is no window for this test to observe"
    )

    new_slot = state._slots.get(data["key"])
    assert new_slot is not None, f"forked slot {data['key']} not found"
    visible = [m["content"] for m in new_slot.messages if m["role"] in ("user", "assistant")]
    assert "v3-OLD" not in visible, (
        "the forked session carries the SUPERSEDED variant 'v3-OLD': a save completed "
        "during the threaded read, so the read returned pre-save bytes while the slot "
        "reported everything persisted. None of the four witnessed counters moved and "
        f"_dirty was cleared without bumping _dirty_gen. Forked transcript: {visible}"
    )
    assert (
        "v3-NEW" in visible
    ), f"the forked session lost the current variant 'v3-NEW' entirely: {visible}"
