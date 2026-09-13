"""Adopt an EXISTING peer session into a local slot (teleport-to-local).

``POST /api/chat/slots`` with ``adopt_remote_slot`` binds a FRESH local slot to a
peer session the crew already holds, instead of minting an empty one over there.
Clicking a peer row in the merged Sessions list then opens that conversation in
the local pane — local transcript, local composer — with turns executing on the
peer.

Three things make that a different problem from a mint, and they are what this
file pins:

* the peer key is **caller-supplied**, so it is untrusted — validated against the
  same live-session view the sidebar renders, which is what refuses a forged key
  and a slot this hub already drives with one check;
* the local slot inherits **the peer's** ``agent`` / ``title`` / ``memory_mode``,
  the last of which is a privacy boundary and must never be defaulted here;
* the peer already **has a history**, so it is copied in once at birth — and that
  copy is best-effort, because a slot that opens with a notice instead of its
  history is usable while a create that 502s over a slow read leaves the user
  nothing.

The ``remote_already_bound`` 409 is asserted NOT to fire (an adopt makes a fresh
slot, and that guard exists only to stop a LIVE LOCAL session's execution being
moved to an empty peer slot), and the hub-driven dedupe is asserted to drop the
peer's own row once the adopt lands — which is what keeps one conversation from
rendering as two rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

import kiro_crew
from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.dashboard import remote_adopt as ra

_SECRET = "AKIAIOSFODNN7EXAMPLE"


# ── stubs ─────────────────────────────────────────────────────────────────────


class _Content:
    """The ``resp.content`` half of an aiohttp response, chunked as the real one is.

    ``iter_chunked`` is the accumulate-to-EOF path both peer reads use, so the
    stub yields wire-sized chunks rather than the whole body at once — the byte
    caps are crossed mid-body in production and must be crossed mid-body here.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Upstream:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _Content(body)


def _manager(*, slots=None, transcript=None, slots_status=200, transcript_status=200, raises=None):
    """A peer answering BOTH reads an adopt makes, with every call recorded.

    ``slots`` backs ``GET api/chat/slots`` (the validate read) and ``transcript``
    backs ``GET api/chat/slots/{key}`` (the backfill read). Recording the calls is
    the point of several tests: the adopt path must NOT mint, must send the peer's
    own key as a path literal, and must ask for a bounded tail only after the full
    read came back over the cap.

    ``transcript`` may be a callable taking the request's ``params`` so a test can
    answer the full read and the tail read differently.
    """
    calls: list[tuple[str, str, str, dict | None]] = []

    @contextlib.asynccontextmanager
    async def _proxy_request(instance_id, method, path, *, params=None, **_kw):
        calls.append((instance_id, method, path, params))
        if raises is not None:
            raise raises
        if path == "api/chat/slots":
            body = json.dumps(slots if slots is not None else []).encode()
            yield _Upstream(slots_status, body)
            return
        payload = transcript(params) if callable(transcript) else transcript
        body = json.dumps(payload if payload is not None else {"messages": []}).encode()
        yield _Upstream(transcript_status, body)

    async def _peer_version(_instance_id):
        return True, kiro_crew.__version__

    return SimpleNamespace(
        proxy_request=_proxy_request,
        peer_version=_peer_version,
        calls=calls,
        status=lambda _i: None,
    )


def _peer_row(key="peer-chat-9", **extra):
    row = {"key": key, "title": key.upper(), "agent": "kirocrew", "memory_mode": "persistent"}
    row.update(extra)
    return row


def _msgs(*rows):
    return {"key": "peer-chat-9", "messages": list(rows), "total": len(rows), "has_more": False}


def _row(role, content, **extra):
    row = {"role": role, "content": content, "cls": "", "ts": "2026-09-11T00:00:00Z"}
    row.update(extra)
    return row


def _app(state, *, app_name="", user="local-app"):
    """The real create handler behind a TestServer.

    Middleware sets ``request["app"]``/``["user"]`` in production; setting them in
    a wrapper keeps the handler — and therefore the gate ORDER — real. ``user``
    defaults to ``local-app`` because the binding's owner gate is a POSITIVE
    assertion, so a bare truthy user is an authenticated NON-owner.
    """
    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        request["user"] = user
        return await api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", handler)
    return app


async def _post(state, payload, **kw):
    async with TestClient(TestServer(_app(state, **kw))) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        return resp.status, await resp.json()


@pytest.fixture
def no_mint(monkeypatch):
    """A spy on ``create_peer_slot``, so a MINT is observable.

    The whole point of adopt is that it does not happen: a mint would open a
    second, empty session on the crew and bind the local slot to that instead of
    to the conversation the user clicked.
    """
    spy = AsyncMock(return_value="peer-minted-1")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.create_peer_slot", spy)
    return spy


def _bound_state(tmp_path, mgr):
    state = _make_state(tmp_path)
    state.instances_manager = mgr
    return state


# ── A. the adopt-create path ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAdoptBindsTheSuppliedKey:
    async def test_adopt_binds_the_peers_own_key_and_does_not_mint(self, tmp_path, no_mint):
        """The load-bearing case: the local slot points at the EXISTING session.

        A mint here would bind the user's new pane to an empty session on the crew
        while the conversation they clicked stayed unreachable.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        slot = state._slots[body["key"]]
        assert slot.executor == "remote"
        assert slot.instance_id == "nobita"
        assert slot.remote_slot == "peer-chat-9"
        assert slot.is_remote is True
        no_mint.assert_not_awaited()

    async def test_the_response_names_the_new_local_slot(self, tmp_path, no_mint):
        """The frontend switches to ``response.key``, so it must be the LOCAL one.

        Answering with the peer's key would send ``switchSlot`` to a session this
        machine does not have.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        assert body["key"] in state._slots
        assert body["key"] != "peer-chat-9"

    async def test_a_fresh_slot_does_not_trip_the_remote_already_bound_guard(
        self, tmp_path, no_mint
    ):
        """The design finding this whole path rests on.

        ``remote_already_bound`` fires only when the create resolves to an
        EXISTING LOCAL slot, because its purpose is to stop a live local session's
        EXECUTION being moved to an empty peer slot. An adopt makes a fresh slot,
        so it must pass — and a regression that widened that guard to "any bind"
        would break adopt entirely while every mint test still passed.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)
        # A live local conversation exists alongside it, so the guard's own
        # condition is present in the process and merely not addressed.
        state.get_or_create_slot("chat-existing").append("user", "mine", "msg msg-u")

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        assert body.get("code") != "remote_already_bound"

    async def test_naming_an_existing_local_slot_is_still_refused(self, tmp_path, no_mint):
        """Adopt does not become a door into the conversion the guard forbids.

        ``name`` addressing a live local session still means "move THIS session's
        execution", which strands its transcript here while turns run on a peer
        that has never seen it. Adopt is a NEW slot or nothing.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "the context that would stop being in play", "msg msg-u")

        status, body = await _post(
            state,
            {"name": "chat-1", "instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"},
        )

        assert status == 409
        assert body["code"] == "remote_already_bound"
        assert slot.executor == "local"
        assert mgr.calls == []


@pytest.mark.asyncio
class TestAdoptTargetValidation:
    async def test_an_unlisted_key_is_refused(self, tmp_path, no_mint):
        """A forged or stale key names no session this hub may bind to."""
        mgr = _manager(slots=[_peer_row("peer-chat-3")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 404
        assert body["code"] == "adopt_target_unknown"
        assert not [s for s in state._slots.values() if s.executor == "remote"]

    async def test_a_hub_driven_key_resolves_to_the_local_slot_that_drives_it(
        self, tmp_path, no_mint
    ):
        """A hub-driven key is not refused — it is ANSWERED with its local slot.

        The contract lists a hub-driven key under ``adopt_target_unknown``, but the
        two rules it states cannot both hold: the hub-driven set is exactly the set
        of local bindings, so the idempotency rule (return the slot that already
        binds this pair) and the "hub-driven is not adoptable" rule name the SAME
        keys. Validating first would therefore 404 the second of two identical
        adopts — the double-click the idempotency rule exists for — because the
        first adopt is what made the key hub-driven.

        So idempotency runs first and this is its general case: the user is handed
        the local session that already drives that peer slot, which is the session
        they wanted, and no second binding is made. Refusing instead would leave a
        peer row the user can see and can never open.
        """
        mgr = _manager(slots=[_peer_row(), _peer_row("peer-chat-3")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)
        driver = state.get_or_create_slot("chat-driver")
        driver.executor = "remote"
        driver.instance_id = "nobita"
        driver.remote_slot = "peer-chat-9"

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        assert body["key"] == "chat-driver"
        assert [s.key for s in state._slots.values() if s.is_remote] == ["chat-driver"]
        # Answered from local state: the peer was never asked.
        assert mgr.calls == []

    async def test_a_key_driven_by_a_DIFFERENT_crew_is_still_adoptable(self, tmp_path, no_mint):
        """Peer slot keys are unique only WITHIN a peer.

        Two crews can each hold a ``peer-chat-9``, so a binding to crew B must not
        make crew A's identically-named session unadoptable.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)
        other = state.get_or_create_slot("chat-other")
        other.executor = "remote"
        other.instance_id = "shizuka"
        other.remote_slot = "peer-chat-9"

        status, _ = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200

    async def test_adopt_without_a_crew_is_a_400(self, tmp_path, no_mint):
        """The key names a session on ONE peer; with no peer there is nothing to
        resolve it against and nowhere to route the turn."""
        state = _bound_state(tmp_path, _manager())

        status, body = await _post(state, {"adopt_remote_slot": "peer-chat-9"})

        assert status == 400
        assert body["code"] == "adopt_needs_instance"

    async def test_an_unreachable_peer_is_a_502_and_binds_nothing(self, tmp_path, no_mint):
        """A validate read that cannot happen must not produce a half-bound slot."""
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        mgr = _manager(
            raises=ProxyRequestError("peer_not_connected", "not connected", http_status=503)
        )
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 502
        assert body["code"] == "remote_bind_failed"
        assert not [s for s in state._slots.values() if s.executor == "remote"]

    async def test_a_refusing_peer_is_a_502(self, tmp_path, no_mint):
        """Every read failure collapses to one code: from here "the crew could not
        be asked" is a single outcome however the read failed."""
        mgr = _manager(slots=[], slots_status=500)
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 502
        assert body["code"] == "remote_bind_failed"

    async def test_a_version_mismatch_is_refused_before_anything_is_bound(self, tmp_path, no_mint):
        """An adopted session's turns run through the same relay a mint's do, so the
        same parity fence applies — and it has to answer before a slot exists
        pointing at a peer that cannot carry a turn."""
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())

        async def _old(_instance_id):
            return True, "0.0.1"

        mgr.peer_version = _old
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 502
        assert body["code"] == "remote_bind_failed"
        assert mgr.calls == []


@pytest.mark.asyncio
class TestAdoptAuthorization:
    """The existing gates must still refuse, and refuse BEFORE the peer is read.

    Adopt does not write to the peer, so there is no orphaned session to worry
    about — but it does spend the owner's tunnel credential and it does disclose
    that a session with a given key exists over there, so a refused caller must
    not reach the read at all.
    """

    async def test_an_app_token_cannot_adopt(self, tmp_path, no_mint):
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state,
            {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"},
            app_name="notes",
        )

        assert status == 404
        assert body["code"] == "slot_not_found"
        assert mgr.calls == []

    async def test_a_non_owner_identity_cannot_adopt(self, tmp_path, no_mint):
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, _ = await _post(
            state,
            {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"},
            user="slack:U123",
        )

        assert status == 403
        assert mgr.calls == []


@pytest.mark.asyncio
class TestAdoptIsIdempotent:
    async def test_a_second_adopt_returns_the_same_local_slot(self, tmp_path, no_mint):
        """A double click must not leave two local slots driving one peer session.

        Two bindings render as two separate conversations, and the sidebar's
        hub-driven filter drops the peer row on either of them — so which of the
        two the user lands in becomes arbitrary. This is also what makes the
        frontend's ``switchSlot(resp.key)`` correct on a retry.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("user", "hi")))
        state = _bound_state(tmp_path, mgr)
        payload = {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}

        _, first = await _post(state, payload)
        bound_before = [s.key for s in state._slots.values() if s.is_remote]
        status, second = await _post(state, payload)

        assert status == 200
        assert second["key"] == first["key"]
        assert [s.key for s in state._slots.values() if s.is_remote] == bound_before

    async def test_two_concurrent_adopts_bind_one_local_slot_not_two(
        self, tmp_path, no_mint, monkeypatch
    ):
        """The race the pre-peer check cannot close.

        The first idempotency check runs before ``resolve_adopt_target`` and
        ``fetch_adopted_backfill``, and both of those suspend -- so two requests
        arriving together clear it as a pair and each go on to mint a local slot
        bound to the SAME peer session. That is worse than a duplicate row: each
        slot accumulates its own turns, so the two transcripts diverge, and
        ``read_peer_slots`` filters the peer's own row on whichever binding it
        happens to see, making the one the user lands in arbitrary.

        The interleaving is FORCED, not hoped for. The fake peer manager resolves
        without ever yielding, so a plain ``gather`` of two posts runs them to
        completion one after the other and the race never happens -- the test then
        passes with the fix reverted, which is worth more as a warning than as
        coverage. Gating the backfill until BOTH requests have reached it puts them
        both past the early check by construction, which is the actual precondition
        of the bug.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("user", "hi")))
        state = _bound_state(tmp_path, mgr)
        payload = {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}

        both_arrived = asyncio.Event()
        arrivals = 0
        real_backfill = ra.fetch_adopted_backfill

        async def gated_backfill(*args, **kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals >= 2:
                both_arrived.set()
            await both_arrived.wait()
            return await real_backfill(*args, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.fetch_adopted_backfill", gated_backfill
        )

        async with TestClient(TestServer(_app(state))) as client:
            first, second = await asyncio.gather(
                client.post("/api/chat/slots", json=payload),
                client.post("/api/chat/slots", json=payload),
            )
            one = await first.json()
            two = await second.json()

        assert arrivals == 2, "both requests must clear the early check to race at all"
        assert (first.status, second.status) == (200, 200)
        bound = [
            slot
            for slot in state._slots.values()
            if slot.is_remote and slot.remote_slot == "peer-chat-9"
        ]
        assert len(bound) == 1
        # Both callers are told about the SAME slot, so the frontend's
        # switchSlot(resp.key) lands on the surviving session either way.
        assert one["key"] == two["key"] == bound[0].key
        no_mint.assert_not_awaited()

    async def test_a_second_tab_gets_the_winners_slot_not_a_404(self, tmp_path, no_mint):
        """Absence from the peer's listing is not always forgery.

        ``read_peer_slots`` drops the rows this hub already drives, so once one
        adopt has stamped its binding, the row another request came to adopt is GONE
        from the listing that validates it. Reported by review: two tabs on one peer
        row left the winner with the session open and the loser holding a 404 for a
        session that exists and is reachable locally.

        The ordering is the whole test, and only ONE ordering reaches the bug. Fully
        sequential does not: the early pre-peer check answers the second request
        before ``resolve_adopt_target`` runs. Fully overlapping does not either: the
        pre-create recheck catches it. The reachable window is a request that has
        ALREADY passed the early check when the winner stamps -- it then computes
        ``driven`` afresh, finds its own target filtered out, and is refused inside
        ``resolve_adopt_target`` before any recheck can speak. So the loser's peer
        read is held here until a binding exists, which is exactly that window.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("user", "hi")))
        state = _bound_state(tmp_path, mgr)
        payload = {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}

        real_read = hi.read_peer_slots
        held_once = False

        async def read_after_the_winner_binds(*args, **kwargs):
            # Hold the FIRST reader until some other request has bound the pair,
            # then let it compute `driven` — which now excludes its own target.
            nonlocal held_once
            if not held_once:
                held_once = True
                for _ in range(200):
                    if ra.adopted_slot_for(state, "nobita", "peer-chat-9") is not None:
                        break
                    await asyncio.sleep(0.01)
            return await real_read(*args, **kwargs)

        monkeypatch = pytest.MonkeyPatch()
        # Patched where `remote_adopt` BOUND it, not where it is defined: the import
        # is module-scope now, so the name is resolved at import time and patching
        # `handlers_instances` would leave this call site untouched. The
        # `held_once` assertion below is what caught that.
        monkeypatch.setattr(
            "kiro_crew.dashboard.remote_adopt.read_peer_slots", read_after_the_winner_binds
        )
        try:
            async with TestClient(TestServer(_app(state))) as client:
                loser, winner = await asyncio.gather(
                    client.post("/api/chat/slots", json=payload),
                    client.post("/api/chat/slots", json=payload),
                )
                loser_body = await loser.json()
                winner_body = await winner.json()
        finally:
            monkeypatch.undo()

        assert held_once, "the held-reader window is the precondition of this test"
        assert (loser.status, winner.status) == (200, 200)
        assert loser_body["key"] == winner_body["key"]
        bound = [
            slot
            for slot in state._slots.values()
            if slot.is_remote and slot.remote_slot == "peer-chat-9"
        ]
        assert len(bound) == 1

    async def test_the_second_adopt_reads_neither_the_list_nor_the_transcript(
        self, tmp_path, no_mint
    ):
        """Idempotency answers from local state, before the peer is touched.

        Re-reading would re-copy the history onto a slot that already has it.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("user", "hi")))
        state = _bound_state(tmp_path, mgr)
        payload = {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}

        await _post(state, payload)
        calls_after_first = len(mgr.calls)
        await _post(state, payload)

        assert len(mgr.calls) == calls_after_first

    async def test_a_half_written_binding_is_not_treated_as_already_adopted(
        self, tmp_path, no_mint
    ):
        """Only a WHOLE binding counts, the same predicate the sidebar filter uses.

        A slot carrying the marker but no target drives nothing, so returning it
        would hand the user a session that cannot run a turn.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)
        broken = state.get_or_create_slot("chat-broken")
        broken.executor = "remote"
        broken.instance_id = "nobita"  # no remote_slot

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        assert body["key"] != "chat-broken"


@pytest.mark.asyncio
class TestInheritedMetadata:
    async def test_memory_mode_comes_from_the_peer_not_the_request(self, tmp_path, no_mint):
        """``memory_mode`` is the user's privacy boundary and the peer session
        already has one. Defaulting it here would let a session opened as
        ``incognito`` over there start writing memory on this machine."""
        mgr = _manager(slots=[_peer_row(memory_mode="incognito")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(
            state,
            {
                "instance_id": "nobita",
                "adopt_remote_slot": "peer-chat-9",
                "memory_mode": "persistent",
            },
        )

        assert state._slots[body["key"]].memory_mode == "incognito"

    async def test_an_unrecognised_peer_mode_is_REFUSED_not_defaulted(self, tmp_path, no_mint):
        """A peer on another build could name a mode this gateway does not have.

        Inheriting it verbatim would smuggle a value past the create handler's own
        ``memory_mode`` validation -- but resolving it to this machine's default is
        the worse of the two, because the default is ``persistent``. The row we
        could NOT read a boundary from is exactly the row that must not be guessed
        at, so the adopt is refused instead.
        """
        mgr = _manager(slots=[_peer_row(memory_mode="ultra")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 502
        assert body["code"] == "adopt_peer_mode_unknown"
        assert state._slots == {}, "no slot may exist when the boundary is unknown"

    async def test_a_peer_row_with_no_mode_at_all_is_REFUSED(self, tmp_path, no_mint):
        """The version-skew case, and the reason this is a privacy fix.

        A crew whose slot rows predate ``memory_mode`` omits the key entirely. Taking
        the REQUEST's mode there would turn every incognito session on that crew into
        a persistent one here, writing memory the user never agreed to -- and it would
        do it silently, on the machine the user is sitting at.
        """
        row = _peer_row()
        del row["memory_mode"]
        mgr = _manager(slots=[row], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state,
            {
                "instance_id": "nobita",
                "adopt_remote_slot": "peer-chat-9",
                "memory_mode": "persistent",
            },
        )

        assert status == 502
        assert body["code"] == "adopt_peer_mode_unknown"
        assert state._slots == {}

    async def test_the_agent_and_title_come_from_the_peer_row(self, tmp_path, no_mint):
        """The peer chose the agent and named the session; there is no picker on an
        adopt, and the title is the label the user just clicked."""
        mgr = _manager(
            slots=[_peer_row(agent="researcher", title="Ledger cleanup")], transcript=_msgs()
        )
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        slot = state._slots[body["key"]]
        assert slot.agent == "researcher"
        assert slot.title == "Ledger cleanup"
        # Pinned, so the background auto-title cannot rename a session the peer owns.
        assert slot._titled is True

    async def test_the_peers_title_wins_over_a_caller_supplied_one(self, tmp_path, no_mint):
        """Inheritance is an OVERRIDE, not a fallback.

        The adopt path sends no title of its own, so a caller-supplied one could
        only disagree with the session being adopted -- and the label the user
        clicked in the merged list is the peer's. Same contract that puts the peer
        in charge of ``agent`` and ``memory_mode``.
        """
        mgr = _manager(slots=[_peer_row(title="Peer name")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(
            state,
            {
                "instance_id": "nobita",
                "adopt_remote_slot": "peer-chat-9",
                "title": "My name for it",
            },
        )

        assert state._slots[body["key"]].title == "Peer name"

    async def test_the_peers_agent_wins_over_a_caller_supplied_one(self, tmp_path, no_mint):
        """Same rule for the agent, and for the same reason.

        There is no agent picker on an adopt, so a caller-supplied agent would open
        the peer's conversation under a different agent than the one that has been
        answering in it.
        """
        mgr = _manager(slots=[_peer_row(agent="peer-agent")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(
            state,
            {
                "instance_id": "nobita",
                "adopt_remote_slot": "peer-chat-9",
                "agent": "my-local-agent",
            },
        )

        assert state._slots[body["key"]].agent == "peer-agent"

    async def test_peer_metadata_is_redacted_before_it_lands_on_the_slot(self, tmp_path, no_mint):
        """A peer title is model-authored text from another machine, and it reaches
        the sidebar. It meets a redactor here for the same reason the peer-slots
        route redacts the row it renders."""
        mgr = _manager(slots=[_peer_row(title=f"key {_SECRET} here")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        assert _SECRET not in state._slots[body["key"]].title


# ── B. transcript backfill ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBackfill:
    async def test_the_peers_history_lands_in_the_local_transcript(self, tmp_path, no_mint):
        """Without this the user opens a session whose history is invisible, then
        sends a turn the peer answers WITH that history — a local transcript that
        disagrees with the conversation."""
        mgr = _manager(
            slots=[_peer_row()],
            transcript=_msgs(_row("user", "what broke?"), _row("assistant", "the loader")),
        )
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        slot = state._slots[body["key"]]
        assert [(m["role"], m["content"]) for m in slot.messages] == [
            ("user", "what broke?"),
            ("assistant", "the loader"),
        ]

    async def test_the_transcript_is_read_from_the_peers_own_slot_path(self, tmp_path, no_mint):
        """The path carries the PEER's key, and the full-history read sends no
        ``limit`` — ``api_chat_slot_detail`` returns the whole chained corpus only
        when neither pagination param is supplied."""
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("user", "hi")))
        state = _bound_state(tmp_path, mgr)

        await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        reads = [(c[1], c[2], c[3]) for c in mgr.calls]
        assert ("GET", "api/chat/slots/peer-chat-9", None) in reads

    async def test_backfilled_peer_text_is_redacted(self, tmp_path, no_mint):
        """``slot.append`` both stores the row and hands it to the ConversationLog,
        so a pass applied afterwards would already have persisted the raw text.

        Applied to the ``user`` role too: the local rule leaves user text raw
        because its author is its only reader, but this text was authored on
        another machine and arrived over a wire.
        """
        mgr = _manager(
            slots=[_peer_row()],
            transcript=_msgs(_row("user", f"use {_SECRET}"), _row("assistant", f"ok {_SECRET}")),
        )
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        blob = json.dumps(state._slots[body["key"]].messages)
        assert _SECRET not in blob

    async def test_backfilled_rows_are_persisted(self, tmp_path, no_mint):
        """A memory-only backfill vanishes on the next gateway restart, leaving the
        session's history invisible again — the exact defect the copy exists for.

        The create's existing ``force=True`` save is what writes them, which is why
        the apply deliberately leaves the slot dirty.
        """
        mgr = _manager(slots=[_peer_row()], transcript=_msgs(_row("assistant", "durable")))
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        stored = state.conversation_log.read_messages_chained(f"dashboard:{body['key']}")
        assert [m["content"] for m in stored] == ["durable"]

    async def test_a_failed_transcript_read_still_opens_the_session(self, tmp_path, no_mint):
        """Non-fatal by contract: the slot is validated and bound, and the peer
        answers the next turn with its history either way. Failing the create
        instead takes a working session away over a read that timed out."""
        mgr = _manager(slots=[_peer_row()], transcript_status=500)
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        slot = state._slots[body["key"]]
        assert slot.is_remote is True
        assert [m["role"] for m in slot.messages] == ["system"]
        assert "could not be copied" in slot.messages[0]["content"]

    async def test_adopting_a_session_the_crew_is_answering_in_says_so(self, tmp_path, no_mint):
        """The settled history lands; the in-flight reply is named, not faked.

        End-to-end counterpart to the unit test on ``prepare_backfill_rows``: what
        matters to the user is that the transcript they open does not show a
        truncated answer as if it were finished, and does tell them why the last
        reply is missing.
        """
        mgr = _manager(
            slots=[_peer_row()],
            transcript=_msgs(
                _row("user", "explain it"),
                _row("streaming", "half a thou"),
            ),
        )
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        slot = state._slots[body["key"]]
        # The notice leads the adopted transcript, where the other backfill notices
        # go -- it reads as a banner on the copied session rather than as a reply.
        assert [m["role"] for m in slot.messages] == ["system", "user"]
        # The partial text is nowhere in the transcript, under any role.
        assert all("half a thou" not in m["content"] for m in slot.messages)
        assert "still answering" in slot.messages[0]["content"]

    async def test_a_malformed_transcript_is_a_notice_not_a_500(self, tmp_path, no_mint):
        mgr = _manager(slots=[_peer_row()], transcript={"messages": "not a list"})
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        assert [m["role"] for m in state._slots[body["key"]].messages] == ["system"]

    async def test_an_empty_peer_session_backfills_nothing_and_says_nothing(
        self, tmp_path, no_mint
    ):
        """An adoptable session can legitimately be empty. A notice there would
        claim history was lost when none existed."""
        mgr = _manager(slots=[_peer_row()], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        assert state._slots[body["key"]].messages == []

    async def test_an_oversized_full_read_falls_back_to_a_bounded_tail(
        self, tmp_path, no_mint, monkeypatch
    ):
        """A prefix of an over-cap JSON document cannot be parsed, so the tail has
        to be asked for separately — and recent turns are worth a second
        round-trip rather than opening with no history at all."""
        monkeypatch.setattr(ra, "PEER_TRANSCRIPT_REPLY_MAX_BYTES", 200)

        def _by_limit(params):
            if params and params.get("limit"):
                return _msgs(_row("assistant", "the tail"))
            return _msgs(*[_row("assistant", "x" * 100) for _ in range(20)])

        mgr = _manager(slots=[_peer_row()], transcript=_by_limit)
        state = _bound_state(tmp_path, mgr)

        status, body = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )

        assert status == 200
        limits = [c[3] for c in mgr.calls if c[2] == "api/chat/slots/peer-chat-9"]
        assert limits == [None, {"limit": str(ra.PEER_TRANSCRIPT_FALLBACK_LIMIT)}]
        rows = state._slots[body["key"]].messages
        # The notice comes FIRST, so it reads as a header on the history below it.
        assert rows[0]["role"] == "system"
        assert "stay on nobita" in rows[0]["content"]
        assert rows[-1]["content"] == "the tail"

    async def test_the_row_cap_keeps_the_tail_and_says_so(self, tmp_path, no_mint, monkeypatch):
        """Rows past the local window cap would be trimmed off the head by
        ``slot.append`` anyway, so copying them costs a redaction pass each and
        buys nothing. The recent turns are the ones the next turn is about."""
        monkeypatch.setattr(ra, "PEER_TRANSCRIPT_MAX_ROWS", 3)
        mgr = _manager(
            slots=[_peer_row()],
            transcript=_msgs(*[_row("assistant", f"m{i}") for i in range(6)]),
        )
        state = _bound_state(tmp_path, mgr)

        _, body = await _post(state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"})

        rows = state._slots[body["key"]].messages
        assert [m["content"] for m in rows] == [
            ra._backfill_truncated_notice("nobita"),
            "m3",
            "m4",
            "m5",
        ]


class TestRowMapping:
    """``prepare_backfill_rows`` is pure, so its edges are pinned directly."""

    def test_wire_only_and_approval_rows_are_dropped(self):
        """``chunk``/``done`` are stream bookkeeping, never transcript.

        ``permission`` is the judgement: it renders an approval bar, and an adopted
        one has no local future to resolve, so every button on it would answer
        ``404 no pending approval`` on a card the user cannot dismiss. The record
        of the approval survives as the ``tool_call``/``tool_result`` pair it gated.
        """
        rows, dropped, _in_flight = ra.prepare_backfill_rows(
            [
                _row("chunk", "par"),
                _row("done", ""),
                _row("permission", "approve rm?"),
                _row("tool_call", "fs_read"),
                _row("tool_result", "ok"),
            ]
        )

        assert [r["role"] for r in rows] == ["tool_call", "tool_result"]
        assert dropped == 0

    def test_an_in_flight_streaming_row_is_dropped_and_reported(self):
        """A frozen partial reply is worse than a stated absence.

        A ``streaming`` row exists only while a turn is in flight on the peer, and
        this hub never mirrors a turn it did not start -- so its suffix never
        arrives. Mapping it to ``assistant`` (the earlier behaviour) persisted the
        snapshot into the local transcript file, where nothing distinguishes it from
        a reply that really ended there: a truncated answer presented as a complete
        one, permanently. Dropped instead, with the flag that turns it into a
        visible notice.
        """
        rows, _, in_flight = ra.prepare_backfill_rows([_row("streaming", "half a thought")])

        assert rows == []
        assert in_flight is True

    def test_a_settled_transcript_reports_no_in_flight_reply(self):
        """The flag is about the PEER still answering, not about adopt in general."""
        rows, _, in_flight = ra.prepare_backfill_rows(
            [_row("user", "hi"), _row("assistant", "all done")]
        )

        assert [r["role"] for r in rows] == ["user", "assistant"]
        assert in_flight is False

    def test_an_unknown_role_is_dropped(self):
        """The local window is re-serialized into a real transcript file, so a role
        a peer on another build invented would be PERSISTED as one — with nothing
        local able to render it."""
        rows, _, _in_flight = ra.prepare_backfill_rows([_row("hologram", "?"), _row("user", "hi")])

        assert [r["role"] for r in rows] == ["user"]

    def test_the_peers_row_delivery_id_is_not_adopted(self):
        """``mid`` is a per-gateway delivery id; taking the peer's would collide
        with the local mid space. The durable tool correlation is kept."""
        rows, _, _in_flight = ra.prepare_backfill_rows(
            [_row("tool_call", "fs_read", meta={"mid": "peer-77", "tool_name": "fs_read"})]
        )

        assert rows[0]["meta"] == {"tool_name": "fs_read"}

    def test_meta_is_redacted(self):
        rows, _, _in_flight = ra.prepare_backfill_rows(
            [_row("tool_result", "done", meta={"tool_output": f"got {_SECRET}"})]
        )

        assert _SECRET not in json.dumps(rows[0]["meta"])

    def test_a_non_string_content_is_not_dropped_silently(self):
        """A peer field that is not a string is still conversation. Serialized
        rather than discarded, the same fallback ``_apply_row`` uses."""
        rows, _, _in_flight = ra.prepare_backfill_rows([_row("assistant", {"parts": ["a"]})])

        assert rows[0]["content"] == '{"parts": ["a"]}'

    def test_a_non_dict_or_roleless_row_is_ignored(self):
        rows, _, _in_flight = ra.prepare_backfill_rows(
            [{"content": "no role"}, _row("", "empty role")]
        )

        assert rows == []


# ── C. the duplicate-row question ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestAdoptedRowLeavesThePeerListing:
    """The open question in the plan (C4 / open decision 3), answered here.

    An adopted slot satisfies ``is_remote`` with ``instance_id`` and
    ``remote_slot`` set, which is exactly the predicate ``read_peer_slots``
    computes its hub-driven set from. So the peer's own row for that session drops
    out of the merged listing on the very next poll, with no explicit exclude and
    nothing for the frontend to do.
    """

    async def test_the_adopted_session_stops_appearing_as_a_peer_row(
        self, tmp_path, no_mint, monkeypatch
    ):
        # The listing route's feature gate reads the real config class, and the
        # create handler in the same test reads it too — so the stub carries the
        # fields BOTH need, not only ``instances``.
        monkeypatch.setattr(
            hi.KiroCrewConfig,
            "load",
            staticmethod(
                lambda: SimpleNamespace(
                    instances=SimpleNamespace(enabled=True),
                    dashboard=SimpleNamespace(default_project=""),
                    default_agent="",
                )
            ),
        )
        mgr = _manager(slots=[_peer_row(), _peer_row("peer-chat-3")], transcript=_msgs())
        state = _bound_state(tmp_path, mgr)

        before = await _listing(state)
        assert sorted(row["key"] for row in before) == ["peer-chat-3", "peer-chat-9"]

        status, _ = await _post(
            state, {"instance_id": "nobita", "adopt_remote_slot": "peer-chat-9"}
        )
        assert status == 200

        after = await _listing(state)
        assert [row["key"] for row in after] == ["peer-chat-3"]


class _ListReq:
    def __init__(self, state, instance_id):
        self.app = {"state": state}
        self.match_info = {"id": instance_id}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = {"user": "local-app", "app": ""}

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


async def _listing(state, instance_id="nobita"):
    resp = await hi.api_instances_chat_slots(_ListReq(state, instance_id))
    assert resp.status == 200
    return json.loads(resp.body.decode())
