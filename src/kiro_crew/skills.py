"""Skills loader — markdown skill files for agent capabilities."""

from __future__ import annotations

import asyncio
import difflib
import errno
import fnmatch
import functools
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import batched, islice, zip_longest
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Iterator, NamedTuple

from kiro_crew import hooks as hooks_module
from kiro_crew import pinned_fs, skill_trust
from kiro_crew.atomic_write import (
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import referenced_skill_names
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import (
    FileTooLargeError,
    safe_read_file,
    safe_read_file_bytes_nolink,
    validate_file_path,
)
from kiro_crew.memory_recall import recall_terms
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.platform_compat import (
    ensure_owner_rwx_dirs,
    is_link_or_junction,
    rmtree_force,
)
from kiro_crew.project_scope import project_scope_satisfied
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_resolved_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.skill_search_index import (
    SKILL_SEARCH_INDEX_FILENAME,
    SkillSearchIndex,
    body_fingerprint,
)
from kiro_crew.skill_usage import SKILL_USAGE_FILENAME, SkillUsageLedger
from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES, validate_scripts
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

logger = logging.getLogger(__name__)


SKILLS_DIR_NAME = "skills"
# Filesystem latency dominates cold discovery. Bound both active readers and
# submitted work so a large catalog cannot create one thread/future per skill.
_CATALOG_READ_WORKERS = 8
_CATALOG_READ_BATCH = 64
# One script-entry population budget for the pending verdict and API reports.
# Reports may additionally retain ONE fixed truncation-summary entry.
_PENDING_SCRIPT_MAX_ENTRIES = 64
_VALIDATION_REPORT_MAX_FINDINGS = 16
_VALIDATION_REPORT_MAX_STRING_CHARS = 1024
_VALIDATION_REPORT_TRUNCATION_KEY = "<truncated>"
#: Re-exported from ``trigger_match``, which owns the value and the grammar
#: it belongs to. Kept as a module name because tests and call sites here
#: reference it.
_MIN_TRIGGER_OVERLAP = MIN_TRIGGER_OVERLAP

# Whether skill CRUD can address the skill directory and its SKILL.md relative to
# a pinned parent descriptor. supports_pinned_walk covers the openat capability
# itself; the extras are exactly the OTHER descriptor-relative syscalls this
# module's pinned branches issue, named one per call site so the probe stays
# derived from the code rather than copied from a neighbour:
#   os.mkdir  -- create, the leaf skill directory under the pinned parent
#   os.unlink -- create's rollback (the partial SKILL.md), and update, via
#                atomic_write's staging cleanup under the pinned parent
#   os.stat   -- delete, via pinned_fs.stat_at, and create's rollback, via
#                pinned_fs.remove_dir_verified (os.lstat is not a supports_dir_fd
#                member even on Linux; the capability belongs to os.stat)
#   os.rename -- create's rollback, via remove_dir_verified's stage-aside
#   os.rmdir  -- create's rollback, both the staged-aside directory and the
#                reclaim when the leaf open loses a race to the mkdir
# delete's own removal is still a by-name shutil.rmtree, the residual documented
# there -- os.rmdir is here for the ROLLBACK, not for that. update additionally
# needs a descriptor-relative rename for atomic_write's publish, which is that
# module's own probe and is asked at the call site. Where this is False (Windows)
# the by-name create/write/rmtree are the floor, unchanged.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {
    os.mkdir,
    os.unlink,
    os.stat,
    os.rename,
    os.rmdir,
}.issubset(os.supports_dir_fd)


def _body_term_hits(content: str, terms: Iterable[str]) -> int:
    """How many of *terms* the body carries, by the SAME rule the index uses.

    Tokenized, then prefix-matched per token, because the persisted index answers
    that way: it stores `recall_terms` output and matches a query term against
    stored terms it prefixes. A plain substring scan here would score the two paths
    differently, so whether a skill ranked at all would depend on which path
    answered for it -- and both can answer inside ONE search, since the index hands
    individual keys back for a direct read.

    Concretely, substring scoring let `rollback` match a body whose only occurrence
    is inside `scrollback`; the index calls that a near miss, and so does this.
    """
    body_terms = recall_terms(content)
    if not body_terms:
        return 0
    return sum(1 for term in terms if any(bt.startswith(term) for bt in body_terms))


def _namespace_groups(skills: list[dict]) -> list[tuple[str, int]]:
    """Family label and member count for the skills the discovery entry omits.

    Eight names out of a thousand skills describe what the operator used lately,
    not what the machine can do, and the tail is reachable only by a search whose
    keywords the model has to guess. A family label plus its size is the cheapest
    thing that answers "is there anything here about X": one short line, flat in
    the number of skills, and phrased in the same vocabulary the keys use, so
    ``app-* (40)`` is already a usable query.

    The label is the key's FIRST segment — the directory for a nested key
    (``kirocrew-dev/babysit`` -> ``kirocrew-dev/``), otherwise the leading hyphen
    token (``web-verify`` -> ``web-*``). One rule, so the line cannot reorder
    itself as skills are added. A family of one is left out: its label would carry
    no more than the name already does.
    """
    counts: dict[str, int] = {}
    for skill in skills:
        key = str(skill.get("key", ""))
        if "/" in key:
            label = key.split("/", 1)[0] + "/"
        elif "-" in key:
            label = key.split("-", 1)[0] + "-*"
        else:
            continue
        counts[label] = counts.get(label, 0) + 1
    return sorted(
        ((label, count) for label, count in counts.items() if count > 1),
        key=lambda pair: (-pair[1], pair[0]),
    )


#: Labels on one family line. A bound rather than a budget trim, so the line
#: cannot grow long enough to push a named skill (which carries the description,
#: the only text saying what a skill does) out of a tight allowance.
_FAMILY_LINE_MAX_LABELS = 6


def _family_line(skills: list[dict]) -> str:
    """One short line naming the families *skills* belong to, or ``""``.

    Bounded at :data:`_FAMILY_LINE_MAX_LABELS` labels, with the rest reported as
    a count, so the cost of this line does not scale with the catalog.
    """
    groups = _namespace_groups(skills)
    if not groups:
        return ""
    shown, rest = groups[:_FAMILY_LINE_MAX_LABELS], groups[_FAMILY_LINE_MAX_LABELS:]
    text = ", ".join(f"{label} ({count})" for label, count in shown)
    if rest:
        text += f", +{len(rest)} more"
    return text


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
# How long a discovered skill-file list is served before a REVALIDATION is due.
# Reaching this deadline never costs the caller a walk: the stale list is handed
# back and the re-walk is queued onto the catalog-refresh worker (see
# _request_catalog_refresh). So the deadline bounds how out-of-date an OUT OF BAND
# change (an AIM sync, a manual cp) may be, and nothing else — the app's own
# create/update/delete/refresh all call _invalidate_iter_cache(), so a skill
# written through the app is visible immediately regardless of this value.
#
# The value is sized against the walk it amortizes, not picked for tidiness: a walk
# of a real skills tree (645 files across 21 roots on a dev desktop, incl.
# AIM-installed package roots) takes ~0.7s, while chat messages arrive MINUTES
# apart, so anything on the order of seconds is missed by every message and
# amortizes nothing. At 60s one walk covers ~12 messages.
_ITER_CACHE_TTL_SECS = 60.0

# How long a caller with NOTHING to serve waits for the first walk of a root set.
#
# This is the one case where a turn can wait on discovery at all: no in-memory
# list, and no stored snapshot either — a machine's very first run, or one whose
# index file was deleted. It is a bounded wait on a background build, not a walk
# on the calling thread: when the budget runs out the caller is served whatever
# has been published, the scope is marked INCOMPLETE (see `catalog_status`), and
# the same build keeps going, so the next call adopts the finished snapshot.
#
# Why wait at all rather than return empty immediately: a small tree finishes
# inside this budget, and finishing is what makes `always: true` bodies known and
# therefore honored. Returning empty would make the first session on every machine
# start without its required instructions. The budget is what keeps a
# 5,000-skill tree from turning that guarantee into a minute of silence — such a
# tree is served from its snapshot on every run but the first.
_COLD_CATALOG_WAIT_SECS = 2.0

# A snapshot read off disk is revalidated only when it is older than this, so a
# process that starts, answers one call and exits does not queue a walk of a tree
# another process enumerated moments ago.
_CATALOG_REVALIDATE_AFTER_SECS = 60.0

# What an agent is told when its scope is served from an unfinished first walk.
# Named rather than inlined because two properties are load-bearing: it must say
# that an always-loaded skill may be MISSING (silence about that is the failure
# this notice exists to avoid), and it must name the call that re-reads the set,
# so the agent has an action rather than a warning.
_DISCOVERY_IN_PROGRESS_NOTICE = (
    "[Skills: discovery in progress]\n"
    "This machine's skill directory is still being built, so the set below may be "
    "incomplete and an always-loaded skill may not have been injected yet. Re-run "
    "skill_search(action='list', offset=0) before concluding a skill does not exist.\n"
    "[End of skills notice]\n\n"
)

# A granted repository remains attacker-controlled after consent. Bound the
# descriptor-relative walker well below Python's recursion limit so a malicious
# nesting chain cannot crash discovery for the whole chat turn. Depth counts
# directories below the project's .kiro/skills root; files at the cap still load.
_PROJECT_SKILL_MAX_DEPTH = 64
# Byte bound on any confined project skill body read on behalf of a session:
# context injection, the skill_search body grep and the /api/skills search
# route all read through this one cap, so an oversized project SKILL.md is
# skipped rather than loaded whole.
PROJECT_SKILL_BODY_CAP = 24_750
PINNED_SKILL_BODIES_CAP = 99_000


class SkillContextCapacityError(ValueError):
    """Required instructions cannot fit; never silently cut a required skill."""


# The "[Skills:]" opener and "[End of skills]" closer wrapping the whole block.
_MAPPED_BLOCK_OVERHEAD_BYTES = 64

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


# Counterpart observer for candidates LEAVING the queue (approved, dismissed,
# or TTL-pruned). Module-level for the same reason as the staged hook: any
# loader instance can consume a candidate. The gateway registers a hook that
# retires the candidate's bell-feed notification — without it, the "awaiting
# review" row stays unread forever and its deep link lands on the
# no-longer-awaiting-review banner.
_PENDING_CONSUMED_HOOK: "Callable[[dict], None] | None" = None


def set_pending_consumed_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear, with ``None``) the pending-candidate consumed observer.

    Called once at gateway boot. Idempotent — a later call replaces the hook.
    """
    global _PENDING_CONSUMED_HOOK
    _PENDING_CONSUMED_HOOK = fn


def _emit_pending_consumed(payload: dict) -> None:
    """Invoke the pending-consumed hook, swallowing every failure.

    Consumption has already succeeded on disk by the time this runs; a broken
    observer must never turn a successful approve/dismiss into a failure.
    """
    fn = _PENDING_CONSUMED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("pending-consumed hook failed", exc_info=True)


# Frontmatter field used to mark a skill as auto-generated.  Absence means
# the skill carries no source field, i.e. is hand-authored.
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
_SHELL_READ_VERBS = frozenset({"cat", "bat", "head", "tail", "less", "more", "view", "type"})

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


def _decode_skill_text(raw: bytes, *, strict: bool = True) -> str:
    """Decode SKILL.md bytes with ``read_text``'s newline handling.

    These reads take bytes rather than ``read_text`` so containment can be checked
    on the descriptor actually opened. ``read_text`` opens in TEXT mode and
    performs universal-newline translation; a bytes read does not. Git checks out
    CRLF on Windows, so without this every frontmatter key would carry a trailing
    ``\r``, nothing would match ``always`` or ``pinned``, and skill bodies would
    silently stop being injected there while Linux and macOS looked fine.

    *strict* decoding propagates invalid UTF-8, which a WRITER must hear
    (``update_auto_skill`` carries version metadata across a rewrite). Callers
    that only render text pass ``strict=False``.
    """
    text = raw.decode("utf-8") if strict else raw.decode("utf-8", errors="replace")
    # Universal newlines, matching TEXT-mode reads: CRLF and lone CR both fold.
    return text.replace("\r\n", "\n").replace("\r", "\n")


_PROJECT_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_project_dir_chain(base: Path) -> int | None:
    """Open every absolute path component through the prior no-follow handle."""
    if not skill_trust.project_skill_traversal_supported():
        return None
    parts = Path(os.path.abspath(base)).parts
    try:
        fd = os.open(parts[0], _PROJECT_DIR_OPEN_FLAGS)
    except OSError:
        return None
    for part in parts[1:]:
        try:
            next_fd = os.open(part, _PROJECT_DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError:
            os.close(fd)
            return None
        os.close(fd)
        fd = next_fd
    return fd


def _walk_confined_skill_fd(
    fd: int,
    current: Path,
    *,
    depth: int = 0,
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Yield an ``os.walk``-shaped tree anchored to directory descriptors."""
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(fd) as scanner:
            for entry in scanner:
                try:
                    entries.append((entry.name, entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        return

    dirs = sorted(name for name, st in entries if stat.S_ISDIR(st.st_mode))
    files = sorted(name for name, st in entries if stat.S_ISREG(st.st_mode))
    if depth >= _PROJECT_SKILL_MAX_DEPTH:
        dirs = []
    # The consumer prunes dot-directories in place before traversal resumes.
    yield str(current), dirs, files
    for name in dirs:
        try:
            child_fd = os.open(name, _PROJECT_DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError:
            # A directory swapped for a link, or removed, fails here without
            # resolving its target.
            continue
        try:
            yield from _walk_confined_skill_fd(child_fd, current / name, depth=depth + 1)
        finally:
            os.close(child_fd)


def _walk_confined_skill_tree(base: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    """Walk a project tree without path-based traversal or link following."""
    fd = _open_project_dir_chain(base)
    if fd is None:
        # A project without .kiro/skills is the common case. Missing, linked,
        # unreadable, and unsupported cannot be distinguished without probing
        # the path again, so keep the refusal observable without warning on
        # every ordinary catalog scan.
        logger.debug(
            "Refusing project skills traversal; a component is missing, linked, "
            "unreadable, or the platform lacks no-follow dirfd support: %s",
            base,
        )
        return
    try:
        yield from _walk_confined_skill_fd(fd, base)
    finally:
        os.close(fd)


def _disabled_app_names() -> frozenset[str]:
    """Installed apps that are currently DISABLED.

    Used to keep a disabled app's bundled skills out of trigger matching.
    ``bridges`` registers each app skill under ``skills/<app>/<skill>`` (plus a
    flat link), so the first path segment names the owning app.

    Read once per matching pass rather than per skill: this runs on every
    message, and ``is_app_enabled`` reads a JSON file per call. Failures return
    an EMPTY set on purpose — the gate then hides nothing, which keeps a
    transient read error from silently stripping an enabled app's skills.
    Deferred import: ``apps.manager`` is a higher layer than this module.
    """
    try:
        from kiro_crew.apps.manager import list_apps

        return frozenset(
            str(a.get("name")) for a in list_apps() if a.get("name") and not a.get("enabled")
        )
    except Exception:
        logger.debug("skills: could not read app enablement", exc_info=True)
        return frozenset()


def _dedupe_identical_skills(skills: list[dict]) -> list[dict]:
    """Drop later rows that are verified byte-identical copies of an earlier row.

    Two stages, so correctness never rests on a metadata coincidence:

    1. **Candidate fingerprint** — ``(name, description, size_bytes)``, the
       fields a summary line is rendered from, all already loaded by
       ``list_skills()``. No collision (the overwhelmingly common case) means
       no file I/O at all.
    2. **Content verification** — compare the SHA-256 digests captured when
       metadata was safely read, and drop the later row **only when the digests
       match**. Equal-metadata skills whose bodies differ (which the
       pinned path would inject in full) are all kept; an unreadable file is
       kept, never dropped.

    The first row wins, preserving the walk order's operator-installed
    precedence. Confined project rows are exempt entirely: their reads are
    gated through the descriptor-pinned reader, and mirrored-root duplicates
    only arise from unconfined trees anyway.
    """
    seen: dict[tuple[str, str, int], list[dict]] = {}
    out: list[dict] = []
    for s in skills:
        if s.get("confine_root"):
            out.append(s)
            continue
        fp = (str(s.get("name", "")), str(s.get("description", "")), int(s.get("size_bytes") or 0))
        rivals = seen.setdefault(fp, [])
        if rivals:
            this_digest = s.get("content_digest")
            if this_digest and any(r.get("content_digest") == this_digest for r in rivals):
                continue  # verified byte-identical copy of an earlier row
        rivals.append(s)
        out.append(s)
    return out


@functools.lru_cache(maxsize=None)
def _builtin_dir_app_name(pkg_dir: str) -> str | None:
    """The manifest name of the builtin app shipped in *pkg_dir*, or ``None``.

    A shipped builtin's package directory is named for its Python package
    (``auto_improvement``) while the app registry keys on the manifest name
    (``auto-improvement``), so the mapping must come from the manifest itself —
    the same source ``apps.discovery`` registers builtins from. Cached for the
    process lifetime: the installed package tree is immutable while running,
    and this is consulted from the per-message trigger-matching pass.
    """
    try:
        with open(os.path.join(pkg_dir, "app.json"), encoding="utf-8") as fh:
            name = json.load(fh).get("name")
        return name if isinstance(name, str) and name else None
    except Exception:
        return None


def _iter_skill_files(
    base: Path,
    *,
    confine_to: tuple[str, ...] | None = None,
    exclude_roots: tuple[str, ...] = (),
) -> list[tuple[str, Path]]:
    """Recursively find all SKILL.md files under *base*.

    Returns ``(relative_name, skill_file_path)`` pairs sorted by name.
    The relative name uses ``/`` as separator (e.g. ``utils/tiny-url``).

    Unconfined provider trees follow links because apps register skills through
    them. Confined project trees never follow directory links or junctions: a
    link target can be a Windows UNC path, where descent would leak credentials.
    """
    if confine_to is not None:
        if len(confine_to) != 1:
            return []
        expected_base = os.path.abspath(Path(confine_to[0]) / ".kiro" / "skills")
        supplied_base = os.path.abspath(base)
        if os.path.normcase(supplied_base) != os.path.normcase(expected_base):
            return []
        results: list[tuple[str, Path]] = []
        for dirpath, dirs, files in _walk_confined_skill_tree(base):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            if "SKILL.md" not in files:
                continue
            skill_file = Path(dirpath) / "SKILL.md"
            rel = skill_file.parent.relative_to(base)
            results.append((str(rel).replace("\\", "/"), skill_file))
        return sorted(results, key=lambda item: item[0])

    if not base.exists():
        return []
    allowed_roots = (os.path.realpath(base),) + _trusted_skill_roots()

    def probe(directory: Path) -> tuple[str | None, list[Path], Path | None]:
        lexical = os.path.abspath(directory)
        if exclude_roots and _within_any(lexical, exclude_roots):
            return lexical, [], None
        real = os.path.realpath(directory)
        if exclude_roots and _within_any(real, exclude_roots):
            return real, [], None
        if not _within_any(real, allowed_roots) or is_sensitive_resolved_path(real):
            return real, [], None
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            # A failed alias has not visited its target. Another spelling may
            # still enumerate it successfully, as with os.walk's error handling.
            return None, [], None
        children = []
        has_skill = False
        for entry in entries:
            if exclude_roots and _within_any(os.path.abspath(entry.path), exclude_roots):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir and not entry.name.startswith("."):
                children.append(directory / entry.name)
            elif not is_dir and entry.name == "SKILL.md":
                has_skill = True
        children.sort(key=lambda path: path.name)
        skill_file = directory / "SKILL.md"
        if not has_skill:
            return real, children, None
        real_file = os.path.realpath(skill_file)
        if exclude_roots and _within_any(real_file, exclude_roots):
            return real, children, None
        if not _within_any(real_file, allowed_roots) or is_sensitive_resolved_path(real_file):
            return real, children, None
        return real, children, skill_file

    results = []
    seen_real: set[str] = set()
    # Commit probes in sorted depth-first order, irrespective of completion
    # order, so links sharing a target keep the same canonical skill key.
    stack = [base]
    pending: dict[Path, Future[tuple[str | None, list[Path], Path | None]]] = {}
    with ThreadPoolExecutor(
        max_workers=_CATALOG_READ_WORKERS, thread_name_prefix="skill-walk"
    ) as pool:
        while stack:
            for directory in reversed(stack):
                if len(pending) >= _CATALOG_READ_BATCH:
                    break
                if directory not in pending:
                    pending[directory] = pool.submit(copy_context().run, probe, directory)
            directory = stack.pop()
            future = pending.pop(directory, None)
            real, children, discovered_file = future.result() if future else probe(directory)
            if real is None or real in seen_real:
                continue
            seen_real.add(real)
            if discovered_file is not None:
                name = str(directory.relative_to(base)).replace("\\", "/")
                results.append((name, discovered_file))
            stack.extend(reversed(children))
    return sorted(results, key=lambda item: item[0])


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


# Provenance marker written into every skill directory this sync installs.
# A dotfile (never a SKILL.md field) so it can never render in skill listings:
# the loader only reads SKILL.md, and dot-entries are pruned from discovery.
# Its content is the full-tree fingerprint of the copy the sync wrote, which is
# what later runs compare against before destroying the destination.
_PROVENANCE_MARKER = ".builtin-skill-provenance"

# Version prefix on the marker content ("<format>:<fingerprint>"). Bump this
# whenever the fingerprint encoding changes (new entry kinds, mode bits, hash
# input layout): a marker in any other format is unparseable rather than
# comparable, so ``_recorded_fingerprint`` reports "no provenance" and the
# sync falls back to the packaged-tree adoption comparison. Without the
# version, an encoding change would make every recorded fingerprint mismatch
# its own unchanged tree and quarantine every untouched builtin fleet-wide.
_PROVENANCE_FORMAT = "2"

# Ceilings on what one tree verification may cost. Fingerprinting runs at
# gateway startup on the event loop, so both the read volume and the walk
# length must stay bounded regardless of what a user placed in the skills dir;
# a tree over either ceiling is treated as "cannot prove" (diverged), and the
# safe direction for anything unprovable is preservation. Packaged builtin
# skills are a few MB and a few dozen entries at most.
_FINGERPRINT_MAX_BYTES = 32 * 1024 * 1024
_FINGERPRINT_MAX_ENTRIES = 4096


def _tree_entries(
    root: Path, *, assume_owner_rwx_dirs: bool = False
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(relative path, kind, detail)`` for the tree under *root*.

    Deterministic order (sorted, top-down), lstat-based, and it never opens or
    follows anything: symlinks yield their target text (``link``), regular
    files their size (``file``), directories ``dir``, and FIFOs / devices /
    sockets ``special`` — so a hostile or accidental special file can never
    hang the walk. Entries that cannot be lstat'ed — and directories the walk
    itself cannot list (``os.walk`` reports those through ``onerror`` instead
    of raising) — yield ``unreadable``, which callers must treat as unequal to
    everything (fail toward "diverged"). The provenance marker itself is
    skipped: it records the fingerprint, so including it would make the
    recorded value impossible to reproduce.
    """
    walk_errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames.sort()
        for dname in list(dirnames):
            entry = Path(dirpath) / dname
            rel = (rel_dir / dname).as_posix()
            try:
                mode = os.lstat(entry).st_mode
            except OSError:
                dirnames.remove(dname)
                yield rel, "unreadable", ""
                continue
            if stat.S_ISLNK(mode) or is_link_or_junction(entry):
                # os.walk(followlinks=False) does not descend POSIX symlinks,
                # but a Windows junction lstats as a plain directory and WOULD
                # be descended — into whatever tree it targets (e.g. a
                # credential directory), enumerating paths outside the
                # file-read gate. Classify both as links so a retargeted
                # link/junction changes the fingerprint, and keep the walk
                # out of the target either way.
                dirnames.remove(dname)
                try:
                    yield rel, "link", os.readlink(entry)
                except OSError:
                    yield rel, "unreadable", ""
            else:
                # Permission bits, like file modes below: a chmod on an
                # installed builtin's directory is a user customization and
                # must diverge the tree instead of being silently reset by
                # the next sync. One deliberate asymmetry: when the caller
                # sets ``assume_owner_rwx_dirs`` (used ONLY for the
                # PACKAGED SOURCE side of a comparison), owner rwx is OR-ed
                # in because the install adds those bits to the fresh
                # copy's directories (``ensure_owner_rwx_dirs`` -- a
                # read-only source such as a Nix store ships 0o555 and the
                # copy must accept marker writes and directory search).
                # A 0o455-class source needs execute added too. Hashing AS
                # THE COPY WILL LOOK keeps that install-owned repair from
                # reading as a user chmod, while the INSTALLED side is always
                # hashed with its real modes -- so a user chmod on the copy,
                # including removing an owner-rwx bit, still diverges. Files
                # are never normalized: the install never rewrites file modes.
                dir_mode = stat.S_IMODE(mode)
                if assume_owner_rwx_dirs:
                    dir_mode |= stat.S_IRWXU
                yield rel, "dir", f"{dir_mode:o}"
        for fname in sorted(filenames):
            entry = Path(dirpath) / fname
            rel = (rel_dir / fname).as_posix()
            if rel == _PROVENANCE_MARKER:
                continue
            try:
                st = os.lstat(entry)
            except OSError:
                yield rel, "unreadable", ""
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    yield rel, "link", os.readlink(entry)
                except OSError:
                    yield rel, "unreadable", ""
            elif stat.S_ISREG(st.st_mode):
                # Size AND permission bits: a mode-only customization (e.g.
                # chmod +x on a builtin script) is a user edit and must
                # diverge the tree. copytree preserves modes, so a clean
                # install still fingerprints equal to its package.
                yield rel, "file", f"{st.st_size}:{stat.S_IMODE(st.st_mode):o}"
            else:
                yield rel, "special", ""
    for err in walk_errors:
        # A directory the walk could not list may hold anything: surface it as
        # an unreadable entry so no consumer can mistake the tree for empty,
        # equal, or provable.
        yield getattr(err, "filename", None) or "<walk-error>", "unreadable", ""


def _trees_stat_equal(a: Path, b: Path) -> bool:
    """Stat-level lazy tree comparison: bail at the first mismatching entry.

    This is the cheap gate in front of content hashing on the startup path: a
    diverged destination (the common case for an unmarked directory that is
    not ours) costs directory listings and lstats up to the first difference,
    never a file read. ``unreadable`` equals nothing, including itself, and a
    pair of trees longer than the entry ceiling is unprovable (unequal) so the
    walk itself stays bounded. The roots' own permission bits are compared
    too: ``_tree_entries`` only yields children, and a chmod on the skill
    directory itself is as much a user customization as one on any child.
    """
    try:
        # ``a`` is the INSTALLED tree (hashed with real modes), ``b`` is the
        # PACKAGED SOURCE, whose directory modes are compared as the copy
        # will look after ``ensure_owner_rwx_dirs`` -- the same
        # asymmetry ``_tree_entries`` applies for child directories. A user
        # chmod on the installed side (including removing owner rwx)
        # therefore still diverges.
        if stat.S_IMODE(os.lstat(a).st_mode) != (stat.S_IMODE(os.lstat(b).st_mode) | stat.S_IRWXU):
            return False
    except OSError:
        return False
    entries = 0
    for ea, eb in zip_longest(_tree_entries(a), _tree_entries(b, assume_owner_rwx_dirs=True)):
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return False
        if ea is None or eb is None or ea != eb or ea[1] == "unreadable":
            return False
    return True


def _skill_tree_fingerprint(root: Path, *, assume_owner_rwx_dirs: bool = False) -> str | None:
    """Stable content hash of the whole skill tree under *root*.

    Covers every entry ``_tree_entries`` yields — file bytes, symlink targets,
    directory structure, special-file presence — so a destination differing
    only by a user-added script, note, empty directory, or a file swapped for
    a symlink fingerprints as diverged.

    Returns None when the tree cannot be proven: a link-or-junction root, an
    unreadable entry, more entries than ``_FINGERPRINT_MAX_ENTRIES``, or more
    file content than ``_FINGERPRINT_MAX_BYTES``. None never equals a recorded
    or computed fingerprint, so every unprovable tree is treated as diverged
    and preserved. File bytes are read through
    :func:`kiro_crew.hooks.safe_read_file_bytes_nolink` with the tree root as
    containment: the descriptor-pinned check rejects symlinks, hardlinked
    inodes, non-regular files, sensitive paths, and any resolved path outside
    the root — so a component swapped between the walk and the open (or a
    hardlink planted at a walked name) reads as unprovable instead of leaking
    outside bytes (e.g. credentials) into the hash.
    """
    if is_link_or_junction(root):
        return None
    digest = hashlib.sha256()
    # The root's own permission bits are part of the installed state: a chmod
    # on the skill directory itself must diverge the fingerprint exactly like
    # a chmod on any entry inside it.
    try:
        # ``assume_owner_rwx_dirs`` (set only when hashing the PACKAGED
        # SOURCE) ORs owner rwx in, so the recorded fingerprint describes
        # the copy as it will exist after ``ensure_owner_rwx_dirs``.
        # The installed side is always hashed with its real modes.
        root_mode = stat.S_IMODE(os.lstat(root).st_mode)
        if assume_owner_rwx_dirs:
            root_mode |= stat.S_IRWXU
    except OSError:
        return None
    digest.update(f"root\0{root_mode:o}\0".encode("utf-8"))
    budget = _FINGERPRINT_MAX_BYTES
    entries = 0
    for rel, kind, detail in _tree_entries(root, assume_owner_rwx_dirs=assume_owner_rwx_dirs):
        if kind == "unreadable":
            return None
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return None
        digest.update(f"{kind}\0{rel}\0{detail}\0".encode("utf-8", "surrogatepass"))
        if kind != "file":
            continue
        try:
            data = safe_read_file_bytes_nolink(
                str(root / rel), within_root=str(root), max_bytes=budget
            )
        except FileTooLargeError:
            # Over the remaining byte budget: the tree costs more to prove
            # than the ceiling allows, so it is unprovable (preserved).
            return None
        if data is None:
            return None
        budget -= len(data)
        digest.update(data)
    return digest.hexdigest()


def _recorded_fingerprint(dest_dir: Path) -> str | None:
    """Return the fingerprint the sync recorded in *dest_dir*, or None.

    A link or junction at the marker path is not a marker (the sync writes
    only regular files): it reads as "no provenance" (user-authored by
    assumption) instead of being followed. ``O_NOFOLLOW`` enforces this
    race-free on POSIX; Windows has no such flag, so the explicit
    link-or-junction probe carries the check there. The fstat re-check keeps
    a FIFO raced onto the path from blocking startup.
    """
    marker = dest_dir / _PROVENANCE_MARKER
    if is_link_or_junction(marker):
        return None
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(marker, open_flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    content = data.decode("utf-8", errors="replace").strip()
    # Only the current format is comparable. An older (or newer, on
    # downgrade) format encodes the fingerprint differently, so comparing it
    # against a freshly computed value would misread every unchanged tree as
    # diverged; treating it as "no provenance" routes those trees through the
    # packaged-tree adoption comparison instead, which re-records ownership
    # in the current format when the copy is verifiably unchanged.
    prefix = _PROVENANCE_FORMAT + ":"
    if not content.startswith(prefix):
        return None
    return content[len(prefix) :] or None


def _write_provenance_marker(dest_dir: Path, fingerprint: str) -> None:
    """Record *fingerprint* as the sync-installed state of *dest_dir*.

    ``atomic_write`` stages a unique temp file and renames it over the marker
    path: the rename replaces whatever occupies that path (including a planted
    symlink) rather than following it, so this write can never land outside
    the skill directory. Best-effort: a failed write only means the next run
    re-derives ownership against the packaged tree, so absence self-heals and
    must never break skill loading.
    """
    try:
        atomic_write(
            dest_dir / _PROVENANCE_MARKER,
            f"{_PROVENANCE_FORMAT}:{fingerprint}\n",
        )
    except OSError:
        logger.warning("could not record builtin-skill provenance in %s", dest_dir, exc_info=True)


def _record_builtin_provenance(dest_dir: Path) -> None:
    """Fingerprint the tree at *dest_dir* and record it as sync-installed."""
    fingerprint = _skill_tree_fingerprint(dest_dir)
    if fingerprint is None:
        logger.warning("skill tree %s cannot be fingerprinted; leaving it unmarked", dest_dir)
        return
    _write_provenance_marker(dest_dir, fingerprint)


def _verified_unchanged_fingerprint(dest_dir: Path, src_dir: Path | None) -> str | None:
    """Return *dest_dir*'s fingerprint iff it is verifiably an unchanged copy
    this sync installed, else None.

    Two ways to prove ownership:
    - The recorded provenance fingerprint still matches the tree on disk.
    - First-install migration rule: installs that predate provenance recording
      carry no marker, and a naive "no marker means user-authored" rule would
      freeze every already-installed builtin at its current version forever.
      So an UNMARKED destination counts as builtin-owned exactly when it
      matches the packaged tree (*src_dir*) byte-for-byte; anything that
      genuinely differs — a user skill, a user-edited builtin, or a builtin
      from an older package whose content has since changed — is user data by
      assumption and is preserved. The stale-cleanup entries have no packaged
      tree left to compare against (``src_dir`` is None), so for them an
      unmarked directory is always user data.

    A destination that is itself a link or junction is never owned: the sync
    only ever creates real directories, and every verification primitive here
    would otherwise read the link's TARGET tree.
    """
    if is_link_or_junction(dest_dir):
        return None
    recorded = _recorded_fingerprint(dest_dir)
    if recorded is not None:
        current = _skill_tree_fingerprint(dest_dir)
        return current if current == recorded else None
    if src_dir is None:
        return None
    if not _trees_stat_equal(dest_dir, src_dir):
        return None
    dest_fingerprint = _skill_tree_fingerprint(dest_dir)
    if dest_fingerprint is None:
        return None
    if dest_fingerprint != _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True):
        return None
    return dest_fingerprint


# A cron script body is one file, not a tree, so its ceiling sits far below the
# whole-tree budget above. A body over this size reads as unverifiable rather
# than being compared -- the same fail-safe direction an unprovable tree takes.
_CRON_SOURCE_MAX_BYTES = 2 * 1024 * 1024

CRON_SOURCE_IN_SYNC = "in-sync"
CRON_SOURCE_DIVERGED = "diverged"
CRON_SOURCE_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class CronScriptSource:
    """One deployed cron script judged against the installed skill asset it came from."""

    name: str
    source: Path
    state: str


def _skill_script_index(base: Path) -> dict[str, list[Path]]:
    """Map each ``*.py`` script asset name to the installed skills shipping it.

    A name can be shipped by more than one skill, so the value is a list and the
    caller decides -- guessing an owner would invent provenance the copy never
    recorded.
    """
    index: dict[str, list[Path]] = {}
    for _name, skill_file in _iter_skill_files(base):
        scripts = skill_file.parent / "scripts"
        if not scripts.is_dir():
            continue
        for entry in sorted(scripts.glob("*.py")):
            index.setdefault(entry.name, []).append(entry)
    return index


def _read_for_comparison(path: Path, root: Path) -> bytes | None:
    """Read *path* under *root* containment, or None when it cannot be proven."""
    try:
        return safe_read_file_bytes_nolink(
            str(path), within_root=str(root), max_bytes=_CRON_SOURCE_MAX_BYTES
        )
    except FileTooLargeError:
        return None
    except OSError:
        return None


def deployed_cron_script_sources() -> list[CronScriptSource]:
    """Judge each deployed cron script that has an installed skill asset of its name.

    This is the second hop of the journey :func:`_verified_unchanged_fingerprint`
    already guards. The first hop -- packaged ``builtin_skills/`` into the
    installed skills dir -- is verified by CONTENT, the ``scripts/`` subtree
    included, precisely because a release that changes only a script leaves
    ``SKILL.md`` byte-identical, so a manifest-only comparison reports "up to
    date" while the install keeps running superseded code. The second hop --
    installed skill asset into ``<config_dir>/crons/`` -- is a hand-run ``cp``
    documented in the owning skill, and nothing has ever compared its two sides.
    The same silent staleness the first hop was taught to catch is therefore
    unobserved one step later.

    Scope is deliberately narrow. A deployed script with NO installed skill asset
    of that name is ABSENT from the result rather than reported: cron script
    bodies are LLM-writeable by design (see :mod:`kiro_crew.cron_script`) and
    most are authored in place with no source anywhere, so whether they ought to
    have one is a product question this function does not raise. Only a script
    that DOES have a source can be out of step with it.

    Reads go through the containment-checked reader the fingerprint helpers use,
    so a symlink, a hardlinked inode, a non-regular file, a path escaping its
    root, or an oversized body yields ``CRON_SOURCE_UNVERIFIABLE`` instead of a
    comparison. Unverifiable never reads as agreement -- an instrument whose read
    failed must not report the two sides equal.

    When several skills ship the same script name, agreement with ANY of them is
    ``CRON_SOURCE_IN_SYNC``: the copy records no owner, so a mismatch against an
    arbitrarily chosen candidate would be a fabricated finding.
    """
    crons_root = config_dir() / "crons"
    skills_root = skills_dir()
    if not crons_root.is_dir() or not skills_root.is_dir():
        return []
    index = _skill_script_index(skills_root)
    if not index:
        return []

    results: list[CronScriptSource] = []
    for deployed in sorted(crons_root.glob("*.py")):
        candidates = index.get(deployed.name)
        if not candidates:
            # No source to be out of step with -- out of scope by design.
            continue
        body = _read_for_comparison(deployed, crons_root)
        state = CRON_SOURCE_UNVERIFIABLE
        matched = candidates[0]
        if body is not None:
            unreadable = 0
            for candidate in candidates:
                source_body = _read_for_comparison(candidate, skills_root)
                if source_body is None:
                    unreadable += 1
                    continue
                if source_body == body:
                    matched = candidate
                    state = CRON_SOURCE_IN_SYNC
                    break
            else:
                # Every candidate was read and none matched, or some could not
                # be read at all. Only the fully-read case is a real divergence;
                # an unread candidate might have been the matching one.
                state = CRON_SOURCE_UNVERIFIABLE if unreadable else CRON_SOURCE_DIVERGED
        results.append(CronScriptSource(name=deployed.name, source=matched, state=state))
    return results


def _claim_dir_for_replacement(dest_dir: Path) -> Path | None:
    """Atomically move *dest_dir* to a dot-prefixed sibling before verifying.

    Verify-then-delete has a race: another process (an editor, a second
    Kiro Crew instance syncing the same home) can swap the directory between
    the fingerprint check and the rmtree, destroying a tree the check never
    saw. Renaming first makes the claim atomic — whatever tree the caller
    verifies is exactly the tree it then deletes, restores, or quarantines.
    The claim name is dot-prefixed so a crash mid-resolution leaves the data
    hidden from skill discovery but intact on disk. Returns None when the
    claim itself fails; the caller must then leave the destination untouched.
    """
    claim = dest_dir.with_name(f".{dest_dir.name}.sync-claim")
    counter = 2
    while os.path.lexists(claim):
        claim = dest_dir.with_name(f".{dest_dir.name}.sync-claim.{counter}")
        counter += 1
    try:
        os.replace(dest_dir, claim)
    except OSError:
        logger.warning(
            "could not claim skill dir %s for replacement; leaving it untouched",
            dest_dir,
            exc_info=True,
        )
        return None
    return claim


def _manifest_is_newer(src_file: Path, dest_file: Path) -> bool:
    """Whether the packaged manifest is newer than the installed one.

    Both stats are guarded because this runs on the gateway's startup path while
    another process (the CLI syncing the same home) may be claiming the very
    destination being measured. The two outcomes are deliberately different:

    * an unreadable DESTINATION means it vanished or was claimed mid-sync, so
      installing the packaged version is the correct answer -- update-due;
    * an unreadable SOURCE means the package itself cannot be read, and there is
      nothing to install from, so the destination is left alone.

    Raising instead would abort the whole sync for every remaining skill, which
    is what an unguarded ``stat`` did once the tree walks widened the window
    between the destination check and this comparison.
    """
    try:
        dest_mtime = dest_file.stat().st_mtime
    except OSError:
        return True
    try:
        return src_file.stat().st_mtime > dest_mtime
    except OSError:
        return False


def _tree_newest_mtime(root: Path) -> float | None:
    """Newest mtime of any regular file in *root*, or None when unprovable.

    The update gate needs to know whether a PACKAGED skill changed at all, not
    whether its ``SKILL.md`` did: a skill directory ships scripts, profiles and
    references alongside the manifest, and those are the files that carry the
    behaviour. Walking for the newest mtime is what makes a script-only release
    visible to the gate.

    The provenance marker is excluded for the same reason
    ``_tree_entries`` excludes it: the sync writes it AFTER copying, so its
    mtime is install time and would dominate every destination tree, making a
    later package update read as older than the copy it should replace — the
    gate would then never fire again.

    Returns None when the tree cannot be measured: an unreadable entry, or more
    entries than ``_FINGERPRINT_MAX_ENTRIES``. None is not a comparable value,
    so the caller falls back to the manifest comparison rather than guessing.
    """
    newest: float | None = None
    entries = 0
    for rel, kind, _value in _tree_entries(root):
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return None
        if kind == "unreadable":
            return None
        if kind != "file":
            continue
        try:
            mtime = os.lstat(root / rel).st_mtime
        except OSError:
            return None
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _tree_has_content(root: Path) -> bool:
    """True when the tree holds anything worth preserving.

    Only a COMPLETELY empty directory (zero entries — e.g. the placeholder an
    app registration leaves behind) counts as content-free; quarantining those
    would only mint junk backups on every update cycle. Any entry at all —
    files, links, specials, unreadable entries, and nested subdirectories,
    whose structure is itself user-made data — counts as content.
    """
    return any(True for _entry in _tree_entries(root))


def _finalize_user_backup(claim: Path, dest_dir: Path) -> Path | None:
    """Move a claimed, diverged tree to its ``.<name>.user-backup`` quarantine.

    Follows the collision behavior of the ``SKILL.md.pre-relocation``
    quarantine below: never overwrite an existing quarantine (``lexists``, so a
    dangling symlink also counts as occupied), pick the first unused numbered
    suffix.

    The quarantine name is ALWAYS dot-prefixed: dot-entries are pruned from
    skill discovery, so the single rename both preserves and deactivates the
    tree. Nothing inside the moved tree is ever touched afterwards — an
    earlier revision renamed ``backup / "SKILL.md"`` post-move, but that
    resolves a path THROUGH the backup directory, and a concurrent writer
    swapping the backup for a symlink between the two steps would redirect
    the rename into the symlink's target tree, outside the skills directory.
    One atomic rename of the claim itself has no such window, and a claim
    that is itself a link or junction is equally safe: the rename moves the
    link object, never its target.
    """
    stem = f".{dest_dir.name}.user-backup"
    backup = dest_dir.with_name(stem)
    counter = 2
    while os.path.lexists(backup):
        backup = dest_dir.with_name(f"{stem}.{counter}")
        counter += 1
    try:
        os.replace(claim, backup)
    except OSError:
        logger.warning(
            "could not move quarantined skill dir %s to %s; data preserved at " "the claim path",
            claim,
            backup,
            exc_info=True,
        )
        return None
    return backup


def _remove_ignorable_dir(path: Path) -> bool:
    """Remove a directory holding nothing worth preserving, race-free.

    The only ignorable content is the provenance marker this sync wrote
    (``_tree_entries`` excludes it, so ``_tree_has_content`` reports such a
    directory content-free). A marker file is only ignorable when it VERIFIES:
    its recorded fingerprint must parse and match the tree it sits in. A
    user-made file that merely shares the marker name (a marker-only name
    collision) fails that check — it is user bytes, so this returns False and
    the caller quarantines the tree instead of deleting anything. The rmdir
    is kernel-atomic: it succeeds only if the directory is STILL empty at
    unlink time, so a file created through a lingering directory handle after
    the emptiness check makes this return False instead of being lost.
    Callers must preserve the tree on False.
    """
    marker = path / _PROVENANCE_MARKER
    try:
        if os.path.lexists(marker):
            recorded = _recorded_fingerprint(path)
            if recorded is None or recorded != _skill_tree_fingerprint(path):
                return False
            marker.unlink()
        os.rmdir(path)
    except OSError:
        return False
    return True


def _dispose_superseded_slot(slot: Path, dest_dir: Path) -> bool:
    """Free the retirement slot name, deleting only what is re-verified.

    The occupant is CLAIMED first (atomic rename), so the tree that gets
    re-verified is exactly the tree that gets deleted — without the claim,
    a concurrent sync could park a fresh copy at the slot between this
    process's verification and its rmtree and have it destroyed unverified
    (this file's other destructive paths all follow the same claim-first
    invariant, see ``_claim_dir_for_replacement``). An occupant that fails
    re-verification carries bytes that landed after it was parked — the
    exact data the retirement exists to protect — and is preserved as a
    user backup instead of deleted.

    Returns True when the slot name is free afterwards. Every failure path
    keeps the occupant's bytes on disk (hidden at a dot-prefixed name at
    worst).
    """
    if not os.path.lexists(slot):
        return True
    slot_claim = _claim_dir_for_replacement(slot)
    if slot_claim is None:
        return False
    if is_link_or_junction(slot_claim):
        # A link at the slot name is user-made; preserve without following.
        _finalize_user_backup(slot_claim, dest_dir)
    elif not _tree_has_content(slot_claim):
        if not _remove_ignorable_dir(slot_claim):
            _finalize_user_backup(slot_claim, dest_dir)
    elif _verified_unchanged_fingerprint(slot_claim, None) is not None:
        if not rmtree_force(slot_claim):
            logger.warning(
                "could not remove epoch-old superseded skill copy %s; " "preserving what remains",
                slot_claim,
            )
            _finalize_user_backup(slot_claim, dest_dir)
    else:
        # Diverged since it was parked: late writes are user data.
        backup = _finalize_user_backup(slot_claim, dest_dir)
        logger.warning(
            "superseded skill copy %s changed after it was parked; " "preserved it at %s",
            slot,
            backup if backup is not None else slot_claim,
        )
    # The claim rename itself freed the slot name; whatever became of the
    # claimed occupant, its bytes are still on disk unless re-verified.
    return True


def _retire_verified_claim(claim: Path, dest_dir: Path, verified_fingerprint: str | None) -> bool:
    """Park a verified-unchanged claim at the hidden per-name retirement slot.

    Deleting a verified claim immediately would still lose bytes written
    through file descriptors that survived the claim rename: the fingerprint
    ran before those writes landed, so verification cannot see them. Instead
    the claim is parked at ``.<name>.superseded`` for one full sync cycle,
    and only the slot's PREVIOUS occupant — quiescent since the last update —
    is ever deleted, after being claimed and re-verified (see
    ``_dispose_superseded_slot``). A late write that landed in the meantime
    makes that re-check fail and the occupant is preserved as a user backup
    instead of deleted. Retention is bounded by construction: at most one
    hidden superseded copy per skill name; update-path slots rotate on the
    next update, and the stale-cleanup pass disposes of its slots on the
    following sweep.

    Returns True when the claim ended up parked; False when the slot could
    not be freed or the park itself failed, in which case the caller must
    preserve the claim rather than delete it.
    """
    slot = dest_dir.with_name(f".{dest_dir.name}.superseded")
    if not _dispose_superseded_slot(slot, dest_dir):
        return False
    try:
        os.replace(claim, slot)
    except OSError:
        logger.warning(
            "could not park verified skill copy %s at %s",
            claim,
            slot,
            exc_info=True,
        )
        return False
    # The parked tree must be re-verifiable next cycle. A claim proven by the
    # first-install migration rule (matches the packaged tree, no marker yet)
    # carries no marker of its own, so record the verified fingerprint now;
    # the marker file itself is excluded from fingerprints, so writing it
    # does not diverge the tree.
    if verified_fingerprint is not None and _recorded_fingerprint(slot) is None:
        _write_provenance_marker(slot, verified_fingerprint)
    return True


def _ensure_builtin_skills(base: Path) -> None:
    """Sync built-in skills: copy new/updated, remove known-stale ones.

    Supports nested directories (e.g. ``utils/tiny-url/SKILL.md``).
    Copies the entire skill directory (scripts, assets, etc.), not just SKILL.md.

    Destruction is provenance-gated: a destination directory is only ever
    removed (or replaced) when it is verifiably an unchanged copy this sync
    installed (see ``_verified_unchanged_fingerprint``), and it is atomically
    claimed before verification so the tree that gets verified is the tree
    that gets destroyed. Anything else — a user skill whose name collides with
    a builtin, a user-edited installed builtin, or a destination carrying
    user-added files — is preserved: moved aside to a ``<name>.user-backup``
    quarantine on update, or left alone entirely in the stale-cleanup pass.

    Cost note: the gateway runs this in a worker thread (``asyncio.to_thread``
    around ``SkillsLoader()``), and all verification work is bounded anyway:
    the steady state (marker present, no update due) costs one small marker
    read per skill; unmarked diverged directories cost a stat-level walk that
    stops at the first mismatch; content hashing only runs on trees whose stat
    manifest already matches a packaged skill, capped at
    ``_FINGERPRINT_MAX_BYTES`` / ``_FINGERPRINT_MAX_ENTRIES``.
    """
    source_names: set[str] = set()
    supplied: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            source_names.add(name)
            # First source root to ship a name owns it for this run. Without
            # this, the second root races the copy the first just made: the
            # destination is this run's own output rather than user data, and
            # which tree ends up installed is decided by comparing mtimes
            # across two unrelated source trees. The project dir is iterated
            # first, so a project skill is not replaced by a packaged
            # one that merely carries a newer file.
            if name in supplied:
                continue
            supplied.add(name)
            src_dir = src_file.parent
            dest_dir = base / name
            dest_file = dest_dir / "SKILL.md"
            # The manifest's own mtime is not a proxy for the skill's: a
            # release that only changes ``scripts/`` leaves ``SKILL.md``
            # byte-identical with its packaged mtime, so a manifest-only
            # comparison reports "up to date" and the installed skill keeps
            # running superseded code indefinitely. Observed on prepare-pr,
            # whose extractor was fixed in the package while every install
            # kept the previous copy and failed against the current workflow.
            #
            # Both arms are kept, OR-ed: the tree arm adds the updates the
            # manifest arm cannot see, and the manifest arm still governs when
            # the tree is unmeasurable or when a locally edited destination
            # carries an mtime newer than anything the package ships. Since
            # ``copytree`` copies with ``copy2``, an unmodified install
            # fingerprints mtime-equal to its package, so a steady state does
            # not re-copy on every startup.
            update_due = not dest_file.exists()
            if not update_due:
                src_newest = _tree_newest_mtime(src_dir)
                dest_newest = _tree_newest_mtime(dest_dir)
                update_due = (
                    src_newest is not None and dest_newest is not None and src_newest > dest_newest
                ) or _manifest_is_newer(src_file, dest_file)
            if not update_due:
                # First-install migration adoption: an up-to-date destination
                # with no marker is from a pre-provenance install. Record
                # ownership NOW, while the installed package still matches it —
                # waiting until the next content update would find the trees
                # differing (new version vs old copy) and wrongly quarantine an
                # untouched builtin. The verified fingerprint is recorded
                # as-is rather than re-scanned, so files added concurrently
                # after the comparison can never be blessed as builtin-owned.
                if dest_dir.exists() and _recorded_fingerprint(dest_dir) is None:
                    adopted = _verified_unchanged_fingerprint(dest_dir, src_dir)
                    if adopted is not None:
                        _write_provenance_marker(dest_dir, adopted)
                continue
            if dest_dir.exists() or is_link_or_junction(dest_dir):
                claim = _claim_dir_for_replacement(dest_dir)
                if claim is None:
                    continue
                verified: str | None = None
                if not is_link_or_junction(claim):
                    verified = _verified_unchanged_fingerprint(claim, src_dir)
                if not is_link_or_junction(claim) and not _tree_has_content(claim):
                    # A placeholder holding nothing but (at most) our own
                    # provenance marker has no user bytes to preserve; the
                    # kernel-atomic rmdir inside fails — and the tree is
                    # preserved instead — if anything landed after the check.
                    if not _remove_ignorable_dir(claim):
                        backup = _finalize_user_backup(claim, dest_dir)
                        logger.warning(
                            "placeholder skill dir %s gained content before "
                            "removal; preserved it at %s",
                            dest_dir,
                            backup if backup is not None else claim,
                        )
                elif verified is not None:
                    if not _retire_verified_claim(claim, dest_dir, verified):
                        # The retirement slot was unusable: preserve the
                        # verified copy rather than delete it. Installing the
                        # packaged version is still correct either way.
                        backup = _finalize_user_backup(claim, dest_dir)
                        logger.warning(
                            "could not retire verified skill copy of %s; " "preserved it at %s",
                            dest_dir,
                            backup if backup is not None else claim,
                        )
                else:
                    backup = _finalize_user_backup(claim, dest_dir)
                    # A failed finalize leaves the data at the dot-prefixed
                    # claim path (hidden but intact); installing the packaged
                    # version is still correct either way.
                    logger.warning(
                        "Skill directory %s does not match the copy this sync "
                        "installed (user-authored or locally edited); preserved "
                        "it at %s before installing the packaged version",
                        dest_dir,
                        backup if backup is not None else claim,
                    )
            # Fingerprint the PACKAGED tree (immutable while this runs) and
            # record that as the installed state: fingerprinting the freshly
            # copied destination instead would bless any user write that lands
            # during the hash as sync-owned, licensing its later deletion. The
            # copy equals the source (the package ships only regular files and
            # directories), so the source fingerprint is the copy's.
            src_fingerprint = _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True)
            try:
                shutil.copytree(src_dir, dest_dir)
            except FileExistsError:
                # Another process (gateway + CLI syncing the same home) won
                # the install race after our claim; its copy of the same
                # packaged skill is the destination now. Losing must not
                # crash the sync.
                logger.info("Skill %s installed concurrently elsewhere; keeping it", name)
                continue
            # copytree preserves source modes verbatim, so a read-only
            # install source (0o555 -- a Nix store path, a read-only mount)
            # yields a copy whose directories reject the provenance-marker
            # write below. Add owner rwx: file creation needs a writable
            # and searchable parent (including 0o455-class sources). The
            # recorded source fingerprint above is computed with
            # ``assume_owner_rwx_dirs=True``, i.e. it describes the
            # copy AS IT EXISTS AFTER this repair, so a clean install does
            # not read as a user customization on the next sync -- while any
            # later chmod on the installed copy (including removing
            # any owner-rwx bit) still diverges.
            ensure_owner_rwx_dirs(dest_dir)
            if src_fingerprint is not None:
                _write_provenance_marker(dest_dir, src_fingerprint)
            else:
                logger.warning(
                    "packaged skill tree %s cannot be fingerprinted; installed "
                    "%s without provenance",
                    src_dir,
                    name,
                )
            logger.info("Synced skill: %s", name)

    # Remove known stale builtin skills (replaced by MCP tools). A name a
    # source STILL ships (e.g. a project-level skill named ``cron``) is not
    # stale: sweeping it would delete on every startup what the loop above
    # just installed. Removal is provenance-gated by the same rule as updates:
    # only an unchanged copy this sync verifiably installed may be deleted by
    # name. A directory with no recorded provenance is user-authored by
    # assumption (a user skill named ``cron`` must survive every startup) and
    # is left alone — its removal, if ever wanted, is a human decision.
    # Deliberate consequence: installs that predate provenance recording keep
    # their stale builtin dirs until a human removes them, because there is no
    # packaged tree left to prove ownership against.
    stale_builtins = {"learn", "subagent", "cron", "kirocrew-core"} - source_names
    if base.exists():
        for name in stale_builtins:
            stale = base / name
            # Unlike update-path slots (rotated by the next update), nothing
            # ever ships for a stale name again, so its parked copy is
            # disposed of here on the sweep AFTER the one that parked it —
            # that is its full quiescent cycle. Ordered before the live-dir
            # handling below, which can park a fresh copy this same run.
            slot = base / f".{name}.superseded"
            if not stale.is_dir() and os.path.lexists(slot):
                _dispose_superseded_slot(slot, stale)
            if is_link_or_junction(stale):
                # The sync only ever creates real directories; a link here is
                # user-made and its target must not even be read.
                logger.debug("Leaving link %s in place: user-made", stale)
                continue
            if not stale.is_dir():
                continue
            if _recorded_fingerprint(stale) is None:
                logger.debug(
                    "Leaving %s in place: no recorded provenance, so treated as " "user-authored",
                    stale,
                )
                continue
            claim = _claim_dir_for_replacement(stale)
            if claim is None:
                continue
            retired = False
            stale_fp = _verified_unchanged_fingerprint(claim, None)
            if stale_fp is not None:
                retired = _retire_verified_claim(claim, stale, stale_fp)
                if retired:
                    logger.info("Retired stale builtin skill: %s", name)
            if not retired:
                # Diverged since the marker was recorded (user data), or the
                # retirement slot was unusable: restore the tree to its
                # original name; on failure it stays hidden but intact at the
                # claim path.
                try:
                    os.replace(claim, stale)
                except OSError:
                    logger.warning(
                        "could not restore %s from claim %s; data preserved " "there",
                        stale,
                        claim,
                        exc_info=True,
                    )
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
                        quarantine = old_skill_md.with_name(f"SKILL.md.pre-relocation.{counter}")
                        counter += 1
                    os.replace(old_skill_md, quarantine)
                    logger.info(
                        "Skill %s relocated to %s; flat copy quarantined at %s "
                        "(preserved on disk, no longer loaded)",
                        old_name,
                        new_name,
                        quarantine,
                    )
                except OSError:
                    logger.warning(
                        "could not quarantine relocated skill's flat copy %s",
                        old_skill_md,
                        exc_info=True,
                    )


#: SHA-256 of the static outputs from the retired conductor skill generator.
#: Exact identity keeps user-authored or edited files outside cleanup scope.
RETIRED_CONDUCTOR_SKILL_SHA256 = frozenset(
    {
        # select_crew-era text (crew triggers, `spawn_run(crew=...)` guidance)
        "ee91da7d58b89ddc4cd3ff097a87f520335193d78cb5937d7636e5baa9ee6ca5",
        # the first select_crew revision, before the crew= vs agent= warning
        "e967c693613dca258f66992b9788a8e5e8e12c4397147f58f6595ee42ffa21be",
    }
)
_RETIRED_CONDUCTOR_SKILL_MAX_BYTES = 16 * 1024


def is_retired_conductor_skill(data: bytes) -> bool:
    """Return whether *data* is a generated conductor skill revision.

    CRLF output from Windows is normalized to the generator's LF form before
    hashing. Bare carriage returns stay significant, as the generator emits none.
    """
    normalized = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest() in RETIRED_CONDUCTOR_SKILL_SHA256


def skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def remove_retired_conductor_skill() -> bool:
    """Remove a byte-exact generated conductor skill through pinned descriptors.

    Return ``True`` only when the skill file is removed. Missing, user-authored,
    and edited files return ``False``. Read and unlink errors propagate so each
    caller can report them without blocking setup or gateway startup; an empty-dir
    prune failure is ignored. A linked conductor directory is refused before any
    file is read.

    The conductor directory is opened relative to the pinned skills-root
    descriptor with ``O_NOFOLLOW``, so a link swapped in at that name raises
    ``OSError`` and propagates to the caller instead of being followed.

    Platforms without descriptor-relative opens keep the no-link final-name and
    bounded-read checks, but ancestor pinning and atomic identity-checked unlink
    degrade to by-name checks around the open and unlink.
    """
    skill_path = skills_dir() / "conductor" / "SKILL.md"
    parent = skill_path.parent
    parent_info = pinned_fs.lstat_by_name(parent)
    if (
        parent_info is None
        or not stat.S_ISDIR(parent_info.st_mode)
        or pinned_fs.is_reparse_point(parent)
    ):
        return False

    if not pinned_fs.supports_pinned_walk():
        before = pinned_fs.lstat_by_name(skill_path)
        if (
            before is None
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
        ):
            return False
        fd = os.open(skill_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
                or (
                    before.st_ino
                    and opened.st_ino
                    and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                )
            ):
                return False
            data = os.read(fd, _RETIRED_CONDUCTOR_SKILL_MAX_BYTES)
            if os.fstat(fd).st_size != len(data):
                return False
        finally:
            os.close(fd)
        if not is_retired_conductor_skill(data):
            return False
        current = pinned_fs.lstat_by_name(skill_path)
        if (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (
                opened.st_ino
                and current.st_ino
                and (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            )
        ):
            return False
        skill_path.unlink()
        try:
            if not os.listdir(parent):
                parent.rmdir()
        except OSError:
            pass
        return True

    root_fd = pinned_fs.pin_parent(
        os.path.realpath(parent.parent),
        what="retired conductor skill directory",
        refusal=OSError,
    )
    dir_fd: int | None = None
    try:
        current_parent = pinned_fs.stat_at(root_fd, parent.name)
        if (
            current_parent is None
            or not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_info.st_dev, parent_info.st_ino)
        ):
            return False
        dir_fd = os.open(parent.name, pinned_fs.dir_flags(), dir_fd=root_fd)
        pinned_parent = os.fstat(dir_fd)
        parent_identity = (pinned_parent.st_dev, pinned_parent.st_ino)
        if parent_identity != (current_parent.st_dev, current_parent.st_ino):
            return False
        before = pinned_fs.stat_at(dir_fd, skill_path.name)
        if (
            before is None
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
        ):
            return False
        fd = os.open(
            skill_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
        try:
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
            ):
                return False
            data = os.read(fd, _RETIRED_CONDUCTOR_SKILL_MAX_BYTES)
            if os.fstat(fd).st_size != len(data):
                return False
        finally:
            os.close(fd)
        if not is_retired_conductor_skill(data):
            return False
        if not pinned_fs.unlink_verified(dir_fd, skill_path.name, identity):
            return False
        try:
            if not os.listdir(dir_fd):
                pinned_fs.remove_dir_verified(
                    root_fd,
                    parent.name,
                    expect=parent_identity,
                )
        except OSError:
            pass
        return True
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        os.close(root_fd)


class _ScopedSkillEntry(NamedTuple):
    """Admission provenance travels with the entry, independently of its key."""

    key: str
    path: Path
    project_root: str | None
    mapping_root: str | None = None


def _fingerprint_mtime_and_size(fingerprint: str) -> tuple[float | None, int]:
    """Recover ``(mtime, size)`` from a ``dev:ino:ctime_ns:mtime_ns:size`` string.

    Lets a catalog read reuse the stat the WALK already paid instead of taking its
    own. ``(None, 0)`` for anything that does not parse, which sends the caller
    down the ordinary stat path rather than serving an invented size — a wrong
    figure here is reported to the user as a skill's injection cost.
    """
    parts = fingerprint.split(":")
    if len(parts) != 5:
        return None, 0
    try:
        return int(parts[3]) / 1_000_000_000, int(parts[4])
    except ValueError:
        return None, 0


class PendingApprovalRefused(Exception):
    """A pending-candidate approval was refused, with a machine-readable reason.

    ``reason`` is one of: ``not_found`` (no such candidate), ``live_exists``
    (a live skill already holds the name), ``kind_mismatch`` (an update
    candidate reached the new-skill approve variant, which would promote it
    fresh while its live target stays unchanged), ``script_validation_failed``
    (``report`` carries the redacted ``{filename: [findings]}`` map from
    ``validate_scripts``), ``target_missing`` (an update candidate whose live
    target is gone), ``stale_base`` (an update merged against an older live
    version), ``invalid_layout`` (symlink / unexpected candidate entry),
    ``redaction_failed``, or ``promotion_failed`` (an OS-level I/O failure —
    a read or write — after all checks passed). Raised by the ``*_checked`` approve variants so
    the dashboard can tell the user WHY the click did nothing; the legacy
    ``approve_pending_skill`` / ``approve_pending_update`` wrappers keep the
    ``None``-on-failure contract for existing callers.
    """

    def __init__(self, reason: str, report: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.report = report


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
            # Never sync on a running event loop: the sync verifies user-owned
            # trees (stat walks, capped content hashing) before it may replace
            # them, so a loader built inside a dashboard/Slack handler would
            # stall the loop and the liveness heartbeat. The gateway already
            # syncs at startup in a worker thread; on-loop constructions just
            # read the already-synced tree.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                _ensure_builtin_skills(self._dir)
            else:
                logger.debug(
                    "Skipping builtin-skill sync on a running event loop; "
                    "gateway startup owns the sync"
                )
        # Cache: path → (mtime or confined-content digest, parsed_frontmatter).
        self._fm_cache: dict[str, tuple[float | bytes, dict[str, str]]] = {}
        # TTL cache of the discovered (name, path) list — avoids an os.walk per
        # message in get_triggered_skills. Keyed by canonical project directory
        # ("" when no project, or the project's skills are not trusted): a
        # trusted project contributes its own skills root, so a single shared
        # slot would serve one session's project skills to a session working in
        # a different project for the whole TTL. (monotonic_deadline, results)
        self._iter_cache: dict[str, tuple[float, list[tuple[str, Path, str | None]]]] = {}
        # Stat fingerprints from the walk that produced each scope's list, so
        # `list_skills` can decide whether a persisted metadata row is still usable
        # WITHOUT re-stat'ing every skill. Keyed scope key → path → fingerprint;
        # an absent entry simply means "stat it yourself".
        self._catalog_fingerprints: dict[str, dict[str, str]] = {}
        # Scope keys whose served list is known to be partial, because the first
        # build outran `_COLD_CATALOG_WAIT_SECS`. Read by `catalog_status` so a
        # search, a list or a directory build can say "still discovering" instead
        # of reporting a truncated answer as the whole truth.
        self._catalog_incomplete: set[str] = set()
        # Unconfined paths adopted from the STORED snapshot that this process has not
        # itself admitted. The index is an agent-writable crew-home leaf, so a stored
        # row is not evidence anything vetted the path it names;
        # `_read_enumerated_skill_bytes` re-runs `validate_file_path` on a path in
        # here before its first read. A walk that republishes a scope clears it.
        self._snapshot_unadmitted: set[str] = set()
        # Single-flight background builds: scope key → the event its build sets on
        # completion. Concurrent sessions sharing this loader join one walk rather
        # than each walking the same tree.
        self._catalog_refreshes: dict[str, tuple[threading.Event, int]] = {}
        # Scope keys queued for the refresh worker, with the generation current when
        # each was queued. One worker drains them, so N scopes cost N SERIAL walks
        # rather than N concurrent ones — a session count must not multiply the
        # filesystem work a shared corpus costs.
        self._catalog_pending: dict[str, int] = {}
        self._catalog_wakeup = threading.Event()
        self._catalog_worker: threading.Thread | None = None
        # True while a build holds the search-index handle. `close()` then leaves
        # shutting the index down to that build, so its store still lands: a host
        # served only by short-lived loaders converges on nothing else.
        self._catalog_building = False
        # Guards the four structures above AND `_iter_cache`: background builds
        # publish into them from a worker thread while foreground callers read.
        self._catalog_lock = threading.Lock()
        # Bumped by every in-process invalidation. A build that started before the
        # bump is publishing an answer that predates a change already known, so its
        # result is dropped rather than allowed to overwrite the newer state. The
        # index's own epoch covers the same race BETWEEN processes.
        self._catalog_generation = 0
        self._closed = False
        self._disabled_apps_cache: tuple[float, frozenset[str]] | None = None
        # (canonical key, allowed) pairs already audited, so the enforcement
        # record is written on first use rather than once per message.
        self._audited_projects: set[tuple[str, bool]] = set()
        # Extra skill paths from config (config injectable for testing)
        cfg = config or KiroCrewConfig.load()
        # The per-message trigger cap is resolved at USE from the live snapshot
        # (see _max_triggered_now), so `kirocrew config set skills.max_triggered`
        # applies to the next message without rebuilding the loader. The
        # construction-time value stays as the fallback for a loader built from an
        # explicitly injected config, before any snapshot exists.
        self._max_triggered = cfg.skills.max_triggered
        self._extra_paths: list[Path] = []
        self._configured_extra_paths: list[Path] = []
        for p in cfg.skills.extra_paths:
            resolved = Path(p).expanduser().resolve()
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive extra skill path: %s", p)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
                self._configured_extra_paths.append(resolved)
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
        self._edition_extra_paths: list[Path] = []
        for edition_path in edition_skill_paths:
            resolved = Path(edition_path).expanduser().resolve()
            if resolved in self._extra_paths:
                continue
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive edition skill path: %s", edition_path)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
                self._edition_extra_paths.append(resolved)
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
        # Term index behind `search_skills`'s body fallback, in the same home as
        # the usage ledger. Best-effort for the same reason: an unusable index
        # only costs the search its old per-file read path.
        self._search_index: SkillSearchIndex | None
        try:
            self._search_index = SkillSearchIndex(self._dir.parent / SKILL_SEARCH_INDEX_FILENAME)
        except Exception:  # pragma: no cover — index is best-effort
            logger.warning(
                "skill-search-index: init failed; search reads bodies from disk",
                exc_info=True,
            )
            self._search_index = None
        # `skills.extra_paths` is a set of source ROOTS, so it is pushed rather than
        # read at use: re-resolving every root (a realpath plus a sensitivity check
        # per entry) on every message is exactly the cost the iter-cache exists to
        # avoid. `max_triggered` is a single int and IS read at use, so it needs no
        # subscription. Held on self because the watcher keeps a bound method weakly.
        self._config_sub = live.subscribe(
            "skills.extra_paths", callback=self._on_config_change, name="SkillsLoader"
        )

    def close(self) -> None:
        """Release persistent resources owned by this loader.

        ``_closed`` is set FIRST, so a build already on the worker publishes
        nothing into a loader that is going away, and so no new build can be queued
        behind this call. The worker is a DAEMON thread and is not joined: a cold
        first walk can outlive the loader that triggered it, and the unsigned MCP
        fallback closes its loader as soon as one search returns, so joining here
        would charge that call the very walk this design moved off the request path.

        A build still in flight KEEPS the index handle, and closes it itself when it
        finishes. Closing it here instead would discard that walk's store, and on a
        host served only by short-lived loaders the store is the one thing that lets
        the next call skip the walk — so discarding it makes every call re-walk
        forever. Publishing into this loader's memory is still refused; only the
        persistence survives.

        Every outstanding completion event is SET, because a queued build the
        worker abandons never reaches the ``finally`` that would have set it — and a
        cold caller waiting on that event would otherwise sit out its whole budget
        for an answer that will never arrive.
        """
        with self._catalog_lock:
            self._closed = True
            self._catalog_pending.clear()
            pending_events = [event for event, _gen in self._catalog_refreshes.values()]
            self._catalog_refreshes.clear()
            # A build in flight owns the handle until it returns; it closes the
            # index on its own way out.
            index = None if self._catalog_building else self._search_index
        for event in pending_events:
            event.set()
        self._catalog_wakeup.set()
        if index is not None:
            index.close()

    async def _on_config_change(self, change: "live.ConfigChange") -> None:
        # Both halves run off-loop. The screening stats every configured root
        # (resolve + is_dir), and a root on a slow or network mount would stall the
        # loop; the adoption then invalidates the persisted catalog, which takes
        # SQLite's write lock and can wait out the busy timeout when another process
        # holds it. Neither belongs on the gateway's event loop.
        screened = await asyncio.to_thread(self._screen_extra_paths, change.new)
        await asyncio.to_thread(self._adopt_extra_paths, screened)

    def reconfigure(self, cfg: KiroCrewConfig) -> None:
        """Re-resolve the configured extra skill roots from *cfg* (synchronously).

        The watcher path splits this into :meth:`_screen_extra_paths` off the loop
        and :meth:`_adopt_extra_paths` on it; this method is the one-call form for
        a caller that is not on the event loop.
        """
        self._adopt_extra_paths(self._screen_extra_paths(cfg))

    @staticmethod
    def _screen_extra_paths(cfg: KiroCrewConfig) -> list[Path]:
        """Resolve and screen ``skills.extra_paths`` -- filesystem work, no state.

        Runs the SAME screening as construction -- expanduser, resolve,
        ``is_sensitive_path`` reject, existence check -- so a root added by hand to
        ``config.json`` can no more reach a credential directory than one present at
        boot. Fails closed per entry: a rejected or missing root is dropped with the
        same log line rather than admitted.
        """
        resolved_paths: list[Path] = []
        # Logged by position, not value: a rejected entry is by definition a path
        # under a credential home, and a reloaded config is an untrusted document,
        # so the string itself never reaches the log.
        for index, p in enumerate(cfg.skills.extra_paths):
            resolved = Path(p).expanduser().resolve()
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive skills.extra_paths[%d] on reload", index)
            elif resolved.is_dir():
                resolved_paths.append(resolved)
            else:
                logger.debug("skills.extra_paths[%d] does not exist; skipped on reload", index)
        return resolved_paths

    def _adopt_extra_paths(self, resolved_paths: list[Path]) -> None:
        """Install screened roots.

        Edition-contributed roots are preserved and stay LAST (lowest precedence);
        they come from the platform context, not config, so a config write must not
        drop them. A root-set change is a full catalog invalidation, not just a
        cleared list: the stored snapshot belongs to the OLD root set, and a walk
        already in flight over those roots must not publish under the new scope, so
        the persisted rows are dropped and the generation moved as well.
        """
        self._configured_extra_paths = resolved_paths
        merged = list(resolved_paths)
        for edition_path in self._edition_extra_paths:
            if edition_path not in merged:
                merged.append(edition_path)
        self._extra_paths = merged
        self._invalidate_iter_cache()

    def _max_triggered_now(self) -> int:
        """The per-message trigger cap, read live.

        Read from the watcher's snapshot rather than the boot copy, so
        ``kirocrew config set skills.max_triggered`` applies to the very next
        message from any writer. The snapshot is a plain attribute read, which is
        what keeps this off the disk on a path that runs once per message --
        loading here would put two stats and a deepcopy in front of every message.

        Falls back to the construction-time value when there is no snapshot: a
        loader built from an explicitly injected config (tests, and any caller that
        already holds one) must honour that config rather than resolve a cap the
        injected document never carried, and the absent-key default is 0, which
        would suppress every skill.
        """
        cfg = live.snapshot()
        if cfg is None:
            return self._max_triggered
        try:
            return int(cfg.skills.max_triggered)
        except (AttributeError, TypeError, ValueError):
            logger.debug("skills.max_triggered read failed; using boot value", exc_info=True)
            return self._max_triggered

    def _trusted_project_key(self, project_dir: str | Path | None) -> str:
        """Canonical key of *project_dir* when its skills may load, else ``""``.

        Folding the trust verdict into the cache key — rather than caching it
        alongside the results — is what makes a revoke take effect on the next
        message instead of after the TTL: withdrawing trust changes the key back
        to ``""``, which selects the project-free cache slot immediately.

        Costs one ``realpath`` plus one cached ``stat`` when a project is set,
        and nothing at all when it is not.
        """
        if project_dir is None:
            return ""
        key = skill_trust.canonical_key(project_dir)
        allowed = key is not None and skill_trust.is_key_trusted(key)
        self._audit_project_skill_enforcement(project_dir, key, allowed)
        if not allowed:
            return ""
        # `allowed` is only true when key is not None; assert for the type checker.
        assert key is not None
        return key

    def _audit_project_skill_enforcement(
        self, project_dir: str | Path, key: str | None, allowed: bool
    ) -> None:
        """Record the enforcement outcome once per directory per process.

        Grant and revoke are audited where the operator acts; this records where
        that authority is USED, so "what did this session load, and on whose
        say-so" is answerable from the log rather than inferred.

        Deliberately NOT per call. This runs on every message via
        ``get_triggered_skills``, and a per-message governance event would bury the
        events that matter while adding hot-path cost to every message.
        Keyed on (canonical key, outcome) so a new directory, or the
        same directory after the feature switch is flipped, is recorded again --
        a second message about an unchanged decision is not.

        ``critical=False``: this is a record, not an audit-or-deny gate. A chat
        turn must not die because the SEL is unwritable, and the authority being
        exercised was already written synchronously when consent was given.
        """
        marker = (key or str(project_dir), allowed)
        if marker in self._audited_projects:
            return
        try:
            sel().log_governance_decision(
                session_key="",
                tool_name="skills",
                scope="project_skills",
                item=key or str(project_dir),
                outcome="allowed" if allowed else "denied",
                rule="project_skills_trust_enforced",
                reason=(
                    "project skills admitted for a granted directory"
                    if allowed
                    else "project skills withheld: no grant, or the feature is off"
                ),
                critical=False,
            )
            self._audited_projects.add(marker)
        except Exception:  # noqa: BLE001 — an unwritable log must not fail a turn
            logger.warning("could not audit project-skills enforcement", exc_info=True)

    def _iter(self, project_dir: str | Path | None = None) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` triples without walking the tree.

        Local skills take precedence over extra paths, and both take precedence
        over a trusted project's own skills. Precedence is why enumeration ORDER is
        part of the answer and not an incidental detail of how it was produced.

        Three tiers, none of which walks on the calling thread:

        1. this loader's in-memory list, while it is inside
           ``_ITER_CACHE_TTL_SECS``;
        2. the same list past that deadline. Expiry SCHEDULES a re-walk; it never
           charges one to the caller that happened to arrive after it;
        3. the stored snapshot for this root set, adopted into tier 1. A restart,
           and every short-lived loader (the unsigned MCP fallback builds one per
           call), lands here rather than walking.

        Only a scope with no snapshot at all — a machine's first run, or one whose
        index file was deleted — waits, and then for at most
        ``_COLD_CATALOG_WAIT_SECS`` on a background build. Past that the partial
        answer is served with the scope marked incomplete, so a caller can say
        "still discovering" instead of "no skills"; see :meth:`catalog_status`.

        A served list says only which files EXIST. Mapping scope, disabled apps,
        project consent and ``repo_scope`` are all applied live on top of it, and
        every body read re-checks confinement on the descriptor it opened — so no
        snapshot, however stale, can widen what a session reaches.
        """
        key = self._catalog_scope_key(project_dir)
        now = time.monotonic()
        with self._catalog_lock:
            cached = self._iter_cache.get(key)
            stale = cached is not None and now >= cached[0]
        if cached is not None:
            if stale:
                self._request_catalog_refresh(key)
            return cached[1]

        snapshot = self._load_catalog_snapshot(key)
        if snapshot is not None:
            rows, built_at = snapshot
            self._adopt_catalog(key, rows, {}, complete=True)
            if time.time() - built_at >= _CATALOG_REVALIDATE_AFTER_SECS:
                self._request_catalog_refresh(key)
            return rows

        with self._catalog_lock:
            already_reported = key in self._catalog_incomplete
        if already_reported:
            # "Still discovering" has already been delivered for this scope, so
            # charging every later turn the same budget would buy nothing and break
            # the rule that a subsequent turn never waits.
            self._request_catalog_refresh(key)
            logger.debug("skill catalog: scope %r still building", key or "<global>")
            return []

        # Waiting on the WORKER rather than walking here is what bounds the cost:
        # the walk continues after the budget expires, so the wait buys a complete
        # answer when one is cheap and costs a fixed ceiling when it is not. The
        # loop is what makes the budget the only limit: a build can be FENCED by a
        # mutation that lands while it runs and then publishes nothing, and its
        # replacement is queued behind it — so waking on one build's completion is
        # not the same as the answer being ready.
        deadline = time.monotonic() + _COLD_CATALOG_WAIT_SECS
        while True:
            done = self._request_catalog_refresh(key)
            remaining = deadline - time.monotonic()
            if done is None or remaining <= 0:
                break
            done.wait(timeout=remaining)
            with self._catalog_lock:
                cached = self._iter_cache.get(key)
            if cached is not None:
                return cached[1]
        with self._catalog_lock:
            cached = self._iter_cache.get(key)
            if cached is not None:
                return cached[1]
            # The build is still running. Serve nothing, but RECORD that this is a
            # partial answer so no caller reports it as a complete "no skills".
            self._catalog_incomplete.add(key)
        logger.debug("skill catalog: first build of scope %r still running", key or "<global>")
        return []

    def _catalog_scope_key(self, project_dir: str | Path | None) -> str:
        """The scope this request reads, as a string the snapshot layer can key on.

        ``_trusted_project_key`` answers ``""`` for "no trusted project", but a
        falsy answer of any shape means the same thing, and the two must not select
        DIFFERENT scopes — one would then be served a snapshot the other built. So
        the coercion happens once, here, rather than at each of the three call
        sites that would otherwise each have to remember it.
        """
        return self._trusted_project_key(project_dir) or ""

    def catalog_status(self, project_dir: str | Path | None = None) -> str:
        """``"complete"`` or ``"building"`` for the scope *project_dir* selects.

        The distinction a caller cannot make from an empty result alone: a machine
        with no skills and a machine whose first discovery pass has not finished
        both enumerate to nothing. Search, list and directory callers use this to
        say which one it is rather than presenting a partial answer as the truth.
        """
        key = self._catalog_scope_key(project_dir)
        with self._catalog_lock:
            return "building" if key in self._catalog_incomplete else "complete"

    def _catalog_fingerprint_hint(self, project_dir: str | Path | None) -> dict[str, str]:
        """Stat fingerprints from the walk that produced this scope's list.

        Empty when this process has not walked the scope yet, which simply sends
        ``list_skills`` down its own stat path.
        """
        key = self._catalog_scope_key(project_dir)
        with self._catalog_lock:
            return self._catalog_fingerprints.get(key, {})

    def _catalog_scope_id(self, project_key: str) -> str:
        """Stable identity of the ROOT SET a stored snapshot belongs to.

        The project key alone is not enough: two loaders can share it and still
        enumerate different trees (a different skills dir under a test
        ``KIROCREW_HOME``, a different ``skills.extra_paths``). Keying on the roots
        as well is what stops one configuration from being served another's
        snapshot. It is also what makes a revoked project grant unreachable rather
        than merely unused: withdrawing trust turns the project key back into ``""``
        (see ``_trusted_project_key``), which selects a DIFFERENT scope, so the rows
        naming that project's files cannot be read from. Digested rather than
        concatenated so the key stays bounded and holds no path text for an
        unrelated reader of the index file.
        """
        material = "\x00".join(
            [str(self._dir), *(str(path) for path in self._extra_paths), project_key]
        )
        return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:32]

    def _snapshot_admitted_roots(self) -> tuple[str, ...]:
        """Roots an unconfined row read off disk may legitimately name.

        The walk admits an unconfined path one of two ways: it came out of a walk
        of this loader's own roots, or ``validate_file_path`` resolved it — which
        may land on an app provider's tree, since an app symlinks its skills into
        the skills dir and the resolved target sits outside it by construction. A
        row read back from the index has neither guarantee, so it is held to the
        union of both: this loader's roots plus the same provider roots the mapping
        walker admits. Lexical, so screening a whole snapshot costs no syscalls.
        """
        return (
            os.path.realpath(self._dir),
            *(os.path.realpath(path) for path in self._extra_paths),
            *_trusted_skill_roots(),
        )

    def _load_catalog_snapshot(
        self, project_key: str
    ) -> tuple[list[tuple[str, Path, str | None]], float] | None:
        """Read this scope's stored enumeration, or ``None`` when there is none.

        ``None`` and an empty row list are different answers: the second means this
        root set was walked and genuinely holds no skills, which must not be
        re-walked on every message just because the answer is nothing.

        No stat fingerprints come back with it, deliberately. They would record what
        a walk in some earlier process saw, and nothing here knows how long ago that
        was, so letting them stand in for metadata validation would make a new
        process serve a description for a file edited out of band since — the one
        thing a restart is expected to notice. A stored row may name a file; it may
        never vouch for its contents.

        The index file is an agent-WRITABLE crew-home leaf, so a stored row is not
        evidence that anything admitted the path it names. Two things therefore
        stand between a row and a read. Here, every unconfined row must name a path
        under :meth:`_snapshot_admitted_roots` — lexical, so a whole snapshot is
        screened without a syscall — and rows that fail are dropped rather than
        served. Then each surviving unconfined path is recorded as NOT YET ADMITTED,
        because containment alone does not say the file is still a regular file in a
        non-sensitive location; ``_read_enumerated_skill_bytes`` re-runs
        ``validate_file_path`` on it before the first read.
        """
        if self._search_index is None or self._closed:
            return None
        stored = self._search_index.catalog_snapshot(self._catalog_scope_id(project_key))
        if stored is None:
            return None
        rows_raw, built_at = stored
        admitted_roots = self._snapshot_admitted_roots()
        own_roots = (self._dir, *self._extra_paths)
        provider_roots = _trusted_skill_roots()
        rows: list[tuple[str, Path, str | None]] = []
        unadmitted: set[str] = set()
        for key, path, confine_root in rows_raw:
            absolute = os.path.abspath(path)
            if confine_root:
                # A confined row's root decides which directory its body is read
                # under, so taking the stored value on trust would let a forged row
                # name ANY project and have it read as a granted one. Only the
                # trusted project key that selected this scope is accepted, the path
                # must sit inside it, and the key must denote that path — the same
                # pair check the unconfined branch applies, for the same reason: the
                # mapping is matched on the path and the body delivered by the key.
                if (
                    not project_key
                    or confine_root != project_key
                    or not _within_any(absolute, (project_key,))
                    or os.path.abspath(Path(project_key) / ".kiro" / "skills" / key / _SKILL_FILE)
                    != absolute
                ):
                    logger.warning("skill catalog: refusing a stored row with a foreign root")
                    return None
                rows.append((key, Path(path), confine_root))
                continue
            if not _within_any(absolute, admitted_roots):
                logger.warning("skill catalog: refusing a stored row outside every root")
                return None
            if not self._key_denotes_path(key, absolute, own_roots, provider_roots):
                logger.warning("skill catalog: refusing a stored row whose key is not its path")
                return None
            rows.append((key, Path(path), None))
            unadmitted.add(str(Path(path)))
        with self._catalog_lock:
            self._snapshot_unadmitted |= unadmitted
        return rows, built_at

    def _admit_snapshot_path(self, path: Path) -> bool:
        """Re-run the walk's admission on an unconfined path read off disk.

        ``True`` — and free — for every path this process walked itself, which is
        the normal case: the set only ever holds rows adopted from the stored
        snapshot, and a walk that republishes the scope empties it. A path in the
        set is put through ``validate_file_path`` exactly once; admitting it retires
        it from the set so later reads cost nothing, and a refusal leaves it in so a
        second attempt is refused again rather than silently admitted.
        """
        key = str(path)
        with self._catalog_lock:
            if key not in self._snapshot_unadmitted:
                return True
        if validate_file_path(key) is None:
            logger.warning("Refusing a stored skill path that no longer admits: %s", path)
            return False
        with self._catalog_lock:
            self._snapshot_unadmitted.discard(key)
        return True

    @staticmethod
    def _key_denotes_path(
        key: str, absolute: str, own_roots: tuple[Path, ...], provider_roots: tuple[str, ...]
    ) -> bool:
        """Does *key* name the skill that *absolute* holds?

        A stored row carries the key and the path as two independent fields, and they
        are consumed by DIFFERENT gates: an agent mapping is matched against the
        PATH (``_matches_any``), while the body is delivered by re-resolving the KEY
        (``load_skill``). A row that pairs one skill's key with another's path
        therefore passes a mapping admitting the second and serves the first — so the
        pair has to be checked, not just each half.

        Two shapes a walk can legitimately produce, both decided lexically:

        * the path IS what the key denotes under one of this loader's own roots, which
          is what the global tree and an in-root ``extra_paths`` entry record;
        * the path is an admitted PROVIDER target — an app symlinks its skills into
          the tree, so ``validate_file_path`` resolves the row out of the root it was
          named in — and the key's last segment still matches the directory holding
          the file.

        Anything else is refused, and the caller refuses the whole snapshot with it: a
        dropped row would leave a truncated catalog being served as a complete one.
        """
        if not key:
            return False
        for root in own_roots:
            if os.path.abspath(root / key / _SKILL_FILE) == absolute:
                return True
        return _within_any(absolute, provider_roots) and (
            os.path.basename(os.path.dirname(absolute)) == key.rsplit("/", 1)[-1]
        )

    def _adopt_catalog(
        self,
        project_key: str,
        rows: list[tuple[str, Path, str | None]],
        fingerprints: dict[str, str],
        *,
        complete: bool,
    ) -> None:
        with self._catalog_lock:
            self._iter_cache[project_key] = (time.monotonic() + _ITER_CACHE_TTL_SECS, rows)
            if fingerprints:
                self._catalog_fingerprints[project_key] = fingerprints
            if complete:
                self._catalog_incomplete.discard(project_key)

    def _request_catalog_refresh(self, project_key: str) -> threading.Event | None:
        """Queue one background walk of *project_key*'s roots; join any in flight.

        Returns the event that build sets on completion, so the cold path can wait
        on it with a budget. ``None`` means no build could be started — a closed
        loader, or a host that refused the thread — and the caller must then live
        with what it already has rather than walking on the request path.
        """
        with self._catalog_lock:
            if self._closed:
                return None
            existing = self._catalog_refreshes.get(project_key)
            if existing is not None and not existing[0].is_set():
                if existing[1] != self._catalog_generation:
                    # The build in flight was queued before a mutation, so it is
                    # already fenced and will publish nothing. Queue the current
                    # generation behind it, or the caller waits out its budget on an
                    # answer that never lands and the next turn pays cold discovery.
                    self._catalog_pending[project_key] = self._catalog_generation
                    self._catalog_wakeup.set()
                return existing[0]
            done = threading.Event()
            self._catalog_refreshes[project_key] = (done, self._catalog_generation)
            self._catalog_pending[project_key] = self._catalog_generation
            if self._catalog_worker is None or not self._catalog_worker.is_alive():
                # Started on first NEED, not in __init__: a loader whose every call
                # is served from a snapshot never starts a thread, which is what
                # keeps the short-lived MCP fallback cheap. DAEMON because a build
                # is deliberately abandonable, and `concurrent.futures` joins its
                # non-daemon workers at interpreter exit — which would make a walk
                # this design moved off the request path delay process exit instead
                # of being dropped.
                try:
                    worker = threading.Thread(
                        target=copy_context().run,
                        args=(self._catalog_worker_loop,),
                        name="skill-catalog-refresh",
                        daemon=True,
                    )
                    worker.start()
                except RuntimeError:  # pragma: no cover — host refused a thread
                    logger.debug("skill catalog: refresh worker unavailable", exc_info=True)
                    self._catalog_refreshes.pop(project_key, None)
                    self._catalog_pending.pop(project_key, None)
                    return None
                self._catalog_worker = worker
        self._catalog_wakeup.set()
        return done

    def _catalog_worker_loop(self) -> None:
        """Drain queued scopes one at a time until this loader closes.

        Serial by construction: several sessions in different trusted projects all
        enumerate the same global tree, so running their builds concurrently would
        multiply that one corpus's filesystem work by the session count.
        """
        while True:
            with self._catalog_lock:
                if self._closed:
                    return
                if self._catalog_pending:
                    project_key, generation = self._catalog_pending.popitem()
                else:
                    # Cleared under the same lock the producer sets it under, and
                    # only after observing an empty queue, so a scope queued in
                    # between cannot lose its wakeup.
                    self._catalog_wakeup.clear()
                    project_key, generation = "", -1
            if generation < 0:
                self._catalog_wakeup.wait()
                continue
            self._run_catalog_build(project_key, generation)

    def _run_catalog_build(self, project_key: str, generation: int) -> None:
        """Walk *project_key*'s roots off the request path and publish the result.

        Failure is logged and dropped: a scope keeps serving whatever it had, which
        is strictly better than failing a turn over a tree that could not be read
        this once.
        """
        try:
            with self._catalog_lock:
                if self._closed:
                    return
                # The handle is READ under the same lock that claims it. Reading it
                # first and claiming second leaves a gap in which `close()` sees no
                # build in flight, closes the index, and latches it unusable — the
                # walk then finishes and persists nothing, which is the one thing the
                # handover exists to prevent.
                index = self._search_index
                self._catalog_building = True
            # Both captured BEFORE the walk. The epoch is the cross-process half of
            # the generation check: `store_catalog` refuses a write whose epoch has
            # moved. The scope is captured because `skills.extra_paths` can be
            # reconfigured while the walk runs, and a scope id computed afterwards
            # would file rows walked over the OLD root set under the NEW root set's
            # key — publishing a catalog that names neither configuration's tree.
            epoch = index.catalog_epoch() if index is not None else None
            scope_id = self._catalog_scope_id(project_key)
            rows = self._iter_uncached(project_key or None)
            fingerprints = self._catalog_fingerprints_for(rows)
            # PERSIST BEFORE PUBLISHING. The store is where a cross-process
            # invalidation is detected: `"stale"` means another process recorded a
            # mutation while this walk ran, so these rows must not be served either.
            # `"unavailable"` is the opposite case — the rows are fine and only the
            # database is missing (a read-only home), and refusing to serve them
            # would make every turn re-walk.
            outcome = (
                index.store_catalog(
                    scope_id,
                    [(name, str(path), within or "") for name, path, within in rows],
                    epoch=epoch,
                )
                if index is not None
                else "unavailable"
            )
            if outcome == "stale":
                logger.debug("skill catalog: another process invalidated mid-walk; dropping")
                return
            with self._catalog_lock:
                if self._closed or generation != self._catalog_generation:
                    # A mutation landed while this walk ran, so its answer predates
                    # a change already known. Publishing it would resurrect the
                    # pre-change list and undo the invalidation. The rows stay
                    # PERSISTED when only `_closed` stopped the publish: they
                    # describe the tree this walk saw, and a host served only by
                    # short-lived loaders converges on nothing else.
                    return
                if scope_id != self._catalog_scope_id(project_key):
                    logger.debug("skill catalog: root set changed mid-walk; dropping the result")
                    return
                self._iter_cache[project_key] = (time.monotonic() + _ITER_CACHE_TTL_SECS, rows)
                self._catalog_fingerprints[project_key] = fingerprints
                self._catalog_incomplete.discard(project_key)
                # Only the paths THIS walk returned are admitted. A forged row the
                # walk rejected keeps its marker, so a reader still holding the old
                # list cannot read it unadmitted.
                self._snapshot_unadmitted -= {
                    str(path) for _name, path, within in rows if within is None
                }
        except Exception:  # noqa: BLE001 — a failed walk must not kill the worker
            logger.warning("skill catalog: background walk failed", exc_info=True)
        finally:
            with self._catalog_lock:
                self._catalog_building = False
                entry = self._catalog_refreshes.pop(project_key, None)
                # The handle was held for this build; a close that arrived while it
                # ran deferred shutting it down to here.
                index_to_close = self._search_index if self._closed else None
            if entry is not None:
                entry[0].set()
            if index_to_close is not None:
                index_to_close.close()

    @staticmethod
    def _catalog_fingerprints_for(
        rows: list[tuple[str, Path, str | None]],
    ) -> dict[str, str]:
        """Stat each unconfined row once, so ``list_skills`` need not stat again.

        Confined project rows are deliberately absent: their cache token comes from
        bytes the no-link reader admitted, never from a path stat, and recording one
        here would reintroduce the probe that confinement exists to prevent.
        """
        fingerprints: dict[str, str] = {}
        for _name, path, within in rows:
            if within is not None:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            fingerprints[str(path)] = (
                f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}:{st.st_mtime_ns}:{st.st_size}"
            )
        return fingerprints

    def _get_disabled_app_names(self) -> frozenset[str]:
        now = time.monotonic()
        if self._disabled_apps_cache is not None and now < self._disabled_apps_cache[0]:
            return self._disabled_apps_cache[1]
        disabled = _disabled_app_names()
        self._disabled_apps_cache = (now + _ITER_CACHE_TTL_SECS, disabled)
        return disabled

    def _iter_visible(
        self, project_dir: str | Path | None = None
    ) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` pairs, filtering out disabled app skills."""
        disabled_apps = self._get_disabled_app_names()
        if not disabled_apps:
            return self._iter(project_dir)
        return [
            (name, skill_file, within)
            for name, skill_file, within in self._iter(project_dir)
            if self._owning_app(name, skill_file) not in disabled_apps
        ]

    def catalog_project_skills(self, project_dir: str | Path) -> list[dict]:
        """Return confined project rows without requiring or exercising trust.

        The consent picker must describe a project skill before the operator
        grants it. Project rows therefore cannot use the legacy Kiro workspace
        scanner, which resolves and reads link targets before the loader can
        reject them. This path enumerates through the loader's confined walker
        and reads each row through the descriptor-pinned no-link reader.
        """
        key = skill_trust.canonical_key(project_dir)
        if key is None:
            return []
        skills: list[dict] = []
        for name, skill_file, confined_root in self._iter_uncached(key):
            if confined_root != key:
                continue
            raw = self._read_enumerated_skill_bytes(
                skill_file, confined_root, max_bytes=PROJECT_SKILL_BODY_CAP
            )
            if raw is None:
                continue
            meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=False))
            description = self._redact_text(meta.get("description", name))
            repo_scope = self._redact_text(meta.get("repo_scope", ""))
            skills.append(
                {
                    "confine_root": confined_root,
                    "key": name,
                    # Preserve the open-standard catalog identity: its display
                    # name is the relative directory name, not a frontmatter
                    # alias that would expand to a different path.
                    "name": name,
                    "description": description,
                    "path": str(skill_file),
                    "dir": str(skill_file.parent),
                    "always": meta.get("always", "").strip().lower() == "true",
                    "repo_scope": repo_scope,
                    # Project paths cannot safely offer a live pointer to the
                    # agent, so report the effective forced-body behavior.
                    "inject_on_trigger": True,
                    "size_bytes": len(raw),
                    "deliveries": self._delivery_count(name),
                    "owned": False,
                }
            )
        return skills

    def _iter_uncached(self, project_key: str | None = None) -> list[tuple[str, Path, str | None]]:
        """Walk the skills dir, extra paths, and an already-canonical project root.

        This function performs no trust check of its own. The loading path passes
        a key confirmed by ``_trusted_project_key``; the catalog path uses it only
        to determine which confined names a later grant could admit. Callers must
        never pass a raw caller-supplied path.
        """
        # Unconfined (None): the global tree may legitimately hold app-registered
        # symlinks resolving into a provider root outside it.
        results: list[tuple[str, Path, str | None]] = [
            (name, path, None) for name, path in _iter_skill_files(self._dir)
        ]
        seen = {name for name, _, _ in results}
        # (root, confine_to): only the project root is confined — see
        # _iter_skill_files. Extra paths keep the provider-root allowance.
        roots: list[tuple[Path, tuple[str, ...] | None]] = [
            (extra, None) for extra in self._extra_paths
        ]
        if project_key:
            project_root = Path(project_key) / ".kiro" / "skills"
            # Appended LAST so a repository cannot shadow a same-named skill
            # the operator installed globally. The confined walker opens the
            # project root and every descendant component relative to no-follow
            # directory handles; no path probe occurs before that confinement.
            roots.append((project_root, (project_key,)))
        for root, confine in roots:
            for name, skill_file in _iter_skill_files(root, confine_to=confine):
                if name in seen:
                    continue
                if confine is not None:
                    # The descriptor-anchored walker already admitted this
                    # lexical name. Resolving it here would reintroduce the
                    # link-swap/UNC probe the walk exists to prevent. Reads are
                    # re-confined at their own descriptor-pinned choke point.
                    results.append((name, skill_file, confine[0]))
                    seen.add(name)
                    continue
                # Route through hooks validation (resolves symlinks + sensitive
                # check) so files read later during trigger matching are vetted.
                resolved = validate_file_path(str(skill_file))
                if resolved is None:
                    continue
                # The vetted root travels WITH the item. Containment is only
                # knowable here, and a side map keyed on the path string kept
                # going wrong: the key could disagree with the value handed out,
                # and a miss read unconfined. Carried in the tuple, neither is
                # expressible.
                results.append((name, Path(resolved), None))
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

        The stored snapshot goes too, and both generation counters move. Each is
        required for a different race: leaving the snapshot would let the next
        process serve the pre-mutation list, and leaving the generations alone would
        let a walk already in flight publish its pre-mutation answer on top of this
        clear — an invalidation a background refresh silently undoes. The in-process
        counter covers this loader's own worker; the index's epoch, which
        ``drop_catalog`` bumps, covers a walk running in another process.

        Emptying the list rather than serving the pre-mutation one is what makes the
        mutator's edit visible immediately, and it is also why the re-walk is QUEUED
        here instead of waiting for the next turn to demand it: on a tree whose walk
        outlasts ``_COLD_CATALOG_WAIT_SECS`` the next turn would otherwise re-enter
        the cold path, and starting the walk now bounds that window to the walk's own
        duration.
        """
        self._disabled_apps_cache = None
        with self._catalog_lock:
            scopes = list(self._iter_cache)
            self._iter_cache = {}
            self._catalog_fingerprints = {}
            self._catalog_incomplete.clear()
            self._snapshot_unadmitted.clear()
            self._catalog_generation += 1
            index = self._search_index
        self._fm_cache.clear()
        if index is not None:
            index.drop_catalog()
        for scope in scopes:
            self._request_catalog_refresh(scope)

    def _read_enumerated_skill_bytes(
        self,
        path: Path,
        within: str | None,
        *,
        max_bytes: int | None = None,
        refusal_reasons: list[str] | None = None,
        canonical_root: str | None = None,
    ) -> bytes | None:
        """Read a file `_iter` enumerated, re-checking the root it was vetted against.

        THE single read point for enumerated skills. `_iter` is TTL-cached, so a
        path it vetted can be replaced by a link out of the granted project before
        anyone reads it; and the containment that made it acceptable is only known
        at enumeration time. This re-checks it against the recorded root, on the
        descriptor actually opened rather than on the path string.

        Returns ``None`` when the file must not be served -- escaped its root, is
        a link out, is not a regular file, is hardlinked, or exceeds the size cap.
        ``None`` is the same answer every caller already handles for "no
        metadata" / "no body", so refusing degrades a row rather than failing a
        turn.

        A path with no recorded root (global skills dir, extra paths, edition
        roots) is read UNCONFINED, which preserves the app-provider symlink that
        `_trusted_skill_roots` exists to allow. External mappings instead carry
        ``canonical_root``: the resolved root admitted during enumeration,
        including an admitted provider target. Never resolve that root again
        after an ancestor swap. Their bodies retain the global read budget.
        """
        if within is None and canonical_root is None:
            # A path this process never walked carries no admission: the index it
            # came from is an agent-writable crew-home leaf, and the direct read
            # below applies no sensitive-path or UNC screen of its own, so a row
            # naming a link into a credential home would be read as a skill body.
            # The walk's own admission (`validate_file_path`) is therefore re-run
            # once per snapshot-derived path, at the single point every enumerated
            # read goes through. The set is checked for emptiness first, which is
            # the normal case and keeps the lock off this hot path: a scope's
            # markers are written before its rows are published, so a caller that
            # holds rows has already observed them.
            if self._snapshot_unadmitted and not self._admit_snapshot_path(path):
                if refusal_reasons is not None:
                    refusal_reasons.append("snapshot_path_refused")
                return None
            # No project grant is involved: the global skills dir, extra paths,
            # edition roots, and the paths writers construct themselves. These
            # are operator-installed, so there is no directory to confine them
            # to -- and taxing them with the hardened reader measurably slowed
            # the per-message listing path (test_skill_listing_cost guards it)
            # and emptied frontmatter on Windows, which stopped anything looking
            # pinned and dropped skill bodies out of the context entirely.
            #
            # A direct read also keeps the failure policy intact for free: an
            # unreadable file raises OSError here, which writers must hear.
            return path.read_bytes()
        try:
            confined_max = (
                hooks_module.MAX_FILE_BYTES
                if max_bytes is None
                else min(max_bytes, hooks_module.MAX_FILE_BYTES)
            )
            raw = safe_read_file_bytes_nolink(
                str(path),
                within_root=canonical_root or within,
                max_bytes=confined_max,
                within_root_is_canonical=canonical_root is not None,
            )
        except FileTooLargeError:
            # A REFUSAL, not an error: an oversized SKILL.md must not abort a
            # chat turn, and the global path applies no cap at all today.
            if refusal_reasons is not None:
                refusal_reasons.append("size_cap")
            logger.warning("Skipping oversized confined skill file: %s", path)
            return None
        if raw is not None:
            return raw
        if refusal_reasons is not None:
            refusal_reasons.append("outside_vetted_root")
        # A confined path is read-only project/provider input. Every refusal,
        # including a file replaced or removed after enumeration, degrades to no
        # metadata/body so one checkout entry cannot abort a chat turn. Writers
        # use the unconfined branch above, where genuine read failures remain loud.
        return None

    def _cached_frontmatter(
        self,
        path: Path,
        mtime: float | None = None,
        *,
        within: str | None,
        canonical_root: str | None = None,
    ) -> dict[str, str]:
        """Parse frontmatter with mtime-based caching.

        *mtime* lets a caller that already stat()'d the file reuse that result.
        ``list_skills()`` needs the size from the same stat, and this path runs
        on a worker during context assembly — one syscall per skill, not two.

        Confined project metadata cannot stat by path: `_iter` is TTL-cached,
        so an attacker can replace the enumerated file with a link before this
        call, and statting that link can initiate a Windows UNC connection.
        Those rows are read through the descriptor-pinned reader first and use
        a digest of the admitted bytes as their cache token.
        """
        if within is not None:
            return self._confined_frontmatter_and_size(path, within)[0]

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
        # Routed through the choke point rather than reading the path directly:
        # this is the site the reviewer found, and a bare read_text here has no
        # containment, no O_NOFOLLOW, no regular-file check and no size cap --
        # so an out-of-project `description` reached the injected skills index
        # verbatim and attacker-set `triggers`/`always` decided what auto-loaded.
        raw = self._read_enumerated_skill_bytes(path, within, canonical_root=canonical_root)
        if raw is None:
            logger.warning("Refusing metadata for a skill outside its vetted root: %s", path)
            return {}
        # A confined path is read-only project/provider metadata: malformed bytes
        # must not abort a chat turn. The unconfined path also serves writers such
        # as update_auto_skill, which must retain strict decoding so a rewrite
        # cannot silently replace undecodable metadata and lose version fields.
        meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=within is None))
        if within is None:
            meta["_content_digest"] = hashlib.sha256(raw).hexdigest()
        self._fm_cache[key] = (mtime, meta)
        return meta

    def _confined_frontmatter_and_size(self, path: Path, within: str) -> tuple[dict[str, str], int]:
        """Read confined metadata before any path-following metadata probe."""
        refusal_reasons: list[str] = []
        raw = self._read_enumerated_skill_bytes(
            path,
            within,
            max_bytes=PROJECT_SKILL_BODY_CAP,
            refusal_reasons=refusal_reasons,
        )
        if raw is None:
            if "size_cap" in refusal_reasons:
                # Keep the path-derived catalog row without retaining attacker
                # metadata. The over-cap sentinel makes every body consumer skip.
                return {}, PROJECT_SKILL_BODY_CAP + 1
            logger.warning("Refusing metadata for a skill outside its vetted root: %s", path)
            return {}, 0

        key = str(path)
        token = hashlib.sha256(raw).digest()
        cached = self._fm_cache.get(key)
        if cached and cached[0] == token:
            return cached[1], len(raw)
        meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=False))
        self._fm_cache[key] = (token, meta)
        return meta, len(raw)

    def list_skills(
        self,
        project_dir: str | Path | None = None,
        *,
        _entries: list[_ScopedSkillEntry] | None = None,
    ) -> list[dict]:
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

        This is blocking filesystem/SQLite work; async callers must offload it.
        Unconfined rows take exactly one stat and reuse its mtime for
        the frontmatter cache. Confined project rows perform no path stat; their
        size and cache token come from bytes admitted by the no-link reader.
        """
        started = time.monotonic()
        cached_metadata = self._search_index.metadata_snapshot() if self._search_index else {}
        snapshot_done = time.monotonic()
        changed_metadata: list[tuple[str, str, dict]] = []
        skill_reads = 0
        skills: list[dict] = []
        entries = (
            [_ScopedSkillEntry(*entry) for entry in self._iter_visible(project_dir)]
            if _entries is None
            else _entries
        )
        # Fingerprints from a walk THIS PROCESS performed, when it has performed
        # one. They let a warm build skip the stat it would otherwise take purely to
        # decide whether the persisted metadata is still good — an O(N) syscall pass
        # every turn pays on a large tree. Restricted to this process's own walk on
        # purpose: a fingerprint read off disk records what some earlier process
        # saw, so trusting it would make a restart serve a description for a file
        # edited out of band since. A row the walk did not fingerprint — a mapped
        # row, or any row on a process still serving the stored list — stats exactly
        # as before, which is validation rather than discovery: no walk, and no body
        # read.
        fingerprint_hint = self._catalog_fingerprint_hint(project_dir)
        scanned = time.monotonic()

        def read_entry(
            entry: _ScopedSkillEntry,
        ) -> tuple[dict[str, str], int, str, tuple[str, str, dict] | None, int]:
            name, skill_file, project_root, mapping_root = entry
            fingerprint = ""
            changed = None
            reads = 0
            if project_root is not None:
                meta, size_bytes = self._confined_frontmatter_and_size(skill_file, project_root)
            else:
                st: os.stat_result | None = None
                # A mapped row's fingerprint carries its mapping root, so the hint
                # (which records the bare stat) would not match what is stored.
                hinted = None if mapping_root else fingerprint_hint.get(str(skill_file))
                if hinted is not None:
                    fingerprint = hinted
                    mtime, size_bytes = _fingerprint_mtime_and_size(hinted)
                else:
                    try:
                        st = skill_file.stat()
                    except OSError:
                        st = None
                    fingerprint = (
                        f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}:{st.st_mtime_ns}:{st.st_size}"
                        if st is not None
                        else ""
                    )
                    if fingerprint and mapping_root:
                        fingerprint += f":{mapping_root}"
                    mtime = st.st_mtime if st is not None else None
                    size_bytes = st.st_size if st is not None else 0
                cached = cached_metadata.get(str(skill_file))
                if cached and cached[0] == fingerprint and cached[1].get("_catalog_key") == name:
                    meta = cached[1]
                else:
                    reads += 1
                    self._fm_cache.pop(str(skill_file), None)
                    meta = self._cached_frontmatter(
                        skill_file,
                        mtime=mtime,
                        within=None,
                        canonical_root=mapping_root,
                    )
                    meta["_catalog_key"] = name
                    if fingerprint:
                        changed = (str(skill_file), fingerprint, meta)
                if mtime is not None and meta:
                    self._fm_cache[str(skill_file)] = (mtime, meta)
            return meta, size_bytes, fingerprint, changed, reads

        def rows() -> Iterator[
            tuple[
                _ScopedSkillEntry,
                tuple[dict[str, str], int, str, tuple[str, str, dict] | None, int],
            ]
        ]:
            if len(entries) < _CATALOG_READ_BATCH:
                for entry in entries:
                    yield entry, read_entry(entry)
                return
            with ThreadPoolExecutor(
                max_workers=_CATALOG_READ_WORKERS, thread_name_prefix="skill-catalog"
            ) as pool:
                for batch in batched(entries, _CATALOG_READ_BATCH):
                    futures = [
                        pool.submit(copy_context().run, read_entry, entry) for entry in batch
                    ]
                    yield from zip(batch, (future.result() for future in futures))

        for entry, (meta, size_bytes, fingerprint, changed, reads) in rows():
            name, skill_file, project_root, mapping_root = entry
            if changed is not None:
                changed_metadata.append(changed)
            skill_reads += reads
            skills.append(
                {
                    # Internal: lets a later re-read (see _rank_key) reuse the root
                    # this row was read under instead of guessing at one.
                    "confine_root": project_root,
                    "mapping_root": mapping_root,
                    "key": name,
                    "name": meta.get("name", name),
                    "description": meta.get("description", name),
                    "path": str(skill_file),
                    "dir": str(skill_file.parent),
                    "always": meta.get("always", "").strip().lower() == "true",
                    # Carried so a caller assembling context can drop a
                    # repo-scoped skill from the INDEX, not just from the
                    # injected body: a summary line the agent is told to read
                    # advertises the skill just as effectively. Stripped because
                    # the consumer guards on this value's truthiness before
                    # calling the gate, so it has to agree with the other two
                    # gate call sites about what counts as "no scope at all".
                    "repo_scope": meta.get("repo_scope", "").strip(),
                    # Mirrors split_triggered: confined project rows always use
                    # the body; only an explicit `false` on an unconfined skill
                    # opts out. A malformed value therefore reads as injecting.
                    "inject_on_trigger": (
                        project_root is not None
                        or meta.get("inject_on_trigger", "").strip().lower() != "false"
                    ),
                    "size_bytes": size_bytes,
                    "fingerprint": fingerprint if project_root is None else "",
                    "content_digest": (
                        meta.get("_content_digest", "") if project_root is None else ""
                    ),
                    "metadata_indexed": project_root is None and bool(fingerprint),
                    "deliveries": self._delivery_count(name),
                    "owned": self._owned_hint(skill_file),
                }
            )
        assembled = time.monotonic()
        if self._search_index and not self._search_index.store_metadata(changed_metadata):
            changed_paths = {path for path, _, _ in changed_metadata}
            for row in skills:
                if row["path"] in changed_paths:
                    row["metadata_indexed"] = False
        logger.debug(
            "skill catalog: %.2fms total, %.2fms snapshot, %.2fms scan, "
            "%.2fms read/assemble, %.2fms persist, %d rows, %d metadata reads",
            (time.monotonic() - started) * 1000,
            (snapshot_done - started) * 1000,
            (scanned - snapshot_done) * 1000,
            (assembled - scanned) * 1000,
            (time.monotonic() - assembled) * 1000,
            len(skills),
            skill_reads,
        )
        return skills

    def _owning_app(self, name: str, skill_file: Path) -> str | None:
        """The app whose bundle this skill came from, or ``None``.

        Two shapes have to resolve to the same owner, because ``bridges``
        registers every app skill twice and either registration can be the one
        this walk kept (see ``_iter_skill_files``'s ``seen_real`` note):

        * the namespaced ``skills/<app>/<skill>`` directory — the first segment
          of ``name`` IS the app;
        * the flat ``skills/<skill>`` link, whose name says nothing — so the
          real path is consulted. An externally installed app resolves under
          the data home's apps root, where the directory name IS the app name.
          A shipped BUILTIN resolves inside the package tree
          (``…/apps/builtins/<pkg dir>/skills/…``), and its package directory
          (``auto_improvement``) is not its app name (``auto-improvement``) —
          the manifest in that directory is the authoritative mapping (see
          ``apps.discovery``).

        Path-shaped, not manifest-keyed, on purpose: it must answer for a
        third-party app just as well as a builtin, and the registration layout
        is the one thing every app shares.
        """
        head = name.split("/", 1)[0]
        if head != name:
            return head
        try:
            from kiro_crew.apps.manager import apps_dir

            real = Path(os.path.realpath(skill_file))
            root = apps_dir()
            if real.is_relative_to(root):
                # <apps root>/<app>/... — the segment directly under the root.
                return real.relative_to(root).parts[0]
            builtins_root = Path(os.path.realpath(Path(__file__).parent)) / "apps" / "builtins"
            if real.is_relative_to(builtins_root):
                pkg_dir = builtins_root / real.relative_to(builtins_root).parts[0]
                return _builtin_dir_app_name(str(pkg_dir))
        except Exception:
            return None
        return None

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
        for key, skill_file, _within in self._iter():
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
        endpoint does not build the map itself. It still reads other loader
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
        # Root dropped at this boundary: the budget view only needs identity and
        # size, and never reads a body through the confined reader.
        skill_pairs = [(n, pth) for n, pth, _w in self._iter()]

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

        # A ledger key that does not name a served skill: resolve it on disk and
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
        """Return True if skill name is safe (no traversal, rooted, or dot-only).

        A rooted name must be rejected because ``Path.__truediv__`` discards
        the base directory when the joined segment is absolute, so
        ``self._dir / name`` would resolve outside the skills root. Both
        flavours are checked: POSIX-absolute (``/etc/x``) and Windows
        rooted/drive-qualified in the forward-slash spelling (``C:/x``,
        ``C:x``, ``//server/share/x``) — the backslash spelling is already
        caught by the ``"\\\\"`` rule. Dot-only spellings (``.``, ``./``)
        must also be rejected: pathlib drops ``.`` components on join, so
        ``self._dir / "."`` collapses to the skills root itself and a delete
        would remove every installed skill. ``PurePosixPath(name).parts`` is
        empty exactly for those spellings.
        """
        return (
            bool(name)
            and ".." not in name
            and "\\" not in name
            and bool(PurePosixPath(name).parts)
            and not PurePosixPath(name).is_absolute()
            and not PureWindowsPath(name).is_absolute()
            and not PureWindowsPath(name).drive
        )

    def load_skill(
        self,
        name: str,
        project_dir: str | Path | None = None,
        *,
        max_bytes: int | None = None,
    ) -> str | None:
        """Load a single skill's content by name (supports nested paths).

        *project_dir* additionally allows a body to come from that project's own
        trusted ``<project>/.kiro/skills``. It is probed LAST so precedence
        matches enumeration: a repository cannot serve the body for a name the
        operator already installed globally.
        """
        if not self._safe_name(name):
            return None
        _t0 = time.monotonic()
        skill_file = self._dir / name / "SKILL.md"
        if skill_file.exists():
            content = self._read_global_skill_text(skill_file, max_bytes)
            if content is None:
                return None
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
                content = self._read_global_skill_text(Path(resolved), max_bytes)
                if content is None:
                    return None
                self._emit_lazy_load_metric(_t0, hit=True)
                return content
        # A trusted project's own skills, last — same order as _iter_uncached.
        project_key = self._trusted_project_key(project_dir)
        if project_key:
            # Allowlist-only, like ``_resolve_path`` and ``resolve_dollar_skills``:
            # the path comes from the ENUMERATION, never built from *name*. No
            # caller-supplied string reaches a path expression, so a crafted name
            # cannot escape the trusted root.
            #
            # The containment test below is defence in depth, not the primary
            # control: ``_iter_uncached`` already refuses a skills root that
            # links out of the granted directory, so a smuggled entry cannot be
            # in this enumeration to begin with. It is kept because it also
            # states which root this branch is permitted to serve, and because
            # the primary control living in a different method is exactly the
            # kind of coupling a later refactor breaks silently.
            for candidate, skill_file, _within in self._iter(project_dir):
                if candidate != name or not _within_any(str(skill_file), (project_key,)):
                    continue
                # The enumeration is TTL-cached, so the path was vetted up to a
                # minute ago: the SKILL.md it names can since have been replaced
                # by a symlink out of the project. Read through the hardened
                # reader, which opens O_NOFOLLOW and fstat()s the descriptor it
                # actually read, and which enforces containment on that same
                # inode rather than on the (now stale) path string.
                # Same choke point as the metadata read, so the two cannot
                # drift apart again -- the previous round hardened this site
                # alone and left its sibling reading the same cached paths
                # unchecked.
                refusal_reasons: list[str] = []
                confined_max = (
                    PROJECT_SKILL_BODY_CAP
                    if max_bytes is None
                    else min(max_bytes, PROJECT_SKILL_BODY_CAP)
                )
                raw = self._read_enumerated_skill_bytes(
                    skill_file,
                    _within,
                    max_bytes=confined_max,
                    refusal_reasons=refusal_reasons,
                )
                if raw is None:
                    if "outside_vetted_root" in refusal_reasons:
                        logger.warning(
                            "Refusing project skill outside its granted root: %s", skill_file
                        )
                    break
                # Decoded explicitly: an implicit read would use the platform's
                # locale encoding and mangle non-ASCII bodies on Windows.
                content = _decode_skill_text(raw, strict=False)
                self._emit_lazy_load_metric(_t0, hit=True)
                return content
        self._emit_lazy_load_metric(_t0, hit=False)
        return None

    def _read_global_skill_text(
        self, path: Path, max_bytes: int | None, *, canonical_root: str | None = None
    ) -> str | None:
        """A global skill body, bounded by *max_bytes* and decode-safe.

        ``max_bytes=None`` reads up to the shared file safety cap with strict
        UTF-8, preserving the global listing path's decode behavior. Every body
        read uses the shared validated reader without project confinement:
        sensitive paths, hardlinks and identity changes are refused on the
        descriptor supplying the bytes.

        With a bound, refuse rather than truncate. A caller that asked for at
        most N bytes is deciding whether the body FITS, and half a skill is not
        a smaller skill: it is a body whose instructions stop mid-sentence.
        ``None`` says "this one cannot be delivered", which the caller reports
        instead of silently dropping.
        """
        try:
            raw = safe_read_file_bytes_nolink(
                str(path),
                max_bytes=max_bytes,
                within_root=canonical_root,
                within_root_is_canonical=canonical_root is not None,
            )
        except FileTooLargeError:
            if max_bytes is None:
                logger.debug("skill body at %s exceeds the shared file-read bound; refusing", path)
            else:
                logger.debug(
                    "skill body at %s exceeds the %d byte bound; refusing", path, max_bytes
                )
            return None
        if raw is None:
            return None
        return _decode_skill_text(raw, strict=max_bytes is None)

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
        if not _DIR_FD_SUPPORTED:
            # exist_ok=False so a skill directory that appeared between the
            # exists() check above and here is REFUSED rather than written
            # through: two concurrent creates would otherwise both mkdir, both
            # write_text the same SKILL.md, and both report success, losing one
            # submitted body. The pinned branch answers the same way, through
            # its own O_EXCL-equivalent -- os.mkdir under the pinned parent raising
            # FileExistsError -- so without this the two branches of this fork
            # disagree on the same request. parents=True
            # still creates the intermediates a nested name needs; only the leaf
            # is refused.
            try:
                skill_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                return False
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            self._invalidate_iter_cache()  # so the new skill shows in list_skills() now
            logger.info("Created skill: %s", name)
            return True

        # Ensure the intermediate tree by name (a nested skill name has parents
        # the caller owns), then create the leaf skill dir and its SKILL.md
        # relative to a pinned descriptor so an ancestor swapped for a link after
        # the exists() check cannot redirect the write. The leaf mkdir refuses a
        # skill dir that appeared in the meantime, matching the exists() guard.
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        # ONE resolution of the parent chain, and everything below it addressed
        # through the descriptor it produced: the leaf directory, its SKILL.md, and
        # the rollback that removes both. A second walk would be a second chance for
        # an ancestor swapped since the first to be followed, and would also leave
        # the create and the rollback pointing at different directories.
        try:
            parent_fd = pinned_fs.open_dir_pinned(skill_dir.parent, what="skill directory")
        except pinned_fs.PinnedPathRefusal:
            return False
        except OSError:
            return False
        try:
            return self._create_skill_pinned(name, content, skill_dir, parent_fd)
        finally:
            os.close(parent_fd)

    def _create_skill_pinned(
        self, name: str, content: str, skill_dir: Path, parent_fd: int
    ) -> bool:
        """Create *skill_dir* and its SKILL.md under *parent_fd*, or leave nothing behind.

        Split out so the rollback has one exit rather than being threaded through
        ``create_skill``'s branches. A partial create is not merely untidy here: the
        leftover directory makes ``create_skill``'s ``exists()`` guard answer False
        forever, so every retry is a 409 over a truncated body that ``list_skills()``
        still serves. Steering's create already unlinks its partial leaf for exactly
        that reason; this is the same rule, plus the directory, because this call is
        the one that created it.

        The leaf directory is created and opened RELATIVE to *parent_fd*, not through
        ``pinned_fs.create_and_open_dir_pinned``. That helper resolves
        ``skill_dir.parent`` with its own ``realpath`` and pins it again, discarding
        the descriptor the caller already walked -- a second resolution, which an
        ancestor swapped since the first is followed by. It would also leave the
        create and the rollback addressing two different directories, so on such a
        swap ``SKILL.md`` lands outside the skills root while the rollback reports an
        identity mismatch on an unrelated one. The helper's two other jobs are
        reproduced here rather than borrowed: a name that already exists is refused
        because ``os.mkdir`` under the pinned parent raises ``FileExistsError`` (the
        exclusivity is the syscall's, not a flag on a helper), and a link or
        non-directory at the leaf becomes the one refusal the caller maps rather than
        a raw errno.
        """
        try:
            os.mkdir(skill_dir.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            # Something holds the name that this call did not create, so it is not
            # ours to write into -- the exists() guard's answer, re-asked without a
            # window. 0o700 matches create_and_open_dir_pinned's mode for every
            # caller, so the directory-mode behaviour is unchanged.
            return False
        try:
            dir_fd = os.open(skill_dir.name, pinned_fs.dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            # A link or a plain file raced onto the name between the mkdir and here.
            # Reclaim the directory this call just made -- rmdir only ever removes an
            # EMPTY one, so the worst case on a swap is losing a directory nobody has
            # written to yet, and leaving it would make every retry answer 409.
            with suppress(OSError):
                os.rmdir(skill_dir.name, dir_fd=parent_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return False
            raise
        # Identity of the directory THIS call created, taken from the descriptor before
        # anything can be swapped at the name, so the rollback below can only ever
        # remove what this call brought into being. Guarded, because an fstat that
        # fails (EIO or ESTALE on a network filesystem) would otherwise leak the
        # descriptor AND strand the directory, and a stranded directory answers every
        # retry with 409.
        try:
            created = os.fstat(dir_fd)
        except BaseException:
            os.close(dir_fd)
            with suppress(OSError):
                os.rmdir(skill_dir.name, dir_fd=parent_fd)
            raise
        # Bound before the guarded region so the rollback can tell "no identity to
        # verify against" from "the identity is X" without inspecting locals.
        leaf: os.stat_result | None = None
        try:
            # 0o666, masked by umask, is what the by-name floor's write_text
            # produces, so the two branches land the same permissions and the pin
            # changes no default. This is the mode prompts.py's own pinned O_EXCL
            # create of user content passes, for the same reason. A tighter default
            # for user-authored skill bodies is a policy change that has to cover
            # both branches and both platforms, so it does not ride a migration.
            fd = os.open(
                "SKILL.md",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o666,
                dir_fd=dir_fd,
            )
            try:
                try:
                    # Identity of the inode this call created, from the descriptor while
                    # it is provably ours: the rollback addresses a NAME, and a rival can
                    # unlink ours and create its own inside the failure window.
                    leaf = os.fstat(fd)
                    data = content.encode("utf-8")
                    written = 0
                    while written < len(data):
                        written += os.write(fd, data[written:])
                except BaseException:
                    # Ask for the identity once more while the descriptor is still open --
                    # the close below is what takes it away, and the rollback arm cannot
                    # unlink anything without one. An EIO or ESTALE that made the first
                    # fstat fail on a network filesystem is usually transient, so this is
                    # a free path back to the verified arm; it can never answer with
                    # another object, because it addresses a descriptor rather than a name.
                    if leaf is None:
                        with suppress(OSError):
                            leaf = os.fstat(fd)
                    raise
            finally:
                os.close(fd)
        except BaseException:
            # Roll the whole create back, leaf first, both through descriptors. Caught
            # broadly rather than on OSError: a KeyboardInterrupt or a MemoryError
            # building the buffer leaves the same half-made skill, and the retry is
            # just as permanently 409 either way.
            #
            # Both halves verify identity, because both address a NAME under a
            # descriptor and a name can be replaced inside the failure window. The
            # leaf goes through unlink_verified, which stats through the directory's
            # own fd and unlinks only if the inode is still the one created above, so
            # a rival that replaced SKILL.md keeps ITS file. The directory goes
            # through remove_dir_verified, which renames it aside under the pinned
            # parent, re-checks (st_dev, st_ino), and only then rmdirs -- a directory
            # swapped in at the name is reported rather than removed. A bare unlink
            # or rmdir by name would delete whatever answers to the name, which is
            # the step this whole migration exists to remove.
            #
            # ``leaf`` is None only when the leaf open failed, or when BOTH fstats on
            # the descriptor this call owned failed -- and the first of those precedes
            # the first os.write, so in every one of those cases nothing was written.
            # No identity therefore means no unlink: the empty SKILL.md keeps the
            # directory non-empty, remove_dir_verified's rmdir fails and puts the name
            # back, and the create is left as a skill with an empty body, which the
            # Skills tab lists and which update_skill and delete_skill both reach. That
            # is a save away from correct; unlinking whatever answers to the name to
            # spare that would destroy a file this code has never read.
            if leaf is not None:
                pinned_fs.unlink_verified(dir_fd, "SKILL.md", (leaf.st_dev, leaf.st_ino))
            outcome = pinned_fs.remove_dir_verified(
                parent_fd, skill_dir.name, expect=(created.st_dev, created.st_ino)
            )
            if not outcome.removed:
                # Reported, not raised over: the original failure is the one the caller
                # needs, and a rollback that could not finish leaves a name a human has
                # to look at. staged_name is set only when the entry was left aside.
                logger.warning(
                    "skill create rollback left %s behind (%s%s)",
                    name,
                    outcome.reason,
                    f", staged as {outcome.staged_name}" if outcome.staged_name else "",
                )
            raise
        finally:
            os.close(dir_fd)
        self._invalidate_iter_cache()  # so the new skill shows in list_skills() now
        logger.info("Created skill: %s", name)
        return True

    def update_skill(self, name: str, content: str) -> bool:
        """Overwrite an existing skill's SKILL.md.  Returns True if found."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return False
        # Both capabilities, not one: the walk that produces the descriptor and the
        # descriptor-relative rename that consumes it are separate probes, and
        # atomic_write REFUSES a descriptor it cannot publish through rather than
        # quietly writing by name, so the floor is chosen here instead.
        if not (_DIR_FD_SUPPORTED and pinned_parent_replace_supported()):
            if not self._write_skill_md(skill_file, content, dir_fd=None):
                return False
            self._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
            logger.info("Updated skill: %s", name)
            return True

        # Pin the skill directory so the atomic replace stages and renames
        # through the walked descriptor rather than by name.
        #
        # open_dir_pinned, not pin_parent: ``self._dir / name`` is a lexical join
        # that nothing canonicalized, so this realpath is the FIRST resolution of
        # the chain rather than a second one, and there is no earlier canonical form
        # for pin_parent to walk. pin_parent here would instead refuse the ordinary
        # symlinks that legitimately sit above the skills root -- a symlinked $HOME
        # is the common one -- and break every update on such a host.
        try:
            dir_fd = pinned_fs.open_dir_pinned(skill_dir, what="skill directory")
        except pinned_fs.PinnedPathRefusal:
            return False
        except OSError:
            return False
        try:
            if not self._write_skill_md(skill_file, content, dir_fd=dir_fd):
                return False
        finally:
            os.close(dir_fd)
        self._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
        logger.info("Updated skill: %s", name)
        return True

    @staticmethod
    def _write_skill_md(skill_file: Path, content: str, *, dir_fd: int | None) -> bool:
        """Atomically replace *skill_file*, carrying its access-control xattrs.

        Routes through ``atomic_write`` with the same ACL carry the steering and
        file-write update paths use: ``mode=`` alone reproduces permission BITS
        only, so a named POSIX ACL the owner set on a skill's SKILL.md would be
        dropped the moment the replace installs a fresh inode. When *dir_fd* is a
        pinned parent the temp create and rename run relative to it, and the ACL
        source is opened relative to it too -- addressing the leaf by name after
        the caller pinned its directory would let a directory replaced at that
        name supply the mode and the ACL while the write published into the pinned
        original, handing the real skill back with permissions chosen by whoever
        did the replacing.

        Returns False when the target is REJECTED -- the source open failed, so
        there is no inode to carry from. A write failure still raises.
        """
        try:
            src_fd = open_access_control_source(skill_file, dir_fd=dir_fd)
        except OSError:
            # The same disposition the steering and file-write updates give this:
            # a rejected target, not a server fault. Continuing with src_fd=None
            # would publish a fresh inode carrying only the permission bits, so a
            # named POSIX ACL the owner set on this SKILL.md would be dropped and
            # the file handed back protected differently from the one it replaced
            # -- silently, on the one path that was supposed to fix that.
            return False
        try:
            # By-name stat only on the unpinned floor: with dir_fd the helper
            # always hands back a descriptor, so the bits and the ACL come from
            # one inode and neither is re-resolved.
            mode = (
                stat.S_IMODE(os.fstat(src_fd).st_mode)
                if src_fd is not None
                else stat.S_IMODE(skill_file.stat().st_mode)
            )
            atomic_write(
                skill_file,
                content,
                mode=mode,
                newline="",
                preserve_access_control_from=src_fd,
                parent_dir_fd=dir_fd,
            )
        finally:
            if src_fd is not None:
                try:
                    os.close(src_fd)
                except OSError:
                    pass
        return True

    def delete_skill(self, name: str) -> bool:
        """Delete a skill directory.  Returns True if found and removed."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if not skill_dir.is_dir():
            return False
        if _DIR_FD_SUPPORTED:
            # A recursive descriptor-relative delete is out of proportion for a
            # skill dir, so the residual guarded here is narrower: pin the parent,
            # answer "is this name a real directory?" from a descriptor-relative
            # lstat, and only then rmtree. The is_dir() above FOLLOWS a link, so a
            # symlinked skill dir reaches this point; shutil.rmtree then refuses it
            # with an OSError the caller would surface as a 500 instead of the
            # not-found the by-name floor gives. A directory swapped for a link
            # after this check is the remaining window -- recorded, and the by-name
            # floor below carries the same posture.
            try:
                parent_fd = pinned_fs.open_dir_pinned(skill_dir.parent, what="skill directory")
            except pinned_fs.PinnedPathRefusal:
                return False
            except OSError:
                return False
            try:
                st = pinned_fs.stat_at(parent_fd, skill_dir.name)
                if st is None or not stat.S_ISDIR(st.st_mode):
                    return False
            finally:
                os.close(parent_fd)
        elif is_link_or_junction(skill_dir):
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
        for name, skill_file, _within in self._iter():
            if exclude and name == exclude:
                continue
            meta = self._cached_frontmatter(skill_file, within=_within)
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
        existing_meta = self._cached_frontmatter(skill_file, within=None)
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
    def _repo_scope_satisfied(relpath: str, project_dir: str | Path | None) -> bool:
        """Mechanical gate for repo-scoped skills (``repo_scope:`` frontmatter).

        A skill carrying ``repo_scope: <relpath>`` is only eligible for
        injection when *project_dir* (or an ancestor of it) contains *relpath*
        — e.g. ``repo_scope: src/kiro_crew`` restricts a skill to sessions
        whose active project IS the Kiro Crew source tree. This is the
        loader-enforced counterpart to a prose "ignore this skill elsewhere"
        scope guard: prose depends on probabilistic LLM obedience, while this
        check runs before the skill ever reaches the context (destructive
        repo-dev instructions must be mechanically contained).

        *project_dir* is the SESSION's active project — the same value the
        ``[PROJECT]`` context block names. The process working directory is
        deliberately NOT consulted: this runs in the gateway while it assembles
        context, so ``Path.cwd()`` is the gateway's own working directory and
        says nothing about the repository the session is working on. Reading it
        made the gate answer by install shape rather than by work: a gateway
        started from inside a checkout of the scoped repo admitted the skill
        into EVERY session, while a packaged install whose cwd holds no marker
        suppressed it for every session, contributors included.

        Fails CLOSED — no project, an unusable one, or any error suppresses the
        skill, so an un-scoped surface never inherits repo-specific rules.

        The rule itself lives in ``kiro_crew.project_scope`` because lessons are
        scoped by the same key: both are instructions injected into a session, so
        both must agree on what "in scope" means.
        """
        return project_scope_satisfied(relpath, project_dir)

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
            # A listed row can be a project skill, so reuse the root the listing
            # recorded rather than reading it unconfined for a ranking signal.
            meta = self._cached_frontmatter(Path(s["path"]), within=s.get("confine_root"))
            hits, anchor = self._auto_activity(key, s["path"], meta)
            pinned = str(meta.get("pinned", "")).strip().lower() == "true"
            slug = key.split("/")[-1]
            exempt_row = (
                pinned
                or key in cron_referenced
                or slug in cron_referenced
                or key in extra_exempt
                or slug in extra_exempt
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
                logger.warning("Too many pending candidates for slug %s; deferring re-stage", slug)
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
        logger.info("Staged pending skill candidate: %s (scripts=%d)", name, len(script_names))
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
            # Same verdict the approve path will reach, so the card can carry a
            # warning badge WITHOUT the user expanding the row first. The
            # verdict walk is self-defending — descriptor-pinned, budgeted,
            # fail-closed (see _pending_scripts_verdict) — so no candidate-wide
            # pre-walk runs here: an unbudgeted os.walk before the budgeted one
            # would itself be the unbounded per-poll traversal the budgets
            # exist to prevent. ``None`` means the platform cannot compute a
            # trustworthy verdict; the field is omitted rather than serving a
            # false all-clear.
            verdict = self._pending_scripts_verdict(child)
            entry = {
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
            if verdict is not None:
                v_ok, v_report = verdict
                entry["script_validation"] = {
                    "ok": v_ok,
                    "report": self._redact_validation_report(v_report),
                }
            out.append(entry)
        return out

    @staticmethod
    def _redact_text(text: object) -> str:
        """Two-pass redaction for untrusted skill text.

        Project catalog metadata and pending skill detail/approval both reach
        the dashboard from files an untrusted producer can write. Apply the same
        exfiltration-URL and credential passes at those read points so neither
        surface can return secrets or promote them live.
        """
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
                            {
                                "filename": str(fp.relative_to(sdir)),
                                "content": fp.read_text(encoding="utf-8"),
                            }
                        )
                    except OSError:
                        continue
        return out

    _ALLOWED_CANDIDATE_TOP = frozenset({"SKILL.md", ".meta.json", "scripts"})

    def _candidate_layout_findings_at(self, root_fd: int) -> list[str]:
        """Top-level layout check mirroring ``_candidate_layout_ok``.

        The approve path refuses a candidate whose ROOT layout is wrong — a
        symlinked ``SKILL.md``/``.meta.json``, a non-regular one (promotion
        reads their bytes, so a directory there is refused too), or any
        unexpected top-level entry — so a verdict that only inspects
        ``scripts/`` would read ``ok: true`` for a candidate approve is
        guaranteed to refuse, which is the predict-the-refusal contract this
        verdict exists to keep.

        Operates on the ALREADY-OPEN candidate-root descriptor the caller
        retains for the whole verdict — this function resolves no path at
        all. Every entry is stat-ed RELATIVE to that descriptor, so nothing
        a candidate does to the tree's names after the root was pinned can
        redirect the scan: the descriptor IS the directory, whatever any
        path now resolves to.

        This is deliberately NOT the recursive candidate-wide pre-walk the
        budgets removed: one capped scan of the top level plus a stat per
        allowed name — no byte is read, nothing recurses (``scripts/``
        internals stay the pinned walk's job). Findings use the same
        ``invalid layout:`` vocabulary as the walk so the frontend renders
        them identically.
        """
        names: list[str] = []
        try:
            with os.scandir(root_fd) as scanner:
                for entry in scanner:
                    names.append(entry.name)
                    if len(names) > 16:
                        # The valid top level has at most 3 entries; a
                        # planted crowd must not grow this per-poll
                        # scan without limit.
                        return ["invalid layout: too many top-level entries"]
        except OSError:
            return ["verdict unavailable: candidate unreadable"]
        findings: list[str] = []
        for nm in sorted(names):
            if nm not in self._ALLOWED_CANDIDATE_TOP:
                findings.append(f"invalid layout: unexpected candidate entry {nm!r}")
                continue
            est = pinned_fs.stat_at(root_fd, nm)
            if est is None:
                findings.append(f"invalid layout: {nm!r} unreadable")
            elif stat.S_ISLNK(est.st_mode):
                findings.append(f"invalid layout: {nm!r} is a symlink")
            elif nm != "scripts" and not stat.S_ISREG(est.st_mode):
                # Promotion reads these files' bytes; a directory (or
                # FIFO) named SKILL.md/.meta.json is refused at
                # approve time, so the verdict must not read clean.
                findings.append(f"invalid layout: {nm!r} is not a regular file")
        return findings

    def _pending_scripts_verdict(self, pdir: Path) -> tuple[bool, dict] | None:
        """Cheap pre-approval validation verdict for the pending LIST path.

        Takes the CANDIDATE ROOT: the top-level layout is prechecked against
        the same rules the approve path enforces (see
        :meth:`_candidate_layout_findings_at`) before the ``scripts`` walk, so a
        symlinked ``SKILL.md`` or a stray top-level file fails the verdict
        here exactly as approve would refuse it.

        The candidate root is resolved EXACTLY ONCE: one
        :func:`pinned_fs.open_dir_pinned` call pins it (``O_NOFOLLOW`` on the
        root and every ancestor), and that descriptor is retained for the
        whole verdict — the top-level layout scan reads through it, and the
        ``scripts`` directory is opened RELATIVE to it (``dir_fd``), with an
        identity check that the opened directory is the same inode the
        layout scan stat-ed. No name under the candidate is ever re-resolved
        from a path, so a candidate root swapped between any two steps —
        whether for a symlink (refused at the pinned open) or for a
        different REAL directory renamed over it (unreachable, because no
        second resolution exists to land on it) — cannot redirect any part
        of the verdict into another tree. Every entry below ``scripts`` is
        likewise stat-ed, opened and read relative to the walk's own
        descriptors.

        The verdict fails CLOSED on everything the approve path would refuse:
        a symlink entry is an invalid layout, an oversized script is flagged
        from its size alone (its bytes are never loaded — this runs on every
        dashboard poll, and a crystallize direct-write can plant files the
        staging cap never saw), an unreadable or undecodable script is a
        finding rather than a silent omission (approve refuses such a
        candidate at redaction, so ``ok: true`` would be a false all-clear),
        and an unexpected walk error degrades to a failing
        verdict-unavailable finding rather than blanking the caller's whole
        list. Small, decodable scripts get the real ``validate_scripts`` run,
        matching the approve path's verdict.

        Returns ``None`` on a platform without descriptor-relative opens,
        BEFORE the candidate is touched at all. A link/junction check
        followed by a by-name scan races with replacement of the candidate
        root: even a refusal can expose the target's filenames. On Windows
        the pre-click "fails validation" badge disappears entirely, because
        no trustworthy verdict can be computed without descriptor-relative
        opens. The caller omits the field; approve remains the authority at
        click time.
        """
        if not pinned_fs.supports_pinned_walk():
            return None
        try:
            root_fd = pinned_fs.open_dir_pinned(pdir, what="pending candidate root")
        except pinned_fs.PinnedPathRefusal:
            return False, {
                "<candidate>": ["invalid layout: candidate root is not a real directory"]
            }
        except OSError:
            return False, {"<candidate>": ["verdict unavailable: candidate unreadable"]}
        try:
            return self._pending_scripts_verdict_at(root_fd)
        finally:
            os.close(root_fd)

    def _pending_scripts_verdict_at(self, root_fd: int) -> tuple[bool, dict] | None:
        """The verdict body, entirely relative to the retained root descriptor."""
        layout = self._candidate_layout_findings_at(root_fd)
        if layout:
            return False, {"<candidate>": layout}
        sst = pinned_fs.stat_at(root_fd, "scripts")
        if sst is None:
            # No scripts directory at all — nothing to validate.
            return True, {}
        if stat.S_ISLNK(sst.st_mode) or not stat.S_ISDIR(sst.st_mode):
            return False, {"<candidate>": ["invalid layout: 'scripts' is not a real directory"]}
        if not pinned_fs.supports_pinned_tree_walk():
            return None
        try:
            scripts_fd = os.open("scripts", pinned_fs.dir_flags(), dir_fd=root_fd)
        except OSError:
            return False, {"<candidate>": ["invalid layout: contains a symlink"]}
        try:
            # The OPENED directory must be the same inode the stat above
            # described — a 'scripts' swapped between the stat and the open
            # (even for another real directory; both live under the pinned
            # root) fails the verdict closed instead of being walked as if
            # it were the audited one.
            ost = os.fstat(scripts_fd)
            if (ost.st_ino, ost.st_dev) != (sst.st_ino, sst.st_dev):
                os.close(scripts_fd)
                return False, {"<candidate>": ["invalid layout: entry changed during scan"]}
        except OSError:
            os.close(scripts_fd)
            return False, {"<candidate>": ["verdict unavailable: candidate unreadable"]}
        scripts: list[dict] = []
        extra: dict[str, list[str]] = {}
        # Hard budgets for the whole walk: this runs on every dashboard poll,
        # and a crystallize direct-write can plant MANY small files — or many
        # nested directories — that the per-file size cap never bounds.
        # Directories count against the same entry budget and recursion is
        # depth-capped, so a planted tree can neither grow the traversal
        # without limit nor raise RecursionError into the caller's degraded
        # ok-true fallback. Exceeding any budget stops the walk immediately
        # and OMITS the verdict (see the breach return below) — never a false
        # refusal claim; the approve path remains the authority on the full
        # set.
        max_files = _PENDING_SCRIPT_MAX_ENTRIES
        max_depth = 8
        budget = {"files": 0, "bytes": 0, "breached": False}
        max_total_bytes = max_files * MAX_SCRIPT_BYTES

        def _breach(reason: str) -> None:
            budget["breached"] = True
            extra.setdefault("<candidate>", []).append(reason)

        def _walk(fd: int, prefix: str, depth: int) -> None:
            # Collect names LAZILY with a cap before sorting: an eager
            # sorted(os.listdir(fd)) materializes an attacker-sized directory
            # in one allocation before any budget applies, which is the exact
            # per-poll exhaustion the budgets exist to prevent. Scanning stops
            # at the entry budget, so at most max_files+1 names are ever held.
            names: list[str] = []
            with os.scandir(fd) as scanner:
                for entry in scanner:
                    names.append(entry.name)
                    if len(names) > max_files:
                        _breach(f"too many scripts: over {max_files} entries")
                        return
            for nm in sorted(names):
                if budget["breached"]:
                    return
                rel = f"{prefix}{nm}"
                try:
                    est = os.stat(nm, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    extra.setdefault(rel, []).append("unreadable script: stat failed")
                    continue
                if stat.S_ISLNK(est.st_mode):
                    extra.setdefault(rel, []).append("invalid layout: entry is a symlink")
                elif stat.S_ISDIR(est.st_mode):
                    # Directories spend the same entry budget as files, and
                    # recursion is depth-capped — a planted tree of many or
                    # deeply nested dirs must not reintroduce the unbounded
                    # per-poll traversal (or a RecursionError that the
                    # caller's fallback would degrade to a false all-clear).
                    budget["files"] += 1
                    if budget["files"] > max_files:
                        _breach(f"too many scripts: over {max_files} entries")
                        return
                    if depth >= max_depth:
                        _breach(f"scripts tree too deep: over {max_depth} levels")
                        return
                    try:
                        sub = os.open(nm, pinned_fs.dir_flags(), dir_fd=fd)
                    except OSError:
                        extra.setdefault(rel, []).append("invalid layout: entry is a symlink")
                        continue
                    try:
                        _walk(sub, f"{rel}/", depth + 1)
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(est.st_mode):
                    if est.st_size > MAX_SCRIPT_BYTES:
                        extra.setdefault(rel, []).append(
                            f"too large: {est.st_size} bytes > {MAX_SCRIPT_BYTES} cap"
                        )
                        continue
                    budget["files"] += 1
                    budget["bytes"] += est.st_size
                    if budget["files"] > max_files or budget["bytes"] > max_total_bytes:
                        _breach(
                            f"too many scripts: over {max_files} entries or "
                            f"{max_total_bytes} aggregate bytes"
                        )
                        return
                    try:
                        # O_NONBLOCK: O_NOFOLLOW rejects a swapped symlink but
                        # NOT a swapped FIFO, and a blocking O_RDONLY open of a
                        # writerless FIFO wedges this poll thread forever. For
                        # a regular file O_NONBLOCK is a no-op on open and
                        # read, so the flag costs nothing on the honest path.
                        rfd = os.open(
                            nm,
                            os.O_RDONLY
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_NONBLOCK", 0),
                            dir_fd=fd,
                        )
                    except OSError:
                        extra.setdefault(rel, []).append("unreadable script: open failed")
                        continue
                    try:
                        # The OPENED descriptor must be the same regular inode
                        # the stat above described: a name swapped between the
                        # stat and the open (FIFO, device, replaced file) fails
                        # the verdict closed instead of being read as if it
                        # were the audited entry.
                        ost = os.fstat(rfd)
                        if not stat.S_ISREG(ost.st_mode) or (ost.st_ino, ost.st_dev) != (
                            est.st_ino,
                            est.st_dev,
                        ):
                            extra.setdefault(rel, []).append(
                                "invalid layout: entry changed during scan"
                            )
                            continue
                        raw = os.read(rfd, MAX_SCRIPT_BYTES + 1)
                    except OSError:
                        extra.setdefault(rel, []).append("unreadable script: read failed")
                        continue
                    finally:
                        os.close(rfd)
                    try:
                        scripts.append({"filename": rel, "content": raw.decode("utf-8")})
                    except UnicodeDecodeError:
                        extra.setdefault(rel, []).append("unreadable script: not valid UTF-8")

        try:
            try:
                _walk(scripts_fd, "", 0)
            finally:
                os.close(scripts_fd)
            if budget["breached"]:
                # A breached budget means the LIST path declined to do the
                # work, not that approve will refuse: approve's collector is
                # unbudgeted, so a candidate with 65 small clean scripts
                # approves fine. Claiming `ok: false` here makes the badge and
                # hint over-promise a refusal that never comes — the honest
                # verdict is NO verdict (the caller omits the field, exactly
                # like a platform that cannot pin the walk). The exhaustion
                # defense is unchanged: the walk already STOPPED at its budget.
                return None
            v_ok, v_report = validate_scripts(scripts)
            if v_ok:
                # Approve validates TWICE: raw, then again after redacting in
                # place (a credential-shaped token redacts into broken
                # syntax and is refused). Mirror the second stage on in-memory
                # copies so a script that is clean raw but breaks under
                # redaction does not read `ok: true` for a click approve
                # refuses. Verdict-side only — nothing on disk is touched.
                redacted = [
                    {"filename": s["filename"], "content": self._redact_text(s["content"])}
                    for s in scripts
                ]
                v_ok, v_report = validate_scripts(redacted)
        except Exception:
            # One broken candidate must not blank the caller's whole pending
            # list, and an unvalidated candidate must not read clean: degrade
            # to a failing verdict-unavailable finding.
            return False, {"<candidate>": ["verdict unavailable: unexpected walk error"]}
        for fn, findings in extra.items():
            v_report.setdefault(fn, []).extend(findings)
        return (v_ok and not extra), v_report

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
        # Same hardened verdict as the pending LIST (descriptor-pinned walk,
        # fail-closed on unreadable/oversized entries) — deriving it from the
        # display collection instead would let a silently omitted unreadable
        # script present a clean detail verdict while approve refuses. The
        # display ``scripts`` list below is unchanged. ``None`` (platform
        # cannot compute a trustworthy verdict) omits the field rather than
        # serving a false all-clear.
        verdict = self._pending_scripts_verdict(pdir)
        for s in scripts:
            s["filename"] = self._redact_text(s.get("filename", ""))
            s["content"] = self._redact_text(s.get("content", ""))
        detail = {
            "slug": slug,
            "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{slug}"),
            "meta": meta,
            "kind": meta.get("kind", "new"),
            "target": meta.get("target"),
            "base_version": meta.get("base_version"),
            "content": self._redact_text(skill_file.read_text(encoding="utf-8")),
            "scripts": scripts,
        }
        if verdict is not None:
            v_ok, v_report = verdict
            detail["script_validation"] = {
                "ok": v_ok,
                "report": self._redact_validation_report(v_report),
            }
        return detail

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
        # One copy of the rule: the verdict's precheck reads this same set, so
        # an entry added here is automatically predicted by the badge.
        _allowed_top = self._ALLOWED_CANDIDATE_TOP
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

    def _validate_and_redact_candidate(self, src: Path, name: str) -> dict[Path, bytes]:
        """Re-validate + redact a candidate's SKILL.md and scripts IN PLACE.

        Shared by ``approve_pending_skill`` and ``approve_pending_update`` so
        both enforce the identical discipline: validate every script (covers
        crystallize direct-writes), snapshot each target's ORIGINAL bytes, redact
        in place, then re-validate scripts (redacting a credential-shaped token
        can break syntax). On ANY failure the originals are restored and a
        ``PendingApprovalRefused`` is raised naming the reason — a validation
        failure carries the redacted ``{filename: [findings]}`` report so the
        refusal can tell the reviewer WHAT was flagged — and a rejected
        candidate is never left corrupted. On success returns the
        ``{path: original_bytes}`` snapshot so the caller can restore on a
        LATER failure (e.g. a failed move / snapshot).
        """
        sdir_src = src / "scripts"
        # Pre-redaction script validation.
        if sdir_src.is_dir():
            ok, report = validate_scripts(self._collect_scripts(sdir_src))
            if not ok:
                logger.warning("Refusing to approve %s: script validation failed: %s", name, report)
                raise PendingApprovalRefused(
                    "script_validation_failed", report=self._redact_validation_report(report)
                )
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
                raise PendingApprovalRefused("redaction_failed")
        # Re-validate scripts AFTER redaction so a broken/altered helper never
        # goes live and the pending draft is not corrupted.
        if sdir_src.is_dir():
            ok, report = validate_scripts(self._collect_scripts(sdir_src))
            if not ok:
                _restore_redacted()
                logger.warning(
                    "Refusing to approve %s: scripts invalid after redaction: %s", name, report
                )
                raise PendingApprovalRefused(
                    "script_validation_failed", report=self._redact_validation_report(report)
                )
        return redact_backup

    def _redact_validation_report(self, report: dict) -> dict:
        """Bound and redact reports at retention for every pending HTTP surface.

        Keep at most the verdict's script-entry budget, with independently
        bounded finding lists and strings. Redact BEFORE shortening strings:
        cutting first could turn a credential into an unrecognised fragment.
        One fixed summary reports omitted entries (including redacted-key
        collisions), findings within retained entries, and characters within
        retained strings. Entries beyond the population cap are never redacted
        or copied.
        """
        safe: dict[str, list[str]] = {}
        omitted_entries = max(0, len(report) - _PENDING_SCRIPT_MAX_ENTRIES)
        omitted_findings = 0
        omitted_chars = 0
        for fn, findings in islice(report.items(), _PENDING_SCRIPT_MAX_ENTRIES):
            redacted_key = self._redact_text(str(fn))
            key = redacted_key[:_VALIDATION_REPORT_MAX_STRING_CHARS]
            # Redaction and shortening can collapse distinct filenames. Never
            # overwrite a prior entry or let a filename steal the summary slot.
            if key in safe or key == _VALIDATION_REPORT_TRUNCATION_KEY:
                omitted_entries += 1
                continue
            omitted_chars += len(redacted_key) - len(key)
            values: list[str] = []
            omitted_findings += max(0, len(findings) - _VALIDATION_REPORT_MAX_FINDINGS)
            for finding in islice(findings, _VALIDATION_REPORT_MAX_FINDINGS):
                redacted = self._redact_text(str(finding))
                value = redacted[:_VALIDATION_REPORT_MAX_STRING_CHARS]
                omitted_chars += len(redacted) - len(value)
                values.append(value)
            safe[key] = values
        if omitted_entries or omitted_findings or omitted_chars:
            safe[_VALIDATION_REPORT_TRUNCATION_KEY] = [
                "too large: validation report truncated; "
                f"omitted {omitted_entries} script entries, "
                f"{omitted_findings} findings from retained entries, "
                f"{omitted_chars} characters from retained strings"
            ]
        return safe

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
        raw = self._cached_frontmatter(skill_file, within=None).get("version", "")
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
        not an update, or its target is not a live auto-skill. Read-only:
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
        _live_fm = self._cached_frontmatter(live_file, within=None)
        proposed_body = self._rewrite_update_frontmatter(
            cand_body,
            target_name=target_name,
            created_at=_live_fm.get("created_at", ""),
            version=current_version + 1,
            pinned=str(_live_fm.get("pinned", "")).strip().lower() in ("true", "1", "yes"),
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
        """``approve_pending_update_checked`` with the legacy ``None`` contract.

        Existing callers branch on ``None`` for "refused for any reason"; the
        checked variant raises ``PendingApprovalRefused`` so the dashboard can
        report WHY. This wrapper keeps their behaviour unchanged.
        """
        try:
            return self.approve_pending_update_checked(slug)
        except PendingApprovalRefused:
            return None

    def approve_pending_update_checked(self, slug: str) -> str:
        """Promote a pending UPDATE candidate over its live target auto-skill.

        Preconditions (all checked BEFORE any live mutation; a failure here
        leaves BOTH the live skill and the candidate untouched and raises
        ``PendingApprovalRefused`` with the reason): the slug is safe, the
        candidate has a ``SKILL.md``, its ``.meta.json`` has
        ``kind == "update"``, and ``target`` names an EXISTING live auto
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
            raise PendingApprovalRefused("not_found")
        src = self._pending_root() / slug
        if not (src / "SKILL.md").exists():
            raise PendingApprovalRefused("not_found")
        meta = self._read_pending_meta(slug)
        if meta.get("kind") != "update":
            raise PendingApprovalRefused("not_found")
        target = meta.get("target")
        if not isinstance(target, str) or not target:
            raise PendingApprovalRefused("target_missing")
        target_slug = self._auto_slug_from_name(target)
        if not self._is_pending_slug_safe(target_slug):
            raise PendingApprovalRefused("target_missing")
        live_dir = self._dir / AUTO_SKILL_NAMESPACE / target_slug
        live_skill = live_dir / "SKILL.md"
        if not live_skill.exists():
            logger.warning(
                "Refusing to approve update %s: target %r is not a live auto skill", slug, target
            )
            raise PendingApprovalRefused("target_missing")
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
            raise PendingApprovalRefused("invalid_layout")
        # Shared symlink + unexpected-entry rejection.
        if not self._candidate_layout_ok(src, target_name):
            raise PendingApprovalRefused("invalid_layout")
        # One verdict, two surfaces: same consult as
        # ``approve_pending_skill_checked`` — a computable failing verdict
        # refuses with the badge's own report, so the update click can never
        # disagree with the badge it stood behind.
        _fold = self._pending_scripts_verdict(src)
        if _fold is not None:
            _fold_ok, _fold_report = _fold
            if not _fold_ok:
                logger.warning(
                    "Refusing to approve update %s: the pending verdict this candidate's "
                    "badge serves reports failing findings",
                    target_name,
                )
                raise PendingApprovalRefused(
                    "script_validation_failed",
                    report=self._redact_validation_report(_fold_report),
                )
        # Re-validate + redact the candidate in place (restores originals on
        # fail, raising PendingApprovalRefused with the reason).
        redact_backup = self._validate_and_redact_candidate(src, target_name)

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
            raise PendingApprovalRefused("promotion_failed")
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
            raise PendingApprovalRefused("stale_base")
        live_created_at = self._cached_frontmatter(live_skill, within=None).get("created_at", "")
        # Carry the live skill's pin forward: a pinned skill is exempt from the
        # lifecycle's inactivity / max-N archival, and silently dropping the flag
        # here would expose a user-pinned skill to being archived.
        live_pinned = str(
            self._cached_frontmatter(live_skill, within=None).get("pinned", "")
        ).strip().lower() in ("true", "1", "yes")
        # Same for the injection opt-out: the candidate never carries it, so
        # writing it over live without this would silently turn full-body
        # injection back on for a skill the user had made pointer-only.
        live_pointer_only = (
            str(self._cached_frontmatter(live_skill, within=None).get("inject_on_trigger", ""))
            .strip()
            .lower()
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
            raise PendingApprovalRefused("promotion_failed")
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
            logger.warning(
                "Refusing to approve update %s: could not write live SKILL.md", target_name
            )
            raise PendingApprovalRefused("promotion_failed")
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
                raise PendingApprovalRefused("promotion_failed")
        # (h) Prune version history to the cap.
        self._prune_versions(versions_dir)
        # (i) Remove the pending candidate.
        # Captured BEFORE the removal so a same-slug replacement staged after
        # this instant keeps its notification (see approve_pending_skill).
        consumed_at = datetime.now(tz=timezone.utc).isoformat()
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
        # The candidate cleanup above ignores rmtree errors (e.g. a Windows
        # file lock), so the candidate can survive in the pending queue even
        # though the update went live. Only report it consumed when the
        # directory is really gone — otherwise the queue still shows an
        # actionable review and its notification must stay unread.
        if not src.exists():
            _emit_pending_consumed(
                {
                    "slug": slug,
                    "outcome": "approved",
                    "name": target_name,
                    "consumed_at": consumed_at,
                }
            )
        return target_name

    def approve_pending_skill(self, slug: str) -> str | None:
        """``approve_pending_skill_checked`` with the legacy ``None`` contract.

        Existing callers branch on ``None`` for "refused for any reason"; the
        checked variant raises ``PendingApprovalRefused`` so the dashboard can
        report WHY. This wrapper keeps their behaviour unchanged.
        """
        try:
            return self.approve_pending_skill_checked(slug)
        except PendingApprovalRefused:
            return None

    def approve_pending_skill_checked(self, slug: str) -> str:
        """Promote a pending candidate to a live auto-skill.

        Re-validates + redacts the candidate, then moves ``auto/.pending/<slug>``
        → ``auto/<slug>`` and marks any bundled scripts executable. Returns the
        live name; raises ``PendingApprovalRefused`` (with the reason, and the
        redacted findings report for a validation failure) if the candidate is
        missing, a live skill of that name already exists, it contains a
        symlink, script validation fails, or redaction fails. Every check runs
        BEFORE the move, so a rejected candidate is left untouched in the
        pending queue.
        """
        if not self._is_pending_slug_safe(slug):
            raise PendingApprovalRefused("not_found")
        src = self._pending_root() / slug
        if not (src / "SKILL.md").exists():
            raise PendingApprovalRefused("not_found")
        # An UPDATE candidate must never be consumed down this path: promoting
        # it fresh would create ``auto/<candidate-slug>`` while its live target
        # stays unchanged — the update silently never lands. The HTTP handler
        # routes on the candidate detail's ``kind``, but a raising detail read
        # drops that to ``None`` and defaults HERE, so the guard has to live at
        # the consumption point. ``kind_mismatch`` (not ``not_found``): the
        # candidate exists and is approvable via its own path, and the
        # not-found recovery copy ("approved or dismissed elsewhere") would be
        # a lie for a still-pending candidate.
        if self._read_pending_meta(slug).get("kind") == "update":
            logger.warning(
                "Refusing to approve %s as a NEW skill: candidate metadata marks it an update",
                slug,
            )
            raise PendingApprovalRefused("kind_mismatch")
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        dest = self._dir / name
        if dest.exists():
            logger.warning("Cannot approve %s: a live skill already exists", name)
            raise PendingApprovalRefused("live_exists")
        # Reject any symlink in the candidate + any unexpected top-level entry
        # (defense-in-depth on top of the mandatory human review); promotion +
        # chmod must only touch known, real files. Factored into a shared helper
        # so the update-approve path enforces the identical layout guard.
        if not self._candidate_layout_ok(src, name):
            raise PendingApprovalRefused("invalid_layout")
        # One verdict, two surfaces: approve consults the SAME verdict function
        # the pre-click badge serves. Where a verdict is computable, a failing
        # one refuses HERE with the verdict's own report, so the badge and the
        # click cannot disagree on any candidate the badge judged — an
        # ``ok: false`` badge is a refusal by construction, not by parallel
        # re-derivation. ``None`` (no pinned opens, or a budget breach) keeps
        # this path's own machinery as the sole authority, exactly the
        # authority split the verdict's contract documents.
        _fold = self._pending_scripts_verdict(src)
        if _fold is not None:
            _fold_ok, _fold_report = _fold
            if not _fold_ok:
                logger.warning(
                    "Refusing to approve %s: the pending verdict this candidate's badge "
                    "serves reports failing findings",
                    name,
                )
                raise PendingApprovalRefused(
                    "script_validation_failed",
                    report=self._redact_validation_report(_fold_report),
                )
        # Re-validate every script + redact the body + scripts before going live;
        # snapshots each file first so a failure restores the ORIGINAL bytes and
        # never leaves a corrupted pending draft (raising PendingApprovalRefused
        # with the reason). Shared with the update path.
        redact_backup = self._validate_and_redact_candidate(src, name)

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
            raise PendingApprovalRefused("promotion_failed")
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
            raise PendingApprovalRefused("promotion_failed")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Cutoff for notification resolution, captured BEFORE the candidate
        # leaves the pending queue: staging refuses to overwrite an existing
        # candidate, so a same-slug replacement can only be staged after this
        # instant — its notification carries a strictly later ``ts`` and must
        # survive the resolve.
        consumed_at = datetime.now(tz=timezone.utc).isoformat()
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
            raise PendingApprovalRefused("promotion_failed")
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
        _emit_pending_consumed(
            {"slug": slug, "outcome": "approved", "name": name, "consumed_at": consumed_at}
        )
        return name

    def dismiss_pending_skill(self, slug: str) -> bool:
        """Delete a pending candidate. Returns True if it existed."""
        if not self._is_pending_slug_safe(slug):
            return False
        pdir = self._pending_root() / slug
        if not pdir.is_dir():
            return False
        # Captured BEFORE the removal so a same-slug replacement staged after
        # this instant keeps its notification (see approve_pending_skill).
        consumed_at = datetime.now(tz=timezone.utc).isoformat()
        shutil.rmtree(pdir)
        logger.info("Dismissed pending skill: %s", slug)
        _emit_pending_consumed({"slug": slug, "outcome": "dismissed", "consumed_at": consumed_at})
        return True

    def dismiss_all_pending(self) -> int:
        """Delete all pending candidates. Returns count dismissed."""
        pending = self.list_pending_skills()
        count = 0
        for entry in pending:
            if self.dismiss_pending_skill(entry["slug"]):
                count += 1
        if count:
            logger.info("Dismissed all %d pending skills", count)
        return count

    def dismiss_pending_slugs(self, slugs: list[str]) -> int:
        """Delete only the specified pending candidates. Returns count dismissed."""
        count = 0
        for slug in slugs:
            if self.dismiss_pending_skill(slug):
                count += 1
        if count:
            logger.info("Dismissed %d of %d requested pending skills", count, len(slugs))
        return count

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

    def get_always_skills(self, project_dir: str | Path | None = None) -> list[str]:
        """Return names of skills marked ``always: true`` in frontmatter.

        *project_dir* is the session's active project, used only by the
        ``repo_scope`` gate; omitting it suppresses every repo-scoped skill
        (see :meth:`_repo_scope_satisfied` for why the gate cannot fall back
        to the process working directory).
        """
        result: list[str] = []
        for name, skill_file, _within in self._iter_visible(project_dir):
            meta = self._cached_frontmatter(skill_file, within=_within)
            if meta.get("always", "").strip().lower() == "true":
                # Stripped so a whitespace-only value means "no scope" here exactly as it
                # does at the other two gate call sites. The guard below tests this
                # value's TRUTHINESS, and `repo_scope: |` over a blank line now resolves
                # to a break rather than to "" -- truthy, so the gate would be handed
                # whitespace and refuse it, suppressing a skill its author never scoped.
                # A trailing break on a real path is NOT the concern:
                # `project_scope_satisfied` strips its own fragment, so `src/x\n` was
                # always gated as `src/x`.
                scope = meta.get("repo_scope", "").strip()
                if scope and not self._repo_scope_satisfied(scope, project_dir):
                    continue
                result.append(name)
        return result

    def sync_builtins(self) -> None:
        """Run the builtin-skill sync for this loader's directory.

        The explicit seam for callers that own an off-loop context (the
        gateway runs this in a worker thread as a background task after the
        dashboard socket binds). Construction-time sync skips itself on a
        running event loop, so without this seam a loop-thread process would
        have no way to sync at all.
        """
        _ensure_builtin_skills(self._dir)

    def get_triggered_skills(
        self,
        text: str,
        project_dir: str | Path | None = None,
        *,
        select: Callable[[], list[str] | None] | None = None,
    ) -> list[str]:
        """Return names of skills whose triggers match the given text.

        Uses word-overlap matching with multi-word trigger phrases and
        negative keywords.  Triggers are comma-separated phrases in the
        ``triggers`` frontmatter field.  A phrase prefixed with ``!`` is a
        negative trigger — if *any* negative trigger matches, the skill is
        excluded regardless of positive matches.

        *project_dir* is the session's active project, used only by the
        ``repo_scope`` gate; omitting it suppresses every repo-scoped skill.

        *select*, when given, may replace the matched set before the audit row
        is written; it returns ``None`` to keep the match. It is a callable, not
        a list, so the caller pays for it only when the match is being audited.

        Returns up to ``max_triggered`` skills sorted by best overlap score.
        """
        text_words = words_of(text)
        scored: list[tuple[str, float]] = []
        # Skills a negative trigger actively excluded — a permission DENY that
        # must still be audited (see the audit event below).
        negated_skills: list[str] = []
        for name, skill_file, _within in self._iter_visible(project_dir):
            meta = self._cached_frontmatter(skill_file, within=_within)
            if meta.get("always", "").strip().lower() == "true":
                continue
            triggers = meta.get("triggers", "")
            if not triggers:
                continue
            # Repo-scoped skills are mechanically suppressed outside their
            # repo — word-overlap can fire on ordinary user phrasing, and a
            # prose scope guard alone is probabilistic. Stripped so a
            # whitespace-only value reads as "no scope" at every gate call site
            # (see the always-on lister for why the truthiness test needs it).
            scope = meta.get("repo_scope", "").strip()
            if scope and not self._repo_scope_satisfied(scope, project_dir):
                continue

            # Scored by the shared primitive, not here: crew routing scores the
            # same trigger grammar, and two implementations would agree on the
            # easy cases and diverge on the ones that matter. `negated` stays
            # separate from the score because the DENY audit below has to tell
            # "scored nothing" apart from "scored well and was vetoed".
            best_overlap, negated = trigger_score(triggers, text_words)

            # Only record a negation as a DENY when the skill would otherwise
            # have triggered (positive overlap met the threshold) — that's the
            # case where the negative trigger actually changed the outcome.
            if negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                negated_skills.append(name)
            elif not negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                scored.append((name, best_overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        triggered = [name for name, _ in scored[: self._max_triggered_now()]]

        # An external *select* runs BEFORE the audit below so the one row records
        # what is actually injected. Its three readings: a list replaces the
        # trigger match, ``[]`` is a real "no skill applies" that empties it, and
        # ``None`` (off, unusable answer, failure, expired budget) keeps it.
        selected = None
        if select is not None:
            try:
                selected = select()
            except Exception as exc:
                logger.debug("skills.select: selection failed (%s)", type(exc).__name__)
            if selected is not None:
                triggered = list(selected)

        # Emit ONE audit event for the matched + denied sets rather than one per
        # skill. A SEL entry per skill (incl. every non-match) on every message
        # would be N synchronous writes that dominate the per-message cost.
        # The security-relevant signals are which
        # skills were injected (permission grant) and which were excluded by a
        # negative trigger (permission deny); both are captured here. Skipped
        # entirely only when nothing triggered or was denied and no selection
        # ran (the common case): a selection that emptied the match is a row.
        if triggered or negated_skills or selected is not None:
            metadata = {"text_hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
            if triggered or selected is not None:
                metadata["skills"] = ",".join(triggered)
                # Record HOW each match was delivered, not just that it matched.
                # A pointer is an offer the agent may decline, so an auditor
                # reconstructing "was this procedure actually in the prompt?"
                # needs the split — the skill list alone does not answer it.
                bodies, pointers = self.split_triggered(triggered, project_dir)
                metadata["bodies"] = ",".join(bodies)
                metadata["pointers"] = ",".join(pointers)
            if selected is not None:
                # Which mechanism chose: an auditor reading an empty or widened
                # set needs to know it was a selection, not a matcher change.
                metadata["selected"] = "true"
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

    def split_triggered(
        self, names: list[str], project_dir: str | Path | None = None
    ) -> tuple[list[str], list[str]]:
        """Split matched *names* into (inject-body, pointer-only), order preserved.

        Full-body injection is the DEFAULT: a matched skill's procedure lands in
        the prompt whether or not the agent chooses to read a file. An
        unconfined skill opts out with ``inject_on_trigger: false``, which
        reduces its contribution to a single pointer line naming it and its
        path. Confined project skills always inject their body: handing the
        agent a live path would bypass the descriptor-confined reader if the
        checkout replaced ``SKILL.md`` after discovery.

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
            # project_dir must reach here: get_triggered_skills can match a
            # trusted project's own skill, and resolving project-blind would
            # return None and DROP it — no body and no pointer, so a matched
            # skill would silently contribute nothing.
            found = self._resolve_path_and_root(name, project_dir)
            if found is None:
                continue
            skill_file, within = found
            meta = self._cached_frontmatter(skill_file, within=within)
            if within is not None:
                enforced.append(name)
            elif meta.get("inject_on_trigger", "").strip().lower() == "false":
                pointer_only.append(name)
            else:
                enforced.append(name)
        return enforced, pointer_only

    def trigger_hint(self, names: list[str], project_dir: str | Path | None = None) -> str:
        """Return a pointer block naming *names* and where to read each one.

        The counterpart to :meth:`get_triggered_skills` for an unconfined skill
        that opted out of full-body injection with ``inject_on_trigger: false``:
        the matcher decides which skills look relevant, and this renders that
        verdict as one line per skill instead of the skill's body. A body costs
        8k-34k chars and is charged again on every turn the match repeats; a line
        costs ~150. Confined project skills are omitted defensively because the
        agent would follow the path outside the confined reader.

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
            # project_dir must reach here for the same reason it must reach
            # split_triggered: a trusted project's own skill can match, and
            # resolving project-blind drops it -- the pointer block would name
            # nothing and the operator would see a match that led nowhere.
            found = self._resolve_path_and_root(name, project_dir)
            if found is None:
                continue
            skill_file, within = found
            if within is not None:
                continue
            meta = self._cached_frontmatter(skill_file, within=within)
            desc = self._short_desc(meta.get("description", "") or name, suffix="…")
            lines.append(f"- **{meta.get('name', name)}**: {desc} → `{skill_file}`")
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

    def _resolve_path(self, name: str, project_dir: str | Path | None = None) -> Path | None:
        """Return the ``SKILL.md`` path for an enumerated skill *name*.

        Allowlist-only, like ``resolve_dollar_skills``: the path comes from the
        enumeration rather than being constructed from *name*, so a crafted
        name cannot escape the skill roots.

        Prefer :meth:`_resolve_path_and_root` when the path will be READ — the
        root a path is confined to is decided by the enumeration, and a caller
        that only has the path would have to guess it.
        """
        resolved = self._resolve_path_and_root(name, project_dir)
        return resolved[0] if resolved else None

    def _resolve_path_and_root(
        self, name: str, project_dir: str | Path | None = None
    ) -> tuple[Path, str | None] | None:
        """The enumerated path for *name* PLUS the root it is confined to.

        The enumeration is the only place containment is knowable, so it is also
        the only place that may answer this. Handing both back together is what
        stops a reader from inventing a root, or from reading with none.
        """
        for candidate, skill_file, within in self._iter(project_dir):
            if candidate == name:
                return skill_file, within
        return None

    def get_context(
        self,
        budget: int | None = None,
        only: list[str] | None = None,
        project_dir: str | Path | None = None,
        project_body_budget: int | None = None,
        *,
        discovery_only: bool = False,
        required_parts_out: list[str] | None = None,
    ) -> str:
        """Build a bounded directory over the agent's resolved available set.

        Mapping grants availability, not eager body delivery. Ordinary global and
        confined skills load on demand through scoped search/list/read. Required
        ``always:true`` bodies share PINNED_SKILL_BODIES_CAP; exceeding it raises
        SkillContextCapacityError instead of silently dropping instructions.

        ``budget`` bounds optional discovery characters. ``discovery_only`` selects
        the shorter pointer; both variants expose complete paginated discovery.
        ``required_parts_out`` separates complete required bodies for the caller's
        protected-content admission. ``only=[]`` admits nothing. The explicit
        ``budget=None`` catalog reader retains its legacy unbudgeted rendering.
        """
        all_skills = self.scoped_skills(project_dir=project_dir, only=only)
        # Scope BEFORE anything is rendered. Dropping a repo-scoped skill only
        # from the injected body still leaves its summary line in the index, and
        # the index tells the agent to read the full file for anything related —
        # so an out-of-scope skill stays one `cat` away and its repo-specific
        # procedure gets applied to the wrong project. Filtering the list is the
        # single place that covers the index, both renderers, and the pinned set.
        # Collapse verified byte-identical copies of the same skill before
        # anything is rendered, for the same reason the scope filter above
        # lives here: this is the single place that covers the index, both
        # renderers, and the pinned set. Multi-root installs commonly
        # materialize one skill twice — a package tree and a flat mirror of it
        # — at different key depths, so `_iter_uncached`'s per-key shadowing
        # never sees the collision and the injected index carries N identical
        # summary lines (and, for a pinned skill, N identical full bodies).
        # Dropping a copy is only safe when the bytes are the same, and
        # `_dedupe_identical_skills` verifies exactly that: same-metadata rows
        # whose content differs are all kept.
        all_skills = _dedupe_identical_skills(all_skills)
        # A first-run scope whose discovery pass has not finished enumerates to
        # nothing, and nothing is indistinguishable from a machine with no skills.
        # That matters here and nowhere else: `always: true` bodies are REQUIRED
        # instructions, so returning "" would drop them with no signal — the one
        # outcome this module refuses everywhere else (see SkillContextCapacityError,
        # which fails loudly rather than trimming a required body). A first run
        # cannot know which skills are `always: true` without enumerating, so the
        # honest answer is neither to block the turn on the walk nor to present a
        # truncated set as complete: state that discovery is still running, so the
        # agent knows its instructions may be incomplete and can re-read them once
        # it finishes. Emitted whether or not mapped rows made `all_skills`
        # non-empty, because an incomplete catalog is incomplete either way.
        notice = (
            _DISCOVERY_IN_PROGRESS_NOTICE if self.catalog_status(project_dir) == "building" else ""
        )
        if not all_skills:
            return notice
        if budget is None:
            return notice + self._legacy_context(
                all_skills,
                restricted=only is not None,
                project_dir=project_dir,
                project_body_budget=project_body_budget,
            )
        # get_always_skills() returns the _iter() identifier — the same value
        # list_skills() exposes as "key" (the dir-relative path, e.g.
        # "team-capabilities/build-helper"), NOT the frontmatter "name". So the
        # pinned check below, _record_use() (also called with the _iter
        # identifier), and _rank_key()'s score(s["key"]) are all consistently
        # keyed by "key" — there is no key/name mismatch here.
        pinned = {str(s["key"]) for s in all_skills if s.get("always")}

        parts: list[str] = []
        pinned_spent = 0

        # Pinned global skills: full content, always injected.
        # A confined path must never be offered to the agent for a later direct
        # read, because that read would sit outside the descriptor-pinned gate.
        for s in all_skills:
            if s["key"] not in pinned:
                continue
            remaining = max(0, PINNED_SKILL_BODIES_CAP - pinned_spent)
            if s.get("confine_root"):
                remaining = min(remaining, PROJECT_SKILL_BODY_CAP)
            content = self.read_scoped_skill(
                str(s["key"]), only=only, project_dir=project_dir, max_bytes=remaining
            )
            if content is None:
                detail = (
                    f"the {PROJECT_SKILL_BODY_CAP}-byte per-project-skill limit, "
                    if s.get("confine_root")
                    else ""
                )
                raise SkillContextCapacityError(
                    f"Required skill {s['key']!r} could not be loaded within {detail}"
                    f"the {PINNED_SKILL_BODIES_CAP}-byte total startup instruction "
                    "capacity, or its file was unreadable. Reduce always:true skills "
                    "or their bodies and verify the file before retrying."
                )
            stripped = self.strip_frontmatter(content)
            rendered = f"### Skill: {s['key']}\n\n{stripped}"
            pinned_spent += len(rendered.encode("utf-8")) + 8
            if pinned_spent + 64 > PINNED_SKILL_BODIES_CAP:
                raise SkillContextCapacityError(
                    f"Required skills exceed the {PINNED_SKILL_BODIES_CAP}-byte "
                    "startup instruction capacity; reduce always:true skills."
                )
            parts.append(rendered)

        def wrap(items: list[str]) -> str:
            if not items:
                return ""
            return "[Skills:]\n" + "\n\n---\n\n".join(items) + "\n[End of skills]\n\n"

        required = wrap(parts)
        if required_parts_out is not None:
            if required:
                required_parts_out.append(required)
            parts = []
        # Without a split consumer, required bodies still spend the total
        # allowance before optional entries. They are never clipped to fit it.
        # The discovery notice is charged here too, so an incomplete catalog cannot
        # push the assembled block past the caller's budget.
        optional_budget = max(
            0, budget - len(notice) - (len(required) if required_parts_out is None else 0)
        )
        optional: list[str] = []
        on_demand = [s for s in all_skills if s["key"] not in pinned]
        if on_demand and discovery_only:
            pointer = (
                "## Skill discovery\n\n"
                "Use skill_search(query) to search this agent’s available skills. "
                "Use skill_search(action='list', offset=0) to browse all pages and "
                "skill_search(action='read', key='full/key') for exact instructions. "
                "Search returns confined project bodies safely; never read project paths directly. "
                "Read returned instructions before use; $skillname loads an explicit skill.\n"
            )
            # Equal usage ranks fall back to key order so the eight names shown
            # do not depend on directory iteration order.
            by_key = sorted(on_demand, key=lambda s: s["key"])
            named: set[str] = set()
            for skill in sorted(by_key, key=self._rank_key, reverse=True)[:8]:
                line = f"- {skill['key']}: {self._short_desc(skill['description'])[:100]}\n"
                if len(wrap([pointer + line])) <= optional_budget:
                    pointer += line
                    named.add(str(skill["key"]))
            # Families last, and only over what was NOT named: the line exists to
            # cover what the entry hides, so repeating a family whose members are
            # all listed spends the budget saying nothing and can crowd out a
            # family that is genuinely unreachable. A name that did not fit is
            # still hidden, so only an admitted one is excluded here.
            groups = _family_line([s for s in on_demand if str(s["key"]) not in named])
            if groups:
                line = f"More families: {groups}\n"
                if len(wrap([pointer + line])) <= optional_budget:
                    pointer += line
            if len(wrap([pointer])) <= optional_budget:
                optional.append(pointer)
        elif on_demand:
            ranked = sorted(on_demand, key=self._rank_key, reverse=True)
            header = (
                "## Available Skills\n\n"
                "Search this agent's scope with skill_search(query). "
                "Browse all pages with skill_search(action='list', offset=0); "
                "load instructions with skill_search(action='read', key='full/key'). "
                "Read instructions before use; run scripts from the skill directory.\n\n"
            )

            def summary(lines: list[str], hidden: list[dict]) -> str:
                # *hidden* is passed in rather than derived from the row count: the
                # admission loop below SKIPS a row that does not fit and keeps
                # trying later ones, so the admitted rows are not a prefix of
                # `candidates` and a tail slice would name the wrong skills.
                footer: list[str] = []
                if hidden:
                    footer.append(
                        f"- _...and {len(hidden)} more skill(s) not shown here. Find them "
                        "with scoped search or paginated list above._"
                    )
                    families = _family_line(hidden)
                    if families:
                        footer.append(f"- _Families not shown: {families}._")
                return header + "\n".join(lines + footer)

            candidates = [
                f"- **{s['name']}**: {self._short_desc(s['description'])} -> "
                + (f"skill_search('{s['key']}')" if s.get("confine_root") else f"`{s['path']}`")
                for s in ranked
            ]
            lines: list[str] = []
            admitted: list[int] = []
            hidden: list[dict] = []
            if len(wrap(optional + [summary(candidates, [])])) <= optional_budget:
                # A complete index needs no omission footer; reserve none when
                # it fits, including the exact-fit boundary.
                lines = candidates
            else:
                remaining = optional_budget - len(wrap([summary([], ranked)]))
                for index, candidate in enumerate(candidates):
                    cost = len(candidate) + 1
                    if cost <= remaining:
                        admitted.append(index)
                        remaining -= cost
                taken = set(admitted)
                lines = [candidates[i] for i in admitted]
                hidden = [s for i, s in enumerate(ranked) if i not in taken]
            block = summary(lines, hidden)
            # Removing rows can change which family labels win the bounded hint.
            # Reconcile that exact footer without slicing a name or instruction.
            while lines and len(wrap(optional + [block])) > optional_budget:
                admitted.pop()
                taken = set(admitted)
                lines = [candidates[i] for i in admitted]
                hidden = [row for i, row in enumerate(ranked) if i not in taken]
                block = summary(lines, hidden)
            if len(wrap(optional + [block])) <= optional_budget:
                optional.append(block)

        return notice + (required if required_parts_out is None else "") + wrap(optional)

    def _legacy_context(
        self,
        all_skills: list[dict],
        restricted: bool = False,
        project_dir: str | Path | None = None,
        project_body_budget: int | None = None,
    ) -> str:
        """Explicit unbudgeted reader, not the default startup path.

        Full content for unconfined pinned (``always: true``) skills, bounded
        bodies for confined project skills, and a one-line summary for every
        unconfined on-demand skill, unranked and untruncated. Project bodies
        replace their unsafe live-path summaries; unconfined skills retain the
        behavior from before lazy loading.

        *restricted* marks *all_skills* as already narrowed by an agent's
        ``skill://`` mapping, so the always-loaded set is narrowed to match: a
        pinned skill outside the mapping must NOT be force-injected, or the
        mapping would not actually bound what the agent sees.

        *project_dir* is forwarded to the ``repo_scope`` gate so this path
        scopes pinned skills exactly as the lazy-load path does — the default
        block must not be the one that leaks a repo-scoped skill.
        """
        always = self.get_always_skills(project_dir)
        if restricted:
            allowed = {s["key"] for s in all_skills} | {s["name"] for s in all_skills}
            always = [a for a in always if a in allowed]
        parts: list[str] = []
        project_skills = [s for s in all_skills if s.get("confine_root")]
        project_keys = {s["key"] for s in project_skills}
        # Full content for unconfined always-loaded skills. Confined pinned
        # skills join every other project row in the bounded loop below.
        for name in always:
            if name in project_keys:
                continue
            content = self.load_skill(name, project_dir)
            if content:
                stripped = self.strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{stripped}")
        self._append_project_skill_bodies(parts, project_skills, project_dir, project_body_budget)
        # Summary for on-demand skills
        on_demand = [s for s in all_skills if s["name"] not in always and not s.get("confine_root")]
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

    def _append_project_skill_bodies(
        self,
        parts: list[str],
        project_skills: list[dict],
        project_dir: str | Path | None,
        budget: int | None,
    ) -> None:
        """Append confined bodies without reading beyond the section budget."""
        wrapper_size = len("[Skills:]\n") + len("\n[End of skills]\n\n")
        separator_size = len("\n\n---\n\n")
        used = wrapper_size + sum(len(part) for part in parts)
        if parts:
            used += separator_size * (len(parts) - 1)

        for skill in project_skills:
            prefix = f"### Skill: {skill['key']}\n\n"
            next_separator = separator_size if parts else 0
            max_bytes: int | None = None
            if budget is not None:
                max_bytes = budget - used - next_separator - len(prefix)
                if max_bytes <= 0:
                    break
                # The enumeration's size is only a hint because the file can be
                # replaced afterward. It avoids opening a file that cannot fit;
                # max_bytes on the descriptor-pinned read closes the race.
                if int(skill.get("size_bytes", 0)) > max_bytes:
                    continue
            content = self.load_skill(skill["key"], project_dir, max_bytes=max_bytes)
            if not content:
                continue
            part = prefix + self.strip_frontmatter(content)
            if budget is not None and used + next_separator + len(part) > budget:
                continue
            parts.append(part)
            used += next_separator + len(part)

    def _record_use(self, key: str) -> None:
        """Best-effort usage bump for the lazy-load ranking. Never raises."""
        if self._usage is None:
            return
        try:
            self._usage.record(key)
        except Exception:  # pragma: no cover — telemetry must not break injection
            pass

    def _recency_boost(self, path_str: str, fingerprint: str = "") -> float:
        """Return the file mtime if the skill is newer than the boost window,
        else 0.0. Lets a freshly-added, never-used skill rank above stale unused
        ones (cold-start protection) without flooding the top of the list.

        The mtime comes from the row's stat *fingerprint* when it carries one, so
        ranking adds no syscall to a turn: ``list_skills`` already recorded that
        stat, or reused the one the catalog walk took. Ranking every row on the
        calling thread was the second O(N) stat pass a message paid, next to the
        metadata validation one. A row with no usable fingerprint — a confined
        project row, or a mapped row whose fingerprint carries its mapping root —
        is stat'ed exactly as before.
        """
        mtime, _size = _fingerprint_mtime_and_size(fingerprint) if fingerprint else (None, 0)
        if mtime is None:
            try:
                mtime = Path(path_str).stat().st_mtime
            except OSError:
                return 0.0
        return mtime if (time.time() - mtime) < _NEW_SKILL_BOOST_WINDOW_SECS else 0.0

    def _rank_key(self, s: dict) -> tuple[float, float]:
        """Sort key for on-demand skills: (usage_hits, effective_recency).
        Higher sorts first. Falls back to recency-only if the ledger is absent."""
        boost = self._recency_boost(s["path"], str(s.get("fingerprint") or ""))
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

    def _body_hits(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> dict[str, int]:
        return {
            key: len(hits)
            for key, hits in self._body_matches(skills, terms, live_keys, project_dir).items()
        }

    def _body_matches(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> dict[str, set[str]]:
        """Refresh once per query, with bounded work and explicit incomplete recall."""
        terms = tuple(terms)
        hits: dict[str, set[str]] = {}
        unconfined = [s for s in skills if not s.get("confine_root")]
        fallback = [s for s in skills if s.get("confine_root")]
        indexed = None
        started = time.monotonic()
        if unconfined and self._search_index is not None:
            rows = []
            for skill in unconfined:
                path = str(skill.get("path", ""))
                fingerprint = skill.get("fingerprint") or body_fingerprint(path)
                if fingerprint:
                    rows.append((str(skill["key"]), path, fingerprint))
            # A scoped query cannot evict the other agents' persistent entries.
            deferred = self._search_index.sync(
                rows,
                budget_seconds=0.25,
                canonical_roots={
                    str(s["key"]): str(s["mapping_root"])
                    for s in unconfined
                    if s.get("mapping_root")
                },
            )
            if deferred is not None:
                pending = self._search_index.pending_keys
                self.search_incomplete = bool(pending)
                answered = [str(s["key"]) for s in unconfined if str(s["key"]) not in deferred]
                indexed = self._search_index.body_matches(answered, terms)
                if indexed is not None:
                    fallback += [
                        s
                        for s in unconfined
                        if str(s["key"]) in deferred and str(s["key"]) not in pending
                    ]
        logger.debug("skill body index refresh/query: %.2fms", (time.monotonic() - started) * 1000)
        if indexed is None:
            fallback += unconfined
        else:
            hits.update(indexed)
        deadline = time.monotonic() + 0.25
        for skill in fallback:
            if time.monotonic() >= deadline:
                self.search_incomplete = True
                break
            cap = PROJECT_SKILL_BODY_CAP if skill.get("confine_root") else None
            if cap is not None and int(skill.get("size_bytes", 0)) > cap:
                continue
            if skill.get("mapping_root") is not None:
                body = self._read_global_skill_text(
                    Path(skill["path"]), cap, canonical_root=skill.get("mapping_root")
                )
            else:
                body = self.load_skill(str(skill["key"]), project_dir, max_bytes=cap)
            content = (body or "").lower()
            matched = {term for term in terms if _body_term_hits(content, [term])}
            if matched:
                hits[str(skill["key"])] = matched
        return hits

    def _scoped_entries(
        self,
        project_dir: str | Path | None,
        only: list[str] | None,
    ) -> list[_ScopedSkillEntry]:
        if only == []:
            return []
        entries = [_ScopedSkillEntry(*entry) for entry in self._iter_visible(project_dir)]
        if only is None:
            return entries
        selected = [entry for entry in entries if _matches_any(str(entry[1]), only)]
        known = {os.path.normcase(os.path.abspath(entry[1])) for entry in entries}
        # Mapping paths are spec-owned, never caller-supplied read keys. Walk the
        # literal prefix through the same provider/sensitive-path fence as the
        # global catalog; do not use glob's unconstrained link traversal.
        for pattern in only:
            prefix = Path(pattern)
            while any(char in str(prefix) for char in "*?["):
                prefix = prefix.parent
            catalog_roots = [self._dir, *self._extra_paths]
            if project_dir:
                catalog_roots.append(Path(project_dir) / ".kiro" / "skills")
            if any(
                _within_any(os.path.abspath(prefix), (os.path.abspath(root),))
                for root in catalog_roots
            ):
                # The regular enumerator owns precedence, disabled apps and
                # project consent. A mapping must not re-admit a filtered row.
                continue
            root = prefix.parent if prefix.name == "SKILL.md" else prefix
            if not root.is_absolute() or validate_file_path(str(root)) is None:
                continue
            admitted_roots = (os.path.realpath(root), *_trusted_skill_roots())
            # An ancestor glob must not descend through a catalog's filtered
            # rows or probe a project skills junction before consent admission.
            excluded = tuple(os.path.abspath(path) for path in catalog_roots)
            for _name, path in _iter_skill_files(root, exclude_roots=excluded):
                identity = os.path.normcase(os.path.abspath(path))
                if identity in known or not _matches_any(str(path), [pattern]):
                    continue
                target = os.path.realpath(path)
                admitted_root = next(
                    (
                        candidate
                        for candidate in admitted_roots
                        if _within_any(target, (candidate,))
                    ),
                    None,
                )
                if admitted_root is None:
                    continue
                known.add(identity)
                digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                selected.append(
                    _ScopedSkillEntry(
                        f"mapped/{digest}/{path.parent.name}", path, None, admitted_root
                    )
                )
        return selected

    def scoped_skills(
        self,
        *,
        project_dir: str | Path | None = None,
        only: list[str] | None = None,
    ) -> list[dict]:
        """The same available set for directory, search, list and explicit reads."""
        started = time.monotonic()
        entries = self._scoped_entries(project_dir, only)
        logger.debug(
            "skill enumeration: %.2fms, %d entries",
            (time.monotonic() - started) * 1000,
            len(entries),
        )
        rows = self.list_skills(project_dir, _entries=entries)
        return [
            row
            for row in rows
            if not row.get("repo_scope")
            or self._repo_scope_satisfied(str(row["repo_scope"]), project_dir)
        ]

    def read_scoped_skill(
        self,
        key: str,
        *,
        only: list[str] | None = None,
        project_dir: str | Path | None = None,
        max_bytes: int = 99_000,
    ) -> str | None:
        """Read an exact catalog key, never an ambiguous leaf or caller path."""
        entry = next(
            (entry for entry in self._scoped_entries(project_dir, only) if entry[0] == key), None
        )
        if entry is None:
            content = self._exact_read_while_building(key, only, project_dir, max_bytes)
        elif entry.mapping_root is not None:
            content = self._read_global_skill_text(
                entry.path, max_bytes, canonical_root=entry.mapping_root
            )
        else:
            content = self.load_skill(key, project_dir, max_bytes=max_bytes)
        if content is None:
            return None
        meta = self._parse_frontmatter_text(content)
        if meta.get("repo_scope") and not self._repo_scope_satisfied(
            meta["repo_scope"], project_dir
        ):
            return None
        return content

    def _exact_read_while_building(
        self,
        key: str,
        only: list[str] | None,
        project_dir: str | Path | None,
        max_bytes: int,
    ) -> str | None:
        """Serve a COMPLETE key during an unfinished first walk, or ``None``.

        A cold catalog cannot answer "does this skill exist", so without this an
        exact read on a machine's first run reports a skill that is right there as
        absent. Only reachable while :meth:`catalog_status` says ``building``: once
        the enumeration exists it is the authority, and a second resolution path
        running alongside it is how the two drift apart.

        *key* is never treated as a path. ``_safe_name`` rejects anything that is not
        a plain catalog key, and the candidate is composed from a root this loader
        already owns, so ``../`` reaches nothing. The complete key is required and
        matched namespace-first — ``team-b/review`` composes only
        ``<root>/team-b/review/SKILL.md`` and can never resolve ``team-a/review``.

        Every live gate still applies: the mapping must admit the candidate, a
        disabled app's skill stays hidden, the caller re-checks ``repo_scope``, and
        the body itself is read through :meth:`load_skill`'s fenced readers. The one
        thing withheld is the confined project tier, whose containment is only
        knowable from the enumeration — a project skill therefore waits for the walk
        rather than being read through a path this method composed.
        """
        if self.catalog_status(project_dir) != "building" or not self._safe_name(key):
            return None
        disabled_apps = self._get_disabled_app_names()
        for root in (self._dir, *self._extra_paths):
            candidate = root / key / "SKILL.md"
            if not candidate.is_file():
                continue
            # Precedence is the enumeration's: the first root holding the key wins,
            # so a refusal here is final rather than a reason to try a lower root.
            if only is not None and not _matches_any(str(candidate), only):
                return None
            if disabled_apps and self._owning_app(key, candidate) in disabled_apps:
                return None
            return self.load_skill(key, project_dir, max_bytes=max_bytes)
        return None

    def search_skills(
        self,
        query: str,
        limit: int = 20,
        *,
        project_dir: str | Path | None = None,
        only: list[str] | None = None,
        offset: int = 0,
        browse: bool = False,
    ) -> list[dict]:
        """Rank total query coverage before rarity, metadata preference and usage.

        Partial matches remain available, with stable full keys and pagination.
        An empty browse request lists the complete resolved scope in key order.
        """
        self.search_incomplete = False
        rows = self.scoped_skills(project_dir=project_dir, only=only)
        # An unfinished first walk is the other way this answer can be partial, and
        # the caller cannot tell it from "no match" without being told: `incomplete`
        # already travels to the MCP tool and the dashboard, so the existing signal
        # carries it rather than a second one.
        building = self.catalog_status(project_dir) == "building"
        self.search_incomplete = building
        offset = max(0, offset)
        if browse:
            return sorted(rows, key=lambda s: str(s["key"]))[offset : offset + limit]
        rows = _dedupe_identical_skills(rows)
        terms = sorted(recall_terms((query or "").strip().lower()))
        if not terms:
            return []
        # Ask every term across both surfaces: a metadata hit must not suppress
        # the other query words found only in the procedure.
        matched: dict[str, set[str]] = {}
        meta_counts: dict[str, int] = {}
        frequencies: dict[str, int] = {}
        live = [str(s["key"]) for s in rows if not s.get("confine_root")]
        bodies = self._body_matches(rows, terms, live, project_dir)
        # `_body_matches` owns this flag for the body tier and assigns it outright,
        # so an unfinished catalog — an independent reason the answer is partial —
        # is re-applied rather than left to be overwritten.
        self.search_incomplete = self.search_incomplete or building
        metadata = self._search_index.metadata_matches(terms) if self._search_index else None
        for row in rows:
            key = str(row["key"])
            if metadata is None or not row.get("metadata_indexed"):
                vocabulary = recall_terms(f"{key} {row['name']} {row['description']}".lower())
                meta_hits = {t for t in terms if any(word.startswith(t) for word in vocabulary)}
            else:
                meta_hits = metadata.get(str(row["path"]), set())
            hits = meta_hits | bodies.get(key, set())
            if hits:
                matched[key] = hits
                meta_counts[key] = len(meta_hits)
                for term in hits:
                    frequencies[term] = frequencies.get(term, 0) + 1

        def rank(row: dict) -> tuple:
            key = str(row["key"])
            hits = matched.get(key, set())
            rarity = sum(math.log1p(len(rows) / max(1, frequencies[t])) for t in hits)
            usage = self._usage.score(key)[0] if self._usage else 0.0
            return (-len(hits), -rarity, -meta_counts.get(key, 0), -usage, key)

        candidates = [s for s in rows if str(s["key"]) in matched]
        return sorted(candidates, key=rank)[offset : offset + limit]

    def resolve_dollar_skills(
        self,
        text: str,
        project_dir: str | Path | None = None,
        *,
        only: list[str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Resolve ``$skillname`` tokens in *text* to loadable skills.

        Scans *text* for ``$token`` occurrences (anywhere, multiple allowed) and
        matches a qualified token against its complete key first. An unqualified
        token matches a unique last path segment of an enumerated skill key — so ``$oncall-handover`` resolves the skill whose key is
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

        names = [str(s["key"]) for s in self.scoped_skills(project_dir=project_dir, only=only)]
        exact = {name: name for name in names}
        leaves: dict[str, list[str]] = {}
        for name in names:
            leaves.setdefault(name.rsplit("/", 1)[-1].casefold(), []).append(name)

        resolved: list[tuple[str, str, str]] = []
        seen_names: set[str] = set()
        for match in _DOLLAR_SKILL_PATTERN.finditer(text):
            token = match.group(1)
            matched = exact.get(token)
            if matched is None and "/" not in token:
                choices = leaves.get(token.casefold(), [])
                matched = choices[0] if len(choices) == 1 else None
            if matched is None or matched in seen_names:
                continue
            content = self.read_scoped_skill(matched, only=only, project_dir=project_dir)
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
        instance — and reading it as the setting would make the writer and the
        reader disagree: ``set_inject_on_trigger`` deliberately leaves an indented
        occurrence alone (deleting it would rewrite the author's prose), so
        honoring it here would keep the opt-in from ever taking effect. Ignoring
        indented lines also drops the junk keys a prose line like
        ``  Steps: do x`` would otherwise invent.

        A value that is a YAML block-scalar indicator (``>``, ``|``, with an
        optional chomping ``-``/``+``) is resolved from the indented lines that
        follow it: folded (``>``) folds single breaks to spaces while keeping
        blank-line and more-indented structure, literal (``|``) preserves
        newlines. Without this, the stored value would be the indicator
        character itself and the real content — a multi-line ``description``
        used for routing — would be dropped, leaving the skill unroutable.
        That grammar is pinned as ``frontmatter.SKILL_LOADER``.
        """
        content = path.read_text(encoding="utf-8")
        return parse_frontmatter(content, SKILL_LOADER)

    @staticmethod
    def _parse_frontmatter_text(content: str) -> dict[str, str]:
        """Same grammar as :meth:`_parse_frontmatter`, on text already read.

        Split out so the enumerated-skill path can read through the containment
        choke point and still share one grammar. `_parse_frontmatter` keeps its
        Path signature because it has a legitimate non-skill caller (the Agent SOP
        description reader) that is not subject to skill confinement.
        """
        return parse_frontmatter(content, SKILL_LOADER)

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter from markdown.

        A fence LOCATOR, not a field parser — deliberately outside
        ``kiro_crew.frontmatter``. Its closer grammar matches
        ``frontmatter._COLUMN0_BLOCK_RE`` — the ``column0_fence`` extraction
        that ``frontmatter.SKILL_LOADER`` binds to the skills surface: the
        closer is the first line after the opener that STARTS with ``---`` —
        trailing text on the closer line is tolerated and consumed, and
        an optional carriage return before each fence newline is tolerated the
        way the parser tolerates one. Anything
        the display parser reads as frontmatter must also be stripped here:
        a stricter closer (a ``---`` must-be-followed-by-newline
        grammar) would let a ``---junk`` or ``--- `` closer parse fields in the UI
        while the whole block leaked to the model. Editing either grammar
        means revisiting the other.
        """
        if content.startswith("---"):
            match = re.match(r"^---\r?\n.*?\r?\n---[^\n]*\n?", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content
