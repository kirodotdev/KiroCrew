"""A client-stamped row-meta key rides the send onto the user row and back out.

The dashboard's feature-request fallback (``website/src/prompts/featureRequest.ts``,
``pages/chat/transcriptRenderers.isFeatureRequestRefusal``) stamps
``meta.featureRequest: true`` on the send beside ``sendId`` and reads it back
from the user row: on the optimistic bubble, on the echoed row, on a reloaded
transcript and in a second tab. Every one of those reads rests on what this
module pins and nothing else pinned: the gateway persists a send's ``meta``
VERBATIM on the user row -- ``RESERVED_ROW_META_KEYS`` is the only ingress gate
and ``_redact_meta`` is a string redactor, not an allowlist -- and hands it back
unchanged on both the WS echo and ``GET /api/chat/slots/{slot}``.
``test_chat_send_echo_scope`` pins ``sendId`` alone, so an allowlist that later
admitted ``sendId`` and dropped the rest would pass it and silently retire the
fallback. Asserted by the key the frontend actually stamps, so the failure reads
as what broke.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import SlotOrigin

_SLOT = "meta-survival-slot"
_FENCE = "meta-survival-test-finished"
_PROMPT = "I\u2019d like to request a feature!"
#: The key the pill's flow stamps (``FEATURE_REQUEST_ROW_META_KEY`` in
#: ``website/src/prompts/featureRequest.ts``). Spelled out here rather than
#: read from the frontend so the backend contract is pinned by name.
_STAMP = "featureRequest"
#: A key the ingress gate DOES strip, sent alongside: proves the gate is the
#: one filter on client meta and that the stamp is not inside it. Read from the
#: gate so the control cannot drift from the set it exercises.
_RESERVED = next(iter(sorted(RESERVED_ROW_META_KEYS)))


@pytest.fixture
def survival_state(tmp_path, monkeypatch):
    # Real authentication with isolated signing/nonce state: the WS echo is
    # gated on the auth middleware's positive dashboard-user claim.
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"meta-survival-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    # Periodic status is unrelated to the row this module follows.
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})

    async def reply(st, slot, message, *, _directive_user_origin):
        slot.append("assistant", "Filed as #13342.")
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", reply)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
    yield state
    stopped.set()


async def _ws_until_fence(socket):
    frames = []
    while True:
        frame = await socket.receive_json()
        if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
            return frames
        frames.append(frame)


def _rows_on_disk(log, key: str) -> list[dict]:
    """The raw JSONL rows for *key*: what is DURABLE, read off the file itself."""
    rows: list[dict] = []
    for line in log._path(key).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("role"):
                rows.append(row)
    return rows


def _user_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("role") == "user"]


@pytest.mark.asyncio
async def test_feature_request_stamp_survives_ingress_persistence_echo_and_fetch(
    survival_state,
):
    from kiro_crew.dashboard.ws import api_ws

    app = _make_app(survival_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)

    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        owner = await client.ws_connect("/api/ws", params={"token": token})
        # The initial slots frame proves the socket is registered for echoes
        # before the send goes out.
        initial = await asyncio.wait_for(owner.receive_json(), timeout=5)
        assert initial["type"] == "slots"

        # INGRESS: the flow's send, byte for byte the shape App.tsx posts --
        # the correlation id, the stamp, and (the control) a gateway-minted key
        # a caller may never supply.
        response = await client.post(
            "/api/chat?ws=1",
            params={"token": token},
            json={
                "slot": _SLOT,
                "message": _PROMPT,
                "meta": {"sendId": "s-fr-seed", _STAMP: True, _RESERVED: {"forged": True}},
            },
        )
        assert response.status == 200
        receipt = await response.json()
        slot = survival_state.get_slot(_SLOT)
        await slot.task

        # ECHO: the correlated user row reaches the owner socket with the stamp
        # intact -- this is the row the optimistic bubble reconciles against.
        survival_state._broadcast({"_type": "refresh", "kinds": _FENCE})
        frames = await asyncio.wait_for(_ws_until_fence(owner), timeout=5)
        echoed = _user_rows(
            [frame["data"] for frame in frames if frame.get("type") == "chat_message"]
        )
        assert len(echoed) == 1
        assert echoed[0]["meta"]["sendId"] == "s-fr-seed"
        assert echoed[0]["meta"]["mid"] == receipt["mid"]
        assert echoed[0]["meta"][_STAMP] is True
        assert _RESERVED not in echoed[0]["meta"]

        # PERSISTED ROW: the live window, then the durable JSONL line the flush
        # writes -- the row a reload after a gateway restart is rebuilt from.
        live = _user_rows(list(slot.messages))
        assert len(live) == 1
        assert live[0]["meta"][_STAMP] is True
        assert _RESERVED not in live[0]["meta"]
        assert _save_slot_to_history(survival_state, slot, force=True)
        key = slot_history_key(slot)
        durable = _user_rows(_rows_on_disk(survival_state.conversation_log, key))
        assert len(durable) == 1
        assert durable[0]["content"] == _PROMPT
        assert durable[0]["meta"]["sendId"] == "s-fr-seed"
        assert durable[0]["meta"][_STAMP] is True
        assert _RESERVED not in durable[0]["meta"]
        # ...and through the reader the slot-detail handler uses for a restored
        # session, not only the raw file.
        chained = _user_rows(survival_state.conversation_log.read_messages_chained(key))
        assert [row["meta"][_STAMP] for row in chained] == [True]

        # TRANSCRIPT FETCH: the reload / second-tab read, with the query the
        # frontend's `chatSlotDetail(slot)` issues (no limit, no cursor).
        detail = await client.get(f"/api/chat/slots/{_SLOT}", params={"token": token})
        assert detail.status == 200
        fetched = _user_rows((await detail.json())["messages"])
        assert len(fetched) == 1
        assert fetched[0]["content"] == _PROMPT
        assert fetched[0]["meta"]["sendId"] == "s-fr-seed"
        # The renderer accepts the literal `true` only, so the TYPE has to
        # survive the round trip, not just the key.
        assert fetched[0]["meta"][_STAMP] is True
        assert _RESERVED not in fetched[0]["meta"]
        await owner.close()
