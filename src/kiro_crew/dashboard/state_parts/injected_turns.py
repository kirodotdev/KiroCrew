"""The markers and prompts of turns the dashboard injects into a slot.

Cron, subagent and monitor envelope prefixes, every ``*_RECOVERY_PREFIX`` the
recovery ladder stamps on a self-queued continuation (``RecoveryCard.tsx`` mirrors
that list; ``test/test_recovery_marker_parity.py`` reads it from this file), and
the deciders and builders of those continuation prompts. A new marker is defined
here, next to the others.
"""

from __future__ import annotations

import json
import re

from kiro_crew.constants import SUBAGENT_BATCH_COMPLETION_PREFIX, SUBAGENT_COMPLETION_PREFIX
from kiro_crew.deny_guidance import remediation_for

# Structured monitor wakeups are automation, not user speech. The controller
# owns the complete envelope; every delivery surface passes it through unchanged.
MONITOR_WAKE_PREFIX = "[Monitor wake]"


# Cron notification wrapper format — used by handlers.py (create), chat.py (detect), transcriptRendering.tsx (render)
CRON_NOTIFY_PREFIX = "[Cron notification from "


CRON_NOTIFY_END = "[End of cron notification]"


CRON_NOTIFY_RE = re.compile(rf'^{re.escape(CRON_NOTIFY_PREFIX)}"(.*)"\]')


# Both sub-agent markers, for the checks that must treat either shape as a system
# injection. Pass this straight to ``str.startswith`` (it accepts a tuple) instead
# of listing the prefixes per call site: the batch marker is a SIBLING of the
# per-agent one rather than an extension of it, so a per-prefix check written
# against one silently misses the other, and a third shape would miss both.
SUBAGENT_COMPLETION_PREFIXES = (
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
)


# One-shot synthesis turn fired after ALL sub-agents in a fan-out complete and
# each result has been processed in its own turn (see gateway._subagent_done arm
# + chat_runner drain/idle branch). Its visible reply is the consolidated,
# user-facing summary. Rendered as an "inject" message (not a user bubble); the
# prefix marks it as a synthetic continuation so it is NOT mirrored to linked
# surfaces (Slack/Telegram) as though the user typed it.
SUBAGENT_SYNTHESIS_PREFIX = "[SYSTEM] Sub-agent synthesis:"


SUBAGENT_SYNTHESIS_PROMPT = (
    f"{SUBAGENT_SYNTHESIS_PREFIX} all sub-agents you spawned have completed and each result was "
    "processed above. Produce a single consolidated synthesis as your reply for the user: "
    "(1) restate the original goal you spawned the sub-agents for, (2) synthesize the combined "
    "findings across all of them (do not just repeat each result in turn), and (3) give concrete "
    "recommended next actions or decisions. This is the user-facing deliverable — keep it clear "
    "and actionable."
)


# Synthetic continuation injected after a recoverable tool refusal (host-gate
# policy deny or the read-only bash gate) ended a turn early. Carries the
# refusal reason back to the model so it can adapt instead of stalling for the
# user. Rendered as an "inject" message (not a user bubble) and never mirrored
# to a linked Slack thread as user input.
REFUSAL_RECOVERY_PREFIX = "[Tool refusal — automatic recovery]"


# Synthetic continuation injected after a genuinely-wedged (stale) turn was
# detected + reset. Tells the model its previous turn was interrupted by a
# system stall — NOT the user — and to resume from its last committed step
# rather than restart. Rendered as an "inject" message (not a user bubble) and
# never mirrored to a linked Slack thread as user input.
STALE_RECOVERY_PREFIX = "[Stalled turn — automatic recovery]"


# Synthetic continuation injected after the per-session watchdog judged an
# in-flight tool dead/stuck and cancelled the session. Unlike the legacy path
# (which re-queued the ORIGINAL user message verbatim — restarting the whole
# task from scratch), this hands the model the stall context so it can check
# partial results and continue. Rendered as an "inject" message (not a user
# bubble) and never mirrored to a linked Slack thread as user input.
TOOL_STALL_RECOVERY_PREFIX = "[Tool stall — automatic recovery]"


# Prefix on the continuation injected after a reset recovers an interrupted
# connection. The body lives in chat_utils so queue provenance and turn routing
# share one canonical instruction.
CONN_RECOVERY_PREFIX = "[Connection lost — automatic recovery]"


# Prefix on the continuation injected when a reset recovers a turn the backend
# refused because the session was still busy. Separate from
# CONN_RECOVERY_PREFIX even though both requeue the same continuation shape:
# nothing was disconnected, and the marker is what the transcript renders, so
# sharing the connection marker would report a dropped connection to a user
# whose status card reads "Session busy". Body: _BUSY_RECOVER_MSG in chat_utils.
BUSY_RECOVERY_PREFIX = "[Session busy — automatic recovery]"


# Prefix on the runner-injected CONTINUE that resumes a turn cut short by a
# transient backend 5xx after tokens/tools had already streamed. The body lives
# in chat_utils as _POSTTOKEN_RECOVER_MSG; the prefix is here so all
# recovery markers share one home and the frontend has one list to mirror.
POSTTOKEN_RECOVERY_PREFIX = "[Interrupted turn — automatic recovery]"


# Prefix on the runner-injected nudge that breaks a repeated empty-generation
# pattern (the model returned no output twice). Body: _EMPTY_AUTO_CONTINUE_MSG.
EMPTY_RESPONSE_RECOVERY_PREFIX = "[Empty response — automatic recovery]"


# Prefix on the runner-injected continuation sent when a turn ended on a
# PROMISE-ONLY final message: the model announced an immediate action ("I'll do
# that now") and then yielded without making the tool call, so the work never
# happened and the turn still billed. Body: _PROMISE_ONLY_CONTINUE_MSG in
# chat_utils. One bounded attempt (slot._promise_only_retries), never a loop.
PROMISE_ONLY_RECOVERY_PREFIX = "[Unfinished action — automatic recovery]"


# Prefix on the runner-injected continuation sent when the BACKEND compacted the
# conversation in the middle of a turn and then ended the turn without finishing
# the work. The compaction itself succeeded — nothing failed — but the request
# that was in flight when the context filled was abandoned, and the turn lands
# looking clean (a settled footer with elapsed time), so without this the chat
# just stops. Body: _COMPACTION_CONTINUE_MSG in chat_utils. One bounded attempt
# (slot._compaction_continue_retries), never a loop.
COMPACTION_RECOVERY_PREFIX = "[Context compacted — automatic recovery]"


# Prefix on the continuation injected when the USER pressed Continue on an
# interrupted turn. Body: _MANUAL_RESUME_MSG in chat_utils. Named into the
# *_RECOVERY_PREFIX family because test_recovery_card_prefixes.py keys the
# cross-language drift guard on that suffix — a marker outside the family is
# invisible to it, and the card would silently render machine prose as a bubble.
# The VALUE is what carries the user-facing meaning, and it deliberately does NOT
# say "automatic recovery" like the automatic-recovery markers: a person pressed
# the button, and the card must not claim the system recovered by itself.
MANUAL_RESUME_RECOVERY_PREFIX = "[Continue — requested by the user]"


# Prefix on the continuation injected when a content-filter refusal landed AFTER
# the turn had already dispatched tool calls and agent.refusal_fallback_model
# names a different model. The user's message is NOT replayed there -- the
# completed tool calls would run a second time -- so the session is moved to the
# fallback model and asked to carry on from the completed work, the same
# continuation a person gets from Continue. Body: _REFUSAL_FALLBACK_RESUME_MSG
# in chat_utils. Named into the *_RECOVERY_PREFIX family so
# test_recovery_card_prefixes.py's drift guard sees it. The VALUE names the
# cause (a model's filter) and the remedy (another model); it says neither
# "requested by the user" (nobody pressed anything) nor "automatic recovery"
# (nothing faulted -- the model declined).
REFUSAL_FALLBACK_RECOVERY_PREFIX = "[Content filter — continuing on the fallback model]"


# Prefix on the continuation injected when a Stop hook returns a block decision
# (`{"decision": "block", "reason": ...}` on exit-0 stdout). The reason IS the
# instruction, handed back as the next turn so a hook can steer the session
# without a round-trip to the user. Named into the *_RECOVERY_PREFIX family so
# test_recovery_card_prefixes.py's drift guard sees it — a marker outside the
# family renders as a full-width bubble instead of a card. The VALUE deliberately
# does not say "recovery": the turn completed and a hook asked for another, so
# nothing failed and nothing was recovered.
HOOK_CONTINUATION_RECOVERY_PREFIX = "[Hook continuation — automatic]"


# Prefix on the informational row surfaced when a Stop-hook continuation run hits
# the `agent.max_stop_hook_nudges` cap: the next block decision is refused, no
# turn is dispatched, and this row is appended instead so the transcript shows
# the loop was force-stopped (with the reached depth as "#N"). Named into the
# *_RECOVERY_PREFIX family so test_recovery_card_prefixes.py's drift guard sees
# it — a marker outside the family renders as a full-width bubble, not a card.
# The VALUE does not say "recovery": nothing failed or recovered, a safety cap
# fired.
HOOK_HALTED_RECOVERY_PREFIX = "[Stop-hook nudge cap reached]"


# Prefix on the DISPLAY-ONLY row appended when a tool deny's reason was steered
# into the running turn (see chat_runner._steer_policy_notice). Nothing is
# queued and no turn is dispatched — the agent already has the reason — so this
# row exists purely so the person sees the blocked-tool card, instead of only a
# generic "Steered" chip that reads as though they had steered the turn
# themselves.
#
# Named into the *_RECOVERY_PREFIX family because test_recovery_card_prefixes.py
# keys its cross-language drift guard on that suffix — a marker outside the
# family is invisible to it and the row would render as a full-width bubble of
# machine prose. The VALUE deliberately does not say "recovery": nothing was
# recovered and no continuation was sent, which is the whole point. Same
# reasoning as HOOK_HALTED_RECOVERY_PREFIX, whose row is also display-only.
REFUSAL_INBAND_RECOVERY_PREFIX = "[Tool blocked — reason sent to the agent]"


def should_queue_refusal_recovery(
    refusal_reasons: list,
    needs_reset: bool,
    *,
    user_stopped: bool,
    notices_sent: int = 0,
    notices_pending: int = 0,
) -> bool:
    """Decide whether to auto-queue a refusal-recovery prompt after a turn.

    Returns False (skip recovery) when:
    - No refusals occurred
    - A session reset is already re-queuing
    - The user stopped the turn (``user_stopped``: a stop still in flight, or
      one that pressed and resolved during the turn)
    - Every refusal was already explained IN-BAND and the backend confirmed it

    ``user_stopped`` is the host's own Stop signal, read LIVE at the call: a stop
    in flight (``slot._stopping``), ``slot._stop_generation`` moved since the
    turn began, or the session manager's stop count for the turn's session key
    moved (a stop issued from a linked channel surface). It is the only
    user-cancel input this gate takes; the backend's wire ``stopReason`` is
    deliberately not one. The two are not the same thing: codex-acp's command
    approval advertises ``cancel`` as its ONLY reject option (measured on
    codex-acp 1.11.0 / codex 0.153.4 -- there is no ``decline``), and codex
    answers that reject by aborting the whole turn with ``stopReason:
    "cancelled"`` before the model is called again. A gate that read that stop
    reason as a Stop press skipped this continuation on every policy block, and
    on codex this continuation is the only channel that reaches the model (the
    turn itself is gone, so no in-band notice can). A backend abort with
    refusals recorded and no Stop pressed is the refusal's own consequence, and
    the continuation is exactly what is owed.

    The parameter is keyword-only and REQUIRED so no caller can reintroduce a
    stop-reason rule by omission. Callers must read it at the gate, not from a
    snapshot taken before an await: a Stop that presses and resolves during an
    awaited Stop hook leaves ``slot._stopping`` False again, and only the
    generation counters still say it happened. The in-flight stop is part of
    that signal rather than a parameter of its own, so a caller cannot pass a
    stale in-flight read next to a live one.

    ``notices_sent`` is how many :func:`build_refusal_steer_notice` bodies were
    steered into the turn, and ``notices_pending`` how many of those the
    ``steering_consumed`` echo did NOT account for. The extra turn is skipped only
    when every refusal got a notice AND none is still pending -- an unconfirmed
    steer is treated as undelivered, so the fallback continuation still runs. The
    check is deliberately coarse (counts, not a per-refusal pairing): its two
    failure directions are not symmetric. Skipping wrongly leaves the model with
    kiro-cli's "User denied tool execution" and no correction, while queueing
    wrongly costs only one extra turn in which the model is told twice. Both
    keep defaults so a caller on a harness without mid-turn steer
    behaves as if nothing was steered.
    """
    if refusal_reasons and notices_sent >= len(refusal_reasons) and notices_pending == 0:
        return False
    return bool(refusal_reasons and not needs_reset and not user_stopped)


def should_queue_hook_continuation(needs_reset: bool, *, user_stopped: bool) -> bool:
    """Decide whether a Stop hook's block decision may inject a continuation.

    Mirrors :func:`should_queue_refusal_recovery`'s suppression set so a hook can
    never override the Stop button: a pending session reset, or a Stop issued
    during the turn (in flight or already resolved, from any surface), both win
    over the hook. Like that gate it takes the host's live Stop signal and not
    the backend's wire ``stopReason``: a backend that aborts a policy-denied
    turn (codex) reports ``cancelled`` with no Stop pressed, and a hook
    continuation is owed there just as the refusal continuation is.
    """
    return bool(not needs_reset and not user_stopped)


def parse_hook_continuations(stdouts: list[str]) -> list[str]:
    """Extract continuation instructions from Stop-hook exit-0 stdout texts.

    ``stdouts`` is what ``_fire`` returns for the Stop event: one entry per exit-0
    hook, plus ``BLOCKED:`` markers for exit-2 denials. Only a well-formed block
    decision carrying a non-blank ``reason`` contributes, because ``reason`` is
    the message that gets injected — a block without one has nothing to say, so
    the turn stops normally. Every other string is ignored, which is what keeps an
    ordinary Stop hook that merely logs from continuing the session.
    """
    reasons: list[str] = []
    for stdout in stdouts:
        try:
            decision = json.loads(stdout)
        except (ValueError, TypeError, RecursionError):
            # RecursionError is a RuntimeError, not a ValueError: json.loads
            # raises it on deeply-nested input, and a pathological hook must not
            # error an otherwise-successful turn.
            continue
        if not isinstance(decision, dict) or decision.get("decision") != "block":
            continue
        reason = decision.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons.append(reason)
    return reasons


def build_refusal_recovery_prompt(
    refusals: list[tuple[str, str]],
    *,
    credential_tool_hint: str = "",
    answered: bool = False,
    turn_aborted: bool = False,
) -> str:
    """Build the body of an automatic continuation after a recoverable tool refusal.

    When a tool call is refused for a recoverable, system-side reason — a
    host-gate policy deny, the read-only bash safety gate, or a PreToolUse policy
    hook block — the reason reaches the dashboard pill and the SEL audit log but
    never the model: kiro-cli's own tool result for a rejected permission is the
    fixed string "User denied tool execution", which is indistinguishable from a
    human having clicked No. So the agent apologises for a cancellation that
    never happened and yields.

    This continuation is the FALLBACK path. The primary path is
    :func:`build_refusal_steer_notice`, which delivers the same reason in-band on
    a harness that supports mid-turn steer, costing no extra turn. This one runs
    when that was impossible (harness without steer) or when the steer was never
    folded in (no ``steering_consumed`` echo covered it).

    ``refusals`` is a list of ``(tool_title, reason)`` tuples recorded during the
    turn (already redacted by the caller). The returned text hands those reasons
    back to the model and frames the block as a system policy decision — NOT a
    user cancellation — so the agent can adapt (an allowed alternative, a
    different tool) or stop on its own with a reason. The caller prepends
    :data:`REFUSAL_RECOVERY_PREFIX`. Returns "" if there is nothing to recover.

    ``answered`` says the turn ALREADY streamed text the user has read, despite
    the block. The premise of the default wording — "the turn ended early, pick up
    where you left off" — is then false, and acting on it makes the model re-answer
    a question the user has already read, once per blocked call and at full turn
    cost. So the body flips to awareness-only: same block reasons, same
    remediation, but an explicit instruction not to restate what was sent.

    That flag deliberately does NOT claim the turn *finished* — no caller can tell
    a delivered answer from a one-line preamble ("Let me check the logs.") before
    the blocked call, because the two are indistinguishable prose flushed at the
    same point in the stream. So this branch conditions its instruction on whether
    the task is done rather than asserting it: continue-from-there is the default
    and stopping is the narrow case. Asserting a finished answer here would tell a
    turn that had only narrated its intent to stop with the work undone.
    The reason still has to be delivered rather than dropped, because on a backend
    without mid-turn steer this turn is the ONLY channel for it — without it the
    model's last word on the subject is kiro-cli's "User denied tool execution",
    and it will keep attributing the block to the user in later turns.

    ``turn_aborted`` says the backend ended the blocked turn as CANCELLED rather
    than letting it run on -- codex, whose only reject option aborts the turn.
    Codex then tells the model, in its own words, that the turn was interrupted
    ("aborted by user" on the tool result, a ``<turn_aborted>`` note saying the
    user interrupted on purpose). Those words are wrong here and they arrive
    right next to this continuation, so the body has to name and overrule them
    explicitly; the generic "not a user action" sentence alone loses to two
    harness-authored messages saying the opposite.

    Lives here (a leaf module that owns the prefix) rather than in context.py so
    chat_runner can import it at module top without a circular import. There is
    deliberately no retry cap: the model decides when to stop, and the user's
    Stop button remains the hard breaker.
    """
    if not refusals:
        return ""
    lines = [
        (
            "One or more tool calls in your previous turn were blocked by a Kiro "
            "Crew safety policy. This was NOT a user action — do not treat it as a "
            "cancellation or interruption by the user. That turn already put text "
            "on screen for the user, so this note is for awareness: carry on from "
            "there rather than starting over."
            if answered
            else "One or more tool calls in your previous turn were blocked by a "
            "Kiro Crew safety policy, which ended the turn early. This was NOT a "
            "user action — do not treat it as a cancellation or interruption by "
            "the user."
        ),
    ]
    if turn_aborted:
        lines.append(
            "The backend then reported that turn as aborted or interrupted (a tool "
            "result reading 'aborted by user', or a note that the user interrupted "
            "the previous turn on purpose). That abort was the consequence of the "
            "blocked call, not an interruption by the user -- disregard those "
            "messages."
        )
    lines += ["", "Blocked:"]
    for title, reason in refusals:
        lines.append(f"  - {title}: {reason}" if reason else f"  - {title}")
    lines += [
        "",
        (
            "Do NOT repeat, restate or re-derive what you already sent — the user "
            "has read it. If the task is NOT finished, continue from there: use an "
            "allowed alternative (for a shell command, a read-only variant) or a "
            "different tool, and say what it changed. Only if the task IS finished "
            "and the block left nothing missing, reply with one short line noting "
            "the block and stop."
            if answered
            else "Decide how to proceed: use an allowed alternative (for a shell "
            "command, a read-only variant), a different tool, or — if the block is "
            "correct and you genuinely cannot proceed — say so and stop. Otherwise "
            "continue the task where you left off."
        ),
    ]
    # Per-class remediation, de-duplicated across the turn's refusals: several
    # blocked calls in one turn are usually the same wall hit from different
    # angles, and repeating identical prose per bullet buries the one instruction
    # that differs. Ordered by first appearance so the earliest refusal's
    # guidance leads.
    guidance: list[str] = []
    for title, reason in refusals:
        text = remediation_for(reason, title, credential_tool_hint=credential_tool_hint)
        if text and text not in guidance:
            guidance.append(text)
    if guidance:
        # NOT rendered as `  - ` bullets: RecoveryCard counts every bullet-shaped
        # line in this body as one blocked tool call (`BULLET_RE`), so guidance in
        # that shape would inflate the card's "N blocked" count with prose. The
        # bullet list above is the wire form of the blocked-item count; this
        # section is prose about it, and the two must stay distinguishable.
        lines += ["", "How to do this properly:"]
        for text in guidance:
            lines += [f"    {text}"]
    return "\n".join(lines)


#: The in-band deny notice (cause wording, builder, bounded steer helper) lives
#: in ``kiro_crew.deny_notice``, a leaf the messaging core may import; the names
#: are re-exported from this module (see the import block) for the dashboard's
#: existing importers.


def build_stale_recovery_prompt() -> str:
    """Body of the continuation injected after an auto-recovered stalled turn.

    A previous turn wedged: the ACP layer detected a genuinely stale turn (total
    stdout+stderr silence past the timeout), probed it via ``session/cancel``, got
    no ack, and the dashboard reset the session. The prior work already committed
    to the conversation is restored by ``session/load`` resume; this nudge tells
    the model to CONTINUE from that last committed step rather than restart the
    task from scratch. The caller prepends :data:`STALE_RECOVERY_PREFIX`. Framed
    as a system stall — NOT a user cancellation — so the agent doesn't stop.
    """
    return (
        "Your previous turn was interrupted by a system stall and has been "
        "automatically recovered. This was NOT a user action — do not treat it "
        "as a cancellation or interruption by the user. The work you already "
        "completed is preserved in the conversation above. Continue from where "
        "you left off and finish the task; do not restart it or repeat steps "
        "that already succeeded."
    )


# Shell output-redirection target, e.g. `> build.log` / `>> build.log`. The
# operator must open a token: nothing but whitespace (or the start of the
# string) may precede it, optionally through a single fd digit (`2>`, `1>>`)
# or the both-streams `&>`. That keeps the `>` inside `->` and `=>` — Markdown
# prose, JS fat arrows — from reading as a redirect, which matters because the
# scanned text is the stalled tool's raw input and is a file's content when the
# tool is a file write. The target class excludes `&` so fd-dup forms (`2>&1`,
# `>&2`) self-exclude, and `)` so a path at the end of a parenthesis does not
# carry the parenthesis along.
_REDIRECT_TARGET_RE = re.compile(r"(?<![^\s])(?:\d|&)?>>?\s*([^\s;|&)]+)")


def extract_log_redirect_target(command: str) -> str:
    """The first real file a shell command redirects output into, or "".

    Used by the tool-stall recovery nudge: when a long command redirected its
    output (long commands typically redirect, e.g. ``> build.log 2>&1``), the model
    should inspect that file's tail instead of blindly re-running the command.
    ``/dev/null`` and fd-dups (``2>&1``) are ignored, and so is a ``>`` that is
    part of another token (``->``, ``=>``): the text scanned is whatever input the
    stalled tool received, which for a file write is the file's content.
    """
    for m in _REDIRECT_TARGET_RE.finditer(command or ""):
        target = m.group(1).strip("\"'")
        if not target or target == "/dev/null":
            continue
        return target
    return ""


def build_tool_stall_recovery_prompt(
    tool_title: str,
    idle_secs: int,
    command: str = "",
    stuck_input: bool = False,
) -> str:
    """Body of the continuation injected after a watchdog tool-stall cancel.

    The per-session watchdog judged an in-flight tool dead (its process exited
    without a result frame), stuck on interactive input, or opaque past the
    UNKNOWN budget, and cancelled the session's turn. This nudge is a SYSTEM
    action — NOT a user cancellation — and replaces the legacy behavior of
    re-queuing the original user message verbatim (which restarted the entire
    task and re-ran the very command that stalled). The caller prepends
    :data:`TOOL_STALL_RECOVERY_PREFIX`.
    """
    idle_mins = max(1, round(idle_secs / 60))
    tool_label = tool_title or "a tool call"
    lines = [
        f"Your previous turn stalled: {tool_label} produced no response for "
        f"~{idle_mins} minute(s) and the turn was ended by a Kiro Crew watchdog. "
        "This was NOT a user action — do not treat it as a cancellation or "
        "interruption by the user.",
        "",
        "Before doing anything else, check whether the tool actually completed "
        "or left partial results — do NOT blindly re-run the whole task or "
        "repeat steps that already succeeded.",
    ]
    log_target = extract_log_redirect_target(command)
    if log_target:
        lines += [
            "",
            f"The command's output was redirected to `{log_target}` — inspect it "
            "with tail (last ~50 lines); do NOT cat the whole file.",
        ]
    if stuck_input:
        lines += [
            "",
            "The command appeared to be waiting for interactive input it will "
            "never receive. Re-run it non-interactively (e.g. with -y, "
            "--no-input, or </dev/null) instead of repeating it as-is.",
        ]
    lines += [
        "",
        "Then continue the task from where you left off.",
    ]
    return "\n".join(lines)


def build_infra_retry_prompt(error_class: str, retry_after_secs: float | None) -> str:
    """The L1 continuation: retry the refused call, nothing else.

    Deliberately NOT a replay of the user's message: tool calls earlier in the
    turn may have taken effect. The model is told which call failed and why,
    and asked to issue that same call again. Opens with
    ``REFUSAL_RECOVERY_PREFIX``: a capacity refusal IS a tool refusal carried
    back to the model, and that is the card the dashboard already renders for
    one -- a new marker would need its own card row and catalog copy.
    """
    hint = (
        f" The server asked for a {int(round(retry_after_secs))}s pause, which has elapsed."
        if retry_after_secs
        else ""
    )
    return (
        f"{REFUSAL_RECOVERY_PREFIX}\n"
        "Your last tool call was refused by the MCP gateway for a transient "
        f"infrastructure reason ({error_class}), not because of its arguments."
        f"{hint} Re-issue exactly that tool call now with the same arguments and "
        "continue from its result. Do not repeat any earlier tool call that "
        "already returned a result."
    )
