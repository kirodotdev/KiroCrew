"""The mediated-secret capability across the two ACP approval transports.

An approved ``call_api_with_secret`` mints a single-use capability for its call id and
exact arguments. These tests pin when it is minted (last, under the ACP write lock),
when it is revoked (an undelivered answer, a turn Stop, a final-floor refusal), and that
nothing minted after a cancel survives it. The transport doubles live in
``test_transport_permission_floor``, beside the floor tests that share them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from test_transport_permission_floor import _allowed, _client, _handle, _record

from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent


@pytest.mark.asyncio
async def test_handle_approval_mints_mediated_request_capability(monkeypatch):
    from kiro_crew import mediated_request_capability

    tool_args = {
        "secret_name": "WEATHER_API_KEY",
        "method": "GET",
        "url": "https://api.weather.example/x",
    }
    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="req-mediated",
        tool_call_id="tc-mediated",
        raw_tool_params=tool_args,
        raw_params_trusted=True,
        mcp_server_name="kirocrew-secrets",
        tool_name="call_api_with_secret",
        mcp_identity_trusted=True,
    )
    mint = MagicMock(return_value="capability-token")
    revoke = MagicMock()
    monkeypatch.setattr(mediated_request_capability, "mint_for_approved_call", mint)
    monkeypatch.setattr(mediated_request_capability, "revoke_minted", revoke)
    handle = _handle(session_key="dashboard:member")
    _record(handle, event)

    async def _write(_request_id, _result, *, before_write=None, withdrawal_check=None):
        # The runtime runs the caller's checks under its write lock before the frame.
        if withdrawal_check is not None:
            withdrawal_check()
        if before_write is not None:
            before_write()

    handle._runtime.send_response.side_effect = _write

    assert await handle.approve_tool("req-mediated") is True

    mint.assert_called_once_with(
        session_key="dashboard:member",
        tool_call_id="tc-mediated",
        mcp_server_name="kirocrew-secrets",
        tool_name="call_api_with_secret",
        tool_args=tool_args,
        identity_trusted=True,
        args_trusted=True,
    )
    assert len(_allowed(handle._runtime.send_response)) == 1
    revoke.assert_not_called()

    async def _write_then_fail(_request_id, _result, *, before_write=None, withdrawal_check=None):
        await _write(_request_id, _result, before_write=before_write)
        raise RuntimeError("write failed")

    handle._runtime.send_response.side_effect = _write_then_fail
    _record(handle, event)
    with pytest.raises(RuntimeError, match="write failed"):
        await handle.approve_tool("req-mediated")
    revoke.assert_called_once_with("dashboard:member", "tc-mediated", "capability-token")


def _mediated_event(request_id: str, tool_call_id: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=tool_call_id,
        raw_tool_params={
            "secret_name": "WEATHER_API_KEY",
            "method": "POST",
            "url": "https://api.weather.example/x",
        },
        raw_params_trusted=True,
        mcp_server_name="kirocrew-secrets",
        tool_name="call_api_with_secret",
        mcp_identity_trusted=True,
    )


@pytest.mark.asyncio
async def test_mediated_capability_is_not_minted_while_the_approval_waits_for_the_lock(
    monkeypatch,
):
    """A grant must not exist before the approval's final checks under the write lock:
    a caller could claim it during the lock wait, and a withdrawal read under the lock
    would then be too late. Withdrawn under the lock, nothing is ever minted."""
    from kiro_crew import mediated_request_capability, permission_floor

    mint = MagicMock(return_value="capability-token")
    monkeypatch.setattr(mediated_request_capability, "mint_for_approved_call", mint)
    handle = _handle(session_key="dashboard:member")
    _record(handle, _mediated_event("req-w", "tc-w"))
    withdrawn = {"now": False}
    seen_before_lock: list[int] = []

    async def _write(_request_id, _result, *, before_write=None, withdrawal_check=None):
        # Everything the approval did before taking the lock is visible here.
        seen_before_lock.append(mint.call_count)
        withdrawn["now"] = True  # the member is stopped while this writer waits
        if withdrawal_check is not None:
            withdrawal_check()
        if before_write is not None:
            before_write()

    handle._runtime.send_response.side_effect = _write

    with pytest.raises(permission_floor.ApprovalWithdrawn):
        await handle.approve_tool("req-w", withdraw_if=lambda: withdrawn["now"])
    assert seen_before_lock == [0]
    mint.assert_not_called()


@pytest.mark.asyncio
async def test_failed_approval_write_revokes_a_grant_already_claimed(monkeypatch):
    """A grant claimed while the approval frame drains is revoked when the write fails,
    so a claimed (active) token from an undelivered approval cannot be consumed."""
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    handle = _handle(session_key="dashboard:member")
    event = _mediated_event("req-c", "tc-c")
    _record(handle, event)
    claimed: list[str] = []

    async def _write(_request_id, _result, *, before_write=None, withdrawal_check=None):
        if before_write is not None:
            before_write()
        claimed.append(cap.claim("dashboard:member", "tc-c", event.raw_tool_params))
        raise RuntimeError("write failed")

    handle._runtime.send_response.side_effect = _write

    with pytest.raises(RuntimeError, match="write failed"):
        await handle.approve_tool("req-c")
    assert claimed and claimed[0]
    assert cap.consume("dashboard:member", claimed[0], event.raw_tool_params) is False
    assert cap._pending == {} and cap._active == {}


@pytest.mark.asyncio
async def test_client_transport_mints_under_the_lock_and_revokes_a_claimed_grant(
    monkeypatch, tmp_path
):
    """The single-client transport has the same ordering and cleanup."""
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    client = _client(tmp_path)
    client._session_key = "dashboard:member"
    event = _mediated_event("req-k", "tc-k")
    _record(client, event)
    order: list[str] = []
    claimed: list[str] = []

    async def _send(_request_id, _result, *, before_write=None):
        order.append("lock")
        if before_write is not None:
            before_write()
        order.append("minted" if cap._pending else "none")
        claimed.append(cap.claim("dashboard:member", "tc-k", event.raw_tool_params))
        raise RuntimeError("write failed")

    client._send_response = _send

    with pytest.raises(RuntimeError, match="write failed"):
        await client.approve_tool("req-k")
    assert order == ["lock", "minted"]
    assert cap.consume("dashboard:member", claimed[0], event.raw_tool_params) is False
    assert cap._active == {}


def _seed_grants(cap, monkeypatch):
    """One pending and one claimed grant for the member, one pending for another session."""
    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    args = _mediated_event("r", "tc").raw_tool_params
    for session, call in (
        ("dashboard:member", "tc-1"),
        ("dashboard:member", "tc-2"),
        ("other", "tc-3"),
    ):
        assert cap.mint_for_gate_event(session, _mediated_event("r-" + call, call))
    claimed = cap.claim("dashboard:member", "tc-2", args)
    assert claimed
    return args, claimed


@pytest.mark.asyncio
async def test_cancelling_a_handle_turn_revokes_its_mediated_grants_only(monkeypatch):
    """A soft Stop must not leave an approved, delivered call able to run later."""
    from kiro_crew import mediated_request_capability as cap

    args, claimed = _seed_grants(cap, monkeypatch)
    handle = _handle(session_key="dashboard:member")
    handle._runtime.send_notification = AsyncMock()

    await handle.cancel()

    assert cap.claim("dashboard:member", "tc-1", args) == ""
    assert cap.consume("dashboard:member", claimed, args) is False
    # Another session's grant survives the cancel.
    assert cap.claim("other", "tc-3", args)
    handle._runtime.send_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelling_a_client_turn_revokes_its_mediated_grants_only(monkeypatch, tmp_path):
    from kiro_crew import mediated_request_capability as cap

    args, claimed = _seed_grants(cap, monkeypatch)
    client = _client(tmp_path)
    client._session_key = "dashboard:member"
    client._session_id = None  # no live process: the revoke must not depend on it

    await client.cancel_session()

    assert cap.claim("dashboard:member", "tc-1", args) == ""
    assert cap.consume("dashboard:member", claimed, args) is False
    assert cap.claim("other", "tc-3", args)


@pytest.mark.asyncio
async def test_a_stop_while_the_handle_approval_waits_for_the_lock_leaves_no_grant(monkeypatch):
    """An approval with no ``withdraw_if`` (a crew-bound chat click) that waits for
    the write lock while the turn is cancelled must not mint afterwards: the cancel
    already swept the session's grants, so a later mint would outlive the Stop."""
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    handle = _handle(session_key="dashboard:member")
    event = _mediated_event("req-s", "tc-s")
    _record(handle, event)

    async def _write(_request_id, _result, *, before_write=None, withdrawal_check=None):
        # The Stop lands while this approval waits for the lock.
        cap.invalidate_session("dashboard:member")
        if before_write is not None:
            before_write()

    handle._runtime.send_response.side_effect = _write

    assert await handle.approve_tool("req-s") is True
    assert cap._pending == {} and cap._active == {}
    assert cap.claim("dashboard:member", "tc-s", event.raw_tool_params) == ""


@pytest.mark.asyncio
async def test_a_stop_while_the_client_approval_waits_for_the_lock_leaves_no_grant(
    monkeypatch, tmp_path
):
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    client = _client(tmp_path)
    client._session_key = "dashboard:member"
    event = _mediated_event("req-t", "tc-t")
    _record(client, event)

    async def _send(_request_id, _result, *, before_write=None):
        cap.invalidate_session("dashboard:member")
        if before_write is not None:
            before_write()

    client._send_response = _send

    assert await client.approve_tool("req-t") is True
    assert cap._pending == {} and cap._active == {}


def test_a_cancel_between_the_check_and_the_insert_still_leaves_no_grant(monkeypatch):
    """The generation is read again after the insert, so a cancel that slips in
    between the first check and the insert cannot leave its grant behind."""
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    event = _mediated_event("req-u", "tc-u")
    mint = cap.ApprovalMint("dashboard:member", event)
    real = cap.mint_for_gate_event

    def _cancel_bumps_but_misses_the_insert(session_key, gate_event):
        # The cancel bumps the generation but its sweep ran just before the
        # insert, so the grant it could not see is left in place.
        cap.invalidate_session("dashboard:member")
        return real(session_key, gate_event)

    monkeypatch.setattr(cap, "mint_for_gate_event", _cancel_bumps_but_misses_the_insert)
    mint.mint()
    assert cap._pending == {} and cap._active == {}
    mint.settle(sent=True)
    assert cap._pending == {}


def test_cancel_generations_stay_bounded_and_eviction_only_refuses(monkeypatch):
    from kiro_crew import mediated_request_capability as cap

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    monkeypatch.setattr(cap, "_cancel_generation", {})
    monkeypatch.setattr(cap, "_MAX_TRACKED_SESSIONS", 3)
    cap.invalidate_session("dashboard:member")
    mint = cap.ApprovalMint("dashboard:member", _mediated_event("req-v", "tc-v"))
    for other in ("a", "b", "c"):
        cap.invalidate_session(other)
    assert len(cap._cancel_generation) == 3
    # The member's entry was evicted: its captured generation is a mismatch.
    mint.mint()
    assert cap._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["client", "handle"])
async def test_a_floor_refused_reprompt_drops_the_earlier_grant_for_its_call_id(
    monkeypatch, tmp_path, kind
):
    """A harness that re-prompts an approved, unconsumed call id is refused by the
    final floor (say a newly added ``auto_deny_tools`` rule). That refusal must drop
    the grant the first approval minted: ``approve_tool`` has already popped the
    event, so the ``reject_tool`` it delegates to could not invalidate it.
    Mutation guard: without the invalidation in ``approve_tool`` the grant survives."""
    from kiro_crew import mediated_request_capability as cap
    from kiro_crew import permission_floor

    monkeypatch.setattr(cap, "_pending", {})
    monkeypatch.setattr(cap, "_active", {})
    first = _mediated_event("req-1", "tc-r")
    assert cap.mint_for_gate_event("dashboard:member", first)
    if kind == "client":
        transport = _client(tmp_path)
        transport._session_key = "dashboard:member"
    else:
        transport = _handle(session_key="dashboard:member")
    _record(transport, _mediated_event("req-2", "tc-r"))
    monkeypatch.setattr(
        permission_floor, "refusal_for", lambda _event: permission_floor.REASON_NO_EVENT
    )
    monkeypatch.setattr(permission_floor, "audit_refusal", lambda *a, **k: None)

    assert await transport.approve_tool("req-2") is False
    assert cap.claim("dashboard:member", "tc-r", first.raw_tool_params) == ""
    assert cap._pending == {} and cap._active == {}
