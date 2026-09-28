"""``compaction.keep`` -- at an AUTO compaction, which tool calls would Jev keep?

A SHADOW measurement, and nothing else. Jev decides nothing: the compaction that
triggered this point runs exactly as it does without the seam, on every backend,
and the answer becomes a row in the decision log plus one line on the compaction
card. :func:`score_compaction` has no apply path and no return value a caller
branches on -- that is what makes it safe to place on the compaction path at all.
What it buys is the number the live design cannot be argued about without: how
much of a real transcript an oracle would keep, against what today's recycle
replay is eligible to keep. "Eligible" rather than "keeps", throughout: the arm is
measured by ROLE, and ``context.build_session_replay`` additionally applies row
quotas and a character budget, so both arms are upper bounds. The field names say
so rather than a comment saying so.

Why this point exists in shadow rather than live
-----------------------------------------------
Nothing in Kiro Crew can replace a harness's message list -- no backend offers a
set-messages call, and ``/compact`` summarizes inside the harness process. The one
place a keep-list COULD land is the transcript Crew re-injects after a recycle
(``context.build_session_replay``), and today that replay admits ``user`` /
``assistant`` / ``inject`` rows only (``context.RECALL_ROLES``): every tool call
and every tool result is already dropped. So a live keep-list would only ever
DISPLACE text inside the same 80 000-character budget, and whether that trade is
worth making is a question about data nobody has yet. This point produces that
data and changes no byte of the replay.

Where it runs, and where it deliberately does not
-------------------------------------------------
``session_compaction.CompactionCoordinator._compact_session``, at the top, as a
background task, with the transcript boundary captured synchronously before the task
exists -- the scoring runs beside the compaction, so a read taken inside the task can
already include rows the compaction appended, and scoring those would describe a
transcript the compaction was not about. That method is the ONE funnel both automatic
entry points reach
-- the per-turn threshold trigger (``check_context_usage`` ->
``_trigger_compaction``) and the awaited between-turn one (``compact_if_needed``)
-- and it is reached BEFORE the Claude arm, before ``_recycle_unmanaged`` and
before ``_compact_in_place``, so every arm CREW ITSELF takes is covered and the
scoring never sits between the trigger and the compaction.

Which is not the same as every backend, and the docs say so rather than implying
it. The hook is INSIDE the funnel, so it covers the compactions Crew dispatches:
the in-place ``/compact`` on kiro-cli, codex and opencode, and the recycle that
follows a failed one or serves a backend with no compaction at all. A harness that
compacts on its OWN initiative is declined by ``_compaction_gate_decision`` before
the funnel -- ``cc_managed`` for Claude-Code sessions, ``compact_unsupported`` for
a member of ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` (KAS) -- and those
compactions are never scored. That is not a gap to close here: there is no
Crew-side dispatch to hang a measurement on, because Crew is not the one
compacting.

A MANUAL ``/compact`` is excluded, and by construction rather than by a flag: the
dashboard dispatches that command as an ordinary turn through
``provider.stream_command("/compact")`` and never calls ``_compact_session``, so
there is no manual path for this point to be on. A person who typed the command
is not being measured.

It cannot delay or alter the compaction
---------------------------------------
The task is created and NOT awaited. The compaction proceeds on its own
coroutine, and the only thing the deadline below decides is whether this task
gives up. When it does, it logs and publishes nothing, which is the same outcome
as "the seam is off": no card line. So the worst case this point can produce is a
missing observation.

What leaves the machine, and the third consent that authorizes it
-----------------------------------------------------------------
The whole slot transcript, at the fitting stage below: user and assistant text,
every tool-call input redacted and clipped, and every tool RESULT replaced by
``ok, N chars (omitted)`` -- results are never sent, only their sizes. That is a
category neither of the keystone's existing scopes covers: ``tool_args`` was
reviewed as the arguments of the one call about to run, not as everything the
session has run, in a request one to two orders of magnitude larger. So this point
needs a THIRD scope, ``consent.STATE_KEY_COMPACTION``, which absent reads as NOT
consented (``gate.POINTS_NEEDING_COMPACTION``, refused by
``gate._scope_consented``). An install that consented before this scope existed --
including one that granted ``tool_args`` -- is INERT here rather than retroactively
signed up.

Fitting, and the ~8% that never fits
------------------------------------
A real transcript is far larger than an oracle state: 87% of it is tool traffic,
and the p50 session at the 70% compaction threshold carries 198 tool calls over
510 000 characters. :data:`STAGES` walks the spike's measured ladder -- inputs at
1000, then 200, then 60 characters, then the conversation text cut to head+tail
halves, then calls rendered on one line -- and the FIRST stage under
:data:`STATE_TOKEN_CEILING` is the one sent. About one session in twelve does not
fit even at the last stage; that one writes an :data:`ERROR_TOO_LARGE` row and
sends nothing, so the population that cannot be measured is visible instead of
silently absent.

Batching, because the questions outnumber the state
---------------------------------------------------
One ``Choice`` per non-pinned call is hundreds of questions on a real transcript,
and one request carrying all of them would exceed the provider's own ceiling. They
are split into requests of :data:`QUESTIONS_PER_REQUEST`, each carrying the SAME
state, run at most :data:`MAX_CONCURRENT_REQUESTS` at a time. Every request is an
ordinary ``decide`` call, so each one is scrubbed, bounded and logged by the gate
on its own.

A PARTIAL answer publishes nothing. The card line is a fraction -- "23 of 61" --
and a fraction over a denominator some of whose members were never asked about is
a false number, not an incomplete one. The outcome row is still written, with
:data:`ERROR_PARTIAL`, so the refusal rate is measurable.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import as_text
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "compaction.keep"

#: The three outcomes of the reference design, which are this question's whole
#: closed domain: drop the call entirely, keep the call without its result, or keep
#: both. The gate refuses an answer outside a question's declared options, so a
#: provider inventing a fourth reads as an invalid result rather than as a branch
#: nobody wrote.
OPTION_DROP = "drop"
OPTION_CALL = "call"
OPTION_BOTH = "both"
OPTIONS: tuple[str, ...] = (OPTION_DROP, OPTION_CALL, OPTION_BOTH)

#: Prefix of a question's id, completed by the call's own index in the state, so an
#: answer names one call and a reader can find it in the request that was sent.
QUESTION_PREFIX = "call_"

#: The rubric, sent as every question's prompt. It names the CONSEQUENCE of each
#: option rather than the option's own word, because the words alone ("call",
#: "both") say nothing about what a reader loses.
PROMPT = (
    "This conversation is about to be compacted, and everything not kept is "
    "forgotten. For this one tool call, what is worth keeping so the assistant can "
    f"carry on? Answer {OPTION_DROP} if neither the call nor its result matters "
    f"any more. Answer {OPTION_CALL} if knowing that the call was made, and with "
    f"what, still matters but its output does not. Answer {OPTION_BOTH} if the "
    "output itself is still needed."
)

#: Estimated tokens one request's STATE may reach. The provider's own request
#: ceiling is larger; this is the state's share of it, leaving room for the
#: questions batched beside it. An estimate, not a count: see
#: :data:`CHARS_PER_TOKEN`.
STATE_TOKEN_CEILING = 25_000

#: Characters per token, the same rough divisor ``skills.select`` estimates
#: ``tokens_saved`` with. A real tokenizer is not worth importing for a fitting
#: decision whose next stage is a step change, and a wrong estimate costs one
#: stage, never a refusal.
CHARS_PER_TOKEN = 4

#: Transcript rows the state is built from. Everything else a transcript carries --
#: ``chunk`` (unflushed streaming text the durable rows already hold),
#: ``permission``, ``done``, the system notices -- says nothing about which tool
#: calls matter.
#:
#: ``thinking`` is NOT here, the same exclusion ``message.steer`` makes: private
#: reasoning is the largest and least reviewed text a turn produces, and the
#: question is answerable without it.
STATE_ROLES = frozenset({"user", "assistant", "tool"})

#: The one role a tool call arrives under, whose ``meta`` carries the join key, the
#: input and the result (``chat_runner._tool_call_meta``).
TOOL_ROLE = "tool"

#: Rows pinned into the state without a question: the FIRST row of the transcript
#: -- the request the whole conversation answers -- and the newest
#: :data:`PINNED_TAIL_ROWS`, which are what the agent is working on right now.
#: Pinning them is the reference's own rule, and it keeps the question off the
#: calls whose answer is not in doubt.
PINNED_TAIL_ROWS = 6

#: Key prefix for a tool row carrying no ``tool_call_id``. Such a row gets a key of
#: its own rather than being folded with its neighbours: two rows that name no call
#: cannot be shown to be the SAME call, and merging them would undercount. The
#: position makes the key unique without colliding with a real id.
_ROW_KEY_PREFIX = "row:"

#: Rows the walk will even look at, newest last. The fitting stages bound what is
#: SENT; this bounds what is READ, so a session with tens of thousands of rows
#: cannot turn one compaction into an unbounded scan.
MAX_ROWS = 4000

#: Tool calls one compaction may ask about. Past this the state is not the problem
#: -- the request count is -- and a measurement that costs forty provider round
#: trips is not worth the compaction it is observing.
MAX_CALLS = 1000

#: Longest tool name sent or recorded. A name is a title the harness chose, so this
#: is a bound rather than a truncation anyone should see.
MAX_TOOL_CHARS = 120

#: Most characters of any ONE retained field -- a message's text, a tool call's input.
#:
#: The other caps here bound COUNTS: :data:`MAX_ROWS` rows, :data:`MAX_CALLS` calls. A
#: count says nothing about size, so one pathological row -- a file read, a base64 blob --
#: is held whole and then re-redacted by :func:`build_state` once per rung of the ladder.
#: This is the bound on that, and it is what makes the transcript's memory finite:
#: :data:`MAX_ROWS` rows of at most this each.
#:
#: 4 KB, which is a SMALL multiple of what any rung sends rather than a large one. Every
#: rung clips a tool input to 1000 characters or less, so nothing is lost there at all; a
#: conversation message longer than this reaches the wire as its first 4 KB, and a
#: measurement of which TOOL CALLS to keep does not turn on the tail of a long message
#: (conversation text is 6% of a measured transcript's characters, tool inputs 33%,
#: results 53%). A bound generous enough to keep every message whole is a bound that does
#: not close the crash it exists for.
#:
#: The LENGTHS are taken before the clip, so every tally and both character arms describe
#: the transcript as it is rather than as it is retained.
MAX_FIELD_CHARS = 4096

#: Questions one request carries. The state is at most
#: :data:`STATE_TOKEN_CEILING`; each question adds the shared rubric plus three
#: short options, so this many keeps a request inside the provider's own ceiling
#: with room to spare. The gate's own row records at most eight answers
#: (``log._MAX_ANSWERS``) whatever this is, which is why the tallies live on the
#: outcome row rather than being recounted from the request rows.
QUESTIONS_PER_REQUEST = 25

#: Requests in flight at once. The whole run is a background observation, so this
#: is low deliberately: a shadow measurement must not be the reason a provider
#: rate-limits the point that is deciding something.
MAX_CONCURRENT_REQUESTS = 4

#: Scheduling slack on top of the provider budget, and the floor and ceiling the
#: wait is clamped into. Held equal to the values every other point in this package
#: waits by, so one shape covers all of them rather than each inventing its own.
#: The ceiling is what bounds the WHOLE run here: a transcript needing ten requests
#: gets the same ten seconds one request does, and a run that outlives it publishes
#: nothing.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0

#: How long the outcome row's write may hold this task, on top of the provider
#: budget. Named and valued as ``gate._LOG_BUDGET_SECS`` is, because it is the same
#: write on the same loop.
LOG_BUDGET_SECS = 0.05

#: Row ``error`` when no fitting stage brought the state under the ceiling. Written
#: instead of a silent return, because the share of sessions that cannot be measured
#: is one of the two numbers this whole point exists to produce.
ERROR_TOO_LARGE = "state-too-large"

#: Row ``error`` when at least one batch answered and at least one did not. The
#: tallies are still written -- they say how much was learned -- but no card line is
#: published, because a fraction whose denominator includes calls nobody was asked
#: about is a false number rather than an incomplete one.
ERROR_PARTIAL = "partial-answers"

#: Row ``error`` when the whole run outlived :func:`wait_budget`. The gate writes
#: its own row for each request it was still inside; this one says the RUN stopped,
#: which is the fact an operator raises ``timeout_ms`` on.
ERROR_RUN_TIMEOUT = "run-timeout"


@dataclass(frozen=True, slots=True)
class Stage:
    """One rung of the fitting ladder.

    *input_chars* clips each tool input; *text_share* keeps that fraction of each
    conversation message as its head and tail halves (1.0 keeps it verbatim);
    *one_line* collapses a tool input's whitespace so a call occupies one line.
    """

    name: str
    input_chars: int
    text_share: float
    one_line: bool = False


#: The fitting ladder, measured in the P3 spike over 206 real transcripts: the
#: share of sessions whose state lands under :data:`STATE_TOKEN_CEILING` runs 25%,
#: 67%, 80%, 88%, 92% down these five rungs. The FIRST one that fits is sent, so an
#: ordinary session pays the mildest stage and only a large one is cut hard.
#:
#: The order is part of the contract: inputs are clipped before conversation text is
#: touched at all, because tool inputs are 33% of a transcript's characters and the
#: text is 6%. Cutting the small half first would buy almost nothing and lose the
#: part the question is answered against.
STAGES: tuple[Stage, ...] = (
    Stage("inputs_1000", 1000, 1.0),
    Stage("inputs_200", 200, 1.0),
    Stage("inputs_60", 60, 1.0),
    Stage("text_half", 60, 0.5),
    Stage("calls_one_line", 60, 0.25, one_line=True),
)


def wait_budget() -> float:
    """How long the whole run may take, clamped into a sane window. Never raises."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("compaction.keep: provider budget unreadable", exc_info=True)
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def redacted(text: object, limit: int | None) -> str:
    """*text* with credentials and exfiltration URLs replaced, then clipped to *limit*.

    ``limit=None`` redacts without clipping, which is what a conversation message
    gets: the brief is that user and assistant text travels verbatim, and the stage's
    own ``text_share`` is what cuts it afterwards. A NUMBER would have to be a guess
    at how much a redactor lengthens the text -- a placeholder is longer than most
    secrets -- so a message whose secret was replaced would lose real characters off
    its end to a bound that was only ever meant to be "as long as it was".

    Redaction, not the gate's refusal, and the difference is the same one
    ``tool_risk.scrubbed`` states: the gate REFUSES a request carrying a credential,
    which is right for text a caller chose to send, while a credential in a tool
    argument is ORDINARY -- an ``aws`` command, a curl header -- so refusing on it
    would mean the point never fires on the transcripts most worth measuring.
    Replacing it keeps the state describable and leaves nothing to leak, and the
    gate's own scan still runs over the result.

    Through :func:`~kiro_crew.platform.context.redact_via_context`, the canonical
    egress shim, and NOT through ``security.redact_credentials`` alone. That
    baseline knows only the patterns this repository ships; a host that loaded a
    companion has its own credential and cookie spellings, and a baseline-only pass
    would send those verbatim to a third party. The shim routes through
    ``current_context().credentials.redact`` so a companion's patterns apply, and a
    standalone process gets byte-for-byte the baseline. Exfiltration URLs are
    cleared by the shipped pass on top, because ``redact`` covers credentials.

    Clipped AFTER redaction, never before: a cut placed inside a secret leaves a
    fragment neither pattern matches, and the fragment is what would go on the wire.
    The clip is a HEAD, unlike ``message_steer.redacted``'s tail, because the
    identifying part of a tool input is its beginning -- the command, the path --
    not its end.

    Never raises, INCLUDING on a composition failure. The shim deliberately
    re-raises ``PlatformCompositionError`` so a host that could not compose its
    companion cannot silently downgrade to the baseline; here that is caught and the
    FIELD IS DROPPED, which is the same direction the rest of this function fails in
    -- a scan that did not complete cannot clear text for the wire, and the
    alternative is sending bytes a companion would have redacted.
    """
    raw = as_text(text)
    if not raw or (limit is not None and limit <= 0):
        return ""
    try:
        # Function-local, and inside the ``try`` on purpose rather than by habit. This
        # module is imported from the compaction path, where a broken point must not cost
        # a session its compaction, so an import that failed has to fail HERE -- where it
        # drops one field -- rather than at module scope, where it would break importing
        # the point at all. Not a circular-import workaround: both targets import cleanly
        # on their own.
        from kiro_crew.platform.context import redact_via_context
        from kiro_crew.security import redact_exfiltration_urls

        cleaned = redact_via_context(raw)
        cleaned, _warnings = redact_exfiltration_urls(cleaned)
    except Exception:
        logger.debug("compaction.keep: redaction failed; dropping the field", exc_info=True)
        return ""
    return cleaned if limit is None else cleaned[:limit]


def head_tail(text: str, share: float) -> str:
    """*share* of *text*, as its head and tail halves joined by an ellipsis.

    A middle cut rather than a tail clip: the opening of a message says what was
    asked and the closing says what it settled on, and the part a reader of the
    whole conversation can least afford to lose is not in between. ``share >= 1``
    returns the text unchanged, so a stage that does not cut costs no allocation
    path of its own.
    """
    if share >= 1.0 or not text:
        return text
    keep = max(0, int(len(text) * share))
    if keep >= len(text):
        return text
    if keep == 0:
        return ""
    half = max(1, keep // 2)
    return text[:half] + " … " + text[-half:]


def result_placeholder(chars: int) -> str:
    """What a tool result is replaced by: its SIZE, never its content.

    Results are 53% of a transcript's characters and the part most likely to carry
    something private, so none of one is ever sent. The size is what the question
    needs: "the output itself is still needed" is a judgement about a call, and how
    much output there was is the only property of it that helps.
    """
    return f"ok, {max(0, chars)} chars (omitted)"


def read_rows(session_key: str, before: str = "") -> list[dict[str, Any]]:
    """The slot transcript's rows for *session_key*, newest last. Blocking IO.

    ``ConversationLog.read_messages`` rather than ``recent`` or the replay builder: those
    two project each row down to ``role`` and ``content``, and the tool input and result
    this point measures live in ``meta``. This is the same JSONL file the dashboard reads,
    so the state describes what this machine actually kept.

    The SINGLE file rather than a chained or archive-inclusive read, and that is the
    subject rather than an oversight: the measurement is about THIS compaction, so its
    universe is the session as the compaction finds it. A chained read would pull in rows
    from a forked parent and a full read the size-rotated archive -- rows the compaction is
    not discarding, which would sit in the denominator and shrink a share that is about
    what this compaction throws away.

    *before* is the boundary the caller captured when the compaction was DECIDED, and
    it is why this function filters at all. The scoring runs BESIDE the compaction, so
    by the time this read happens the harness may already have appended rows -- the
    summary the compaction produced, a recycle notice -- and scoring those would
    describe a transcript the compaction was not about. Rows newer than the boundary
    are dropped.

    A row is dropped only when it can be SHOWN to be newer: comparison goes through
    ``history.transcript_sort_key``, the canonical key every path that orders two
    transcript streams uses, and a row whose timestamp that helper cannot parse is
    KEPT. Such a row is a legacy naive value rather than something a compaction
    appended a moment ago, and excluding it would silently shrink the measurement.

    Bounded to the newest :data:`MAX_ROWS`, AFTER the boundary filter, so a burst of
    appended rows cannot push real ones out of the window. Never raises: an unreadable
    transcript yields no rows, which is a compaction that goes unmeasured.
    """
    if not session_key:
        return []
    try:
        # Function-local for the reason :func:`redacted` states: an import failure here
        # yields no rows -- a compaction that goes unmeasured -- while the same failure at
        # module scope would break importing the point on the compaction path.
        from kiro_crew.history import ConversationLog

        rows = ConversationLog().read_messages(session_key)
    except Exception:
        logger.debug("compaction.keep: transcript unreadable", exc_info=True)
        return []
    if not isinstance(rows, list):
        return []
    usable = [row for row in rows if isinstance(row, dict)]
    if before:
        usable = [row for row in usable if not _is_after(row, before)]
    return usable[-MAX_ROWS:]


def _is_after(row: Mapping[str, Any], boundary: str) -> bool:
    """Whether *row* can be SHOWN to have been written after *boundary*. Never raises.

    ``False`` for a row with no timestamp and for one whose timestamp the canonical
    key cannot parse: the rule is "exclude only what is provably newer", because the
    rows this exists to exclude were written by this process moments ago in the current
    format, while an unparseable one is a legacy value from a much older build.
    """
    stamp = row.get("ts")
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        # Function-local for the reason :func:`redacted` states; here the degraded answer
        # is ``False``, which KEEPS the row.
        from kiro_crew.history import transcript_sort_key

        row_bucket, row_epoch = transcript_sort_key(stamp)
        edge_bucket, edge_epoch = transcript_sort_key(boundary)
    except Exception:
        logger.debug("compaction.keep: timestamp unreadable; keeping the row", exc_info=True)
        return False
    # Bucket 1 is the helper's "unparseable" class. Neither side being comparable means
    # nothing is proven, so the row stays.
    if row_bucket != 0 or edge_bucket != 0:
        return False
    return row_epoch > edge_epoch


def replay_roles() -> frozenset[str]:
    """The roles today's recycle replay ADMITS, read from the replay's own definition.

    Imported here rather than restated, so the eligible arm cannot drift from the
    role set ``context.build_session_replay`` admits. It is the role filter ONLY --
    that function also applies per-role row quotas and a character budget, which is
    why the arm built from this is named ``chars_today_eligible``. Function-local
    because ``context`` is the whole prompt builder and this module is reached from
    the compaction path; a failed import falls back to the three roles that
    definition has always held, which keeps the comparison available rather than
    losing the row.
    """
    try:
        from kiro_crew.context import RECALL_ROLES

        return frozenset(RECALL_ROLES)
    except Exception:
        logger.debug("compaction.keep: replay roles unreadable; using the shipped set")
        return frozenset({"user", "assistant", "inject"})


@dataclass(slots=True)
class CallFact:
    """One tool call as this point measures it -- real sizes, not sent sizes.

    ``input_chars`` and ``result_chars`` are the lengths the TRANSCRIPT holds, not
    the lengths of the clipped state: a live keep-list would re-inject the
    transcript's own bytes, so those are the numbers a chars comparison has to be
    made of. The clipping is a property of the question, not of the saving.
    """

    index: int
    tool: str
    raw_input: str
    input_chars: int
    result_chars: int
    pinned: bool

    @property
    def question_id(self) -> str:
        """This call's question id, which is also its key in the state."""
        return f"{QUESTION_PREFIX}{self.index}"


@dataclass(slots=True)
class Transcript:
    """Everything one compaction's scoring is computed from.

    ``chars_all`` is every character the compaction is about to discard -- the
    conversation text plus every tool input and result -- and it is the denominator
    the card's percentage is taken over.

    ``chars_today_eligible`` is the subset today's recycle replay is ELIGIBLE to
    keep: the rows whose role it admits (:func:`replay_roles`). It is an UPPER BOUND
    on what the replay actually re-injects, not that amount, and the name says so
    because the difference is real -- ``build_session_replay`` additionally applies
    per-role row quotas and an 80 000-character budget scaled by the model window,
    and a session past either of those is replayed with less than this. Measuring
    the true figure is not a matter of calling one more function: the quota walk
    takes a ``ConversationLog`` rather than a row list, and the budget is applied
    inline while formatting, so the only thing to measure would be a rendered string
    whose length carries ``Role:`` prefixes and separators -- formatting overhead
    inside a content measurement, and large enough to break the invariant below.
    Naming the bound is honest; re-deriving the selection here would be a second
    implementation of it, free to disagree with the one that runs.

    ``chars_jev_eligible`` inherits that bound, because it seeds from the arm above.

    The three are ONE character universe, and that is an invariant rather than a
    coincidence: ``chars_today_eligible <= chars_jev_eligible <= chars_all`` must
    hold, because the card prints ``chars_jev_eligible / chars_all``. The roles
    differ on either side -- the replay admits ``inject`` rows, which carry no tool
    call and are not part of the state -- so a denominator built only from
    :data:`STATE_ROLES` would omit bytes the numerator counted and render a share
    above 100%. Every row either side counts is counted in ``chars_all``.

    ``calls_truncated`` is how many tool CALLS the walk dropped to fit
    :data:`MAX_CALLS` -- the OLDEST ones, so the admitted set is the newest
    ``MAX_CALLS`` and still chronological. Calls, not rows: one call appears as up to
    three ``role="tool"`` rows sharing a ``tool_call_id``, and they are folded before
    anything is counted. It is a count rather than a flag because
    the surface states it: "23 of 61 (+140 not scored)" is a true sentence about a
    capped session, where a bare "23 of 61" would be a wrong one -- 61 is the cap,
    not the session's call count. Drawing nothing at all would be the other wrong
    answer: it hides a measurement that is accurate about everything it covers.

    The truncated calls' characters are in NEITHER arm, which keeps the two on one
    universe; the count is what says the arms cover less than the session.
    """

    messages: list[dict[str, str]]
    calls: list[CallFact]
    chars_all: int
    chars_today_eligible: int
    calls_truncated: int = 0


def build_transcript(rows: Sequence[Mapping[str, Any]]) -> Transcript:
    """Read *rows* into the facts the state and the row are both built from.

    A pure walk, so it is testable without a session and cannot alter one. Pins the
    first row and the newest :data:`PINNED_TAIL_ROWS`, folds tool rows by
    ``tool_call_id``, bounds the result at :data:`MAX_CALLS`, and never raises: an
    unreadable row is skipped.

    Two passes, and the split is load-bearing. One logical tool call appears as up to
    THREE ``role="tool"`` rows sharing one ``tool_call_id`` -- the dispatch row, and
    the approval path's own approved/rejected row -- so a per-ROW count is not a count
    of calls. Folding has to finish before the cap is applied, or the cap would
    measure rows; and the characters have to be added after the fold, or a duplicated
    row would count its input twice in the denominator.

    Every retained string is REDACTED here as well as bounded, because the bound and the
    redaction cannot be separated: clipping first cuts a credential spanning the bound into
    a fragment neither redactor matches, and that fragment is what would travel. So the
    admission bound goes through :func:`redacted`, which is redact-then-clip. The stage's
    own further clip in :func:`build_state` then only ever sees redacted text. Lengths are
    taken from the RAW value, so no tally and neither character arm moves.
    """
    usable = [row for row in rows if isinstance(row, Mapping)]
    kept_roles = replay_roles()
    pinned_from = max(0, len(usable) - PINNED_TAIL_ROWS)
    messages: list[dict[str, str]] = []
    chars_all = 0
    chars_today_eligible = 0
    # ``key -> draft``, in first-appearance order. The key is the ``tool_call_id`` when
    # the row carries one; a row without one gets a key of its own, because two rows
    # that name no call cannot be shown to be the same call.
    drafts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    # ── pass 1: the conversation, and the tool rows folded by call ──
    for position, row in enumerate(usable):
        try:
            role = str(row.get("role", "") or "")
            content = row.get("content")
            content = content if isinstance(content, str) else ""
            counted_today = role in kept_roles
            if counted_today:
                chars_today_eligible += len(content)
            if role not in STATE_ROLES:
                # Counted in the DENOMINATOR anyway when the replay admits it. An
                # ``inject`` row is exactly this case: it carries no tool call, so it
                # is not part of the state, but its bytes are in the eligible arm and
                # therefore in the Jev arm -- and a denominator that omitted them
                # would render a share above 100%.
                if counted_today:
                    chars_all += len(content)
                continue
            pinned = position == 0 or position >= pinned_from
            if role != TOOL_ROLE:
                # Counted at FULL length, retained bounded: the arms describe the
                # transcript, the memory describes what a rung could ever send.
                chars_all += len(content)
                if content:
                    # ``redacted`` rather than a slice: it is redact-THEN-clip, and a
                    # slice here cut a credential spanning the bound into a fragment
                    # neither redactor matches -- and the fragment is what would travel.
                    messages.append({"role": role, "text": redacted(content, MAX_FIELD_CHARS)})
                continue
            meta = row.get("meta")
            meta = meta if isinstance(meta, Mapping) else {}
            call_id = meta.get("tool_call_id")
            call_id = call_id if isinstance(call_id, str) and call_id else ""
            key = call_id or f"{_ROW_KEY_PREFIX}{position}"
            raw_input = meta.get("input")
            raw_input = raw_input if isinstance(raw_input, str) else ""
            raw_output = meta.get("output")
            raw_output = raw_output if isinstance(raw_output, str) else ""
            # The input is RETAINED bounded and COUNTED whole. The output is never sent
            # at any stage -- only ``result_placeholder(result_chars)`` is -- so only its
            # length is worth keeping, and holding the bytes was the larger half of what
            # this transcript cost.
            draft = drafts.get(key)
            if draft is None:
                drafts[key] = {
                    # Redact-then-clip at admission, for the reason above. The result is
                    # what a later stage clips further, so the bound is never applied to
                    # unredacted bytes at any point.
                    "tool": redacted(content, MAX_FIELD_CHARS),
                    "input": redacted(raw_input, MAX_FIELD_CHARS),
                    "input_chars": len(raw_input),
                    "output_chars": len(raw_output),
                    "pinned": pinned,
                }
                continue
            # A later row for a call already seen. Each field takes the first NON-EMPTY
            # value: the dispatch row carries the input, the result handler writes the
            # output onto whichever row it matched, and the approval row's title is a
            # decorated restatement of the dispatch row's. ``pinned`` is an OR, because
            # a call whose approval landed inside the newest rows is one the agent is
            # still working through.
            if not draft["input"] and raw_input:
                draft["input"] = redacted(raw_input, MAX_FIELD_CHARS)
                draft["input_chars"] = len(raw_input)
            if not draft["output_chars"] and raw_output:
                draft["output_chars"] = len(raw_output)
            if not draft["tool"] and content:
                # Guarded rather than ``or``-ed, so a call already carrying a name does
                # not pay for a redaction pass whose result is thrown away.
                draft["tool"] = redacted(content, MAX_FIELD_CHARS)
            draft["pinned"] = bool(draft["pinned"]) or pinned
        except Exception:
            logger.debug("compaction.keep: skipping an unreadable transcript row", exc_info=True)

    # ── pass 2: the cap, over CALLS rather than rows ──
    #
    # The NEWEST :data:`MAX_CALLS`, in chronological order. A forward walk that stops
    # admitting at the cap keeps the OLDEST calls, which is the wrong end: the newest
    # are the ones the agent is still working from, they are what the pinned tail is
    # chosen for, and they are what the docs promise. The dropped calls are COUNTED --
    # the surface says "(+K not scored)" -- and their characters are left out of BOTH
    # arms, so the two still describe one universe and the count is what says that
    # universe is smaller than the session.
    folded = list(drafts.values())
    truncated = max(0, len(folded) - MAX_CALLS)
    calls: list[CallFact] = []
    for draft in folded[truncated:]:
        chars_all += int(draft["input_chars"]) + int(draft["output_chars"])
        calls.append(
            CallFact(
                index=len(calls),
                tool=str(draft["tool"]),
                raw_input=str(draft["input"]),
                input_chars=int(draft["input_chars"]),
                result_chars=int(draft["output_chars"]),
                pinned=bool(draft["pinned"]),
            )
        )
    return Transcript(
        messages=messages,
        calls=calls,
        chars_all=chars_all,
        chars_today_eligible=chars_today_eligible,
        calls_truncated=truncated,
    )


def build_state(transcript: Transcript, stage: Stage) -> dict[str, Any]:
    """The state one batch of questions is asked about, rendered at *stage*.

    Every tool input passes :func:`redacted` -- redaction first, then the stage's
    clip -- and every result is :func:`result_placeholder`, so no result byte is in
    here at any stage. A pinned call carries ``pinned: true`` so the oracle can see
    that it is context rather than something it is being asked about.
    """
    calls: list[dict[str, Any]] = []
    for call in transcript.calls:
        text = redacted(call.raw_input, stage.input_chars)
        if stage.one_line:
            text = " ".join(text.split())
        entry: dict[str, Any] = {
            "id": call.question_id,
            "tool": redacted(call.tool, MAX_TOOL_CHARS),
            "input": text,
            "result": result_placeholder(call.result_chars),
        }
        if call.pinned:
            entry["pinned"] = True
        calls.append(entry)
    messages = [
        {
            "role": row["role"],
            "text": head_tail(redacted(row["text"], None), stage.text_share),
        }
        for row in transcript.messages
    ]
    return {"messages": [row for row in messages if row["text"]], "calls": calls}


def estimated_tokens(state: Mapping[str, Any]) -> int:
    """Roughly how many tokens *state* is, rendered the way the wire renders it.

    ``json.dumps`` rather than ``len`` of the parts, for the reason
    ``gate._scan_text`` dumps: the fitting decision has to be made about the bytes
    that will actually be sent, keys and quoting included.
    """
    try:
        rendered = json.dumps(state, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(state)
    return len(rendered) // CHARS_PER_TOKEN


def fit_state(transcript: Transcript) -> tuple[dict[str, Any], Stage] | None:
    """The first stage whose state is under the ceiling, or ``None``.

    ``None`` is the ~8% of sessions the spike measured as never fitting. The caller
    writes :data:`ERROR_TOO_LARGE` for it and sends nothing: a state above the
    provider's ceiling is refused on arrival, and guessing at a sixth stage nobody
    measured would put an unmeasured shape on the wire.
    """
    for stage in STAGES:
        state = build_state(transcript, stage)
        if estimated_tokens(state) <= STATE_TOKEN_CEILING:
            return state, stage
    return None


def questions_for(calls: Sequence[CallFact]) -> list[Question]:
    """One ``Choice`` per NON-PINNED call, in transcript order.

    A pinned call gets none: it is kept whatever an answer would have said, so
    asking would spend a question on a decision already made.
    """
    return [
        Choice(call.question_id, PROMPT, options=list(OPTIONS)) for call in calls if not call.pinned
    ]


def batches(questions: Sequence[Question], size: int | None = None) -> list[list[Question]]:
    """*questions* split into requests of at most *size*, in order.

    Order is kept so a reader holding the request rows can line them up against the
    state's own call order; nothing depends on it, which is why the split is a plain
    slice rather than anything cleverer.

    *size* defaults to :data:`QUESTIONS_PER_REQUEST` resolved AT CALL TIME rather than
    as a default argument value: a default is evaluated once at import, so the bound
    would be frozen at whatever the constant held then -- invisible to a test that
    sets it, and to any future caller that lowers it.
    """
    step = max(1, QUESTIONS_PER_REQUEST if size is None else size)
    return [list(questions[i : i + step]) for i in range(0, len(questions), step)]


def read_decisions(answers: Any, asked: Sequence[Question]) -> dict[str, tuple[str, float]] | None:
    """``{question_id: (option, p)}`` for one batch, or ``None`` when unusable.

    The gate has already held each value against its own declared options, so this
    is the second reading rather than the only one. It is ALL-OR-NOTHING for the
    batch: a mapping missing one of the calls it was asked about cannot be folded
    into a fraction over all of them, and silently dropping the gap is what would
    make the card's number wrong rather than absent.
    """
    if not isinstance(answers, dict):
        return None
    out: dict[str, tuple[str, float]] = {}
    for question in asked:
        answer = answers.get(question.id)
        if not isinstance(answer, Answer):
            return None
        value = answer.value
        if not isinstance(value, str) or value not in OPTIONS:
            return None
        if isinstance(answer.p, bool) or not isinstance(answer.p, (int, float)):
            return None
        out[question.id] = (value, float(answer.p))
    return out


def tally(transcript: Transcript, decisions: Mapping[str, tuple[str, float]]) -> dict[str, Any]:
    """The counts and the two character arms, off one complete set of *decisions*.

    A pinned call counts as kept-both, because that is what pinning does; it is
    also counted on its own, so ``kept_both`` is never mistaken for "this many were
    judged worth keeping whole". ``chars_jev_eligible`` is built from the
    TRANSCRIPT's own lengths, not the clipped state's: what a live keep-list would
    re-inject is the transcript's bytes, so those are the bytes a comparison has to
    be made of.

    ``chars_today_eligible <= chars_jev_eligible <= chars_all`` holds by
    construction: the numerator seeds from the replay arm and only ever ADDS kept
    tool bytes, and every byte either arm counts is in ``chars_all``
    (:class:`Transcript`). That is what makes the card's percentage a percentage --
    an upper-bound one, for the reason :class:`Transcript` states.
    """
    pinned = kept_both = kept_call = dropped = 0
    chars_jev = transcript.chars_today_eligible
    for call in transcript.calls:
        if call.pinned:
            pinned += 1
            kept_both += 1
            chars_jev += call.input_chars + call.result_chars
            continue
        option, _p = decisions[call.question_id]
        if option == OPTION_BOTH:
            kept_both += 1
            chars_jev += call.input_chars + call.result_chars
        elif option == OPTION_CALL:
            kept_call += 1
            chars_jev += call.input_chars
        else:
            dropped += 1
    return {
        "total_calls": len(transcript.calls),
        "pinned_calls": pinned,
        "kept_both": kept_both,
        "kept_call": kept_call,
        "dropped": dropped,
        "chars_all": transcript.chars_all,
        "chars_today_eligible": transcript.chars_today_eligible,
        "chars_jev_eligible": chars_jev,
        "calls_truncated": transcript.calls_truncated,
    }


def build_outcome(
    *,
    turn_id: str,
    counts: Mapping[str, Any],
    requests: int,
    fitting_stage: str,
) -> dict[str, Any]:
    """The fields the outcome row and the card line share.

    ``latency_ms`` is deliberately NOT here: it is a core row field
    (:func:`~kiro_crew.decisions.log.build_row`), so the row carries it at top level
    and an ``extra`` naming it would be dropped.

    ``point`` IS here, unlike ``skills.select``'s outcome, because this record is
    stamped on the same ``meta.decisions_strip`` field two other points use and the
    frontend dispatches on it: a record with no point reads as the oldest shape.
    """
    return {
        "turn_id": turn_id,
        "point": POINT,
        "requests": requests,
        "fitting_stage": fitting_stage,
        **dict(counts),
    }


# ── The hand-off to the compaction card ──
#
# The record has to reach the notice row ``dashboard/state.py`` appends when the
# compaction finishes. ``decisions.outcomes`` is the wrong vehicle: its one slot per
# session belongs to the next ASSISTANT reply, and claiming it here would take the
# strip a ``skills.select`` turn had already published and attach it to a compaction
# notice. So this module keeps its own store, on the same three bounds and for the
# same reasons -- one entry per session, a TTL, and a ceiling -- because the reader
# is allowed to never arrive: a compaction that finishes before the scoring does
# takes no record, which is the documented "no card line".

#: How long a published record stays claimable. Short, unlike ``outcomes``' ten
#: minutes: the consumer is the callback at the end of THIS compaction, seconds
#: away, and a record still sitting here a minute later belongs to a compaction
#: whose notice has already been drawn.
RECORD_TTL_SECONDS = 60.0

#: Most sessions holding an unclaimed record at once. A gateway compacts a handful
#: of sessions in a minute, so reaching this means records are being published and
#: never read, and the oldest are the least likely to ever be.
MAX_PENDING_RECORDS = 200

#: ``session_key -> (published_at_monotonic, record)``, oldest publish first.
_pending: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()

#: ``session_key -> the newest attempt token minted for it``. A scoring run whose
#: token is not the session's newest publishes NOTHING, which is what keeps a
#: compaction whose scoring straggles past its own notice from having its record
#: popped by the NEXT compaction on that key and shown as that one's measurement.
#: One entry per session rather than a queue, because only the newest attempt has
#: a notice still to be drawn.
_attempts: dict[str, int] = {}

#: The source of every token, PROCESS-WIDE and monotonic, so no token is ever handed
#: out twice. A counter kept per session restarts at 1 once the cap evicts its entry,
#: and a re-minted 1 is numerically equal to the 1 some straggler is still carrying --
#: which is the one way an equality comparison can admit a record it should refuse.
#: Advanced under ``_pending_lock`` like everything else here, though ``next`` on a
#: ``count`` is itself atomic.
_attempt_source = itertools.count(1)

_pending_lock = threading.Lock()


def begin_attempt(session_key: str) -> int:
    """Mint this session's next compaction-attempt token. Never raises.

    Called SYNCHRONOUSLY by the coordinator before the scoring task is created, so
    the token exists before anything can publish against it and two compactions on
    one key are ordered by the loop turn that started them rather than by which
    scoring finished first.

    Minting also RETIRES whatever the previous attempt had pending: a record nobody
    read belongs to a compaction whose notice has already been drawn, and leaving it
    would let this attempt's ``take_record`` claim it. That is the same rule
    ``outcomes.discard`` applies at the start of a turn, for the same reason.

    The token comes from ONE process-wide counter, so a value handed out is never handed
    out again. Per-session numbering restarts at 1 whenever the cap evicts that session's
    entry, and a re-minted 1 equals the 1 a straggler minted before the eviction is still
    carrying -- so the equality comparison in :func:`publish_record`, which may otherwise
    only ever REFUSE, would admit a record belonging to a compaction whose notice is long
    drawn. A monotonic source removes the collision instead of narrowing its odds.
    """
    if not session_key or not isinstance(session_key, str):
        return 0
    with _pending_lock:
        token = next(_attempt_source)
        _attempts[session_key] = token
        _pending.pop(session_key, None)
        while len(_attempts) > MAX_PENDING_RECORDS:
            # Bounded on the same terms as the records: a gateway that compacted
            # thousands of distinct sessions must not keep an entry per session
            # forever. Dropping the OLDEST-inserted entry loses a session's token,
            # which the subtractive comparison reads as a MISMATCH -- the safe
            # direction -- and cannot resurrect a value, because no value repeats.
            _attempts.pop(next(iter(_attempts)))
    return token


def publish_record(session_key: str, record: dict[str, Any], attempt: int = 0) -> None:
    """Hand *record* to whatever draws this session's compaction notice. Never raises.

    A falsy key or a non-dict record is DROPPED rather than stored: the key is what
    the consumer matches on, so a record filed under ``""`` would be handed to the
    next keyless reader.

    *attempt* is the token :func:`begin_attempt` gave this run. A run whose token is
    not the session's newest publishes nothing: its compaction's notice is already
    drawn and the pending slot belongs to the compaction now in flight, so publishing
    would show one compaction's measurement on another's notice.
    ``0`` means "no token", which still stores: a caller with no attempt of its own
    (a test, a future single-shot caller) is not trying to correlate.
    """
    if not session_key or not isinstance(session_key, str) or not isinstance(record, dict):
        logger.debug("compaction.keep: record dropped (key=%r)", session_key)
        return
    now = time.monotonic()
    with _pending_lock:
        # ``get`` with NO default, so an ABSENT counter compares unequal and the record
        # is discarded. Defaulting to *attempt* made a missing counter compare EQUAL,
        # which turned the token into something that could AUTHORIZE a publish -- on a
        # key whose counter the cap had evicted, a straggler's record would have been
        # admitted on the strength of a value nobody held. The token is subtractive:
        # it may only ever refuse.
        if attempt and _attempts.get(session_key) != attempt:
            logger.debug("compaction.keep: a later compaction superseded this scoring")
            return
        _drop_expired(now)
        _pending.pop(session_key, None)
        _pending[session_key] = (now, record)
        while len(_pending) > MAX_PENDING_RECORDS:
            _pending.popitem(last=False)


def take_record(session_key: str) -> dict[str, Any] | None:
    """Pop this session's pending record, or ``None`` when there is none.

    ``None`` is the overwhelmingly common answer -- the seam is off by default --
    so it is the cheap path: one lookup on a bounded mapping, no IO. The read is
    DESTRUCTIVE for the reason ``outcomes.consume`` is: the record describes ONE
    compaction, and leaving it in place would attach it to the next one.
    """
    if not session_key:
        return None
    now = time.monotonic()
    with _pending_lock:
        _drop_expired(now)
        entry = _pending.pop(session_key, None)
    if entry is None:
        return None
    published_at, record = entry
    return None if now - published_at > RECORD_TTL_SECONDS else record


def _drop_expired(now: float) -> None:
    """Evict entries older than the TTL. Caller holds :data:`_pending_lock`.

    Walks from the oldest and stops at the first live entry: the mapping is in
    publish order, so everything after it is younger.
    """
    for key in list(_pending.keys()):
        published_at, _record = _pending[key]
        if now - published_at <= RECORD_TTL_SECONDS:
            return
        del _pending[key]


def pending_count() -> int:
    """How many sessions hold an unclaimed record. For tests and diagnostics."""
    with _pending_lock:
        return len(_pending)


def reset_records() -> None:
    """Forget every pending record and every attempt token.

    For tests, so one cannot inherit another's; the attempt counters go too, because
    a leftover counter would make the next test's first token compare unequal.
    """
    with _pending_lock:
        _pending.clear()
        _attempts.clear()


# ── The run ──


async def score_compaction(
    session_key: str, attempt: int = 0, *, before: str = ""
) -> dict[str, Any] | None:
    """Score one compaction in the shadow. Returns the published record, or ``None``.

    The return value is for TESTS and for a caller that wants to log it. Nothing in
    the product branches on it: the compaction has already been decided by the time
    this is called, and this coroutine has no apply path. ``None`` covers the seam
    being off, an unsampled session, an unconsented scope, an empty or unreadable
    transcript, a state that never fits, a partial answer, a timeout and a refused
    row -- every one of which means "no card line".

    *before* is the transcript boundary the caller captured when the compaction was
    decided. It is passed in rather than taken here because this coroutine runs BESIDE
    the compaction: a boundary read at this point would already be after whatever the
    harness appended. See :func:`read_rows`.

    *attempt* is the token :func:`begin_attempt` minted for this compaction. It
    decides only whether the RECORD is published: a run that outlived its own
    compaction's notice is not the newest attempt on the session, and its
    measurement would land on the next compaction's notice. The rows are written
    either way -- the observation happened, and the log is where it lives.

    Never raises except :class:`asyncio.CancelledError`, which the gate propagates:
    cancellation is the gateway going away, not a measurement failure.
    """
    started = time.monotonic()
    turn_id = uuid.uuid4().hex[:16]
    try:
        # The keystone read and the transcript read are both filesystem IO, and both
        # are refusals in the overwhelming majority of installs, so the CHEAP one
        # goes first: an unconsented machine must not read a megabyte of transcript
        # to find out it is not sending anything.
        if not await asyncio.to_thread(core.is_enabled, POINT, session_key=session_key):
            return None
        return await asyncio.wait_for(
            _run(
                session_key,
                turn_id=turn_id,
                started=started,
                attempt=attempt,
                before=before,
            ),
            timeout=wait_budget(),
        )
    except asyncio.TimeoutError:
        # The RUN stopped, which is not what the gate's own per-request rows say.
        # Recorded, because a compaction that goes unmeasured for want of time is
        # exactly the operator-actionable fact (``timeout_ms``) this point is here
        # to surface.
        logger.debug("compaction.keep: the run outlived its budget")
        await _record(
            session_key,
            latency_ms=_elapsed_ms(started),
            extra={"turn_id": turn_id, "point": POINT},
            error=ERROR_RUN_TIMEOUT,
        )
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("compaction.keep: leaving this compaction unmeasured", exc_info=True)
        return None


async def _run(
    session_key: str, *, turn_id: str, started: float, attempt: int = 0, before: str = ""
) -> dict[str, Any] | None:
    """Read, fit, ask and record. Bounded by the caller's one wait."""
    rows = await asyncio.to_thread(read_rows, session_key, before)
    if not rows:
        return None
    transcript = await asyncio.to_thread(build_transcript, rows)
    if not transcript.calls:
        # Nothing to keep and nothing to drop: a conversation with no tool calls is
        # one this point has no observation about, and a row saying "0 of 0" would
        # be furniture in the log rather than a measurement.
        return None
    fitted = await asyncio.to_thread(fit_state, transcript)
    if fitted is None:
        await _record(
            session_key,
            latency_ms=_elapsed_ms(started),
            extra={
                "turn_id": turn_id,
                "point": POINT,
                "total_calls": len(transcript.calls),
                "chars_all": transcript.chars_all,
            },
            error=ERROR_TOO_LARGE,
        )
        return None
    state, stage = fitted
    asked = questions_for(transcript.calls)
    if not asked:
        # Every call is pinned, so there is nothing to ask and no answer to compare.
        return None
    groups = batches(asked)
    decisions, complete = await _ask_all(
        state, groups, session_key=session_key, turn_id=turn_id, stage=stage
    )
    if not decisions:
        # No batch answered. The gate has already written a row per refused request
        # carrying its own error category, so a second row here would say nothing
        # that is not already in the file.
        return None
    counts = (
        tally(transcript, decisions)
        if complete
        else {
            "total_calls": len(transcript.calls),
            "answered_calls": len(decisions),
            "chars_all": transcript.chars_all,
            "chars_today_eligible": transcript.chars_today_eligible,
        }
    )
    outcome = build_outcome(
        turn_id=turn_id,
        counts=counts,
        requests=len(groups),
        fitting_stage=stage.name,
    )
    row = await _record(
        session_key,
        latency_ms=_elapsed_ms(started),
        extra=outcome,
        error=None if complete else ERROR_PARTIAL,
    )
    if row is None or not complete:
        # Two reasons not to publish, both of them "the number would be wrong or
        # unaccountable rather than merely absent":
        #
        # * a PARTIAL answer -- the line says "N of M tool calls", and M would
        #   include calls nobody was asked about;
        # * a row that was not written -- the thumbs on the line post that turn id,
        #   and there would be no row for a verdict to be about.
        #
        # A TRUNCATED walk is NOT one of them: the line states the overflow itself
        # ("+K not scored"), so it is a qualified true sentence rather than a wrong
        # one, and withholding it would hide a measurement that is accurate about
        # everything it covers.
        #
        # Both cases still WROTE the row: the observation happened and the log is
        # where it lives.
        return None
    publish_record(session_key, row, attempt)
    return row


async def _ask_all(
    state: Mapping[str, Any],
    groups: Sequence[Sequence[Question]],
    *,
    session_key: str,
    turn_id: str,
    stage: Stage,
) -> tuple[dict[str, tuple[str, float]], bool]:
    """Every batch, at most :data:`MAX_CONCURRENT_REQUESTS` at a time.

    Returns ``(decisions, complete)``. ``complete`` is whether EVERY batch answered
    usably, which is what the caller needs to decide whether a fraction over all the
    calls is a true number.

    Each batch is an ordinary ``decide`` carrying the SAME state, so each one is
    scrubbed, bounded by ``timeout_secs`` and logged on its own -- the alternative,
    one giant request, exceeds the provider's ceiling and loses every answer to a
    single failure.
    """
    limit = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _one(index: int, batch: Sequence[Question]) -> dict[str, tuple[str, float]] | None:
        async with limit:
            extra: dict[str, Any] = {
                "turn_id": turn_id,
                "request": index,
                "requests": len(groups),
                "questions": len(batch),
                "fitting_stage": stage.name,
            }
            answers = await core.decide(
                POINT, dict(state), list(batch), session_key=session_key, extra=extra
            )
            return read_decisions(answers, batch)

    results = await asyncio.gather(
        *(_one(index, batch) for index, batch in enumerate(groups)),
        return_exceptions=True,
    )
    decisions: dict[str, tuple[str, float]] = {}
    complete = True
    for result in results:
        if isinstance(result, BaseException) or result is None:
            if isinstance(result, asyncio.CancelledError):
                raise result
            complete = False
            continue
        decisions.update(result)
    return decisions, complete


async def _record(
    session_key: str,
    *,
    latency_ms: int,
    extra: dict[str, Any],
    error: str | None,
) -> dict[str, Any] | None:
    """Write one row and return it, or ``None`` when it did not land. Never raises.

    OFF THE LOOP and bounded, the shape ``gate._write`` and
    ``tool_risk._record_outcome`` already use for the same write on the same loop:
    this coroutine runs on the gateway's event loop, so a synchronous append would
    put a lock wait and a filesystem write there.
    """
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=latency_ms,
            error=error,
            extra=extra,
        )
        written = await asyncio.wait_for(asyncio.to_thread(_log.append, row), LOG_BUDGET_SECS)
    except asyncio.TimeoutError:
        # The worker thread is NOT cancellable, so the append may still land --
        # acceptable for a write that cannot corrupt a line. What this arm refuses
        # is the RECORD, because the caller stopped waiting for its row.
        logger.debug("compaction.keep: the row outlived its write budget")
        return None
    except Exception:
        logger.debug("compaction.keep: could not record the row", exc_info=True)
        return None
    return row if written else None


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
