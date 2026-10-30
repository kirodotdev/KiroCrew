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

Bundle construction, validation, the delivery hop and the import endpoint's answers,
redaction and transcript prefix are pinned here. Layer B, the kiro-cli context a bundle
carries, is read, validated, written, joined and rolled back in
``test_session_transfer_layer_b.py``; the rows an import replays, its construction window
with the slot-cap accounting across it, and arrival filing in
``test_session_transfer_landing.py``; the import route's body and the host bounds that admit
it in ``test_session_transfer_import_body.py``; the transcript file's own privacy line
withholding a bundle at and after the read, the staged upload and the staging sweep in
``test_session_transfer_outbound.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _async_value,
    _FakeLog,
    _identity_exchange,
    _isolate_import_tmp,
    _JsonReply,
    _make_request,
    _OwnerReq,
    _PersistentLine,
    _PostedBody,
    _run_import,
    _send,
    _slot,
    _state,
    _stub_state,
    _valid,
)

from kiro_crew.dashboard.session_transfer import (
    BUNDLE_VERSION,
    _validate_bundle,
    build_transfer_bundle_async,
    local_instance_label,
)
from kiro_crew.testing.wait import async_wait_until, await_until

# ── bundle construction ──────────────────────────────────────────────────


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

    class _Log(_PersistentLine):
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
        async def send_session_bundle(self, _id, bundle, **_kw):
            captured["bundle"] = bundle
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_Log(),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 200, resp.body
    contents = [m["content"] for m in captured["bundle"]["messages"]]
    assert contents == ["persisted", "unsaved turn"], contents


@pytest.mark.asyncio
async def test_send_handler_revalidates_the_line_before_the_tunnel_post(monkeypatch):
    from kiro_crew.dashboard import handlers_instances as hi

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    class _TightensAtCommit(_FakeLog):
        @contextlib.contextmanager
        def publication_hold(self, _key, *, expected_keys=None):
            from kiro_crew.history import TranscriptWithheld

            raise TranscriptWithheld("fake: tightened before tunnel send")
            yield

    class _Mgr:
        async def send_session_bundle(self, _id, _bundle, **_kw):
            raise AssertionError("the tunnel POST ran after publication was refused")

    slot = _slot([{"role": "user", "content": "private", "ts": ""}])
    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_TightensAtCommit(slot.messages),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_slot_not_persistent"


@pytest.mark.asyncio
async def test_send_refuses_if_assembled_chain_loses_a_member(tmp_path, monkeypatch):
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    tab_id = "ddddeeeeffff"
    root = "dashboard:chat-transfer-root"
    sibling = "dashboard:chat-transfer-sibling"
    log = ConversationLog(base_dir=tmp_path / "sessions")
    await asyncio.to_thread(log.append, root, "user", "root", tab_id=tab_id)
    await asyncio.to_thread(log.append, sibling, "user", "sibling", tab_id=tab_id)
    slot = _slot([{"role": "user", "content": "root", "ts": ""}])
    slot.key = "chat-transfer-root"

    class _Mgr:
        async def send_session_bundle(self, _id, _bundle, **_kw):
            raise AssertionError("the tunnel POST ran after the chain changed")

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=log,
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    build = st.build_transfer_bundle_async

    async def _build_then_delete(*args, **kwargs):
        bundle = await build(*args, **kwargs)
        assert await asyncio.to_thread(log.delete_session, sibling)
        return bundle

    monkeypatch.setattr(hi, "build_transfer_bundle_async", _build_then_delete)
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "transfer_snapshot_unstable"


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
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "stale"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
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

    class _Resp(_JsonReply):
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

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            # First call carries the stale credential and is rejected; the retry
            # must carry the freshly minted one.
            cookie = headers["Cookie"]
            sent.append(cookie)
            return _Resp(403 if "stale" in cookie else 200)

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await _send(mgr, "peer", {"bundle_version": 1})
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

    class _Log(_PersistentLine):
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

    class _Log(_PersistentLine):
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
        async def send_session_bundle(self, _id, bundle, **_kw):
            sent.append(bundle)
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        # An app token, and a DIFFERENT app than the slot's owner.
        claims={"user": "owner", "app": "other-app"},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    # An app token never reaches the transfer body: ``_guard`` demands the
    # positively-identified owner, and the owner predicate treats any non-empty
    # ``app`` claim as not-the-owner. The slot-ownership check further in
    # (``api_instances_send_session``'s 404 for a slot another app owns) is kept as
    # defence in depth behind that gate, so an app that does reach the body still
    # cannot name a slot it does not own.
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "owner_only"
    assert sent == [], "nothing may be delivered for a slot the app does not own"


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


def test_validate_imposes_no_size_ceiling():
    """Validation is STRUCTURAL only — no message count, per-message, or total cap.

    A transfer is never blocked by size (the owner's decision); memory safety comes
    from streaming the body to disk on arrival, not from refusing large sessions.
    A bundle past each ceiling a validator could plausibly enforce (5,000 messages,
    1 MB/message, 20 MB total) validates cleanly. Each ceiling is crossed by the
    cheapest input that crosses it, so the fixture stays ~25 MB rather than the
    gigabytes a naive "every message oversized" fixture would allocate.
    """
    filler = "x" * 4_200
    huge = [{"role": "user", "content": filler, "ts": ""} for _ in range(6_000)]
    huge.append({"role": "assistant", "content": "y" * 1_100_000, "ts": ""})
    assert sum(len(m["content"]) for m in huge) > 20_000_000
    bundle, err = _validate_bundle(_valid(messages=huge))
    assert err is None
    assert len(bundle["messages"]) == 6_001
    assert len(bundle["messages"][-1]["content"]) == 1_100_000


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
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr.status = lambda _id: None  # type: ignore[method-assign]
    ok, payload = await _send(mgr, "peer", {"bundle_version": 1})

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
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    ok, payload = await _send(mgr, "peer", {"bundle_version": 1})

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
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "irrelevant-credential"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
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

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            raise ConnectionRefusedError(111, "connection refused")

    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: _RefusingSession())
    ok, payload = await _send(mgr, "peer", {"bundle_version": 1})

    assert ok is False
    assert payload["code"] == "transfer_unreachable"


def _peer_answering(status: int, *, body: object = None):
    """A fake ``aiohttp.ClientSession`` factory + the POST counter it records.

    ``body=None`` models a peer whose body is not JSON — what aiohttp's own
    default error responses (text/plain) look like from the client side.
    """
    posts = {"n": 0}

    class _Resp(_JsonReply):
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

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
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
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "tok"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
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
        ok, payload = await _send(mgr, "peer", {"bundle_version": 2})
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

    source = "".join(
        Path(st.__file__).with_name(f"session_transfer{part}.py").read_text(encoding="utf-8")
        for part in ("", "_staging", "_layers", "_intake", "_install")
    )
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

    # folder_id names the arrival folder in THIS instance's own sidebar tree so
    # the client can open it; it is a tree reference, not a credential.
    assert set(body) == {"ok", "key", "title", "messages", "resume_mode", "folder_id"}
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
    event loop: import calls ``_build_redacted_rows`` via ``asyncio.to_thread``
    before any slot exists, so the regex work runs on a worker thread and the loop
    is free to service other turns. This pins that the redaction pass is
    dispatched to a thread rather than run inline on the loop.
    """
    from kiro_crew.dashboard import session_transfer as st

    offloaded = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(fn, *a, **k):
        if getattr(fn, "__name__", "") in {"_build_redacted_rows", "_validate_bundle"}:
            offloaded.append(fn.__name__)
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(st.asyncio, "to_thread", _tracking_to_thread)

    big = [{"role": "assistant", "content": "x" * 500, "ts": ""} for _ in range(20)]
    await _run_import(st, monkeypatch, _valid(messages=big))

    assert sorted(offloaded) == [
        "_build_redacted_rows",
        "_validate_bundle",
    ], "validation or redaction ran on the event loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [750, 15_000])
async def test_import_persists_every_row_of_a_bundle_over_the_window(monkeypatch, tmp_path, n):
    """Every imported row reaches the transcript exactly once, in order.

    Only the newest ``_IMPORT_WINDOW`` rows are hydrated into the slot; the rest
    are written to disk as the frozen prefix the save then carries verbatim. 15k
    is past the slot's in-memory cap (``_MAX_SLOT_MESSAGES``): hydrating every
    row trimmed the oldest ones out of the window before the save, and nothing
    else held them, so they were lost.
    """
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "sessions")

    async def _real_save(state, slot, **_k):
        assert await asyncio.to_thread(cp._save_slot_to_history, state, slot, force=True)

    state = _stub_state(st, monkeypatch, save=_real_save)
    state.conversation_log = log
    big = [{"role": "assistant", "content": f"row-{i}", "ts": ""} for i in range(n)]
    slot = await _run_import(st, monkeypatch, _valid(messages=big), state=state, return_slot=True)

    assert len(slot.messages) == min(n, st._IMPORT_WINDOW)
    assert slot.messages[-1]["content"] == f"row-{n - 1}"
    assert slot._disk_older_count == n - len(slot.messages)
    lines = log._path(cp.slot_history_key(slot)).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines[1:]]
    assert [r["content"] for r in rows] == [f"row-{i}" for i in range(n)]
    mids = [r["meta"]["mid"] for r in rows]
    assert len(set(mids)) == n, "a row reached the transcript twice or without its own id"


@pytest.mark.asyncio
async def test_a_failed_save_removes_the_import_prefix(monkeypatch, tmp_path):
    """The prefix is written just before the save; a save that fails must not
    leave it behind as a stray transcript of an import that was refused."""
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "sessions")
    seen: list = []

    async def _failing_save(state, slot, **_k):
        path = log._path(cp.slot_history_key(slot))
        assert path.exists(), "the prefix was not on disk before the save"
        seen.append(path)
        raise OSError("disk went away")

    state = _stub_state(st, monkeypatch, save=_failing_save)
    state.conversation_log = log
    big = [{"role": "assistant", "content": f"row-{i}", "ts": ""} for i in range(900)]
    resp = await _run_import(st, monkeypatch, _valid(messages=big), state=state)

    assert resp.status >= 500
    assert seen and not seen[0].exists()


@pytest.mark.asyncio
async def test_a_cancelled_import_never_publishes_its_prefix(monkeypatch, tmp_path):
    """The prefix writer's thread outlives a cancelled import. Its rename must
    not land after the rollback, or History keeps a session missing its newest
    rows."""
    import threading

    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "sessions")
    writing, release, done = threading.Event(), threading.Event(), threading.Event()
    real_entry, real_write = st._build_message_entry_uncached, st._write_import_prefix

    def _slow_entry(row, **kw):
        writing.set()
        release.wait(10)
        return real_entry(row, **kw)

    def _tracked_write(*a, **kw):
        try:
            return real_write(*a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(st, "_build_message_entry_uncached", _slow_entry)
    monkeypatch.setattr(st, "_write_import_prefix", _tracked_write)
    state = _stub_state(st, monkeypatch)
    state.conversation_log = log
    big = [{"role": "assistant", "content": f"row-{i}", "ts": ""} for i in range(900)]
    task = asyncio.create_task(st.api_chat_slot_import(_make_request(state, _valid(messages=big))))
    assert await asyncio.to_thread(writing.wait, 10), "the prefix write never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The abandoned worker runs on to its rename point after the rollback.
    release.set()
    assert await asyncio.to_thread(done.wait, 10), "the prefix writer never finished"
    sessions_dir = tmp_path / "sessions"
    files = [p for p in sessions_dir.rglob("*") if p.is_file()] if sessions_dir.exists() else []
    assert files == [], f"a cancelled import left {files}"


@pytest.mark.asyncio
async def test_a_cancel_after_the_prefix_landed_removes_it(monkeypatch, tmp_path):
    """Cancelled during the save, after the prefix was renamed into place. The
    save's worker cannot be stopped and reads the prefix back as it writes, so
    nothing is removed while it runs; once it finishes, the transcript it
    published is removed, never left as an orphan or a truncated copy."""
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "sessions")
    saving, release = asyncio.Event(), asyncio.Event()
    seen: list = []

    async def _slow_save(state, slot, **_k):
        path = log._path(cp.slot_history_key(slot))
        assert path.exists(), "the prefix was not on disk before the save"
        seen.append(path)
        saving.set()
        await release.wait()
        # The worker's own write: it rewrites the transcript from the prefix
        # it just read, so the prefix must still be there.
        assert path.exists(), "the prefix was removed under the running save"
        path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
        return True

    state = _stub_state(st, monkeypatch, save=_slow_save)
    state.conversation_log = log
    big = [{"role": "assistant", "content": f"row-{i}", "ts": ""} for i in range(900)]
    task = asyncio.create_task(st.api_chat_slot_import(_make_request(state, _valid(messages=big))))
    await await_until(saving, what="the save reaching its parked write")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[0].exists(), "the transcript was removed while its save still ran"

    release.set()
    await async_wait_until(
        lambda: not seen[0].exists(),
        describe=lambda: "the finished save left a rolled-back transcript",
    )


def test_the_import_prefix_is_streamed_not_joined(tmp_path):
    """Writing the prefix holds one serialised row at a time, never the whole
    prefix: a joined copy of every row is extra memory the admission never
    reserved, on exactly the large imports it admits."""
    import tracemalloc

    from kiro_crew.dashboard import chat_utils
    from kiro_crew.dashboard import session_transfer as st

    n, width = 4000, 2000
    rows = [
        {
            "role": "assistant",
            "content": f"{i:06d}" + "x" * width,
            "ts": "t",
            "meta": {"mid": f"m{i}"},
        }
        for i in range(n)
    ]
    path = tmp_path / "s" / "slot.jsonl"
    # The display-redaction verdict store is a bounded process-wide cache that
    # other tests fill; its table doubling when this write adds 4,000 verdicts is
    # not a copy of the prefix, so start it empty to measure only the write.
    chat_utils._clear_display_redaction_cache()
    tracemalloc.start()
    try:
        st._write_import_prefix(path, "2026-01-01T00:00:00Z", rows)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    serialised = path.stat().st_size
    assert serialised > n * width
    assert (
        peak < serialised // 4
    ), f"prefix write peaked at {peak} bytes for a {serialised}-byte file"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["_type"] == "metadata"
    assert [json.loads(line)["meta"]["mid"] for line in lines[1:]] == [f"m{i}" for i in range(n)]
    assert [p.name for p in path.parent.iterdir()] == [
        "slot.jsonl"
    ], "a staged file was left behind"


def test_the_prefix_rename_runs_outside_the_publication_lock(monkeypatch, tmp_path):
    """The cancellation arm takes the publication lock on the event loop, so the
    worker never holds it across the rename (whose Windows retry sleeps). A
    cancellation that lands while the rename runs still removes the file."""
    from kiro_crew.dashboard import session_transfer as st

    publication = st._PrefixPublication()
    real_replace = st.replace_with_retry
    held: list[bool] = []

    def _replace(src, dst):
        held.append(publication.lock.locked())
        # The rollback runs while the rename is in flight and sees nothing
        # published yet, so it leaves the file to the worker.
        assert publication.mark_abandoned() is False
        real_replace(src, dst)

    monkeypatch.setattr(st, "replace_with_retry", _replace)
    rows = [{"role": "user", "content": "x", "ts": "t", "meta": {"mid": "m0"}}]
    path = tmp_path / "s" / "slot.jsonl"
    st._write_import_prefix(path, "2026-01-01T00:00:00Z", rows, publication)

    assert held == [False], "the rename ran under the publication lock"
    assert list(path.parent.iterdir()) == [], "an abandoned prefix survived its rename"


def test_a_failed_prefix_write_leaves_no_file(monkeypatch, tmp_path):
    """A write that fails part-way leaves neither the target nor its staged copy."""
    from kiro_crew.dashboard import session_transfer as st

    calls = {"n": 0}
    real = st._build_message_entry_uncached

    def _boom(row, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk went away")
        return real(row, **kw)

    monkeypatch.setattr(st, "_build_message_entry_uncached", _boom)
    rows = [
        {"role": "user", "content": str(i), "ts": "t", "meta": {"mid": f"m{i}"}} for i in range(5)
    ]
    path = tmp_path / "s" / "slot.jsonl"
    with pytest.raises(OSError):
        st._write_import_prefix(path, "2026-01-01T00:00:00Z", rows)
    assert list(path.parent.iterdir()) == []


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
