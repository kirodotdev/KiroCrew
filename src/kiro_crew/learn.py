"""Lesson store — persistent corrections and preferences.

Lessons are saved via the ``kirocrew learn`` CLI (called by the LLM via bash)
and loaded into every session's context alongside memory and skills.

Storage: ``<config_dir>/lessons.jsonl`` (append-only JSONL).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

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


# ── Types ──


@dataclass
class Lesson:
    """A single learned correction."""

    ts: str
    rule: str
    category: str  # "tool", "preference", "knowledge"
    negative: str | None = None


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
        self._lock = threading.Lock()
        # mtime-based cache: (mtime, lessons)
        self._cache: tuple[float, list[Lesson]] | None = None

    def save(self, lesson: Lesson) -> None:
        """Append a lesson, skipping near-duplicates and pruning if over limit."""
        with self._lock:
            existing = self.load_all()
            new_lower = lesson.rule.lower().strip()
            for ex in existing:
                if ex.rule.lower().strip() == new_lower:
                    logger.debug("Skipping duplicate lesson: %s", lesson.rule)
                    return
            existing.append(lesson)
            if len(existing) > _MAX_LESSONS_TOTAL:
                existing = existing[-_MAX_LESSONS_TOTAL:]
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                "".join(json.dumps(asdict(le)) + "\n" for le in existing),
                encoding="utf-8",
            )
            self._cache = None  # invalidate
        logger.info("Saved lesson: %s", lesson.rule)

    def remove(self, rule_substring: str) -> bool:
        """Remove lessons whose rule contains *rule_substring*. Returns True if any removed."""
        lessons = self.load_all()
        lower = rule_substring.lower()
        kept = [le for le in lessons if lower not in le.rule.lower()]
        if len(kept) == len(lessons):
            return False
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            "".join(json.dumps(asdict(le)) + "\n" for le in kept), encoding="utf-8"
        )
        self._cache = None  # invalidate
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
                lessons.append(
                    Lesson(
                        ts=data.get("ts", ""),
                        rule=data.get("rule", ""),
                        category=data.get("category", "knowledge"),
                        negative=data.get("negative"),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        self._cache = (mtime, lessons)
        return lessons

    def get_context(self) -> str:
        """Format lessons as context for injection into prompts."""
        lessons = self.load_all()
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
