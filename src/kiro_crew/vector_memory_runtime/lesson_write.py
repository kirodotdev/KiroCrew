"""Writing one lesson: ``write_lesson`` and ``embed_lesson``.

``write_lesson`` refuses a volatile or inadmissible lesson, preflights the value it would
store, resolves the exact-rule pass (``lessons.resolve_exact_rule``), runs the scope-local
dedup scan behind its authority pre-pass, and queues every supersede until ``set_semantic``
has committed the submission; then it drains the queue and publishes the lesson's vector
under ``_vector_commit``. The lesson value codec and the lesson readers live in
``vector_memory_runtime.lessons``.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields read: ``embed_fn``, ``_space_generation``,
``_sem_rel`` and ``_sem_guard``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import struct
from typing import TYPE_CHECKING

from kiro_crew import memory_schema
from kiro_crew.embeddings import PRIORITY_BULK
from kiro_crew.lesson_validation import (
    contains_volatile_lesson_fact,
    normalize_lesson_applies,
    utf8_encodable,
)
from kiro_crew.project_scope import canonical_scope, scope_is_admissible
from kiro_crew.validation import normalize_lesson_category
from kiro_crew.vector_memory_runtime import lessons as _lessons
from kiro_crew.vector_memory_runtime.write_outcomes import LessonWriteOutcome, LessonWriteResult

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def write_lesson(
    self: VectorMemoryStore,
    rule: str,
    category: str = "knowledge",
    negative: str | None = None,
    source: str = "user_explicit",
    rule_emb: list[float] | None = None,
    rule_emb_generation: int | None = None,
    repo_scope: str | None = None,
    *,
    applies: str | None = None,
    rule_emb_resolved: bool = False,
    defer_backfills: bool = False,
    facets: "memory_schema.MemoryFacets | None" = None,
) -> LessonWriteResult:
    """Write a lesson as a semantic entry with key lesson.<hash>.

    Returns which outcome occurred (see :class:`LessonWriteOutcome`) rather than a
    bare ``bool``, whose ``False`` conflated "validation refused this", "a dedup
    rule claimed it", "it is already stored exactly as submitted" and "your bare
    re-submit kept the stored clause" -- four facts a caller cannot act on without
    telling them apart. The result's TRUTH VALUE is still the old predicate (see
    :class:`LessonWriteResult`), so a caller that only needs "did this write
    something" keeps using ``if store.write_lesson(...)`` unchanged.

    Deduplicates against existing lessons:
    - Substring match: if existing contains new (or vice versa), longer wins
    - Topic overlap: if the shared significant words are >=50% of the LARGER of
      the two keyword sets, newer replaces older
    - Semantic similarity: if >85% cosine similarity, newer replaces older --
      unless a stored near-duplicate outranks this write (``user_explicit``
      over a lower-authority source, or strictly higher stored confidence),
      which reports ``deduped`` / ``semantic_similarity``.

    **A call that stores nothing deletes nothing.** Every supersede the scan
    decides on is QUEUED, and the queue drains only once ``set_semantic`` has
    committed the submission. So a ``deduped`` verdict from any branch, and a
    ``refused`` from the store's own validation, each preserve every live lesson
    row and report an empty ``superseded``. They are not byte no-ops on the
    database: a lazy embedding backfill for a row this call READ may already have
    been flushed, which changes a vector and no lesson.

    Draining after the commit puts the two outcomes in the safe order, and leaves
    one window it cannot close: a drain that stops partway -- a raised error, a
    killed process -- keeps the submission and leaves the rows it had not reached
    yet. That direction is deliberate. The residue is a duplicate, never a lost
    lesson, and the next write matching those rows retires them.

    Two of those three rules DELETE a stored lesson, and the substring rule's
    "longer wins" direction means a submitted rule can retire a stored one that
    is more general than it -- attaching a condition to a rule makes the text
    longer and the guidance NARROWER, so the row that survives can be the one
    that applies less often. That is the designed behaviour and this method
    keeps it: the
    alternative is a store that accumulates near-identical rules, which is what
    these three rules exist to prevent, and the onboarding import already shows
    the sanctioned way to opt out of it (route to ``set_semantic_if_absent``,
    which cannot replace anything -- see ``onboarding_import``).

    What it does NOT keep is the silence. Every rule that deletes records the
    rule text it removed in :attr:`LessonWriteResult.superseded`, so a caller is
    never handed a bare ``inserted`` for a call that destroyed a lesson the
    user still wanted. The result is the only place that can carry this: the
    deleted row is a tombstone, so it is gone from ``get_lessons``, from
    ``learn_list`` and from the injected lessons block by the time the caller
    looks.

    Pass ``rule_emb`` to reuse an embedding already computed by the caller
    and avoid a second blocking embed of the identical text. A caller doing
    that MUST also read :attr:`space_generation` BEFORE it embeds and pass it
    as ``rule_emb_generation``, so a model swap landing between that embed and
    this write is detected and the vector is left NULL for the backfill
    instead of being committed into the wrong space. ``rule_emb_resolved``
    additionally distinguishes an attempted embed that returned ``None`` from
    no attempt, and ``defer_backfills`` suppresses lazy legacy-row inference;
    together they let a caller keep inference outside a short publication lock.

    Runtime model-identity assertions and recognized concrete-ID model-selection
    imperatives in either persisted field are refused here, before embedding or
    deduplication. A model-version literal without either form remains durable. The
    JSONL fallback calls the same predicate, so MCP, dashboard, consolidation,
    task-runner, and direct callers share the boundary.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _MAX_BACKFILLS_PER_CALL = vm._MAX_BACKFILLS_PER_CALL
    _lesson_key = vm._lesson_key

    if contains_volatile_lesson_fact(rule, negative):
        return LessonWriteResult(LessonWriteOutcome.REFUSED, "volatile_session_fact")
    # A lone surrogate is JSON-legal (the model can emit ``\ud800`` in its
    # output) but not UTF-8 encodable, and ``_lesson_key`` hashes the rule --
    # and the scope with it -- via ``str.encode()`` below. Refuse it here, at
    # the single derive site every caller shares (consolidation's native
    # ``lessons`` array, its rerouted ``lesson.*`` semantic items, the JSONL
    # fallback, MCP, dashboard), so a bad character skips the lesson instead of
    # aborting the whole consolidation pass with a UnicodeEncodeError.
    if not utf8_encodable(rule):
        return LessonWriteResult(LessonWriteOutcome.REFUSED, "rule_not_utf8_encodable")
    rule_lower = rule.lower()
    # A whitespace-only clause is no clause. `--negative "   "` is truthy, so
    # without this it composed "<rule> — NOT:    " and REPLACED a real stored
    # clause with blanks -- silent loss of the guidance the user had saved.
    #
    # isinstance FIRST, because this normalisation is what makes a non-string
    # reachable as a crash: consolidation passes the LLM's own
    # item.get("negative") straight through (history.py), so a model emitting
    # `"negative": 123` would hit .strip() and abort the whole run with
    # AttributeError. Before this normalisation existed an int only ever reached
    # an f-string, which interpolated it harmlessly -- so the guard is paying for
    # the strip, not for a pre-existing hole. A non-string is not usable
    # guidance, and str()-ifying it would store a repr as if the user wrote it,
    # so treat it as absent.
    negative = negative.strip() or None if isinstance(negative, str) else None
    # Same normalisation and the same non-string guard as the clause above:
    # consolidation forwards the model's own value unchecked, so a blank scope
    # stores as absent (applies everywhere) and a non-string is treated as
    # absent rather than reaching .strip() and aborting the run.
    # Canonicalise to the form the GATE compares, so storage and the gate agree
    # on what one scope is. See canonical_scope for why the raw string is wrong.
    #
    # A scope the gate can NEVER satisfy is refused here rather than normalised
    # or dropped. Both alternatives are wrong in opposite directions:
    # canonicalising "/src/pkg" strips the slash and ACTIVATES the lesson in
    # every repository holding src/pkg, which it was never validly scoped to;
    # returning None instead would store it GLOBALLY, which is the fail-open a
    # scoped lesson must never take. Refusing is the only answer that neither
    # invents a scope nor widens one, and it keeps this surface consistent with
    # the schema, which already rejects the same shapes.
    if repo_scope is not None and isinstance(repo_scope, str) and repo_scope.strip():
        if not scope_is_admissible(repo_scope):
            return LessonWriteResult(LessonWriteOutcome.REFUSED, "scope_inadmissible")
    repo_scope = canonical_scope(repo_scope)
    # Same reason as the rule above: the canonical scope is hashed with the
    # rule in ``_lesson_key``, so a lone surrogate here would crash the pass.
    if repo_scope is not None and not utf8_encodable(repo_scope):
        return LessonWriteResult(LessonWriteOutcome.REFUSED, "scope_not_utf8_encodable")
    # The category is now part of the stored value, so an unusable one would be
    # scanned by validate_semantic and could REJECT the whole lesson -- turning a
    # bad label into lost guidance. Consolidation passes the LLM's own
    # item.get("category") straight through (history.py) with no validation,
    # unlike the REST and MCP paths, which are enum-restricted by
    # LEARN_ADD_SCHEMA. The shared helper clamps to that same enum
    # (write policy, strict=True), safely handling unhashable labels
    # (a dict or list from the LLM) that would make a raw set membership
    # test raise and abort consolidation instead of clamping.
    category = normalize_lesson_category(category, strict=True)
    # The tier goes the other way from the category, which CLAMPS an unusable
    # label so a bad string cannot cost the user the whole lesson. There is no
    # safe clamp for a tier: picking "directive" would inject a note into every
    # session, and picking "experience" would demote a standing rule. Both are
    # silent. So a misspelled tier raises here, which is a caller bug and
    # reaches that caller, while an OMITTED tier is the supported choice to
    # leave the row unclassified.
    applies = normalize_lesson_applies(applies)
    # What the STORED row will carry. Equal to the submission on an insert,
    # but the enrich branch below replaces it with the persisted value,
    # because the tier is write-once. Reported back so a caller guarding a
    # DELETION on the tier reads what is stored rather than what was sent.
    effective_applies = applies
    rule_words = self._lesson_keywords(rule_lower)
    # Same reasoning as write_episodic: carry the space generation to the write
    # so a swap landing between the embed and the lock cannot commit a vector
    # from the previous space.
    #
    # A caller-supplied ``rule_emb`` was embedded BEFORE this call, so its space
    # is provenance this method cannot infer — capturing here would compare the
    # post-swap generation against itself and wave the stale vector through.
    # Such callers pass the ``space_generation`` they read before embedding.
    if rule_emb is not None and rule_emb_generation is not None:
        lesson_embed_generation = rule_emb_generation
    else:
        lesson_embed_generation = self._space_generation
    if rule_emb is None and not rule_emb_resolved:
        rule_emb = self.embed_lesson(rule)
    backfills_done = 0
    # (blob, key, space generation, exact value embedded). The generation is
    # recorded per entry, not once for the call: these lazy backfills embed
    # inside the dedup scan below, so a swap can land between entries.
    pending_backfills: list[tuple[bytes, str, int, str, list[float]]] = []

    # PREFLIGHT the final value BEFORE the dedup scan below, which DELETES
    # superseded rows. The value was only validated by set_semantic at the very
    # end, so a value this store refuses (e.g. an injection-pattern ``negative``)
    # cost the caller its existing lesson: the dedup scan deleted the old row,
    # then set_semantic refused the replacement, and the route still returned
    # HTTP 200 with no lesson stored. Validating here makes the whole call a
    # no-op when the replacement cannot land.
    key = _lesson_key(rule, repo_scope)
    # The mapping shape keeps the two halves as separate fields, so they
    # survive a round-trip regardless of what characters the rule contains.
    # The legacy in-band form ("<rule><sep><negative>") is still READ below
    # and by every renderer — no migration; old rows upgrade only when a
    # re-submit rewrites them anyway. validate_semantic size-gates lesson
    # mappings on their content (legacy-equivalent bytes), so the JSON
    # envelope does not shrink the accepted rule capacity.
    lesson_value: dict[str, object] = {
        "rule": rule,
        "category": category,
        "negative": negative,
    }
    # The key is added only when a scope was given, so an unscoped lesson keeps
    # the exact stored shape it has always had and no existing row is churned.
    if repo_scope:
        lesson_value["repo_scope"] = repo_scope
    # Same additive rule for the tier: a caller that names none leaves the key
    # ABSENT, so the row is byte-identical to what this writer produced before
    # the field existed and reads as unclassified. Absent and "not applicable"
    # are the same answer here, which is why there is no stored sentinel.
    if applies:
        lesson_value["applies"] = applies
    value: object = lesson_value
    confidence = 1.0 if source == "user_explicit" else 0.9
    preflight = self.validate_semantic(key, value, confidence, source)
    if preflight is not None:
        code, message = preflight
        logger.info("Lesson rejected before dedup (%s): %s", code, message)
        return LessonWriteResult(LessonWriteOutcome.REFUSED, code.value)

    def _flush_backfills() -> None:
        for blob, bk, gen, body, vector in pending_backfills:
            with self._vector_commit(vector, best_effort=True) as current:
                if not current or gen != self._space_generation:
                    continue
                self.db.execute(
                    f"UPDATE {self._sem_rel} SET embedding = ? WHERE key = ? "
                    f"AND value_json = ? AND embedding IS NULL AND is_deleted = 0"
                    f"{self._sem_guard}",
                    (blob, bk, body),
                )

    # TWO PASSES, and the order is load-bearing.
    #
    # Pass 1 resolves THIS lesson. Pass 2 runs the generic dedup rules, and those
    # can claim the write on an UNRELATED row -- a superset whose text contains our
    # rule. get_lessons() orders by updated_at DESC, then by md5 key within one
    # stamp, so whether such a row is scanned before ours is effectively random,
    # and doing both in one loop made the outcome depend on that order: an
    # unrelated superset seen first discarded an enrichment we had already
    # selected, and the clause was dropped on HTTP 200.
    # Resolving the exact match first makes the result order-independent, and
    # pass 2 is skipped entirely once pass 1 claims the write.
    # Deduplication is SCOPE-LOCAL, and both passes below share this list.
    #
    # A lesson scoped to one repository and a global one are different lessons
    # even when their wording is close, so a scoped write must never supersede,
    # enrich, or be discarded against a row from another scope. Without this the
    # generic dedup rules (substring containment, >50% keyword overlap, high
    # cosine similarity) reach across scopes and DELETE guidance the submitter
    # never addressed -- writing a repo-scoped rule could retire a global one
    # that merely shared most of its significant words.
    #
    lesson_rows = _lessons.lesson_rows_in_scope(self.get_lessons(), repo_scope)

    exact = _lessons.resolve_exact_rule(
        self,
        lesson_rows,
        rule=rule,
        key=key,
        negative=negative,
        category=category,
        applies=applies,
        confidence=confidence,
        source=source,
    )
    matched = exact is not None
    if exact is not None:
        if exact.result is not None:
            _flush_backfills()
            return exact.result
        key, value, effective_applies = exact.key, exact.value, exact.applies

    # Built once for the whole scan (query-side vector + norm are the same
    # for every row) rather than per candidate — see _stored_similarity_scorer.
    similarity = self._stored_similarity_scorer(rule_emb) if rule_emb else None

    # Every row this call tombstoned, in the order it went. Collected rather
    # than counted: a count tells the caller a lesson is gone without telling it
    # WHICH, and the row is a tombstone by the time the caller could look it up.
    # Populated only where the deletions actually run -- after the write lands --
    # so a result that reports a supersede is always a result whose write landed.
    superseded: list[str] = []

    # Every supersede the scan decides on, DEFERRED as (key, report): the
    # queue drains only after the write is COMMITTED, so no route that
    # declines the submission can cost a stored lesson. Deferring is the
    # whole invariant rather than one branch's detail -- rows are scanned in
    # recency order, so an eager delete in any branch can precede a later
    # row's refusal, and the caller is then handed a decline for a call that
    # emptied part of the store. Draining after ``set_semantic`` extends the
    # same guarantee to a value the store itself rejects.
    deferred_supersedes: list[tuple[str, str, str, str]] = []

    # AUTHORITY PRE-PASS -- non-mutating, decided before the scan's first
    # deletion. The invariant: every refusal the authority rule can make
    # is a verdict on rows the scan has not touched yet, decided
    # before the scan mutates anything. Rows are scanned in recency order,
    # not authority order, so a mid-scan authority decline could land
    # AFTER the untouched branches (or an earlier semantic supersede)
    # already deleted a row -- losing a stored lesson while storing
    # nothing. So the authority verdict is settled here, over the same
    # rows the semantic branch below will see: the pre-pass shares
    # ``backfills_done`` / ``pending_backfills`` with the main scan and
    # memoizes each computed blob onto its row dict, so a row embedded
    # here is never re-embedded or re-counted below, and the semantic
    # branch's visible set is a subset of the pre-pass's. (The pre-pass
    # can spend budget on rows the main scan never reaches -- it walks the
    # whole list, while the scan can return early on a lexical claimant --
    # so the SETS can differ even though no row is ever double-charged.)
    # A ``user_explicit`` write can never be declined, so the pass is
    # skipped for it entirely. The pass is non-mutating for its own reason,
    # independent of the scan's deferral below: an authority decline must not
    # spend the call's embed budget rewriting rows it is about to refuse.
    # Scan-wide authority ordering is a tracked follow-up decision.
    if (
        self.algorithm_version != "v2"
        and similarity is not None
        and source != "user_explicit"
        and not matched
    ):
        for existing in lesson_rows:
            pre_text = _lessons._lesson_row_text(existing)
            if pre_text is None:
                continue
            # PURE semantic matches only. A row the mutating scan's
            # substring or topic-overlap branch would claim FIRST (per-row
            # branch order) keeps main's outcome for it -- those branches
            # are source-blind by main's design, and the pre-pass must not
            # decline a write that main would have resolved lexically
            # before the semantic test ever ran. The predicates mirror the
            # scan's own, on the same normalized text.
            pre_lower = pre_text.lower()
            if rule_lower in pre_lower or pre_lower in rule_lower:
                continue
            if rule_words:
                pre_words = self._lesson_keywords(pre_lower)
                if pre_words and (
                    len(rule_words & pre_words) / max(len(rule_words), len(pre_words)) >= 0.5
                ):
                    continue
            existing_emb_blob = existing.get("embedding")
            row_blob: bytes | None = None
            if (
                existing_emb_blob
                and isinstance(existing_emb_blob, bytes)
                and len(existing_emb_blob) >= 4
            ):
                row_blob = existing_emb_blob
            elif self.embed_fn and not defer_backfills and backfills_done < _MAX_BACKFILLS_PER_CALL:
                # Same lazy-backfill contract as the main scan (count even
                # on failure; generation sampled BEFORE the embed). The
                # blob is memoized onto the row dict so the main scan
                # neither re-embeds nor re-counts this row; a failure is
                # marked so the row is attempted at most once per call,
                # exactly as before this pass existed.
                backfill_generation = self._space_generation
                existing_emb = self._try_embed(
                    _lessons._lesson_embed_text(json.loads(existing["value_json"])),
                    PRIORITY_BULK,
                )
                if existing_emb:
                    row_blob = struct.pack(f"{len(existing_emb)}f", *existing_emb)
                    pending_backfills.append(
                        (
                            row_blob,
                            existing["key"],
                            backfill_generation,
                            existing["value_json"],
                            existing_emb,
                        )
                    )
                    existing["embedding"] = row_blob
                else:
                    existing["_authority_prepass_embed_failed"] = True
                backfills_done += 1
            if row_blob is None:
                continue
            if similarity({"embedding": row_blob}) > 0.85:
                # Outranking means a ``user_explicit`` row over this
                # lower-authority write, or a strictly higher stored
                # confidence -- the confidence half on its own merit: the
                # onboarding import stores the user's own lessons at
                # confidence 1.0 under source "import", so a 0.9
                # consolidation write must not retire them. Strict ``>``
                # is a deliberate divergence from ``_write_semantic``'s
                # same-key rule (which treats confidences within 0.1 as
                # equal and lets the newer write win): near-duplicates are
                # DIFFERENT rows with no same-key freshness to prefer, and
                # equal confidence falls through to newest-wins below.
                try:
                    existing_confidence = float(existing.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    existing_confidence = 0.0
                if existing.get("source") == "user_explicit" or existing_confidence > confidence:
                    logger.info(
                        "Lesson semantic dedup: higher-authority %r kept over %s",
                        existing["key"],
                        source,
                    )
                    # Nothing has been deleted: this return precedes the
                    # mutating scan entirely, so a declined write costs no
                    # stored row and ``superseded`` is always empty here.
                    _flush_backfills()
                    return LessonWriteResult(
                        LessonWriteOutcome.DEDUPED,
                        "semantic_similarity",
                        tuple(superseded),
                        winner=_lessons._lesson_row_report_text(existing) or pre_text,
                        winner_repo_scope=_lessons._lesson_scope(
                            json.loads(existing["value_json"])
                        ),
                    )

    # Private V2 keeps exact-rule enrichment above, but similarity cannot
    # authorize deleting a different instruction. Corrections target a key
    # explicitly; uncertain conflicts remain visible for owner review.
    for existing in [] if matched or self.algorithm_version == "v2" else lesson_rows:
        existing_text = _lessons._lesson_row_text(existing)
        if existing_text is None:
            continue
        existing_lower = existing_text.lower()
        # Two renderings of one row, and the split is the point. Every COMPARISON
        # below stays on ``existing_text`` (the embed rendering) so no dedup
        # decision changes; only what a deletion REPORTS uses the display
        # rendering, which keeps the NOT-clause. Falls back to the comparison text
        # when a row has no display form, so the report can never be emptier than
        # the row it names.
        existing_report = _lessons._lesson_row_report_text(existing) or existing_text

        may_retire_existing, covering_row_arrives_less_often = _lessons.tier_permissions(
            existing, applies
        )

        # Substring dedup
        if rule_lower in existing_lower and not covering_row_arrives_less_often:
            logger.info(
                "Lesson dedup: %s already covered by %s [%s]", key, existing["key"], category
            )
            _flush_backfills()
            return LessonWriteResult(
                LessonWriteOutcome.DEDUPED,
                "substring_covered",
                tuple(superseded),
                winner=existing_report,
                winner_repo_scope=_lessons._lesson_scope(json.loads(existing["value_json"])),
            )
        if existing_lower in rule_lower:
            # Like the topic and semantic branches below, this one deletes nothing:
            # at most it queues the supersede, and the drain after ``set_semantic``
            # logs each queued row it retires or skips.
            if may_retire_existing:
                deferred_supersedes.append(
                    (existing["key"], existing_report, existing["value_json"], "contains")
                )
            continue

        # Topic overlap dedup
        if rule_words:
            existing_words = self._lesson_keywords(existing_lower)
            if existing_words:
                overlap = rule_words & existing_words
                # Divided by the LARGER keyword set, not the smaller one. Against
                # the smaller set the ratio measures "how much of the shorter rule
                # the longer one covers", so a two-word rule whose words both
                # appear in a nineteen-word rule scores 100% and DELETES it —
                # detailed guidance destroyed by a terse near-truism. Against the
                # larger set the score is symmetric, and reaching 0.5 requires the
                # two rules to genuinely be about the same thing.
                ratio = len(overlap) / max(len(rule_words), len(existing_words))
                if ratio >= 0.5:
                    if may_retire_existing:
                        deferred_supersedes.append(
                            (
                                existing["key"],
                                existing_report,
                                existing["value_json"],
                                "%.0f%% keyword overlap" % (ratio * 100),
                            )
                        )
                    continue

        # Semantic dedup via embeddings (use stored embedding when available)
        if similarity is not None:
            existing_emb_blob = existing.get("embedding")
            row_blob = None
            if (
                existing_emb_blob
                and isinstance(existing_emb_blob, bytes)
                and len(existing_emb_blob) >= 4
            ):
                row_blob = existing_emb_blob
            elif (
                self.embed_fn
                and not defer_backfills
                and backfills_done < _MAX_BACKFILLS_PER_CALL
                and not existing.get("_authority_prepass_embed_failed")
            ):
                # Lazy backfill: compute embedding for legacy lessons (count even on failure)
                # Sampled BEFORE the embed: _try_embed returns None when a swap
                # spanned its own call, so this value is the blob's true space.
                # Sampling after it returns would tag an old blob with the new
                # generation and the flush check would wave it through.
                # A row the authority pre-pass already attempted is skipped:
                # the pre-pass memoized a successful blob onto the row dict
                # (so this branch is not reached) and marked a failure, so
                # every row is attempted at most once per call, exactly as
                # before the pre-pass existed.
                backfill_generation = self._space_generation
                # Embed the canonical rule text (matching write_lesson), not
                # the display rendering -- the vector must live in the same
                # space as the query vectors it is compared against.
                existing_emb = self._try_embed(
                    _lessons._lesson_embed_text(json.loads(existing["value_json"])),
                    PRIORITY_BULK,
                )
                if existing_emb:
                    row_blob = struct.pack(f"{len(existing_emb)}f", *existing_emb)
                    pending_backfills.append(
                        (
                            row_blob,
                            existing["key"],
                            backfill_generation,
                            existing["value_json"],
                            existing_emb,
                        )
                    )
                backfills_done += 1
            if row_blob is not None:
                sim = similarity({"embedding": row_blob})
                if sim > 0.85:
                    # Newest wins, matching the substring and topic-overlap
                    # branches above -- both supersede the stored row
                    # unconditionally. A length tie-break on
                    # ``len(rule) > len(existing_text)`` would DROP the
                    # submission when it loses, making character count
                    # decide which of two near-identical rules is current.
                    # A correction is frequently SHORTER than the stale
                    # lesson it corrects (a retracted claim collapses to a
                    # one-line "not installed"), so the losing case landed
                    # exactly on corrections -- and left the stale lesson in
                    # effect, the one outcome that actively misleads the
                    # agent rather than merely losing information.
                    #
                    # No authority check HERE, by construction: the
                    # non-mutating pre-pass above already returned DEDUPED
                    # if any purely-semantic row outranks the write. Like
                    # both lexical branches, this one only QUEUES its
                    # supersede -- a later row can still decline the write
                    # via ``substring_covered``, and a declined write
                    # executes no deletion at all.
                    if may_retire_existing:
                        deferred_supersedes.append(
                            (
                                existing["key"],
                                existing_report,
                                existing["value_json"],
                                "%.2f cosine" % sim,
                            )
                        )
                    continue

    # No pending backfill is dropped for a queued row. The queue is a list of
    # CANDIDATE deletions until the write commits, so discarding their vectors
    # here would cost the surviving rows their embeddings on exactly the paths
    # that delete nothing. A vector written to a row this call then retires is
    # one spent UPDATE on a tombstone.

    _flush_backfills()

    # ``repo_scope`` mirrors onto the ``scope`` carve axis when the caller named
    # no scope facet of its own. ``value_json`` stays authoritative -- the lesson
    # reader keeps reading it -- so this is an index projection, never a second
    # source of truth for what a lesson is scoped to.
    if repo_scope and (facets is None or not facets.scope):
        # ``replace`` rather than a field-by-field rebuild: naming the four other
        # axes here would silently DROP any axis added to MemoryFacets later.
        prior: memory_schema.MemoryFacets = (
            facets if facets is not None else memory_schema.MemoryFacets()
        )
        facets = dataclasses.replace(prior, scope=repo_scope)
    err = self.set_semantic(key, value, confidence, source, facets=facets)
    if err is not None:
        # Nothing was deleted: the scan only QUEUED its supersedes, and the
        # queue drains below this return. So a value the store rejects costs
        # no stored row, and ``superseded`` is empty here by construction.
        return LessonWriteResult(LessonWriteOutcome.REFUSED, err[0].value, tuple(superseded))
    # THE WRITE HAS LANDED -- drain the supersede queue. Every route that
    # declines the submission returned above, so reaching this line is what
    # makes each queued deletion a genuine replacement rather than a loss.
    #
    # Each row is deleted only while its body is still the one the scan READ, and
    # the comparison is the delete statement's own, so no writer can land between
    # checking and tombstoning. Nothing serializes this method for its whole
    # length and ``_lesson_key`` keys on the rule and scope alone, so a competing
    # write CAN reach a queued key inside this window -- a user enriching the very
    # rule being retired lands on it exactly -- and a second process on the same
    # database file is ordered by no lock this process holds. The guard decides
    # the write's OWN key too: ``set_semantic`` has committed by here, so a queued
    # row sharing that key fails it, and no separate same-key check is needed.
    #
    # A supersede is LOGGED here because this is where one happens; the scan only
    # nominates rows. The scan's log lines and this drain's carry keys and labels
    # (category, source, reason, a count), never row text: a lesson can hold
    # whatever the user tells the agent (credentials, paths, names), and a log
    # record can outlive the turn, on disk and in the dashboard's log stream.
    # Both keys are row keys, so an operator can join a supersede line to the
    # retired row's tombstone and audit event, and by its "so far" count to the
    # matching ``superseded`` entry in the result. Logging the keys rather than a
    # fresh digest is deliberate: a newly computed hash would correlate with
    # nothing. No log line here hashes anything; a weak-hash finding belongs at
    # ``_lesson_slug``'s digest, not at a line that logs a key.
    #
    # The attribution is passed INTO delete_semantic (winning key + reason), so
    # the tombstone's record status and audit event record it durably in the
    # database rather than only in the ``learn_add`` reply, which a
    # timed-out-but-committed write can lose. The DB event/status write is that
    # durable trace; the line below logs at WARNING so the same deletion is
    # visible to a human tailing gateway.log (a default install records WARNING+
    # to disk), not only to a query against the audit table.
    for d_key, d_report, d_body, d_reason in deferred_supersedes:
        if not self.delete_semantic(
            d_key,
            source,
            expect_value_json=d_body,
            superseded_by=key,
            supersede_reason=d_reason,
        ):
            logger.info(
                "Lesson supersede skipped: %s changed or went while %s was written",
                d_key,
                key,
            )
            continue
        logger.warning(
            "Lesson supersede: %s replaces %s [%s] (%s), %d so far",
            key,
            d_key,
            category,
            d_reason,
            len(superseded) + 1,
        )
        superseded.append(d_report)

    if rule_emb:
        emb_blob = struct.pack(f"{len(rule_emb)}f", *rule_emb)
        with self._vector_commit(rule_emb, best_effort=True) as current:
            if not current or self._space_generation != lesson_embed_generation:
                # Swap landed mid-write: leave the vector NULL for the backfill
                # instead of persisting one from the previous space. The lesson
                # row itself is already written.
                logger.debug("Dropping a lesson embedding produced in a previous space")
            else:
                # Body equality pins the actual embedding input. A later edit,
                # tombstone or completed backfill must win this tail race.
                # ensure_ascii=False matches the representation set_semantic
                # persists; an escaped dump would match no row for a
                # non-ASCII lesson, leaving its embedding NULL.
                self.db.execute(
                    f"UPDATE {self._sem_rel} SET embedding = ? WHERE key = ? "
                    f"AND value_json = ? AND embedding IS NULL AND is_deleted = 0"
                    f"{self._sem_guard}",
                    (emb_blob, key, json.dumps(value, ensure_ascii=False)),
                )
    # ``matched`` is pass 1's verdict: it rewrote an EXISTING row under that row's
    # own key to attach a clause, which is an enrichment. Every other route here
    # wrote a new row under the submitted rule's key -- including the ones that
    # superseded an older row first, since the caller's lesson did not exist under
    # this key before. Same two words the JSONL store uses for the same events.
    return LessonWriteResult(
        LessonWriteOutcome.ENRICHED if matched else LessonWriteOutcome.INSERTED,
        superseded=tuple(superseded),
        applies=effective_applies,
    )


def embed_lesson(self: VectorMemoryStore, rule: str) -> list[float] | None:
    """Embed a lesson rule once for reuse across dedup passes.

    Synchronous (performs a blocking embed); callers on an event loop
    should wrap this in ``asyncio.to_thread()``.
    """
    return self._try_embed(rule) if self.embed_fn else None
