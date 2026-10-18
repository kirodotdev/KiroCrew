"""gatewayd's claim-push keyed by (pid, token) and the binding it creates.

The three token topologies, co-tenancy isolation, clearing and re-attribution of an
identity, the register path preferring the binding over both process-tree sources, a
``spawn_run``-shaped subagent surviving its parent's rekey, and a claim that binds while
the register runs.

Minting and delivery of the token are pinned in
``test_mcp_gateway_stub_session_token_minting.py``; where the token may and may not go
(logs, journals, the fallback, pooled control planes) in
``test_mcp_gateway_stub_session_token.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import mcp_gateway_stub_session_token_helpers
import pytest
from mcp_gateway_stub_session_token_helpers import (
    PARENT_KEY,
    SUB_KEY,
    TOKEN_A,
    TOKEN_B,
    _attest,
    _claim_with_token,
    _close,
    _live_conn,
    _next_caller,
    _register_with_token,
)
from test_identity_topology import GATEWAY, KIRO_CLI, MCP_SERVER, SESSION_HOST, ProcessTopology
from test_mcp_gateway_claim import (
    _ANCESTORS,
    _CALL,
    _PID,
    _WRAPPER_PID,
    _claim,
    _FakePool,
    _handle,
    _patch_env,
    _QueueReader,
    _RecordingWriter,
    _register,
)

from kiro_crew import mcp_caller, mcp_core
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.testing.wait import LoopHop, await_until

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_clean_gateway_state = mcp_gateway_stub_session_token_helpers._clean_gateway_state

pytestmark = pytest.mark.xdist_group("mcp_gateway")


# ---------------------------------------------------------------------------
# Claim-push keyed by (pid, token) — the three topologies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("topology", ["no-token", "all-tokens", "mixed"])
async def test_claim_across_token_topologies(
    monkeypatch: pytest.MonkeyPatch, topology: str
) -> None:
    """One runtime, two connections, three deployment shapes.

    ``no-token``: neither connection carries one (a hand-written config, an
    overlay predating the token) — a tokenless claim re-targets both, exactly as
    it always did. ``all-tokens``: both name a session, and a claim for one
    reaches only that one. ``mixed``: the tokened connection is selected by the
    claim and the tokenless one rides along, because it has no finer identity
    than the process tree the claim named.
    """
    token_first = "" if topology == "no-token" else TOKEN_A
    token_second = TOKEN_B if topology == "all-tokens" else ""

    first_backend, first_reader, first_task = await _live_conn(
        monkeypatch, "", token_first, "stub-first"
    )
    second_backend, second_reader, second_task = await _live_conn(
        monkeypatch, "", token_second, "stub-second"
    )

    claim_token = "" if topology == "no-token" else TOKEN_A
    ack = await gw._apply_claim(_claim_with_token(_WRAPPER_PID, PARENT_KEY, claim_token))
    assert ack["type"] == "claimed"
    assert ack["connections"] == 2
    expected_updates = {"no-token": 2, "all-tokens": 1, "mixed": 2}[topology]
    assert ack["updated"] == expected_updates

    first_caller = await _next_caller(first_backend, first_reader)
    second_caller = await _next_caller(second_backend, second_reader)
    assert first_caller is not None and first_caller.session_key == PARENT_KEY
    if topology == "all-tokens":
        # The other session on the same runtime keeps its own identity — here,
        # none yet. This is the property the item exists for.
        assert second_caller is None
    else:
        assert second_caller is not None and second_caller.session_key == PARENT_KEY

    await _close(first_reader, first_task)
    await _close(second_reader, second_task)


@pytest.mark.asyncio
async def test_a_claim_for_one_session_leaves_its_sibling_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE isolation property. Two connections under ONE runtime PID with
    different tokens: a claim for A re-keys only A, B is untouched, and a claim
    carrying no token still re-targets both (backward compatibility)."""
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")

    assert (await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A)))["updated"] == 1
    assert (await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B)))["updated"] == 1
    assert (await _next_caller(a_backend, a_reader)).session_key == PARENT_KEY
    assert (await _next_caller(b_backend, b_reader)).session_key == SUB_KEY

    # A re-claim of the parent slot (warm-pool rekey) must not move the subagent.
    assert (await gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-2", TOKEN_A)))[
        "updated"
    ] == 1
    assert (await _next_caller(a_backend, a_reader)).session_key == "dashboard:chat-2"
    assert (await _next_caller(b_backend, b_reader)).session_key == SUB_KEY

    # A tokenless claim keeps the PID-wide reach it has always had.
    ack = await gw._apply_claim(_claim(_PID, "dashboard:chat-3"))
    assert ack["updated"] == 2
    assert (await _next_caller(a_backend, a_reader)).session_key == "dashboard:chat-3"
    assert (await _next_caller(b_backend, b_reader)).session_key == "dashboard:chat-3"

    await _close(a_reader, a_task)
    await _close(b_reader, b_task)


@pytest.mark.asyncio
async def test_a_tokenless_stub_is_not_handed_to_a_co_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tokenless stub on a runtime hosting SEVERAL sessions stays where it is.

    One runtime, three connections: two that name their session with a token, and
    one launched outside the token path that names none. While only one session has
    claimed, the tokenless stub rides along on the process tree exactly as it always
    did — the runtime still has one plausible owner. Once a SECOND session claims the
    same runtime, the tree stops naming a session, and the next claim must not take
    the tokenless stub with it: that stub's ``work_brief`` and ``work_report`` would
    then read and write the claiming session's ledger instead of its own.
    """
    token_c = "c" * 64
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    # One session so far: the runtime has a single plausible owner, so the
    # tokenless stub is still re-targeted on the tree. Unchanged behavior.
    first = await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert first["updated"] == 2 and first["skipped"] == 0
    assert (await _next_caller(a_backend, a_reader)).session_key == PARENT_KEY
    assert (await _next_caller(plain_backend, plain_reader)).session_key == PARENT_KEY

    # A second session claims the same runtime. Now the PID names two sessions,
    # so it names neither: the tokenless stub is not reassigned.
    second = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert second["updated"] == 1, "only the stub whose token the claim names"
    assert second["skipped"] == 1, "the tokenless stub is not retargeted"
    assert (await _next_caller(b_backend, b_reader)).session_key == SUB_KEY
    # THE property, and it is CLEARED rather than frozen: leaving the earlier
    # PARENT_KEY in place would let a stub that belongs to the second tenant go
    # on answering as the first, which stops the misattribution moving without
    # stopping it. No caller means strict tools refuse with a diagnosis.
    assert await _next_caller(plain_backend, plain_reader) is None

    # A claim naming the tokenless stub's runtime for a THIRD session cannot
    # attribute it either, and does not re-grant the identity just cleared.
    third = await gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-9", token_c))
    assert third["skipped"] == 1
    assert await _next_caller(plain_backend, plain_reader) is None

    await _close(a_reader, a_task)
    await _close(b_reader, b_task)
    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_a_cleared_stub_regains_identity_after_co_tenant_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleared tokenless stub is re-attributed once the co-tenant leaves.

    Two recovery paths exist for a cleared stub. This test pins the second one:
    once the co-tenant's token-carrying connections have disconnected,
    ``_pid_live_tenants`` drops below two, ``shared_pid`` becomes False, and a
    tokened claim from the remaining session can retarget the tokenless stub.
    The first path (a tokenless claim) is already covered by
    ``test_claim_across_token_topologies``; this test covers the second so the
    comment's two named routes are each backed by a test.

    The test also pins item 2: ``identity_refused`` is reset when the connection
    is retargeted, so the recaller is not permanently blocked after recovery.
    """
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    # One session so far — tokenless stub rides along as usual.
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert (await _next_caller(plain_backend, plain_reader)).session_key == PARENT_KEY

    # Co-tenant joins: tokenless stub is cleared.
    second = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert second["skipped"] == 1
    assert await _next_caller(plain_backend, plain_reader) is None

    # Confirm identity_refused was set so we can prove it is cleared after recovery.
    plain_conn = next(c for c in gw._CONN_INDEX[_PID] if c.stub_uuid == "stub-plain")
    assert plain_conn.identity_refused, "should be refused while co-tenant is live"

    # Co-tenant disconnects: _pid_live_tenants now sees only stub-a's session.
    await _close(b_reader, b_task)

    # First session re-claims: shared_pid is now False, tokenless stub is retargeted.
    third = await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert third["updated"] >= 1, "stub-plain should be retargeted"
    assert (await _next_caller(plain_backend, plain_reader)).session_key == PARENT_KEY

    # identity_refused is cleared by the retarget (item 2).
    assert not plain_conn.identity_refused, "retarget should clear the refused flag"

    await _close(a_reader, a_task)
    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_replacing_a_live_identity_on_a_shared_runtime_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Overwriting a DIFFERENT live identity on a shared runtime is a warning.

    A tokenless claim keeps its PID-wide reach, so on a runtime hosting several
    sessions it still replaces each one's live identity. That is the case an
    operator has to be able to find in a log: on a single-session runtime the same
    replacement is just the warm-pool rekey this path exists for, and stays at INFO.
    """
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))

    # One claimed session: a re-claim of that same slot is the routine rekey.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-2", TOKEN_A))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    # Two claimed sessions, then a tokenless claim that reaches both: each stub
    # loses a live identity to a session that is not its own.
    await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        ack = await gw._apply_claim(_claim(_PID, "dashboard:chat-3"))
    assert ack["updated"] == 2, "a tokenless claim keeps the PID-wide reach it had"
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "replaced a live identity" in r.getMessage()
    ]
    assert len(warnings) == 2, warnings
    assert any("stub-a" in m for m in warnings) and any("stub-b" in m for m in warnings)
    # The token itself must never reach a log record.
    assert not any(TOKEN_A in m or TOKEN_B in m for m in warnings)

    await _close(a_reader, a_task)
    await _close(b_reader, b_task)


@pytest.mark.asyncio
async def test_a_refused_stub_cannot_name_itself_with_a_recaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing an identity must not hand the stub a way to choose a new one.

    The recaller is deny-by-default and may fill exactly one state: a key-less,
    tokenless connection that nothing has named yet. A co-tenancy refusal
    produces a connection that LOOKS like that, so without a verdict flag the
    refused stub could name any session it liked -- and the key it would send
    comes from its own process-tree walk, which on a shared runtime is the very
    per-runtime answer the refusal rejected.
    """
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    refusing = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert refusing["skipped"] == 1
    assert await _next_caller(plain_backend, plain_reader) is None

    # The refused stub now asks for an identity of its own choosing.
    plain_reader.feed(_register(PARENT_KEY))
    plain_reader.feed({"type": "recaller", "session_key": PARENT_KEY})
    assert (
        await _next_caller(plain_backend, plain_reader) is None
    ), "a refusal is a verdict; only claim-push may name this connection"

    await _close(a_reader, a_task)
    await _close(b_reader, b_task)
    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_clearing_an_identity_evicts_its_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription granted under an identity must not outlive it.

    The cleared connection is not retargeted, so it never reaches the retarget
    list that pass 2 evicts from. Skipping it would leave the OLD session's
    resource-update URIs -- which can carry tokens or presigned params -- still
    being delivered to a connection that answers as nobody.
    """
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    b_backend, b_reader, b_task = await _live_conn(monkeypatch, "", TOKEN_B, "stub-b")
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    # Give the tokenless stub an identity, so the clearing has something to take.
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert (await _next_caller(plain_backend, plain_reader)).session_key == PARENT_KEY

    evicted: list[str] = []

    class _EvictingPool(_FakePool):
        def backends_hosting_stub(self, stub_uuid: str) -> Any:
            return [self]

        async def evict_stub_subscriptions(self, stub_uuid: str) -> None:
            evicted.append(stub_uuid)

    ack = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B), _EvictingPool())
    assert ack["skipped"] == 1
    assert "stub-plain" in evicted, "the cleared connection owes the same eviction"

    await _close(a_reader, a_task)
    await _close(b_reader, b_task)
    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_sequential_claims_on_one_runtime_are_not_sharing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime claimed by one session and later by another is NOT shared.

    "How many sessions has this pid ever been claimed for" and "how many does it
    serve now" are different questions, and only the second one makes a PID-keyed
    answer a guess. A warm-pool runtime hands over: session A finishes, session B
    takes the process. Reading that as two tenants would refuse to retarget the
    runtime's tokenless stubs, so they would keep A's identity while B is the
    only session there -- which is the very misattribution this guard exists to
    prevent, re-created on a single-session runtime.
    """
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    first = await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert first["updated"] == 1 and first["skipped"] == 0
    assert (await _next_caller(plain_backend, plain_reader)).session_key == PARENT_KEY

    # The handover. Two tokens have now named this pid, but only one session is
    # on it, so the tokenless stub must follow.
    second = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert second["skipped"] == 0, "a handover is not a co-tenant"
    assert second["updated"] == 1
    assert (await _next_caller(plain_backend, plain_reader)).session_key == SUB_KEY

    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_a_rekey_of_one_slot_is_not_sharing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-claiming the SAME token slot is a rekey, not a second tenant.

    The claim's own stubs still carry the previous session's caller at the moment
    it is applied, so counting them would make every warm-pool rekey of a
    single-session runtime read as shared.
    """
    a_backend, a_reader, a_task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-a")
    plain_backend, plain_reader, plain_task = await _live_conn(monkeypatch, "", "", "stub-plain")

    assert (await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A)))["skipped"] == 0
    rekey = await gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-2", TOKEN_A))
    assert rekey["skipped"] == 0, "the slot's own stubs are not co-tenants of it"
    assert (await _next_caller(a_backend, a_reader)).session_key == "dashboard:chat-2"
    assert (await _next_caller(plain_backend, plain_reader)).session_key == "dashboard:chat-2"

    await _close(a_reader, a_task)
    await _close(plain_reader, plain_task)


@pytest.mark.asyncio
async def test_a_claim_binds_its_token_even_when_it_matches_nothing() -> None:
    """A session's claim is pushed BEFORE its stubs launch, so "matched zero" is
    the normal ordering — the binding is what the register then reads."""
    ack = await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert ack["type"] == "claim-noop" and ack["updated"] == 0
    bound = gw._token_caller(TOKEN_B, [_PID])
    assert bound is not None and bound.session_key == SUB_KEY
    # …and the binding is scoped to the runtime the claim named: a connection
    # elsewhere on the host holding the same token gets nothing from it.
    assert gw._token_caller(TOKEN_B, [999999]) is None
    assert gw._token_caller(TOKEN_B, []) is None


def test_the_binding_table_is_bounded() -> None:
    """A long-running daemon must not accumulate bindings without bound.
    Dropping the oldest costs a re-claim; it never invents an identity."""
    for index in range(gw._MAX_TOKEN_BINDINGS + 10):
        gw._bind_token(f"tok-{index}", CallerContext(session_key=f"s{index}"), _PID)
    assert len(gw._TOKEN_BINDINGS) == gw._MAX_TOKEN_BINDINGS
    assert gw._token_caller("tok-0", [_PID]) is None
    assert gw._token_caller(f"tok-{gw._MAX_TOKEN_BINDINGS + 9}", [_PID]) is not None


# ---------------------------------------------------------------------------
# Register-time identity: the binding outranks both process-tree sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_prefers_the_binding_over_the_stubs_self_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent's stub inherits the runtime's env/pid-file, so it self-reports
    the PARENT's key. The token names the session it actually serves."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    backend, reader, task = await _live_conn(monkeypatch, PARENT_KEY, TOKEN_B, "stub-sub")
    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    await _close(reader, task)


@pytest.mark.asyncio
async def test_register_prefers_the_binding_over_the_proc_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server-side SO_PEERCRED walk reaches the same runtime tree, so it
    answers with the parent too. The binding wins and the walk's session key is
    never adopted."""
    backend, sel = _patch_env(monkeypatch)
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: 9100)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: (PARENT_KEY, [9100, 9020]))
    # The claim names the runtime as the KERNEL sees it, which is the chain the
    # token is authenticated against.
    await gw._apply_claim(_claim_with_token(9020, SUB_KEY, TOKEN_B))

    reader = _QueueReader()
    reader.feed(_register_with_token("", TOKEN_B, stub_uuid="stub-sub"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)

    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    # The host chain is still indexed, so this session's own later claims land.
    assert 9020 in gw._CONN_INDEX
    await _close(reader, task)


@pytest.mark.asyncio
async def test_an_unclaimed_token_defers_identity_and_its_claim_repairs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed. A token says "a claim names my session", so until that claim
    arrives there is nothing to grant: every remaining source answers per
    RUNTIME, and this stub self-reports the parent's key precisely because it
    shares the parent's process."""
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn(monkeypatch, PARENT_KEY, TOKEN_B, "stub-sub")
    assert backend.callers[0] is None

    # …and its own claim repairs it, without touching the parent's connection.
    assert (await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B)))["updated"] == 1
    assert (await _next_caller(backend, reader)).session_key == SUB_KEY
    await _close(reader, task)


@pytest.mark.asyncio
async def test_an_unclaimed_token_is_refused_even_with_nothing_else_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EMPTY binding table is not evidence that the tree answer is right — it
    is the state a fresh daemon, a respawn and an evicted binding all share, and
    in each of those a subagent's stub would otherwise be handed its parent's
    session. So the refusal cannot be conditional on some sibling happening to
    be named."""
    backend, reader, task = await _live_conn(monkeypatch, PARENT_KEY, TOKEN_A, "stub-parent")
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_self_reported_ancestry_cannot_authenticate_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The register frame is peer-supplied IN FULL, ``ancestor_pids`` included.

    So the actor who read another session's token out of ``/proc`` can also name
    that session's runtime in its own chain, and a token checked against the
    self-reported pids would let one actor satisfy both halves — the second
    factor would authenticate nothing. Only the chain gatewayd walks from the
    kernel's peer pid counts: here the stub claims the victim's runtime pids and
    the kernel places it somewhere else entirely.
    """
    _attest(monkeypatch, [777001, 777002])
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_on(
        monkeypatch, TOKEN_A, "stub-thief", list(_ANCESTORS)
    )
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_stolen_token_buys_nothing_outside_its_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token rides an ``env`` pair, and ``/proc/<pid>/environ`` is readable
    at the operator's own uid, so a process on the host can learn another
    session's token. A claim binds the token TOGETHER WITH the runtime PID it
    named, and both are required: presenting the token from a connection the
    kernel does not place under that runtime resolves to nothing."""
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    # A stub elsewhere on the host: its own ancestry shares no pid with the
    # claimed runtime, and it presents the stolen token plus a self-report.
    backend, reader, task = await _live_conn_on(
        monkeypatch, TOKEN_A, "stub-thief", [777001, 777002]
    )
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_daemon_respawn_leaves_no_session_wearing_anothers_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gatewayd holds the bindings in memory only, so a respawn empties them
    while both sessions are still live. Their stubs reconnect and re-register
    with the SAME tokens; neither may resolve from the shared tree, and the
    per-turn re-claim (``publish_turn_identity`` -> ``reclaim``) is what restores
    each one to its own session."""
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    # The respawn: a new daemon process starts with an empty table.
    gw._TOKEN_BINDINGS.clear()

    parent_backend, parent_reader, parent_task = await _live_conn(
        monkeypatch, PARENT_KEY, TOKEN_A, "stub-parent"
    )
    sub_backend, sub_reader, sub_task = await _live_conn(
        monkeypatch, PARENT_KEY, TOKEN_B, "stub-sub"
    )
    assert parent_backend.callers[0] is None
    assert sub_backend.callers[0] is None

    # Each session's next turn re-pushes its own claim.
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    await gw._apply_claim(_claim_with_token(_PID, SUB_KEY, TOKEN_B))
    assert (await _next_caller(parent_backend, parent_reader)).session_key == PARENT_KEY
    assert (await _next_caller(sub_backend, sub_reader)).session_key == SUB_KEY

    await _close(parent_reader, parent_task)
    await _close(sub_reader, sub_task)


@pytest.mark.asyncio
async def test_recaller_cannot_name_a_token_carrying_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stub's recaller poll resolves the same pid file, so it re-opens the
    hole the register path just closed. Same rule, same place in the pipeline:
    only claim-push may name a token."""
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register_with_token("", TOKEN_B, stub_uuid="stub-sub"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    assert backend.callers[0] is None

    reader.feed({"type": "recaller", "session_key": PARENT_KEY, "session_type": "dashboard"})
    assert (await _next_caller(backend, reader)) is None
    denied = [
        event
        for event in sel
        if event.get("operation") == "mcp-gateway.caller-rekey" and event.get("outcome") == "denied"
    ]
    assert denied and "token-carrying connection" in denied[-1]["error"]
    await _close(reader, task)


# ---------------------------------------------------------------------------
# End to end: a spawn_run-shaped subagent survives the parent's rekey
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_subagent_session_survives_its_parents_rekey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole item, end to end, on the real topology.

    ``ProcessTopology`` models the tree a ``spawn_run`` subagent actually runs
    in: ONE kiro-cli under the session host, with the gateway's
    ``session_pid_<host_pid>.txt`` naming the PARENT slot — so both the stub's
    own walk and gatewayd's peer walk resolve the parent for BOTH sessions'
    stubs. Two connections are opened under that one runtime, each carrying its
    session's token; the parent then rekeys (warm-pool re-claim) and the
    subagent's stub must still forward as the SUBAGENT.

    Then the consequence that matters: with the identity gatewayd stamps on that
    connection, ``require_strict_session_key`` — the one gate ``monitor_start``
    and every other reflexive tool routes through — answers with the subagent's
    key. A subagent cannot arm a loop on, or report into, its parent's slot.
    """
    topo = ProcessTopology(tmp_path)
    topo.add(GATEWAY, 1)
    topo.add(SESSION_HOST, GATEWAY)
    topo.add(KIRO_CLI, SESSION_HOST)
    topo.add(MCP_SERVER, KIRO_CLI)
    topo.write_session_pid(SESSION_HOST, PARENT_KEY)
    monkeypatch.setattr(gw, "_config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr(gw, "_ppid_fn", topo.parent_lookup("host"))

    runtime_pids = [KIRO_CLI, SESSION_HOST]
    parent_backend, parent_reader, parent_task = await _live_conn_on(
        monkeypatch, TOKEN_A, "stub-parent", runtime_pids
    )
    sub_backend, sub_reader, sub_task = await _live_conn_on(
        monkeypatch, TOKEN_B, "stub-sub", runtime_pids
    )

    # Both sessions claim their own token (the parent at slot claim, the
    # subagent at its create_session).
    await gw._apply_claim(_claim_with_token(SESSION_HOST, PARENT_KEY, TOKEN_A))
    await gw._apply_claim(_claim_with_token(SESSION_HOST, SUB_KEY, TOKEN_B))
    # The parent slot re-keys onto this warm runtime: a PID-wide claim, which
    # reaches every stub identity under that PID.
    await gw._apply_claim(_claim_with_token(SESSION_HOST, "dashboard:chat-9", TOKEN_A))

    parent_caller = await _next_caller(parent_backend, parent_reader)
    sub_caller = await _next_caller(sub_backend, sub_reader)
    assert parent_caller.session_key == "dashboard:chat-9"
    assert sub_caller.session_key == SUB_KEY

    # What a reflexive tool in the subagent's backend process then resolves.
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    # Value-based restore, not token-based: this test crosses asyncio task
    # boundaries and pytest-xdist shares the worker's Context across tests.
    previous = mcp_caller._CURRENT_CALLER.get()
    mcp_caller._CURRENT_CALLER.set(sub_caller)
    try:
        resolved, refusal = mcp_core.require_strict_session_key("Error: no session.")
    finally:
        mcp_caller._CURRENT_CALLER.set(previous)
    assert refusal == ""
    assert resolved == SUB_KEY
    assert resolved != parent_caller.session_key

    await _close(parent_reader, parent_task)
    await _close(sub_reader, sub_task)


async def _live_conn_on(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    stub_uuid: str,
    ancestor_pids: list[int],
) -> tuple[Any, Any, Any]:
    backend, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register_with_token("", token, stub_uuid=stub_uuid, ancestor_pids=ancestor_pids))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    return backend, reader, task


# ---------------------------------------------------------------------------
# The ordering the register loses: a claim that binds while the register runs
# ---------------------------------------------------------------------------


def _claim_during_register(
    monkeypatch: pytest.MonkeyPatch, frame: dict[str, Any]
) -> list[dict[str, Any]]:
    """Push *frame* while the register is parked on its start-id snapshot.

    That await is the race. Register-time resolution has already run and read an
    unbound token, and the connection is not in ``_CONN_INDEX`` yet — so the
    claim binds the token and reaches nothing, which is the measured
    ``unclaimed session token`` / ``claim matched no connections yet`` pair. The
    snapshot is an executor hop, so driving the claim from that seam makes the
    ordering deterministic rather than hoping to catch it.
    """
    loop = asyncio.get_running_loop()
    hop = LoopHop(loop)
    acks: list[dict[str, Any]] = []
    real = gw._get_process_start_id

    def snapshot(pid: int) -> Any:
        if not acks:
            acks.append(hop.run(gw._apply_claim(frame), "apply claim"))
        return real(pid)

    monkeypatch.setattr(gw, "_get_process_start_id", snapshot)
    return acks


async def _register_racing_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    claim_pid: int,
    host_chain: list[int],
    stub_pids: list[int] | None = None,
    claim_frame: dict[str, Any] | None = None,
) -> tuple[Any, list[dict[str, Any]], Any, Any]:
    backend, _sel = _patch_env(monkeypatch)
    _attest(monkeypatch, host_chain)
    if claim_frame is None:
        claim_frame = _claim_with_token(claim_pid, SUB_KEY, TOKEN_B)
    acks = _claim_during_register(monkeypatch, claim_frame)
    reader = _QueueReader()
    reader.feed(_register_with_token("", TOKEN_B, stub_uuid="stub-raced", ancestor_pids=stub_pids))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    # The claim really did miss — without that, the test proves nothing.
    assert acks == [{"type": "claim-noop", "updated": 0, "connections": 0}]
    return backend, acks, reader, task


@pytest.mark.asyncio
async def test_a_claim_that_binds_while_the_register_runs_still_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stranding case. A token-carrying connection is nameable by claim-push
    alone, so a claim that binds after resolution and before the index leaves
    nothing able to name it: every in-tree MCP call in that session is refused
    for the connection's whole life. The register asks after its last await,
    so the claim is already bound when it does and the connection is named."""
    backend, _acks, reader, task = await _register_racing_a_claim(monkeypatch, 9020, [9100, 9020])
    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    await _close(reader, task)


@pytest.mark.asyncio
async def test_the_re_ask_authenticates_on_the_attested_chain_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factor must stay the one the registrant cannot author. Here the stub
    self-reports the runtime the claim named while the kernel places it
    elsewhere, so the re-ask must refuse exactly as the register did: resolving
    against ``indexed_pids`` (which folds in ``ancestor_pids``) would let one
    process that read another session's token satisfy both halves itself."""
    backend, _acks, reader, task = await _register_racing_a_claim(
        monkeypatch, 9020, [9100], stub_pids=[9020]
    )
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_rekey_during_register_does_not_leave_the_previous_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving token re-bound to the claiming session is the ordinary
    warm-pool rekey, so the register can read session A and be overtaken by B
    across the same awaits. Reading only the identity-less half would forward
    every call in that window as A — wrong-principal execution, the class the
    claim's own two-pass ordering exists to prevent."""
    await gw._apply_claim(_claim_with_token(9020, PARENT_KEY, TOKEN_B))
    backend, _acks, reader, task = await _register_racing_a_claim(monkeypatch, 9020, [9100, 9020])
    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_rekey_onto_another_runtime_clears_the_stale_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Take the re-read whole. When the rekey moves the token to a runtime the
    kernel does not place this peer under, the honest answer is the register's
    own fail-closed one — no identity — and keeping the previous session's name
    would be a stale grant nothing later in the connection's life revokes."""
    await gw._apply_claim(_claim_with_token(9020, PARENT_KEY, TOKEN_B))
    backend, _acks, reader, task = await _register_racing_a_claim(monkeypatch, 777777, [9100, 9020])
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_recycled_pid_cannot_satisfy_a_stale_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid is a reusable NUMBER, so membership in the attested chain is not on
    its own evidence that the process the claim named is the one this stub sits
    under. The binding carries the claimed process's start token and the register
    compares it against this connection's own register-time snapshot, the same
    guard claim-push applies, so a definite mismatch refuses rather than hand
    over the session that holds the number's earlier generation."""
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: "generation-2")
    frame = _claim_with_token(9020, SUB_KEY, TOKEN_B)
    frame["pid_start_id"] = "generation-1"
    backend, _acks, reader, task = await _register_racing_a_claim(
        monkeypatch, 9020, [9100, 9020], claim_frame=frame
    )
    assert backend.callers[0] is None
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_matching_generation_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the guard above: same shape, same snapshot, and the
    claim names the generation this connection actually registered under."""
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: "generation-2")
    frame = _claim_with_token(9020, SUB_KEY, TOKEN_B)
    frame["pid_start_id"] = "generation-2"
    backend, _acks, reader, task = await _register_racing_a_claim(
        monkeypatch, 9020, [9100, 9020], claim_frame=frame
    )
    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    await _close(reader, task)


def _count_token_asks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    asks: list[tuple[Any, ...]] = []
    real = gw._token_caller

    def counting(*args: Any) -> Any:
        asks.append(args)
        return real(*args)

    monkeypatch.setattr(gw, "_token_caller", counting)
    return asks


def _denials(sel: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in sel if e.get("operation") == "mcp-gateway.peer-identity-denied"]


@pytest.mark.asyncio
async def test_a_token_carrying_stub_asks_its_binding_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ask decides the connection, and it is the one that sees a claim which
    bound while the register ran: no refusal is logged or audited for a stub
    that ends up named."""
    asks = _count_token_asks(monkeypatch)
    backend, sel = _patch_env(monkeypatch)
    _attest(monkeypatch, [9100, 9020])
    acks = _claim_during_register(monkeypatch, _claim_with_token(9020, SUB_KEY, TOKEN_B))
    reader = _QueueReader()
    reader.feed(_register_with_token("", TOKEN_B, stub_uuid="stub-once"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    assert acks == [{"type": "claim-noop", "updated": 0, "connections": 0}]
    assert len(asks) == 1
    assert backend.callers[0] is not None
    assert backend.callers[0].session_key == SUB_KEY
    assert _denials(sel) == []
    await _close(reader, task)


@pytest.mark.asyncio
async def test_an_unclaimed_token_still_logs_and_audits_its_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The single ask keeps the fail-closed branch whole: no identity, the
    ``unclaimed session token`` log line, and the peer-identity denial audit."""
    caplog.set_level(logging.INFO, logger=gw.logger.name)
    asks = _count_token_asks(monkeypatch)
    backend, sel = _patch_env(monkeypatch)
    _attest(monkeypatch, [9100, 9020])
    reader = _QueueReader()
    reader.feed(_register_with_token("", TOKEN_B, stub_uuid="stub-unclaimed"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    assert len(asks) == 1
    assert backend.callers[0] is None
    assert any("unclaimed session token" in r.getMessage() for r in caplog.records)
    denied = _denials(sel)
    assert len(denied) == 1
    assert "unclaimed session token" in denied[0]["resources"]
    await _close(reader, task)
