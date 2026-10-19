"""What a turn's final text claims, read before the runner decides how the turn ended.

Promise-only closers (a turn that says it will act and then stops), unfinished-progress
claims, deferred follow-up promises (a turn that says it will report back later), and
tool-call markup that leaked into the reply text, each judged with quoted
code stripped first; and a provider-only weighted-token banner at the head of the reply,
recognized by its exact grammar and kept visible whenever the evidence the turn can see
could make it an echo.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.messaging.split import FENCE_CLOSE, FENCE_OPEN, iter_fence_lines
from kiro_crew.widget_parse import parse_widgets

# The opening of the "Loaded prompt" chip ``prompt_assembly`` writes for an
# ``@prompt`` expansion; only the chip, never the body, reaches the transcript.
_PROMPT_CHIP_PREFIX = "📜 Loaded prompt "

# High-confidence "I will do it right now" endings. Kept deliberately NARROW: a
# broad natural-language detector risks false positives, duplicate writes, and
# continuation loops, so this matches only a terminal first-person commitment to
# an IMMEDIATE action, with an explicit now/right-away marker. "I'll explain that
# now: ..." is NOT caught, because the detector also requires that the promise be
# the LAST thing in the text (nothing substantive follows it) — an announcement
# followed by the actual content is a normal answer.
# The immediacy markers are deliberately RESTRICTIVE: only true "right now"
# adverbs. `next` and `go ahead and` are excluded because they match "I'll do
# that next week" and permission-seeking closers — `next` is a sequencer, not an
# immediacy signal. A bare `going to` alternative is excluded too: with no
# first-person subject it matches third-person statements like "The deployment is
# going to start now", firing an unrelated continuation. Only the subject-bound
# `i'm going to` form is accepted.
_PROMISE_NOW_RE = re.compile(
    r"\b(?:i(?:'|’)?ll|i\s+will|let\s+me|i(?:'|’)?m\s+going\s+to)\b"
    r"[^.!?\n]*?"
    r"\b(?:now|right\s+away|right\s+now|immediately)\b"
    r"[^.!?\n]*[.!?]?\s*$",
    re.IGNORECASE,
)
# A trailing sentence that is ONLY a promise (no colon-introduced content, no
# code fence, no list) — it confirms the promise is terminal, not a preamble.
_PROMISE_HAS_FOLLOWING_CONTENT_RE = re.compile(r":\s*\S|```|\n\s*[-*\d]")
# Permission-seeking / no-action closers that a naive immediacy match misfires
# on. These are the OPPOSITE of a promise-to-act: the
# turn is correctly yielding to the user or explicitly declining to act. If any
# appears in the final segment, it is never a promise-only turn. "let me know ...
# now"/"...next" reads as immediate to the regex but is a hand-off; "for now" /
# "as-is" / "stop here" are explicit non-actions.
_NO_ACTION_CLOSER_RE = re.compile(
    r"\blet\s+me\s+know\b"
    r"|\bfor\s+now\b"
    r"|\bfor\s+the\s+moment\b"
    r"|\bas[-\s]is\b"
    r"|\bstop\s+here\b"
    r"|\bleave\s+(?:it|that|this)\b",
    re.IGNORECASE,
)
# An APPROVAL-GATED closer keeps the decision with the user, even when the
# sentence otherwise reads as a promise-to-act ("If that looks good, I'll push
# it now.", "Just say the word and I'll open the PR right away."). The current
# no-action list does not cover these because they contain both a real
# commitment ("I'll ... now") and a conditional opener; auto-continuing them
# dispatches an unattended action the user was still being asked to approve.
# Bias, like the negation gate, is toward reject on ambiguous conditionals — a
# false reject just lands the turn normally (safe); a false accept executes a
# possibly-irreversible side effect (push, merge, delete) without consent.
_APPROVAL_GATED_RE = re.compile(
    # ANY conditional `if` opener, not just specific pronouns: "If CI passes, I'll
    # delete it now" is as gated as "If you approve ...". Bias toward reject is
    # safe here (a false reject just lands normally); a pronoun-only list lets
    # "If CI passes ..." slip through.
    r"\bif\b" r"|\bjust\s+say\s+the\s+word\b" r"|\bwant\s+me\s+to\b" r"|\bshall\s+i\b"
    # Consent DEFERRAL: the action is gated on the user's approval/confirmation,
    # even when the sentence reads as "I'll ... now" ("I'll wait for your approval
    # before I delete it right now"). "with your approval" is not the only form:
    # "wait for / for / pending your approval", "your go-ahead/sign-off/
    # confirmation", "before you approve" and "you to confirm" are far more common,
    # and auto-continuing any of them dispatches an action the model explicitly
    # said it would hold for consent. The forms are kept PRECISE (a possessive
    # consent-noun, or a deferral verb bound to you/your) rather than bare
    # "pending"/"await"/"before you", so a genuine promise like "merge the pending
    # PR now" is not falsely rejected — that closes the consent CLASS without an
    # over-broad reject. Reject-bias is still safe (a false reject just lands).
    r"|\byour\s+(?:approval|go[-\s]?ahead|sign[-\s]?off|confirmation|permission|ok(?:ay)?|blessing)\b"
    r"|\bwait(?:ing)?\s+for\s+(?:you|your|approval|confirmation|sign[-\s]?off|permission|the\s+go[-\s]?ahead)\b"
    r"|\bbefore\s+you\s+(?:approve|confirm|decide|sign\s+off|review|say|weigh\s+in|ok(?:ay)?)\b"
    r"|\byou\s+to\s+(?:approve|confirm|decide|sign\s+off|review|weigh\s+in|say|tell)\b"
    # Temporal/conditional gates in ANY form ("once tests are green", "when the
    # build passes", "after CI", "until you confirm"), not only "... you ...".
    # Closes the conditional CLASS in one rule rather than enumerating each noun;
    # reject-bias is safe.
    r"|\b(?:once|when|after|as\s+soon\s+as|until)\b"
    # Remaining subordinating-conditional conjunctions ("Unless you object, I'll
    # merge now", "Assuming you're fine, I'll push now"). The risky ones are bound
    # to a following pronoun/complementizer so a benign adjective ("the provided
    # config", "the given file") is NOT falsely rejected.
    r"|\bunless\b"
    r"|\bassuming\s+(?:you|that|we|it|the|your)\b"
    r"|\bprovided\s+(?:that|you)\b"
    r"|\bgiven\s+(?:that|you)\b"
    r"|\b(?:as|so)\s+long\s+as\b",
    # NOTE (residual risk): this is a deny-list, and the
    # DANGEROUS direction for the consent class is the false ACCEPT (auto-continuing
    # an action the user was still being asked to approve), which enumeration cannot
    # fully close — a novel conditional phrasing will always slip through. Two
    # mitigations bound the blast radius: (1) `_PROMISE_ONLY_CONTINUE_MSG` itself
    # instructs the model NOT to act if it was waiting on approval/a condition (a
    # semantic backstop independent of this regex), and (2) recovery only INJECTS a
    # continuation — the tool call still flows through the normal approval path, so
    # only auto-approve/yolo turns an escape into an unattended side effect. The
    # accept-list inversion (fire only on a proven-unconditional commitment) is the
    # real long-term fix but is a behaviour-changing redesign, out of scope here.
    # A cheaper structural bound worth considering: skip promise-only recovery (or
    # downgrade it to the notice-only arm) under auto-approve/yolo, so a detector
    # false-accept can never become an unattended side effect.
    re.IGNORECASE,
)
# A NEGATED commitment before the immediacy marker ("I'm not going to open the PR
# now", "I won't do that now", "I can't right now") is the OPPOSITE of a promise
# to act, but the bare `going to`/`i'll` alternatives above still match it.
# Reject when a negated-commitment form is followed, within the same sentence, by
# an immediacy marker. Bias is deliberately toward REJECTING on ambiguous
# negation: a false reject just lands the turn normally (safe), whereas a false
# accept injects an unwanted action.
_NEGATED_PROMISE_RE = re.compile(
    r"\b(?:not\s+going\s+to|never\s+going\s+to|won(?:'|’)?t|will\s+not"
    r"|i(?:'|’)?ll\s+not|can(?:'|’)?t|cannot|not\s+able\s+to|unable\s+to"
    r"|do(?:es)?n(?:'|’)?t|do(?:es)?\s+not|no\s+longer)\b"
    r"[^.!?\n]*?"
    r"\b(?:now|right\s+away|right\s+now|immediately)\b",
    re.IGNORECASE,
)
# The promise gate (`_PROMISE_NOW_RE`) is TERMINAL-ANCHORED (`...$`): any promise
# it matches lives entirely in the final sentence. The three reject gates must be
# scoped to that SAME sentence, not `.search()` the whole segment — otherwise an
# everyday `if`/`when`/`after`/`let me know`/negation in an EARLIER sentence
# ("When you asked about X, I fixed it. I'll open the PR now.") vetoes a genuine
# terminal promise, leaving the promise-only turn unrecovered (an
# asymmetric-scope false negative). Splitting on sentence + newline boundaries
# keeps the reject-bias but only where the promise can actually be.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?\n]+")

# A status update can be accurate while a turn is streaming and become false as
# soon as that turn returns control. This detector is deliberately notice-only:
# unlike the promise-only recovery above, it may run after tools with side
# effects, so its caller must never replay the turn automatically. Requiring a
# first-person claim at the start of a sentence excludes third-person status
# reports such as "the subagent is still working" or "the monitor will continue"
# that really can outlive this turn. A first-person claim that the main agent is
# running or checking one remains foreground work and is intentionally matched.
_UNFINISHED_PROGRESS_RE = re.compile(
    r"(?:^|[.!?\n]\s*)(?P<claim>(?:next[,:]?\s+)?"
    r"(?:"
    r"(?:i(?:'|’)?m|i\s+am)\s+(?:still\s+)?"
    r"(?:continuing\s+(?:with|to)|proceeding\s+(?:with|to)|working\s+(?:on|through)"
    r"|running\b|checking\b|validating\b|testing\b|building\b|reviewing\b"
    r"|preparing\b|investigating\b)"
    r"|i(?:'|’)?ll\s+(?:keep|continue)\s+"
    r"(?:working|running|checking|validating|testing|building|reviewing|preparing|investigating)\b"
    r"))",
    re.IGNORECASE,
)


def _terminal_sentence(text: str) -> str:
    """Return the last sentence of ``text`` — the span the terminal promise gate
    matches. Reject gates are scoped to this so a subordinate clause earlier in a
    multi-sentence closer cannot veto a promise that sits only at the end."""
    parts = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(text) if p.strip()]
    return parts[-1] if parts else text


def has_unfinished_progress_claim(final_segment_text: str) -> bool:
    """Return whether a completed turn's final sentence says foreground work continues.

    The caller supplies only text emitted after the last tool boundary. A match
    is diagnostic, not permission to re-run anything: earlier tools may already
    have produced side effects. Only the terminal sentence can describe work as
    still running when control returns; an earlier progress update followed by a
    completed-status sentence is not an unfinished claim. A colon followed by
    content is likewise a lead-in fulfilled by the same response.
    """
    text = (final_segment_text or "").strip()
    terminal = _terminal_sentence(text)
    match = _UNFINISHED_PROGRESS_RE.search(terminal)
    if match is None:
        return False
    claim = terminal[match.start("claim") :]
    return not bool(re.search(r":\s*\S", claim))


# A DEFERRED follow-up promise: the turn says the agent will come back later
# ("I'll report back when the build finishes", "I'll let you know once the
# pipeline is green", "I'll keep an eye on it"). Unlike the immediate promise
# above, nothing about this is wrong while the turn runs. It becomes false when
# the turn returns control with nothing set up to wake the agent: no monitor, no
# scheduled job, no wait, no background run. The session then sits idle until
# the person writes again, which is the incident this detector exists for.
# Notice-only, like the progress claim: the turn may have run tools with side
# effects, so the caller never replays it.
#
# The subject must be first person and the commitment future tense. Up to three
# filler words may sit between them ("I'll then report back", "I will also
# circle back"), so the follow-up verb is the commitment, not a later clause.
# The bare verbs watch, monitor and poll are also nouns and objects of other
# verbs ("I'll set up a monitor", "I'll explain how to poll the API"), so they
# count only directly after the subject, optionally behind one adverb.
_DEFERRED_FOLLOWUP_RE = re.compile(
    r"\b(?:i(?:'|’)?ll|i\s+will|i(?:'|’)?m\s+going\s+to|i\s+am\s+going\s+to)\s+"
    r"(?:(?:[a-z]+\s+){0,3}?"
    r"(?:report\s+back|get\s+back\s+to\s+you|let\s+you\s+know|ping\s+you|notify\s+you"
    r"|update\s+you|follow\s+up|circle\s+back|check\s+back|check\s+in\s+again|come\s+back"
    r"|keep\s+(?:an\s+)?eye\s+on|keep\s+watching|keep\s+monitoring|keep\s+polling"
    r"|(?:send|post|give)\s+(?:you\s+)?(?:an?\s+)?(?:update|summary|report|status))"
    r"|(?:(?:then|also|just|now)\s+)?(?:watch|monitor|poll))\b",
    re.IGNORECASE,
)
# A follow-up that waits on the PERSON needs no wake: their next message is the
# trigger ("Let me know when you're ready and I'll report back", "Reply here and
# I'll follow up"). Negations are explicit non-promises.
_FOLLOWUP_USER_GATED_RE = re.compile(
    r"\blet\s+me\s+know\b|\btell\s+me\b|\bask\s+me\b|\bping\s+me\b|\bmessage\s+me\b"
    r"|\bsend\s+(?:me\s+)?(?:a\s+)?message\b|\breply\b|\bwhen\s+you(?:'|’)?re\s+ready\b"
    r"|\bwhen\s+you\s+are\s+ready\b|\bif\s+you(?:(?:'|’)d|\s+would)?\s+(?:want|like|need|prefer)\b|\bwant\s+me\s+to\b"
    r"|\bshall\s+i\b|\bshould\s+i\b|\byour\s+(?:approval|go[-\s]?ahead|confirmation)\b"
    r"|\b(?:not|never|won(?:'|’)?t|can(?:'|’)?t|cannot)\b",
    re.IGNORECASE,
)
#: The tools that leave something behind which wakes this session after the turn
#: ends. A turn that called any of them made its follow-up real. Matched on the
#: bare tool name (see :func:`_bare_tool_name`). ``wait`` is deliberately absent:
#: it blocks inside the turn, so a turn that waited and then promised a later
#: update still has nothing scheduled. ``spawn_steer`` is absent because it
#: reaches a child that already exists, which the runtime probe counts.
WAKE_ARMING_TOOLS: frozenset[str] = frozenset(
    {
        "monitor_start",
        "monitor_watch",
        "monitor_update",
        "cron_add",
        "cron_update",
        "cron_resume",
        "cron_trigger",
        "register_hook",
        "spawn_run",
        "spawn_continue",
        "workflow_run",
        "workflow_rerun_subtree",
        "task_run",
    }
)
# Backends spell an MCP tool as ``mcp__<server>__<tool>``, ``<server>/<tool>``,
# ``@<server>/<tool>`` or ``<server>___<tool>``; the tool is always the last part.
_TOOL_NAME_SEPARATOR_RE = re.compile(r"_{2,}|/|::")


def _bare_tool_name(tool_name: str) -> str:
    """The tool part of a possibly server-qualified tool name."""
    return _TOOL_NAME_SEPARATOR_RE.split(tool_name)[-1]


#: How many trailing sentences a deferred promise may sit in. The promise is
#: rarely the very last sentence ("I'll report back when it finishes. The build
#: is about 20 minutes."), but one buried at the top of a long answer is a plan
#: the answer went on to fulfil, not the turn's closing commitment.
_DEFERRED_FOLLOWUP_TAIL_SENTENCES = 2


def has_deferred_followup_promise(final_segment_text: str) -> bool:
    """Return whether a turn's closing sentences promise to follow up LATER.

    The caller supplies only text emitted after the last tool boundary, and must
    still check that nothing was set up to wake the session
    (:func:`turn_armed_a_wake` plus any monitor already running) before saying
    so: this reads the words, not the runtime. Quoted code and an ``[OPTIONS:]``
    trailer are stripped first, so a pasted transcript or a button label cannot
    trigger it. A follow-up gated on the person's own next message, and a negated
    one, are not promises the runtime has to keep.
    """
    text, _choices = split_options_trailer(final_segment_text or "", hide_partial=False)
    text = _strip_quoted_code(text).strip()
    if not text:
        return False
    sentences = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(text) if p.strip()]
    for sentence in sentences[-_DEFERRED_FOLLOWUP_TAIL_SENTENCES:]:
        match = _DEFERRED_FOLLOWUP_RE.search(sentence)
        if match is None or _FOLLOWUP_USER_GATED_RE.search(sentence):
            continue
        # A colon after the promise introduces what it promised, delivered in
        # the same reply ("I'll give you a summary: the build passed").
        if re.search(r":\s*\S", sentence[match.start() :]):
            continue
        return True
    return False


def turn_armed_a_wake(turn_tool_identities: tuple[tuple[str, str, str, bool], ...]) -> bool:
    """True when any call this turn was a tool that wakes the session later.

    Each identity is ``(tool_call_id, mcp_server_name, tool_name, trusted)``.
    Fails toward "armed": an identity that is not a well-formed tuple, or that
    carries no tool name (claude-agent-acp and opencode frames name no tool),
    counts as a wake, because the only cost of a wrong True is a missing notice,
    while a wrong False tells the person nothing is scheduled when something is.
    """
    for identity in turn_tool_identities:
        if not isinstance(identity, tuple) or len(identity) < 3:
            return True
        tool_name = identity[2]
        if not isinstance(tool_name, str):
            return True
        bare_name = _bare_tool_name(tool_name)
        if not bare_name or bare_name in WAKE_ARMING_TOOLS:
            return True
    return False


#: Shown when a turn ends on a deferred follow-up promise and nothing wakes the
#: session. Says what will NOT happen and what the person can do about it.
DEFERRED_FOLLOWUP_NOTICE = (
    "ℹ️ The reply says the agent will follow up later, but this turn set up no "
    "monitor, scheduled job or background run, and no monitor is running on "
    "this session — so nothing will wake it to send that update. Send a message "
    "when you want it, or ask the agent to set up a monitor."
)


def is_promise_only_terminal(final_segment_text: str) -> bool:
    """True when the turn's FINAL segment is a promise to act immediately, with no
    action taken and nothing substantive following the promise.

    ``final_segment_text`` MUST be the assistant text emitted AFTER the last tool
    call of the turn (the runner resets its segment buffer at each tool boundary,
    so its final ``assistant_text`` already is exactly that). Evaluating only the
    post-last-tool text is what keeps an already-executed action from being
    replayed: a turn that DID call the tool and then summarised has a final
    segment that is a summary, not a promise, so it does not match.

    Narrow by construction (three gates, all required):
      1. there is a first-person commitment to an immediate action
         (``I'll ... now`` / ``let me ... now`` / ``going to ... right away``);
      2. that promise sits at the END of the text (``...now.`` is the last
         sentence), so ``I'll explain now: <the explanation>`` does NOT match;
      3. no colon-introduced content, code fence, or list follows — those mean
         the "promise" was actually a lead-in to delivered content.
    Returns False on empty/whitespace so a genuinely empty turn stays on the
    dedicated empty-response path."""
    text = (final_segment_text or "").strip()
    if not text:
        return False
    # The three reject gates are scoped to the terminal sentence (where a matched
    # promise must live), NOT the whole segment — see `_SENTENCE_BOUNDARY_RE`.
    terminal = _terminal_sentence(text)
    # Gate 0: a permission-seeking or explicit-no-action closer is the OPPOSITE of
    # a promise-to-act, even when it contains an immediacy word ("let me know what
    # you'd like to do next", "I'll leave that as-is for now"). Reject up front so
    # the guard never overrides a user decision point with an unsolicited action.
    if _NO_ACTION_CLOSER_RE.search(terminal):
        return False
    # Gate 0b: a negated commitment ("I'm not going to ... now", "I won't ... now")
    # is an explicit NON-action, not an unfulfilled promise. Reject it before the
    # immediacy match, which would otherwise fire on the bare commitment token.
    if _NEGATED_PROMISE_RE.search(terminal):
        return False
    # Gate 0c: an approval-gated closer ("If that looks good, I'll push it now")
    # leaves the decision with the user; auto-continuing it dispatches the
    # action the user was still being asked to approve.
    if _APPROVAL_GATED_RE.search(terminal):
        return False
    # Gate 3: a promise that introduces content (colon + text, a fence, a list)
    # is a preamble to a real answer, not an unfulfilled action. Scanned over the
    # WHOLE text on purpose: the content that unmasks the promise as a preamble
    # follows it (a colon tail on the same line, or a fence/list on later lines),
    # so it is outside the terminal sentence by construction.
    if _PROMISE_HAS_FOLLOWING_CONTENT_RE.search(text):
        return False
    # Gates 1 + 2: an immediate-action commitment that is the terminal sentence.
    return bool(_PROMISE_NOW_RE.search(text))


# Fenced code blocks (``` or ~~~, any length ≥3) and inline code spans (a
# backtick run closed by an equal-length run on the same line) are QUOTED
# content: a user pasting a leak transcript, or the model explaining tool
# syntax, legitimately shows an invoke block inside them. Both are stripped
# before the leak scan so quoted machine syntax never triggers the notice.
# Only PAIRED delimiters are removed — an unpaired fence or tick run inside a
# genuinely leaked payload leaves the surrounding invoke tags visible (failure
# direction: a missed notice, never a suppressed one).
#
# LINEAR BY CONSTRUCTION, deliberately. The scan runs on the event loop at
# every turn completion over model-authored text, so it must stay linear on
# ADVERSARIAL input too: a variable-length backreference pattern like
# ``(`{3,}|~{3,}).*?(?P=fence)`` backtracks over every possible delimiter
# split, and a few thousand consecutive backticks stall the loop for tens of
# seconds — long enough for the liveness watchdog to kill the gateway. So the
# delimiters are tokenized with a fixed-alternative regex (no backreferences)
# and paired in one forward pass.
_CODE_DELIM_RE = re.compile(r"`+|~{3,}")


def _strip_quoted_code(text: str) -> str:
    """*text* with paired code fences and inline code spans removed, linearly.

    One forward pass over the delimiter tokens:

    * A run of ≥3 backticks or ≥3 tildes OPENS a fence; the next run of the
      same character AT LEAST AS LONG (CommonMark's closer rule) CLOSES it,
      and everything between is dropped — so a four-backtick quote can carry
      triple-backtick content without being cut short. Delimiters of the
      other kind, and shorter runs of the same kind, inside an open fence are
      content.
    * Outside a fence, a backtick run of length L opens an inline span; the
      next run of exactly length L on the SAME line closes it. A newline
      discards all pending span opens (spans cannot contain newlines), and a
      pending open that never closes is emitted as literal text.

    Every token is visited once and every output chunk is appended/discarded
    at most once, so the pass is linear whatever the input shape.
    """
    out: list[str] = []
    # Pending inline-span opens: run length -> index in ``out`` where the
    # opening run was appended. Cleared on every newline and on fence entry.
    span_open_at: dict[int, int] = {}
    fence_char = ""  # non-empty while inside an open fence
    fence_open_len = 0
    fence_open_out_idx = 0
    pos = 0
    for tok in _CODE_DELIM_RE.finditer(text):
        seg = text[pos : tok.start()]
        delim = tok.group()
        pos = tok.end()
        if fence_char:
            # Inside a fence: append tentatively — the content is dropped only
            # when a closer actually arrives, so an UNPAIRED fence leaves every
            # byte in place (fail toward detection, never suppression).
            out.append(seg)
            if delim[0] == fence_char and len(delim) >= fence_open_len:
                del out[fence_open_out_idx:]  # drop the fence and its content
                fence_char = ""
            else:
                out.append(delim)
            continue
        out.append(seg)
        if "\n" in seg:
            span_open_at.clear()
        if len(delim) >= 3:
            # Fence opener (either character). Tentatively append; the closer
            # (if any) truncates back to this index.
            fence_open_out_idx = len(out)
            out.append(delim)
            fence_char = delim[0]
            fence_open_len = len(delim)
            span_open_at.clear()
            continue
        opened = span_open_at.pop(len(delim), None)
        if opened is not None:
            del out[opened:]  # drop the span, its delimiters, and its content
            # Pending opens recorded INSIDE the just-dropped span went with it;
            # keeping their indices would point past the truncated output.
            span_open_at = {ln: i for ln, i in span_open_at.items() if i < opened}
        else:
            span_open_at[len(delim)] = len(out)
            out.append(delim)
    # An unpaired fence needs no special handling here: its tentatively-
    # appended delimiter is already in ``out`` as literal text.
    out.append(text[pos:])
    return "".join(out)


# The leaked block's signature: an invoke open tag with a name attribute
# (optionally namespace-prefixed the way some harnesses emit it). MACHINE-SHAPED
# on purpose — unlike the promise-only detector this is not a natural-language
# inference, so the false-positive surface is quoted syntax (handled by the
# stripping above), not phrasing.
_LEAKED_INVOKE_OPEN_RE = re.compile(
    r"<(?:[A-Za-z][\w.-]*:)?invoke\s+name\s*=\s*[\"'][^\"'<>]+[\"']"
)
# Corroboration: the block must also carry a parameter tag or its own close tag,
# so a lone stray open tag in prose (a truncated quote, a typo'd example) does
# not read as a full leaked invocation.
_LEAKED_INVOKE_BODY_RE = re.compile(r"<(?:[A-Za-z][\w.-]*:)?(?:parameter\b|/invoke\s*>)")


def open_construct_at_end(text: str) -> str:
    """Name the construct left OPEN at the end of *text*, or ``""`` when
    finalizing a segment here is safe.

    A segment cut is a PERSISTENCE boundary: the accumulated text becomes its
    own assistant row and the buffer resets. Cutting inside a construct that
    renders only when WHOLE therefore destroys it, because the halves persist
    as two rows and nothing rejoins them.

    A tool call cuts the segment, so while background agents run their tool
    calls interleave with the parent's token stream and cut it wherever they
    land. One reply then persists as many assistant rows split mid-word, and an
    options marker whose closing ``]`` falls in the next row matches no render
    grammar: the chips are lost and the tail reaches the user as literal prose.
    The alternation repeats for as long as the wave runs.

    Provenance cannot decide this. A child tool call arrives on this path with
    no ``sub_session_id`` and no agent marker, indistinguishable from the
    parent's own call, so the cut is made safe by asking WHERE it lands rather
    than who owns it.

    Deferral is bounded by construction: the turn-end flush does not consult
    this predicate, so an unterminated construct still persists when the turn
    closes. The cost of deferring is transcript ORDERING (the text lands after
    the tools it preceded) and never text loss.

    The name is returned rather than a bool so a deferred cut can log WHY.
    """
    if not text:
        return ""
    # An options marker is open when a trailing ``[OPTIONS:`` fragment is still
    # in flight. split_options_trailer owns that rule: at hide_partial=True it
    # trims an in-flight partial marker, at hide_partial=False it keeps it, so
    # the two bodies differ exactly when one is open. Reusing it (rather than a
    # local ``[OPTIONS:`` + ASCII ``]`` test) keeps the MARKER_CLOSERS set and
    # the grammar viability check in one place -- an ASCII-only copy misses a
    # marker whose only closers are lookalikes (e.g. ``】``).
    held, _choices = split_options_trailer(text, hide_partial=True)
    kept, _choices2 = split_options_trailer(text, hide_partial=False)
    if held != kept:
        return "options-line"
    # A fence is open at the end exactly when the text ends INSIDE a fenced
    # block. iter_fence_lines classifies each line as opener/closer/body/
    # outside; walking those roles leaves `inside` true iff the last opener was
    # never matched by a closer. Reusing this machine avoids a second spelling
    # of the fence rule (its docstring forbids one) and inherits the real
    # CommonMark logic -- line-start openers, matched run lengths -- so an
    # inline triple-backtick in prose does not read as an open fence, and a
    # closed fence whose closer line ends exactly at the text end is not
    # mistaken for an open one.
    inside_fence = False
    for _line, role in iter_fence_lines(text):
        if role == FENCE_OPEN:
            inside_fence = True
        elif role == FENCE_CLOSE:
            inside_fence = False
    if inside_fence:
        return "code-fence"
    # A widget is open when the last widget span parse_widgets finds is
    # unterminated. Reusing its ``truncated`` flag (rather than counting
    # ``<mcwidget`` vs ``</mcwidget>``) honours the code-span and fence rules
    # that module applies, which a naive tag count ignores.
    widgets = parse_widgets(text)
    if widgets and widgets[-1].truncated:
        return "mcwidget"
    return ""


def has_leaked_tool_call(text: str) -> bool:
    """True when *text* contains a tool invocation emitted as PROSE — an
    unquoted invoke-block open tag with a parameter or close tag: the model
    writes the call into the text channel instead of executing it, the turn ends
    with zero tool calls, and the session silently stalls.

    Quoted syntax is excluded structurally: fenced code blocks and inline code
    spans are stripped before the scan, so a pasted bug report or an explained
    example never matches. Callers decide what to DO with a match, and the two
    turn shapes differ: :func:`should_notice_leaked_tool_call` requires
    ``turn_tool_calls == 0`` because it un-lands the turn, which would
    misdescribe a turn whose earlier calls had side effects, while
    :func:`should_notice_mixed_turn_leak` handles that shape notice-only. A
    tool-heavy turn IS a leak — it is just not un-landable.
    """
    if not text:
        return False
    stripped = _strip_quoted_code(text)
    opener = _LEAKED_INVOKE_OPEN_RE.search(stripped)
    if not opener:
        return False
    # Corroboration must FOLLOW the opener: a parameter/close tag sitting
    # before a lone opener is unrelated markup, not this invocation's body.
    return bool(_LEAKED_INVOKE_BODY_RE.search(stripped, opener.end()))


def should_notice_leaked_tool_call(
    *,
    stop_reason: str,
    end_turn_reason: str,
    final_segment_text: str,
    prompt_depth: int,
    is_cancelled: bool,
    refusal_reasons: list,
    turn_tool_calls: int = 0,
) -> bool:
    """Decide whether to surface the leaked-tool-call NOTICE.

    The defect: the model emits an invoke block into its TEXT channel instead
    of executing it (observed when the target is a deferred MCP tool whose
    schema is not yet bound, and with large nested arguments), the turn then
    ends with zero tool calls, and the session idles — in a monitor/autonudge
    loop this is a silent stall the user only discovers by noticing nothing
    happened.

    NOTICE-ONLY, deliberately. An injected "re-issue that call" continuation
    would carry runtime authority into a session where the call can execute
    with no human gate — slot trust, global yolo, OR a static agent tool
    allowlist all auto-approve it, and the last of these is invisible at this
    layer, so no downgrade condition can be written here that fails closed.
    The leaked block is also not proof of the model's own intent: untrusted
    external content (a pasted issue, a fetched page quoting a leak) can carry
    an unfenced block the model merely reproduces. So the turn is marked
    un-landed and the user gets a visible card naming what happened; nothing
    is queued and nothing can execute. An unattended loop loses one cycle and
    retries on its own schedule — visibly, which is the half of the defect this
    layer can fix honestly.

    Gates: the turn ended NORMALLY (cancel/refusal/error paths own their own
    reporting), made ZERO tool calls — not because a tool-heavy turn is a
    different shape (it is the same leak) but because THIS path un-lands the
    turn, and a turn whose earlier calls had real side effects must not be
    marked unacted; that shape is noticed without un-landing by
    :func:`should_notice_mixed_turn_leak` — is top-level, and its final segment carries the machine-shaped
    leak (:func:`has_leaked_tool_call`). No one-shot budget: nothing is
    re-queued, so there is no loop to bound, and every leaked turn deserves
    its own visible mark.
    """
    if is_cancelled or refusal_reasons:
        return False
    if turn_tool_calls != 0:
        return False
    if stop_reason != end_turn_reason:
        return False
    if prompt_depth != 0:
        return False
    return has_leaked_tool_call(final_segment_text)


def should_notice_mixed_turn_leak(
    *,
    stop_reason: str,
    end_turn_reason: str,
    final_segment_text: str,
    prompt_depth: int,
    turn_tool_calls: int,
) -> bool:
    """Decide whether to surface the MIXED-TURN leak notice.

    The shape :func:`should_notice_leaked_tool_call` deliberately declines: the
    turn dispatched tool calls and THEN leaked a final one as text. Its
    ``turn_tool_calls == 0`` gate exists to protect UN-LANDING — earlier calls
    in the turn may already have taken effect, so marking it unacted could
    misdescribe it — and that reasoning is untouched here. This predicate
    governs only the user-visible NOTICE, which carries no such hazard: nothing
    is queued, nothing is un-landed, nothing executes.

    Why the notice is worth a card on a shape that lands normally: the mixed
    turn hides the gap BETTER than the zero-call one, not worse. The model
    reads state, announces the write, leaks the write, and the reply lands
    looking like a completed action — some of the work may already have
    happened, so there is no stall to notice and no missing output to explain.
    Before this the only trace was a gateway log line, which the person in the
    chat does not read.

    Conditions are EXACTLY the ones the runner already used for its diagnostic
    log, so the set of diagnosed turns does not move — only their visibility:
    at least one dispatched tool call, a NORMAL end-turn (which excludes the
    cancelled stop reason), top-level, and a final segment carrying the
    machine-shaped leak (:func:`has_leaked_tool_call`).

    The card says the earlier calls were ATTEMPTED rather than ran, and never
    "nothing was run" as the sibling does, because ``turn_tool_calls`` counts
    dispatches at the tool-call event, before approval — a refused call
    increments it too. The count bounds what may have landed rather than
    asserting any of it did, and a reader who took "nothing was run" literally
    here would redo work that already took effect.

    ``turn_tool_calls`` is REQUIRED, unlike the sibling's defaulted parameter:
    zero is that one's firing shape but this one's refusing shape, so a default
    here would silently disable the predicate rather than exercise it.
    """
    if turn_tool_calls <= 0:
        return False
    if stop_reason != end_turn_reason:
        return False
    if prompt_depth != 0:
        return False
    return has_leaked_tool_call(final_segment_text)


def should_notice_compaction_dropped_leak(
    *,
    dropped_leak: bool,
    leak_already_noticed: bool,
    stop_reason: str,
    end_turn_reason: str,
    prompt_depth: int,
    is_cancelled: bool,
    refusal_reasons: list,
) -> bool:
    """Decide whether to surface a notice for a leak the COMPACTION BOUNDARY ate.

    The shape neither sibling can see. A real (non-synthesized) mid-turn
    compaction terminal is a segment boundary, so the runner resets
    ``assistant_text`` there: text streamed before the summarization belongs to
    the window that was just summarized and must not carry into the segment
    flushed afterwards. That reset is correct for the text path and wrong for
    this one — the leak scan reads the accumulator AT TURN END, so a leak that
    streamed before the boundary is gone by the time either
    :func:`should_notice_leaked_tool_call` or
    :func:`should_notice_mixed_turn_leak` runs, and both decline on an empty
    segment.

    The leak still reached the user: chunks stream to the wire as they arrive,
    so the raw invoke block was on screen before the boundary wiped the
    accumulator. Nothing persisted it (the boundary does not flush) and nothing
    explained it, so the turn shows raw machine syntax, runs no tool, and
    carries no account of either — the silent pass-through this predicate
    closes.

    The turn then usually takes the post-compaction continuation arm, which
    fires precisely BECAUSE the segment is blank
    (:func:`should_continue_after_compaction`). So the user sees the leaked XML
    followed by "continuing automatically", with no statement that a tool call
    was written as prose instead of run.

    Takes the FACT, not the text: at turn end the text is already gone, which is
    the defect. The boundary is the only place it exists, so the scan
    (:func:`has_leaked_tool_call`) runs there and only the boolean travels.

    NOTICE-ONLY, and more strictly so than its siblings. It does not un-land the
    turn and sets no flag any recovery arm reads, so it never competes with the
    continuation the compaction path injects — a leaked block must not be
    re-issued by this layer under any of the three auto-approval routes
    (:func:`should_notice_leaked_tool_call` documents why). It therefore needs
    no ``turn_tool_calls`` gate: that gate exists to protect UN-LANDING, and
    there is nothing here to un-land.

    Owning no outcome is also why the caller evaluates this OUTSIDE the
    ``if``/``elif`` chain its siblings sit in. Every arm of that chain owns the
    turn's outcome, and two of them are RECOVERIES (the L1 infrastructure retry
    and the promise-only guard); a turn can both drop a leak at its boundary and
    need one of those, and the gates below exclude neither shape. An exclusive
    slot would therefore starve the recovery, so the card is evaluated
    independently and the turn keeps whatever outcome its own arm gave it.
    ``leak_already_noticed`` — not the ordering — is what keeps one turn to one
    leak card: the caller sets it on whichever sibling already carded.

    Gates are the turn-shape ones only: the boundary recorded a leak, no sibling
    has carded this turn, the turn ended NORMALLY, was not cancelled, carried no
    tool refusals (those paths own their own reporting), and is top-level.
    """
    if not dropped_leak:
        return False
    if leak_already_noticed:
        return False
    if is_cancelled or refusal_reasons:
        return False
    if stop_reason != end_turn_reason:
        return False
    return prompt_depth == 0


# Some model backends occasionally emit their private context-budget reminder as
# answer text at the end of a long turn. Keep recognition deliberately exact and
# suppression conservative: ordinary discussion of tokens and quoted/code text
# survives without provenance. A capacity request the session's own human wrote
# fails open regardless of request verb or turn kind; typed provider recovery is
# host-minted provenance.
# The prefix is recognized at a line break, at end of input, or glued directly to
# an uppercase letter: the observed provider shapes are a banner-only message and
# a banner fused to the reply's first word with no separator. A lowercase
# continuation (``leftover``), punctuation before glued text, and quoted or
# mid-text mentions are not recognized. Recognition proves only the shape; who
# authored the bytes is decided by ``classify_provider_budget_banner``.
# No regex: the banner is the fixed text ``You have <count> weighted tokens left``,
# compared exactly around one numeric field.
_PROVIDER_BUDGET_BANNER_HEAD = "You have "
_PROVIDER_BUDGET_BANNER_TAIL = " weighted tokens left"
_ASCII_DIGITS = frozenset("0123456789")
#: The topic words of a model-capacity or token-budget request, compared as whole
#: words (a two-word entry as two words with one space between). Any text from
#: the session's own human that names one of them fails open: the banner stays
#: visible on that turn. A list of question shapes cannot be complete ("how many
#: tokens are left", "I can still use", "have I got left", "the number of
#: tokens", "whether I have any tokens left" are a few of the forms a person
#: uses), and the two mistakes are not symmetric: failing open on a turn that
#: mentions tokens shows a leaked banner once, which is what every such turn
#: showed without suppression, while a missed wording deletes an answer the
#: person asked for. So no verb, subject or question shape is required, and a
#: message about an API token or a project budget fails open too.
_PROVIDER_BUDGET_TOPIC_PHRASES = frozenset(
    {
        "token",
        "tokens",
        "budget",
        "budgets",
        "capacity",
        "quota",
        "quotas",
        "allowance",
        "allowances",
        "context window",
        "context windows",
    }
)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _skip_blanks(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _is_banner_count(field: str) -> bool:
    """A plain ASCII count (``8154``) or one grouped by commas in threes (``8,154``)."""
    groups = field.split(",")
    if not all(group and set(group) <= _ASCII_DIGITS for group in groups):
        return False
    return len(groups) == 1 or (len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]))


def _banner_end(text: str, pos: int) -> int | None:
    """Where a banner whose fixed text ends at ``pos`` stops, or None if it is not one.

    It stops before an ASCII uppercase letter glued to it (left in place), or
    after an optional ``.``/``!``, blanks and either the end of the text or one
    or more line breaks and the blanks after them.
    """
    if pos < len(text) and "A" <= text[pos] <= "Z":
        return pos
    if pos < len(text) and text[pos] in ".!":
        pos += 1
    pos = _skip_blanks(text, pos)
    if pos == len(text):
        return pos
    after = pos
    while True:
        if text.startswith("\r\n", after):
            after += 2
        elif text.startswith("\n", after):
            after += 1
        else:
            break
    return None if after == pos else _skip_blanks(text, after)


def strip_provider_budget_banner(text: str) -> tuple[str, bool]:
    """Remove one leading provider-only weighted-token banner.

    Returns ``(clean_text, removed)``. A leading-only rule is intentional: the
    provider artifact arrives before answer prose, while the same words later in
    an answer can be legitimate user-facing discussion. The fixed parts are
    compared exactly and the count field must be ASCII digits.
    """
    source = text or ""
    start = _skip_blanks(source, 0)
    if not source.startswith(_PROVIDER_BUDGET_BANNER_HEAD, start):
        return text, False
    field_start = start + len(_PROVIDER_BUDGET_BANNER_HEAD)
    field_end = field_start
    while field_end < len(source) and (
        source[field_end] in _ASCII_DIGITS or source[field_end] == ","
    ):
        field_end += 1
    if not _is_banner_count(source[field_start:field_end]):
        return text, False
    if not source.startswith(_PROVIDER_BUDGET_BANNER_TAIL, field_end):
        return text, False
    end = _banner_end(source, field_end + len(_PROVIDER_BUDGET_BANNER_TAIL))
    if end is None:
        return text, False
    return source[end:], True


def mentions_provider_budget(message: str) -> bool:
    """Return whether the asker's text names tokens, a budget, capacity or a quota.

    Any such mention fails open, with no verb, subject or question shape
    required: a leaked banner then stays visible on that turn, which is what
    every such turn shows without suppression, while a missed wording would
    delete an answer the person asked for. Whitespace is folded before the
    search so a wrapped or double-spaced message reads the same. Fixed
    synthetic provider-recovery text never consults this gate.
    """
    # Lower-case one character at a time, keeping any character whose lower case
    # is longer (``İ``), so word boundaries stay where the message has them.
    folded = "".join(
        low if len(low := ch.lower()) == 1 else ch for ch in " ".join((message or "").split())
    )
    return any(_names_word(folded, phrase) for phrase in _PROVIDER_BUDGET_TOPIC_PHRASES)


def _names_word(text: str, phrase: str) -> bool:
    """Whether ``phrase`` occurs in ``text`` with no word character on either side."""
    start = text.find(phrase)
    while start != -1:
        end = start + len(phrase)
        if (start == 0 or not _is_word_char(text[start - 1])) and (
            end == len(text) or not _is_word_char(text[end])
        ):
            return True
        start = text.find(phrase, start + 1)
    return False


#: A separator between digit groups (``8,154``, ``8 154``, a thin or no-break
#: space). Folded away before a count is read or looked for, so a number reads
#: the same however it is grouped.
_DIGIT_GROUP_SEPARATORS = frozenset(",\u202f\u00a0 ")


def _drop_digit_grouping(text: str) -> str:
    """Drop each separator that sits after a digit and before exactly three more digits."""
    kept = []
    for index, ch in enumerate(text):
        if ch in _DIGIT_GROUP_SEPARATORS and index > 0 and text[index - 1].isdecimal():
            group = text[index + 1 : index + 4]
            after = index + 4
            if (
                len(group) == 3
                and group.isdecimal()
                and (after == len(text) or not _is_word_char(text[after]))
            ):
                continue
        kept.append(ch)
    return "".join(kept)


def _fold_unicode_digits(text: str) -> str:
    """Map every Unicode decimal digit to its ASCII equivalent, nothing else.

    A count the reply echoes can be written in another script's decimal digits
    -- Arabic-Indic ``٨١٥٤``, Devanagari, fullwidth -- and still name the same
    whole number as the banner's ASCII ``8154``. Each decimal digit (Unicode
    category Nd) becomes its value ``0``-``9``; every other character is left
    exactly as it is, so this is a per-digit map, never a broad substring
    rewrite. Run before the count is read or searched for so both sides compare
    on one digit alphabet. ASCII text (the common case) is returned untouched.
    """
    if not text or text.isascii():
        return text
    return "".join(
        str(unicodedata.decimal(ch)) if (not ch.isascii() and ch.isdecimal()) else ch for ch in text
    )


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _escape_hex_at(text: str, pos: int) -> tuple[int, int] | None:
    """Read a JSON ``\\uXXXX`` escape starting at ``pos``, behind any run of backslashes
    (a payload serialised twice doubles them): ``(code unit, end)``, or None."""
    after = pos
    while after < len(text) and text[after] == "\\":
        after += 1
    if after == pos or not text.startswith("u", after):
        return None
    digits = text[after + 1 : after + 5]
    if len(digits) != 4 or not set(digits) <= _HEX_DIGITS:
        return None
    return int(digits, 16), after + 5


def _decode_json_unicode_escapes(text: str) -> str:
    """Spell each JSON ``\\uXXXX`` escape in *text* as the character it encodes.

    A tool result whose payload is serialised with ``json.dumps`` (an object a
    parser cannot show as plain text, such as a stderr-only shell result) keeps
    every non-ASCII character as an escape, so an Arabic-Indic ``٨١٥٤`` in it
    reads ``\\u0668\\u0661\\u0665\\u0664`` and names no number at all. Decoding
    the escapes lets such a figure, and an escaped digit-group separator beside
    it, read as the characters the tool returned. A lone surrogate escape is
    left as it is. Text with no escape is returned untouched.
    """
    if "\\u" not in text:
        return text
    out = []
    pos = 0
    while pos < len(text):
        if text[pos] != "\\":
            out.append(text[pos])
            pos += 1
            continue
        escape = _escape_hex_at(text, pos)
        if escape is None:
            # No escape starts anywhere in this backslash run (each later start
            # ends the run at the same place), so keep the whole run as written
            # in one step: the decode stays linear on a long run.
            run_end = pos
            while run_end < len(text) and text[run_end] == "\\":
                run_end += 1
            out.append(text[pos:run_end])
            pos = run_end
            continue
        code, end = escape
        if 0xD800 <= code <= 0xDBFF:
            low = _escape_hex_at(text, end)
            if low is not None and 0xDC00 <= low[0] <= 0xDFFF:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low[0] - 0xDC00)))
                pos = low[1]
                continue
        # A lone surrogate is left as written.
        out.append(text[pos:end] if 0xD800 <= code <= 0xDFFF else chr(code))
        pos = end
    return "".join(out)


def _banner_count(banner: str) -> str:
    """The count a matched banner carries, with Unicode digits and grouping folded away."""
    text = _drop_digit_grouping(_fold_unicode_digits(banner or ""))
    start = 0
    while start < len(text) and not text[start].isdecimal():
        start += 1
    end = start
    while end < len(text) and text[end].isdecimal():
        end += 1
    return text[start:end]


def _names_whole_number(text: str, count: str) -> bool:
    """Whether ``count`` occurs in ``text`` as a whole number, optionally written as
    an integral decimal (``8154.0``): no digit or ``.`` before it, and no digit or
    ``.``-then-digit after it."""
    start = text.find(count)
    while start != -1:
        before_ok = start == 0 or not (text[start - 1].isdecimal() or text[start - 1] == ".")
        end = start + len(count)
        if text.startswith(".0", end):
            end += 1
            while end < len(text) and text[end] == "0":
                end += 1
        after_ok = not (
            (end < len(text) and text[end].isdecimal())
            or (text.startswith(".", end) and end + 1 < len(text) and text[end + 1].isdecimal())
        )
        if before_ok and after_ok:
            return True
        start = text.find(count, start + 1)
    return False


def _carries_count(text: str, count: str) -> bool:
    """Whether ``text`` names ``count`` as a whole number, however its digits are grouped or scripted.

    ``You have 8,154 weighted tokens left``, ``you have 8154 weighted tokens
    left``, a translation carrying Arabic-Indic ``٨١٥٤`` and ``{"remaining":
    8154}`` all carry ``8154``, and so does the integral decimal ``8154.0`` a
    JSON number may be written as; ``18154``, ``8154.2`` and ``8154.05`` do not
    name the same whole number. Unicode decimal digits are folded to ASCII first so a figure
    the reply echoes in another script still protects it, then digit grouping is
    folded, so both ends of the comparison read one digit alphabet. A figure a
    serialised tool result carries as JSON ``\\uXXXX`` escapes counts too: the
    text is read both as written and with those escapes decoded, so decoding can
    only find more evidence, never hide a count the written text shows.
    """
    raw = text or ""
    for candidate in dict.fromkeys((raw, _decode_json_unicode_escapes(raw))):
        if _names_whole_number(_drop_digit_grouping(_fold_unicode_digits(candidate)), count):
            return True
    return False


def classify_provider_budget_banner(
    text: str,
    user_authority: str,
    *,
    is_provider_recovery: bool,
    is_transient_recovery: bool,
    prior_visible_output: bool,
    tool_results: Iterable[str] = (),
    echo_evidence_complete: bool = True,
    transcript_texts: Iterable[str] = (),
    turn_inputs: Iterable[str] = (),
) -> tuple[str, bool]:
    """Classify one exact leading banner using artifact-specific provenance.

    ``bool`` means the matched prefix is provider metadata. The checks that keep
    the text visible run before either strip, so they hold on every turn kind:
    a typed transient retry may legitimately repeat the original answer, a
    mention of tokens, a budget, capacity or a quota in ``user_authority``
    keeps the bytes, and the banner's count (the same number, however grouped,
    in any case, wording or language) in ``user_authority`` itself, in a tool
    result in ``tool_results`` or in an earlier row in ``transcript_texts`` is
    evidence the reply echoes, reformats, translates or repeats text the turn
    can see. A count that appears nowhere the turn can see is the provider's
    own figure. The caller
    passes as ``user_authority`` the turn's own payload unless it is runner- or
    app-authored (a payload another session or a scheduled job injected is read
    like typed text), plus each consumed steer the session's human typed, never
    a recovery's own continuation text, as ``tool_results`` the
    text of every tool result delivered to the model this turn, and as
    ``transcript_texts`` the text and tool output of every row of an earlier
    turn in this conversation, whoever wrote it, and as ``turn_inputs`` what
    the model read as input this turn, whoever wrote it: the prompt as sent (an
    ``@prompt`` body, an app's or a peer's injected payload, the runner's own
    prepends) and every steer the turn consumed, a peer's included.
    ``turn_inputs`` is count evidence only: the topic gate reads
    ``user_authority`` alone, so a peer or a prompt file that names tokens does
    not keep a banner by its topic.

    Only then do the strips apply. Typed provider recovery has host-minted
    ownership and removes the prefix. For an ordinary turn, grammar alone cannot
    prove authorship: a leading prefix before semantic answer text remains
    byte-for-byte visible, and a banner-only tail is an artifact only after the
    turn already delivered visible model output AND the echo evidence is whole.
    ``echo_evidence_complete`` is False when a prompt image could hold the
    count, an earlier transcript prefix is outside the live window, or a tool
    result's recorded text is not known to be whole. The tail then stays visible
    as an echo the available evidence cannot rule out, and the typed-recovery
    strip stands down for the same reason. A
    banner-only turn with no such evidence is ambiguous and therefore stays
    visible, including regenerated answers and echoes whose prompt uses wording
    this classifier has never seen.
    """
    stripped, matched = strip_provider_budget_banner(text)
    if not matched:
        return text, False
    if is_transient_recovery or mentions_provider_budget(user_authority):
        return text, False
    # The echo evidence is the banner's count. The human's own words this turn,
    # a tool result delivered this turn, any row of an earlier turn, or any input
    # the model read this turn (the prompt as sent, a consumed steer from anyone),
    # that carries the same number is what a reply in the banner's shape may be
    # echoing, reformatting, translating or repeating (grouped digits, a changed
    # case, a rewording, a wrapped line, another language all keep the number),
    # so the number alone is the test and both sides are folded first. A
    # different number, or none, is no evidence.
    count = _banner_count(text[: len(text) - len(stripped)])
    if count and (
        _carries_count(user_authority, count)
        or any(_carries_count(result, count) for result in tool_results)
        or any(_carries_count(earlier, count) for earlier in transcript_texts)
        or any(_carries_count(given, count) for given in turn_inputs)
    ):
        return text, False
    # Evidence the classifier could not read cannot rule an echo out for
    # either strip. A count may sit in prompt-image pixels, an omitted transcript
    # prefix, a cut result, or an output field the recorded row never showed.
    if not echo_evidence_complete:
        return text, False
    if is_provider_recovery:
        return stripped, True
    if prior_visible_output and not stripped:
        return stripped, True
    return text, False


#: How much per-turn input evidence a turn keeps: the prompt as sent plus every
#: consumed steer (``turn_inputs``), and what the session's own human wrote
#: (``user_authority``). A steer's admission records are released when it
#: settles, so these copies are the only ones left and need their own bound.
#: Past either bound a text is not kept and the turn's echo evidence is marked
#: incomplete: a count the dropped text held cannot be ruled out, so neither
#: strip runs and the banner stays visible.
TURN_EVIDENCE_MAX_ITEMS = 64
TURN_EVIDENCE_MAX_CHARS = 1_000_000


def retain_turn_evidence(retained: list[str], texts: Iterable[str]) -> bool:
    """Append each of *texts* to *retained* while both bounds hold.

    Returns False once a text did not fit (it and every later one are left
    out), True when all were kept. An empty text is kept as an item too, so the
    prompt as sent stays first whatever follows it.
    """
    size = sum(len(kept) for kept in retained)
    for text in texts:
        if len(retained) >= TURN_EVIDENCE_MAX_ITEMS or size + len(text) > TURN_EVIDENCE_MAX_CHARS:
            return False
        retained.append(text)
        size += len(text)
    return True


#: How many distinct image-path candidates a slot remembers from the prompts it
#: sent. Past it the slot counts as having sent an image, which only keeps a
#: banner-shaped reply visible.
SENT_IMAGE_REFS_MAX = 64


def remember_sent_image_refs(slot: object, refs: Iterable[str]) -> None:
    """Add *refs*, image paths a prompt this slot sent names, to the slot's record.

    Lexical candidates only (``image_path_screen.image_path_candidates``); whether
    one names a file is probed once a banner matched, never on an ordinary turn.
    The record is never cleared: the model's conversation keeps every picture
    it was sent. Past ``SENT_IMAGE_REFS_MAX`` the slot is marked as having sent an
    image outright.
    """
    kept = list(getattr(slot, "_sent_prompt_image_refs", ()) or ())
    for ref in refs:
        if ref in kept:
            continue
        if len(kept) >= SENT_IMAGE_REFS_MAX:
            slot._sent_prompt_image = True  # type: ignore[attr-defined]
            break
        kept.append(ref)
    slot._sent_prompt_image_refs = tuple(kept)  # type: ignore[attr-defined]


def extend_user_authority(authority: str, texts: Iterable[str]) -> tuple[str, bool]:
    """Join *texts* onto *authority* (one per line) while its size bound holds.

    Returns ``(authority, fits)``. ``fits`` is False once a text was left out;
    the caller then marks the turn's echo evidence incomplete.
    """
    for text in texts:
        if not text:
            continue
        joined = f"{authority}\n{text}"
        if len(joined) > TURN_EVIDENCE_MAX_CHARS:
            return authority, False
        authority = joined
    return authority, True


class TurnEchoEvidence:
    """What a banner-shaped reply may be echoing, read off the slot's rows.

    The one owner of the banner's echo evidence: the turn's runner builds it only
    once a banner matched, so a turn with no banner never walks its rows or
    probes the filesystem. ``tool_results`` and ``transcript_texts`` are lazy
    and are walked only when the count is looked up; ``complete`` says whether
    every source the model could echo was readable here.
    """

    def __init__(
        self,
        messages: list[dict],
        rows_before_turn: Iterable[str],
        *,
        prompt_text: str,
        turn_inputs: list[str],
        attachments: Iterable[object],
        sent_prompt_image: bool,
        omitted_older_rows: bool,
        tool_output_truncated: bool,
        inputs_overflowed: bool,
        carries_image_ref,
    ) -> None:
        self._messages = messages
        self._before = rows_before_turn
        self._prompt_text = prompt_text
        self._turn_inputs = turn_inputs
        self._attachments = tuple(attachments)
        self._sent_prompt_image = sent_prompt_image
        self._omitted_older_rows = omitted_older_rows
        self._tool_output_truncated = tool_output_truncated
        self._inputs_overflowed = inputs_overflowed
        self._carries_image_ref = carries_image_ref

    def _earlier(self, row: dict) -> bool:
        return str(row.get("ts") or "") in self._before

    @staticmethod
    def _row_meta(row: dict) -> dict:
        meta = row.get("meta")
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def _skill_load_bodies(cls, row: dict) -> tuple[list[str], bool]:
        """The skill bodies a ``skill_load`` row recorded, and whether every one is readable."""
        meta = cls._row_meta(row)
        if meta.get("kind") != "skill_load":
            return [], True
        skills = meta.get("skills")
        if not isinstance(skills, list):
            return [], False
        bodies = [
            s["body"] for s in skills if isinstance(s, dict) and isinstance(s.get("body"), str)
        ]
        return bodies, len(bodies) == len(skills)

    @classmethod
    def _is_prompt_load(cls, row: dict) -> bool:
        """Whether *row* is an ``@prompt`` chip, which keeps no copy of the expanded body.

        A chip written before rows carried ``kind: prompt_load`` is matched by
        its text, so an older transcript fails open too.
        """
        if cls._row_meta(row).get("kind") == "prompt_load":
            return True
        return row.get("role") == "system" and str(row.get("content") or "").startswith(
            _PROMPT_CHIP_PREFIX
        )

    @property
    def complete(self) -> bool:
        """Whether every source the model could echo is readable here.

        Not when: the turn's own input evidence overflowed its bound, a prompt
        image (this turn's or an earlier one's, which the model's conversation
        keeps) could hold the count, rows before the live window were not
        restored, a tool result this turn delivered was not whole, an earlier
        tool row does not record that it was whole (legacy rows fail open), or
        an earlier skill-load row kept no readable body for a skill it loaded,
        or an earlier ``@prompt`` expansion kept only its chip, not its body.
        ``turn_inputs`` holds the prompt as sent first, then the consumed steers.
        """
        if self._inputs_overflowed or self._sent_prompt_image is True:
            return False
        if self._omitted_older_rows or self._tool_output_truncated:
            return False
        request = "\n".join(
            (
                self._prompt_text,
                *self._turn_inputs[1:],
                *(str(path) for path in self._attachments),
            )
        )
        if self._carries_image_ref(request):
            return False
        for row in self._messages:
            if not self._earlier(row):
                continue
            meta = self._row_meta(row)
            if row.get("role") == "tool" and meta.get("outputTruncated") is not False:
                return False
            if not self._skill_load_bodies(row)[1]:
                return False
            if self._is_prompt_load(row):
                return False
            if row.get("role") == "user" and self._carries_image_ref(
                "\n".join(
                    (
                        str(row.get("content") or ""),
                        *(str(f) for f in meta.get("files") or ()),
                    )
                )
            ):
                return False
        return True

    def tool_results(self) -> Iterable[str]:
        """The output of every tool row this turn delivered."""
        for row in self._messages:
            if row.get("role") == "tool" and not self._earlier(row):
                yield str(self._row_meta(row).get("output") or "")

    def transcript_texts(self) -> Iterable[str]:
        """Every earlier row's text, tool output and recorded skill bodies, plus
        each assistant segment this turn already finalized.

        A consumed steer can ask the model to repeat its own first sentence, so
        this turn's finalized segments count too. The reply being classified is
        not a row yet (its chunks are not finalized), so it is never its own
        evidence.
        """
        for row in self._messages:
            if not (self._earlier(row) or row.get("role") == "assistant"):
                continue
            meta = self._row_meta(row)
            for text in (
                str(row.get("content") or ""),
                str(meta.get("output") or ""),
                *self._skill_load_bodies(row)[0],
            ):
                if text:
                    yield text
