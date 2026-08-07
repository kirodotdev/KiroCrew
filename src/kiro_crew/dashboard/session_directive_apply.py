"""Apply a decoded session directive against the consumer's OWN session.

Called from ``dashboard/chat_runner.py``'s ``EVENT_TOOL_RESULT`` handler — the
single shared turn loop for every interactive surface (dashboard, Slack,
Discord, taskrunner, …). The caller supplies the AUTHORITATIVE ``slot`` and
``session_key`` for the turn, so a stateless tool's directive is applied to the
exact session that produced it. Effects run IN-PROCESS via the same cores the
HTTP endpoints call (no loopback HTTP, no user-token dance): the consumer is
the authoritative session, so cross-session misattribution is unrepresentable.

Every branch returns a human-readable confirmation string and NEVER raises into
the runner. NOTE: gateway-off (the default), the MODEL already received the
tool's OWN return over the MCP pipe; this string is recorded on KiroCrew's
transcript / WS / hook surfaces, it does NOT replace the model's tool result.
That is why the tool bodies phrase their own message to not over-claim an effect
this consumer applies (and may refuse) after the fact.

IMPORTS ARE DELIBERATELY FUNCTION-LOCAL here, with ``session_surface`` the one
exception: it imports nothing from ``kiro_crew``, so it cannot cycle. ``sel`` is
a genuine cycle (``sel`` -> config -> apps -> dashboard, and chat_runner imports
this module before it imports sel). The rest (autonudge, autonudge_authz,
chat_utils, security, chat_handlers) are deferred on purpose: they keep this
module cheap to import from the turn loop's import graph, and they resolve the
symbol at CALL time so patching the SOURCE module is what tests (and any runtime
override) actually observe — a module-scope ``from X import name`` would freeze a
stale binding and silently bypass it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiro_crew.session_surface import has_dashboard_surface

logger = logging.getLogger(__name__)

# Directives whose effect targets a DASHBOARD chat slot (its project/CWD, its
# follow-up card, its question card). The HTTP endpoints they replaced were
# dashboard-scoped, so the applier keeps that boundary; the monitor trio is
# intentionally NOT here because it binds by session and supports Slack/Discord.
_DASHBOARD_ONLY_DIRECTIVES = frozenset({"set_project", "suggest_followup", "ask_question"})


class _DirectiveDenied(Exception):
    """Raised by an applier when it refuses on a permission decision (e.g. a
    sensitive-path block). Audited as ``outcome="denied"`` by the wrapper."""


def _audit(session_key: str, kind: str, outcome: str) -> None:
    """Emit a SEL tool-invocation event for one directive application.

    AUTOSDE ``backend-security-controls`` requires every tool invocation AND
    permission decision to emit a SEL event — the effect now runs here (not in
    the tool body or an HTTP endpoint), so the audit must too. Best-effort: a
    telemetry failure must never break the turn.
    """
    try:
        # Local import: kiro_crew.sel transitively pulls config -> apps ->
        # dashboard, which cycles with this dashboard-side module at import time
        # (chat_runner imports this module before it imports sel).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=session_key, source="mcp-directive", tool_name=kind, outcome=outcome
        )
    except Exception:
        logger.debug("session-directive SEL audit failed", exc_info=True)


async def apply_session_directive(
    state: Any,
    slot: Any,
    session_key: str,
    kind: str,
    args: dict[str, Any],
) -> str:
    """Apply directive *kind* with *args* to *slot*/*session_key*; return a
    confirmation string for the model. Fail-soft: any error is returned as a
    readable message, never raised. Every path emits a SEL audit event."""
    if kind in _DASHBOARD_ONLY_DIRECTIVES and not has_dashboard_surface(session_key):
        # These three act on a dashboard chat SLOT (its project/CWD, its
        # follow-up card, its question card), so the boundary is whether an open
        # tab exists to receive the effect — not where the conversation started.
        # A channel-born session displayed in a tab qualifies; a cron, sub-agent
        # or otherwise tabless caller does not, and must not silently retarget a
        # slot's project or address a card nothing will render. The consumer is
        # the only layer that knows the authoritative session, so the check
        # belongs HERE.
        _audit(session_key, kind, "denied")
        return (
            f"Error: {kind} only works from a dashboard chat session "
            f"(this turn is {session_key!r}). Nothing was changed."
        )
    try:
        if kind == "monitor_start":
            result = await _monitor_start(state, session_key, args)
        elif kind == "monitor_update":
            result = await _monitor_update(session_key, args)
        elif kind == "autonudge_stop":
            result = await _autonudge_stop(state, session_key, args)
        elif kind == "set_project":
            result = await _set_project(state, slot, args)
        elif kind == "suggest_followup":
            result = await _suggest_followup(state, slot, args)
        elif kind == "ask_question":
            result = await _ask_question(state, slot, args)
        else:
            _audit(session_key, kind, "error")
            return f"Error: unknown session directive {kind!r}."
    except _DirectiveDenied as exc:
        _audit(session_key, kind, "denied")
        return str(exc)
    except Exception as exc:  # never propagate into the turn loop
        logger.warning("apply_session_directive(%s) failed", kind, exc_info=True)
        _audit(session_key, kind, "error")
        return f"Error applying {kind}: {exc}"
    # Some appliers RETURN a readable failure instead of raising (an invalid
    # project dir, an absent loop, no attached client), so a blanket "success"
    # would falsely mark those in the SEL chain. Derive the outcome from the
    # result the same way call_tool_with_logging does (an "Error:" prefix ==
    # failed), keeping the audit truthful for the failure paths too.
    _audit(session_key, kind, "error" if result.startswith("Error:") else "success")
    return result


# ── autonudge trio ──────────────────────────────────────────────────────────


def _binding(session_key: str) -> str | None:
    from kiro_crew.autonudge import binding_key_for

    return binding_key_for(session_key)


async def _monitor_start(state: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge

    svc = get_instance()
    if svc is None:
        return "Monitor loop NOT armed: auto-nudge is disabled on this host."
    binding = _binding(session_key)
    if not binding:
        return "monitor_start is not supported from this session type."
    idle_secs = int(args.get("idle_secs") or 300)
    max_cycles = int(args.get("max_cycles") or 0)
    max_runtime_secs = int(args.get("max_runtime_secs") or 0)
    # USER-ARMED GATES ONLY (GPT review P1): this applier runs on the agent's
    # own AUTO-APPROVED directives, so an agent-supplied gate command is
    # unreviewed code reaching a shell with no human in the loop — the exact
    # execution path the review flagged. Arming a gate is therefore a user
    # action (dashboard goal popover / REST), never an agent directive. The
    # stop-time ENFORCEMENT below is unchanged — the gate still constrains the
    # agent; the agent just cannot author what gets executed.
    if str(args.get("exit_gate_cmd") or "").strip():
        raise _DirectiveDenied(
            "monitor_start: exit_gate_cmd cannot be set from an agent "
            "directive — a gate is a shell command, and this surface is "
            "auto-approved. Ask the user to arm one (dashboard goal popover "
            "or POST /api/autonudge)."
        )
    exit_gate_cmd = ""
    # GATE INHERITANCE (design review): arming replaces any existing loop on
    # the slot, so without this an agent whose stop was refused could re-arm
    # UNGATED and then stop unverified — the same self-disarm the
    # monitor_update guard closes, one tool over. An existing (user-armed)
    # gate is carried onto the replacement loop verbatim.
    inherited_gate = False
    existing = svc.get_by_slot(binding)
    existing_gate = str(getattr(existing, "exit_gate_cmd", "") or "").strip() if existing else ""
    if existing_gate:
        exit_gate_cmd = existing_gate
        inherited_gate = True
        # TERMINAL-BOUND CLAMPS (design review + GPT on the merged result):
        # without these, re-arming with a tiny cap (max_cycles=1) or a tiny
        # wall-clock budget expires the replacement through a deliberately
        # UNGATED terminal — the same indirect disarm that monitor_update's
        # lowering guards deny. The replacement must not expire sooner than
        # the replaced loop would have: clamp each bound to at least the
        # replaced loop's remaining allowance (an unlimited replaced bound
        # forces an unlimited replacement bound).
        replaced_cap = int(getattr(existing, "max_cycles", 0) or 0)
        if replaced_cap == 0:
            max_cycles = 0
        else:
            remaining = max(replaced_cap - int(getattr(existing, "cycle_count", 0) or 0), 1)
            if max_cycles != 0 and max_cycles < remaining:
                max_cycles = remaining
        replaced_budget = int(getattr(existing, "max_runtime_secs", 0) or 0)
        if replaced_budget == 0:
            max_runtime_secs = 0
        else:
            # The budget anchors on created_ts, which RESETS on the
            # replacement loop — so the replaced loop's remaining seconds is
            # the floor for the new budget.
            created = float(getattr(existing, "created_ts", 0.0) or 0.0)
            elapsed = int(time.time() - created) if created else 0
            remaining_budget = max(replaced_budget - elapsed, 1)
            if max_runtime_secs != 0 and max_runtime_secs < remaining_budget:
                max_runtime_secs = remaining_budget
    loop, error, _status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=binding,
        message=str(args.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path="",
        max_runtime_secs=max_runtime_secs,
        exit_gate_cmd=exit_gate_cmd,
        source="mcp-directive",
        caller="session-directive",
    )
    if error is not None:
        return f"Failed to start monitor loop: {error}"
    cap = f", stopping after {max_cycles} cycles" if max_cycles else ", with NO cycle cap"
    if max_runtime_secs:
        cap += f", wall-clock budget {max_runtime_secs}s"
    if exit_gate_cmd:
        cap += (
            f". EXIT GATE armed: autonudge_stop will only succeed once "
            f"`{exit_gate_cmd}` exits 0"
        )
        if inherited_gate:
            cap += (
                " (INHERITED from the replaced loop — an agent-initiated re-arm "
                "cannot drop or change an existing gate, and the replacement's "
                "cycle cap is clamped so it cannot expire ungated sooner than "
                "the replaced loop would have; ask the user to alter the gate)"
            )
    return (
        f"Monitor loop {getattr(loop, 'id', '?')} started on this session: the "
        f"message re-injects {idle_secs}s after each turn ENDS (idle gap){cap}. "
        "End your turn now — the loop wakes you. Call autonudge_stop when the "
        "exit condition is met."
    )


async def _monitor_update(session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_update_nudge

    svc = get_instance()
    if svc is None:
        return "Cannot update monitor loop: auto-nudge is disabled on this host."
    binding = _binding(session_key)
    if not binding:
        return "monitor_update is not supported from this session type."
    loop = svc.get_by_slot(binding)
    if not loop:
        return "No active monitor loop on this session to update."
    patch = dict(args.get("patch") or {})
    # USER-ARMED GATES ONLY (GPT review P1 + the earlier self-disarm guard,
    # unified): the exit gate is a shell command and this applier runs on the
    # agent's own AUTO-APPROVED directives, so the agent may neither arm,
    # change, nor remove one — any exit_gate_cmd here is refused. Arming and
    # altering gates is a user action (dashboard / REST PATCH), which never
    # routes through this applier. (Previously an agent could ADD a gate
    # where none existed; that was the unreviewed-command execution path the
    # GPT review flagged, closed along with monitor_start's.)
    if "exit_gate_cmd" in patch:
        raise _DirectiveDenied(
            f"monitor_update: exit_gate_cmd cannot be set, changed, or removed "
            f"from an agent directive (loop {loop.id}) — gates are user-armed. "
            "Ask the user — the dashboard goal popover or "
            "PATCH /api/autonudge/{loop_id} can."
        )
    # Terminal-bound lowering escape (design review + GPT on the merged
    # result): BOTH terminal bounds — the cycle cap AND the wall-clock budget
    # — are deliberately ungated, so on a GATED loop shrinking either one
    # expires the loop without its gate ever running: an indirect
    # self-disarm. Raising (or 0 = unlimited) keeps working; only lowering is
    # a user action on a gated loop.
    if str(getattr(loop, "exit_gate_cmd", "") or "").strip():
        new_cap_val = patch.get("max_cycles")
        current_cap_val = int(getattr(loop, "max_cycles", 0) or 0)
        if (
            new_cap_val is not None
            and int(new_cap_val) != 0
            and (current_cap_val == 0 or int(new_cap_val) < current_cap_val)
        ):
            raise _DirectiveDenied(
                f"monitor_update: loop {loop.id} has an exit gate, and lowering "
                "its cycle cap would let the loop expire ungated (the cap path "
                "does not run the gate). Raise the cap, pass 0, or ask the user."
            )
        new_budget_val = patch.get("max_runtime_secs")
        current_budget_val = int(getattr(loop, "max_runtime_secs", 0) or 0)
        if (
            new_budget_val is not None
            and int(new_budget_val) != 0
            and (current_budget_val == 0 or int(new_budget_val) < current_budget_val)
        ):
            raise _DirectiveDenied(
                f"monitor_update: loop {loop.id} has an exit gate, and lowering "
                "its wall-clock budget would let the loop expire ungated (the "
                "budget path does not run the gate). Raise the budget, pass 0, "
                "or ask the user."
            )
    cycle_count = int(getattr(loop, "cycle_count", 0) or 0)
    current_cap = int(getattr(loop, "max_cycles", 0) or 0)
    new_cap = patch.get("max_cycles", current_cap)
    # Capped-loop guard: a cap at/below the delivered count deactivates the loop
    # without another fire — refuse rather than promise a wake that never comes.
    if not (new_cap == 0 or new_cap > cycle_count):
        raise _DirectiveDenied(
            f"monitor_update: max_cycles={new_cap} is at or below this loop's "
            f"delivered cycle count ({cycle_count}), so it would deactivate "
            "without firing again. Pass a larger cap, or 0 for unlimited."
        )
    # Spent-budget guard, same shape as the cycle-cap one: a wall-clock budget
    # at/below the loop's elapsed age deactivates it on the next timer without
    # another fire — refuse rather than promise a wake that never comes.
    if "max_runtime_secs" in patch:
        new_budget = int(patch["max_runtime_secs"] or 0)
        created_ts = float(getattr(loop, "created_ts", 0.0) or 0.0)
        elapsed = int(time.time() - created_ts) if created_ts else 0
        if new_budget and created_ts and elapsed >= new_budget:
            raise _DirectiveDenied(
                f"monitor_update: max_runtime_secs={new_budget} is at or below "
                f"this loop's elapsed runtime ({elapsed}s since it was armed), "
                "so it would deactivate without firing again. Pass a larger "
                "budget, or 0 for unlimited."
            )
    revived = False
    # Paused-loop protection: never silently resume unattended execution as a
    # side effect of a metadata edit — revive ONLY a loop stopped by one of its
    # own terminal bounds whose stopping bound is actually being raised. Keyed
    # on the PERSISTED ``stopped_reason`` recorded at deactivation time: the
    # cycle-count heuristic stays only as a legacy fallback for stores written
    # before the field existed, and the budget side has NO heuristic at all —
    # elapsed time keeps growing after a manual pause, so "budget looks spent"
    # cannot distinguish a pause from an expiry (GPT review on #2116: a
    # budget raise must never resume a loop the user paused).
    if not getattr(loop, "active", True):
        reason = str(getattr(loop, "stopped_reason", "") or "")
        stopped_at_cap = reason == "cycle_cap" or (
            not reason and current_cap > 0 and cycle_count >= current_cap
        )
        raising_cap = "max_cycles" in patch and (new_cap == 0 or new_cap > current_cap)
        stopped_at_budget = reason == "runtime_budget"
        # A budget-raise passed the spent-budget guard above, so any budget in
        # the patch here is beyond the loop's elapsed age (or 0 = unlimited).
        raising_budget = "max_runtime_secs" in patch
        if stopped_at_cap and raising_cap:
            patch["active"] = True
            revived = True
        elif stopped_at_budget and raising_budget:
            patch["active"] = True
            revived = True
        else:
            # Name the bound that actually stopped the loop, so the remedy in
            # the message is the one that will work.
            if stopped_at_budget:
                bound = (
                    f"its {int(getattr(loop, 'max_runtime_secs', 0) or 0)}s wall-clock "
                    "budget ran out; raise max_runtime_secs above the loop's age "
                    "(or pass 0)"
                )
            elif stopped_at_cap:
                bound = "it hit its cycle cap; raise max_cycles above the cap (or pass 0)"
            else:
                bound = "it was paused manually; ask the user, or use monitor_start"
            raise _DirectiveDenied(
                f"Monitor loop {loop.id} is PAUSED (cycle {cycle_count}"
                + (f" of {current_cap}" if current_cap else ", no cap")
                + f"). monitor_update will not resume it as a side effect: {bound}."
            )
    _new_loop, error, _status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message=patch.get("message"),
        idle_secs=patch.get("idle_secs"),
        max_cycles=patch.get("max_cycles"),
        active=patch.get("active"),
        max_runtime_secs=patch.get("max_runtime_secs"),
        exit_gate_cmd=patch.get("exit_gate_cmd"),
        source="mcp-directive",
        caller="session-directive",
    )
    if error is not None:
        return f"Failed to update monitor loop: {error}"
    fields = ", ".join(sorted(k for k in patch if k != "active"))
    return (
        f"Monitor loop {loop.id} updated on this session ({fields})."
        + (" The stopped loop has been re-armed." if revived else "")
    )


async def _autonudge_stop(state: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance

    svc = get_instance()
    if svc is None:
        return "No auto-nudge loop to stop (auto-nudge is disabled on this host)."
    binding = _binding(session_key)
    if not binding:
        return "autonudge_stop is not supported from this session type."
    loop = svc.get_by_slot(binding)
    if not loop:
        return "No active auto-nudge loop on this session — nothing to stop."
    loop_id = loop.id
    gate_note = ""
    gate = str(getattr(loop, "exit_gate_cmd", "") or "").strip()
    if gate:
        # REVALIDATE before execution (GPT review): the gate was vetted at
        # ARM time, but governance policy can tighten between arm and stop —
        # a stored command the current policy forbids must not execute on
        # yesterday's approval. Same vet as the arm path (deny-list,
        # credential paths, redaction stability, caller-identity governance),
        # off-thread because it reads policy files from disk. A gate that no
        # longer vets clean fails CLOSED for execution but does NOT block the
        # stop: executing a now-forbidden command is the risk being managed,
        # and refusing the stop would force exactly that execution on retry.
        # The loud unverified warning keeps the exit honest.
        from kiro_crew.autonudge_authz import (
            _governance_session_key,
            vet_exit_gate_cmd,
        )

        revet_err = await asyncio.to_thread(
            vet_exit_gate_cmd, gate, _governance_session_key(binding)
        )
        if revet_err:
            logger.warning(
                "AutoNudge exit gate for loop %s no longer passes vetting — "
                "skipping execution (%s)",
                loop_id,
                revet_err,
            )
            gate_note = (
                " WARNING: this loop's exit gate was NOT run — the stored "
                f"command no longer passes storage-time vetting ({revet_err}). "
                "The exit condition is NOT verified."
            )
        else:
            # Offloaded: _gate_cwd stats directories (Path.is_dir on a
            # possibly network-mounted project dir) and reads config for the
            # workspace fallback — filesystem work that must not run on the
            # event loop (GPT review: a slow mount would freeze the gateway).
            gate_cwd = await asyncio.to_thread(_gate_cwd, state, binding)
            gate_note = await _run_exit_gate(loop_id, gate, cwd=gate_cwd)
    await svc.remove(loop_id)
    reason = str(args.get("reason") or "").strip()
    return (
        f"Auto-nudge loop {loop_id} stopped on this session"
        + (f" (reason: {reason})" if reason else "")
        + "."
        + gate_note
        + " No further nudges will fire."
    )


# Exit-gate execution bounds. The timeout is sized for a real verification
# command (a focused test run, a health-check curl) while keeping a hung gate
# from parking the stop indefinitely; the output cap keeps a chatty gate from
# flooding the refusing turn's context.
_EXIT_GATE_TIMEOUT_SECS = 120
_EXIT_GATE_MAX_OUTPUT = 2000

# error_kind values (cron_script.run_command_sandboxed) that mean the gate
# COULD NOT RUN on this host at all — as opposed to ran-and-failed. Only these
# fail OPEN. Typed on purpose: an earlier revision substring-matched the
# human-readable output, which a gate command could spoof
# (`echo "No POSIX shell available"; exit 1`) to fail open, and which a
# harmless reword in cron_script.py would silently flip to fail-closed.
_GATE_STRUCTURAL_ERROR_KINDS = frozenset({"no_shell", "sandbox_unavailable"})


def _gate_cwd(state: Any, binding: str) -> str | None:
    """Resolve the directory a loop's exit gate should execute in.

    Without an anchor the gate inherits the gateway DAEMON's cwd (design
    review): a relative-path gate like ``pytest -q tests/`` — the schema's own
    flagship example — would run from the wrong directory and fail for reasons
    unrelated to work quality, refusing every stop until the loop burns to its
    cap. Anchor precedence: the slot's project directory (where the agent's
    own file work is scoped) → the slot's workspace directory → the default
    workspace directory (channel keys have no slot). Returns ``None`` only if
    even that resolution fails, in which case the runner falls back to the
    daemon cwd (pre-existing behavior).
    """
    from kiro_crew.config.loader import workspace_dir_for

    slot = None
    try:
        slot = state._slots.get(binding) if state is not None else None
    except Exception:  # noqa: BLE001 - a state fake without _slots
        slot = None
    # Real dashboard slots store their working directory in ``slot.project``
    # (state.py); ``project_dir`` is kept as a fallback for structural fakes
    # that predate this fix (GPT review: reading only project_dir meant the
    # anchor ALWAYS fell through to the workspace dir on real slots).
    project = ""
    if slot is not None:
        project = str(
            getattr(slot, "project", "") or getattr(slot, "project_dir", "") or ""
        ).strip()
    if project and Path(project).is_dir():
        return project
    try:
        return str(workspace_dir_for(getattr(slot, "workspace", "default") if slot else "default"))
    except Exception:  # noqa: BLE001 - fall back to daemon cwd
        return None


async def _run_exit_gate(loop_id: str, gate: str, cwd: str | None = None) -> str:
    """Execute a loop's exit gate; return a result note or REFUSE the stop.

    The gate runs in the STRICT command sandbox (``run_command_sandboxed`` with
    ``sandbox_mode="strict"``, which additionally hides ``~/.ssh``) — the
    command was vetted at arm time by the same storage-time guards as a cron
    ``command``, but unlike a cron job an exit gate has no legitimate SSH use
    (it verifies work: run tests, curl a health check), so the wider "cc"
    profile's deliberate ``~/.ssh`` exposure is not inherited. Text-only
    vetting cannot see inside a referenced script (``python3 /tmp/gate.py``),
    so the exec-time sandbox is what bounds a model-written script here.
    Offloaded to the executor: the runner blocks on the subprocess.

    Outcome policy, keyed on the runner's MACHINE-READABLE ``error_kind``
    (never on output text, which contains attacker-influenced stdout):

    * **ran, exit 0** → note "gate passed"; the stop proceeds.
    * **ran and failed** (non-zero exit, timeout — an unfinished verification
      has verified nothing) → the stop is REFUSED via ``_DirectiveDenied``
      with the bounded gate output, so the agent gets the evidence it needs
      to actually finish the job and retry.
    * **structurally unable to run** (``no_shell`` / ``sandbox_unavailable``)
      → fail OPEN with a loud warning in the note. This is a QUALITY gate,
      not a security boundary — failing closed on a structural inability
      would trap the loop until its caps on exactly the hosts that can never
      satisfy it.

    A refused stop leaves the loop untouched; the user-facing stops (REST
    DELETE, STOP sentinel, cycle caps) never run the gate, so a broken gate
    cannot make a loop unstoppable by its owner.
    """
    from kiro_crew.config.loader import config_dir
    from kiro_crew.cron_script import run_command_sandboxed
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    # Hide the ENTIRE Kiro Crew data home from the gate. Text vetting cannot
    # see inside a referenced interpreter script, and the data home is the
    # keystone: a script that rewrites security_policy.json (or the loop
    # store, or lessons) is rewriting the agent's own ceiling. No legitimate
    # verification command needs to touch it. Offloaded: config_dir() reads
    # (and may create) the config directory — filesystem work that must not
    # run on the event loop.
    try:
        hidden: tuple[str, ...] = (str(await asyncio.to_thread(config_dir)),)
    except Exception:  # noqa: BLE001 - fall back to profile-only hiding
        hidden = ()
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: run_command_sandboxed(
            gate,
            timeout=_EXIT_GATE_TIMEOUT_SECS,
            sandbox_mode="strict",
            extra_hidden_dirs=hidden,
            # Anchor to the slot's project/workspace dir (see _gate_cwd) so a
            # relative-path gate verifies THE loop's work, not the daemon cwd.
            cwd=cwd,
        ),
    )
    output = str(result.get("output") or "")
    if len(output) > _EXIT_GATE_MAX_OUTPUT:
        output = output[:_EXIT_GATE_MAX_OUTPUT] + "\n[gate output truncated]"
    # Gate stdout is attacker-influenceable runtime text that flows into the
    # transcript / dashboard / channel surfaces — scrub credential patterns
    # and exfiltration URLs like every other LLM-visible output path
    # (backend-security-controls).
    output, _ = redact_exfiltration_urls(output)
    output, _ = redact_credentials(output)
    if result.get("status") == "ok":
        return " Exit gate passed (exit 0)."
    if result.get("error_kind") in _GATE_STRUCTURAL_ERROR_KINDS:
        logger.warning(
            "AutoNudge exit gate for loop %s could not execute (%s) — allowing the stop",
            loop_id,
            result.get("error_kind"),
        )
        return (
            " WARNING: the exit gate could not be executed on this host, so the "
            "exit condition was NOT verified:\n" + output
        )
    raise _DirectiveDenied(
        f"autonudge_stop REFUSED: this loop's exit gate did not pass "
        f"(exit_code={result.get('exit_code')}). The loop stays active — finish "
        "the work the gate checks for, then call autonudge_stop again. Gate "
        f"command: `{gate}`\nGate output:\n{output}"
    )


# ── dashboard-only effects ───────────────────────────────────────────────────


async def _set_project(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.security import is_sensitive_path

    clear = bool(args.get("clear"))
    project = str(args.get("project") or "").strip()
    old_project = getattr(slot, "project", "") or ""
    if clear or not project:
        slot.project = ""
        if old_project:
            slot._pending_reset_history_key = effective_session_key(slot)
        _push(state)
        return "Project cleared. The next message cold-starts with no project scope."
    expanded = os.path.expanduser(project)

    def _validate() -> tuple[str, bool, bool]:
        """Resolve + classify the path on a worker thread.

        `realpath`/`isdir` touch the filesystem, so a network-mounted project
        path would stall chat, heartbeat and liveness if resolved on the event
        loop (no-blocking-call-on-event-loop). Returns
        (realpath, sensitive, is_dir); the sensitive check runs on BOTH the
        pre-resolution and resolved forms — the pre-check keeps a sensitive
        path from being probed at all, the post-check catches symlink/".."
        evasion.
        """
        if is_sensitive_path(expanded):
            return "", True, False
        rp_ = os.path.realpath(expanded)
        if is_sensitive_path(rp_):
            return rp_, True, False
        return rp_, False, os.path.isdir(rp_)

    rp, sensitive, is_dir = await asyncio.to_thread(_validate)
    if sensitive:
        # Permission decision — raise so the wrapper audits it as denied.
        raise _DirectiveDenied("Error: access denied (sensitive path).")
    if not is_dir:
        return f"Error: not a directory: {rp}"
    slot.project = rp
    if rp != old_project:
        slot._pending_reset_history_key = effective_session_key(slot)
        try:
            from kiro_crew.dashboard.chat_handlers import _save_recent_project

            # Offload the recent-projects file IO (mkdir + read + atomic write)
            # off the event loop — the HTTP endpoint this replaced did the same.
            await asyncio.to_thread(_save_recent_project, rp)
        except Exception:
            logger.debug("save recent project failed", exc_info=True)
    _push(state)
    return (
        f"Project set to {rp}. The session cold-starts with the new CWD and "
        "project-level .kiro/steering on the next message."
    )


async def _suggest_followup(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_handlers import _redact_followup_item

    items = [_redact_followup_item(i) for i in (args.get("items") or [])]
    if not items:
        return "No follow-up items to show."
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return "Follow-up card could not be delivered (no owner channel)."
    clients = int(
        await deliver("followup_card", {"slot": slot.key, "items": items, "ts": time.time()})
    )
    if clients == 0:
        return (
            "Follow-up card prepared, but no dashboard client is attached — "
            "restate the follow-ups in your reply text so they are not lost."
        )
    if not getattr(slot, "project", ""):
        # The card renders "Start in new worktree" DISABLED when the slot has no
        # project directory (FollowUpCard.tsx gates on projectDir), and this
        # confirmation is the model's only window into that: without it the
        # agent recommends the worktree route in sessions where it can never
        # work — Research Lab worker slots, for one, are created unscoped
        # (auto_research/handlers.py) — and steers the user into a dead button.
        return (
            "Follow-up card shown below the composer. Note: this session has no "
            "project directory, so the card's 'Start in new worktree' button is "
            "disabled. Point the user at 'Add to this session' instead, or "
            "suggest they scope a project first (the composer's Project chip)."
        )
    return "Follow-up card shown below the composer."


async def _ask_question(state: Any, slot: Any, args: dict[str, Any]) -> str:
    """Post a NON-BLOCKING question card to this session's slot. The card
    carries no ask_id, so the frontend submit sends the answers as an ordinary
    next message that resumes the session — the agent must END its turn now."""
    post = getattr(state, "post_question_card", None)
    if post is None:
        return "Question card could not be delivered (no card channel)."
    clients = int(await post(slot.key, args.get("questions") or []))
    if clients == 0:
        return (
            "Question posted, but no dashboard client is attached to see it — "
            "ask in plain text and end your turn instead."
        )
    return (
        "Question card shown in this session. End your turn now — the user's "
        "answer will arrive as your next message; do not re-ask or guess."
    )


def _push(state: Any) -> None:
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        try:
            push()
        except Exception:
            logger.debug("push_slots_update failed", exc_info=True)
