"""The memory a consolidation pass reads into its prompt and writes back.

A pass shows the model a bounded ``## Current Semantic Memory`` table
(:func:`_bounded_semantic_table`) and withholds deletes of keys the table did not show
(:func:`_withhold_unseen_deletes`). A ``lesson.*`` key the model put in the ``semantic``
array is moved to the ``lessons`` array first (:func:`_reroute_lesson_semantic_keys`), so
it gets the lessons path's dedup and checks instead of a plain semantic write. It then
writes what the model extracted:
lessons through :func:`_save_lessons`, which drops a lesson with a malformed scope and
forwards its authored tier, and semantic and episodic rows through
:func:`_write_structured_memory`, which refuses to delete a standing lesson
(:func:`_lesson_delete_decision`) and whose inline embeddings :class:`_EmbedBudget` bounds
per pass. :func:`_is_plausible_memory_file` refuses placeholder text before it replaces a V1
memory file, and :func:`_session_facets` gives every row the session's carve axes. Each
writer publishes inside ``HistoryConsolidator._publication_hold_checked``, which reads
``memory.persistence_enabled`` and ``memory.writes_enabled`` through
``_persistence_disabled`` before the run's first commit: a switch turned off before that
commit refuses the run before it writes anything, and a run that has committed once
finishes its remaining writes.

Each function here whose first parameter is ``self`` is a ``HistoryConsolidator`` method:
the class binds it under the same name (``history_consolidation.py``), so it reads the
consolidator's own fields and calls its siblings through ``self``.

Instance fields read: ``_lesson_store``, ``_vector_store``.

Instance fields written: none.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports only ``history_consolidation_span``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.history_consolidation_span import _RunCommitState
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ON_TOPIC,
    authored_lesson_applies,
    extracted_lesson_applies,
)
from kiro_crew.project_scope import scope_is_admissible
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory_schema import MemoryFacets
    from kiro_crew.vector_memory import VectorMemoryStore


#: Wall-clock ceiling on the memory writes of ONE consolidation pass that embed
#: inline. A pass writes up to ``_MAX_SEMANTIC_PER_CONSOLIDATION`` +
#: ``_MAX_EPISODIC_PER_CONSOLIDATION`` rows and each one embeds its text through a
#: blocking inference call, so a degraded embedder makes the pass cost N times one
#: call's latency — all of it on an embed-pool worker, which is shared with every
#: other embed consumer. Past the ceiling the rest of the pass is written with its
#: embedding deferred, so it stops queueing inference it has measured to be slow.
#: Deferred rows are filled in by the standing repair sweep
#: (``backfill_missing_embeddings``), which is what makes deferral lossless.
_EMBED_BUDGET_SECS_PER_PASS = 60.0


#: The one line under a bounded ``## Current Semantic Memory`` table. The prompt
#: tells the model to update or delete the keys it can see, so a table that lost
#: rows silently would read as "those facts do not exist" and invite a deletion
#: of nothing or a near-duplicate of a dropped key. Same vocabulary as the chat
#: path's startup omission notice, so a reader learns one shape.
_SEMANTIC_OMISSION_NOTICE = (
    "[Context budget: omitted {count} of {total} semantic rows above the "
    "{limit}-character consolidation budget; the least recently updated rows were "
    "left out. The table above is PARTIAL: a key you do not see may still exist, "
    "so update or delete only keys listed above and treat a missing key as "
    "unknown, not absent.]"
)


class _BoundedTable(NamedTuple):
    """A rendered semantic table, how many rows it left out, and the keys it shows."""

    text: str
    omitted: int
    visible_keys: frozenset[str]


def _recency(updated_at: object) -> tuple[int, float]:
    """Rank an ``updated_at`` for the newest-first cut.

    Stamps are parsed, not compared as text: the store writes ISO 8601 with an
    offset, while imported or older rows can carry a naive stamp, a space
    separator or an epoch, and as text those shapes rank by their separator
    before their instant. A stamp that parses ranks by instant; one that does
    not ranks after every one that does.
    """
    if isinstance(updated_at, (int, float)) and math.isfinite(updated_at):
        return 1, float(updated_at)
    if not isinstance(updated_at, str):
        return 0, 0.0
    text = updated_at.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return 1, float(text)
        except ValueError:
            return 0, 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return 1, parsed.timestamp()


def _bounded_semantic_table(rows: list[dict], entries: list[dict], cap: int) -> _BoundedTable:
    """Render ``entries`` as the prompt's semantic table within ``cap`` characters.

    ``rows`` are the store rows (in the store's key order) that ``entries`` were
    rendered from, one to one. A table that fits renders whole and byte-identical
    to the uncapped form. Over the cap, the most recently updated rows are kept
    -- the order the chat path's ``semantic_cap`` reads without a query -- and
    rendered in the same key order, so the block keeps its shape and only loses
    its oldest rows. Returns the block, the number of rows left out, and the keys
    the block shows, which is what the writers may update or delete.

    The fit is found by bisection on the row count with the real renderer rather
    than by an estimate per row, so the bound is exact for whatever ``indent``
    and escaping produce, at the cost of O(log n) serialisations. A row is kept
    whole or not at all: when even the newest row alone is over the cap the
    table is ``[]`` and every row counts as omitted, because a value cut short
    would read as the fact itself.
    """
    whole = json.dumps(entries, indent=1) if entries else "[]"
    if len(whole) <= cap:
        return _BoundedTable(whole, 0, frozenset(str(r.get("key", "")) for r in rows))
    # Newest first; key as the tiebreak so rows written in the same second keep
    # one order across runs (a stable sort on top of the key order given).
    by_recency = sorted(range(len(rows)), key=lambda i: str(rows[i].get("key", "")))
    by_recency.sort(key=lambda i: _recency(rows[i].get("updated_at")), reverse=True)

    def _render(count: int) -> str:
        kept = sorted(by_recency[:count])
        return json.dumps([entries[i] for i in kept], indent=1) if kept else "[]"

    fits, overflows = 0, len(rows)
    rendered = "[]"
    while overflows - fits > 1:
        middle = (fits + overflows) // 2
        candidate = _render(middle)
        if len(candidate) <= cap:
            fits, rendered = middle, candidate
        else:
            overflows = middle
    visible = frozenset(str(rows[i].get("key", "")) for i in by_recency[:fits])
    return _BoundedTable(rendered, len(rows) - fits, visible)


def _withhold_unseen_deletes(
    result: dict, visible_keys: frozenset[str] | None, logger: logging.Logger
) -> tuple[dict, int]:
    """Drop every ``delete`` naming a key the bounded semantic table never showed.

    Keys are guessable (``user.work_email``, ``project.*``), so a model reading a
    partial table can name a row it never read, and the omission notice alone
    does not stop it. A delete has nothing behind it to arbitrate, so it is
    withheld here, on the model's answer, before either write path reads it --
    ``_write_structured_memory`` and the member store's ``apply_consolidation``
    share this one fence so they cannot drift. An update is NOT withheld: the
    new value's authority is the transcript, not the rendered table, and the
    store arbitrates the old one (``_write_semantic`` skips a consolidation
    overwrite of a user-stated row; the member store turns a conflicting update
    into an owner proposal). Refusing it would also refuse a user's genuine
    correction for a key the cap cut from the table, and the span is marked
    consolidated either way, so that correction would be gone for good. ``None``
    means no bounded table was rendered and nothing is withheld.

    Returns the result (a shallow copy when anything was dropped) and how many
    distinct keys were withheld; each is logged once, however often it is named.
    """
    items = result.get("semantic")
    if visible_keys is None or not isinstance(items, list):
        return result, 0
    kept: list = []
    withheld: set[str] = set()
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("delete")
            and isinstance(item.get("key"), str)
            and item["key"] not in visible_keys
        ):
            if item["key"] not in withheld:
                withheld.add(item["key"])
                logger.warning(
                    "Semantic consolidation refused delete of %r: key not in the rendered table"
                    " (omitted above the prompt budget), so the model never saw its value",
                    item["key"],
                )
            continue
        kept.append(item)
    if not withheld:
        return result, 0
    return {**result, "semantic": kept}, len(withheld)


#: The keys a rerouted ``lesson.*`` semantic item may carry into a lesson, mapped
#: from the semantic shape to the lesson shape. A lesson's rule is the semantic
#: ``value``; ``category``, ``negative``, ``applies`` and ``repo_scope`` are
#: already the lesson field names and copy across unchanged.
_LESSON_PASSTHROUGH_FIELDS = ("category", "negative", "applies", "repo_scope")


def _reroute_lesson_semantic_keys(result: dict, logger: logging.Logger) -> dict:
    """Move ``semantic`` items whose key is ``lesson.*`` into the ``lessons`` array.

    The prompt asks the model for ``pref.*`` / ``project.*`` / ``user.*`` keys in
    ``semantic`` and for lessons in ``lessons``, but that split is not enforced.
    A ``lesson.*`` key left in ``semantic`` is written straight through
    ``set_semantic`` (V1) or ``_write_semantic`` (V2), so it skips the exact-rule
    dedup, the volatile-fact screen and the extraction-enabled gate that
    ``write_lesson`` / the member store's lesson branch apply -- which is how the
    same rule ends up stored several times under different model-chosen keys.
    Rerouting it to ``lessons`` before any write hands it the same
    checks as the model's other lessons, on BOTH paths, because this runs once on
    the shared ``result`` before it branches to ``_write_structured_memory`` /
    ``_save_lessons`` (V1) and ``apply_consolidation`` (V2).

    Only a CREATE is rerouted. A ``delete`` of a ``lesson.*`` key stays in
    ``semantic``: the delete site already tier-protects standing lessons
    (``_lesson_delete_decision``), and the lessons array has no delete verb.
    The lesson's rule is the item's ``value``, so an item whose value is not a
    non-empty string is not a lesson and is DROPPED (restating the issue's note
    that a value like ``true`` is not a lesson) rather than left to be written as
    a plain semantic row -- which would reintroduce exactly the un-deduped
    ``lesson.*`` semantic row this reroute exists to remove.

    Returns the result -- a shallow copy with fresh ``semantic`` / ``lessons``
    lists when anything moved, otherwise the input unchanged. Reroutes and drops
    are logged once for the pass; no caller needs the counts.
    """
    items = result.get("semantic")
    if not isinstance(items, list):
        return result
    kept_semantic: list = []
    moved_lessons: list = []
    dropped = 0
    for item in items:
        key = item.get("key") if isinstance(item, dict) else None
        if not (isinstance(key, str) and key.startswith("lesson.")) or (
            isinstance(item, dict) and item.get("delete")
        ):
            kept_semantic.append(item)
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            # A lesson's rule is text. A non-string (``true``, a number, an
            # object) or an empty value is not a lesson; drop it rather than
            # let it fall through to a plain semantic write.
            dropped += 1
            continue
        lesson: dict = {"rule": value}
        for field in _LESSON_PASSTHROUGH_FIELDS:
            if field in item:
                lesson[field] = item[field]
        moved_lessons.append(lesson)
    if not moved_lessons and not dropped:
        return result
    existing = result.get("lessons")
    lessons = list(existing) if isinstance(existing, list) else []
    lessons.extend(moved_lessons)
    if moved_lessons:
        logger.info(
            "Consolidation rerouted %d lesson.* semantic item(s) to the lessons path",
            len(moved_lessons),
        )
    if dropped:
        logger.warning(
            "Consolidation dropped %d lesson.* semantic item(s) with a non-text value",
            dropped,
        )
    return {**result, "semantic": kept_semantic, "lessons": lessons}


class _EmbedBudget:
    """One consolidation pass's embed-time ceiling, and the latch it arms.

    The ceiling is measured over the store WRITES, not over the embed calls
    themselves: the consolidation layer has no seam onto an individual embed, and
    write time is the quantity that actually has to be bounded. Inference is the
    only unbounded part of a write — the rest is local SQLite work — so a slow
    embedder is what normally spends this budget, though lock contention or a
    stalled disk can spend it too. The remedy for either is the same, and it is
    self-healing: the repair sweep embeds what this pass deferred.

    Once tripped the latch stays tripped for the rest of the pass — that is the
    whole point. Without it, an embedder that is slow for the first row is slow for
    every row, and the pass pays that latency once per item before finishing with
    exactly the same rows it would have written anyway (a failed embed already
    stores a NULL vector for the repair sweep to fill).
    """

    __slots__ = ("_budget", "_logger", "_spent", "tripped")

    def __init__(self, budget_secs: float, logger: logging.Logger) -> None:
        self._budget = budget_secs
        self._logger = logger
        self._spent = 0.0
        self.tripped = False

    @contextlib.contextmanager
    def measured(self):
        """Time one store write and charge it to the pass, arming the latch once."""
        started = _time.monotonic()
        try:
            yield
        finally:
            self._spent += _time.monotonic() - started
            if not self.tripped and self._spent >= self._budget:
                self.tripped = True
                # Once per pass, not once per row: a degraded embedder would
                # otherwise repeat this line for every remaining item.
                self._logger.warning(
                    "Consolidation spent %.1fs on memory writes (budget %.1fs); "
                    "embedding is deferred to the repair sweep for the rest of this pass",
                    self._spent,
                    self._budget,
                )


#: Default for the two write helpers' store arguments, meaning "argument not
#: supplied — use the global handle off ``self``". It cannot be ``None``, because
#: ``ContextBuilder.ensure_store`` may answer ``None`` for an unavailable legacy
#: V1 silo (private V2 raises), and that answer must skip the tier rather than
#: inherit the global store:
#: inheriting files one crew's rows into the operator's own memory, which is the
#: misfiling the resolved handles exist to prevent. Typed ``Any`` so each parameter
#: keeps its real annotation.


class _InheritGlobal:
    """Sentinel type for "argument omitted, inherit the consolidator's handle".

    A CLASS rather than a bare ``object()`` so the parameters it defaults can keep
    their real annotations. Typed ``Any``, the sentinel erased mypy's view of both
    store parameters — and ``_save_lessons`` is called positionally, so a swapped
    ``(vector_store, lesson_store)`` pair would have written one crew's lessons
    through the other's handle with nothing to catch it.
    """

    __slots__ = ()


_INHERIT_GLOBAL = _InheritGlobal()


class _LessonDeleteDecision(NamedTuple):
    """A consolidation lesson-delete decision plus the body it was read from.

    ``checked_value_json`` carries the exact ``value_json`` the tier was read
    from so an allowed delete can compare-and-delete against it; it is ``None``
    for a protected decision and for a non-lesson key.

    ``reason`` tells the three protected causes apart so they log and count
    separately, since they mean different things to an operator:

    * ``"allow"`` -- not protected; the delete proceeds.
    * ``"tier"`` -- a standing (``always``/unstated) or non-mapping lesson row:
      a real tier refusal.
    * ``"absent"`` -- no active row under a ``lesson.*`` key: nothing exists to
      protect, refused only to keep an unconditional delete out of the
      delete-plus-re-add window.
    * ``"unreadable"`` -- the row read raised: a store outage, not a tier
      decision, and worth a warning.
    """

    protected: bool
    checked_value_json: str | None
    reason: str


_PLACEHOLDER_BODIES = frozenset(
    {
        "unchanged",
        "no change",
        "no changes",
        "no change needed",
        "no changes needed",
        "no changes required",
        "no update",
        "no updates",
        "no update needed",
        "no updates needed",
        "nothing changed",
        "nothing to update",
        "nothing to change",
        "none",
        "n/a",
        "na",
        "empty",
        "same",
        "same as before",
        "as before",
        "see above",
        "content unchanged",
        "file unchanged",
    }
)


def _is_plausible_memory_file(content: str, header: str) -> bool:
    """Refuse placeholder text before it overwrites a complete memory file."""
    first_line, _, body = content.strip().partition("\n")
    if first_line.strip() != header:
        return False
    normalized = body.strip().lower().strip(" \t\"'`*_~.,!()[]")
    return normalized not in _PLACEHOLDER_BODIES


def _session_facets(meta: dict, key: str) -> "MemoryFacets":
    """The carve axes for everything this session's consolidation writes.

    The consolidator is the writer worth threading first: it is the one that
    produces most rows and it already holds every axis one line away. The others
    have no identity in scope and would have to invent one.

    ``crew`` comes from the session's ``agent`` metadata, which is the CREW alias
    (``cfg.agents`` key) rather than the kiro-cli agent template. Confusing the two
    is a named bug -- resolving a store from the template answers ``default`` for
    exactly the crew that configured otherwise -- so this reads ``meta["agent"]`` and
    never ``kiro_agent``. Note that is a DIFFERENT key from the one
    :func:`kiro_crew.context.store_of_session` reads (``meta["memory_store"]``): a crew
    alias and the store it binds to are separate facts. Each named member now owns a
    unique private store; recording the alias separately preserves its provenance.

    ``surface`` uses ``telemetry_channel_of``, whose output is a BOUNDED label and
    never the key itself, so the column cannot acquire one value per conversation.
    Not ``sel._infer_source``, which fails OPEN to ``"slack"`` for an unrecognised
    key and would file dashboard rows inside a slack carve.

    Never raises: a facet is an index projection, and no carve axis is worth
    failing a consolidation over.
    """
    from kiro_crew.memory_schema import MemoryFacets

    try:
        from kiro_crew.messaging.link import telemetry_channel_of

        surface = telemetry_channel_of(key)
    except Exception:
        surface = ""
    crew = meta.get("agent")
    return MemoryFacets(
        surface=surface,
        crew=crew if isinstance(crew, str) else "",
        session_key=key,
    )


def _gated_lesson_scope(self: HistoryConsolidator, item: dict) -> tuple[str | None, bool]:
    """The lesson's ``repo_scope`` to forward, plus whether to DROP the lesson.

    Returns ``(scope, False)`` for no scope (absent, ``None``, or a
    whitespace-only string -- the unchanged global path) or an admissible
    one, and ``(None, True)`` (with the reason logged) when a PRESENT scope
    is malformed. A malformed scope refuses the WHOLE lesson rather than
    stripping the scope: storing it globally would be the fail-open a
    scoped lesson must never take (``write_lesson`` draws the same line for
    inadmissible strings). The value is untrusted model output, so this
    seam also refuses the shapes the stores' own guards cannot see -- a
    non-string would slip past ``write_lesson``'s string-only admissibility
    check and be canonicalised to a GLOBAL write, and ``LessonStore.save``
    never checks admissibility at all.
    """
    raw = item.get("repo_scope")
    refusal: str | None = None
    if raw is None:
        return None, False
    elif not isinstance(raw, str):
        refusal = "scope_not_a_string"
    elif not raw.strip():
        return None, False
    elif not scope_is_admissible(raw):
        refusal = "scope_inadmissible"
    if refusal:
        # Only the closed-set reason code is interpolated, never the
        # untrusted value itself.
        self._logger.warning(
            "Dropping consolidation lesson with malformed repo_scope (%s)",
            refusal,
        )
        return None, True
    return raw, False


def _lesson_tier(self: HistoryConsolidator, item: dict) -> str | None:
    """The lesson's authored ``applies`` tier to forward, or ``None`` for unstated.

    One policy for every consolidation write path, so the member-store path
    in ``VectorMemoryStore.apply_consolidation`` and this one cannot drift:
    see ``extracted_lesson_applies``.
    """
    return extracted_lesson_applies(item.get("applies"), self._logger)


def _lesson_delete_decision(
    self: HistoryConsolidator, vector_store: "VectorMemoryStore", del_key: object
) -> "_LessonDeleteDecision":
    """Whether a consolidation delete of *del_key* must be refused, and the
    exact stored body the decision was read from.

    A model's guessed contradiction may retire an ``on_topic`` finding but
    never a standing rule the user taught -- the same invariant the
    ``/api/lessons`` contradiction sweep enforces before it supersedes a
    candidate. Only ``lesson.*`` keys carry a tier, so a non-lesson key is
    never protected here. The stored row is read authoritatively (the tier is
    write-once, so the persisted value is the author's), and anything that is
    not the ``on_topic`` tier -- ``always``, an unstated row, an unreadable or
    missing value -- is protected: the narrowest fail-safe, since demoting a
    real standing rule is the costlier mistake.

    ``checked_value_json`` is the ``value_json`` the tier was read from, so an
    allowed delete can COMPARE-AND-DELETE against it: ``_lesson_key`` keys on
    rule text plus scope alone, so a delete-plus-re-add is the documented way
    to change a tier and can put a standing rule under the same key between
    this read and the delete. Passing the checked body as ``expect_value_json``
    makes the delete a no-op when the row moved, closing that race without a
    lock this call site cannot hold. It is ``None`` for a protected decision
    (no delete follows) and for a non-lesson key (which the caller deletes
    unconditionally, its existing contract).
    """
    if not isinstance(del_key, str) or not del_key.startswith("lesson."):
        return _LessonDeleteDecision(protected=False, checked_value_json=None, reason="allow")
    try:
        row = vector_store.get_semantic(del_key)
    except Exception:
        # An unreadable row is protected, not silently deletable: a lookup
        # failure must never widen what a guess is allowed to retire. This is
        # a store outage, not a tier decision, so it carries its own reason.
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="unreadable")
    if not isinstance(row, dict):
        # No active row under this lesson key. This is NOT a safe no-op: the
        # allow path deletes unconditionally (no value to compare against),
        # and the absent state is the intermediate state of the documented
        # delete-plus-re-add re-tier -- a concurrent learn_add can recreate
        # the key as a standing rule inside the window between this read and
        # the delete, which the unconditional delete would then tombstone.
        # There is nothing legitimate for a guess to delete under an absent
        # lesson key anyway, so protect: refuse rather than race.
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="absent")
    raw = row.get("value_json")
    checked_value_json = raw if isinstance(raw, str) else None
    decoded: object = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            # A row whose body will not decode has no readable tier; treat it
            # as the protected (standing) class rather than guessing it is a
            # finding.
            return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="tier")
    # Only the mapping shape can carry a tier. A legacy string row, or any
    # value that does not decode to a mapping, has no author-stated tier and
    # is protected -- the same unstated->standing treatment readers give it.
    if not isinstance(decoded, dict):
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="tier")
    protected = authored_lesson_applies(decoded.get("applies")) != LESSON_APPLIES_ON_TOPIC
    return _LessonDeleteDecision(
        protected=protected,
        checked_value_json=None if protected else checked_value_json,
        reason="tier" if protected else "allow",
    )


def _save_lessons(
    self: HistoryConsolidator,
    raw: object,
    vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    lesson_store: "LessonStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    *,
    facets: "MemoryFacets | None" = None,
    key: str = "",
    commit_state: _RunCommitState | None = None,
) -> None:
    """Save extracted lessons from consolidation result.

    Both stores are passed in rather than read off ``self`` so a crew's
    corrections land in its own silo. Omitting them keeps the historical
    behaviour (the global handles), which is what the workspace and default
    arms of the caller want.

    An explicitly passed ``None`` is NOT omission (:data:`_INHERIT_GLOBAL`):
    it means the silo has no store of that tier, so the tier is skipped. The
    caller reaches this method whenever EITHER store is live, so a silo whose
    vector store could not be stood up arrives here with
    ``vector_store=None`` and a real ``lesson_store`` — and inheriting the
    global vector store there would take the dedup-aware branch below and
    file the crew's corrections into the operator's own table, never touching
    the silo's ``lessons.jsonl`` at all.
    """
    if isinstance(vector_store, _InheritGlobal):
        vector_store = self._vector_store
    if isinstance(lesson_store, _InheritGlobal):
        lesson_store = self._lesson_store
    if not isinstance(raw, list):
        return

    # Cap like semantic/episodic: each write_lesson can perform up to 6
    # blocking embeds, so an uncapped LLM lessons array would occupy a
    # worker thread for minutes.
    max_lessons = _MAX_LESSONS_PER_CONSOLIDATION
    if len(raw) > max_lessons:
        self._logger.warning(
            "Consolidation returned %d lessons; capping to %d",
            len(raw),
            max_lessons,
        )
        raw = raw[:max_lessons]

    # Prefer vector store (dedup-aware) over JSONL
    if vector_store:
        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                scope, drop = self._gated_lesson_scope(item)
                if drop:
                    continue
                rule_generation = vector_store.space_generation
                rule_emb = vector_store.embed_lesson(item["rule"])
                with self._publication_hold_checked(key, commit_state) as publication:
                    ok = vector_store.write_lesson(
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                        rule_emb=rule_emb,
                        rule_emb_generation=rule_generation,
                        rule_emb_resolved=True,
                        defer_backfills=True,
                        # Gated by _gated_lesson_scope above; write_lesson
                        # canonicalises and re-checks admissibility itself.
                        repo_scope=scope,
                        # Already normalized by _lesson_tier, so write_lesson's
                        # own raising check cannot fire on it.
                        applies=self._lesson_tier(item),
                        facets=facets,
                    )
                    if ok:
                        publication.mark_committed()
                if ok:
                    count += 1
        if count:
            self._logger.info("Extracted %d lesson(s) from chat (vector store)", count)
        return

    if not lesson_store:
        return
    from datetime import timezone as _tz

    from kiro_crew.learn import Lesson

    count = 0
    for item in raw:
        if isinstance(item, dict) and item.get("rule"):
            scope, drop = self._gated_lesson_scope(item)
            if drop:
                continue
            with self._publication_hold_checked(key, commit_state) as publication:
                outcome = lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        # Gated by _gated_lesson_scope above (LessonStore.save
                        # canonicalises but never checks admissibility itself).
                        repo_scope=scope,
                        # None is dropped by _serializable, so an unstated row
                        # is byte-identical to one written before the field.
                        applies=self._lesson_tier(item),
                    )
                )
                if outcome != "refused":
                    publication.mark_committed()
            if outcome != "refused":
                count += 1
    if count:
        self._logger.info("Extracted %d lesson(s) from chat", count)


def _write_structured_memory(
    self: HistoryConsolidator,
    result: dict,
    key: str,
    vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    *,
    facets: "MemoryFacets | None" = None,
    snapshot: dict | None = None,
    visible_keys: frozenset[str] | None = None,
    messages: list[dict] | None = None,
    commit_state: _RunCommitState | None = None,
) -> None:
    """Write semantic + episodic entries from consolidation result.

    *vector_store* is the session's RESOLVED store. Omitting it keeps the
    global handle, which is what the workspace and default arms want; an
    explicit ``None`` means the silo has no vector store and the tier is
    skipped, the same distinction :meth:`_save_lessons` draws.

    *visible_keys* is the set of keys the prompt's bounded semantic table
    showed the model; a ``delete`` of a key outside it is withheld before the
    loop (``_withhold_unseen_deletes`` says why an update is not). ``None``
    means the caller rendered no bounded table and nothing is withheld.
    """
    if isinstance(vector_store, _InheritGlobal):
        vector_store = self._vector_store
    if not vector_store:
        return
    source = f"consolidation:{key}"
    private_policy = getattr(vector_store, "algorithm_version", "v1") == "v2"
    # Shared by both tiers below: each embeds inline, so both charge the same
    # pass and either can arm the latch for the other.
    budget = _EmbedBudget(_EMBED_BUDGET_SECS_PER_PASS, self._logger)

    result, withheld = _withhold_unseen_deletes(result, visible_keys, self._logger)

    # Semantic entries
    semantic_items = result.get("semantic")
    if isinstance(semantic_items, list):
        written = 0
        deleted = 0
        skipped = 0
        refused = 0
        protected = 0
        absent = 0
        unreadable = 0
        stale_skipped = 0
        for item in semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            # Handle deletion of stale keys
            if item.get("delete"):
                # A model's guess may retire an on_topic finding, never a
                # standing rule the user taught. The /api/lessons
                # contradiction sweep enforces this; consolidation is the
                # sibling model-judged deletion path, so it enforces the same
                # invariant here rather than in delete_semantic -- an explicit
                # forget must still be able to remove a standing rule, and
                # only this call site knows the delete is an inference.
                decision = self._lesson_delete_decision(vector_store, item["key"])
                if decision.protected:
                    # Three protected causes mean different things to an
                    # operator, so they log and count apart rather than as one
                    # "protected" total that hid a store outage among real
                    # tier refusals.
                    if decision.reason == "absent":
                        absent += 1
                        self._logger.info(
                            "Semantic consolidation skipped delete of %r: no "
                            "active lesson row (refused to avoid an "
                            "unconditional delete racing a re-add)",
                            item["key"],
                        )
                    elif decision.reason == "unreadable":
                        unreadable += 1
                        self._logger.warning(
                            "Semantic consolidation could not read lesson %r to "
                            "check its tier; refused the delete (store read "
                            "failed)",
                            item["key"],
                        )
                    else:
                        protected += 1
                        self._logger.info(
                            "Semantic consolidation refused to retire standing "
                            "lesson %r: a guess may retire an on_topic finding, "
                            "never a standing rule",
                            item["key"],
                        )
                    continue
                with self._publication_hold_checked(key, commit_state) as publication:
                    if private_policy:
                        published = vector_store.propose_semantic_delete(item["key"], source)
                        if published:
                            refused += 1
                    else:
                        # Compare-and-delete against the body the tier was
                        # read from: _lesson_key keys on rule text plus scope,
                        # so a concurrent delete-plus-re-add (the documented
                        # way to change a tier) can put a standing rule under
                        # this key between the read and here. expect_value_json
                        # makes the delete a no-op when the row moved, so a
                        # guess cannot tombstone a replacement it never checked.
                        # A non-lesson key carries None and deletes as before.
                        published = vector_store.delete_semantic(
                            item["key"],
                            source,
                            expect_value_json=decision.checked_value_json,
                        )
                        if published:
                            deleted += 1
                        elif decision.checked_value_json is not None:
                            # The compare-and-delete lost: the row body moved
                            # between the guard's read and the UPDATE, so
                            # nothing was tombstoned. Announce it like the
                            # sibling paths do rather than letting it vanish
                            # into "0 deleted", which reads as no delete items.
                            stale_skipped += 1
                            self._logger.info(
                                "Semantic consolidation skipped delete of %r: "
                                "the row changed under the key after the tier "
                                "check (compare-and-delete no-op)",
                                item["key"],
                            )
                    if published:
                        publication.mark_committed()
                continue
            if "value" not in item or item["value"] is None:
                # Counted and logged here because this path returns before set_semantic, so
                # the VALUE_EMPTY reject event never fires for the omission that motivated it.
                skipped += 1
                self._logger.warning(
                    "Semantic consolidation skipped %r: item carries no value", item["key"]
                )
                continue
            try:
                conf = float(item.get("confidence", 0.5))
            except (ValueError, TypeError):
                skipped += 1
                continue
            if not math.isfinite(conf) or not 0 <= conf <= 1:
                skipped += 1
                continue
            # Always our own source, never "user_explicit": a confidence of 1.0 is the
            # LLM's claim that the user stated the fact, not proof of it. Under
            # `consolidation:<key>` the conflict resolution in _write_semantic protects
            # a genuine user-stated row from a re-summarization (conflict_skip) while
            # still letting consolidation create new keys and update its own.
            extra = {}
            if private_policy and item.get("metadata") is not None:
                extra["metadata"] = item["metadata"]
            if private_policy and snapshot and messages and item["key"] in snapshot:
                from kiro_crew.memory_record_metadata import verified_correction

                evidence = verified_correction(
                    key=item["key"],
                    before=snapshot[item["key"]],
                    value=item["value"],
                    quote=item.get("correction_quote"),
                    messages=messages,
                    session_key=key,
                )
                if evidence:
                    extra["correction"] = evidence
                    extra["expected_revision"] = evidence.revision
            previous = snapshot.get(item["key"]) if snapshot else None
            previous_value_json = previous.get("value_json") if isinstance(previous, dict) else None
            if not isinstance(previous_value_json, str):
                previous_value_json = None
            defer = budget.tripped
            embedding_generation = vector_store.space_generation
            with budget.measured():
                embedding = (
                    None if defer else vector_store.embed_semantic(item["key"], item["value"])
                )
                retirement_embedding = (
                    None
                    if defer or previous_value_json is None
                    else vector_store.embed_semantic_retirement(item["key"], previous_value_json)
                )
            with self._publication_hold_checked(key, commit_state) as publication:
                err = vector_store.set_semantic(
                    key=item["key"],
                    value=item["value"],
                    confidence=conf,
                    source=source,
                    facets=facets,
                    defer_embedding=defer,
                    embedding=embedding,
                    embedding_resolved=True,
                    embedding_generation=embedding_generation,
                    retirement_embedding=retirement_embedding,
                    retirement_embedding_resolved=True,
                    retirement_value_json=previous_value_json,
                    **extra,
                )
                if err is None:
                    publication.mark_committed()
            if err is None:
                written += 1
            else:
                # Counted apart from `skipped`: several reject causes reach here and only
                # VALUE_EMPTY is a missing value, so a shared label names the wrong cause.
                reject_code, reason = err
                refused += 1
                # The reason names the specific cause a bare code cannot (which
                # confidence lost, which proposal holds the value). Causes the store
                # audits leave a memory_events row under the cause as the event type:
                # log_reject_event stores only the rejected value (old_value None,
                # new_value cut to 200 chars). A conflict_skip row stores both values,
                # but not every conflict_skip refusal writes one: a revision mismatch
                # or bad metadata returns the code with no row. VALUE_SIZE and
                # VALUE_ENCODING audit nothing, which is why the pointer is scoped
                # rather than a promise for every code.
                self._logger.warning(
                    "Semantic consolidation refused %r: %s: %s"
                    " (memory_events records the rejected value for audited causes;"
                    " a conflict_skip row, when written, records both)",
                    item["key"],
                    reject_code.value,
                    reason,
                )
        if (
            written
            or deleted
            or skipped
            or refused
            or protected
            or absent
            or unreadable
            or stale_skipped
            or withheld
        ):
            self._logger.info(
                "Semantic consolidation: %d written, %d deleted, %d skipped (no value), "
                "%d refused, %d protected (standing rule), %d absent, %d unreadable, "
                "%d stale-skipped (compare-and-delete no-op), "
                "%d withheld (delete of a key not in the rendered table)",
                written,
                deleted,
                skipped,
                refused,
                protected,
                absent,
                unreadable,
                stale_skipped,
                withheld,
            )

    # Episodic entries
    episodic_items = result.get("episodic")
    if isinstance(episodic_items, list):
        written = 0
        deferred = 0
        for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            tags = item.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                continue
            try:
                importance = float(item.get("importance", 0.5))
            except (ValueError, TypeError):
                continue
            if not math.isfinite(importance) or not 0 <= importance <= 1:
                continue
            # `defer_embedding` stores the row with a NULL vector instead of
            # embedding it here. The text is keyword-searchable at once and the
            # repair sweep fills the vector in.
            #
            # `preserve_existing` comes with it, and is not optional: without a
            # vector the similarity dedup cannot run, and on a legacy V1 store
            # at its episodic cap the insert would then tombstone the
            # lowest-importance row to make room for a paraphrase it never
            # compared against. A write that cannot arbitrate a conflict has no
            # standing to evict, so at the cap the deferred row is refused
            # instead — the transcript it came from is still on disk, and the
            # row it would have displaced is not recoverable.
            #
            # Read before the write, so the row that SPENDS the budget is the
            # last one to pay for an embed rather than the first to skip one.
            defer = budget.tripped
            with budget.measured():
                embedding_generation = vector_store.space_generation
                embedding = None if defer else vector_store.embed_episodic(item["text"])
                with self._publication_hold_checked(key, commit_state) as publication:
                    ep_ok = vector_store.write_episodic(
                        text=item["text"],
                        embedding=embedding,
                        embedding_resolved=True,
                        embedding_generation=embedding_generation,
                        conversation_id=key,
                        tags=tags,
                        importance=importance,
                        source=source,
                        facets=facets,
                        defer_embedding=defer,
                        preserve_existing=defer,
                    )
                    if ep_ok:
                        publication.mark_committed()
            if ep_ok:
                written += 1
                if defer:
                    deferred += 1
        if written:
            self._logger.info(
                "Wrote %d episodic entries from consolidation (%d with embedding deferred)",
                written,
                deferred,
            )
