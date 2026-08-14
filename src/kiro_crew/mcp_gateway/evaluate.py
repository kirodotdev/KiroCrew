"""Run the pre-flight for servers whose verdict is missing or stale, and cache it.

This is the orchestration the layers below deliberately do not do: ``preflight``
knows how to provoke one server, ``verdict_cache`` knows how to remember, and
``shareability`` knows how to judge. This module decides WHICH servers are worth
paying for, which is a policy question and belongs in one place.

The policy: evaluate only what changed. A server whose execution identity
already has a cached verdict is skipped, so the steady-state cost of the whole
feature is a file read. A newly installed or upgraded MCP costs two spawns,
once.

Never called while rendering anything. The pre-flight spawns processes, so this
runs from the explicit probe action the operator already triggers — the same
action that already spawns every configured server.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew.mcp_discovery import PROBE_MAX_CONCURRENCY
from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env
from kiro_crew.mcp_gateway.preflight import preflight
from kiro_crew.mcp_gateway.stub import binary_fingerprint
from kiro_crew.mcp_gateway.verdict_cache import (
    CachedPreflight,
    Identity,
    VerdictCache,
    load_cache,
    now,
)

logger = logging.getLogger(__name__)

#: How many servers one pass will provoke. Each costs TWO spawns, so this is the
#: real process budget for a single probe: two servers, four short-lived
#: processes. Deliberately small — a machine that just had twenty MCPs added
#: covers them over ten probes rather than paying forty spawns inside one
#: request, and until a server is measured it reads as ``unknown``, which is the
#: honest answer rather than a delay.
MAX_EVALUATIONS_PER_PASS = 2

#: Fan-out ceiling for the pre-flights in one pass, taken from the prober rather
#: than chosen here: both spawn subprocesses and resolve names on the loop's
#: default executor, so two independent caps would let this pass flood the pool
#: the prober's own bound exists to protect.
_PROBE_FAN_OUT = PROBE_MAX_CONCURRENCY


def _assert_off_loop(what: str) -> None:
    """Raise if called from the event loop thread.

    The blocking work below is reached from a request handler, so an
    accidentally synchronous call does not fail — it stalls every chat sharing
    that loop for as long as the disk takes, which is invisible in tests and
    surfaces only as a starvation failure under load. Raising converts that into
    an immediate, attributable error.

    Inside ``asyncio.to_thread`` there is no running loop in the worker thread,
    so the correct call path passes, as does any synchronous caller.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(f"{what} does blocking IO and must not run on the event loop")


def identity_for(server: Any) -> Identity:
    """Execution identity of *server*, using the hashes ``PoolKey`` is built from.

    Stored inside the server's row rather than used as its key, so one server
    keeps one row and a changed identity reads as "not measured" instead of
    adding a second row.

    Env is hashed by the same helper the pool uses, so the rotating-secret
    exclusions apply identically — a credential rotation must not look like a
    different server here either.

    ``binary_version`` fingerprints everything the launch actually runs, not just
    the executable. Most MCP servers are launched THROUGH an interpreter —
    ``python server.py``, ``node server.js`` — so fingerprinting ``command``
    alone identifies the interpreter, which does not change when the server's own
    code is edited in place. The argv entries that resolve to real files are
    fingerprinted too, so editing the script invalidates the verdict the way
    replacing a compiled binary does. Without that, the most common shape of MCP
    server keeps a stale measurement for ever: a server that BECAME
    caller-sensitive would ship its first caller's ``initialize`` result to
    everyone else, which is the outcome this identity exists to prevent.

    BLOCKING IO: resolving the binary walks ``PATH``, and each fingerprint stats
    and hashes bounded content. Call it from a worker thread.
    """
    _assert_off_loop("identity_for")
    args = list(server.args or [])
    env = getattr(server, "env", None)
    return Identity(
        command_args_hash=hash_command(server.command, args),
        env_hash=hash_effective_env(
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        ),
        binary_version=_launch_fingerprint(server.command, args),
    )


def _launch_fingerprint(command: str, args: list[str]) -> str:
    """Fingerprint of the command plus every argv entry that names a real file.

    Non-file arguments (flags, ports, module names) contribute nothing here —
    they are already covered by ``hash_command``, which hashes argv verbatim.
    What this adds is the CONTENT of the files argv points at, which argv itself
    cannot express.

    An argument that does not resolve to a readable file is skipped rather than
    treated as empty, so a flag value that merely looks like a path does not make
    the key unstable between passes.
    """
    parts = [binary_fingerprint(command)]
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        try:
            if not Path(arg).is_file():
                continue
        except OSError:
            continue
        parts.append(binary_fingerprint(arg))
    return "\u0000".join(parts)


def _load_and_identify(
    servers: list[Any], runtime_dir: Path
) -> tuple[VerdictCache, dict[str, Identity]]:
    """Read the cache and derive every execution identity in one worker thread.

    These are one step, not two: the identity is what a stored row is validated
    against, and deriving it is the more expensive half — a ``PATH`` walk plus a
    bounded content hash per server, against a single file read. Splitting them
    across the loop boundary is what left the costlier half on the loop.
    """
    return load_cache(runtime_dir), {s.name: identity_for(s) for s in servers}


async def evaluate_new_servers(servers: list[Any], runtime_dir: Path) -> dict[str, CachedPreflight]:
    """Pre-flight the servers with no current measurement; return every known one.

    *servers* are ``McpServerInfo`` objects. Returns name -> verdict for every
    server that has one, stored or freshly derived, so a caller can render
    without a second lookup.

    Nothing is deleted here. One server owns one row, so a row is replaced by its
    own server's next measurement and by nothing else — there is no inventory to
    compare against and no size to bound. That matters because the only caller's
    list comes from ``probe_all``, which excludes consent-disabled rows by design:
    any rule that deleted "servers not in this list" would discard the valid
    measurement of every disabled server.

    Every filesystem touch here is offloaded: this runs inside a request handler
    on the gateway's event loop, and a slow disk would otherwise stall every chat
    sharing that loop, not just this probe.
    """
    cache, identities = await asyncio.to_thread(_load_and_identify, servers, runtime_dir)

    known: dict[str, CachedPreflight] = {}
    due: list[Any] = []
    budget = MAX_EVALUATIONS_PER_PASS
    for server in servers:
        hit = cache.get(server.name, identities[server.name])
        if hit is not None:
            known[server.name] = hit
            continue
        if getattr(server, "disabled", False) or not getattr(server, "command", ""):
            # A disabled server must not be spawned (probing is the act consent
            # gates), and a server with no command has no stdio pipe to stub.
            continue
        if budget <= 0:
            continue
        budget -= 1
        due.append(server)

    # Bounded fan-out, not a sequential walk. This is awaited by a request the
    # operator is waiting on, and each pre-flight is two spawns that can each hit
    # the probe timeout — serially that is the budget times two timeouts of dead
    # wait, so one hung server used to make the whole pass feel hung. The cap is
    # the prober's own, because the same executor and the same DNS resolution sit
    # underneath: raising it here would flood the pool this bound exists to
    # protect. Each pre-flight already spawns twice, so the real process ceiling
    # is twice this number.
    sem = asyncio.Semaphore(_PROBE_FAN_OUT)

    async def _measure(server: Any) -> tuple[Any, Any]:
        async with sem:
            return server, await preflight(server)

    for server, result in await asyncio.gather(*(_measure(s) for s in due)):
        verdict = CachedPreflight(
            ran=result.ran,
            caller_sensitive=result.caller_sensitive,
            reasons=result.reasons,
            evaluated_at=now(),
        )
        if result.ran:
            cache.put(server.name, identities[server.name], verdict)
        else:
            # A pre-flight that could not run says nothing about the server, only
            # about the moment: a missing credential, an unreachable tunnel, a
            # binary mid-install. Caching that keys the failure to an execution
            # identity that has not changed, so the server would never be
            # re-evaluated once the condition clears. Report it for this pass and
            # pay the spawn again next time.
            logger.info("shareability: %s could not be evaluated yet", server.name)
        known[server.name] = verdict
        logger.info(
            "shareability: evaluated %s -> ran=%s caller_sensitive=%s",
            server.name, result.ran, result.caller_sensitive,
        )

    await asyncio.to_thread(cache.flush)
    return known
