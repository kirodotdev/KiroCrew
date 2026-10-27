"""The batch spawn: ``spawn_sub_agents`` and the deferred step that waits for its members.

``spawn_sub_agents`` validates a batch and hands the dispatch loop a
:class:`_SubAgentsStep`, which submits one member per step, polls the children, holds for
the parent's slot (:class:`_ResumeHold`) and builds the reply, so a large batch never holds
the shared MCP worker. The audit owner and the per-entry refusal bound it records live here
too.

This module is an owner of :mod:`kiro_crew.mcp_tools.spawn`, which serves every name here
and stays the import path and the patch seam: a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.constants import LEAKED_TOOL_CALL_NOTE, NO_TOOL_CALLS_NOTE
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.mcp_tools.spawn_parts.listing import _queued_why
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.subagent_persistence import agent_dir_for_display
from kiro_crew.validation import (
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    SPAWN_SUB_AGENTS_SCHEMA,
    validate_tool_args,
)

# A parked batch retains one refusal per entry, each bounded to this many
# characters: room for the 60-char task prefix, an ``agent_not_found`` refusal
# carrying its full bounded roster (``_MAX_AVAILABLE_IN_ERROR`` names) and the
# ``(+N more, call spawn_list)`` pointer, which the model needs to correct itself.
_SA_ERROR_MAX_CHARS = 800


# Owner recorded on an audit record when the resolver named no session. An empty
# owner is ambiguous by construction: a resolver whose every identity source
# failed and a spawn with genuinely no owning session both produce ``""``, and
# nothing downstream can recover which one it was. This marker names the failed
# resolution where it happens, so the audit trail says "the owner was lost"
# rather than naming no session at all.
#
# Same ``unresolved:<pid>`` wire format the computer-use shim already writes
# (``mcp_computer.UNRESOLVED_SESSION_PREFIX``), so one audit-reader vocabulary
# covers both producers. Deliberately NOT trustworthy attribution -- the prefix
# says it is not, so a reader cannot mistake a pid for a session identity.
_OWNER_UNRESOLVED_PREFIX = "unresolved:"


def _audit_owner(parent_session: str) -> str:
    """The owner to record on an audit record for *parent_session*.

    A resolved key is recorded verbatim. An empty one becomes the unresolved
    marker for THIS process, read at call time so a forked child cannot report
    its parent's pid.

    Audit-only. The spawn REQUEST keeps the empty owner, because
    ``parent_session_key`` addresses per-slot frame delivery and a synthetic key
    there would route frames at a slot that does not exist.
    """
    if parent_session:
        return parent_session
    return f"{_OWNER_UNRESOLVED_PREFIX}{os.getpid()}"


def _held_by_a_deferral(record: Mapping[str, Any]) -> bool:
    """A queued, not-started member the gate DEFERRED (not one waiting for a slot).

    Read off the row's OWN deferral sentence (``reason_detail``), which the
    gateway sends only while that deferral is in force. ``reason`` is the
    parent's wait label, last writer wins, so a sibling's deferral would
    relabel a member that only waits behind the cap.
    """
    return (
        record.get("queued") is True
        and record.get("resuming") is not True
        and bool(str(record.get("reason_detail") or "").strip())
    )


#: Server-side hold per resume-poll request (seconds); under the client GET
#: timeout so a held request never reads as a hang.
RESUME_HOLD_SECS = 8.0


class _ResumeHold:
    """Wait, one probe at a time, for the subagent parent's slot to be granted back.

    Only a ``subagent:<id>`` parent has a lane slot to wait for; a chat-turn
    parent is done at once. The gateway answers ``known=False`` for a run it
    does not hold (finished, other incarnation), which also releases the hold.
    Split into probes so the deferred ``spawn_sub_agents`` can take one per
    step: each probe is one server-held request of at most ``RESUME_HOLD_SECS``,
    never the whole wait. The deadline passing with the slot still not granted
    yields a note for the tool result.
    """

    def __init__(self, parent_session: str, deadline: float) -> None:
        self.parent_id = ""
        if parent_session.startswith("subagent:"):
            self.parent_id = parent_session[len("subagent:") :]
        self.deadline = deadline
        self.held = False

    def probe(self) -> tuple[bool, dict[str, Any] | None]:
        """``(done, note)``: ``done`` False means ask again; ``note`` is the
        result note when the deadline passed with the slot still not granted."""
        if not self.parent_id:
            return True, None
        remaining = self.deadline - mcp_core.time.monotonic()
        if remaining <= 0:
            if not self.held:
                # The deadline was already spent on the children (still_running
                # is reported for them); nothing about the slot was observed.
                return True, None
            return True, {
                "status": "resume_pending",
                "parent": self.parent_id,
                "note": (
                    "The children finished but this run's execution slot was not granted "
                    "back before the wait deadline; it re-enters through admission by "
                    "capacity."
                ),
            }
        hold = max(0.0, min(RESUME_HOLD_SECS, remaining))
        try:
            st = mcp_core._get(f"/api/spawn/{self.parent_id}/resume?wait_secs={hold:.1f}")
        except Exception:
            return True, None  # a gateway that cannot answer is not a reason to hold
        if not isinstance(st, dict) or st.get("error"):
            return True, None
        if st.get("known") is not True or st.get("granted") is not False:
            return True, None
        # ``granted`` False with the hold consumed: the pump has not reached
        # this parent yet; ask again (the request itself was the wait).
        self.held = True
        return False, None


def spawn_sub_agents(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    """Spawn the agents, then wait for them without holding the MCP worker.

    Submission, polling the children, the parent's slot grant and collection
    are :class:`_SubAgentsStep` phases the dispatch loop times. Each submission
    posts one member, so a large batch never holds the shared worker for its
    cumulative submission time (see ``mcp_shared.DeferredTool``).
    """
    args = validate_tool_args(args, SPAWN_SUB_AGENTS_SCHEMA)
    agents_input = args.get("agents")
    if not agents_input or not isinstance(agents_input, list):
        return "Error: 'agents' array is required"
    cwd = args.get("cwd") or ""
    # Context scope: batch-wide, absent ⇒ true (same rule as spawn_run).
    sa_groups = {
        k: False
        for k in ("include_memory", "include_lessons", "include_project")
        if args.get(k, True) is False
    }
    parent_session = mcp_core._resolve_session_key()

    def _redact_sa(text: str) -> str:
        return redact(text)

    def _inline_result(aid: str, raw: str) -> str:
        """A finished child's retained output as this reply carries it.

        Redacted, and past ``COMPLETION_KEEP_DEFAULT_CHARS`` replaced by the same
        summary plus disk path that ``spawn_run`` gives, so a large transcript
        does not fill the caller's context window. One rule for a child that
        completed and one that ended in error.
        """
        result_text = _redact_sa(raw)
        if len(result_text) > COMPLETION_KEEP_DEFAULT_CHARS:
            try:
                result_path = str(agent_dir_for_display(aid) / "result.txt")
            except (ValueError, OSError):
                result_path = ""
            if result_path:
                result_text = mcp_core.summarize_result(result_text, result_path)
        return result_text

    # Validate individual agent entries (schema guarantees dict entries)
    for entry in agents_input:
        p = entry.get("prompt", "")
        if not isinstance(p, str):
            return "Error: each agents[] member's 'prompt' must be a string"
        if len(p) > MAX_MEDIUM_STRING:
            entry["prompt"] = p[:MAX_MEDIUM_STRING]
        a = entry.get("agent_or_mode") or ""
        if not isinstance(a, str):
            # A non-string here is unbounded by the length clamp below and
            # would be retained by the parked step until submission.
            return "Error: each agents[] member's 'agent_or_mode' must be a string"
        if len(a) > MAX_SHORT_STRING:
            entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

    # One-line allowed-only audit, mirroring spawn_run: a one-task call is only
    # visible here now that the solo gate is gone, so log it to keep the
    # one-task rate measurable without reviving the removed gate.
    if sum(1 for e in agents_input if e.get("prompt", "").strip()) == 1:
        mcp_core.sel().log_api_access(
            caller="internal",
            operation="spawn.solo",
            outcome="allowed",
            source="spawn_sub_agents",
            resources=parent_session or "",
        )

    mcp_core.sel().log_tool_invocation(
        session_key=_audit_owner(parent_session),
        source="mcp_core",
        tool_name="spawn_sub_agents",
        outcome="attempt",
        metadata={"agent_count": len(agents_input)},
    )

    # The parked step retains its members until each is submitted, so it keeps
    # only the two clamped fields it reads; any other key a caller sent is
    # dropped here, as the blocking tool ignored it.
    pending = [
        {"prompt": entry["prompt"], "agent_or_mode": entry.get("agent_or_mode") or ""}
        for entry in agents_input
        if entry.get("prompt", "").strip()
    ]

    try:
        max_wait = float(os.environ.get("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
    except (TypeError, ValueError):
        max_wait = 7200.0
    max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
    return _SubAgentsStep(
        sa_ids=[],
        sa_deferred=set(),
        sa_errors=[],
        parent_session=parent_session,
        max_wait=max_wait,
        inline_result=_inline_result,
        agents_input=pending,
        sa_groups=sa_groups,
        cwd=cwd,
    )


class _SubAgentsStep(DeferredTool):
    """Submit the batch, then poll, hold and collect without owning the worker.

    One submission step posts one member. Later steps poll the children, probe
    the parent's slot (``hold``), or build the final reply (``collect``).
    """

    #: Seconds between polls of the children.
    POLL_INTERVAL = 2.0
    #: Seconds between session keepalives while polling. Pinging
    #: ``/api/session-keepalive`` keeps the gateway's ``is_responsive()`` from
    #: flagging this session as stale and SIGTERMing the ACP subprocess
    #: mid-wait, which would abort the very sub-agents being waited on.
    KEEPALIVE_SECS = 60.0

    def __init__(
        self,
        *,
        sa_ids: list[str],
        sa_deferred: set[str],
        sa_errors: list[str],
        parent_session: str,
        max_wait: float,
        inline_result: Callable[[str, str], str],
        agents_input: list[dict[str, Any]] | None = None,
        sa_groups: dict[str, bool] | None = None,
        cwd: str = "",
    ) -> None:
        self.agents_input = agents_input or []
        self.sa_groups = sa_groups or {}
        self.cwd = cwd
        self._submit_index = 0
        self.sa_ids = sa_ids
        self.sa_deferred = sa_deferred
        self.sa_errors = sa_errors
        # Members whose POST failed in transport: the gateway may have accepted
        # them, so they are neither ids nor plain refusals, and a retry could
        # run them twice. Bounded like ``sa_errors``.
        self.sa_unknown: list[str] = []
        self.parent_session = parent_session
        self.max_wait = max_wait
        self._inline_result = inline_result
        now = mcp_core.time.monotonic()
        self.deadline = now + max_wait
        self.next_ping = now + self.KEEPALIVE_SECS  # first keepalive after 60s, not immediately
        self.next_due = now
        self.wait_settled: set[str] = set()
        self.phase = "submit" if agents_input is not None else "poll"
        self.hold = _ResumeHold(parent_session, self.deadline)
        self.resume_note: dict[str, Any] | None = None
        self._settled_ids: set[str] = set()
        # Per-child tallies the collect step computes for this call's own SEL
        # row (``outcome`` and ``metadata``), written at settle. ``None`` until a
        # collect ran: a batch the gateway refused outright settles from the
        # submit phase with an error text and gets no outcome row, as the
        # blocking tool returned before writing one.
        self._counts: dict[str, int] | None = None

    def due_at(self) -> float:
        return self.next_due

    def abandon(self) -> str | None:
        """This wait cannot be continued -- the serving process is exiting,
        the parked table is full, or the step pool could not start -- and the
        loop will never step it again. The children were already spawned and
        keep running, so "retry the call" would run them twice: answer with
        their ids instead, worded without naming a cause this step cannot
        see. Nothing is marked collected, so each child's completion event
        still arrives. With no child accepted yet AND no member whose POST
        failed in transport (acceptance unknown) -- turned away before its
        first member was submitted, or every member plainly refused -- nothing
        runs, so the whole call may safely be retried: ``None`` keeps the
        loop's retryable refusal. A member of unknown acceptance is reported
        as such, since resubmitting it could run it twice."""
        if not self.sa_ids and not self.sa_unknown:
            return None
        self.phase = "abandoned"
        sa_results = [
            json.dumps(
                {
                    "status": "still_running",
                    "task_ids": list(self.sa_ids),
                    "query": "spawn_status/spawn_list",
                    "note": (
                        "This wait could not be continued; these sub-agents were already "
                        "spawned, were NOT cancelled and keep running. Do not "
                        "spawn them again. Their [Subagent completion event] messages still "
                        "arrive; poll spawn_list or spawn_status for progress."
                    ),
                }
            )
        ]
        remaining = len(self.agents_input) - self._submit_index
        if remaining:
            sa_results.append(
                json.dumps(
                    {
                        "status": "not_submitted",
                        "count": remaining,
                        "note": (
                            f"{remaining} batch member(s) were NOT submitted or spawned. "
                            "Only these unsubmitted members may be submitted again."
                        ),
                    }
                )
            )
        if self.sa_unknown:
            sa_results.append(
                json.dumps(
                    {
                        "status": "unknown_acceptance",
                        "count": len(self.sa_unknown),
                        "members": self.sa_unknown,
                        "note": (
                            "These members' spawn requests failed in transport, so the "
                            "gateway may or may not have accepted them. Do not resubmit "
                            "them blindly; check spawn_list first."
                        ),
                    }
                )
            )
        if self.sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": self.sa_errors}))
        return "\n\n".join(sa_results)

    def _submit_one(self) -> str | None:
        if self._submit_index < len(self.agents_input):
            entry = self.agents_input[self._submit_index]
            prompt = entry["prompt"].strip()
            body = {
                "task": prompt,
                "agent": entry.get("agent_or_mode") or "",
                "parent_session": self.parent_session,
                **self.sa_groups,
            }
            if self.cwd:
                body["cwd"] = self.cwd
            data = mcp_core._post("/api/spawn", body)
            self._submit_index += 1
            if data.get("transport_error"):
                line = f"{redact(prompt)[:60]}: {redact(str(data.get('error', '')))}"
                self.sa_unknown.append(line[:_SA_ERROR_MAX_CHARS])
                self.sa_errors.append(line[:_SA_ERROR_MAX_CHARS])
            elif data.get("error"):
                self.sa_errors.append(
                    f"{redact(prompt)[:60]}: {redact(data['error'])}"[:_SA_ERROR_MAX_CHARS]
                )
            elif data.get("id"):
                aid = data["id"]
                self.sa_ids.append(aid)
                if data.get("status") == "queued":
                    self.sa_deferred.add(aid)
            else:
                self.sa_errors.append(
                    f"{redact(prompt)[:60]}: spawn returned no agent id"[:_SA_ERROR_MAX_CHARS]
                )
        now = mcp_core.time.monotonic()
        self.next_due = now
        if self._submit_index < len(self.agents_input):
            return None
        if not self.sa_ids and self.sa_errors:
            return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in self.sa_errors)
        if not self.sa_ids:
            return "Error: no valid agent entries found in 'agents' array"
        # Submission time is not part of the children's blocking wait budget.
        self.deadline = now + self.max_wait
        self.next_ping = now + self.KEEPALIVE_SECS
        self.hold = _ResumeHold(self.parent_session, self.deadline)
        self.phase = "poll"
        # Every member is posted: a parked, polling step retains no prompts.
        self.agents_input = []
        self._submit_index = 0
        return None

    def step(self) -> str | None:
        if self.phase == "submit":
            return self._submit_one()
        if self.phase == "poll":
            self._poll_once()
            return None
        if self.phase == "hold":
            # Hold the result until the PARENT holds its execution slot again. A
            # subagent parent blocked here yielded its lane slot
            # (waiting_children); its last child ending wakes it in the store,
            # but the slot comes back through admission's pump by capacity.
            # Handing the result over before the grant would let the parent run
            # on without a slot. Event-driven: each request is held server-side
            # until the grant or its bound, so a grant is seen at once. Bounded
            # by the same deadline; on expiry the results are still returned,
            # with the pending resume named.
            done, self.resume_note = self.hold.probe()
            if done:
                self.phase = "collect"
            self.next_due = mcp_core.time.monotonic()
            return None
        return self._collect()

    def _poll_once(self) -> None:
        now = mcp_core.time.monotonic()
        if now >= self.deadline:
            self.phase = "hold"
            return
        if now >= self.next_ping:
            try:
                mcp_core._post("/api/session-keepalive", {})
            except Exception:
                pass  # keepalive is best-effort
            self.next_ping = now + self.KEEPALIVE_SECS
        # Settled for this wait, and not polled again: done, or held by a gate
        # DEFERRAL -- that can wait far longer than this call should hold the
        # parent's turn, and its completion event arrives on its own later. A
        # member queued only behind the concurrency cap is NOT settled: it
        # drains with the wave itself, and the tool's contract is to return its
        # result. An error settles only THIS pass (an agent that never sets
        # done=True would otherwise spin the loop until max_wait), since it can
        # be one failed poll of a member that is still running.
        all_settled = True
        for aid in [a for a in self.sa_ids if a not in self.wait_settled]:
            sa_st = mcp_core._get(f"/api/spawn/{aid}")
            if sa_st.get("done") or _held_by_a_deferral(sa_st):
                self.wait_settled.add(aid)
            elif not sa_st.get("error"):
                all_settled = False
                break
        if all_settled:
            self.phase = "hold"
            self.next_due = mcp_core.time.monotonic()
            return
        self.next_due = mcp_core.time.monotonic() + self.POLL_INTERVAL

    def _collect(self) -> str:
        # Collect results
        sa_results: list[str] = []
        _settled_ids: set[str] = set()  # agents confirmed settled (done or error)
        # Children the wait ended on, with the state each was last seen in. The
        # wait expiring is a fact about THIS call, not about them: they keep their
        # own execution budget, are never cancelled here, and their completion
        # events still arrive, so the caller is told how to keep following them
        # rather than told they failed.
        _unsettled: dict[str, str] = {}
        # Members accepted and still not started when the wait ended (gate-deferred,
        # or waiting for a slot), with the reason they wait. Each is reported ONCE,
        # in the ``queued`` record below, and never as an error: a caller that sees
        # an error for accepted work dispatches it again, and then it runs twice.
        never_started: dict[str, str] = {}
        completed = still_running = errored = 0
        for aid in self.sa_ids:
            sa_st = mcp_core._get(f"/api/spawn/{aid}")
            sa_name = redact(sa_st.get("agent", ""))
            label = sa_name if sa_name else aid
            if not sa_st.get("done") and sa_st.get("queued") is True:
                still_running += 1
                if sa_st.get("resuming") is True:
                    # It ran before and waits to go on: running work, not unstarted.
                    _unsettled[aid] = "waiting_to_resume"
                else:
                    never_started[aid] = _queued_why(sa_st)
                continue
            if sa_st.get("error"):
                errored += 1
                # Only mark as settled if done is also true (confirmed terminal
                # state). An "error" without "done" could be a transport failure
                # from _get() — the agent may still be running.
                if sa_st.get("done"):
                    _settled_ids.add(aid)
                failure: dict[str, Any] = {
                    "agent": label,
                    "status": "error",
                    "error": redact(sa_st["error"]),
                }
                if sa_st.get("done"):
                    # On a finished run ``error`` is why it ended badly, sent next to
                    # the output the run retained (a timed-out run keeps its partial
                    # work). This call marks the child collected, so its completion
                    # event is not injected either: the reply is where that output
                    # reaches the caller.
                    failure["text"] = self._inline_result(aid, sa_st.get("result", ""))
                # The hint goes on a deferred child that is not done, and on any child
                # whose answer carries no ``done`` at all: that is the error _get()
                # returns when the gateway does not answer, so no row was read and the
                # child, accepted at spawn time, may still be running.
                if not sa_st.get("done") and (aid in self.sa_deferred or "done" not in sa_st):
                    failure["hint"] = (
                        "accepted at spawn time; its state couldn't be read now; "
                        "check spawn_status before re-spawning"
                    )
                sa_results.append(json.dumps(failure))
            elif not sa_st.get("done"):
                still_running += 1
                if sa_st.get("awaiting_approval"):
                    _unsettled[aid] = "waiting_permission"
                else:
                    _unsettled[aid] = "running"
            else:
                completed += 1
                _settled_ids.add(aid)
                result_text = self._inline_result(aid, sa_st.get("result", ""))
                record: dict[str, Any] = {
                    "agent": label,
                    "status": "completed",
                    "text": result_text,
                }
                # Exactly True: a status read that does not say (a run recovered
                # from disk, a stopped or failed run) attaches nothing.
                if sa_st.get("made_no_tool_calls") is True:
                    record["unverified"] = f"This agent {NO_TOOL_CALLS_NOTE}"
                if sa_st.get("leaked_tool_call") is True:
                    record["leaked_tool_call"] = f"This agent {LEAKED_TOOL_CALL_NOTE}"
                sa_results.append(json.dumps(record))
        if _unsettled:
            sa_results.append(
                json.dumps(
                    {
                        "status": "still_running",
                        "task_ids": list(_unsettled),
                        "states": _unsettled,
                        "waited_secs": int(self.max_wait),
                        "query": "spawn_status/spawn_list",
                        "note": (
                            "The blocking wait ended; these sub-agents were NOT cancelled and "
                            "keep running on their own budget. Their [Subagent completion "
                            "event] messages still arrive; poll spawn_list or spawn_status "
                            "for progress."
                        ),
                    }
                )
            )
        # Members still queued and not started (collected above). They have no run
        # yet, so without this record the reason -- the one fact that says what to
        # change -- would stay in the gateway log.
        if never_started:
            sa_results.append(
                json.dumps(
                    {
                        "status": "queued",
                        "agents": never_started,
                        "note": (
                            "Queued, not started: accepted, and waiting for the reason given. "
                            "They start on their own once it clears and their [Subagent "
                            "completion event] messages arrive as usual; nothing was "
                            "cancelled. Do not spawn them again."
                        ),
                    }
                )
            )
        if self.resume_note:
            sa_results.append(json.dumps(self.resume_note))
        if self.sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": self.sa_errors}))
        self._settled_ids = _settled_ids
        self._counts = {
            "completed": completed,
            "still_running": still_running,
            "errored": errored,
        }
        return "\n\n".join(sa_results)

    def on_settled(self, text: str) -> None:
        # Only the driver's accepted-result commit suppresses completion events
        # and writes this tool's own row: a cancelled collection or an abandon
        # reply has delivered no child output. The generic ``call_tool_with_logging``
        # row is _AuditedDeferred's; this one carries the per-child tallies and the
        # ``partial`` outcome, as the blocking tool's did.
        if self.phase == "abandoned":
            return
        counts, self._counts = self._counts, None
        if counts is not None:
            mcp_core.sel().log_tool_invocation(
                session_key=_audit_owner(self.parent_session),
                source="mcp_core",
                tool_name="spawn_sub_agents",
                outcome=(
                    "completed"
                    if not counts["still_running"] and not counts["errored"]
                    else "partial"
                ),
                metadata={"spawned": len(self.sa_ids), **counts},
            )
        settled_ids, self._settled_ids = self._settled_ids, set()
        if settled_ids and self.parent_session:
            try:
                mcp_core._post(
                    "/api/spawn/mark-collected",
                    {"ids": list(settled_ids), "parent_session": self.parent_session},
                    timeout=5,
                )
            except Exception:
                pass  # best-effort: a missed mark leaves the completion event enabled
