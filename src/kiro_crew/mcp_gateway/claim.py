"""Claim-push: the gateway tells gatewayd which session owns a runtime.

The claim event ("session S now owns kiro-cli PID P") is born in the main
gateway process at warm-pool ``rekey()`` time — with both the session key and
the runtime PID in hand. Before this module, that information reached gatewayd
only indirectly: the dashboard wrote ``session_pid_<pid>.txt`` and each stub
polled for it (``stub._recaller_loop``) with a bounded budget. Pool processes
claimed after the budget expired stayed identity-less for life (empty
``_meta.caller`` on every forwarded call → ``spawn_run`` records an empty
``parent_session`` → subagent completions fall back to notification-only).

Claim-push inverts the flow: the knower pushes. On ``rekey()`` the gateway
sends a one-shot ``claim`` frame over the gatewayd unix socket::

    {"type": "claim", "pid": <kiro_pid>, "caller": {"session_key": ..., ...}}

gatewayd applies it to every live stub connection whose ``parent_pid`` matches
(see ``gatewayd`` claim handling). Unlike the stub-initiated ``recaller``
frame — which is deny-by-default and must never overwrite an existing
identity — the claim frame originates from the gateway itself (same uid-gated
0700 socket trust level as Register), so it is allowed to REPLACE a stale
identity. That is what keeps the caller correct across warm-pool re-claims.

The stub-side recaller poll is kept as a fallback for gatewayd-restart races,
but claim-push is the primary, event-driven path.

Import-light on purpose: imported from ``acp/client.py`` and
``acp/session_provider.py`` (hot paths) and must not pull in config loading.
The only non-stdlib imports are ``platform_compat`` (process start tokens for
the PID-recycle guard), ``executors`` (offloads the /proc token read from the
event loop), and ``mcp_gateway.transport`` — all already in the
``transport -> platform_compat -> executors`` chain, which reaches no config
loader; transport is needed because the endpoint is a unix socket on POSIX
and a named pipe on Windows, and this module must not know which.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections import OrderedDict
from typing import Any, Optional

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway import transport

logger = logging.getLogger(__name__)

#: Aggregate bound on the ENTIRE claim round-trip (connect + write + read-ack)
#: so a wedged gatewayd can never stall a claim task past this budget (the
#: task is fire-and-forget, but leaked tasks pile up).
_CLAIM_TIMEOUT_SECS = 5.0

#: Env variable carrying a stub's per-session token. Read by
#: ``mcp_gateway.stub`` and forwarded on the Register frame; see
#: :func:`mint_stub_session_token` for what it is for.
STUB_SESSION_TOKEN_ENV = "KIROCREW_STUB_SESSION_TOKEN"

#: Token entropy in bytes. 32 bytes = 256 bits, well above the 128-bit floor
#: an unguessable per-session name needs: a stub that could guess another
#: session's token would inherit its identity from gatewayd.
_TOKEN_BYTES = 32


def mint_stub_session_token() -> str:
    """Return a fresh per-session stub token.

    Every identity channel the stub path had before this token is keyed on the
    PROCESS TREE — the stub's own ``KIROCREW_SESSION_KEY``/pid-file walk,
    gatewayd's SO_PEERCRED ``/proc`` walk, and claim-push, which re-targets
    every connection under a runtime PID. One kiro-cli process hosts many ACP
    sessions (``agent.session_sharing``: a ``spawn_run`` subagent runs on its
    parent's process), so all three answer with the PARENT's session for a
    subagent's stub, and a parent re-claim overwrites whatever the subagent had.

    The token is the per-SESSION name that tree cannot supply: minted here,
    handed to exactly one ACP session's injected stub entries, and matched by
    gatewayd against the token a claim carries. It is a bearer name for a
    session's identity, so it must be unguessable — hence ``secrets`` — and it
    must never be logged, exported in ``stats()``, or written to the stub
    fallback journal.
    """
    return secrets.token_hex(_TOKEN_BYTES)


def classify_session_type(session_key: str) -> str:
    """Session-type label for a caller block. Mirrors ``stub._build_caller_block``."""
    if session_key.startswith("cron:"):
        return "cron"
    if session_key.startswith("hook:"):
        return "hook"
    if session_key.startswith("dashboard:"):
        return "dashboard"
    if session_key:
        return "slack-thread"
    return "unknown"


def build_claim_frame(
    pid: int,
    session_key: str,
    channel_id: Optional[str],
    stub_session_token: str = "",
) -> dict:
    """Assemble the one-shot claim frame sent to gatewayd.

    ``pid_start_id`` is the claimed runtime's process start token
    (``platform_compat.get_process_start_id``) — gatewayd compares it against
    the token it recorded at register time so a claim can never land on a
    connection whose PID was recycled to a different process. ``None`` means
    "identity unknown" (Windows, unreadable /proc) and is treated by gatewayd
    as a match, preserving legacy behavior.

    ``stub_session_token`` names ONE of the ACP sessions the runtime hosts
    (:func:`mint_stub_session_token`), so gatewayd re-targets only that
    session's stub connections instead of every connection under the PID. Empty
    omits the field and keeps the PID-wide behavior, which is what a runtime
    whose sessions carry no token still needs.
    """
    frame = {
        "type": "claim",
        "pid": pid,
        "pid_start_id": platform_compat.get_process_start_id(pid),
        "caller": {
            "session_key": session_key,
            "session_type": classify_session_type(session_key),
            "principal_id": os.environ.get("USER") or os.environ.get("USERNAME") or "",
            "channel_id": channel_id or "",
        },
    }
    if stub_session_token:
        frame["stub_session_token"] = stub_session_token
    return frame


async def _send_claim_inner(
    socket_path: str,
    pid: int,
    session_key: str,
    channel_id: Optional[str],
    stub_session_token: str = "",
) -> bool:
    """Unbounded socket round-trip; ``send_claim`` enforces the time budget."""
    # build_claim_frame resolves the runtime's start token from /proc; a
    # /proc read can wedge on a D-state target, so keep it off the event
    # loop (a leaked worker thread on a wedge is survivable; a frozen loop
    # is not). send_claim's aggregate wait_for still bounds this await.
    frame = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        build_claim_frame,
        pid,
        session_key,
        channel_id,
        stub_session_token,
    )
    reader, writer = await transport.connect(socket_path)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode("utf-8")) if raw else {}
        kind = resp.get("type") if isinstance(resp, dict) else None
        if kind == "claimed":
            logger.info(
                "claim-push acknowledged: pid=%d session_key=%s updated=%s",
                pid,
                session_key,
                resp.get("updated"),
            )
            return True
        if kind == "claim-noop":
            # gatewayd accepted the claim and recorded the session token
            # binding, but no stub connection is indexed under the pid yet.
            # That is the normal ordering (the claim is pushed before the
            # session's stubs launch, and the register that follows resolves
            # through the kept binding), so it is not a fault worth a WARNING.
            # A wedged pid index answers the same way; the two differ in
            # aggregate (every claim on the host answering noop, none ever
            # "claimed"), which is why the line is kept at INFO. Nothing was
            # retargeted, so the return value stays False.
            logger.info(
                "claim-push pending: pid=%d session_key=%s matched no stub "
                "connection yet; gatewayd keeps the session token binding for "
                "the stubs that register next",
                pid,
                session_key,
            )
            return False
        logger.warning(
            "claim-push not acknowledged: pid=%d session_key=%s resp=%r",
            pid,
            session_key,
            resp,
        )
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def send_claim(
    socket_path: str,
    pid: int,
    session_key: str,
    channel_id: Optional[str] = None,
    stub_session_token: str = "",
) -> bool:
    """Send one claim frame to gatewayd. Returns True when acknowledged.

    gatewayd answers one of three ways. ``claimed`` means live stub connections
    were retargeted. ``claim-noop`` means the claim was accepted and the session
    token binding recorded, but no stub connection is indexed under the pid yet
    (the claim is pushed before the session's stubs register, so this is the
    usual first answer); it returns False because nothing was retargeted, but
    logs at INFO because it is the expected ordering, not a fault.
    ``claim-rejected`` (a malformed frame), an empty reply or an unrecognised
    one return False and log at WARNING.

    The whole round-trip (connect + write + read-ack) runs under a single
    aggregate ``asyncio.wait_for`` bound of ``_CLAIM_TIMEOUT_SECS`` — a wedged
    gatewayd that accepts connections but stalls on responding can never hold
    a claim task past the budget.

    Best-effort by design: a missing socket, dead gatewayd, or timeout logs
    at warning and returns False — the stub-side recaller poll remains as the
    fallback repair path, and a claim retry happens naturally on the next
    rekey.
    """
    try:
        return await asyncio.wait_for(
            _send_claim_inner(socket_path, pid, session_key, channel_id, stub_session_token),
            timeout=_CLAIM_TIMEOUT_SECS,
        )
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            "claim-push failed (recaller fallback still applies): pid=%d " "session_key=%s: %s",
            pid,
            session_key,
            exc,
        )
        return False


def schedule_claim(
    socket_path: Optional[str],
    pid: Optional[int],
    session_key: str,
    channel_id: Optional[str] = None,
    stub_session_token: str = "",
) -> None:
    """Fire-and-forget claim push. Safe to call from sync code (``rekey()``).

    No-ops quietly when preconditions are missing (no gateway socket
    configured, unknown pid, empty session key, or no running event loop) —
    every one of those cases is a legitimate non-gateway or test context
    where claim-push simply does not apply.
    """
    if (
        not socket_path
        or not isinstance(socket_path, str)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not session_key
    ):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _claim_and_rebind(socket_path, pid, session_key, channel_id, stub_session_token)
    )
    # Retain a reference so the task is never GC'd mid-flight; discard on done.
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set["asyncio.Task[bool]"] = set()


async def _claim_and_rebind(
    socket_path: str,
    pid: int,
    session_key: str,
    channel_id: Optional[str],
    stub_session_token: str,
) -> bool:
    """Send the claim, then the token's agent binding again when one was sent before.

    gatewayd keeps both in memory only, so the claim a turn re-pushes
    (``reclaim``) is also what restores the agents after a daemon respawn.
    """
    claimed = await send_claim(socket_path, pid, session_key, channel_id, stub_session_token)
    entry = _BOUND_AGENTS.get(stub_session_token) if stub_session_token else None
    if entry:
        _bound_pid, start_id, agents = entry
        await send_agent_binding(
            socket_path, pid, stub_session_token, agents, pid_start_id=start_id
        )
    return claimed


#: Cap on remembered token -> binding entries, ONE named bound shared with the
#: daemon's own table (``daemon.identity._MAX_TOKEN_BINDINGS`` imports it). It is
#: a BACKSTOP, not an LRU: a live session's binding is never evicted by age (that
#: would lose its declared env after a daemon restart, with no path back but
#: recreating the session). An entry is dropped when its session ends
#: (:func:`forget_agent_binding`), and over the cap only entries whose runtime
#: process is gone are pruned (:func:`_evict_dead_bindings_over_cap`); a live one
#: is kept even past the cap.
MAX_SESSION_BINDINGS = 512

#: Sentinel for ``send_agent_binding``'s ``pid_start_id`` so ``None`` (a genuine
#: "unknown token", which never denies a resolution) is told from "not supplied,
#: read it fresh".
_UNSET_START_ID = "\x00unset"

#: ``stub_session_token`` -> (runtime pid, that pid's start token, the agents
#: last bound to it), most recent last, so :func:`_claim_and_rebind` and
#: :func:`replay_agent_bindings` can send the binding again after a gatewayd
#: restart with the ORIGINAL process generation, never a recycled pid's.
_BOUND_AGENTS: "OrderedDict[str, tuple[int, Optional[str], frozenset[str]]]" = OrderedDict()


def forget_agent_binding(stub_session_token: str) -> None:
    """Drop a session's remembered agent binding when its session ends.

    Called from the session teardown that retires the token, so the cache holds
    only live sessions and a long-lived one is never pushed out by newer ones.
    """
    if stub_session_token:
        _BOUND_AGENTS.pop(stub_session_token, None)


def _evict_dead_bindings_over_cap() -> None:
    """Keep :data:`_BOUND_AGENTS` bounded without ever evicting a live session.

    Past the backstop cap, drop only entries whose runtime process is gone -- a
    session that ended without its :func:`forget_agent_binding` hook (a crash, a
    missed teardown). A live session's binding stays, even past the cap, because
    losing it would strand its private backends with no declared env after a
    daemon restart. If every entry over the cap is still live, they are kept and
    the overflow is logged once rather than evicting one of them.
    """
    if len(_BOUND_AGENTS) <= MAX_SESSION_BINDINGS:
        return
    for token, (pid, _start, _agents) in list(_BOUND_AGENTS.items()):
        if len(_BOUND_AGENTS) <= MAX_SESSION_BINDINGS:
            return
        if not platform_compat.pid_exists(pid):
            _BOUND_AGENTS.pop(token, None)
    if len(_BOUND_AGENTS) > MAX_SESSION_BINDINGS:
        logger.warning(
            "agent-binding cache holds %d live sessions, over the %d backstop; "
            "not evicting a live session",
            len(_BOUND_AGENTS),
            MAX_SESSION_BINDINGS,
        )


#: How deep :func:`stub_entry_agents` looks: the ``session/new`` params, a
#: ``mcpServers`` array or map, an entry, and the ``_meta.kiro.customAgents``
#: descriptors one KAS session carries its own servers in.
_STUB_SCAN_DEPTH = 8


def stub_entry_agents(params: Any) -> frozenset[str]:
    """The ``--agent`` of every gateway stub entry anywhere in *params*.

    A gateway stub is identified the way the rewriter marks one: a kiro-agent-JSON
    entry carries the wrapper marker, and an ACP array entry (whose marker the
    shaping drops) runs the stub module. Only such an entry's ``--agent`` is read,
    from the ``--server`` / ``--agent`` flags the rewriter packs into the
    ``--stub-flags-b64`` envelope. An unrelated agent-spec server that happens to
    pass ``--server`` / ``--agent`` CLI flags of its own is not a stub and binds no
    agent. Read from the ``session/new`` / ``session/load`` params the gateway
    itself built, so the answer is the agents whose stubs this session is about to
    start.
    """
    # Local: every Kiro Crew MCP server imports this module, and only the gateway
    # process calls this function.
    from kiro_crew.mcp_gateway import STUB_MODULE
    from kiro_crew.mcp_gateway.hashing import expand_stub_flags
    from kiro_crew.mcp_gateway.rewriter_launch import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY

    found: set[str] = set()

    def _walk(node: Any, depth: int) -> None:
        if depth > _STUB_SCAN_DEPTH:
            return
        if isinstance(node, dict):
            raw_args = node.get("args")
            # The rewriter's own test for a stub: the wrapper marker (kiro-agent
            # JSON), or args that run the stub module (an ACP array element, whose
            # marker the shaping dropped). Without this, any server passing
            # ``--server``/``--agent`` of its own would be read as a stub binding.
            marked = node.get(_WRAPPER_MARKER) is True or node.get(_WRAPPER_MARKER_LEGACY) is True
            runs_stub = isinstance(raw_args, list) and any(
                isinstance(a, str) and STUB_MODULE in a for a in raw_args
            )
            if marked or runs_stub:
                args = raw_args if isinstance(raw_args, list) else []
                try:
                    # A rewritten stub carries its flags in one envelope token.
                    args = expand_stub_flags(args)
                except ValueError:
                    args = []
                if "--server" in args:
                    for flag, value in zip(args, args[1:]):
                        if flag == "--agent" and isinstance(value, str) and value:
                            found.add(value)
            for value in node.values():
                _walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                _walk(value, depth + 1)

    _walk(params, 0)
    return frozenset(found)


def build_agent_binding_frame(
    pid: int, stub_session_token: str, agents: frozenset[str], pid_start_id: Optional[str]
) -> dict:
    """The ``agent-binding`` frame: these *agents*' stubs run under this token in *pid*.

    *pid_start_id* is the runtime's process-start token, captured when the
    binding was first sent and carried unchanged through a replay, so a daemon
    that restarted re-authorizes the ORIGINAL process and not a different one
    that recycled its pid.
    """
    return {
        "type": "agent-binding",
        "pid": pid,
        "pid_start_id": pid_start_id,
        "stub_session_token": stub_session_token,
        "agents": sorted(agents),
    }


async def _send_agent_binding_inner(
    socket_path: str,
    pid: int,
    stub_session_token: str,
    agents: frozenset[str],
    pid_start_id: Optional[str],
) -> bool:
    frame = build_agent_binding_frame(pid, stub_session_token, agents, pid_start_id)
    reader, writer = await transport.connect(socket_path)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(resp, dict) and resp.get("type") == "agent-bound":
            return True
        logger.warning("agent binding not acknowledged: pid=%d resp=%r", pid, resp)
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def send_agent_binding(
    socket_path: Optional[str],
    pid: Optional[int],
    stub_session_token: str,
    agents: frozenset[str],
    pid_start_id: Optional[str] = _UNSET_START_ID,
) -> bool:
    """Tell gatewayd which agents' stubs run under *stub_session_token*.

    Sent before the ``session/new`` that starts those stubs, and awaited, so a
    stub registering under the token is served its agent's declared env.
    gatewayd takes it only from its owner gateway. Best-effort under the same
    time budget as a claim: a missing or wedged daemon returns False, and the
    stubs are then served with no declaring agent until the next claim of the
    session sends it again.

    *pid_start_id* is the runtime's process-start token. Left at the sentinel,
    it is read fresh here and cached with the binding; a replay passes the
    cached value back so a recycled pid is never re-authorized as the original
    runtime.
    """
    if (
        not socket_path
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not stub_session_token
        or not agents
    ):
        return False
    if pid_start_id is _UNSET_START_ID:
        # Read the runtime's start token once, off the loop (a /proc read can
        # wedge on a D-state target), and cache it so a replay reuses it.
        try:
            pid_start_id = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), platform_compat.get_process_start_id, pid
            )
        except Exception:  # graceful: an unknown token never denies a resolution
            pid_start_id = None
    _BOUND_AGENTS.pop(stub_session_token, None)
    _BOUND_AGENTS[stub_session_token] = (pid, pid_start_id, agents)
    _evict_dead_bindings_over_cap()
    try:
        return await asyncio.wait_for(
            _send_agent_binding_inner(socket_path, pid, stub_session_token, agents, pid_start_id),
            timeout=_CLAIM_TIMEOUT_SECS,
        )
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("agent binding failed: pid=%d: %s", pid, exc)
        return False


async def replay_agent_bindings(socket_path: Optional[str]) -> int:
    """Re-send every remembered agent binding to the daemon on *socket_path*.

    The gateway calls this the moment it (re)connects to a daemon it owns, right
    after it announces the owner port: a restarted daemon holds no bindings
    (they live only in its memory), and a stub that reconnects before the
    session's next turn would otherwise have its private backend started with no
    declared env. Re-sending every live binding at once closes that window
    without waiting for the next turn's ``reclaim``. The runtime pids are the
    stubs' own, unchanged by a gatewayd restart. Returns how many were sent.
    """
    if not socket_path:
        return 0
    sent = 0
    for token, (pid, start_id, agents) in list(_BOUND_AGENTS.items()):
        if await send_agent_binding(socket_path, pid, token, agents, pid_start_id=start_id):
            sent += 1
    return sent
