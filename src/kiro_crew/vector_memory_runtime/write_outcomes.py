"""What a store write reports: the semantic reject code and the lesson and episode outcomes.

``SemanticRejectCode`` names why validation refused a semantic value, ``LessonWriteOutcome``
and ``LessonWriteResult`` say what a ``write_lesson`` call did, and ``EpisodicWriteOutcome``
says what an episodic write did. :mod:`kiro_crew.vector_memory` re-exports every name here,
and that is the import path callers use. Types only: nothing here touches a store.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SemanticRejectCode(str, Enum):
    KEY_FORMAT = "key_format"
    ALLOWLIST = "allowlist_reject"
    RESERVED_PREFIX = "reserved_prefix"
    CONFIDENCE = "low_confidence"
    VALUE_SIZE = "value_size"
    VALUE_EMPTY = "value_empty"
    VALUE_ENCODING = "value_encoding"
    INJECTION = "injection_blocked"
    CONFLICT = "conflict_skip"


class LessonWriteOutcome(str, Enum):
    """What a lesson write actually DID, for callers that must tell the cases apart.

    A bare ``bool`` cannot: ``False`` covers several unrelated things --
    validation refused the value, a dedup rule claimed the write,
    the submit was a genuine no-op, or a bare re-submit deliberately kept the stored
    NOT-clause. The first two mean "your lesson did not land"; the last two mean
    "your lesson is fine, there was nothing to do". A caller that cannot tell them
    apart has to guess, and a caller reading every ``False`` as "the vector store
    did not take it" writes a second record into ``lessons.jsonl``.

    The vocabulary matches :meth:`kiro_crew.learn.LessonStore.save_or_enrich`, which
    returns ``inserted``/``enriched``/``unchanged``, so the two stores
    describe the same events with the same words.
    """

    INSERTED = "inserted"
    ENRICHED = "enriched"
    UNCHANGED = "unchanged"
    DEDUPED = "deduped"
    REFUSED = "refused"


class EpisodicWriteOutcome(str, Enum):
    """What an episodic write DID, for callers that must tell refusals apart.

    ``write_episodic`` returns ``False`` for both refusals. A caller that
    records refused rows as handled (the ops-mission-control ledger cursor) must
    retry an ``AT_CAPACITY`` refusal once space frees, but never a final one, so
    it needs the cause the store decided, not a guess made afterwards.
    """

    WRITTEN = "written"
    #: Treated as final: an active row already has this text (prefix/exact dedup)
    #: or a similar vector (similarity conflict), or the text itself is
    #: unacceptable (length bounds, injection screen). Only the text refusals are
    #: final in fact; a dedup or similarity refusal can change once the colliding
    #: row is tombstoned, and a caller that cursors ``REFUSED`` (the ledger
    #: indexer) accepts losing that later retry.
    REFUSED = "refused"
    #: A merge-only (``preserve_existing``) write found a full V1 store.
    AT_CAPACITY = "at_capacity"


# The two outcomes that changed the store. UNCHANGED is deliberately NOT here: the
# lesson IS stored as submitted, but nothing was written, so a caller asking "did I
# need to do something" gets no, while a caller asking "is my lesson stored" reads
# ``stored`` below.
_LESSON_WROTE_OUTCOMES = frozenset({LessonWriteOutcome.INSERTED, LessonWriteOutcome.ENRICHED})


@dataclass(frozen=True)
class LessonWriteResult:
    """A lesson write's outcome plus the short reason code behind it.

    ``reason`` names WHICH rule produced the outcome -- a
    :class:`SemanticRejectCode` value for ``REFUSED``, the dedup rule's name for
    ``DEDUPED``, and ``kept_stored_clause`` for the one ``UNCHANGED`` case that is
    not a byte-identical re-submit. It is ``None`` when the outcome says everything
    there is to say. Surfaces that report back to a human or a model (the CLI, the
    ``/api/lessons`` response, the ``learn_add`` tool result) need the reason; the
    ones that only branch on success do not.

    ``superseded`` names the stored rules THIS CALL DELETED. Every field above
    describes what happened to the SUBMITTED lesson, and that was the whole
    vocabulary -- so a write that tombstoned somebody else's stored rule reported
    a plain ``inserted`` with ``reason=None``, and the caller was told its lesson
    was saved with nothing naming what the save cost. Supersede-on-dedup is
    deliberate (see :meth:`VectorMemoryStore.write_lesson`, and the docstring's
    "longer wins" / "newer replaces older"), and this field does not change it:
    it only reports which rows that rule deleted. It is
    empty on every path that deleted nothing, so a surface can render it with a
    bare ``if`` and say nothing when there is nothing to say.

    **Truthiness is deliberate, and it is why this type is the whole return value
    rather than something shipped beside a ``bool``.** Three callers plus ~55
    assertions read ``write_lesson``'s answer with a
    bare ``if``/``assert``. Returning any ordinary object would make every one of
    them unconditionally true -- silently, since a bare ``if`` on a truthy value is not
    a type error and mypy cannot flag it. :meth:`__bool__` closes exactly that hole:
    ``bool(result)`` is ``wrote``, byte-for-byte the
    predicate those callers are written against. So there is one method, one
    name, and nothing to migrate to -- a caller that needs the detail reads
    :attr:`outcome`, and a caller that only needs "did this write something" keeps
    using the truth value.
    """

    outcome: LessonWriteOutcome
    reason: str | None = None
    #: Rules this call tombstoned. A tuple, not a list, because the dataclass is
    #: frozen and a mutable default would let a caller edit a write's own record of
    #: what it destroyed. Defaults to empty so the ~60 existing construction sites
    #: -- ``LessonWriteResult(OUTCOME)`` and ``LessonWriteResult(OUTCOME, reason)``
    #: -- are unchanged, and any surface that ignores the field keeps its behaviour.
    superseded: tuple[str, ...] = ()
    #: The tier the STORED row actually carries after this write, which is not the
    #: submitted one on an enrichment: the tier is write-once, so a clause-only
    #: re-submit omitting ``applies`` keeps the persisted value while the submitted
    #: value is ``None``. A caller that guards a DELETION on the tier has to read
    #: this, because the submitted value answers a different question -- gating on
    #: it let an ordinary enrichment of a stored finding skip the guard entirely and
    #: retire a standing rule. ``None`` means the row is unstated, which every read
    #: path serves AS a standing rule. Defaults to ``None`` so the ~60 existing
    #: construction sites are unchanged.
    applies: str | None = None
    #: On ``DEDUPED``, the stored lesson that claimed this write, rendered the way
    #: every human-facing surface renders it (rule plus NOT-clause). The outcome
    #: alone says "an existing lesson covers it" without naming which, so a caller
    #: replacing a stale lesson had to page ``learn_list`` to find the row it lost
    #: to. ``None`` on every other outcome, and when the row has no display text.
    winner: str | None = None
    #: The winning row's stored ``repo_scope``, or ``None`` when it is global.
    #: Read from the row, not the submission. The route turns it into the delete
    #: selector for that row before it leaves the process.
    winner_repo_scope: str | None = None

    def __bool__(self) -> bool:
        """``wrote`` -- the exact predicate the old ``bool`` return answered.

        Preserving it is the whole point: see the class docstring. Do NOT redefine
        this as ``stored``, which would quietly turn a no-op re-submit into a write
        for every caller that branches on the truth value.

        Removing it does NOT redden the wide assertion surface, which is exactly why
        it is easy to lose: without it a result object is truthy by default, so every
        positive ``assert store.write_lesson(...)`` keeps passing while asserting
        nothing at all. Only the negative assertions and the dedicated tests in
        ``TestWriteLessonTruthValueIsTheOldBool`` catch its absence -- verified by
        deleting this method, which left 160 tests green and reddened 5.
        """
        return self.wrote

    @property
    def wrote(self) -> bool:
        """The store changed -- a row was inserted, or an existing row enriched."""
        return self.outcome in _LESSON_WROTE_OUTCOMES

    @property
    def stored(self) -> bool:
        """The lesson is in the store as submitted -- written now, or already there.

        Distinct from :attr:`wrote` (and from the truth value): a no-op re-submit did
        not write anything, yet the caller's lesson is stored, so telling them it
        failed would be false.
        """
        return self.outcome is LessonWriteOutcome.UNCHANGED or self.wrote


# Logs, API errors and pickles name these classes as ``kiro_crew.vector_memory.<name>``,
# the import path every caller uses, so each keeps that module as its own.
SemanticRejectCode.__module__ = "kiro_crew.vector_memory"
LessonWriteOutcome.__module__ = "kiro_crew.vector_memory"
EpisodicWriteOutcome.__module__ = "kiro_crew.vector_memory"
LessonWriteResult.__module__ = "kiro_crew.vector_memory"
