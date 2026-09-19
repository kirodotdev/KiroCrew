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
from kiro_crew.dashboard.chat_utils import effective_session_key

TOOL_CALL_ID = "tc-app-1"

#: The meta key a replayed row reads. Spelled once here so a rename has to move
#: this constant, and the frontend's own reader is named beside it.
META_KEY = "mcp_app"


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
        assert all(p["meta"] == {META_KEY: True} for p in patches)
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
