"""A transcript row records the MCP App this tool call produced.

The render payload is live-only by design: it goes out on the owner-only
WebSocket, carries the app's callback capability, and its spool record expires.
A stored transcript holds nothing that can rebuild the frame, so a reader can
have the turn's prose describing an artifact they cannot see. The row's durable
flag is what lets the dashboard say the app exists.

These drive the real ``dashboard.chat_runner._run_chat`` turn loop with a fake
ACP client, so what they measure is the flag a REPLAY reads off disk rather
than a reimplementation of the write. The harness mirrors
``test_acp_tool_identity.TestChatRunnerDirectiveSeam``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import mcp_apps_render
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_utils import effective_session_key, session_key_for

TOOL_CALL_ID = "tc-app-1"

#: The meta key a replayed row reads. Spelled once here so a rename has to move
#: this constant, and the frontend's own reader is named beside it.
META_KEY = "mcp_app"
LEAD_KEY = "mcp_app_lead"


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    d = tmp_path / "mcp-apps"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(d))
    return d


def _write_spool(spool_dir: Path, sid: str, session_key: str) -> None:
    (spool_dir / f"{sid}.json").write_text(
        json.dumps(
            {
                "schema": mcp_apps_render.SPOOL_SCHEMA_VERSION,
                "server": "excalidraw",
                "tool": "create_view",
                "html": "<h1>diagram</h1>",
                "session_key": session_key,
            }
        ),
        encoding="utf-8",
    )


def _stub_state(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    # The render goes out on the OWNER channel; stub it too or the seam would
    # reach real WebSocket plumbing that this harness does not stand up.
    state.broadcast_ws_owners = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    return state


async def _drive(state, slot, events):
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await chat_runner._run_chat(state, slot, "draw me a diagram")
    task = getattr(slot, "task", None)
    if task is not None:
        await task


def _tool_rows(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if m.get("role") == "tool" and m.get("meta", {}).get("tool_call_id") == TOOL_CALL_ID
    ]


def _events(marker: str) -> list[AcpEvent]:
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=TOOL_CALL_ID,
            title="create_view",
            tool_name="create_view",
            mcp_server_name="excalidraw",
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=TOOL_CALL_ID,
            tool_output=f"Done - two rectangles and an arrow. {marker}",
            tool_final=True,
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
        AcpEvent(kind=EVENT_COMPLETE),
    ]


def _events_two_rows(marker: str) -> list[AcpEvent]:
    """One call, TWO transcript rows -- the auto-approved shape.

    An auto-approved tool is announced twice (the pending pill, then the
    approved one) under a single ``tool_call_id``, so the turn appends two rows
    and flags both. Any assertion about WHICH of an occurrence's rows is the lead
    needs at least two, or writing the marker once and writing it on every row
    are indistinguishable.
    """
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=TOOL_CALL_ID,
            title="create_view",
            tool_name="create_view",
            mcp_server_name="excalidraw",
        ),
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=TOOL_CALL_ID,
            title="create_view",
            tool_name="create_view",
            mcp_server_name="excalidraw",
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=TOOL_CALL_ID,
            tool_output=f"Done - two rectangles and an arrow. {marker}",
            tool_final=True,
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
        AcpEvent(kind=EVENT_COMPLETE),
    ]


class TestAClaimedAppIsRecordedOnItsRow:
    @pytest.mark.asyncio
    async def test_a_claimed_app_leaves_a_durable_flag(self, tmp_path, spool):
        """The whole point: once the live payload is gone, the stored row still
        says this call produced an app."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-ok")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        assert rows, "the turn recorded no tool row to carry the flag"
        assert all(r["meta"].get(META_KEY) is True for r in rows)
        # The control token itself never reaches the transcript.
        assert all(sid not in (r["meta"].get("output") or "") for r in rows)

    @pytest.mark.asyncio
    async def test_the_frame_really_went_out_in_that_turn(self, tmp_path, spool):
        """Positive control on the harness: the flag above accompanies a real
        render, so the negative cases below are about the seam rather than about
        a turn where nothing happened."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-live")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        sent = [c.args[0] for c in state.broadcast_ws_owners.call_args_list if c.args]
        assert sent.count("mcp_app_render") == 1

    def test_a_slot_key_past_the_claim_bound_still_takes_the_claim(self, tmp_path, spool):
        """A key the claim cannot encode costs the recovery, never the app.

        The claim body bounds the slot key's length. FAILING the write when a key exceeds
        it propagates to the blanket handler around the claim, which strips the marker and
        records nothing -- so both halves go at once: no frame goes out AND the row
        carries no flag, leaving nothing to recover from either. Omitting the key from the
        body gives up only the restore-time attribution, so the claim is still taken here
        and the caller still renders.

        Pinned at the claim rather than through a driven turn: a key this long cannot
        reach the turn harness, because the session file it would be named after exceeds
        the filesystem's own filename limit. The claim is written to the spool under the
        record id, so it does not depend on that file.
        """
        overlong = "dashboard:" + "k" * 600
        assert len(overlong) > mcp_apps_render._MAX_SLOT_KEY_LEN
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, overlong)

        took = mcp_apps_render._take_claim(sid, overlong, "tc-1", ["2026-01-01T00:00:00Z"], ["m-1"])

        assert took == "took", "an unencodable key must not fail the claim"
        # The record's one render is spent, so a replayed marker cannot render it twice.
        assert (spool / f"{sid}.rendered").exists()


class TestTheFlagAlsoReachesOpenClients:
    @pytest.mark.asyncio
    async def test_a_live_meta_patch_carries_the_flag(self, tmp_path, spool):
        """`chat.mcpApps` is a BOUNDED cache, so a session that opens many apps
        evicts an older payload while its row is still on screen. Storing the
        flag alone would leave that row blank until a reload, so the flag is also
        pushed as a `chat_message_update` patch.

        The patch carries ONLY the flag: the reducer merges meta, and the row's
        output is capped at 1 MB and already delivered by its own event. It is
        addressed by the row's own ``ts`` rather than by tool-call id, because an
        auto-approved call has two rows sharing that id and the client's reducer
        patches only the newest of them.
        """
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-patch")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        patches = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message_update"
            and (c.args[1].get("meta") or {}).get(META_KEY)
        ]
        rows = _tool_rows(slot)
        assert len(patches) == len(rows), "one patch per flagged row"
        assert {p["ts"] for p in patches} == {str(r.get("ts")) for r in rows}
        assert all(p["slot"] == slot.key for p in patches)
        # The LEAD patch carries the extra marker; the sibling carries the flag
        # alone. Asserted as an exact set per patch rather than a subset, so a
        # stray key cannot ride along unnoticed.
        assert patches[0]["meta"] == {META_KEY: True, LEAD_KEY: True}
        assert all(p["meta"] == {META_KEY: True} for p in patches[1:])
        # Addressing a row by id would be ambiguous, so the patch must not.
        assert all("tool_call_id" not in p for p in patches)

    @pytest.mark.asyncio
    async def test_an_ordinary_tool_call_patches_nothing(self, tmp_path, spool):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-patch-none")
        slot._titled = True

        await _drive(state, slot, _events(""))

        assert not [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message_update"
            and (c.args[1].get("meta") or {}).get(META_KEY)
        ]


class TestNoViewerStillRecordsTheApp:
    @pytest.mark.asyncio
    async def test_zero_owner_sockets_still_records_it(self, tmp_path, spool):
        """An unattended run has no dashboard attached, and
        ``broadcast_ws_owners`` returns early when no owner socket is registered,
        so the payload reaches no browser. The row is still flagged, and that is
        the decision rather than an oversight: gating on a delivered count would
        write nothing for exactly the run where the reader arrives afterwards and
        most needs to be told the app exists.

        This leaves the real ``broadcast_ws_owners`` in place, unstubbed, so the
        zero-delivery path is the shipped one rather than a fake.
        """
        state = _stub_state(tmp_path)
        del state.broadcast_ws_owners  # fall through to the real method
        assert not getattr(state, "_owner_ws_clients", None), "fixture has an owner socket"
        slot = state.get_or_create_slot("app-unattended")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        assert rows
        assert all(r["meta"].get(META_KEY) is True for r in rows)


class TestNothingIsFlaggedWithoutAClaim:
    @pytest.mark.asyncio
    async def test_an_ordinary_tool_call_is_not_flagged(self, tmp_path, spool):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-none")
        slot._titled = True

        await _drive(state, slot, _events(""))

        rows = _tool_rows(slot)
        assert rows
        assert all(META_KEY not in r["meta"] for r in rows)

    @pytest.mark.asyncio
    async def test_a_marker_whose_record_is_gone_is_not_flagged(self, tmp_path, spool):
        """An expired or swept record yields no app, so there is nothing to
        point at. Flagging on the marker alone would put the notice on a row
        that produced no app, which is why the seam reports the claim instead of
        the caller testing for a marker."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-expired")
        slot._titled = True
        sid = uuid.uuid4().hex  # deliberately no spool file

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        assert rows
        assert all(META_KEY not in r["meta"] for r in rows)
        # Still stripped, so the user never sees the control token.
        assert all(sid not in (r["meta"].get("output") or "") for r in rows)

    @pytest.mark.asyncio
    async def test_a_marker_bound_to_another_session_is_not_flagged(self, tmp_path, spool):
        """A replayed marker refused by the session-binding check leaves THIS
        session's row with no app of its own."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-foreign")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, "dashboard:someone-else")

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        assert rows
        assert all(META_KEY not in r["meta"] for r in rows)


class TestAPreservedRowSharingTheIdIsNotFlagged:
    """A reset that keeps the transcript must not let an old row take the flag.

    A project change recreates the session without clearing ``slot.messages``,
    so a historical tool row survives it. ``tool_call_id`` is the BACKEND's id
    and session-local, so the call after that reset can legitimately be issued
    an id the old row already holds. Selecting rows by id alone then returns
    both, and the flag is written to both -- permanently, since it is never
    written ``False``. Because the client draws the notice on the FIRST flagged
    row for an id, the stale row would answer for the app and the row that
    actually has it would be passed over.
    """

    @pytest.mark.asyncio
    async def test_only_the_row_from_this_turn_carries_the_flag(self, tmp_path, spool):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-reused-id")
        slot._titled = True

        # The survivor of the reset: same id, appended BEFORE this turn, and
        # with no app of its own.
        stale = slot.append(
            "tool",
            "\U0001f527 create_view",
            "msg msg-tool",
            meta={"tool_call_id": TOOL_CALL_ID, "done": True},
        )
        stale_ts = str(stale.get("ts") or "")
        assert stale_ts, "the harness must give the historical row a ts to be identified by"
        assert META_KEY not in stale["meta"]

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        fresh = [r for r in rows if str(r.get("ts") or "") != stale_ts]
        assert fresh, "the turn recorded no row of its own to carry the flag"
        assert all(
            r["meta"].get(META_KEY) is True for r in fresh
        ), "this turn's own row must still be flagged"
        assert (
            META_KEY not in stale["meta"]
        ), "the preserved row never had an app and must not claim one"

    @pytest.mark.asyncio
    async def test_no_live_patch_addresses_the_preserved_row(self, tmp_path, spool):
        """The stored rows and the live patches must agree.

        A patch is addressed by a row's own ``ts``, so one naming the historical
        row would flag it on every open client even though nothing was persisted
        there -- the same false notice, reached by the other half of the pair.
        """
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-reused-id-ws")
        slot._titled = True
        stale = slot.append(
            "tool",
            "\U0001f527 create_view",
            "msg msg-tool",
            meta={"tool_call_id": TOOL_CALL_ID, "done": True},
        )
        stale_ts = str(stale.get("ts") or "")
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        patched = [
            c.args[1].get("ts")
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_message_update"
        ]
        assert patched, "no flag patch was sent at all, so this proves nothing"
        assert stale_ts not in patched

    @pytest.mark.asyncio
    async def test_the_claim_records_only_the_row_from_this_turn(self, tmp_path, spool):
        """The DURABLE half: the sidecar must not name the preserved row.

        The claim is what a later replay is attributed against, so a stale row's
        ``ts`` written into it outlives this turn: the next time that row reaches
        the seam the claim answers ``spent`` instead of ``inert``, and the row
        that never had an app is flagged as though it had lost one. The stored
        flag and the live patch above are both same-turn, so neither can see
        this; only the sidecar's own content can.
        """
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-reused-id-claim")
        slot._titled = True
        stale = slot.append(
            "tool",
            "\U0001f527 create_view",
            "msg msg-tool",
            meta={"tool_call_id": TOOL_CALL_ID, "done": True},
        )
        stale_ts = str(stale.get("ts") or "")
        assert stale_ts, "the harness must give the historical row a ts"

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        sentinel = spool / f"{sid}.rendered"
        assert sentinel.exists(), "the claim was never taken, so this proves nothing"
        claimed = json.loads(sentinel.read_text(encoding="utf-8"))["rows"]
        fresh_ts = [
            str(r.get("ts") or "") for r in _tool_rows(slot) if str(r.get("ts") or "") != stale_ts
        ]
        assert fresh_ts, "the turn recorded no row of its own"
        assert set(claimed) & set(fresh_ts), "the claim must name the row that owns the app"
        assert stale_ts not in claimed, "the claim must not name the preserved row"


class TestOneRowPerOccurrenceIsMarkedTheLead:
    """The client draws the notice on ONE row per call, and the backend picks it.

    An auto-approved call appends a pre- and a post-approval row under one
    ``tool_call_id`` and both are flagged, so something must choose between them
    or one lost app reads as two. That choice cannot be derived on the client: a
    pre-reset row that had its own app keeps ``mcp_app`` for good -- the flag is
    never written ``False`` -- so any rule picking the earliest flagged row for an
    id answers with that stale row. Keying on the earliest row for the id drew
    nothing at all (the stale row is unflagged), and keying on the earliest
    FLAGGED row suppressed the newer app's notice whenever the stale row had had
    an app. Only the turn that appended the rows knows which are its own, so the
    turn stamps the lead.
    """

    @pytest.mark.asyncio
    async def test_exactly_one_flagged_row_is_the_lead(self, tmp_path, spool):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-lead")
        slot._titled = True
        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events_two_rows(f"[kirocrew-mcp-app:{sid}]"))

        flagged = [r for r in _tool_rows(slot) if r["meta"].get(META_KEY) is True]
        assert len(flagged) >= 2, (
            "the occurrence produced fewer than two flagged rows, so a lead "
            "written once and a lead written on every row are indistinguishable"
        )
        leads = [r for r in flagged if r["meta"].get(LEAD_KEY) is True]
        assert len(leads) == 1, "exactly one row of the occurrence is the lead"
        assert leads[0]["ts"] == flagged[0]["ts"], "the lead is the earliest flagged row"

    @pytest.mark.asyncio
    async def test_a_preserved_row_does_not_take_the_lead(self, tmp_path, spool):
        """The case the client could not get right, measured at the source.

        A historical row under the same id already carries the flag from ITS own
        app. The lead must still be this turn's row, because the marker is written
        only for rows this turn appended -- nothing here consults row order across
        the transcript.
        """
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-lead-reset")
        slot._titled = True
        stale = slot.append(
            "tool",
            "\U0001f527 create_view",
            "msg msg-tool",
            meta={"tool_call_id": TOOL_CALL_ID, "done": True, META_KEY: True, LEAD_KEY: True},
        )
        stale_ts = str(stale.get("ts") or "")
        assert stale_ts

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        fresh = [r for r in _tool_rows(slot) if str(r.get("ts") or "") != stale_ts]
        assert fresh, "the turn recorded no row of its own"
        assert any(
            r["meta"].get(LEAD_KEY) is True for r in fresh
        ), "this turn's occurrence must have its own lead"
        # The preserved row keeps what it had; this turn does not rewrite it.
        assert stale["meta"].get(LEAD_KEY) is True
        assert stale["meta"].get(META_KEY) is True


class TestTheClaimIsReadWithTheKeyItWasWrittenUnder:
    """A claim records the CANONICAL producing session, not the bare slot key.

    ``_take_claim`` is handed ``binding_key``, so that is what the claim body
    records, while the bare slot key is what every other chat event routes on. A
    restore asking with the bare key matches nothing and recovers nothing, in
    silence. The reader takes a KEY, so what needs pinning is every caller that
    CHOOSES one -- there are three, and one of them has no slot to ask.
    """

    class _Slot:
        key = "chat-1-1"
        linked_session_key = "slack:1700000000.1"

        def __init__(self):
            self.messages = []
            self._dirty = False

    def _spy(self, monkeypatch) -> list[str]:
        asked: list[str] = []
        monkeypatch.setattr(
            chat_persistence.mcp_apps_render,
            "load_claimed_row_groups",
            lambda key: asked.append(key) or [],
        )
        return asked

    def test_the_inline_wrapper_asks_for_the_canonical_key(self, monkeypatch):
        slot = self._Slot()
        asked = self._spy(monkeypatch)

        chat_persistence._recover_mcp_app_claims(slot)

        assert asked == [effective_session_key(slot)], "a bare key recovers nothing"
        assert asked != [slot.key]

    @pytest.mark.asyncio
    async def test_the_threaded_wrapper_asks_for_the_canonical_key(self, monkeypatch):
        slot = self._Slot()
        asked = self._spy(monkeypatch)

        await chat_persistence._recover_mcp_app_claims_async(slot)

        assert asked == [effective_session_key(slot)]
        assert asked != [slot.key]

    def test_the_pre_build_derivation_matches_what_the_built_slot_would_answer(self):
        """The third chooser has no slot yet, so it derives the key instead.

        That derivation and ``effective_session_key`` must answer alike or the
        targeted rehydration reads a key the live path never writes -- and it fails
        in silence, recovering nothing. Both now go through ``session_key_for``, so
        this pins the pair rather than a second spelling of the rule.
        """
        for linked in ("", "slack:1700000000.1", "discord:abc"):
            slot = self._Slot()
            slot.linked_session_key = linked
            derived = session_key_for(slot.key, linked)
            assert derived == effective_session_key(slot), linked
        # And the fallback is NOT the transcript key, which cannot carry a session.
        assert session_key_for("slack_1700000000.1") == "dashboard:slack_1700000000.1"

    @pytest.mark.asyncio
    async def test_a_channel_slots_key_comes_from_its_persisted_link(self, monkeypatch):
        """The third chooser must read ``linked_session_key`` out of the metadata.

        A channel-born slot's NAME is its transcript's filename stem, and the
        session key cannot be recovered from it -- so deriving from the name alone
        addresses a ``dashboard:`` session that never existed and recovers nothing.
        This is the one case where the name and the key genuinely differ.
        """
        linked = "slack:1700000000.1"
        built = self._Slot()
        built.key = "slack_1700000000.1"
        built.linked_session_key = linked
        asked = self._spy(monkeypatch)
        monkeypatch.setattr(
            chat_persistence,
            "_prefetch_rehydrate_inputs",
            lambda *a, **k: ({"linked_session_key": linked}, True, [{}], {}, None, None, {}),
        )
        monkeypatch.setattr(chat_persistence, "slot_closed_since", lambda *a, **k: False)
        monkeypatch.setattr(chat_persistence, "_deletion_during_read", lambda *a, **k: None)
        monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", lambda *a, **k: built)
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}

        await chat_persistence.rehydrate_slot_from_history_async(state, built.key)

        assert asked == [linked], "the name is the file stem, not the session"
        assert asked != ["dashboard:slack_1700000000.1"]

    @pytest.mark.asyncio
    async def test_an_unbound_channel_slot_is_asked_for_what_the_write_used(self, monkeypatch):
        """The fallback must be ``effective_session_key``'s, not the transcript's.

        A channel-born slot the dashboard could not bind has an EMPTY
        ``linked_session_key`` -- ``surface_channel_session`` surfaces it unbound
        rather than guess a key the channel never reads. That is the one case where
        the two candidate fallbacks disagree, because ``slot_transcript_key``
        returns a channel stem unchanged while ``_history_key_for`` prefixes
        ``dashboard:``.

        The write side settles it: ``chat_runner`` claims with
        ``producing_session_key=effective_session_key(slot)``, so that is the key
        the claim is filed under and the only key a read can match. Asking with the
        bare stem finds nothing and recovers nothing, in silence. This has to be
        asserted HERE, at the call site that chooses the key, because the sibling
        test above drives ``session_key_for`` directly and a call site swapped to
        the transcript key passes it untouched.
        """
        built = self._Slot()
        built.key = "slack_1700000000.1"
        built.linked_session_key = ""
        asked = self._spy(monkeypatch)
        monkeypatch.setattr(
            chat_persistence,
            "_prefetch_rehydrate_inputs",
            lambda *a, **k: ({}, True, [{}], {}, None, None, {}),
        )
        monkeypatch.setattr(chat_persistence, "slot_closed_since", lambda *a, **k: False)
        monkeypatch.setattr(chat_persistence, "_deletion_during_read", lambda *a, **k: None)
        monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", lambda *a, **k: built)
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}

        await chat_persistence.rehydrate_slot_from_history_async(state, built.key)

        assert asked == [effective_session_key(built)], (
            "an unbound channel slot must be asked for the key the claim was "
            "written under, which is the one the live path computes"
        )
        assert asked == ["dashboard:slack_1700000000.1"]
        assert asked != [built.key], "the transcript key names no session"


class TestARecoveredFlagIsLeftToBeSaved:
    """A recovery that is never persisted is a notice lost at the sidecar's TTL.

    The restore paths mark the slot clean as their last act, so the recovery runs
    after that and has to re-arm ``_dirty`` itself. Nothing else will: the flag is
    in memory only, and the claim it came from is swept at its 24h TTL.
    """

    class _Slot:
        key = "chat-1-1"

        def __init__(self, rows):
            self.messages = rows
            self._dirty = False

    def test_a_recovered_row_leaves_the_slot_dirty(self):
        ts = "2026-01-01T00:00:00.000001Z"
        slot = self._Slot([{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}])

        chat_persistence._reconcile_mcp_app_claims(slot, [{ts}])

        assert slot.messages[0]["meta"][META_KEY] is True
        assert slot._dirty is True, "an unsaved recovery is lost at the sidecar TTL"

    def test_recovering_nothing_leaves_the_slot_clean(self):
        """A restore that recovered nothing must not be re-saved for no reason."""
        slot = self._Slot([{"role": "tool", "ts": "t1", "meta": {"tool_call_id": "tc"}}])

        chat_persistence._reconcile_mcp_app_claims(slot, [])

        assert slot._dirty is False
        assert META_KEY not in slot.messages[0]["meta"]

    def test_a_row_that_kept_its_flag_does_not_re_dirty_the_slot(self):
        """Already flagged means already saved: nothing was recovered."""
        ts = "2026-01-01T00:00:00.000001Z"
        slot = self._Slot(
            [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc", META_KEY: True}}]
        )

        chat_persistence._reconcile_mcp_app_claims(slot, [{ts}])

        assert slot._dirty is False, "no new state, so no new save"


class TestTargetedRehydrationRecoversToo:
    """One slot rebuilt on demand recovers, the same as a bulk restore.

    ``rehydrate_slot_from_history_async`` is the ONLY way anything outside
    ``chat_persistence`` rebuilds a single slot from disk, and it has eight such
    callers. Wiring them one at a time is how the bulk drivers were first done and
    two of four were missed, so the recovery sits inside this function and these
    pin it there: the first drives the REAL reader against a real claim on disk,
    so a recovery removed from the wrapper cannot pass by stubbing.

    The live-slot early return is pinned the other way. Nothing was read from disk
    on that path and the flags are already in memory, so a spool scan there would
    be paid per cache hit on a lookup ``messaging`` documents as O(1).
    """

    class _Slot:
        key = "chat-1-1"
        linked_session_key = None

        def __init__(self, rows):
            self.messages = rows
            self._dirty = False

    @staticmethod
    def _row(ts: str) -> dict:
        return {"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}

    def _stub_rehydration(self, monkeypatch, built):
        """Stand in for the transcript machinery, never for the recovery."""
        monkeypatch.setattr(
            chat_persistence,
            "_prefetch_rehydrate_inputs",
            lambda *a, **k: ({"title": "t"}, True, [{}], {}, None, None, {}),
        )
        monkeypatch.setattr(chat_persistence, "slot_closed_since", lambda *a, **k: False)
        monkeypatch.setattr(chat_persistence, "_deletion_during_read", lambda *a, **k: None)
        monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", lambda *a, **k: built)

    @pytest.mark.asyncio
    async def test_a_slot_rebuilt_on_demand_gets_its_flag_back(self, monkeypatch, spool):
        ts = "2026-01-01T00:00:00.000001Z"
        built = self._Slot([self._row(ts)])
        # Under the CANONICAL key, because that is what the claim records and what
        # the reader asks for; writing it under the bare slot key recovers nothing.
        (spool / "sid.rendered").write_text(
            json.dumps({"slot_key": effective_session_key(built), "rows": [ts]}),
            encoding="utf-8",
        )
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}
        self._stub_rehydration(monkeypatch, built)

        got = await chat_persistence.rehydrate_slot_from_history_async(state, built.key)

        assert got is built
        assert got.messages[0]["meta"][META_KEY] is True, (
            "a targeted rehydration returned a row whose claim was spent and whose "
            "flag the crash lost, so the notice is gone for this session"
        )
        assert got._dirty is True, "an unsaved recovery is lost at the sidecar TTL"

    @pytest.mark.asyncio
    async def test_a_rebuild_that_finds_no_claim_is_left_clean(self, monkeypatch, spool):
        """Otherwise every targeted rehydration would re-save for nothing."""
        built = self._Slot([self._row("2026-01-01T00:00:00.000001Z")])
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}
        self._stub_rehydration(monkeypatch, built)

        got = await chat_persistence.rehydrate_slot_from_history_async(state, built.key)

        assert got is built
        assert META_KEY not in got.messages[0]["meta"]
        assert got._dirty is False

    @pytest.mark.asyncio
    async def test_a_live_slot_is_handed_back_without_reading_the_spool(self, monkeypatch):
        live = self._Slot([self._row("2026-01-01T00:00:00.000001Z")])
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {live.key: live}
        reads: list[str] = []
        monkeypatch.setattr(
            chat_persistence.mcp_apps_render,
            "load_claimed_row_groups",
            lambda key: reads.append(key) or [],
        )

        got = await chat_persistence.rehydrate_slot_from_history_async(state, live.key)

        assert got is live
        assert reads == [], "a cache hit read nothing from disk, so it recovers nothing"


class TestNoAwaitSeparatesTheDeletionCheckFromTheBuild:
    """The recovery's spool read runs in the PREFETCH phase, not after the build.

    ``rehydrate_slot_from_history_async`` places its two race re-checks
    synchronously and immediately before the build so no await can reopen the
    windows they close. An await after the build reopens the deletion one a step
    later and worse: the slot is published by then and RETURNED, so a deletion
    landing during that await hands the caller a slot for a session that has been
    deleted, it accepts a message, and the delete-won save discards it. The remedy
    is the split this module uses everywhere else.

    The two bulk drivers are a different case and stay as they are: each already
    yields with ``await asyncio.sleep(0)`` at that exact point by construction, and
    neither hands the slot back to anyone, so there is no return value through
    which a stale slot becomes reachable.
    """

    class _Slot:
        linked_session_key = None

        def __init__(self, key, rows):
            self.key = key
            self.messages = rows
            self._dirty = False

    def _stub(self, monkeypatch, built, order):
        monkeypatch.setattr(
            chat_persistence,
            "_prefetch_rehydrate_inputs",
            lambda *a, **k: ({"title": "t"}, True, [{}], {}, None, None, {}),
        )
        monkeypatch.setattr(chat_persistence, "slot_closed_since", lambda *a, **k: False)
        monkeypatch.setattr(
            chat_persistence,
            "_rehydrate_slot_from_history",
            lambda *a, **k: order.append("build") or built,
        )

    @pytest.mark.asyncio
    async def test_the_spool_is_read_before_the_deletion_check(self, monkeypatch):
        order: list[str] = []
        built = self._Slot("chat-1-1", [])
        self._stub(monkeypatch, built, order)
        monkeypatch.setattr(
            chat_persistence,
            "_read_mcp_app_claims",
            lambda key: order.append("read") or [],
        )
        monkeypatch.setattr(
            chat_persistence,
            "_deletion_during_read",
            lambda *a, **k: order.append("deletion-check"),
        )
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}

        await chat_persistence.rehydrate_slot_from_history_async(state, "chat-1-1")

        assert order == ["read", "deletion-check", "build"], (
            "the spool read must happen before the deletion check, or the await it "
            "needs lands after the check and reopens the window"
        )

    @pytest.mark.asyncio
    async def test_a_deletion_landing_during_the_read_still_refuses(self, monkeypatch):
        """The consequence of the ordering, not just the ordering.

        The read is the slow part, so it is where a deletion lands. With the read
        in front of the check, the check sees it and the rebuild is refused.
        """
        built = self._Slot("chat-1-1", [])
        self._stub(monkeypatch, built, [])
        deleted: list[bool] = []
        monkeypatch.setattr(
            chat_persistence,
            "_read_mcp_app_claims",
            lambda key: deleted.append(True) or [],
        )
        monkeypatch.setattr(
            chat_persistence,
            "_deletion_during_read",
            lambda *a, **k: "deleted" if deleted else None,
        )
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}

        got = await chat_persistence.rehydrate_slot_from_history_async(state, "chat-1-1")

        assert got is None, (
            "a session deleted while the spool was read must not be rebuilt and "
            "handed to a caller that will write into it"
        )

    @pytest.mark.asyncio
    async def test_the_apply_after_the_build_awaits_nothing(self, monkeypatch):
        """A sync apply is what keeps the check adjacent to the build.

        Driving the real apply with a real claim proves the recovery still lands
        through the synchronous path, so this is not merely an ordering assertion.
        """
        ts = "2026-01-01T00:00:00.000001Z"
        built = self._Slot("chat-1-1", [{"role": "tool", "ts": ts, "meta": {}}])
        self._stub(monkeypatch, built, [])
        monkeypatch.setattr(chat_persistence, "_deletion_during_read", lambda *a, **k: None)
        monkeypatch.setattr(chat_persistence, "_read_mcp_app_claims", lambda key: [{ts}])
        monkeypatch.setattr(
            chat_persistence,
            "_recover_mcp_app_claims_async",
            AsyncMock(side_effect=AssertionError("the apply must not await")),
        )
        state = MagicMock()
        state.conversation_log = MagicMock()
        state._slots = {}

        got = await chat_persistence.rehydrate_slot_from_history_async(state, "chat-1-1")

        assert got is built
        assert got.messages[0]["meta"][META_KEY] is True
        assert got._dirty is True


class TestTheFlagAndItsLeadReachTheRowTogether:
    """Both keys land in ONE write, because a half-written row renders nothing.

    The periodic flush takes ``slot.messages`` as a shallow copy of REFERENCES and
    serializes those same live ``meta`` dicts on a worker thread. Two separate
    assignments are therefore visible half-done: a snapshot between them stores
    ``mcp_app`` with no ``mcp_app_lead``. The client draws the notice on the lead
    alone, so that row shows nothing at all.

    Nothing repairs it either, which is what makes the ordering worth pinning rather
    than arguing: this same pass reads a surviving ``mcp_app`` as proof the lead
    survived, so it marks the claim's lead taken and skips the row, and the claim is
    swept at its TTL.
    """

    class _Recording(dict):
        """A meta dict that remembers HOW it was written, not just what it holds."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.key_sets: list[str] = []
            self.bulk_updates: list[tuple[str, ...]] = []

        def __setitem__(self, key, value):
            self.key_sets.append(key)
            super().__setitem__(key, value)

        def update(self, *args, **kwargs):
            if args and isinstance(args[0], dict):
                self.bulk_updates.append(tuple(sorted(args[0])))
            super().update(*args, **kwargs)

    def test_both_keys_arrive_in_one_update(self):
        meta = self._Recording({"mid": "m-1"})
        rows = [{"role": "tool", "ts": "2026-01-01T00:00:01.000001Z", "meta": meta}]

        flagged = mcp_apps_render.apply_claimed_rows([{"m-1"}], rows)

        assert flagged == 1, "the row should have been recovered"
        assert meta["mcp_app"] is True and meta["mcp_app_lead"] is True
        assert meta.bulk_updates == [("mcp_app", "mcp_app_lead")], (
            "the flag and its lead must reach the row in ONE update: a flush "
            "serializing this dict between two assignments stores a flagged row with "
            "no lead, and no pass repairs that"
        )
        assert (
            "mcp_app" not in meta.key_sets and "mcp_app_lead" not in meta.key_sets
        ), "neither key may be assigned on its own"

    def test_a_non_lead_row_still_arrives_in_one_update(self):
        first = self._Recording({"mid": "m-1"})
        second = self._Recording({"mid": "m-2"})
        rows = [
            {"role": "tool", "ts": "2026-01-01T00:00:01.000001Z", "meta": first},
            {"role": "tool", "ts": "2026-01-01T00:00:02.000001Z", "meta": second},
        ]

        flagged = mcp_apps_render.apply_claimed_rows([{"m-1", "m-2"}], rows)

        assert flagged == 2
        assert first.bulk_updates == [("mcp_app", "mcp_app_lead")]
        assert second.bulk_updates == [("mcp_app",)], (
            "a row that is not the lead still takes its flag through one update, so "
            "the write shape does not depend on which row won the lead"
        )
        assert second.get("mcp_app_lead") is None


class TestAClaimIsMatchedByRowIdentityNotTimestamp:
    """A ``ts`` does not name a row, so the claim is attributed by ``meta.mid``.

    ``_ChatSlot.append`` preserves an explicit ``ts`` verbatim for a row replayed
    from a channel transcript -- rewriting it would reorder the replay -- and a
    coarse clock stamps two same-tick appends identically, which is the collision
    ``meta.mid`` is minted per row to answer. A concurrent foreign append sharing
    the app row's timestamp would therefore make the restore flag a row that never
    had an app, and flag it DURABLY: ``mcp_app`` is never written back ``False``.

    The ids and the timestamps are never mixed. A claim carrying ids is matched on
    ids ALONE; one carrying none was written before the ids existed and keeps the
    ts match, because that is the only identity it has. On that fallback an
    ambiguous ts -- one naming more than one restored row -- identifies neither, so
    neither is flagged: silence on a row is this module's failure direction, a
    false notice is not.
    """

    KEY = "dashboard:chat-9-9"
    TS = "2026-01-01T00:00:00.000001Z"

    @staticmethod
    def _claim(spool, name: str, body: dict) -> Path:
        path = spool / f"{name}.rendered"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    @staticmethod
    def _row(ts: str, mid: str | None = None) -> dict:
        meta: dict = {"tool_call_id": "tc"}
        if mid:
            meta["mid"] = mid
        return {"role": "tool", "ts": ts, "meta": meta}

    def _recover(self, rows: list[dict]) -> int:
        groups = mcp_apps_render.load_claimed_row_groups(self.KEY)
        return mcp_apps_render.apply_claimed_rows(groups, rows)

    def test_a_foreign_row_sharing_the_timestamp_is_not_flagged(self, spool):
        """The reported defect: only the row the claim NAMES may be flagged."""
        self._claim(
            spool,
            "sid",
            {"slot_key": self.KEY, "rows": [self.TS], "row_ids": ["m-app"]},
        )
        app = self._row(self.TS, "m-app")
        foreign = self._row(self.TS, "m-foreign")

        flagged = self._recover([app, foreign])

        assert flagged == 1
        assert app["meta"].get("mcp_app") is True
        assert "mcp_app" not in foreign["meta"], (
            "a row that merely shares the claimed ts never had this app, and the "
            "flag it would get is durable"
        )

    def test_a_claim_with_ids_ignores_its_own_recorded_timestamp(self, spool):
        """A claim carrying ids is matched on ids ALONE, never on its own ts too.

        Reachable when the claim's own row has left the transcript -- rewound, or
        trimmed -- while another row carries the ``ts`` it recorded. That ts is then
        UNAMBIGUOUS, so the fallback's shared-ts guard does not fire and cannot
        help; only keeping the two identities unmixed refuses the stranger. Without
        this case the mixing is invisible, because in a transcript that still holds
        both rows the shared-ts guard blocks the stranger for a different reason.
        """
        self._claim(
            spool,
            "sid",
            {"slot_key": self.KEY, "rows": [self.TS], "row_ids": ["m-app"]},
        )
        stranger = self._row(self.TS, "m-stranger")

        assert self._recover([stranger]) == 0
        assert "mcp_app" not in stranger["meta"], (
            "the claim named a row by id; a stranger holding the ts it also "
            "recorded is not that row"
        )

    def test_a_claim_written_before_the_ids_still_recovers(self, spool):
        """The fallback has to work, or an upgrade silently stops recovering."""
        self._claim(spool, "sid", {"slot_key": self.KEY, "rows": [self.TS]})
        row = self._row(self.TS, "m-app")

        assert self._recover([row]) == 1
        assert row["meta"].get("mcp_app") is True

    def test_an_ambiguous_timestamp_on_the_fallback_flags_nothing(self, spool):
        """Two rows, one ts, no ids: the claim names neither, so neither is flagged."""
        self._claim(spool, "sid", {"slot_key": self.KEY, "rows": [self.TS]})
        first = self._row(self.TS, "m-one")
        second = self._row(self.TS, "m-two")

        assert self._recover([first, second]) == 0
        assert "mcp_app" not in first["meta"]
        assert "mcp_app" not in second["meta"]

    def test_a_spent_claim_is_attributed_by_id_not_by_timestamp(self, spool):
        """The same defect on the IN-TURN half, where the wrong answer is ``spent``.

        ``spent`` is reported to the caller as ``app_on_row``, so attributing it to
        a row the claim does not name puts the absence notice on that row -- the one
        outcome this attribution exists to prevent.
        """
        sentinel = self._claim(
            spool,
            "sid",
            {
                "slot_key": self.KEY,
                "tool_call_id": "tc-old",
                "rows": [self.TS],
                "row_ids": ["m-app"],
            },
        )

        assert (
            mcp_apps_render._attribute_spent_claim(
                sentinel, "sid", self.KEY, "tc-new", [self.TS], ["m-foreign"]
            )
            == "inert"
        ), "sharing the claimed ts is not owning the claim"
        assert (
            mcp_apps_render._attribute_spent_claim(
                sentinel, "sid", self.KEY, "tc-new", [self.TS], ["m-app"]
            )
            == "spent"
        )


class TestACollapsedCallIdFlagsNothing:
    """A redacted call id names every redacted call, so it names none of them.

    The rows store the REDACTED ``tool_call_id`` and the turn compares against
    the redacted form, which is the convention ``_tool_meta`` documents. Credential
    redaction does not mask a span inside the value: it replaces the whole field
    with one constant, so two different calls whose ids both look like a
    credential end up byte-identical. Selecting rows by that value then returns
    another call's row as an owner of this call's app, and the flag it receives is
    permanent -- it is never written ``False``, and the client draws its notice on
    the first flagged row it finds for the id.

    So the flag is withheld where the id cannot name one call. That fails towards
    silence, which a later render repairs; a flag on a row that never had an app
    does not repair.
    """

    #: Two DISTINCT ids that redact to one string. A JWT-shaped value is matched
    #: WHOLESALE by the credential detector -- the whole field becomes one
    #: constant rather than a span inside it being masked -- so two of them
    #: differing anywhere arrive at every later reader byte-identical. That
    #: wholesale property is what this class is about, and ``_premise`` asserts it
    #: rather than trusting it.
    #:
    #: A JWT shape rather than the more familiar cloud-key one deliberately: an
    #: access-key literal is matched by GitHub's own push protection, which
    #: refuses the push outright, and writing one in fragments to slip past that
    #: check would be hiding a credential pattern from a security control for a
    #: value that is not a credential at all. The header segment below is public
    #: (it decodes to an algorithm name), the payload differs between the two,
    #: and the signature segment is filler.
    _JWT_HEAD = "eyJhbGciOiJIUzI1NiJ9"
    _JWT_SIG = "s" * 20
    ID_APP = f"{_JWT_HEAD}.{'a' * 30}.{_JWT_SIG}"
    ID_OTHER = f"{_JWT_HEAD}.{'b' * 30}.{_JWT_SIG}"

    def _premise(self) -> str:
        """The collapse this class is about, asserted rather than assumed.

        If the detector stops matching these, every assertion below would pass
        for the wrong reason, so the premise fails the test loudly instead.
        """
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        assert self.ID_APP != self.ID_OTHER, "the two ids must really differ"
        left = _redact_tool_field(self.ID_APP)
        right = _redact_tool_field(self.ID_OTHER)
        assert left != self.ID_APP, "the premise needs the id to be redacted at all"
        assert left == right, "the premise needs the two ids to collapse to one string"
        return left

    def _events(self, marker: str) -> list[AcpEvent]:
        """Two calls in one turn: the app's, and an unrelated tool's."""
        return [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self.ID_APP,
                title="create_view",
                tool_name="create_view",
                mcp_server_name="excalidraw",
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self.ID_OTHER,
                title="read_file",
                tool_name="read_file",
                mcp_server_name="filesystem",
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=self.ID_APP,
                tool_output=f"Done - two rectangles and an arrow. {marker}",
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
            AcpEvent(kind=EVENT_COMPLETE),
        ]

    @pytest.mark.asyncio
    async def test_an_unrelated_call_row_is_not_flagged(self, tmp_path, spool):
        collapsed = self._premise()

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-collapsed-id")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, self._events(f"[kirocrew-mcp-app:{sid}]"))

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(rows) >= 2, "the harness must append a row for each of the two calls"
        assert all(
            r.get("meta", {}).get("tool_call_id") == collapsed for r in rows
        ), "both rows must store the collapsed id, or this test is not exercising the collapse"

        other = [r for r in rows if "read_file" in (r.get("content") or "")]
        assert other, "the unrelated call recorded no row to check"
        for r in other:
            assert (
                META_KEY not in r["meta"]
            ), "an unrelated call's row must not claim an app it never had"
            assert LEAD_KEY not in r["meta"], "nor may it be the row the notice is drawn on"

    @pytest.mark.asyncio
    async def test_no_row_at_all_takes_the_flag_when_the_id_collapses(self, tmp_path, spool):
        """The app's own row is given up too, and deliberately.

        Both rows carry the same stored value, so there is nothing left to tell
        the app's row from the other one. Flagging the app's row would mean
        flagging both, which is the defect; flagging neither is the honest
        answer, and the frame still renders for the live client.
        """
        self._premise()

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-collapsed-none")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, self._events(f"[kirocrew-mcp-app:{sid}]"))

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        flagged = [r for r in rows if META_KEY in r.get("meta", {})]
        assert not flagged, (
            "no row can be attributed from a collapsed id, so none may be flagged; "
            f"{len(flagged)} was"
        )

    @pytest.mark.asyncio
    async def test_the_claim_names_no_row_when_the_id_collapses(self, tmp_path, spool):
        """The DURABLE half, which the in-turn flag assertions cannot see.

        The claim is what a later replay is attributed against, so a row named in
        it outlives this turn: the next time that row reaches the seam the claim
        answers ``spent`` rather than ``inert`` and the row is flagged as though it
        had lost an app. Selecting owners by a collapsed id would write the
        unrelated call's row into the claim, so the restore would flag it on a
        later reload even though nothing was flagged during the turn.

        An empty row list is the documented degrade rather than a failure: the
        claim is still written for its render-once half and simply attributes
        nothing.
        """
        self._premise()

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-collapsed-claim")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, self._events(f"[kirocrew-mcp-app:{sid}]"))

        sentinel = spool / f"{sid}.rendered"
        assert sentinel.exists(), "the claim was never taken, so this proves nothing"
        body = json.loads(sentinel.read_text(encoding="utf-8"))

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        row_ts = {str(r.get("ts") or "") for r in rows}
        row_ids = {str(r.get("meta", {}).get("mid") or "") for r in rows}
        assert len(rows) >= 2, "the harness must append a row for each of the two calls"

        assert not set(body.get("rows") or []) & row_ts, (
            "a collapsed id cannot say which row owns the app, so the claim must name none; "
            f"it named {body.get('rows')!r}"
        )
        assert not set(body.get("row_ids") or []) & row_ids, (
            "nor may it record a row identity it cannot attribute; "
            f"it named {body.get('row_ids')!r}"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_id_still_flags_its_row(self, tmp_path, spool):
        """The guard is conditional, not a blanket refusal.

        An ordinary id survives redaction unchanged, still names one call, and
        must keep flagging its own row -- otherwise the fix above would have
        turned the feature off rather than made it honest.
        """
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        assert (
            _redact_tool_field(TOOL_CALL_ID) == TOOL_CALL_ID
        ), "this test needs an id redaction leaves alone"

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-plain-id")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, _events(f"[kirocrew-mcp-app:{sid}]"))

        rows = _tool_rows(slot)
        assert rows, "the turn recorded no row for the ordinary id"
        assert any(
            r["meta"].get(META_KEY) is True for r in rows
        ), "an ordinary id must still record its app on its own row"


class TestAnIdThatAlreadyIsTheRedactionTag:
    """The collision a "record only what redaction changed" rule lets through.

    Credential redaction replaces the whole field with one constant, so an id
    whose raw value ALREADY equals that constant is left alone -- and is then
    byte-identical to what every credential-shaped id becomes. Recording only the
    values redaction changed leaves such a pair tracked as ONE source, which reads
    unambiguous, so the app notice lands on whichever row the walk reaches first
    and never repairs: the flag is never written ``False``.

    ``tool_call_id`` comes from the LLM -- the loop preamble says so in its own
    comment -- so this pair is producible rather than a freak coincidence, which
    is why every id is recorded and not only the changed ones.
    """

    @staticmethod
    def _tag() -> str:
        """The redaction tag, taken from the module that writes it."""
        from kiro_crew.security.redaction import REDACTED_CREDENTIAL_TAG

        return REDACTED_CREDENTIAL_TAG

    #: A credential-shaped id, in the JWT shape the sibling class explains.
    ID_APP = f"eyJhbGciOiJIUzI1NiJ9.{'c' * 30}.{'s' * 20}"

    def _premise(self) -> str:
        """Both halves of the collision, asserted rather than assumed."""
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        tag = self._tag()
        assert self.ID_APP != tag, "the two ids must really differ before redaction"
        assert (
            _redact_tool_field(self.ID_APP) == tag
        ), "the premise needs the credential-shaped id to redact ONTO the tag"
        assert _redact_tool_field(tag) == tag, (
            "the premise needs the tag itself to pass through redaction UNCHANGED -- "
            "that is what makes it indistinguishable from the redacted id"
        )
        return tag

    def _events(self, marker: str) -> list[AcpEvent]:
        """The app's call carries the tag literally; an unrelated call redacts onto it."""
        return [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self._tag(),
                title="create_view",
                tool_name="create_view",
                mcp_server_name="excalidraw",
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self.ID_APP,
                title="read_file",
                tool_name="read_file",
                mcp_server_name="filesystem",
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=self._tag(),
                tool_output=f"Done - two rectangles and an arrow. {marker}",
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
            AcpEvent(kind=EVENT_COMPLETE),
        ]

    @pytest.mark.asyncio
    async def test_no_row_is_flagged_when_a_literal_tag_shares_the_value(self, tmp_path, spool):
        collapsed = self._premise()

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-tag-literal")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, self._events(f"[kirocrew-mcp-app:{sid}]"))

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(rows) >= 2, "the harness must append a row for each of the two calls"
        assert all(
            r.get("meta", {}).get("tool_call_id") == collapsed for r in rows
        ), "both rows must store the same value, or this test is not exercising the collision"

        flagged = [r for r in rows if META_KEY in r.get("meta", {})]
        assert not flagged, (
            "an id that already is the redaction tag shares its value with every "
            "credential-shaped id, so it names no single call and no row may be "
            f"flagged from it; {len(flagged)} was"
        )
        lead = [r for r in rows if LEAD_KEY in r.get("meta", {})]
        assert not lead, "nor may any row be chosen as the one the notice is drawn on"

    @pytest.mark.asyncio
    async def test_the_claim_names_no_row_for_a_literal_tag_id(self, tmp_path, spool):
        """The durable half: the collision must not reach a later reload either."""
        self._premise()

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-tag-claim")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        await _drive(state, slot, self._events(f"[kirocrew-mcp-app:{sid}]"))

        sentinel = spool / f"{sid}.rendered"
        assert sentinel.exists(), "the claim was never taken, so this proves nothing"
        body = json.loads(sentinel.read_text(encoding="utf-8"))

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        row_ts = {str(r.get("ts") or "") for r in rows}
        row_ids = {str(r.get("meta", {}).get("mid") or "") for r in rows}

        assert not set(body.get("rows") or []) & row_ts, (
            "the claim must name no row it cannot attribute; " f"it named {body.get('rows')!r}"
        )
        assert not set(body.get("row_ids") or []) & row_ids, (
            "nor any row identity; " f"it named {body.get('row_ids')!r}"
        )


class TestTheRetainedIdentityKeyIsBounded:
    """What the turn RETAINS is bounded, not merely how many entries it keeps.

    A cap on the number of entries still retains that many externally-controlled
    strings, so the key is a digest: 16 characters however long the id was. An id
    too long to track at all names nothing rather than being kept.
    """

    def test_the_key_is_a_fixed_width_digest_however_long_the_id(self):
        from kiro_crew.dashboard.chat_runner import _MAX_TCID_LEN, _tcid_identity_key

        short = _tcid_identity_key("tool-call-1")
        assert len(short) == 16, f"an ordinary id must key to 16 chars, got {len(short)}"

        long_but_allowed = "t" * (_MAX_TCID_LEN - 1)
        keyed = _tcid_identity_key(long_but_allowed)
        assert len(keyed) == 16, (
            "the retained key must not grow with the id -- that is the whole point of "
            f"digesting it; got {len(keyed)} for a {len(long_but_allowed)}-char id"
        )

    def test_an_over_long_id_names_nothing(self):
        from kiro_crew.dashboard.chat_runner import _MAX_TCID_LEN, _tcid_identity_key

        assert _tcid_identity_key("x" * (_MAX_TCID_LEN + 1)) == "", (
            "an id past the tracking length cannot be told from another that shares "
            "its truncated prefix, so it must name nothing"
        )
        assert _tcid_identity_key("") == "", "an absent id names nothing"
        assert _tcid_identity_key(None) == "", "nor does a missing one"

    def test_the_key_matches_what_the_reader_computes(self):
        """Both ends must agree, or the gate reads unambiguous for a collapsed id."""
        from kiro_crew.dashboard.chat_runner import _tcid_identity_key
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        raw = f"eyJhbGciOiJIUzI1NiJ9.{'d' * 30}.{'s' * 20}"
        once = _redact_tool_field(raw)
        assert _tcid_identity_key(raw) == _tcid_identity_key(once), (
            "the preamble keys the raw-but-redacted value and the reader keys the "
            "value it re-redacts, so the helper must give both the same key"
        )

    @pytest.mark.asyncio
    async def test_an_over_long_id_flags_no_row(self, tmp_path, spool):
        from kiro_crew.dashboard.chat_runner import _MAX_TCID_LEN

        over_long = "z" * (_MAX_TCID_LEN + 1)

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-over-long-id")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        marker = f"[kirocrew-mcp-app:{sid}]"
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=over_long,
                title="create_view",
                tool_name="create_view",
                mcp_server_name="excalidraw",
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=over_long,
                tool_output=f"Done - two rectangles and an arrow. {marker}",
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        await _drive(state, slot, events)

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        flagged = [r for r in rows if META_KEY in r.get("meta", {})]
        assert not flagged, (
            "an id too long to track cannot be shown to name one call, so it must be "
            f"withheld rather than flagged; {len(flagged)} was"
        )


class TestTheTrackedSourcesAreBounded:
    """The turn tracks a bounded number of ids, and overflow withholds.

    The ids come from the LLM, so how many distinct ones a turn carries is not
    the runner's to trust. Past ``_MAX_TCID_SOURCES`` a further id is treated as
    collapsed, which withholds its notice -- the same direction every other
    ambiguity takes here, and the one a later render repairs.
    """

    def test_the_bound_is_finite(self):
        from kiro_crew.dashboard import chat_runner

        cap = chat_runner._MAX_TCID_SOURCES
        assert isinstance(cap, int) and cap > 0, "the bound must be a positive int"

    @pytest.mark.asyncio
    async def test_an_id_past_the_bound_flags_nothing(self, tmp_path, spool, monkeypatch):
        """With the bound lowered, the id that overflows it is withheld.

        Lowering the cap rather than driving hundreds of events keeps the test
        fast and pins the mechanism rather than the number.
        """
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        assert (
            _redact_tool_field(TOOL_CALL_ID) == TOOL_CALL_ID
        ), "this test needs ordinary ids redaction leaves alone"

        monkeypatch.setattr(chat_runner, "_MAX_TCID_SOURCES", 1)

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("app-past-bound")
        slot._titled = True

        sid = uuid.uuid4().hex
        _write_spool(spool, sid, effective_session_key(slot))

        # One unrelated call first, so it consumes the single tracked slot and the
        # app's own id is the one that overflows the bound.
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tool-call-unrelated-1",
                title="read_file",
                tool_name="read_file",
                mcp_server_name="filesystem",
            ),
            *_events(f"[kirocrew-mcp-app:{sid}]"),
        ]
        await _drive(state, slot, events)

        rows = [m for m in slot.messages if m.get("role") == "tool"]
        flagged = [r for r in rows if META_KEY in r.get("meta", {})]
        assert not flagged, (
            "an id past the tracking bound cannot be shown to name one call, so it "
            f"must be withheld rather than flagged; {len(flagged)} was"
        )
