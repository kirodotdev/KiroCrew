"""What KAS speaks beyond ACP on a session.

Its hooks requests (list, session start, execute), the checkpoint rewind of an
unanswered prompt, and its own session-update discriminants (mode, session info).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_compaction_failed_at``, ``_compaction_terminal_seen``,
``_hook_tasks``, ``_kas_prompt_row``, ``_last_kas_mode_id``, ``kas_auto_approved``,
``kas_projected_agent``, ``last_compaction_transient``.

Instance fields read: ``_crew_agent``, ``_listed_hooks``, ``_prompt_starts``,
``_runtime``, ``_session_id``, ``_session_key``, ``_turn_done``,
``kas_registered_agents``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_profile, kas_wire
from kiro_crew.acp._dispatch import kas_crew_agent_id, redact_text
from kiro_crew.acp.client import compaction_failure_detail, compaction_failure_is_transient
from kiro_crew.acp.types import (
    ACP_BACKENDS_HOOKS_LIST,
    EVENT_AGENT_SWITCHED,
    EVENT_COMPACTION_STATUS,
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    METHOD_KAS_CHECKPOINT_REVERT,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


#: Hook executions one session may have running at once. A hook runs for up to
#: its own timeout, so this bounds the processes a peer can hold open.
_MAX_INFLIGHT_HOOK_EXECUTIONS = 4

#: The backend's hooks requests, answered by
#: :meth:`AcpSessionHandle._answer_kas_hooks_request`. A frozenset so the
#: dispatch test is one membership check. ``executeHook`` is the one that runs a
#: command, and :func:`kas_wire.hooks_execute` owns every gate it passes.
_KAS_HOOKS_METHODS = frozenset(
    {
        kas_wire.METHOD_HOOKS_LIST,
        kas_wire.METHOD_HOOKS_SESSION_START,
        kas_wire.METHOD_HOOKS_EXECUTE,
    }
)


async def discard_unanswered_prompt(self: AcpSessionHandle) -> bool:
    """Rewind KAS history to before this handle's last prompt; True when KAS did.

    For a caller about to re-send a prompt that failed before any output:
    without it KAS keeps the failed copy and the model reads both. Sends
    nothing (False) when no row was recorded (only the KAS update branch
    records one, so every other harness stops here), when the engine did not
    announce checkpoints, when a turn is running, or when a later turn started
    on this handle since that prompt, so another prompt's row is never the one
    rewound. One shot.
    """
    row = self._kas_prompt_row
    self._kas_prompt_row = None
    caps = getattr(self._runtime, "_agent_capabilities", None)
    meta = caps.get("_meta") if isinstance(caps, dict) else None
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    if not (
        row is not None
        and row[0] == self._prompt_starts
        and self._turn_done.is_set()
        and isinstance(kiro, dict)
        and kiro.get(kas_wire.CAPABILITY_CHECKPOINTS) is True
    ):
        return False
    req_id = await self._send_awaited(
        METHOD_KAS_CHECKPOINT_REVERT, {"sessionId": self._session_id, "messageId": row[1]}
    )
    result = getattr(await self._wait_for_response(req_id, timeout=10.0), "result", None)
    return isinstance(result, dict) and result.get("success") is True


def _is_kas_hooks_request(self: AcpSessionHandle, msg: JsonRpcMessage) -> bool:
    """Whether this frame is a hooks request THIS session may answer.

    A named predicate rather than an inline condition, so the backend clause
    has a test that fails when it is removed: asserting the membership set's
    contents cannot catch a deleted membership CHECK, and the check is the part
    that keeps operator-authored hook commands away from a backend that never
    defined the channel.

    Five conditions, all required: the frame is a request (it carries an id and
    so needs a response), its method is a string, that string is one of the
    hooks methods, this session's backend is in the capability set, and -- for
    ``executeHook``, the one that runs a command -- the handshake announced
    hooks. The type check is load-bearing, not defensive: ``method`` carries
    whatever the peer put on the wire, and a JSON list or object there makes the
    membership test raise :class:`TypeError` inside the dispatch loop.

    The announce clause keeps the execute path dark in fact, not only by hint:
    a backend that sends ``executeHook`` unasked is answered ``-32601`` like any
    method it was never offered.
    """
    return (
        msg.id is not None
        and isinstance(msg.method, str)
        and msg.method in _KAS_HOOKS_METHODS
        and self._runtime.acp_backend in ACP_BACKENDS_HOOKS_LIST
        and (msg.method != kas_wire.METHOD_HOOKS_EXECUTE or kas_wire.hooks_announced())
    )


async def _answer_kas_hooks_request(self: AcpSessionHandle, msg: JsonRpcMessage) -> None:
    """Answer one hooks request from the backend.

    The answers are built in :mod:`kiro_crew.acp.kas_wire`, which owns the
    shapes; this is the route that carries them.

    ``list`` and ``sessionStart`` stay on this loop: each reads an in-memory
    dict and nothing else, so a thread hop would buy nothing. ``list`` records
    what it answered under this handle's OWNING session, which is the record
    ``executeHook`` checks.

    ``executeHook`` runs as a task of its own. It waits on a subprocess for up
    to the hook's timeout, and this loop demuxes every frame of the turn --
    including the cancel that would end it.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    if msg.method == kas_wire.METHOD_HOOKS_EXECUTE:
        if len(self._hook_tasks) >= _MAX_INFLIGHT_HOOK_EXECUTIONS:
            # Refused before a task exists, so a flood of execute frames
            # holds a bounded number of hook processes, never one per frame.
            # Audited like every other refusal on this path.
            reason = "too many hooks already running"
            await asyncio.to_thread(
                kas_wire.audit_execute_refusal,
                self._session_key,
                self._crew_agent,
                params,
                reason,
            )
            await self._send_hook_error(msg.id, f"Refused by Kiro Crew: {reason}")
            return
        task = asyncio.create_task(self._answer_kas_hook_execute(msg.id, params))
        self._hook_tasks.add(task)
        task.add_done_callback(self._hook_tasks.discard)
        return
    if msg.method == kas_wire.METHOD_HOOKS_LIST:
        result = kas_wire.hooks_list_response(
            params, session_key=self._session_key, listed=self._listed_hooks
        )
    else:
        result = kas_wire.hooks_session_start_response(params)
    await self._runtime.send_response(msg.id, result)


async def _answer_kas_hook_execute(self: AcpSessionHandle, request_id: Any, params: dict) -> None:
    """Run one listed hook and answer the request, refusal included.

    A refusal is answered as a JSON-RPC error carrying its reason, never as a
    result: a result carries an exit code, and a command that never started has
    none. An unexpected failure is answered the same way, and a cancelled run
    is answered ``cancelled`` before the cancellation propagates, so the
    backend's turn is never left waiting on a request nobody will answer.
    """
    try:
        result = await kas_wire.hooks_execute(
            params,
            session_key=self._session_key,
            agent=self._crew_agent,
            listed=self._listed_hooks,
        )
    except asyncio.CancelledError:
        try:
            await self._runtime.send_response(request_id, {"exitCode": -1, "cancelled": True})
        except Exception:
            logger.warning("KAS executeHook cancel answer undeliverable for %s", self._session_id)
        raise
    except kas_wire.HookExecuteRefused as exc:
        logger.info("KAS executeHook refused for %s: %s", self._session_id, exc)
        await self._send_hook_error(request_id, f"Refused by Kiro Crew: {exc}")
        return
    except Exception:
        logger.exception("KAS executeHook failed for %s", self._session_id)
        await self._send_hook_error(request_id, "Kiro Crew could not run the hook")
        return
    try:
        await self._runtime.send_response(request_id, result)
    except Exception:
        logger.warning("KAS executeHook answer undeliverable for %s", self._session_id)


def _cancel_hook_tasks(self: AcpSessionHandle) -> None:
    """Cancel every in-flight hook execution this session started.

    ``run_script_hook`` kills the hook's process tree when it is cancelled
    mid-wait, so a cancelled turn or a torn-down session leaves no hook running.
    """
    # Read defensively: a handle assembled without ``__init__`` carries none.
    for task in list(getattr(self, "_hook_tasks", ())):
        task.cancel()


async def _send_hook_error(self: AcpSessionHandle, request_id: Any, message: str) -> None:
    try:
        await self._runtime.send_error(request_id, kas_wire.HOOK_EXECUTE_REFUSED_CODE, message)
    except Exception:
        logger.warning("KAS executeHook refusal undeliverable for %s", self._session_id)


def _handle_kas_update(
    self: AcpSessionHandle, session_update: str, update: dict
) -> list[AcpEvent] | None:
    """Map a KAS-only ``session/update`` discriminant to Crew events.

    Returns a (possibly empty) event list for a discriminant KAS uses in
    place of a kiro-cli ``_kiro.dev/*`` method, or ``None`` when the
    discriminant is not KAS-specific (ordinary chunk/tool frames) so the
    caller falls through to the shared parser. Only ever reached on the KAS
    backend.
    """
    if session_update == UPDATE_CURRENT_MODE:
        # KAS agent-switch echo (kiro-cli: _kiro.dev/agent/switched). The new
        # mode id stands in for kiro-cli's agentName. KAS re-emits this to
        # report the CURRENT mode, so suppress only a repeat of the
        # already-emitted mode (a no-op re-assert); every actual change is
        # emitted. First-sight is NOT suppressed: the session's initial
        # current_mode_update is drained pre-prompt without reaching here, so
        # the first frame that does reach here is a real switch that must not
        # be dropped.
        mode_id = update.get("currentModeId")
        if not (isinstance(mode_id, str) and mode_id):
            return []
        # Deduped on the WIRE id: a crewmate registered as ``vibe-crewmate``
        # and the engine's own ``vibe`` are two modes, though both report
        # under the name ``vibe`` below.
        if mode_id == self._last_kas_mode_id:
            return []
        self._last_kas_mode_id = mode_id
        registered = getattr(self, "kas_registered_agents", None)
        # Back to the crewmate's own id when this is an agent the batch
        # registered under a wire id (see attach_kas_custom_agents).
        crew_id = kas_crew_agent_id(registered, mode_id)
        # The session now runs this agent, so a turn that names none meets
        # this agent's spec hooks, not the one the batch was built for, and
        # what it auto-approves is this agent's entry in the registered batch
        # -- chosen by wire id, so a built-in of the same name never inherits
        # the crewmate's approvals.
        if self.kas_projected_agent:
            from kiro_crew.acp.kas_agents import switched_auto_approved

            self.kas_projected_agent = crew_id
            self.kas_auto_approved = switched_auto_approved(registered, mode_id)
        return [AcpEvent(kind=EVENT_AGENT_SWITCHED, text=crew_id)]
    if session_update == UPDATE_SESSION_INFO:
        return self._handle_kas_session_info(update)
    if session_update in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        # Read for its ``ttft`` stamp, then handed on: the chunk itself is the
        # shared parser's to render.
        kiro = kas_wire.kiro_meta(update)
        if kiro is not None and _kas_turn_running(self):
            kas_profile.record_ttft(self.last_prompt_stats, kiro)
        return None
    # available_commands_update / any other KAS discriminant falls through to
    # the shared parser, which already returns [] for it — Crew surfaces no
    # available-commands UI for any backend (kiro-cli's
    # _kiro.dev/commands/available is likewise unconsumed), so there is
    # nothing to render and no separate branch is needed.
    return None


def _handle_kas_session_info(self: AcpSessionHandle, update: dict) -> list[AcpEvent]:
    """Map a KAS ``session_info_update`` (``_meta.kiro`` union) to events.

    This one discriminant carries what kiro-cli splits across separate
    methods: ``context_usage`` (the context meter) and ``turn_completion``
    (per-turn billing) together reconstruct the single ``_kiro.dev/metadata``
    frame, while the ``summarization_*`` kinds are KAS's compaction status
    (kiro-cli: ``_kiro.dev/compaction/status``) and the ``steering_*`` kinds
    are KAS's mid-turn steer echo (kiro-cli: ``session/update`` steer
    discriminants, handled by the "steer" action).
    """
    kiro = kas_wire.kiro_meta(update)
    if kiro is None:
        return []
    kind = kiro.get(kas_wire.FIELD_KIND)
    if kind == kas_wire.KIND_USER_MESSAGE_ID_ASSIGNED:
        row_id = kiro.get(kas_wire.FIELD_USER_MESSAGE_ID)
        if isinstance(row_id, str) and row_id:
            self._kas_prompt_row = (self._prompt_starts, row_id)
        return []
    if kind == kas_wire.KIND_CONTEXT_USAGE:
        self._apply_kas_context_pct(kiro.get(kas_wire.FIELD_USAGE_PERCENTAGE))
        return []
    if kind == kas_wire.KIND_TURN_COMPLETION:
        self._apply_kas_turn_completion(kiro)
        return []
    if kind in kas_wire.SUMMARIZATION_KINDS:
        if kind == kas_wire.KIND_SUMMARIZATION_COMPLETED:
            # Pre-compaction counts do not describe the session — drop
            # them so the meter resets and fresh telemetry re-derives real
            # numbers (parity with the kiro-cli compaction handler).
            self.last_prompt_stats.reset_after_compaction()
            self._compaction_failed_at = None
            status_type = "completed"
        elif kind == kas_wire.KIND_SUMMARIZATION_FAILED:
            status_type = "failed"
            # Parity with AcpClient._handle_compaction_status: log the WHOLE
            # frame at WARNING. Without it a KAS summarization failure
            # leaves the chat row as the only record of it — and when the
            # row's reason collapses to a placeholder there is nothing to
            # grep server-side and no way to learn which field the reason
            # actually arrived in.
            # redact_text, not the bare frame: conversationSummary rides in
            # this payload, so an unredacted dump would persist whatever the
            # conversation contained -- a pasted credential included -- into
            # gateway.log. Same scrub the notice below applies, for the same
            # reason.
            logger.warning("KAS summarization failed — raw frame: %s", redact_text(str(kiro)))
            # KAS is the third producer of a failed compaction status and
            # rides the SAME dispatch loop, so it gets the same bounded
            # post-failure wait — a KAS turn abandoned after failed
            # summarization must not drain to the ceiling either.
            self._compaction_failed_at = time.monotonic()
            self.last_compaction_transient = compaction_failure_is_transient(kiro)
        else:
            status_type = "started"
        if status_type != "started":
            self._compaction_terminal_seen = True
        # conversationSummary is backend-echoed, LLM-influenced text that
        # reaches the dashboard — redact exfil URLs/credentials first.
        summary = redact_text(str(kiro.get(kas_wire.FIELD_CONVERSATION_SUMMARY, "") or ""))
        if status_type == "failed":
            # conversationSummary is empty on failure, so the notice would
            # collapse to "unknown error" here too — carry KAS's own reason
            # (redacted + bounded by the helper).
            summary = compaction_failure_detail(kiro)
        return [AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)]
    if kind in kas_wire.STEERING_KINDS:
        # KAS mid-turn steer echo. kiro-cli sends these as `session/update`
        # discriminants (handled by the "steer" action); KAS instead puts the
        # kind under `_meta.kiro`, so route it here. `injected` is the
        # settling signal (→ EVENT_STEER_CONSUMED, which _settle_consumed_steers
        # consumes); queued/cleared mirror the kiro path. Never trust
        # backend-echoed steer text — redact before it reaches any surface.
        if kind == kas_wire.KIND_STEERING_CLEARED:
            return [AcpEvent(kind=EVENT_STEER_CLEARED)]
        if kind == kas_wire.KIND_STEERING_INJECTED:
            # The steer now sits in history after the prompt; a rewind to
            # the prompt would drop it too.
            self._kas_prompt_row = None
        text = redact_text(str(kiro.get(kas_wire.FIELD_CONTENT) or ""))
        steer_kind = (
            EVENT_STEER_CONSUMED if kind == kas_wire.KIND_STEERING_INJECTED else EVENT_STEER_QUEUED
        )
        return [AcpEvent(kind=steer_kind, text=text)]
    return []


def _apply_kas_context_pct(self: AcpSessionHandle, pct: object) -> None:
    """Apply a KAS ``context_usage`` percentage to the context meter.

    A real ``usage_update`` is authoritative (``context_tokens_from_usage``)
    and must not be clobbered. ``sanitize_pct`` (shared with the kiro-cli
    metadata path) clamps NaN/±inf/out-of-range and returns None when the
    value is absent or unparseable, so malformed telemetry degrades to
    "no reading" rather than aborting the active turn.
    """
    pct_f = self.last_prompt_stats.sanitize_pct(pct)
    if pct_f is None or self.last_prompt_stats.context_tokens_from_usage:
        return
    self.last_prompt_stats.context_pct = pct_f
    self.last_prompt_stats.note_pct_reported()
    self._backfill_context_window(pct_f)


def _apply_kas_turn_completion(self: AcpSessionHandle, kiro: dict) -> None:
    """Set per-turn credits from a KAS ``turn_completion`` frame.

    Delegates the ``promptTurnSummaries`` credit sum (only ``unit ==
    "credit"`` entries count; the acp provider bills in credits) to
    ``kas_wire.turn_credits``. The frame carries the whole turn's summary, so
    the total is ASSIGNED, not accumulated: a duplicate or resume-replayed
    ``turn_completion`` reports the same total and must not inflate the
    displayed cost. A malformed frame (``turn_credits`` returns ``None``)
    leaves the prior value untouched rather than zeroing.
    """
    total = kas_wire.turn_credits(kiro)
    if total is not None:
        self.last_prompt_stats.credits = total
    if _kas_turn_running(self):
        kas_profile.record_breakdown(self.last_prompt_stats, kiro)


def _kas_turn_running(handle: AcpSessionHandle) -> bool:
    """Whether a prompt of this handle is in flight, so a frame belongs to it.

    ``session/load`` replays every recorded turn's ``turn_completion``, breakdown
    included, before any prompt runs; those describe turns the crew log already
    recorded, and recording them again would count each one twice per resume. Read
    defensively: a handle assembled without ``__init__`` carries no event.
    """
    done = getattr(handle, "_turn_done", None)
    return done is not None and not done.is_set()
