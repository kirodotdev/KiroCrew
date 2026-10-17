"""What a turn's final text claims, read before the runner decides how the turn ended.

Promise-only closers (a turn that says it will act and then stops), unfinished-progress
claims, and tool-call markup that leaked into the reply text, each judged with quoted
code stripped first.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import re

from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.messaging.split import FENCE_CLOSE, FENCE_OPEN, iter_fence_lines
from kiro_crew.widget_parse import parse_widgets

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
