"""The lesson read model: stored-value codec, ranking, rendering and scope.

A lesson is a ``lesson.*`` semantic row whose value is either the mapping
``{"rule", "category", "negative", ...}`` or a legacy in-band string; the codec
below reads both without guessing. The readers here decide which rows count as
lesson population, which reach a prompt and in which tier and order, which rows
a contradiction sweep or a delete may address, and they fetch through the
store's locked helpers. The write path, ``VectorMemoryStore.write_lesson``, stays
on the store and uses the same codec.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.embeddings import PRIORITY_INTERACTIVE
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ON_TOPIC,
    LESSON_APPLIES_UNSTATED,
    LESSON_APPLIES_VALUES,
    contains_volatile_lesson_fact,
    render_lesson_tier,
    render_withheld_tier,
    tighter_lesson_budget,
)
from kiro_crew.project_scope import (
    canonical_scope,
    project_scope_satisfied,
    scope_is_admissible,
    scope_selector_is_inadmissible,
)
from kiro_crew.vector_memory_runtime.embedding import _RecallQuery
from kiro_crew.vector_memory_runtime.text_scoring import (
    _hybrid_score,
    _keyword_score,
    _row_stem_tokens_for_scan,
    _stem_one,
    _stem_words,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.vector_memory import LessonWriteResult, VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


# Joins a lesson's rule to its NOT-clause in a legacy single-value row, and renders
# a mapping-shaped one for display. Reads need it as well as writes: it is what
# tells "<rule>" apart from "<rule><sep><negative>" when deciding whether a bare
# re-submit would strip a clause that is already stored.
_LESSON_NEGATIVE_SEP = " — NOT: "


def _lesson_fields(decoded: object) -> tuple[str, str | None] | None:
    """Extract ``(rule, negative)`` from a mapping-shaped lesson value.

    The mapping shape — ``{"rule": ..., "category": ..., "negative": ...}`` — is
    the one place a lesson's two halves exist as separate fields, so reading them
    back needs no parsing and cannot be confused by a rule whose own text contains
    ``_LESSON_NEGATIVE_SEP``. Returns ``None`` when *decoded* is not that shape
    (strings are the legacy in-band form and are read by ``_split_stored``;
    anything else is not lesson data). A blank or non-string ``negative`` is
    normalized to ``None`` — mirroring ``write_lesson``'s own input normalization,
    so a round-trip compares equal to what was submitted.
    """
    if not isinstance(decoded, dict):
        return None
    rule = decoded.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        return None
    negative = decoded.get("negative")
    if not isinstance(negative, str) or not negative.strip():
        negative = None
    else:
        negative = negative.strip()
    return rule.strip(), negative


def _lesson_scope(decoded: object) -> str | None:
    """Extract ``repo_scope`` from a lesson value, or None when unscoped.

    Only the mapping shape can carry a scope. A legacy string row has nowhere to
    put one, so it reads as unscoped and keeps applying everywhere -- which is
    what an existing store expects. A blank or non-string value normalizes to
    None, mirroring the write path, so a round-trip compares equal.
    """
    if not isinstance(decoded, dict):
        return None
    scope = decoded.get("repo_scope")
    if not isinstance(scope, str) or not scope.strip():
        return None
    return scope.strip()


def _lesson_scope_unusable(decoded: object) -> bool:
    """Whether a lesson carries a ``repo_scope`` that is PRESENT but unusable.

    Absent and present-but-broken are different answers and must not collapse.
    Absent means "applies everywhere", which is the correct default. A present
    value that is not a usable string -- a list or a number from an imported or
    hand-edited row -- means "this was meant to be scoped and we cannot tell
    where", so the row is withheld at injection rather than admitted globally.
    Treating it as absent is fail-OPEN: the one direction this gate must never
    take.
    """
    if not isinstance(decoded, dict):
        return False
    if "repo_scope" not in decoded:
        return False
    scope = decoded["repo_scope"]
    if scope is None:
        return False
    # Asks the GATE's own admissibility test rather than carrying a second notion
    # of "usable". A non-blank string is not enough: "." is a string and the gate
    # refuses it, so judging by shape marked it usable, it rendered nothing, and it
    # still counted as stored knowledge -- which silenced the JSONL store and lost
    # the lessons the user saved. Deferring here is what keeps the two in step.
    return not scope_is_admissible(scope)


def _decoded_lesson_value(row: object) -> object:
    """Decode a lesson row's stored value, answering ``None`` on anything malformed.

    Used by the write scan's tier guard, which runs per candidate row. A raise here
    would abort the whole write over one unreadable neighbour, and the guard's
    fail-safe direction is "cannot tell" -> the row reads as UNSTATED, which every
    path treats as a standing rule and therefore protects: a finding cannot retire
    a row this cannot classify, and a standing submission is not declined as
    covered by one. Protecting a row that may be a mere finding costs a duplicate;
    the other direction silently deletes a rule.
    """
    try:
        value = row["value_json"]  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        return None
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _lesson_applies(decoded: object) -> str:
    """Read a lesson's authored tier, or ``unclassified`` when it has none.

    Only the mapping shape can carry a tier. A legacy string row has nowhere to
    put one and reads as unclassified, which is what an existing store expects.
    The vocabulary and the reason the tier is authored rather than derived live in
    ``lesson_validation``; this function is only the mapping-shape reader, the
    same split ``_lesson_scope`` uses for ``repo_scope``.
    """
    if not isinstance(decoded, dict):
        return LESSON_APPLIES_UNSTATED
    applies = decoded.get("applies")
    if not isinstance(applies, str):
        return LESSON_APPLIES_UNSTATED
    normalized = applies.strip().lower()
    return normalized if normalized in LESSON_APPLIES_VALUES else LESSON_APPLIES_UNSTATED


def _lesson_display_text(decoded: object) -> str:
    """Render a decoded lesson value as the prose that goes into the prompt.

    Lessons are stored in two shapes, and only one of them is a string. The
    legacy ``learn_add`` form is ``"<rule>"`` or ``"<rule><sep><negative>"``
    (see ``_LESSON_NEGATIVE_SEP``), while ``write_lesson`` and the onboarding
    import store a mapping ``{"rule": ..., "category": ..., "negative": ...}``,
    which keeps the two halves apart without in-band escaping. Interpolating the
    decoded value directly therefore pasted a Python ``dict`` repr into the system
    prompt for every imported lesson: the model was handed ``{'rule': 'Prefer dark
    mode', 'category': 'preference', 'negative': None}`` instead of the rule,
    spending tokens on punctuation and field names while burying the instruction it
    is supposed to follow.

    Stored bytes are read as-is: legacy string rows are returned unchanged (no
    migration runs, and ``_split_stored`` still parses them where enrichment
    needs the halves), while mapping rows are recomposed with the separator only
    for DISPLAY -- the fields, not this rendering, remain the source of truth.

    An unrecognized shape yields ``""`` and is skipped by the caller rather than
    being stringified as a guess. This runs while a session's prompt is being
    built, where a raise costs the whole turn, so every branch has to produce a
    string without trusting the value's type.
    """
    if isinstance(decoded, str):
        return decoded.strip()
    if isinstance(decoded, dict):
        rule = decoded.get("rule")
        if not isinstance(rule, str) or not rule.strip():
            return ""
        negative = decoded.get("negative")
        if isinstance(negative, str) and negative.strip():
            return f"{rule.strip()}{_LESSON_NEGATIVE_SEP}{negative.strip()}"
        return rule.strip()
    return ""


def _lesson_fields_for_row(decoded: object, key: str) -> tuple[str, str | None] | None:
    """Extract fields from either stored lesson shape without guessing.

    Mapping rows already separate the rule and NOT-clause. Legacy strings store
    them in-band, where a rule can itself contain the separator. Try each boundary
    through ``_split_stored``; that helper accepts one only when the row's key proves
    the prefix is the original rule. A row keyed by another writer stays one rule,
    preserving the old fail-safe behavior for ambiguous imports and migrations.
    """
    fields = _lesson_fields(decoded)
    if fields is not None:
        return fields
    if not isinstance(decoded, str) or not decoded.strip():
        return None
    text = decoded.strip()
    idx = text.find(_LESSON_NEGATIVE_SEP)
    while idx != -1:
        candidate = text[:idx].strip()
        if candidate:
            base, stored_clause = _split_stored(text, candidate.lower(), key)
            if base is not None and stored_clause:
                negative = text[idx + len(_LESSON_NEGATIVE_SEP) :].strip() or None
                return base, negative
        idx = text.find(_LESSON_NEGATIVE_SEP, idx + 1)
    return text, None


def _renderable_lesson_text(decoded: object, key: str) -> str:
    """Return prompt text only for a row that may count as lesson population.

    Population and rendering must reject the same malformed, withheld, and
    volatile legacy rows. Otherwise a row that renders nothing can still make the
    vector store authoritative and silently suppress valid JSONL lessons.
    Repository scope is applied later because a valid out-of-project row still
    proves that the vector store is populated. The row key safely separates a
    legacy string's rule from its in-band NOT-clause before validation.
    """
    text = _lesson_display_text(decoded)
    if not text:
        return ""
    fields = _lesson_fields_for_row(decoded, key)
    if fields is None:
        return ""
    rule, negative = fields
    if contains_volatile_lesson_fact(rule, negative):
        return ""
    if _lesson_scope_unusable(decoded):
        return ""
    return text


def _lesson_embed_text(decoded: object) -> str:
    """The text a lesson's embedding is computed FROM, matching write_lesson.

    The write path embeds the bare ``rule`` (never the NOT-clause), so every
    vector that participates in semantic similarity must come from the same
    input space: a mapping row embeds its ``rule`` field. A legacy string row
    cannot be split reliably (that ambiguity is what the mapping shape fixes),
    so it embeds the stored text as-is -- the best available approximation and
    what those rows have always embedded.
    """
    if isinstance(decoded, dict):
        fields = _lesson_fields(decoded)
        if fields is not None:
            return fields[0]
    return _lesson_display_text(decoded)


def _split_stored(existing_val: str, rule_norm: str, existing_key: str) -> tuple[str | None, bool]:
    """Split a stored lesson value against a normalized rule.

    Returns ``(base, stored_clause)``: the stored spelling of the rule, and whether
    a NOT-clause follows it. ``(None, False)`` means this row is not that rule.

    The separator is stored IN-BAND and unescaped, so the value alone is ambiguous:
    ``A — NOT: B`` is either rule ``A`` with clause ``B``, or a bare rule whose text
    happens to contain the separator. No amount of text parsing settles that -- both
    readings are valid, and picking either one by itself loses data in the other case
    (silently dropping a clause update one way, OVERWRITING an unrelated rule the
    other).

    The row itself carries the answer: the key is ``md5(rule)`` taken at write time,
    in the rule's stored casing. So a candidate prefix is the rule only when it
    hashes to this row's key. That is exact rather than heuristic, and it is why
    every separator boundary can be tried safely.

    Rows keyed some other way -- the onboarding import uses sha256, and legacy
    migrations set their own keys -- match only on the whole value. For those a
    case-variant re-submit onto an EXISTING clause will not enrich. That is a missed
    enrichment, never an overwrite: the ambiguous branch always declines.

    Case-insensitivity here is ``lower()``, not ``casefold()`` -- see write_lesson for
    why. ``casefold()``'s ß-to-ss expansion conflates "Maße" with "Masse", which would
    make this function confidently return the WRONG row's spelling as ``base``.
    """
    from kiro_crew import vector_memory  # circular import: the key digest stays in the facade

    stripped = existing_val.strip()
    if stripped.lower() == rule_norm:
        return stripped, False  # the whole value is the rule; no clause
    slug = existing_key.split(".", 1)[-1]
    idx = stripped.find(_LESSON_NEGATIVE_SEP)
    while idx != -1:
        prefix = stripped[:idx].strip()
        # Compare whole prefixes, never a slice at len(rule_norm): lower() can still
        # CHANGE length ("İ" -> "i" + combining dot), so a length-based slice cuts in
        # the wrong place for exactly the case-variant inputs this serves.
        if prefix.lower() == rule_norm and vector_memory._lesson_slug(prefix) == slug:
            return prefix, True  # the key confirms prefix IS the rule
        idx = stripped.find(_LESSON_NEGATIVE_SEP, idx + 1)
    return None, False


def _lesson_row_text(row: dict) -> str | None:
    """The row's value as lesson TEXT, or None when it has no lesson shape.

    set_semantic accepts any object, so an import or a legacy migration can
    leave a list or a rule-less dict under a lesson.* key. str() would render
    a Python repr, and every text comparison in the dedup scan -- the substring
    dedup and the keyword overlap -- would then match against that repr.
    Skipping is the honest reading: it is not lesson text.

    Mapping-shaped rows (write_lesson's own format, and the onboarding
    import's) render through _lesson_embed_text (the rule only, without
    the NOT-clause), so deduplication compares rules on the same basis
    that embedding similarity does — the negative qualifies the rule but
    does not change its identity.
    """
    text = _lesson_embed_text(json.loads(row["value_json"]))
    return text or None


def _lesson_row_report_text(row: dict) -> str | None:
    """The row's value as the text a SUPERSEDE REPORT must name.

    Deliberately NOT ``_lesson_row_text``. That one renders through
    ``_lesson_embed_text``, which returns a mapping row's ``rule`` field
    ALONE -- the NOT-clause is stripped, because dedup has to compare rules
    on the same basis embedding similarity does. Correct for comparing, and
    wrong for reporting: a stored lesson's clause carries its sharpest
    guidance ("prefer ruff -- NOT: for type checking"), so naming only the
    bare rule hands the user back a lesson they cannot restore. The row is a
    tombstone, so there is no second place to read the clause from.

    ``_lesson_display_text`` is the recomposition every other human-facing
    renderer uses (the injected prompt, ``learn list``), so a restored rule
    reads exactly as it did when stored.
    """
    text = _lesson_display_text(json.loads(row["value_json"]))
    return text or None


@dataclass(frozen=True)
class ExactRuleMatch:
    """Pass 1's verdict when a stored row in the submission's scope IS its rule.

    ``result`` is set when the call ends here (a no-op re-submit, a kept stored
    NOT-clause, or an enrichment the store refuses). Otherwise ``key`` and
    ``value`` are the enrichment to write under the EXISTING row's key, and
    ``applies`` is the tier that row will carry: the stored tier for a mapping row
    (the tier is write-once), the submitted one for a legacy string row.
    """

    result: LessonWriteResult | None
    key: str = ""
    value: object = None
    applies: str | None = None


def lesson_rows_in_scope(rows: list[dict], repo_scope: str | None) -> list[dict]:
    """The lesson rows a write in *repo_scope* may dedup against, in stored order.

    Deduplication is SCOPE-LOCAL. A lesson scoped to one repository and a global
    one are different lessons even when their wording is close, so a scoped write
    must never supersede, enrich, or be discarded against a row from another scope.
    Without this the generic dedup rules (substring containment, >50% keyword
    overlap, high cosine similarity) reach across scopes and DELETE guidance the
    submitter never addressed -- writing a repo-scoped rule could retire a global
    one that merely shared most of its significant words.

    A row whose value will not parse is dropped here rather than compared,
    matching what :func:`_lesson_row_text` does with a value that has no lesson
    shape. A row whose scope is present but unusable belongs to NO partition. It is
    withheld at injection, so letting it read as unscoped here would let it dedup
    away a genuine global write: the caller would be told the lesson was saved
    while the only row carrying that rule never reaches a prompt. All three readers
    of this field agree on that.
    """
    in_scope = []
    for row in rows:
        try:
            decoded = json.loads(row["value_json"])
        except (ValueError, TypeError):
            continue
        if _lesson_scope_unusable(decoded):
            continue
        if _lesson_scope(decoded) == repo_scope:
            in_scope.append(row)
    return in_scope


def resolve_exact_rule(
    store: VectorMemoryStore,
    lesson_rows: list[dict],
    *,
    rule: str,
    key: str,
    negative: str | None,
    category: str,
    applies: str | None,
    confidence: float,
    source: str,
) -> ExactRuleMatch | None:
    """Pass 1 of ``write_lesson``: find the stored row that IS this rule, if any.

    Resolving the exact match before the generic dedup rules is what makes the
    outcome independent of scan order: those rules can claim the write on an
    UNRELATED row -- a superset whose text contains the rule -- and doing both in
    one loop let an unrelated superset seen first discard an enrichment already
    selected, dropping the clause on HTTP 200. ``None`` means no stored row is this
    rule, and the caller runs the generic rules.
    """
    from kiro_crew import vector_memory as vm  # circular import: the result types stay public there

    # lower(), deliberately NOT casefold(). casefold() maps ß to ss, which matches
    # "Straße" against "STRASSE" -- but the same mapping makes "Maße" and "Masse"
    # compare EQUAL, and those are different words, so a clause submitted for one
    # attached itself to the other and the intended lesson was never created. The
    # two behaviours are inseparable, so this is a trade: lower() never conflates
    # distinct rules, and its cost is a missed enrichment rather than a corrupted
    # one. Keep both stores on the same function.
    rule_norm = rule.strip().lower()
    effective_applies = applies
    for existing in lesson_rows:
        decoded = json.loads(existing["value_json"])
        fields = _lesson_fields(decoded)
        if fields is not None:
            # Mapping shape: the halves are separate fields, so the stored
            # ``rule`` IS the rule and identifying it needs no key confirmation,
            # whatever key the writer derived (write_lesson uses md5, the
            # onboarding import sha256). This is what lets a re-submit enrich an
            # imported lesson, which the string form could never do safely.
            #
            # Identity is the stored rule TEXT, never the key alone: a row whose
            # key and stored rule disagree would otherwise be claimed by this
            # rule and rewritten, attaching the submitted clause to a different
            # lesson and dropping the submitted rule entirely.
            stored_rule, stored_negative = fields
            if stored_rule.lower() != rule_norm:
                continue
            base = stored_rule
            stored_clause = stored_negative is not None
        elif isinstance(decoded, str):
            existing_val = decoded
            # Key equality FIRST: md5(rule) identifies THIS lesson exactly,
            # whatever the stored value contains. Otherwise defer to
            # _split_stored, which confirms a candidate prefix against the row's
            # own key rather than guessing a reading of the in-band separator.
            if existing["key"] == key:
                legacy_base: str | None = rule.strip()
                stored_clause = existing_val != legacy_base
            else:
                legacy_base, stored_clause = _split_stored(existing_val, rule_norm, existing["key"])
            if legacy_base is None:
                continue
            base = legacy_base
            stored_negative = None  # in-band; only its presence is known
        else:
            continue  # not lesson data (list, rule-less dict, ...)

        if not negative and stored_clause:
            # A BARE re-submit of a rule that already carries a clause. Writing
            # the bare value would delete the stored negative, so keep what is
            # there. This is also what the call did before the fix, so no caller
            # sees a change here.
            logger.info(
                "Keeping the stored NOT-clause on %r; re-submit carried none",
                existing["key"],
            )
            return ExactRuleMatch(
                vm.LessonWriteResult(vm.LessonWriteOutcome.UNCHANGED, "kept_stored_clause")
            )
        if fields is not None:
            # Mapping row: a re-submit that changes nothing the fields express
            # is a no-op. Category is effectively WRITE-ONCE here: it is not
            # compared or rewritten on enrichment, because the intent of a
            # re-submit-with-clause is "attach the clause", not "recategorize"
            # (correcting a category means delete + re-add). The string form
            # never stored a category for anything to have depended on.
            if negative == stored_negative:
                return ExactRuleMatch(vm.LessonWriteResult(vm.LessonWriteOutcome.UNCHANGED))
            stored_category = decoded.get("category")
            enriched: dict[str, object] = {
                "rule": stored_rule,
                "category": stored_category if isinstance(stored_category, str) else category,
                "negative": negative,
            }
            # The scope is WRITE-ONCE for the same reason the category is: the
            # intent of a re-submit-with-clause is "attach the clause", not
            # "re-scope". Carrying the STORED value forward means enrichment can
            # never strip a scope, and re-scoping is a delete + re-add.
            stored_scope = _lesson_scope(decoded)
            if stored_scope:
                enriched["repo_scope"] = stored_scope
            # The tier is WRITE-ONCE for exactly the same reason, and carrying
            # the STORED value forward is what keeps enrichment from silently
            # demoting a standing rule to a past finding. Re-tiering is a
            # delete + re-add, like re-scoping and recategorizing.
            stored_applies = _lesson_applies(decoded)
            if stored_applies != LESSON_APPLIES_UNSTATED:
                enriched["applies"] = stored_applies
                effective_applies = stored_applies
            else:
                effective_applies = None
            target: object = enriched
        else:
            # Legacy string row. Recompose from the STORED base so a
            # case-variant re-submit attaches its clause without silently
            # re-casing the rule. A byte-identical re-submit stays a no-op (the
            # row is not churned into the new shape); an actual enrichment
            # rewrites it as a mapping, upgrading the row in place.
            target_text = base if not negative else f"{base}{_LESSON_NEGATIVE_SEP}{negative}"
            if target_text == existing_val:
                return ExactRuleMatch(vm.LessonWriteResult(vm.LessonWriteOutcome.UNCHANGED))
            target = {"rule": base, "category": category, "negative": negative}
        # The caller's preflight validated the value built from the SUBMITTED rule;
        # this one differs, so validate what is actually written.
        enrich_reject = store.validate_semantic(existing["key"], target, confidence, source)
        if enrich_reject is not None:
            return ExactRuleMatch(
                vm.LessonWriteResult(vm.LessonWriteOutcome.REFUSED, enrich_reject[0].value)
            )
        # Write back under the EXISTING key -- a case-variant would otherwise
        # insert a second row for the same lesson under a different md5. The
        # caller's shared tail does the write.
        return ExactRuleMatch(None, existing["key"], target, effective_applies)
    return None


def tier_permissions(existing: dict, applies: str | None) -> tuple[bool, bool]:
    """``(may_retire_existing, covering_row_arrives_less_often)`` for one scanned row.

    *existing* is the stored row the dedup scan is looking at and *applies* the
    submission's tier; the two answers gate the scan's deletions and its declines.
    """
    # A finding must never retire a standing rule. The scan's three delete branches
    # decide on text alone, so a longer `on_topic` write that merely
    # OVERLAPS an authored `always` rule tombstones it and leaves only the
    # topic-scoped row -- the same durability loss the JSONL prune is ordered
    # to avoid, and worse here because a tombstone is silent and every read
    # path filters it. Asymmetric on purpose: an `always` write may still
    # retire an `on_topic` row, because promoting guidance to a standing rule
    # is the direction the user is asking for.
    #
    # An `on_topic` write may retire ONLY another `on_topic` row. Comparing
    # against `always` alone leaves every UNSTATED row unprotected, which is
    # the inconsistency that matters most in practice: an unstated row is
    # served AS a standing rule at injection, and on an install whose lessons
    # predate this field every row is unstated -- so the narrow form protects
    # nothing on exactly the stores that hold the user's real corrections.
    # Read-side and write-side must classify a row the same way.
    existing_applies = _lesson_applies(_decoded_lesson_value(existing))
    existing_is_finding = existing_applies == LESSON_APPLIES_ON_TOPIC
    submission_is_standing = applies != LESSON_APPLIES_ON_TOPIC
    may_retire_existing = submission_is_standing or existing_is_finding

    # The DECLINE needs the mirror of that rule, for the same reason. A
    # submission is "already covered" only while the covering row arrives at
    # least as often as the submission would, and tiering is what makes that
    # conditional: a covering `on_topic` row arrives only when the request is
    # about it, so declining a STANDING submission contained in one drops the
    # rule from every unrelated turn and reports `substring_covered` for a
    # row that does not cover it there. A re-submit is declined identically,
    # so nothing recovers it. In that one direction the submission is stored
    # instead, which can leave the standing rule beside the longer finding
    # that contains it -- the price of not silently refusing to create the
    # rule, and the safe side of it.
    covering_row_arrives_less_often = submission_is_standing and existing_is_finding
    return may_retire_existing, covering_row_arrives_less_often


def lesson_keywords(text: str) -> set[str]:
    """Extract significant words from a lesson rule, ignoring stop words."""
    stop = {
        "always",
        "never",
        "use",
        "do",
        "dont",
        "don't",
        "the",
        "a",
        "an",
        "to",
        "in",
        "for",
        "and",
        "or",
        "not",
        "is",
        "it",
        "my",
        "i",
        "me",
        "should",
        "must",
        "that",
        "this",
        "with",
        "be",
        "of",
        "on",
        "no",
        "yes",
    }
    return {w for w in re.split(r"\W+", text) if len(w) > 2 and w not in stop}


def find_contradiction_candidates(
    store: VectorMemoryStore,
    rule: str,
    threshold_low: float = 0.4,
    threshold_high: float = 0.85,
    rule_emb: list[float] | None = None,
    repo_scope: str | None = None,
) -> list[dict]:
    """Find lessons related to rule but not caught by standard dedup.

    Returns lessons with cosine similarity in [threshold_low, threshold_high)
    — candidates that may contradict the new rule. Pass ``rule_emb`` to reuse
    an embedding already computed by the caller and avoid a second blocking
    embed of the identical text.

    Candidates are SCOPE-LOCAL: only rows whose stored ``repo_scope`` equals
    *repo_scope* are considered. Superseding resolves a contradiction by
    DELETING the losing row, and a repository-scoped rule that contradicts a
    global one inside its own tree does not contradict it anywhere else --
    sweeping across scopes would retire the global rule for every other
    repository on the strength of one repo's exception.

    Each row carries its authored ``applies`` tier. The caller refuses to let
    a past finding retire a standing or untiered rule, and it can only apply
    that refusal to a row whose tier it can read -- omitting the tier here
    made every row read as untiered, which turned that narrowing into a
    blanket no-op for findings rather than a guard.
    """
    if rule_emb is None:
        rule_emb = store._try_embed(rule) if store.embed_fn else None
    if not rule_emb:
        return []
    # Builds the query-side work (vector + its norm) once for the whole scan,
    # same reasoning as _rank_lessons / get_semantic_context. A row with no
    # stored embedding, or one at a different dimensionality, scores 0.0 —
    # which threshold_low's default of 0.4 already excludes without an
    # explicit skip.
    similarity = store._stored_similarity_scorer(rule_emb)
    candidates = []
    for existing in store.get_lessons():
        sim = similarity(existing)
        if threshold_low <= sim < threshold_high:
            try:
                decoded = json.loads(existing["value_json"])
            except (ValueError, TypeError):
                continue
            if _lesson_scope_unusable(decoded):
                continue
            if _lesson_scope(decoded) != repo_scope:
                continue
            # Rendered text, not str(): a mapping-shaped row would otherwise
            # hand its Python repr to the contradiction prompt as the "rule".
            existing_val = _lesson_display_text(decoded)
            if not existing_val:
                continue
            # The tier travels WITH the candidate. The sweep's caller refuses to
            # let a finding retire a standing or legacy rule, and it can only
            # apply that rule to a row whose tier it can read -- a candidate
            # shaped {key, rule, similarity} reads as unstated for every row,
            # which turns "narrow the sweep" into "disable the sweep" for every
            # on_topic write.
            #
            # The stored BODY travels with it too, exactly as read here. The
            # sweep's delete runs minutes later, after a per-candidate LLM
            # verdict, so this row is a write-time snapshot: ``_lesson_key``
            # keys on rule text plus scope alone, so re-tiering that rule in
            # the meantime (a delete plus a re-add, the documented way to
            # change a tier) puts a DIFFERENT row under the same key. An
            # unconditional delete then tombstones the replacement -- and if
            # the replacement is the `always` tier, it destroys exactly the
            # row the filter above exists to protect. Handing this body back
            # as ``expect_value_json`` makes the delete compare-and-delete,
            # the same guard the inline dedup pass already applies.
            candidates.append(
                {
                    "key": existing["key"],
                    "rule": existing_val,
                    "similarity": sim,
                    "applies": _lesson_applies(decoded),
                    "value_json": existing["value_json"],
                }
            )
    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates[:5]


def has_any_lesson(store: VectorMemoryStore) -> bool:
    """Whether any active row decodes to RENDERABLE lesson data, ignoring scope.

    Distinguishes "this store is not populated yet" from "this store is
    populated but nothing is in scope for this project". Those look identical
    in a rendered context block and need opposite handling: the first means the
    JSONL store is still the authority, the second means this store already
    answered and the JSONL store must stay silent.

    A ``lesson.*`` key is not sufficient evidence. ``set_semantic`` accepts any
    object, so an import or a legacy migration can leave a list, a rule-less
    dict, malformed scope, or volatile pre-boundary row under one. Every
    renderer skips those rows. Counting one as population would silence the
    JSONL store while nothing renders, so saved corrections would vanish. The
    shared predicate keeps this authority check aligned with rendering.

    Selects only ``key`` and ``value_json``, never ``SELECT *``: reading every
    embedding blob is the duplicate-SELECT cost the rendering path was written
    to avoid. The key proves whether a legacy in-band separator marks a clause.
    """
    rows = store._fetch_all_locked(
        "SELECT key, value_json FROM semantic_memory "
        "WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
    )
    for row in rows:
        try:
            decoded = json.loads(row["value_json"])
        except (ValueError, TypeError):
            continue
        if _renderable_lesson_text(decoded, row["key"]):
            return True
    return False


def get_lessons(store: VectorMemoryStore, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Return lesson.* entries ordered by most recently updated.

    ``offset`` skips that many of the NEWEST rows and is honoured only with
    a positive ``limit``: it exists so a paging reader (``GET /api/lessons``)
    can walk back through the population one bounded window at a time
    without materializing the rows it skips. The unbounded read has nothing
    to page and ignores it.
    """
    sql = (
        "SELECT * FROM semantic_memory "
        "WHERE is_deleted = 0 AND key LIKE 'lesson.%' "
        "ORDER BY updated_at DESC"
    )
    # On the same concurrent context-injection path as get_semantic_context
    # (get_lessons_context runs on executor threads while lesson writes are
    # offloaded to workers), so the fetch must be serialized on the shared
    # connection. _db_lock is reentrant, so callers that already hold it
    # remain safe.
    if limit is not None and limit > 0:
        sql += " LIMIT ? OFFSET ?"
        rows = store._fetch_all_locked(sql, (limit, max(0, offset)))
    else:
        # Unbounded: the whole lesson population, which is what the
        # _stored_similarity_scorer callers (_rank_lessons,
        # find_contradiction_candidates) score over — the other half of
        # the whole-population read volume. The LIMIT branch above is bounded and
        # so is not a population scan.
        rows = store._fetch_all_locked(sql, scan="semantic")
    return [dict(r) for r in rows]


def count_lessons(store: VectorMemoryStore) -> int:
    """Return the number of live lessons without materializing them.

    ``get_lessons()`` returns full row dicts (including embedding blobs);
    callers that only need the COUNT (the status paths poll it every few
    seconds per client) must not pull every lesson row into memory just to
    ``len()`` it. Same predicate and ``_db_lock`` serialization as
    ``get_lessons``, so it is safe from executor threads and the loop
    alike and always agrees with ``len(get_lessons())``.
    """
    rows = store._fetch_all_locked(
        "SELECT COUNT(*) AS n FROM semantic_memory WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
    )
    return int(rows[0]["n"]) if rows else 0


def has_any_decodable_lesson(store: VectorMemoryStore) -> bool:
    """Whether any active ``lesson.*`` row holds JSON that decodes at all.

    The tier-authority test for the lessons LIST (``GET /api/lessons``),
    which is looser than ``has_any_lesson()`` on purpose: the list keeps
    every row that decodes -- a legacy string, a rule-less mapping, a
    volatile pre-boundary row -- rendered through ``str()`` and marked
    withheld, because this list is the only surface that can show such a
    row so it stays deletable. The list drops only a row whose stored JSON
    does not decode, so a store holding nothing but those rows has nothing
    this list can answer with, and the JSONL tier must stay the authority.
    Selects only ``value_json``, never the embedding blobs, and stops at the
    first row that decodes.
    """
    rows = store._fetch_all_locked(
        "SELECT value_json FROM semantic_memory WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
    )
    for row in rows:
        try:
            json.loads(row["value_json"])
        except (ValueError, TypeError):
            continue
        return True
    return False


def delete_lesson(
    store: VectorMemoryStore,
    rule_substring: str,
    repo_scope: str | None = None,
    *,
    exact: bool = False,
) -> bool:
    """Delete lessons whose value contains rule_substring.

    Substring matching on the rule text is deliberate: a user targets a
    lesson by a fragment of its rule rather than retyping the whole thing.
    *exact* narrows the text match to the whole rendered lesson text
    (case-insensitive, surrounding whitespace ignored): a caller that holds
    the full text -- a table row's Delete button -- names ONE row, where the
    substring path would also take every longer rule containing it. The
    scope selector applies identically in both modes.
    A lesson's identity is the pair ``(rule, repo_scope)`` -- the scope is
    folded into the semantic key so a scoped and a global lesson sharing
    rule text are two distinct rows, and the selector decides which of
    them a delete reaches. When *repo_scope* is None (the default) scope
    stays out of the match and every substring hit is deleted. When it is
    given, a row is deleted only when it ALSO carries that scope --
    compared canonically on both sides, so the two never disagree over
    trailing-slash / backslash forms and the canonical form of an empty
    selector targets the unscoped (global) rows specifically. A nonempty
    selector the write surface would refuse -- a bare ``/``, an absolute
    path, a dot segment -- is refused with :class:`ValueError` rather than
    canonically folded onto rows the caller never named. A STORED scope
    that is present but unusable marks a scoped-but-broken row, which the
    injection gate withholds; a scope-selective delete never claims such a
    row, and the unselective (absent) path is what removes it.
    """
    if repo_scope is not None and scope_selector_is_inadmissible(repo_scope):
        raise ValueError(f"repo_scope does not name a usable scope: {repo_scope!r}")
    deleted = False
    scope_selective = repo_scope is not None
    wanted_scope = canonical_scope(repo_scope) if scope_selective else None
    wanted_text = rule_substring.lower().strip()
    for e in store.get_lessons():
        val = json.loads(e["value_json"])
        # Match against the rendered lesson text so a mapping-shaped row is
        # matched on its rule/clause, not on its repr (which would let a
        # substring like "category" delete every imported lesson). Rows with
        # no lesson shape fall back to str() so junk rows stay deletable.
        text = _lesson_display_text(val) or str(val)
        if exact:
            if text.lower().strip() != wanted_text:
                continue
        elif rule_substring.lower() not in text.lower():
            continue
        # ``_lesson_scope`` reads a mapping row's scope and normalises a legacy
        # string row (which cannot carry one) to None -- the same reader the
        # injection gate uses, so delete and inject agree on what a row's scope
        # is. Canonicalise it before comparing so the two sides fold identically.
        #
        # A row whose stored scope is PRESENT but unusable (an imported "/",
        # a non-string) is scoped-but-broken, not global: the injection gate
        # withholds it via the same classifier, so a scope-selective delete
        # never claims it -- an all-slash stored scope would otherwise fold
        # to None and be tombstoned by the explicit-global selector. Such a
        # row stays reachable through the unselective (absent) path, which
        # is how junk rows stay deletable.
        if scope_selective:
            if _lesson_scope_unusable(val):
                continue
            if canonical_scope(_lesson_scope(val)) != wanted_scope:
                continue
        store.delete_semantic(e["key"], "user_explicit")
        deleted = True
    return deleted


def get_lessons_context(
    store: VectorMemoryStore,
    query_text: str = "",
    cap: int = 0,
    project_dir: str | Path | None = None,
    *,
    recall_query: _RecallQuery | None = None,
    background: bool = False,
    hard_cap: int = 0,
    directive_budget: int = 0,
    experience_budget: int = 0,
) -> str:
    """Format lessons for prompt injection, most relevant first.

    Lessons are ranked against *query_text* using the same hybrid
    vector + keyword score as :meth:`get_semantic_context` when the query has a
    vector, and by rarity-weighted word overlap when it has none (see
    ``rank_lessons``), then emitted until *cap* characters are used. Ranking is
    relevance-only — neither ``source`` nor ``confidence`` contributes — so an
    unrelated user-taught rule cannot displace a relevant inferred one.

    Args:
        query_text: Request to rank against. Empty keeps recency order for
            explicit recall, never as filler in background admission.
        background: Preserve all eligible in-scope rules, without query
            ranking beyond a lexical pass. Below the ``hard_cap`` ceiling the
            block is returned complete; only when the full set exceeds that
            ceiling does admission fall back to the ordinary lessons budget
            ``cap``. Extraction source does not establish optionality.
        cap: Character budget. In explicit recall it is the sole limit. In
            background admission it is not consulted below the ceiling; above
            the ceiling it becomes the ordinary target (bounded by
            ``hard_cap``). 0 means no ordinary budget, so an overflowing
            background block falls back to the ``hard_cap`` ceiling alone.
        hard_cap: Model-safety ceiling for background admission. It is both
            the admission gate -- content at or below it is returned
            byte-identical, dropping no rule -- and the upper bound the
            effective overflow budget is never allowed to exceed. 0 means no
            ceiling (unbounded).
        project_dir: The session's active project, used only by the
            ``repo_scope`` gate. Omitting it withholds every scoped lesson.
    """
    # Scope is applied BEFORE the counts are taken, so a lesson withheld as
    # out-of-scope is not reported as "omitted" -- omitted means "did not fit
    # the budget", and conflating the two would tell the model that rules it
    # should never see are being kept from it for space.
    with store._db_lock:
        store._check_recall_query(recall_query)
        lesson_rows = store._eligible_rows(store.get_lessons(), "directive")
    entries = _renderable_entries(lesson_rows, project_dir)
    if not entries:
        return ""
    if background:
        # Two tiers, two budgets, and the split is AUTHORED rather than
        # inferred (see ``_lesson_applies``). Standing rules -- plus every row
        # whose author named no tier -- go in the directive block, which is
        # the one the prompt tells the agent to always follow. Rows the
        # author marked as past findings go in a separate, much smaller
        # experience block, because carrying every past finding into an
        # unrelated conversation is what made a bare "hi" expensive.
        #
        # Both budgets are supplied by the caller and are window-INDEPENDENT.
        # Deriving one shared allowance from the model window instead makes
        # startup injection scale with the window, so moving from a 200K to a
        # 1M model multiplies it about fivefold for a user who changed nothing.
        #
        # Ranking still puts rules relevant to this request first, lexically
        # only: startup never spends an embedding inference.
        ranked = (
            store._rank_lessons(
                entries,
                query_text,
                recall_query=recall_query or _RecallQuery(None, None, None),
            )
            if query_text
            else entries
        )
        # Within the rule tier, AUTHORED directives are ordered ahead of
        # unclassified rows. Unclassified is this reader's safe-direction guess
        # about a row whose author never said what it was; an authored directive
        # is the user stating outright that it must always apply. Ranked
        # together, a pile of untagged rows displaces exactly the rules the user
        # was most explicit about.
        authored: list[tuple[dict, str]] = []
        untagged: list[tuple[dict, str]] = []
        experiences: list[tuple[dict, str]] = []
        for entry in ranked:
            applies = _lesson_applies(json.loads(entry[0]["value_json"]))
            if applies == LESSON_APPLIES_ON_TOPIC:
                experiences.append(entry)
            elif applies == LESSON_APPLIES_UNSTATED:
                untagged.append(entry)
            else:
                authored.append(entry)
        directives = authored + untagged
        unclassified = len(untagged)
        # The model-safety ceiling bounds the two blocks TOGETHER, and the
        # directive block is served first: when room is short, a past finding
        # yields to a standing rule rather than the two sharing the shortfall.
        directive_tier = {
            "header": (
                "[Learned corrections — retained rules from past mistakes.\n"
                "Follow explicit user rules; stored inferences do not override "
                "the current user.]"
            ),
            "footer": "[End of learned corrections]",
            "omission": (
                "[Context budget: omitted {count} of {total} retained rules above the "
                "{limit}-character rule budget. This is a BUDGET limit, not a judgement "
                "that they stopped applying: read them with learn_list or use "
                "memory_recall.]"
            ),
        }
        if directive_budget:
            directive_room = tighter_lesson_budget(directive_budget, hard_cap)
        else:
            # A caller that names NO tier budget keeps the shipped two-stage
            # contract: the rule block is COMPLETE below the model-safe ceiling
            # whatever ``cap`` says -- the pinned retain-every-eligible-in-scope-
            # rule invariant -- and only an overflow, where that invariant is
            # already unmet, falls back to the ordinary lessons budget. Naming a
            # tier budget is what asks for a window-independent bound instead,
            # so the two mechanisms never both decide one block.
            whole = render_lesson_tier(directives, 0, **directive_tier)[0]
            directive_room = (
                0
                if not hard_cap or len(whole) <= hard_cap
                else tighter_lesson_budget(cap, hard_cap)
            )
        directive_block, directive_omitted = render_lesson_tier(
            directives,
            directive_room,
            **directive_tier,
        )
        # ``max(1, …)`` rather than the bare remainder: ``tighter_lesson_budget``
        # reads 0 as "no limit from this source", so a directive block that
        # consumed the whole ceiling would hand the experience tier an
        # UNBOUNDED budget -- the exact inversion of what no room left means.
        # A budget of 1 fits no lesson and renders the labelled
        # everything-omitted block, which overshoots the ceiling by the notice
        # and is the documented trade: a silent empty block is indistinguishable
        # from "this user has no findings".
        experience_room = tighter_lesson_budget(
            experience_budget,
            max(1, hard_cap - len(directive_block)) if hard_cap else 0,
        )
        if (
            experiences
            and query_text.strip()
            and not store._any_lesson_overlap(experiences, query_text)
        ):
            # Nothing here is about this request, so spend none of the allowance
            # on it. A finding is DEFINED as material worth having when the task
            # touches it; newest-first filler is not a weaker version of that, it
            # is unrelated by construction -- and it never rescued a near-miss
            # either, since it surfaces the NEWEST rows rather than the closest
            # ones. A bare greeting lands here too: it names no topic, so no
            # finding is on it. The frame still renders, which is what turns "you
            # have findings, none matched, go ask" into something the next turn
            # can act on rather than an absence it cannot see.
            experience_block = render_withheld_tier(
                len(experiences),
                header="[Learned experience — past findings]",
                footer="[End of learned experience]",
                notice=(
                    "[Withheld all {total} past findings: none of them share "
                    "wording with this request. They are NOT gone and this is not "
                    "a budget limit -- call memory_recall with a specific "
                    "question, or learn_list, when the task turns out to touch "
                    "one.]"
                ),
            )
            experience_omitted = len(experiences)
        else:
            experience_block, experience_omitted = render_lesson_tier(
                experiences,
                experience_room,
                header=(
                    "[Learned experience — past findings, relevant ones first.\n"
                    "Reference material, not standing rules; call memory_recall for more.]"
                ),
                footer="[End of learned experience]",
                omission=(
                    "[Context budget: omitted {count} of {total} past findings above the "
                    "{limit}-character findings budget. This is a BUDGET limit, not a "
                    "judgement that they stopped applying: call memory_recall or "
                    "learn_list for the rest.]"
                ),
            )
        # Observability without new state: the two blocks are separately
        # labelled in the prompt and each omission notice names its own
        # counts, so what was injected is readable from the prompt itself.
        # A counter stashed on the store would race between two sessions
        # rendering against one instance and could report either one's
        # numbers to the other.
        logger.debug(
            "background lessons: directives=%d/%d chars=%d unclassified=%d "
            "experiences=%d/%d chars=%d",
            len(directives) - directive_omitted,
            len(directives),
            len(directive_block),
            unclassified,
            len(experiences) - experience_omitted,
            len(experiences),
            len(experience_block),
        )
        return directive_block + experience_block
    total = len(entries)
    ranked = (
        store._rank_lessons(entries, query_text, recall_query=recall_query)
        if query_text
        else entries
    )
    order = "most relevant" if query_text else "most recent"

    def render(rows: list[tuple[dict, str]]) -> str:
        header = (
            "[Learned corrections — user-taught rules from past mistakes.\n"
            "ALWAYS follow these. They override default behavior."
        )
        if len(rows) < total:
            header += (
                f"\nShowing {len(rows)} of {total} lessons, {order} first; "
                f"{total - len(rows)} omitted."
            )
        body = "\n".join(f"- {text}" for _, text in rows)
        return f"{header}]\n{body}\n[End of learned corrections]\n"

    if not cap:
        return render(ranked)

    selected: list[tuple[dict, str]] = []
    used = 0
    for entry in ranked:
        size = len(entry[1]) + 3  # "- " prefix and newline
        if selected and used + size > cap:
            # Skip rather than stop: one long lesson high in the ranking
            # must not discard every shorter one behind it that still fits.
            continue
        selected.append(entry)
        used += size
    # The header grows with the counts it reports, so trim to fit rather
    # than reserving a guessed margin. At least one lesson is always kept.
    while len(selected) > 1 and len(render(selected)) > cap:
        selected.pop()
    return render(selected)


def _renderable_entries(
    lesson_rows: list[dict], project_dir: str | Path | None
) -> list[tuple[dict, str]]:
    """``(row, text)`` for every row that renders and is in scope for *project_dir*."""
    entries: list[tuple[dict, str]] = []
    for row in lesson_rows:
        try:
            decoded = json.loads(row["value_json"])
        except (TypeError, ValueError, RecursionError):
            # One unreadable row must not fail every context build; the key
            # names the row to repair, the value is left out of the log.
            logger.warning("Skipping lesson %r: stored value_json does not decode", row["key"])
            continue
        text = _renderable_lesson_text(decoded, row["key"])
        if not text:
            continue
        scope = _lesson_scope(decoded)
        if scope and not project_scope_satisfied(scope, project_dir):
            continue
        entries.append((row, text))
    return entries


#: Request words in ``lesson_keywords``' stop list and words of two letters or
#: fewer are ignored. Each remaining request word is matched once through its
#: stem. A follow-up message earns a lesson only when they share at least
#: ``_TURN_LESSON_TERMS`` distinct stems that are each rare: found in at most
#: ``_TURN_LESSON_RARITY`` of the N stored lessons, and never fewer than one, so
#: a small store still admits a lesson through words no other lesson carries.
#: Rarity is a count of distinct stems, not a sum of their weights: a sum lets
#: one word found in a single lesson admit it alone, and lets a store of a few
#: hundred lessons count an ordinary word as rare.
_TURN_LESSON_TERMS = 2
_TURN_LESSON_RARITY = 0.01


def turn_lessons(
    store: VectorMemoryStore,
    query_text: str,
    *,
    shown: Callable[[str], bool],
    project_dir: str | Path | None = None,
    max_rows: int,
    max_chars: int,
    render_lesson: Callable[[str], str] | None = None,
) -> list[tuple[str, str]]:
    """``(key, text)`` of the lessons a follow-up message should add, best first.

    Every eligible, in-scope lesson is a candidate, rules and findings alike,
    except those *shown* reports as already in the session. Request words in
    ``lesson_keywords``' stop list and words of two letters or fewer are ignored.
    Each remaining request word is matched once, through its stem. A lesson is
    admitted only when it shares at least ``_TURN_LESSON_TERMS`` distinct stems
    with the message, each found in at most ``_TURN_LESSON_RARITY`` of the stored
    lessons (and never fewer than one lesson); admitted lessons are ordered by
    the summed rarity weight of every shared stem over the square root of their
    length, the startup order, and taken while their rendered forms fit
    *max_rows* and *max_chars*. A lesson too long for the room left is skipped,
    not a stop. No embedding is spent.
    """
    if not query_text.strip() or max_rows <= 0:
        return []
    with store._db_lock:
        lesson_rows = store._eligible_rows(store.get_lessons(), "directive")
    entries = _renderable_entries(lesson_rows, project_dir)
    if not entries:
        return []
    query_stems = {_stem_one(word) for word in store._lesson_keywords(query_text.lower())}
    row_tokens = _row_stem_tokens_for_scan(len(entries))
    shared_by_row, sizes, document_frequency = _shared_lesson_stems(
        entries, query_stems, row_tokens
    )
    rows = len(entries)
    rare_stems = {
        stem
        for stem, count in document_frequency.items()
        if count <= max(1, _TURN_LESSON_RARITY * rows)
    }
    weight = _rarity_weights(rows, document_frequency)
    admitted = [
        (sum(weight[stem] for stem in shared) / math.sqrt(sizes[index]), index)
        for index, shared in enumerate(shared_by_row)
        if len(shared & rare_stems) >= _TURN_LESSON_TERMS and not shown(entries[index][1])
    ]
    # Stable, so equal scores keep the stored newest-first order.
    admitted.sort(key=lambda pair: -pair[0])
    chosen: list[tuple[str, str]] = []
    used = 0
    for _, index in admitted:
        if len(chosen) == max_rows:
            break
        row, text = entries[index]
        rendered = render_lesson(text) if render_lesson is not None else text
        size = len(rendered) + 3  # "- " prefix and newline
        if used + size > max_chars:
            continue
        chosen.append((row["key"], text))
        used += size
    return chosen


def rank_lessons(
    store: VectorMemoryStore,
    entries: list[tuple[dict, str]],
    query_text: str,
    *,
    recall_query: _RecallQuery | None = None,
) -> list[tuple[dict, str]]:
    """Order *entries* by hybrid relevance to *query_text*, most relevant first.

    Stored ``embedding`` blobs are reused, so this costs one embed for the
    query rather than one per lesson. The sort is stable and *entries*
    arrives newest-first, so equal scores keep recency order and a query
    that matches nothing degrades to plain recency.

    A query with no vector -- every startup render, and any recall whose embed
    is unavailable -- is scored by :func:`_lexical_lesson_scores` instead of the
    capped overlap count the hybrid score takes as its keyword half.
    """
    request_words = set(re.findall(r"\w+", query_text.lower()))
    if recall_query is not None:
        query_emb = recall_query.vector
    elif store.embed_fn:
        query_emb = store._try_embed(query_text, PRIORITY_INTERACTIVE)
    else:
        query_emb = None
    # Same row-side derivation, and the same width rule, as the semantic scan:
    # a lesson's tokens depend only on its own rendered text, and only a pass
    # that fits the cache can hit it.
    row_tokens = _row_stem_tokens_for_scan(len(entries))
    if not query_emb:
        lexical_query_words = {_stem_one(word) for word in request_words}
        lexical = _lexical_lesson_scores(entries, lexical_query_words, row_tokens)
        # ``sorted`` is stable, so equal scores -- including every zero-overlap
        # row -- keep the caller's newest-first order.
        order = sorted(range(len(entries)), key=lambda index: -lexical[index])
        return [entries[index] for index in order]
    query_words = _stem_words(request_words)
    similarity = store._stored_similarity_scorer(query_emb)
    scored: list[tuple[float, tuple[dict, str]]] = []
    for entry in entries:
        row, text = entry
        # Only the rendered text is matched. A lesson key is
        # ``lesson.<md5hash>``, which carries no words, so there is no key
        # term to weight here the way get_semantic_context() weights its own.
        overlap = len(query_words & row_tokens(text.lower()))
        score = _hybrid_score(_keyword_score(overlap), similarity(row))
        scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored]


def _lexical_lesson_scores(
    entries: list[tuple[dict, str]],
    query_words: set[str],
    row_tokens: Callable[[str], frozenset[str]],
) -> list[float]:
    """Score each entry's words against the request's, one float per entry.

    Each shared token is weighted by how rare it is among *entries*
    (``log((N + 1) / (df + 0.5))``), and the sum is divided by the square root
    of the row's number of distinct words.

    The hybrid score's keyword half, ``_keyword_score``, saturates at ten shared
    tokens, so a long first message would tie nearly every stored rule at the top
    and the stable sort would return newest-first. A distinctive term counts for
    far more than a common one; a token carried by nearly every row weighs close
    to nothing (about ``0.5 / N``). The length factor discounts incidental overlap
    in a long row, the same correction ``history_search.search_sessions`` makes
    for long sessions.

    Every weight is positive, since ``df <= N``, so any overlap still outranks
    none: ordering is unchanged for a zero-overlap row, and the findings tier's
    admission test (``any_lesson_overlap``) still agrees with this ranking.
    """
    masses, sizes = _lexical_lesson_weights(entries, query_words, row_tokens)
    return [mass / math.sqrt(size) if mass else 0.0 for mass, size in zip(masses, sizes)]


def _lexical_lesson_weights(
    entries: list[tuple[dict, str]],
    query_words: set[str],
    row_tokens: Callable[[str], frozenset[str]],
) -> tuple[list[float], list[int]]:
    """Per entry, the summed rarity weight of its tokens in *query_words*, and its length.

    Rarity is ``log((N + 1) / (df + 0.5))`` over *entries*. Document frequency
    is counted only for tokens the request carries, over row token sets
    ``row_tokens`` already memoises, so no row is re-stemmed. A row's length is
    its number of distinct words, so an inflection does not make it look longer.
    """
    shared_by_row, sizes, document_frequency = _shared_lesson_stems(
        entries, query_words, row_tokens
    )
    weight = _rarity_weights(len(entries), document_frequency)
    return [sum(weight[token] for token in shared) for shared in shared_by_row], sizes


def _shared_lesson_stems(
    entries: list[tuple[dict, str]],
    query_words: set[str],
    row_tokens: Callable[[str], frozenset[str]],
) -> tuple[list[frozenset[str]], list[int], dict[str, int]]:
    """Per entry, its tokens in *query_words* and its number of distinct words; and,
    for each such token, the number of entries carrying it."""
    shared_by_row: list[frozenset[str]] = []
    sizes: list[int] = []
    document_frequency: dict[str, int] = {}
    for _, text in entries:
        normalized_text = text.lower()
        tokens = row_tokens(normalized_text)
        shared = tokens & query_words
        shared_by_row.append(shared)
        sizes.append(len(set(re.findall(r"\w+", normalized_text))))
        for token in shared:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return shared_by_row, sizes, document_frequency


def _rarity_weights(rows: int, document_frequency: dict[str, int]) -> dict[str, float]:
    """``log((N + 1) / (df + 0.5))`` per token: the fewer of the *rows* carry it, the heavier."""
    return {
        token: math.log((rows + 1) / (count + 0.5)) for token, count in document_frequency.items()
    }


def any_lesson_overlap(
    store: VectorMemoryStore, entries: list[tuple[dict, str]], query_text: str
) -> bool:
    """Whether ANY entry shares a stemmed word with *query_text*.

    The admission test for the findings tier deliberately uses the broader
    surface-plus-stem request set, while lexical ranking uses one stem per request
    word. Any overlap found by ranking is therefore admitted, without letting an
    exact inflection count twice in the ranking. This is not the shared unstemmed
    helper the JSONL store uses: stemming matches strictly more, so borrowing that
    answer would discard this store's stem-only hits.

    Only the KEYWORD half is consulted, which is exactly right on the startup
    path: it passes a recall query whose vector is ``None``, so the similarity
    term contributes nothing there and the keyword overlap IS the whole score.
    """
    if not entries or not query_text.strip():
        return False
    query_words = _stem_words(set(re.findall(r"\w+", query_text.lower())))
    if not query_words:
        return False
    row_tokens = _row_stem_tokens_for_scan(len(entries))
    return any(query_words & row_tokens(text.lower()) for _, text in entries)
