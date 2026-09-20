"""Per-session stub tokens: one runtime PID, one identity per ACP session.

Every identity channel the broker-stub path had before this token is keyed on
the PROCESS TREE — the stub's own ``KIROCREW_SESSION_KEY``/pid-file walk,
gatewayd's SO_PEERCRED ``/proc`` walk, and claim-push, which re-targets every
connection indexed under a runtime PID. One kiro-cli process hosts N ACP
sessions (``agent.session_sharing``: a ``spawn_run`` subagent runs on its
parent's process), so all three answer with the PARENT's session for a
subagent's stub, and a parent re-claim overwrites whatever the subagent had.

The token is the per-SESSION name that tree cannot supply. These tests pin:

* it is minted per session, rides the injected ACP entry's ``env``, and comes
  back on the Register frame WITHOUT entering the PoolKey digest,
* claim-push keyed by ``(pid, token)`` across all three topologies — no
  connection tokened, all tokened, mixed — and the isolation property that
  makes the whole item worth landing: a claim for session A leaves session B's
  stub on the same runtime alone,
* a claim carrying NO token still re-targets every connection under the PID,
  byte-for-byte as before, so a stub from a hand-written config or an older
  overlay is unaffected,
* the register path prefers the token binding over BOTH process-tree sources,
  and refuses a tree answer outright when a sibling session on the same runtime
  is named while this connection's token is not,
* the token never reaches a log record, ``stats()``, the stub fallback journal,
  or the prewarm file — it is a bearer name for a session's identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.machinery
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from test_identity_topology import GATEWAY, KIRO_CLI, MCP_SERVER, SESSION_HOST, ProcessTopology
from test_mcp_gateway_claim import (
    _ANCESTORS,
    _CALL,
    _PID,
    _WRAPPER_PID,
    _claim,
)
from test_mcp_gateway_claim import _FakeBackend as _FakeBackendType
from test_mcp_gateway_claim import (
    _FakePool,
    _handle,
    _patch_env,
    _QueueReader,
    _RecordingWriter,
    _register,
)
from test_mcp_gateway_session_inject import _PROBE_SCRIPT, REAL_CLI

from kiro_crew import mcp_caller, mcp_core, platform_compat
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import claim as claim_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import prewarm as prewarm_mod
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.session_servers import (
    STUB_SESSION_TOKEN_ENV,
    attach_stub_session_token,
)

pytestmark = pytest.mark.xdist_group("mcp_gateway")

PARENT_KEY = "dashboard:chat-1-parent"
SUB_KEY = "dashboard:chat-1-parent:sub-7"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


@pytest.fixture(autouse=True)
def _clean_gateway_state() -> Any:
    """Both module-level registries the token path touches.

    ``test_mcp_gateway_claim``'s own autouse fixture does not reach this module,
    and a leaked binding is exactly the state that would make a later test read
    a session name no claim in it ever pushed.
    """
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()


def _register_with_token(
    session_key: str,
    token: str,
    *,
    stub_uuid: str = "cp-stub-0001",
    ancestor_pids: list[int] | None = None,
) -> dict[str, Any]:
    frame = _register(session_key, ancestor_pids)
    frame["stub_uuid"] = stub_uuid
    if token:
        frame["stub_session_token"] = token
    return frame


def _claim_with_token(pid: Any, session_key: str, token: str) -> dict[str, Any]:
    frame = _claim(pid, session_key)
    if token:
        frame["stub_session_token"] = token
    return frame


# ---------------------------------------------------------------------------
# Minting and the injected entry
# ---------------------------------------------------------------------------


def test_token_is_unguessable_and_unique() -> None:
    """A guessable token IS another session's identity: gatewayd hands a
    connection the session its token names."""
    tokens = {claim_mod.mint_stub_session_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        # hex over >= 128 bits of randomness (the module mints 256).
        assert len(token) >= 32
        assert int(token, 16) >= 0


def test_attach_names_every_stub_entry_of_one_session() -> None:
    entries = [
        {"name": "one", "command": "python", "args": [], "env": []},
        {"name": "two", "command": "python", "args": [], "env": [{"name": "X", "value": "1"}]},
    ]
    out = attach_stub_session_token(entries, TOKEN_A)
    for entry in out:
        assert {"name": STUB_SESSION_TOKEN_ENV, "value": TOKEN_A} in entry["env"]
    # The operator's own pair survives beside it.
    assert {"name": "X", "value": "1"} in out[1]["env"]
    # The caller's list may be a cached array shared with another session.
    assert entries[0]["env"] == [] and len(entries[1]["env"]) == 1


def test_attach_is_a_no_op_without_a_token() -> None:
    """A build with the gateway off, or a caller that cannot mint, keeps the
    pre-token wire shape byte for byte."""
    entries = [{"name": "one", "command": "python", "args": [], "env": []}]
    assert attach_stub_session_token(entries, "") is entries


def test_attach_replaces_rather_than_appends_a_second_token() -> None:
    """Two pairs of the same name leave which one wins to the child's env
    parser — so the entry must never carry two."""
    once = attach_stub_session_token(
        [{"name": "one", "command": "python", "args": [], "env": []}], TOKEN_A
    )
    twice = attach_stub_session_token(once, TOKEN_B)
    names = [pair["name"] for pair in twice[0]["env"]]
    assert names.count(STUB_SESSION_TOKEN_ENV) == 1
    assert twice[0]["env"][-1]["value"] == TOKEN_B


def test_stub_forwards_the_token_from_its_env(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _stub_args()
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    assert stub_mod.build_register_payload(args)["stub_session_token"] == TOKEN_A
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV)
    # Absent, the key is omitted entirely rather than sent empty: gatewayd's
    # PID-keyed behavior is selected by the field NOT being there.
    assert "stub_session_token" not in stub_mod.build_register_payload(args)


def test_the_token_is_not_a_pool_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-connection value in the PoolKey would give every session its own
    backend and pooling would silently stop."""
    args = _stub_args()
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    first = stub_mod.build_register_payload(args)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_B)
    second = stub_mod.build_register_payload(args)
    assert first["stub_session_token"] != second["stub_session_token"]
    assert PoolKey.from_register(first).stable_hash() == PoolKey.from_register(second).stable_hash()


def _stub_args() -> Any:
    return stub_mod._parse_args(
        [
            "--server",
            "echo-mcp",
            "--agent",
            "cp-agent",
            "--target-command",
            "python",
            "--work-dir",
            "/tmp",
            "--poolable",
        ]
    )


# ---------------------------------------------------------------------------
# Claim-push keyed by (pid, token) — the three topologies
# ---------------------------------------------------------------------------


async def _live_conn(
    monkeypatch: pytest.MonkeyPatch,
    session_key: str,
    token: str,
    stub_uuid: str,
) -> tuple[Any, Any, Any]:
    """Drive one real connection handler to its first forwarded call."""
    backend, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register_with_token(session_key, token, stub_uuid=stub_uuid))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
    return backend, reader, task


def _attest(monkeypatch: pytest.MonkeyPatch, host_chain: list[int]) -> None:
    """Make the kernel place this connection's peer under *host_chain*.

    The token's second factor is the SO_PEERCRED-derived chain gatewayd walks
    itself, so a test that wants a token honoured has to attest one; a test that
    wants it refused simply does not.
    """
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: host_chain[0])
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", list(host_chain)))


async def _close(reader: Any, task: Any) -> None:
    reader.feed({"type": "unregister"})
    await task


async def _next_caller(backend: Any, reader: Any) -> Any:
    """The identity the connection forwards on its NEXT call."""
    backend.forwarded.clear()
    reader.feed(_CALL)
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
    return backend.callers[-1]


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
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)

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
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
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
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
    return backend, reader, task


# ---------------------------------------------------------------------------
# The token is a bearer name: it must not be written anywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_log_record_or_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Anyone who can read a token can be the session it names, so it must not
    appear in a log line an operator pastes into a ticket, nor in the metrics
    snapshot the dashboard renders."""
    caplog.set_level(logging.DEBUG)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn(monkeypatch, PARENT_KEY, TOKEN_A, "stub-parent")
    await _close(reader, task)
    assert TOKEN_A not in caplog.text

    snapshot = json.dumps(_FakePoolStats().stats())
    assert TOKEN_A not in snapshot


class _FakePoolStats(_FakePool):
    """``BackendPool.stats()`` shape as the control plane returns it — the token
    is not one of its dimensions and must not become one."""

    def stats(self) -> dict[str, Any]:
        return {"backends": 1, "sessions": 2, "pool_labels": ["cp-agent:echo-mcp"]}


def test_the_token_never_reaches_the_stub_fallback_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The journal survives the process and is world-readable to the operator's
    own tooling; a degrading stub must not leave its session's name in it."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    monkeypatch.setattr(stub_mod, "_fallback_log_path", lambda: tmp_path / "stub_fallback.jsonl")
    stub_mod.log_fallback("handshake_timeout", "stub-parent", "cp-agent:echo-mcp", _stub_args())
    assert TOKEN_A not in (tmp_path / "stub_fallback.jsonl").read_text(encoding="utf-8")


def test_the_fallback_backend_never_inherits_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the gateway is unavailable the stub EXECs the operator's real backend
    in its own place, copying its environment wholesale. That environment holds
    this session's token, and the process about to inherit it is a third-party
    server binary which could later register with it and be answered as this
    session. Its own declared env is restored on that path; the token never was
    part of it.
    """
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    monkeypatch.setenv("PATH", str(tmp_path))
    declared = tmp_path / "env.json"
    declared.write_text(json.dumps({"REAL_SERVER_KEY": "abc"}), encoding="utf-8")
    args = stub_mod._parse_args(
        [
            "--server",
            "echo-mcp",
            "--agent",
            "cp-agent",
            "--target-command",
            "true",
            "--work-dir",
            str(tmp_path),
            "--env-file",
            str(declared),
        ]
    )

    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: list[str], _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit):
        stub_mod.fallback_exec(args)

    assert STUB_SESSION_TOKEN_ENV not in seen["env"]
    assert TOKEN_A not in json.dumps(seen["env"])
    # …while the server's OWN declared env still reaches it: this scrub must not
    # cost the fallback path the parity with a directly-launched server that is
    # its entire purpose.
    assert seen["env"]["REAL_SERVER_KEY"] == "abc"


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_prewarm_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prewarming PERSISTS whole register payloads so the hottest PoolKeys can be
    respawned at startup. It only ever needs the pool dimensions, and a token on
    disk outlives the session it names."""
    hot = prewarm_mod.HotKeyStore(tmp_path / "hot_keys.json")
    backend, sel = _patch_env(monkeypatch)

    async def _get(_key: Any) -> None:
        return None

    pool = _FakePool()
    setattr(pool, "get", _get)
    reader = _QueueReader()
    reader.feed(_register_with_token(PARENT_KEY, TOKEN_A, stub_uuid="stub-parent"))
    reader.feed(_CALL)
    task = asyncio.create_task(
        asyncio.wait_for(
            gw._handle_connection(
                reader,
                _RecordingWriter(),
                pool=pool,
                resolver=object(),
                socket_path=Path("/tmp/cp.sock"),
                hot_keys=hot,
            ),
            timeout=5.0,
        )
    )
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
    await _close(reader, task)

    payloads = hot.top_register_payloads(10)
    assert payloads, "the register was never recorded — this test proves nothing"
    assert TOKEN_A not in json.dumps(payloads)
    hot.flush()
    assert TOKEN_A not in (tmp_path / "hot_keys.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The Crew side: who mints the token, and when the claim is pushed
# ---------------------------------------------------------------------------


def _bare_runtime(
    monkeypatch: pytest.MonkeyPatch,
    pid: int = 4242,
    published: list[tuple[str, str]] | None = None,
) -> Any:
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime(work_dir="/tmp")
    runtime._mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
    monkeypatch.setattr(type(runtime), "pid", property(lambda _self: pid))
    # ``_own_stub_session`` now also publishes the token's signed mapping (the
    # switch-free identity channel). Recorded rather than written: these are unit
    # tests of the naming/claim contract, and letting them touch the mapping
    # directory would make each one depend on a trust root it never set up.
    import kiro_crew.acp.runtime as rt_mod

    sink = published if published is not None else []
    monkeypatch.setattr(
        rt_mod, "publish_session_token", lambda token, key: sink.append((token, key))
    )
    return runtime


@pytest.mark.asyncio
async def test_the_runtime_names_the_session_before_its_stubs_can_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kiro-cli launches this session's stubs while it serves ``session/new``, so
    a claim sent afterwards races the register it exists to inform. The claim is
    pushed — and awaited — first, and the token it names is the one on the
    entries handed to that request."""
    import kiro_crew.acp.runtime as rt_mod

    sent: list[tuple[Any, ...]] = []

    async def _record(socket_path, pid, session_key, channel_id, token):
        sent.append((socket_path, pid, session_key, token))
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _record)
    runtime = _bare_runtime(monkeypatch)
    entries, token = await runtime._own_stub_session(
        [{"name": "one", "command": "python", "args": [], "env": []}], SUB_KEY
    )
    assert token
    assert {"name": STUB_SESSION_TOKEN_ENV, "value": token} in entries[0]["env"]
    assert sent == [("/tmp/kirocrew-gw.sock", 4242, SUB_KEY, token)]


@pytest.mark.asyncio
async def test_each_session_on_one_runtime_gets_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sessions sharing a kiro-cli process must not share the name that
    tells them apart."""
    import kiro_crew.acp.runtime as rt_mod

    async def _ok(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _ok)
    runtime = _bare_runtime(monkeypatch)
    entry = [{"name": "one", "command": "python", "args": [], "env": []}]
    _first, parent_token = await runtime._own_stub_session(list(entry), PARENT_KEY)
    _second, sub_token = await runtime._own_stub_session(list(entry), SUB_KEY)
    assert parent_token and sub_token and parent_token != sub_token


@pytest.mark.asyncio
async def test_an_unclaimed_worker_mints_a_token_but_pushes_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm-pool worker is spawned before any session claims it, so there is
    no owner to name yet — its ``rekey()`` carries the same token later."""
    import kiro_crew.acp.runtime as rt_mod

    sent: list[Any] = []

    async def _record(*a: Any, **k: Any) -> bool:
        sent.append(a)
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _record)
    runtime = _bare_runtime(monkeypatch)
    entries, token = await runtime._own_stub_session(
        [{"name": "one", "command": "python", "args": [], "env": []}], ""
    )
    assert token
    assert {"name": STUB_SESSION_TOKEN_ENV, "value": token} in entries[0]["env"]
    assert sent == []


@pytest.mark.asyncio
async def test_no_stub_entries_still_names_the_session_but_pushes_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway off: nothing to CLAIM, but the session still gets a name.

    A token without a reachable gatewayd is not inert, because gatewayd is not its
    only reader: it also resolves through a MAC-signed mapping file
    (:mod:`kiro_crew.session_token_sig`) that the strict identity resolver reads
    with no daemon, no broker stub and no config switch. So a session on a
    gateway-less install — the default install — needs a name of its own, and
    withholding one is the identity gap this asserts against.

    The claim half is independent and is asserted too: with no stub entries there
    is no stub connection for a claim to inform, so no claim is pushed.
    """
    import kiro_crew.acp.runtime as rt_mod

    async def _boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("claimed a session with no stub connections to inform")

    monkeypatch.setattr(rt_mod, "send_claim", _boom)
    published: list[tuple[str, str]] = []
    runtime = _bare_runtime(monkeypatch, published=published)
    entries, token = await runtime._own_stub_session([], PARENT_KEY)
    assert entries == []
    assert token
    # ...and the name is published, or the resolver would have nothing to read.
    assert published == [(token, PARENT_KEY)]


def test_the_shared_runtime_rekey_claims_its_own_session_not_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm-pool re-claim on a runtime hosting subagents must reach only the
    claiming session's stubs — which is the token the handle carries."""
    import kiro_crew.acp.session_provider as sp_mod

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )

    class _Handle:
        stub_session_token = TOKEN_A

        def rebind_watchdog(self, *_a: Any, **_k: Any) -> None:
            pass

        class last_prompt_stats:  # noqa: N801 - mirrors the real attribute name
            @staticmethod
            def reset_context_state() -> None:
                pass

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = 4242
        _crew_agent = ""
        _last_activity = 0.0

    provider = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    provider._handle = _Handle()
    provider._runtime = _Runtime()
    provider.rekey(PARENT_KEY, None, "", None)
    assert pushed == [(4242, PARENT_KEY, TOKEN_A)]


#: Every ``create_session`` call that opens a session on a runtime SHARED with
#: other sessions, and the local name holding that session's key. A session here
#: that names no owner carries a token no claim ever names, so its stubs resolve
#: to nothing — and before the token they resolved to whichever session the
#: shared runtime's process tree pointed at. Deleting one keyword argument
#: re-opens exactly that, which no behavioural test on another file would catch.
_SHARED_RUNTIME_SESSION_SITES = [
    ("src/kiro_crew/subagent_manager/run.py", "session_key=session_key"),
    ("src/kiro_crew/session_allocation.py", "session_key=key"),
]


@pytest.mark.parametrize("path,expected", _SHARED_RUNTIME_SESSION_SITES)
def test_sessions_on_a_shared_runtime_name_their_own_owner(path: str, expected: str) -> None:
    body = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    start = body.index("await runtime.create_session(")
    call = body[start : body.index("\n        )", start)]
    assert expected in call, f"{path} opens a shared-runtime session without naming its owner"


def test_every_turn_re_pushes_the_sessions_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding lives in the daemon's memory, so it can be lost while the
    session is alive. The turn boundary that already rewrites the pid file is
    where it is re-established — reached through ``getattr`` so a provider
    without stubs is left alone."""
    import asyncio as _asyncio

    from kiro_crew.messaging import identity as identity_mod

    calls: list[str] = []

    class _Inner:
        def reclaim(self) -> None:
            calls.append("reclaimed")

    class _Provider:
        client = _Inner()

    class _Sessions:
        def get_pid(self, _key: str) -> None:
            return None

        def get_provider(self, _key: str) -> Any:
            return _Provider()

    _asyncio.run(identity_mod.publish_turn_identity(_Sessions(), PARENT_KEY))
    assert calls == ["reclaimed"]


def test_a_provider_without_stubs_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stub token means no claim to push and nothing that would read one."""
    import asyncio as _asyncio

    from kiro_crew.messaging import identity as identity_mod

    class _Sessions:
        def get_pid(self, _key: str) -> None:
            return None

        def get_provider(self, _key: str) -> Any:
            return object()

    _asyncio.run(identity_mod.publish_turn_identity(_Sessions(), PARENT_KEY))


@pytest.mark.asyncio
async def test_warm_reuse_claims_the_fresh_sessions_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``new_conversation`` reuses the runtime and launches FRESH stubs, so it
    mints a fresh token that nothing has named. Left unclaimed, those stubs are
    refused — a warm worker would run its whole next task with no identity."""
    import kiro_crew.acp.session_provider as sp_mod

    class _Handle:
        def __init__(self, token: str = "") -> None:
            self.stub_session_token = token
            self.memory_mode = "persistent"
            self.model = ""
            self.session_id = "fresh"
            self.destroyed = False

        async def destroy(self) -> None:
            self.destroyed = True

    fresh = _Handle(TOKEN_B)

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        _work_dir = "/tmp/ws"
        _agent = "kirocrew"
        pid = 4242
        # ``new_conversation`` refuses a backend whose teardown does not evict the
        # old session, so it reads the backend before creating anything. ``""`` is
        # not "unset" here -- it is kiro's own id (``ACP_BACKEND_KIRO``), the real
        # runtime's default and a member of the eviction set. Warm reuse succeeding
        # IS this test's subject, so the stub has to carry it.
        acp_backend = ""

        def is_alive(self) -> bool:
            return True

        async def create_session(self, **_kwargs: Any) -> Any:
            return fresh

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((key, token)),
    )
    provider = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    provider._handle = _Handle(TOKEN_A)
    provider._runtime = _Runtime()
    provider._session_key = PARENT_KEY
    provider._channel_id = None
    await provider.new_conversation()

    assert provider._handle is fresh
    assert pushed == [(PARENT_KEY, TOKEN_B)]


@pytest.mark.asyncio
async def test_a_cold_started_session_can_re_bind_after_a_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold-start topology: a top-level kiro session that never rekeys.

    ``rekey()`` is a warm-pool event, so a pool miss, pooling switched off, or a
    subagent's own session reaches it never — and a provider that learned its key
    only there would re-claim with an EMPTY session key. gatewayd rejects that
    frame as malformed BEFORE recording the binding, so the token could never be
    re-bound: one daemon respawn and the session is identity-less for life. The
    key therefore arrives at construction, and this asserts the whole chain —
    empty key binds nothing, seeded key re-binds and re-targets the live stub.
    """
    import kiro_crew.acp.session_provider as sp_mod

    _attest(monkeypatch, _ANCESTORS)
    backend, reader, task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-cold")
    assert backend.callers[0] is None

    class _Handle:
        stub_session_token = TOKEN_A

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = _PID

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )

    # What a provider that never learned its key would have pushed — and what
    # gatewayd does with it: nothing, not even the binding.
    keyless = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    keyless._handle = _Handle()
    keyless._runtime = _Runtime()
    keyless._session_key = ""
    keyless._channel_id = None
    keyless.reclaim()
    assert pushed == [(_PID, "", TOKEN_A)]
    ack = await gw._apply_claim(_claim_with_token(_PID, "", TOKEN_A))
    assert ack["type"] == "claim-rejected"
    assert gw._token_is_unbound(TOKEN_A)

    # The cold-start provider is handed its owner at construction instead.
    pushed.clear()
    seeded = sp_mod.AcpSessionProvider(
        _Handle(), _Runtime(), session_key=PARENT_KEY, channel_id="C_CP"
    )
    seeded.reclaim()
    assert pushed == [(_PID, PARENT_KEY, TOKEN_A)]
    ack = await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert ack["type"] == "claimed" and ack["updated"] == 1
    assert (await _next_caller(backend, reader)).session_key == PARENT_KEY
    await _close(reader, task)


#: Every construction of a provider over a session that will never rekey, and
#: the local name holding that session's key. Same failure mode as the
#: shared-runtime session sites: one deleted keyword argument and the provider's
#: re-claim carries no key, which gatewayd discards.
_PROVIDER_OWNER_SITES = [
    ("src/kiro_crew/providers/acp.py", "session_key=self._owning_session_key()"),
    ("src/kiro_crew/subagent_manager/run.py", "session_key=session_key"),
]


@pytest.mark.parametrize("path,expected", _PROVIDER_OWNER_SITES)
def test_every_provider_is_told_which_session_it_serves(path: str, expected: str) -> None:
    body = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    # The CONSTRUCTION, not a mention: the module also names the class in its
    # import and in prose about which client shape a slot carries.
    start = body.index("= AcpSessionProvider(")
    call = body[start : body.index("\n        )", start)]
    assert expected in call, f"{path} builds a provider that cannot name its own session"


@pytest.mark.asyncio
async def test_a_subagents_own_turn_re_establishes_its_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent's turn is driven by its manager, not by a dispatch surface, so
    the shared identity publisher never runs for it — and it is the session type
    this token exists to protect. Its provider re-claims at its own turn
    boundary, so a daemon respawn mid-run costs it one turn rather than the whole
    remaining run."""
    import kiro_crew.acp.session_provider as sp_mod

    class _Handle:
        stub_session_token = TOKEN_B
        session_id = "subagent-test-session"

        async def prompt(self, _message: str) -> Any:
            assert pushed == [(_PID, SUB_KEY, TOKEN_B)]
            for event in ():
                yield event

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = _PID
        process_instance = "runtime-test-instance"

        def saw_not_logged_in(self) -> bool:
            return False

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )
    provider = sp_mod.AcpSessionProvider(_Handle(), _Runtime(), session_key=SUB_KEY)
    async for _event in provider.stream("go"):
        pass
    assert pushed == [(_PID, SUB_KEY, TOKEN_B)]


def test_the_client_reclaim_names_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.acp.client as client_mod

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        client_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((key, token)),
    )
    client = client_mod.AcpClient.__new__(client_mod.AcpClient)
    client._stub_session_token = TOKEN_A
    client._mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
    client._session_key = PARENT_KEY
    client._channel_id = None
    client._process = None
    client.reclaim()
    assert pushed == [(PARENT_KEY, TOKEN_A)]
    # A client with no injected stubs pushes nothing: there is no token to name.
    pushed.clear()
    client._stub_session_token = ""
    client.reclaim()
    assert pushed == []


# ---------------------------------------------------------------------------
# The shipped binary: env on the injected element does not cost precedence
# ---------------------------------------------------------------------------

_TOKEN_DRIVER = r"""
import json, os, subprocess, sys, threading, time
from kiro_crew import platform_compat

w = sys.argv[1]
p = subprocess.Popen(["kiro-cli", "acp", "--agent", "pooltest"], cwd=w + "/proj",
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True,
                     env={**os.environ, "KIRO_HOME": w + "/khome"})


def teardown():
    try:
        platform_compat.kill_process_tree(p.pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        p.kill()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    for stream in (p.stdin, p.stdout, p.stderr):
        try:
            stream.close()
        except OSError:
            pass


send = lambda o: (p.stdin.write(json.dumps(o) + "\n"), p.stdin.flush())
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1, "clientCapabilities":
                 {"fs": {"readTextFile": False, "writeTextFile": False}}}})
_line = []
_t = threading.Thread(target=lambda: _line.append(p.stdout.readline()), daemon=True)
_t.start()
_t.join(30)
if not _line:
    teardown()
    sys.exit("kiro-cli never answered initialize within 30s")
send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": w + "/proj", "mcpServers": [
          {"name": "shared", "command": sys.executable,
           "args": [sys.argv[2], w + "/marks/INJECTED"],
           "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": sys.argv[3]}]}]}})
deadline = time.time() + 20
injected = os.path.join(w, "marks", "INJECTED")
while time.time() < deadline and not os.path.exists(injected):
    time.sleep(0.05)
time.sleep(1)
teardown()
"""

#: Writes the marker AND the token it observed in its own env, so the test can
#: tell "the element was honored" from "the element's env was honored".
_TOKEN_PROBE = _PROBE_SCRIPT.replace(
    'pathlib.Path(sys.argv[1]).write_text("x", encoding="utf-8")',
    "pathlib.Path(sys.argv[1]).write_text(\n"
    '    os.environ.get("KIROCREW_STUB_SESSION_TOKEN", ""), encoding="utf-8"\n'
    ")",
).replace("import json\nimport pathlib", "import json\nimport os\nimport pathlib")


@pytest.mark.skipif(not REAL_CLI, reason="kiro-cli not on PATH")
@pytest.mark.skipif(os.name != "posix", reason="POSIX pathing in fixture")
def test_real_kiro_cli_delivers_the_token_and_still_overrides_the_spec() -> None:
    """ANTI-DRIFT GUARD, the token half.

    ``test_real_kiro_cli_prefers_session_injected_server`` pins that a
    session-injected element outranks the agent spec's same-named entry — with
    an EMPTY ``env``. The token rides that element's ``env``, so this pins the
    two things that shape now depends on: the child receives the pair, and
    carrying it does not cost the precedence pooling relies on.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "khome" / "agents").mkdir(parents=True)
        (root / "proj").mkdir()
        (root / "marks").mkdir()
        probe = root / "probe.py"
        probe.write_text(_TOKEN_PROBE, encoding="utf-8")
        (root / "khome" / "agents" / "pooltest.json").write_text(
            json.dumps(
                {
                    "name": "pooltest",
                    "description": "token delivery probe",
                    "model": "claude-haiku-4.5",
                    "tools": [],
                    "prompt": "probe",
                    "mcpServers": {
                        "shared": {
                            "command": sys.executable,
                            "args": [str(probe), str(root / "marks" / "FROM_SPEC")],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        driver = root / "drive.py"
        driver.write_text(_TOKEN_DRIVER, encoding="utf-8")
        # Popen + a finally that reaps the whole TREE, not subprocess.run: the
        # driver spawns kiro-cli, which spawns the probe servers, and a
        # parent-side timeout would return from run() leaving all of them alive
        # holding the temp dir. The driver tears its own tree down on every exit
        # it controls; this covers the exit it does not.
        # kiro-cli writes its own log directory and telemetry spool under TMPDIR;
        # aimed at this tree, that residue is deleted with the test's directory
        # instead of outliving it in the shared temp root.
        child_tmp = root / "tmp"
        child_tmp.mkdir()
        child_env = {
            **os.environ,
            "TMPDIR": str(child_tmp),
            "TMP": str(child_tmp),
            "TEMP": str(child_tmp),
        }
        proc = subprocess.Popen(
            [sys.executable, str(driver), str(root), str(probe), TOKEN_A],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "driver exceeded its 180s budget"
        finally:
            if proc.poll() is None:
                try:
                    platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    proc.kill()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        deadline = time.time() + 5
        while time.time() < deadline and not (root / "marks" / "INJECTED").exists():
            time.sleep(0.2)
        assert (root / "marks" / "INJECTED").exists(), (
            "the injected server never launched with env on its element\n"
            f"driver exit: {proc.returncode}\n"
            f"driver stdout: {(stdout or '')[-2000:]}\n"
            f"driver stderr: {(stderr or '')[-2000:]}"
        )
        assert (root / "marks" / "INJECTED").read_text(encoding="utf-8") == TOKEN_A, (
            "the element's env never reached the child: the stub cannot report a "
            "token it was not given, and every session on a shared runtime would "
            "fall back to the parent's identity"
        )
        assert not (root / "marks" / "FROM_SPEC").exists(), (
            "the spec's same-named server ALSO launched: an element carrying env "
            "no longer overrides, so every pooled server would run twice"
        )


# ---------------------------------------------------------------------------
# The token reaches Kiro Crew's OWN pooled control planes, and nothing else
# ---------------------------------------------------------------------------


async def _live_conn_for_server(
    monkeypatch: pytest.MonkeyPatch, server_name: str, *, control_plane: bool
) -> tuple[Any, Any, Any]:
    """A token-carrying, claimed connection whose stub fronts *server_name*.

    ``control_plane`` is what the spawn path decided from the resolved command;
    the handler reads that flag, never the name the stub registered.
    """
    backend, sel = _patch_env(monkeypatch)
    backend.control_plane = control_plane
    reader = _QueueReader()
    frame = _register_with_token(PARENT_KEY, TOKEN_A, stub_uuid=f"stub-{server_name}")
    frame["server_name"] = server_name
    reader.feed(frame)
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(backend.forwarded.wait(), timeout=5.0)
    return backend, reader, task


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", sorted(gw.CONTROL_PLANE_BACKENDS))
async def test_a_pooled_control_plane_is_handed_the_sessions_token(
    monkeypatch: pytest.MonkeyPatch, server_name: str
) -> None:
    """gatewayd spawns a pooled backend from its OWN environment, so the
    per-session token is not in that backend's env. ``kirocrew-core`` and
    ``kirocrew-cron`` post back to the gateway for the session they act for and
    must prove it with ``X-Session-Token``; the caller block is the only channel
    that can carry it to them."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, server_name, control_plane=True
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == TOKEN_A
    parsed = CallerContext.from_meta(mcp_caller.build_caller_meta(caller))
    assert parsed is not None and parsed.session_token == TOKEN_A
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_third_party_backend_never_sees_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anyone who can read the token can be the session it names. A pooled
    third-party server gets the caller's identity, never its bearer name."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, "echo-mcp", control_plane=False
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == ""
    assert "sessionToken" not in mcp_caller.build_caller_meta(caller)[mcp_caller.CALLER_META_KEY]
    await _close(reader, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", sorted(gw.CONTROL_PLANE_BACKENDS))
async def test_a_reserved_name_on_a_foreign_command_never_sees_the_token(
    monkeypatch: pytest.MonkeyPatch, server_name: str
) -> None:
    """The name in the register frame is the stub's claim; the spawn target
    resolves separately from the spec. A backend registered under a reserved
    name whose command was NOT ours is a third party wearing our name, and the
    token stays out of its caller block."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, server_name, control_plane=False
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == ""
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_respawned_replacement_is_judged_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control plane that dies mid-session is replaced by a fresh spawn, and
    the replacement is re-checked: one that fails the control-plane check gets
    a tokenless caller, both on the respawn's own probe and on every frame the
    session forwards afterwards. The base caller the handler holds for the
    connection never carries the token, so the token the dead backend was
    entitled to cannot ride along to whatever answers next."""
    from kiro_crew.mcp_gateway.backend import BackendGone

    class _DiesOnce(_FakeBackendType):
        control_plane = True

        async def forward_from_stub(
            self, _uuid: str, _msg: dict, caller: Any = None, tenant_nonce: str = ""
        ) -> None:
            self.callers.append(caller)
            self.forwarded.set()
            raise BackendGone("died")

    replacement = _FakeBackendType()
    replacement.control_plane = False
    handed_to_respawn: list[Any] = []

    async def _fake_respawn(*_args: Any, caller: Any = None, **_kw: Any) -> Any:
        handed_to_respawn.append(caller)
        return replacement, asyncio.Queue(), asyncio.create_task(asyncio.sleep(0))

    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    _, sel = _patch_env(monkeypatch)
    dying = _DiesOnce()

    async def _acquire_dying(_pool: Any, _key: Any, _resolver: Any, **_kw: Any) -> Any:
        return dying, True

    monkeypatch.setattr(gw, "_acquire_backend", _acquire_dying)
    monkeypatch.setattr(gw, "_respawn_backend_for_stub", _fake_respawn)
    reader = _QueueReader()
    frame = _register_with_token(PARENT_KEY, TOKEN_A, stub_uuid="stub-respawn")
    frame["server_name"] = "kirocrew-core"
    reader.feed(frame)
    reader.feed(_CALL)
    reader.feed(dict(_CALL, id=2))
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(replacement.forwarded.wait(), timeout=5.0)

    # The dead control plane was entitled to the token ...
    assert dying.callers[0] is not None and dying.callers[0].session_token == TOKEN_A
    # ... the respawn was handed the tokenless base, so its probe of the
    # replacement cannot leak it ...
    assert handed_to_respawn and handed_to_respawn[0].session_token == ""
    # ... and the replacement that failed the check never sees it either.
    assert replacement.callers[0] is not None
    assert replacement.callers[0].session_key == PARENT_KEY
    assert replacement.callers[0].session_token == ""
    await _close(reader, task)


def test_the_token_is_attached_per_backend_never_to_the_base_caller() -> None:
    """``_caller_for_backend`` is the single place the token joins a caller: a
    control plane gets a token-bearing COPY, anything else gets the base back
    unchanged, and the base itself is never mutated."""
    from types import SimpleNamespace

    base = CallerContext(session_key=PARENT_KEY)
    conn = SimpleNamespace(stub_session_token=TOKEN_A)
    ours = SimpleNamespace(control_plane=True)
    theirs = SimpleNamespace(control_plane=False)

    handed = gw._caller_for_backend(ours, base, conn)  # type: ignore[arg-type]
    assert handed is not None and handed.session_token == TOKEN_A
    assert handed is not base and base.session_token == ""
    assert gw._caller_for_backend(theirs, base, conn) is base  # type: ignore[arg-type]
    assert gw._caller_for_backend(ours, base, None) is base  # type: ignore[arg-type]
    assert gw._caller_for_backend(ours, None, conn) is None  # type: ignore[arg-type]
    assert (
        gw._caller_for_backend(ours, base, SimpleNamespace(stub_session_token=""))  # type: ignore[arg-type]
        is base
    )


def test_control_plane_set_mirrors_session_mcp() -> None:
    """gatewayd names the set itself so the daemon does not import
    ``kiro_crew.agent`` at boot; this pin is what stops the two copies drifting."""
    from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS

    assert gw.CONTROL_PLANE_BACKENDS == frozenset(CONTROL_PLANE_SERVERS)


class TestSpawnsOwnControlPlane:
    """``_spawns_own_control_plane`` is decided from the exec'd command against
    the managed spec's invocation -- the same source the spec writer reads."""

    @pytest.fixture
    def managed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
        launcher = tmp_path / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        entry = {"command": str(launcher), "args": ["mcp-core"]}
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name: dict(entry) if name == "kirocrew-core" else None,
        )
        return entry

    def test_the_managed_invocation_is_ours(self, managed: dict[str, Any]) -> None:
        assert gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env={}
        )

    def test_a_symlink_to_the_launcher_is_the_same_program(
        self, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(managed["command"])
        assert gw._spawns_own_control_plane("kirocrew-core", str(alias), ["mcp-core"], env={})

    def test_a_foreign_binary_under_the_reserved_name_is_not(
        self, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        other = tmp_path / "evil"
        other.write_text("#!/bin/sh\n")
        assert not gw._spawns_own_control_plane("kirocrew-core", str(other), ["mcp-core"])

    def test_our_binary_with_different_args_is_not(self, managed: dict[str, Any]) -> None:
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-cron"])
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core", "--extra"]
        )

    def test_a_name_outside_the_set_is_never_ours(self, managed: dict[str, Any]) -> None:
        assert not gw._spawns_own_control_plane("echo-mcp", managed["command"], ["mcp-core"])

    def test_a_denied_reserved_name_is_logged_with_the_failed_condition(
        self,
        managed: dict[str, Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legitimate install that trips the check otherwise presents only as
        every cron tool answering 403: the daemon log must name the backend and
        which condition failed (binary, args, shadowed root, or no spec)."""
        other = tmp_path / "evil"
        other.write_text("#!/bin/sh\n")
        shadow_root = tmp_path / "proj"
        (shadow_root / "kiro_crew").mkdir(parents=True)
        (shadow_root / "kiro_crew" / "__init__.py").write_text("")
        cases = [
            (dict(command=str(other), args=["mcp-core"]), "is not the spec's"),
            (dict(command=managed["command"], args=["mcp-cron"]), "differ from spec"),
            (
                dict(
                    command=managed["command"],
                    args=["mcp-core"],
                    env={"PYTHONPATH": str(shadow_root)},
                ),
                "child environment carries non-empty PYTHONPATH",
            ),
        ]
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            for kwargs, fragment in cases:
                caplog.clear()
                assert not gw._spawns_own_control_plane("kirocrew-core", **kwargs)
                (record,) = [r for r in caplog.records if "denied the session token" in r.message]
                assert "'kirocrew-core'" in record.message and fragment in record.message
            caplog.clear()
            import kiro_crew.agent as agent_mod

            monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name: None)
            assert not gw._spawns_own_control_plane(
                "kirocrew-core", managed["command"], ["mcp-core"]
            )
            assert any("no managed spec entry" in r.message for r in caplog.records)
            # A name outside the set is not a denial of anything reserved: silent.
            caplog.clear()
            assert not gw._spawns_own_control_plane("echo-mcp", managed["command"], ["mcp-core"])
            assert not [r for r in caplog.records if "denied the session token" in r.message]

    def test_an_unresolvable_managed_entry_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, managed: dict[str, Any]
    ) -> None:
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name: None)
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-core"])

    def test_the_real_managed_entry_matches_itself(self) -> None:
        """No patching: whatever this install emits for ``kirocrew-cron`` must be
        recognised as ours, or the fix that hands the token over is inert."""
        from kiro_crew.agent import managed_mcp_spec_entry

        entry = managed_mcp_spec_entry("kirocrew-cron")
        assert entry is not None
        assert gw._spawns_own_control_plane(
            "kirocrew-cron", entry["command"], entry["args"], env={}
        )

    def test_a_kiro_crew_package_beside_the_launcher_is_not_ours(
        self, managed: dict[str, Any]
    ) -> None:
        """Script form: the launcher's directory is ``sys.path[0]``."""
        beside = Path(managed["command"]).parent / "kiro_crew"
        beside.mkdir()
        (beside / "__init__.py").write_text("")
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-core"])

    def test_the_spawn_site_decides_off_the_event_loop(self) -> None:
        """The classifier imports ``kiro_crew.agent``, reads config and stats a
        fixed local root; the one production call must run in a thread."""
        import inspect
        import re

        source = inspect.getsource(gw)
        bare_calls = [
            m.start()
            for m in re.finditer(r"(?<!def )_spawns_own_control_plane\(", source)
            if not source[max(0, m.start() - 200) : m.start()]
            .rstrip()
            .endswith("await asyncio.to_thread(")
        ]
        assert not bare_calls, "the control-plane verdict is computed on the event loop"
        assert "await asyncio.to_thread(\n            _spawns_own_control_plane," in source

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [True, False], ids=["ours", "denied"])
    async def test_the_verdict_is_decided_before_the_child_exists(
        self, monkeypatch: pytest.MonkeyPatch, verdict: bool
    ) -> None:
        """The check reads the child's final env and fixed local root. Run
        after the spawn, foreign code could erase its own shadow before the
        parent looked and be handed the token; so the verdict is taken BEFORE
        ``spawn_backend`` forks. ``PYTHONSAFEPATH`` remains defense in depth,
        never part of the grant decision. A denied backend gets neither the
        flag nor the token."""
        from test_mcp_gateway_spawn_gate import _fake_backend, _register_frame

        from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

        order: list[str] = []
        spawned_env: dict[str, str] = {}
        backend = _fake_backend()

        def classify(server_name: str, command: str, args: Any, *, env: Any, work_dir: Any) -> bool:
            assert server_name == "kirocrew-cron" and command == "kirocrew" and args == ["mcp-cron"]
            assert "PYTHONSAFEPATH" not in env, "the check sees the env the child would get"
            order.append("verdict")
            return verdict

        async def fake_spawn(**kwargs: Any) -> Any:
            order.append("spawn")
            spawned_env.update(kwargs["env"])
            return backend

        monkeypatch.setattr(gw, "_spawns_own_control_plane", classify)
        monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
        monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (dict(env), []))
        key = PoolKey.from_register(_register_frame(server_name="kirocrew-cron"))
        got, was_spawned = await gw._acquire_backend(
            BackendPool(max_backends=4),
            key,
            lambda k: ("kirocrew", ["mcp-cron"], {}, k.work_dir),
        )

        assert got is backend and was_spawned
        assert order == ["verdict", "spawn"], "the verdict must precede the fork"
        assert backend.control_plane is verdict
        assert (spawned_env.get("PYTHONSAFEPATH") == "1") is verdict

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("server_name", "command", "args"),
        [("kirocrew-cron", "kirocrew", ["mcp-cron"]), ("echo-mcp", "echo-server", ["--stdio"])],
        ids=["control-plane", "third-party"],
    )
    async def test_utf8_pinning_is_applied_after_the_verdict_for_every_backend(
        self, monkeypatch: pytest.MonkeyPatch, server_name: str, command: str, args: list[str]
    ) -> None:
        """The classifier is handed a ``PYTHON*``-free environment and the child
        still receives Kiro Crew's UTF-8 pair. Re-asserting the pair before the
        verdict would deny every control plane its own token; dropping it would
        let a pooled interpreter build stdio from a Windows ANSI codepage."""
        from test_mcp_gateway_spawn_gate import _fake_backend, _register_frame

        from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
        from kiro_crew.platform_compat import _UTF8_PROCESS_ENV

        classifier_env: dict[str, str] = {}
        spawned_env: dict[str, str] = {}
        backend = _fake_backend()

        def classify(name: str, cmd: str, argv: Any, *, env: Any, work_dir: Any) -> bool:
            classifier_env.update(env)
            return name == "kirocrew-cron"

        async def fake_spawn(**kwargs: Any) -> Any:
            spawned_env.update(kwargs["env"])
            return backend

        monkeypatch.setattr(gw, "_spawns_own_control_plane", classify)
        monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
        monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (dict(env), []))
        key = PoolKey.from_register(_register_frame(server_name=server_name))
        resolved_env = {"PATH": "/usr/bin"}
        got, _ = await gw._acquire_backend(
            BackendPool(max_backends=4),
            key,
            lambda k: (command, list(args), dict(resolved_env), k.work_dir),
        )

        assert got is backend
        assert not [k for k in classifier_env if k.upper().startswith("PYTHON")]
        for env_key, value in _UTF8_PROCESS_ENV.items():
            assert spawned_env[env_key] == value
        assert backend.control_plane is (server_name == "kirocrew-cron")

    def test_utf8_pinning_keeps_the_real_fence_on_our_side(self, managed: dict[str, Any]) -> None:
        """The pair the spawn site adds is exactly what the real classifier
        would deny, which is why it is added only after the verdict."""
        from kiro_crew.platform_compat import _UTF8_PROCESS_ENV

        assert gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env={}
        )
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env=dict(_UTF8_PROCESS_ENV)
        )


class TestModuleFormShadowing:
    """A managed ``<python> -m kiro_crew <sub>`` still needs a token fence.

    The fixed CWD or launcher root must not shadow this process's package.
    The complete Python environment namespace denies the token without
    modelling interpreter flags, path entries, archive loaders, or future
    import semantics.
    """

    SUB = "mcp-cron"

    @pytest.fixture
    def module_entry(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        entry = {"command": sys.executable, "args": ["-s", "-m", "kiro_crew", self.SUB]}
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name: dict(entry) if name == "kirocrew-cron" else None,
        )
        return entry

    def _ours(self, entry: dict[str, Any], **kw: Any) -> bool:
        return gw._spawns_own_control_plane("kirocrew-cron", entry["command"], entry["args"], **kw)

    def test_a_clean_project_cwd_is_ours(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        (tmp_path / "README.md").write_text("not a package")
        assert self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_a_project_carrying_its_own_kiro_crew_package_is_not(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    # The extension suffix is the platform's own (``.so`` / ``.pyd``): the
    # classifier asks the import system for its suffixes, so the test must too.
    @pytest.mark.parametrize("suffix", [".py", ".pyc", importlib.machinery.EXTENSION_SUFFIXES[-1]])
    def test_a_project_carrying_a_kiro_crew_module_file_is_not(
        self, module_entry: dict[str, Any], tmp_path: Path, suffix: str
    ) -> None:
        (tmp_path / f"kiro_crew{suffix}").write_bytes(b"")
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_a_namespace_directory_does_not_shadow(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        """No ``__init__.py``: the import system scans past it to the real package."""
        (tmp_path / "kiro_crew").mkdir()
        assert self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_our_own_checkout_as_cwd_is_ours(self, module_entry: dict[str, Any]) -> None:
        """A dev gateway running from ``src/`` with ``src/`` as the project CWD
        loads the very package this process runs -- that is not a shadow."""
        import kiro_crew

        src = Path(kiro_crew.__file__).resolve().parent.parent
        assert self._ours(module_entry, env={}, work_dir=src)

    def test_pythonpath_can_shadow_too(self, module_entry: dict[str, Any], tmp_path: Path) -> None:
        clean = tmp_path / "project"
        clean.mkdir()
        foreign = tmp_path / "lib"
        (foreign / "kiro_crew").mkdir(parents=True)
        (foreign / "kiro_crew" / "__init__.py").write_text("")
        assert self._ours(module_entry, env={}, work_dir=clean)
        assert not self._ours(module_entry, env={"PYTHONPATH": str(foreign)}, work_dir=clean)
        # Relative values are non-empty too; no child-root model is consulted.
        assert not self._ours(module_entry, env={"PYTHONPATH": "lib"}, work_dir=tmp_path)
        # An empty variable adds no root and remains allowed.
        assert self._ours(module_entry, env={"PYTHONPATH": ""}, work_dir=clean)

    def test_any_non_empty_pythonpath_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        clean = tmp_path / "clean"
        clean.mkdir()
        assert not self._ours(module_entry, env={"PYTHONPATH": str(clean)}, work_dir=clean)

    def test_a_zip_pythonpath_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        """A zipimport package bypassed the old directory-and-suffix scan."""
        archive = tmp_path / "foreign.egg"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("kiro_crew/__init__.py", "")
        assert not self._ours(module_entry, env={"PYTHONPATH": str(archive)}, work_dir=tmp_path)

    def test_any_non_empty_pythonhome_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        home = tmp_path / "python-home"
        home.mkdir()
        assert not self._ours(module_entry, env={"PYTHONHOME": str(home)}, work_dir=tmp_path)
        assert self._ours(module_entry, env={"PYTHONHOME": ""}, work_dir=tmp_path)

    def test_pythonuserbase_denies_and_names_the_variable(
        self,
        module_entry: dict[str, Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not self._ours(
                module_entry,
                env={"PYTHONUSERBASE": str(tmp_path / "foreign-user-site")},
                work_dir=tmp_path,
            )

        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        assert "child environment carries non-empty PYTHONUSERBASE" in record.message

    @pytest.mark.parametrize(
        "key",
        ["PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHON_FUTURE_IMPORT_ROOT"],
    )
    def test_python_namespace_additions_fail_closed(
        self, module_entry: dict[str, Any], tmp_path: Path, key: str
    ) -> None:
        assert not self._ours(module_entry, env={key: "foreign"}, work_dir=tmp_path)
        assert self._ours(module_entry, env={key: ""}, work_dir=tmp_path)

    def test_a_foreign_package_in_per_user_site_denies_the_token(
        self,
        module_entry: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Per-user site-packages needs no variable to be on the child's path.

        The interpreter adds it from a default location ahead of the install's
        own site-packages, so removing ``PYTHONUSERBASE`` relocates it rather
        than disabling it and ``PYTHONSAFEPATH`` does not cover it either.
        """
        user_site = tmp_path / "user-site"
        (user_site / "kiro_crew").mkdir(parents=True)
        (user_site / "kiro_crew" / "__init__.py").write_text("")
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))

        project = tmp_path / "project"
        project.mkdir()
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not self._ours(module_entry, env={}, work_dir=project)

        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        # The reason quotes the root with ``!r``, which doubles the backslashes
        # of a Windows path, so compare against the repr rather than the str.
        assert repr(str(user_site)) in record.message

    def test_a_user_install_keeps_its_token(
        self, module_entry: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``--user`` install holds the real package in user-site, not a foreign one.

        Disabling user-site would break that shape outright, so the check must
        compare the package it finds rather than refuse the root's existence.
        """
        ours = Path(gw.kiro_crew.__file__).resolve().parent
        user_site = tmp_path / "user-site"
        user_site.mkdir()
        (user_site / "kiro_crew").symlink_to(ours, target_is_directory=True)
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))

        project = tmp_path / "project"
        project.mkdir()
        assert self._ours(module_entry, env={}, work_dir=project)

    def test_an_unresolvable_user_site_fails_closed(
        self, module_entry: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("no user base")

        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", _boom)
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_user_site_is_switched_off_when_nothing_there_is_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``.pth`` entry runs code at startup under any filename.

        Inspecting user-site for a foreign package cannot see a startup hook, so
        the surface is removed instead when nothing legitimate occupies it.
        """
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(tmp_path / "elsewhere"))
        assert gw._user_site_holds_our_package() is False

    def test_user_site_stays_on_for_a_user_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its own package lives there; switching user-site off would break it."""
        user_site = tmp_path / "user-site"
        user_site.mkdir()
        (user_site / "kiro_crew").symlink_to(
            Path(gw.kiro_crew.__file__).resolve().parent, target_is_directory=True
        )
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))
        assert gw._user_site_holds_our_package() is True

    def test_an_unresolvable_user_site_leaves_the_child_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unresolvable must not silently switch user-site off for the child."""

        def _boom() -> str:
            raise RuntimeError("no user base")

        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", _boom)
        assert gw._user_site_holds_our_package() is True

    def test_disabled_user_site_needs_no_further_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", False)
        assert gw._user_site_holds_our_package() is False

    def test_interpreter_flags_do_not_relax_the_token_fence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``-P``/``-I`` semantics may change; the token fence stays stricter."""
        entry = {"command": sys.executable, "args": ["-P", "-s", "-m", "kiro_crew", self.SUB]}
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name: dict(entry))
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")
        assert not self._ours(entry, env={}, work_dir=tmp_path)
        assert not self._ours(entry, env={"PYTHONPATH": str(tmp_path)}, work_dir=tmp_path)
        isolated = {"command": sys.executable, "args": ["-I", "-m", "kiro_crew", self.SUB]}
        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name: dict(isolated))
        assert not self._ours(isolated, env={"PYTHONPATH": str(tmp_path)}, work_dir=tmp_path)
