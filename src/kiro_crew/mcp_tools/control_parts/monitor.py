"""The monitoring-loop tools: arming, inspecting, updating and stopping a session's loop.

``monitor_start`` arms the prompt loop and ``monitor_watch`` the structured monitor;
``monitor_inspect`` reads the monitor bound to the caller's session, ``monitor_update``
changes it, and ``monitor_stop`` and ``autonudge_stop`` end it. The tools that change a
loop resolve a strict session identity and emit a session directive through
:func:`~kiro_crew.mcp_tools.control_parts.directive._emit_directive`; the context refusal
and the arming preflight refusal that some of them run first are defined here.

This module is an owner of :mod:`kiro_crew.mcp_tools.control`, which serves every name
here and stays the import path and the patch seam: a patch through it reaches this module
too (:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

import json
import time
from typing import Any

from kiro_crew import autonudge, mcp_core
from kiro_crew.autonudge_judge import screen_phrase
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_tools._limits import (
    _MONITOR_DEFAULT_MAX_CYCLES,
    _MONITOR_DEFAULT_MAX_RUNTIME_SECS,
)
from kiro_crew.mcp_tools.control_parts.directive import _emit_directive
from kiro_crew.monitoring.limits import runtime_ceiling_secs
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_AGENT_TURNS,
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_PROVIDER_ERRORS,
    DEFAULT_MONITOR_RUNTIME_SECS,
    DEFAULT_MONITOR_TOKENS,
    MAX_MONITOR_CHECK_NAMES,
    PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
    retained_outcome_blocks_rearm,
)
from kiro_crew.monitoring.targets import normalize_pull_request_target
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.validation import (
    AUTONUDGE_STOP_SCHEMA,
    MONITOR_INSPECT_SCHEMA,
    MONITOR_START_SCHEMA,
    MONITOR_STOP_SCHEMA,
    MONITOR_UPDATE_SCHEMA,
    MONITOR_WATCH_SCHEMA,
    ValidationError,
    validate_judge_spec,
    validate_tool_args,
)


def autonudge_stop(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

    # Resolve the current session's binding key and stop any loop on it.
    # STRICT resolution via the shared gate (env-var only, no PID walk): this
    # tool mutates another process's persistent loop state, and a subagent
    # lives under the parent slot's process tree — a PID-walk would let it
    # silently stop the PARENT session's loop (matches set_project's rule).
    # Resolve-half only: an empty key deliberately does NOT refuse here — it
    # falls through to the directive, whose consumer resolves its own session.
    sk, _ = mcp_core.require_strict_session_key("autonudge_stop")
    # Stateless: emit a directive; the session-aware consumer
    # (chat_runner) resolves the loop by ITS OWN session and stops it. The
    # tool carries no session identity — sk is used only to short-circuit a
    # context where a directive can never be applied (cron/hook/subagent).
    if mcp_core._autonudge_binding_key(sk) is None and sk:
        return (
            "No auto-nudge loop to stop: this tool only works from within "
            "a dashboard, Slack, Discord, or Webex session "
            f"(current session_key={sk!r})."
        )
    return _emit_directive(
        "autonudge_stop",
        {"reason": args.get("reason", "").strip()},
        # NOT a confirmation, and worded so a model cannot read it as one: this
        # tool resolves no session, so it cannot know whether a loop is bound
        # here. The consumer applies the stop and records the real outcome —
        # including "nothing was stopped" when the binding resolves no loop —
        # onto the transcript, so a caller that reads this as success would be
        # acting on an unverified claim.
        "Stop REQUESTED for this session's auto-nudge loop. This is not "
        "confirmation that a loop was found or stopped; the applied outcome is "
        "recorded separately and may report that nothing was stopped.",
    )


def monitor_start(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_START_SCHEMA)
    # STRICT resolution via the shared gate (env-var only, no PID walk):
    # monitor_start creates a persistent unattended loop that repeatedly runs
    # tools in the bound session. A subagent under the parent's process tree
    # must NOT be able to PID-walk into the parent's identity and mint a loop
    # the parent user never asked for (crosses the session authorization
    # boundary). Resolve-half only: the short-circuit below is on context,
    # not identity, so an empty key falls through to the directive.
    sk, _ = mcp_core.require_strict_session_key("monitor_start")
    # Stateless: only short-circuit contexts where a directive can
    # never be applied (cron/hook/subagent). The session-aware consumer
    # (chat_runner) supplies the binding key and arms the loop.
    if mcp_core._autonudge_binding_key(sk) is None and sk:
        return (
            "monitor_start only works from within a dashboard, Slack, Discord, "
            f"or Webex session (current session_key={sk!r}). For other "
            "contexts use cron_add or a HEARTBEAT.md task."
        )
    message = args["message"].strip()
    if not message:
        return "monitor_start: message must not be empty."
    interval_secs = int(args.get("interval_secs") or 300)
    # Default to bounded cycle and runtime caps. A loop without either bound
    # only ever stops when the model volunteers an autonudge_stop, and observed
    # loop stores show that is not reliable.
    raw_max = args.get("max_cycles")
    max_cycles = _MONITOR_DEFAULT_MAX_CYCLES if raw_max is None else int(raw_max)
    # The runtime budget is bounded by default alongside the cycle cap, so a
    # quiet or slow loop cannot survive indefinitely without a fresh decision.
    # An omitted budget takes the default capped to the operator ceiling, so a
    # ceiling below the default never refuses a value the caller did not send.
    max_runtime_secs = int(
        args.get("max_runtime_secs")
        or min(_MONITOR_DEFAULT_MAX_RUNTIME_SECS, runtime_ceiling_secs())
    )
    # The one escape from gating, and deliberately an opt-OUT. An opt-IN is what
    # An opt-out is used rather than opt-in: an opt-in default gates everything
    # and releases nothing (every opt-in mechanism sees zero adoption because
    # the default never moves), while this releases the minority of loops whose
    # duty is to act WHILE the subject is quiet (refresh a heartbeat, chase a
    # silent reviewer, rebase onto a moving base). Those loops otherwise have no
    # control but the wording of their own instruction, which is a fragile
    # thing to key a cadence on.
    gate = args.get("gate")
    gate = True if gate is None else bool(gate)
    # Infer from the message AS IT WILL BE STORED. The authorizer redacts
    # exfiltration URLs and credentials at its own chokepoint before persisting,
    # so a subject named by a URL the redactor rewrites survives here but not
    # there -- and the ack would then promise gating for a loop that is armed
    # ungated. Applying the same transform first makes the disclosure describe
    # the loop that will actually exist.
    stored_message, _ = redact_exfiltration_urls(message)
    stored_message, _ = redact_credentials(stored_message)
    # ``banner`` is CONDITIONAL, unlike the fields above: a caller that sets no
    # banner must see the payload shape it saw before, because the tool's
    # contract test asserts this dict by EXACT equality. The applier reads it
    # with ``.get``, so absent and empty mean the same thing there.
    banner = str(args.get("banner") or "").strip()
    # The SUBJECT, when the caller names one. Validated by the schema's allowed set, so
    # anything here is already the one supported kind.
    watch = str(args.get("watch") or "").strip()
    # The judge brief, bounded HERE rather than at the applier: this is the surface
    # the owner typed it at, so a refusal names the field they can fix. The schema
    # only says the value is an object; these are the bounds on what it may hold.
    try:
        judge_spec = validate_judge_spec(args.get("judge"))
    except ValidationError as exc:
        return f"monitor_start: {exc.field}: {exc.message}"
    # After the brief is validated, because the brief's own ``targets`` list is the
    # FIRST place the subject is looked for -- a loop naming its pull request there
    # and not in the message is gated, and an ack derived from the message alone
    # would tell its caller the opposite. Scrubbed for the same reason the message
    # above is: the disclosure has to describe the loop that will actually exist.
    #
    # ``gate or watch`` mirrors the service's own fold: an explicit watch gates on its
    # own, so an ack derived from ``gate`` alone would promise a plain re-injection for a
    # loop that is about to be armed gated. ``slot_key`` is what makes a work-ledger
    # subject resolvable at all -- a session's key is not in its own prose -- and it is
    # the BINDING key, the same one the applier passes, so the ack names the subject the
    # loop will actually carry rather than a second derivation of it.
    #
    # ``session_texts`` is the session log a bare ``PR <number>`` resolves against. The
    # MCP server's run of this handler is off the gateway's loop, so it reads it there.
    # The gateway's directive replay runs this handler ON its event loop and discards
    # the ack, so that run skips the read: the applier reads the log off-loop itself.
    binding_key = str(mcp_core._autonudge_binding_key(sk) or "")
    session_texts = (
        None
        if mcp_core.directive_capture_active()
        else autonudge.read_session_texts(stored_message, binding_key, watch)
    )
    gated = (
        autonudge.infer_monitor(
            stored_message,
            time.time(),
            judge=autonudge.scrubbed_judge_spec(judge_spec) if judge_spec else None,
            watch=watch,
            slot_key=binding_key,
            session_texts=session_texts,
        )
        if (gate or watch)
        else None
    )
    # Before the payload is built, so the emitted dict is byte-identical to what
    # it was (its shape is asserted by exact equality in the contract test) and a
    # certain refusal is reported instead of acknowledged.
    preflight_refusal = _arming_preflight_refusal("monitor_start", sk)
    if preflight_refusal:
        return preflight_refusal
    payload: dict[str, Any] = {
        "message": message,
        "idle_secs": interval_secs,
        "max_cycles": max_cycles,
        "max_runtime_secs": max_runtime_secs,
        "gate": gate,
    }
    if banner:
        payload["banner"] = banner
    # CONDITIONAL for the reason ``banner`` and ``judge`` are: a caller that names no
    # watch must produce the payload shape it produced before this field existed, which
    # the contract test asserts by exact equality. The applier reads it with ``.get``, so
    # absent and empty agree there.
    if watch:
        payload["watch"] = watch
    # CONDITIONAL for the reason ``banner`` is: a caller that arms no judge must see
    # the payload shape it saw before, which the contract test asserts by exact
    # equality. The applier reads it with ``.get``, so absent and empty agree there.
    if judge_spec:
        payload["judge"] = judge_spec
    # Say whether this loop will be GATED, in the ack, at the surface that armed
    # it. This calls the SCHEDULER'S OWN decision function rather than
    # re-deriving the answer from the target: a subject can infer cleanly and
    # still fail to form a valid monitor, so a second evaluation could claim a
    # gate the loop never got -- and a disclosure that can be wrong is worse than
    # none. Without any disclosure the ack promises a plain re-injection "every
    # {interval}s", which for a gated loop is untrue, and this whole change
    # exists because a cadence change nobody could see had no effect.
    return _emit_directive(
        "monitor_start",
        payload,
        (
            "Monitor loop requested on this session: "
            + (
                f"observing {gated.target} every {interval_secs}s and "
                "re-injecting the message only when the tick needs you -- "
                f"{screen_phrase()}, and a raised wake lands up to about "
                "one interval after the tick that saw it"
                + (f" and the {max_cycles} cap counts delivered turns" if max_cycles else "")
                if gated is not None
                else f"the message will re-inject every {interval_secs}s"
            )
            + " (user messages defer a due "
            "fire to their turn's end without restarting the countdown)"
            + (f", stopping after {max_cycles} cycles" if max_cycles else ", with NO cycle cap")
            + (
                f", runtime budget {max_runtime_secs}s (awake time while the gateway "
                "runs; a restart counts the time since created_ts on the wall clock)"
                if max_runtime_secs
                else ""
            )
            + ". End your turn now. Arming happens when this turn's result is "
            "processed, so this ack cannot confirm it; the outcome is reported "
            'as a transcript notice on this session — "Automation loop armed: '
            'loop <id> … next wake …" or "Automation loop NOT armed: <reason> '
            "[status N]\" — and the applier's own result replaces this text in "
            "the transcript. If the notice says NOT armed, read the reason "
            "before trying again. Only a live dashboard/Slack/Discord/Webex "
            "session can host a loop. Call autonudge_stop when "
            "the exit condition is met; hitting the cap is a runaway backstop, "
            "not a finish. From dashboard, Slack, or Discord, use monitor_update "
            "if the instruction goes stale; on Webex, stop this loop and create "
            "a new finite one instead."
        ),
    )


def _monitor_context_refusal(
    tool_name: str,
    session_key: str,
    message: str,
    *,
    error: str = "unsupported_session_binding",
) -> str:
    """Return a failed tool result and retain the security-relevant refusal."""
    mcp_core.sel().log_tool_invocation(
        session_key=session_key or "mcp_core",
        source="mcp",
        tool_name=tool_name,
        outcome="denied",
        error=error,
    )
    return f"Error: {message}"


def _arming_preflight_refusal(tool_name: str, session_key: str) -> str:
    """Refuse IN BAND when the turn boundary is certain to refuse this call.

    Two causes are certain from one reading: the session cannot host a loop at
    all (its memory mode, reported as ``hosting_refusal`` by the authorizer's own
    :func:`kiro_crew.autonudge_authz.loop_hosting_refusal`, so the text here is
    the text the authorizer denies with), or a retained stop holds the binding.
    The memory-mode half skips ``monitor_update`` on a legacy timer loop, whose
    authorizer has no memory-mode rule.

    An arming tool answers the model over its own pipe DURING the turn, while
    ``apply_session_directive`` runs after the turn's result is processed. A
    refusal decided there cannot reach the model in the arming turn, so without a
    preflight the model ends its turn holding a "requested" ack for a monitor that
    does not exist. This is the only place that can say otherwise in time.

    Reads the endpoint ``monitor_inspect`` already reads, so it grants no new
    capability, and NEVER writes: clearing retained evidence stays owner-only.

    SKIPPED ENTIRELY during gateway-side directive replay
    (:func:`mcp_core.directive_capture_active`). That run discards this text, and
    the read would be a blocking loopback request to the very gateway whose event
    loop is synchronously waiting on this call -- it could not be answered, and
    every co-hosted session would stall until the timeout. Nothing is lost: the
    preflight exists to reach the MODEL in the arming turn, which only the MCP-side
    run can do, and the turn boundary still refuses the arm on its own.

    Fails OPEN -- an unreadable gateway returns ``""`` and the caller emits as
    before, because failing closed would let one bad read block all arming. The
    turn boundary remains the enforcement point, so both TOCTOU directions are
    benign: a record cleared just after the read costs one retryable refusal, and
    one written just after it is still caught authoritatively.

    The retained-stop half covers the STRUCTURED record, the only one the endpoint
    reports an outcome for. A paused legacy timer loop reads as ``autonudge_loop`` with no outcome
    and is left to the existing create-only refusal.
    """
    if mcp_core.directive_capture_active():
        return ""
    try:
        reading = mcp_core._get("/api/autonudge/session-monitor", session_key=session_key)
    except Exception:
        return ""
    if not isinstance(reading, dict) or reading.get("error"):
        return ""
    hosting_refusal = reading.get("hosting_refusal")
    # A legacy timer loop is updated through ``authorize_and_update_nudge``,
    # which applies no memory-mode rule (it also carries pause), so an update to
    # one is not refused at the turn boundary and must not be refused here. A
    # slot tightened to temporary after the loop was armed can still shrink or
    # pause it.
    legacy_update = tool_name == "monitor_update" and isinstance(
        reading.get("autonudge_loop"), dict
    )
    if isinstance(hosting_refusal, str) and hosting_refusal.strip() and not legacy_update:
        not_done = (
            "no monitor was changed" if tool_name == "monitor_update" else "no loop was armed"
        )
        return (
            f"{tool_name}: NOT applied — {hosting_refusal.strip()}. This session's "
            "memory mode cannot be relaxed, so the same call will be refused every "
            f"time. Tell the user in this turn that {not_done}, and do NOT report "
            "monitoring as started or changed. A persistent session can host the loop."
        )
    if reading.get("active"):
        return ""
    monitor = reading.get("monitor")
    if not isinstance(monitor, dict):
        return ""
    outcome = monitor.get("outcome")
    if not retained_outcome_blocks_rearm(outcome, monitor.get("stopped_reason")):
        return ""
    target = str(monitor.get("target") or "").strip()
    return (
        f"{tool_name}: NOT applied — this session's automation binding still holds a "
        f"STOPPED monitor"
        + (f" on {target}" if target else "")
        + f" whose outcome ({outcome}) is retained as evidence, so a re-arm here is "
        "refused and nothing would be watched. The record is deliberately not "
        "replaceable by an agent: only the session's owner can clear it, from the "
        "dashboard's goal/automation popover (Clear), after which this call will "
        "succeed. Tell the user that is the one step needed, and do NOT report "
        "monitoring as started. Use monitor_inspect to read the retained record."
    )


def _parsed_pull_request_target(kind: Any, raw: Any) -> tuple[str, str]:
    """Return ``(url, "")`` for a valid PR target, or ``("", "Error: …")``.

    Guard normalization before emitting a new monitor. Target normalization
    raises, and a raise from a directive tool escapes this server's own return path: the
    JSON-RPC layer turns it into the same ``"Error: …"`` text, but past the point
    that tags a decline as a refusal, so the consumer reads it as a LOST directive
    marker and fires the WARNING reserved for a transport regression. Guarding the
    two sites separately would let a second site go unguarded; a
    single seam makes the next caller correct by construction.
    """
    try:
        return (
            normalize_pull_request_target(
                str(kind),
                str(raw),
                gitlab_hosts=KiroCrewConfig.load().dashboard.gitlab_hosts,
            ),
            "",
        )
    except ValueError as exc:
        return "", f"Error: {exc}"


def monitor_watch(name: str, args: dict[str, Any]) -> str:
    """Validate and emit a session-bound structured monitor directive."""
    args = validate_tool_args(args, MONITOR_WATCH_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_watch requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_watch", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_watch",
            sk,
            "monitor_watch only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}).",
        )
    target, target_error = _parsed_pull_request_target(args["kind"], args["target"])
    if target_error:
        return target_error
    # AFTER target validation so a malformed target keeps its own specific error,
    # and before the directive is emitted so the model is never handed a
    # success-shaped ack for an arm a retained stop guarantees will be refused.
    preflight_refusal = _arming_preflight_refusal("monitor_watch", sk)
    if preflight_refusal:
        return preflight_refusal
    payload = {
        "kind": args["kind"],
        "target": target,
        "objective": args["objective"],
        "cadence_secs": int(args.get("interval_secs") or DEFAULT_MONITOR_CADENCE_SECS),
        # Omitted: the default capped to the operator ceiling, as for monitor_start.
        "max_runtime_secs": int(
            args.get("max_runtime_secs")
            or min(DEFAULT_MONITOR_RUNTIME_SECS, runtime_ceiling_secs())
        ),
        # Tested for None rather than truthiness: 0 is the unlimited sentinel here
        # and is falsy, so `or` would silently replace an explicit "no ceiling"
        # with the default. The siblings keep `or` because 0 is invalid for them.
        "max_agent_turns": (
            DEFAULT_MONITOR_AGENT_TURNS
            if args.get("max_agent_turns") is None
            else int(args["max_agent_turns"])
        ),
        "max_tokens": int(args.get("max_tokens") or DEFAULT_MONITOR_TOKENS),
        "max_provider_errors": int(
            args.get("max_provider_errors") or DEFAULT_MONITOR_PROVIDER_ERRORS
        ),
        "wake_instructions": str(args.get("wake_instructions") or "").strip(),
    }
    return _emit_directive(
        "monitor_watch",
        payload,
        # Non-committal by construction: arming happens when this turn's result is
        # processed, so no text produced here can confirm it. It must therefore name
        # the NOT-armed notice, or a refused arm reads as monitoring started.
        "Structured monitor requested for this session; application is still pending. "
        "End your turn now. Arming happens when this turn's result is processed, so "
        "this ack cannot confirm it; the outcome is reported as a transcript notice "
        'on this session — "Automation loop armed: …" or "Automation loop NOT armed: '
        '<reason> [status N]". If the notice says NOT armed, read the reason before '
        "trying again — a monitor the user stopped is retained as evidence and only "
        "its owner can clear it. Do NOT report monitoring as started on the strength "
        "of this ack; verify with monitor_inspect at the start of a later turn or in "
        "response to a later wake, and treat active=true with a matching target as "
        "the only confirmation.",
    )


def monitor_inspect(name: str, args: dict[str, Any]) -> str:
    """Read the monitor bound to a verified strict session identity.

    Reports whichever shape the session's loop holds: a structured monitor's
    full record, or a legacy timer loop's presence and cadence reading. The
    session-monitor endpoint returns the legacy reading under ``autonudge_loop``,
    so a widened gate here lets a caller verify a timer loop is armed and firing
    rather than being told inspection is unavailable for its session type.
    """
    validate_tool_args(args, MONITOR_INSPECT_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "Monitor inspection unavailable without an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_inspect", sk, strict_err)
    # The GENERAL binding, so a legacy timer loop resolves here too (this also
    # admits a Webex session, which hosts a legacy loop but no structured
    # monitor). The endpoint distinguishes the shapes.
    if mcp_core._autonudge_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_inspect",
            sk,
            f"monitor_inspect is unavailable for this session type ({sk!r}).",
        )
    result = mcp_core._get("/api/autonudge/session-monitor", session_key=sk)
    if result.get("error"):
        return f"Error: Monitor inspection failed: {result['error']}"
    return json.dumps(
        _compact_monitor_inspection(result),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _compact_monitor_inspection(result: dict[str, Any]) -> dict[str, Any]:
    """Project the browser record into a bounded, agent-oriented status."""
    compact = {key: result.get(key) for key in ("enabled", "active", "monitor_id") if key in result}
    # Surface the auto-nudge loop reading so a caller can tell an armed
    # auto-nudge loop from nothing armed. It is already a bounded, fixed-key dict
    # from the handler, so it passes through as-is; absent on responses that
    # predate the field, and None when no loop is armed.
    if "autonudge_loop" in result:
        compact["autonudge_loop"] = result.get("autonudge_loop")
    raw = result.get("monitor")
    if not isinstance(raw, dict):
        compact["monitor"] = None
        return compact
    fields = (
        "kind",
        "target",
        "objective",
        "budgets",
        "cadence_secs",
        "last_observation_status",
        "last_observation_reason_code",
        "last_fingerprint",
        "last_wake_fingerprint",
        "wake_in_flight",
        "wake_count",
        "token_usage_known",
        "agent_turns",
        "input_tokens",
        "output_tokens",
        "probe_count",
        "provider_error_count",
        "consecutive_provider_errors",
        "last_probe_at",
        "created_ts",
        "last_decision",
        "last_wake_reason_code",
        "last_provider_error",
        "next_probe_at",
        "outcome",
        "stopped_reason",
        "user_stop_reason",
        "stopped_at",
    )
    monitor = {key: raw.get(key) for key in fields}
    observation = raw.get("last_observation")
    if isinstance(observation, dict):
        observation_fields = (
            "state",
            "draft",
            "head_revision",
            "mergeability",
            "review_decision",
            "blocking_review",
            "unresolved_review_threads",
            "review_threads_complete",
        )
        summary = {key: observation.get(key) for key in observation_fields}
        checks = observation.get("checks")
        if isinstance(checks, dict):
            check_summary: dict[str, Any] = {}
            for status in ("failed", "pending", "unknown"):
                values = checks.get(status)
                if isinstance(values, list):
                    check_summary[status] = values[:MAX_MONITOR_CHECK_NAMES]
                    check_summary[f"{status}_count"] = len(values)
            passed = checks.get("passed")
            if isinstance(passed, list):
                check_summary["passed_count"] = len(passed)
            # Displaced rows are counted, not listed: the count is what tells a reader
            # that rows were declassified, and the identities are in the full
            # observation for whoever needs them. A cut bucket spends its last slot on
            # a sentinel the compact reader never sees, so the count is taken off the
            # sentinel and the cut is said out loud beside it -- a bare length would
            # read as an exact total at exactly the bound, which is where a cut is
            # likeliest. The live buckets need neither, because they are listed and
            # their own sentinel travels with them.
            superseded = checks.get("superseded")
            if isinstance(superseded, list):
                cut = PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY in superseded
                check_summary["superseded_count"] = len(superseded) - (1 if cut else 0)
                if cut:
                    check_summary["superseded_incomplete"] = True
            summary["checks"] = check_summary
        monitor["observation"] = summary
    compact["monitor"] = monitor
    return compact


def monitor_stop(name: str, args: dict[str, Any]) -> str:
    """Emit a durable stop directive for this session's monitor, without caller identity."""
    args = validate_tool_args(args, MONITOR_STOP_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_stop requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_stop", sk, strict_err)
    # The GENERAL binding, so a legacy timer loop resolves here too (and a Webex
    # session, which hosts a legacy loop but no structured monitor). A stop that
    # answered only for a structured monitor is a silent no-op on the loop shape
    # most sessions run: the caller believes the loop ended while it keeps
    # firing. The applier routes by the resolved loop's shape.
    if mcp_core._autonudge_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_stop",
            sk,
            "monitor_stop only works from within a dashboard, Slack, Discord, "
            f"or Webex session (current session_key={sk!r}).",
        )
    return _emit_directive(
        "monitor_stop",
        {"reason": str(args.get("reason") or "").strip()},
        "Monitor stop requested for this session.",
    )


def monitor_update(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_UPDATE_SCHEMA)
    # STRICT resolution via the shared gate, same rationale as
    # monitor_start/autonudge_stop: this mutates persistent loop state that
    # drives unattended turns, so a subagent must not PID-walk into the
    # parent's identity and rewrite the parent session's instruction.
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_update requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    # Stateless: short-circuit only un-appliable contexts; the
    # consumer resolves the loop by its own session and patches it.
    if not sk:
        return _monitor_context_refusal("monitor_update", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_update",
            sk,
            "monitor_update only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}).",
        )
    patch: dict[str, Any] = {}
    if args.get("message") is not None:
        new_message = str(args["message"]).strip()
        if not new_message:
            return "monitor_update: message must not be empty (omit it to leave unchanged)."
        patch["message"] = new_message
    if args.get("interval_secs") is not None:
        patch["idle_secs"] = int(args["interval_secs"])
    if args.get("max_cycles") is not None:
        patch["max_cycles"] = int(args["max_cycles"])
    if args.get("max_runtime_secs") is not None:
        patch["max_runtime_secs"] = int(args["max_runtime_secs"])
    if args.get("target") is not None:
        # The authoritative applier validates the target against the retained kind.
        patch["target"] = str(args["target"])
    if args.get("objective") is not None:
        patch["objective"] = str(args["objective"])
    for field in ("max_agent_turns", "max_tokens", "max_provider_errors"):
        if args.get(field) is not None:
            patch[field] = int(args[field])
    if args.get("wake_instructions") is not None:
        patch["wake_instructions"] = str(args["wake_instructions"]).strip()
    # Blank is KEPT here, unlike ``message`` above which rejects it: a loop with
    # no instruction cannot fire, but a loop with no banner is the default state,
    # so "" has to round-trip as a request to CLEAR. Dropping it as "unchanged"
    # would make a banner set once impossible to remove without tearing the loop
    # down and losing its cycle count.
    if args.get("banner") is not None:
        patch["banner"] = str(args["banner"]).strip()
    # An empty object is KEPT, for the reason a blank banner is: ``{}`` is how an
    # owner's own CRITERIA come off a live loop, which returns it to the shipped
    # default brief rather than taking the judge off -- ``false`` is the bypass, and
    # it normalises to a reserved marker instead of to this shape. Dropping ``{}`` as
    # "unchanged" would make a brief set once impossible to clear without tearing the
    # loop down. Validated here as well as on the arm path, because this is the other
    # door into the same stored field and an unvalidated one would be the way around
    # the bound.
    if args.get("judge") is not None:
        try:
            patch["judge"] = validate_judge_spec(args["judge"])
        except ValidationError as exc:
            return f"monitor_update: {exc}"
    # Blank is DROPPED here, unlike ``banner`` and ``judge`` above: there is no "clear the
    # watch" request to express. Disarming a subject is what ``gate: false`` on a fresh arm
    # is for, and a loop silently losing its watch through a metadata edit is the "looks
    # armed, observes nothing" state the service's own fold exists to prevent.
    if str(args.get("watch") or "").strip():
        patch["watch"] = str(args["watch"]).strip()
    if not patch:
        mcp_core.sel().log_tool_invocation(
            session_key=sk, source="mcp", tool_name="monitor_update", outcome="noop"
        )
        return (
            "monitor_update: nothing to change — pass at least one of "
            "message, interval_secs, max_cycles, max_runtime_secs, target, "
            "objective, max_agent_turns, max_tokens, max_provider_errors, "
            "wake_instructions, banner, judge, watch."
        )
    # AFTER the empty-patch no-op so that more specific answer still wins. A
    # retained stop cannot be updated either: ``update_monitor`` answers "not found
    # or already terminal" at the turn boundary, and unlike the two arming
    # directives a refused monitor_update gets no transcript notice at all — so
    # without this the retarget failure is invisible to both the model and the user.
    preflight_refusal = _arming_preflight_refusal("monitor_update", sk)
    if preflight_refusal:
        return preflight_refusal
    return _emit_directive(
        "monitor_update",
        {"patch": patch},
        f"Monitor-loop update requested for this session "
        f"({', '.join(sorted(patch))}); it applies only if a loop is active "
        "here, so do not assume the change landed.",
    )
