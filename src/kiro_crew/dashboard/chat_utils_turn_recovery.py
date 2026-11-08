"""How a turn ended, and whether the runner continues it.

Normalised stop reasons, the empty-turn causes and the rungs of their recovery ladder,
false current-tool blockers, the promise-only and post-compaction continuation
decisions, and the reset causes whose requeue resumes an interrupted turn.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from kiro_crew.dashboard.chat_utils_injected_rows import (
    _BUSY_RECOVER_MSG,
    _CONN_RECOVER_MSG,
    RecoveryPayload,
    payload_for_replay,
)
from kiro_crew.dashboard.chat_utils_turn_text import is_promise_only_terminal
from kiro_crew.dashboard.state import (
    MANUAL_RESUME_RECOVERY_PREFIX,
    REFUSAL_FALLBACK_RECOVERY_PREFIX,
)
from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS

#: Normalised, CLOSED stop-reason vocabulary for the empty-turn diagnostic. The
#: raw wire value is never logged: a backend is free to invent a reason string,
#: and an unbounded value in a diagnostic is both a cardinality hazard and a
#: place model- or user-derived text could appear.
#:
#: The three literals are spelled here rather than imported from
#: ``kiro_crew.acp.types``, following the precedent in ``metrics/turns.py``:
#: ``scripts/check_agent_sdk_boundary.py`` refuses an ACP import in this module,
#: whose facade is baselined at ONE ACP edge. Duplicating a wire constant is
#: only safe with a guard, so ``test_dashboard_chat_empty_response.py`` pins each against the
#: ACP constant it mirrors — the test tree is outside the gate's scope, so the
#: pin can import what this module may not.
_STOP_END_TURN = "end_turn"
_STOP_CANCELLED_REASON = "cancelled"
_STOP_REFUSAL = "refusal"

#: A terminal event arrived carrying NO stop reason at all. Deliberately its own
#: value rather than folded onto ``end_turn``: ``metrics.turns.turn_outcome``
#: reads absence as a clean turn (correct for latency accounting, where the acp
#: path leaves it unset on every normal completion), but here the two are the
#: whole question — "the provider said the turn ended and produced nothing" is a
#: model-side event, while "the provider never said why it stopped" is a
#: transport-side one, and these diagnostics exist to tell them apart.
STOP_REASON_ABSENT = "absent"
#: A terminal stop reason outside the closed set above.
STOP_REASON_OTHER = "other"

#: Causes for a turn that reached the empty-response verdict. Closed set,
#: low-cardinality, content-free — safe for a log line and for a metric
#: attribute if one is ever added.
EMPTY_CAUSE_NO_TERMINAL = "no_terminal_event"
EMPTY_CAUSE_SYNTHETIC = "synthetic_completion"
EMPTY_CAUSE_VISIBLE_PARTIAL = "visible_partial"
EMPTY_CAUSE_TOOL_ONLY = "tool_only"
EMPTY_CAUSE_THINKING_ONLY = "thinking_only"
EMPTY_CAUSE_PROVIDER_EMPTY = "provider_empty"
EMPTY_CAUSE_OTHER = "other"

#: Which rung of the empty-response ladder claimed the turn.
EMPTY_RUNG_REPLAY = "replay"
EMPTY_RUNG_CONTINUE = "continue"
EMPTY_RUNG_GIVE_UP = "give_up"


def normalize_stop_reason(stop_reason: str | None) -> str:
    """Map a raw terminal stop reason onto the closed diagnostic vocabulary.

    ``None`` and ``""`` both answer :data:`STOP_REASON_ABSENT` — an omitted
    reason, which is a distinct observation from a clean ``end_turn`` and must
    not be laundered into one. Anything unrecognised answers
    :data:`STOP_REASON_OTHER`, so no raw backend string is ever logged.
    """
    raw = stop_reason or ""
    if not raw:
        return STOP_REASON_ABSENT
    if raw in (_STOP_END_TURN, _STOP_CANCELLED_REASON, _STOP_REFUSAL):
        return raw
    if raw.startswith("error:"):
        return "error"
    return STOP_REASON_OTHER


@dataclass(frozen=True)
class EmptyTurnActivity:
    """What a turn DID, in booleans only, as observed at the empty-response verdict.

    Every field is a bool or a value from a closed set. There are deliberately no
    counts, no durations, no token or credit numbers, no paths, no ids, and no
    text: this object exists to be written to a log line, and the incident it was
    built for is one where the interesting facts (did a tool run? did the user
    already read something?) are exactly the facts a privacy-safe diagnostic can
    carry. A count would answer no question the bool does not, and token counts
    and costs are billing data that has no business in a warning.

    ``billed`` is likewise a bool: whether the provider reported ANY billing
    dimension for the turn (``llm_helpers.usage_has_billing``). It separates the
    two shapes of "nothing came back" that look identical from the runner — a
    turn the provider generated and charged for, versus one it never ran.
    """

    #: A terminal ``EVENT_COMPLETE`` arrived. False means the stream ended
    #: without one, and every other field describes a turn nobody closed.
    saw_terminal: bool = False
    #: The terminal was SYNTHESIZED by the provider layer (watchdog, timeout,
    #: cancel-unacked) rather than reported by the backend. Retained past the
    #: event arm because the verdict below is reached long after it.
    terminal_synthetic: bool = False
    #: Normalised terminal stop reason — see :func:`normalize_stop_reason`.
    stop_reason: str = STOP_REASON_ABSENT
    #: At least one assistant text chunk streamed this turn.
    saw_text: bool = False
    #: A visible assistant segment was FLUSHED and persisted at a tool boundary,
    #: so the user has already read text this turn even though the final segment
    #: is empty.
    flushed_visible: bool = False
    #: At least one tool call was dispatched.
    had_tools: bool = False
    #: At least one thinking chunk arrived.
    had_thinking: bool = False
    #: The provider reported some billing dimension for the turn.
    billed: bool = False

    @property
    def productive(self) -> bool:
        """True when the turn did work that can carry state or side effects.

        This is the load-bearing predicate: a turn that is productive must NEVER
        have its originating message replayed verbatim, because the replay
        re-executes tool calls that already completed and re-derives an answer
        the user has already read. Text that merely STREAMED is not enough on its
        own — an un-flushed partial segment is still in ``assistant_text`` and is
        handled by the answer branch — so the three triggers are a flushed
        visible segment, a dispatched tool call, and thinking, each of which
        leaves the conversation in a state a replay would corrupt or duplicate.
        """
        return self.flushed_visible or self.had_tools or self.had_thinking


def classify_empty_turn(activity: EmptyTurnActivity) -> str:
    """Name the cause of an empty-response verdict, from the closed cause set.

    Ordered most-specific first, and the order is the point: the causes overlap
    (a synthesized terminal usually also has tool activity), so a flat set of
    predicates would report whichever the code happened to check first. The
    ranking is by what an operator must act on.

    1. :data:`EMPTY_CAUSE_NO_TERMINAL` — nobody closed the turn, so no other
       field can be trusted to describe a complete picture.
    2. :data:`EMPTY_CAUSE_SYNTHETIC` — the provider layer closed it, so the
       emptiness is ours, not the model's.
    3. :data:`EMPTY_CAUSE_VISIBLE_PARTIAL` — the user read an answer that a tool
       boundary flushed away; the turn is not empty in any sense the user would
       recognise.
    4. :data:`EMPTY_CAUSE_TOOL_ONLY` / :data:`EMPTY_CAUSE_THINKING_ONLY` — work
       happened with nothing said.
    5. :data:`EMPTY_CAUSE_PROVIDER_EMPTY` — a clean ``end_turn`` with no
       activity at all. The genuine provider-side empty, and the only cause for
       which replaying the original message is the right recovery.
    6. :data:`EMPTY_CAUSE_OTHER` — a closed terminal with no activity and no
       clean ``end_turn``, most importantly an OMITTED stop reason. Distinct
       from ``provider_empty`` on purpose: the incident's third attempt looked
       identical to a provider empty in the log and was not diagnosable.
    """
    if not activity.saw_terminal:
        return EMPTY_CAUSE_NO_TERMINAL
    if activity.terminal_synthetic:
        return EMPTY_CAUSE_SYNTHETIC
    if activity.flushed_visible:
        return EMPTY_CAUSE_VISIBLE_PARTIAL
    if activity.had_tools:
        return EMPTY_CAUSE_TOOL_ONLY
    if activity.had_thinking:
        return EMPTY_CAUSE_THINKING_ONLY
    if activity.stop_reason == _STOP_END_TURN:
        return EMPTY_CAUSE_PROVIDER_EMPTY
    return EMPTY_CAUSE_OTHER


# A current-turn blocker the model can only have invented.  Kiro Crew receives
# real tool failures through a refusal/error/result frame; a normal end_turn that
# follows a successful read/search call carries no runtime signal that tools were
# disabled.  Match the complete terminal response, not just its prefix: otherwise
# untrusted content could append an action and mint a synthetic action turn.
_FALSE_CURRENT_TOOL_BLOCKER_RE = re.compile(
    r"^\s*(?:"
    r"i(?:'|’)m blocked from further tool execution in the resumed session: "
    r"the screenshot delivery tools are no longer callable here\."
    r"|i(?:'|’)m proceeding, but this turn(?:'|’)s tool budget was exhausted "
    r"immediately after loading the workflow\. "
    r"No publish or deployment has happened yet\."
    r")\s*$",
    re.IGNORECASE,
)
_FALSE_CURRENT_TOOL_BLOCKER_NEAR_MISS_RE = re.compile(
    r"^\s*i(?:'|’)?m\b"
    r"(?=[^\n]*(?:tool|read|search|fetch))"
    r"(?=[^\n]*(?:block|unavail|inaccess|no\s+longer|budget|exhaust))"
    r"[^\n]+$",
    re.IGNORECASE,
)
# Replay additionally admits two first-party discovery tools whose contracts are
# read-only but which are not auto-approval candidates. Keep the shared host
# builtins sourced from the approval gate so those identities cannot drift.
_READ_ONLY_PREPARATION_TOOLS = _HOST_READ_ONLY_BUILTIN_TOOLS | frozenset(
    {"introspect", "tool_search"}
)


def is_false_current_tool_blocker(final_segment_text: str) -> bool:
    """Whether the assistant falsely claims THIS turn lost tool execution.

    This is deliberately narrower than a generic ``budget`` search.  A model may
    legitimately explain a subagent cap or quote an earlier failure; only a
    first-person claim about the live turn is actionable here.
    """
    return bool(_FALSE_CURRENT_TOOL_BLOCKER_RE.search(final_segment_text or ""))


def is_false_current_tool_blocker_near_miss(final_segment_text: str) -> bool:
    """Whether blocker-adjacent text missed the replay grammar.

    Diagnostic only: this predicate never grants replay authority. It lets the
    runner count wording drift without logging model text or widening the two
    incident statements accepted by :func:`is_false_current_tool_blocker`.
    """
    text = final_segment_text or ""
    return not is_false_current_tool_blocker(text) and bool(
        _FALSE_CURRENT_TOOL_BLOCKER_NEAR_MISS_RE.search(text)
    )


def tool_calls_are_read_only_preparation(
    turn_tool_calls: int,
    turn_tool_identities: tuple[tuple[str, str, str, bool], ...],
    successful_tool_call_ids: frozenset[str],
    *,
    builtin_identity_trusted: bool,
) -> bool:
    """True when every dispatch is a proven successful read-only builtin.

    Each identity is ``(tool_call_id, mcp_server_name, tool_name,
    tool_identity_trusted)``. The caller must positively prove the serving
    backend is Kiro, and every name must carry extractor provenance rather than
    merely being non-empty. MCP tools are excluded even when their names look
    read-only; their schemas are not owned by this host. Missing provenance,
    duplicate ids, absent identity, an unknown builtin, an incomplete/failed
    result, or any count mismatch fails closed.
    """
    if not builtin_identity_trusted:
        return False
    if turn_tool_calls <= 0 or len(turn_tool_identities) != turn_tool_calls:
        return False
    call_ids: list[str] = []
    for call_id, server_name, tool_name, identity_trusted in turn_tool_identities:
        if not all(isinstance(value, str) for value in (call_id, server_name, tool_name)):
            return False
        if (
            not call_id
            or server_name
            or identity_trusted is not True
            or tool_name not in _READ_ONLY_PREPARATION_TOOLS
        ):
            return False
        call_ids.append(call_id)
    if len(set(call_ids)) != len(call_ids):
        return False
    return frozenset(call_ids) == successful_tool_call_ids


def should_recover_promise_only(
    *,
    stop_reason: str,
    end_turn_reason: str,
    produced_visible_output: bool,
    final_segment_text: str,
    prompt_depth: int,
    promise_only_retries: int,
    is_cancelled: bool,
    refusal_reasons: list,
    turn_tool_calls: int = 0,
    turn_tool_identities: tuple[tuple[str, str, str, bool], ...] = (),
    successful_tool_call_ids: frozenset[str] = frozenset(),
    builtin_identity_trusted: bool = False,
    directive_user_origin: bool = False,
    stop_in_progress: bool = False,
    stop_generation_unchanged: bool = True,
    queue_empty: bool = True,
    no_pending_steers: bool = True,
) -> bool:
    """Decide whether to inject ONE unacted-turn continuation.

    All must hold (each guards a distinct failure mode):
      * NO Stop is in progress (``stop_in_progress`` is the runner's
        ``_should_suppress_requeue``). A soft Stop pressed while the promise
        streamed can lose the cancel race and arrive here as a normal
        ``end_turn``; re-queueing then would dispatch the very action the user
        tried to stop. Every sibling recovery path gates on this, so this one
        must too;
      * NO Stop was pressed at ANY point during this turn
        (``stop_generation_unchanged``: the monotonic ``slot._stop_generation``
        snapshotted at turn start still matches). A Stop that pressed AND
        resolved back to idle during the turn is invisible to ``stop_in_progress``
        but the user still cancelled; do not re-dispatch the announced action;
      * NO user follow-up is queued (``queue_empty``). ``queue_insert(0, ...)``
        would jump the continuation ahead of a user-typed "don't do that" or
        clarifying message; the user's queued input must process FIRST — respect
        it by falling through to a normal landing instead of overriding it;
      * NO mid-turn steer is pending (``no_pending_steers``). A steer lands in
        ``slot._pending_steers``, a SEPARATE channel from ``_queue``; it is only
        degraded into a queue card in ``_run_chat``'s ``finally``, which runs
        AFTER this guard. So a "don't delete" steer is invisible to
        ``queue_empty`` here, and firing recovery would schedule the announced
        action despite the just-arrived revocation. Abort when any user input
        exists, in either channel;
      * the turn ended NORMALLY (``end_turn``), not cancelled/refused/errored —
        those have their own paths and must stay unchanged;
      * it produced visible output (a promise IS visible output) and is not the
        empty-response case (that path owns ``not produced_visible_output``);
      * ordinary promise-only recovery still requires ZERO tool calls. A second,
        narrower shape may contain calls only when every dispatch has a unique id,
        the runner positively identified the serving backend as Kiro, the provider
        supplied a canonical non-MCP builtin identity carrying explicit extractor
        provenance, that identity is one of the fixed read/search/fetch tools,
        and a final
        ``status=completed`` result arrived for exactly the same id set. Display
        ``tool_kind`` and name non-emptiness are never authorization evidence.
        Unknown/MCP/missing/untrusted identity,
        duplicate ids, incomplete or failed calls, and every count/status mismatch
        fail closed. The tool-bearing shape also requires
        ``directive_user_origin``: the runner replays the authenticated user's
        exact message, never the model-authored blocker or an announced action.
        This lets a skill read or deferred-tool search retry without replaying a
        completed mutation or minting authority from fetched content;
      * the final segment is either a terminal promise-to-act
        (:func:`is_promise_only_terminal`) on a zero-call turn, or the narrowly
        full-matched false current-tool blocker above after read-only preparation.
        Because the runner resets its segment buffer at every tool boundary,
        ``final_segment_text`` is exactly the text AFTER the last tool call;
      * this is a top-level turn (``prompt_depth == 0``) and the one-shot budget
        is unspent (``promise_only_retries < 1``) — bounded to a single attempt,
        never a loop.
    Model-agnostic: nothing here keys on a model id.

    Language scope: the terminal-promise detector (``is_promise_only_terminal``)
    matches English commitment/immediacy tokens only; a non-English promise-only
    turn falls through and lands normally. Failure bias is
    safe (false negative, not false positive)."""
    if stop_in_progress or not stop_generation_unchanged or not queue_empty:
        return False
    if not no_pending_steers:
        return False
    if is_cancelled or refusal_reasons:
        return False
    if turn_tool_calls != 0 and not tool_calls_are_read_only_preparation(
        turn_tool_calls,
        turn_tool_identities,
        successful_tool_call_ids,
        builtin_identity_trusted=builtin_identity_trusted,
    ):
        return False
    if turn_tool_calls and not directive_user_origin:
        return False
    if stop_reason != end_turn_reason:
        return False
    if not produced_visible_output:
        return False
    if prompt_depth != 0 or promise_only_retries >= 1:
        return False
    if turn_tool_calls:
        return is_false_current_tool_blocker(final_segment_text)
    return is_promise_only_terminal(final_segment_text)


def should_continue_after_compaction(
    *,
    compaction_started: bool,
    compaction_settled: bool,
    user_requested_compaction: bool,
    final_segment_text: str,
    stop_reason: str,
    end_turn_reason: str,
    prompt_depth: int,
    compaction_continue_retries: int,
    is_cancelled: bool,
    refusal_reasons: list,
    stop_in_progress: bool = False,
    stop_generation_unchanged: bool = True,
    queue_empty: bool = True,
    no_pending_steers: bool = True,
) -> bool:
    """Decide whether to inject ONE post-compaction continuation.

    The scenario: the backend hit its context ceiling PART WAY THROUGH a turn,
    summarized the conversation in place, and then ended the turn without
    finishing the work it was doing. The compaction succeeded, so nothing
    reports an error and the turn lands as a clean ``end_turn`` with a settled
    footer — the request is simply abandoned, and the chat appears to stop dead.
    kiro-cli re-sends the pending request itself after compacting; the Claude
    backend does not, so the recovery has to live here.

    Unlike :func:`should_recover_promise_only` this needs no text detector and
    is deliberately NOT gated on ``turn_tool_calls == 0``: a mid-turn compaction
    is a hard fact reported by the backend, not a guess about what prose meant,
    and the turns that overflow the window are precisely the long tool-heavy
    ones. Re-dispatch safety comes from the continuation's own wording, which
    tells the model the summary records completed work and not to re-run it.

    All must hold:
      * a compaction STARTED and COMPLETED inside this turn
        (``compaction_started``/``compaction_settled``) — the turn really was
        interrupted by one, rather than merely following one. ``compaction_settled``
        means the COMPLETED terminal specifically, not any terminal: a compaction
        that FAILED also ends, and continuing after one would assert a summary
        exists when the context was never summarized;
      * the user did NOT ask for it (``user_requested_compaction`` is False for
        anything but an explicit ``/compact``). A deliberate ``/compact`` has
        done exactly what was asked and must land quietly; auto-continuing it
        would turn a housekeeping command into an unrequested turn;
      * the turn produced NO answer of its own after the compaction
        (``final_segment_text`` is blank). A backend that DID resume and finish
        leaves text here, so this is also what keeps the fix from double-
        prompting a backend that self-heals. A compaction NOTICE does not count
        as such an answer: the Claude adapter ships it as ordinary assistant text
        and nothing deletes the chunk, so the ACP layer flags it
        (``AcpEvent.control_notice``) and the caller SUBTRACTS the flagged chunks
        from the segment text it passes here. Subtracting at this one call rather
        than suppressing the chunk upstream is deliberate: the notice must still
        stream and persist, so the text path stays untouched and only this gate —
        the one reader that asks "did the turn answer?" — discounts it;
      * the turn ended NORMALLY (``end_turn``), was not cancelled, and carried
        no tool refusals — those have their own paths;
      * no Stop is in progress, no Stop was pressed at any point in the turn,
        no user follow-up is queued, and no mid-turn steer is pending. Same four
        user-intent gates every sibling recovery path uses, for the same reason:
        a queued continuation must never jump ahead of, or act against, input
        the user has already given;
      * this IS a top-level turn
        (``prompt_depth == 0``), and the one-shot budget is unspent
        (``compaction_continue_retries < 1``) — one attempt, never a loop. The
        bound matters more here than elsewhere: if the continuation itself
        overflows the window again, an unbounded version would compact and
        continue forever.
    Model-agnostic and backend-agnostic: nothing here keys on a model id or a
    backend name — it reads the normalized compaction status every backend's
    adapter now produces.
    """
    if not compaction_started or not compaction_settled:
        return False
    if user_requested_compaction:
        return False
    if stop_in_progress or not stop_generation_unchanged or not queue_empty:
        return False
    if not no_pending_steers:
        return False
    if is_cancelled or refusal_reasons:
        return False
    if stop_reason != end_turn_reason:
        return False
    if final_segment_text.strip():
        return False
    return prompt_depth == 0 and compaction_continue_retries < 1


# Injected when the USER presses Continue on an interrupted turn. Worded to be
# TRUE in both interruption shapes, which is why the endpoint needs no branch:
# a turn that streamed partway and one that produced nothing at all read this
# same text correctly. It must not assert that completed work exists above —
# _POSTTOKEN_RECOVER_MSG does ("The work already done above ... is preserved"),
# and after a gateway restart mid-first-turn that is simply false, which would
# point the model at progress it cannot find.
_MANUAL_RESUME_MSG = (
    f"{MANUAL_RESUME_RECOVERY_PREFIX}\n"
    "The previous turn was interrupted before it finished (a dropped "
    "connection, a restart, or a backend error) and the user has asked you to "
    "carry on. Look at the conversation above, work out what was already "
    "completed, and finish the user's most recent request from there. Do NOT "
    "re-run steps or tools that already completed successfully, and do NOT "
    "assume any particular progress was made — if nothing was done yet, simply "
    "start the request now."
)
# Injected when the user presses Continue on a slot whose last turn ended
# NORMALLY. Continue is offered on any idle slot with a transcript (a killed
# gateway writes no error row, so an interrupted turn can be shape-identical to
# a clean one — see ``_is_interrupted``), which means the button must also have
# something true to say when nothing was actually cut short. Sharing
# ``MANUAL_RESUME_RECOVERY_PREFIX`` is deliberate: to the user the two are one
# button, so they must fold into the same RecoveryCard.
#
# The closing sentence is load-bearing. Without an explicit licence to say "this
# is done", a model handed a bare "keep going" on a finished thread invents
# follow-up work to justify the turn.
_MANUAL_CONTINUE_MSG = (
    f"{MANUAL_RESUME_RECOVERY_PREFIX}\n"
    "The user pressed Continue without typing a new instruction. Look at the "
    "conversation above and carry on with their most recent request: take the "
    "next step that was still outstanding, or finish anything left half-done. "
    "Do NOT re-run steps or tools that already completed successfully. If the "
    "request is genuinely complete, say so in one line instead of inventing "
    "further work."
)
# Injected INSTEAD of the user's message when a content-filter refusal landed
# after the turn had already dispatched tool calls and agent.refusal_fallback_model
# names a different model (chat_turn.model_fallback._refusal_fallback_retry). Replaying the
# message would run those tool calls a second time, so the session -- moved to
# the fallback model -- is asked to carry on from the completed work, which is
# what a person gets by pressing Continue. The nudge goes to the SAME harness
# session the refused turn ran on (the retention every Continue relies on), and
# Kiro Crew itself never re-sends the message once a tool ran. Unlike
# ``_MANUAL_RESUME_MSG`` it offers no restart clause: it is sent only when the
# turn dispatched a tool, so "nothing was done yet" is never true of it, and a
# model that cannot see the partial turn (a session that did not keep it) is
# told to stop rather than start over -- failing safe instead of re-running the
# writes.
#
# Deliberately silent about the content filter and the model swap: the FALLBACK
# model reads this body, and telling it the request was just declined primes it
# to decline too. The user learns both from the retry notice card beside it.
# Not in ``_SYNTHETIC_RECOVERY_MSGS``: the retry turn is recognized by its queue
# id, not by text, and the turn-start re-arm already skips recovery-kind entries.
_REFUSAL_FALLBACK_RESUME_MSG = (
    f"{REFUSAL_FALLBACK_RECOVERY_PREFIX}\n"
    "The previous turn ended before it finished. Look at the conversation above, "
    "work out what was already completed, and finish the user's most recent "
    "request from there. Do NOT re-run steps or tools that already completed "
    "successfully. If the completed work is not visible in the conversation "
    "above, do NOT start the request over — say so and stop."
)


class ResetCause(str, Enum):
    """Why a turn's session had to be reset, which selects the continuation the
    requeue carries — and so the row the transcript renders.

    A closed set rather than a boolean or a caller-supplied string: every reset
    site must state its cause, and a site added later cannot silently inherit
    another cause's user-facing label.

    ``str`` mixin (not ``StrEnum``) for Py3.10 compat, matching ``KindSupport``.
    """

    CONNECTION_LOST = "connection_lost"
    SESSION_BUSY = "session_busy"


# Named by the path callers import it from: the facade that serves it.
ResetCause.__module__ = "kiro_crew.dashboard.chat_utils"


#: The continuation each cause resumes with once the turn has emitted output.
_CONTINUATION_BY_CAUSE = {
    ResetCause.CONNECTION_LOST: _CONN_RECOVER_MSG,
    ResetCause.SESSION_BUSY: _BUSY_RECOVER_MSG,
}


def build_recovery_requeue(
    message: str,
    turn_emitted: bool,
    cause: ResetCause,
    *,
    message_is_synthetic: bool,
    ambiguous_delivery: bool = False,
) -> tuple[str, RecoveryPayload]:
    """Choose the prompt for a reset-and-requeue recovery, and label its provenance.

    Once output or a tool call has been emitted, replaying the original request
    can repeat side effects. A continuation instead resumes from restored
    conversation state. Before any output, the original request is safe and is
    still required for the model to begin the work.

    ``ambiguous_delivery`` forces the continuation even BEFORE any output: a
    request-frame drain stall (see ``AcpProcessDied``) left the prompt in the
    transport, so a kiro-cli that merely paused reading could have consumed and
    acted on it without ever producing host-visible output -- the one case
    ``turn_emitted`` cannot see. Replaying the prompt verbatim there would run its
    tools a second time, so an ambiguous delivery takes the same safe continuation
    an emitted turn does.

    That decision is the same for every cause, but the continuation is not:
    ``cause`` is required because the marker it carries is what the transcript
    renders, and a session that was merely busy must not be reported as a lost
    connection.

    The text and its label are returned together because choosing them apart is how
    they drifted. Replaying ``message`` unchanged only means "the user's own words"
    when this turn was not itself a recovery: a second consecutive failure before any
    output re-queues the runner's previous continuation, so ``turn_emitted`` alone
    cannot say whose words these are. ``message_is_synthetic`` carries that from the
    queue entry that produced the turn, and is required for the same reason ``cause``
    is — a requeue site added later must not silently inherit "the user said this".
    """
    if turn_emitted or ambiguous_delivery:
        return _CONTINUATION_BY_CAUSE[cause], RecoveryPayload.CONTINUATION
    return message, payload_for_replay(message_is_synthetic)
