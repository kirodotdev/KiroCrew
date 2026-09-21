"""Shared validation for durable lesson text."""

from __future__ import annotations

import re
from collections.abc import Sequence

from kiro_crew.model_registry import MODEL_ID_LITERAL_PATTERN

_MODEL_ID_TOKEN_RE = rf"{MODEL_ID_LITERAL_PATTERN}" r"(?:\w|[.-](?=\w))*" r"(?!\w|[.-](?=\w))"
_MODEL_CLAUSE_END_RE = (
    r"(?=$|\n|[.,;:!?)\]]|\s+(?:"
    r"for|when|whenever|if|unless|in|on|at|to|over|instead|rather|and|or|but|as|"
    r"with|without|because|since|by|until|while|so|only|from|during|before|after|"
    r"except|via|per|not"
    r")\b)"
)
_RUNNING_AS_MODEL_IDENTITY_RE = (
    r"(?:(?:the\s+)?(?:current|active|selected)\s+model\b"
    r"|(?:the\s+)?(?:current|active|selected)\s+backend\s+model\b"
    rf"|(?:the\s+)?(?:current|active|selected)\s+backend\b{_MODEL_CLAUSE_END_RE}"
    r"|(?:the\s+)?(?:model\s+backend|backend\s+model)\b"
    r"|(?:the\s+)?model\b"
    rf"|(?:(?:the\s+)?(?:model|backend)\s+)?{_MODEL_ID_TOKEN_RE})"
)
_VOLATILE_MODEL_FACT_RE = re.compile(
    r"\b(?:current|active)\s+model(?:\s+identity)?\s*"
    r"(?:is\b|was\b|changes?\b|shown\b|[:=])"
    rf"|\b(?:selected|session)\s+model(?:\s+identity)?\s*"
    rf"(?:is\b|was\b|[:=])\s*{MODEL_ID_LITERAL_PATTERN}"
    r"|\b(?:selected|session)\s+model\s+identity\s*(?:changes?\b|shown\b)"
    rf"|\brunning\s+as\s+{_RUNNING_AS_MODEL_IDENTITY_RE}",
    re.IGNORECASE,
)
_MODEL_SELECTION_VERB_RE = (
    r"(?:(?:use|choose|select|prefer)\b(?!\s+of\b)" r"|switch\s+to\b|stick\s+with\b)"
)
_MODEL_OBJECT_RE = (
    r"(?:(?:the|a|an|this|that|our|your)\s+)?"
    r"(?:(?:default|primary|fallback|cheaper|newer|latest|same)\s+)?"
    r"(?:(?:model|backend|provider)\s+)?"
    rf"{_MODEL_ID_TOKEN_RE}"
    r"(?:\s+(?:model|backend|provider)\b)?"
    rf"{_MODEL_CLAUSE_END_RE}"
)
_BEHAVIORAL_MODEL_PIN_RE = re.compile(
    rf"(?:\b(?:always|never|should|must)\s+{_MODEL_SELECTION_VERB_RE}"
    rf"|(?:^|[.!?]\s+|\n\s*)"
    rf"\s*(?:(?:for|when)\b[^,\n]{{0,120}},\s*)?"
    rf"(?:(?:please|kindly)\s+)?(?:do\s+)?"
    rf"{_MODEL_SELECTION_VERB_RE})"
    rf"\s+{_MODEL_OBJECT_RE}",
    re.IGNORECASE,
)


def contains_volatile_lesson_fact(
    rule: object,
    negative: object = None,
) -> bool:
    """Whether either persisted field records runtime identity or a model pin.

    Runtime model-identity assertions and recognized imperatives whose selected
    concrete model-ID object ends its clause are volatile in every category. A
    version literal or an ID that qualifies a following tooling noun is durable,
    including in a NOT-clause.
    """
    rule_text = rule if isinstance(rule, str) else ""
    negative_text = negative if isinstance(negative, str) else ""
    return any(
        _VOLATILE_MODEL_FACT_RE.search(text) or _BEHAVIORAL_MODEL_PIN_RE.search(text)
        for text in (rule_text, negative_text)
        if text
    )


#: A lesson whose author stated it is a standing rule: it applies to the session
#: regardless of what the user's current message is about.
LESSON_APPLIES_ALWAYS = "always"
#: A lesson whose author stated it is a past finding: worth having when the task
#: touches it, not something to carry into every unrelated conversation.
LESSON_APPLIES_ON_TOPIC = "on_topic"
#: A row whose author never stated which of the two it is. Every lesson written
#: before this field existed is in this class, and so is a row whose stored value
#: is not one of the two literals above. Readers give it the DIRECTIVE tier's
#: treatment, because demoting a real standing rule is the costlier mistake.
LESSON_APPLIES_UNSTATED = "unstated"

LESSON_APPLIES_VALUES = (LESSON_APPLIES_ALWAYS, LESSON_APPLIES_ON_TOPIC)

#: Why a JSONL lesson write was refused when the store is full rather than
#: because of the value's content. The JSONL store answers both cases with the
#: bare ``refused`` outcome it shares with :class:`LessonWriteOutcome`, so a route
#: that reported every refusal as ``volatile_session_fact`` told a user who hit
#: the row cap to reword a rule that was never the problem. Only the JSONL store
#: raises this: the vector store has no row cap and names its own reasons.
LESSON_REFUSED_AT_CAPACITY = "store_at_capacity"


def normalize_lesson_applies(tier: object) -> str | None:
    """Validate a caller-supplied tier for a write path.

    Returns the canonical literal, or ``None`` when the caller named no tier.

    A value that is present but not one of the two literals RAISES rather than
    normalizing to unclassified. The asymmetry is deliberate: a write surface
    that misspells the tier has a bug and must hear about it, while a caller that
    omits it is making the ordinary, supported choice to leave the row
    unclassified. Silently accepting ``"Directive "`` as unclassified would put a
    standing rule into the relevance-ranked tier with nothing in the logs.

    The tier is AUTHORED, never derived. No property of a stored row separates "a
    rule the user wants enforced in every session" from "something we worked out
    once": ``source`` does not -- the consolidator can extract a real safety
    correction, and a human can type a note about last week's outage;
    ``category`` does not -- its values describe subject matter; keyword shape
    does not -- "always" appears in both classes and reliably in neither. A
    reader that guessed from any of those would re-decide the user's intent on
    every turn, and it would fail silently in the direction that costs most.
    """
    if tier is None:
        return None
    if isinstance(tier, str):
        normalized = tier.strip().lower()
        if not normalized:
            return None
        if normalized in LESSON_APPLIES_VALUES:
            return normalized
    raise ValueError(f"lesson tier must be one of {LESSON_APPLIES_VALUES}, got {tier!r}")


def authored_lesson_applies(tier: object) -> str | None:
    """The tier a stored row's author actually stated, or ``None``.

    The READ-side counterpart of ``normalize_lesson_applies``, and deliberately
    the opposite on a bad value: a listing surface serves rows the store already
    accepted, including ones an older write path or a hand-edited file left
    malformed, so raising there would fail a whole page over one row. Anything
    that is not one of the two literals answers ``None``, the same class every
    row written before the field existed is in.

    ``None`` is not a third tier. It means no author answered, which readers
    treat as the standing-rule tier -- so a surface reporting this must render
    ``None`` as absence rather than as a value, or it names a distinction the
    injection path does not make.
    """
    if not isinstance(tier, str):
        return None
    normalized = tier.strip().lower()
    return normalized if normalized in LESSON_APPLIES_VALUES else None


def tighter_lesson_budget(*budgets: int) -> int:
    """The smallest positive budget among *budgets*; ``0`` when all are unbounded.

    ``0`` means "no limit from this source", which is what every caller predating
    the startup budgets passes, so it must not win a ``min()``. Returning ``0``
    only when EVERY input is unbounded is what keeps those callers byte-identical.
    """
    positive = [budget for budget in budgets if budget > 0]
    return min(positive) if positive else 0


_RELEVANCE_MIN_TOKEN = 3


def _relevance_tokens(text: str) -> frozenset[str]:
    """Lowercase word tokens of length >= 3, for a cheap lexical overlap score."""
    token, tokens = [], set()
    for char in text.lower():
        if char.isalnum():
            token.append(char)
        else:
            if len(token) >= _RELEVANCE_MIN_TOKEN:
                tokens.add("".join(token))
            token = []
    if len(token) >= _RELEVANCE_MIN_TOKEN:
        tokens.add("".join(token))
    return frozenset(tokens)


def order_by_request_relevance(
    entries: list[tuple[object, str]], query_text: str
) -> list[tuple[object, str]]:
    """Stable-sort one lesson tier by lexical overlap with *query_text*, best first.

    Applied to EVERY tier, findings and rules alike, and applied per tier so the
    tiers themselves keep their precedence. Two reasons it is not confined to
    findings. Ordering decides only which rows survive a truncation that is going
    to happen anyway -- it is not admission, so no row is dropped for being
    irrelevant that a budget would have kept. And the vector store already ranks
    its whole eligible set this way (``_rank_lessons``, pre-existing on the
    background path), so confining it to findings here left the two stores
    disagreeing: a 100-row legacy store lost an exact-topic match on the JSONL
    path while the vector path kept it, because every pre-field row lands in the
    rule tier and that tier stayed newest-first.

    An empty query, or no overlap anywhere, leaves the caller's order untouched.

    The residual is real and is the reason the omission notice exists: inside an
    OVERFLOWING rule tier, a standing rule unrelated to this message sorts last
    and can be the one omitted. The notice names it and points at ``learn_list``;
    ordering cannot both favour the current task and ignore it.

    Lexical only, and deliberately so: this runs while a session's first prompt is
    being assembled, where an embedding call is the cost the budget exists to avoid.
    Stemming is also skipped -- the vector tier owns hybrid scoring; here the job is
    to stop a merely OLD row from losing to newer irrelevant ones.
    """
    if not query_text.strip() or not entries:
        return entries
    wanted = _relevance_tokens(query_text)
    if not wanted:
        return entries
    # ``sorted`` is stable, so equal-scoring rows keep the caller's newest-first
    # order and a zero-overlap store renders byte-identically to an unordered one.
    return sorted(entries, key=lambda e: -len(wanted & _relevance_tokens(e[1])))


def any_request_overlap(entries: Sequence[tuple[object, str]], query_text: str) -> bool:
    """Whether ANY entry shares a significant word with *query_text*.

    The admission test for the findings tier, and deliberately the SAME tokenizer
    ``order_by_request_relevance`` sorts with: ordering and admission must read one
    signal, or a tier is suppressed for a match its own ranking found. The vector
    store therefore asks its OWN ranker instead of calling this -- it stems, so it
    matches strictly more, and borrowing this unstemmed answer would discard its
    stem-only hits.

    A query with no significant words answers False: a bare greeting names no
    topic, so no past finding is on it.
    """
    wanted = _relevance_tokens(query_text)
    if not wanted or not entries:
        return False
    return any(wanted & _relevance_tokens(text) for _, text in entries)


def render_withheld_tier(total: int, *, header: str, footer: str, notice: str) -> str:
    """Render a tier's FRAME with no entries, for a tier withheld on relevance.

    Not the same event as an overflow, so it does not share that wording: nothing
    was too big to fit, the request simply named nothing these rows are about.
    Rendering the frame anyway is the same reason an overflow is announced -- a
    silently absent block is indistinguishable from "this user has no findings",
    and that is the reading that stops anyone from going to look. *notice* takes
    ``total``.
    """
    if total <= 0:
        return ""
    return header + "\n" + footer + "\n" + notice.format(total=total) + "\n\n"


def render_lesson_tier(
    entries: Sequence[tuple[object, str]],
    budget: int,
    *,
    header: str,
    footer: str,
    omission: str,
) -> tuple[str, int]:
    """Render one lesson tier within *budget* characters.

    Returns ``(block, omitted_count)``. *entries* are ``(row, text)`` pairs in the
    order they should be kept; *omission* is a format string taking ``count``,
    ``total`` and ``limit``.

    ``budget`` of ``0`` is unbounded. An empty tier renders ``""`` rather than an
    empty labelled block, so a user with no lessons of a kind sees no header for
    it.

    **The omission notice is never dropped to fit.** When the budget is too small
    for even one lesson, the block is the header, the notice and the footer -- a
    labelled block saying nothing arrived. That is the one outcome this function
    must always be able to produce: a budget that silently rendered an empty
    block, or nothing at all, would look exactly like "this user has no rules",
    which is the failure the budget itself introduces and must therefore report.
    """
    if not entries:
        return "", 0
    total = len(entries)

    def render(rows: Sequence[tuple[object, str]], omitted: int) -> str:
        # Byte shape is the SHIPPED one and is load-bearing: the block ends with a
        # single newline after its footer, and an omission notice follows the
        # footer rather than sitting inside the block, then adds the blank line
        # that separates this block from whatever comes next. Callers depend on
        # both -- ``test_extraction_provenance_never_makes_a_rule_optional``
        # asserts the exact terminator, and ``test_session_context_char_extents``
        # pins the block's character extent to the byte.
        block = header + "\n" + "\n".join(f"- {text}" for _, text in rows) + "\n" + footer + "\n"
        if omitted:
            block += omission.format(count=omitted, total=total, limit=budget) + "\n\n"
        return block

    full = render(entries, 0)
    if not budget or len(full) <= budget:
        return full, 0
    # Reserve room for the notice at its widest (every entry omitted), so one
    # pass over the rows decides which prefix fits.
    frame = len(render([], total))
    room = budget - frame
    kept: list[tuple[object, str]] = []
    for entry in entries:
        line = len(entry[1]) + 3  # "- " prefix and newline
        if line > room:
            # SKIP this one and keep going, rather than ending the selection. The
            # kept set is then a superset of what stopping here would give -- an
            # entry that does not fit is dropped either way, so continuing can only
            # add later ones that DO fit, and it never displaces an earlier row
            # because room is only ever spent on rows already accepted. Stopping
            # wasted room a shorter, more relevant row could have used: five
            # in-budget rows rendered 1,408 of 1,650 characters while omitting a
            # later 76-character line, with 242 characters free.
            continue
        room -= line
        kept.append(entry)
    return render(kept, total - len(kept)), total - len(kept)
