"""The subagent spawning, steering, and host headroom tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool are reached through this module, and ``test_mcp_tool_registry`` fails
if one arrives without the other. The descriptors, the list and status reads and
the batch spawn live in the ``spawn_parts`` owners (the map is at the foot of this
module); ``spawn_run``, the continue, steer and release tools and the host
headroom read live here.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import os
import sys as _sys
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror, mcp_core
from kiro_crew import resource_status as host_status
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.mcp_tools.spawn_parts.listing import spawn_list, spawn_status
from kiro_crew.mcp_tools.spawn_parts.wave import _SA_ERROR_MAX_CHARS, spawn_sub_agents
from kiro_crew.spawn_label import normalize_spawn_label
from kiro_crew.subagent import (
    AGENT_INTERNAL_CODE,
    AGENT_NOT_AVAILABLE_CODE,
    AGENT_NOT_FOUND_CODE,
    resolve_max_subagents,
)
from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_PHRASE
from kiro_crew.validation import (
    SPAWN_CONTINUE_SCHEMA,
    SPAWN_RELEASE_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_STEER_SCHEMA,
    validate_tool_args,
)

logger = logging.getLogger(__name__)

#: Bound on one ``/api/spawn/lost`` report posted from a deferred's ``cancel()``
#: or ``abandon()``, which the dispatch loop runs on its own thread: a gateway
#: that is not answering must cost that thread one short wait, not one
#: ``_post`` default timeout per unsubmitted task.
_LOST_REPORT_TIMEOUT_SECS = 5.0


def _is_unknown_agent_refusal(resp: Mapping[str, Any], agent: str) -> bool:
    """True when *resp* is the gateway refusing *agent* as a name this wave cannot use.

    Reads the response's machine-readable ``code`` (``AGENT_NOT_FOUND_CODE`` for a
    name it cannot load, ``AGENT_NOT_AVAILABLE_CODE`` for one the parent agent's
    spec forbids, ``AGENT_INTERNAL_CODE`` for one of Kiro Crew's own generated
    specs -- each spelled once in ``subagent`` and imported here), not its prose. The
    refusal text is advisory and free to be reworded; before this it WAS the
    contract, so any rewording silently disabled the wave short-circuit until a
    test caught it.

    The response answers the POST that named *agent*, so no name re-check is
    needed -- pairing is what the old text match had to reconstruct from the
    message. ``agent`` is still required, because an unnamed request means "use
    the default" and can never produce this refusal.

    Fail-soft by construction: a miss reproduces today's behavior (every member is
    dispatched and refused individually), never a refusal of a name the gateway
    would have accepted. That asymmetry is what makes a missing code safe -- a
    client newer than the gateway simply loses the short-circuit -- while using it
    to REJECT a spawn would not be.
    """
    return bool(agent) and resp.get("code") in (
        AGENT_NOT_FOUND_CODE,
        AGENT_NOT_AVAILABLE_CODE,
        AGENT_INTERNAL_CODE,
    )


def _collapse_effort_verdicts(
    pairs: list[tuple[str, tuple[str, str]]],
) -> list[tuple[str, tuple[str, str]]]:
    """Group (subagent id, verdict text) pairs into (id list, verdict text) rows.

    ``reasoning_effort`` and ``model`` are batch-wide, so a wide fan-out
    usually yields the IDENTICAL verdict for every member — rendering it once
    per subagent injects N copies of the same line into the calling agent's
    context. Collapse each group of 2+ ids sharing a verdict into one
    row naming all of them ("a1, a2, a3"); a verdict unique to one subagent
    keeps its own row, so mixed batches keep full per-id attribution. Groups
    preserve first-seen dispatch order, and ids keep their dispatch order
    within a group, so the collapsed output remains deterministic.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for sid, text in pairs:
        grouped.setdefault(text, []).append(sid)
    return [(", ".join(ids), text) for text, ids in grouped.items()]


class _SpawnRunStep(DeferredTool):
    """Submit one task per pool step, then return the background-run receipt."""

    def __init__(
        self,
        *,
        task_count: int,
        agent_ids: list[str],
        transport_errors: list[str],
        submit_one: Callable[[int], None],
        receipt: Callable[[], str],
        reconcile_lost: Callable[[str], bool],
    ) -> None:
        self.task_count = task_count
        self.agent_ids = agent_ids
        self.transport_errors = transport_errors
        self._submit_one = submit_one
        self._receipt = receipt
        self._reconcile_lost = reconcile_lost
        self._submit_index = 0
        self._unsubmitted_closed = False
        self.phase = "submit"
        self.next_due = mcp_core.time.monotonic()

    def _close_unsubmitted(self) -> None:
        """Reconcile every task this call never posted, exactly once.

        Each member index ends in one bucket: accepted, refused (reconciled by
        the submit itself), transport-unknown (left to the stuck-wave sweep), or
        unsubmitted. Every sibling's ``batch_total`` counts the unsubmitted
        ones, so the wave digest holds the accepted children's results until
        each of them is reported lost here. Before the first POST the gateway
        has no wave for this batch, so there is nothing to reconcile and a
        report would only announce failures for a call that spawned nothing.
        """
        if self._unsubmitted_closed or self._submit_index == 0:
            return
        self._unsubmitted_closed = True
        reason = "not submitted: the spawn_run call ended before this task was posted"
        for _ in range(self._submit_index, self.task_count):
            if not self._reconcile_lost(reason):
                # The gateway is not answering: every later report would wait
                # out the same timeout on the loop's thread, and the stuck-wave
                # sweep closes the wave anyway. One bounded attempt is the cost.
                break

    def due_at(self) -> float:
        return self.next_due

    def step(self) -> str | None:
        if self._submit_index < self.task_count:
            self._submit_one(self._submit_index)
            self._submit_index += 1
        self.next_due = mcp_core.time.monotonic()
        if self._submit_index < self.task_count:
            return None
        self.phase = "done"
        return self._receipt()

    def abandon(self) -> str | None:
        remaining = self.task_count - self._submit_index
        self._close_unsubmitted()
        if not self.agent_ids and not self.transport_errors:
            return None
        self.phase = "abandoned"
        return self._receipt() + (
            "\n\nAny accepted subagents were NOT cancelled and keep running "
            "(queued members start once their condition clears). Do not spawn them again. "
            f"{remaining} task(s) were NOT submitted; only those unsubmitted tasks may be "
            "submitted again as a new call. They are reported lost to the wave, so its "
            "digest does not wait on them."
        )

    def cancel(self) -> None:
        # Accepted children keep running and deliver their completion events;
        # the tasks never posted are reported lost so their wave can close.
        self._close_unsubmitted()


def spawn_run(name: str, args: dict[str, Any]) -> str | DeferredTool:
    """Prepare a background batch; its deferred steps submit one task at a time."""
    args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

    tasks = args.get("tasks")
    task = args.get("task")

    # Support both single task and batch tasks. A ``tasks`` entry is a prompt
    # string or an object carrying per-task overrides; both
    # normalise to (prompt, overrides) here so the dispatch loop has one shape.
    task_overrides: list[dict[str, str]] = []
    if tasks and isinstance(tasks, list):
        task_list = []
        for t in tasks:
            if isinstance(t, dict):
                prompt = t.get("task") or ""
                if not prompt.strip():
                    continue
                task_list.append(prompt)
                task_overrides.append({k: v for k, v in t.items() if k != "task" and v})
            elif isinstance(t, str) and t.strip():
                task_list.append(t)
                task_overrides.append({})
    elif task:
        task_list = [task]
        task_overrides = [{}]
    else:
        return "Error: task or tasks is required"

    # Read parent session key so completions inject back into this session.
    parent_session = mcp_core._resolve_session_key()
    if len(task_list) == 1:
        # One-line allowed-only audit: the task count is already known here,
        # and this is the only place a one-task spawn_run call is visible at
        # all now that the solo gate is gone. Kept so a one-task rate can
        # still be measured/re-audited without reviving the removed gate.
        mcp_core.sel().log_api_access(
            caller="internal",
            operation="spawn.solo",
            outcome="allowed",
            source="spawn_run",
            resources=parent_session or "",
        )

    # Fire-and-forget — gateway's SubagentManager queues excess tasks
    # and auto-spawns them as slots free up.
    agent = args.get("agent") or ""
    # The crew this run is DELEGATED to, distinct from `agent`: `agent` names a
    # kiro-cli template, `crew` names a crew member and is what gives the child
    # that crew's memory silo. Read here and forwarded below; the schema accepting
    # the field is not enough, and a field the handler drops makes every documented
    # `spawn_run(crew=...)` a silent no-op that runs on the operator's own memory.
    crew = args.get("crew") or ""
    target_member = args.get("target_member") or ""
    agents_list = args.get("agents") or []
    max_turns = args.get("max_turns") or 0
    cwd = args.get("cwd") or ""
    model = args.get("model") or ""
    reasoning_effort = args.get("reasoning_effort") or ""
    backend = args.get("backend") or ""
    label = normalize_spawn_label(args.get("label"))
    keep = bool(args.get("keep"))
    # Context scope: absent ⇒ true, so a parent that passes nothing gets the
    # same context a normal session would.
    inc_memory = args.get("include_memory", True) is not False
    inc_lessons = args.get("include_lessons", True) is not False
    inc_project = args.get("include_project", True) is not False
    if agents_list and len(agents_list) != len(task_list):
        return (
            f"Error: agents length ({len(agents_list)}) must match tasks length ({len(task_list)})"
        )
    agent_ids: list[str] = []
    can_work = True
    agent_names: list[str] = []
    # (subagent id, reason) pairs from the server's effort verdict — the
    # gateway resolves the effective model (per-call value, else role pin,
    # else unpinned) and reports when the requested effort cannot apply.
    effort_drops: list[tuple[str, tuple[str, str]]] = []
    # (subagent id, note) pairs for the delivery mirror: the resolved model and
    # the family settings key a requested effort is delivered under.
    effort_applies: list[tuple[str, tuple[str, str]]] = []
    # (subagent id, note) pairs for a level only session start can judge: with
    # no model pinned the provider binds it to the model the backend serves.
    effort_pendings: list[tuple[str, tuple[str, str]]] = []
    # (subagent id, note) pairs for a run a pin starts on its own runtime.
    runtime_notes: list[tuple[str, tuple[str, str]]] = []
    agent_tasks: list[str] = []
    # Each accepted member's label, "" where the call gave none.
    agent_labels: list[str] = []
    # subagent id -> the gate's reason, for members the gateway accepted but
    # answered ``status: "queued"`` (deferred, not started).
    queued_reasons: dict[str, str] = {}
    errors: list[str] = []
    transport_errors: list[str] = []
    # Forward this session's own approval_mode (set as an env var at
    # process spawn -- see _cron_extra_env in slack/gateway_runtime/cron_fire.py, mirroring
    # KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) so a cron running with
    # approval_mode="auto" deterministically auto-approves its own
    # spawn_run subagent launches. Without this, SubagentManager.spawn's
    # only route to auto-approve is its own parent_trusted lookup, which
    # requires parent_session to resolve back to the cron's session key
    # -- an identity-plumbing path that can fail silently and leave the
    # spawn stuck on the interactive approval path a cron has no
    # responder for.
    approval_mode = os.environ.get("KIROCREW_APPROVAL_MODE", "")
    # Batch/wave identity: one id per multi-task spawn_run call so the
    # gateway can digest completions (one injection turn per wave instead
    # of N) and emit batch lifecycle events at 60-100-agent scale.
    batch_id = uuid.uuid4().hex[:12] if len(task_list) > 1 else ""

    def _reconcile_lost(reason: str) -> bool:
        """Tell the gateway this member never reached ``mgr.spawn``.

        Every sibling's ``batch_total`` counts it, so an un-reconciled member
        leaves the wave at submitted < expected forever: the digest never closes
        and held sibling results strand until restart. Returns whether the
        report was delivered; the POST is bounded by ``_LOST_REPORT_TIMEOUT_SECS``
        because ``_close_unsubmitted`` runs it from the dispatch loop's thread.
        """
        if not batch_id:
            return True
        try:
            reply = mcp_core._post(
                "/api/spawn/lost",
                {
                    "batch_id": batch_id,
                    "batch_total": len(task_list),
                    "reason": reason[:300],
                    "parent_session": parent_session,
                },
                timeout=_LOST_REPORT_TIMEOUT_SECS,
            )
        except Exception:
            return False  # reaper backstop covers delivery failure
        # ``_post`` answers a transport or HTTP failure as an error dict rather
        # than raising, so the reply decides whether the report landed.
        return not (isinstance(reply, dict) and reply.get("error"))

    # Agent names this wave already learned the gateway refuses as unknown.
    # Re-posting one cannot succeed: the refusal is a property of the NAME, not
    # of the task, so the rest of a wave that shares it is dead on arrival. The
    # observed cost of not knowing that was a whole wave of doomed dispatches on
    # one invented name.
    refused_agents: dict[str, str] = {}

    def _submit_one(i: int) -> None:
        nonlocal can_work
        t = task_list[i]
        over = task_overrides[i]
        a = agents_list[i] if agents_list else agent
        t_model = over.get("model") or model
        t_effort = over.get("reasoning_effort") or reasoning_effort
        t_backend = over.get("backend") or backend
        t_label = normalize_spawn_label(over.get("label")) or label
        if a in refused_agents:
            # Short line on purpose: the full roster is already on the first
            # refusal above, and repeating it once per remaining member would
            # bury it.
            errors.append(
                f"{t[:60]}: not dispatched - agent {a!r} refused above"[:_SA_ERROR_MAX_CHARS]
            )
            _reconcile_lost(refused_agents[a])
            return
        body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
        if crew:
            body["crew"] = crew
        if target_member:
            body["target_member"] = target_member
        if batch_id:
            body["batch_id"] = batch_id
            body["batch_total"] = len(task_list)
        if max_turns:
            body["max_turns"] = max_turns
        if cwd:
            body["cwd"] = cwd
        if t_model:
            body["model"] = t_model
        if t_effort:
            body["reasoning_effort"] = t_effort
        if t_backend:
            body["backend"] = t_backend
        if t_label:
            body["label"] = t_label
        if keep:
            body["keep"] = True
        if not inc_memory:
            body["include_memory"] = False
        if not inc_lessons:
            body["include_lessons"] = False
        if not inc_project:
            body["include_project"] = False
        if approval_mode:
            body["approval_mode"] = approval_mode
        d = mcp_core._post("/api/spawn", body)
        can_work = can_work and d.get("parent_work_supported") is True
        if d.get("error"):
            error_line = f"{t[:60]}: {d['error']}"
            if d.get("install_command"):
                error_line += f" (install: {d['install_command']})"
            error_line = error_line[:_SA_ERROR_MAX_CHARS]
            if d.get("transport_error"):
                # The gateway may have accepted the spawn before the
                # response failed. Treat it as unknown, not rejected, and
                # do not reconcile it as lost (which could close a batch
                # early while the accepted member is still running).
                transport_errors.append(error_line)
                return
            errors.append(error_line)
            if a and _is_unknown_agent_refusal(d, a):
                refused_agents[a] = str(d["error"])
            # Wave-liveness reconcile: every sibling's batch_total counts
            # THIS member,
            # but an explicit pre-spawn rejection never reached mgr.spawn
            # unless the response says "counted". Un-reconciled, the
            # wave's submitted < expected forever — the digest never
            # closes and held sibling results strand until restart.
            # Transport failures are deliberately excluded because their
            # acceptance status is unknown; the stuck-wave reaper is the
            # safe backstop when such a submission was truly lost.
            if not d.get("counted"):
                _reconcile_lost(str(d.get("error", "")))
            return
        agent_ids.append(d.get("id", "?"))
        agent_names.append(a)
        agent_tasks.append(t)
        agent_labels.append(t_label)
        if d.get("status") == "queued":
            # Accepted but DEFERRED by the gate (memory floor, critical posture,
            # adaptive cap at 0): keyed and counted like a started member, but
            # not running, and re-checked on the next eligible pump pass (after
            # the admit wait, once a slot is free and startup is not full). Reported apart
            # below so the caller does not wait for a completion event as if
            # it had started. An older gateway sends no ``status``; a row
            # waiting merely for a slot or the stagger tick sends ``spawned``.
            queued_reasons[str(d.get("id", "?"))] = str(
                d.get("reason_detail") or d.get("reason") or "deferred by the spawn gate"
            )[:_SA_ERROR_MAX_CHARS]
        if d.get("effort_dropped"):
            effort_drops.append((str(d.get("id", "?")), (t_effort, str(d["effort_dropped"]))))
        if d.get("effort_applied"):
            effort_applies.append((str(d.get("id", "?")), (t_effort, str(d["effort_applied"]))))
        if d.get("effort_pending"):
            effort_pendings.append((str(d.get("id", "?")), (t_effort, str(d["effort_pending"]))))
        if d.get("runtime_note"):
            runtime_notes.append((str(d.get("id", "?")), ("", str(d["runtime_note"]))))

    def _receipt() -> str:
        return _spawn_run_receipt(
            agent_ids=agent_ids,
            agent_names=agent_names,
            agent_tasks=agent_tasks,
            agent_labels=agent_labels,
            effort_drops=effort_drops,
            effort_applies=effort_applies,
            effort_pendings=effort_pendings,
            runtime_notes=runtime_notes,
            queued_reasons=queued_reasons,
            errors=errors,
            transport_errors=transport_errors,
            parent_session=parent_session,
            can_work=can_work,
            keep=keep,
        )

    return _SpawnRunStep(
        task_count=len(task_list),
        agent_ids=agent_ids,
        transport_errors=transport_errors,
        submit_one=_submit_one,
        receipt=_receipt,
        reconcile_lost=_reconcile_lost,
    )


def _spawn_run_receipt(
    *,
    agent_ids: list[str],
    agent_names: list[str],
    agent_tasks: list[str],
    effort_drops: list[tuple[str, tuple[str, str]]],
    effort_applies: list[tuple[str, tuple[str, str]]],
    effort_pendings: list[tuple[str, tuple[str, str]]],
    queued_reasons: dict[str, str],
    errors: list[str],
    transport_errors: list[str],
    parent_session: str,
    can_work: bool,
    keep: bool,
    agent_labels: list[str] | None = None,
    runtime_notes: list[tuple[str, tuple[str, str]]] | None = None,
) -> str:
    """Render the accumulated background-run receipt without gateway requests."""
    spawn_lines: list[str] = []
    # Server-computed effort verdicts (never a rejection — gated on agent_ids
    # so a total-failure result keeps its "Error:" first line, which SEL and
    # callers test as a prefix). The gateway resolves the effective model
    # (per-call value, else the subagent role pin, else unpinned/"auto") at
    # accept time, so — unlike the old client-side check — this also reports
    # the default case where no per-call model was passed and the effort
    # would otherwise be dropped silently.
    if agent_ids:
        # A verdict is keyed by (effort, text): a tasks[] object may override
        # the batch-wide level, so each line names the level its tasks asked for.
        for drop_ids, (lvl, drop_reason) in _collapse_effort_verdicts(effort_drops):
            spawn_lines.append(f"ℹ reasoning_effort='{lvl}' dropped for {drop_ids}: {drop_reason}")
        for applied_ids, (lvl, applied_note) in _collapse_effort_verdicts(effort_applies):
            spawn_lines.append(
                f"✓ reasoning_effort='{lvl}' applied for {applied_ids} ({applied_note})"
            )
        # Neither a ✓ nor "dropped": the outcome is decided after this result.
        for pending_ids, (lvl, pending_note) in _collapse_effort_verdicts(effort_pendings):
            spawn_lines.append(f"ℹ reasoning_effort='{lvl}' for {pending_ids}: {pending_note}")
        # Memory cost of a pin: it is the user's setting, not the call's, that
        # usually carries it, so the receipt is where the caller first sees it.
        for note_ids, (_unused, note) in _collapse_effort_verdicts(runtime_notes or []):
            spawn_lines.append(f"ℹ {note_ids}: {note}")
    if not parent_session and agent_ids:
        # Orphan alert: without a parent session key the subagents cannot
        # deliver completion events back to this conversation and will
        # not appear in the Subagents panel for this session. This
        # fails silently — say it
        # loudly so the agent/user can fall back to spawn_list +
        # result.txt polling instead of waiting forever.
        spawn_lines.append(
            "⚠ parent_session UNRESOLVED — these subagents are orphaned: "
            "completion events will NOT arrive in this conversation. "
            "Poll spawn_list and read ~/.kiro/crew/subagents/<id>/result.txt "
            "instead. (Identity plumbing issue — check KIROCREW_HOST_PID / "
            "session_pid / claim-push.)"
        )
    if agent_ids:
        # A member's line names it by its label when the call gave one, else by
        # the start of its task text.
        labels = agent_labels or [""] * len(agent_tasks)
        shown = [lbl or t[:80] for t, lbl in zip(agent_tasks, labels)]
        members = list(zip(agent_ids, agent_names, shown))
        started = [m for m in members if m[0] not in queued_reasons]
        deferred = [m for m in members if m[0] in queued_reasons]
        if started:
            if parent_session:
                spawn_lines.append(
                    f"Spawned {len(started)} subagent(s). Results will arrive as completion events:"
                )
            else:
                # Orphaned (warning above): completion events cannot be
                # delivered — do not promise them in the same breath.
                spawn_lines.append(
                    f"Spawned {len(started)} subagent(s). Monitor results via polling:"
                )
            for aid, a, t in started:
                label = f"{aid} ({a})" if a else aid
                spawn_lines.append(f"  {label}: {t[:80]}")
        if deferred:
            # One gate verdict covers the wave (memory is host-wide), so the
            # first member's reason heads the block; a member whose reason
            # differs is annotated on its own line. The header keeps the
            # ``N subagent(s).`` shape of the Spawned line on purpose: the
            # dashboard's inline run card recognises a launch by that marker
            # and reads the ``  <id> (<agent>): <task>`` lines that follow, so
            # a queued-only wave still gets its card (which is what shows the
            # queued count and, with the event's reason, why it waits).
            head_reason = queued_reasons[deferred[0][0]]
            spawn_lines.append(
                f"Queued {len(deferred)} subagent(s). Not started yet: {head_reason}. "
                "The gateway re-checks them and starts each one once the condition "
                "clears; only then does its result arrive:"
            )
            for aid, a, t in deferred:
                label = f"{aid} ({a})" if a else aid
                note = "" if queued_reasons[aid] == head_reason else f" [{queued_reasons[aid]}]"
                spawn_lines.append(f"  {label}: {t[:80]}{note}")
        if keep:
            spawn_lines.append(
                "These conversations have GUARANTEED continuability: after "
                "completion, use spawn_continue(conversation=<id>, task=...) "
                "for follow-up work with full context, and "
                "spawn_release(conversation=<id>) when the workstream is done."
            )
    if errors:
        if agent_ids:
            spawn_lines.append(f"\n❌ {len(errors)} task(s) failed to start:")
        elif transport_errors:
            # No confirmed starts: retain the Error prefix used by SEL and
            # callers even though other submissions remain uncertain.
            spawn_lines.append(f"Error: {len(errors)} task(s) failed to start:")
        else:
            spawn_lines.append(
                f"Error: {len(errors)} task(s) failed to start; "
                "none of the requested subagents were started:"
            )
        for e in errors:
            spawn_lines.append(f"  - {e}")
    if transport_errors:
        if agent_ids or errors:
            spawn_lines.append(
                f"\n⚠ {len(transport_errors)} task(s) have unknown acceptance status:"
            )
        else:
            spawn_lines.append(
                f"Error: acceptance status is unknown for " f"{len(transport_errors)} task(s):"
            )
        for e in transport_errors:
            spawn_lines.append(f"  - {e}")
        guidance = (
            "The gateway may have accepted these tasks before the response failed. "
            "Do not retry automatically. Check spawn_list"
        )
        if parent_session:
            guidance += " and wait for completion events"
        guidance += (
            ". spawn_list lists queued spawns too, but a submission still in flight "
            "may not be listed yet; wait and recheck before retrying to avoid "
            "duplicate work."
        )
        spawn_lines.append(guidance)
    if agent_ids:
        if parent_session:
            if can_work:
                spawn_lines.append(
                    "\nAdvance only your ready, non-overlapping parent work for a short bounded "
                    "step (at most one minute), then END YOUR TURN so queued completion events "
                    "can be delivered. If no such work remains, END YOUR TURN now. "
                    "Do not poll, duplicate child work, or claim the task is complete."
                )
            else:
                spawn_lines.append(
                    "\nEND YOUR TURN now: this caller has no confirmed parent-work delivery "
                    "boundary. Wait for the [Subagent completion event] messages. "
                    "Dispatch is not completion."
                )
        else:
            spawn_lines.append(
                "\nDo NOT wait for completion events — poll spawn_list and read "
                "result.txt files instead."
            )
    elif not errors and not transport_errors:
        # Defensive fallback: every non-empty task list should produce an
        # id or an error, but never imply work was accepted if neither did.
        spawn_lines.append("Error: no subagents were started.")
    return "\n".join(spawn_lines)


def spawn_continue(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_CONTINUE_SCHEMA)
    conv = (args.get("conversation") or "").strip()
    task = (args.get("task") or "").strip()
    if not conv or not task:
        return "Error: conversation and task are required"
    parent_session = mcp_core._resolve_session_key()
    body = {"task": task, "parent_session": parent_session}
    if args.get("agent"):
        body["agent"] = args["agent"]
    if args.get("model"):
        body["model"] = args["model"]
    if args.get("max_turns"):
        body["max_turns"] = args["max_turns"]
    d = mcp_core._post(f"/api/spawn/{conv}/continue", body)
    if d.get("error"):
        return f"Error: {d['error']}"
    return (
        f"Continued conversation {conv} as run {d.get('id', '?')}. "
        "The result will arrive as a [Subagent completion event] — "
        "END YOUR TURN and wait for it."
    )


def spawn_steer(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_STEER_SCHEMA)
    agent_id = (args.get("agent_id") or "").strip()
    message = (args.get("message") or "").strip()
    mode = (args.get("mode") or "interrupt").strip()
    if not agent_id or not message:
        return "Error: agent_id and message are required"
    d = mcp_core._post(f"/api/spawn/{agent_id}/steer", {"message": message, "mode": mode})
    if d.get("error"):
        return f"Error: {d['error']}"
    if mode == "follow_up":
        return (
            f"Queued follow-up for run {agent_id}: it will be delivered as "
            "a continuation on the run's conversation after its current "
            "turn completes. The continuation's result arrives as a "
            "separate [Subagent completion event] — after this run's own."
        )
    return (
        f"Steered run {agent_id}: the message was injected into its "
        "running turn. Its completion event will reflect the correction."
    )


def spawn_release(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_RELEASE_SCHEMA)
    conv = (args.get("conversation") or "").strip()
    if not conv:
        return "Error: conversation is required"
    d = mcp_core._post(f"/api/spawn/{conv}/release", {})
    if d.get("error"):
        return f"Error: {d['error']}"
    if d.get("status") == "cancelled":
        return (
            f"Cancelled run {conv} before it started: it will not start. Its stop "
            "arrives as a [Subagent completion event]; do not spawn it again unless "
            "you still need the work."
        )
    if d.get("status") == "not_disposed":
        return (
            f"Not disposed: conversation {conv} was dropped from Crew's list, but its "
            "backend keeps its own session store and that session was not deleted. "
            "A later spawn_continue may still resume it."
        )
    return f"Released conversation {conv} — it can no longer be continued."


def _live_adaptive_state() -> dict[str, Any] | None:
    """The adaptive controller's state, however this process can reach it.

    In the GATEWAY process the controller is a module-level registry read. A
    tool server is a DIFFERENT process, so that registry is empty there and the
    answer has to come over the loopback API: without it the only number this
    tool can print is the configured ceiling (``max_subagents``), while the cap
    actually in force may be 1. Returns None when neither path answers; the
    caller then labels what it prints as a ceiling.

    ``/api/spawn/adaptive`` and NOT ``/api/tasks/summary``, which carries the
    same object: the ``/api/spawn`` prefix is on ``_MIXED_INTERNAL_API_PATHS``,
    so a sibling route under it is reachable with the internal secret this
    process holds, while the summary route is in neither internal bucket (it
    also lists per-task session keys and lease owners). Reading it there would
    403 and leave the ceiling printed -- the defect this exists to fix.

    Every failure is logged at debug. A silent degrade to the ceiling is exactly
    what went unnoticed before, so the one line that explains it has to exist.
    """
    state = host_status.adaptive_state()
    if state:
        return state
    try:
        payload = mcp_core._get("/api/spawn/adaptive", timeout=5)
    except Exception:
        logger.debug("adaptive state read failed in transport", exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        logger.debug(
            "adaptive state read refused: %s",
            payload.get("error") if isinstance(payload, dict) else type(payload).__name__,
        )
        return None
    remote = payload.get("adaptive")
    if isinstance(remote, dict) and remote:
        return remote
    logger.debug("adaptive state read returned no controller state")
    return None


def resource_status(name: str, args: dict[str, Any]) -> str:
    rstatus = host_status.probe()
    out = rstatus.summary_lines()
    # ``summary_lines`` already appended the adaptive block when this process
    # OWNS the controller. It does not in a tool server, so fetch it and render
    # it with the same helper rather than a second formatting of the same facts.
    have_block = any(line.startswith("Adaptive concurrency") for line in out)
    state = None if have_block else _live_adaptive_state()
    if state:
        out.extend(host_status.adaptive_summary_lines(state))
    elif not have_block:
        try:
            cap = resolve_max_subagents(KiroCrewConfig.load())
        except Exception:
            cap = 0
        if cap > 0:
            out.append(
                f"  Sub-agent ceiling: {cap} (configured max; the cap actually in "
                "force could not be read from the gateway)"
            )
    if rstatus.posture == "critical":
        out.append(
            "\nGuidance: memory is critically low — do NOT start heavy work "
            "(full suites, large builds, big sub-agent waves) now; run only "
            "light steps or wait for memory to free."
        )
    elif rstatus.posture == "tight":
        out.append(
            "\nGuidance: memory is tight — prefer the lighter path (targeted "
            "tests, fewer sub-agents, deferred builds) for heavy work."
        )
    elif rstatus.memory_pressure_held:
        # The posture is figure-based and can read AMPLE while the macOS kernel
        # reports pressure; the AMPLE guidance would then contradict the gate.
        # The queue note itself is on the "Kernel memory pressure" line above.
        out.append(
            f"\nGuidance: {MEMORY_PRESSURE_PHRASE} — prefer the lighter path for heavy work."
        )
    elif rstatus.posture == "ample":
        out.append(
            "\nGuidance: ample headroom right now — nothing is reserved, "
            "so it can change before you act."
        )
    else:
        out.append(
            "\nGuidance: headroom could not be measured on this host — "
            "proceed with normal caution."
        )
    return "\n".join(out)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], "str | DeferredTool"]] = {
    "spawn_run": spawn_run,
    "spawn_continue": spawn_continue,
    "spawn_steer": spawn_steer,
    "spawn_release": spawn_release,
    "spawn_list": spawn_list,
    "spawn_status": spawn_status,
    "spawn_sub_agents": spawn_sub_agents,
    "resource_status": resource_status,
}


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.mcp_tools.spawn.<name>`` stays the import path and the patch
# surface for every name they define: a patch through this module reaches every owner
# that binds the name (see :mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its responsibility:
#
#   ``spawn_parts.schemas`` -- every spawn descriptor and the agent roster they name.
#   ``spawn_parts.listing`` -- spawn_list, spawn_status and the queued-wait wording.
#   ``spawn_parts.wave`` -- spawn_sub_agents: submit a batch, wait for it off the MCP worker.
_OWNERS = (
    "kiro_crew.mcp_tools.spawn_parts.schemas",
    "kiro_crew.mcp_tools.spawn_parts.listing",
    "kiro_crew.mcp_tools.spawn_parts.wave",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_tools.spawn_parts.listing import _queued_why  # noqa: F401
    from kiro_crew.mcp_tools.spawn_parts.schemas import (  # noqa: F401
        _MAX_ROSTER_NAMES,
        _agent_roster_hint,
        _parent_allowlist_filter,
        _parent_template_for_roster,
        schemas,
    )
    from kiro_crew.mcp_tools.spawn_parts.wave import (  # noqa: F401
        _OWNER_UNRESOLVED_PREFIX,
        RESUME_HOLD_SECS,
        _audit_owner,
        _held_by_a_deferral,
        _ResumeHold,
        _SubAgentsStep,
    )
if TYPE_CHECKING:  # names the owners import from elsewhere in kiro_crew, still served here
    from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS, NO_TOOL_CALLS_NOTE  # noqa: F401
    from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS  # noqa: F401
    from kiro_crew.execution_context import read_session_execution  # noqa: F401
    from kiro_crew.platform import redact_via_context as redact  # noqa: F401
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F401
    from kiro_crew.spawn_label import SPAWN_LABEL_MAX_CHARS  # noqa: F401
    from kiro_crew.subagent import (  # noqa: F401
        agent_matches_allowlist,
        format_subagent_usage,
        is_internal_agent_spec,
        parent_spawn_allowlists,
        visible_agent_names,
    )
    from kiro_crew.subagent_persistence import agent_dir_for_display  # noqa: F401
    from kiro_crew.subagent_wait_reasons import queued_wait_text  # noqa: F401
    from kiro_crew.validation import (  # noqa: F401
        MAX_MEDIUM_STRING,
        MAX_SHORT_STRING,
        SPAWN_SUB_AGENTS_SCHEMA,
    )
