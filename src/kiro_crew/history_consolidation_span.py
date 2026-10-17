"""The transcript span one consolidation pass covers, and how its attempt ends.

A history pass prompts a bounded, message-aligned prefix of a session's unconsolidated
tail: :func:`_consolidation_chunk` takes it within :data:`_CONSOLIDATION_PROMPT_BUDGET_CHARS`,
and a first message too large for the budget alone is cut into budget-sized slices
(:func:`_slice_head_for_budget`), at most :data:`_CONSOLIDATION_MAX_HEAD_SLICES` per head.
:class:`AttemptedSpan` is the identity a billed attempt records, and the retry metadata
keys are where ``ConversationLog`` stores it. The sentinels and exceptions here are the
ways a pass ends without its writes (refused, busy, never dispatched, persistence
switched off or the consolidation lease lost mid-run), and :class:`_RunCommitState` is whether a run has published anything yet.
:func:`_persistence_disabled` and :func:`_lesson_extraction_disabled` read the
``memory.*`` switches that gate a pass.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports no other member of the family.
"""

from __future__ import annotations

from typing import NamedTuple

from kiro_crew.history_projection import DISPLAY_ONLY_ROLES
from kiro_crew.image_refs import strip_image_refs
from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

# Budget-sized slices one over-budget head message may be prompted in before the
# fallback retires its remainder. A message near the transcript cap
# (:data:`~kiro_crew.history._SESSION_MAX_BYTES`, 10 MB) would otherwise slice
# into ~160 budget-sized pieces, each a sequential consolidation LLM call that
# holds up every message behind it — the per-head cost would scale with the
# message's size with no ceiling. Once the durable sub-offset has advanced
# ``N`` whole budget spans into the head AND more than one slice still remains
# (see :func:`_head_slice_ceiling_reached`) the head is retired the same bounded
# way the attempt cap retires an unprocessable span: its remainder is marked
# consolidated WITH a logged reason and the user-facing abandon notice (never
# silently), so the cost of one pathological message is bounded and the tail
# behind it still consolidates. The bound is on CHARACTERS, so near-span lines
# are prompted about ``N`` times (``N`` whole 64 KB spans ≈ 512 KB covered, and
# a head that drains within one more slice keeps its final short slice) while
# content whose lines advance well under a span (e.g. minified ~32 KB lines) can
# take up to roughly ``2 * N`` calls — a small constant either way, far past any
# genuine conversation turn; the retired remainder is a pasted blob memory
# extraction has no use for.
_CONSOLIDATION_MAX_HEAD_SLICES = 8


# Rendered CHARACTERS of transcript one history consolidation prompt may carry.
# The unconsolidated tail is otherwise unbounded: a session that goes a long time
# between passes — or whose consolidation kept failing — renders every message
# since the marker into one prompt, and past some length no provider accepts it.
# The span that most needs extracting is then the one that can never be
# extracted.
#
# Characters, not bytes: the ceiling exists to keep a prompt inside a context
# window, and a context window is measured in tokens. Code points track tokens
# far more evenly across scripts than UTF-8 bytes do — a CJK transcript is
# roughly one token per character but three bytes per character, so a byte
# budget would cut it to a third of the span it gives a Latin one for no reason
# the provider cares about. 65_536 characters leaves room beside the transcript
# for the instructions and the current memory blocks in every context window
# Kiro Crew dispatches to.
_CONSOLIDATION_PROMPT_BUDGET_CHARS = 64 * 1024


_CONSOLIDATION_META_KEYS: frozenset[str] = frozenset(
    {
        "consolidation_attempts",
        "consolidation_retry_at",
        "consolidation_env_failures",
        "consolidation_attempts_generation",
        "consolidation_attempts_offset",
        "consolidation_attempts_count",
        "consolidation_attempts_prompted",
        "consolidation_attempts_sub_offset",
        "consolidation_attempts_sub_bounded",
    }
)


#: The durable sub-message extraction offset and the rotation generation it is
#: tied to. When the FIRST unconsolidated message alone exceeds the prompt
#: budget it is sliced (see :func:`_slice_head_for_budget`), and
#: ``consolidation_sub_offset`` records how far into that message extraction has
#: reached. It is honoured only while ``consolidation_sub_offset_generation``
#: equals the live ``rotation_generation`` — a rotation or rewrite advances the
#: generation and the stale sub-offset is dropped, so the message is re-sliced
#: from its start rather than resumed at a character position that describes
#: different content. Kept SEPARATE from
#: :data:`_CONSOLIDATION_META_KEYS` because the retry accounting is cleared the
#: moment a span is marked (success or abandon), whereas a non-final slice's
#: sub-offset must survive to the next pass.
_CONSOLIDATION_SUBOFFSET_META_KEYS: frozenset[str] = frozenset(
    {
        "consolidation_sub_offset",
        "consolidation_sub_offset_generation",
    }
)


class _ConsolidationRefusedSentinel:
    """No completed consolidation pass was recorded for this span.

    Busy and backoff refusals precede dispatch, but a source recheck or lease
    loss can refuse after a provider turn was billed. Callers must leave
    completion bookkeeping unchanged for either sentinel.
    """

    __slots__ = ()


_CONSOLIDATION_REFUSED = _ConsolidationRefusedSentinel()
#: Another process holds the session's consolidation lease.
_CONSOLIDATION_BUSY = _ConsolidationRefusedSentinel()


def _no_pass_ran(outcome: object) -> bool:
    """True when *outcome* reports no completed pass recorded.

    Test this, never one sentinel instance: a new refusal reason must not read
    as a completed pass to the throttles and offsets that follow a pass.
    """
    return isinstance(outcome, _ConsolidationRefusedSentinel)


class AttemptedSpan(NamedTuple):
    """Identity of the transcript span a billed consolidation turn covered.

    ``total`` and ``prompted`` answer different questions and must not be
    collapsed. ``total`` is how far the TRANSCRIPT reached when the turn was
    charged. ``prompted`` is how far the PROMPT reached, and is the only offset
    the abandon path may write to the durable marker — the tail past it was
    never sent to any provider, so marking it consolidated would drop it from
    memory unread.

    The retry accounting stamps BOTH, and needs both: ``total`` is what a later
    transcript is compared against to tell new content from the same content,
    and ``prompted`` is what says whether that comparison means anything. An
    attempt that stopped short of ``total`` covered a prefix, and a prefix does
    not change when messages are appended behind it (see
    :meth:`ConversationLog._attempts_describe_current_span`).

    ``sub_offset`` and ``sub_prompted`` extend that identity INSIDE a single
    over-budget head message. When a message alone exceeds the budget it is
    sliced (see :func:`_slice_head_for_budget`): ``sub_offset`` is the character
    offset into the head the slice began at and ``sub_prompted`` where it
    reached. A failed slice is charged against ``(offset, sub_offset)``, so the
    attempt cap abandons at most one budget-sized slice rather than the whole
    message, and growth BEHIND the head cannot release a bounded sub-slice. Both
    are ``0`` for the ordinary message-aligned case, which leaves the existing
    identity unchanged.
    """

    total: int
    generation: int
    offset: int
    prompted: int
    sub_offset: int = 0
    sub_prompted: int = 0


class _ConsolidationNotDispatched(Exception):
    """A consolidation prompt never reached the provider."""


class _PersistenceDisabledMidRun(Exception):
    """The persistence switch turned off before this run committed output."""


class _LeaseLostMidRun(Exception):
    """The pass's consolidation lease belongs to a peer before it can write.

    Raised from a publication hold, a fenced marker write or a fenced failure
    charge when the token on the session's metadata line names another
    acquisition: the pass's renewals fell silent past the ceiling and a peer
    claimed the span it is still working on, so the outputs and accounting it
    would still write are that peer's.
    """


# The exceptions reach tracebacks and logs by their dotted name, which names the
# module they were defined in before the split.
_ConsolidationNotDispatched.__module__ = "kiro_crew.history_consolidation"
_PersistenceDisabledMidRun.__module__ = "kiro_crew.history_consolidation"
_LeaseLostMidRun.__module__ = "kiro_crew.history_consolidation"


class _RunCommitState:
    """What one consolidation run has committed, and the lease it commits under.

    ``lease_token`` is the acquisition token this run's durable writes are fenced
    on: every publication and marker write re-reads it under the transcript lock
    and refuses when a peer has taken the lease over. ``None`` for a run that
    holds no lease of its own (skill detection entered directly, tests).
    """

    __slots__ = ("committed", "lease_token")

    def __init__(self, lease_token: str | None = None) -> None:
        self.committed = False
        self.lease_token = lease_token

    def mark_committed(self) -> None:
        """Record that a guarded durable publication succeeded."""
        self.committed = True


def _persistence_disabled() -> bool:
    """True when the operator turned automatic memory writes off.

    ``memory.persistence_enabled`` is the global persistence switch and
    ``memory.writes_enabled`` the writes-only one; either off answers True. Consolidation
    is the largest automatic writer (lessons, semantic, episodic, preferences,
    projects, history, auto-skills all flow from one pass), so a disabled
    system must not schedule it — pausing entirely rather than run-and-discard,
    so no LLM turn is ever billed for output that would be thrown away.
    Read through ``KiroCrewConfig.load()`` (fingerprint-cached, so per-turn
    checks cost a stat) rather than a constructor flag, so flipping the key
    takes effect without a gateway restart. Imported lazily to keep this
    module's import graph light (same rationale as the facade seams above).
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.memory_sections import memory_writes_off_key

    return memory_writes_off_key(KiroCrewConfig.load().memory) is not None


def _lesson_extraction_disabled() -> bool:
    """True when consolidation must not save the lessons it extracted.

    ``memory.lesson_extraction_enabled`` stops the lesson stage alone: the pass
    still runs and writes history, semantic, episodic and the rest.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.memory_sections import lesson_extraction_enabled

    return not lesson_extraction_enabled(KiroCrewConfig.load().memory)


def _fmt_message(message: dict, *, prestripped: bool = False) -> str:
    """Render one transcript message for a consolidation prompt.

    The row's image references are replaced with a content-free marker before
    the text is quoted. A consolidation prompt is history ABOUT a session, and
    the prompt builder (``build_prompt_blocks``) inlines every still-readable
    image path it finds in a prompt as a real image block. Left in, each
    screenshot the session ever pasted rides along at full base64 size on every
    extraction turn: one measured span carried 83 attachments and 67 MB of
    image data around 600 KB of conversation, and the background session's own
    transcript grew by that whole record on each retry until its KAS process
    held 1.9 GB. Memory extraction reads text; it has no use for the pixels.

    *prestripped* must be set when ``message['content']`` is a SLICE of a head
    whose image references were already stripped over the whole head
    (:func:`_consolidation_head_content`). Stripping is NOT slice-local:
    ``strip_image_refs`` masks fenced code spans, and a slice that begins inside
    a fence hides from ``_mask_code_spans`` where that fence opened, so a
    re-strip would read the fenced block as prose and replace an image path the
    whole-head pass deliberately preserved — silently corrupting the extraction
    prompt and advancing past the corrupted slice. When *prestripped* is set the
    second strip is skipped and the already-resolved slice content is quoted
    verbatim. A WHOLE message (not a slice) must leave this False so its own
    refs are stripped once here.
    """
    tools = f" [tools: {', '.join(message['tools'])}]" if message.get("tools") else ""
    content = message["content"] if prestripped else strip_image_refs(message["content"])
    return f"[{message.get('ts', '?')[:16]}] {message['role'].upper()}{tools}: {content}"


def _prompt_rows(messages: list[dict]) -> list[dict]:
    """*messages* without display-only rows, which no consolidation prompt carries."""
    return [
        m
        for m in messages
        if m.get("role") not in DISPLAY_ONLY_ROLES and not is_warm_task_brief_message(m)
    ]


def _consolidation_chunk(messages: list[dict]) -> list[dict]:
    """Return the longest message-aligned prefix of *messages* that fits the budget.

    Message-aligned rather than byte-truncated so the marker can advance by a
    whole number of messages: a prompt cut mid-message would leave the durable
    offset describing a boundary that does not exist in the transcript, and the
    remainder would be re-rendered from a different starting point on the next
    pass. The caller marks exactly this prefix consolidated and leaves the rest
    for the pass after it.

    The separator is charged too. The prompt joins the rendered messages with
    ``"\\n"``, so a budget computed from the rendered sizes alone lets the
    transcript block exceed the ceiling by one character per message — enough to
    matter on a tail of thousands.

    Display-only rows (``DISPLAY_ONLY_ROLES``) are never rendered into a prompt
    (see :func:`_prompt_rows`), so they cost nothing here and are carried inside
    the prefix with the rows around them.

    A first rendered message that alone exceeds the budget is returned anyway
    rather than refused. Its size is a permanent property of the transcript, so refusing it
    stalls the session forever at whatever backoff the refusal arms, and every
    message behind it with it. Sending it is no worse than the unbounded prompt
    this budget replaces, and it terminates: an over-context provider error is a
    normal failed attempt, and the attempt cap abandons that one message so the
    tail behind it consolidates on the next pass.
    """
    budget = _CONSOLIDATION_PROMPT_BUDGET_CHARS
    used = 0
    rendered_any = False
    for index, message in enumerate(messages):
        if message.get("role") in DISPLAY_ONLY_ROLES or is_warm_task_brief_message(message):
            continue
        # One separator per rendered message after the first, matching the
        # "\n".join the prompt builder performs over exactly these strings.
        rendered = len(_fmt_message(message)) + (1 if rendered_any else 0)
        if rendered_any and used + rendered > budget:
            return messages[:index]
        used += rendered
        rendered_any = True
    return messages


def _head_exceeds_budget(message: dict) -> bool:
    """True when *message* alone renders larger than one prompt budget.

    This is the ``_consolidation_chunk`` returned-it-anyway case (a first
    rendered message over the budget): a whole-message prompt of it is rejected
    by any provider that enforces a context ceiling, so it is the message that
    sub-chunking slices rather than prompts whole.
    """
    if message.get("role") in DISPLAY_ONLY_ROLES or is_warm_task_brief_message(message):
        return False
    return len(_fmt_message(message)) > _CONSOLIDATION_PROMPT_BUDGET_CHARS


def _slice_boundary(content: str, start: int, span: int) -> int:
    """Where to end a slice of ``content[start:]`` that is at most *span* chars.

    Cut on the LAST stable boundary at or before ``start + span`` — a line break,
    then a paragraph/whitespace run, then a bare character — so the same message
    always slices the same way regardless of how far extraction has reached. A
    boundary search that found nothing (one unbroken token longer than the span)
    falls back to the character cut at ``start + span``, which still terminates.

    Returns an absolute offset into *content*, always ``> start`` (so a pass
    makes progress) and ``<= len(content)``.
    """
    end = min(start + span, len(content))
    if end >= len(content):
        return len(content)
    window = content[start:end]
    # Prefer a line boundary: cut just after the last newline in the window so
    # the slice ends on a whole line and the next slice starts a fresh one.
    nl = window.rfind("\n")
    if nl > 0:
        return start + nl + 1
    # Then any whitespace run (paragraph/word boundary), cut just after it.
    for i in range(len(window) - 1, 0, -1):
        if window[i].isspace():
            return start + i + 1
    # One unbroken token wider than the span: a bare character cut terminates.
    return end


def _consolidation_head_content(message: dict) -> str:
    """The head's prompt-ready content: image references stripped ONCE, whole.

    Slicing operates on THIS, not the raw content, for two reasons the Opus
    review flagged. ``strip_image_refs`` masks fenced code spans
    (``_mask_code_spans``) so a path inside a code fence stays readable. A raw
    ``content[start:end]`` cut can land inside a fenced block, flipping the
    fence parity the slice presents to ``strip_image_refs`` relative to the
    whole head — so a slice that opens mid-fence looks like prose and an image
    reference the whole-head scan would mask gets stripped (or, cut the other
    way, a real reference is left to be inlined into the prompt the sub-chunking
    path exists to keep image-free). Resolving references once over the WHOLE
    head removes the ambiguity: every reference is classified against the head's
    real fence structure, and the slices carry the already-resolved text.

    It is also what makes a slice's size honest. ``_fmt_message`` must NOT run
    ``strip_image_refs`` again when it renders a slice: a slice that begins
    inside a fence shows a different fence parity than the whole head, so a
    re-strip reads the fenced block as prose and replaces a path the whole-head
    pass preserved. So the slice is rendered with ``prestripped=True`` (the
    second strip is skipped) and the raw slice width predicts the rendered width
    directly, rather than being inflated — or corrupted — by a slice-local strip
    the slicer could not see.
    """
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    return strip_image_refs(content)


def _slice_head_for_budget(message: dict, sub_offset: int) -> tuple[dict, int, int, bool]:
    """Slice *message* from *sub_offset* to one budget-sized prompt-safe chunk.

    Returns ``(sliced_message, start, new_sub_offset, is_last_slice)``:

    * ``sliced_message`` is a shallow copy of *message* whose ``content`` is the
      slice ``content[start:new_sub_offset]`` of the HEAD'S STRIPPED content
      (see :func:`_consolidation_head_content`). It renders within the budget
      because the slice width leaves room for the envelope ``_fmt_message`` adds
      (``[ts] ROLE[tools]: ``), which is charged against the budget too.
    * ``start`` is the clamped offset the slice actually began at — equal to
      *sub_offset* when it was in range, else ``0`` (a stale offset at or past
      the content length re-slices from the start). The caller stamps the retry
      accounting with this, not the raw argument, so the recorded slice identity
      matches the content that was prompted.
    * ``new_sub_offset`` is the absolute character offset extraction has now
      reached in the head's STRIPPED content, to be made durable so a later pass
      resumes here (or, when ``is_last_slice``, so the marker moves past the
      whole message). The stripping is deterministic and idempotent, so the same
      head always strips to the same string and a durable sub-offset describes
      the same position on every pass.
    * ``is_last_slice`` is True when the slice reaches the end of the content, so
      the caller advances the durable message marker instead of the sub-offset.
    """
    content = _consolidation_head_content(message)
    start = sub_offset
    if start < 0 or start >= len(content):
        start = 0
    # The envelope (timestamp, role, tools, ": ") is charged against the budget,
    # so the content slice must be smaller than the budget by that much. Render
    # the message with empty content to measure the exact envelope width.
    envelope = len(_fmt_message({**message, "content": ""}))
    span = max(1, _CONSOLIDATION_PROMPT_BUDGET_CHARS - envelope)
    end = _slice_boundary(content, start, span)
    # The content is already image-ref-stripped (``_consolidation_head_content``)
    # and the final prompt renders this slice with ``prestripped=True`` (so
    # ``_fmt_message`` does NOT re-strip it — re-stripping a slice that begins
    # mid-fence corrupts preserved paths), hence a slice sized against the budget
    # renders at its raw width. Measure with the SAME ``prestripped=True`` here so
    # the shrink loop predicts the real rendered width. The loop is kept as a
    # cheap belt-and-braces check: should rendering ever add width (an envelope
    # wider than measured, a future renderer), shrink the raw slice until it
    # renders within the budget, keeping at least one character of progress.
    #
    # The next span is scaled by how far over budget the current slice rendered
    # (``budget / rendered``), so a slice dense with expanding refs converges in
    # a handful of steps rather than one character at a time. Each step is also
    # forced strictly below the current slice length — ``_slice_boundary`` cuts
    # on the last line/word boundary, so without that cap a line-aligned slice
    # would re-find the same boundary and never shrink — and a boundary search
    # that still does not move falls back to a bare character cut. ``end`` drops
    # every iteration and a smaller raw slice cannot render larger, so the loop
    # converges.
    budget = _CONSOLIDATION_PROMPT_BUDGET_CHARS
    while end > start + 1:
        rendered = len(_fmt_message({**message, "content": content[start:end]}, prestripped=True))
        if rendered <= budget:
            break
        length = end - start
        scaled = int(length * budget / rendered)
        span = max(1, min(length - 1, scaled))
        candidate = _slice_boundary(content, start, span)
        end = candidate if candidate < end else start + span
    sliced = {**message, "content": content[start:end]}
    return sliced, start, end, end >= len(content)


def _head_slice_ceiling_reached(message: dict, sub_offset: int) -> bool:
    """True when *message* has been sliced to its per-head ceiling already.

    A head near the transcript cap would otherwise slice into ~160 budget-sized
    pieces, each a sequential consolidation LLM call (see
    :data:`_CONSOLIDATION_MAX_HEAD_SLICES`). The ceiling is a CHARACTER bound on
    the head's stripped content — ``_CONSOLIDATION_MAX_HEAD_SLICES`` whole slice
    spans — read off the durable sub-offset alone, so it needs no extra persisted
    state and is exact across passes: *sub_offset* is where the next slice would
    begin. The retire fires only once BOTH hold: the sub-offset has reached that
    bound AND more than one slice of content still remains. A remainder that fits
    in one more slice is always prompted, never retired, so a head that drains in
    N (or N + 1) slices keeps its final short slice — there is no span that would
    have fit in one more slice yet is dropped unread (the earlier ``- 1`` bound
    dropped exactly that span).

    The bound is a count of CHARACTERS, not of slices. A slice cuts on the LAST
    line/word boundary at or before the span (``_slice_boundary``), so it
    advances the sub-offset by at most one span but, on content with no boundary
    near the span (very long lines, e.g. minified JSON ~32K-char lines), it can
    advance well under one span. So the number of LLM calls before the retire
    fires is about ``_CONSOLIDATION_MAX_HEAD_SLICES`` for near-span lines but can
    be up to roughly ``2 * _CONSOLIDATION_MAX_HEAD_SLICES`` for lines that advance
    about half a span each — bounded by a small constant either way, which is the
    point (it caps the per-head cost near the transcript limit at a dozen-odd
    calls instead of ~160). In all cases the retired remainder is handled
    fail-safe (logged + user notice), never marked consolidated silently.
    """
    content = _consolidation_head_content(message)
    envelope = len(_fmt_message({**message, "content": ""}))
    span = max(1, _CONSOLIDATION_PROMPT_BUDGET_CHARS - envelope)
    ceiling_chars = max(1, _CONSOLIDATION_MAX_HEAD_SLICES * span)
    resume = sub_offset if 0 <= sub_offset < len(content) else 0
    # Retire ONLY when the ceiling is reached AND more than one slice of content
    # still remains. A remainder that fits in a single slice is always prompted,
    # never retired, so a head that drains in N (or N+1) slices keeps its final
    # short slice — the log/notice then report the budget-span bound rather than
    # an exact slice count, since the actual slice count ranges from N (near-span
    # lines) to about 2N (lines that advance well under a span per cut).
    return resume >= ceiling_chars and (len(content) - resume) > span
