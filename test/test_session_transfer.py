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
        get_or_create_slot=_get_or_create,
        push_slots_update=lambda: None,
    )

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

    assert set(body) == {"ok", "key", "title", "messages"}
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
    monkeypatch.setattr(st, "SessionMap", lambda: SimpleNamespace(get=lambda _k: sid))

    got = st._read_layer_b("dashboard:slot-1")

    assert got is not None
    assert got["sid"] == sid
    assert got["envelope"]["session_id"] == sid
    assert "Prompt" in got["events"]


def test_layer_b_is_absent_when_the_session_has_no_kiro_context(monkeypatch, tmp_path):
    """A brand-new slot (or a pruned map entry) has no Layer B; the transfer must
    degrade to transcript-only rather than fail."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "SessionMap", lambda: SimpleNamespace(get=lambda _k: None))

    assert st._read_layer_b("dashboard:slot-1") is None


def test_layer_b_absent_when_the_files_were_pruned(monkeypatch, tmp_path):
    """A map entry can outlive its files; a missing pair is not an error."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "SessionMap", lambda: SimpleNamespace(get=lambda _k: "no-such-sid"))

    assert st._read_layer_b("dashboard:slot-1") is None


def test_layer_b_events_are_redacted_on_egress(monkeypatch, tmp_path):
    """The ``.jsonl`` is RAW kiro-cli state that can hold a credential the
    redactors never saw. It leaves the host, so it is scrubbed here — the WHOLE
    blob, user turns included, because this is model context rather than the
    human's shown words."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    secret = "AKIAIOSFODNN7EXAMPLE"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text(f'{{"c":"key {secret}"}}\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "SessionMap", lambda: SimpleNamespace(get=lambda _k: sid))

    got = st._read_layer_b("dashboard:slot-1")

    assert got is not None
    assert secret not in got["events"]


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


def test_materialise_layer_b_writes_the_files_and_joins_the_slot(monkeypatch, tmp_path):
    """Establishing the session_map entry is what the resume path reads to issue
    ``session/load`` — and it is also what auto-disables the lossy prefix."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    recorded: dict = {}

    class _Map:
        def set(self, key, sid, *, provider="", cwd=""):
            recorded.update({"key": key, "sid": sid, "provider": provider, "cwd": cwd})

    monkeypatch.setattr(st, "SessionMap", _Map)

    ok = st._materialise_layer_b(
        "dashboard:imported-1",
        {"envelope": {"session_id": "peer-sid", "cwd": "/peer/path"}, "events": '{"k":1}\n'},
        "my-agent",
    )

    assert ok is True
    assert recorded["key"] == "dashboard:imported-1"
    assert recorded["provider"] == "acp"
    # Fresh sid, NOT the peer's — copy semantics.
    new_sid = recorded["sid"]
    assert new_sid and new_sid != "peer-sid"
    written = json.loads((tmp_path / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert written["session_id"] == new_sid
    assert written["cwd"] == ""
    assert (tmp_path / f"{new_sid}.jsonl").read_text(encoding="utf-8") == '{"k":1}\n'


def test_materialise_layer_b_is_best_effort(monkeypatch, tmp_path):
    """A Layer B failure must NOT fail an already-persisted import — the session
    still opens as the transcript-only copy."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    class _Boom:
        def set(self, *_a, **_k):
            raise OSError("map write failed")

    monkeypatch.setattr(st, "SessionMap", _Boom)

    assert st._materialise_layer_b("k", {"envelope": {}, "events": ""}, "") is False


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

    def _fake(sm_key, layer_b, agent):
        calls.update({"sm_key": sm_key, "layer_b": layer_b, "agent": agent})
        return True

    monkeypatch.setattr(st, "_materialise_layer_b", _fake)
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

    monkeypatch.setattr(st, "_materialise_layer_b", lambda *_a, **_k: False)
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

    monkeypatch.setattr(st, "_materialise_layer_b", _count)
    resp = await _run_import(st, monkeypatch, _valid(bundle_version=1))

    assert resp.status == 200
    assert called["n"] == 0


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


def _stub_state(st, monkeypatch):
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

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    monkeypatch.setattr(st, "_sync_dashboard_slots", lambda _s: None)
    state = SimpleNamespace(
        _slots={},
        get_or_create_slot=lambda **_k: slot,
        push_slots_update=lambda: None,
    )
    state._imported_slot = slot
    return state


async def _run_import(st, monkeypatch, body, *, return_slot=False, created=None):
    state = _stub_state(st, monkeypatch)
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
