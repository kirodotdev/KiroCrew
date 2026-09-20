"""Session transfer between instances — bundle, validation, and import.

Covers the two halves of the feature (``build_transfer_bundle_async`` on the
sending side, ``api_chat_slot_import`` on the receiving side) plus the
tunnel-manager delivery hop, with the emphasis on the invariants a reviewer would
want pinned:

* **copy, never move** — the source is untouched and the target key is new;
* **project does NOT travel** — the documented decision that an imported
  session arrives unscoped so the user re-picks a checkout;
* **unknown bundle versions are refused** rather than best-effort parsed;
* **the token never appears in a transfer response** (instances.md §6's
  "connect + refresh-token are the only two token-crossing routes").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.dashboard.session_transfer import (
    BUNDLE_VERSION,
    _validate_bundle,
    build_transfer_bundle_async,
    local_instance_label,
)

# ── bundle construction ──────────────────────────────────────────────────


class _FakeLog:
    def __init__(self, messages):
        self._messages = messages

    def read_messages_chained(self, _key):
        return list(self._messages)


def _slot(
    messages, *, title="My session", titled=True, agent="", dirty=False, project="", disk_older=0
):
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
        # Frozen-prefix length for the id-based tail merge (_append_unflushed_tail
        # scans the disk read from this offset). 0 fits fakes whose disk read IS
        # the window; a test whose window is a tail of a longer transcript must
        # override it with the real prefix length.
        _disk_older_count=disk_older,
        _pending_rewrite=False,
        _dirty_gen=0,
        memory_mode="persistent",
        # Idle by default: Layer B only travels when no turn is in flight.
        running=False,
        _in_stage_execution=False,
    )


def _state(messages):
    return SimpleNamespace(conversation_log=_FakeLog(messages))


@pytest.mark.asyncio
async def test_bundle_carries_only_visible_roles():
    msgs = [
        {"role": "user", "content": "hi", "ts": "t1"},
        {"role": "tool", "content": "tool frame", "ts": "t2"},
        {"role": "assistant", "content": "hello", "ts": "t3"},
        {"role": "system", "content": "sys", "ts": "t4"},
    ]
    slot = _slot(msgs)
    bundle = await build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert bundle["bundle_version"] == BUNDLE_VERSION
    assert bundle["origin"] == "mac"
    assert [m["role"] for m in bundle["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in bundle["messages"]] == ["hi", "hello"]


@pytest.mark.asyncio
async def test_bundle_does_not_carry_project_or_model():
    """The two fields deliberately dropped — a dangling path and an
    entitlement-specific model id (see the module docstring in the source)."""
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, project="/Volumes/workplace/only-on-my-mac")
    slot.model = "some-model-id"
    bundle = await build_transfer_bundle_async(_state(msgs), slot)

    assert "project" not in bundle
    assert "model" not in bundle
    assert "/Volumes/workplace/only-on-my-mac" not in json.dumps(bundle)


@pytest.mark.asyncio
async def test_bundle_reads_full_history_not_just_resident_window():
    """A long session keeps only a tail in memory; the bundle must be complete."""
    on_disk = [{"role": "user", "content": f"turn {i}", "ts": ""} for i in range(10)]
    # slot.messages holds only the last two — bundling those would truncate.
    # disk_older=8 is what a real slot reports: eight on-disk rows precede the
    # resident window, and the tail merge must scan only the window region.
    slot = _slot(on_disk[-2:], disk_older=8)
    bundle = await build_transfer_bundle_async(_state(on_disk), slot)

    assert len(bundle["messages"]) == 10
    assert bundle["messages"][0]["content"] == "turn 0"


@pytest.mark.asyncio
async def test_bundle_title_marker_does_not_compound_across_hops():
    """A session bounced back and forth must not grow one prefix per hop."""
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title="⇄ Already imported once")
    bundle = await build_transfer_bundle_async(_state(msgs), slot)

    assert bundle["title"] == "Already imported once"


@pytest.mark.asyncio
async def test_bundle_untitled_slot_carries_empty_title():
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title="slot-1", titled=False)
    assert (await build_transfer_bundle_async(_state(msgs), slot))["title"] == ""


@pytest.mark.asyncio
async def test_send_handler_sends_each_turn_exactly_once(monkeypatch):
    """Each turn is sent exactly once.

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
        return True

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

    # The resolved agent now lands on the slot through the shared materialiser
    # (via the metadata snapshot), not through a ``get_or_create_slot(agent=...)``
    # kwarg — so assert it on the returned slot, which is what the property is
    # actually about. ``_resolve_agent`` is offloaded when a hint is present.
    slot = await _run_import(st, monkeypatch, _valid(agent="my-agent"), return_slot=True)
    assert st._resolve_agent in offloaded, "agent resolution must be offloaded"
    assert slot.agent == "my-agent"

    # Unhinted: no offload for AGENT RESOLUTION specifically. Redaction always
    # offloads (``_redact_history_rows`` runs the regex pass off the loop before
    # construction), so the invariant is that an empty hint adds no _resolve_agent
    # hop — not that ``to_thread`` is never called at all.
    offloaded.clear()
    created2: dict = {}
    await _run_import(st, monkeypatch, _valid(agent=""), created=created2)
    assert st._resolve_agent not in offloaded, "an empty agent hint must not resolve an agent"
    assert created2.get("agent") == ""
    monkeypatch.setattr(st.asyncio, "to_thread", real_to_thread)


@pytest.mark.asyncio
async def test_bundle_appends_nothing_when_everything_is_persisted():
    persisted = [{"role": "user", "content": "one", "ts": ""}]
    slot = _slot(persisted)
    slot.messages = list(persisted)
    slot._disk_window_len = 1
    slot._dirty = False

    bundle = await build_transfer_bundle_async(_state(persisted), slot)

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


@pytest.mark.asyncio
async def test_bundle_redacts_assistant_content_on_the_way_out():
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
    bundle = await build_transfer_bundle_async(_state(msgs), slot)
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


@pytest.mark.asyncio
async def test_bundle_redacts_the_title_on_the_way_out():
    """A title is generated from user content, and the resume path assigns a
    client-supplied title with no scan of its own — so it can carry a credential
    that would otherwise leave the host verbatim."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs, title=f"debugging {secret}")

    bundle = await build_transfer_bundle_async(_state(msgs), slot)

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


@pytest.mark.asyncio
async def test_bundle_reads_the_transcript_key_not_the_session_key():
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

    bundle = await build_transfer_bundle_async(SimpleNamespace(conversation_log=_Log()), slot)

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
        return True

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
        bundle = await st.build_transfer_bundle_async(_state([persisted]), slot, origin="mac")
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
    """A refused import must leave no tab behind.

    Under the fold the slot is NEVER published before success: the shared
    materialiser hands it back retracted, and import re-registers + broadcasts
    only at the very end of a successful import. So on the save-failure path
    there is nothing a client ever saw — the correct rollback is simply that the
    slot is absent from ``_slots`` and the construction count is released. A
    broadcast here would announce the removal of a tab no client was ever told
    about, so its ABSENCE on this path is correct, not a regression. (Before the
    fold the slot was published at creation, so a rollback had to broadcast its
    removal; the fold removed the early publish, and with it the need.)
    """
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
    # No phantom tab, and the construction count released — the real invariant.
    assert state._slots == {}
    assert state._slots_under_construction == set()
    # The slot was never published, so the failure path broadcasts nothing:
    # there is no tab to retract from any client's view.
    assert pushes["n"] == 0, "a never-published slot must not broadcast a removal"


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
        return True

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
        return True

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
        bundle = await st.build_transfer_bundle_async(_state(disk), slot, origin="mac")
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
async def test_send_bundle_reports_an_unreachable_peer_without_leaking_the_bundle(monkeypatch):
    from kiro_crew.instances import ssh_tunnel_manager as mod
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    mgr._tokens = {"peer": "irrelevant-credential"}
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=1
    )

    # The POST fails at CONNECT, modelled rather than provoked: "a port nothing
    # listens on" is not a property a test can assume of the host. Endpoint
    # agents on managed machines intercept loopback connects and answer every
    # port with HTTP 200 (observed: a SOAP envelope from 127.0.0.1:1), which made
    # this test report the peer reachable and the bundle delivered.
    class _RefusingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, json=None, headers=None):
            raise ConnectionRefusedError(111, "connection refused")

    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: _RefusingSession())
    ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 1})

    assert ok is False
    assert payload["code"] == "transfer_unreachable"


def _peer_answering(status: int, *, body: object = None):
    """A fake ``aiohttp.ClientSession`` factory + the POST counter it records.

    ``body=None`` models a peer whose body is not JSON — what aiohttp's own
    default error responses (text/plain) look like from the client side.
    """
    posts = {"n": 0}

    class _Resp:
        def __init__(self) -> None:
            self.status = status

        async def json(self):
            if body is None:
                raise ValueError("not json")
            return body

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

    return _Session, posts


@pytest.mark.parametrize("status", [404, 405])
@pytest.mark.asyncio
async def test_send_bundle_names_an_older_peer_when_the_importer_is_missing(status):
    """A peer with no importer route gets named as too old, not as a status code.

    405 is the shape a real pre-importer peer produces: the path falls through
    to ``/api/chat/slots/{slot}``, which is registered GET/DELETE only.
    """
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

    async def _no_remint(_id):
        raise AssertionError("a missing route is not a credential problem")

    mgr.refresh_token = _no_remint  # type: ignore[method-assign]
    session_cls, posts = _peer_answering(status)

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: session_cls()  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle("peer", {"bundle_version": 2})
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_peer_too_old"
    # The message must say what to DO; a bare status code is what this replaces.
    assert "older Kiro Crew" in payload["error"]
    assert "update it" in payload["error"]
    assert str(status) not in payload["error"]
    assert posts["n"] == 1, "a missing route is final — no downgrade retry"


def test_importer_never_answers_404_or_405():
    """Pins the premise of the mapping above.

    ``send_session_bundle`` reads 404/405 as "this peer has no importer". That
    is only sound while the importer's own refusals stay off those two codes;
    if one starts using them, a real refusal would be misreported as a stale
    build.
    """
    from pathlib import Path

    import kiro_crew.dashboard.session_transfer as st

    source = Path(st.__file__).read_text(encoding="utf-8")
    assert "status=404" not in source
    assert "status=405" not in source


# ── import endpoint ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_creates_a_new_slot_with_no_project(monkeypatch):
    """The headline decision: an imported session arrives unscoped.

    Driven through the real shared materialisation path (the fold): the handler
    lands the transcript in memory and routes construction through
    ``_materialise_slot_from_history``, which never sets a project. The assertion
    is that the resulting slot is a fresh, unscoped copy.
    """
    from kiro_crew.dashboard import session_transfer as st

    created: dict = {}
    slot = await _run_import(
        st,
        monkeypatch,
        _valid(
            title="Design chat",
            origin="macbook",
            messages=[
                {"role": "user", "content": "what about the tunnel?", "ts": ""},
                {"role": "assistant", "content": "it forwards loopback", "ts": ""},
            ],
        ),
        created=created,
        return_slot=True,
    )

    # Copy semantics: a brand-new key, and no project inherited. A fresh
    # _ChatSlot has an empty project and the shared path never sets one, so an
    # unscoped arrival is the real behaviour, not a stub artifact.
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
async def test_import_redacts_off_the_loop_before_construction(monkeypatch):
    """A big bundle must not hold the loop in one un-yielded pass.

    Redaction is regex-heavy and the content is peer-supplied. Construction is
    synchronous, so the redaction cost runs AHEAD of construction and off the
    event loop: import calls ``_redact_history_rows`` via ``asyncio.to_thread``
    before any slot exists, so the regex work runs on a worker thread and the loop
    is free to service other turns. This pins that the redaction pass is
    dispatched to a thread rather than run inline on the loop.
    """
    from kiro_crew.dashboard import session_transfer as st

    offloaded = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(fn, *a, **k):
        if getattr(fn, "__name__", "") == "_redact_history_rows":
            offloaded.append(fn)
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(st.asyncio, "to_thread", _tracking_to_thread)

    big = [{"role": "assistant", "content": "x" * 500, "ts": ""} for _ in range(20)]
    await _run_import(st, monkeypatch, _valid(messages=big))

    assert offloaded, "import redaction did not run off the event loop before construction"


@pytest.mark.asyncio
async def test_import_persists_every_row_of_a_bundle_over_the_resume_window(monkeypatch):
    """A bundle larger than resume's 500-row window must persist EVERY row.

    The shared materialiser windows resume's rows to the newest 500 because the
    earlier ones already sit on disk. Import's rows exist only in memory and are
    all persisted by its own save, so nothing is "older on disk": it passes
    ``window_limit=None`` and must hydrate every row with ``_disk_older_count``
    at 0. Applying resume's cap here would silently drop everything past the last
    500 and claim a frozen prefix of rows that were never written -- a
    silent-data-loss regression on the exact "lossy copy" the transfer feature's
    resume_mode plumbing exists to surface. Bundles carry up to _MAX_MESSAGES
    (5000) rows in-contract, so >500 is an ordinary input, not an edge.
    """
    from kiro_crew.dashboard import session_transfer as st

    n = 750  # comfortably past the 500 window, well under _MAX_MESSAGES
    big = [{"role": "assistant", "content": f"row-{i}", "ts": ""} for i in range(n)]
    slot = await _run_import(st, monkeypatch, _valid(messages=big), return_slot=True)

    # Every row is hydrated onto the slot -- none dropped by a resume-shaped cap.
    assert len(slot.messages) == n, (
        f"import kept only {len(slot.messages)} of {n} rows; a bundle over the "
        "resume window was silently truncated"
    )
    assert slot.messages[0]["content"] == "row-0", "the oldest rows were dropped"
    assert slot.messages[-1]["content"] == f"row-{n - 1}"
    # No phantom frozen prefix: nothing is older-on-disk for an in-memory import.
    assert slot._disk_older_count == 0, (
        f"_disk_older_count={slot._disk_older_count}; import claims a frozen prefix "
        "of on-disk rows that were never written, poisoning the save accounting"
    )
    assert slot._disk_older_durable_count == 0


@pytest.mark.asyncio
async def test_import_does_not_arm_the_disk_delete_won_guard(monkeypatch):
    """Import read no transcript off disk, so the delete-won identity guard must
    stay dormant.

    ``_disk_meta_observed`` / ``_disk_meta_created_at`` tell a later save that this
    slot was hydrated from an existing on-disk transcript, arming the guard that
    refuses to overwrite a file recreated under it. Import synthesises its
    metadata and has no pre-existing file, so setting the observed bit would arm
    the guard against a disk read that never happened.
    """
    from kiro_crew.dashboard import session_transfer as st

    slot = await _run_import(st, monkeypatch, _valid(), return_slot=True)
    assert slot._disk_meta_observed is False, (
        "import armed the delete-won disk-identity guard, but it read no transcript " "off disk"
    )
    assert slot._disk_meta_created_at == ""


@pytest.mark.asyncio
async def test_imported_rows_are_replayed_silently_not_broadcast(monkeypatch):
    """Import is a silent replay onto a RETRACTED slot: no row may broadcast.

    The materialiser keeps the slot out of ``_slots`` during hydration so nothing
    can interleave. ``_ChatSlot.append`` also broadcasts a live ``chat_message``
    SSE event when ``broadcast=True`` (its docstring names session_transfer among
    the replay callers that must pass False), which would push a retracted slot's
    peer content to every client and retire live question cards. Every appended
    row must therefore carry ``broadcast=False``.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    seen_broadcast: list[bool] = []
    real_append = _ChatSlot.append

    def _spy_append(self, role, content, cls="", ts="", *, broadcast=True, **kw):
        seen_broadcast.append(broadcast)
        return real_append(self, role, content, cls, ts, broadcast=broadcast, **kw)

    monkeypatch.setattr(_ChatSlot, "append", _spy_append)

    msgs = [
        {"role": "user", "content": "q", "ts": ""},
        {"role": "assistant", "content": "a", "ts": ""},
    ]
    await _run_import(st, monkeypatch, _valid(messages=msgs))

    assert seen_broadcast, "no rows were appended; fixture did not engage"
    assert all(b is False for b in seen_broadcast), (
        f"an imported row was appended with broadcast=True ({seen_broadcast}); it "
        "would fan a retracted slot's content out as a live SSE event"
    )


@pytest.mark.asyncio
async def test_imported_rows_carry_a_minted_mid(monkeypatch):
    """Bundle rows have no message id, so the materialiser must mint one.

    The old importer left ``mint_mid`` at its default (True) and minted a mid per
    row; the shared path defaults to False for resume, whose disk rows already
    carry mids. Import passes ``mint_missing_mids=True`` -- without it, imported
    rows land permanently id-less and drop out of every mid-keyed feature
    (row-identity dedup, the non-legacy Fork path).
    """
    from kiro_crew.dashboard import session_transfer as st

    msgs = [
        {"role": "user", "content": "q", "ts": ""},
        {"role": "assistant", "content": "a", "ts": ""},
    ]
    slot = await _run_import(st, monkeypatch, _valid(messages=msgs), return_slot=True)

    for m in slot.messages:
        mid = (m.get("meta") or {}).get("mid")
        assert isinstance(mid, str) and mid, f"an imported row landed without a minted mid: {m!r}"


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


def test_write_layer_b_files_writes_a_fresh_sid_and_never_touches_the_map(monkeypatch, tmp_path):
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

    bundle = st._assemble_bundle(
        [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", None
    )

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


#: Stands in for the over-limit Layer B, which the test body materializes from
#: the production constant. A 40 MB string literal here would be built while the
#: module is IMPORTED, so every xdist worker pays ~38 MiB during collection and
#: holds it for the whole session -- the mark keeps its argvalues alive on the
#: function object. Deriving the length from ``_MAX_LAYER_B_CHARS`` also keeps
#: the test honest if that limit ever moves.
_OVERSIZE_LAYER_B = "oversize-layer-b"


@pytest.mark.parametrize(
    "layer_b,code",
    [
        ("not a dict", "transfer_layer_b_not_object"),
        ({"envelope": "nope", "events": ""}, "transfer_layer_b_bad_envelope"),
        ({"envelope": {}, "events": 5}, "transfer_layer_b_bad_events"),
        (_OVERSIZE_LAYER_B, "transfer_layer_b_too_large"),
    ],
)
def test_validate_rejects_a_malformed_layer_b(layer_b, code):
    """Layer B is untrusted peer input and is bounded BEFORE anything is written."""
    from kiro_crew.dashboard import session_transfer as st

    if layer_b is _OVERSIZE_LAYER_B:
        layer_b = {"envelope": {}, "events": "x" * (st._MAX_LAYER_B_CHARS + 1)}
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
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

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
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

    assert json.loads(resp.body)["resume_mode"] == "session_load"


@pytest.mark.asyncio
async def test_a_carried_bundle_resumes_and_its_tab_is_not_marked_transcript_only(monkeypatch):
    """The import-side invariant the file export now depends on.

    A file export that carries Layer B produces a v2 bundle WITH ``layer_b`` and
    WITHOUT ``layer_b_skipped``. When that context materialises, the tab resumes
    through ``session/load`` and must NOT wear the "transcript only" suffix -- the
    suffix is for a degraded copy, and a carried export is not degraded.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)

    bundle = _valid(layer_b={"envelope": {}, "events": "e"})
    assert "layer_b_skipped" not in bundle, "a carried export never sets the skip flag"

    slot = await _run_import(st, monkeypatch, bundle, return_slot=True)

    assert "transcript only" not in slot.title, slot.title


@pytest.mark.asyncio
async def test_import_reports_prefix_when_layer_b_failed(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

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
        return True

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
    the map keeps an entry for a tab that does not exist and the
    ``<sid>.{json,jsonl}`` pair lingers until a prune sweeps it."""
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, key: (forgotten.append(key), "")[1])

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
    live = SimpleNamespace(forget_conversation=lambda key: (dropped.append(key), "old-sid")[1])

    assert st._forget_layer_b_join(live, "dashboard:imported-1") == "old-sid"
    assert dropped == ["dashboard:imported-1"]


def test_forget_layer_b_join_is_silent_without_a_live_manager():
    from kiro_crew.dashboard import session_transfer as st

    assert st._forget_layer_b_join(None, "k") == ""  # must not raise


@pytest.mark.asyncio
async def test_slot_is_unreachable_until_construction_finishes(monkeypatch):
    """A slot mid-import is retracted from ``_slots`` for its async tail.

    The materialiser returns the slot registered, but the import handler pops it
    from ``state._slots`` for the async Layer B write/join + durable save, so no
    raw ``state._slots.get`` acquirer (delete/close, regenerate, rewind) can reach
    it mid-finalization. It stays under ``begin_slot_construction`` for the count,
    and is re-registered on the success path once finalization lands. So at the
    Layer B write the slot must be ABSENT from ``_slots`` and UNDER CONSTRUCTION;
    after the handler returns it is registered and out of the construction set.
    Retracting is safe here because import mints its own key (no concurrent
    same-key request can target it), unlike a client-supplied resume key.
    """
    from kiro_crew.dashboard import session_transfer as st

    seen_registered: list[bool] = []
    seen_under_construction: list[bool] = []
    state = _stub_state(st, monkeypatch)

    def _probe(*_a, **_k):
        # Sampled from inside the build, standing in for a concurrent lookup.
        s = state._imported_slot
        seen_registered.append(s.key in state._slots)
        seen_under_construction.append(s.key in state._slots_under_construction)
        return "new-sid"

    monkeypatch.setattr(st, "_write_layer_b_files", _probe)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)

    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 200
    # RETRACTED (absent from _slots, so no raw acquirer can find it) AND under
    # construction (the count is still open) during the async tail.
    assert seen_registered == [False], (
        "the slot must be retracted from _slots during the async Layer B tail so "
        "no raw acquirer can close/mutate it mid-finalization"
    )
    assert seen_under_construction == [True], (
        "the slot must stay under construction during the tail (count open, "
        "released in the finally)"
    )
    # ...and re-registered + published once everything landed.
    slot = state._imported_slot
    assert state._slots.get(slot.key) is slot
    assert (
        slot.key not in state._slots_under_construction
    ), "construction was never ended; the slot would stay hidden forever"


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


def _make_request(state, body, *, raw: str | None = None, gz: bytes | None = None):
    """A minimal aiohttp-request stand-in for the import handler.

    Serves BYTES via ``read()``, not a pre-parsed dict, because the handler
    decides the body format from the first two bytes — a stub that handed back a
    dict would skip the very branch under test. ``gz`` sends compressed bytes
    verbatim (what a browser uploads); ``raw`` sends an exact string (malformed
    JSON); otherwise *body* is serialised the way a real caller would.
    """

    if gz is not None:
        payload = gz
    elif raw is not None:
        payload = raw.encode("utf-8")
    else:
        payload = json.dumps(body).encode("utf-8")

    async def _read():
        return payload

    return SimpleNamespace(
        app={"state": state},
        get=lambda _k, default="": default,
        # A real request always has headers, and the caller-identity rule reads
        # ``X-Session-Key`` off them when the auth middleware published no app
        # claim. Empty is the dashboard owner, which is what these tests are.
        headers={},
        read=_read,
    )


def _stub_state(st, monkeypatch, save=None):
    # A REAL _ChatSlot, not a hand-rolled fake: the fold routes import through
    # the shared materialiser, which sets ~20 slot attributes and re-runs the
    # title/provenance helpers. A fake that only grows the fields the test
    # happens to touch would silently diverge from the production slot; a real
    # one cannot. get_or_create_slot below mints the key the same way the real
    # one does (name=None -> a fresh key).
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    def _get_or_create(name=None, *_a, **kwargs):
        s = _ChatSlot(name or "imported-1", agent=kwargs.get("agent", ""))
        # Production's get_or_create_slot REGISTERS the slot in _slots (and
        # broadcasts) before returning; model that, or a test asserting the slot
        # is hidden-during-construction passes vacuously because the stub never
        # put it in _slots at all.
        state._slots[s.key] = s
        state._imported_slot = s
        return s

    async def _save(*_a, **_k):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", save or _save)
    monkeypatch.setattr(st, "_sync_dashboard_slots", lambda _s: None)
    state = SimpleNamespace(
        _slots={},
        # Mirrors DashboardState's construction accounting: a slot retracted
        # while it is built is absent from _slots but still counts against every
        # cap, so the stub has to model both halves or the handler's cap check
        # and its release would not be exercised at all.
        _slots_under_construction=set(),
        # The shared materialiser toggles this per the imported session's
        # memory_mode; import's bundle carries none, so it stays untouched here,
        # but the attribute must exist for the discard/add to run.
        _restricted_keys=set(),
        _tags=[],
        _tags_authoritative=True,
        _folders=[],
        # ``DashboardState`` always HAS this attribute, and ``None`` is a real
        # production value for it (``state.py``: ``conversation_log:
        # ConversationLog | None = None``). Present here because the import
        # handler's delete witness reads it on every import; without it the stub
        # raises ``AttributeError`` where production reads a legitimate ``None``.
        #
        # ``None`` makes the witness answer False by its own documented rule — a
        # store with no path resolver cannot witness a delete — so these tests
        # keep exercising the real ``session_was_deleted``, and the DELETE path is
        # driven deliberately in
        # ``test_a_delete_during_the_folder_check_is_not_reported_as_landed``
        # rather than being simulated everywhere.
        conversation_log=None,
        get_or_create_slot=_get_or_create,
        push_slots_update=lambda: None,
        # The materialiser wraps creation + begin_slot_construction in this to
        # defer the creation broadcast; the stub's push is already a no-op, so a
        # nullcontext models it faithfully enough for the paths these tests
        # exercise (the deferred-broadcast/filter interaction is covered on a real
        # DashboardState in test_resume_publishes_hydrated_slot).
        suspend_slots_push=lambda: contextlib.nullcontext(),
        # The live SessionManager surface the Layer B path threads through.
        sessions=SimpleNamespace(
            seed_conversation=lambda *a, **k: None,
            forget_conversation=lambda _k: "",
            resumable_sid=lambda _k: None,
            set_autocompact_pct=lambda *a, **k: None,
        ),
    )
    state.live_slot_count = lambda: len(state._slots) + len(state._slots_under_construction)
    state.begin_slot_construction = state._slots_under_construction.add
    state.end_slot_construction = state._slots_under_construction.discard

    # The folder store, modelled because arrival-provenance filing writes to it
    # on EVERY import (``arrival_folders``). Omitting it would not make these
    # tests fail — the filing is best-effort and swallows the AttributeError —
    # which is exactly why it is here: without it every import test would cover
    # the unfiled fallback and none would cover the shipped path.
    #
    # Mirrors DashboardState.mutate_folders' contract: the mutator runs against
    # the LIVE list and returns ``(changed, value)``, ``on_committed`` fires only
    # when something changed, and the caller gets ``value``.
    async def _mutate_folders(mutate, on_committed=None):
        changed, value = mutate(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read_folders(read):
        return read(state._folders)

    state.mutate_folders = _mutate_folders
    state.read_folders = _read_folders

    # Seed with the first slot the handler will mint, so tests that read
    # ``state._imported_slot`` before the call still resolve; _get_or_create
    # rebinds it to the real minted slot when the handler runs.
    state._imported_slot = _ChatSlot("imported-1")
    return state


async def _run_import(
    st, monkeypatch, body, *, return_slot=False, created=None, save=None, gz=None, state=None
):
    state = state if state is not None else _stub_state(st, monkeypatch, save=save)
    if created is not None:
        from kiro_crew.dashboard.chat_handlers import _ChatSlot

        def _get_or_create(name=None, *_a, **kwargs):
            created.clear()
            created.update(kwargs)
            if name is not None:
                created["name"] = name
            s = _ChatSlot(name or "imported-1", agent=kwargs.get("agent", ""))
            state._slots[s.key] = s
            state._imported_slot = s
            return s

        state.get_or_create_slot = _get_or_create
    resp = await st.api_chat_slot_import(_make_request(state, body, gz=gz))
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

    On Windows the POSIX mode bits are a no-op, so the owner-only DACL applied by
    ``atomic_write(restrict_to_owner=True)`` is the only thing making the file
    owner-only -- if it fails, the context would be readable by other local
    accounts on a shared machine. Refusing costs only resume fidelity (the import
    lands transcript-only), so it is the cheaper side of the trade. Both files
    must go: the pair is useless alone and the ``.json`` carries context too.

    The lockdown seam lives inside ``atomic_write`` now (it locks the temp file
    down BEFORE any content reaches it), so the failure is injected
    at ``platform_compat.restrict_to_owner`` -- the module-level function the
    helper calls -- not at a name in this module.
    """
    from kiro_crew import platform_compat
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    def _refuse(_path):
        raise OSError("cannot resolve the invoking user's SID")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _refuse)

    got = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert got is None, "returned a sid for context it could not protect"
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"left unprotected context on disk: {leftovers}"


def test_layer_b_lockdown_precedes_content(monkeypatch, tmp_path):
    """The context window must never exist in a file that has not been locked
    down yet.

    On Windows the POSIX mode bits are a no-op, so the owner-only DACL is the
    only protection; applying it after the rename left Layer B readable under
    the inherited ACL for the write window. Asserted by measuring
    each file's SIZE at the moment its lockdown is applied — zero means no
    payload byte existed yet. A post-write stat passes on the buggy ordering
    too, so it would not be a regression test.
    """
    from kiro_crew import platform_compat
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    sizes: list[int] = []
    real_restrict = platform_compat.restrict_to_owner

    def _measuring_restrict(target):
        sizes.append(os.stat(target).st_size)
        return real_restrict(target)

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _measuring_restrict)

    got = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert got is not None
    assert len(sizes) == 2, f"expected one lockdown per file of the pair: {sizes}"
    assert sizes == [0, 0], f"a file already held payload bytes when it was locked down: {sizes}"


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
    sig = _THINKING_ENVELOPE["session_state"]["conversation_metadata"]["user_turn_metadatas"][0][
        "result"
    ]["Ok"]["content"][0]["data"]["signature"]

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
async def test_a_refusal_cleans_layer_b_when_the_delete_left_no_replacement(monkeypatch):
    """ABSENT is not REPLACED, and ``dict.get`` answers ``None`` for both.

    The ordinary permanent delete pops by key and puts nothing back, so this
    import's own Layer B pair is nobody else's -- and the delete does not unwind
    these module-local helpers. Folding the absent case into the replaced one
    orphans a ``.json``, a ``.jsonl`` and a join for a session with no tab, and
    nothing re-cleans it: this refusal return is terminal and re-arms nothing.

    Mutation-checked: collapsing the two cases back into one ``else`` leaves
    ``unlinked`` empty here while the replacement test below still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    # The join this import wrote, so the map names THIS import's sid. Load-bearing:
    # the forget is scoped by equality against it.
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_with_no_replacement(_state, slot):
        # What the permanent delete does: pop by key, put nothing back.
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_with_no_replacement)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert forgotten, "the join must be dropped -- with no replacement it is this import's own"
    assert unlinked == ["lb-sid"], (
        "this import's own Layer B pair must be removed; nothing else will, so "
        f"leaving it orphans files in the shared store: {unlinked}"
    )


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_replacement_s_layer_b_alone(monkeypatch):
    """The other half, and the reason the guard exists at all.

    A DIFFERENT object holding the key is a session somebody else created. Its
    slot, its join and its files are its own writer's, so a refusal touches none
    of them -- the case that makes a bare pop-by-key wrong.

    Mutation-checked: widening the cleanup to fire whenever the key is not this
    object unlinks the replacement's files here, while the absent test above
    still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)
    replacement = SimpleNamespace(key="", folder_id="")

    def _deleted_then_recreated(_state, slot):
        replacement.key = slot.key
        state._slots[slot.key] = replacement
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_then_recreated)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert unlinked == [], f"a replacement's files must survive: {unlinked}"
    assert forgotten == [], "a replacement's join mapping must survive"
    assert (
        state._slots.get(replacement.key) is replacement
    ), "the replacement's own slot must still be registered"


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_foreign_join_mapping_alone(monkeypatch):
    """An ABSENT key does not make the mapping at that key this import's.

    A replacement can land at the same key, register its OWN join, and be popped
    again before the object read -- which leaves ``current is None`` while the
    mapping belongs to that replacement. Forgetting by key alone would drop its
    mapping and its continuable mark, and nothing re-arms: this refusal return is
    terminal. The object comparison cannot separate this from a plain delete, so
    the forget is scoped by equality against this import's own sid instead.

    Mutation-checked: dropping the guard, or weakening it from an equality to a
    presence test, forgets the foreign mapping here while the own-sid test above
    still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    # A replacement's join occupies the key: present, and NOT this import's sid.
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "replacement-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_then_replaced_then_popped(_state, slot):
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_then_replaced_then_popped)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert forgotten == [], (
        "a mapping naming another session's sid must survive -- forgetting it "
        f"drops that session's resume context with nothing to re-arm: {forgotten}"
    )
    # The other half, in the same call: scoping the forget must not disable the
    # cleanup this import genuinely owes for its own files.
    assert unlinked == [
        "lb-sid"
    ], f"this import's own Layer B pair must still be removed: {unlinked}"


@pytest.mark.asyncio
async def test_a_refusal_without_layer_b_forgets_no_mapping(monkeypatch):
    """Holding no Layer B of its own, this import has no mapping to drop.

    The bundle carries no Layer B, so there is no join this import wrote. Any
    mapping sitting at the key is therefore somebody else's by construction, and
    a forget here could only ever take a foreign one.

    Mutation-checked: dropping the guard forgets the squatting mapping here.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "squatter-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_with_no_replacement(_state, slot):
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_with_no_replacement)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 409, resp.body
    assert forgotten == [], f"an import holding no Layer B must drop no mapping: {forgotten}"
    assert unlinked == [], f"and must unlink no files: {unlinked}"


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


# ── install from a FILE: the body format ─────────────────────────────────


@pytest.mark.asyncio
async def test_import_accepts_the_exact_bytes_the_export_endpoint_writes(monkeypatch):
    """The acceptance bar: an exported file installs with no step in between.

    Compressed with the EXPORT MODULE'S OWN serialiser, not a local
    ``gzip.compress`` that happens to look similar — the defect this closes was
    precisely that the two halves of one product disagreed about a format, so a
    test that re-implements the sending half could pass while the endpoint still
    refuses the real file. ``GET .../export`` answers ``application/gzip``; the
    importer read ``request.json()`` and rejected those bytes as malformed JSON.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    resp = await _run_import(st, monkeypatch, None, gz=gzip_bundle(_valid()))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["messages"] == 1


@pytest.mark.asyncio
async def test_import_still_accepts_a_plain_json_body(monkeypatch):
    """The tunnel's server-to-server caller posts uncompressed JSON and must not
    break: the sender is an independently-updated install, so a receiver that
    started demanding gzip would refuse every peer that has not shipped this."""
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid())

    assert resp.status == 200, resp.body


@pytest.mark.asyncio
async def test_import_sniffs_the_magic_and_not_the_content_type(monkeypatch):
    """A browser uploading a ``.gz`` off disk sends whatever its platform guesses
    for the type — often ``application/octet-stream``, sometimes nothing. The
    stub request carries NO content type at all, so a handler that branched on
    the header could not reach the gzip path this asserts."""
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    request = _make_request(_stub_state(st, monkeypatch), None, gz=gzip_bundle(_valid()))
    assert not hasattr(request, "content_type")

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body


@pytest.mark.asyncio
async def test_import_refuses_a_corrupt_gzip_with_its_own_code(monkeypatch):
    """A truncated upload gets a code of its own, not the bad-JSON one.

    The sender needs to know its FILE did not survive the trip; told
    ``transfer_invalid_json`` it would go looking for a syntax error in a
    document it never wrote by hand.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    truncated = gzip_bundle(_valid())[: len(gzip_bundle(_valid())) // 2]
    resp = await _run_import(st, monkeypatch, None, gz=truncated)

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_invalid_gzip"


@pytest.mark.asyncio
async def test_import_still_refuses_plain_garbage_as_bad_json(monkeypatch):
    """Bytes that are neither gzip nor JSON keep the code they always had."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, None, raw="{not json"))

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_invalid_json"


def test_gunzip_refuses_a_bomb_while_it_is_still_small(monkeypatch):
    """The cap bounds the ALLOCATION, not the result.

    A ``gzip.decompress`` followed by a ``len()`` check ALSO refuses an oversized
    body — after allocating every byte of it, which on a compression bomb is the
    whole attack. So "it was refused" proves nothing on its own. What is asserted
    here is the quantity actually held when the refusal fires: at most one chunk
    past the cap. Replace the incremental loop with decompress-then-measure and
    this reddens, because the reported size becomes the full expansion.
    """
    import gzip

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_MAX_DECOMPRESSED_BYTES", 4096)
    monkeypatch.setattr(st, "_CHUNK_BYTES", 1024)
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    assert len(bomb) < 64 * 1024, "the point of the fixture is that it is tiny"

    with pytest.raises(st._BundleTooLarge) as caught:
        st._gunzip_bounded(bomb)

    held = caught.value.args[0]
    assert held <= 4096 + 1024, f"held {held} bytes before refusing"


@pytest.mark.asyncio
async def test_import_refuses_an_oversized_compressed_body(monkeypatch):
    """End to end: the bound is wired to a coded refusal, not only to a helper."""
    import gzip

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_MAX_DECOMPRESSED_BYTES", 4096)
    resp = await _run_import(st, monkeypatch, None, gz=gzip.compress(b"\0" * (1024 * 1024)))

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_bundle_too_large"


def test_the_decompression_cap_never_makes_gzip_stricter_than_plain_json():
    """The property that makes the cap safe, not the arithmetic behind it.

    ``client_max_size`` (60 MiB) bounds EVERY body, compressed or not, so the
    plain path can never deliver more than that much JSON. As long as the
    decompressed ceiling is above it, the gzip path accepts strictly more than the
    plain path ever could -- which is what makes "a bundle this refuses" a bundle
    that was already unimportable by the only route that existed before.

    Pinned rather than argued, because the arithmetic reads as though the cap
    tracks the validator's CHARACTER ceilings, and a character ceiling is not a
    byte ceiling: ``ensure_ascii`` renders one non-ASCII char as six bytes. The
    number moving with those ceilings is a convenience; this comparison is the
    guarantee.
    """
    from kiro_crew.dashboard import session_transfer as st

    assert st._MAX_DECOMPRESSED_BYTES > st._GATEWAY_CLIENT_MAX_SIZE
    # And the magnitude still comes from the validator, so the two move together.
    assert st._MAX_DECOMPRESSED_BYTES == (
        st._MAX_TOTAL_CHARS + st._MAX_LAYER_B_CHARS + st._JSON_ENVELOPE_SLACK
    )


def test_the_stated_gateway_body_limit_matches_the_gateway():
    """The restated constant has to be the real one, or the test above proves
    nothing. Read out of the server module rather than trusted."""
    from pathlib import Path

    import kiro_crew.dashboard.server as server_mod
    from kiro_crew.dashboard import session_transfer as st

    source = Path(server_mod.__file__).read_text(encoding="utf-8")
    assert "client_max_size=60 * 1024 * 1024" in source
    assert st._GATEWAY_CLIENT_MAX_SIZE == 60 * 1024 * 1024


# ── install from a FILE: the round trip ──────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_expansions_are_bounded_and_the_excess_is_refused(monkeypatch):
    """The per-body cap bounds ONE request; the sum is what exhausts a host.

    Without a concurrency bound, N authenticated requests each hold up to the
    ceiling at the same time. Asserted on the OBSERVED simultaneity, not on the
    presence of a semaphore: the gunzip is replaced with one that records how
    many are inside it at once.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    monkeypatch.setattr(st, "_expansion_lock", None)
    monkeypatch.setattr(st, "_expansion_slots", None)
    monkeypatch.setattr(st, "_expansion_waiting", 0)

    inside = {"now": 0, "peak": 0}
    release = asyncio.Event()

    async def _slow_to_thread(fn, *args):
        if fn is st._gunzip_bounded:
            inside["now"] += 1
            inside["peak"] = max(inside["peak"], inside["now"])
            await release.wait()
            inside["now"] -= 1
            return fn(*args)
        return fn(*args)

    monkeypatch.setattr(st.asyncio, "to_thread", _slow_to_thread)
    gz = gzip_bundle(_valid())

    # More than the queue allows, all in flight together.
    running = [
        asyncio.create_task(_run_import(st, monkeypatch, None, gz=gz))
        for _ in range(st._MAX_CONCURRENT_EXPANSIONS + st._MAX_QUEUED_EXPANSIONS + 2)
    ]
    await asyncio.sleep(0.05)
    refused = [t for t in running if t.done()]
    release.set()
    results = await asyncio.gather(*running)

    assert inside["peak"] <= st._MAX_CONCURRENT_EXPANSIONS, inside
    assert refused, "the excess must be refused immediately, not parked"
    busy = [r for r in results if r.status == 429]
    assert busy, [r.status for r in results]
    assert json.loads(busy[0].body)["code"] == "transfer_expansion_busy"


@pytest.mark.asyncio
async def test_the_expansion_permit_outlives_the_decompression(monkeypatch):
    """A permit that ends at the gunzip bounds the CPU, not the memory.

    A decompressed bundle stays resident -- first as bytes, then as the parsed
    document -- through redaction and persistence, so what has to be bounded is
    how many are resident AT ONCE, not how many are inflating at once. Measured
    inside the post-decompression pass rather than at the gunzip: with the permit
    released when the body has been read, every admitted caller frees its slot
    immediately and they all pile into redaction together, which is precisely the
    sum the bound exists to prevent.
    """
    from kiro_crew.dashboard import chat_handlers as ch
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    monkeypatch.setattr(st, "_expansion_lock", None)
    monkeypatch.setattr(st, "_expansion_slots", None)
    monkeypatch.setattr(st, "_expansion_waiting", 0)

    resident = {"now": 0, "peak": 0}
    release = asyncio.Event()

    async def _park_in_redaction(fn, *args):
        if fn is ch._redact_history_rows:
            resident["now"] += 1
            resident["peak"] = max(resident["peak"], resident["now"])
            await release.wait()
            resident["now"] -= 1
        return fn(*args)

    monkeypatch.setattr(st.asyncio, "to_thread", _park_in_redaction)
    gz = gzip_bundle(_valid())

    running = [
        asyncio.create_task(_run_import(st, monkeypatch, None, gz=gz))
        for _ in range(st._MAX_CONCURRENT_EXPANSIONS + st._MAX_QUEUED_EXPANSIONS + 2)
    ]
    await asyncio.sleep(0.05)
    peak_while_parked = resident["peak"]
    release.set()
    await asyncio.gather(*running)

    assert peak_while_parked, "no caller reached redaction; the harness missed the pass"
    assert peak_while_parked <= st._MAX_CONCURRENT_EXPANSIONS, resident


@pytest.mark.asyncio
async def test_the_transcript_only_mark_reads_layer_b_skipped_not_absence(monkeypatch):
    """A file bundle MAY carry Layer B and usually will not — so the mark is
    driven by the sender's explicit signal, never inferred from absence.

    Three cases, because inferring from absence gets two of them wrong:

    * no Layer B and no signal — a session that never HAD a kiro-cli context.
      Nothing was lost, so nothing is marked. Inference would mark it.
    * no Layer B, ``layer_b_skipped`` set — context existed and was withheld
      (the export's default-off posture, or a mid-turn source). Marked.
    * Layer B present — full fidelity, not marked.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.session_export import gzip_bundle

    never_had = await _run_import(st, monkeypatch, None, gz=gzip_bundle(_valid()), return_slot=True)
    assert "transcript only" not in never_had.title

    withheld = await _run_import(
        st,
        monkeypatch,
        None,
        gz=gzip_bundle(_valid(layer_b_skipped=True)),
        return_slot=True,
    )
    assert "transcript only" in withheld.title


# ── the round trip, over the real handlers ───────────────────────────────


@pytest.mark.asyncio
async def test_an_exported_file_installs_with_no_step_in_between(tmp_path):
    """The two halves of the feature, wired to each other over HTTP.

    Every other test here stubs one side. This one stubs NEITHER: a real
    ``DashboardState``, the real ``api_chat_slot_export``, the real
    ``api_chat_slot_import``. It is the test whose absence let the defect ship —
    each half was covered, the PAIR was not, and the pair is where they disagreed
    about a format.

    Also covers, in one pass, the two exit criteria that are about repetition
    rather than about a single install: the same file installed twice yields TWO
    separate sessions, and a plain-JSON body keeps working alongside gzip so the
    tunnel is unregressed.
    """
    import gzip

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.session_export import api_chat_slot_export
    from kiro_crew.dashboard.session_transfer import api_chat_slot_import

    state = _make_state(tmp_path)
    # Make the SOURCE session auto-approving, so the exported file genuinely
    # RECORDS `approval_policy: "auto"`. Exit criterion: an installed session
    # lands interactive regardless of what its source record says — asserted
    # below, and vacuous unless the file actually carries the dangerous value.
    state.sessions.has_session.return_value = True
    state.sessions.get_approval_policy.return_value = "auto"
    app = web.Application(client_max_size=60 * 1024 * 1024)
    app["state"] = state
    app.router.add_post("/api/chat/slots/import", api_chat_slot_import)
    app.router.add_get("/api/chat/slots/{slot}/export", api_chat_slot_export)

    async with TestClient(TestServer(app)) as client:
        body = json.dumps(_valid(origin="seedbox", title="round trip")).encode()

        # Seed one session the way the tunnel does: an uncompressed JSON body.
        seeded = await client.post(
            "/api/chat/slots/import", data=body, headers={"Content-Type": "application/json"}
        )
        assert seeded.status == 200, await seeded.text()
        seeded_key = (await seeded.json())["key"]

        # Export it. These are the bytes a user is handed.
        exported = await client.get(f"/api/chat/slots/{seeded_key}/export")
        assert exported.status == 200, await exported.text()
        assert exported.headers["Content-Type"] == "application/gzip"
        gz = await exported.read()
        assert gz[:2] == b"\x1f\x8b"
        # The file really does record auto-approval, so the interactive-on-arrival
        # assertion at the end is measuring something. Without this the mock could
        # stop reporting a policy and that assertion would still pass.
        assert json.loads(gzip.decompress(gz))["source"]["approval_policy"] == "auto"

        # Install them UNCHANGED. No gunzip, no re-encode, no manual step. This
        # is the assertion the whole change exists for.
        first = await client.post(
            "/api/chat/slots/import",
            data=gz,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert first.status == 200, await first.text()
        first_key = (await first.json())["key"]

        # The same file again: another session, not a refusal and not a merge.
        second = await client.post(
            "/api/chat/slots/import",
            data=gz,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert second.status == 200, await second.text()
        second_key = (await second.json())["key"]
        assert second_key != first_key

        # And the tunnel's plain body still lands, next to them.
        tunnel_again = await client.post(
            "/api/chat/slots/import", data=body, headers={"Content-Type": "application/json"}
        )
        assert tunnel_again.status == 200, await tunnel_again.text()

    # The same file installed twice is TWO sessions, not one replaced: install
    # only ever adds, so a second install of one file cannot reach the first.
    assert first_key != second_key, (first_key, second_key)
    assert first_key in state._slots and second_key in state._slots

    # An installed session lands INTERACTIVE, whatever the source recorded: the
    # materialiser applies a fixed field set that does not include
    # approval_policy, so there is no path by which a file can arrive
    # pre-approved to run tools.
    for key in (first_key, second_key):
        assert getattr(state._slots[key], "approval_policy", "") == ""


@pytest.mark.asyncio
async def test_a_body_past_the_server_limit_is_told_it_is_too_large(tmp_path):
    """A body the server refuses by SIZE gets the size answer, not the generic one.

    ``request.read()`` raises ``HTTPRequestEntityTooLarge`` once the body passes
    the Application's ``client_max_size``, which is the one read failure whose
    cause the server KNOWS. Catching it with everything else would answer
    ``transfer_body_unreadable`` — copy that hedges between "too large" and "the
    connection dropped" — and send a person whose file is simply too big looking
    for a network fault. The limit is set small here so the assertion is about
    the branch and not about moving 60 MiB.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.session_transfer import api_chat_slot_import

    app = web.Application(client_max_size=1024)
    app["state"] = _make_state(tmp_path)
    app.router.add_post("/api/chat/slots/import", api_chat_slot_import)

    async with TestClient(TestServer(app)) as client:
        oversized = await client.post(
            "/api/chat/slots/import",
            data=b"x" * 4096,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert oversized.status == 400, await oversized.text()
        payload = await oversized.json()

    assert payload["code"] == "transfer_bundle_too_large", payload
    assert "size limit" in payload["error"], payload


# ── arrival provenance filing ────────────────────────────────────────────
#
# docs/request-for-change/rfc-arrival-provenance-filing.md. The unit-level
# behaviour of the folder resolution lives in test/test_arrival_folders.py; these
# pin what the HANDLER does with it, which is where the two properties that matter
# most are observable: both arrival routes file, and placement is resolved late
# enough that a refusal cannot leave a folder behind.


def _folder_names(state):
    """``{name: parent_id}`` for the stub store, for readable assertions."""
    return {str(f["name"]): str(f.get("parent_id", "")) for f in state._folders}


def _filed_folder(state):
    """The folder the imported slot was filed into, as a name; "" when unfiled."""
    slot = state._imported_slot
    fid = getattr(slot, "folder_id", "")
    if not fid:
        return ""
    return next((str(f["name"]) for f in state._folders if str(f["id"]) == fid), "<dangling>")


@pytest.mark.asyncio
async def test_a_plain_json_arrival_is_filed_under_imported_from_sender(monkeypatch):
    """The TUNNEL shape: a peer posts a plain-JSON bundle.

    This is the changed default the RFC exists for — a peer-pushed session used
    to land at the top level.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert _folder_names(state) == {
        "Imported": "",
        "from mac": next(f["id"] for f in state._folders if f["name"] == "Imported"),
    }
    assert _filed_folder(state) == "from mac"


@pytest.mark.asyncio
async def test_a_gzipped_arrival_is_filed_exactly_like_the_plain_one(monkeypatch):
    """The FILE shape, and the property the RFC's §3 turns on: the destination
    must not depend on the body's format.

    A gzip body and a plain body carrying the same ``origin`` must reach the same
    folder. Asserted by importing both into one store and requiring ONE pair of
    folders plus one shared child id — a per-format destination would show up as
    two children or two groups.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    plain = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert plain.status == 200, plain.body
    first = state._imported_slot.folder_id

    from kiro_crew.dashboard.session_export import gzip_bundle

    gz = gzip_bundle(_valid(origin="mac"))
    second_resp = await st.api_chat_slot_import(_make_request(state, None, gz=gz))
    assert second_resp.status == 200, second_resp.body
    second = state._imported_slot.folder_id

    assert first and second and first == second
    assert sorted(_folder_names(state)) == ["Imported", "from mac"]


@pytest.mark.asyncio
async def test_an_app_scoped_arrival_lands_unfiled_and_creates_no_folder(monkeypatch):
    """RFC §5.1. The folder store has a global ceiling, so an app token that
    could create a folder per arrival could loop imports with distinct origins
    until the person is refused a folder of their own."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    request = _make_request(state, _valid(origin="mac"))
    request.get = lambda k, default="": "some-app" if k == "app" else default

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body
    assert state._folders == []
    assert _filed_folder(state) == ""


@pytest.mark.asyncio
async def test_filing_identity_comes_from_the_shared_caller_rule(monkeypatch):
    """RFC §5.1's second half: identity from ``effective_request_app``, never
    from ``request.get("app")`` alone.

    The internal-secret transport publishes no app claim, so the shared rule
    DERIVES one from the calling session's key. A handler reading only the claim
    sees the dashboard owner here and would create a folder for an app caller —
    which is the ceiling loop §5.1 refuses. Mutation-checked: swapping the
    handler's ``effective_request_app`` back to ``request.get("app", "")`` files
    this arrival and reddens the assertion below.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    state = _stub_state(st, monkeypatch)
    # A slot an app owns, named by the caller's own session key. Nothing about
    # the request carries an app claim.
    caller = _ChatSlot("app-caller")
    caller._app = "some-app"
    state._slots["app-caller"] = caller

    request = _make_request(state, _valid(origin="mac"))
    request.headers = {"X-Session-Key": "dashboard:app-caller"}
    assert request.get("app", "") == "", "the premise: no app claim on the request"

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body
    assert state._folders == []
    assert _filed_folder(state) == ""


@pytest.mark.asyncio
async def test_a_second_arrival_from_one_sender_lands_beside_the_first(monkeypatch):
    """The RFC's first acceptance bullet: not in a second folder of the same
    name."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    first_id = state._imported_slot.folder_id

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert second.status == 200, second.body
    second_id = state._imported_slot.folder_id

    assert first_id == second_id
    assert len(state._folders) == 2, _folder_names(state)


@pytest.mark.asyncio
async def test_a_landed_arrival_records_the_row_it_shares(monkeypatch):
    """The call site a failed creator's rollback depends on.

    Written on the landed path, immediately above the final witness, and only for
    a row the filing ADOPTED. The first import CREATED its rows, so no other
    import holds those ids and no other rollback can reach them: it records
    nothing. The second ADOPTED, so its landing is what stops the first one's
    rollback deleting a placement this session points at once it archives and goes
    invisible to the live-slot check.

    Destination only. The second import adopted the GROUP as well, and that row is
    deliberately left unmarked: the rollback's second guard already spares a row
    whose child survived.

    Mutation-checked: dropping the call reddens the shared-row assertion, and
    dropping the ``not in created_folders`` condition reddens the first import's.
    """
    from kiro_crew.dashboard import arrival_folders as af
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id
    assert not any(bool(f.get(af.ARRIVAL_ADOPTED_KEY)) for f in state._folders), (
        "an import that CREATED its rows records nothing: no other import holds "
        "those ids, so no other rollback can reach them"
    )

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert second.status == 200, second.body
    assert state._imported_slot.folder_id == shared_id, "the premise: the second adopted the row"

    rows = {str(f["id"]): f for f in state._folders}
    group_id = next(str(f["id"]) for f in state._folders if str(f["name"]) == "Imported")
    assert rows[shared_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "a landed arrival that adopted its destination must record it, or the "
        "rollback of whichever import created that row deletes a placement this "
        "session still points at"
    )
    assert not rows[group_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "the adopted PARENT is left unmarked on purpose -- a row whose child "
        "survives is already spared by the rollback's second guard"
    )


@pytest.mark.asyncio
async def test_a_delete_landing_in_the_shared_row_mark_refuses(monkeypatch):
    """Why the shared-row mark sits ABOVE the final witness rather than below it.

    ``mark_arrival_folder_shared`` is the last await on the path, so a
    ``DELETE /api/sessions/{key}`` can land inside it. Below the witness that
    delete arrives past the last check: it removes the transcript and pops the
    slot, and the handler still answers ``200 ok``, with nothing downstream to
    correct it -- the success return does not re-arm ``_dirty`` and the delete's
    pop is terminal. Above the witness the same delete is caught and the request
    refuses.

    The delete is injected BY the mark, so the witness can only observe it if the
    witness runs afterwards. That states the ordering as behaviour rather than as
    a line number, which a rearrangement cannot satisfy by accident.

    Mutation-checked: moving the mark back below the witness makes this read 200,
    and so does dropping the witness call.
    """
    from kiro_crew.dashboard import arrival_folders as af
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    deleted: list[str] = []
    real_mark = st.mark_arrival_folder_shared

    async def _mark_then_delete(mark_state, folder_id, **_kw):
        recorded = await real_mark(mark_state, folder_id)
        # The delete lands INSIDE this await, which is the whole point of where
        # the call sits: a witness above it cannot see this.
        deleted.append(str(folder_id))
        # Report what the real mark reported, so this exercises the ORDERING and
        # not the separate unfiling branch a failed mark takes.
        return recorded

    monkeypatch.setattr(st, "mark_arrival_folder_shared", _mark_then_delete)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: bool(deleted))

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert deleted, "the premise: the second import adopted a row and marked it"
    assert second.status == 409, (
        "a delete landing in the shared-row mark must refuse -- below the witness "
        "that await yields the loop past the last check and the handler publishes "
        "ok for a transcript the delete has already removed"
    )
    assert json.loads(second.body).get("code", "").startswith("transfer_import_deleted")
    rows = {str(f["id"]): f for f in state._folders}
    assert rows[shared_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "the mark stands through the refusal: taking one back needs each import's "
        "own claim recorded on the row, so this leaves one visible deletable row "
        "rather than stripping a claim that may belong to another import"
    )


@pytest.mark.asyncio
async def test_a_mark_that_cannot_be_recorded_unfiles_the_session(monkeypatch):
    """What an unrecorded mark owes, and why reporting it is not enough.

    The mark is the ONLY thing sparing an adopted row once this session archives:
    the rollback's occupancy guard reads live slots, and an archived session is
    popped out of that mapping. So a mark that never landed means a concurrent
    creator's rollback can reclaim the row while this transcript still points at
    it, and the person is left filed into a folder that does not exist.

    The remedy is to unfile, which is the state the folder-gone repair already
    produces -- cleared and persisted, rather than a dangling id left behind.

    Mutation-checked: making the mark's failure path report success, or dropping
    the unfiling branch, both leave the stale ``folder_id`` on the slot.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    async def _mark_fails(_state, _folder_id, **_kw):
        return False

    monkeypatch.setattr(st, "mark_arrival_folder_shared", _mark_fails)

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert second.status == 200, second.body
    assert state._imported_slot.folder_id == "", (
        "an unrecorded mark must unfile the session: leaving the id set points the "
        "transcript at a row a concurrent creator's rollback can reclaim"
    )
    assert shared_id, "the premise: the first import created a row to adopt"


@pytest.mark.asyncio
async def test_a_recorded_mark_leaves_the_session_filed(monkeypatch):
    """The companion. Without it the unfiling could fire unconditionally and this
    would still look correct, because an unfiled session is also a valid state."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert second.status == 200, second.body
    assert (
        state._imported_slot.folder_id == shared_id
    ), "a mark that was recorded must leave the session filed where it landed"


@pytest.mark.asyncio
async def test_two_senders_get_two_children_under_one_group(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    for origin in ("mac", "linux-desk"):
        resp = await st.api_chat_slot_import(_make_request(state, _valid(origin=origin)))
        assert resp.status == 200, resp.body

    group_id = next(f["id"] for f in state._folders if f["name"] == "Imported")
    assert _folder_names(state) == {
        "Imported": "",
        "from mac": group_id,
        "from linux-desk": group_id,
    }


@pytest.mark.asyncio
async def test_a_bundle_with_no_sender_is_filed_under_the_group(monkeypatch):
    """A missing ``origin`` names no peer, so inventing "from unknown" would make
    an absent field look like one."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="")))

    assert resp.status == 200, resp.body
    assert _folder_names(state) == {"Imported": ""}
    assert _filed_folder(state) == "Imported"


@pytest.mark.asyncio
async def test_placement_is_resolved_after_the_slot_exists(monkeypatch):
    """RFC §5.2. The post-await slot-cap re-check can answer 429, and a folder
    written in FRONT of it is left behind when it fires — the same folder-store
    exhaustion with a narrower trigger.

    The cap is made to bite only AFTER the handler's early check, which is the
    shape a real concurrent burst produces, and the assertion is that the refused
    import left the store empty. Mutation-checked: moving the filing call up to
    the ``meta`` construction reddens this.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        # First call is the handler's early check (must pass); every later call is
        # the post-await re-check (must refuse).
        return 0 if calls["n"] == 1 else st.MAX_LIVE_SLOTS

    state.live_slot_count = _count
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 429, resp.body
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"
    assert state._folders == [], "a refused import must leave no folder behind"


@pytest.mark.asyncio
async def test_a_folder_deleted_during_finalisation_leaves_no_dangling_id(monkeypatch):
    """RFC §5.5, the folder-lifecycle defect that is hardest to see from the code.

    For the whole finalisation stretch the slot is RETRACTED from
    ``state._slots``, and that mapping is what the folder delete handler's unfile
    sweep iterates — so a delete committing here cannot see the session to unfile
    it. The repair after re-registration is what closes it. Mutation-checked:
    removing the post-registration re-check leaves ``folder_id`` pointing at a
    row that is gone, which this test reads as ``<dangling>``.
    """
    from kiro_crew.dashboard import session_transfer as st

    saved: list[str] = []

    async def _save_then_delete(_state, slot, *_a, **_k):
        saved.append(getattr(slot, "folder_id", ""))
        # The person deletes the folder while the durable save is in flight.
        state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_save_then_delete)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert saved and saved[0], "the premise: the save carried a folder_id"
    assert state._imported_slot.folder_id == ""
    assert _filed_folder(state) == ""


@pytest.mark.asyncio
async def test_a_refused_repair_save_is_not_reported_as_a_landed_import(monkeypatch):
    """A refusal is not a commit.

    ``save_slot_off_loop`` turns an exception into ``True`` under best-effort,
    but it returns ``False`` CLEANLY when the delete-won guard fires, i.e. this
    session's transcript was deleted while the import was finishing. That
    ``False`` is terminal: the guard returns cleanly so the flush clears
    ``_dirty`` and the delete stands, so nothing re-arms and no later flush
    corrects a reported success. Ignoring it answered ``ok: true`` for a session
    whose file is gone and left the slot published as a zombie.

    Mutation-checked: dropping the ``if not`` around the repair save makes this
    read 200 with ``ok: true`` and the slot still in ``_slots``.
    """
    from kiro_crew.dashboard import session_transfer as st

    calls: list[bool] = []
    pushes: list[int] = []

    async def _save_then_lose_to_a_delete(_state, _slot, *_a, **_k):
        first = not calls
        calls.append(True)
        if first:
            # The durable save lands, and the person deletes the folder while it
            # is in flight -- which is what arms the repair below.
            state._folders.clear()
            return True
        # The repair save now meets the delete-won guard: this session's file was
        # removed, so the write is refused cleanly rather than raising.
        return False

    state = _stub_state(st, monkeypatch, save=_save_then_lose_to_a_delete)
    state.push_slots_update = lambda: pushes.append(1)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert len(calls) == 2, "the premise: the repair save actually ran"
    assert resp.status == 409, resp.body
    body = json.loads(resp.body)
    assert body.get("code") == "transfer_import_deleted"
    assert "ok" not in body, "a refused import must not answer ok at all"
    assert key not in state._slots, "the zombie slot must not stay registered"
    assert pushes == [], "a refused import must not publish the slot as landed"


@pytest.mark.asyncio
async def test_the_finalisation_saves_are_pinned_to_this_slot(monkeypatch):
    """Why the synchronous identity check is not enough on its own.

    Both finalisation saves check ``state._slots.get(slot.key) is slot`` and then
    await. The executor wait frees the event loop, so a close landing in that gap
    pops the slot and persists ``closed=True`` while the in-flight save still holds
    ``closed=False`` in memory. ``chat_persistence`` has the atomic answer -- it
    re-reads the map at the commit boundary with no await before the write -- but
    ONLY when ``expected_slot_name`` is passed; absent it, that recheck is skipped
    entirely and the stale snapshot lands on top of the dismissal.

    Asserted on the ARGUMENT rather than through behaviour on purpose: a stub save
    cannot exercise the real commit-boundary recheck, so a behavioural test here
    would pass with the pin removed.

    Mutation-checked: dropping ``expected_slot_name`` from the repair save reddens
    this.
    """
    from kiro_crew.dashboard import session_transfer as st

    seen: list[dict] = []

    async def _record_then_lose_the_folder(_state, _slot, *_a, **kw):
        seen.append(dict(kw))
        if len(seen) == 1:
            # Arms the repair: the person deletes the folder mid-import.
            state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_record_then_lose_the_folder)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert resp.status == 200, resp.body
    assert len(seen) >= 2, "the premise: the repair save ran after the durable save"
    assert seen[-1].get("expected_slot_name") == key, (
        "the repair save must pin the slot name, or chat_persistence skips its "
        "commit-boundary recheck and a concurrent close can be overwritten"
    )


@pytest.mark.asyncio
async def test_a_close_at_the_repair_saves_commit_boundary_is_not_a_deletion(monkeypatch):
    """The pin's refusal and the delete-won guard both answer ``False``, and they
    need opposite handling.

    A pin refusal means a close or replacement won the map, so the import DID land
    and the close is the person's own later action: skip, and answer success.
    Reporting it as a deletion would claim the transcript was destroyed when it was
    not, and would drop a slot that belongs to somebody else now.

    The sibling above covers the other direction: a ``False`` with this slot still
    in the map is the delete-won guard, which is terminal and answers 409.

    Mutation-checked: treating every ``False`` as a deletion makes this read 409.
    """
    from kiro_crew.dashboard import session_transfer as st

    calls: list[bool] = []

    async def _save_then_lose_the_slot(_state, slot, *_a, **_k):
        first = not calls
        calls.append(True)
        if first:
            state._folders.clear()
            return True
        # The close landed inside this save's executor wait: it popped the slot and
        # persisted its own ``closed=True``, so the commit-boundary recheck refuses
        # this stale write. Nothing was deleted.
        state._slots.pop(slot.key, None)
        return False

    state = _stub_state(st, monkeypatch, save=_save_then_lose_the_slot)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert len(calls) == 2, "the premise: the repair save actually ran"
    assert resp.status == 200, resp.body
    assert body.get("ok") is True, (
        "a pin refusal is not a deletion: the transcript landed, so the import "
        "reports success and leaves the close alone"
    )
    assert (
        "transfer_import_deleted" not in resp.body.decode()
    ), "claiming deletion here would report data loss that did not happen"


@pytest.mark.asyncio
async def test_a_close_during_the_folder_check_is_not_undone_by_the_repair(monkeypatch):
    """The repair save may only write a slot it still owns.

    The folder-existence check awaits, and a close landing inside that await pops
    the slot and THEN persists ``closed=True``. The imported slot object's own
    ``closed`` is still False, so an unguarded repair save writes that flag back
    off: the archived record loses the dismissal and the tab the person closed
    resurfaces, while both requests report success.

    The import itself landed, so this asserts the repair is SKIPPED rather than
    refused -- and that the close's flag survives.

    Mutation-checked two ways: dropping the guard runs the repair save and clears
    the flag, and writing it with ``_slot_still_ours`` polarity (an absent key
    counting as still ours) does exactly the same, because a close pops first.
    """
    from kiro_crew.dashboard import session_transfer as st

    saves: list[str] = []
    archived = {"closed": False}

    async def _count_then_delete_the_folder(_state, slot, *_a, **_k):
        first = not saves
        saves.append(getattr(slot, "folder_id", ""))
        if first:
            # The person deletes the arrival folder while the durable save is in
            # flight, which is what arms the repair below.
            state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_count_then_delete_the_folder)
    real_exists = st.arrival_folder_exists

    async def _a_close_lands_inside_the_check(_state, folder_id):
        answer = await real_exists(_state, folder_id)
        if not answer:
            # The folder is gone, so this is the call that arms the repair. The
            # person closes the tab in the same window: a close pops the slot
            # FIRST, then persists the dismissal.
            slot = state._imported_slot
            state._slots.pop(slot.key, None)
            archived["closed"] = True
        return answer

    monkeypatch.setattr(st, "arrival_folder_exists", _a_close_lands_inside_the_check)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert saves and saves[0], "the premise: the durable save carried a folder_id"
    assert len(saves) == 1, (
        "the repair save must not run once the slot has left _slots -- it would "
        "write this object's closed=False over the close's own record"
    )
    assert archived["closed"] is True, "the person's dismissal must survive"
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True, (
        "the transcript landed and the close is the person's own later action, so "
        "the import is not a failure"
    )


@pytest.mark.asyncio
async def test_a_delete_during_the_folder_check_is_not_reported_as_landed(monkeypatch):
    """The folder-gone guard alone leaves the COMMON case unchecked.

    A ``DELETE /api/sessions/{key}`` landing in the folder-existence await removes
    this transcript and pops the slot while the folder it points at is still
    perfectly fine. The repair branch is keyed on the FOLDER being gone, so it is
    skipped, and without the unconditional witness the handler falls straight
    through to ``ok: true`` for a session that has already been destroyed. Nothing
    corrects it afterwards: the success return does not re-arm ``_dirty`` and the
    delete's pop is terminal.

    The folder is deliberately left INTACT here. That is the whole point -- with
    the folder gone this would pass on the older guard alone and prove nothing.

    Mutation-checked: deleting the witness call makes this read 200 with
    ``ok: true`` and the slot still registered.
    """
    from kiro_crew.dashboard import session_transfer as st

    pushes: list[int] = []
    existed: list[bool] = []
    state = _stub_state(st, monkeypatch)
    state.push_slots_update = lambda: pushes.append(1)

    real_exists = st.arrival_folder_exists

    async def _watch_exists(_state, folder_id):
        answer = await real_exists(_state, folder_id)
        existed.append(answer)
        return answer

    monkeypatch.setattr(st, "arrival_folder_exists", _watch_exists)
    # The folder survives the check; only the SESSION is deleted.
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert existed and existed[-1] is True, (
        "the premise: the folder was still THERE at the repair check, so the "
        "folder-gone branch was not the one that produced this refusal"
    )
    assert resp.status == 409, resp.body
    body = json.loads(resp.body)
    assert body.get("code") == "transfer_import_deleted"
    assert "ok" not in body, "a refused import must not answer ok at all"
    assert key not in state._slots, "the zombie slot must not stay registered"
    assert pushes == [], "a refused import must not publish the slot as landed"


@pytest.mark.asyncio
async def test_the_delete_witness_runs_on_the_ordinary_success_path(monkeypatch):
    """The companion pin, and the one that keeps the guard above from going dead.

    A witness reached only through the folder-gone branch would leave the case
    that finding was about unguarded while every test still passed. So assert it
    is consulted on the plain import too -- folder intact, nothing deleted, 200 --
    which is exactly the path the older code returned ``ok`` on without asking.
    """
    from kiro_crew.dashboard import session_transfer as st

    asked: list[str] = []
    state = _stub_state(st, monkeypatch)

    def _witness(_state, slot):
        asked.append(slot.key)
        return False

    monkeypatch.setattr(st, "session_was_deleted", _witness)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    assert asked == [
        state._imported_slot.key
    ], "the witness must be consulted once on the success path, for THIS slot"


@pytest.mark.asyncio
async def test_the_witness_still_runs_when_the_repair_save_reported_success(monkeypatch):
    """Why the witness is unconditional and not an ``elif`` on the folder branch.

    ``save_slot_off_loop`` returning ``True`` does NOT mean the delete-won guard
    reached a decision: under best-effort it converts a RAISING save to ``True``
    and marks the slot dirty. So a shape that only asks the witness when the
    folder branch was skipped would trust a ``True`` that decided nothing.

    Here the folder IS gone, the repair save reports success the way a swallowed
    exception does, and the session was deleted underneath. An ``elif`` answers
    200; the unconditional check answers 409. This test is the difference.
    """
    from kiro_crew.dashboard import session_transfer as st

    saves: list[bool] = []

    async def _save_then_lose_the_folder(_state, _slot, *_a, **_k):
        first = not saves
        saves.append(True)
        if first:
            # The person deletes the folder while the durable save is in flight,
            # which is what arms the repair branch below.
            state._folders.clear()
        # Both saves report success -- the repair one the way best-effort does
        # when it swallows an exception, having decided nothing.
        return True

    state = _stub_state(st, monkeypatch, save=_save_then_lose_the_folder)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert len(saves) == 2, "the premise: the repair save ran and reported success"
    assert not state._folders, "the premise: the folder branch was the one taken"
    assert resp.status == 409, resp.body
    assert json.loads(resp.body).get("code") == "transfer_import_deleted"


@pytest.mark.asyncio
async def test_a_refused_import_takes_back_the_folder_it_created(monkeypatch):
    """The folder is committed before the transcript's durable save, so a 503
    would otherwise leave an empty row behind for every distinct sender. Nothing
    reclaims it afterwards -- there is no orphan-folder sweep -- so the handler
    unwinds its own rows on the way out, exactly as it unwinds the Layer B files.

    Mutation-checked: dropping the rollback call leaves the folders in the store.
    """
    from kiro_crew.dashboard import session_transfer as st

    async def _save_fails(*_a, **_k):
        raise OSError("sessions dir is not writable")

    state = _stub_state(st, monkeypatch, save=_save_fails)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 503, resp.body
    assert json.loads(resp.body).get("code") == "transfer_import_save_failed"
    assert state._folders == [], "a refused import must not leave its folders behind"


@pytest.mark.asyncio
async def test_a_refusal_discloses_a_transcript_it_cannot_remove(monkeypatch):
    """A refusal may only claim what it achieved.

    The transcript is persisted BEFORE the witness runs, and the witness reports
    "deleted" for three different situations: the file is gone, the file belongs
    to a NEW incarnation, and existence is unverifiable. Only the first makes
    "nothing was kept" true. The other two leave a file this instance must not
    unlink -- a new incarnation is somebody else's session, and an unverifiable
    read names nothing safe to remove -- so the answer discloses it.

    Mutation-checked: answering the plain code regardless makes this read
    ``transfer_import_deleted`` with a body promising nothing was kept.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)
    monkeypatch.setattr(st, "session_transcript_remains", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert resp.status == 409, resp.body
    assert body.get("code") == "transfer_import_deleted_partial"
    assert "remains on disk" in body.get("error", ""), "the leftover must be disclosed"
    assert "nothing was kept" not in body.get(
        "error", ""
    ), "the clean-slate promise is exactly what this case cannot make"


@pytest.mark.asyncio
async def test_a_refusal_still_promises_a_clean_slate_when_the_file_is_gone(monkeypatch):
    """The companion, and what keeps the disclosure from swallowing the plain
    case: when the transcript really is gone, the refusal says so plainly."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)
    monkeypatch.setattr(st, "session_transcript_remains", lambda _s, _slot: False)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert resp.status == 409, resp.body
    assert body.get("code") == "transfer_import_deleted"
    assert "nothing was kept" in body.get("error", "")


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_same_key_replacement_alone(monkeypatch):
    """The refusal must not clean up by key alone.

    The witness fires because THIS slot's session was deleted, and a replacement
    can land at the same key while the finalisation tail runs. Popping by key
    would drop the replacement's slot and forgetting the join by key would take
    its mapping and its files -- silent loss of a session this import never owned.

    Mutation-checked: removing the identity guard pops the replacement.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    state = _stub_state(st, monkeypatch)
    state.sessions.forget_conversation = lambda k: (forgotten.append(k), "")[1]
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    replacement = SimpleNamespace(key="imported-1", folder_id="")

    def _swap(_state, _slot):
        # A replacement takes the key while the tail is running, which is exactly
        # the situation the witness reports as "deleted".
        state._slots["imported-1"] = replacement
        return False

    monkeypatch.setattr(st, "session_transcript_remains", _swap)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 409, resp.body
    assert (
        state._slots.get("imported-1") is replacement
    ), "the replacement's slot must survive a refusal that is not about it"
    assert forgotten == [], "the replacement's Layer B mapping must not be forgotten"


@pytest.mark.asyncio
async def test_a_folder_store_failure_leaves_the_session_filed_nowhere(monkeypatch):
    """RFC §5.6. Filing is convenience; the transcript is the payload, so a store
    failure must not fail an import that works today."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not writable")

    state.mutate_folders = _boom
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    assert _filed_folder(state) == ""
