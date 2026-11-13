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

# Budget-sized slices one over-budget head message may be prompted in before its
# remainder is retired. A message near the 10 MB transcript cap would otherwise
# take ~160 sequential LLM calls, holding up every message behind it. Past the
# ceiling (see :func:`_head_slice_ceiling_reached`) the remainder is marked
# consolidated with a logged reason and the user-facing abandon notice, never
# silently. The bound is on characters, so it costs about ``N`` to ``2 * N``
# calls depending on line lengths.
_CONSOLIDATION_MAX_HEAD_SLICES = 8


# Rendered CHARACTERS of transcript one history consolidation prompt may carry.
# Without a bound, a long-unconsolidated tail renders into one prompt no
# provider accepts, and the span that most needs extracting never is.
# Characters, not bytes: code points track tokens far more evenly across
# scripts (CJK is ~1 token but 3 UTF-8 bytes per character). 65_536 leaves room
# for the instructions and memory blocks in every context window Kiro Crew uses.
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
#: tied to. ``consolidation_sub_offset`` records how far into an over-budget
#: first message (see :func:`_slice_head_for_budget`) extraction has reached. It
#: is honoured only while ``consolidation_sub_offset_generation`` equals the live
#: ``rotation_generation``, so a rotation or rewrite re-slices from the start.
#: Separate from :data:`_CONSOLIDATION_META_KEYS`: retry accounting is cleared
#: when a span is marked, but a non-final slice's sub-offset must survive.
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
    completion bookkeeping unchanged for every sentinel.
    """

    __slots__ = ()


_CONSOLIDATION_REFUSED = _ConsolidationRefusedSentinel()
#: Another process holds the session's consolidation lease.
_CONSOLIDATION_BUSY = _ConsolidationRefusedSentinel()
#: A preference-only pass got no usable answer: the turn was billed and came
#: back with no reply or an unparseable one, or it could not be dispatched. Nothing was
#: extracted, so the window stays pending and the caller arms its own backoff.
_CONSOLIDATION_EMPTY = _ConsolidationRefusedSentinel()
#: The turn was billed, but the conversation moved on (a newer user row, or the
#: transcript changed) before the result could be written, so it was discarded.
#: The window stays pending; the threshold caller waits before it tries again.
_CONSOLIDATION_STALE = _ConsolidationRefusedSentinel()


class PrefsWindow:
    """Where a preference-only pass starts, and where the one it ran stopped.

    The preference-only window has no durable marker: the threshold caller
    tracks it as an in-memory offset. It hands the offset in as ``start`` (an
    absolute message count), and the pass prompts one budget-sized,
    message-aligned chunk from there and writes the absolute count it covered
    to ``end``. The caller advances its offset to ``end``, so a tail larger
    than the budget drains over later passes instead of being skipped.
    ``end`` stays ``None`` until the pass knows its span.

    ``start`` counts rows of one transcript generation: a rotation or rewrite
    replaces the rows it counted. ``generation`` is the ``rotation_generation``
    it was recorded under (``None``: unknown, read as current). A pass whose
    snapshot is of another generation starts at the snapshot's own offset
    instead, and writes the generation it read to ``snapshot_generation``.
    """

    __slots__ = ("end", "generation", "snapshot_generation", "start")

    def __init__(self, start: int, generation: int | None = None) -> None:
        self.start = start
        self.generation = generation
        self.end: int | None = None
        self.snapshot_generation: int | None = None


def _no_pass_ran(outcome: object) -> bool:
    """True when *outcome* reports no completed pass recorded.

    Test this, never one sentinel instance: a new refusal reason must not read
    as a completed pass to the throttles and offsets that follow a pass.
    """
    return isinstance(outcome, _ConsolidationRefusedSentinel)


class AttemptedSpan(NamedTuple):
    """Identity of the transcript span a billed consolidation turn covered.

    ``total`` is how far the TRANSCRIPT reached when the turn was charged;
    ``prompted`` is how far the PROMPT reached, and is the only offset the
    abandon path may write to the marker, since the tail past it was never sent.
    Retry accounting stamps both: ``total`` tells new content from the same, and
    ``prompted`` says whether that comparison means anything (a prefix does not
    change when messages are appended; see
    :meth:`ConversationLog._attempts_describe_current_span`).

    ``sub_offset`` and ``sub_prompted`` extend the identity inside one sliced
    over-budget head (see :func:`_slice_head_for_budget`). A failed slice is
    charged against ``(offset, sub_offset)``, so the cap abandons at most one
    slice, and growth behind the head cannot release it. Both are ``0`` for the
    ordinary message-aligned case.
    """

    total: int
    generation: int
    offset: int
    prompted: int
    sub_offset: int = 0
    sub_prompted: int = 0


class _ConsolidationNotDispatched(Exception):
    """A consolidation turn that spent nothing.

    Its prompt never reached the provider, or the provider sent back no text and billed
    nothing.
    """


class _ConsolidationReplyUnusable(Exception):
    """The provider answered a consolidation turn without a usable JSON object.

    The message is the reason the caller charges the attempt with.
    """


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

    Image references are replaced with a content-free marker: the prompt builder
    inlines every readable image path as a full base64 block, which once grew a
    span to 67 MB of images around 600 KB of text. Extraction reads only text.

    Set *prestripped* when the content is a SLICE of a head already stripped
    whole (:func:`_consolidation_head_content`). Stripping is not slice-local: a
    slice opening inside a code fence would re-strip the fenced block as prose
    and corrupt a preserved path. A whole message leaves it False.
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

    Message-aligned so the marker advances by whole messages and the next pass
    starts at a real boundary. The ``"\\n"`` separator is charged too.
    Display-only rows (``DISPLAY_ONLY_ROLES``) are never rendered (see
    :func:`_prompt_rows`), so they cost nothing and ride inside the prefix.

    A first message that alone exceeds the budget is returned anyway: its size is
    permanent, so refusing it would stall the session forever. An over-context
    error is an ordinary failed attempt, and the attempt cap abandons that one
    message so the tail behind it consolidates.
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

    Slicing works on THIS, not the raw content. ``strip_image_refs`` masks fenced
    code spans, and a raw cut inside a fence flips the parity a slice presents, so
    a slice-local strip would mask or inline the wrong references. Stripping the
    whole head classifies every reference against its real fence structure. The
    slices are then rendered with ``prestripped=True``, so a slice's raw width
    also predicts its rendered width.
    """
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    return strip_image_refs(content)


def _slice_head_for_budget(message: dict, sub_offset: int) -> tuple[dict, int, int, bool]:
    """Slice *message* from *sub_offset* to one budget-sized prompt-safe chunk.

    Returns ``(sliced_message, start, new_sub_offset, is_last_slice)``:

    * ``sliced_message``: a shallow copy whose ``content`` is
      ``content[start:new_sub_offset]`` of the head's STRIPPED content (see
      :func:`_consolidation_head_content`), sized so the ``_fmt_message``
      envelope also fits the budget.
    * ``start``: the clamped start (``0`` for an out-of-range offset). The caller
      stamps retry accounting with this, so the identity matches what was sent.
    * ``new_sub_offset``: the offset reached in the stripped content, made durable
      so the next pass resumes there. Stripping is deterministic, so the offset
      names the same position on every pass.
    * ``is_last_slice``: the slice reaches the end, so the caller advances the
      message marker instead of the sub-offset.
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
    # The content is already stripped and renders with ``prestripped=True``, so
    # measure with the same flag. The loop is a cheap check should rendering ever
    # add width: each step scales the span by ``budget / rendered`` and is forced
    # below the current length (``_slice_boundary`` could re-find the same
    # boundary), falling back to a bare character cut, so ``end`` always drops
    # and the loop converges.
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

    The ceiling (see :data:`_CONSOLIDATION_MAX_HEAD_SLICES`) is a CHARACTER bound
    on the stripped content, read off the durable sub-offset alone, so it needs no
    extra state. It fires only once the sub-offset reached the bound AND more
    than one slice remains: a remainder that fits one more slice is always
    prompted, never dropped.

    A slice cuts on the last line/word boundary within the span, so on content
    with few boundaries (minified JSON) it advances well under a span. The call
    count before the retire is therefore between ``N`` and about ``2 * N``,
    still a small constant. The retired remainder is always logged and noticed.
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
