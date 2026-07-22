"""Skills loader — markdown skill files for agent capabilities."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.hooks import validate_file_path
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.skill_usage import SKILL_USAGE_FILENAME, SkillUsageLedger

logger = logging.getLogger(__name__)


SKILLS_DIR_NAME = "skills"
_MIN_TRIGGER_OVERLAP = 0.7

# Lazy-load ranking (Mesh skill lazy-load): the session-start skills block only
# affords a bounded slice of the context budget, so on-demand skills are ranked
# by usage and summarized top-down; the tail is discoverable via `skill_search`.
# Per-skill description is truncated to this many chars in the summary line so a
# few verbose descriptions can't dominate the block.
_SHORT_DESC_CHARS = 160
# A skill whose file mtime is within this window gets a recency boost in the
# ranking so a freshly-added, never-used skill still surfaces instead of being
# starved by the rich-get-richer usage ordering.
_NEW_SKILL_BOOST_WINDOW_SECS = 7 * 24 * 60 * 60

# ── $skill inline trigger ──
# A ``$skillname`` token anywhere in a user message explicitly loads that skill,
# across all three sources (kirocrew builtin, workspace, extra paths).
# Resolution is allowlist-only: the token must match the last path segment of an
# already-enumerated skill key (per input-validation guidance — no path
# is ever constructed from the raw token, which structurally blocks traversal like
# ``$../../etc/passwd``). The charset is deliberately lowercase-led so shell-style
# tokens (``$PATH``, ``$5``) and prose ($variable mid-sentence in caps) don't match
# real skill slugs.
#   (?<![\w$])  — not preceded by a word char or another $ (avoids ``foo$bar``, ``$$x``)
#   [a-z0-9]    — must start with a lowercase letter or digit
#   [a-z0-9/_-]* — slug body: lowercase, digits, slash (nested keys), underscore, hyphen
_DOLLAR_SKILL_PATTERN = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")
# Cap how many distinct $skills one message may expand — bounds prompt growth and
# matches the spirit of the per-message trigger cap.
_MAX_DOLLAR_SKILLS = 5
# Cache the discovered skill-file list for this long. get_triggered_skills runs
# on EVERY message; without this it os.walk()s the skills dir + every extra
# path per message. Skills change rarely (add/remove via setup or sync), so a
# short TTL keeps trigger matching off the per-message filesystem hot path
# while still picking up new skills within a few seconds.
_ITER_CACHE_TTL_SECS = 5.0

# ── Auto skill creation ──

# Namespace for auto-generated skills — keeps them out of the way of
# hand-authored skills.  Final path: ``~/.kirocrew/skills/auto/<name>/SKILL.md``.
AUTO_SKILL_NAMESPACE = "auto"

# Frontmatter field used to mark a skill as auto-generated.  Absence means
# the skill is hand-authored (or legacy, pre-feature).
AUTO_SKILL_SOURCE_VALUE = "auto"

# Cap synthesized procedure markdown at 10 KB.  Longer outputs indicate
# the aux LLM failed to stay on-task and should be rejected.
AUTO_SKILL_MAX_PROCEDURE_CHARS = 10_240

# Regex for auto-generated skill name segment validation.  Deliberately
# restrictive — we control the generator so we don't need to accept
# arbitrary unicode.  ``_safe_name`` already rejects ``..`` and ``\``;
# this is an additional sanitization layer specific to auto-gen.
_AUTO_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# Bundled fallback — inside the kiro_crew package
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"


@dataclass(frozen=True)
class AutoSkillProvenance:
    """Immutable provenance record for an auto-generated skill.

    Serialized into the SKILL.md YAML frontmatter (``source: auto``,
    ``session_key``, ``created_at``, ``refined_at``, ``reuse_count``) so
    operators can always see how a skill was produced and when it was
    last refined.  Absence of ``source: auto`` identifies the skill as
    hand-authored.
    """

    session_key: str
    created_at: str  # ISO 8601 UTC
    refined_at: str = ""  # ISO 8601 UTC; empty until first refinement
    reuse_count: int = 0

    @staticmethod
    def now_iso() -> str:
        """Return the current time as an ISO 8601 UTC string."""
        return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    def to_frontmatter_lines(self) -> list[str]:
        """Serialize to the YAML key/value lines used in SKILL.md frontmatter."""
        lines = [
            f"source: {AUTO_SKILL_SOURCE_VALUE}",
            f"session_key: {self.session_key}",
            f"created_at: {self.created_at}",
        ]
        if self.refined_at:
            lines.append(f"refined_at: {self.refined_at}")
        if self.reuse_count:
            lines.append(f"reuse_count: {self.reuse_count}")
        return lines


def _auto_name_from_title(raw: str) -> str:
    """Convert a free-form title into a safe ``auto/<slug>`` skill name.

    Strategy:
    - lowercase
    - replace any run of non-alphanumerics with a single hyphen
    - strip leading/trailing hyphens
    - truncate to 62 chars (leaves room for uniqueness suffix)

    Returns the slug component only; caller prepends the namespace.
    Returns an empty string if the input can't be sanitized.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:62].rstrip("-")
    if not _AUTO_NAME_PATTERN.match(slug):
        return ""
    return slug


def _build_auto_skill_content(
    *,
    slug: str,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
) -> str:
    """Render a complete ``SKILL.md`` body for an auto-generated skill.

    Layout::

        ---
        name: auto/<slug>
        description: <description>
        triggers: <comma-separated triggers>
        source: auto
        session_key: <session>
        created_at: <iso8601>
        refined_at: <iso8601>      # omitted if empty
        reuse_count: <int>         # omitted if 0
        ---

        # <slug> (auto-generated)

        <procedure_md>

    The leading ``---`` keeps this compatible with existing frontmatter
    parsing in ``SkillsLoader._parse_frontmatter``.  YAML values are
    single-line and newline-stripped to stay within the parser's
    ``key: value`` line format.
    """
    name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
    desc_safe = re.sub(r"\s+", " ", description or "").strip() or name
    triggers_safe = re.sub(r"\s+", " ", triggers or "").strip()
    header_lines = [
        "---",
        f"name: {name}",
        f"description: {desc_safe}",
    ]
    if triggers_safe:
        header_lines.append(f"triggers: {triggers_safe}")
    header_lines.extend(provenance.to_frontmatter_lines())
    header_lines.append("---")
    # Normalize line endings, strip leading/trailing blanks so diffs
    # between revisions stay readable.
    body = procedure_md.replace("\r\n", "\n").strip()
    return "\n".join(header_lines) + "\n\n" + body + "\n"


def _project_skills_dir() -> Path | None:
    """Return project-level skills/ dir from KIROCREW_PROJECT_DIR, or None."""
    val = os.environ.get("KIROCREW_PROJECT_DIR")
    if val:
        p = Path(val) / "skills"
        if p.is_dir():
            return p
    return None


def _iter_skill_files(base: Path) -> list[tuple[str, Path]]:
    """Recursively find all SKILL.md files under *base*.

    Returns ``(relative_name, skill_file_path)`` pairs sorted by name.
    The relative name uses ``/`` as separator (e.g. ``utils/tiny-url``).

    Uses os.walk with followlinks=True because Python 3.12's Path.rglob
    does not follow symlinks.
    """
    results: list[tuple[str, Path]] = []
    if not base.exists():
        return results
    real_base = os.path.realpath(base)
    seen_real: set[str] = set()
    for dirpath, _dirs, files in os.walk(base, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_real:
            _dirs.clear()  # prune this branch — symlink loop
            continue
        seen_real.add(real)
        # Path containment: ensure we stay within the skill base directory
        try:
            Path(real).relative_to(real_base)
        except ValueError:
            _dirs.clear()
            continue
        if is_sensitive_path(real):
            _dirs.clear()  # never traverse into credential stores
            continue
        if "SKILL.md" in files:
            skill_file = Path(dirpath) / "SKILL.md"
            if is_sensitive_path(os.path.realpath(str(skill_file))):
                continue
            rel = skill_file.parent.relative_to(base)
            name = str(rel).replace("\\", "/")
            results.append((name, skill_file))
    return sorted(results, key=lambda x: x[0])


def _ensure_builtin_skills(base: Path) -> None:
    """Sync built-in skills: copy new/updated, remove stale.

    Supports nested directories (e.g. ``utils/tiny-url/SKILL.md``).
    Copies the entire skill directory (scripts, assets, etc.), not just SKILL.md.
    Removes skills from *base* that no longer exist in any source.
    """
    # Collect all source skill names
    source_names: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            source_names.add(name)
            src_dir = src_file.parent
            dest_dir = base / name
            dest_file = dest_dir / "SKILL.md"
            if not dest_file.exists() or src_file.stat().st_mtime > dest_file.stat().st_mtime:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(src_dir, dest_dir)
                logger.info("Synced skill: %s", name)

    # Remove known stale builtin skills (replaced by MCP tools)
    stale_builtins = {"learn", "subagent", "cron", "kirocrew-core"}
    if base.exists():
        for name in stale_builtins:
            stale = base / name
            if stale.is_dir():
                shutil.rmtree(stale)
                logger.info("Removed stale builtin skill: %s", name)


def skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def aim_skills_dir() -> Path:
    """Root of AIM-installed skills (``~/.aim/skills``).

    Factored out (rather than inlined in ``SkillsLoader.__init__``) so tests can
    monkeypatch it and stay hermetic — otherwise every loader construction would
    pick up the developer's real AIM skills.
    """
    return Path.home() / ".aim" / "skills"


class SkillsLoader:
    """Load skill markdown files from ~/.kirocrew/skills/.

    Supports nested directories. Each skill is identified by its
    relative path from the skills root (e.g. ``utils/tiny-url``).

    Directory layout::

        ~/.kirocrew/skills/
        ├── learn/SKILL.md
        ├── subagent/SKILL.md
        ├── code/
        │   ├── code-review/SKILL.md
        │   └── code-task-generation/SKILL.md
        └── utils/
            ├── url-shortener/SKILL.md
            └── mcp-debug/SKILL.md
    """

    def __init__(
        self,
        skills_path: Path | None = None,
        install_builtins: bool = True,
        config: KiroCrewConfig | None = None,
    ):
        self._dir = skills_path or skills_dir()
        if install_builtins:
            _ensure_builtin_skills(self._dir)
        # Cache: path → (mtime, parsed_frontmatter)
        self._fm_cache: dict[str, tuple[float, dict[str, str]]] = {}
        # TTL cache of the discovered (name, path) list — avoids an os.walk per
        # message in get_triggered_skills. (monotonic_deadline, results)
        self._iter_cache: tuple[float, list[tuple[str, Path]]] | None = None
        # Extra skill paths from config (config injectable for testing)
        cfg = config or KiroCrewConfig.load()
        # Snapshot the per-message trigger cap here so get_triggered_skills (the
        # only caller, run on EVERY message) doesn't re-load + re-validate the
        # whole config just to read one int. Matches the eventual-consistency of
        # _extra_paths below — both are resolved once from the construction-time
        # config and refreshed when the loader is rebuilt (per gateway).
        self._max_triggered = cfg.skills.max_triggered
        self._extra_paths: list[Path] = []
        for p in cfg.skills.extra_paths:
            resolved = Path(p).expanduser().resolve()
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive extra skill path: %s", p)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
            else:
                logger.debug("Extra skill path does not exist: %s", p)
        # Implicitly include the AIM skills root (~/.aim/skills) as a lowest-
        # precedence source. The dashboard's /api/skills (and thus the $skill
        # autocomplete picker) lists AIM-installed skills, so the $skill resolver
        # MUST be able to resolve them too — otherwise the picker offers a token
        # the backend can't load. We scan the dir directly (not the async `aim skills list`
        # CLI) so this stays sync + cache-friendly; it's the same on-disk truth the CLI reports.
        # Skipped if already covered by a configured extra_path, missing, or sensitive.
        aim_skills_root = aim_skills_dir().resolve()
        if (
            aim_skills_root not in self._extra_paths
            and aim_skills_root.is_dir()
            and not is_sensitive_path(str(aim_skills_root))
        ):
            self._extra_paths.append(aim_skills_root)

        # Edition-contributed skill paths (CPP seam). A companion returns extra
        # SKILL.md source roots via McpToolingProvider.extra_skills(); the public
        # Default returns [] so this is a no-op for the standalone edition.
        # Lowest precedence (appended last, after local + AIM), sensitivity- and
        # existence-checked exactly like the configured extra_paths. Deferred
        # context read via the sel.py pattern so skills.py never imports the
        # platform package at module load; fails closed to no extra paths.
        from kiro_crew.platform.context import current_context, safe_context_call

        edition_skill_paths: list[Path] = safe_context_call(
            lambda: list(current_context().mcp_tooling.extra_skills()),
            fallback_factory=list,
            log_message="extra_skills lookup failed; using none",
        )
        for edition_path in edition_skill_paths:
            resolved = Path(edition_path).expanduser().resolve()
            if resolved in self._extra_paths:
                continue
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive edition skill path: %s", edition_path)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
            else:
                logger.debug("Edition skill path does not exist: %s", edition_path)

        # Persistent usage ledger for hotness-ranked lazy skill injection.
        # Co-located with the skills root's parent (the KiroCrew home) so it
        # travels with runtime state. Best-effort: a failure here must not break
        # skill loading — ranking then falls back to recency/unweighted order.
        self._usage: SkillUsageLedger | None
        try:
            self._usage = SkillUsageLedger(self._dir.parent / SKILL_USAGE_FILENAME)
        except Exception:  # pragma: no cover — ledger is best-effort telemetry
            logger.warning(
                "skill-usage: ledger init failed; ranking falls back to unweighted",
                exc_info=True,
            )
            self._usage = None

    def _iter(self) -> list[tuple[str, Path]]:
        """Return all ``(name, skill_file)`` pairs, TTL-cached.

        Local skills take precedence over extra paths. The underlying os.walk
        is cached for ``_ITER_CACHE_TTL_SECS`` because this runs on every
        message via ``get_triggered_skills`` — re-walking the skills tree (plus
        every extra path) per message was a per-message latency cost.
        """
        cached = self._iter_cache
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        results = self._iter_uncached()
        self._iter_cache = (time.monotonic() + _ITER_CACHE_TTL_SECS, results)
        return results

    def _iter_uncached(self) -> list[tuple[str, Path]]:
        """Walk the skills dir + extra paths once (no caching)."""
        results = _iter_skill_files(self._dir)
        seen = {name for name, _ in results}
        for root in self._extra_paths:
            for name, skill_file in _iter_skill_files(root):
                if name in seen:
                    continue
                # Route through hooks validation (resolves symlinks + sensitive
                # check) so files read later during trigger matching are vetted.
                resolved = validate_file_path(str(skill_file))
                if resolved is None:
                    continue
                results.append((name, Path(resolved)))
                seen.add(name)
        return results

    def _invalidate_iter_cache(self) -> None:
        """Drop cached skill state so a just-written mutation is visible now.

        Called by create/update/delete/refresh. Clears both the skill-file list
        cache AND the mtime-keyed frontmatter cache: an in-place ``update_skill``
        can overwrite a file within the same filesystem mtime tick as the prior
        read, so keying the frontmatter cache on mtime alone would return the
        stale parse. Dropping it here keeps the mutator's edit immediately
        reflected in ``list_skills`` / ``get_triggered_skills``.
        """
        self._iter_cache = None
        self._fm_cache.clear()

    def _cached_frontmatter(self, path: Path) -> dict[str, str]:
        """Parse frontmatter with mtime-based caching."""
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._fm_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        meta = self._parse_frontmatter(path)
        self._fm_cache[key] = (mtime, meta)
        return meta

    def list_skills(self) -> list[dict]:
        """Return list of skill metadata dicts with key, name, description, path, dir, always."""
        skills: list[dict] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            skills.append(
                {
                    "key": name,
                    "name": meta.get("name", name),
                    "description": meta.get("description", name),
                    "path": str(skill_file),
                    "dir": str(skill_file.parent),
                    "always": meta.get("always", "").lower() == "true",
                }
            )
        return skills

    @staticmethod
    def _safe_name(name: str) -> bool:
        """Return True if skill name is safe (no path traversal)."""
        return bool(name) and ".." not in name and "\\" not in name

    def load_skill(self, name: str) -> str | None:
        """Load a single skill's content by name (supports nested paths)."""
        if not self._safe_name(name):
            return None
        _t0 = time.monotonic()
        skill_file = self._dir / name / "SKILL.md"
        if skill_file.exists():
            content = skill_file.read_text(encoding="utf-8")
            self._emit_lazy_load_metric(_t0, hit=True)
            return content
        # Check extra paths
        for extra in self._extra_paths:
            skill_file = extra / name / "SKILL.md"
            if skill_file.exists():
                resolved = validate_file_path(str(skill_file))
                if resolved is None:
                    logger.warning("Refusing to load skill from sensitive path: %s", skill_file)
                    continue
                content = Path(resolved).read_text(encoding="utf-8")
                self._emit_lazy_load_metric(_t0, hit=True)
                return content
        self._emit_lazy_load_metric(_t0, hit=False)
        return None

    @staticmethod
    def _emit_lazy_load_metric(t0: float, *, hit: bool) -> None:
        """Best-effort OTEL emit for on-demand skill body loads."""
        try:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            attrs: dict[str, str | int | bool | float] = {"hit": hit}
            get_recorder().histogram(
                "kirocrew.skill.lazy_load.duration",
                elapsed_ms,
                unit="ms",
                attrs=attrs,
            )
            get_recorder().counter("kirocrew.skill.lazy_load.count", attrs=attrs)
        except Exception:  # never let telemetry break skill loading
            pass

    def create_skill(self, name: str, content: str) -> bool:
        """Create a new skill directory with SKILL.md.  Returns True on success."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if skill_dir.exists():
            return False
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # so the new skill shows in list_skills() now
        logger.info("Created skill: %s", name)
        return True

    def update_skill(self, name: str, content: str) -> bool:
        """Overwrite an existing skill's SKILL.md.  Returns True if found."""
        if not self._safe_name(name):
            return False
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            return False
        skill_file.write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
        logger.info("Updated skill: %s", name)
        return True

    def delete_skill(self, name: str) -> bool:
        """Delete a skill directory.  Returns True if found and removed."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if not skill_dir.is_dir():
            return False
        shutil.rmtree(skill_dir)
        self._invalidate_iter_cache()  # so the removal is reflected in list_skills() now
        logger.info("Deleted skill: %s", name)
        return True

    # ── Auto skill creation ──

    def is_auto_generated(self, name: str) -> bool:
        """Return True if *name* refers to a skill in the auto namespace.

        Cheap filesystem check (no frontmatter parse) based on the
        directory prefix.  Used for filtering and safety guards (e.g.
        refusing to overwrite a hand-authored skill from an auto-update
        path).
        """
        if not self._safe_name(name):
            return False
        return name.startswith(f"{AUTO_SKILL_NAMESPACE}/")

    def find_similar(
        self,
        description: str,
        threshold: float = 0.85,
        *,
        exclude: str = "",
    ) -> str | None:
        """Return the name of an existing skill whose description overlaps with *description*.

        Uses case-insensitive word-set Jaccard-like overlap against every
        loaded skill's ``description`` frontmatter value:

            score = |words(a) ∩ words(b)| / |words(a) ∪ words(b)|

        Intended for deduplication of auto-generated skills — we don't
        want the agent producing a near-duplicate of an existing skill.
        Returns the first skill whose score ≥ *threshold*, or ``None``
        if nothing matches.

        *exclude* lets callers suppress self-matches during refinement.
        """
        if not description:
            return None
        query_words = set(re.findall(r"\w+", description.lower()))
        if not query_words:
            return None
        best_name: str | None = None
        best_score: float = 0.0
        for name, skill_file in self._iter():
            if exclude and name == exclude:
                continue
            meta = self._cached_frontmatter(skill_file)
            existing = meta.get("description", "")
            if not existing:
                continue
            existing_words = set(re.findall(r"\w+", existing.lower()))
            if not existing_words:
                continue
            intersection = query_words & existing_words
            union = query_words | existing_words
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score:
                best_score = score
                best_name = name
        if best_score >= threshold:
            return best_name
        return None

    def create_auto_skill(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
    ) -> str | None:
        """Write a new auto-generated skill under ``auto/<slug>/SKILL.md``.

        Returns the full skill name (``auto/<slug>``) on success, or
        ``None`` if the slug is invalid or the skill already exists.

        Caller is responsible for:
        - Running ``find_similar()`` first to avoid near-duplicates.
        - Passing already-redacted ``procedure_md`` (sensitive data is
          the caller's responsibility — this method is pure I/O).
        - Enforcing the ``skills.auto_create_from_sessions`` config flag.
        """
        if not _AUTO_NAME_PATTERN.match(slug):
            logger.warning("Rejected auto skill: slug %r failed validation", slug)
            return None
        if len(procedure_md) > AUTO_SKILL_MAX_PROCEDURE_CHARS:
            logger.warning(
                "Rejected auto skill %s: procedure %d chars exceeds cap %d",
                slug,
                len(procedure_md),
                AUTO_SKILL_MAX_PROCEDURE_CHARS,
            )
            return None
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        skill_dir = self._dir / name
        if skill_dir.exists():
            logger.info("Auto skill %s already exists, skipping", name)
            return None
        content = _build_auto_skill_content(
            slug=slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # new skill visible to trigger matching now
        logger.info("Created auto skill: %s", name)
        return name

    def update_auto_skill(
        self,
        name: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
    ) -> bool:
        """Update an existing auto-generated skill with a refined procedure.

        Refuses to overwrite skills NOT in the auto namespace — protects
        hand-authored skills from being clobbered by the refine path.
        Returns True on success.

        Caller is responsible for passing already-redacted ``procedure_md``.
        """
        if not self.is_auto_generated(name):
            logger.warning(
                "Refusing to auto-refine non-auto skill: %s (not in %s/)",
                name,
                AUTO_SKILL_NAMESPACE,
            )
            return False
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            return False
        if len(procedure_md) > AUTO_SKILL_MAX_PROCEDURE_CHARS:
            logger.warning(
                "Refusing to refine %s: procedure %d chars exceeds cap %d",
                name,
                len(procedure_md),
                AUTO_SKILL_MAX_PROCEDURE_CHARS,
            )
            return False
        # Preserve the original creation timestamp — refinement must not
        # clobber provenance history.  Callers typically pass a fresh
        # provenance with created_at=now; we override from the existing
        # frontmatter here so the write path is authoritative.  Uses
        # ``dataclasses.replace`` because AutoSkillProvenance is frozen.
        existing_meta = self._cached_frontmatter(skill_file)
        original_created_at = existing_meta.get("created_at")
        if original_created_at:
            provenance = replace(provenance, created_at=original_created_at)
        slug = name.split("/", 1)[1]
        content = _build_auto_skill_content(
            slug=slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )
        skill_file.write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # so the refined triggers/description apply now
        logger.info("Refined auto skill: %s", name)
        return True

    def list_auto_skills(self) -> list[dict]:
        """Return metadata dicts for all skills under the auto namespace.

        Dashboard / CLI consumers use this to display provenance to
        users.  Hand-authored skills are excluded.
        """
        return [s for s in self.list_skills() if s["key"].startswith(f"{AUTO_SKILL_NAMESPACE}/")]

    def get_always_skills(self) -> list[str]:
        """Return names of skills marked ``always: true`` in frontmatter."""
        result: list[str] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            if meta.get("always", "").lower() == "true":
                result.append(name)
        return result

    def get_triggered_skills(self, text: str) -> list[str]:
        """Return names of skills whose triggers match the given text.

        Uses word-overlap matching with multi-word trigger phrases and
        negative keywords.  Triggers are comma-separated phrases in the
        ``triggers`` frontmatter field.  A phrase prefixed with ``!`` is a
        negative trigger — if *any* negative trigger matches, the skill is
        excluded regardless of positive matches.

        Returns up to ``max_triggered`` skills sorted by best overlap score.
        """
        text_words = set(re.findall(r"\w+", text.lower()))

        scored: list[tuple[str, float]] = []
        # Skills a negative trigger actively excluded — a permission DENY that
        # must still be audited (see the audit event below).
        negated_skills: list[str] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            if meta.get("always", "").lower() == "true":
                continue
            triggers = meta.get("triggers", "")
            if not triggers:
                continue

            # Split into positive and negative triggers
            negated = False
            best_overlap = 0.0
            for trigger in triggers.split(","):
                trigger = trigger.strip().lower()
                if not trigger:
                    continue
                # Negative trigger: "!search" excludes if "search" words match.
                # Don't break — keep scoring the remaining positive triggers so
                # best_overlap is correct regardless of trigger order; the DENY
                # audit below needs it to know the skill would otherwise have
                # triggered (e.g. "!test, shorten url" must still compute the
                # "shorten url" overlap).
                if trigger.startswith("!"):
                    neg_words = set(re.findall(r"\w+", trigger[1:]))
                    if neg_words and neg_words <= text_words:
                        negated = True
                else:
                    trigger_words = set(re.findall(r"\w+", trigger))
                    if not trigger_words:
                        continue
                    overlap = len(trigger_words & text_words) / len(trigger_words)
                    best_overlap = max(best_overlap, overlap)

            # Only record a negation as a DENY when the skill would otherwise
            # have triggered (positive overlap met the threshold) — that's the
            # case where the negative trigger actually changed the outcome.
            if negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                negated_skills.append(name)
            elif not negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                scored.append((name, best_overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        triggered = [name for name, _ in scored[: self._max_triggered]]

        # Record usage — triggered skills are injected full-body this turn, so
        # this is the authoritative "skill was used" signal that feeds the
        # lazy-load hotness ranking in get_context.
        for name in triggered:
            self._record_use(name)

        # Emit ONE audit event for the matched + denied sets rather than one per
        # skill. Previously this wrote a SEL entry for every skill (incl. every
        # non-match) on every message — N synchronous writes per message that
        # dominated the per-message cost. The security-relevant signals are which
        # skills were injected (permission grant) and which were excluded by a
        # negative trigger (permission deny); both are captured here. Skipped
        # entirely only when nothing triggered and nothing was denied (the
        # common case).
        if triggered or negated_skills:
            metadata = {"text_hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
            if triggered:
                metadata["skills"] = ",".join(triggered)
            if negated_skills:
                metadata["negated"] = ",".join(negated_skills)
            sel().log_tool_invocation(
                session_key="skills",
                tool_name="skill_trigger",
                tool_kind="permission",
                outcome="triggered" if triggered else "denied",
                metadata=metadata,
            )
        return triggered

    def get_context(self, budget: int | None = None) -> str:
        """Build skills context for prompt injection (lazy-loaded).

        Pinned skills (``always: true`` frontmatter) get full content, always —
        this is the "core" set (mark core skills ``always: true`` to pin
        them). The remaining on-demand skills are ranked by usage (hottest
        first, with a recency boost for freshly-added skills) and summarized
        top-down until *budget* chars are consumed; the long tail is left
        discoverable via the ``skill_search`` tool, the ``$skillname`` inline
        token, ``cat``, and the per-message trigger auto-loader. This bounds the
        block so no single section can blow the context budget.

        ``budget=None`` (opt-in OFF, the default) returns the LEGACY full-dump
        block — every on-demand skill summarized, unranked and untruncated,
        byte-for-byte the pre-lazy-load behavior. An integer ``budget`` (opt-in
        ON) switches to the bounded, usage-ranked top-K described above.
        """
        all_skills = self.list_skills()
        if not all_skills:
            return ""
        if budget is None:
            return self._legacy_context(all_skills)
        # get_always_skills() returns the _iter() identifier — the same value
        # list_skills() exposes as "key" (the dir-relative path, e.g.
        # "team-capabilities/build-helper"), NOT the frontmatter "name". So the
        # pinned check below, _record_use() (also called with the _iter
        # identifier), and _rank_key()'s score(s["key"]) are all consistently
        # keyed by "key" — there is no key/name mismatch here.
        pinned = set(self.get_always_skills())

        parts: list[str] = []

        # Pinned (core / always:true): full content, always injected.
        for s in all_skills:
            if s["key"] not in pinned:
                continue
            content = self.load_skill(s["key"])
            if content:
                stripped = self.strip_frontmatter(content)
                parts.append(f"### Skill: {s['key']}\n\n{stripped}")

        # On-demand: rank by usage (hottest first), fill a summary block up to
        # `budget`, then point at skill_search for the tail.
        on_demand = [s for s in all_skills if s["key"] not in pinned]
        if on_demand:
            ranked = sorted(on_demand, key=self._rank_key, reverse=True)
            header = (
                "## Available Skills\n\n"
                "The most-used skills are listed below. If a request relates to "
                "one, read its full file with `cat <path>` first. To run a "
                "skill's scripts, `cd` into its directory. Relevant skills also "
                "auto-load when your message matches their triggers.\n\n"
            )
            # Reserve room for everything that surrounds the summary lines so the
            # FINAL returned string stays within `budget` and the caller's backstop
            # truncation never chops the trailing "...N more / skill_search" footer:
            # the "[Skills:]"/"[End of skills]" wrapper, the "---" separators, the
            # pinned parts already in `parts`, the header, and the footer line.
            footer_reserve = (
                len(
                    f"- _...and {len(ranked)} more skill(s) not shown here. Find them "
                    f"with the `skill_search` tool (grep by keyword), the "
                    f"`$skillname` inline token, or `cat` a known path._"
                )
                + 1
            )  # +1 for the "\n" join before the footer
            wrap_overhead = len("[Skills:]\n") + len("\n[End of skills]\n\n")
            sep_overhead = len("\n\n---\n\n") * len(parts)
            lines: list[str] = []
            used = wrap_overhead + sep_overhead + sum(len(p) for p in parts) + len(header)
            shown = 0
            for s in ranked:
                line = (
                    f"- **{s['name']}**: {self._short_desc(s['description'])} "
                    f"-> `{s['path']}` (dir: `{s['dir']}`)"
                )
                if (
                    budget is not None
                    and shown > 0
                    and used + len(line) + 1 + footer_reserve > budget
                ):
                    break
                lines.append(line)
                used += len(line) + 1
                shown += 1
            remaining = len(ranked) - shown
            if remaining > 0:
                lines.append(
                    f"- _...and {remaining} more skill(s) not shown here. Find them "
                    f"with the `skill_search` tool (grep by keyword), the "
                    f"`$skillname` inline token, or `cat` a known path._"
                )
            parts.append(header + "\n".join(lines))

        return "[Skills:]\n" + "\n\n---\n\n".join(parts) + "\n[End of skills]\n\n"

    def _legacy_context(self, all_skills: list[dict]) -> str:
        """Pre-lazy-load skills block (opt-in OFF, the default).

        Full content for pinned (``always: true``) skills + a one-line summary
        for EVERY on-demand skill, unranked and untruncated — byte-for-byte the
        behavior before the lazy-load feature, so leaving ``skills.lazy_load``
        off is a zero-impact upgrade.
        """
        always = self.get_always_skills()
        parts: list[str] = []
        # Full content for always-loaded skills
        for name in always:
            content = self.load_skill(name)
            if content:
                stripped = self.strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{stripped}")
        # Summary for on-demand skills
        on_demand = [s for s in all_skills if s["name"] not in always]
        if on_demand:
            summary_lines = [
                "## Available Skills",
                "",
                "If a user request relates to any skill below, read the full "
                "skill file first with `cat <path>` before responding.",
                "To run a skill's scripts, `cd` into its directory first.",
                "",
            ]
            for s in on_demand:
                summary_lines.append(
                    f"- **{s['name']}**: {s['description']} → `{s['path']}` (dir: `{s['dir']}`)"
                )
            parts.append("\n".join(summary_lines))
        return "[Skills:]\n" + "\n\n---\n\n".join(parts) + "\n[End of skills]\n\n"

    def _record_use(self, key: str) -> None:
        """Best-effort usage bump for the lazy-load ranking. Never raises."""
        if self._usage is None:
            return
        try:
            self._usage.record(key)
        except Exception:  # pragma: no cover — telemetry must not break injection
            pass

    def _recency_boost(self, path_str: str) -> float:
        """Return the file mtime if the skill is newer than the boost window,
        else 0.0. Lets a freshly-added, never-used skill rank above stale unused
        ones (cold-start protection) without flooding the top of the list."""
        try:
            mtime = Path(path_str).stat().st_mtime
        except OSError:
            return 0.0
        return mtime if (time.time() - mtime) < _NEW_SKILL_BOOST_WINDOW_SECS else 0.0

    def _rank_key(self, s: dict) -> tuple[float, float]:
        """Sort key for on-demand skills: (usage_hits, effective_recency).
        Higher sorts first. Falls back to recency-only if the ledger is absent."""
        boost = self._recency_boost(s["path"])
        if self._usage is None:
            return (0.0, boost)
        return self._usage.score(s["key"], recency_boost=boost)

    @staticmethod
    def _short_desc(desc: str) -> str:
        """Collapse whitespace and truncate a description for the summary line."""
        d = " ".join((desc or "").split())
        if len(d) > _SHORT_DESC_CHARS:
            return d[:_SHORT_DESC_CHARS].rstrip() + "..."
        return d

    def search_skills(self, query: str, limit: int = 20) -> list[dict]:
        """Grep skills by keyword for on-demand discovery (the skill_search tool).

        Scores each skill by how many query terms appear in its key / name /
        description; only when the metadata misses entirely does it fall back to
        grepping the skill body (bounded cost, and only on an explicit tool
        call — never per message). Results are ranked by match strength then
        usage, capped at *limit*. Does NOT record usage — searching is not using.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = [t for t in re.findall(r"\w+", q) if t]
        if not terms:
            return []
        scored: list[tuple[int, float, dict]] = []
        for s in self.list_skills():
            hay = f"{s['key']} {s['name']} {s['description']}".lower()
            meta_hits = sum(1 for t in terms if t in hay)
            body_hits = 0
            if meta_hits == 0:
                content = (self.load_skill(s["key"]) or "").lower()
                body_hits = sum(1 for t in terms if t in content)
            total = meta_hits * 10 + body_hits
            if total <= 0:
                continue
            usage = self._usage.score(s["key"])[0] if self._usage else 0.0
            scored.append((total, usage, s))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [s for _, _, s in scored[:limit]]

    def resolve_dollar_skills(self, text: str) -> list[tuple[str, str, str]]:
        """Resolve ``$skillname`` tokens in *text* to loadable skills.

        Scans *text* for ``$token`` occurrences (anywhere, multiple allowed) and
        matches each token against the **last path segment** of every enumerated
        skill key — so ``$oncall-handover`` resolves the skill whose key is
        ``WorkforceEmploymentKnowledgeBase/oncall-handover``. Matching is
        case-insensitive on the leaf.

        Security (per input-validation guidance): this is allowlist-only. The
        token is *matched against* the vetted, already-enumerated skill set from
        ``_iter()`` — no filesystem path is ever built from the raw token. A
        token like ``$../../etc/passwd`` simply matches nothing. Content is loaded
        through ``load_skill`` (which inherits ``_safe_name`` + ``validate_file_path``
        + sensitive-path gating) and frontmatter is stripped before return.

        Returns a list of ``(token, skill_name, stripped_body)`` tuples — one per
        distinct resolved skill, in first-appearance order, deduped, and capped at
        ``_MAX_DOLLAR_SKILLS``. Unknown tokens are silently skipped (left literal by
        the caller). Returns an empty list if *text* has no resolvable tokens.
        """
        if not text or "$" not in text:
            return []

        # Build leaf → full-key map once from the enumerated (allowlisted) set.
        # _iter() already applies local > workspace > AIM precedence and dedupes
        # by full key, so the first full key seen for a given leaf wins.
        leaf_to_name: dict[str, str] = {}
        for name, _path in self._iter():
            leaf = name.rsplit("/", 1)[-1].lower()
            leaf_to_name.setdefault(leaf, name)

        resolved: list[tuple[str, str, str]] = []
        seen_names: set[str] = set()
        for match in _DOLLAR_SKILL_PATTERN.finditer(text):
            token = match.group(1)
            # Match on the leaf segment of the token (supports ``$a/b`` typed by
            # the user, though the common case is a bare leaf).
            leaf = token.rsplit("/", 1)[-1].lower()
            matched: str | None = leaf_to_name.get(leaf)
            if matched is None or matched in seen_names:
                continue
            content = self.load_skill(matched)
            if content is None:
                continue
            seen_names.add(matched)
            resolved.append((token, matched, self.strip_frontmatter(content)))
            self._record_use(matched)
            if len(resolved) >= _MAX_DOLLAR_SKILLS:
                break
        return resolved

    @staticmethod
    def has_dollar_candidate(text: str) -> bool:
        """True if *text* contains at least one ``$skill``-shaped token.

        Distinguishes a genuine (if unresolved) skill-invocation attempt from
        an incidental ``$`` (e.g. ``$5``, ``$42``, ``$PATH``, a bare ``$``). The
        caller uses this to decide whether an empty ``resolve_dollar_skills``
        result is worth a ``not_found`` audit event — keeps the regex the single
        source of truth instead of duplicating it in chat_runner.

        Note: the token charset is digit-led (so a skill like ``5whys`` works via
        ``$5whys``), which means a purely numeric ``$5`` *matches the regex*. A
        bare price is not a skill attempt, so we additionally require the matched
        token to contain at least one letter before counting it as a candidate.
        """
        if not text or "$" not in text:
            return False
        return any(
            any(c.isalpha() for c in m.group(1)) for m in _DOLLAR_SKILL_PATTERN.finditer(text)
        )

    # ── Private ──

    @staticmethod
    def _parse_frontmatter(path: Path) -> dict[str, str]:
        """Parse YAML frontmatter from a markdown file (simple key: value)."""
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}
        meta: dict[str, str] = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip("\"'")
        return meta

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter from markdown."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content
