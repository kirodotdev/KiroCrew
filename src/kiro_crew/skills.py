"""Skills loader — markdown skill files for agent capabilities."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import referenced_skill_names
from kiro_crew.hooks import safe_read_file, validate_file_path
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.skill_usage import SKILL_USAGE_FILENAME, SkillUsageLedger
from kiro_crew.skills_script_validator import validate_scripts

logger = logging.getLogger(__name__)


SKILLS_DIR_NAME = "skills"
_MIN_TRIGGER_OVERLAP = 0.7


def _matches_any(path: str, globs: list[str]) -> bool:
    """True if *path* matches any fnmatch glob in *globs*.

    Used to narrow the injected skills block to an agent template's
    ``skill://`` mapping. Both sides are compared as real filesystem paths
    (the URIs are pre-expanded by ``agent_discovery.expand_skill_uri``), and a
    symlinked skill dir is tried in resolved form too so a mapping written
    against the link target still matches the catalog's listed path.
    """
    if not path:
        return False
    if any(fnmatch.fnmatch(path, g) for g in globs):
        return True
    try:
        real = str(Path(path).resolve(strict=True))
    except OSError:
        return False
    return real != path and any(fnmatch.fnmatch(real, g) for g in globs)


# Lazy-load ranking (Mesh skill lazy-load): the session-start skills block only
# affords a bounded slice of the context budget, so on-demand skills are ranked
# by usage and summarized top-down; the tail is discoverable via `skill_search`.
# Per-skill description is truncated to this many chars in the summary line so a
# few verbose descriptions can't dominate the block. Sized as a guardrail against
# a pathological description rather than a routine trim: the description is the
# only signal the model has for deciding whether to load a skill, so the cap sits
# above the typical length (~290 chars across the built-in set) and bites only the
# outliers. Descriptions also arrive from the public registry, where their length
# is not ours to control — hence a cap rather than hand-trimming.
_SHORT_DESC_CHARS = 300
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
# path per message.
#
# This was 5.0s, which did not achieve that: a walk of a real skills tree (645
# files across 21 roots on a dev desktop, incl. AIM-installed package roots)
# takes ~0.7s, and chat messages arrive MINUTES apart — so every message missed
# the cache and paid the full walk, and the 5s only ever deduped the several
# _iter() calls WITHIN one message. At 60s the walk is amortized ~12x with a
# worst-case staleness of one minute.
#
# Staleness only affects skills added OUT OF BAND (AIM sync, a manual cp):
# the app's own create/update/delete/refresh all call _invalidate_iter_cache(),
# so a skill written through the app is visible immediately regardless of TTL.
_ITER_CACHE_TTL_SECS = 60.0

# ── Auto skill creation ──

# Namespace for auto-generated skills — keeps them out of the way of
# hand-authored skills.  Final path: ``~/.kiro/crew/skills/auto/<name>/SKILL.md``.
AUTO_SKILL_NAMESPACE = "auto"

# Archive area for retired auto-skills. A dot-prefixed dir so it is pruned from
# skill discovery (``_iter_skill_files``) — archived skills never trigger, but
# stay on disk and are restorable. Layout: ``auto/.archive/<slug>/SKILL.md``.
AUTO_ARCHIVE_DIRNAME = ".archive"

# Staging area for unapproved skill candidates. Dot-prefixed so it is pruned
# from discovery — pending candidates never trigger. Layout:
# ``auto/.pending/<slug>/{SKILL.md, scripts/, .meta.json}``.
AUTO_PENDING_DIRNAME = ".pending"

# Per-skill version history. A dot-prefixed dir *inside* a live auto-skill
# (``auto/<slug>/.versions/v<N>-SKILL.md``) so it is pruned from skill discovery
# (``_iter_skill_files`` skips dot-dirs) — historical snapshots never trigger and
# never surface in list_skills / list_auto_skills. Written by
# ``approve_pending_update`` before each live overwrite.
VERSIONS_DIRNAME = ".versions"

# Cap on retained per-skill version snapshots; oldest are pruned past this.
MAX_SKILL_VERSIONS = 20

# ── Pending-staged observer hook ──────────────────────────────────────────────
# A candidate can be staged by ANY ``SkillsLoader`` instance (consolidation uses
# the ContextBuilder's loader; dashboard requests build their own), so the
# observer is registered at MODULE level rather than per instance — otherwise a
# gateway-wired instance callback would silently miss the consolidation path that
# produces most candidates. The gateway registers a hook that raises a bell-feed
# notification + broadcasts ``skills.pending_changed``; CLI processes register
# nothing and simply stage silently.
_PENDING_STAGED_HOOK: "Callable[[dict], None] | None" = None


def set_pending_staged_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear, with ``None``) the pending-candidate observer.

    Called once at gateway boot. Idempotent — a later call replaces the hook, so
    a re-created dashboard state does not stack duplicate notifications.
    """
    global _PENDING_STAGED_HOOK
    _PENDING_STAGED_HOOK = fn


def _emit_pending_staged(payload: dict) -> None:
    """Invoke the pending-staged hook, swallowing every failure.

    Staging has already succeeded on disk by the time this runs; a broken or
    slow observer must never turn a successful stage into a failure.
    """
    fn = _PENDING_STAGED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("pending-staged hook failed", exc_info=True)


# Derived lifecycle states for auto-skills (not persisted — computed from
# usage recency at lifecycle-run time).
SKILL_STATE_ACTIVE = "active"
SKILL_STATE_STALE = "stale"
SKILL_STATE_ARCHIVED = "archived"

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
    pinned: bool = False  # user-pinned: exempt from lifecycle eviction

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
        if self.pinned:
            lines.append("pinned: true")
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


def _trusted_skill_roots() -> tuple[str, ...]:
    """Resolved roots a symlink inside the skills tree may legitimately point into.

    An app ships its skills inside its OWN tree, and
    ``apps.bridges._register_skills`` symlinks them into the skills dir "so the
    skill scanner finds the skill" — so their resolved paths land OUTSIDE the
    skills base by construction. Two roots are legitimate skill providers:

    * the installed ``kiro_crew`` package — built-in apps keep their skills
      under ``apps/builtins/<app>/skills/``;
    * ``<data home>/apps`` — externally installed apps.

    A symlink resolving anywhere else stays rejected: an arbitrary target would
    admit unvetted ``SKILL.md`` prose into the agent's context.
    """
    roots: list[str] = [os.path.realpath(Path(__file__).parent)]
    try:
        roots.append(os.path.realpath(config_dir() / "apps"))
    except Exception:  # noqa: BLE001 — an unresolvable data home must not stop scanning
        pass
    return tuple(roots)


def _within_any(candidate: str, roots: tuple[str, ...]) -> bool:
    """True when the already-resolved *candidate* equals one of *roots* or sits under it."""
    cand = Path(candidate)
    for root in roots:
        try:
            if cand == Path(root) or cand.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


#: Basename every skill's body lives under. Used as a cheap pre-filter before
#: any filesystem work when deciding whether a tool call touched a skill.
_SKILL_FILE = "SKILL.md"

#: Argument names under which file-reading tools carry their target. Covers the
#: builtin read tool's ``path`` plus the spellings other tools use; a name that
#: is absent simply yields no candidate.
_TOOL_READ_PATH_KEYS = ("path", "file_path", "filePath", "paths", "files")

#: A whitespace/quote-delimited token ending in the skill basename — how a skill
#: read appears inside a shell command (``cat /x/SKILL.md``). Anchored on the
#: basename so it cannot match an arbitrary argument.
_SHELL_SKILL_PATH_RE = re.compile(r"""[^\s"'|;&><]+SKILL\.md""")


def _tool_read_path_candidates(
    tool_name: str, raw_params: dict | None, command: str | None
) -> list[str]:
    """File targets of a tool call that DELIVERS file content to the model.

    Returns nothing for a call that merely names a path — a delete, move, line
    count, or grep. The ledger's hits mean "a body reached the model", so
    crediting a mention would re-create the mention-as-use conflation that the
    separate searches tally exists to avoid.

    Never raises on a malformed params dict — a tool's arguments are
    model-authored and may hold anything.
    """
    out: list[str] = []
    if isinstance(raw_params, dict) and tool_name in _CONTENT_READ_TOOLS:
        for key in _TOOL_READ_PATH_KEYS:
            value = raw_params.get(key)
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, (list, tuple)):
                out.extend(v for v in value if isinstance(v, str))
    if isinstance(command, str) and command:
        for segment in _shell_segments_reading_content(command):
            out.extend(_SHELL_SKILL_PATH_RE.findall(segment))
    return out


#: Shell commands that deliver a file's CONTENT to the model. Deliberately
#: narrow: the ledger counts bodies that reached the model, so a command that
#: merely names a path — ``rm``, ``mv``, ``wc``, ``chmod`` — earns nothing, and
#: neither does ``grep``, which emits matching lines rather than the body.
#: ``head``/``tail`` deliver a prefix, which is still a body the model read.
_SHELL_READ_VERBS = frozenset(
    {"cat", "bat", "head", "tail", "less", "more", "view", "type"}
)

#: Tools whose result hands the model a file's content. ``grep``/``glob`` are
#: read-KIND but return matches and names, not bodies, so they are excluded for
#: the same reason ``grep`` is above.
_CONTENT_READ_TOOLS = frozenset({"fs_read", "read", "read_file", "readFile"})

#: Splits a shell command into independently-invoked segments, so the verb that
#: applies to a given path is the one that precedes it in ITS segment — without
#: this, ``cat a.txt && rm x/SKILL.md`` would read as a ``cat`` of the skill.
_SHELL_SEGMENT_RE = re.compile(r"(?:\|\||&&|[;|&\n]|\$\(|`)")


def _shell_segments_reading_content(command: str) -> list[str]:
    """Segments of *command* whose leading verb delivers file content.

    A segment's verb is its first bare token; leading environment assignments
    (``FOO=bar cat x``) and absolute paths (``/bin/cat``) are tolerated.
    """
    reading: list[str] = []
    for segment in _SHELL_SEGMENT_RE.split(command):
        for token in segment.split():
            if "=" in token and not token.startswith("-"):
                continue  # leading VAR=value assignment
            verb = token.rsplit("/", 1)[-1]
            if verb in _SHELL_READ_VERBS:
                reading.append(segment)
            break  # only the segment's first bare token is its verb
    return reading


def _mentions_skill_basename(raw_params: dict | None, command: str | None) -> bool:
    """Whether a tool call's arguments name a skill body at all.

    Independent of read intent: used only to tell "this call had nothing to do
    with skills" apart from "this call named a skill but our read-intent
    allowlists did not recognise it", which is what a provider tool rename looks
    like from here.
    """
    if isinstance(command, str) and _SKILL_FILE in command:
        return True
    if not isinstance(raw_params, dict):
        return False
    for value in raw_params.values():
        if isinstance(value, str):
            if _SKILL_FILE in value:
                return True
        elif isinstance(value, (list, tuple)):
            if any(isinstance(v, str) and _SKILL_FILE in v for v in value):
                return True
    return False


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
    # A skills-tree symlink into an app's own tree resolves outside ``base`` by
    # construction — allow those provider roots, and nothing else.
    allowed_roots = (real_base,) + _trusted_skill_roots()
    seen_real: set[str] = set()
    for dirpath, _dirs, files in os.walk(base, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_real:
            _dirs.clear()  # prune this branch — symlink loop
            continue
        seen_real.add(real)
        # Prune dot-directories (e.g. ``auto/.archive``, ``.pending``) so
        # archived / pending / hub-state skills are never enumerated as live,
        # trigger-matchable skills. Mutating ``_dirs`` in place prunes the walk.
        # SORTED so enumeration is deterministic: ``bridges._register_skills``
        # registers each app skill twice (``skills/<app>/<skill>`` and a flat
        # ``skills/<skill>``), both resolving to one target, so the ``seen_real``
        # guard keeps exactly one — and without a sort ``os.walk`` picks the
        # winner in arbitrary ``scandir`` order, giving the same skill a
        # different key on different machines.
        _dirs[:] = sorted(d for d in _dirs if not d.startswith("."))
        # Path containment: stay inside the skills base, or inside a trusted
        # skill-provider root reached through an app's registered symlink.
        if not _within_any(real, allowed_roots):
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


# Skills RELOCATED into the kirocrew-dev/ folder (the Kiro Crew development
# suite). Without this, an upgraded install keeps BOTH the old flat copy
# and the new nested copy — two divergent copies of the same skill matched
# nondeterministically by trigger overlap. The flat copy is NOT deleted (it
# may carry user edits
# the mtime-preserving sync deliberately protects): its SKILL.md is renamed
# to SKILL.md.pre-relocation, which removes it from loader discovery while
# preserving every byte on disk for the user to reconcile. Only done when
# the nested replacement is verifiably present, so a failed/partial sync
# never disables the only copy.
#
# Module level so the packaging guard in test/test_builtin_skill_packaging.py
# can assert every destination actually ships: a destination the package never
# installs makes this migration a permanent no-op and leaves the flat copy as
# the only one the loader finds.
_RELOCATED_SKILLS: dict[str, str] = {
    "prepare-pr": "kirocrew-dev/prepare-pr",
    "babysit": "kirocrew-dev/babysit",
    "kirocrew-worktree-dev": "kirocrew-dev/kirocrew-worktree-dev",
}


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
        for old_name, new_name in _RELOCATED_SKILLS.items():
            old_skill_md = base / old_name / "SKILL.md"
            if old_skill_md.is_file() and (base / new_name / "SKILL.md").exists():
                try:
                    # Never overwrite an earlier quarantine (a rollback or
                    # reinstall can recreate SKILL.md after a prior migration;
                    # os.replace would silently destroy the preserved copy).
                    # Pick the first unused numbered name instead.
                    quarantine = old_skill_md.with_name("SKILL.md.pre-relocation")
                    counter = 2
                    while quarantine.exists():
                        quarantine = old_skill_md.with_name(
                            f"SKILL.md.pre-relocation.{counter}"
                        )
                        counter += 1
                    os.replace(old_skill_md, quarantine)
                    logger.info(
                        "Skill %s relocated to %s; flat copy quarantined at %s "
                        "(preserved on disk, no longer loaded)",
                        old_name, new_name, quarantine,
                    )
                except OSError:
                    logger.warning(
                        "could not quarantine relocated skill's flat copy %s",
                        old_skill_md, exc_info=True,
                    )


def skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


class SkillsLoader:
    """Load skill markdown files from ~/.kiro/crew/skills/.

    Supports nested directories. Each skill is identified by its
    relative path from the skills root (e.g. ``utils/tiny-url``).

    Directory layout::

        ~/.kiro/crew/skills/
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

        # Edition-contributed skill paths (CPP seam). A companion returns extra
        # SKILL.md source roots via McpToolingProvider.extra_skills(); the public
        # Default returns [] so this is a no-op for the standalone edition.
        # Lowest precedence (appended last, after local + configured extra_paths),
        # sensitivity- and
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

    def _cached_frontmatter(self, path: Path, mtime: float | None = None) -> dict[str, str]:
        """Parse frontmatter with mtime-based caching.

        *mtime* lets a caller that already stat()'d the file reuse that result.
        ``list_skills()`` needs the size from the same stat, and this path runs
        on the event loop during context assembly — one syscall per skill, not
        two.
        """
        key = str(path)
        if mtime is None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return {}
        cached = self._fm_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        # Failures PROPAGATE deliberately. Not every caller is a reader:
        # ``update_auto_skill`` reads this to carry ``created_at``, ``version``,
        # ``pinned`` and ``inject_on_trigger`` across a rewrite, so degrading an
        # unreadable file to "no metadata" here would make it silently drop those
        # and clobber a version snapshot. A reader that would rather show a row
        # than fail catches this at ITS call site instead.
        meta = self._parse_frontmatter(path)
        self._fm_cache[key] = (mtime, meta)
        return meta

    def list_skills(self) -> list[dict]:
        """Return per-skill metadata for the dashboard's Skills page.

        Carries the three fields the injection-cost control needs alongside the
        identity ones: whether the skill opted out of full-body injection, how
        big its body is, and how many times that body was actually DELIVERED into
        a prompt. Cost is the product of the last two, and a user deciding
        whether to opt a skill out cannot weigh it without both.

        ``deliveries`` counts body deliveries, not trigger matches: the ledger
        records only when a body reaches the prompt, so a false-positive match, a
        pointer-only skill, and an undelivered match all count zero. Two
        consequences a caller must not paper over — a skill already opted out
        stops accruing entirely, so its figure is historical and frozen; and this
        is therefore a measure of what was SPENT, never of how often the skill
        was relevant.

        ``deliveries`` is ``None`` when the skill has no ledger entry, which is
        different from zero: an entry can also age out of the 30-day window.

        ``owned`` says whether Kiro Crew may rewrite the file. A skill reached
        through ``skills.extra_paths`` is listed but not ours to edit, so the UI
        must not offer a toggle the endpoint will refuse.

        This runs on the event loop as part of context assembly (the skill
        index), so it takes exactly ONE stat per skill — the same count as
        before ``size_bytes`` existed — by feeding that stat's mtime to the
        frontmatter cache instead of letting it stat again.
        """
        skills: list[dict] = []
        for name, skill_file in self._iter():
            try:
                st: os.stat_result | None = skill_file.stat()
            except OSError:
                st = None
            meta = self._cached_frontmatter(
                skill_file, mtime=st.st_mtime if st is not None else None
            )
            skills.append(
                {
                    "key": name,
                    "name": meta.get("name", name),
                    "description": meta.get("description", name),
                    "path": str(skill_file),
                    "dir": str(skill_file.parent),
                    "always": meta.get("always", "").lower() == "true",
                    # Mirrors split_triggered: only an explicit `false` opts out,
                    # so a malformed value reads as injecting, as it behaves.
                    "inject_on_trigger": (
                        meta.get("inject_on_trigger", "").strip().lower() != "false"
                    ),
                    "size_bytes": st.st_size if st is not None else 0,
                    "deliveries": self._delivery_count(name),
                    "owned": self._owned_hint(skill_file),
                }
            )
        return skills

    def _owned_hint(self, skill_file: Path) -> bool:
        """Whether *skill_file* sits under the directory Kiro Crew owns.

        Syscall-free on purpose: this runs once per skill inside ``list_skills``,
        which the event loop calls while assembling the skill index, and
        ``Path.resolve()`` costs a stat each. It is an ADVISORY hint for the UI —
        the authoritative check is the resolved one in
        ``set_inject_on_trigger``, which is the write boundary and runs once per
        toggle. A path that only differs by a symlink therefore reads as owned
        here and is still refused there; the failure mode is a toggle that
        reports an error, never an unowned file being rewritten.
        """
        try:
            return skill_file.is_relative_to(self._dir)
        except (OSError, ValueError):
            return False

    def _served_key_by_realpath(self) -> dict[str, str]:
        """Map each served skill file's realpath to its canonical served key.

        Applies the same canonical rule as ``resolve_ledger_aliases`` — the real
        file's key beats a symlink's, then alphabetical — so a read through a
        symlinked skill is credited to the key the budget screen displays rather
        than splitting one file's cost across two rows. Uncached and
        resolve()-bound for the same reason stated there, so callers must gate
        it behind a cheap check rather than running it per tool call.
        """
        by_realpath: dict[str, list[tuple[str, Path]]] = {}
        for key, skill_file in self._iter():
            try:
                rp = str(skill_file.resolve())
            except (OSError, RuntimeError):
                # A cyclic symlink raises RuntimeError, not OSError.
                continue
            by_realpath.setdefault(rp, []).append((key, skill_file))
        return {
            rp: min(pairs, key=lambda p: (p[1].is_symlink(), p[0]))[0]
            for rp, pairs in by_realpath.items()
        }

    def resolve_tool_read_keys(
        self,
        tool_name: str = "",
        raw_params: dict | None = None,
        command: str | None = None,
    ) -> list[str]:
        """Served skill keys whose body a tool call is about to deliver.

        Resolution only — nothing is recorded, so the caller can run this off
        the event loop and credit later, once the read is confirmed to have
        completed. Returns keys deduped, so one command naming a file twice
        yields it once.

        Only content-delivering reads qualify (see
        ``_tool_read_path_candidates``): a tool call that merely names a skill
        path earns nothing, because the ledger's hits mean a body reached the
        model.

        Filesystem-bound (``_iter`` plus a ``resolve()`` per served skill), so
        candidates are filtered on the ``SKILL.md`` basename first and callers
        must keep this off the event loop.
        """
        if self._usage is None:
            return []
        candidates = [
            c
            for c in _tool_read_path_candidates(tool_name, raw_params, command)
            if _SKILL_FILE in c
        ]
        if not candidates:
            # The read-intent allowlists (`_CONTENT_READ_TOOLS`,
            # `_SHELL_READ_VERBS`) encode the provider's current tool spellings.
            # A rename would silently restore the pre-existing undercount with
            # nothing failing, so a call that clearly names a skill yet yields no
            # candidate is logged — the one signal that distinguishes drift from
            # a legitimately non-reading tool call.
            if _mentions_skill_basename(raw_params, command):
                logger.debug(
                    "skill-read: %r names a skill but is not a content read "
                    "(tool=%r); check the read-intent allowlists if the provider "
                    "renamed its tools",
                    command or raw_params,
                    tool_name,
                )
            return []
        try:
            realpath_to_key = self._served_key_by_realpath()
        except OSError:
            return []
        keys: list[str] = []
        for cand in candidates:
            try:
                rp = str(Path(cand).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
            key = realpath_to_key.get(rp)
            if key is not None and key not in keys:
                keys.append(key)
        return keys

    def credit_skill_reads(self, keys: list[str]) -> None:
        """Record a delivery for each key in *keys*. Best-effort, never raises.

        Separate from ``resolve_tool_read_keys`` so the credit lands only after
        the read has actually completed — a tool call that was denied or failed
        must not leave a delivery behind.
        """
        for key in keys:
            self._record_use(key)

    def resolve_ledger_aliases(self) -> dict[str, list[str]]:
        """Map served skill keys to ledger keys that resolve to the same file.

        Returns ``{served_key: [alias_key, ...]}`` — only entries with at least
        one alias appear. Unresolvable ledger keys (no SKILL.md on disk) are
        dropped silently.

        The result is NOT cached. It depends on what each served path currently
        resolves to, so any sound cache key would have to resolve every served
        file — the same work the cache would save. `_iter()` has its own TTL, so
        repeat calls (e.g. dashboard refreshes) do not re-walk the skills tree.

        This is the public seam for *alias resolution* specifically — the budget
        endpoint no longer builds the map itself. It still reads other loader
        internals to assemble its rows, so this is one step out of that coupling,
        not the end of it. It deliberately does NOT live inside ``list_skills()``
        — that method guarantees one stat per skill and runs on the hot path
        during context assembly; filesystem resolution here is acceptable only
        at dashboard-refresh frequency.
        """
        if self._usage is None:
            return {}

        snapshot = self._usage.snapshot()
        if not snapshot:
            return {}

        # NOT cached, deliberately. The map is a function of the ledger's keys
        # AND of what each served path currently RESOLVES to, so a sound cache key
        # has to resolve every served file — exactly the work a cache would be
        # there to avoid. Keying on names alone was demonstrably unsound: deleting
        # an alias, or retargeting a served symlink, changes no name, so a hit
        # kept crediting deliveries to the wrong skill. A cache that is only
        # correct when nothing moved is worse than no cache, and `_iter()` already
        # carries its own TTL, so repeat calls do not re-walk the tree.
        skill_pairs = self._iter()

        # Group served keys by resolved path. Two served keys CAN name the same
        # file: a file-level symlink (`old/SKILL.md` -> `new/SKILL.md`) leaves
        # both directories real, so `_iter()` yields both. Treating each as its
        # own skill splits one file's cost across two rows, which is the very
        # thing this fold exists to prevent — so one key per file is canonical
        # and the rest are aliases.
        by_realpath: dict[str, list[tuple[str, Path]]] = {}
        for key, skill_file in skill_pairs:
            try:
                rp = str(skill_file.resolve())
            except (OSError, RuntimeError):
                # A cyclic symlink raises RuntimeError("Symlink loop from ..."),
                # NOT OSError, so it must be caught explicitly or one bad link
                # takes the whole endpoint down with a 500.
                continue
            by_realpath.setdefault(rp, []).append((key, skill_file))

        realpath_to_served: dict[str, str] = {}
        alias_map: dict[str, list[str]] = {}
        for rp, pairs in by_realpath.items():
            # The real file's key beats a symlink's, then alphabetical — so the
            # winner does not depend on directory iteration order.
            canonical, _ = min(pairs, key=lambda p: (p[1].is_symlink(), p[0]))
            realpath_to_served[rp] = canonical
            for key, _ in pairs:
                if key != canonical:
                    alias_map.setdefault(canonical, []).append(key)

        # Roots to resolve a ledger key against. `_iter()` serves the main skills
        # dir AND every extra path (an installed app's own skills dir), and each
        # names its skills relative to its OWN root — so an app skill's alias key
        # only resolves under that app's root. Resolving against `_dir` alone
        # silently drops every app-skill alias.
        roots = [self._dir, *self._extra_paths]

        # A ledger key that no longer names a served skill: resolve it on disk and
        # fold it into whichever served key shares its file.
        for ledger_key in snapshot:
            if ledger_key in realpath_to_served.values():
                continue  # Already the canonical key for its file.
            if any(ledger_key in a for a in alias_map.values()):
                continue  # Already folded as a served alias above.
            for root in roots:
                candidate = root / ledger_key / "SKILL.md"
                try:
                    rp = str(candidate.resolve())
                except (OSError, RuntimeError):
                    continue  # Unresolvable or a symlink loop — try the next root.
                if not Path(rp).exists():
                    continue
                served_key = realpath_to_served.get(rp)
                if served_key is None:
                    continue
                if ledger_key != served_key:
                    alias_map.setdefault(served_key, []).append(ledger_key)
                break  # First root that resolves wins; a key names one file.

        for aliases in alias_map.values():
            aliases.sort()

        return alias_map

    def _delivery_count(self, key: str) -> int | None:
        """Body deliveries recorded for *key*, or ``None`` when untracked.

        Best-effort: the ledger is telemetry, so a missing or unreadable one
        yields ``None`` rather than failing the whole listing.
        """
        if self._usage is None:
            return None
        try:
            hits, _ = self._usage.score(key)
        except Exception:
            return None
        return int(hits) if hits else None

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
        # Re-emit the lifecycle lines ``_build_auto_skill_content`` does not know
        # about. Dropping ``version`` would make the next update-approval read the
        # skill as v1 and overwrite an existing ``.versions/v1-SKILL.md`` snapshot;
        # dropping ``pinned`` would silently remove the skill's archival exemption;
        # dropping ``inject_on_trigger`` would turn full-body injection back on for
        # a skill the user had made pointer-only — a setting undoing itself behind
        # an unrelated refine.
        _carry: list[str] = []
        _ver = existing_meta.get("version", "")
        try:
            _vn = int(_ver)
        except (TypeError, ValueError):
            _vn = 0
        if _vn > 1:
            _carry.append(f"version: {_vn}")
        if str(existing_meta.get("pinned", "")).strip().lower() in ("true", "1", "yes"):
            _carry.append("pinned: true")
        if str(existing_meta.get("inject_on_trigger", "")).strip().lower() == "false":
            _carry.append("inject_on_trigger: false")
        if _carry:
            content = content.replace("\n---\n", "\n" + "\n".join(_carry) + "\n---\n", 1)
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

    @staticmethod
    def _repo_scope_satisfied(relpath: str) -> bool:
        """Mechanical gate for repo-scoped skills (``repo_scope:`` frontmatter).

        A skill carrying ``repo_scope: <relpath>`` is only eligible for
        injection when the current working directory (or an ancestor) contains
        *relpath* — e.g. ``repo_scope: src/kiro_crew`` restricts a skill to
        sessions actually working inside the KiroCrew source tree. This is the
        loader-enforced counterpart to a prose "ignore this skill elsewhere"
        scope guard: prose depends on probabilistic LLM obedience, while this
        check runs before the skill ever reaches the context (destructive
        repo-dev instructions must be mechanically contained).

        Fails CLOSED on any error — a repo-scoped skill is suppressed unless
        its scope is positively confirmed.
        """
        rel = relpath.strip().strip("/")
        if not rel or ".." in rel.split("/"):
            return False
        try:
            cwd = Path.cwd().resolve()
        except OSError:
            return False
        for candidate in (cwd, *cwd.parents):
            try:
                if (candidate / rel).exists():
                    return True
            except OSError:
                continue
        return False
    # ── Auto skill lifecycle: pin / archive / restore / eviction ──

    @staticmethod
    def _cron_referenced_skills() -> set[str]:
        """Skill keys referenced by any cron job (best-effort, never raises).

        A skill a cron job depends on must never be archived out from under it.
        Any import/read failure yields an empty set (no protection, no crash).
        """
        try:  # pragma: no cover - cron reference API is environment-dependent
            return set(referenced_skill_names())
        except Exception:
            return set()

    def _auto_created_ts(self, meta: dict) -> float:
        """Parse ``created_at`` frontmatter to a unix timestamp, else 0.0."""
        raw = meta.get("created_at", "")
        if not raw:
            return 0.0
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            return 0.0

    def _auto_activity(self, key: str, path_str: str, meta: dict) -> tuple[int, float]:
        """Return ``(hits, anchor_ts)`` for an auto-skill.

        ``anchor_ts`` is the most recent evidence of relevance: last recorded
        use, else the created_at frontmatter, else the file mtime — so a
        never-used-but-freshly-created skill is not treated as ancient.
        """
        hits = 0
        last_seen = 0.0
        if self._usage is not None:
            try:
                hits_f, last_seen = self._usage.score(key)
                hits = int(hits_f)
            except Exception:
                hits, last_seen = 0, 0.0
        anchor = last_seen or self._auto_created_ts(meta)
        if not anchor:
            try:
                anchor = Path(path_str).stat().st_mtime
            except OSError:
                anchor = 0.0
        return hits, anchor

    def set_pinned(self, name: str, pinned: bool) -> bool:
        """Pin/unpin an auto-skill (exempt from lifecycle eviction).

        Edits the ``pinned:`` frontmatter line in place. Returns True on
        success. Only auto-generated skills may be pinned.
        """
        if not self.is_auto_generated(name):
            return False
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            return False
        content = skill_file.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, re.DOTALL)
        if not m:
            return False
        fm_lines = [ln for ln in m.group(1).split("\n") if not ln.strip().startswith("pinned:")]
        if pinned:
            fm_lines.append("pinned: true")
        new_content = "---\n" + "\n".join(fm_lines) + "\n---\n" + m.group(2)
        # Atomic write (temp + rename): a partial write must never truncate the
        # live SKILL.md and lose the skill's content on a full-disk failure.
        atomic_write(skill_file, new_content)
        self._invalidate_iter_cache()
        logger.info("%s auto skill: %s", "Pinned" if pinned else "Unpinned", name)
        return True

    def set_inject_on_trigger(self, name: str, inject: bool) -> bool:
        """Opt a skill in or out of full-body injection on a trigger match.

        Edits the ``inject_on_trigger:`` frontmatter line in place, mirroring
        :meth:`set_pinned`. ``inject=False`` writes the opt-out; ``inject=True``
        removes the line rather than writing ``true``, because injecting is the
        default and an absent key is the honest way to say "unchanged".

        Refuses any skill whose file resolves outside this loader's own skills
        dir. ``_resolve_path`` also reaches ``skills.extra_paths`` and the
        kiro-cli user/workspace skill dirs — directories Kiro Crew does not own
        and may not even be able to write. Rewriting a foreign ``SKILL.md``
        because a dashboard toggle was flipped is a side effect nobody asked
        for, so ownership is checked before the write, not left to the UI (which
        does gate on source, but the endpoint is reachable directly).

        Returns False when the skill cannot be resolved, is not ours, or has no
        frontmatter block to edit — the caller surfaces that as a failed toggle
        rather than silently reporting success on a no-op.
        """
        if not self._safe_name(name):
            return False
        skill_file = self._resolve_path(name)
        if skill_file is None or not skill_file.exists():
            return False
        try:
            owned_root = self._dir.resolve()
            if not skill_file.resolve().is_relative_to(owned_root):
                logger.warning("Refusing to edit a skill outside %s: %s", owned_root, skill_file)
                return False
        except OSError:
            return False
        try:
            content = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, re.DOTALL)
        if not m:
            return False
        fm_lines = [
            ln
            for ln in m.group(1).split("\n")
            # Only a TOP-LEVEL key, matched without stripping: an indented
            # `inject_on_trigger:` belongs to a block scalar (a description that
            # documents the flag, say), and deleting that line would silently
            # rewrite the skill's prose while toggling a setting.
            if not ln.lower().startswith("inject_on_trigger:")
        ]
        if not inject:
            fm_lines.append("inject_on_trigger: false")
        new_content = "---\n" + "\n".join(fm_lines) + "\n---\n" + m.group(2)
        # Atomic write (temp + rename), for the same reason set_pinned uses it:
        # a partial write must never truncate the live SKILL.md.
        atomic_write(skill_file, new_content)
        self._invalidate_iter_cache()
        logger.info(
            "Skill %s on trigger: %s", "injects fully" if inject else "sends a pointer", name
        )
        return True

    def _archive_root(self) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / AUTO_ARCHIVE_DIRNAME

    @staticmethod
    def _is_pending_slug_safe(slug: str) -> bool:
        """Strict guard for a single-segment auto-skill slug.

        Rejects empty, ``.``/``..``, leading-dot, and any separator/traversal —
        so e.g. ``dismiss_pending_skill(".")`` can't collapse to the pending
        root and wipe the whole queue.
        """
        return (
            bool(slug)
            and slug not in (".", "..")
            and not slug.startswith(".")
            and "/" not in slug
            and "\\" not in slug
            and ".." not in slug
        )

    def archive_auto_skill(self, name: str) -> bool:
        """Move an auto-skill into the archive (recoverable, never deleted).

        Refuses non-auto skills. Returns True on success.
        """
        if not self.is_auto_generated(name):
            return False
        slug = name.split("/", 1)[1]
        src = self._dir / name
        if not src.is_dir():
            return False
        dest = self._archive_root() / slug
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # Never destroy a recoverable archive: a same-slug skill was
            # archived before. Version the destination so the prior copy
            # survives (archive-not-delete contract).
            i = 2
            while (self._archive_root() / f"{slug}-{i}").exists():
                i += 1
            dest = self._archive_root() / f"{slug}-{i}"
        shutil.move(str(src), str(dest))
        self._invalidate_iter_cache()
        logger.info("Archived auto skill: %s", name)
        return True

    def restore_auto_skill(self, slug: str) -> str | None:
        """Restore an archived auto-skill back to ``auto/<slug>``.

        Returns the restored skill name, or None if not found / name clash.
        """
        if not self._is_pending_slug_safe(slug):
            return None
        src = self._archive_root() / slug
        if not src.is_dir():
            return None
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        dest = self._dir / name
        if dest.exists():
            logger.warning("Cannot restore %s: a live skill already exists", name)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        self._invalidate_iter_cache()
        logger.info("Restored auto skill: %s", name)
        return name

    def list_archived_auto_skills(self) -> list[dict]:
        """Return ``{slug, path}`` for every archived auto-skill."""
        root = self._archive_root()
        out: list[dict] = []
        if not root.is_dir():
            return out
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                out.append({"slug": child.name, "path": str(child / "SKILL.md")})
        return out

    def run_skill_lifecycle(
        self,
        *,
        max_auto_skills: int,
        stale_after_days: int,
        archive_after_days: int,
        cron_referenced: set[str] | None = None,
        exempt: set[str] | None = None,
        now: float | None = None,
    ) -> dict:
        """Age + bound the auto-skill set. Archives (never deletes).

        Two passes:
        1. **Inactivity**: archive any auto-skill whose anchor is older than
           ``archive_after_days``. Pinned and cron-referenced skills are exempt.
           Never-used (hits==0) skills younger than ``stale_after_days`` are
           exempt (grace floor).
        2. **Max-N backstop**: if more than ``max_auto_skills`` remain live,
           archive the lowest-ranked (by hits, then recency) down to the cap,
           again skipping pinned / cron-referenced skills.

        Returns a counts dict: ``{checked, marked_stale, archived, capped}``.
        """
        if now is None:
            now = time.time()
        if cron_referenced is None:
            cron_referenced = self._cron_referenced_skills()
        extra_exempt = exempt or set()
        stale_cutoff = now - stale_after_days * 86400
        archive_cutoff = now - archive_after_days * 86400
        counts = {"checked": 0, "marked_stale": 0, "archived": 0, "capped": 0}

        # Snapshot live auto-skills with their activity + exemption status.
        rows: list[dict] = []
        for s in self.list_auto_skills():
            key = s["key"]
            meta = self._cached_frontmatter(Path(s["path"]))
            hits, anchor = self._auto_activity(key, s["path"], meta)
            pinned = str(meta.get("pinned", "")).lower() == "true"
            slug = key.split("/")[-1]
            exempt_row = (
                pinned
                or key in cron_referenced or slug in cron_referenced
                or key in extra_exempt or slug in extra_exempt
            )
            rows.append({"key": key, "hits": hits, "anchor": anchor, "exempt": exempt_row})
            counts["checked"] += 1

        # Pass 1 — inactivity archival.
        survivors: list[dict] = []
        for r in rows:
            if r["exempt"]:
                survivors.append(r)
                continue
            never_used_grace = r["hits"] == 0 and r["anchor"] > stale_cutoff
            if not never_used_grace and r["anchor"] <= archive_cutoff:
                if self.archive_auto_skill(r["key"]):
                    counts["archived"] += 1
                    continue
            if r["hits"] == 0 and r["anchor"] <= stale_cutoff:
                counts["marked_stale"] += 1
            elif r["anchor"] <= stale_cutoff:
                counts["marked_stale"] += 1
            survivors.append(r)

        # Pass 2 — max-N backstop over what survived pass 1.
        evictable = [r for r in survivors if not r["exempt"]]
        overflow = len(survivors) - max_auto_skills
        if overflow > 0 and evictable:
            evictable.sort(key=lambda r: (r["hits"], r["anchor"]))
            for r in evictable[:overflow]:
                if self.archive_auto_skill(r["key"]):
                    counts["archived"] += 1
                    counts["capped"] += 1
        return counts

    # ── Auto skill staging: pending-approval queue ──

    def _pending_root(self) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / AUTO_PENDING_DIRNAME

    def stage_skill_candidate(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
        scripts: list[dict] | None = None,
        source: str = "consolidation",
        kind: str = "new",
        target: str | None = None,
        base_version: int | None = None,
    ) -> str | None:
        """Write a skill candidate to the pending queue (not live).

        Layout: ``auto/.pending/<slug>/{SKILL.md, scripts/*, .meta.json}``.
        Scripts are written **non-executable** — the executable bit is only set
        on approval. Returns ``auto/<slug>`` on success, else ``None`` (invalid
        slug, oversized procedure). Caller passes already-redacted content.

        ``kind`` distinguishes a brand-new candidate (``"new"``, the default,
        approved via ``approve_pending_skill``) from an UPDATE proposal against
        an existing live auto-skill (``"update"``, approved via
        ``approve_pending_update``). For an update, ``target`` names the live
        auto-skill (``auto/<slug>``) and ``base_version`` records the live
        version the merge was based on. These are written into ``.meta.json``
        (``kind`` always; ``target`` / ``base_version`` only when provided) so
        existing new-candidate callers are unaffected.
        """
        if not _AUTO_NAME_PATTERN.match(slug):
            logger.warning("Rejected pending skill: slug %r failed validation", slug)
            return None
        if len(procedure_md) > AUTO_SKILL_MAX_PROCEDURE_CHARS:
            logger.warning("Rejected pending skill %s: procedure too long", slug)
            return None
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        root = self._pending_root()
        root.mkdir(parents=True, exist_ok=True)
        # Atomically CLAIM a pending dir. mkdir(exist_ok=False) closes the TOCTOU
        # between an exists() check and the create. If the natural slug is already
        # awaiting review we must NOT overwrite it (the queued candidate is
        # immutable until approved/dismissed) — but we also must NOT silently drop
        # THIS candidate: consolidation advances its message offset regardless of
        # per-candidate outcome, so a distinct skill that merely slugifies the
        # same as a pending one would be lost forever. Allocate a unique sibling
        # slug (<slug>-2, -3, …) so it still gets queued. Genuine re-detections of
        # the SAME skill are suppressed upstream by the metadata dedupe before
        # staging, so this does not flood the queue with duplicates.
        pdir = root / slug
        try:
            pdir.mkdir(exist_ok=False)
        except FileExistsError:
            claimed: "Path | None" = None
            for _n in range(2, 51):
                cand_dir = root / f"{slug}-{_n}"
                try:
                    cand_dir.mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                claimed = cand_dir
                break
            if claimed is None:
                logger.warning(
                    "Too many pending candidates for slug %s; deferring re-stage", slug
                )
                return name
            pdir = claimed
            slug = claimed.name
            name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
            logger.info("Slug in use; staging distinct candidate as %s", name)
        try:
            content = _build_auto_skill_content(
                slug=slug,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            (pdir / "SKILL.md").write_text(content, encoding="utf-8")
            script_names: list[str] = []
            clean_scripts = [s for s in (scripts or []) if isinstance(s, dict)]
            if clean_scripts:
                sdir = pdir / "scripts"
                sdir.mkdir(exist_ok=True)
                for s in clean_scripts:
                    fn = str(s.get("filename", "")).strip()
                    # Guard the script filename against traversal / nesting.
                    if not fn or "/" in fn or "\\" in fn or ".." in fn:
                        continue
                    (sdir / fn).write_text(str(s.get("content", "")), encoding="utf-8")
                    script_names.append(fn)
            meta = {
                "slug": slug,
                "name": name,
                "source": source,
                "created_at": provenance.created_at or AutoSkillProvenance.now_iso(),
                "description": description,
                "triggers": triggers,
                "has_scripts": bool(script_names),
                "scripts": script_names,
                "kind": kind or "new",
            }
            if target is not None:
                meta["target"] = target
            if base_version is not None:
                meta["base_version"] = base_version
            (pdir / ".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            # A partial write (e.g. disk full) must not leave a CLAIMED but empty
            # dir behind: a later stage would see it exists and report the slug as
            # "already awaiting review" while no reviewable candidate exists.
            # Roll back the atomic claim so the slug can be re-staged cleanly.
            shutil.rmtree(pdir, ignore_errors=True)
            raise
        logger.info(
            "Staged pending skill candidate: %s (scripts=%d)", name, len(script_names)
        )
        # Notify any registered observer (the gateway wires a bell-feed
        # notification + a ``skills.pending_changed`` WS event) so a candidate
        # awaiting review surfaces instead of sitting unseen in the queue. Fired
        # for BOTH new and update candidates, from every producer that stages
        # through this choke point. Best-effort: an observer failure must never
        # fail the staging that already succeeded on disk.
        #
        # ``description``/``triggers`` ride along because the observer's only
        # other option is to re-read ``.meta.json`` off disk (a second read of
        # what was just written, on the staging path) -- and without them a
        # notification can only say THAT a skill was generated, never what it
        # does, which is the one fact a reviewer needs to decide whether to open
        # the queue at all.
        _emit_pending_staged(
            {
                "name": name,
                "slug": slug,
                "kind": kind or "new",
                "target": target,
                "source": source,
                "has_scripts": bool(script_names),
                "description": description,
                "triggers": triggers,
            }
        )
        return name

    def _read_pending_meta(self, slug: str) -> dict:
        mf = self._pending_root() / slug / ".meta.json"
        # Never follow an LLM-planted symlink (could point at a sensitive file).
        if mf.is_symlink():
            return {}
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Recursively redact secrets from LLM-produced metadata before it can
        # surface via the pending list/detail API. The crystallize skill writes
        # .meta.json directly, bypassing the consolidation redaction path, so a
        # credential in ANY (incl. nested) value must be scrubbed here.
        redacted = self._redact_deep(data)
        return redacted if isinstance(redacted, dict) else {}

    def list_pending_skills(self) -> list[dict]:
        """Return ``{slug, name, description, triggers, has_scripts, created_at, path}``
        for every staged candidate."""
        root = self._pending_root()
        out: list[dict] = []
        if not root.is_dir():
            return out
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / "SKILL.md").exists():
                continue
            # Only surface canonical slugs. A crystallize direct-write could name
            # the pending dir with credential-shaped text; anything that isn't a
            # canonical single-segment slug is skipped so it can't be serialized
            # to the dashboard as a "slug" (and can't be approved/dismissed by
            # the slug-keyed handlers, which apply the same guard).
            if not _AUTO_NAME_PATTERN.match(child.name):
                continue
            meta = self._read_pending_meta(child.name)
            out.append(
                {
                    "slug": child.name,
                    "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{child.name}"),
                    "description": meta.get("description", ""),
                    "triggers": meta.get("triggers", ""),
                    "has_scripts": bool(meta.get("has_scripts")),
                    "created_at": meta.get("created_at", ""),
                    "source": meta.get("source", ""),
                    "kind": meta.get("kind", "new"),
                    "target": meta.get("target"),
                    "base_version": meta.get("base_version"),
                    # NB: no on-disk ``path`` — this dict is API-facing (feeds
                    # /api/skills/-/pending) and must not leak the server's home
                    # / directory layout to dashboard clients.
                }
            )
        return out

    @staticmethod
    def _redact_text(text: object) -> str:
        """Two-pass redaction (exfiltration URLs + credentials) — the same
        passes ``HistoryConsolidator._process_auto_skills`` applies. Run at the
        pending detail/approve choke points so producers that bypass that path
        (notably the ``crystallize`` skill writing straight to the queue) can't
        surface secrets to the dashboard or promote them live."""
        if not isinstance(text, str):
            return ""
        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        return safe

    def _redact_deep(self, obj: object) -> object:
        """Recursively redact every string in a nested dict/list structure so a
        credential hidden in a nested ``.meta.json`` value can't reach the
        dashboard unredacted (top-level-only redaction missed those). String
        dict KEYS are redacted too — a prompt-injected key can carry a secret."""
        if isinstance(obj, str):
            return self._redact_text(obj)
        if isinstance(obj, dict):
            return {
                (self._redact_text(k) if isinstance(k, str) else k): self._redact_deep(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._redact_deep(v) for v in obj]
        return obj

    @staticmethod
    def _candidate_has_symlink(pdir: Path) -> bool:
        """True if the candidate dir itself or any entry under it is a symlink —
        so the read/approve paths never follow an LLM-planted link to a
        sensitive file. (Scripts always require human review before going live;
        this is defense-in-depth, not the primary control.)"""
        if os.path.islink(str(pdir)):
            return True
        for root, dirs, files in os.walk(pdir):
            for nm in list(dirs) + list(files):
                if os.path.islink(os.path.join(root, nm)):
                    return True
        return False

    def _redact_file_in_place(self, fp: Path) -> bool:
        """Redact secrets from a file in place. Returns False if the file could
        not be read or a required rewrite failed — the caller MUST abort
        promotion so an unredacted secret never reaches a live skill."""
        try:
            original = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        safe = self._redact_text(original)
        if safe == original:
            return True
        try:
            fp.write_text(safe, encoding="utf-8")
        except OSError:
            return False
        return True

    @staticmethod
    def _collect_scripts(sdir: Path) -> list[dict]:
        """Recursively collect ``{filename, content}`` for every regular file
        under ``sdir`` (relative filenames). Recursion + symlink-skip ensure a
        nested script (``scripts/nested/evil.py``) can't evade validation or
        review by hiding below the top level."""
        out: list[dict] = []
        if not sdir.is_dir():
            return out
        for root, _dirs, files in os.walk(sdir):
            for nm in sorted(files):
                fp = Path(root) / nm
                if fp.is_file() and not fp.is_symlink():
                    try:
                        out.append(
                            {"filename": str(fp.relative_to(sdir)),
                             "content": fp.read_text(encoding="utf-8")}
                        )
                    except OSError:
                        continue
        return out

    def get_pending_skill(self, slug: str) -> dict | None:
        """Return full pending-candidate detail incl. SKILL.md body + script bodies."""
        if not self._is_pending_slug_safe(slug):
            return None
        pdir = self._pending_root() / slug
        skill_file = pdir / "SKILL.md"
        if not skill_file.exists():
            return None
        # Reject any symlink in the candidate on the read path too (approval
        # already rejects them) so the detail API can't be tricked into reading
        # a sensitive file a candidate symlinked SKILL.md / a nested file to.
        if self._candidate_has_symlink(pdir):
            logger.warning("Refusing to read pending %s: candidate contains a symlink", slug)
            return None
        meta = self._read_pending_meta(slug)
        scripts = self._collect_scripts(pdir / "scripts")
        for s in scripts:
            s["filename"] = self._redact_text(s.get("filename", ""))
            s["content"] = self._redact_text(s.get("content", ""))
        return {
            "slug": slug,
            "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{slug}"),
            "meta": meta,
            "kind": meta.get("kind", "new"),
            "target": meta.get("target"),
            "base_version": meta.get("base_version"),
            "content": self._redact_text(skill_file.read_text(encoding="utf-8")),
            "scripts": scripts,
        }

    def _candidate_layout_ok(self, src: Path, name: str) -> bool:
        """Shared candidate-layout guard for BOTH approve paths.

        Rejects (a) any symlink anywhere in the candidate (defense-in-depth on
        top of the mandatory human review — promotion + chmod must only touch
        real files), and (b) any unexpected top-level entry: only ``SKILL.md``,
        ``.meta.json`` and a ``scripts`` DIRECTORY are allowed. An injected
        auxiliary file (dropped outside the validated set) would ride live
        WITHOUT validation or redaction; a regular file named ``scripts`` would
        skip the directory-only script validation + redaction walk. Returns True
        only when the layout is safe to promote.
        """
        if self._candidate_has_symlink(src):
            logger.warning("Refusing to approve %s: candidate contains a symlink", name)
            return False
        _allowed_top = {"SKILL.md", ".meta.json", "scripts"}
        for entry in src.iterdir():
            if entry.name not in _allowed_top:
                logger.warning(
                    "Refusing to approve %s: unexpected candidate entry %r", name, entry.name
                )
                return False
            if entry.name == "scripts" and not entry.is_dir():
                logger.warning(
                    "Refusing to approve %s: 'scripts' must be a directory, not a file", name
                )
                return False
        return True

    def _validate_and_redact_candidate(self, src: Path, name: str) -> dict[Path, bytes] | None:
        """Re-validate + redact a candidate's SKILL.md and scripts IN PLACE.

        Shared by ``approve_pending_skill`` and ``approve_pending_update`` so
        both enforce the identical discipline: validate every script (covers
        crystallize direct-writes), snapshot each target's ORIGINAL bytes, redact
        in place, then re-validate scripts (redacting a credential-shaped token
        can break syntax). On ANY failure the originals are restored and ``None``
        is returned so a rejected candidate is never left corrupted. On success
        returns the ``{path: original_bytes}`` snapshot so the caller can restore
        on a LATER failure (e.g. a failed move / snapshot).
        """
        sdir_src = src / "scripts"
        # Pre-redaction script validation.
        if sdir_src.is_dir():
            ok, report = validate_scripts(self._collect_scripts(sdir_src))
            if not ok:
                logger.warning(
                    "Refusing to approve %s: script validation failed: %s", name, report
                )
                return None
        # Snapshot each target FIRST so an abort after partial in-place redaction
        # restores the candidate's ORIGINAL bytes.
        redact_targets = [src / "SKILL.md"]
        if sdir_src.is_dir():
            for root, _dirs, files in os.walk(sdir_src):
                for nm in files:
                    fp = Path(root) / nm
                    if fp.is_file() and not fp.is_symlink():
                        redact_targets.append(fp)
        redact_backup: dict[Path, bytes] = {}
        for fp in redact_targets:
            try:
                redact_backup[fp] = fp.read_bytes()
            except OSError:
                pass

        def _restore_redacted() -> None:
            for _fp, _b in redact_backup.items():
                try:
                    _fp.write_bytes(_b)
                except OSError:
                    pass

        for fp in redact_targets:
            if not self._redact_file_in_place(fp):
                _restore_redacted()
                logger.warning(
                    "Refusing to approve %s: could not redact %s before promotion", name, fp.name
                )
                return None
        # Re-validate scripts AFTER redaction so a broken/altered helper never
        # goes live and the pending draft is not corrupted.
        if sdir_src.is_dir():
            ok, report = validate_scripts(self._collect_scripts(sdir_src))
            if not ok:
                _restore_redacted()
                logger.warning(
                    "Refusing to approve %s: scripts invalid after redaction: %s", name, report
                )
                return None
        return redact_backup

    @staticmethod
    def _auto_slug_from_name(name: str) -> str:
        """Return the bare slug for an auto-skill *name*, accepting either
        ``auto/<slug>`` or a bare ``<slug>``. Non-auto namespaces (any name with
        a slash after stripping the ``auto/`` prefix) fall through and are caught
        by the ``_is_pending_slug_safe`` guard at the call sites."""
        if name.startswith(f"{AUTO_SKILL_NAMESPACE}/"):
            return name.split("/", 1)[1]
        return name

    def get_auto_skill_version(self, name: str) -> int:
        """Return the ``version`` frontmatter of a live auto-skill (default 1).

        Accepts ``auto/<slug>`` or a bare ``<slug>``. Returns 1 when the skill
        is missing, has no ``version`` line, or the value is unparseable — so a
        pre-versioning skill reads as version 1.
        """
        slug = self._auto_slug_from_name(name)
        if not self._is_pending_slug_safe(slug):
            return 1
        skill_file = self._dir / AUTO_SKILL_NAMESPACE / slug / "SKILL.md"
        if not skill_file.exists():
            return 1
        raw = self._cached_frontmatter(skill_file).get("version", "")
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 1
        return v if v >= 1 else 1

    def read_auto_skill_body(self, name: str) -> str | None:
        """Return the full live ``SKILL.md`` text for an auto-skill, or ``None``.

        Accepts ``auto/<slug>`` or a bare ``<slug>``; refuses any non-auto
        namespace (a multi-segment name). Returns ``None`` when the skill is
        missing or unreadable. Used by the API to render an old-vs-new diff for
        update candidates.

        Refuses to follow a symlink anywhere on the path. This body is fed to the
        update-merge turn UNREDACTED (redaction runs on the merge OUTPUT), so a
        swapped ``SKILL.md`` symlink pointing at credential storage would put
        those bytes into an LLM prompt. Resolve, then verify the real path is
        still inside the skills tree and is not a sensitive location.
        """
        slug = self._auto_slug_from_name(name)
        if not self._is_pending_slug_safe(slug):
            return None
        base = self._dir / AUTO_SKILL_NAMESPACE / slug
        skill_file = base / "SKILL.md"
        if not skill_file.exists():
            return None
        # No symlink on the skill dir or the file itself.
        if os.path.islink(str(base)) or os.path.islink(str(skill_file)):
            logger.warning("Refusing to read %s: symlink on the live skill path", name)
            return None
        real = os.path.realpath(str(skill_file))
        # The resolved path must still live under the skills root, and must never
        # be a credential/sensitive location.
        try:
            Path(real).relative_to(os.path.realpath(str(self._dir)))
        except ValueError:
            logger.warning("Refusing to read %s: resolves outside the skills tree", name)
            return None
        if is_sensitive_path(real):
            logger.warning("Refusing to read %s: resolves to a sensitive path", name)
            return None
        try:
            # Read the RESOLVED path through the hardened primitive, not the
            # original one: the checks above vet ``real``, so reading
            # ``skill_file`` again would validate one path and read another.
            # safe_read_file re-checks is_sensitive_path and opens with
            # O_NOFOLLOW, closing a swap of the final component after our check.
            return safe_read_file(real)
        except (OSError, PermissionError):
            return None

    @staticmethod
    def _rewrite_update_frontmatter(
        candidate_content: str,
        *,
        target_name: str,
        created_at: str,
        version: int,
        pinned: bool = False,
        pointer_only: bool = False,
    ) -> str:
        """Rebuild an update candidate's body as the new live SKILL.md.

        Keeps the candidate's description/triggers/source/body (the merged new
        content) but forces ``name`` to the live target, preserves the live
        ``created_at``, and stamps ``version``. Any ``name`` / ``created_at`` /
        ``version`` / ``pinned`` / ``inject_on_trigger`` lines from the candidate
        are dropped and re-emitted so the live skill's identity + history are
        authoritative, not the candidate's. ``pinned`` is carried from the LIVE
        skill: a candidate never sets it, and losing it would drop the target's
        lifecycle exemption and expose a user-pinned skill to archival.
        ``pointer_only`` is carried the same way and for the same reason: a
        candidate never sets ``inject_on_trigger``, so dropping it would silently
        re-enable full-body injection on a skill the user had opted out — a
        setting reverting itself behind an unrelated approval.
        """
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", candidate_content, re.DOTALL)
        if m:
            fm_lines = m.group(1).split("\n")
            body = m.group(2)
        else:
            fm_lines = []
            body = candidate_content
        kept: list[str] = []
        for ln in fm_lines:
            if not ln.strip():
                continue
            key = ln.split(":", 1)[0].strip() if ":" in ln else ""
            if key in ("name", "created_at", "version", "pinned", "inject_on_trigger"):
                continue
            kept.append(ln)
        new_fm = [f"name: {target_name}"]
        new_fm.extend(kept)
        if created_at:
            new_fm.append(f"created_at: {created_at}")
        new_fm.append(f"version: {version}")
        if pinned:
            new_fm.append("pinned: true")
        if pointer_only:
            new_fm.append("inject_on_trigger: false")
        return "---\n" + "\n".join(new_fm) + "\n---\n\n" + body.strip() + "\n"

    def _versions_root(self, target_slug: str) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / target_slug / VERSIONS_DIRNAME

    def _prune_versions(self, versions_dir: Path) -> None:
        """Keep only the newest ``MAX_SKILL_VERSIONS`` ``v<N>-SKILL.md``
        snapshots in *versions_dir*, deleting the lowest-numbered excess."""
        if not versions_dir.is_dir():
            return
        snaps: list[tuple[int, Path]] = []
        for p in versions_dir.iterdir():
            mm = re.match(r"^v(\d+)-SKILL\.md$", p.name)
            if p.is_file() and mm:
                snaps.append((int(mm.group(1)), p))
        snaps.sort(key=lambda t: t[0])
        excess = len(snaps) - MAX_SKILL_VERSIONS
        for _n, p in snaps[:excess] if excess > 0 else []:
            try:
                p.unlink()
            except OSError:
                pass

    def preview_pending_update(self, slug: str) -> dict | None:
        """Return an approval preview for a pending UPDATE candidate.

        Produces ``{live_body, proposed_body, diff, from_version, to_version,
        base_version, stale_base}`` where ``proposed_body`` is the EXACT content
        ``approve_pending_update`` would write (same frontmatter rewrite), so the
        reviewer's diff is what approval actually does — not raw candidate text
        whose ``name`` / ``created_at`` / ``version`` lines are rewritten anyway.

        Returns ``None`` when the slug is unsafe, the candidate is missing or is
        not an update, or its target is no longer a live auto-skill. Read-only:
        never mutates the candidate or the live skill.
        """
        if not self._is_pending_slug_safe(slug):
            return None
        src = self._pending_root() / slug
        cand_file = src / "SKILL.md"
        if not cand_file.exists() or cand_file.is_symlink():
            return None
        meta = self._read_pending_meta(slug)
        if meta.get("kind") != "update":
            return None
        target = meta.get("target")
        if not isinstance(target, str) or not target:
            return None
        target_slug = self._auto_slug_from_name(target)
        if not self._is_pending_slug_safe(target_slug):
            return None
        live_file = self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md"
        if not live_file.exists():
            return None
        target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
        # Read the live body through the guarded reader (symlink + sensitive-path
        # + inside-tree checks) rather than touching the file directly — this
        # feeds the dashboard API.
        live_body = self.read_auto_skill_body(target_name)
        if live_body is None:
            return None
        try:
            cand_body = cand_file.read_text(encoding="utf-8")
        except OSError:
            return None
        current_version = self.get_auto_skill_version(target_name)
        _live_fm = self._cached_frontmatter(live_file)
        proposed_body = self._rewrite_update_frontmatter(
            cand_body,
            target_name=target_name,
            created_at=_live_fm.get("created_at", ""),
            version=current_version + 1,
            pinned=str(_live_fm.get("pinned", "")).strip().lower()
            in ("true", "1", "yes"),
            pointer_only=str(_live_fm.get("inject_on_trigger", "")).strip().lower() == "false",
        )
        # Redact both sides: this feeds the dashboard API, and the candidate is
        # only redacted in place at approve time (so an un-approved draft may
        # still hold a credential-shaped token).
        live_safe = self._redact_text(live_body)
        proposed_safe = self._redact_text(proposed_body)
        diff = "".join(
            difflib.unified_diff(
                live_safe.splitlines(keepends=True),
                proposed_safe.splitlines(keepends=True),
                fromfile=f"{target_name} (v{current_version}, live)",
                tofile=f"{target_name} (v{current_version + 1}, proposed)",
                n=3,
            )
        )
        raw_base = meta.get("base_version")
        return {
            "live_body": live_safe,
            "proposed_body": proposed_safe,
            "diff": diff,
            "from_version": current_version,
            "to_version": current_version + 1,
            "base_version": raw_base,
            "stale_base": isinstance(raw_base, int) and raw_base != current_version,
        }

    def _resolve_snapshot_version(self, versions_dir: Path, fm_version: int) -> int:
        """Return the version number to snapshot the CURRENT live body under.

        Normally the live frontmatter's ``version`` is authoritative. But if a
        snapshot already exists at that number the numbering has drifted (e.g. an
        older refine stripped the ``version`` line, so the live skill reads as v1
        again) — writing there would DESTROY the earlier snapshot. In that case
        continue above the highest snapshot on disk instead, so history is only
        ever appended to.
        """
        if not (versions_dir / f"v{fm_version}-SKILL.md").exists():
            return fm_version
        highest = fm_version
        for p in versions_dir.iterdir():
            mm = re.match(r"^v(\d+)-SKILL\.md$", p.name)
            if p.is_file() and mm:
                highest = max(highest, int(mm.group(1)))
        logger.warning(
            "Version numbering drifted for %s: snapshot v%d exists; continuing at v%d",
            versions_dir.parent.name,
            fm_version,
            highest + 1,
        )
        return highest + 1

    def approve_pending_update(self, slug: str) -> str | None:
        """Promote a pending UPDATE candidate over its live target auto-skill.

        Preconditions (all checked BEFORE any live mutation; a failure here
        leaves BOTH the live skill and the candidate untouched, returns None):
        the slug is safe, the candidate has a ``SKILL.md``, its ``.meta.json``
        has ``kind == "update"``, and ``target`` names an EXISTING live auto
        skill. Then: the shared symlink/unexpected-entry guard runs, scripts are
        re-validated, and SKILL.md + scripts are redacted in place (originals
        restored on failure).

        Promotion: snapshot the current live ``SKILL.md`` to
        ``auto/<target>/.versions/v<N>-SKILL.md`` (N = current live version),
        write the candidate over live with frontmatter rewritten (preserve live
        ``created_at``, ``name`` = ``auto/<target>``, ``version`` = N+1), move the
        candidate scripts into the live ``scripts/`` (exec bit set on POSIX),
        prune ``.versions`` to the newest ``MAX_SKILL_VERSIONS``, delete the
        pending dir, and SEL-audit. Returns ``auto/<target>`` on success.
        """
        if not self._is_pending_slug_safe(slug):
            return None
        src = self._pending_root() / slug
        if not (src / "SKILL.md").exists():
            return None
        meta = self._read_pending_meta(slug)
        if meta.get("kind") != "update":
            return None
        target = meta.get("target")
        if not isinstance(target, str) or not target:
            return None
        target_slug = self._auto_slug_from_name(target)
        if not self._is_pending_slug_safe(target_slug):
            return None
        live_dir = self._dir / AUTO_SKILL_NAMESPACE / target_slug
        live_skill = live_dir / "SKILL.md"
        if not live_skill.exists():
            logger.warning(
                "Refusing to approve update %s: target %r is not a live auto skill", slug, target
            )
            return None
        target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
        # The LIVE side is a write target here (unlike approve_pending_skill, which
        # moves into a fresh dest), so it needs its own symlink guard: a symlinked
        # ``scripts/`` (or any symlinked entry) would let ``mkdir``/``copy2`` follow
        # the link and write candidate content OUTSIDE the skill directory.
        if self._candidate_has_symlink(live_dir):
            logger.warning(
                "Refusing to approve update %s: live skill directory contains a symlink",
                target_name,
            )
            return None
        # Shared symlink + unexpected-entry rejection.
        if not self._candidate_layout_ok(src, target_name):
            return None
        # Re-validate + redact the candidate in place (restores originals on fail).
        redact_backup = self._validate_and_redact_candidate(src, target_name)
        if redact_backup is None:
            return None

        def _restore_redacted() -> None:
            for _fp, _b in redact_backup.items():
                try:
                    _fp.write_bytes(_b)
                except OSError:
                    pass

        # Compute the new live content from the redacted candidate BEFORE any
        # live mutation — a read failure aborts with live + candidate intact.
        try:
            candidate_body = (src / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            _restore_redacted()
            return None
        current_version = self.get_auto_skill_version(target_name)
        # Snapshot under a number that is guaranteed free, so an earlier snapshot
        # can never be destroyed by drifted numbering.
        versions_dir = self._versions_root(target_slug)
        snapshot_version = (
            self._resolve_snapshot_version(versions_dir, current_version)
            if versions_dir.is_dir()
            else current_version
        )
        new_version = snapshot_version + 1
        # ``base_version`` records the live version the merge was computed
        # against. If the live skill advanced since staging, this candidate's body
        # was merged from an OLDER base, so writing it would replace whatever the
        # intervening approval added. REFUSE rather than warn: the reviewer cannot
        # be relied on to notice, because an already-open sibling candidate's diff
        # is served from the frontend query cache and may still be the v1-based
        # one. The candidate stays pending so it can be dismissed (a fresh
        # proposal will be merged against the new base).
        raw_base = meta.get("base_version")
        if isinstance(raw_base, int) and raw_base != current_version:
            # The candidate stays pending so the reviewer can dismiss it, which
            # means it stays VISIBLE — so it must also stay byte-identical to what
            # was staged. Redaction already ran in place above; undo it, or the
            # rejected draft is left permanently altered and the diff the reviewer
            # re-opens is not the one they staged.
            _restore_redacted()
            logger.warning(
                "Refusing to approve stale update for %s: candidate based on v%s, live is v%d",
                target_name,
                raw_base,
                current_version,
            )
            sel().log_tool_invocation(
                session_key="skills",
                tool_name="auto_skill_update_approve",
                tool_kind="permission",
                outcome="rejected",
                metadata={
                    "target": target_name,
                    "base_version": raw_base,
                    "live_version": current_version,
                    "reason": "stale_base",
                },
            )
            return None
        live_created_at = self._cached_frontmatter(live_skill).get("created_at", "")
        # Carry the live skill's pin forward: a pinned skill is exempt from the
        # lifecycle's inactivity / max-N archival, and silently dropping the flag
        # here would expose a user-pinned skill to being archived.
        live_pinned = str(
            self._cached_frontmatter(live_skill).get("pinned", "")
        ).strip().lower() in ("true", "1", "yes")
        # Same for the injection opt-out: the candidate never carries it, so
        # writing it over live without this would silently turn full-body
        # injection back on for a skill the user had made pointer-only.
        live_pointer_only = (
            str(self._cached_frontmatter(live_skill).get("inject_on_trigger", "")).strip().lower()
            == "false"
        )
        new_live_content = self._rewrite_update_frontmatter(
            candidate_body,
            target_name=target_name,
            created_at=live_created_at,
            version=new_version,
            pinned=live_pinned,
            pointer_only=live_pointer_only,
        )
        # Snapshot the current live SKILL.md into .versions/ (point-of-no-return
        # is the live overwrite below; if the snapshot fails, live is untouched).
        versions_dir = self._versions_root(target_slug)
        snapshot = versions_dir / f"v{snapshot_version}-SKILL.md"
        try:
            versions_dir.mkdir(parents=True, exist_ok=True)
            live_prev = live_skill.read_text(encoding="utf-8")
            atomic_write(snapshot, live_prev)
        except OSError:
            _restore_redacted()
            logger.warning(
                "Refusing to approve update %s: could not snapshot live version", target_name
            )
            return None
        # (f) Write candidate over live.
        try:
            atomic_write(live_skill, new_live_content)
        except OSError:
            # atomic_write renames into place, so a failure leaves the live
            # SKILL.md untouched; drop the snapshot we just wrote and restore.
            try:
                snapshot.unlink()
            except OSError:
                pass
            _restore_redacted()
            logger.warning("Refusing to approve update %s: could not write live SKILL.md", target_name)
            return None
        # (g) Promote candidate scripts into the live scripts/ dir (exec bit on
        # POSIX). COPY rather than move: the pending dir is deleted in (i), so a
        # move that fails partway would leave the approved script in neither
        # place. Copying keeps the candidate intact as the rollback source, and
        # any failure aborts the whole approval — restoring the live SKILL.md
        # from the snapshot we just wrote and leaving the candidate reviewable.
        src_scripts = src / "scripts"
        copied: list[Path] = []
        # Pre-existing destinations we OVERWRITE: keep their original bytes+mode so
        # a rollback restores them. Without this, replacing an existing live script
        # and then failing on a later file would roll SKILL.md back while leaving
        # the replacement script live — an internally inconsistent skill.
        overwritten: dict[Path, tuple[bytes, int]] = {}
        if src_scripts.is_dir():
            live_scripts = live_dir / "scripts"
            try:
                live_scripts.mkdir(parents=True, exist_ok=True)
                for root, _dirs, files in os.walk(src_scripts):
                    rel_root = Path(root).relative_to(src_scripts)
                    for nm in files:
                        sfp = Path(root) / nm
                        if not sfp.is_file() or sfp.is_symlink():
                            continue
                        dest_dir = live_scripts / rel_root
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dfp = dest_dir / nm
                        if dfp.exists():
                            # Snapshot BEFORE the overwrite; a read failure here
                            # aborts rather than clobbering un-restorable content.
                            _st = dfp.stat()
                            overwritten[dfp] = (dfp.read_bytes(), _st.st_mode)
                        else:
                            # Only track files WE created, so a rollback never
                            # deletes a script the live skill already shipped.
                            copied.append(dfp)
                        shutil.copy2(str(sfp), str(dfp))
                        dfp.chmod(dfp.stat().st_mode | 0o111)
            except OSError:
                for _p in copied:
                    try:
                        _p.unlink()
                    except OSError:
                        pass
                for _p, (_b, _mode) in overwritten.items():
                    try:
                        _p.write_bytes(_b)
                        _p.chmod(_mode)
                    except OSError:
                        logger.error(
                            "Update %s rollback could not restore live script %s",
                            target_name,
                            _p.name,
                        )
                try:
                    atomic_write(live_skill, live_prev)
                except OSError:
                    logger.error(
                        "Update %s failed mid-promotion AND the live SKILL.md could "
                        "not be restored; the snapshot remains at %s",
                        target_name,
                        snapshot,
                    )
                else:
                    try:
                        snapshot.unlink()
                    except OSError:
                        pass
                _restore_redacted()
                logger.warning(
                    "Refusing to approve update %s: could not promote candidate scripts",
                    target_name,
                )
                return None
        # (h) Prune version history to the cap.
        self._prune_versions(versions_dir)
        # (i) Remove the pending candidate.
        shutil.rmtree(src, ignore_errors=True)
        # (j) Audit the approved update.
        sel().log_tool_invocation(
            session_key="skills",
            tool_name="auto_skill_update_approve",
            tool_kind="permission",
            outcome="invoked",
            metadata={
                "target": target_name,
                "from_version": current_version,
                "to_version": new_version,
                "base_version": raw_base,
                "stale_base": False,
            },
        )
        # (8) Make the updated live skill visible to trigger matching now.
        self._invalidate_iter_cache()
        logger.info(
            "Approved pending update: %s (v%d -> v%d)", target_name, current_version, new_version
        )
        return target_name

    def approve_pending_skill(self, slug: str) -> str | None:
        """Promote a pending candidate to a live auto-skill.

        Re-validates + redacts the candidate, then moves ``auto/.pending/<slug>``
        → ``auto/<slug>`` and marks any bundled scripts executable. Returns the
        live name, or ``None`` if the candidate is missing, a live skill of that
        name already exists, it contains a symlink, script validation fails, or
        redaction fails. Every check runs BEFORE the move, so a rejected
        candidate is left untouched in the pending queue.
        """
        if not self._is_pending_slug_safe(slug):
            return None
        src = self._pending_root() / slug
        if not (src / "SKILL.md").exists():
            return None
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        dest = self._dir / name
        if dest.exists():
            logger.warning("Cannot approve %s: a live skill already exists", name)
            return None
        # Reject any symlink in the candidate + any unexpected top-level entry
        # (defense-in-depth on top of the mandatory human review); promotion +
        # chmod must only touch known, real files. Factored into a shared helper
        # so the update-approve path enforces the identical layout guard.
        if not self._candidate_layout_ok(src, name):
            return None
        # Re-validate every script + redact the body + scripts before going live;
        # snapshots each file first so a failure restores the ORIGINAL bytes and
        # never leaves a corrupted pending draft. Shared with the update path.
        redact_backup = self._validate_and_redact_candidate(src, name)
        if redact_backup is None:
            return None

        def _restore_redacted() -> None:
            for _fp, _b in redact_backup.items():
                try:
                    _fp.write_bytes(_b)
                except OSError:
                    pass

        # Drop pending-only bookkeeping ONLY after every check + redaction has
        # passed and immediately before the move, so a failed approval leaves the
        # candidate — including its .meta.json (description/triggers) — intact in
        # the pending queue for re-review. A removal FAILURE (non-writable dir,
        # etc.) must ABORT: otherwise the raw, possibly secret-bearing .meta.json
        # would ride into the live skill dir and be exposed by the browser. Only
        # an already-absent file (FileNotFoundError) is benign. We stash the meta
        # bytes first so a subsequent MOVE failure can restore them (otherwise the
        # candidate would be left stranded in pending without its metadata).
        meta_path = src / ".meta.json"
        meta_backup: bytes | None = None
        try:
            meta_backup = meta_path.read_bytes()
        except FileNotFoundError:
            meta_backup = None
        except OSError:
            _restore_redacted()
            logger.warning(
                "Refusing to approve %s: could not read pending .meta.json before promotion", name
            )
            return None
        try:
            meta_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _restore_redacted()
            logger.warning(
                "Refusing to approve %s: could not remove pending .meta.json before promotion",
                name,
            )
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
        except OSError:
            # Promotion failed after we deleted the pending bookkeeping — restore
            # .meta.json AND the redacted files so the candidate stays intact in
            # the pending queue for re-review instead of being left corrupted.
            if meta_backup is not None and src.is_dir():
                try:
                    meta_path.write_bytes(meta_backup)
                except OSError:
                    pass
            _restore_redacted()
            logger.warning("Refusing to approve %s: could not move candidate live", name)
            return None
        # Mark scripts executable now that a human approved them (recursively).
        sdir = dest / "scripts"
        if sdir.is_dir():
            for root, _dirs, files in os.walk(sdir):
                for nm in files:
                    sf = Path(root) / nm
                    if sf.is_file() and not sf.is_symlink():
                        try:
                            sf.chmod(sf.stat().st_mode | 0o111)
                        except OSError:
                            pass
        self._invalidate_iter_cache()
        logger.info("Approved pending skill: %s", name)
        return name

    def dismiss_pending_skill(self, slug: str) -> bool:
        """Delete a pending candidate. Returns True if it existed."""
        if not self._is_pending_slug_safe(slug):
            return False
        pdir = self._pending_root() / slug
        if not pdir.is_dir():
            return False
        shutil.rmtree(pdir)
        logger.info("Dismissed pending skill: %s", slug)
        return True

    def prune_pending(self, ttl_days: int, *, now: float | None = None) -> int:
        """Remove pending candidates older than ``ttl_days``. Returns count pruned.

        Age is measured from the candidate directory's filesystem mtime (set when
        the queue writes it), NOT the LLM-supplied ``created_at`` metadata: a
        ``crystallize`` direct-write could stamp an arbitrarily old ``created_at``
        and trick pruning into ``rmtree``-ing fresh, unreviewed work.
        """
        if now is None:
            now = time.time()
        cutoff = now - ttl_days * 86400
        pruned = 0
        root = self._pending_root()
        for entry in self.list_pending_skills():
            pdir = root / entry["slug"]
            try:
                ts = pdir.stat().st_mtime
            except OSError:
                continue
            if ts <= cutoff and self.dismiss_pending_skill(entry["slug"]):
                pruned += 1
        return pruned

    def get_always_skills(self) -> list[str]:
        """Return names of skills marked ``always: true`` in frontmatter."""
        result: list[str] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            if meta.get("always", "").lower() == "true":
                scope = meta.get("repo_scope", "")
                if scope and not self._repo_scope_satisfied(scope):
                    continue
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
            # Repo-scoped skills are mechanically suppressed outside their
            # repo — word-overlap can fire on ordinary user phrasing, and a
            # prose scope guard alone is probabilistic.
            scope = meta.get("repo_scope", "")
            if scope and not self._repo_scope_satisfied(scope):
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
                # Record HOW each match was delivered, not just that it matched.
                # A pointer is an offer the agent may decline, so an auditor
                # reconstructing "was this procedure actually in the prompt?"
                # needs the split — the skill list alone no longer answers it.
                bodies, pointers = self.split_triggered(triggered)
                metadata["bodies"] = ",".join(bodies)
                metadata["pointers"] = ",".join(pointers)
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

    def split_triggered(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Split matched *names* into (inject-body, pointer-only), order preserved.

        Full-body injection is the DEFAULT: a matched skill's procedure lands in
        the prompt whether or not the agent chooses to read a file. A skill opts
        out with ``inject_on_trigger: false``, which reduces its contribution to
        a single pointer line naming it and its path.

        The default is deliberately the expensive one. A pointer makes delivery
        voluntary, so a skill authored to be *obeyed* on match — a mandatory
        pre-flight check, say — would be silently skipped by an agent that
        declines to read it, and a silent miss is the failure mode with no
        signal to catch it. Defaulting the other way would make forgetting the
        field fail open. Opting out is a per-skill statement that the skill is
        an offer rather than a mandate, which only its author can make.
        """
        enforced: list[str] = []
        pointer_only: list[str] = []
        for name in names:
            skill_file = self._resolve_path(name)
            if skill_file is None:
                continue
            meta = self._cached_frontmatter(skill_file)
            if meta.get("inject_on_trigger", "").strip().lower() == "false":
                pointer_only.append(name)
            else:
                enforced.append(name)
        return enforced, pointer_only

    def trigger_hint(self, names: list[str]) -> str:
        """Return a pointer block naming *names* and where to read each one.

        The counterpart to :meth:`get_triggered_skills` for a skill that opted
        out of full-body injection with ``inject_on_trigger: false``: the matcher
        decides which skills look relevant, and this renders that verdict as one
        line per skill instead of the skill's body. A body costs 8k-34k chars and
        is charged again on every turn the match repeats; a line costs ~150.

        The agent reaches the procedure the same way ``get_context``'s
        ``## Available Skills`` block already directs it to — by reading the
        path. The wording deliberately does NOT ask for a re-read of a skill
        already present earlier in the conversation: ACP replays native
        history, so that content is still in the window, and a needless ``cat``
        would spend a tool round-trip only to put the body back in as tool
        output.

        Returns ``""`` for an empty *names* (no block, not an empty header).
        """
        lines: list[str] = []
        for name in names:
            skill_file = self._resolve_path(name)
            if skill_file is None:
                continue
            meta = self._cached_frontmatter(skill_file)
            desc = self._short_desc(meta.get("description", "") or name, suffix="…")
            lines.append(
                f"- **{meta.get('name', name)}**: {desc} → `{skill_file}`"
            )
        if not lines:
            return ""
        return (
            "[Relevant skills for this message]\n"
            "These skills match this message. If one applies, read its file "
            "before acting — unless it already appears earlier in this "
            "conversation, in which case you already have its instructions.\n"
            + "\n".join(lines)
            + "\n[End of relevant skills]\n\n"
        )

    def _resolve_path(self, name: str) -> Path | None:
        """Return the ``SKILL.md`` path for an enumerated skill *name*.

        Allowlist-only, like ``resolve_dollar_skills``: the path comes from the
        enumeration rather than being constructed from *name*, so a crafted
        name cannot escape the skill roots.
        """
        for candidate, skill_file in self._iter():
            if candidate == name:
                return skill_file
        return None

    def get_context(self, budget: int | None = None, only: list[str] | None = None) -> str:
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

        *only* restricts the block to skills whose ``SKILL.md`` path matches one
        of the given fnmatch globs — the agent template's ``skill://`` mapping
        (see ``agent_discovery.agent_skill_globs``). ``None`` (the default) means
        no restriction. An *only* list that matches nothing yields ``""`` rather
        than silently falling back to the full catalog: an agent mapped to a
        skill that has since been deleted must not inherit every other skill.
        """
        all_skills = self.list_skills()
        if only is not None:
            all_skills = [s for s in all_skills if _matches_any(s.get("path", ""), only)]
        if not all_skills:
            return ""
        if budget is None:
            return self._legacy_context(all_skills, restricted=only is not None)
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
                    f"-> `{s['path']}`"
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

    def _legacy_context(self, all_skills: list[dict], restricted: bool = False) -> str:
        """Pre-lazy-load skills block (opt-in OFF, the default).

        Full content for pinned (``always: true``) skills + a one-line summary
        for EVERY on-demand skill, unranked and untruncated — byte-for-byte the
        behavior before the lazy-load feature, so leaving ``skills.lazy_load``
        off is a zero-impact upgrade.

        *restricted* marks *all_skills* as already narrowed by an agent's
        ``skill://`` mapping, so the always-loaded set is narrowed to match: a
        pinned skill outside the mapping must NOT be force-injected, or the
        mapping would not actually bound what the agent sees.
        """
        always = self.get_always_skills()
        if restricted:
            allowed = {s["key"] for s in all_skills} | {s["name"] for s in all_skills}
            always = [a for a in always if a in allowed]
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
                "To run a skill's scripts, `cd` into the directory containing its `SKILL.md`.",
                "",
            ]
            for s in on_demand:
                summary_lines.append(
                    f"- **{s['name']}**: {self._short_desc(s['description'])} → `{s['path']}`"
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
    def _short_desc(desc: str, suffix: str = "...") -> str:
        """Collapse whitespace and truncate a description for the summary line.

        Cuts on a word boundary when one falls in the last fifth of the budget so
        the line ends on a readable word instead of mid-token; a description with
        no such boundary (one very long token) is cut hard.
        """
        d = " ".join((desc or "").split())
        if len(d) <= _SHORT_DESC_CHARS:
            return d
        cut = d[:_SHORT_DESC_CHARS]
        space = cut.rfind(" ")
        if space >= _SHORT_DESC_CHARS * 4 // 5:
            cut = cut[:space]
        return cut.rstrip() + suffix

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
        # _iter() already applies local > extra-path precedence and dedupes
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
        """Parse YAML frontmatter from a markdown file (simple key: value).

        Only a key at column 0 is a field. An indented ``key: value`` belongs to
        the enclosing block scalar — a description that documents a setting, for
        instance — and reading it as the setting made the writer and the reader
        disagree: ``set_inject_on_trigger`` deliberately leaves an indented
        occurrence alone (deleting it would rewrite the author's prose), so
        honoring it here meant the opt-in could never take effect. Ignoring
        indented lines also drops the junk keys a prose line like
        ``  Steps: do x`` used to invent.
        """
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}
        meta: dict[str, str] = {}
        for line in match.group(1).split("\n"):
            if ":" in line and not line[:1].isspace():
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
