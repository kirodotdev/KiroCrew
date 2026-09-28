"""Lesson store — persistent corrections and preferences.

Lessons are saved via the ``kirocrew learn`` CLI (called by the LLM via bash)
and loaded into every session's context alongside memory and skills.

Storage: ``<config_dir>/lessons.jsonl`` (append-only JSONL).
"""

from __future__ import annotations

import json
import logging
import stat
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ALWAYS,
    LESSON_APPLIES_ON_TOPIC,
    LESSON_APPLIES_VALUES,
    any_request_overlap,
    contains_volatile_lesson_fact,
    order_by_request_relevance,
    render_lesson_tier,
    render_withheld_tier,
    tighter_lesson_budget,
)
from kiro_crew.memory_startup import require_memory_ready
from kiro_crew.memory_stores import named_store_operation
from kiro_crew.project_scope import (
    canonical_scope,
    project_scope_satisfied,
    scope_is_admissible,
    scope_selector_is_inadmissible,
)

try:
    from kiro_crew.config.loader import config_dir as _config_dir
except ImportError:
    _config_dir = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ── Constants ──


# Fallback data dir, used ONLY when a live ``config_dir()`` lookup is unavailable
# or raises (see ``LessonStore.__init__`` / ``_reject_sensitive``). This is a pure
# literal, resolved at use time — it must NOT call ``config_dir()`` at import, or
# merely importing this module would fire the one-time blocking legacy-home
# migration as an import side effect. The migration stays gated at the single
# ``ensure_data_home()`` call in the CLI prologue; the live home is resolved
# lazily via ``config_dir()`` inside ``LessonStore.__init__``. Honors
# ``KIROCREW_HOME`` only insofar as this fallback is rarely reached — a NAMED
# memory store cannot land here (see ``_is_owned_store_root``), so the normal
# path resolves through ``config_dir()``, which does honor the override.
_DEFAULT_DIR = Path.home() / ".kiro" / "crew"


def _is_owned_store_root(base_dir: Path) -> bool:
    """Is *base_dir* a NAMED memory store's own directory?

    True only for a DIRECT child of ``memory_stores_root()`` — the same parent
    equality ``memory_stores._named_store_dir`` re-checks after composing a path,
    so a symlinked component cannot smuggle an outside directory past this. The
    tightest test that admits a real store: it grants nothing to
    ``memory_stores/`` itself, to a nested path under a store, or to any other
    fenced keystone (``profiles/``, ``security_policy.json``).

    Never raises. ``memory_stores_root()`` reads the config home, and a store root
    that cannot be resolved is simply not owned — the caller then falls through to
    the ordinary sensitive-path check, which is the safe direction.
    """
    try:
        from kiro_crew.memory_stores import memory_stores_root

        root = memory_stores_root().resolve()
        candidate = Path(base_dir)
        # Identity, matching ``memory_stores._named_store_dir``: parent equality
        # alone accepts a link that redirects one store onto another INSIDE the
        # root, which would append this crew's corrections to that crew's
        # lessons.jsonl with the check reporting success.
        return candidate.resolve() == root / candidate.name
    except Exception:
        logger.debug("could not decide store ownership for %s", base_dir, exc_info=True)
        return False


_LESSONS_FILE = "lessons.jsonl"
_MAX_LESSONS_TOTAL = 200  # prune oldest when exceeded

# One lock PER FILE, shared by every LessonStore instance addressing it. A
# per-instance lock serializes nothing when two instances point at the same file --
# and they do: DashboardState.lessons and context.get_lessons_for() construct
# separate instances over the same global store. Without this, the atomic
# enrich-or-insert below is atomic only against itself, so a dashboard refinement
# racing a consolidation write could still lose a clause.
#
# Still in-process only. threading.Lock does not span processes, so the CLI and the
# gateway remain last-writer-wins against each other; the per-write temp name below
# is what keeps that case from corrupting the file rather than merely losing an edit.
_PATH_LOCKS: dict[Path, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Return the process-wide lock for *path*, creating it on first use."""
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path, threading.Lock())


# ── Types ──


@dataclass
class Lesson:
    """A single learned correction."""

    ts: str
    rule: str
    category: str  # "tool", "preference", "knowledge"
    negative: str | None = None
    # Path fragment naming the repository this correction belongs to, or None for
    # a correction that applies everywhere. Absent is the default so every stored
    # lesson keeps applying exactly as before, and only a lesson that opts in is
    # ever withheld. See ``kiro_crew.project_scope``.
    repo_scope: str | None = None
    # Which tier this correction belongs to: a standing rule the session must
    # follow regardless of topic, or a past finding worth having when the task
    # touches it. ``None`` is the default and reads as unclassified, which every
    # row written before this field carries; readers give that class the standing
    # rule's treatment. The value is AUTHORED at the write surface, never derived
    # from ``category``, ``ts`` or wording -- see
    # ``kiro_crew.lesson_validation.normalize_lesson_applies``.
    applies: str | None = None


# ── Storage ──


def _serializable(lesson: Lesson) -> dict:
    """The row as stored, omitting ``applies`` when the writer named no tier.

    A bare ``asdict`` emits ``"applies": null`` on every row, which rewrites every
    legacy line the next time any lesson is saved and contradicts the additive
    contract the vector writer keeps ("absent when unset"). The two stores must
    agree on that, because the same absence has to read as unstated in both. Every
    other field is emitted unconditionally, including a ``None`` ``negative`` and
    ``repo_scope``, so existing rows are byte-identical.
    """
    row = asdict(lesson)
    if row.get("applies") is None:
        row.pop("applies", None)
    return row


def _prune_to_total(lessons: list[Lesson]) -> list[Lesson]:
    """Trim *lessons* to ``_MAX_LESSONS_TOTAL``, evicting past findings first.

    A plain ``lessons[-_MAX_LESSONS_TOTAL:]`` here is tier-blind, and that is a
    durability hazard rather than a cosmetic one: findings are the cheap,
    high-volume class, so accumulating them deletes the user's oldest authored
    standing rules from disk. That is strictly worse than omitting one from a
    prompt -- an omission is announced and recoverable through ``learn_list``,
    while a pruned row is gone.

    Eviction order is therefore experiences, then unclassified rows, then
    authored directives, each oldest-first within its class. Authored directives
    are evicted only when they alone exceed the cap; a user who really has 200+
    standing rules still hits a bound, and that bound is the honest one rather
    than a silent preference for whatever was typed most recently.

    Order among the KEPT rows is preserved (oldest-first, as stored), so the file
    layout and every reader that depends on it are unchanged.
    """
    if len(lessons) <= _MAX_LESSONS_TOTAL:
        return lessons
    over = len(lessons) - _MAX_LESSONS_TOTAL
    # Index-based so the survivors can be returned in their original order.
    by_class: dict[int, list[int]] = {0: [], 1: [], 2: []}
    for index, lesson in enumerate(lessons):
        if lesson.applies == LESSON_APPLIES_ON_TOPIC:
            by_class[0].append(index)
        elif lesson.applies == LESSON_APPLIES_ALWAYS:
            by_class[2].append(index)
        else:
            by_class[1].append(index)
    drop: set[int] = set()
    for rank in (0, 1, 2):
        for index in by_class[rank]:
            if len(drop) >= over:
                break
            drop.add(index)
        if len(drop) >= over:
            break
    return [lesson for index, lesson in enumerate(lessons) if index not in drop]


class LessonStore:
    """Append-only JSONL store for learned corrections."""

    def _reject_sensitive(self, label: str, path: Path) -> None:
        """Enforce fallback to default dir and emit SEL audit event."""
        self._dir = _DEFAULT_DIR
        logger.warning("%s is a sensitive path; falling back to default", label)
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key="system",
                source="init",
                tool_name="LessonStore",
                outcome="rejected",
                resources=str(path),
                error=f"{label} is a sensitive path; falling back to default",
            )
        except Exception:
            logger.warning("Failed to emit SEL audit event for %s", label, exc_info=True)

    def __init__(self, base_dir: Path | None = None):
        from kiro_crew.security import is_sensitive_path

        if base_dir:
            from kiro_crew.memory_stores import memory_store_version, named_store_of_db

            store_name = named_store_of_db(base_dir / "memory.db")
            if store_name and memory_store_version(store_name) == 2:
                raise ValueError("Member lessons are stored only in the member database")
            if _is_owned_store_root(base_dir):
                # A named memory store's own root. It is inside the keystone
                # ``memory_stores/`` fence, so ``is_sensitive_path`` answers True
                # for it — correctly, because that fence is what stops an AGENT
                # FILE TOOL from reading another crew's memory. This class is not
                # a tool: it is the store's owner, the same way ``MemoryStore``
                # opens its own markdown tree directly. Fix the reader, never the
                # fence; relaxing ``is_sensitive_path`` here would unfence the
                # subtree for every tool caller too.
                #
                # Without this branch per-store lessons are not merely lost, they
                # are MISFILED: the fallback is a write target, so every crew's
                # corrections append to the one global ``lessons.jsonl``.
                self._dir = base_dir
            elif is_sensitive_path(str(base_dir)):
                self._reject_sensitive("base_dir", base_dir)
            else:
                self._dir = base_dir
        elif _config_dir is not None:
            try:
                candidate = _config_dir()
            except Exception:
                logger.warning("config_dir() failed; falling back to default", exc_info=True)
                self._dir = _DEFAULT_DIR
            else:
                if is_sensitive_path(str(candidate)):
                    self._reject_sensitive("config_dir()", candidate)
                else:
                    self._dir = candidate
        else:
            self._dir = _DEFAULT_DIR
        self._path = self._dir / _LESSONS_FILE
        from kiro_crew.memory_stores import named_store_of_db

        self._memory_store_name = named_store_of_db(self._dir / "memory.db")
        self._lock = _lock_for(self._path)
        # mtime-based cache: (mtime, lessons)
        self._cache: tuple[float, list[Lesson]] | None = None

    @property
    def path(self) -> Path:
        """The JSONL file this store reads and writes.

        Public so a reader that needs the FILE rather than parsed rows — the
        injection audit, which must report an unreadable tier instead of the empty
        list :meth:`load_all` answers with — resolves the path through the class
        that owns it. Re-composing ``<base_dir>/lessons.jsonl`` at the caller drops
        the two fallbacks ``__init__`` applies (a sensitive ``base_dir``, an absent
        or raising ``config_dir()``), so an audit built that way would report on a
        file no writer ever writes.
        """
        return self._path

    def _write_all(self, lessons: list[Lesson]) -> None:
        """Replace the file atomically. The caller MUST hold ``self._lock``.

        tmp + ``os.replace`` rather than ``write_text`` for two reasons. A reader
        can observe this file without the lock (``load_all`` takes none), and
        ``atomic_write`` renames a unique temp file over the target, so a reader sees
        either the whole old file or the whole new one -- never a half-written one.
        That is what makes the unlocked read in ``load_all`` safe, so no lock has to
        be added there, and a crash mid-write cannot truncate the store.

        Uses the repo's ``atomic_write`` rather than a hand-rolled temp + rename.
        Hand-rolling it re-introduces three problems the shared helper already
        solves: a temp name that two writers could collide on (it uses
        ``tempfile.mkstemp``), a bare ``os.replace`` that raises ``PermissionError``
        when Windows Search or AV holds a handle (it uses ``replace_with_retry``),
        and a replacement inode carrying umask permissions instead of the store's
        (it applies ``mode`` via ``fchmod_safe``).

        The mode is read off the existing file so a restrictive store stays
        restrictive -- ``write_text`` preserves it implicitly by reusing the inode,
        and swapping the inode drops it. A store being created for
        the first time gets ``0o600``: lesson text is personal content, and nothing
        else needs to read it.

        ``newline=""`` keeps the bytes exact. The default would apply
        universal-newline translation on write, and this file is read, edited and
        written back on every save.
        """
        require_memory_ready(self._memory_store_name)
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except OSError:
            mode = 0o600
        atomic_write(
            self._path,
            "".join(json.dumps(_serializable(le)) + "\n" for le in lessons),
            mode=mode,
            newline="",
        )
        self._cache = None  # invalidate

    @named_store_operation
    def save(self, lesson: Lesson) -> str:
        """Insert *lesson*, skipping a rule that is already stored.

        Deliberately does NOT enrich. Most callers here are automatic --
        consolidation, task-runner extraction, onboarding import -- and an
        automatic writer must not replace a NOT-clause a human authored. Only an
        explicit refinement (the /api/lessons route, ``kirocrew learn add``)
        should attach a clause, and those call :meth:`save_or_enrich`.

        Returns the same outcome string as the shared body. Automatic callers use
        ``refused`` to avoid counting, notifying, or ledgering a lesson that did not
        persist; callers that ignore the return remain unaffected.
        """
        return self._insert_or_enrich(lesson, enrich=False)

    @named_store_operation
    def save_or_enrich(self, lesson: Lesson) -> str:
        """Insert *lesson*, or attach its NOT-clause to the record holding the same
        rule, in ONE lock acquisition. Returns
        ``inserted``/``enriched``/``unchanged``/``refused``.

        For EXPLICIT refinement only -- see :meth:`save` for why automatic writers
        must not reach this.

        Re-submitting a rule to attach a NOT-clause must not fall into the duplicate
        check, which matches on the rule alone: returning before looking at
        ``negative`` would drop the clause behind an HTTP 200.
        """
        return self._insert_or_enrich(lesson, enrich=True)

    def _insert_or_enrich(self, lesson: Lesson, *, enrich: bool) -> str:
        """Shared body for :meth:`save` and :meth:`save_or_enrich`.

        The single lock acquisition is the load-bearing part. Doing enrich and
        insert as two separate locked calls let a concurrent writer insert the
        same rule in the gap, so the second call saw a duplicate and skipped --
        dropping the clause exactly as before, just less often. The whole
        read-decide-write sequence runs inside the lock, so there is no gap.

        Matching is ``lower()``, deliberately NOT ``casefold()``. This reversed an
        earlier decision in the same change, so the reasoning matters: ``casefold()``
        maps ``ß`` to ``ss`` in order to match a stored "Straße" against a submitted
        "STRASSE" -- but the very same mapping makes "Maße" and "Masse" compare EQUAL,
        and those are different German words. Under ``casefold()`` a clause submitted
        for "Masse" attached itself to the stored "Maße" and the intended lesson was
        never created: the wrong rule enriched, the right one discarded.

        The two behaviours are inseparable -- both come from the one ß rule -- so this
        is a trade, not a fix. ``lower()`` is the safe side of it: it never conflates
        two distinct rules, and its cost is a MISSED enrichment (a ß case-variant
        inserts a second row) rather than a corrupted one. Losing a refinement is
        recoverable; silently rewriting the wrong lesson is not.

        A re-submit carrying NO clause never strips one that is already stored --
        it reports ``unchanged``, matching the vector store for the same case.
        """
        if contains_volatile_lesson_fact(lesson.rule, lesson.negative):
            return "refused"
        with self._lock:
            # A whitespace-only clause is no clause. Same defect as the vector store's:
            # `--negative "   "` is truthy, so it would replace a real stored clause
            # with blanks. Normalised here too, because both stores are reached
            # directly by the CLI, the route, consolidation and the task runner.
            # isinstance FIRST for the same reason as the vector store: consolidation
            # hands over the LLM's own value, and .strip() on a non-string would
            # abort the run with AttributeError.
            wanted_negative = (
                lesson.negative.strip() or None if isinstance(lesson.negative, str) else None
            )
            # Same normalisation for the scope key: a whitespace-only value is no
            # scope, and a non-string is refused before ``.strip()`` can raise,
            # because consolidation hands over a value the model produced.
            wanted_scope = canonical_scope(lesson.repo_scope)
            # load_all() is called under the lock deliberately: it takes no lock of
            # its own, so this is not a re-entrant acquisition on a non-reentrant
            # Lock. Do NOT call save() from in here for the same reason.
            existing = self.load_all()
            wanted = lesson.rule.lower().strip()
            updated: list[Lesson] = []
            matched = False
            outcome = "inserted"
            for le in existing:
                # Scope is part of a lesson's IDENTITY, not a field to overwrite, and
                # the comparison is STRICT. The same rule with and without a scope is
                # two lessons, exactly as in the vector store, where the scope is
                # folded into the key so the two never share a row.
                #
                # Matching loosely broke it in both directions: on rule text alone a
                # submission for repo B replaced repo A's scope and A lost the lesson,
                # and treating an omitted scope as "no conflict" made a genuine
                # save-this-globally request bind to a scoped row and report success
                # without ever creating the global lesson. Addressing a scoped lesson
                # therefore means naming its scope.
                if (
                    not matched
                    and wanted_scope == le.repo_scope
                    and (le.rule.lower().strip() == wanted)
                ):
                    matched = True
                    # A field is enriched only when the submission carries a value
                    # for it AND that value differs. A bare re-submit therefore
                    # never strips a stored clause or scope -- it reports
                    # ``unchanged``, matching the vector store for the same case.
                    next_negative = le.negative
                    if wanted_negative is not None:
                        next_negative = wanted_negative
                    next_scope = le.repo_scope
                    if wanted_scope is not None:
                        next_scope = wanted_scope
                    if not enrich or (next_negative == le.negative and next_scope == le.repo_scope):
                        outcome = "unchanged"
                        updated.append(le)
                    else:
                        outcome = "enriched"
                        # Build a REPLACEMENT record rather than mutating in place:
                        # load_all() hands back the cached objects, so an in-place
                        # edit followed by a failed write would leave the cache
                        # advertising a clause that was never persisted -- and that
                        # cache feeds context injection.
                        updated.append(replace(le, negative=next_negative, repo_scope=next_scope))
                    continue
                updated.append(le)
            if outcome == "unchanged":
                return outcome  # nothing to write; leave the file untouched
            if not matched:
                # Insert the normalised clause too, so a whitespace-only one is stored
                # as absent rather than as blanks.
                submitted = replace(lesson, negative=wanted_negative, repo_scope=wanted_scope)
                updated.append(submitted)
                if len(updated) > _MAX_LESSONS_TOTAL:
                    updated = _prune_to_total(updated)
                    # The submission is itself prunable, and at the cap it is the
                    # FIRST candidate when it is the oldest row of the lowest
                    # surviving class -- an `on_topic` write against a full store
                    # hits this deterministically. Reporting "inserted" for a row
                    # that is not in the file is a silent false success on a memory
                    # write, which is the one outcome a caller cannot recover from:
                    # it never learns to retry. Identity is by object here, not by
                    # text, because a same-text row already present took the
                    # `matched` branch above.
                    if not any(row is submitted for row in updated):
                        logger.info(
                            "Refused lesson: the store is at its %d-row cap and every "
                            "retained row outranks this one: %s",
                            _MAX_LESSONS_TOTAL,
                            lesson.rule,
                        )
                        return "refused"
            self._write_all(updated)
        logger.info("%s lesson: %s", outcome.capitalize(), lesson.rule)
        return outcome

    @named_store_operation
    def remove(
        self, rule_substring: str, repo_scope: str | None = None, *, exact: bool = False
    ) -> bool:
        """Remove lessons whose rule contains *rule_substring*. Returns True if any removed.

        Substring matching on the rule text is deliberate: a user targets a
        lesson by a fragment of its rule rather than retyping the whole thing
        (``test_remove_matching`` deletes "Use tool-b" by passing "tool-b").
        *exact* narrows the text match to the whole rule (case-insensitive,
        surrounding whitespace ignored): a caller that holds the full rule -- a
        table row's Delete button -- names ONE row, where the substring path would
        also take every longer rule containing it ("use tabs" -> "always use
        tabs"). The scope selector applies identically in both modes.

        A lesson's identity is the pair ``(rule, repo_scope)``: the same rule
        scoped to a repo and stored globally are two distinct rows, and the
        selector decides which of them a delete reaches.

        When *repo_scope* is None (the default) the scope is not part of the
        match: every row whose rule contains the substring is removed. When
        *repo_scope* is given, a row is removed only when it ALSO carries that
        scope, compared after ``canonical_scope`` on both sides so
        trailing-slash and backslash variants fold together (matching the
        write path's identity). Passing the canonical form of ``None`` -- an
        empty or whitespace-only selector -- targets the unscoped (global)
        rows specifically, which is how a caller deletes the global row while
        leaving a same-rule scoped one in place. A nonempty selector the write
        surface would refuse -- a bare ``/``, an absolute path, a dot segment
        -- is refused with :class:`ValueError` rather than canonically folded
        onto rows the caller never named. A STORED scope that is present but
        inadmissible marks a scoped-but-broken row, which the injection gate
        withholds; a scope-selective delete never claims such a row, and the
        unselective (absent) path is what removes it.

        Holds the lock. An unlocked read-modify-write here would lose a concurrent
        ``save`` outright -- and without that lock the atomicity
        :meth:`save_or_enrich` claims would not actually hold.
        """
        if repo_scope is not None and scope_selector_is_inadmissible(repo_scope):
            raise ValueError(f"repo_scope does not name a usable scope: {repo_scope!r}")
        with self._lock:
            lessons = self.load_all()
            lower = rule_substring.lower()
            wanted_text = lower.strip()
            # A missing selector leaves scope out of the match. A present one --
            # including the canonical form of an empty string, which is None and
            # targets the unscoped rows -- is compared canonically against each
            # row's own canonical scope, so the two never disagree with the
            # write path over trailing-slash / backslash forms.
            scope_selective = repo_scope is not None
            wanted_scope = canonical_scope(repo_scope) if scope_selective else None

            def _matches(le: Lesson) -> bool:
                if exact:
                    if le.rule.lower().strip() != wanted_text:
                        return False
                elif lower not in le.rule.lower():
                    return False
                if scope_selective:
                    # A stored scope that is present but inadmissible (an
                    # imported "/") is scoped-but-broken, not global: the
                    # injection gate withholds such a row rather than treating
                    # it as applies-everywhere, so a scope-selective delete
                    # never claims it -- it would otherwise fold to None and be
                    # removed by the explicit-global selector. The unselective
                    # (absent) path still reaches it.
                    if le.repo_scope is not None and not scope_is_admissible(le.repo_scope):
                        return False
                    if canonical_scope(le.repo_scope) != wanted_scope:
                        return False
                return True

            kept = [le for le in lessons if not _matches(le)]
            if len(kept) == len(lessons):
                return False
            self._write_all(kept)
        return True

    @named_store_operation
    def load_all(self) -> list[Lesson]:
        """Load all lessons from the JSONL file. Uses mtime-based caching."""
        require_memory_ready(self._memory_store_name)
        if not self._path.exists():
            self._cache = None
            return []
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return []
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        lessons: list[Lesson] = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                raw_scope = data.get("repo_scope")
                # A PRESENT but unusable scope is not "applies everywhere": the row
                # meant to be scoped and cannot say where, so it is dropped rather
                # than admitted globally (fail-open) or passed to the gate, where a
                # non-string would raise while a prompt is being assembled.
                if raw_scope is not None and (
                    not isinstance(raw_scope, str) or not raw_scope.strip()
                ):
                    continue
                lessons.append(
                    Lesson(
                        ts=data.get("ts", ""),
                        rule=data.get("rule", ""),
                        category=data.get("category", "knowledge"),
                        negative=data.get("negative"),
                        repo_scope=raw_scope,
                        # A stored tier the vocabulary does not recognize reads as
                        # unclassified rather than raising: this is the READ path,
                        # and a hand-edited or future-schema row must not take down
                        # prompt assembly for every other lesson in the file.
                        applies=(
                            data["applies"].strip().lower()
                            if isinstance(data.get("applies"), str)
                            and data["applies"].strip().lower() in LESSON_APPLIES_VALUES
                            else None
                        ),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        self._cache = (mtime, lessons)
        return lessons

    def _applicable(self, lessons: list[Lesson], project_dir: str | Path | None) -> list[Lesson]:
        """Drop volatile lessons and lessons outside *project_dir*.

        Existing volatile rows remain readable through ``load_all`` and removable by
        the user, but never reach a prompt. A lesson with no scope applies everywhere;
        a scoped one is withheld unless the session's project is positively inside the
        named tree.
        """
        return [
            le
            for le in lessons
            if not contains_volatile_lesson_fact(le.rule, le.negative)
            and (not le.repo_scope or project_scope_satisfied(le.repo_scope, project_dir))
        ]

    @named_store_operation
    def get_context(
        self,
        project_dir: str | Path | None = None,
        *,
        cap: int = 0,
        directive_budget: int = 0,
        experience_budget: int = 0,
        query_text: str = "",
    ) -> str:
        """Format lessons as context for injection into prompts.

        *project_dir* is the session's active project, used only by the
        ``repo_scope`` gate; omitting it withholds every scoped lesson. ``cap``
        is a model-safety ceiling for this rendered block, not the ordinary
        background budget.

        *directive_budget* and *experience_budget* are the startup allowances for
        the two authored tiers. Both default to ``0`` (unbounded), which keeps
        every caller predating them byte-identical; ``context.py`` passes real
        values on the session-start path. Standing rules -- plus every row whose
        author named no tier -- are served first and from the larger budget, so a
        crowd of past findings cannot displace a rule the user expects enforced.

        JSONL carries no relevance signal, so newest-first is the only stable
        ranking available when a budget forces a choice, and it is applied within
        each tier separately.
        """
        lessons = self._applicable(self.load_all(), project_dir)
        if not lessons:
            return ""
        # Newest-first, then split. Ordering before the split gives each tier a
        # newest-first baseline, which the per-tier relevance sort below preserves
        # for rows of equal overlap.
        #
        # Within the rule tier, AUTHORED directives are ordered ahead of
        # unclassified rows, and that precedence is load-bearing rather than
        # cosmetic. Unclassified is this reader's safe-direction guess about a row
        # whose author never said what it was; an authored directive is the user
        # stating outright that it must always apply. Ordered in one pool, a pile of
        # untagged notes displaces exactly the rules the user was most explicit
        # about -- measured on a synthetic store, six authored directives all fell
        # out of a 7.4K budget behind forty newer untagged rows. Sorting the two
        # sub-lists SEPARATELY is what keeps that precedence while still letting
        # relevance decide inside each of them.
        authored: list[Lesson] = []
        unclassified: list[Lesson] = []
        experiences_rows: list[Lesson] = []
        for lesson in reversed(lessons):
            if lesson.applies == LESSON_APPLIES_ON_TOPIC:
                experiences_rows.append(lesson)
            elif lesson.applies == LESSON_APPLIES_ALWAYS:
                authored.append(lesson)
            else:
                unclassified.append(lesson)

        def entries(rows: list[Lesson]) -> list[tuple[object, str]]:
            return [
                (
                    lesson,
                    f"{lesson.rule} — {lesson.negative}" if lesson.negative else lesson.rule,
                )
                for lesson in rows
            ]

        # Every tier is ordered by relevance to this request before the budget cuts,
        # and ordered PER TIER so authored rules keep their precedence over untagged
        # rows. Newest-first alone drops a row the task actually needs: measured on a
        # 199-lesson store, a relevant-but-old finding fell outside the findings
        # budget while newer irrelevant ones fitted, and on a 100-row store whose
        # rows all predate the tier field an exact-topic match fell outside the RULE
        # budget the same way -- every such row lands in the rule tier, so leaving
        # that tier unordered lost a match the vector store kept, which ranks its
        # whole eligible set. Ordering is not admission: it decides which rows
        # survive a truncation that is going to happen anyway.
        ranked_directives = order_by_request_relevance(entries(authored), query_text)
        ranked_directives += order_by_request_relevance(entries(unclassified), query_text)
        directive_room = tighter_lesson_budget(directive_budget, cap)
        directive_block, _ = render_lesson_tier(
            ranked_directives,
            directive_room,
            header=(
                "[Learned corrections — user-taught rules from past mistakes.\n"
                "ALWAYS follow these. They override default behavior.]"
            ),
            footer="[End of learned corrections]",
            omission=(
                "[Context budget: omitted {count} of {total} retained rules above the "
                "{limit}-character rule budget. This is a BUDGET limit, not a judgement "
                "that they stopped applying: read them with learn_list.]"
            ),
        )
        # ``max(1, …)`` because ``tighter_lesson_budget`` reads 0 as unbounded, so a
        # directive block that consumed the whole ceiling would otherwise hand this
        # tier no limit at all.
        experience_room = tighter_lesson_budget(
            experience_budget,
            max(1, cap - len(directive_block)) if cap else 0,
        )
        experience_entries = entries(experiences_rows)
        if (
            experience_entries
            and query_text.strip()
            and not any_request_overlap(experience_entries, query_text)
        ):
            # Nothing here is about this request, so spend none of the allowance on
            # it. A finding is DEFINED as material worth having when the task
            # touches them; newest-first filler is not a weaker version of that, it
            # is unrelated to the task by construction -- and it never rescued a
            # near-miss either, because it surfaces the NEWEST rows rather than the
            # ones the request is closest to. A bare greeting lands here too: it
            # names no topic, so no finding is on it. The frame still renders, which
            # is what turns "you have findings, none matched, go ask" into something
            # the next turn can act on rather than an absence it cannot see.
            experience_block = render_withheld_tier(
                len(experience_entries),
                header="[Learned experience — past findings]",
                footer="[End of learned experience]",
                notice=(
                    "[Withheld all {total} past findings: none of them share wording "
                    "with this request. They are NOT gone and this is not a budget "
                    "limit -- call memory_recall with a specific question, or "
                    "learn_list, when the task turns out to touch one.]"
                ),
            )
        else:
            experiences = order_by_request_relevance(experience_entries, query_text)
            experience_block, _ = render_lesson_tier(
                experiences,
                experience_room,
                header=(
                    # "relevant ones first", matching the vector tier's wording, because
                    # ``order_by_request_relevance`` runs above: claiming "newest first"
                    # here would describe the pre-sort order, and the sort deliberately
                    # lets an OLDER relevant finding outrank newer irrelevant ones. The
                    # sort is stable, so equal-overlap rows -- and a store with no
                    # overlap at all -- still read newest-first underneath.
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
        return directive_block + experience_block
