"""Session transfer between instances — bundle, validation, and import.

Covers the two halves of the feature (``build_transfer_bundle`` on the sending
side, ``api_chat_slot_import`` on the receiving side) plus the tunnel-manager
delivery hop, with the emphasis on the invariants a reviewer would want pinned:

* **copy, never move** — the source is untouched and the target key is new;
* **project does NOT travel** — the documented decision that an imported
  session arrives unscoped so the user re-picks a checkout;
* **unknown bundle versions are refused** rather than best-effort parsed;
* **the token never appears in a transfer response** (instances.md §6's
  "connect + refresh-token are the only two token-crossing routes").
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.dashboard.session_transfer import (
    BUNDLE_VERSION,
    _validate_bundle,
    build_transfer_bundle,
    local_instance_label,
)

# ── bundle construction ──────────────────────────────────────────────────


class _FakeLog:
    def __init__(self, messages):
        self._messages = messages

    def read_messages_chained(self, _key):
        return list(self._messages)


def _slot(messages, *, title="My session", titled=True, agent="", dirty=False, project=""):
    return SimpleNamespace(
        key="slot-1",
        title=title,
        _titled=titled,
        agent=agent,
        project=project,
        messages=list(messages),
        _dirty=dirty,
        _resumed_count=len(messages),
        _disk_window_len=len(messages),
        _pending_rewrite=False,
        _dirty_gen=0,
        memory_mode="persistent",
        # Idle by default: Layer B only travels when no turn is in flight.
        running=False,
        _in_stage_execution=False,
    )


def _state(messages):
    return SimpleNamespace(conversation_log=_FakeLog(messages))


def test_bundle_carries_only_visible_roles():
    msgs = [
        {"role": "user", "content": "hi", "ts": "t1"},
        {"role": "tool", "content": "tool frame", "ts": "t2"},
        {"role": "assistant", "content": "hello", "ts": "t3"},
        {"role": "system", "content": "sys", "ts": "t4"},
    ]
    slot = _slot(msgs)
    bundle = build_transfer_bundle(_state(msgs), slot, origin="mac")

    assert bundle["bundle_version"] == BUNDLE_VERSION
    assert bundle["origin"] == "mac"
    assert [m["role"] for m in bundle["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in bundle["messages"]] == ["hi", "hello"]


def test_bundle_does_not_carry_project_or_model():
    """The two fields deliberately dropped — a dangling path and an
    entitlement-specific model id (see the module docstring in the source)."""
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, project="/Volumes/workplace/only-on-my-mac")
    slot.model = "some-model-id"
    bundle = build_transfer_bundle(_state(msgs), slot)

    assert "project" not in bundle
    assert "model" not in bundle
    assert "/Volumes/workplace/only-on-my-mac" not in json.dumps(bundle)


def test_bundle_reads_full_history_not_just_resident_window():
    """A long session keeps only a tail in memory; the bundle must be complete."""
    on_disk = [{"role": "user", "content": f"turn {i}", "ts": ""} for i in range(10)]
    # slot.messages holds only the last two — bundling those would truncate.
    slot = _slot(on_disk[-2:])
    bundle = build_transfer_bundle(_state(on_disk), slot)

    assert len(bundle["messages"]) == 10
    assert bundle["messages"][0]["content"] == "turn 0"


def test_bundle_appends_unflushed_tail():
    on_disk = [{"role": "user", "content": "persisted", "ts": ""}]
    slot = _slot(on_disk, dirty=True)
    slot.messages = on_disk + [{"role": "assistant", "content": "not yet saved", "ts": ""}]
    slot._resumed_count = 1
    bundle = build_transfer_bundle(_state(on_disk), slot)

    assert [m["content"] for m in bundle["messages"]] == ["persisted", "not yet saved"]


def test_bundle_title_marker_does_not_compound_across_hops():
    """A session bounced back and forth must not grow one prefix per hop."""
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title="⇄ Already imported once")
    bundle = build_transfer_bundle(_state(msgs), slot)

    assert bundle["title"] == "Already imported once"


def test_bundle_untitled_slot_carries_empty_title():
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title="slot-1", titled=False)
    assert build_transfer_bundle(_state(msgs), slot)["title"] == ""


@pytest.mark.asyncio
async def test_send_handler_sends_each_turn_exactly_once(monkeypatch):
    """Regression for the duplicate-tail bug, restated as its real contract.

    The original bug was a pre-bundle flush combined with a ``_resumed_count``
    slice: the save wrote the tail to disk but did not touch that counter, so the
    bundle re-appended the same turns from memory. The guard was originally
    written as "the handler must not flush" — but the flush was the mechanism,
    not the contract. Slicing on ``_disk_window_len`` (which the save DOES
    advance) makes flushing correct, and a later finding showed it is required to
    carry in-place edits. So this asserts the invariant that actually matters:
    every turn appears exactly once.
    """
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    persisted = {"role": "user", "content": "persisted", "ts": ""}
    tail = {"role": "assistant", "content": "unsaved turn", "ts": ""}
    disk = {"messages": [persisted]}

    class _Log:
        def read_messages_chained(self, _key):
            return list(disk["messages"])

    slot = _slot([persisted], dirty=True)
    slot.messages = [persisted, tail]
    slot._disk_window_len = 1
    slot.key = "slot-1"

    async def _save(_state, s, best_effort=True):
        disk["messages"] = list(s.messages)
        s._dirty = False
        s._disk_window_len = len(s.messages)

    monkeypatch.setattr(st, "save_slot_off_loop", _save)

    captured: dict = {}

    class _Mgr:
        async def send_session_bundle(self, _id, bundle):
            captured["bundle"] = bundle
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_Log(),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
    )
    request = SimpleNamespace(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        get=lambda k, default="": {"user": "owner"}.get(k, default),
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 200, resp.body
    contents = [m["content"] for m in captured["bundle"]["messages"]]
    assert contents == ["persisted", "unsaved turn"], contents


def test_bundle_accepts_a_prefetched_history_without_touching_disk():
    """The async wrapper reads the transcript in a thread and passes it in; the
    assembler must use it rather than re-reading."""

    class _Exploding:
        def read_messages_chained(self, _key):
            raise AssertionError("must not read disk when history is supplied")

    slot = _slot([])
    slot.messages = []
    state = SimpleNamespace(conversation_log=_Exploding())
    prefetched = [{"role": "user", "content": "from thread", "ts": ""}]

    bundle = build_transfer_bundle(state, slot, history=prefetched)

    assert [m["content"] for m in bundle["messages"]] == ["from thread"]


@pytest.mark.asyncio
async def test_build_bundle_async_offloads_the_blocking_read_to_a_thread():
    """The transcript read is large synchronous file IO; running it on the event
    loop stalls every task and starves the watchdog heartbeat."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    seen: dict[str, object] = {}

    def _record_thread(fn, *args):
        seen["offloaded"] = fn
        return fn(*args)

    async def _fake_to_thread(fn, *args):
        return _record_thread(fn, *args)

    original = st.asyncio.to_thread
    st.asyncio.to_thread = _fake_to_thread  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")
    finally:
        st.asyncio.to_thread = original  # type: ignore[assignment]

    # Assembly (including the regex-heavy redaction) is offloaded too, not just
    # the read — holding the loop for either is what starves the heartbeat.
    assert seen.get("offloaded") is st._read_and_assemble
    assert [m["content"] for m in bundle["messages"]] == ["hi"]


@pytest.mark.asyncio
async def test_snapshot_retries_when_a_flush_lands_during_the_read():
    """Regression: the offloaded read introduced an await the 5s flush can land in.

    Simulates the dangerous interleaving — the read returns PRE-flush content and
    the flush then advances the boundary and clears ``_dirty``. A naive merge
    would see a clean slot and drop the tail entirely. The snapshot must notice
    the boundary moved and retry, so the tail still reaches the copy.
    """
    from kiro_crew.dashboard import session_transfer as st

    tail = {"role": "assistant", "content": "tail turn", "ts": ""}
    persisted = {"role": "user", "content": "persisted", "ts": ""}

    slot = _slot([persisted], dirty=True)
    slot.messages = [persisted, tail]
    slot._disk_window_len = 1
    # Already persisted as far as the pre-bundle flush is concerned: this test
    # targets the post-await guards, so it must not trigger a real save.
    slot._dirty = False

    # Disk content grows when the simulated flush lands.
    disk = {"messages": [persisted]}
    reads: list[int] = []

    class _Log:
        def read_messages_chained(self, _key):
            reads.append(len(disk["messages"]))
            return list(disk["messages"])

    state = SimpleNamespace(conversation_log=_Log())

    calls = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _flush_midway(fn, *args):
        result = fn(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            # The flush completes while we were "off the loop": the tail is now
            # on disk and the persisted boundary has advanced.
            disk["messages"] = [persisted, tail]
            slot._disk_window_len = 2
            slot._dirty = False
        return result

    st.asyncio.to_thread = _flush_midway  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(state, slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    contents = [m["content"] for m in bundle["messages"]]
    # Retried, so the post-flush disk read carries the tail exactly once.
    assert calls["n"] >= 2, "expected a retry after the boundary moved"
    assert contents == ["persisted", "tail turn"], contents


@pytest.mark.asyncio
async def test_import_offloads_agent_resolution_and_skips_it_when_unhinted(monkeypatch):
    """``list_agents`` scans a directory and parses manifests — not on the loop.

    Also asserts the common case pays no thread hop: an empty hint resolves
    without touching disk at all.
    """
    from kiro_crew.dashboard import session_transfer as st

    offloaded: list[object] = []
    real_to_thread = st.asyncio.to_thread

    async def _record(fn, *args):
        offloaded.append(fn)
        return fn(*args)

    monkeypatch.setattr(st.asyncio, "to_thread", _record)
    monkeypatch.setattr(st, "_resolve_agent", lambda n: n)

    created: dict = {}
    await _run_import(st, monkeypatch, _valid(agent="my-agent"), created=created)
    assert st._resolve_agent in offloaded or offloaded, "agent resolution must be offloaded"
    assert created.get("agent") == "my-agent"

    # Unhinted: no offload for agent resolution.
    offloaded.clear()
    created2: dict = {}
    await _run_import(st, monkeypatch, _valid(agent=""), created=created2)
    assert offloaded == [], "an empty agent hint must not cost a thread hop"
    assert created2.get("agent") == ""
    monkeypatch.setattr(st.asyncio, "to_thread", real_to_thread)


def test_bundle_boundary_is_the_disk_window_not_the_resume_count():
    """Regression: the persisted boundary is ``_disk_window_len``, not ``_resumed_count``.

    ``_resumed_count`` records how many messages were loaded when the slot was
    REHYDRATED, so for a session created in this gateway run it stays 0 no matter
    how often the slot flushes. Slicing on it appended the entire resident window
    on top of the disk history and duplicated every persisted turn — the ordinary
    case, not an edge case.
    """
    persisted = [
        {"role": "user", "content": "one", "ts": ""},
        {"role": "assistant", "content": "two", "ts": ""},
    ]
    unsaved = {"role": "user", "content": "three", "ts": ""}

    slot = _slot(persisted)
    slot.messages = persisted + [unsaved]
    # A fresh (never-rehydrated) slot that has flushed: resume count is still 0,
    # but two window messages are on disk.
    slot._resumed_count = 0
    slot._disk_window_len = 2
    slot._dirty = True

    bundle = build_transfer_bundle(_state(persisted), slot)

    assert [m["content"] for m in bundle["messages"]] == ["one", "two", "three"]


def test_bundle_appends_nothing_when_everything_is_persisted():
    persisted = [{"role": "user", "content": "one", "ts": ""}]
    slot = _slot(persisted)
    slot.messages = list(persisted)
    slot._disk_window_len = 1
    slot._dirty = False

    bundle = build_transfer_bundle(_state(persisted), slot)

    assert [m["content"] for m in bundle["messages"]] == ["one"]


@pytest.mark.asyncio
async def test_send_bundle_remints_once_when_the_peer_rejects_the_credential():
    """A retained credential can go stale while the tunnel stays CONNECTED, which
    is the condition ``token_validates`` exists for. One re-mint retry turns that
    into a transparent success instead of a spurious rejection."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {"peer": "stale"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )

    reminted: list[str] = []

    async def _refresh(instance_id):
        reminted.append(instance_id)
        mgr._tokens[instance_id] = "fresh"
        return "fresh"

    mgr.refresh_token = _refresh  # type: ignore[method-assign]

    sent: list[str] = []

    class _Resp:
        def __init__(self, status):
            self.status = status

        async def json(self):
            return {"key": "remote-1"} if self.status == 200 else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, json=None, headers=None):
            # First call carries the stale credential and is rejected; the retry
            # must carry the freshly minted one.
            cookie = headers["Cookie"]
            sent.append(cookie)
            return _Resp(403 if "stale" in cookie else 200)

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 1})
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert reminted == ["peer"]
    assert len(sent) == 2
    assert "mc_token_7778=stale" in sent[0]
    assert "mc_token_7778=fresh" in sent[1]


@pytest.mark.asyncio
async def test_bundle_refuses_while_a_rewrite_is_still_owed():
    """A rewind/regenerate leaves ``_pending_rewrite`` set until the TRUNCATING
    rewrite is written. Until then disk holds the pre-edit transcript and is
    longer than the resident window, so the boundary slice appends nothing and a
    bundle would carry turns the user explicitly rewound away."""
    from kiro_crew.dashboard import session_transfer as st

    kept = {"role": "user", "content": "kept", "ts": ""}
    rewound = {"role": "assistant", "content": "rewound away", "ts": ""}

    slot = _slot([kept])
    slot.messages = [kept]
    # Disk still has both; memory has been truncated and the rewrite is owed.
    slot._disk_window_len = 2
    slot._dirty = False  # this test targets the guards, not the pre-flush
    slot._pending_rewrite = True

    with pytest.raises(st.SnapshotUnstable):
        await st.build_transfer_bundle_async(_state([kept, rewound]), slot, origin="mac")


@pytest.mark.asyncio
async def test_snapshot_failure_is_raised_rather_than_read_inline():
    """Exhausted retries must FAIL, not fall back to a blocking inline read.

    An inline read would trade a lossy transcript for a blocking one, and on a
    large active session the blocking read is what starves the heartbeat into a
    watchdog-triggered gateway exit. A transfer is a copy, so failing costs
    nothing — the source is untouched and the user can retry.
    """
    from kiro_crew.dashboard import session_transfer as st

    persisted = {"role": "user", "content": "persisted", "ts": ""}
    slot = _slot([persisted], dirty=True)
    slot.messages = [persisted]
    slot._disk_window_len = 1
    slot._dirty = False  # this test targets the guards, not the pre-flush

    state = _state([persisted])
    bumps = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _never_settles(fn, *args):
        result = fn(*args)
        # Move the persisted boundary on every attempt so the check never passes.
        # Grow the window in step so the post-await guard does not fire first:
        # this test is about the retry cap, not the boundary-ahead refusal.
        bumps["n"] += 1
        slot._disk_window_len += 1
        slot.messages = slot.messages + [{"role": "user", "content": "more", "ts": ""}]
        return result

    st.asyncio.to_thread = _never_settles  # type: ignore[assignment]
    try:
        with pytest.raises(st.SnapshotUnstable):
            await st.build_transfer_bundle_async(state, slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert bumps["n"] == st._SNAPSHOT_ATTEMPTS


@pytest.mark.asyncio
async def test_import_refuses_when_the_durable_save_fails(monkeypatch):
    """A swallowed write failure would ack a transfer that only exists in memory,
    so a restart before the next flush would lose the imported session."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    async def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(st, "save_slot_off_loop", _boom)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "transfer_import_save_failed"
    # The half-created slot must not be left in the table.
    assert state._slots == {}


def test_bundle_redacts_assistant_content_on_the_way_out():
    """The bundle leaves this host, so redaction cannot be left to the receiver.

    A transcript written before the redactors existed (or carried in from a
    channel) can still hold a raw credential on disk; relying on the peer to
    scrub it would send the secret across the boundary first.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    msgs = [
        {"role": "user", "content": f"my key is {secret}", "ts": ""},
        {"role": "assistant", "content": f"noted {secret}", "ts": ""},
    ]
    slot = _slot(msgs)
    bundle = build_transfer_bundle(_state(msgs), slot)
    user_msg, assistant_msg = bundle["messages"]

    assert secret not in assistant_msg["content"]
    # The human's own words stay verbatim, matching the fork and import paths.
    assert secret in user_msg["content"]


def test_session_transfer_is_registered_as_an_egress_sink():
    """It emits transcript content off-host, so the posture panel must count it
    as an output boundary rather than allowlisting it as non-egress."""
    from kiro_crew.security_posture import (
        _REDACTION_SINKS,
        NON_EGRESS_REDACTION_MODULES,
    )

    modules = {module for _label, module, _detail in _REDACTION_SINKS}
    assert "dashboard/session_transfer.py" in modules
    assert "dashboard/session_transfer.py" not in NON_EGRESS_REDACTION_MODULES


def test_bundle_redacts_the_title_on_the_way_out():
    """A title is generated from user content, and the resume path assigns a
    client-supplied title with no scan of its own — so it can carry a credential
    that would otherwise leave the host verbatim."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title=f"debugging {secret}")

    bundle = build_transfer_bundle(_state(msgs), slot)

    assert secret not in bundle["title"]


@pytest.mark.asyncio
async def test_bundle_refuses_when_the_boundary_is_ahead_of_the_window():
    """Regression: a flush landing mid-stream leaves ``_disk_window_len`` larger
    than the resident window, so the tail slice yields nothing.

    ``_save_slot_to_history`` sets the boundary over the RAW window (streaming
    ``chunk`` rows included); ``_flush_segment`` then shrinks ``slot.messages`` to
    drop that chunk run and append the finalized assistant message, without
    adjusting the boundary. A transfer started during that turn would ship only
    the on-disk turns and still answer 200.
    """
    from kiro_crew.dashboard import session_transfer as st

    persisted = [
        {"role": "user", "content": "u1", "ts": ""},
        {"role": "assistant", "content": "a1", "ts": ""},
        {"role": "user", "content": "u2", "ts": ""},
    ]
    slot = _slot(persisted)
    # Post-_flush_segment shape: window shrank to 4, boundary still counts the
    # 3 chunk rows the flush wrote (6).
    slot.messages = persisted + [{"role": "assistant", "content": "a2", "ts": ""}]
    slot._disk_window_len = 6
    slot._dirty = False  # this test targets the guards, not the pre-flush

    with pytest.raises(st.SnapshotUnstable):
        await st.build_transfer_bundle_async(_state(persisted), slot, origin="mac")


@pytest.mark.asyncio
async def test_snapshot_rechecks_pending_rewrite_after_the_await():
    """Regression: a rewind landing DURING the threaded read must be caught.

    ``_pending_rewrite`` can flip to True while ``_disk_window_len`` stays put, so
    the boundary check alone reads as "stable" and the bundle would carry turns
    the user just discarded. The guards therefore run after every await, not only
    before the first one.
    """
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "kept", "ts": ""}]
    slot = _slot(msgs)
    slot.messages = list(msgs)
    slot._disk_window_len = 1
    slot._dirty = False  # this test targets the guards, not the pre-flush

    real_to_thread = st.asyncio.to_thread

    async def _rewind_midway(fn, *args):
        result = fn(*args)
        # The rewind lands while we are off the loop; the boundary does not move.
        slot._pending_rewrite = True
        return result

    st.asyncio.to_thread = _rewind_midway  # type: ignore[assignment]
    try:
        with pytest.raises(st.SnapshotUnstable):
            await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]


def test_bundle_reads_the_transcript_key_not_the_session_key():
    """Regression: an unbound channel slot's session key names a phantom file.

    ``surface_channel_session`` deliberately surfaces a channel-born slot
    UNBOUND when its channel key cannot be resolved, leaving
    ``linked_session_key`` empty. ``effective_session_key`` then falls back to
    ``dashboard:<stem>`` — a transcript no read path uses — so bundling from it
    would ship only the resident window and silently drop every older turn.
    ``chat_utils`` documents the split: transcript paths use
    ``slot_history_key``.
    """
    from kiro_crew.dashboard import session_transfer as st

    reads: list[str] = []

    class _Log:
        def read_messages_chained(self, key):
            reads.append(key)
            return [{"role": "user", "content": "older turn", "ts": ""}]

    slot = _slot([])
    slot.messages = []
    slot._disk_window_len = 0
    # Channel-born but unbound: the state the finding is about.
    slot.linked_session_key = ""
    slot.channel_origin = True
    slot.key = "slack_1700000000"

    bundle = st.build_transfer_bundle(SimpleNamespace(conversation_log=_Log()), slot)

    assert reads, "expected a transcript read"
    # The phantom dashboard-prefixed key must NOT be what we read.
    assert reads[0] == st.slot_history_key(slot)
    assert not reads[0].startswith("dashboard:")
    assert [m["content"] for m in bundle["messages"]] == ["older turn"]


@pytest.mark.asyncio
async def test_bundle_flushes_a_dirty_slot_so_in_place_edits_travel(monkeypatch):
    """Regression: an edit BELOW the boundary is invisible to the tail slice.

    A variant switch replaces an already-persisted assistant turn in place, so
    ``_disk_window_len`` does not move and ``messages[boundary:]`` is empty. If
    that edit's own save failed, disk holds the previous response and the copy
    would ship it. Flushing first closes the gap — and is safe because the save
    advances the boundary, unlike the ``_resumed_count`` slice this originally
    used.
    """
    from kiro_crew.dashboard import session_transfer as st

    edited = {"role": "assistant", "content": "the NEW variant", "ts": ""}
    disk = {"messages": [{"role": "assistant", "content": "the old variant", "ts": ""}]}

    class _Log:
        def read_messages_chained(self, _key):
            return list(disk["messages"])

    slot = _slot(disk["messages"])
    slot.messages = [edited]
    slot._disk_window_len = 1  # the edit sits BELOW the boundary
    slot._dirty = True

    async def _save(_state, s, best_effort=True):
        # A real save rewrites the window and re-stamps the boundary.
        disk["messages"] = list(s.messages)
        s._dirty = False
        s._disk_window_len = len(s.messages)

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    bundle = await st.build_transfer_bundle_async(
        SimpleNamespace(conversation_log=_Log()), slot, origin="mac"
    )

    assert [m["content"] for m in bundle["messages"]] == ["the NEW variant"]


@pytest.mark.asyncio
async def test_bundle_refuses_when_the_pre_bundle_flush_fails(monkeypatch):
    """An unpersistable source must fail the transfer, not ship a stale copy."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.messages = list(msgs)
    slot._disk_window_len = 1
    slot._dirty = True

    async def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(st, "save_slot_off_loop", _boom)
    with pytest.raises(st.SnapshotUnstable):
        await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")


@pytest.mark.asyncio
async def test_snapshot_retries_when_a_turn_lands_during_assembly():
    """Regression: the tail is captured BEFORE the await, so a turn appended
    during the threaded assembly is not in it — and it does not move the
    boundary, so the boundary check alone would return a bundle missing a turn
    that exists by the time we answer."""
    from kiro_crew.dashboard import session_transfer as st

    persisted = {"role": "user", "content": "persisted", "ts": ""}
    late = {"role": "assistant", "content": "late turn", "ts": ""}

    slot = _slot([persisted])
    slot.messages = [persisted]
    slot._disk_window_len = 1
    slot._dirty = False

    calls = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _append_midway(fn, *args):
        result = fn(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            # A turn lands while we are off the loop. Boundary does not move.
            slot.messages = slot.messages + [late]
        return result

    st.asyncio.to_thread = _append_midway  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(
            _state([persisted]), slot, origin="mac"
        )
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert calls["n"] >= 2, "expected a retry after the message count changed"
    assert [m["content"] for m in bundle["messages"]] == ["persisted", "late turn"]


@pytest.mark.asyncio
async def test_send_refuses_an_app_that_does_not_own_the_slot(monkeypatch):
    """An app token clears _guard() (it sets request["user"]), so without an
    ownership check an app declaring /api/instances could have ANOTHER slot's
    transcript copied to a peer — an exfiltration path out of the app sandbox.

    404, not 403: a slot owned by another app must be indistinguishable from one
    that does not exist (CWE-204), matching chat_fork.
    """
    from kiro_crew.dashboard import handlers_instances as hi

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    slot = _slot([{"role": "user", "content": "secret", "ts": ""}])
    slot.key = "slot-1"
    slot._app = "owner-app"

    sent: list = []

    class _Mgr:
        async def send_session_bundle(self, _id, bundle):
            sent.append(bundle)
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
    )
    request = SimpleNamespace(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        # A DIFFERENT app than the slot's owner.
        get=lambda k, default="": {"user": "owner", "app": "other-app"}.get(k, default),
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 404
    assert json.loads(resp.body)["code"] == "transfer_slot_not_found"
    assert sent == [], "nothing may be delivered for a slot the app does not own"


@pytest.mark.asyncio
async def test_snapshot_retries_on_an_in_place_edit_during_assembly():
    """Regression: an in-place edit moves neither the boundary nor the count.

    A variant switch replaces an already-persisted turn, so only ``_dirty_gen``
    (bumped centrally by the ``_dirty`` setter) reveals it. Without that marker
    the copy could carry the superseded response.
    """
    from kiro_crew.dashboard import session_transfer as st

    persisted = {"role": "assistant", "content": "old variant", "ts": ""}
    slot = _slot([persisted])
    slot.messages = [persisted]
    slot._disk_window_len = 1
    slot._dirty = False
    slot._dirty_gen = 7

    calls = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _edit_midway(fn, *args):
        result = fn(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            # Same length, same boundary — only the generation moves.
            slot.messages[0] = {"role": "assistant", "content": "new variant", "ts": ""}
            slot._dirty_gen += 1
        return result

    st.asyncio.to_thread = _edit_midway  # type: ignore[assignment]
    try:
        await st.build_transfer_bundle_async(_state([persisted]), slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert calls["n"] >= 2, "expected a retry after the dirty generation moved"


@pytest.mark.asyncio
async def test_import_broadcasts_the_rollback_so_no_phantom_slot_remains(monkeypatch):
    """``get_or_create_slot`` already told clients the session exists, so a
    silent pop on failure leaves a tab that resolves to nothing."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    pushes = {"n": 0}

    def _push():
        pushes["n"] += 1

    state.push_slots_update = _push

    async def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(st, "save_slot_off_loop", _boom)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 503
    assert state._slots == {}
    assert pushes["n"] >= 1, "the rollback must be broadcast, not silent"


@pytest.mark.asyncio
async def test_bundle_refuses_when_the_slot_never_settles(monkeypatch):
    """An edit landing inside the flush spends an attempt rather than being
    trusted. A slot that keeps changing exhausts the budget and is refused —
    the transfer never ships a transcript it could not pin down."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "assistant", "content": "old variant", "ts": ""}]
    slot = _slot(msgs)
    slot.messages = list(msgs)
    slot._disk_window_len = 0
    slot._dirty = True
    slot._dirty_gen = 3

    async def _save_then_edit(_state, s, best_effort=True):
        s._disk_window_len = len(s.messages)
        # Never settles: an edit lands inside every save, and a real in-place
        # edit leaves the slot dirty, so the next attempt flushes again.
        s._dirty = True
        s._dirty_gen += 1

    monkeypatch.setattr(st, "save_slot_off_loop", _save_then_edit)

    with pytest.raises(st.SnapshotUnstable):
        await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")


@pytest.mark.asyncio
async def test_retry_reflushes_so_it_cannot_serialize_a_superseded_variant(monkeypatch):
    """A retry exists because the slot changed, and that change is unpersisted.

    Re-reading disk without flushing again would serialize the superseded
    content — the exact staleness the flush exists to prevent.
    """
    from kiro_crew.dashboard import session_transfer as st

    disk: list = [{"role": "assistant", "content": "old variant", "ts": ""}]
    slot = _slot(disk)
    slot.messages = [{"role": "assistant", "content": "old variant", "ts": ""}]
    slot._disk_window_len = 1
    slot._dirty = False
    slot._dirty_gen = 1

    saves = {"n": 0}

    async def _save(_state, s, best_effort=True):
        saves["n"] += 1
        # Persist whatever is in memory now.
        disk[:] = [dict(m) for m in s.messages]
        s._disk_window_len = len(s.messages)
        s._dirty = False

    monkeypatch.setattr(st, "save_slot_off_loop", _save)

    reads = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _switch_variant_once(fn, *args):
        result = fn(*args)
        reads["n"] += 1
        if reads["n"] == 1:
            # A variant switch: in place, so neither boundary nor count moves.
            slot.messages[0] = {"role": "assistant", "content": "new variant", "ts": ""}
            # The real ``_dirty`` setter bumps the generation centrally; the
            # SimpleNamespace stub has no property, so do both explicitly.
            slot._dirty = True
            slot._dirty_gen += 1
        return result

    st.asyncio.to_thread = _switch_variant_once  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(
            _state(disk), slot, origin="mac"
        )
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert saves["n"] >= 1, "the retry must flush again before re-reading disk"
    contents = [m["content"] for m in bundle["messages"]]
    assert "old variant" not in contents, "serialized the superseded variant"
    assert contents == ["new variant"]


def test_local_instance_label_is_a_short_single_token():
    label = local_instance_label()
    assert label
    assert "." not in label


# ── bundle validation ────────────────────────────────────────────────────


def _valid(**over):
    body = {
        "bundle_version": BUNDLE_VERSION,
        "origin": "mac",
        "title": "t",
        "agent": "",
        "messages": [{"role": "user", "content": "hi", "ts": ""}],
    }
    body.update(over)
    return body


def test_validate_accepts_a_well_formed_bundle():
    bundle, err = _validate_bundle(_valid())
    assert err is None
    assert bundle["messages"] == [{"role": "user", "content": "hi", "ts": ""}]


@pytest.mark.parametrize(
    "body,code",
    [
        ("not a dict", "transfer_body_not_object"),
        (_valid(bundle_version=999), "transfer_version_unsupported"),
        (_valid(bundle_version=None), "transfer_version_unsupported"),
        (_valid(messages="nope"), "transfer_messages_not_array"),
        (_valid(messages=[]), "transfer_bundle_empty"),
        (_valid(messages=["nope"]), "transfer_message_not_object"),
        (_valid(messages=[{"role": "tool", "content": "x"}]), "transfer_message_bad_role"),
        (_valid(messages=[{"role": "user", "content": 5}]), "transfer_message_bad_content"),
        (_valid(title=5), "transfer_bad_title"),
        (_valid(origin=5), "transfer_bad_origin"),
        (_valid(agent=5), "transfer_bad_agent"),
    ],
)
def test_validate_rejects_with_a_machine_readable_code(body, code):
    bundle, err = _validate_bundle(body)
    assert err is not None, f"expected {code} to be rejected"
    assert bundle == {}
    payload = json.loads(err.body)
    assert payload["code"] == code
    assert payload["error"]


def test_validate_refuses_an_unknown_version_rather_than_guessing():
    """Both ends are independently-updated installs: a silently misread field
    would land as corrupted conversation, so refusal is the correct behaviour."""
    _, err = _validate_bundle(_valid(bundle_version=BUNDLE_VERSION + 1))
    assert err is not None
    assert err.status == 400


def test_validate_caps_message_count():
    many = [{"role": "user", "content": "x", "ts": ""} for _ in range(5_001)]
    _, err = _validate_bundle(_valid(messages=many))
    assert err is not None
    assert json.loads(err.body)["code"] == "transfer_too_many_messages"


def test_validate_caps_single_message_length():
    big = [{"role": "user", "content": "x" * 1_000_001, "ts": ""}]
    _, err = _validate_bundle(_valid(messages=big))
    assert err is not None
    assert json.loads(err.body)["code"] == "transfer_message_too_long"


def test_validate_caps_total_bundle_size():
    # 25 messages x 900k chars each trips the 20M total without tripping the
    # per-message cap.
    msgs = [{"role": "user", "content": "x" * 900_000, "ts": ""} for _ in range(25)]
    _, err = _validate_bundle(_valid(messages=msgs))
    assert err is not None
    assert json.loads(err.body)["code"] == "transfer_bundle_too_large"


def test_validate_truncates_an_overlong_title_instead_of_failing():
    bundle, err = _validate_bundle(_valid(title="t" * 5_000))
    assert err is None
    assert len(bundle["title"]) == 500


def test_validate_coerces_a_non_string_ts_to_empty():
    bundle, err = _validate_bundle(
        _valid(messages=[{"role": "user", "content": "hi", "ts": 12345}])
    )
    assert err is None
    assert bundle["messages"][0]["ts"] == ""


# ── tunnel-manager delivery hop ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_bundle_refuses_when_peer_not_connected():
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr.status = lambda _id: None  # type: ignore[method-assign]
    ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 1})

    assert ok is False
    assert payload["code"] == "transfer_peer_not_connected"


@pytest.mark.asyncio
async def test_send_bundle_refuses_when_no_credential_is_held():
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 1})

    assert ok is False
    assert payload["code"] == "transfer_no_credential"


@pytest.mark.asyncio
async def test_send_bundle_reports_an_unreachable_peer_without_leaking_the_bundle():
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    # A port nothing listens on: the POST fails at connect.
    mgr._tokens = {"peer": "irrelevant-credential"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=1
    )
    ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 1})

    assert ok is False
    assert payload["code"] == "transfer_unreachable"


# ── import endpoint ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_creates_a_new_slot_with_no_project(monkeypatch):
    """The headline decision: an imported session arrives unscoped.

    Driven through the handler with the slot machinery stubbed, so the assertion
    is about the handler's contract rather than DashboardState internals.
    """
    from kiro_crew.dashboard import session_transfer as st

    created = {}

    class _Slot:
        def __init__(self):
            self.key = "imported-1"
            self.title = ""
            self._titled = False
            self.agent = ""
            self.project = "SHOULD-BE-CLEARED"
            self.messages: list[dict] = []
            self._resumed_count = 0

        def append(self, role, content, _cls, ts="", broadcast=True):
            self.messages.append({"role": role, "content": content, "ts": ts})

        def drain(self):
            pass

    slot = _Slot()

    def _get_or_create(**kwargs):
        created.update(kwargs)
        # A freshly created slot has no project; the handler must not set one.
        slot.project = ""
        return slot

    state = SimpleNamespace(
        _slots={},
        _slots_under_construction=set(),
        get_or_create_slot=_get_or_create,
        push_slots_update=lambda: None,
    )
    state.live_slot_count = lambda: len(state._slots) + len(state._slots_under_construction)
    state.begin_slot_construction = state._slots_under_construction.add
    state.end_slot_construction = state._slots_under_construction.discard

    async def _save(*_a, **_k):
        return None

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    monkeypatch.setattr(st, "_sync_dashboard_slots", lambda _s: None)

    request = _make_request(
        state,
        _valid(
            title="Design chat",
            origin="macbook",
            messages=[
                {"role": "user", "content": "what about the tunnel?", "ts": ""},
                {"role": "assistant", "content": "it forwards loopback", "ts": ""},
            ],
        ),
    )
    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert payload["key"] == "imported-1"
    assert payload["messages"] == 2
    # Copy semantics: a brand-new key, and no project inherited.
    assert slot.project == ""
    assert "project" not in created
    # Provenance is visible in the title so a transferred tab is never mistaken
    # for a locally-born one.
    assert slot.title == "⇄ Design chat (from macbook)"
    assert [m["content"] for m in slot.messages] == [
        "what about the tunnel?",
        "it forwards loopback",
    ]


@pytest.mark.asyncio
async def test_import_response_never_carries_a_credential(monkeypatch):
    """instances.md §6: connect + refresh-token are the ONLY token-crossing
    routes. A transfer response must not become a third."""
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid())
    body = json.loads(resp.body)

    assert set(body) == {"ok", "key", "title", "messages", "resume_mode"}
    assert "token" not in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_import_rejects_an_unknown_version_over_http(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid(bundle_version=42))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_version_unsupported"


@pytest.mark.asyncio
async def test_import_rejects_invalid_json(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    request = _make_request(state, None, raw="{not json")
    resp = await st.api_chat_slot_import(request)

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_invalid_json"


@pytest.mark.asyncio
async def test_import_refuses_past_the_slot_cap(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    state._slots = {f"s{i}": object() for i in range(500)}
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"


@pytest.mark.asyncio
async def test_import_redacts_assistant_content_but_not_the_users_own_words(monkeypatch):
    """Matches the fork path: inbound assistant text is redacted, the human's
    own turn is left verbatim so their words are never corrupted."""
    from kiro_crew.dashboard import session_transfer as st

    secret = "AKIAIOSFODNN7EXAMPLE"
    resp_slot = await _run_import(
        st,
        monkeypatch,
        _valid(
            messages=[
                {"role": "user", "content": f"my key is {secret}", "ts": ""},
                {"role": "assistant", "content": f"noted {secret}", "ts": ""},
            ]
        ),
        return_slot=True,
    )
    user_msg, assistant_msg = resp_slot.messages

    assert secret in user_msg["content"]
    assert secret not in assistant_msg["content"]


@pytest.mark.asyncio
async def test_import_drops_an_agent_the_target_does_not_have(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_resolve_agent", lambda _n: "")
    created = {}
    await _run_import(st, monkeypatch, _valid(agent="agent-only-on-the-source"), created=created)

    assert created.get("agent") == ""


@pytest.mark.asyncio
async def test_import_yields_to_the_event_loop_on_a_large_bundle(monkeypatch):
    """A big bundle must not hold the loop in one un-yielded pass.

    Redaction is regex-heavy and the content is peer-supplied, so an un-yielded
    import starves the loop heartbeat until LoopStallWatchdog _exit()s the
    gateway — the failure chat_persistence.restore_open_slots_async documents on
    the same read-and-redact work.

    Shrinks the yield BUDGET rather than inflating the payload. An earlier version
    pushed 2 MB through the real redactors: fine locally, but it blew the 120s
    per-test timeout under 3.12's coverage instrumentation in CI. Budget-scaling
    exercises the same branch in kilobytes, with no dependence on how fast
    redaction happens to be on the runner.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_YIELD_AFTER_CHARS", 1_000)

    yields = 0
    real_sleep = asyncio.sleep

    async def _counting_sleep(delay, *a, **k):
        nonlocal yields
        if delay == 0:
            yields += 1
        return await real_sleep(delay, *a, **k)

    monkeypatch.setattr(st.asyncio, "sleep", _counting_sleep)

    # 20 turns x 500 chars = 10 KB against the 1 KB budget → trips every 2nd turn.
    # Asserting a lower bound (not an exact count) keeps this robust to a future
    # budget tweak while still proving the loop yields repeatedly.
    big = [{"role": "assistant", "content": "x" * 500, "ts": ""} for _ in range(20)]
    await _run_import(st, monkeypatch, _valid(messages=big))

    assert yields >= 5, f"expected repeated yields once the budget is exceeded, got {yields}"


@pytest.mark.asyncio
async def test_import_does_not_yield_for_a_small_bundle(monkeypatch):
    """The yield is budgeted, not per-message — a normal session pays nothing."""
    from kiro_crew.dashboard import session_transfer as st

    yields = 0
    real_sleep = asyncio.sleep

    async def _counting_sleep(delay, *a, **k):
        nonlocal yields
        if delay == 0:
            yields += 1
        return await real_sleep(delay, *a, **k)

    monkeypatch.setattr(st.asyncio, "sleep", _counting_sleep)
    await _run_import(st, monkeypatch, _valid())

    assert yields == 0


# ── Layer B (kiro-cli context) ──────────────────────────────────────────


def _sessions(sid):
    """A stand-in for the live SessionManager's resume-lookup surface.

    Used with ``_resolve_layer_b_sid``, which runs ON THE LOOP -- ``resumable_sid``
    self-prunes the session map, so it must never be reached from a worker thread.
    """
    return SimpleNamespace(resumable_sid=lambda _k: sid)


def test_layer_b_bundle_carries_the_context_when_the_session_has_one(monkeypatch, tmp_path):
    """Layer B is what makes an imported session RESUME instead of replaying a
    lossy transcript prefix, so it must ride along when the session has one."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "11111111-2222-3333-4444-555555555555"
    (tmp_path / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "cwd": "/Users/src/proj"}), encoding="utf-8"
    )
    (tmp_path / f"{sid}.jsonl").write_text('{"kind":"Prompt"}\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    got = st._read_layer_b(sid)

    assert got is not None
    assert got["sid"] == sid
    assert got["envelope"]["session_id"] == sid
    assert "Prompt" in got["events"]


def test_layer_b_is_absent_when_the_session_has_no_kiro_context(monkeypatch, tmp_path):
    """A brand-new slot (or a pruned map entry) has no Layer B; the transfer must
    degrade to transcript-only rather than fail."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._read_layer_b("") is None


def test_layer_b_absent_when_the_files_were_pruned(monkeypatch, tmp_path):
    """A map entry can outlive its files; a missing pair is not an error."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._read_layer_b("no-such-sid") is None


def test_events_jsonl_loadable_accepts_valid_and_rejects_truncated():
    """Structural check only -- it must never rewrite what it inspects."""
    from kiro_crew.dashboard import session_transfer as st

    blob = (
        json.dumps({"kind": "Prompt", "data": {"content": "hi", "n": 1}})
        + "\n"
        + json.dumps({"kind": "AssistantMessage", "data": {"text": "ok"}})
        + "\n"
    )

    assert st._events_jsonl_is_loadable(blob) is True
    assert st._events_jsonl_is_loadable("not json at all\n") is False
    # One bad record poisons the blob even when others are fine.
    assert st._events_jsonl_is_loadable(json.dumps({"kind": "Prompt"}) + "\ntruncated {\n") is False
    # Empty and blank-only are structurally fine.
    assert st._events_jsonl_is_loadable("") is True
    assert st._events_jsonl_is_loadable("\n\n") is True


@pytest.mark.asyncio
async def test_unparseable_layer_b_degrades_to_transcript_only(monkeypatch, tmp_path):
    """End-to-end on the send side: a crash-truncated source blob must produce a
    bundle with no Layer B rather than one the peer cannot load."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "eeeeeeee-1111-2222-3333-555555555555"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text('{"kind":"Prompt"}\ntruncated {\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._read_layer_b(sid) is None


@pytest.mark.asyncio
async def test_import_refuses_unparseable_layer_b_from_the_peer(monkeypatch, tmp_path):
    """The sender is not trusted: an unparseable blob must not be installed."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._write_layer_b_files({"envelope": {}, "events": "truncated {\n"}, "") is None
    assert not list(tmp_path.glob("*.json")), "nothing may be written for a refused blob"


def test_events_jsonl_handles_empty_and_blank():
    from kiro_crew.dashboard import session_transfer as st

    assert st._events_jsonl_is_loadable("") is True
    assert st._events_jsonl_is_loadable("\n\n") is True


# The shape of a REAL kiro-cli thinking block, which earlier revisions of this
# feature corrupted. ``signature`` is a cryptographic signature over the thinking
# content; the provider validates it when the conversation is replayed, so any
# rewrite of a covered byte makes the peer's NEXT turn fail -- long after the
# import reported success. Every Layer B test below uses this shape rather than an
# empty ``{}`` envelope, because an empty envelope cannot catch that class of bug.
_THINKING_ENVELOPE = {
    "session_id": "src-sid",
    "cwd": "/Users/someone/work/project",
    "session_state": {
        "version": "v1",
        "agent_name": "sender-agent",
        "permissions": {"filesystem": {"allowed_read_paths": ["/Users/someone/work"]}},
        "conversation_metadata": {
            "user_turn_metadatas": [
                {
                    "result": {
                        "Ok": {
                            "content": [
                                {
                                    "kind": "thinking",
                                    "data": {
                                        "modelId": "some-model",
                                        "text": "let me think about the plan",
                                        "redactedContent": None,
                                        # base64-shaped, like the real signature
                                        "signature": "Ci8KCEFTU0lTVEFOVBIhCgt0aGlua2luZ19zaWc",
                                    },
                                }
                            ]
                        }
                    }
                }
            ]
        },
    },
}


def test_layer_b_leaves_the_conversation_byte_exact_on_egress(monkeypatch, tmp_path):
    """Layer B ships verbatim. Redacting it and transplanting it cannot both hold:
    the thinking-block signature covers the content, so a scrub invalidates the
    conversation the transfer exists to carry."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    events = json.dumps({"kind": "AssistantMessage", "data": {"text": "ok"}}) + "\n"
    (tmp_path / f"{sid}.json").write_text(json.dumps(_THINKING_ENVELOPE), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text(events, encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    got = st._read_layer_b(sid)

    assert got is not None
    assert got["events"] == events, "the events blob was rewritten"
    assert got["envelope"] == _THINKING_ENVELOPE, "the envelope was rewritten"


@pytest.mark.asyncio
async def test_layer_b_is_skipped_while_a_turn_is_in_flight(monkeypatch):
    """Layer A records the prompt on submit; kiro-cli writes Layer B only when the
    turn persists. A mid-turn bundle would therefore pair a transcript that SHOWS
    the prompt with a context that lacks it, and the peer would resume the model
    behind its own visible transcript. Degrade to transcript-only instead."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.running = True
    resolved: list[str] = []
    monkeypatch.setattr(
        st, "_resolve_layer_b_sid", lambda *a: (resolved.append("called"), "sid")[1]
    )

    bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert "layer_b" not in bundle
    assert bundle["bundle_version"] == 2
    assert resolved == [], "the sid must not even be resolved mid-turn"


@pytest.mark.asyncio
async def test_layer_b_is_skipped_between_stages_of_a_staged_plan(monkeypatch):
    """``running`` reads False between stages, so the staged-plan flag is checked
    too (chat_handlers documents that gap)."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.running = False
    slot._in_stage_execution = True
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *a: "sid")

    bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert "layer_b" not in bundle


@pytest.mark.asyncio
async def test_layer_b_travels_when_the_slot_is_idle(monkeypatch, tmp_path):
    """The positive case: an idle slot still carries its context."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "cccccccc-1111-2222-3333-444444444444"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *a: sid)

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.running = False

    bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert bundle["layer_b"]["envelope"]["session_id"] == sid


@pytest.mark.asyncio
async def test_layer_b_eligibility_is_recomputed_on_snapshot_retry(monkeypatch):
    """Regression: a prompt starting DURING the threaded read forces a retry, and
    that retry re-reads Layer A with the new prompt. If eligibility were computed
    once up front, the retry would ship the new prompt alongside the pre-turn
    Layer B — the exact skew the mid-turn check exists to prevent, reintroduced
    through the retry path."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "persisted", "ts": ""}]
    slot = _slot(msgs)
    slot.running = False
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *a: "the-sid")

    calls = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _turn_starts_midway(fn, *args):
        result = fn(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            # A prompt lands while we are off the loop: Layer A grows AND the slot
            # goes busy. Both must be seen by the retry.
            slot.messages = slot.messages + [{"role": "user", "content": "new prompt", "ts": ""}]
            slot.running = True
        return result

    st.asyncio.to_thread = _turn_starts_midway  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert calls["n"] >= 2, "expected a retry once the slot changed"
    # The retry saw the running slot, so Layer B must be dropped even though the
    # first attempt was eligible.
    assert "layer_b" not in bundle
    assert [m["content"] for m in bundle["messages"]] == ["persisted", "new prompt"]


@pytest.mark.asyncio
async def test_imported_tab_is_marked_when_it_arrived_transcript_only(monkeypatch):
    """The sender's row is ephemeral (component state in a menu that closes), but
    the consequence is felt later on the RECEIVING machine. The tab title is the
    surface that persists and sits where the loss will be discovered."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    slot = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), return_slot=True
    )

    assert slot.title.endswith("— transcript only"), slot.title


@pytest.mark.asyncio
async def test_imported_tab_is_not_marked_when_layer_b_landed(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    slot = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), return_slot=True
    )

    assert "transcript only" not in slot.title


@pytest.mark.asyncio
async def test_a_v1_import_is_not_marked_transcript_only(monkeypatch):
    """A v1 bundle carries no context by construction — the copy is exactly what
    was sent, so flagging it would cry wolf."""
    from kiro_crew.dashboard import session_transfer as st

    slot = await _run_import(st, monkeypatch, _valid(bundle_version=1), return_slot=True)

    assert "transcript only" not in slot.title


def test_read_layer_b_takes_a_sid_not_the_live_manager():
    """Regression guard: ``resumable_sid`` -> ``SessionMap.get`` SELF-PRUNES, so it
    is a map mutation and must stay on the event loop (same contract as the
    join). The threaded reader therefore takes an immutable sid and has no way to
    reach the map."""
    import inspect

    from kiro_crew.dashboard import session_transfer as st

    params = inspect.signature(st._read_layer_b).parameters
    assert list(params) == ["sid"]
    assert "sessions" not in params
    # ...and the assemble step the thread runs carries only the sid too.
    assert "sessions" not in inspect.signature(st._read_and_assemble).parameters


def test_resolve_layer_b_sid_is_the_loop_side_lookup():
    from kiro_crew.dashboard import session_transfer as st

    assert st._resolve_layer_b_sid(_sessions("the-sid"), "dashboard:slot-1") == "the-sid"
    assert st._resolve_layer_b_sid(_sessions(None), "dashboard:slot-1") == ""
    assert st._resolve_layer_b_sid(None, "dashboard:slot-1") == ""


def test_resolve_layer_b_sid_swallows_lookup_failure():
    from kiro_crew.dashboard import session_transfer as st

    def _boom(_k):
        raise RuntimeError("map unreadable")

    assert st._resolve_layer_b_sid(SimpleNamespace(resumable_sid=_boom), "k") == ""


def test_layer_b_envelope_rewrite_neutralises_the_source_host():
    """The conversation state (what makes resume work) is kept verbatim; only the
    fields that reference the SOURCE host are rewritten."""
    from kiro_crew.dashboard import session_transfer as st

    env = {
        "session_id": "old-sid",
        "cwd": "/Volumes/workplace/only-on-my-mac",
        "title": "leaky title",
        "session_state": {
            "conversation_metadata": {"user_turn_metadatas": [{"keep": "me"}]},
            "agent_name": "source-agent",
            "permissions": {
                "filesystem": {
                    "allowed_read_paths": ["/Volumes/workplace/only-on-my-mac"],
                    "allowed_write_paths": ["/Volumes/workplace/only-on-my-mac"],
                }
            },
        },
    }

    out = st._rewrite_layer_b_envelope(env, "fresh-sid", "target-agent")

    # Fresh identity keeps copy-never-move: a repeat send cannot collide.
    assert out["session_id"] == "fresh-sid"
    # Unscoped on arrival, matching the deliberate decision to drop `project`.
    assert out["cwd"] == ""
    assert out["session_state"]["permissions"]["filesystem"]["allowed_read_paths"] == []
    assert out["session_state"]["permissions"]["filesystem"]["allowed_write_paths"] == []
    assert out["session_state"]["agent_name"] == "target-agent"
    assert out["title"] is None
    # The resumable context itself survives untouched — that is the whole point.
    assert out["session_state"]["conversation_metadata"] == {
        "user_turn_metadatas": [{"keep": "me"}]
    }
    # The source path must not survive anywhere in the envelope.
    assert "only-on-my-mac" not in json.dumps(out)
    # The caller's dict is not mutated.
    assert env["session_id"] == "old-sid"


def test_layer_b_rewrite_tolerates_a_minimal_envelope():
    """A peer on a different kiro-cli build may omit whole subtrees."""
    from kiro_crew.dashboard import session_transfer as st

    out = st._rewrite_layer_b_envelope({}, "fresh-sid", "")

    assert out["session_id"] == "fresh-sid"
    assert out["session_state"]["agent_name"] is None


def test_write_layer_b_files_writes_a_fresh_sid_and_never_touches_the_map(
    monkeypatch, tmp_path
):
    """File writes run in a worker thread, so they must NOT touch the session
    map: ``SessionMap.set`` mutates a shared dict and serialises the whole file,
    which races the event loop's own map writes."""
    import inspect

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "peer-sid", "cwd": "/peer/path"}, "events": '{"k":1}\n'},
        "my-agent",
    )

    # Fresh sid, NOT the peer's — copy semantics.
    assert new_sid and new_sid != "peer-sid"
    written = json.loads((tmp_path / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert written["session_id"] == new_sid
    assert written["cwd"] == ""
    assert written["session_state"]["agent_name"] == "my-agent"
    # Events are re-serialised per record by the redactor, so compare PARSED
    # content, not bytes: JSON spacing is normalised (`{"k":1}` -> `{"k": 1}`),
    # which is semantically identical for a consumer that parses it.
    written_events = (tmp_path / f"{new_sid}.jsonl").read_text(encoding="utf-8")
    assert [json.loads(ln) for ln in written_events.split("\n") if ln.strip()] == [{"k": 1}]
    # No map access from the thread half — asserted structurally: the function
    # takes no ``sessions`` handle, so it cannot reach the live map at all.
    # (A source-text scan is wrong here: the docstring names the hazard on
    # purpose.)
    assert "sessions" not in inspect.signature(st._write_layer_b_files).parameters


def test_write_layer_b_files_returns_none_on_failure(monkeypatch, tmp_path):
    from kiro_crew.dashboard import session_transfer as st

    def _boom():
        raise OSError("no dir")

    monkeypatch.setattr(st, "kiro_sessions_dir", _boom)

    assert st._write_layer_b_files({"envelope": {}, "events": ""}, "") is None


def test_join_layer_b_goes_through_the_live_manager():
    """Regression guard for the review finding: constructing ``SessionMap()``
    writes a correct file that the live map then clobbers, because its ``_data``
    is a startup snapshot and every ``set`` rewrites the whole file.

    Asserted by absence of the IMPORT — if the name is not bound in the module it
    cannot be constructed, and the body's own comments legitimately mention the
    hazard they prevent.
    """
    from kiro_crew.dashboard import session_transfer as st

    assert not hasattr(st, "SessionMap"), "session_transfer must not import SessionMap"
    recorded: dict = {}
    live = SimpleNamespace(
        seed_conversation=lambda key, sid, *, provider="", cwd="": recorded.update(
            {"key": key, "sid": sid, "provider": provider}
        )
    )

    assert st._join_layer_b(live, "dashboard:imported-1", "new-sid") is True
    assert recorded == {"key": "dashboard:imported-1", "sid": "new-sid", "provider": "acp"}


def test_join_layer_b_without_a_live_manager_is_a_clean_no():
    from kiro_crew.dashboard import session_transfer as st

    assert st._join_layer_b(None, "k", "sid") is False


def test_join_layer_b_is_best_effort():
    """A join failure must NOT fail an already-persisted import — the session
    still opens as the transcript-only copy."""
    from kiro_crew.dashboard import session_transfer as st

    def _boom(*_a, **_k):
        raise OSError("map write failed")

    assert st._join_layer_b(SimpleNamespace(seed_conversation=_boom), "k", "sid") is False


def test_bundle_includes_layer_b_when_present():
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    bundle = st._assemble_bundle(
        msgs, "t", "", "mac", {"sid": "s", "envelope": {"session_id": "s"}, "events": "e"}
    )

    assert bundle["bundle_version"] == 2
    assert bundle["layer_b"]["envelope"]["session_id"] == "s"
    # The sid is NOT sent: the importer allocates its own.
    assert "sid" not in bundle["layer_b"]


def test_bundle_omits_layer_b_when_the_session_has_none():
    from kiro_crew.dashboard import session_transfer as st

    bundle = st._assemble_bundle([{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", None)

    assert "layer_b" not in bundle


def test_validate_accepts_a_v1_bundle_without_layer_b():
    """A v1 sender (transcript-only) must still be able to send us a copy."""
    bundle, err = _validate_bundle(_valid(bundle_version=1))

    assert err is None
    assert "layer_b" not in bundle


def test_validate_accepts_a_v2_bundle_with_layer_b():
    bundle, err = _validate_bundle(
        _valid(layer_b={"envelope": {"session_id": "s"}, "events": '{"k":1}\n'})
    )

    assert err is None
    assert bundle["layer_b"]["events"] == '{"k":1}\n'


@pytest.mark.parametrize(
    "layer_b,code",
    [
        ("not a dict", "transfer_layer_b_not_object"),
        ({"envelope": "nope", "events": ""}, "transfer_layer_b_bad_envelope"),
        ({"envelope": {}, "events": 5}, "transfer_layer_b_bad_events"),
        ({"envelope": {}, "events": "x" * 40_000_001}, "transfer_layer_b_too_large"),
    ],
)
def test_validate_rejects_a_malformed_layer_b(layer_b, code):
    """Layer B is untrusted peer input and is bounded BEFORE anything is written."""
    _, err = _validate_bundle(_valid(layer_b=layer_b))

    assert err is not None
    assert json.loads(err.body)["code"] == code


@pytest.mark.asyncio
async def test_import_materialises_layer_b_so_the_session_resumes(monkeypatch):
    """End-to-end on the receiving side: a v2 bundle must land a joined Layer B."""
    from kiro_crew.dashboard import session_transfer as st

    calls: dict = {}

    def _fake(layer_b, agent):
        calls.update({"layer_b": layer_b, "agent": agent})
        return "new-sid"

    monkeypatch.setattr(st, "_write_layer_b_files", _fake)
    monkeypatch.setattr(
        st, "_join_layer_b", lambda _s, k, _sid: calls.update({"sm_key": k}) or True
    )
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {"session_id": "s"}, "events": "e"})
    )

    assert resp.status == 200
    assert calls["layer_b"]["events"] == "e"
    # Joined under the SESSION key (what turns run on), which is what the resume
    # path reads — not the raw slot key.
    assert calls["sm_key"]


@pytest.mark.asyncio
async def test_import_still_succeeds_when_layer_b_cannot_be_materialised(monkeypatch):
    """Best-effort: the transcript copy already persisted, so a Layer B failure
    must not turn a landed import into an error."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"})
    )

    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True


@pytest.mark.asyncio
async def test_import_of_a_v1_bundle_does_not_touch_layer_b(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    called = {"n": 0}

    def _count(*_a, **_k):
        called["n"] += 1
        return True

    monkeypatch.setattr(st, "_write_layer_b_files", _count)
    resp = await _run_import(st, monkeypatch, _valid(bundle_version=1))

    assert resp.status == 200
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_import_reports_session_load_when_layer_b_landed(monkeypatch):
    """The sender must be able to tell a full copy from a degraded one — without
    this the feature's own failure mode (a silently lossy copy) shows the same
    green "Sent" as a successful resume."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"})
    )

    assert json.loads(resp.body)["resume_mode"] == "session_load"


@pytest.mark.asyncio
async def test_import_reports_prefix_when_layer_b_failed(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"})
    )

    assert json.loads(resp.body)["resume_mode"] == "prefix"


@pytest.mark.asyncio
async def test_import_reports_prefix_for_a_v1_bundle(monkeypatch):
    """A v1 bundle carries no context by construction, so "prefix" is the honest
    answer rather than an error."""
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid(bundle_version=1))

    assert json.loads(resp.body)["resume_mode"] == "prefix"


@pytest.mark.asyncio
async def test_send_bundle_downgrades_to_v1_when_the_peer_refuses_v2():
    """Gaining Layer B must not REMOVE the ability to send to a not-yet-upgraded
    peer: an older peer refuses bundle_version 2 outright, so retry once with the
    transcript-only v1 shape it has always accepted."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {"peer": "tok"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )

    seen: list[dict] = []

    class _Resp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, json=None, headers=None):
            seen.append(dict(json))
            if json.get("bundle_version") == 2:
                return _Resp(400, {"code": "transfer_version_unsupported"})
            return _Resp(200, {"key": "remote-1", "resume_mode": "prefix"})

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle(
            "peer",
            {"bundle_version": 2, "messages": [], "layer_b": {"envelope": {}, "events": "e"}},
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert len(seen) == 2
    # The retry drops Layer B and re-tags as v1 — same conversation, v1 fidelity.
    assert seen[0]["bundle_version"] == 2 and "layer_b" in seen[0]
    assert seen[1]["bundle_version"] == 1 and "layer_b" not in seen[1]


@pytest.mark.asyncio
async def test_send_bundle_downgrades_only_once():
    """A peer that refuses BOTH versions must surface an error, not spin."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {"peer": "tok"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    posts = {"n": 0}

    class _Resp:
        status = 400

        async def json(self):
            return {"code": "transfer_version_unsupported"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, json=None, headers=None):
            posts["n"] += 1
            return _Resp()

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle(
            "peer", {"bundle_version": 2, "layer_b": {"envelope": {}, "events": "e"}}
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_version_unsupported"
    assert posts["n"] == 2, "one downgrade retry, then stop"


@pytest.mark.asyncio
async def test_layer_b_lands_before_the_transcript_is_persisted(monkeypatch):
    """Ordering guard: the slot is registered in ``state._slots`` synchronously
    and handlers enumerate that dict directly, so it is GET-reachable before the
    awaits finish. A prompt landing while the join is missing cold-starts a FRESH
    context and the later join binds to nothing — the silent context loss this
    feature exists to prevent. So the join must be written FIRST.
    """
    from kiro_crew.dashboard import session_transfer as st

    order: list[str] = []

    def _mat(*_a, **_k):
        order.append("layer_b")
        return "new-sid"

    async def _save(*_a, **_k):
        order.append("save")

    monkeypatch.setattr(st, "_write_layer_b_files", _mat)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), save=_save
    )

    assert resp.status == 200
    assert order == ["layer_b", "save"], order


@pytest.mark.asyncio
async def test_failed_save_rolls_back_the_layer_b_join(monkeypatch):
    """Because the join now precedes the save, a rollback has to undo it — else
    the map keeps an entry for a tab that no longer exists and the
    ``<sid>.{json,jsonl}`` pair lingers until a prune sweeps it."""
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(
        st, "_forget_layer_b_join", lambda _s, key: (forgotten.append(key), "")[1]
    )

    async def _boom(*_a, **_k):
        raise OSError("disk full")

    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), save=_boom
    )

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "transfer_import_save_failed"
    assert forgotten, "the join must be dropped when the import is refused"


def test_unlink_layer_b_files_removes_the_pair(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    sid = "dddddddd-eeee-ffff-0000-111111111111"
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    (tmp_path / f"{sid}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text("", encoding="utf-8")

    st._unlink_layer_b_files(sid)

    assert not (tmp_path / f"{sid}.json").exists()
    assert not (tmp_path / f"{sid}.jsonl").exists()


def test_forget_layer_b_join_returns_the_sid_for_file_cleanup():
    from kiro_crew.dashboard import session_transfer as st

    dropped: list[str] = []
    live = SimpleNamespace(
        forget_conversation=lambda key: (dropped.append(key), "old-sid")[1]
    )

    assert st._forget_layer_b_join(live, "dashboard:imported-1") == "old-sid"
    assert dropped == ["dashboard:imported-1"]


def test_forget_layer_b_join_is_silent_without_a_live_manager():
    from kiro_crew.dashboard import session_transfer as st

    assert st._forget_layer_b_join(None, "k") == ""  # must not raise


@pytest.mark.asyncio
async def test_slot_is_unreachable_until_construction_finishes(monkeypatch):
    """``get_or_create_slot`` registers the slot in ``state._slots`` AND calls
    ``push_slots_update()`` before returning, so without retracting it the tab is
    visible and GET-reachable while its transcript is empty and its Layer B
    unjoined — a prompt then cold-starts a fresh context the later join can never
    attach to. The slot must be absent from ``_slots`` for the whole build and
    present exactly once at the end.
    """
    from kiro_crew.dashboard import session_transfer as st

    seen: list[bool] = []
    state = _stub_state(st, monkeypatch)
    slot = state._imported_slot

    def _probe(*_a, **_k):
        # Sampled from inside the build, standing in for a concurrent GET.
        seen.append(slot.key in state._slots)
        return "new-sid"

    monkeypatch.setattr(st, "_write_layer_b_files", _probe)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)

    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 200
    assert seen == [False], "the slot must be unreachable while it is being built"
    # ...and reachable once, after everything landed.
    assert state._slots.get(slot.key) is slot


@pytest.mark.asyncio
async def test_send_bundle_downgrades_a_v2_bundle_that_has_no_layer_b():
    """A session with no kiro-cli context ships v2 with NO ``layer_b`` key. Gating
    the downgrade on Layer B presence would skip exactly those transfers and fail
    them against a v1 peer, so the gate is the VERSION."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {"peer": "tok"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    seen: list[dict] = []

    class _Resp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, json=None, headers=None):
            seen.append(dict(json))
            if json.get("bundle_version") == 2:
                return _Resp(400, {"code": "transfer_version_unsupported"})
            return _Resp(200, {"key": "remote-1"})

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        # No "layer_b" key at all — the context-free case.
        ok, payload = await mgr.send_session_bundle(
            "peer", {"bundle_version": 2, "messages": [{"role": "user", "content": "hi"}]}
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert len(seen) == 2, "a context-free v2 bundle must still downgrade"
    assert seen[1]["bundle_version"] == 1


# ── helpers ──────────────────────────────────────────────────────────────


def _make_request(state, body, *, raw: str | None = None):
    """A minimal aiohttp-request stand-in for the import handler."""

    async def _json():
        if raw is not None:
            return json.loads(raw)
        return body

    return SimpleNamespace(
        app={"state": state},
        get=lambda _k, default="": default,
        json=_json,
    )


def _stub_state(st, monkeypatch, save=None):
    class _Slot:
        def __init__(self):
            self.key = "imported-1"
            self.title = ""
            self._titled = False
            self.agent = ""
            self.project = ""
            self.messages: list[dict] = []
            self._resumed_count = 0

        def append(self, role, content, _cls, ts="", broadcast=True):
            self.messages.append({"role": role, "content": content, "ts": ts})

        def drain(self):
            pass

    slot = _Slot()

    async def _save(*_a, **_k):
        return None

    monkeypatch.setattr(st, "save_slot_off_loop", save or _save)
    monkeypatch.setattr(st, "_sync_dashboard_slots", lambda _s: None)
    state = SimpleNamespace(
        _slots={},
        # Mirrors DashboardState's construction accounting: a slot retracted
        # while it is built is absent from _slots but still counts against every
        # cap, so the stub has to model both halves or the handler's cap check
        # and its release would not be exercised at all.
        _slots_under_construction=set(),
        get_or_create_slot=lambda **_k: slot,
        push_slots_update=lambda: None,
        # The live SessionManager surface the Layer B path threads through.
        sessions=SimpleNamespace(
            seed_conversation=lambda *a, **k: None,
            forget_conversation=lambda _k: "",
            resumable_sid=lambda _k: None,
        ),
    )
    state.live_slot_count = lambda: len(state._slots) + len(state._slots_under_construction)
    state.begin_slot_construction = state._slots_under_construction.add
    state.end_slot_construction = state._slots_under_construction.discard
    state._imported_slot = slot
    return state


async def _run_import(st, monkeypatch, body, *, return_slot=False, created=None, save=None):
    state = _stub_state(st, monkeypatch, save=save)
    if created is not None:
        slot = state._imported_slot

        def _get_or_create(**kwargs):
            created.update(kwargs)
            return slot

        state.get_or_create_slot = _get_or_create
    resp = await st.api_chat_slot_import(_make_request(state, body))
    assert isinstance(resp, web.Response)
    if return_slot:
        return state._imported_slot
    return resp


def _async_value(value):
    """Return a zero-arg coroutine function yielding *value* (stub for request.json)."""

    async def _inner():
        return value

    return _inner


# ── Layer B resource + permission bounds ─────────────────────────────────


def test_layer_b_cap_is_checked_before_the_file_is_read(monkeypatch, tmp_path):
    """The cap must bound the ALLOCATION, not merely the result.

    A post-read ``len()`` check also returns ``None`` for an oversized log, so
    "returns None" proves nothing on its own -- by then the multi-gigabyte blob
    is already resident and the gateway has already OOMed. The only observable
    difference is that the bytes are never read, which is what this pins.
    """
    from pathlib import Path as _Path

    from kiro_crew.dashboard import session_transfer as st

    sid = "oversized"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text("x" * 500, encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "_MAX_LAYER_B_CHARS", 100)

    reads: list[str] = []
    real_read_text = _Path.read_text

    def _spy(self, *a, **k):
        reads.append(self.name)
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(_Path, "read_text", _spy)

    assert st._read_layer_b(sid) is None
    assert f"{sid}.jsonl" not in reads, "the oversized log was read despite the cap"


def test_layer_b_cap_also_covers_the_envelope_read(monkeypatch, tmp_path):
    """``.json`` is read on the same path and was unbounded too."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "big-envelope"
    (tmp_path / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pad": "x" * 500}), encoding="utf-8"
    )
    (tmp_path / f"{sid}.jsonl").write_text('{"kind":"Prompt"}\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "_MAX_LAYER_B_CHARS", 100)

    assert st._read_layer_b(sid) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses ACLs")
def test_imported_layer_b_is_owner_only(monkeypatch, tmp_path):
    """Layer B is the model's whole context window -- every user turn and tool
    result. Default umask 022 would publish the imported copy at 0644 for any
    other local user to read.
    """
    from kiro_crew.dashboard import session_transfer as st

    d = tmp_path / "cli"  # absent, so this call is the one that creates it
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: d)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    for suffix in (".json", ".jsonl"):
        mode = (d / f"{new_sid}{suffix}").stat().st_mode & 0o777
        assert mode == 0o600, f"{suffix} landed at {oct(mode)}, not owner-only"
    assert d.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses ACLs")
def test_import_does_not_repermission_kiro_clis_existing_dir(monkeypatch, tmp_path):
    """Hardening is scoped to the directory this code creates.

    This is kiro-cli's own sessions dir; silently tightening a pre-existing one
    would mutate posture on a directory the feature does not own. The FILES are
    owner-only either way, which is what actually contains the context.
    """
    from kiro_crew.dashboard import session_transfer as st

    d = tmp_path / "cli"
    d.mkdir()
    # A deliberately GROUP/OTHER-READABLE fixture: the whole point is to hand the
    # code a directory laxer than what it would choose, so "did not re-permission"
    # is observable. Asserting on an already-0700 tmp dir would pass even if the
    # code did chmod it. Test-only fixture state, never a shipped permission.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- see above  # noqa: E501
    os.chmod(d, 0o755)  # separate from mkdir: mkdir's mode is umask-masked
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: d)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    assert d.stat().st_mode & 0o777 == 0o755, "pre-existing dir was re-permissioned"
    assert (d / f"{new_sid}.jsonl").stat().st_mode & 0o777 == 0o600


def test_layer_b_write_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    """The shared helper allocates its temp via ``mkstemp`` rather than a
    deterministic ``<name>.tmp``, which is what made concurrent writers race to
    ENOENT. Pin that only the two real files remain.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"{new_sid}.json",
        f"{new_sid}.jsonl",
    ]


def test_validate_carries_the_skipped_flag_through():
    """The marker branches on this flag, so it must survive normalisation.

    ``_validate_bundle`` builds a fresh dict, so a field it does not copy reads
    as absent downstream no matter what the sender put on the wire.
    """
    bundle, err = _validate_bundle(_valid(layer_b_skipped=True))
    assert err is None
    assert bundle["layer_b_skipped"] is True
    # Coerced, not passed through: it arrives from an untrusted peer.
    bundle, _ = _validate_bundle(_valid(layer_b_skipped="yes"))
    assert bundle["layer_b_skipped"] is True
    bundle, _ = _validate_bundle(_valid())
    assert bundle["layer_b_skipped"] is False


def test_mid_turn_bundle_announces_that_it_withheld_context():
    """An absent ``layer_b`` is ambiguous on the wire; the sender disambiguates."""
    from kiro_crew.dashboard import session_transfer as st

    skipped = st._assemble_bundle([], "t", "", "mac", None, True)
    never_had = st._assemble_bundle([], "t", "", "mac", None, False)

    assert skipped["layer_b_skipped"] is True
    assert "layer_b" not in skipped
    assert "layer_b_skipped" not in never_had


@pytest.mark.asyncio
async def test_imported_tab_is_marked_when_the_sender_withheld_layer_b(monkeypatch):
    """The gap: a mid-turn source ships NO ``layer_b``, so gating the marker on
    that key silenced the tab in exactly the case the sender's own row was
    reporting as transcript-only.

    The two negatives are covered elsewhere: a v1 bundle by
    ``test_a_v1_import_is_not_marked_transcript_only``, and a v2 session that
    never had a context by ``test_import_creates_a_new_slot_with_no_project``
    (plain ``_valid()``, no flag -> no suffix).
    """
    from kiro_crew.dashboard import session_transfer as st

    bundle = _valid(layer_b_skipped=True)
    bundle.pop("layer_b", None)

    slot = await _run_import(st, monkeypatch, bundle, return_slot=True)

    assert slot.title.endswith("— transcript only"), slot.title


# ── slot-cap accounting across the construction window ───────────────────


def test_retracted_import_slot_still_counts_against_the_cap():
    """A slot retracted for construction is unreachable but ALLOCATED.

    The cap is sampled before creation and reads the live count, so if a
    retracted slot stopped counting, concurrent imports would each sample a
    total that excluded every other import in flight -- and all of them would be
    waved past a cap that was already full.
    """
    from kiro_crew.dashboard.state import DashboardState

    state = DashboardState.__new__(DashboardState)
    state._slots = {"chat-1": object()}
    state._slots_under_construction = set()

    assert state.live_slot_count() == 1

    state.begin_slot_construction("chat-2")
    assert state.live_slot_count() == 2, "an in-flight slot vanished from the cap"

    # Idempotent, and releasing restores the count.
    state.end_slot_construction("chat-2")
    state.end_slot_construction("chat-2")
    assert state.live_slot_count() == 1


@pytest.mark.asyncio
async def test_import_releases_the_construction_count_on_every_exit(monkeypatch):
    """A leaked construction key inflates the cap for the process lifetime --
    every later import would be refused with the cap never actually reached.

    Drives the failure path (the durable save refuses, which returns 503 from
    inside the ``try``) and asserts the release happened anyway.
    """
    from kiro_crew.dashboard import session_transfer as st

    async def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    state = _stub_state(st, monkeypatch, save=_boom)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 503
    assert state._slots_under_construction == set(), "construction count leaked"
    # And the slot was not published either -- a refused import leaves no tab.
    assert state._slots == {}


@pytest.mark.asyncio
async def test_import_releases_the_construction_count_on_success(monkeypatch):
    """The success path publishes the slot and stops counting it separately, so
    the total does not double-count a landed import."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 200
    assert state._slots_under_construction == set()
    assert state.live_slot_count() == 1


def test_a_mapped_but_unreadable_layer_b_is_reported_as_withheld(monkeypatch):
    """Absence and LOSS are different wires.

    A mapped sid whose files will not read (pruned, over the cap, unparseable)
    is context the session had and is giving up, so the peer must hear about it
    -- otherwise the imported tab looks like a complete copy with no resumable
    context behind it. An empty sid stays silent: there was nothing to carry.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_read_chained_history", lambda *_a, **_k: [])
    monkeypatch.setattr(st, "_read_layer_b", lambda _sid: None)

    lost = st._read_and_assemble(
        None, "k", [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", "mapped-sid"
    )
    never_had = st._read_and_assemble(
        None, "k", [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", ""
    )

    assert lost["layer_b_skipped"] is True
    assert "layer_b_skipped" not in never_had


def test_layer_b_is_discarded_when_owner_lockdown_fails(monkeypatch, tmp_path):
    """Fail CLOSED, and leave nothing behind.

    On Windows the ``mode=0o600`` on the write is a no-op, so the DACL call is the
    only thing making the file owner-only -- if it raises, the context is readable
    by other local accounts on a shared machine. Refusing costs only resume
    fidelity (the import lands transcript-only), so it is the cheaper side of the
    trade. Both files must go: the pair is useless alone and the ``.json`` carries
    context too.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    def _refuse(_path):
        raise OSError("cannot resolve the invoking user's SID")

    monkeypatch.setattr(st, "restrict_to_owner", _refuse)

    got = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert got is None, "returned a sid for context it could not protect"
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"left unprotected context on disk: {leftovers}"


def test_import_preserves_the_thinking_signature_verbatim(monkeypatch, tmp_path):
    """THE regression test for this feature's worst failure mode.

    An earlier revision redacted Layer B on both boundaries. Measured against 704
    real sessions on a developer machine, that rewrote a thinking-block
    ``signature`` in 41% of them -- and the provider validates that signature when
    it replays the conversation, so the peer's ``session/load`` succeeded and its
    very NEXT turn was rejected. Every test in the suite passed, because they all
    used ``{"envelope": {}}``.

    Asserts on the byte-level file content, not on the in-memory dict, because the
    file is what kiro-cli reads.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    sig = _THINKING_ENVELOPE["session_state"]["conversation_metadata"][
        "user_turn_metadatas"
    ][0]["result"]["Ok"]["content"][0]["data"]["signature"]

    new_sid = st._write_layer_b_files(
        {"envelope": _THINKING_ENVELOPE, "events": '{"kind":"Prompt"}\n'}, "target-agent"
    )

    assert new_sid
    written = json.loads((tmp_path / f"{new_sid}.json").read_text(encoding="utf-8"))
    landed = written["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
    block = landed["result"]["Ok"]["content"][0]
    assert block["data"]["signature"] == sig, "the thinking signature was rewritten"
    assert block["data"]["text"] == "let me think about the plan"
    # The events blob lands byte-for-byte.
    assert (tmp_path / f"{new_sid}.jsonl").read_text(encoding="utf-8") == '{"kind":"Prompt"}\n'
    # Host-naming fields ARE still neutralised -- byte-exact applies to the
    # conversation, not to the fields that point at the sender's machine.
    assert written["session_id"] == new_sid
    assert written["cwd"] == ""
    assert written["session_state"]["agent_name"] == "target-agent"
    assert written["session_state"]["permissions"]["filesystem"]["allowed_read_paths"] == []


# ── failure-path hygiene found by review on ff205cba1 ────────────────────


def test_layer_b_write_leaves_no_half_pair_when_the_second_write_fails(monkeypatch, tmp_path):
    """The pair is written one file at a time. A failure on the SECOND write left
    the first behind: an orphan no join references, that ``_read_layer_b`` will not
    load (it needs both), and that nothing else cleans up."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    real_write = st.atomic_write
    calls = {"n": 0}

    def _fail_on_second(path, text, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space left on device")
        return real_write(path, text, **kw)

    monkeypatch.setattr(st, "atomic_write", _fail_on_second)

    got = st._write_layer_b_files(
        {"envelope": _THINKING_ENVELOPE, "events": '{"kind":"Prompt"}\n'}, ""
    )

    assert got is None
    assert calls["n"] == 2, "expected the second write to be the failing one"
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"half-pair left on disk: {leftovers}"


@pytest.mark.asyncio
async def test_import_rolls_back_layer_b_on_cancellation(monkeypatch):
    """``CancelledError`` is a BaseException, so the ordinary ``except Exception``
    never saw it -- a shutdown after the join left an orphaned map entry and files
    behind a slot that never publishes."""
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    async def _cancel(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(st, "save_slot_off_loop", _cancel)
    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(
        st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "lb-sid")[1]
    )
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch, save=_cancel)
    with pytest.raises(asyncio.CancelledError):
        await st.api_chat_slot_import(
            _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
        )

    assert forgotten, "the join was not rolled back on cancellation"
    assert unlinked == ["lb-sid"], f"files were not removed: {unlinked}"
    assert state._slots == {}
    assert state._slots_under_construction == set()


@pytest.mark.asyncio
async def test_slot_cap_is_rechecked_after_the_pre_creation_awaits(monkeypatch):
    """The first cap test is necessary but not sufficient: body parsing and agent
    resolution both await, so concurrent imports near the cap all clear it before
    any of them allocates. The second test sits with no await before creation.

    Simulated by filling the map DURING the awaited agent resolution -- exactly
    what a sibling request would do.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    async def _resolve_then_fill(*_a, **_k):
        # A concurrent import lands while this one is awaiting.
        state._slots.update({f"s{i}": object() for i in range(500)})
        return ""

    monkeypatch.setattr(st, "asyncio", asyncio)
    monkeypatch.setattr(st, "_resolve_agent", lambda hint: "")
    monkeypatch.setattr(asyncio, "to_thread", _resolve_then_fill)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(agent="some-agent")))

    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"
    # Nothing was allocated by the request that lost the race.
    assert "imported-1" not in state._slots
