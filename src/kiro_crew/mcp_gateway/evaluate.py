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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.mcp_discovery import PROBE_MAX_CONCURRENCY
from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env
from kiro_crew.mcp_gateway.preflight import PreflightResult, preflight
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


#: Serializes one evaluator pass end to end, from the cache read to the flush.
#:
#: The store is a single JSON object rewritten whole, so a pass is a
#: read-modify-write. Two overlapping passes each load the file, measure, and
#: flush their own in-memory copy: whichever flushes LAST silently drops every row
#: the other wrote, reverting freshly measured servers to "not measured".
#:
#: That is reachable rather than theoretical: the operator-initiated pass runs for
#: minutes, and the dashboard's own probe path calls this evaluator too, so a probe
#: firing while a measurement pass is in flight is the normal case rather than the
#: unlucky one. One lock per process is enough because the file has a single writer
#: — this evaluator — and every caller reaches it through here.
_PASS_LOCK = LoopBoundLock()


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
    exclusions apply — a credential rotation must not look like a different
    server here either.

    It deliberately does NOT pass ``identity_keys``, so a variable an operator
    named in ``mcp_gateway.pool_identity_env`` still does not enter THIS hash.
    The pool needs the value in its key because the value decides which backend
    is the right backend; a measurement is a claim about the server's BEHAVIOUR,
    and rotating a credential does not change that. Folding it in here would
    discard a good measurement on every rotation, which is the stale-verdict
    failure this identity exists to prevent, in reverse. So the two hashes agree
    on the default set and diverge only on named keys, on purpose.

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


def reported_version(server: Any) -> str:
    """The version the server called itself in its handshake, or ``""``.

    Free because the probe already stores the whole ``serverInfo``. Empty means
    the question is unanswered — not probed this pass, or a server that reports
    no version — and ``VerdictCache.get`` treats it as no information rather than
    as a value that differs.
    """
    info = getattr(server, "server_info", None)
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) else ""


async def evaluate_new_servers(
    servers: list[Any],
    runtime_dir: Path,
    *,
    budget: int | None = MAX_EVALUATIONS_PER_PASS,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, CachedPreflight]:
    """Pre-flight the servers with no current measurement; return every known one.

    *servers* are ``McpServerInfo`` objects. Returns name -> verdict for every
    server that has one, stored or freshly derived, so a caller can render
    without a second lookup.

    *budget* caps how many servers are measured in this pass. ``None`` lifts the
    cap, which is for an operator who explicitly asked to measure everything and
    is watching progress — never for a request that renders a page, where an
    uncapped pass would spawn two processes per configured server while somebody
    waits. *on_progress* is called with ``(measured, attempted, total)`` after
    each measurement so that caller can report where it is; it runs on the event
    loop, so it must not block.

    ``measured`` and ``attempted`` are separate because they answer different
    questions and routinely disagree. A server whose pre-flight could not run --
    a missing credential, an unreachable tunnel, a host where the probe cannot
    spawn at all -- was attempted and produced no verdict, so it is still
    unmeasured afterwards. Reporting one number for both is what let a pass that
    measured nothing close with "Measured 30 servers" beside a table still
    showing thirty unmeasured rows. ``attempted`` is what a progress bar has to
    advance on, or it would sit at zero for the whole pass; ``measured`` is what
    a claim about the outcome has to be built from.

    Nothing is deleted here. One server owns one row, so a row is replaced by its
    own server's next measurement and by nothing else — there is no inventory to
    compare against and no size to bound. That matters because the only caller's
    list comes from ``probe_all``, which excludes consent-disabled rows by design:
    any rule that deleted "servers not in this list" would discard the valid
    measurement of every disabled server.

    Every filesystem touch here is offloaded: this runs inside a request handler
    on the gateway's event loop, and a slow disk would otherwise stall every chat
    sharing that loop, not just this probe.

    One pass runs at a time, and a BUDGETED pass does not queue behind an uncapped
    one. An uncapped operator pass runs for minutes; the budgeted caller is a
    request somebody is waiting on, and waiting for the lock would hold its
    already-computed response for the whole pass. A budgeted call therefore takes
    the lock only if it is free and otherwise returns what is already stored.
    Nothing is lost by yielding: the caller's own cap means it was only ever going
    to measure a couple of servers, "not measured yet" is a state it already
    renders, and the uncapped pass holding the lock is measuring the same servers
    anyway.
    """
    if budget is not None and _PASS_LOCK.locked():
        logger.debug("shareability: a pass is already running; serving stored rows")
        return await asyncio.to_thread(_stored_verdicts, servers, runtime_dir)
    async with _PASS_LOCK:
        return await _evaluate_pass(servers, runtime_dir, budget, on_progress)


def _stored_verdicts(servers: list[Any], runtime_dir: Path) -> dict[str, CachedPreflight]:
    """Every verdict already on disk for *servers*, measuring nothing.

    Blocking IO, so it is called through a thread. Reads by name rather than by
    identity because this path deliberately spends nothing: resolving an identity
    means fingerprinting a binary per server, which is the cost the caller is
    yielding to avoid. A row whose identity has moved on is corrected by the pass
    that is already running.
    """
    cache = load_cache(runtime_dir)
    known: dict[str, CachedPreflight] = {}
    for server in servers:
        row = cache.get_by_name(server.name)
        if row is not None:
            known[server.name] = row
    return known


async def _evaluate_pass(
    servers: list[Any],
    runtime_dir: Path,
    budget: int | None,
    on_progress: Callable[[int, int, int], None] | None,
) -> dict[str, CachedPreflight]:
    """One pass, holding ``_PASS_LOCK``. Split out so the lock has one owner."""
    cache, identities = await asyncio.to_thread(_load_and_identify, servers, runtime_dir)

    known: dict[str, CachedPreflight] = {}
    candidates: list[tuple[bool, Any]] = []
    for server in servers:
        hit = cache.get(server.name, identities[server.name], reported_version(server))
        if hit is not None:
            known[server.name] = hit
            # Stored is not the same as settled. A divergent row is kept so the
            # page can SHOW it -- the dashboard builds its rows from this cache
            # and nothing else, so a result that is not stored is not merely
            # forgotten, it is reported as "never measured" about a server we
            # just spawned twice -- but it must not suppress a re-measure, being
            # the one verdict two spawns cannot justify freezing (#4339). So it
            # stays a candidate and the next pass re-derives it, which is what
            # lets a press clear a row that was wrong.
            if not hit.caller_sensitive:
                continue
        if getattr(server, "disabled", False) or not getattr(server, "command", ""):
            # A disabled server must not be spawned (probing is the act consent
            # gates), and a server with no command has no stdio pipe to stub.
            continue
        candidates.append((server.name in known, server))

    # Never-measured servers are admitted FIRST, and the budget is applied after
    # that ordering rather than while walking the config. A divergent row is
    # re-measured every pass and so competes for the same budget for ever; in
    # config order a large enough divergent set would take every slot and a server
    # nobody has ever probed would sit behind it indefinitely -- and it would never
    # even enter this list, because the budget was already spent. Sorting first is
    # what makes the bound fair instead of positional.
    candidates.sort(key=lambda pair: pair[0])
    due: list[Any] = [s for _, s in candidates] if budget is None else [
        s for _, s in candidates[:budget]
    ]

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
            try:
                return server, await preflight(server)
            except Exception:
                # One server's payload must never end the pass. Every facet the
                # pre-flight compares is JSON this server chose, projected by code
                # that walks it — so the ways a hostile or broken server can make
                # that projection raise are not enumerable from here (a name that
                # is not a string, nesting deep enough to exhaust the stack, and
                # whatever the next one turns out to be). The flush happens after
                # this loop, so an escaping exception discards every verdict the
                # pass has already paid two spawns each for.
                #
                # Failing to measure a server is a state the module already has an
                # honest answer for, so it takes that answer: unmeasurable, which
                # reads as ``unknown`` and is never evidence against the server.
                # ``CancelledError`` is a ``BaseException`` and so passes through —
                # shutdown is not a measurement outcome.
                logger.warning(
                    "shareability: %s could not be measured; treating as unmeasurable",
                    getattr(server, "name", "?"), exc_info=True,
                )
                return server, PreflightResult(ran=False, detail="preflight_error")

    total = len(due)
    completed = 0
    # Counted apart from ``completed`` on purpose. The two branches below differ
    # in exactly the way an operator cares about: a measured server gets a row and
    # stops being unmeasured, while one that could not be reached deliberately
    # gets none and is still offered by the button afterwards. One counter for
    # both cannot express that, and the readout was built on the assumption that
    # it could.
    measured = 0
    # ``as_completed``, not ``gather``: gather resolves only once every pre-flight
    # has finished, so a progress hook driven from its result would report 0 of N
    # for the whole pass and then jump straight to N of N — useless for the one
    # caller that exists, an operator watching a minutes-long pass.
    for finished in asyncio.as_completed([_measure(s) for s in due]):
        server, result = await finished
        verdict = CachedPreflight(
            ran=result.ran,
            caller_sensitive=result.caller_sensitive,
            reasons=result.reasons,
            evaluated_at=now(),
            # Read from the server passed in, not from the pre-flight's throwaway
            # copies: this is the identification the dashboard is showing, and it
            # is what the next pass will compare against.
            reported_version=reported_version(server),
        )
        if result.ran:
            # Both outcomes are stored, including a divergence. Storing is what
            # makes a result VISIBLE (the dashboard reads this cache and only this
            # cache); what makes a divergence non-durable is that the loop above
            # refuses to let such a row skip the next measurement. Separating
            # those two meanings is the whole of the #4339 fix -- the earlier
            # attempt withheld the row instead, which made the measurement
            # invisible and had the page call the server unmeasured.
            cache.put(server.name, identities[server.name], verdict)
            measured += 1
        else:
            # A pre-flight that could not run says nothing about the server, only
            # about the moment: a missing credential, an unreachable tunnel, a
            # binary mid-install. Caching that keys the failure to an execution
            # identity that has not changed, so the server would never be
            # re-evaluated once the condition clears. Report it for this pass and
            # pay the spawn again next time.
            logger.info("shareability: %s could not be evaluated yet", server.name)
        known[server.name] = verdict
        completed += 1
        if on_progress is not None:
            # A reporting hook must never be able to abandon measurements that
            # already cost two spawns each, so its failure is logged and dropped.
            try:
                on_progress(measured, completed, total)
            except Exception:
                logger.debug("shareability: progress hook raised", exc_info=True)
        logger.info(
            "shareability: evaluated %s -> ran=%s caller_sensitive=%s",
            server.name, result.ran, result.caller_sensitive,
        )

    await asyncio.to_thread(cache.flush)
    return known
