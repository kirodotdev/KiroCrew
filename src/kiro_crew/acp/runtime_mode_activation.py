"""Putting a started session on its agent.

The bracketed ``session/set_mode`` with its skill-view aliases and the one-at-a-time projection
generation, the spawn-agent checks, and the model an agent switch selected.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_native_skill_projection``, ``_skill_projection_generation``,
``_skill_projection_issued``, ``_skill_projection_lock_obj``,
``_skill_projection_unadopted``, ``_switched_models``.

Instance fields read: ``_agent``, ``_spawn_skill_projection``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import acp_tool_gate
from kiro_crew.acp._dispatch import parse_session_modes, set_mode_params
from kiro_crew.acp.session_handle import AcpModeNotFound, AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKENDS_MARKDOWN_AGENT_SPECS, METHOD_SET_MODE
from kiro_crew.agent import markdown_spec_for_agent
from kiro_crew.metrics.events import SKILL_VIEW_FALLBACKS, emit_counter

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


# After a skill-view alias is published, how long ``session/set_mode`` waits
# before each retry of a "not found" answer. kiro-cli reloads its agents
# directory 500 ms after the last data write to it (a trailing debounce, then a
# full rescan; measured selectable 0.7-1.5 s after publication) and never on a
# rename, and a miss does not trigger a reload itself. Short even steps land
# soon after a reload; the total (3 s) stays well inside the set_mode budget, and
# after it the session start fails with the restart remedy rather than activate
# any older copy of the agent.
_PROJECTED_MODE_RETRY_DELAYS_SECS: tuple[float, ...] = (0.5, 0.5, 0.5, 0.5, 1.0)


async def _prepare_projection_to_completion(
    prepare: Callable[..., Any], *args: Any, **kwargs: Any
) -> tuple[Any, bool]:
    """Run *prepare* in a worker thread and wait for it even through cancellation.

    Returns ``(result, cancelled)``. A cancelled start must not leave the
    projection lock while its worker is still running: the worker may already
    have read an edit, and the caller adopts that view before re-raising. The
    wait is bounded by the preparation itself (its alias lock times out)."""
    worker = asyncio.ensure_future(asyncio.to_thread(prepare, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            # ``wait`` never cancels the worker when this task is cancelled.
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled and worker.exception() is not None:
        # The worker's own failure must not replace the cancellation the caller
        # asked for; an unadopted preparation is recorded by the caller either way.
        raise asyncio.CancelledError
    return worker.result(), cancelled


#
# Every session start re-prepares the native skill projection and sends the
# alias it published, and starts on one runtime overlap. A preparation reads
# the agent specs BEFORE it waits on the cross-process alias lock, so without
# ordering a start that read the spec before an edit revoked a grant could
# return last, become ``_native_skill_projection`` and activate that alias.
#
# Two mechanisms close it, and each needs the other:
#
# * ``_skill_projection_lock`` serializes preparation AND adoption per runtime,
#   so preparations read the specs in the order they are adopted. It is held
#   only across the ``to_thread`` preparation and the in-memory adoption,
#   never across ``session/set_mode`` (which can take a whole handshake budget
#   while a switched-to MCP server boots), and never across blocking I/O on
#   the loop: the file I/O runs in the worker thread.
# * a generation, issued under the lock before the read, stamps each adopted
#   projection. Adoption refuses one older than the adopted generation, and a
#   start whose ``set_mode`` is still pending re-checks it before every attempt
#   and after the answer: a newer view adopted meanwhile is the one it uses, or,
#   once the host has already answered for the older alias, the start fails.
#   The after-answer check first takes the lock itself, so a preparation that
#   was still running when the answer arrived is adopted before it is judged.
#   A preparation runs to completion even when its start is cancelled, and its
#   view is adopted before the cancellation propagates; one that yields no
#   view (or raises) is recorded as unadopted, and until a later view is
#   adopted a pending start cannot prove its view current and fails.
#   The lock is what makes the counter mean "read later": issued outside it, a
#   later generation could belong to a thread that read the spec first.
#
# Lock order (outermost first): ``_skill_projection_lock`` (asyncio, this
# runtime) -> the alias file lock ``_projection_alias_lock`` (flock, taken in
# the worker thread) -> the workspace ``cli.json`` settings lock -> the
# projection lease. Nothing that holds a file lock ever awaits the asyncio
# lock: the file locks are taken and released inside one synchronous call on a
# worker thread, so the thread cannot wait on the loop, and the loop never
# blocks on a file lock. ``_stdin_write_lock`` is never taken while this lock
# is held, because the send happens after adoption releases it, and the
# after-answer check takes this lock with no other lock held.


def _skill_projection_lock(self: AcpRuntime) -> asyncio.Lock:
    """The per-runtime lock around skill-view preparation and adoption.

    Created on first use, like :meth:`_stdin_write_lock`, so a runtime built
    without ``__init__`` (test doubles) has one."""
    lock = getattr(self, "_skill_projection_lock_obj", None)
    if lock is None:
        lock = asyncio.Lock()
        self._skill_projection_lock_obj = lock
    return lock


def _issue_skill_projection_generation(self: AcpRuntime) -> int:
    """The next preparation generation. Call with the projection lock held."""
    generation = int(getattr(self, "_skill_projection_issued", 0)) + 1
    self._skill_projection_issued = generation
    return generation


def _adopted_skill_projection_generation(self: AcpRuntime) -> int:
    return int(getattr(self, "_skill_projection_generation", 0))


def _note_unadopted_skill_projection(self: AcpRuntime, generation: int) -> None:
    """Record that preparation *generation* ended without being adopted.

    It may have read an edit no adopted view reflects (it returned no view, or
    raised), so until a later preparation is adopted a pending start cannot
    prove its view is current. Call with the projection lock held."""
    if generation > self._unadopted_skill_projection_generation():
        self._skill_projection_unadopted = generation


def _unadopted_skill_projection_generation(self: AcpRuntime) -> int:
    return int(getattr(self, "_skill_projection_unadopted", 0))


def _adopt_skill_projection(self: AcpRuntime, prepared: Any, generation: int) -> bool:
    """Make *prepared* this runtime's projection unless a newer one was adopted.

    Call with the projection lock held. Returns ``False`` -- and changes
    nothing -- for a preparation older than (or as old as) the adopted one."""
    if generation <= self._adopted_skill_projection_generation():
        return False
    self._native_skill_projection = prepared
    self._skill_projection_generation = generation
    return True


def _activates_agent_by_mode(self: AcpRuntime) -> bool:
    """Whether ``session/set_mode`` names something this host can resolve.

    Read from ``ACP_BACKEND_ROUTING`` rather than declared here or asked of
    the harness: the hosts whose privileged tools are governed by an agent
    spec are exactly the hosts that HAVE an agent to activate, and a second
    copy of that membership would be free to disagree with the one that
    decides. kiro-cli and the KAS relay are that population.

    A host routed another way has no agent id to send. codex's modes are its
    own permission tiers (``read-only`` / ``agent``), so a Crew agent name
    resolves to nothing there and the request faults — taking down, on the
    cleanup path below, a session that had started fine.
    """
    return acp_tool_gate.routing_for(self.acp_backend) is acp_tool_gate.Routing.AGENT_SPEC


def _mode_available(agent: str, resp: dict[str, Any]) -> bool:
    """Whether ``set_mode`` should be attempted for ``agent`` given a
    ``session/new``|``session/load`` response.

    True when the backend advertised no ``modes`` list at all (older kiro-cli
    / offline fake backend — attempt for backward compatibility) OR the agent
    is in the advertised ``availableModes``. False when a modes list WAS
    advertised (even an empty one) and the agent is absent — the case that
    would otherwise fault with ``-32603 "Mode '<agent>' not found"``. An
    explicitly-empty ``availableModes: []`` therefore fails closed, not open.
    """
    ids, _current, advertised = parse_session_modes(resp)
    if not advertised:
        return True
    return agent in ids


async def _verify_spawn_agent_active(
    self: AcpRuntime,
    session_id: str,
    resp: dict[str, Any],
    *,
    override: str | None,
) -> None:
    """Fail closed when the agent the ``--agent`` flag selected never loaded.

    Guard (A2) — the spawn-flag half of Guard (A) in :meth:`create_session`.
    ``set_mode`` only ever activates an EXPLICIT override (or, on KAS, the
    injected default), so on kiro-cli the agent chosen by ``--agent`` — the
    agent of every ordinary session — reaches no availability check at all.
    It needs one, because that spec can fail to load silently: kiro-cli
    validates ``~/.kiro/agents/<agent>.json`` with ``deny_unknown_fields``
    and, on ANY unknown field, rejects the spec wholesale and runs its own
    default agent instead. :func:`kiro_crew.agent.migrate_agent_specs` exists
    to strip the two keys already known to trip it; nothing validates the
    rest, and a spec written by another tool (or by a product this install
    superseded) can carry more.

    Nothing downstream notices the substitution. The ``set_mode`` response is
    never read back, ``currentModeId`` is never re-compared, and
    :mod:`kiro_crew.acp.mcp_session_report` only LOGS — its own docstring
    forbids reading a missing report as "not mounted". The session then runs
    with NONE of Kiro Crew's control plane, while the global provider
    ``mcp.json`` that Kiro Crew pins off only on specs IT writes stays
    merged. So third-party MCP servers declared there keep working and every
    Kiro Crew tool the injected prompt names — ``learn_add`` among them —
    answers "does not exist", which the agent reports to the user as its
    memory being unavailable.

    Skipped when an override is requested: Guard (A) checks the agent
    ``set_mode`` will activate.

    ``currentModeId`` is read as PROOF, in both directions. A live probe of
    this backend settles what it means: a spec that loads is reported as the
    current mode AND listed in ``availableModes``, while a spec the backend
    refuses is absent from the list and ``currentModeId`` names the backend's
    own default instead. So a non-empty ``currentModeId`` naming something
    OTHER than the spawn agent is positive evidence of the substitution, and
    fails closed even when no advertised list came back. Treating a
    current-mode mismatch as an extra ADMIT is the hole this avoids: a
    ``currentModeId``-only response naming a substituted agent would otherwise
    sail through the compatibility escape.

    That escape is therefore narrow. It applies only when the response names
    NO current mode, where the advertised list is the sole signal and its
    absence is no evidence of a substitution -- so older kiro-cli and the
    offline fake backend behave exactly as before.

    Asked of the harness, which answers yes only for a host whose argv
    actually carries ``--agent``. Never phrased as an inequality: a negative
    test would silently capture every host added later (harness-parity H5),
    and a host that activates its agent by ``set_mode`` already has that
    response as proof -- on KAS the agent travels over the wire as an injected
    custom agent and Guard (A) covers it. Asked of the host rather than of
    "the KAS projection came back None" so the same call serves
    :meth:`load_session`, which never builds that projection.
    """
    if override or not self._agent:
        return
    if self._harness.verifies_agent_activation:
        spawn_agent = self._agent
        ids, current, _adv = parse_session_modes(resp)
        if current:
            if current == spawn_agent:
                return
        elif self._mode_available(spawn_agent, resp):
            return
        await self.terminate_session(session_id)
        raise AcpRuntimeError(
            f"Agent {spawn_agent!r} was spawned with --agent but is not the "
            f"agent this session is running (current mode: "
            f"{current or '(none reported)'}; advertised: {ids or 'none'}). "
            + await self._spawn_agent_not_loaded_reason(spawn_agent)
        )


async def _spawn_agent_not_loaded_reason(self: AcpRuntime, spawn_agent: str) -> str:
    """Why the ``--agent`` spec did not load, and the remedy, for Guard (B).

    Crew's roster lists an agent defined as one markdown file (``<name>.md``,
    the v3 / Kiro IDE form) for every backend, but a host that answers False
    to ``reads_markdown_agent_specs`` discovers ``*.json`` alone, so such an
    agent reaches this guard exactly like a missing JSON spec does -- and the
    generic advice ("run setup to rewrite the agent config") would send the
    operator to repair a file that is not the problem. The markdown check is
    asked only once the failure has ALREADY happened, on the refusal branch,
    so the spawn path itself gains no gate and no failure mode
    (harness-parity H13): the host answers from
    ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``, never "is kiro", and a host added
    later that reads markdown is handed the generic text unchanged.
    """
    if not self._harness.reads_markdown_agent_specs:
        markdown_spec = await asyncio.to_thread(
            markdown_spec_for_agent, spawn_agent, self._work_dir
        )
        if markdown_spec is not None:
            from kiro_crew.acp_backends import backend_to_stored

            capable = ", ".join(
                repr(b) for b in sorted(map(backend_to_stored, ACP_BACKENDS_MARKDOWN_AGENT_SPECS))
            )
            return (
                f"{spawn_agent!r} is defined in markdown ({markdown_spec.name}); the "
                f"{self._harness.backend or 'kiro'!r} backend loads JSON agent specs "
                f"only, so it ran its own default agent instead, which would "
                f"silently drop every Kiro Crew tool the agent's prompt relies on. "
                f"Switch agent.acp_backend to {capable} or add a JSON spec for this "
                f"agent."
            )
    return (
        f"Its ~/.kiro/agents/{spawn_agent}.json is missing, or the backend "
        f"refused to load it. Refusing to run the backend's own default "
        f"agent in its place, which would silently drop every Kiro Crew "
        f"tool the agent's prompt relies on. Run `kirocrew setup "
        f"--agent-only` to rewrite the agent config."
    )


def _resolve_start_alias(self: AcpRuntime, projection: Any, mode_agent: str) -> str:
    """Resolve *mode_agent*'s alias for a session START, launch-agent-aware.

    The launched agent's own activation is allowed at EVERY start even with
    no prepared view (the process is already running as it, and a shared
    runtime starts many sessions as self._agent over its life), so it takes
    ``spawn_agent`` (which keeps the authored name) rather than the strict
    ``agent``. Every OTHER mode_agent takes the strict resolver, so a switch
    to a mode this view never prepared is still rejected. Used at the initial
    resolution AND at both supersession re-checks, so a newer EMPTY projection
    adopted by a concurrent start does not reject the unchanged launch agent
    the strict resolver has no entry for."""
    if mode_agent and mode_agent == self._agent:
        return str(projection.spawn_agent(mode_agent))
    return str(projection.agent(mode_agent))


def _superseding_alias(self: AcpRuntime, projection: Any, mode_agent: str) -> str:
    """*mode_agent*'s alias in a newer view adopted while its start was pending.

    The start's own preparation is older than that view, so its alias is never
    sent again; when the newer view does not offer the agent at all, the
    start fails rather than fall back to the older one."""
    try:
        return self._resolve_start_alias(projection, mode_agent)
    except ValueError as exc:
        emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
        raise AcpRuntimeError(
            f"Agent {mode_agent!r} changed while this session was starting, and its "
            f"new skill view cannot be used ({exc}). The session was not started on "
            "the copy from before the change, which could carry permissions the change "
            "removed. Fix the agent and start the session again; if it keeps failing, "
            "restart the gateway."
        ) from exc


def _refuse_if_view_unverified(self: AcpRuntime, mode_agent: str, used_generation: int) -> None:
    """Fail a start while the newest preparation of this runtime was not adopted.

    A preparation that began after this start's own and ended without a view
    (no view could be prepared, or it raised) may have read an edit to
    *mode_agent*'s spec that no adopted view reflects, so nothing proves the
    alias this start sends, or the host activated, is current. A view adopted
    after that preparation read the specs later still, and clears it."""
    unadopted = self._unadopted_skill_projection_generation()
    if unadopted <= max(used_generation, self._adopted_skill_projection_generation()):
        return
    emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
    logger.warning(
        "AcpRuntime set_mode: a newer skill-view preparation did not complete while "
        "agent=%s was starting; refusing to start the session on a view it cannot "
        "verify",
        mode_agent,
    )
    raise AcpRuntimeError(
        f"Agent {mode_agent!r}: a newer preparation of its skill view did not "
        "complete while this session was starting, so nothing proves the view "
        "kiro-cli would run still matches the agent's spec. The session was not "
        "started. Start it again; if it keeps failing, restart the gateway."
    )


def _switched_model_record(self: AcpRuntime) -> dict[str, str]:
    """The per-session ``agent/switched`` model record, created on first use.

    Read through this rather than the attribute because a runtime built with
    ``object.__new__`` (the shape several start-path tests use) never ran
    ``__init__``, and a start must not fail for want of an empty record.
    """
    record: dict[str, str] | None = getattr(self, "_switched_models", None)
    if record is None:
        record = {}
        self._switched_models = record
    return record


def _note_switched_model(self: AcpRuntime, session_id: str, params: dict[str, Any] | None) -> None:
    """Record the model a ``_kiro.dev/agent/switched`` frame names for *session_id*.

    Called from the reader loop before the frame is queued, so the record is
    in place by the time the ``session/set_mode`` answer that follows it
    resolves. A frame naming no model leaves the previous record alone.
    """
    model = (params or {}).get("model")
    if isinstance(model, str) and model.strip():
        self._switched_model_record()[session_id] = model.strip()


def take_switched_model(self: AcpRuntime, session_id: str) -> str:
    """Remove and return the model recorded for *session_id*, or ``""``.

    For a session handle whose ``set_mode`` switches a live session's agent:
    it clears the record before sending, so only that activation's frame is
    read back, and takes it before the next turn.
    """
    return self._switched_model_record().pop(session_id, "")


async def _served_default_after_mode(
    self: AcpRuntime, handle: AcpSessionHandle, session_id: str
) -> None:
    """Re-run the served-default check for the model ``set_mode`` just applied.

    Both session-start paths check the served default right after storing the
    ``session/new`` / ``session/load`` response, which is BEFORE the mode is
    activated. kiro-cli's ``set_mode`` puts the session on the model the
    activated agent's spec pins, undoing that earlier switch, and
    ``agent.model`` is copied into ``kirocrew.json``. An unentitled default
    then fails the first prompt with "no access to model".

    The model judged is the one kiro-cli itself reported in the
    ``_kiro.dev/agent/switched`` frame for this activation, not a second read
    of the spec file: the file can change after kiro-cli read it, and only
    the host knows which copy of the agent it loaded. No frame, or a frame
    without a model (older kiro-cli), is no evidence and changes nothing.
    """
    activated = self._switched_model_record().pop(session_id, "")
    if activated:
        await handle.ensure_served_default(activated_model=activated)


def _refuse_if_view_superseded(
    self: AcpRuntime, mode_agent: str, sent_alias: str, used_generation: int
) -> None:
    """Fail a start the host answered after a newer view changed its agent.

    A concurrent start may adopt a newer projection while this start's
    ``set_mode`` is pending. Nothing says whether the host activated the older
    alias before or after that newer preparation read the spec, so when the
    newer view gives *mode_agent* a different alias the session may be running
    a view an edit has since revoked grants from, and it is not started. A
    newer view that leaves this agent's alias unchanged refuses nothing. Call
    with the projection lock held, so no preparation is mid-flight."""
    self._refuse_if_view_unverified(mode_agent, used_generation)
    if self._adopted_skill_projection_generation() == used_generation:
        return
    projection = getattr(self, "_native_skill_projection", None)
    try:
        newest = (
            self._resolve_start_alias(projection, mode_agent) if projection is not None else None
        )
    except ValueError:
        newest = None
    if newest == sent_alias:
        return
    emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
    logger.warning(
        "AcpRuntime set_mode: skill view %s for agent=%s was replaced by a newer "
        "preparation while the request was pending; refusing to start the session",
        sent_alias,
        mode_agent,
    )
    raise AcpRuntimeError(
        f"Agent {mode_agent!r} changed while this session was starting, and kiro-cli "
        f"may have activated the skill view prepared before the change ({sent_alias}), "
        "which could carry permissions the change removed. The session was not "
        "started. Start it again; if it keeps failing, restart the gateway."
    )


async def _activate_mode_bracketed(
    self: AcpRuntime,
    session_id: str,
    mode_agent: str,
    *,
    budget: float,
    payload_snapshot: Any,
    wire_registered: bool,
    mode_id: str | None = None,
) -> str | None:
    """Send ``session/set_mode`` for *mode_agent* inside the derived-spec bracket.

    *mode_id* is the id the host registered *mode_agent* under when that
    differs (``kas_wire_agent_id``); it is what ``set_mode`` names, and
    *mode_agent* stays the id every gate and message uses.

    ONE body for both session-start paths (create and resume), because the bracket
    is a protocol and a protocol written twice is two protocols the moment one copy
    is edited. Any failure terminates *session_id* first: ``session/new`` or
    ``session/load`` already succeeded, so the session exists in the host and a
    plain local unregister would leak it in the shared process.

    A ``set_mode`` naming an agent activates that agent's spec, and the spec it
    activates needs the same bracket the spawn's does. Neither other gate reaches
    here: the spawn gate is keyed to ``self._agent``, so a SHARED runtime spawned
    as one agent and switched to another on this line passes no gate, and a host
    that builds no in-process tool surface never runs ``session_mcp``'s gate.

    WHERE the spec is consumed differs by host, and that decides which snapshot the
    post-check may use -- exactly ONE per consumed load:

    * ``wire_registered`` -- the spec was consumed at ``session/new``, in the wire
      payload built under the projection's own gate; ``set_mode`` activates what is
      already registered and re-reads nothing. A fresh read HERE would judge the
      file while the host holds the payload, so a revocation landing between the
      build and this line would pass that read while the registered definition
      still carried the grants it removed. The post-check compares against the
      payload's OWN snapshot. No gate call on this path: a second snapshot for one
      consumed load is the defect, not a safeguard.
    * otherwise -- the spec is consumed HERE: kiro-cli reads it from disk at
      ``set_mode`` and boots that agent's MCP servers. Gated BEFORE the send,
      because a stale mirror activated here mounts and auto-approves a server the
      default agent does not have.

    After the send returns the host has consumed the spec, which closes the window
    the snapshot opened: a write landing before that point is caught, and one
    landing after cannot change what was already consumed. Same answer as the
    ``initialize`` bracket -- a session that may have activated an unverified spec
    must not survive.
    """
    from kiro_crew.agent import (
        DerivedSpecStale,
        require_fresh_derived_spec,
        require_unchanged_derived_spec,
    )

    if wire_registered:
        mode_snapshot = payload_snapshot
    else:
        try:
            mode_snapshot = await asyncio.to_thread(
                require_fresh_derived_spec, mode_agent, self._work_dir
            )
        except DerivedSpecStale as exc:
            await self.terminate_session(session_id)
            raise AcpRuntimeError(str(exc)) from exc
    try:
        projection_now: Any = None
        used_generation = 0
        if getattr(self, "_native_skill_projection", None) is not None:
            from kiro_crew.acp.skill_projection import (
                CrossProcessProjectionLockTimeout,
                InProcessProjectionWaitTimeout,
                prepare_native_skill_projection,
            )

            # Preparation and adoption run under the per-runtime projection
            # lock, so overlapping starts read the specs in the order their
            # views are adopted (see _skill_projection_lock for the lock order).
            async with self._skill_projection_lock():
                previous = getattr(self, "_native_skill_projection", None)
                generation = self._issue_skill_projection_generation()
                adopted = False
                try:
                    # Keep the transport mode selected at spawn for this process.
                    # The rollback environment switch takes effect after restart.
                    lock_errors: list[OSError] = []
                    prepared, cancelled = await _prepare_projection_to_completion(
                        prepare_native_skill_projection,
                        self._work_dir,
                        enabled=True,
                        on_lock_unavailable=lock_errors.append,
                    )
                    if prepared is not None:
                        # set_mode always names the FRESH alias, never an older
                        # one: an alias the host loaded earlier (the spawn one
                        # included) may hold a generation of the spec an edit has
                        # since revoked grants from. Inbound frames keep
                        # translating every alias this process published before
                        # (``recognise``), so an agent the host still lists under
                        # an older alias stays selectable. Holding
                        # ``_spawn_skill_projection`` keeps those aliases -- and so
                        # the host's view of them -- out of the prune.
                        spawn = getattr(self, "_spawn_skill_projection", None)
                        if spawn is not None:
                            prepared.recognise(spawn)
                        if previous is not None:
                            prepared.recognise(previous)
                        # Under the lock a fresh generation is always the
                        # newest; the refusal keeps an older preparation from
                        # ever replacing a newer one should adoption be reached
                        # any other way.
                        adopted = self._adopt_skill_projection(prepared, generation)
                    if cancelled:
                        # Adopted first: a view this preparation read must not
                        # vanish with the start that asked for it.
                        raise asyncio.CancelledError
                    if prepared is None:
                        # No view could be prepared (the alias lock is busy), so
                        # nothing proves any alias this process holds still says
                        # what the agent's spec says now. Fail the start rather
                        # than activate one.
                        emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_unprepared"})
                        logger.warning(
                            "skill projection: re-preparation unavailable at set_mode; "
                            "refusing to start agent %r on a view it cannot verify",
                            mode_agent,
                        )
                        # Name a holder only when the preparer proved one: its
                        # in-process wait timing out means another start in THIS
                        # gateway; the alias file lock timing out means another
                        # process. Anything else (a bad lock file, the settings
                        # lock, a lease, a failed write) names no holder.
                        if any(isinstance(e, InProcessProjectionWaitTimeout) for e in lock_errors):
                            held_by = (
                                "another session start in this gateway still "
                                "held the skill-view lock"
                            )
                        elif any(
                            isinstance(e, CrossProcessProjectionLockTimeout) for e in lock_errors
                        ):
                            held_by = "the skill-view lock is held by another Kiro Crew process"
                        else:
                            held_by = "a lock or file it needs was unavailable"
                        raise AcpRuntimeError(
                            f"Agent {mode_agent!r}: its skill view could not be "
                            f"prepared ({held_by}), so this session was not "
                            "started. Try again in a moment; if it keeps failing, "
                            "restart the gateway."
                        )
                finally:
                    if not adopted:
                        self._note_unadopted_skill_projection(generation)
                projection_now = self._native_skill_projection
                used_generation = self._adopted_skill_projection_generation()
        # set_mode is a handshake request: switching to an agent boots THAT
        # agent's MCP servers, the same server (re-)initialization that gives
        # session/new and session/load their 90s budget. A switched-to server
        # pending OAuth holds the response for its full 30s wait, so the generic
        # _REQUEST_TIMEOUT would turn set_mode into the SAME race the
        # session-start floor exists to prevent (see _SESSION_NEW_TIMEOUT).
        #
        # kiro-cli 2.25/2.26 does not reload its agents directory on the rename
        # that publishes an alias, so a fresh one can answer ``Mode '<alias>' not
        # found`` while the file is on disk. A miss for the alias this projection
        # translated *mode_agent* to -- and for nothing else -- forces a rescan and
        # is retried after _PROJECTED_MODE_RETRY_DELAYS_SECS. When the host still
        # has not loaded it, the start FAILS with the remedy: no other copy of the
        # agent (an older alias, or the authored spec kiro-cli cached) is proven
        # to match the spec on disk, so none is activated in its place. Every other
        # error propagates as before.
        params = set_mode_params(session_id, mode_id or mode_agent)
        # Translated from the projection this bracket adopted and sent as
        # fixed wire params -- past the send's own translation, which reads
        # ``_native_skill_projection`` at send time. The projection lock is
        # NOT held across the send, so a concurrent start may adopt a newer
        # view while this request is pending or between retries: before each
        # attempt the alias is re-derived from the newest adopted view (never
        # an older one), and an answer that arrives after a newer view
        # changed this agent fails the start (``_refuse_if_view_superseded``).
        # ``translate`` is passed only when a projection exists, so the send
        # is the same call as before for every caller without one.
        wire = params
        sent_alias: str | None = None
        untranslated: dict[str, Any] = {}
        if projection_now is not None:
            # The launched agent's own activation is allowed at EVERY session
            # start, even with no prepared view: the process is already running
            # as it, and a shared runtime starts many sessions as self._agent
            # over its life, so this is not a one-shot token to consume -- doing
            # so would break the second shared session that legitimately starts
            # as the same agent. _resolve_start_alias keeps the authored name
            # for self._agent; every OTHER mode_agent takes the strict agent(),
            # so a switch to a mode this projection never prepared is still
            # rejected and an agent cannot escape its scope. The SAME resolver
            # runs at both supersession re-checks below, so a newer empty
            # projection adopted by a concurrent start does not reject the
            # unchanged launch agent. The general (mid-session) set_mode path
            # stays strict as before -- this allowance is scoped to the start
            # bracket and keyed on self._agent, not on a mutable token.
            sent_alias = self._resolve_start_alias(projection_now, mode_agent)
            wire = {**params, "modeId": sent_alias}
            untranslated = {"translate": False}
        retries = iter(_PROJECTED_MODE_RETRY_DELAYS_SECS)
        missed: str | None = None
        while True:
            if (
                sent_alias is not None
                and self._adopted_skill_projection_generation() != used_generation
            ):
                projection_now = self._native_skill_projection
                used_generation = self._adopted_skill_projection_generation()
                newest = self._superseding_alias(projection_now, mode_agent)
                if newest != sent_alias:
                    # The retry schedule and the miss record belong to the
                    # alias being replaced; the newer one starts its own.
                    sent_alias = newest
                    wire = {**params, "modeId": sent_alias}
                    retries = iter(_PROJECTED_MODE_RETRY_DELAYS_SECS)
                    missed = None
            if sent_alias is not None:
                self._refuse_if_view_unverified(mode_agent, used_generation)
            try:
                await self._send_and_await(METHOD_SET_MODE, wire, timeout=budget, **untranslated)
            except AcpModeNotFound as exc:
                if sent_alias is None or exc.mode_id != sent_alias:
                    raise
                missed = exc.mode_id
                delay = next(retries, None)
                if delay is None:
                    emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_unloaded"})
                    logger.warning(
                        "AcpRuntime set_mode: kiro-cli has not loaded skill view %s for "
                        "agent=%s after a forced reload and %d attempt(s); refusing to "
                        "start the session on any other copy of the agent",
                        missed,
                        mode_agent,
                        1 + len(_PROJECTED_MODE_RETRY_DELAYS_SECS),
                    )
                    raise AcpRuntimeError(
                        f"Agent {mode_agent!r} changed since this kiro-cli process "
                        f"started, and kiro-cli has not loaded its new skill view "
                        f"({missed}) after a forced reload. The session was not started "
                        "on an older copy of the agent, which could carry permissions "
                        "the change removed. Restart the gateway to apply the change."
                    ) from exc
                # kiro-cli reloads on a data write, never on the rename that
                # published the alias, and a miss triggers no reload: rewrite
                # the alias's own bytes in place so it rescans, then retry.
                from kiro_crew.acp.skill_projection import announce_alias

                await asyncio.to_thread(announce_alias, exc.mode_id)
                await asyncio.sleep(delay)
                continue
            break
        if sent_alias is not None:
            # Wait out any preparation already running: one that began before
            # this answer may have read an edit the host's activation predates,
            # and it is adopted only when it finishes. Nothing else is held
            # here, so this cannot invert the lock order.
            async with self._skill_projection_lock():
                self._refuse_if_view_superseded(mode_agent, sent_alias, used_generation)
        if missed is not None:
            emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "loaded_after_retry"})
            logger.info(
                "AcpRuntime set_mode: kiro-cli loaded skill view %s for agent=%s " "after a reload",
                missed,
                mode_agent,
            )
    except Exception:
        await self.terminate_session(session_id)
        raise
    try:
        await asyncio.to_thread(require_unchanged_derived_spec, mode_snapshot)
    except DerivedSpecStale as exc:
        await self.terminate_session(session_id)
        raise AcpRuntimeError(str(exc)) from exc
    return sent_alias
