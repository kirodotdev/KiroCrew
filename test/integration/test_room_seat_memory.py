"""A Bot's room seat across the real boot: launched as its Bot, learning in its own store.

The seat is bound to its Bot's store before its session starts, so the session
manager reads a member execution and launches the seat in member context, past
the warm pool. This drives that launch through the real gateway: a Bot created
the way the Crew page creates one, a room and a seat added over HTTP, the room
loop, the session manager, the provider and the spawned backend process, with
``kiro_crew.testing.fake_acp_backend`` standing in for the model. The memory call
is the one a managed MCP server makes for the seat, attested by a token published
the way ``session/new`` publishes it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.testing.wait import async_wait_until

_MARKER = "room seat integration lesson quince"
_OWNER_MARKER = "owner global lesson medlar"


async def _until_listening(gw, room_id: str, seat_id: str, *, secs: float = 60.0) -> None:
    """Wait for the seat's session to start: the room reports it ``listening``."""
    state = ""

    async def _listening() -> bool:
        nonlocal state
        room = await gw.get_json(f"/api/channels/{room_id}")
        state = str(room["members"].get(seat_id, {}).get("state", ""))
        return state == "listening"

    await async_wait_until(
        _listening, timeout=secs, describe=lambda: f"the seat never started: {state}"
    )


@pytest.mark.asyncio
async def test_a_bot_seat_launches_as_its_bot_and_learns_in_its_own_store(gateway_boot) -> None:
    from kiro_crew.execution_context import read_session_execution

    bot = "room-bot"
    async with gateway_boot() as gw:
        created = await gw.post_json("/api/agents", {"name": bot, "kiro_agent": "kirocrew"})
        store, member_id = created["memory_store"], created["member_id"]
        assert store and store != "default" and member_id, created

        room = (await gw.post_json("/api/channels", {"topic": "seat launch"}))["channel"]
        added = await gw.post_json(
            f"/api/channels/{room['id']}/agents", {"role": "Bot", "agent": bot}
        )
        seat = added["agent"]
        key = seat["session_key"]
        await _until_listening(gw, room["id"], seat["id"])

        provider = gw.state.sessions.get_provider(key)
        assert provider is not None
        assert provider._crew_agent == bot
        assert provider.member_context is True
        assert provider.is_process_alive()
        execution = await asyncio.to_thread(read_session_execution, key)
        assert execution is not None
        assert execution.member_id == member_id
        assert execution.store.store_id == store

        owner_added = await gw.post(
            "/api/lessons", {"rule": _OWNER_MARKER}, headers={"X-Session-Key": "dashboard:ui"}
        )
        assert owner_added.status == 200, await owner_added.text()
        headers = gw.mcp_headers(key)
        learned = await gw.post("/api/lessons", {"rule": _MARKER}, headers=headers, auth=False)
        assert learned.status == 200, await learned.text()

        seat_view = await gw.get_json("/api/lessons", headers=headers, auth=False)
        owner_view = await gw.get_json("/api/lessons")
        seat_rules = [row["rule"] for row in seat_view["lessons"]]
        owner_rules = [row["rule"] for row in owner_view["lessons"]]
        assert _MARKER in seat_rules and _OWNER_MARKER not in seat_rules, seat_rules
        assert _OWNER_MARKER in owner_rules and _MARKER not in owner_rules, owner_rules
