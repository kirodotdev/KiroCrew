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
from kiro_crew.project_scope import canonical_scope, project_scope_satisfied

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
# ``KIROCREW_HOME`` only insofar as this fallback is rarely reached — the normal
# path resolves through ``config_dir()``, which does honor the override.
_DEFAULT_DIR = Path.home() / ".kiro" / "crew"
_LESSONS_FILE = "lessons.jsonl"
_MAX_LESSONS_IN_CONTEXT = 50
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


# ── Storage ──


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
            if is_sensitive_path(str(base_dir)):
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
        self._lock = _lock_for(self._path)
        # mtime-based cache: (mtime, lessons)
        self._cache: tuple[float, list[Lesson]] | None = None

    def _write_all(self, lessons: list[Lesson]) -> None:
        """Replace the file atomically. The caller MUST hold ``self._lock``.

        tmp + ``os.replace`` rather than ``write_text`` for two reasons. A reader
        can observe this file without the lock (``load_all`` takes none), and
        ``atomic_write`` renames a unique temp file over the target, so a reader sees
        either the whole old file or the whole new one -- never a half-written one.
        That is what makes the unlocked read in ``load_all`` safe, so no lock has to
        be added there, and a crash mid-write can no longer truncate the store.

        Uses the repo's ``atomic_write`` rather than a hand-rolled temp + rename.
        Hand-rolling it re-introduced three problems the shared helper already
        solves: a temp name that two writers could collide on (it uses
        ``tempfile.mkstemp``), a bare ``os.replace`` that raises ``PermissionError``
        when Windows Search or AV holds a handle (it uses ``replace_with_retry``),
        and a replacement inode carrying umask permissions instead of the store's
        (it applies ``mode`` via ``fchmod_safe``).

        The mode is read off the existing file so a restrictive store stays
        restrictive -- ``write_text`` used to preserve it implicitly by reusing the
        inode, and swapping the inode silently dropped it. A store being created for
        the first time gets ``0o600``: lesson text is personal content, and nothing
        else needs to read it.

        ``newline=""`` keeps the bytes exact. The default would apply
        universal-newline translation on write, and this file is read, edited and
        written back on every save.
        """
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except OSError:
            mode = 0o600
        atomic_write(
            self._path,
            "".join(json.dumps(asdict(le)) + "\n" for le in lessons),
            mode=mode,
            newline="",
        )
        self._cache = None  # invalidate

    def save(self, lesson: Lesson) -> None:
        """Insert *lesson*, skipping a rule that is already stored.

        Deliberately does NOT enrich. Most callers here are automatic --
        consolidation, task-runner extraction, onboarding import -- and an
        automatic writer must not replace a NOT-clause a human authored. Only an
        explicit refinement (the /api/lessons route, ``kirocrew learn add``)
        should attach a clause, and those call :meth:`save_or_enrich`.

        Kept returning ``None`` so every existing caller is unaffected.
        """
        self._insert_or_enrich(lesson, enrich=False)

    def save_or_enrich(self, lesson: Lesson) -> str:
        """Insert *lesson*, or attach its NOT-clause to the record holding the same
        rule, in ONE lock acquisition. Returns ``inserted``/``enriched``/``unchanged``.

        For EXPLICIT refinement only -- see :meth:`save` for why automatic writers
        must not reach this.

        Re-submitting a rule to attach a NOT-clause used to store nothing: the
        duplicate check matched on the rule alone and returned before looking at
        ``negative``, so the clause was dropped behind an HTTP 200.
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
                updated.append(replace(lesson, negative=wanted_negative, repo_scope=wanted_scope))
                if len(updated) > _MAX_LESSONS_TOTAL:
                    updated = updated[-_MAX_LESSONS_TOTAL:]
            self._write_all(updated)
        logger.info("%s lesson: %s", outcome.capitalize(), lesson.rule)
        return outcome

    def remove(self, rule_substring: str) -> bool:
        """Remove lessons whose rule contains *rule_substring*. Returns True if any removed.

        Holds the lock. It previously did an unlocked read-modify-write, so a
        concurrent ``save`` could be lost outright -- and without that lock the
        atomicity :meth:`save_or_enrich` claims would not actually hold.
        """
        with self._lock:
            lessons = self.load_all()
            lower = rule_substring.lower()
            kept = [le for le in lessons if lower not in le.rule.lower()]
            if len(kept) == len(lessons):
                return False
            self._write_all(kept)
        return True

    def load_all(self) -> list[Lesson]:
        """Load all lessons from the JSONL file. Uses mtime-based caching."""
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
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        self._cache = (mtime, lessons)
        return lessons

    def _applicable(self, lessons: list[Lesson], project_dir: str | Path | None) -> list[Lesson]:
        """Drop lessons whose ``repo_scope`` does not cover *project_dir*.

        A lesson with no scope applies everywhere, so an existing store is
        unaffected. A scoped one is withheld unless the session's project is
        positively inside the named tree -- the gate fails closed, so a surface
        with no project loses the scoped lessons rather than inheriting another
        repository's rules.
        """
        return [
            le
            for le in lessons
            if not le.repo_scope or project_scope_satisfied(le.repo_scope, project_dir)
        ]

    def get_context(self, project_dir: str | Path | None = None) -> str:
        """Format lessons as context for injection into prompts.

        *project_dir* is the session's active project, used only by the
        ``repo_scope`` gate; omitting it withholds every scoped lesson.
        """
        lessons = self._applicable(self.load_all(), project_dir)
        if not lessons:
            return ""

        lessons = lessons[-_MAX_LESSONS_IN_CONTEXT:]

        lines = [
            "[Learned corrections — user-taught rules from past mistakes.\n"
            "ALWAYS follow these. They override default behavior.]"
        ]
        for lesson in lessons:
            entry = f"- {lesson.rule}"
            if lesson.negative:
                entry += f" — {lesson.negative}"
            lines.append(entry)
        lines.append("[End of learned corrections]\n")
        return "\n".join(lines)
