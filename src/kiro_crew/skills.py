"""Skills loader — markdown skill files for agent capabilities.

``SkillsLoader`` is the composition root. It owns the loader's state and locks and
delegates most of its rules to the owners in :mod:`kiro_crew.skill_runtime`. What
stays in this file is what repository guards pin to it by path: the
enumerated-read choke point and its readers, the ``repo_scope`` gate sites,
trigger scoring, every redactor call site (the pending-review sink and the
consent-picker catalog), the skill-tree walk, and the packaged-skill sync with its
provenance and currency checks. Trust enforcement stays beside the choke point it
feeds. Every name that moved is still importable from here. The owners read this
module's constants, and every name it imports from another ``kiro_crew`` module,
through it at call time, so a patch of one of those here still reaches the moved
code. A helper that moved is patched on its owner module instead; sibling owners
call it as an attribute of that module, so the patch reaches every caller.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import csv
import difflib
import errno
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice, zip_longest
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Iterator, Literal, NamedTuple

from kiro_crew import hooks as hooks_module
from kiro_crew import pinned_fs, platform_compat, skill_trust

# Some names below are bound for others rather than for this module's own code:
# the owners in ``skill_runtime`` read them through this module at call time, and
# callers may import any of them from here.
from kiro_crew.atomic_write import (  # noqa: F401
    atomic_write,
    fsync_dir,
    fsync_dir_fd,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.constants import (
    AUTO_SKILL_AUTHORITY_PARENT_DIRNAME,
    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
)
from kiro_crew.cron import referenced_skill_names  # noqa: F401
from kiro_crew.dep_sync import normalize as normalize_distribution_name
from kiro_crew.deploy import _SKILLS_DIR as _DEPLOY_SKILLS_DIR
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import (  # noqa: F401
    FileTooLargeError,
    safe_read_file,
    safe_read_file_bytes_nolink,
    validate_file_path,
)
from kiro_crew.memory_recall import recall_terms  # noqa: F401
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.platform_compat import (  # noqa: F401
    PinnedDirectory,
    ensure_owner_rwx_dirs,
    file_lock,
    is_link_or_junction,
    pinned_directory,
    rmtree_force,
)
from kiro_crew.project_scope import project_scope_satisfied
from kiro_crew.release_channel import _DISTRIBUTION_NAME
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_resolved_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel

# The loader's rules live in these owners; the imports below them keep every
# module-level name that moved importable from here, as the same object.
from kiro_crew.skill_runtime import authoring as _authoring
from kiro_crew.skill_runtime import auto_skills as _auto_skills
from kiro_crew.skill_runtime import catalog as _catalog
from kiro_crew.skill_runtime import delivery as _delivery
from kiro_crew.skill_runtime import listing as _listing
from kiro_crew.skill_runtime import read_credit as _read_credit
from kiro_crew.skill_runtime import search as _search
from kiro_crew.skill_runtime import versions as _versions
from kiro_crew.skill_runtime.catalog import (  # noqa: F401
    _GLOB_CHARS,
    _builtin_dir_app_name,
    _canonical_glob,
    _canonical_prefix,
    _disabled_app_names,
    _glob_with_prefix,
    _literal_split,
    _matches_any,
    _project_prefix,
    _StoredCatalog,
    _with_canonical_globs,
)
from kiro_crew.skill_runtime.delivery import _family_line, _namespace_groups  # noqa: F401
from kiro_crew.skill_runtime.listing import (  # noqa: F401
    _dedupe_identical_skills,
    _fingerprint_mtime_and_size,
)
from kiro_crew.skill_runtime.read_credit import (  # noqa: F401
    _shell_segments_reading_content,
    _tool_read_path_candidates,
)
from kiro_crew.skill_runtime.search import SkillSearchReport, _body_term_hits  # noqa: F401
from kiro_crew.skill_search_index import (  # noqa: F401
    SKILL_SEARCH_INDEX_FILENAME,
    SkillSearchIndex,
    body_fingerprint,
)
from kiro_crew.skill_usage import names_skill_file  # noqa: F401  (read_credit reads it via sk)
from kiro_crew.skill_usage import SKILL_USAGE_FILENAME, SkillUsageLedger
from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES, validate_scripts
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

logger = logging.getLogger(__name__)


SKILLS_DIR_NAME = "skills"
# Filesystem latency dominates cold discovery. Bound both active readers and
# submitted work so a large catalog cannot create one thread/future per skill.
_CATALOG_READ_WORKERS = 8
_CATALOG_READ_BATCH = 64


# A SKILL.md whose body is a saved web page is not a skill: its markup would
# land in the prompt. The bounded cache warns about each path about once.
@functools.lru_cache(maxsize=256)
def _warn_html_skill(path: str) -> bool:
    logger.warning("Skipping skill whose SKILL.md body is HTML, not markdown: %s", path)
    return True


def _html_skill_refused(meta: dict, path: object) -> bool:
    # The key holds a colon, so no front-matter line can set it.
    return bool(meta.get("_html:body")) and _warn_html_skill(str(path))


# One script-entry population budget for the pending verdict and API reports.
# Reports may additionally retain ONE fixed truncation-summary entry.
_PENDING_SCRIPT_MAX_ENTRIES = 64
# Depth bound shared by BOTH pinned traversals of a pending candidate -- the
# verdict walk and the detail read. A candidate tree is written directly by an
# agent, so a nesting chain is free to produce; bounding well below Python's
# recursion limit keeps it from turning a read into a crash. One constant because
# the two walks must agree: a tree the verdict already declines to judge must not
# be one the detail read still descends.
_PENDING_SCRIPT_MAX_DEPTH = 8
_VALIDATION_REPORT_MAX_FINDINGS = 16
_VALIDATION_REPORT_MAX_STRING_CHARS = 1024
_VALIDATION_REPORT_TRUNCATION_KEY = "<truncated>"
#: Re-exported from ``trigger_match``, which owns the value and the grammar
#: it belongs to. Kept as a module name because tests and call sites here
#: reference it.
_MIN_TRIGGER_OVERLAP = MIN_TRIGGER_OVERLAP

# Whether skill CRUD can address the skill directory and its SKILL.md relative to
# a pinned parent descriptor. supports_pinned_walk covers the openat capability
# itself; the extras are exactly the OTHER descriptor-relative syscalls the CRUD
# pinned branches issue (create, update and create's rollback in
# ``skill_runtime/authoring.py``, delete here), named one per call site so the
# probe stays derived from the code rather than copied from a neighbour:
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


#: Labels on one family line. A bound rather than a budget trim, so the line
#: cannot grow long enough to push a named skill (which carries the description,
#: the only text saying what a skill does) out of a tight allowance.
_FAMILY_LINE_MAX_LABELS = 6


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
# The startup pointer names this many skills; the ranked index's head is the
# same width so both variants agree on which rows are "the front".
_INDEX_HEAD_SLOTS = 8
# Of those slots, how many the user's own skills may claim on provenance alone.
# Fewer than all of them, so a shipped skill with real usage keeps a name even
# under a large user tree — the mirror of the empty-ledger case this guards.
_INDEX_USER_SLOTS = 6

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
# written through the app is visible at once to the loader that wrote it. The
# fence is per loader: another loader, in this process or another one, that
# already HOLDS a list keeps serving it until this deadline, and only a loader
# that reads the stored snapshot afterwards sees the drop.
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
#: Paths a check admitted for an unconfined read are remembered up to this many,
#: the oldest admission first out. The rows are agent-influenced (the stored
#: snapshot), so the set must not grow with them; an evicted path only costs
#: another check.
_VETTED_READS_MAX = 4096

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
# What one exact-key read may deliver, in UTF-8 bytes. A tool response is cut at
# ``validation.MAX_RESPONSE_LEN`` characters and the cut takes the TAIL, so a body
# that does not fit under that ceiling with its framing would lose its closing
# instructions silently. A larger body is therefore refused whole and served in
# whole-line pages that each fit; the bound is a context budget, not a file limit,
# so it neither grows for a large skill nor shrinks what is on disk. The pinning
# path's ``PINNED_SKILL_BODIES_CAP`` above is the same number; if this capacity
# is ever revisited, the two move together.
SKILL_READ_CAPACITY = 99_000

# Why an exact-key read delivered no body. Three values because the caller acts
# differently on each: a key outside the scope is a lookup to correct, an
# unreadable file is the operator's to repair, and a body over the capacity is
# read again in pages. One sentence naming all three sends the reader after the
# wrong one.
SKILL_READ_OUTSIDE_SCOPE = "outside_scope"
SKILL_READ_UNREADABLE = "unreadable"
SKILL_READ_OVER_CAPACITY = "over_capacity"


class SkillBodyPage(NamedTuple):
    """Whole lines of one skill body, never more than the read's capacity in UTF-8 bytes."""

    content: str
    line_offset: int
    line_count: int
    total_lines: int
    total_bytes: int
    next_offset: int | None


class SkillReadRefusal(NamedTuple):
    """Why an exact-key read delivered nothing, with the numbers its message needs."""

    reason: str
    capacity: int
    size_bytes: int | None = None  # the whole body, when the read measured it
    confined: bool = False  # ``capacity`` is the confined project body cap
    line: int | None = None  # over_capacity: the one line that fits no page
    # outside_scope while the scope's catalog is still building: the key may yet
    # resolve once the walk finishes, so the absence is not conclusive.
    incomplete: bool = False


class _ExactRead(NamedTuple):
    """One pass of the exact-key resolution chain, with its refusal classified.

    ``refusal`` is one of the three read reasons when ``content`` is ``None`` and
    empty when a body was delivered. ``incomplete`` marks an outside-scope miss
    taken while the scope's catalog was still building.
    """

    content: str | None
    refusal: str
    confined: bool
    incomplete: bool = False


def _page_skill_body(
    body: str, *, offset: int | None, limit: int | None, capacity: int
) -> SkillBodyPage | SkillReadRefusal:
    """Deliver ``body`` whole, or the whole lines from ``offset`` that fit ``capacity``.

    Lines rather than bytes: a byte window can split a multi-byte character or a
    sentence, and the file and transcript readers this tool sits beside already
    page in lines, so an agent carries one unit across them. Every returned line
    is complete; the page stops at the last line that still fits. Only ``"\\n"``
    separates lines, because the decoder has already folded every newline form
    to it, so the count matches what a file reader shows for the same file.
    Every size here is of the body AS DELIVERED -- UTF-8 bytes of that decoded
    text -- never of the file on disk: the capacity is a budget on what one
    response carries, and a CRLF checkout is larger on disk than what is sent.

    The body may be as large as the shared file safety cap, so nothing here
    materializes it line by line: the count is a scan, the page start is a walk
    of newline positions, and only the lines of the page itself are copied out.
    """
    total_bytes = len(body.encode("utf-8"))
    if offset is None and limit is None:
        if total_bytes > capacity:
            return SkillReadRefusal(SKILL_READ_OVER_CAPACITY, capacity, size_bytes=total_bytes)
        total_lines = _skill_body_line_count(body)
        return SkillBodyPage(body, 0, total_lines, total_lines, total_bytes, None)
    total_lines = _skill_body_line_count(body)
    start = max(0, offset or 0)
    if start >= total_lines:
        # Past the last line: an empty page that still names the count, which is
        # what the renderer prints for any page with no lines.
        return SkillBodyPage("", start, 0, total_lines, total_bytes, None)
    position = 0
    for _ in range(start):
        position = body.find("\n", position) + 1
    taken: list[str] = []
    spent = 0
    most = None if limit is None else max(1, limit)
    while position < len(body) and (most is None or len(taken) < most):
        newline = body.find("\n", position)
        end = len(body) if newline < 0 else newline + 1
        line = body[position:end]
        size = len(line.encode("utf-8"))
        if spent + size > capacity:
            if not taken:
                return SkillReadRefusal(
                    SKILL_READ_OVER_CAPACITY, capacity, size_bytes=size, line=start
                )
            break
        taken.append(line)
        spent += size
        position = end
    end_line = start + len(taken)
    return SkillBodyPage(
        "".join(taken),
        start,
        len(taken),
        total_lines,
        total_bytes,
        end_line if end_line < total_lines else None,
    )


def _skill_body_line_count(body: str) -> int:
    """Lines in ``body`` as a file reader counts them: a final unterminated line counts."""
    return body.count("\n") + (1 if body and not body.endswith("\n") else 0)


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

# One lock file for the whole auto slug space, held across an availability test
# and the claim it authorizes. Dot-prefixed and a plain file, so discovery skips
# it (``_iter_skill_files`` skips dot-dirs and reads only ``<name>/SKILL.md``).
# It lives at the skills root rather than inside ``auto/`` so taking it does not
# create the auto namespace as a side effect of a refused claim.
AUTO_SLUG_CLAIM_LOCK_NAME = ".auto-slug-claim.lock"

# The critical section is one directory test plus a small write, so a holder that
# has not released within seconds is stuck rather than busy. This overrides
# ``file_lock``'s own default ceiling downward, because the default suits a caller
# whose work may legitimately run long and a claim path's does not: refusing early
# and letting the next consolidation pass retry beats waiting on a dead holder.
AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS = 5.0

#: The one acquisition order for every auto-skill lock. A path takes any
#: subsequence of it, always left to right, and never blocks on an earlier lock
#: while it holds a later one, so no two paths can wait on each other. The one
#: out-of-order acquisition is a try-acquire that never waits: staging, under the
#: ``namespace`` lock, try-acquires ``claim`` locks to tell an active claim from
#: retired evidence (``SkillsLoader._pending_slug_claimed``).
#:
#: 1. ``slug-claim`` -- :data:`AUTO_SLUG_CLAIM_LOCK_NAME` at the skills root: one
#:    test-and-allocate across the live and pending halves of the slug space.
#:    Staging, a live publish (``create_auto_skill``) and a restore take it first.
#: 2. ``claim`` -- ``locks/claims/<claim>.lock`` under the private authority, held
#:    for the life of one claim transaction (approve, dismiss, unattended apply).
#:    Restart recovery only ever try-acquires it.
#: 3. ``target`` -- ``locks/target-<slug>.lock``: one live auto-skill. Promotion
#:    and recovery take it while holding their claim; a live publish and a
#:    restore take it inside the slug-claim lock; every other live mutation
#:    (update, pin, inject, archive, delete) takes it alone. A live name that is
#:    an alias of a canonical slug takes that slug's lock; a name no promotion
#:    can target under any spelling takes none.
#: 4. ``namespace`` -- ``locks/pending.lock``: the pending directory. Staging takes
#:    it inside the slug-claim lock; a claim rename and a refusal restore take it
#:    inside their claim lock.
#:
#: Publication itself never needs the slug-claim lock: it moves a stage into
#: ``auto/<slug>`` with a no-replace rename, so an occupied name refuses
#: atomically instead of being tested first.
AUTO_SKILL_LOCK_ORDER: tuple[str, ...] = ("slug-claim", "claim", "target", "namespace")


@dataclass
class ClaimRefusal:
    """Whether a claim path's ``None`` is transient and worth retrying.

    Claim paths return ``None`` for several unrelated reasons. An invalid slug,
    an over-long procedure and an exhausted sibling walk are properties of the
    CANDIDATE: the same input refused once is refused forever, so a retry is
    waste. An unacquired claim lock and an indeterminate active-claim inspection
    are properties of the MOMENT. The former means another process held the
    coordination lock; the latter means I/O could not prove the slug unclaimed.
    Both leave the candidate unwritten and promise that the next pass retries.
    That promise is only keepable by a caller that can distinguish them from a
    final candidate verdict, and a bare ``None`` cannot.

    Pass an instance to a claim path to learn which it was. It is deliberately a
    mutable out-parameter rather than a raise or a changed return type: the lock
    helper documents that it never raises, because an escaping error would abort a
    consolidation pass mid-way, and every existing caller that does not care about
    the distinction keeps reading the same ``str | None``.
    """

    #: True only when the refusal is transient and the same candidate should retry.
    retryable: bool = False
    #: Stable reason for a retryable refusal, or ``None`` for a final candidate verdict.
    reason: str | None = None


# Per-skill version history. A dot-prefixed dir *inside* a live auto-skill
# (``auto/<slug>/.versions/v<N>-SKILL.md``) so it is pruned from skill discovery
# (``_iter_skill_files`` skips dot-dirs) — historical snapshots never trigger and
# never surface in list_skills / list_auto_skills. Written by
# ``approve_pending_update`` before each live overwrite.
VERSIONS_DIRNAME = ".versions"

# Cap on retained per-skill version snapshots; oldest are pruned past this.
MAX_SKILL_VERSIONS = 20


# Public, hidden holding area for candidate inodes that may have retained writers.
# It stays outside the masked private tree so a descriptor opened before the claim
# never crosses beneath a trusted ancestor. Dot-prefix discovery pruning keeps these
# consumed candidates out of the pending and live skill catalogs.
AUTO_QUARANTINE_DIRNAME = ".quarantine"

# Public holding area for detached old-live generations. These inodes were
# reachable by agent processes before publication, so they must never cross
# beneath private authority: a retained directory descriptor follows a rename and
# its ``..`` would bypass the path mask on the private root. Candidate quarantine
# stays separate so one claim name has one unambiguous role per parent.
AUTO_LIVE_QUARANTINE_DIRNAME = ".live-quarantine"

# ``skills/auto/.private``: where an earlier revision of this protocol kept its
# authority. Read only to refuse an installation that still holds one.
AUTO_PRIVATE_DIRNAME = ".private"

AUTO_CLAIMS_DIRNAME = "claims"
AUTO_EVIDENCE_DIRNAME = "evidence"

# ``pending.lock`` serializes pending publish/claim/restore; target-specific
# files serialize live updates or first publication to one auto-skill.
AUTO_LOCKS_DIRNAME = "locks"
_PROMOTE_LOCK_TIMEOUT_S = 10.0
_PROMOTE_LOCK_POLL_S = 0.05
_CLAIM_LOCK_MAX_STATE_BYTES = 1_500_000

#: Maximum names read from any one authority-state directory while startup decides
#: whether stale claim state is idle. Certification stays on the gateway's
#: readiness path, before the orchestrator exists and the dashboard socket binds,
#: so no agent this gateway spawns can run before the obsolete-spelling and
#: stale-state checks. Its work is bounded regardless of planted state: a few
#: lstat calls for the obsolete spellings; one provenance read of at most
#: ``_AUTHORITY_PROVENANCE_MAX_BYTES`` and its MAC; only when provenance does not
#: verify, at most six directory scans of ``_STALE_CLAIM_SCAN_LIMIT + 1`` entries
#: each (authority root, ``claims/``, ``evidence/``, ``locks/claims/`` and both
#: public quarantines) plus two renames; when provisioning, one ``mkdir``, one
#: record write with its ``fsync`` and two directory syncs; and one rename probe
#: (create, rename and unlink of one empty file). An overflow is indeterminate and
#: disables auto-skills instead of extending that path.
_STALE_CLAIM_SCAN_LIMIT = 1024

#: Maximum unconsumed names counted while staging or recovery determines the
#: active claim namespace. A public quarantine name already retired into private
#: evidence is history and is not counted. This separately names the runtime
#: bound while keeping it aligned with startup's stale-authority scan ceiling.
_ACTIVE_CLAIM_SCAN_LIMIT = _STALE_CLAIM_SCAN_LIMIT


class _StaleClaimScanOverflow(RuntimeError):
    """A stale-authority directory exceeded the bounded startup scan."""


class _ActiveClaimScanOverflow(RuntimeError):
    """The active claim namespace exceeded its bounded runtime scan."""


def _bounded_stale_claim_names(
    target: int | Path,
    *,
    label: str,
    scanner=os.scandir,
) -> set[str]:
    """Read at most ``_STALE_CLAIM_SCAN_LIMIT`` names from one pinned directory."""
    names: set[str] = set()
    with scanner(target) as entries:
        for entry in entries:
            if len(names) >= _STALE_CLAIM_SCAN_LIMIT:
                raise _StaleClaimScanOverflow(
                    f"{label} exceeded the {_STALE_CLAIM_SCAN_LIMIT}-entry "
                    "stale-claim scan limit"
                )
            names.add(entry.name)
    return names


# Whole-generation snapshots retain file bytes until publication completes. Generated
# skill bodies and scripts are capped far below these ceilings, while live trees may also
# carry version history. Bound every allocation axis so a planted candidate or live tree
# fails closed instead of exhausting the gateway while it is authenticated.
_SKILL_SNAPSHOT_MAX_FILE_BYTES = 1024 * 1024
_SKILL_SNAPSHOT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_SKILL_SNAPSHOT_MAX_ENTRIES = 512
_SKILL_SNAPSHOT_MAX_DEPTH = 32

# The provenance record is deliberately outside the authority root it certifies.
# ``tag-grants`` is a pre-existing, precreated whole-directory sandbox mask and
# file-tool deny floor, so an older sandbox cannot plant this record before the
# new authority leaf exists. The MAC under ``token_signing.key`` additionally
# makes a copied or hand-written record inert.
_AUTHORITY_PROVENANCE_PARENT = AUTO_SKILL_AUTHORITY_PARENT_DIRNAME
_AUTHORITY_PROVENANCE_NAME = "auto-skill-private-authority.json"
_AUTHORITY_PROVENANCE_VERSION = 2
_AUTHORITY_PROVENANCE_MAX_BYTES = 4096
_AUTHORITY_PROVENANCE_DOMAIN = b"kiro-crew:auto-skill-private-authority:v2\x00"
_AUTHORITY_HOME_IDENTITIES_LOCK = threading.RLock()
_AUTHORITY_HOME_IDENTITIES: dict[str, tuple[str, _TaggedFileIdentity]] = {}
_STARTUP_AUTHORITY_BINDING: _CertifiedAuthorityBinding | None = None
_AUTHORITY_RETIRE_COMMAND = "kirocrew skills authority-retire"

#: Why this gateway's startup refused to provision or certify auto-skill
#: authority, when it did. Set only by
#: :func:`initialize_gateway_auto_skill_private_authority`; read by
#: :func:`auto_skill_promotion_disabled_reason`.
_STARTUP_AUTHORITY_REFUSAL: str | None = None

#: The refusal for a host whose agents run in a sandbox Kiro Crew does not build.
#: Pending maintainer confirmation of the supported platform contract; this is the
#: fail-closed choice.
_DELEGATED_SANDBOX_REFUSAL = (
    "agents on this host run in a sandbox Kiro Crew does not build (native Windows, "
    "or macOS with the Kiro CLI internal sandbox), which cannot hide the auto-skill "
    "authority from them; auto-skill staging and promotion stay off on this host"
)

# Stable refusal codes: callers and audit rows must distinguish an operator who
# explicitly disabled the sandbox from a delegated or unavailable protecting mask.
_SANDBOX_OFF_REFUSAL = "sandbox_off"
_SANDBOX_MASK_UNAVAILABLE_REFUSAL = "sandbox_mask_unavailable"

#: The refusal for a data home whose filesystem has no atomic no-replace rename.
_NO_REPLACE_RENAME_REFUSAL = (
    "the data home's filesystem has no atomic no-replace rename "
    "(renameat2(RENAME_NOREPLACE) on Linux, renameatx_np(RENAME_EXCL) on macOS), which "
    "auto-skill claims and publication need; move KIROCREW_HOME to a local filesystem "
    "that supports it. Auto-skill staging and promotion stay off on this data home"
)

# Name prefix of a no-replace rename probe's scratch file. A crash can leave one
# behind; it is never a claim, so the stale-authority check, lifecycle and claim
# recovery all skip it, and recovery removes one old enough to be orphaned.
_RENAME_PROBE_PREFIX = ".rename-probe-"

#: Minimum age before restart recovery treats a ``claims/`` rename probe as
#: orphaned. A live probe exists for one create-rename-unlink sequence; another
#: process can be inside one while recovery scans, and removing its file would
#: fail that process's claim. Measured against the file's own modification time.
_ORPHANED_RENAME_PROBE_MIN_AGE_S = 300.0

#: Maximum orphaned rename probes one recovery pass removes, so cleanup adds a
#: bounded amount of work to a pass that is itself bounded by
#: ``_ACTIVE_CLAIM_SCAN_LIMIT``.
_ORPHANED_RENAME_PROBE_CLEANUP_LIMIT = 64

# Obsolete authority spellings already reported as inert by this process.
_INERT_OBSOLETE_SPELLINGS_LOGGED: set[str] = set()


def _note_inert_obsolete_authority_spelling(path: Path) -> None:
    """Log, once per process and path, that an obsolete spelling is ignored."""
    key = _normalized_authority_path(path)
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if key in _INERT_OBSOLETE_SPELLINGS_LOGGED:
            return
        _INERT_OBSOLETE_SPELLINGS_LOGGED.add(key)
    logger.warning(
        "Ignoring obsolete auto-skill authority spelling %s: the hidden-parent "
        "authority already exists, so nothing there is trusted or read. It is safe "
        "to remove",
        path,
    )


@dataclass(frozen=True)
class _TaggedFileIdentity:
    """A serializable identity from an already-open native handle."""

    kind: str
    volume: int
    object_id: int | bytes


@dataclass(frozen=True)
class _CertifiedAuthorityBinding:
    """Native identities certified by the external authority provenance record."""

    configured_home: Path
    canonical_home: Path
    home_identity: _TaggedFileIdentity
    root_path: Path
    root_identity: _TaggedFileIdentity
    provenance_path: Path


@dataclass(frozen=True)
class _SkillTreeSnapshot:
    """One immutable tree generation captured from authenticated opened inodes."""

    files: dict[Path, bytes]
    file_modes: dict[Path, int]
    dir_modes: dict[Path, int]
    generation_hash: str
    root_identity: _TaggedFileIdentity


@dataclass(frozen=True)
class _ValidatedCandidateSnapshot:
    """One authenticated candidate generation captured at the claim boundary."""

    source_files: dict[Path, bytes]
    files: dict[Path, bytes]
    modes: dict[Path, int]
    metadata: dict[str, object]
    generation_hash: str


@dataclass(frozen=True)
class _ClaimSnapshot:
    """Immutable facts captured immediately before a pending claim rename.

    ``tree`` is process-local snapshot authority. The durable lock record keeps
    only the generation witness and metadata needed by restart recovery; no
    recovered path may publish a candidate without a fresh pre-claim snapshot.
    """

    generation_hash: str | None
    metadata_bytes: bytes | None
    tree: _SkillTreeSnapshot | None = field(default=None, compare=False, repr=False)
    quarantine_identity: _TaggedFileIdentity | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class _PinnedSkillParent:
    """One opened skill-state parent and the identity captured from its handle."""

    path: Path
    fd: int
    identity: tuple[int, int]
    native_identity: _TaggedFileIdentity


@dataclass(frozen=True)
class _PinnedPrivateState:
    """The public/private authority hierarchy held from canonical roots."""

    data_home: _PinnedSkillParent
    auto: _PinnedSkillParent
    pending: _PinnedSkillParent
    quarantine: _PinnedSkillParent
    live_quarantine: _PinnedSkillParent
    private: _PinnedSkillParent
    claims: _PinnedSkillParent
    evidence: _PinnedSkillParent
    locks: _PinnedSkillParent
    claim_locks: _PinnedSkillParent


def _normalized_authority_path(path: Path) -> str:
    """Normalize one absolute spelling without resolving its symlinks."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))


def _gateway_authority_homes() -> tuple[Path, Path]:
    """Return the selected canonical home and unresolved configured spelling."""
    from kiro_crew.config.paths import _valid_override_home

    selected = Path(config_dir())
    configured = selected
    raw_home = os.environ.get("KIROCREW_HOME")
    if raw_home and _valid_override_home() is not None:
        configured = Path(raw_home).expanduser()
    return selected, configured


def _auto_skill_authority_masked_view_reason() -> str | None:
    """Why this process cannot prove authority absence from its filesystem view."""
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    evidence = sandbox.agent_confinement_evidence()
    if evidence is None:
        return None
    return (
        "the auto-skill authority cannot be observed from inside the agent sandbox "
        f"({evidence}); its masked view cannot prove the authority root absent"
    )


def _auto_skill_authority_root_exists() -> bool:
    """Whether this data home may name an auto-skill authority root.

    Root absence is accepted only at an unmasked gateway or operator boundary.
    Inside an agent sandbox, ``tag-grants`` may be an empty bind-masked or
    Seatbelt-confined view while a real authority and lock exist outside it, so
    that view can never prove absence and is reported as existing. Any unreadable,
    retargeted, linked, or otherwise indeterminate home likewise fails closed
    toward existence. Re-read on every call: an operator retirement or a gateway
    provisioning the root changes the answer.
    """
    if _auto_skill_authority_masked_view_reason() is not None:
        return True
    try:
        selected, configured = _gateway_authority_homes()
        configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
        canonical_home = configured.resolve(strict=True)
        if selected.resolve(strict=True) != canonical_home:
            return True
        root = canonical_home / _AUTHORITY_PROVENANCE_PARENT / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        return os.path.lexists(root)
    except (OSError, RuntimeError, ValueError):
        return True


def _auto_skill_authority_retire_hint() -> str:
    """The operator recovery that removes an unusable authority from this home."""
    return (
        "stop every Kiro Crew gateway and agent using this data home, then run "
        f"`{_AUTHORITY_RETIRE_COMMAND}` from the operator's own terminal; the command "
        "refuses claims in flight and moves the authority, its record and the public "
        "quarantine history aside for inspection"
    )


def _move_authority_aside(
    loader: SkillsLoader,
    provenance_parent: _PinnedSkillParent,
    *,
    root_identity: _TaggedFileIdentity,
    record_identity: _TaggedFileIdentity | None,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Rename the authority, its record and the public quarantines aside under one token.

    A public ``.quarantine`` or ``.live-quarantine`` name is history only while
    private ``evidence/`` holds the same name, and ``evidence/`` leaves with the
    root. Left in place, every such name would count as an active claim against
    the next fresh root, so a home past ``_ACTIVE_CLAIM_SCAN_LIMIT`` lifetime
    claims could never stage or recover again. Both public quarantines therefore
    move beside their evidence: renamed, never read and never deleted, and the
    third element lists where they went (empty when neither existed).

    The caller must hold no pin on the root itself: a Windows directory handle
    opened without ``FILE_SHARE_DELETE`` forbids renaming that directory, so the
    root is re-checked here by native identity instead. A failed rename or sync
    restores every entry already renamed under its own name.
    """
    token = secrets.token_hex(8)
    with contextlib.ExitStack() as stack:
        auto_parent: _PinnedSkillParent | None = None
        if os.path.lexists(loader._dir):
            skills_parent = stack.enter_context(
                loader._pin_skill_parent(loader._dir.resolve(strict=True))
            )
            try:
                auto_parent = stack.enter_context(
                    loader._pin_skill_child_parent(
                        skills_parent,
                        AUTO_SKILL_NAMESPACE,
                        create=False,
                    )
                )
            except FileNotFoundError:
                auto_parent = None
        moves: list[tuple[_PinnedSkillParent, str, _TaggedFileIdentity | None]] = [
            (provenance_parent, AUTO_SKILL_PRIVATE_STATE_DIRNAME, root_identity)
        ]
        if record_identity is not None:
            moves.append((provenance_parent, _AUTHORITY_PROVENANCE_NAME, record_identity))
        if auto_parent is not None:
            moves.extend(
                (auto_parent, name, None)
                for name in (AUTO_QUARANTINE_DIRNAME, AUTO_LIVE_QUARANTINE_DIRNAME)
                if loader._pinned_child_exists(auto_parent, name)
            )
        moved: list[tuple[_PinnedSkillParent, str, _TaggedFileIdentity | None]] = []
        try:
            for parent, name, identity in moves:
                loader._rename_skill_child_no_replace(
                    parent,
                    name,
                    parent,
                    f"{name}.stale-{token}",
                    expected_identity=identity,
                )
                moved.append((parent, name, identity))
            loader._sync_pinned_parent(provenance_parent)
            if auto_parent is not None and any(parent is auto_parent for parent, *_ in moved):
                loader._sync_pinned_parent(auto_parent)
        except (OSError, RuntimeError, ValueError):
            unrestored: list[str] = []
            rollback_error: BaseException | None = None
            for parent, name, identity in reversed(moved):
                try:
                    loader._rename_skill_child_no_replace(
                        parent,
                        f"{name}.stale-{token}",
                        parent,
                        name,
                        expected_identity=identity,
                    )
                    loader._sync_pinned_parent(parent)
                except (OSError, RuntimeError, ValueError) as exc:
                    unrestored.append(str(parent.path / f"{name}.stale-{token}"))
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise OSError(
                    errno.EIO,
                    "authority retirement failed and could not be undone; still "
                    f"set aside: {', '.join(unrestored)}: {rollback_error}",
                ) from rollback_error
            raise
    history = tuple(
        parent.path / f"{name}.stale-{token}"
        for parent, name, _identity in moved
        if parent is not provenance_parent
    )
    return (
        provenance_parent.path / f"{AUTO_SKILL_PRIVATE_STATE_DIRNAME}.stale-{token}",
        (
            provenance_parent.path / f"{_AUTHORITY_PROVENANCE_NAME}.stale-{token}"
            if record_identity is not None
            else None
        ),
        history,
    )


def _active_authority_claim_names(
    loader: SkillsLoader,
    provenance_parent: _PinnedSkillParent,
) -> list[str]:
    """Names in an untrusted authority root's ``claims/``, read with one bounded scan.

    ``claims/`` holds only claims in flight; every retention path moves a claim
    to ``evidence/`` last. History (``evidence/``, the public quarantines and
    retained claim locks) grows by one name per claim and is never read here, so
    a long-lived home cannot outgrow this check. A root that is not a real
    directory holds no journal any recovery could open, so it has no claims.
    Raises ``_StaleClaimScanOverflow`` past ``_STALE_CLAIM_SCAN_LIMIT`` names and
    ``OSError`` when the root or ``claims/`` cannot be inspected.
    """
    root_path = provenance_parent.path / AUTO_SKILL_PRIVATE_STATE_DIRNAME
    root_info = loader._stat_pinned_child(provenance_parent, AUTO_SKILL_PRIVATE_STATE_DIRNAME)
    if not stat.S_ISDIR(root_info.st_mode) or is_link_or_junction(root_path):
        return []
    with loader._pin_skill_child_parent(
        provenance_parent,
        AUTO_SKILL_PRIVATE_STATE_DIRNAME,
        create=False,
    ) as root:
        try:
            with loader._pin_skill_child_parent(
                root,
                AUTO_CLAIMS_DIRNAME,
                create=False,
            ) as claims:
                target: int | Path = claims.fd if _DIR_FD_SUPPORTED else claims.path
                names = _bounded_stale_claim_names(target, label="claims")
        except FileNotFoundError:
            return []
    return sorted(name for name in names if not name.startswith(_RENAME_PROBE_PREFIX))


def _retire_untrusted_authority(
    loader: SkillsLoader,
    canonical_home: Path,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Move an authority root aside on a host where no process can promote.

    Reached only while ``_auto_skill_sandbox_excludes_every_promoter`` holds:
    every process on this data home refuses certification, so no certified
    promoter can hold the root, and its provenance is never consulted. A stale
    record after a ``cp -a``, rsync or backup restore, and a valid root whose
    retired history outgrew the stale-state scan, are both moved aside intact,
    as ``_set_aside_stale_authority`` does at a startup reseed. Idleness is
    decided from active ``claims/`` only. A claim journal found there refuses,
    because moving its root would leave that claim unrecoverable.
    """
    authority = f"{_AUTHORITY_PROVENANCE_PARENT}/{AUTO_SKILL_PRIVATE_STATE_DIRNAME}"
    record = f"{_AUTHORITY_PROVENANCE_PARENT}/{_AUTHORITY_PROVENANCE_NAME}"
    pending = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_PENDING_DIRNAME}"
    quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_QUARANTINE_DIRNAME}"
    live_quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_LIVE_QUARANTINE_DIRNAME}"
    by_hand = (
        "To recover by hand: keep every Kiro Crew gateway and agent using this data "
        f"home stopped; move {authority} and {record} out of the data home, keeping "
        f"both for inspection; to requeue a candidate for review, move its "
        f"{quarantine}/<slug>--<token> directory back to {pending}/<slug>; then move "
        f"{quarantine} and {live_quarantine} out with them, because their names are "
        "history only beside that authority's evidence. By-name live mutation and "
        "pending dismissal resume once the root is gone"
    )
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        try:
            claims = _active_authority_claim_names(loader, provenance_parent)
        except FileNotFoundError:
            raise OSError(
                errno.ENOENT,
                f"no auto-skill authority root exists at {authority}; by-name live "
                "mutation and pending dismissal already apply on this data home",
            ) from None
        except (_StaleClaimScanOverflow, OSError) as exc:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority claims could not be inspected ({exc}); "
                f"retirement refused. {by_hand}",
            ) from None
        if claims:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority has {len(claims)} claim(s) in flight "
                f"({', '.join(claims[:3])}); retirement refused. {by_hand}",
            )
        root_identity = loader._pinned_child_identity(
            provenance_parent,
            AUTO_SKILL_PRIVATE_STATE_DIRNAME,
        )
        if root_identity is None:
            raise OSError(errno.EBUSY, "authority root changed before operator retirement")
        try:
            loader._stat_pinned_child(provenance_parent, _AUTHORITY_PROVENANCE_NAME)
        except FileNotFoundError:
            record_identity = None
        else:
            record_identity = loader._pinned_child_identity(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
            )
            if record_identity is None:
                raise OSError(
                    errno.EBUSY,
                    "authority provenance record changed before operator retirement",
                )
        retired = _move_authority_aside(
            loader,
            provenance_parent,
            root_identity=root_identity,
            record_identity=record_identity,
        )
    logger.warning(
        "Auto-skill authority retired without verification on a host where no process "
        "can promote: the root was moved aside to %s, with its public quarantine "
        "history (%s)",
        retired[0],
        ", ".join(str(path) for path in retired[2]) or "none present",
    )
    return retired


def _retire_verified_authority(
    loader: SkillsLoader,
    canonical_home: Path,
    configured: Path,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Verify provenance and idleness, then move the authority aside.

    Used wherever a certified promoter could exist, so an unverifiable root is
    never moved: it refuses with the stopped-installation handoff unchanged.
    """
    binding = loader._ensure_private_authority(
        canonical_home,
        configured_home=configured,
        create=False,
    )
    verify_auto_skill_private_authority(binding)

    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        # The root pin lives only for the verification: Windows refuses to rename
        # a directory whose handle lacks ``FILE_SHARE_DELETE``, so the renames
        # re-check the root by its certified native identity instead.
        with loader._pin_skill_child_parent(
            provenance_parent,
            AUTO_SKILL_PRIVATE_STATE_DIRNAME,
            create=False,
        ) as private:
            record_identity = loader._pinned_child_identity(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
            )
            if (
                home_parent.native_identity != binding.home_identity
                or private.native_identity != binding.root_identity
                or record_identity is None
                or not loader._authority_record_valid(
                    provenance_parent,
                    configured,
                    canonical_home,
                    home_parent.native_identity,
                    private.native_identity,
                )
            ):
                raise loader._authority_handoff(
                    "authority or provenance changed before operator retirement"
                )
        claims = loader._stale_authority_claims(provenance_parent)
        if claims is None:
            raise OSError(
                errno.EBUSY,
                "auto-skill authority claims could not be inspected; retirement refused",
            )
        if claims:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority has {len(claims)} claim(s) in flight "
                f"({', '.join(claims[:3])}); retirement refused",
            )
        return _move_authority_aside(
            loader,
            provenance_parent,
            root_identity=binding.root_identity,
            record_identity=record_identity,
        )


def retire_auto_skill_private_authority() -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Move an idle auto-skill authority aside for operator recovery.

    The CLI caller holds the data home's gateway lock, so no gateway can start or
    remain active during this operation. Where a certified promoter could exist,
    provenance, selected-home identity, root identity, and the bounded no-claim
    verdict are rechecked while their parents stay pinned. Where
    ``_auto_skill_sandbox_excludes_every_promoter`` holds, no process on this data
    home can be certified, so an unverifiable root is moved aside without being
    trusted and idleness is read from active ``claims/`` only. The root, its
    record and the public ``.quarantine`` and ``.live-quarantine`` directories
    all take one ``.stale-<token>`` suffix and remain available for inspection,
    so a later fresh root starts with no claim history. Returns the root, the
    record (``None`` when there was none) and the set-aside quarantines. No fresh
    authority is provisioned.
    """
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL

    selected, configured = _gateway_authority_homes()
    configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
    canonical_home = configured.resolve(strict=True)
    if selected.resolve(strict=True) != canonical_home:
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted away from the selected data home",
        )
    loader = SkillsLoader.__new__(SkillsLoader)
    loader._dir = canonical_home / SKILLS_DIR_NAME
    if _auto_skill_sandbox_excludes_every_promoter():
        retired = _retire_untrusted_authority(loader, canonical_home)
    else:
        retired = _retire_verified_authority(loader, canonical_home, configured)

    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        _STARTUP_AUTHORITY_BINDING = None
        _STARTUP_AUTHORITY_REFUSAL = None
    return retired


def initialize_auto_skill_private_authority(
    skills_root: Path | None = None,
    *,
    data_home: Path | None = None,
    configured_home: Path | None = None,
) -> _CertifiedAuthorityBinding:
    """Exclusively provision and process-certify auto-skill authority.

    Production calls this only through the common gateway startup boundary;
    explicit calls remain the test-fixture seam that models that startup.
    """
    global _STARTUP_AUTHORITY_BINDING
    selected_home = Path(data_home) if data_home is not None else config_dir()
    configured = Path(configured_home) if configured_home is not None else selected_home
    configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
    canonical_home = configured.resolve(strict=True)
    selected_canonical = selected_home.resolve(strict=True)
    if canonical_home != selected_canonical:
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted away from the selected data home",
        )
    root = skills_root or canonical_home / SKILLS_DIR_NAME
    root_exists = os.path.lexists(root)
    loader = SkillsLoader.__new__(SkillsLoader)
    loader._dir = root
    loader._migrate_legacy_private_state(
        root.resolve(strict=root_exists),
        canonical_home,
    )
    binding = loader._ensure_private_authority(
        canonical_home,
        configured_home=configured,
        create=True,
    )
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        existing = _STARTUP_AUTHORITY_BINDING
        if existing is not None and existing != binding:
            raise loader._authority_handoff(
                "a different auto-skill authority was already certified in this process"
            )
        _STARTUP_AUTHORITY_BINDING = binding
    verify_auto_skill_private_authority(binding)
    return binding


def _agent_sandbox_is_delegated() -> bool:
    """Whether agents here run in a sandbox whose hidden paths Kiro Crew cannot set.

    The authority is protected by the ``tag-grants`` mask Kiro Crew's own Linux
    namespace and macOS Seatbelt profile apply. A delegated sandbox (native
    Windows, or macOS with the Kiro CLI internal sandbox) applies no such mask, so
    an agent's shell could reach the authority and approve its own candidate.
    """
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    return sandbox.spawn_delegates_masking()


def _auto_skill_authority_sandbox_refusal() -> str | None:
    """Why the authority cannot be protected from an agent spawned now."""
    if _agent_sandbox_is_delegated():
        return _DELEGATED_SANDBOX_REFUSAL
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    try:
        mode = sandbox.configured_sandbox_mode()
        masked = sandbox.credential_mask_applies(mode)
    except (OSError, RuntimeError, ValueError):
        return _SANDBOX_MASK_UNAVAILABLE_REFUSAL
    if masked:
        return None
    return _SANDBOX_OFF_REFUSAL if mode == "off" else _SANDBOX_MASK_UNAVAILABLE_REFUSAL


def initialize_gateway_auto_skill_private_authority() -> _CertifiedAuthorityBinding:
    """Run the process-wide auto-skill authority initialization boundary.

    Fails closed, and records why, where the protecting credential mask does not
    apply, where agent sandboxing is delegated, or where provisioning or
    certification refuses. The caller keeps the gateway running or stops a
    standalone mutating command before it advances state.
    """
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    sandbox_refusal = _auto_skill_authority_sandbox_refusal()
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if sandbox_refusal is not None:
            # A live mode change can expose the authority after startup. Revoke
            # this process's certificate before any later promotion can use it.
            _STARTUP_AUTHORITY_BINDING = None
            if (
                _auto_skill_authority_root_exists()
                and _auto_skill_sandbox_excludes_every_promoter()
            ):
                sandbox_refusal = (
                    f"{sandbox_refusal}; an auto-skill authority still exists on this "
                    f"data home. {_auto_skill_authority_retire_hint()}"
                )
            _STARTUP_AUTHORITY_REFUSAL = sandbox_refusal
            raise OSError(errno.ENOTSUP, sandbox_refusal)
        if _STARTUP_AUTHORITY_BINDING is not None:
            verify_auto_skill_private_authority(_STARTUP_AUTHORITY_BINDING)
            return _STARTUP_AUTHORITY_BINDING
        selected, configured = _gateway_authority_homes()
        try:
            binding = initialize_auto_skill_private_authority(
                data_home=selected,
                configured_home=configured,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _STARTUP_AUTHORITY_REFUSAL = str(exc) or type(exc).__name__
            raise
        probe = SkillsLoader.__new__(SkillsLoader)
        probe._dir = binding.canonical_home / SKILLS_DIR_NAME
        try:
            probe._probe_rename_under_authority(binding)
        except (OSError, RuntimeError, ValueError):
            logger.warning("Auto-skill no-replace rename probe failed", exc_info=True)
            # Fail closed once, here: with the primitive missing a candidate
            # could be staged but never approved or dismissed through a claim.
            _STARTUP_AUTHORITY_BINDING = None
            _STARTUP_AUTHORITY_REFUSAL = _NO_REPLACE_RENAME_REFUSAL
            raise OSError(errno.ENOTSUP, _NO_REPLACE_RENAME_REFUSAL) from None
        _STARTUP_AUTHORITY_REFUSAL = None
        return binding


def auto_skill_promotion_disabled_reason() -> str | None:
    """Why this process cannot stage or promote auto-skills, or ``None`` when it can.

    Staging, approval, unattended update application and claim-based dismissal
    all need the certified authority; this names the reason they will refuse, so
    a caller can report it instead of a generic failure.
    """
    try:
        require_auto_skill_private_authority()
    except (OSError, RuntimeError, ValueError) as exc:
        return _STARTUP_AUTHORITY_REFUSAL or str(exc) or "auto-skill authority is unavailable"
    return None


def _no_replace_refusal_still_binds_data_home() -> bool:
    """Re-probe the data-home-wide refusal only from an unmasked boundary.

    A probe inside the masked ``tag-grants`` view does not inspect the real
    authority root and can never grant lock-free mutation.
    """
    if _auto_skill_authority_masked_view_reason() is not None:
        return False
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if _STARTUP_AUTHORITY_REFUSAL != _NO_REPLACE_RENAME_REFUSAL:
            return False
    try:
        selected, configured = _gateway_authority_homes()
        configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
        canonical_home = configured.resolve(strict=True)
        if selected.resolve(strict=True) != canonical_home:
            return False
        probe = SkillsLoader.__new__(SkillsLoader)
        probe._dir = canonical_home / SKILLS_DIR_NAME
        binding = probe._ensure_private_authority(
            canonical_home,
            configured_home=configured,
            create=False,
        )
        probe._probe_rename_under_authority(binding)
    except NotImplementedError:
        return True
    except OSError as exc:
        unsupported = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}
        unsupported.add(getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
        return exc.errno in unsupported
    except (RuntimeError, ValueError):
        return False
    return False


def _auto_skill_sandbox_excludes_every_promoter() -> bool:
    """Whether a host-wide sandbox condition makes an existing root unusable.

    This reading selects the startup diagnostic and retirement hint only. It
    never grants lock-free mutation: a certified process may already hold a
    target lock when the setting changes. Three causes are shared widely enough
    to make retirement actionable: delegated agent sandboxing, an effective
    ``agent.sandbox`` of ``off``, and a genuine backend-less host. A transient
    backend probe failure, foreign outer sandbox, or unreadable predicate keeps
    the generic retry guidance instead.

    Never cached: a live sandbox or backend change must affect the next startup
    diagnostic.
    """
    if _agent_sandbox_is_delegated():
        return True
    if _auto_skill_authority_sandbox_refusal() is None:
        return False
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    try:
        if sandbox.effective_sandbox_mode(sandbox.configured_sandbox_mode()) == "off":
            return True
        return sandbox.unavailable_kind() == "no_backend"
    except (OSError, RuntimeError, ValueError):
        return False


def _auto_skill_promotion_ruled_out() -> bool:
    """Whether lock-free by-name mutation is safe for this data home now.

    An absent authority root is re-read on every call and proves no process can
    begin an authority-backed promotion only when this process has an unmasked
    filesystem view. The other grant is the data-home-wide no-replace refusal
    while a fresh unmasked probe still proves the publication primitive
    unavailable. Inside an agent sandbox both verdicts are indeterminate, so
    every mutation requires the authority-backed target lock and fails closed if
    that hidden lock cannot be acquired. A startup refusal or host-wide sandbox
    reading cannot bypass a lock a certified process may already hold.
    """
    if not _auto_skill_authority_root_exists():
        return True
    return _no_replace_refusal_still_binds_data_home()


def require_auto_skill_private_authority() -> _CertifiedAuthorityBinding:
    """Return the startup certificate after revalidating its mask and identity."""
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    sandbox_refusal = _auto_skill_authority_sandbox_refusal()
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if sandbox_refusal is not None:
            _STARTUP_AUTHORITY_BINDING = None
            _STARTUP_AUTHORITY_REFUSAL = sandbox_refusal
            raise OSError(errno.ENOTSUP, sandbox_refusal)
        binding = _STARTUP_AUTHORITY_BINDING
    if binding is None:
        raise OSError(
            errno.EBUSY,
            "auto-skill private authority has no gateway-startup certificate",
        )
    verify_auto_skill_private_authority(binding)
    return binding


def verify_auto_skill_private_authority(binding: _CertifiedAuthorityBinding) -> None:
    """Revalidate the startup binding without creating or repairing any path."""
    configured = Path(_normalized_authority_path(binding.configured_home))
    current_canonical = configured.resolve(strict=True)
    selected_canonical = Path(config_dir()).resolve(strict=True)
    if _normalized_authority_path(current_canonical) != _normalized_authority_path(
        binding.canonical_home
    ) or _normalized_authority_path(selected_canonical) != _normalized_authority_path(
        binding.canonical_home
    ):
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted after startup certification",
        )
    loader = SkillsLoader.__new__(SkillsLoader)
    loader._dir = binding.canonical_home / SKILLS_DIR_NAME
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(binding.canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        private = stack.enter_context(
            loader._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            )
        )
        if (
            home_parent.native_identity != binding.home_identity
            or private.native_identity != binding.root_identity
            or private.path != binding.root_path
            or not loader._authority_record_valid(
                provenance_parent,
                configured,
                binding.canonical_home,
                binding.home_identity,
                binding.root_identity,
            )
            or not loader._pinned_parent_matches(home_parent)
            or not loader._pinned_parent_matches(private)
            or not loader._pinned_parent_matches(provenance_parent)
        ):
            raise loader._authority_handoff(
                "certified authority changed between initialization and use"
            )


def _reset_auto_skill_private_authority_for_tests() -> None:
    """Reset process startup state for an isolated test data home."""
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        _STARTUP_AUTHORITY_BINDING = None
        _STARTUP_AUTHORITY_REFUSAL = None
        _AUTHORITY_HOME_IDENTITIES.clear()
        _INERT_OBSOLETE_SPELLINGS_LOGGED.clear()


def canonical_skill_text_hash(content: str | bytes) -> str:
    """Hash UTF-8 skill text after canonicalizing CRLF/CR newlines to LF.

    Text-mode reads normalize newlines while descriptor snapshots retain raw
    bytes.  A base-content binding must describe the logical SKILL.md text, not
    which platform wrote it; candidate-generation hashes remain byte-exact.
    """
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


# Informational observer for prose-only updates promoted without review. This is
# separate from the staged hook because the candidate is already live.
_UPDATE_AUTO_APPLIED_HOOK: "Callable[[dict], None] | None" = None


def set_update_auto_applied_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear) the unattended-update observer."""
    global _UPDATE_AUTO_APPLIED_HOOK
    _UPDATE_AUTO_APPLIED_HOOK = fn


def _emit_update_auto_applied(payload: dict) -> None:
    """Invoke the unattended-update observer without affecting promotion."""
    fn = _UPDATE_AUTO_APPLIED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("update-auto-applied hook failed", exc_info=True)


# Frontmatter field used to mark a skill as auto-generated.  Absence means
# the skill carries no source field, i.e. is hand-authored.
AUTO_SKILL_SOURCE_VALUE = "auto"

# Cap synthesized procedure markdown at 10 KB.  Longer outputs indicate
# the aux LLM failed to stay on-task and should be rejected.
AUTO_SKILL_MAX_PROCEDURE_CHARS = 10_240

# Byte bound on a pending candidate's two DOCUMENT files, ``SKILL.md`` and
# ``.meta.json``, for the detail read that informs approval. Derived from the
# limit above rather than chosen: the procedure is bounded in characters and a
# UTF-8 character is at most four bytes, so the body alone may legitimately
# reach four times the limit; the frontmatter around it (name, the one-line
# description and trigger list, the provenance fields, the heading) gets one
# more limit's worth of headroom, many times what the generator asks of those
# fields (a description of at most 150 characters, three to eight triggers),
# and ``.meta.json``, which repeats the description and triggers beside at most
# ``_PENDING_SCRIPT_MAX_ENTRIES`` script names and carries no procedure, fits
# under the same number. Staging bounds only the procedure, so a candidate whose
# frontmatter or metadata runs past the headroom is refused by this cap like any
# other over-cap document. It is a separate cap from ``MAX_SCRIPT_BYTES``,
# which bounds a bundled EXECUTABLE script and is the per-file cap under
# ``scripts/``: a document read under that cap refuses every valid generated
# skill whose SKILL.md is over 4 KiB as unreadable, with Approve disabled, for a
# candidate approve would promote. A document over this cap is refused whole;
# the read is bounded and never serves a partial view.
_PENDING_DOCUMENT_MAX_BYTES = (
    AUTO_SKILL_MAX_PROCEDURE_CHARS * 4  # the procedure, at UTF-8's four bytes per character
    + AUTO_SKILL_MAX_PROCEDURE_CHARS  # frontmatter headroom
)

# Regex for auto-generated skill name segment validation.  Deliberately
# restrictive — we control the generator so we don't need to accept
# arbitrary unicode.  ``_safe_name`` already rejects ``..`` and ``\``;
# this is an additional sanitization layer specific to auto-gen.
_AUTO_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# Bundled fallback — inside the kiro_crew package
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"

#: The installed ``kiro_crew`` package tree: a trusted provider root for skill
#: symlinks, and the only tree whose hardlinked ``SKILL.md`` can be admitted (see
#: :func:`_installed_package_bytes_match`). A module binding so a test can stand
#: up a fake site dir.
_INSTALLED_PACKAGE_DIR = Path(__file__).parent

#: RECORD hash names accepted as an admission witness: the wheel spec requires
#: sha256 or stronger, and a weaker one could be matched on purpose.
_RECORD_HASHES = frozenset({"sha256", "sha384", "sha512"})


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
    roots: list[str] = [os.path.realpath(_INSTALLED_PACKAGE_DIR)]
    try:
        roots.append(os.path.realpath(config_dir() / "apps"))
    except Exception:  # noqa: BLE001 — an unresolvable data home must not stop scanning
        pass
    return tuple(roots)


_package_record_lock = threading.Lock()
#: package dir -> (cache key, recorded ``SKILL.md`` digests). The key is the site
#: dir's own stamp plus each candidate RECORD's, so a dist-info added or removed
#: (the site dir changes) or a RECORD rewritten (its stamp changes) re-reads.
_package_record_cache: dict[str, tuple[tuple, dict[str, frozenset[tuple[str, str]]]]] = {}


def _record_stamp(record: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(record)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _package_record_paths(package_dir: str) -> list[str]:
    """Every RECORD the installer left for this package, found by name alone.

    One listing of the site dir, which reads no other distribution's metadata:
    the cost is that directory's entry count, never their RECORD sizes. More
    than one survives an upgrade that leaves the previous ``dist-info`` behind;
    each is a candidate, and a stale one can only vouch for bytes the previous
    release shipped. An editable install
    keeps its ``dist-info`` in site-packages, not beside the source tree, so it
    finds none and admits nothing.
    """
    wanted = normalize_distribution_name(_DISTRIBUTION_NAME)
    records: list[str] = []
    try:
        with os.scandir(os.path.dirname(package_dir)) as entries:
            for entry in entries:
                stem, dot, suffix = entry.name.rpartition(".")
                if (
                    dot
                    and suffix == "dist-info"
                    and normalize_distribution_name(stem.split("-", 1)[0]) == wanted
                ):
                    records.append(os.path.join(entry.path, "RECORD"))
    except OSError:
        return []
    return sorted(records)


def _read_package_record(
    record: str, package_dir: str
) -> tuple[tuple[int, int, int], dict[str, tuple[str, str]]] | None:
    """*record*'s identity and its ``SKILL.md`` digests, or ``None``.

    A RECORD that does not list the package's own ``__init__.py`` vouches for
    nothing, so it answers no digests. ``None`` means it could not be read or
    changed while it was read: the identity is taken before and after the parse,
    so digests are never filed under a stamp that belongs to a different RECORD.
    """
    before = _record_stamp(record)
    if before is None:
        return None
    site_dir = os.path.dirname(package_dir)
    package = os.path.basename(package_dir)
    owned = f"{package}/__init__.py"
    skill_row = f"/{_SKILL_FILE}"
    owns = False
    digests: dict[str, tuple[str, str]] = {}
    try:
        with open(record, encoding="utf-8", newline="") as fh:
            # A substring test first: only two kinds of row matter, and running
            # every one of a few thousand rows through the CSV parser is what
            # made this the slow part.
            wanted = (line for line in fh if line.startswith(owned) or skill_row in line)
            for row in csv.reader(wanted):
                if not row:
                    continue
                if row[0] == owned:
                    owns = True
                    continue
                parts = PurePosixPath(row[0]).parts
                if (
                    len(row) < 2
                    or parts[-1:] != (_SKILL_FILE,)
                    or parts[:1] != (package,)
                    or ".." in parts
                ):
                    continue
                name, sep, value = row[1].partition("=")
                if sep:
                    absolute = os.path.normpath(os.path.join(site_dir, *parts))
                    digests[os.path.normcase(absolute)] = (name.lower(), value)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if _record_stamp(record) != before:
        return None
    return before, digests if owns else {}


def _recorded_skill_digests() -> dict[str, frozenset[tuple[str, str]]]:
    """The installed package's recorded ``SKILL.md`` digests, re-read on reinstall.

    Cached per process under the site dir's stamp and every candidate RECORD's,
    so the steady state costs a few ``stat`` calls and a reinstall, a dist-info
    appearing mid-upgrade, or a RECORD replaced mid-read is re-read on the next
    call rather than judged against a previous answer. A RECORD that changed
    while it was parsed is left out and its stamp is not recorded, so the next
    call reads it again.
    """
    package_dir = os.path.realpath(_INSTALLED_PACKAGE_DIR)
    site_stamp = _record_stamp(os.path.dirname(package_dir))
    with _package_record_lock:
        cached = _package_record_cache.get(package_dir)
    if cached is not None and cached[0][0] == site_stamp:
        if all(_record_stamp(record) == stamp for record, stamp in cached[0][1]):
            return cached[1]
    stamps: list[tuple[str, tuple[int, int, int] | None]] = []
    merged: dict[str, set[tuple[str, str]]] = {}
    for record in _package_record_paths(package_dir):
        found = _read_package_record(record, package_dir)
        if found is None:
            # Unreadable or mid-rewrite: a ``None`` stamp never matches a file
            # that exists, so the next call reads it again.
            stamps.append((record, None))
            continue
        stamps.append((record, found[0]))
        for path, digest in found[1].items():
            merged.setdefault(path, set()).add(digest)
    digests = {path: frozenset(found) for path, found in merged.items()}
    key = (site_stamp, tuple(stamps))
    with _package_record_lock:
        _package_record_cache[package_dir] = (key, digests)
    return digests


def _installed_package_bytes_match(path: str, data: bytes) -> bool:
    """Admit a hardlinked ``SKILL.md`` only when it IS the installed package's file.

    Installers such as uv hardlink package files out of their cache, so a
    built-in app skill can legitimately carry ``st_nlink > 1``. The no-link
    reader refuses that shape everywhere else; here it is admitted on content:
    *path* must be a ``SKILL.md`` the distribution's RECORD lists inside the
    installed package tree, and *data* (the bytes actually read) must hash to the
    digest recorded for it. Any other hardlinked file, including one in the
    skills dir, an app under the data home, an extra path or a project, has no
    RECORD entry and stays refused. Rewriting RECORD needs write access to the
    same tree as the package code, which already decides what the gateway runs.
    """
    try:
        recorded = _recorded_skill_digests().get(os.path.normcase(os.path.normpath(path)), ())
        for algorithm, value in recorded:
            if algorithm not in _RECORD_HASHES:
                continue
            digest = hashlib.new(algorithm, data).digest()
            actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if hmac.compare_digest(actual, value):
                return True
        return False
    except Exception:  # noqa: BLE001 — an admission that cannot be decided refuses
        return False


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


@functools.lru_cache(maxsize=1)
def _packaged_skill_names() -> frozenset[str]:
    """Keys of the skills this package ships, walked once per process.

    Two packaged trees install into the skills dir: ``builtin_skills/`` (the
    builtin sync) and the deploy layer's own ``deploy/skills/`` copies. Both are
    immutable while the process runs, so the one walk is the whole cost; the
    startup index asks this per row to tell a shipped skill from one the user
    wrote.
    """
    return frozenset(
        name
        for root in (_BUILTIN_SKILLS_DIR, _DEPLOY_SKILLS_DIR)
        if root.is_dir()
        for name, _ in _iter_skill_files(root)
    )


#: Basename every skill's body lives under. Used as a cheap pre-filter before
#: any filesystem work when deciding whether a tool call touched a skill.
_SKILL_FILE = "SKILL.md"

#: Argument names under which file-reading tools carry a flat target. A name
#: that is absent simply yields no candidate. kiro-cli's own `read` batches its
#: targets under ``operations`` instead (see ``_tool_read_path_candidates``).
_TOOL_READ_PATH_KEYS = ("path", "file_path", "filePath", "paths", "files")

#: A whitespace/quote-delimited token ending in the skill basename — how a skill
#: read appears inside a shell command (``cat /x/SKILL.md``). Anchored on the
#: basename so it cannot match an arbitrary argument.
_SHELL_SKILL_PATH_RE = re.compile(r"""[^\s"'|;&><]+SKILL\.md""")


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


def _decode_skill_text(raw: bytes, *, strict: bool = True) -> str:
    """Decode SKILL.md bytes with ``read_text``'s newline handling.

    These reads take bytes rather than ``read_text`` so containment can be checked
    on the descriptor actually opened. ``read_text`` opens in TEXT mode and
    performs universal-newline translation; a bytes read does not. Git checks out
    CRLF on Windows, so without this every frontmatter key would carry a trailing
    ``\r``, nothing would match ``always`` or ``pinned``, and skill bodies would
    silently stop being injected there while Linux and macOS looked fine.

    ``utf-8-sig`` for the same reason: a UTF-8 file saved "with BOM" (Notepad and
    other Windows editors) is valid UTF-8 whose first character is U+FEFF, which
    is not content. Left in, it sits in front of the ``---`` fence, so the
    frontmatter grammar (column 0, position 0) finds no block and the skill lists
    with no metadata -- and the mark itself lands in the injected body. The codec
    strips one leading mark and is otherwise plain UTF-8: a file without one
    decodes byte-for-byte as before, and a file that is not UTF-8 at all (UTF-16,
    which opens with ``0xFF``) still raises under *strict*.

    *strict* decoding propagates invalid UTF-8, which a WRITER must hear
    (``update_auto_skill`` carries version metadata across a rewrite). Callers
    that only render text pass ``strict=False``.
    """
    text = raw.decode("utf-8-sig") if strict else raw.decode("utf-8-sig", errors="replace")
    # Universal newlines, matching TEXT-mode reads: CRLF and lone CR both fold.
    return text.replace("\r\n", "\n").replace("\r", "\n")


_PROJECT_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


#: (base, refused component, errno) triples already warned about, so a project
#: whose chain is refused is named once per process rather than on every catalog
#: build. Bounded: past the cap the warning is still emitted, just not recorded.
_CHAIN_REFUSALS_WARNED: set[tuple[str, str, int]] = set()
_CHAIN_REFUSALS_WARNED_CAP = 256


def _note_chain_refusal(base: Path, component: str, exc: OSError) -> None:
    """Say which component of a project skills path the no-follow walk refused.

    A missing component is the ordinary case (most projects have no
    ``.kiro/skills``) and stays at DEBUG. Anything else -- a symlinked
    directory (refused by ``O_NOFOLLOW``), a file where a directory was
    expected, a permission denial -- means the operator's skills exist but will
    never load, and the remedy is to inspect that one path, so it is a WARNING
    naming it.
    """
    err = exc.errno or 0
    if err == errno.ENOENT:
        logger.debug("project skills path has no %r component: %s", component, base)
        return
    marker = (str(base), component, err)
    if marker in _CHAIN_REFUSALS_WARNED:
        return
    if len(_CHAIN_REFUSALS_WARNED) < _CHAIN_REFUSALS_WARNED_CAP:
        _CHAIN_REFUSALS_WARNED.add(marker)
    # O_DIRECTORY | O_NOFOLLOW reports a symlink as ELOOP or ENOTDIR depending
    # on the kernel, the same ENOTDIR a regular file gets, so name both.
    if err in (errno.ELOOP, errno.ENOTDIR):
        reason = "is a symlink or not a directory"
    else:
        reason = f"could not be opened ({exc.strerror or f'errno {err}'})"
    logger.warning(
        "project skills under %s are not loaded: path component %r %s; project "
        "skills are walked without following links, so this component must be "
        "a real, readable directory",
        base,
        component,
        reason,
    )


def _open_project_dir_chain(base: Path) -> int | None:
    """Open every absolute path component through the prior no-follow handle."""
    if not skill_trust.project_skill_traversal_supported():
        return None
    parts = Path(os.path.abspath(base)).parts
    try:
        fd = os.open(parts[0], _PROJECT_DIR_OPEN_FLAGS)
    except OSError as exc:
        _note_chain_refusal(base, parts[0], exc)
        return None
    for part in parts[1:]:
        try:
            next_fd = os.open(part, _PROJECT_DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            _note_chain_refusal(base, part, exc)
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
        if not _within_any(real, allowed_roots):
            # On Windows, record the resolved child and the roots it is judged
            # against whenever the containment gate turns a child of the skills
            # base away, so a CI run surfaces the exact (candidate, roots) pair
            # the gate rejects. Windows-only and logging-only: the return is
            # unchanged, so no platform's behaviour is affected.
            if os.name == "nt":
                logger.warning(
                    "skill discovery: _within_any rejected a child of base "
                    "(candidate=%r, roots=%r)",
                    real,
                    allowed_roots,
                )
            return real, [], None
        if is_sensitive_resolved_path(real):
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
    "prepare-pr": "kirocrew-dev/kirocrew-prepare-pr",
    # Renamed in place: the nested copy an earlier release installed.
    "kirocrew-dev/prepare-pr": "kirocrew-dev/kirocrew-prepare-pr",
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


def _strip_carried_opt_out(data: bytes) -> bytes | None:
    """Return the SKILL.md bytes *data* minus a carried ``inject_on_trigger: false``.

    Only the exact form the builtin sync writes is recognised: *data* must be
    what the shared rewrite produces when it applies the opt-out to the
    returned bytes. Anything else (no line, a ``true`` value, an indented
    occurrence, a hand-placed line elsewhere in the block, undecodable bytes)
    returns None, so the caller treats the tree as unprovable rather than
    tolerating an edit.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = _authoring.rewrite_inject_on_trigger(text, True)
    if stripped is None or _authoring.rewrite_inject_on_trigger(stripped, False) != text:
        return None
    return stripped.encode("utf-8")


def _skill_tree_fingerprint(
    root: Path, *, assume_owner_rwx_dirs: bool = False, strip_carried_opt_out: bool = False
) -> str | None:
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

    ``strip_carried_opt_out`` hashes the tree as it was before the sync carried
    the user's Context-budget opt-out onto it: the top-level ``SKILL.md`` bytes
    already read through the hardened path above are passed through
    ``_strip_carried_opt_out`` and hashed with their stripped size. Every
    other entry, and every guard, is unchanged. A ``SKILL.md`` that does not
    hold exactly the carried line makes the tree unprovable (None).
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
        if kind != "file":
            digest.update(f"{kind}\0{rel}\0{detail}\0".encode("utf-8", "surrogatepass"))
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
        if strip_carried_opt_out and rel == "SKILL.md":
            # The bytes come from the descriptor-pinned read above; only
            # their content and the size recorded for them are substituted.
            stripped = _strip_carried_opt_out(data)
            if stripped is None:
                return None
            data = stripped
            detail = f"{len(data)}:{detail.split(':', 1)[1]}"
        digest.update(f"{kind}\0{rel}\0{detail}\0".encode("utf-8", "surrogatepass"))
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


def _matches_recorded(root: Path, recorded: str, current: str | None) -> bool:
    """Does the tree at *root* (fingerprinted as *current*) match *recorded*?

    The marker records the PACKAGED tree's fingerprint. A tree the sync
    installed while carrying the user's Context-budget opt-out differs from
    that by the one ``inject_on_trigger: false`` line on ``SKILL.md``, so when
    the straight comparison fails the tree is fingerprinted again with that
    line stripped. Any other difference still fails both comparisons.
    """
    if current is None:
        return False
    if current == recorded:
        return True
    return _skill_tree_fingerprint(root, strip_carried_opt_out=True) == recorded


def _verified_unchanged_fingerprint(dest_dir: Path, src_dir: Path | None) -> str | None:
    """Return *dest_dir*'s fingerprint iff it is verifiably an unchanged copy
    this sync installed, else None.

    Two ways to prove ownership:
    - The recorded provenance fingerprint still matches the tree on disk,
      allowing for a carried Context-budget opt-out (``_matches_recorded``).
      The value returned is the tree's own fingerprint either way.
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
        return current if _matches_recorded(dest_dir, recorded, current) else None
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


SKILL_INSTALL_IN_SYNC = "in-sync"
SKILL_INSTALL_BEHIND = "behind"
SKILL_INSTALL_EDITED = "edited"
SKILL_INSTALL_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class InstalledSkillCurrency:
    """One installed builtin skill judged against the packaged tree it came from."""

    name: str
    source: Path
    state: str


def _first_linked_skill_component(base: Path, name: str) -> Path | None:
    """First directory strictly BETWEEN *base* and ``base / name`` that is a link.

    A skill name may be nested (``kirocrew-dev/kirocrew-prepare-pr``), so testing the
    leaf alone leaves the directories above it unscreened while every probe of
    the leaf still resolves through them. A link at ``<skills>/kirocrew-dev``
    then makes the fingerprint hash a tree outside the skills directory and
    report it as this install, which is the verdict the leaf screen exists to
    refuse.

    Components are tested ROOT-FIRST and the walk stops at the first hit, so
    each ``lstat`` runs only after the component above it is known not to be a
    link and the walk itself never traverses one.

    A link here is operator-made: these are directories the sync creates itself,
    exactly as the leaf is. A flat name has no components between the two paths
    and costs no stat at all.
    """
    current = base
    for part in PurePosixPath(name).parts[:-1]:
        current = current / part
        if is_link_or_junction(current):
            return current
    return None


def installed_skill_currency() -> list[InstalledSkillCurrency]:
    """Judge each installed skill against the packaged tree the sync would pick.

    The question this answers is currency: does the install still correspond to
    the package this process is running. A script can report its own identity
    but not its own currency, because currency is a relation between the
    install and the package, and only one side of it is visible from inside a
    shipped file. Both sides are visible here.

    No new provenance data is needed, because the marker already holds the
    answer. ``_ensure_builtin_skills`` records the fingerprint of the PACKAGED
    tree (not of the copy it wrote), and ``_tree_entries`` hashes relative
    paths, modes, link targets and file bytes while excluding mtime entirely.
    The recorded value is therefore a portable content identity of the package
    the install came from, and comparing it against a fresh fingerprint of the
    packaged tree needs no version constant inside any shipped file, no marker
    format change, and no network call. One install differs from the tree the
    marker records: a builtin the sync reinstalled while carrying the user's
    Context-budget opt-out holds one extra ``inject_on_trigger: false`` line in
    ``SKILL.md``. The marker still records the PACKAGED fingerprint, and the
    ownership check tolerates exactly that line (``_matches_recorded``), so
    such an install reads as in sync rather than edited or behind.

    What makes staleness silent today is the update gate, not the marker: it
    compares mtimes, so an installed copy carrying an mtime newer than anything
    the package ships is judged up to date, no copy happens, and nothing says
    so. This function reads the same two trees the gate reads and reports the
    comparison the gate throws away.

    ``SKILL_INSTALL_BEHIND`` states that the two sides disagree, not which is
    older: a content hash cannot order two revisions, exactly as
    :func:`deployed_cron_script_sources` reports divergence without a
    direction. "Behind" names the actionable direction in practice because the
    packaged side is the code this process is running.

    ``SKILL_INSTALL_EDITED`` takes precedence over a currency verdict when both
    apply. The marker still identifies the package an edited install came from,
    but the edit is the fact that has to be reconciled first: while it stands,
    the sync quarantines the directory rather than updating it, so reporting
    the install merely as behind would name a remedy that does not apply.

    Source roots are consulted in the sync's own order, project skills before
    packaged ones, and the first root to ship a name owns it. Comparing a
    project skill against a packaged tree of the same name would otherwise
    report every shadowing skill as behind the builtin it deliberately
    replaces.

    An installed directory that NO source root ships is ABSENT from the result
    rather than reported: with no packaged tree there is nothing to be out of
    step with, and a skill the operator installed themselves must not be
    described as stale. Likewise a name that is packaged but not installed --
    the sync installs it on the next run, and an absent directory has no
    currency to judge.

    Every read failure yields ``SKILL_INSTALL_UNVERIFIABLE`` rather than a
    comparison: an unmarked install (pre-provenance, or user-authored by
    assumption), a destination that is itself a link or junction, a skills
    directory that is itself linked, a linked directory between the skills
    directory and a nested install, and a tree over the fingerprint ceilings
    all land there. Unverifiable never reads as agreement, because an
    instrument whose read failed must not report the two sides equal.

    POSIX only, and that gate is the first thing here rather than a detail.
    Every read below reaches its target by name, and the link test guarding each
    one is a separate syscall from the read it guards, so a concurrent writer to
    the skills directory can substitute a link in between. On POSIX the cost of
    losing that race is a wrong verdict. On Windows a junction whose target is a
    UNC share turns the next local-looking stat into an outbound connection that
    authenticates as this process, which nothing afterwards can take back, and
    the descriptor-pinned walk that would close it is unavailable there:
    :func:`kiro_crew.pinned_fs.supports_pinned_walk` requires ``O_NOFOLLOW`` and
    ``os.open`` in ``os.supports_dir_fd``, and Windows offers neither. So this
    reports nothing on Windows rather than reading unsafely, and the doctor
    section says the check does not run instead of printing verdicts it cannot
    stand behind. Tracked separately, with the core primitive it needs.
    """
    if os.name == "nt":
        return []
    base = skills_dir()
    # Screened BEFORE the first stat, because every probe below is built from
    # this path. A link here makes each local-looking stat a read of whatever
    # the link targets, and the sync only ever creates this directory for real,
    # so a link is operator-made and its target is not this gateway's install
    # tree. A fingerprint taken through it would describe some other tree and
    # be reported as this install.
    base_readable = not is_link_or_junction(base)
    if base_readable and not base.is_dir():
        return []

    results: list[InstalledSkillCurrency] = []
    supplied: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            # First source root to ship a name owns it, mirroring the sync.
            if name in supplied:
                continue
            supplied.add(name)
            src_dir = src_file.parent
            if not base_readable:
                # Nothing under an unreadable base can be judged, and presence
                # is the first thing that cannot be tested. Reported rather than
                # skipped: a section that printed nothing would read as a
                # gateway with no installs, and unverifiable must never read as
                # agreement.
                results.append(
                    InstalledSkillCurrency(
                        name=name,
                        source=src_dir,
                        state=SKILL_INSTALL_UNVERIFIABLE,
                    )
                )
                continue
            dest_dir = base / name
            if _first_linked_skill_component(base, name) is not None:
                # Screened before the leaf, for the same reason the base is
                # screened before this loop: a nested name's intermediate
                # directories are resolved through by every probe below,
                # including the link test on the leaf itself, so a link there is
                # traversed by whichever probe runs first. Reported rather than
                # skipped, because presence is the first thing that cannot be
                # tested and an omitted name would read as not installed.
                results.append(
                    InstalledSkillCurrency(
                        name=name,
                        source=src_dir,
                        state=SKILL_INSTALL_UNVERIFIABLE,
                    )
                )
                continue
            # Order is the safety property, not a style choice. The link test is
            # local to dest_dir, while the SKILL.md stat resolves THROUGH it, so
            # testing the link first means a linked install is never traversed.
            # Reversed, the stat reads the link's target before anything has
            # judged the path, and a tree outside the skills directory then
            # decides whether this install is present.
            if not is_link_or_junction(dest_dir) and not (dest_dir / "SKILL.md").is_file():
                # Not installed: nothing on disk to judge. A link IS judged even
                # when its SKILL.md does not resolve, because a dangling one is
                # an install whose currency cannot be established, not an
                # absence -- and the state check refuses to read any link's
                # target, so reaching it costs no traversal.
                continue
            results.append(
                InstalledSkillCurrency(
                    name=name,
                    source=src_dir,
                    state=_skill_currency_state(dest_dir, src_dir),
                )
            )
    return sorted(results, key=lambda entry: entry.name)


def _skill_currency_state(dest_dir: Path, src_dir: Path) -> str:
    """Compare one installed skill tree against the packaged tree it came from."""
    if is_link_or_junction(dest_dir):
        # The sync only ever creates real directories, so a link here is
        # user-made and its target must not even be read.
        return SKILL_INSTALL_UNVERIFIABLE
    recorded = _recorded_fingerprint(dest_dir)
    if recorded is None:
        return SKILL_INSTALL_UNVERIFIABLE
    installed = _skill_tree_fingerprint(dest_dir)
    if installed is None:
        return SKILL_INSTALL_UNVERIFIABLE
    if not _matches_recorded(dest_dir, recorded, installed):
        return SKILL_INSTALL_EDITED
    packaged = _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True)
    if packaged is None:
        return SKILL_INSTALL_UNVERIFIABLE
    return SKILL_INSTALL_IN_SYNC if recorded == packaged else SKILL_INSTALL_BEHIND


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


def _linked_component(base: Path, name: str) -> Path | None:
    """The first directory of *name* under *base* that is a link or junction, if any.

    *name* is a catalog key, so a nested one (``kirocrew-dev/x``) has a family
    directory on the way. A link anywhere on that path points outside the skills
    home, and the sync must not rename or remove anything through it.
    """
    parts = Path(name).parts
    for i in range(len(parts)):
        candidate = base.joinpath(*parts[: i + 1])
        if is_link_or_junction(candidate):
            return candidate
    return None


def _dest_opted_out_of_injection(dest_dir: Path) -> bool:
    """Did the user flip the Context-budget switch OFF on this installed builtin?

    The switch writes ``inject_on_trigger: false`` into the installed
    ``SKILL.md`` frontmatter (``skill_runtime.authoring.set_inject_on_trigger``),
    the one user-mutable setting on a built-in skill. Read it from the
    destination BEFORE the update claims the directory, so the opt-out can be
    carried onto the freshly installed packaged copy.

    Returns False on any read/parse failure: a carry is a best-effort
    convenience, never a reason to abort an install. Only a top-level
    ``inject_on_trigger: false`` counts, matching the writer and
    ``_parse_frontmatter`` (an indented occurrence is prose, not the setting).
    """
    skill_file = dest_dir / "SKILL.md"
    try:
        content = safe_read_file(str(skill_file))
    except (OSError, PermissionError, ValueError):
        return False
    try:
        meta = parse_frontmatter(content, SKILL_LOADER)
    except (OSError, ValueError):
        return False
    return str(meta.get("inject_on_trigger", "")).strip().lower() == "false"


def _carried_skill_md(src_dir: Path) -> bytes | None:
    """Return the packaged ``SKILL.md`` bytes with ``inject_on_trigger: false`` applied.

    Mirrors ``skill_runtime.versions._rewrite_update_frontmatter`` and the
    auto-skill refine path: a packaged ``SKILL.md`` never carries the switch, so
    an update that reinstalls it would silently turn full-body injection back on
    for a skill the user had made pointer-only — the setting reverting itself
    behind an unrelated app update. Append the one frontmatter
    line the user set, leaving the rest of the packaged body authoritative.

    The input is the PACKAGED file, never the installed one: the carried bytes
    are staged before anything is published, so no installed file is read and
    then rewritten while a concurrent writer could replace it.

    Returns None (install the packaged file verbatim) when the packaged copy
    already opts out, so the install still equals the packaged tree the marker
    records, and on any read/decode/parse failure: a carry is best-effort,
    never a reason to fail the sync.
    """
    try:
        data = safe_read_file_bytes_nolink(str(src_dir / "SKILL.md"), within_root=str(src_dir))
    except (OSError, ValueError, FileTooLargeError):
        return None
    if data is None:
        return None
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        packaged_meta = parse_frontmatter(content, SKILL_LOADER)
    except (OSError, ValueError):
        return None
    if str(packaged_meta.get("inject_on_trigger", "")).strip().lower() == "false":
        return None
    new_content = _authoring.rewrite_inject_on_trigger(content, False)
    if new_content is None:
        return None
    return new_content.encode("utf-8")


def _copy_with_carried_skill_md(dest_dir: Path, carried: bytes) -> Callable[[str, str], str]:
    """``copytree`` copy function that publishes *carried* as the top-level ``SKILL.md``.

    Every other file is copied with ``shutil.copy2``. The top-level
    ``SKILL.md`` is created exclusively with the carried bytes and then given
    the packaged file's mode and timestamps (``shutil.copystat``), exactly as
    ``copy2`` would, so the published file differs from the packaged one only
    by the carried line. The fingerprint hashes file modes, and the carried-line
    tolerance substitutes only the size, so a mode that differed (an
    owner-only temp file, say) would read as an edit and be quarantined.

    The file is written once, as it is published. If something else created it
    first, that file is left alone rather than overwritten; the marker records
    the packaged fingerprint, so it reads as a divergence and is preserved.
    """
    target = os.path.normcase(os.path.join(os.fspath(dest_dir), "SKILL.md"))

    def _copy(src: str, dst: str) -> str:
        if os.path.normcase(os.fspath(dst)) != target:
            return shutil.copy2(src, dst)
        try:
            with open(dst, "xb") as fh:
                fh.write(carried)
        except FileExistsError:
            logger.info("%s appeared before the sync published it; keeping it", dst)
            return dst
        shutil.copystat(src, dst)
        return dst

    return _copy


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
            # Set only when a diverged destination carried the user's
            # Context-budget opt-out; applied as the packaged copy is written.
            carry_opt_out = False
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
                    # The Context-budget switch is the one user-mutable setting
                    # on a built-in skill. Read it from the CLAIMED tree, after
                    # the atomic rename aside, not from dest_dir before the
                    # claim: the claim fixes which tree is verified and retired,
                    # so a dashboard toggle landing in that window is reflected
                    # here instead of being read stale and then discarded.
                    carry_opt_out = _dest_opted_out_of_injection(claim)
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
            # The carried opt-out is staged from the PACKAGED SKILL.md and
            # written by the copy itself, so the published file is created
            # once, with the packaged mode, and never read back and rewritten.
            carried = _carried_skill_md(src_dir) if carry_opt_out else None
            try:
                if carried is None:
                    shutil.copytree(src_dir, dest_dir)
                else:
                    shutil.copytree(
                        src_dir,
                        dest_dir,
                        copy_function=_copy_with_carried_skill_md(dest_dir, carried),
                    )
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
    stale_builtins = {
        "learn",
        "subagent",
        "cron",
        "kirocrew-core",
        # Not shipped: retire the installed copy.
        "kirocrew-dev/kirocrew-codebase-refactor",
    } - source_names
    if base.exists():
        for name in stale_builtins:
            stale = base / name
            family = Path(name).parent
            if family.parts and _linked_component(base, family.as_posix()) is not None:
                # A nested name's family directory is a user-made link: its
                # parked slot lives inside the link target too, so nothing on
                # this path is claimed, disposed of or read.
                logger.debug("Leaving %s in place: its family directory is a link", stale)
                continue
            # Unlike update-path slots (rotated by the next update), nothing
            # ever ships for a stale name again, so its parked copy is
            # disposed of here on the sweep AFTER the one that parked it —
            # that is its full quiescent cycle. Ordered before the live-dir
            # handling below, which can park a fresh copy this same run.
            slot = stale.with_name(f".{stale.name}.superseded")
            if not stale.is_dir() and os.path.lexists(slot):
                _dispose_superseded_slot(slot, stale)
            if _linked_component(base, name) is not None:
                # The sync only ever creates real directories; a link on the
                # way (a nested name's family directory included) is user-made
                # and its target must not even be read.
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
                # A directory on the way to the old SKILL.md that is a link or
                # junction points outside the skills home. Renaming through it
                # would rename a file the operator linked in, so leave it alone.
                linked = _linked_component(base, old_name)
                if linked is not None:
                    logger.warning(
                        "Skill %s relocated to %s, but %s is a link; not "
                        "quarantining through it (the linked copy is untouched)",
                        old_name,
                        new_name,
                        linked,
                    )
                    continue
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
    ``redaction_failed``, ``promotion_disabled`` (this host cannot stage or
    promote auto-skills: see :func:`auto_skill_promotion_disabled_reason`), or
    ``promotion_failed`` (an OS-level I/O failure — a read or write, a refused
    read of the live skill's metadata included — or a claimed generation that
    changed or could not be published).
    Raised by the ``*_checked`` approve variants so
    the dashboard can tell the user WHY the click did nothing; the legacy
    ``approve_pending_skill`` / ``approve_pending_update`` wrappers keep the
    ``None``-on-failure contract for existing callers.
    """

    def __init__(self, reason: str, report: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.report = report


class PendingDismissalRefused(Exception):
    """A dismissal was refused while its pending candidate is still staged.

    ``reason`` is ``promotion_disabled``: the data home still has an auto-skill
    authority root, so a dismissal must claim the candidate under that
    authority, and this process holds no certificate for it. ``detail`` is
    :func:`auto_skill_promotion_disabled_reason`; ``hint`` names the operator
    recovery where ``kirocrew skills authority-retire`` applies to this host, and
    is ``None`` elsewhere. Raised only by ``dismiss_pending_skill_checked``; the
    unsuffixed ``dismiss_pending_skill`` keeps its ``False`` for every caller.
    """

    def __init__(self, reason: str, *, detail: str, hint: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.hint = hint


class LiveSkillMutationRefused(Exception):
    """A live auto-skill delete or pin was refused while the skill is still there.

    ``reason`` is ``promotion_disabled``: the data home still has an auto-skill
    authority root, so the mutation must take that authority's target lock, and
    this process holds no certificate for it. ``detail`` and ``hint`` carry the
    same meaning as on :class:`PendingDismissalRefused`. Raised only by
    ``delete_skill_checked`` and ``set_pinned_checked``; the unsuffixed methods
    keep their ``False`` for every caller.
    """

    def __init__(self, reason: str, *, detail: str, hint: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.hint = hint


def _note_refusal(
    refusal: list[PendingApprovalRefused] | None,
    reason: PendingApprovalRefused,
) -> None:
    """Record why a claimed promotion refused; the first reason recorded wins.

    The claim protocol returns ``None`` for every refusal so a failure can never
    be mistaken for a partial success. The ``*_checked`` approve variants still
    owe the dashboard the reason, so the refusal sites that have one note it
    here and the variant raises the first one noted. Each site constructs the
    exception with a literal reason, which the dashboard's refusal-vocabulary pin
    reads from source.
    """
    if refusal is not None and not refusal:
        refusal.append(reason)


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
        # Unconfined paths this process has VETTED, the only ones
        # `_read_enumerated_skill_bytes` reads without first checking them
        # (`_vet_unconfined_path`). Fail-closed: any other path, such as a row
        # adopted from the agent-writable stored snapshot, is checked before its
        # first read. `_walk_vetted` holds exactly the paths the newest published
        # walk returned, replaced whole by the next; `_read_vetted` holds the
        # paths a check admitted, bounded to `_VETTED_READS_MAX` and emptied by
        # every invalidation and every published walk.
        self._walk_vetted: frozenset[str] = frozenset()
        self._read_vetted: OrderedDict[str, None] = OrderedDict()
        # `_snapshot_admitted_roots`, keyed by (generation, skills dir, extra
        # paths), so a vet resolves the roots once per root set, not per read.
        self._admitted_roots_memo: tuple[tuple[object, ...], tuple[str, ...]] | None = None
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
        # The newest generation a committed `drop_catalog` covers. The stored
        # snapshot is served only while it equals `_catalog_generation`: an
        # invalidation moves the generation before it drops, so the tier is off
        # while that drop runs, and a drop that FAILS (a neighbour held the write
        # lock past the busy timeout) leaves it behind until a later one lands.
        self._snapshot_clean_generation = 0
        # Held across an invalidation's drop and across a build's store, so the
        # two never interleave: a drop always lands after a store a mutation
        # overtook, and a drop one of them landed is not repeated by the other.
        # Ordered before the index's own lock, never inside it.
        self._catalog_drop_lock = threading.Lock()
        self._closed = False
        self._disabled_apps_cache: tuple[float, frozenset[str]] | None = None
        # (canonical key, allowed) pairs already audited, so the enforcement
        # record is written on first use rather than once per message.
        self._audited_projects: set[tuple[str, bool]] = set()
        # Whether the unsupported-platform warning has been considered, so it
        # is logged at most once per loader rather than once per message.
        self._project_skills_unsupported_warned = False
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
        if (
            not allowed
            and not self._project_skills_unsupported_warned
            and not skill_trust.project_skill_traversal_supported()
        ):
            # Without the no-follow directory-descriptor walk (Windows) the gate
            # refuses every project before touching its path, so the operator
            # otherwise sees no skills, no trust prompt and no reason. Name the
            # platform limit, not a project: probing whether `.kiro/skills`
            # exists would be the very path lookup the gate refuses to make.
            # Once per loader, and silent when the operator switched it off.
            self._project_skills_unsupported_warned = True
            if skill_trust.project_skills_enabled():
                logger.warning(
                    "project skills (<project>/.kiro/skills) are not loaded on this "
                    "platform: it lacks the no-follow directory-descriptor traversal "
                    "the project-skill trust gate requires, so no project can be "
                    "trusted here and no trust prompt is offered; global skills are "
                    "unaffected"
                )
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
                    else (
                        "project skills withheld: this platform lacks no-follow "
                        "directory traversal"
                        if not skill_trust.project_skill_traversal_supported()
                        else "project skills withheld: no grant, or the feature is off"
                    )
                ),
                critical=False,
            )
            self._audited_projects.add(marker)
        except Exception:  # noqa: BLE001 — an unwritable log must not fail a turn
            logger.warning("could not audit project-skills enforcement", exc_info=True)

    def _iter(self, project_dir: str | Path | None = None) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` triples without walking the tree."""
        return _catalog._iter(self, project_dir)

    def _catalog_scope_key(self, project_dir: str | Path | None) -> str:
        """The scope this request reads, as a string the snapshot layer can key on."""
        return _catalog._catalog_scope_key(self, project_dir)

    def catalog_status(self, project_dir: str | Path | None = None) -> str:
        """``"complete"`` or ``"building"`` for the scope *project_dir* selects.

        Contract and rationale: ``skill_runtime.catalog.catalog_status``.
        """
        return _catalog.catalog_status(self, project_dir)

    def _catalog_fingerprint_hint(self, project_dir: str | Path | None) -> dict[str, str]:
        """Stat fingerprints from the walk that produced this scope's list."""
        return _catalog._catalog_fingerprint_hint(self, project_dir)

    def _catalog_scope_id(self, project_key: str) -> str:
        """Stable identity of the ROOT SET a stored snapshot belongs to."""
        return _catalog._catalog_scope_id(self, project_key)

    def _snapshot_admitted_roots(self) -> tuple[str, ...]:
        """Roots an unconfined row read off disk may legitimately name."""
        return _catalog._snapshot_admitted_roots(self)

    def _load_catalog_snapshot(self, project_key: str) -> _StoredCatalog | None:
        """Read this scope's stored enumeration, or ``None`` when there is none."""
        return _catalog._load_catalog_snapshot(self, project_key)

    def _vet_unconfined_path(self, path: Path) -> bool:
        """May *path* be read unconfined? Checks it once unless a walk returned it."""
        return _catalog._vet_unconfined_path(self, path)

    @staticmethod
    def _key_denotes_path(
        key: str, absolute: str, own_roots: tuple[Path, ...], provider_roots: tuple[str, ...]
    ) -> bool:
        """Does *key* name the skill that *absolute* holds?"""
        return _catalog._key_denotes_path(key, absolute, own_roots, provider_roots)

    def _adopt_snapshot(
        self, project_key: str, snapshot: _StoredCatalog, *, generation: int
    ) -> list[tuple[str, Path, str | None]] | None:
        """Serve a stored enumeration for *project_key*, or ``None`` when it is fenced."""
        return _catalog._adopt_snapshot(self, project_key, snapshot, generation=generation)

    def _request_catalog_refresh(self, project_key: str) -> threading.Event | None:
        """Queue one background walk of *project_key*'s roots; join any in flight."""
        return _catalog._request_catalog_refresh(self, project_key)

    def _catalog_worker_loop(self) -> None:
        """Drain queued scopes one at a time until this loader closes."""
        return _catalog._catalog_worker_loop(self)

    def _run_catalog_build(self, project_key: str, generation: int) -> None:
        """Walk *project_key*'s roots off the request path and publish the result."""
        return _catalog._run_catalog_build(self, project_key, generation)

    @staticmethod
    def _catalog_fingerprints_for(
        rows: list[tuple[str, Path, str | None]],
    ) -> dict[str, str]:
        """Stat each unconfined row once, so ``list_skills`` need not stat again."""
        return _catalog._catalog_fingerprints_for(rows)

    def _get_disabled_app_names(self) -> frozenset[str]:
        return _catalog._get_disabled_app_names(self)

    def _iter_visible(
        self, project_dir: str | Path | None = None
    ) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` pairs, filtering out disabled app skills."""
        return _catalog._iter_visible(self, project_dir)

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
                    **self._usage_fields(name),
                    "owned": False,
                }
            )
        return skills

    def _iter_uncached(self, project_key: str | None = None) -> list[tuple[str, Path, str | None]]:
        """Walk the skills dir, extra paths, and an already-canonical project root."""
        return _catalog._iter_uncached(self, project_key)

    def _invalidate_iter_cache(self) -> None:
        """Drop cached skill state so a just-written mutation is visible now."""
        return _catalog._invalidate_iter_cache(self)

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
            # A path this process never walked carries no admission: a row from the
            # stored snapshot came out of an agent-writable crew-home leaf, and the
            # direct read below applies no sensitive-path or UNC screen of its own,
            # so a row naming a link into a credential home would be read as a skill
            # body. The walk's own admission is therefore run on every path that is
            # not already vetted (`_vet_unconfined_path`), at the single point every
            # enumerated read goes through. Fail-closed: a path missing from the
            # vetted sets costs one check, never an unchecked read.
            if not self._vet_unconfined_path(path):
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
        for_write: bool = False,
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

        *for_write* is set by a caller that rewrites the file from what it reads
        here. A refused unconfined read then raises ``PermissionError`` instead of
        answering "no metadata", which would have it rewrite the skill without its
        ``version``, ``pinned``, ``inject_on_trigger`` and ``created_at``.
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
            if for_write:
                raise PermissionError(f"refusing to read skill metadata for a rewrite: {path}")
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

    def _readable_frontmatter(
        self,
        path: Path,
        *,
        within: str | None,
        mtime: float | None = None,
        canonical_root: str | None = None,
    ) -> dict[str, str] | None:
        """Frontmatter for a READER, or ``None`` when the row must be dropped.

        The reader-side counterpart of :meth:`_cached_frontmatter`, whose
        failures propagate for the writers' sake. Contract and rationale:
        ``skill_runtime.listing._readable_frontmatter``.
        """
        return _listing._readable_frontmatter(
            self,
            path,
            within=within,
            mtime=mtime,
            canonical_root=canonical_root,
        )

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

        Blocking filesystem and SQLite work: async callers must offload it. Contract and
        rationale: ``skill_runtime.listing.list_skills``.
        """
        return _listing.list_skills(self, project_dir, _entries=_entries)

    def _owning_app(self, name: str, skill_file: Path) -> str | None:
        """The app whose bundle this skill came from, or ``None``."""
        return _catalog._owning_app(self, name, skill_file)

    def _owned_hint(self, skill_file: Path) -> bool:
        """Whether *skill_file* sits under the directory Kiro Crew owns."""
        return _listing._owned_hint(self, skill_file)

    def _served_key_by_realpath(self) -> dict[str, str]:
        """Map each served skill file's realpath to its canonical served key."""
        return _read_credit._served_key_by_realpath(self)

    def resolve_tool_read_keys(
        self,
        tool_name: str = "",
        raw_params: dict | None = None,
        command: str | None = None,
    ) -> list[str]:
        """Served skill keys whose body a tool call is about to deliver.

        Filesystem-bound: callers keep it off the event loop. Contract and rationale:
        ``skill_runtime.read_credit.resolve_tool_read_keys``.
        """
        return _read_credit.resolve_tool_read_keys(self, tool_name, raw_params, command)

    def credit_skill_reads(self, keys: list[str]) -> None:
        """Record a delivery for each key in *keys*. Best-effort, never raises.

        Contract and rationale: ``skill_runtime.read_credit.credit_skill_reads``.
        """
        return _read_credit.credit_skill_reads(self, keys)

    def resolve_ledger_aliases(self) -> dict[str, list[str]]:
        """Map served skill keys to ledger keys that resolve to the same file.

        Contract and rationale: ``skill_runtime.read_credit.resolve_ledger_aliases``.
        """
        return _read_credit.resolve_ledger_aliases(self)

    def _usage_fields(self, key: str) -> dict[str, int | float | None]:
        """The listing's ``deliveries`` and ``last_used_at`` for *key*."""
        return _listing._usage_fields(self, key)

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
        refusal_reasons: list[str] | None = None,
    ) -> str | None:
        """Load a single skill's content by name (supports nested paths).

        *project_dir* additionally allows a body to come from that project's own
        trusted ``<project>/.kiro/skills``. It is probed LAST so precedence
        matches enumeration: a repository cannot serve the body for a name the
        operator already installed globally.

        *refusal_reasons*, when given, receives why a candidate that was found
        could not be served (``size_cap``, or a reader's refusal); a ``None``
        with nothing appended means no tier holds the name at all.
        """
        if not self._safe_name(name):
            return None
        _t0 = time.monotonic()
        skill_file = self._dir / name / "SKILL.md"
        if skill_file.exists():
            content = self._read_global_skill_text(
                skill_file, max_bytes, refusal_reasons=refusal_reasons
            )
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
                    if refusal_reasons is not None:
                        refusal_reasons.append("sensitive_path")
                    continue
                content = self._read_global_skill_text(
                    Path(resolved), max_bytes, refusal_reasons=refusal_reasons
                )
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
                if refusal_reasons is None:
                    refusal_reasons = []
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
                if _html_skill_refused(self._parse_frontmatter_text(content), skill_file):
                    break
                self._emit_lazy_load_metric(_t0, hit=True)
                return content
        self._emit_lazy_load_metric(_t0, hit=False)
        return None

    def _read_global_skill_text(
        self,
        path: Path,
        max_bytes: int | None,
        *,
        canonical_root: str | None = None,
        refusal_reasons: list[str] | None = None,
    ) -> str | None:
        """A global skill body, bounded by *max_bytes* and decode-safe.

        ``max_bytes=None`` reads up to the shared file safety cap with strict
        UTF-8, preserving the global listing path's decode behavior. Every body
        read uses the shared validated reader without project confinement:
        sensitive paths, hardlinks and identity changes are refused on the
        descriptor supplying the bytes. The one hardlink admitted is an installed
        package ``SKILL.md`` whose bytes match the installer's RECORD digest
        (:func:`_installed_package_bytes_match`), which is how a hardlinking
        installer lays out the built-in app skills this method serves.

        With a bound, refuse rather than truncate. A caller that asked for at
        most N bytes is deciding whether the body FITS, and half a skill is not
        a smaller skill: it is a body whose instructions stop mid-sentence.
        ``None`` says "this one cannot be delivered", which the caller reports
        instead of silently dropping. *refusal_reasons* records which it was:
        ``size_cap`` when the body is over the bound, ``reader_refused`` when the
        validated reader declined the file itself.

        A page is the one sanctioned partial read, and it is partial only because
        the caller asked for one: :meth:`read_scoped_skill_page` reads the body
        whole under the file safety cap and hands back the whole lines that fit
        one response together with what remains and where the next page starts,
        so the reader is told there is more. A read that asked for no page is
        still refused whole. This function itself never returns part of a body.
        """
        try:
            raw = safe_read_file_bytes_nolink(
                str(path),
                max_bytes=max_bytes,
                within_root=canonical_root,
                within_root_is_canonical=canonical_root is not None,
                admit_hardlinked=_installed_package_bytes_match,
            )
        except FileTooLargeError:
            if max_bytes is None:
                logger.debug("skill body at %s exceeds the shared file-read bound; refusing", path)
            else:
                logger.debug(
                    "skill body at %s exceeds the %d byte bound; refusing", path, max_bytes
                )
            if refusal_reasons is not None:
                refusal_reasons.append("size_cap")
            return None
        if raw is None:
            if refusal_reasons is not None:
                refusal_reasons.append("reader_refused")
            return None
        text = _decode_skill_text(raw, strict=max_bytes is None)
        return None if _html_skill_refused(self._parse_frontmatter_text(text), path) else text

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
        """Create a new skill directory with SKILL.md.  Returns True on success.

        An ``auto/`` name is written under its per-target lock so it cannot
        interleave with a promotion (:data:`AUTO_SKILL_LOCK_ORDER`).
        Contract and rationale: ``skill_runtime.authoring.create_skill``.
        """
        if not self._safe_name(name):
            return False
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Create lock unavailable for %s", name)
                return False
            return _authoring.create_skill(self, name, content)

    def _create_skill_pinned(
        self, name: str, content: str, skill_dir: Path, parent_fd: int
    ) -> bool:
        """Create *skill_dir* and its SKILL.md under *parent_fd*, or leave nothing behind."""
        return _authoring._create_skill_pinned(self, name, content, skill_dir, parent_fd)

    def update_skill(self, name: str, content: str) -> bool:
        """Overwrite an existing skill's SKILL.md.  Returns True if found.

        An ``auto/`` name is written under its per-target lock (lock order:
        :data:`AUTO_SKILL_LOCK_ORDER`). Contract and rationale:
        ``skill_runtime.authoring.update_skill``.
        """
        if not self._safe_name(name):
            return False
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Update lock unavailable for %s", name)
                return False
            return _authoring.update_skill(self, name, content)

    @staticmethod
    def _write_skill_md(skill_file: Path, content: str, *, dir_fd: int | None) -> bool:
        """Atomically replace *skill_file*, carrying its access-control xattrs."""
        return _authoring._write_skill_md(skill_file, content, dir_fd=dir_fd)

    def delete_skill(self, name: str) -> bool:
        """Delete a skill directory.  Returns True if found and removed."""
        if not self._safe_name(name):
            return False
        # ``auto/`` names are removed under their target lock, and the bare
        # namespace (or an alias spelling of it) is refused outright: deleting it
        # would remove every live, pending and quarantined auto-skill entry.
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Delete lock unavailable for %s", name)
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

    def delete_skill_checked(self, name: str) -> bool:
        """Delete like :meth:`delete_skill`, naming an authority refusal.

        Returns ``True`` once the skill is removed and ``False`` when it is absent
        or its deletion failed for another reason. Raises
        ``LiveSkillMutationRefused`` when :meth:`_live_auto_mutation_refusal`
        names why the target lock was refused: the case a dashboard would
        otherwise report as "not found" for a skill the operator can still see.
        """
        if self.delete_skill(name):
            return True
        refusal = self._live_auto_mutation_refusal(name)
        if refusal is not None:
            raise refusal
        return False

    def _live_auto_mutation_refusal(self, name: str) -> LiveSkillMutationRefused | None:
        """The authority refusal behind a failed live ``auto/`` mutation, or ``None``.

        Read only after the mutation returned ``False``, so it can change which
        refusal is reported and never what is mutated. It follows the lock
        decision in :meth:`_live_auto_mutation_lock`: only an ``auto/`` name with
        a canonical promotion target takes the authority-backed target lock, and
        that lock is refused for want of a certificate only while by-name
        mutation is not ruled in and this process reports a disabled reason. A
        skill that is gone stays ``None``, so it still reads as not found.
        """
        if not self.is_auto_generated(name):
            return None
        target_slug = self._auto_slug_from_name(name)
        if (
            not self._is_pending_slug_safe(target_slug.split("/", 1)[0])
            or self._live_auto_target_lock_slug(target_slug) is None
            or _auto_skill_promotion_ruled_out()
        ):
            return None
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is None or not (self._dir / name).is_dir():
            return None
        hint = (
            _auto_skill_authority_retire_hint()
            if _auto_skill_sandbox_excludes_every_promoter()
            else None
        )
        return LiveSkillMutationRefused("promotion_disabled", detail=disabled, hint=hint)

    # ── Auto skill creation ──

    def is_auto_generated(self, name: str) -> bool:
        """Return True if *name* refers to a skill in the auto namespace.

        Contract and rationale: ``skill_runtime.auto_skills.is_auto_generated``.
        """
        return _auto_skills.is_auto_generated(self, name)

    def find_similar(
        self,
        description: str,
        threshold: float = 0.85,
        *,
        exclude: str = "",
    ) -> str | None:
        """Return the name of an existing skill whose description overlaps with *description*.

        Contract and rationale: ``skill_runtime.auto_skills.find_similar``.
        """
        return _auto_skills.find_similar(self, description, threshold, exclude=exclude)

    def create_auto_skill(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
        refusal: ClaimRefusal | None = None,
    ) -> str | None:
        """Write a new auto-generated skill under ``auto/<slug>/SKILL.md``.

        The caller passes already-redacted content. ``None`` is a refusal; a
        ``ClaimRefusal`` says whether it was a lock. The owner takes the slug
        claim lock and then the target's lock (:data:`AUTO_SKILL_LOCK_ORDER`).
        Contract and rationale: ``skill_runtime.auto_skills.create_auto_skill``.
        """
        return _auto_skills.create_auto_skill(
            self,
            slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
            refusal=refusal,
        )

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

        The caller passes already-redacted content. Runs under the target's lock
        (:data:`AUTO_SKILL_LOCK_ORDER`). Contract and rationale:
        ``skill_runtime.auto_skills.update_auto_skill``.
        """
        if not self.is_auto_generated(name):
            logger.warning(
                "Refusing to auto-refine non-auto skill: %s (not in %s/)",
                name,
                AUTO_SKILL_NAMESPACE,
            )
            return False
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Refine lock unavailable for %s", name)
                return False
            return _auto_skills.update_auto_skill(
                self,
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )

    def list_auto_skills(self) -> list[dict]:
        """Return metadata dicts for all skills under the auto namespace.

        Contract and rationale: ``skill_runtime.auto_skills.list_auto_skills``.
        """
        return _auto_skills.list_auto_skills(self)

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
        """Skill keys referenced by any cron job (best-effort, never raises)."""
        return _auto_skills._cron_referenced_skills()

    def _auto_created_ts(self, meta: dict) -> float:
        """Parse ``created_at`` frontmatter to a unix timestamp, else 0.0."""
        return _auto_skills._auto_created_ts(self, meta)

    def _auto_activity(self, key: str, path_str: str, meta: dict) -> tuple[int, float]:
        """Return ``(hits, anchor_ts)`` for an auto-skill."""
        return _auto_skills._auto_activity(self, key, path_str, meta)

    def set_pinned(self, name: str, pinned: bool) -> bool:
        """Pin/unpin an auto-skill (exempt from lifecycle eviction).

        Runs under the target's lock (:data:`AUTO_SKILL_LOCK_ORDER`). Contract and
        rationale: ``skill_runtime.authoring.set_pinned``.
        """
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Pin lock unavailable for %s", name)
                return False
            return _authoring.set_pinned(self, name, pinned)

    def set_pinned_checked(self, name: str, pinned: bool) -> bool:
        """Pin like :meth:`set_pinned`, raising ``LiveSkillMutationRefused`` the way
        :meth:`delete_skill_checked` does instead of returning ``False``."""
        if self.set_pinned(name, pinned):
            return True
        refusal = self._live_auto_mutation_refusal(name)
        if refusal is not None:
            raise refusal
        return False

    def set_inject_on_trigger(self, name: str, inject: bool) -> bool:
        """Opt a skill in or out of full-body injection on a trigger match.

        An ``auto/`` skill is rewritten under its target lock
        (:data:`AUTO_SKILL_LOCK_ORDER`). Contract and rationale:
        ``skill_runtime.authoring.set_inject_on_trigger``.
        """
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Injection lock unavailable for %s", name)
                return False
            return _authoring.set_inject_on_trigger(self, name, inject)

    def _archive_root(self) -> Path:
        return _auto_skills._archive_root(self)

    @staticmethod
    def _is_pending_slug_safe(slug: str) -> bool:
        """Strict guard for a single-segment auto-skill slug."""
        return _auto_skills._is_pending_slug_safe(slug)

    def archive_auto_skill(self, name: str) -> bool:
        """Move an auto-skill into the archive (recoverable, never deleted).

        Runs under the target's lock (:data:`AUTO_SKILL_LOCK_ORDER`). Contract and
        rationale: ``skill_runtime.auto_skills.archive_auto_skill``.
        """
        with self._live_auto_mutation_lock(name) as acquired:
            if not acquired:
                logger.warning("Archive lock unavailable for %s", name)
                return False
            return _auto_skills.archive_auto_skill(self, name)

    def _archive_auto_skill_lifecycle_locked(self, name: str) -> bool:
        """Archive after lifecycle has acquired the target lock and checked recovery."""
        return _auto_skills.archive_auto_skill(self, name)

    def restore_auto_skill(self, slug: str) -> str | None:
        """Restore an archived auto-skill back to ``auto/<slug>``.

        ``None`` is a refusal: not found, a live name clash, or a lock. The owner
        takes the slug claim lock and then the target's lock
        (:data:`AUTO_SKILL_LOCK_ORDER`). Contract and rationale:
        ``skill_runtime.auto_skills.restore_auto_skill``.
        """
        return _auto_skills.restore_auto_skill(self, slug)

    def list_archived_auto_skills(self) -> list[dict]:
        """Return ``{slug, path}`` for every archived auto-skill.

        Contract and rationale: ``skill_runtime.auto_skills.list_archived_auto_skills``.
        """
        return _auto_skills.list_archived_auto_skills(self)

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

        Contract and rationale: ``skill_runtime.auto_skills.run_skill_lifecycle``.
        """
        return _auto_skills.run_skill_lifecycle(
            self,
            max_auto_skills=max_auto_skills,
            stale_after_days=stale_after_days,
            archive_after_days=archive_after_days,
            cron_referenced=cron_referenced,
            exempt=exempt,
            now=now,
        )

    # ── Auto skill staging: pending-approval queue ──

    def _pending_root(self) -> Path:
        return _auto_skills._pending_root(self)

    def _quarantine_root(self) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / AUTO_QUARANTINE_DIRNAME

    def _live_quarantine_root(self) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / AUTO_LIVE_QUARANTINE_DIRNAME

    def _private_root(self) -> Path:
        return (
            config_dir().resolve(strict=True)
            / _AUTHORITY_PROVENANCE_PARENT
            / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        )

    def _authority_provenance_path(self) -> Path:
        return (
            config_dir().resolve(strict=True)
            / _AUTHORITY_PROVENANCE_PARENT
            / _AUTHORITY_PROVENANCE_NAME
        )

    @staticmethod
    def _tag_opened_identity(fd: int) -> _TaggedFileIdentity | None:
        raw = platform_compat.opened_file_identity(fd)
        if raw is None:
            return None
        volume, object_id = raw
        if platform_compat.IS_WINDOWS:
            if (
                not isinstance(object_id, bytes)
                or not object_id.startswith(b"F128")
                or len(object_id) != 20
            ):
                return None
            return _TaggedFileIdentity("windows-file-id-128", int(volume), object_id[4:])
        if not isinstance(object_id, int):
            return None
        return _TaggedFileIdentity("posix-dev-ino", int(volume), int(object_id))

    @staticmethod
    def _identity_payload(identity: _TaggedFileIdentity) -> dict[str, object]:
        if identity.kind == "windows-file-id-128" and isinstance(identity.object_id, bytes):
            return {
                "kind": identity.kind,
                "volume": identity.volume,
                "file_id": base64.b64encode(identity.object_id).decode("ascii"),
            }
        if identity.kind == "posix-dev-ino" and isinstance(identity.object_id, int):
            return {
                "kind": identity.kind,
                "device": identity.volume,
                "inode": identity.object_id,
            }
        raise ValueError("unsupported file identity")

    @staticmethod
    def _identity_from_payload(value: object) -> _TaggedFileIdentity | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        if kind == "posix-dev-ino":
            device = value.get("device")
            inode = value.get("inode")
            if isinstance(device, int) and device >= 0 and isinstance(inode, int) and inode >= 0:
                return _TaggedFileIdentity(kind, device, inode)
            return None
        if kind == "windows-file-id-128":
            volume = value.get("volume")
            encoded = value.get("file_id")
            if not isinstance(volume, int) or volume < 0 or not isinstance(encoded, str):
                return None
            try:
                file_id = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                return None
            if len(file_id) != 16:
                return None
            return _TaggedFileIdentity(kind, volume, file_id)
        return None

    def _legacy_private_root(self) -> Path:
        return self._dir / AUTO_SKILL_NAMESPACE / AUTO_PRIVATE_DIRNAME

    def _claims_root(self) -> Path:
        return self._private_root() / AUTO_CLAIMS_DIRNAME

    def _evidence_root(self) -> Path:
        return self._private_root() / AUTO_EVIDENCE_DIRNAME

    def _locks_root(self) -> Path:
        return self._private_root() / AUTO_LOCKS_DIRNAME

    @staticmethod
    def _pinned_parent_matches(pin: _PinnedSkillParent) -> bool:
        """Whether *pin.path* still names the directory held by *pin.fd*."""
        try:
            opened = os.fstat(pin.fd)
            opened_identity = SkillsLoader._tag_opened_identity(pin.fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened_identity is None
                or opened_identity != pin.native_identity
            ):
                return False
            if platform_compat.IS_WINDOWS:
                return platform_compat.opened_path_identity_matches(pin.fd, pin.path)
            if (opened.st_dev, opened.st_ino) != pin.identity:
                return False
            named = os.stat(pin.path, follow_symlinks=False)
            return stat.S_ISDIR(named.st_mode) and os.path.samestat(opened, named)
        except (OSError, ValueError):
            return False

    def _sync_pinned_parent(self, parent: _PinnedSkillParent) -> None:
        """Durably sync one authenticated parent without reopening its name."""
        if not self._pinned_parent_matches(parent):
            raise OSError("skill-state parent changed before directory sync")
        fsync_dir_fd(parent.fd, parent.path)
        if not self._pinned_parent_matches(parent):
            raise OSError("skill-state parent changed during directory sync")

    def _sync_pinned_rename_parents(
        self,
        source: _PinnedSkillParent,
        destination: _PinnedSkillParent,
    ) -> None:
        """Sync both parents after a cross-directory authority rename.

        Destination first: once its name is durable, a source-side failure can
        at worst resurrect a duplicate. Sync the source next so recovery never
        has to distinguish that resurrection from an interrupted transition.
        """
        self._sync_pinned_parent(destination)
        if source.native_identity != destination.native_identity:
            self._sync_pinned_parent(source)

    @staticmethod
    def _opened_path_matches(fd: int, path: Path, expected: os.stat_result) -> bool:
        """Authenticate an opened entry against a pre-open lstat result.

        Windows CRT inode fields are not a stable comparison between a by-name
        stat and a handle. Once the native no-reparse identity check authenticates
        the held descriptor, it is the authority there. POSIX retains ``samestat``
        because its descriptor-relative paths depend on that inode comparison.
        """
        if platform_compat.IS_WINDOWS:
            return platform_compat.opened_path_identity_matches(fd, path)
        return os.path.samestat(expected, os.fstat(fd))

    @contextlib.contextmanager
    def _pin_skill_parent(self, path: Path) -> Iterator[_PinnedSkillParent]:
        """Open one real directory and keep its name-to-identity binding live."""
        if _DIR_FD_SUPPORTED:
            fd = pinned_fs.open_dir_pinned(path, what="skill-state parent", refusal=OSError)
        else:
            fd = platform_compat.pin_directory(path)
        try:
            opened = os.fstat(fd)
            native_identity = self._tag_opened_identity(fd)
            if native_identity is None:
                raise OSError(f"skill-state parent identity is unavailable: {path}")
            pin = _PinnedSkillParent(
                path,
                fd,
                (opened.st_dev, opened.st_ino),
                native_identity,
            )
            if not self._pinned_parent_matches(pin):
                raise OSError(f"skill-state parent changed while opening: {path}")
            yield pin
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _pin_skill_child_parent(
        self,
        parent: _PinnedSkillParent,
        name: str,
        *,
        create: bool,
        created_out: list[bool] | None = None,
    ) -> Iterator[_PinnedSkillParent]:
        """Create or open one directory while retaining its pinned parent."""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise OSError("unsafe skill-state child directory name")
        if not self._pinned_parent_matches(parent):
            raise OSError("skill-state parent changed before child open")
        child_path = parent.path / name
        created = False
        try:
            child_info = self._stat_pinned_child(parent, name)
        except FileNotFoundError:
            if not create:
                raise
            if not self._pinned_parent_matches(parent):
                raise OSError("skill-state parent changed before child create")
            if _DIR_FD_SUPPORTED:
                os.mkdir(name, 0o700, dir_fd=parent.fd)
            else:
                os.mkdir(child_path, 0o700)
            created = True
            child_info = self._stat_pinned_child(parent, name)
        if not stat.S_ISDIR(child_info.st_mode) or is_link_or_junction(child_path):
            raise OSError(f"unsafe skill-state child directory: {child_path}")
        if not self._pinned_parent_matches(parent):
            raise OSError("skill-state parent changed before child pin")
        if _DIR_FD_SUPPORTED:
            fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
        else:
            fd = platform_compat.pin_directory(child_path)
        try:
            opened = os.fstat(fd)
            native_identity = self._tag_opened_identity(fd)
            if native_identity is None:
                raise OSError(f"skill-state child identity is unavailable: {child_path}")
            child = _PinnedSkillParent(
                child_path,
                fd,
                (opened.st_dev, opened.st_ino),
                native_identity,
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not self._pinned_parent_matches(parent)
                or not self._pinned_parent_matches(child)
                or not self._opened_path_matches(fd, child_path, child_info)
            ):
                raise OSError(f"skill-state child changed while opening: {child_path}")
            if create:
                # The child descriptor authenticates what the name currently
                # reaches; syncing that child cannot make the name durable in
                # its parent. Flush the already-held parent before any caller
                # can use this authority directory. Repeat for an existing
                # child on a create-capable path so a prior interrupted create
                # that returned before this boundary is repaired on retry.
                self._sync_pinned_parent(parent)
            if created_out is not None:
                created_out[:] = [created]
            yield child
        finally:
            os.close(fd)

    def _restrict_private_parent(self, parent: _PinnedSkillParent) -> None:
        """Make one authenticated private directory owner-only on this platform."""
        if platform_compat.IS_POSIX:
            platform_compat.fchmod_safe(parent.fd, 0o700)
        else:
            platform_compat.restrict_dir_to_owner(parent.path)
        if not self._pinned_parent_matches(parent):
            raise OSError("private skill-state root changed during permission lockdown")

    @staticmethod
    def _authority_record_body(
        configured_home: Path,
        canonical_home: Path,
        home_identity: _TaggedFileIdentity,
        root_identity: _TaggedFileIdentity,
    ) -> dict[str, object]:
        return {
            "version": _AUTHORITY_PROVENANCE_VERSION,
            "configured_home": _normalized_authority_path(configured_home),
            "canonical_home": _normalized_authority_path(canonical_home),
            "selected_home": SkillsLoader._identity_payload(home_identity),
            "authority_root": SkillsLoader._identity_payload(root_identity),
        }

    @staticmethod
    def _authority_record_mac(body: dict[str, object]) -> str:
        from kiro_crew.dashboard import token_secret

        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(
            token_secret._get_secret(),
            _AUTHORITY_PROVENANCE_DOMAIN + encoded,
            hashlib.sha256,
        ).hexdigest()

    def _read_pinned_regular_file(
        self,
        parent: _PinnedSkillParent,
        name: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        fd: int | None = None
        try:
            before = self._stat_pinned_child(parent, name)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            path = parent.path / name
            if platform_compat.IS_WINDOWS:
                fd = platform_compat.open_file_no_reparse(path)
            else:
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                if _DIR_FD_SUPPORTED:
                    fd = os.open(name, flags, dir_fd=parent.fd)
                else:
                    fd = os.open(path, flags)
            opened = os.fstat(fd)
            if (
                not self._opened_path_matches(fd, path, before)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                return None
            captured = self._stable_file_payload(fd, opened, max_bytes=max_bytes)
            if captured is None or not self._pinned_parent_matches(parent):
                return None
            return captured[0]
        except OSError:
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def _authority_record_valid(
        self,
        provenance_parent: _PinnedSkillParent,
        configured_home: Path,
        canonical_home: Path,
        home_identity: _TaggedFileIdentity,
        root_identity: _TaggedFileIdentity,
    ) -> bool:
        raw = self._read_pinned_regular_file(
            provenance_parent,
            _AUTHORITY_PROVENANCE_NAME,
            max_bytes=_AUTHORITY_PROVENANCE_MAX_BYTES,
        )
        if raw is None:
            return False
        try:
            record = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError):
            return False
        if not isinstance(record, dict):
            return False
        mac = record.pop("mac", None)
        if not isinstance(mac, str):
            return False
        expected_body = self._authority_record_body(
            configured_home,
            canonical_home,
            home_identity,
            root_identity,
        )
        if record != expected_body:
            return False
        try:
            expected_mac = self._authority_record_mac(expected_body)
            return hmac.compare_digest(mac, expected_mac)
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _authority_handoff(reason: str) -> OSError:
        return OSError(
            errno.EBUSY,
            "auto-skill private authority requires stopped-installation recovery: "
            f"{reason}; stop every Kiro Crew gateway and agent using this data home, "
            "preserve the existing authority root and provenance record untouched, "
            "and retry only after operator inspection",
        )

    def _ensure_private_authority(
        self,
        canonical_home: Path,
        *,
        configured_home: Path | None = None,
        create: bool,
    ) -> _CertifiedAuthorityBinding:
        """Verify or provision authority beneath the pre-existing hidden parent."""
        configured = configured_home or canonical_home
        configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
        authority_parent_path = canonical_home / _AUTHORITY_PROVENANCE_PARENT
        private_path = authority_parent_path / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        provenance_path = authority_parent_path / _AUTHORITY_PROVENANCE_NAME
        if not is_sensitive_path(str(private_path)) or not is_sensitive_path(str(provenance_path)):
            raise OSError("auto-skill authority or provenance is not agent-denied")
        with contextlib.ExitStack() as stack:
            home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
            home_key = _normalized_authority_path(configured)
            canonical_key = _normalized_authority_path(canonical_home)
            selected_binding = (canonical_key, home_parent.native_identity)
            with _AUTHORITY_HOME_IDENTITIES_LOCK:
                selected = _AUTHORITY_HOME_IDENTITIES.get(home_key)
                if selected is not None and selected != selected_binding:
                    raise self._authority_handoff(
                        "selected data-home path or identity changed during this process"
                    )
                _AUTHORITY_HOME_IDENTITIES.setdefault(home_key, selected_binding)
            provenance_parent = stack.enter_context(
                self._pin_skill_child_parent(
                    home_parent,
                    _AUTHORITY_PROVENANCE_PARENT,
                    create=create,
                )
            )
            try:
                root_info = self._stat_pinned_child(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                )
            except FileNotFoundError:
                root_info = None
            try:
                self._stat_pinned_child(
                    provenance_parent,
                    _AUTHORITY_PROVENANCE_NAME,
                )
                record_exists = True
            except FileNotFoundError:
                record_exists = False

            stale_reason: str | None = None
            if root_info is None:
                if record_exists:
                    stale_reason = "provenance exists but authority root is absent"
            elif not stat.S_ISDIR(root_info.st_mode) or is_link_or_junction(private_path):
                raise self._authority_handoff("authority root is linked or not a directory")
            elif not record_exists:
                stale_reason = "authority root has no certified provenance"
            else:
                with self._pin_skill_child_parent(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                ) as existing:
                    if self._authority_record_valid(
                        provenance_parent,
                        configured,
                        canonical_home,
                        home_parent.native_identity,
                        existing.native_identity,
                    ):
                        self._restrict_private_parent(existing)
                        if (
                            not self._pinned_parent_matches(home_parent)
                            or not self._pinned_parent_matches(existing)
                            or not self._pinned_parent_matches(provenance_parent)
                        ):
                            raise self._authority_handoff(
                                "certified authority changed during final verification"
                            )
                        return _CertifiedAuthorityBinding(
                            configured_home=configured,
                            canonical_home=canonical_home,
                            home_identity=home_parent.native_identity,
                            root_path=existing.path,
                            root_identity=existing.native_identity,
                            provenance_path=provenance_path,
                        )
                stale_reason = (
                    "authority provenance, selected-home identity, or root identity mismatches"
                )
            if stale_reason is not None:
                if not create:
                    raise self._authority_handoff(stale_reason)
                # A restored backup, a host move, a regenerated token key or a
                # renumbering remount all land here with nothing wrong in the
                # data. Reseed when no claim is in flight; otherwise refuse with
                # the exact recovery (``_set_aside_stale_authority``).
                self._set_aside_stale_authority(
                    provenance_parent,
                    stale_reason,
                    root_present=root_info is not None,
                    record_present=record_exists,
                )

            if not create:
                raise self._authority_handoff(
                    "authority root is absent; the startup initializer must provision it"
                )
            created_out: list[bool] = []
            private = stack.enter_context(
                self._pin_skill_child_parent(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=True,
                    created_out=created_out,
                )
            )
            if created_out != [True]:
                raise self._authority_handoff("authority root appeared before exclusive create")
            self._restrict_private_parent(private)
            self._sync_pinned_parent(provenance_parent)
            home_identity = home_parent.native_identity
            root_identity = private.native_identity
            body = self._authority_record_body(
                configured,
                canonical_home,
                home_identity,
                root_identity,
            )
            body["mac"] = self._authority_record_mac(body)
            self._write_pinned_new_file(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
                (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
                mode=0o600,
            )
            self._sync_pinned_parent(provenance_parent)
            if not self._authority_record_valid(
                provenance_parent,
                configured,
                canonical_home,
                home_identity,
                root_identity,
            ):
                raise self._authority_handoff("fresh provenance did not verify")
            if (
                not self._pinned_parent_matches(home_parent)
                or not self._pinned_parent_matches(private)
                or not self._pinned_parent_matches(provenance_parent)
            ):
                raise self._authority_handoff("fresh authority changed after provenance durability")
            return _CertifiedAuthorityBinding(
                configured_home=configured,
                canonical_home=canonical_home,
                home_identity=home_identity,
                root_path=private.path,
                root_identity=root_identity,
                provenance_path=provenance_path,
            )

    def _stale_authority_claims(self, provenance_parent: _PinnedSkillParent) -> list[str] | None:
        """Names of in-flight stale claims, or ``None`` when inspection is unknown.

        Every directory scan is bounded by ``_STALE_CLAIM_SCAN_LIMIT``. Public
        candidate and old-live quarantines, plus retained claim-lock names, are
        terminal only when the stale authority's private evidence directory has
        the same claim name. An unmatched name is an interrupted claim, including
        the crash window after pending-to-quarantine rename and before private
        claim materialization. Nothing in the stale root is trusted as authority.
        """

        def child_names(
            parent: _PinnedSkillParent,
            name: str,
            *,
            label: str,
        ) -> set[str]:
            try:
                with self._pin_skill_child_parent(parent, name, create=False) as child:
                    target: int | Path = child.fd if _DIR_FD_SUPPORTED else child.path
                    return _bounded_stale_claim_names(target, label=label)
            except FileNotFoundError:
                return set()

        retired_roots = {AUTO_CLAIMS_DIRNAME, AUTO_EVIDENCE_DIRNAME, AUTO_LOCKS_DIRNAME}
        try:
            live: set[str] = set()
            claims: set[str] = set()
            evidence: set[str] = set()
            claim_locks: set[str] = set()
            try:
                with self._pin_skill_child_parent(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                ) as root:
                    listing: int | Path = root.fd if _DIR_FD_SUPPORTED else root.path
                    top = _bounded_stale_claim_names(listing, label="authority root")
                    live = {
                        name
                        for name in top - retired_roots
                        if not name.startswith(_RENAME_PROBE_PREFIX)
                    }
                    claims = {
                        name
                        for name in child_names(root, AUTO_CLAIMS_DIRNAME, label="claims")
                        if not name.startswith(_RENAME_PROBE_PREFIX)
                    }
                    evidence = child_names(root, AUTO_EVIDENCE_DIRNAME, label="evidence")
                    try:
                        with self._pin_skill_child_parent(
                            root,
                            AUTO_LOCKS_DIRNAME,
                            create=False,
                        ) as locks:
                            claim_locks = child_names(
                                locks,
                                AUTO_CLAIMS_DIRNAME,
                                label="claim locks",
                            )
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                # A missing private root carries no evidence that any public
                # quarantine entry retired, so every such entry remains in flight.
                pass

            claim_surfaces: dict[str, set[str]] = {name: {name} for name in claims}
            public_quarantine: set[str] = set()
            public_live_quarantine: set[str] = set()
            if os.path.lexists(self._dir):
                with self._pin_skill_parent(self._dir.resolve(strict=True)) as skills_parent:
                    try:
                        with self._pin_skill_child_parent(
                            skills_parent,
                            AUTO_SKILL_NAMESPACE,
                            create=False,
                        ) as auto_parent:
                            public_quarantine = child_names(
                                auto_parent,
                                AUTO_QUARANTINE_DIRNAME,
                                label="public candidate quarantine",
                            )
                            public_live_quarantine = child_names(
                                auto_parent,
                                AUTO_LIVE_QUARANTINE_DIRNAME,
                                label="public live quarantine",
                            )
                    except FileNotFoundError:
                        pass

            for name in public_quarantine - evidence:
                claim_surfaces.setdefault(name, set()).add(f"{AUTO_QUARANTINE_DIRNAME}/{name}")
            for name in public_live_quarantine - evidence:
                claim_surfaces.setdefault(name, set()).add(f"{AUTO_LIVE_QUARANTINE_DIRNAME}/{name}")
            for lock_name in claim_locks:
                claim_name = lock_name.removesuffix(".lock")
                if not lock_name.endswith(".lock") or claim_name not in evidence:
                    claim_surfaces.setdefault(claim_name, set()).add(
                        f"{AUTO_LOCKS_DIRNAME}/{AUTO_CLAIMS_DIRNAME}/{lock_name}"
                    )
            live.update("; ".join(sorted(surfaces)) for surfaces in claim_surfaces.values())
            return sorted(live)
        except _StaleClaimScanOverflow:
            raise
        except (OSError, RuntimeError, ValueError):
            return None

    def _set_aside_stale_authority(
        self,
        provenance_parent: _PinnedSkillParent,
        reason: str,
        *,
        root_present: bool,
        record_present: bool,
    ) -> None:
        """Move an idle stale authority and its record aside, or refuse with recovery.

        Provenance binds the selected home's and the root's native identities
        under ``token_signing.key``, so a restored backup, a ``cp -a`` or rsync
        host move, a regenerated key or a remount that renumbers devices leaves it
        unverifiable for good. A stale root is never trusted again. With no claim
        in flight it is renamed aside intact under a ``.stale-<token>`` name in the
        same masked parent, never read for authority and never deleted, and the
        caller provisions a fresh root: the quarantine-and-reseed the ``chat_tag``
        grant store beside it already applies. An in-flight claim's journal lives
        in that untrusted root, so startup refuses and names the exact recovery.
        """
        scan_problem: str | None = None
        try:
            live = self._stale_authority_claims(provenance_parent)
        except _StaleClaimScanOverflow as exc:
            live = None
            scan_problem = str(exc)
        if live is None or live:
            in_flight = (
                (
                    f"its stale claim state is indeterminate ({scan_problem})"
                    if scan_problem is not None
                    else "its claims could not be inspected"
                )
                if live is None
                else f"{len(live)} claim(s) are still in flight ({', '.join(live[:3])})"
            )
            authority = f"{_AUTHORITY_PROVENANCE_PARENT}/{AUTO_SKILL_PRIVATE_STATE_DIRNAME}"
            record = f"{_AUTHORITY_PROVENANCE_PARENT}/{_AUTHORITY_PROVENANCE_NAME}"
            pending = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_PENDING_DIRNAME}"
            quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_QUARANTINE_DIRNAME}"
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority provenance no longer matches this data home ({reason}) "
                f"and {in_flight}, so startup will not reseed it. To recover: stop every "
                f"Kiro Crew gateway and agent using this data home; move {authority} and "
                f"{record} out of the data home, keeping both for inspection; to requeue "
                f"an in-flight candidate for review, move its {quarantine}/<slug>--<token> "
                f"directory back to {pending}/<slug>; then start the gateway, which "
                "provisions a fresh authority. Auto-skill staging and promotion stay off "
                "until then",
            )
        token = secrets.token_hex(8)
        for present, name in (
            (root_present, AUTO_SKILL_PRIVATE_STATE_DIRNAME),
            (record_present, _AUTHORITY_PROVENANCE_NAME),
        ):
            if present:
                self._rename_skill_child_no_replace(
                    provenance_parent,
                    name,
                    provenance_parent,
                    f"{name}.stale-{token}",
                )
        self._sync_pinned_parent(provenance_parent)
        logger.warning(
            "Auto-skill authority provenance did not verify (%s) and no claim was in "
            "flight: the previous authority was moved aside as *.stale-%s under %s and a "
            "fresh authority is provisioned",
            reason,
            token,
            provenance_parent.path,
        )

    def _migrate_legacy_private_state(
        self,
        canonical_skills: Path,
        canonical_home: Path,
    ) -> None:
        """Refuse obsolete authority until a stopped installation handles it offline.

        The refusal guards FIRST creation only. Once the masked
        ``tag-grants/auto-skill-private`` root exists, this installation already
        passed that boundary, and both obsolete spellings are agent-writable: a
        directory at either one was planted (or left by a downgraded build) and
        holds nothing this installation trusts. It is logged once, never opened
        or read, and certification proceeds. Refusing it instead would let an
        agent withhold the certificate across every restart, and without one each
        by-name live mutation fails closed on its target lock, so a planted
        ``always: true`` auto-skill could not be removed from any product surface.
        """
        authority = canonical_home / _AUTHORITY_PROVENANCE_PARENT / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        transitional_direct = canonical_home / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        legacy = canonical_skills / AUTO_SKILL_NAMESPACE / AUTO_PRIVATE_DIRNAME
        if os.path.lexists(authority):
            for obsolete in (transitional_direct, legacy):
                if os.path.lexists(obsolete):
                    _note_inert_obsolete_authority_spelling(obsolete)
            return
        if os.path.lexists(transitional_direct):
            with self._pin_skill_parent(canonical_home) as home_parent:
                direct_info = self._stat_pinned_child(
                    home_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                )
                if not stat.S_ISDIR(direct_info.st_mode) or is_link_or_junction(
                    transitional_direct
                ):
                    raise OSError(
                        errno.EXDEV,
                        "transitional direct auto-skill private state is unsafe",
                    )
                with self._pin_skill_child_parent(
                    home_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                ) as direct_parent:
                    listing_target: int | Path = (
                        direct_parent.fd if _DIR_FD_SUPPORTED else direct_parent.path
                    )
                    with os.scandir(listing_target) as entries:
                        populated = next(entries, None) is not None
            state = "populated" if populated else "empty"
            action = (
                "move the complete direct root outside the data home for offline recovery"
                if populated
                else "remove the empty direct root"
            )
            raise OSError(
                errno.EBUSY,
                f"{state} transitional direct auto-skill private state requires "
                "stopped-installation recovery: stop every Kiro Crew gateway and agent "
                f"using this data home, {action}, and retry; never move it beneath "
                "tag-grants",
            )

        if not os.path.lexists(legacy):
            return
        with (
            self._pin_skill_parent(canonical_home) as home_parent,
            self._pin_skill_parent(legacy.parent) as auto_parent,
        ):
            legacy_info = self._stat_pinned_child(auto_parent, AUTO_PRIVATE_DIRNAME)
            if not stat.S_ISDIR(legacy_info.st_mode) or is_link_or_junction(legacy):
                raise OSError(errno.EXDEV, "legacy private skill state is unsafe or cross-device")

            with self._pin_skill_child_parent(
                auto_parent,
                AUTO_PRIVATE_DIRNAME,
                create=False,
            ) as legacy_parent:
                if legacy_parent.native_identity.volume != home_parent.native_identity.volume:
                    raise OSError(
                        errno.EXDEV,
                        "legacy private skill state is unsafe or cross-device",
                    )
                listing_target = legacy_parent.fd if _DIR_FD_SUPPORTED else legacy_parent.path
                with os.scandir(listing_target) as entries:
                    populated = next(entries, None) is not None
            if populated:
                raise OSError(
                    errno.EBUSY,
                    "populated legacy auto-skill private state requires stopped-installation "
                    "migration: stop every Kiro Crew gateway and agent using this data home, "
                    "move skills/auto/.private outside the data home for offline recovery, "
                    "and retry; never move it beneath tag-grants",
                )
            raise OSError(
                errno.EBUSY,
                "empty legacy auto-skill private state requires stopped-installation "
                "migration: stop every Kiro Crew gateway and agent using this data home, "
                "remove the empty skills/auto/.private directory, and retry so "
                "tag-grants/auto-skill-private is created with fresh inodes",
            )

    def _preflight_private_state(
        self,
        *,
        require_sensitive: bool,
    ) -> tuple[Path, _CertifiedAuthorityBinding]:
        """Authenticate public and direct private ancestry before mutation."""
        binding = require_auto_skill_private_authority()
        canonical_home = binding.canonical_home
        skills_root_exists = os.path.lexists(self._dir)
        if is_link_or_junction(self._dir):
            raise OSError("skills root is a link or reparse point")
        canonical_skills = self._dir.resolve(strict=skills_root_exists)
        home_info = os.lstat(canonical_home)
        if not stat.S_ISDIR(home_info.st_mode) or is_link_or_junction(canonical_home):
            raise OSError("crew data home is not one real directory")

        # The obsolete authority spellings are checked at startup certification
        # only (``initialize_auto_skill_private_authority``), and refused there
        # only before the hidden-parent authority first exists. Both are agent-
        # writable and hold nothing trusted once it does, so a check here would
        # let a planted empty directory refuse every live mutation.
        private_root = self._private_root()
        projected_private = (
            private_root.resolve(strict=True)
            if os.path.lexists(private_root)
            else (canonical_home / _AUTHORITY_PROVENANCE_PARENT / AUTO_SKILL_PRIVATE_STATE_DIRNAME)
        )
        if require_sensitive and (
            not is_sensitive_path(str(private_root))
            or not is_sensitive_path(str(projected_private))
        ):
            raise OSError("private root is not agent-denied")

        with contextlib.ExitStack() as stack:
            if skills_root_exists:
                skills_parent = stack.enter_context(self._pin_skill_parent(canonical_skills))
                try:
                    auto_parent = stack.enter_context(
                        self._pin_skill_child_parent(
                            skills_parent,
                            AUTO_SKILL_NAMESPACE,
                            create=False,
                        )
                    )
                except FileNotFoundError:
                    auto_parent = None
                if auto_parent is not None:
                    for sibling_name in (
                        AUTO_PENDING_DIRNAME,
                        AUTO_QUARANTINE_DIRNAME,
                        AUTO_LIVE_QUARANTINE_DIRNAME,
                    ):
                        try:
                            stack.enter_context(
                                self._pin_skill_child_parent(
                                    auto_parent,
                                    sibling_name,
                                    create=False,
                                )
                            )
                        except FileNotFoundError:
                            pass

            verify_auto_skill_private_authority(binding)
            home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
            if home_parent.native_identity != binding.home_identity:
                raise self._authority_handoff(
                    "selected data-home changed between provenance verification and use"
                )
            provenance_parent = stack.enter_context(
                self._pin_skill_child_parent(
                    home_parent,
                    _AUTHORITY_PROVENANCE_PARENT,
                    create=False,
                )
            )
            try:
                private = stack.enter_context(
                    self._pin_skill_child_parent(
                        provenance_parent,
                        AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                        create=False,
                    )
                )
            except FileNotFoundError as exc:
                raise self._authority_handoff(
                    "authority root disappeared after provenance verification"
                ) from exc
            if (
                private.native_identity != binding.root_identity
                or private.path != binding.root_path
            ):
                raise self._authority_handoff(
                    "authority root changed between provenance verification and use"
                )

            existing_children: dict[str, _PinnedSkillParent] = {}
            for child_name in (
                AUTO_CLAIMS_DIRNAME,
                AUTO_EVIDENCE_DIRNAME,
                AUTO_LOCKS_DIRNAME,
            ):
                try:
                    existing_children[child_name] = stack.enter_context(
                        self._pin_skill_child_parent(
                            private,
                            child_name,
                            create=False,
                        )
                    )
                except FileNotFoundError:
                    pass
            locks = existing_children.get(AUTO_LOCKS_DIRNAME)
            if locks is not None:
                try:
                    stack.enter_context(
                        self._pin_skill_child_parent(
                            locks,
                            AUTO_CLAIMS_DIRNAME,
                            create=False,
                        )
                    )
                except FileNotFoundError:
                    pass
        return canonical_skills, binding

    @contextlib.contextmanager
    def _pin_private_state(
        self,
        *,
        create: bool,
        require_sensitive: bool = False,
    ) -> Iterator[_PinnedPrivateState]:
        """Pin public and private authority parents from canonical roots."""
        canonical_skills, binding = self._preflight_private_state(
            require_sensitive=require_sensitive,
        )
        canonical_home = binding.canonical_home
        if create and not os.path.lexists(self._dir):
            self._dir.mkdir(parents=True, exist_ok=True)
            if is_link_or_junction(self._dir) or self._dir.resolve(strict=True) != canonical_skills:
                raise OSError("skills root changed during creation")
        private_root = self._private_root()
        with contextlib.ExitStack() as stack:
            home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
            if home_parent.native_identity != binding.home_identity:
                raise self._authority_handoff("selected data-home changed before authority use")
            provenance_parent = stack.enter_context(
                self._pin_skill_child_parent(
                    home_parent,
                    _AUTHORITY_PROVENANCE_PARENT,
                    create=False,
                )
            )
            private = stack.enter_context(
                self._pin_skill_child_parent(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                )
            )
            if (
                private.native_identity != binding.root_identity
                or private.path != binding.root_path
            ):
                raise self._authority_handoff("authority root changed before use")
            skills_parent = stack.enter_context(self._pin_skill_parent(canonical_skills))
            auto_parent = stack.enter_context(
                self._pin_skill_child_parent(
                    skills_parent,
                    AUTO_SKILL_NAMESPACE,
                    create=create,
                )
            )
            public_parents: dict[str, _PinnedSkillParent] = {}
            for public_name in (
                AUTO_PENDING_DIRNAME,
                AUTO_QUARANTINE_DIRNAME,
                AUTO_LIVE_QUARANTINE_DIRNAME,
            ):
                public_parent = stack.enter_context(
                    self._pin_skill_child_parent(
                        auto_parent,
                        public_name,
                        create=create,
                    )
                )
                if not self._pinned_parent_matches(public_parent):
                    raise OSError(f"{public_name} root is not a real directory")
                public_parents[public_name] = public_parent
            pending = public_parents[AUTO_PENDING_DIRNAME]
            quarantine = public_parents[AUTO_QUARANTINE_DIRNAME]
            live_quarantine = public_parents[AUTO_LIVE_QUARANTINE_DIRNAME]

            if auto_parent.native_identity.volume != private.native_identity.volume:
                raise OSError(errno.EXDEV, "public and private skill authority are cross-device")
            claims = stack.enter_context(
                self._pin_skill_child_parent(
                    private,
                    AUTO_CLAIMS_DIRNAME,
                    create=create,
                )
            )
            evidence = stack.enter_context(
                self._pin_skill_child_parent(
                    private,
                    AUTO_EVIDENCE_DIRNAME,
                    create=create,
                )
            )
            locks = stack.enter_context(
                self._pin_skill_child_parent(
                    private,
                    AUTO_LOCKS_DIRNAME,
                    create=create,
                )
            )
            claim_locks = stack.enter_context(
                self._pin_skill_child_parent(
                    locks,
                    AUTO_CLAIMS_DIRNAME,
                    create=create,
                )
            )
            resolved_private = private_root.resolve(strict=True)
            if require_sensitive and (
                not is_sensitive_path(str(private_root))
                or not is_sensitive_path(str(resolved_private))
            ):
                raise OSError("private root is not agent-denied")
            resolved_private.relative_to(canonical_home)
            yield _PinnedPrivateState(
                data_home=home_parent,
                auto=auto_parent,
                pending=pending,
                quarantine=quarantine,
                live_quarantine=live_quarantine,
                private=private,
                claims=claims,
                evidence=evidence,
                locks=locks,
                claim_locks=claim_locks,
            )

    @staticmethod
    def _stat_pinned_child(pin: _PinnedSkillParent, name: str) -> os.stat_result:
        """lstat one child under *pin* without following the child itself."""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise OSError("unsafe skill-state child name")
        if os.name == "nt":
            return os.stat(pin.path / name, follow_symlinks=False)
        return os.stat(name, dir_fd=pin.fd, follow_symlinks=False)

    def _pinned_child_identity(
        self,
        parent: _PinnedSkillParent,
        name: str,
    ) -> _TaggedFileIdentity | None:
        """Open one current child without following it and return native identity."""
        fd: int | None = None
        try:
            current = self._stat_pinned_child(parent, name)
            path = parent.path / name
            if platform_compat.IS_WINDOWS:
                fd = platform_compat.open_path_no_reparse(path)
                opened = os.fstat(fd)
                opened_attrs = getattr(opened, "st_file_attributes", None)
                current_is_reparse = bool(
                    getattr(current, "st_reparse_tag", 0) or is_link_or_junction(path)
                )
                if opened_attrs is not None:
                    opened_is_reparse = bool(opened_attrs & 0x00000400)
                    if current_is_reparse != opened_is_reparse:
                        return None
                identity = self._tag_opened_identity(fd)
                if identity is None or not self._pinned_parent_matches(parent):
                    return None
                return identity
            if stat.S_ISDIR(current.st_mode):
                if _DIR_FD_SUPPORTED:
                    fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
                else:
                    fd = platform_compat.pin_directory(path)
            elif stat.S_ISREG(current.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                if _DIR_FD_SUPPORTED:
                    fd = os.open(name, flags, dir_fd=parent.fd)
                else:
                    fd = os.open(path, flags)
            elif stat.S_ISLNK(current.st_mode):
                return _TaggedFileIdentity(
                    "posix-dev-ino",
                    int(current.st_dev),
                    int(current.st_ino),
                )
            else:
                return None
            if not self._opened_path_matches(fd, path, current):
                return None
            return self._tag_opened_identity(fd)
        except OSError:
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def _rename_skill_child_no_replace(
        self,
        source: _PinnedSkillParent,
        source_name: str,
        destination: _PinnedSkillParent,
        destination_name: str,
        *,
        expected_identity: _TaggedFileIdentity | None = None,
        compensate_mismatch_to_source: bool = True,
    ) -> os.stat_result:
        """Move one exact child between captured parents without replacing."""
        if not self._pinned_parent_matches(source) or not self._pinned_parent_matches(destination):
            raise OSError("skill-state parent changed before rename")
        before_identity = self._pinned_child_identity(source, source_name)
        if before_identity is None or (
            expected_identity is not None and before_identity != expected_identity
        ):
            raise OSError("skill-state child identity changed before rename")
        if platform_compat.IS_WINDOWS:
            # MoveFileW refuses an existing destination. The source and final
            # names are authenticated with native 128-bit IDs; CRT inode fields
            # are never mutation authority.
            os.rename(source.path / source_name, destination.path / destination_name)
        else:
            platform_compat.rename_noreplace(
                source_name,
                destination_name,
                src_dir_fd=source.fd,
                dst_dir_fd=destination.fd,
            )
        if not self._pinned_parent_matches(source) or not self._pinned_parent_matches(destination):
            raise OSError("skill-state parent changed during rename")
        after = self._stat_pinned_child(destination, destination_name)
        after_identity = self._pinned_child_identity(destination, destination_name)
        if after_identity is None or after_identity != before_identity:
            if not compensate_mismatch_to_source:
                raise OSError(
                    "skill-state child changed after rename; public destination left untouched"
                )
            restored_name = source_name
            if after_identity is None or self._pinned_child_exists(source, restored_name):
                restored_name = f".mismatch-{source_name[:32]}-{secrets.token_hex(8)}"
            try:
                if platform_compat.IS_WINDOWS:
                    os.rename(
                        destination.path / destination_name,
                        source.path / restored_name,
                    )
                else:
                    platform_compat.rename_noreplace(
                        destination_name,
                        restored_name,
                        src_dir_fd=destination.fd,
                        dst_dir_fd=source.fd,
                    )
                self._sync_pinned_rename_parents(destination, source)
                if (
                    after_identity is not None
                    and self._pinned_child_identity(source, restored_name) != after_identity
                ):
                    raise OSError("mismatched child changed during compensation")
            except OSError:
                logger.error(
                    "Could not evacuate mismatched rename destination %s",
                    destination.path / destination_name,
                    exc_info=True,
                )
            raise OSError("skill-state child changed during rename")
        return after

    def _rename_untrusted_link_no_replace(
        self,
        source: _PinnedSkillParent,
        source_name: str,
        destination: _PinnedSkillParent,
        destination_name: str,
        *,
        expected_identity: _TaggedFileIdentity,
    ) -> None:
        """Move one exact public link that can never become publication authority."""
        before = self._stat_pinned_child(source, source_name)
        if not (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_reparse_tag", 0))
            or is_link_or_junction(source.path / source_name)
        ):
            raise OSError("public entry is not an untrusted link")
        self._rename_skill_child_no_replace(
            source,
            source_name,
            destination,
            destination_name,
            expected_identity=expected_identity,
        )
        after = self._stat_pinned_child(destination, destination_name)
        if not (
            stat.S_ISLNK(after.st_mode)
            or bool(getattr(after, "st_reparse_tag", 0))
            or is_link_or_junction(destination.path / destination_name)
        ):
            raise OSError("public link changed during rename")

    def _unlink_skill_child(
        self,
        parent: _PinnedSkillParent,
        name: str,
        *,
        expected: os.stat_result | None = None,
        expected_identity: _TaggedFileIdentity | None = None,
        directory: bool = False,
    ) -> bool:
        """Remove only the captured child under one revalidated parent."""
        if not self._pinned_parent_matches(parent):
            return False
        try:
            current = self._stat_pinned_child(parent, name)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        current_identity = (
            self._pinned_child_identity(parent, name) if platform_compat.IS_WINDOWS else None
        )
        if platform_compat.IS_WINDOWS:
            if expected_identity is None or current_identity != expected_identity:
                return False
        elif expected is not None and not os.path.samestat(current, expected):
            return False
        try:
            if platform_compat.IS_WINDOWS:
                if (
                    expected_identity is None
                    or expected_identity.kind != "windows-file-id-128"
                    or not isinstance(expected_identity.object_id, bytes)
                ):
                    return False
                removed = platform_compat.unlink_path_if_identity(
                    parent.path / name,
                    (
                        expected_identity.volume,
                        b"F128" + expected_identity.object_id,
                    ),
                    directory=directory,
                )
                return removed and self._pinned_parent_matches(parent)
            if directory:
                os.rmdir(name, dir_fd=parent.fd)
            else:
                os.unlink(name, dir_fd=parent.fd)
        except OSError:
            return False
        return self._pinned_parent_matches(parent)

    def _remove_private_tree(
        self,
        path: Path,
        *,
        what: str,
        expected_identity: _TaggedFileIdentity | None = None,
    ) -> bool:
        """Remove one opened private tree without traversing a mutable name.

        POSIX delegates the recursive walk to ``pinned_fs`` and requires that
        its independently opened root is the same inode captured here. Windows
        keeps a no-reparse, non-delete-sharing handle on the root while deleting
        children bottom-up; it closes that handle only for the final empty
        ``rmdir``, with the parent handle still pinning every ancestor.
        """
        if not os.path.lexists(path):
            return True
        try:
            with self._pin_skill_parent(path.parent) as parent:
                root_fd = platform_compat.pin_directory(path)
                try:
                    root_identity = self._tag_opened_identity(root_fd)
                    if root_identity is None or (
                        expected_identity is not None and root_identity != expected_identity
                    ):
                        return False
                    if platform_compat.IS_WINDOWS:
                        if not platform_compat.opened_path_identity_matches(root_fd, path):
                            return False
                        # Validate the complete tree before the first mutation.
                        if self._skill_tree_snapshot(path) is None:
                            return False
                        for current_root, dirs, files in os.walk(
                            path, topdown=False, followlinks=False
                        ):
                            current_path = Path(current_root)
                            with self._pin_skill_parent(current_path) as current_parent:
                                for filename in files:
                                    child = self._stat_pinned_child(current_parent, filename)
                                    child_identity = self._pinned_child_identity(
                                        current_parent,
                                        filename,
                                    )
                                    if (
                                        not stat.S_ISREG(child.st_mode)
                                        or child.st_nlink != 1
                                        or child_identity is None
                                        or not self._unlink_skill_child(
                                            current_parent,
                                            filename,
                                            expected=child,
                                            expected_identity=child_identity,
                                        )
                                    ):
                                        return False
                                for dirname in dirs:
                                    child_path = current_path / dirname
                                    if is_link_or_junction(child_path):
                                        return False
                                    child = self._stat_pinned_child(current_parent, dirname)
                                    child_identity = self._pinned_child_identity(
                                        current_parent,
                                        dirname,
                                    )
                                    if (
                                        not stat.S_ISDIR(child.st_mode)
                                        or child_identity is None
                                        or not self._unlink_skill_child(
                                            current_parent,
                                            dirname,
                                            expected=child,
                                            expected_identity=child_identity,
                                            directory=True,
                                        )
                                    ):
                                        return False
                    else:
                        resolved = str(path.resolve(strict=True))

                        def approve_root(fd: int, tree: pinned_fs.PinnedTree) -> str | None:
                            opened_identity = self._tag_opened_identity(fd)
                            if opened_identity != root_identity:
                                return "private tree changed identity"
                            if tree.links:
                                return "private tree contains links"
                            return None

                        removed = pinned_fs.remove_tree_pinned(
                            resolved,
                            what=what,
                            approve=approve_root,
                            refusal=OSError,
                        )
                        return removed.removed
                finally:
                    os.close(root_fd)
                # The target is empty. The held parent still prevents an
                # ancestor swap; refuse a replacement/reparse point and remove
                # only the captured empty directory.
                if not self._pinned_parent_matches(parent):
                    return False
                root_now = self._stat_pinned_child(parent, path.name)
                current_identity = self._pinned_child_identity(parent, path.name)
                if (
                    not stat.S_ISDIR(root_now.st_mode)
                    or current_identity != root_identity
                    or is_link_or_junction(path)
                ):
                    return False
                return self._unlink_skill_child(
                    parent,
                    path.name,
                    expected=root_now,
                    expected_identity=root_identity,
                    directory=True,
                )
        except (OSError, ValueError):
            logger.warning("Could not safely remove %s %s", what, path)
            return False

    def _private_state_roots_safe(self, *, create: bool, require_sensitive: bool = False) -> bool:
        """Authenticate the complete private hierarchy under retained parent pins."""
        try:
            self._preflight_private_state(
                require_sensitive=require_sensitive,
            )
            if not create and not os.path.lexists(self._private_root()):
                return True
            with self._pin_private_state(
                create=create,
                require_sensitive=require_sensitive,
            ):
                return True
        except (OSError, RuntimeError, ValueError) as exc:
            disabled = auto_skill_promotion_disabled_reason()
            if disabled is not None:
                # Authority itself is unavailable (no certificate, a startup
                # refusal, a revoked mask): the tree layout is not the problem,
                # so the link remedy below would send the operator the wrong way.
                logger.warning(
                    "Auto-skill private state under %s is unavailable: %s",
                    self._private_root(),
                    disabled,
                )
                return False
            logger.error(
                "Refusing unsafe auto-skill private state under %s: %s. Remove links or "
                "junctions inside the skills tree; to relocate it, link the Kiro Crew "
                "data-home root instead, then retry.",
                self._private_root(),
                exc,
            )
            return False

    def _open_skill_lock(
        self,
        parent: _PinnedSkillParent,
        name: str,
        *,
        created_out: list[bool] | None = None,
    ) -> int:
        """Open one lone regular lock relative to its retained parent pin."""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise OSError("unsafe skill lock name")
        if not self._pinned_parent_matches(parent):
            raise OSError("skill lock parent changed before open")
        path = parent.path / name
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | nofollow
        fd: int | None = None
        pre: os.stat_result | None = None
        created = False
        try:
            try:
                pre = self._stat_pinned_child(parent, name)
            except FileNotFoundError:
                if _DIR_FD_SUPPORTED:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent.fd)
                else:
                    fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            else:
                if is_link_or_junction(path) or not stat.S_ISREG(pre.st_mode) or pre.st_nlink != 1:
                    raise OSError(f"refusing unsafe skill lock {path}")
                if _DIR_FD_SUPPORTED:
                    fd = os.open(name, flags, dir_fd=parent.fd)
                else:
                    fd = os.open(str(path), flags)
            opened = os.fstat(fd)
            if (
                not self._pinned_parent_matches(parent)
                or (pre is not None and not self._opened_path_matches(fd, path, pre))
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise OSError(f"refusing unsafe skill lock {path}")
            platform_compat.prepare_lock_file(fd)
            if created_out is not None:
                created_out[:] = [created]
            return fd
        except OSError:
            if fd is not None:
                os.close(fd)
            raise

    @contextlib.contextmanager
    def _file_lock(
        self,
        name: str,
        *,
        state_out: list[_PinnedPrivateState] | None = None,
    ) -> Iterator[bool]:
        """Yield whether a bounded cross-process advisory lock was acquired.

        ``state_out`` is an operation-local handoff of the exact hierarchy this
        lock keeps pinned. It is populated only while the context is active; a
        caller that needs a public namespace parent must perform all work before
        leaving the ``with`` block and must never retain the object afterward.
        """
        try:
            private_state = self._pin_private_state(
                create=True,
                require_sensitive=True,
            )
            state = private_state.__enter__()
        except (OSError, RuntimeError, ValueError):
            logger.warning("Could not authenticate skill lock parent for %s", name)
            yield False
            return
        fd: int | None = None
        acquired = False
        try:
            try:
                fd = self._open_skill_lock(state.locks, name)
            except OSError:
                logger.warning("Could not open skill lock %s", state.locks.path / name)
                yield False
                return
            deadline = time.monotonic() + _PROMOTE_LOCK_TIMEOUT_S
            while True:
                if platform_compat.try_acquire_lock(fd, exclusive=True):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(_PROMOTE_LOCK_POLL_S)
            if acquired and state_out is not None:
                state_out[:] = [state]
            yield acquired
        finally:
            if acquired and fd is not None:
                platform_compat.release_lock(fd)
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
            private_state.__exit__(None, None, None)

    @contextlib.contextmanager
    def _promotion_lock(self, target_slug: str) -> Iterator[bool]:
        """Serialize promotions to one live auto-skill across processes.

        Refuses a non-canonical slug instead of locking it: the lock file is
        NAMED by the slug while the live directory is RESOLVED by the
        filesystem, and the two disagree on aliases. On a case-insensitive
        filesystem ``Foo`` opens ``auto/foo`` but locks ``target-Foo.lock``;
        Win32 strips trailing dots/spaces from path components, so ``foo.``
        opens ``auto/foo`` while locking ``target-foo..lock``. Either way two
        writers hold different locks over one directory and updates are lost.
        Every product-created slug already matches ``_AUTO_NAME_PATTERN`` (all
        creation paths enforce it), so canonical callers are unaffected and an
        alias fails closed here. A live mutation never reaches this refusal with
        an alias: :meth:`_live_auto_mutation_lock` folds an alias onto its
        canonical slug first, and mutates a name no promotion can target by name.
        """
        if not _AUTO_NAME_PATTERN.fullmatch(target_slug):
            logger.warning("Refusing promotion lock for non-canonical slug: %r", target_slug)
            yield False
            return
        with self._file_lock(f"target-{target_slug}.lock") as acquired:
            yield acquired

    @staticmethod
    def _audit_reserved_auto_mutation_denial(name: str) -> None:
        """Record a reserved-namespace mutation refusal without changing its verdict."""
        try:
            sel().log_tool_invocation(
                session_key="skills",
                tool_name="skill_mutation",
                tool_kind="permission",
                outcome="denied",
                metadata={
                    "target": name,
                    "reason": "reserved_auto_namespace",
                },
            )
        except Exception:  # noqa: BLE001 — audit failure cannot allow the mutation
            logger.warning("Could not audit reserved auto-skill mutation denial", exc_info=True)

    @staticmethod
    def _live_auto_target_lock_slug(slug: str) -> str | None:
        """The canonical promotion target a live ``auto/`` name must serialize with.

        A promotion only ever targets a slug matching ``_AUTO_NAME_PATTERN``, and
        its lock is NAMED by that slug. A live name can still reach that
        directory under another spelling: a case-insensitive or case-folding
        filesystem resolves ``Foo`` to ``foo``, Win32 strips trailing dots and
        spaces so ``foo.`` opens ``foo``, and a nested ``foo/bar`` lives inside
        ``foo``. Each of those takes the canonical slug's lock. The fold (NFKC,
        then case folding, then trailing dots and spaces) is wider than any one
        filesystem's alias rule, which can only add serialization, never remove it.

        A name with no canonical fold is one promotion can never reach under any
        spelling, so ``None`` is returned and the caller mutates it by name as
        before the claim protocol: nothing can race it. Refusing it instead would
        let an agent plant a skill the operator cannot delete or disable (an
        injected ``auto/Persist_Me`` carrying ``always: true``).
        """
        head = slug.split("/", 1)[0]
        folded = unicodedata.normalize("NFKC", head).casefold().rstrip(" .")
        return folded if _AUTO_NAME_PATTERN.fullmatch(folded) else None

    @contextlib.contextmanager
    def _live_auto_mutation_lock(self, name: str) -> Iterator[bool]:
        """Serialize a live auto-skill mutation with candidate promotion.

        A name promotion can target, under any alias spelling, takes that
        target's canonical lock; any other ``auto/`` name is mutated by name
        (:meth:`_live_auto_target_lock_slug`). The bare namespace and its
        dot-prefixed internals (``.pending``, the quarantines) are refused: they
        are never a loadable skill, and a by-name mutation there would bypass the
        claim protocol.
        """
        if not self.is_auto_generated(name):
            namespace, _separator, _slug = name.partition("/")
            # Case-insensitive filesystems resolve e.g. ``AUTO/x`` to the same
            # directory as ``auto/x``, and Win32 strips trailing dots/spaces
            # from path components, so ``auto.`` (or ``auto ``) opens the
            # ``auto`` directory too.  The bare reserved namespace — in any of
            # those alias spellings — would otherwise take the lock-free
            # manual-skill branch and let a delete remove every live, pending,
            # and private auto-skill entry.
            if namespace.rstrip(" .").casefold() == AUTO_SKILL_NAMESPACE.casefold():
                self._audit_reserved_auto_mutation_denial(name)
                logger.warning("Refusing reserved auto-skill path: %s", name)
                yield False
                return
            yield True
            return
        target_slug = self._auto_slug_from_name(name)
        head = target_slug.split("/", 1)[0]
        if not self._is_pending_slug_safe(head):
            yield False
            return
        lock_slug = self._live_auto_target_lock_slug(target_slug)
        if lock_slug is None:
            yield True
            return
        with self._promotion_lock(lock_slug) as acquired:
            if not acquired and _auto_skill_promotion_ruled_out():
                # Root absence or the re-proven no-replace refusal means no
                # authority-backed promotion can race this by-name mutation.
                yield True
                return
            if not acquired:
                masked_view = _auto_skill_authority_masked_view_reason()
                if masked_view is not None:
                    logger.warning(
                        "Auto-skill mutation of %s requires the authority target lock, " "but %s",
                        name,
                        masked_view,
                    )
                elif _auto_skill_authority_root_exists():
                    disabled = auto_skill_promotion_disabled_reason()
                    if disabled is not None and _auto_skill_sandbox_excludes_every_promoter():
                        logger.warning(
                            "Auto-skill mutation of %s requires the authority target lock, "
                            "but this process cannot authenticate it (%s). %s",
                            name,
                            disabled,
                            _auto_skill_authority_retire_hint(),
                        )
            yield acquired

    def _quarantine_is_consumed_evidence(
        self,
        state: _PinnedPrivateState,
        claim_name: str,
    ) -> bool:
        """Whether one public quarantine has complete authenticated evidence."""
        try:
            # Evidence-state publication precedes the private claim-to-evidence
            # rename. While the trusted claim still exists, the lifecycle is
            # active or interrupted even if a colliding evidence name exists.
            try:
                self._stat_pinned_child(state.claims, claim_name)
            except FileNotFoundError:
                pass
            else:
                return False

            evidence_info = self._stat_pinned_child(state.evidence, claim_name)
            quarantine_info = self._stat_pinned_child(state.quarantine, claim_name)
            lock_name = self._claim_lock_path(claim_name).name
            lock_info = self._stat_pinned_child(state.claim_locks, lock_name)
            if (
                not stat.S_ISDIR(evidence_info.st_mode)
                or not stat.S_ISDIR(quarantine_info.st_mode)
                or not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or is_link_or_junction(self._evidence_root() / claim_name)
                or is_link_or_junction(self._quarantine_root() / claim_name)
            ):
                return False
            fd = self._open_skill_lock(state.claim_locks, lock_name)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return False
        acquired = False
        try:
            acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
            if not acquired:
                return False
            evidence_state = self._authenticated_claim_evidence_state(
                fd,
                self._claim_lock_path(claim_name),
                claim_name,
            )
            return evidence_state is not None and self._public_claim_evidence_matches(
                state,
                evidence_state,
                claim_name,
            )
        finally:
            if acquired:
                platform_compat.release_lock(fd)
            os.close(fd)

    def _pending_slug_claimed(
        self,
        slug: str,
        *,
        private_state: _PinnedPrivateState | None = None,
    ) -> bool | None:
        """Whether an active claim owns *slug*, or ``None`` if inspection failed.

        ``None`` is deliberately not false: a transient I/O failure cannot prove
        the slug free. Staging aborts and retries rather than reusing a live
        claim's destination. Each directory scan counts at most
        ``_ACTIVE_CLAIM_SCAN_LIMIT`` unconsumed names and retains only the current
        slug-matching name. A public quarantine name whose claim already retired
        into private ``evidence/`` is history: every promotion, dismissal and TTL
        prune leaves one forever, so it is skipped before it is counted. Only a
        certified promoter writes ``evidence/``, so the uncounted names are
        bounded by real claim history, never by what an agent plants.
        """

        def retired_history(state: _PinnedPrivateState, claim_name: str) -> bool:
            try:
                self._stat_pinned_child(state.claims, claim_name)
            except FileNotFoundError:
                pass
            else:
                return False
            try:
                self._stat_pinned_child(state.evidence, claim_name)
            except FileNotFoundError:
                return False
            return True

        def matching_claim(
            state: _PinnedPrivateState,
            parent: _PinnedSkillParent,
            *,
            label: str,
            require_unconsumed_quarantine: bool = False,
        ) -> bool:
            if not _DIR_FD_SUPPORTED and not self._pinned_parent_matches(parent):
                raise OSError(f"{label} parent changed during active-claim inspection")
            target: int | Path = parent.fd if _DIR_FD_SUPPORTED else parent.path
            counted = 0
            with os.scandir(target) as entries:
                for entry in entries:
                    claim_name = entry.name
                    if not (require_unconsumed_quarantine and retired_history(state, claim_name)):
                        if counted >= _ACTIVE_CLAIM_SCAN_LIMIT:
                            raise _ActiveClaimScanOverflow(
                                f"{label} exceeded the {_ACTIVE_CLAIM_SCAN_LIMIT}-entry "
                                "active-claim scan limit"
                            )
                        counted += 1
                    if claim_name.rsplit("--", 1)[0] != slug:
                        continue
                    if require_unconsumed_quarantine and self._quarantine_is_consumed_evidence(
                        state,
                        claim_name,
                    ):
                        continue
                    return True
            return False

        try:
            state_context = (
                contextlib.nullcontext(private_state)
                if private_state is not None
                else self._pin_private_state(create=False)
            )
            with state_context as state:
                if matching_claim(state, state.claims, label="active claims"):
                    return True
                if matching_claim(
                    state,
                    state.quarantine,
                    label="public candidate quarantine",
                    require_unconsumed_quarantine=True,
                ):
                    return True
                return False
        except FileNotFoundError:
            return False
        except _ActiveClaimScanOverflow as exc:
            logger.warning(
                "Could not determine whether pending slug %s has an active claim: %s",
                slug,
                exc,
            )
            return None
        except (OSError, RuntimeError, ValueError):
            logger.warning("Could not determine whether pending slug %s has an active claim", slug)
            return None

    def _probe_rename_under_authority(self, binding: _CertifiedAuthorityBinding) -> None:
        """Raise unless an atomic no-replace rename works in the certified root.

        Startup's copy of the claim-time probe. Claims, restores and publication
        all need the primitive, so a data home on a filesystem without it (NFS,
        older ZFS, many FUSE mounts) is refused once at certification instead of
        accepting candidates that could then be neither approved nor dismissed.
        """
        token = secrets.token_hex(16)
        source_name = f"{_RENAME_PROBE_PREFIX}{token}-source"
        destination_name = f"{_RENAME_PROBE_PREFIX}{token}-destination"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with contextlib.ExitStack() as stack:
            home = stack.enter_context(self._pin_skill_parent(binding.canonical_home))
            parent = stack.enter_context(
                self._pin_skill_child_parent(home, _AUTHORITY_PROVENANCE_PARENT, create=False)
            )
            root = stack.enter_context(
                self._pin_skill_child_parent(
                    parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                )
            )
            if root.native_identity != binding.root_identity:
                raise OSError("authority root changed before the rename probe")
            try:
                if _DIR_FD_SUPPORTED:
                    fd = os.open(source_name, flags, 0o600, dir_fd=root.fd)
                else:
                    fd = os.open(str(root.path / source_name), flags, 0o600)
                os.close(fd)
                self._rename_skill_child_no_replace(root, source_name, root, destination_name)
            finally:
                for probe_name in (source_name, destination_name):
                    try:
                        probe = self._stat_pinned_child(root, probe_name)
                        self._unlink_skill_child(
                            root,
                            probe_name,
                            expected=probe,
                            expected_identity=self._pinned_child_identity(root, probe_name),
                        )
                    except OSError:
                        continue

    def _probe_no_replace_rename(self) -> bool:
        """Verify atomic no-replace support under one retained claims parent."""
        token = secrets.token_hex(16)
        source_name = f"{_RENAME_PROBE_PREFIX}{token}-source"
        destination_name = f"{_RENAME_PROBE_PREFIX}{token}-destination"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            with self._pin_private_state(create=True) as state:
                claims = state.claims
                if _DIR_FD_SUPPORTED:
                    fd = os.open(source_name, flags, 0o600, dir_fd=claims.fd)
                else:
                    if not self._pinned_parent_matches(claims):
                        raise OSError("claims parent changed before rename probe")
                    fd = os.open(str(claims.path / source_name), flags, 0o600)
                os.close(fd)
                fd = None
                self._rename_skill_child_no_replace(
                    claims,
                    source_name,
                    claims,
                    destination_name,
                )
                destination = self._stat_pinned_child(claims, destination_name)
                destination_identity = self._pinned_child_identity(claims, destination_name)
                return self._unlink_skill_child(
                    claims,
                    destination_name,
                    expected=destination,
                    expected_identity=destination_identity,
                )
        except OSError:
            logger.warning(
                "Atomic no-replace rename is unavailable under %s. Move KIROCREW_HOME to a "
                "local filesystem that supports renameat2(RENAME_NOREPLACE) on Linux or "
                "renameatx_np(RENAME_EXCL) on macOS, then retry.",
                self._claims_root(),
                exc_info=True,
            )
            return False
        finally:
            if fd is not None:
                os.close(fd)
            try:
                with self._pin_private_state(create=False) as state:
                    for probe_name in (source_name, destination_name):
                        try:
                            probe = self._stat_pinned_child(state.claims, probe_name)
                        except OSError:
                            continue
                        probe_identity = self._pinned_child_identity(
                            state.claims,
                            probe_name,
                        )
                        self._unlink_skill_child(
                            state.claims,
                            probe_name,
                            expected=probe,
                            expected_identity=probe_identity,
                        )
            except OSError:
                logger.debug("Could not remove a rename probe", exc_info=True)

    def _claim_lock_path(self, claim_name: str) -> Path:
        return self._locks_root() / "claims" / f"{claim_name}.lock"

    @staticmethod
    def _claim_lock_state_payload(claim_name: str, *, completed: bool) -> bytes:
        return (("C" if completed else "A") + claim_name + "\n").encode("utf-8")

    @classmethod
    def _read_authenticated_claim_lock_payload(
        cls, fd: int, lock_path: Path, claim_name: str
    ) -> bytes | None:
        """Read a bounded payload only from the held lock file's authenticated inode."""
        if is_link_or_junction(lock_path):
            return None
        try:
            linked = os.lstat(lock_path)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(linked.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or linked.st_nlink != 1
                or opened.st_nlink != 1
                or not cls._opened_path_matches(fd, lock_path, linked)
                or opened.st_size > _CLAIM_LOCK_MAX_STATE_BYTES
            ):
                return None
            os.lseek(fd, 0, os.SEEK_SET)
            payload = os.read(fd, _CLAIM_LOCK_MAX_STATE_BYTES + 1)
        except OSError:
            return None
        if len(payload) != opened.st_size:
            return None
        return payload

    @classmethod
    def _authenticated_claim_lock_state(
        cls,
        fd: int,
        lock_path: Path,
        claim_name: str,
        *,
        completed: bool,
    ) -> bool:
        """Authenticate a fixed-size active/completed record in a held claim lock."""
        payload = cls._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        return payload == cls._claim_lock_state_payload(claim_name, completed=completed)

    @classmethod
    def _write_claim_lock_payload(
        cls,
        fd: int,
        lock_path: Path,
        claim_name: str,
        payload: bytes,
    ) -> bool:
        """Durably replace the held claim lock's authenticated bounded payload."""
        if not payload or len(payload) > _CLAIM_LOCK_MAX_STATE_BYTES:
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short claim-lock state write")
                remaining = remaining[written:]
            os.ftruncate(fd, len(payload))
            os.fsync(fd)
        except OSError:
            logger.warning("Could not write claim lock state %s", lock_path)
            return False
        return cls._read_authenticated_claim_lock_payload(fd, lock_path, claim_name) == payload

    def _initialize_claim_lock_state(self, fd: int, lock_path: Path, claim_name: str) -> bool:
        """Durably bind a fresh claim lock to its active claim before claiming."""
        return self._write_claim_lock_payload(
            fd,
            lock_path,
            claim_name,
            self._claim_lock_state_payload(claim_name, completed=False),
        )

    @staticmethod
    def _claim_snapshot_fields(snapshot: _ClaimSnapshot) -> dict[str, object]:
        return {
            "claim_generation": snapshot.generation_hash,
            "claim_metadata": (
                base64.b64encode(snapshot.metadata_bytes).decode("ascii")
                if snapshot.metadata_bytes is not None
                else None
            ),
            "quarantine_identity": (
                SkillsLoader._identity_payload(snapshot.quarantine_identity)
                if snapshot.quarantine_identity is not None
                else None
            ),
        }

    @staticmethod
    def _claim_snapshot_from_fields(data: dict[str, object]) -> _ClaimSnapshot | None:
        generation = data.get("claim_generation")
        encoded_metadata = data.get("claim_metadata")
        if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{64}", generation) is None:
            return None
        if encoded_metadata is None:
            metadata_bytes = None
        elif isinstance(encoded_metadata, str):
            try:
                metadata_bytes = base64.b64decode(encoded_metadata, validate=True)
            except (ValueError, binascii.Error):
                return None
        else:
            return None
        encoded_identity = data.get("quarantine_identity")
        quarantine_identity = SkillsLoader._identity_from_payload(encoded_identity)
        if quarantine_identity is None:
            return None
        return _ClaimSnapshot(
            generation,
            metadata_bytes,
            quarantine_identity=quarantine_identity,
        )

    def _write_claim_snapshot_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
        snapshot: _ClaimSnapshot,
    ) -> bool:
        """Durably bind recovery to exact pre-rename generation and metadata."""
        if not self._authenticated_claim_lock_state(
            fd,
            lock_path,
            claim_name,
            completed=False,
        ):
            return False
        payload = json.dumps(
            {
                "state": "claimed",
                "format": 1,
                "claim": claim_name,
                **self._claim_snapshot_fields(snapshot),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)

    def _authenticated_claim_snapshot_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
    ) -> _ClaimSnapshot | None:
        """Return the durable pre-rename claim snapshot, if authenticated."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if (
            data.get("state") != "claimed"
            or data.get("format") != 1
            or data.get("claim") != claim_name
        ):
            return None
        return self._claim_snapshot_from_fields(data)

    def _prepare_claim_publication(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
        *,
        kind: str,
        target_slug: str,
        before_hash: str | None,
        after_hash: str,
        claim_snapshot: _ClaimSnapshot,
        live_backup_identity: _TaggedFileIdentity | None,
        snapshot_version: int | None,
        new_version: int | None,
    ) -> bool:
        """Durably journal the exact publication recovery must reconcile."""
        if (
            not self._authenticated_claim_lock_state(
                fd,
                lock_path,
                claim_name,
                completed=False,
            )
            and self._authenticated_claim_snapshot_state(fd, lock_path, claim_name) is None
        ):
            return False
        active_snapshot = self._authenticated_claim_snapshot_state(
            fd,
            lock_path,
            claim_name,
        )
        if claim_snapshot.quarantine_identity is None and active_snapshot is not None:
            claim_snapshot = _ClaimSnapshot(
                claim_snapshot.generation_hash,
                claim_snapshot.metadata_bytes,
                tree=claim_snapshot.tree,
                quarantine_identity=active_snapshot.quarantine_identity,
            )
        payload = json.dumps(
            {
                "state": "prepared",
                "format": 2,
                "claim": claim_name,
                "kind": kind,
                "target": target_slug,
                "before": before_hash,
                "after": after_hash,
                "live_backup_identity": (
                    self._identity_payload(live_backup_identity)
                    if live_backup_identity is not None
                    else None
                ),
                **self._claim_snapshot_fields(claim_snapshot),
                "snapshot": snapshot_version,
                "version": new_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)

    def _authenticated_claim_publication(
        self, fd: int, lock_path: Path, claim_name: str
    ) -> dict[str, object] | None:
        """Parse a prepared publication only after authenticating its lock inode."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("state") != "prepared" or data.get("claim") != claim_name:
            return None
        journal_format = data.get("format", 1)
        if journal_format not in (1, 2):
            return None
        kind = data.get("kind")
        target = data.get("target")
        before_hash = data.get("before")
        after_hash = data.get("after")
        claim_generation = data.get("claim_generation")
        claim_metadata = data.get("claim_metadata")
        snapshot_version = data.get("snapshot")
        new_version = data.get("version")
        live_backup_identity = self._identity_from_payload(data.get("live_backup_identity"))
        if kind not in ("new", "update"):
            return None
        if not isinstance(target, str) or not self._is_pending_slug_safe(target):
            return None
        if before_hash is not None and (
            not isinstance(before_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", before_hash)
        ):
            return None
        if not isinstance(after_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", after_hash):
            return None
        if claim_generation is not None and (
            not isinstance(claim_generation, str)
            or not re.fullmatch(r"[0-9a-f]{64}", claim_generation)
        ):
            return None
        if (claim_generation is not None or claim_metadata is not None) and (
            self._claim_snapshot_from_fields(data) is None
        ):
            return None
        if snapshot_version is not None and (
            not isinstance(snapshot_version, int) or snapshot_version < 1
        ):
            return None
        if new_version is not None and (not isinstance(new_version, int) or new_version < 1):
            return None
        if kind == "update" and (
            before_hash is None
            or snapshot_version is None
            or new_version is None
            or (journal_format == 2 and live_backup_identity is None)
        ):
            return None
        if kind == "new" and journal_format == 2 and data.get("live_backup_identity") is not None:
            return None
        return data

    def _publication_live_backup_identity(
        self,
        publication: dict[str, object],
    ) -> _TaggedFileIdentity | None:
        return self._identity_from_payload(publication.get("live_backup_identity"))

    def _authenticated_claim_restore_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
    ) -> tuple[str, _TaggedFileIdentity] | None:
        """Return the durable pending destination and quarantined native identity."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(data, dict)
            or data.get("state") not in {"claimed", "prepared", "evidence"}
            or data.get("claim") != claim_name
            or self._claim_snapshot_from_fields(data) is None
        ):
            return None
        restore_slug = data.get("restore_slug")
        restore_identity = self._identity_from_payload(data.get("restore_identity"))
        if (
            not isinstance(restore_slug, str)
            or not self._is_pending_slug_safe(restore_slug)
            or restore_identity is None
        ):
            return None
        return restore_slug, restore_identity

    def _write_claim_restore_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
        *,
        restore_slug: str,
        restore_identity: _TaggedFileIdentity,
    ) -> bool:
        """Durably bind a no-replace restore of the exact public inode."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return False
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(data, dict)
            or data.get("state") not in {"claimed", "prepared"}
            or data.get("claim") != claim_name
            or self._claim_snapshot_from_fields(data) is None
            or not self._is_pending_slug_safe(restore_slug)
        ):
            return False
        data["restore_slug"] = restore_slug
        data["restore_identity"] = self._identity_payload(restore_identity)
        restore_payload = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._write_claim_lock_payload(fd, lock_path, claim_name, restore_payload)

    def _clear_claim_restore_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
    ) -> bool:
        """Clear a refused in-process destination before trying another slot."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return False
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(data, dict)
            or data.get("state") not in {"claimed", "prepared"}
            or data.get("claim") != claim_name
        ):
            return False
        data.pop("restore_slug", None)
        data.pop("restore_identity", None)
        data.pop("restore_generation", None)
        cleared_payload = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._write_claim_lock_payload(fd, lock_path, claim_name, cleared_payload)

    def _authenticated_claim_evidence_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
    ) -> dict[str, object] | None:
        """Return a durable private-evidence record bound to this claim lock."""
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if (
            data.get("state") != "evidence"
            or data.get("claim") != claim_name
            or data.get("evidence") != claim_name
            or self._claim_snapshot_from_fields(data) is None
        ):
            return None
        return data

    def _public_claim_evidence_matches(
        self,
        state: _PinnedPrivateState,
        evidence_state: dict[str, object],
        claim_name: str,
    ) -> bool:
        """Validate every public identity required by one evidence journal."""
        snapshot = self._claim_snapshot_from_fields(evidence_state)
        if snapshot is None or snapshot.quarantine_identity is None:
            return False
        try:
            candidate = self._stat_pinned_child(state.quarantine, claim_name)
            candidate_identity = self._pinned_child_identity(state.quarantine, claim_name)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(candidate.st_mode)
            or candidate_identity != snapshot.quarantine_identity
            or is_link_or_junction(state.quarantine.path / claim_name)
        ):
            return False
        if evidence_state.get("format") == 2 and evidence_state.get("kind") == "update":
            old_live_identity = self._publication_live_backup_identity(evidence_state)
            if old_live_identity is None:
                return False
            try:
                old_live = self._stat_pinned_child(state.live_quarantine, claim_name)
                current_old_live_identity = self._pinned_child_identity(
                    state.live_quarantine,
                    claim_name,
                )
            except OSError:
                return False
            if (
                not stat.S_ISDIR(old_live.st_mode)
                or current_old_live_identity != old_live_identity
                or is_link_or_junction(state.live_quarantine.path / claim_name)
            ):
                return False
        return True

    def _commit_claim_evidence_state(
        self,
        fd: int,
        lock_path: Path,
        claim_name: str,
    ) -> bool:
        """Forbid requeue before moving only the trusted claim to evidence."""
        existing = self._authenticated_claim_evidence_state(fd, lock_path, claim_name)
        if existing is not None:
            return True
        payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
        if payload is None:
            return False
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(data, dict)
            or data.get("state") not in {"claimed", "prepared"}
            or data.get("claim") != claim_name
            or self._claim_snapshot_from_fields(data) is None
        ):
            return False
        data["state"] = "evidence"
        data["evidence"] = claim_name
        evidence_payload = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._write_claim_lock_payload(fd, lock_path, claim_name, evidence_payload)

    def _commit_claim_lock_state(self, fd: int, lock_path: Path, claim_name: str) -> bool:
        """Durably transition a held active/prepared claim to completed."""
        if self._authenticated_claim_lock_state(fd, lock_path, claim_name, completed=True):
            return True
        payload = self._claim_lock_state_payload(claim_name, completed=True)
        return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)

    def _published_update_evidence_matches(self, claim: Path, claim_fd: int) -> bool:
        """Whether mandatory public old-live evidence still matches its journal."""
        lock_path = self._claim_lock_path(claim.name)
        publication = self._authenticated_claim_publication(
            claim_fd,
            lock_path,
            claim.name,
        )
        if (
            publication is None
            or publication.get("format") != 2
            or publication.get("kind") != "update"
        ):
            return False
        expected_identity = self._publication_live_backup_identity(publication)
        if expected_identity is None:
            return False
        _stage, backup = self._publication_paths(claim.name)
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as private_state:
                retained = self._stat_pinned_child(
                    private_state.live_quarantine,
                    backup.name,
                )
                retained_identity = self._pinned_child_identity(
                    private_state.live_quarantine,
                    backup.name,
                )
                return (
                    stat.S_ISDIR(retained.st_mode)
                    and retained_identity == expected_identity
                    and not is_link_or_junction(backup)
                )
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return False

    def _retain_published_update_evidence(self, claim: Path, claim_fd: int) -> bool:
        """Retain the exact public old-live inode beside private claim evidence.

        A writer may keep a directory or file descriptor across the live-to-public-
        quarantine rename and mutate that inode indefinitely. Its identity remains
        journal-bound, but its mutable bytes are evidence only and never publication
        authority. It is never moved under or deleted from private authority.
        """
        lock_path = self._claim_lock_path(claim.name)
        publication = self._authenticated_claim_publication(
            claim_fd,
            lock_path,
            claim.name,
        )
        if (
            publication is None
            or publication.get("format") != 2
            or publication.get("kind") != "update"
        ):
            return False
        expected_identity = self._publication_live_backup_identity(publication)
        if expected_identity is None:
            return False
        _stage, backup = self._publication_paths(claim.name)
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as private_state:
                retained = self._stat_pinned_child(
                    private_state.live_quarantine,
                    backup.name,
                )
                retained_identity = self._pinned_child_identity(
                    private_state.live_quarantine,
                    backup.name,
                )
                if (
                    not stat.S_ISDIR(retained.st_mode)
                    or retained_identity != expected_identity
                    or is_link_or_junction(backup)
                ):
                    return False
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            logger.error(
                "Could not retain published skill backup as public evidence %s",
                claim,
                exc_info=True,
            )
            return False
        return self._retain_claim_evidence(claim, claim_fd)

    def _commit_claim_consumption(self, claim: Path, claim_fd: int) -> bool:
        """Make a published claim durably recoverable before reporting success.

        The in-tree marker and the external lock state are independent records.
        Write the marker first. If an update committed, retain its pre-publication
        inode and prepared journal as private evidence instead of collapsing the
        journal to a completed bit and deleting a generation a retained writer can
        still mutate. If marker publication fails, the exact prepared journal is
        already durable and restart recovery can classify the live generation.
        """
        lock_path = self._claim_lock_path(claim.name)
        publication = self._authenticated_claim_publication(
            claim_fd,
            lock_path,
            claim.name,
        )
        retain_update = (
            publication is not None
            and publication.get("format") == 2
            and publication.get("kind") == "update"
        )
        if retain_update and not self._published_update_evidence_matches(claim, claim_fd):
            logger.error(
                "Published update lacks mandatory identity-bound old-live evidence: %s",
                claim,
            )
            return False
        marker_completed = self._write_completion_marker(claim)
        if not marker_completed:
            if publication is not None:
                logger.warning(
                    "Completion marker write failed for %s; prepared journal retained",
                    claim,
                )
            else:
                logger.error("Published claim has no durable consumption record: %s", claim)
            return False
        retained = (
            self._retain_published_update_evidence(claim, claim_fd)
            if retain_update
            else self._retain_claim_evidence(claim, claim_fd)
        )
        if not retained:
            if retain_update:
                logger.error(
                    "Published update evidence retention failed for %s; "
                    "marker and journal retained",
                    claim,
                )
                return False
            logger.warning(
                "Published claim evidence retention deferred for %s; "
                "authenticated marker and journal retained",
                claim,
            )
            return True
        return True

    def _cleanup_claim_lock(self, claim_name: str) -> None:
        """Remove a completed claim lock under the pinned private hierarchy."""
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as private_state:
                try:
                    self._stat_pinned_child(private_state.quarantine, claim_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                else:
                    return
                try:
                    self._stat_pinned_child(private_state.live_quarantine, claim_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                else:
                    return
                try:
                    self._stat_pinned_child(private_state.claims, claim_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                else:
                    return
                try:
                    self._stat_pinned_child(private_state.evidence, claim_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                else:
                    return
                lock_name = self._claim_lock_path(claim_name).name
                try:
                    lock_info = self._stat_pinned_child(
                        private_state.claim_locks,
                        lock_name,
                    )
                except FileNotFoundError:
                    return
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    return
                lock_identity = self._pinned_child_identity(
                    private_state.claim_locks,
                    lock_name,
                )
                if not self._unlink_skill_child(
                    private_state.claim_locks,
                    lock_name,
                    expected=lock_info,
                    expected_identity=lock_identity,
                ):
                    logger.debug("Could not remove completed claim lock %s", claim_name)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            logger.debug("Could not pin completed claim lock %s", claim_name, exc_info=True)

    def _completion_marker_present(
        self,
        claim: Path,
        expected_claim_identity: _TaggedFileIdentity | None = None,
    ) -> bool:
        """Check the reserved marker through a pinned claim directory."""
        try:
            with self._pin_skill_parent(claim) as parent:
                if (
                    expected_claim_identity is not None
                    and parent.native_identity != expected_claim_identity
                ):
                    return False
                self._stat_pinned_child(parent, ".promoted")
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _remove_untrusted_completion_marker(
        self,
        claim: Path,
        expected_claim_identity: _TaggedFileIdentity | None = None,
    ) -> bool:
        """Remove only one reserved marker entry, never a linked/replaced tree."""
        try:
            with self._pin_skill_parent(claim) as parent:
                if (
                    expected_claim_identity is not None
                    and parent.native_identity != expected_claim_identity
                ):
                    return False
                try:
                    marker = self._stat_pinned_child(parent, ".promoted")
                except FileNotFoundError:
                    return True
                marker_identity = self._pinned_child_identity(parent, ".promoted")
                if stat.S_ISDIR(marker.st_mode):
                    # Never recurse through an untrusted marker name. An empty
                    # directory can be removed exactly; a non-empty one leaves
                    # the claim private for later inspection.
                    return self._unlink_skill_child(
                        parent,
                        ".promoted",
                        expected=marker,
                        expected_identity=marker_identity,
                        directory=True,
                    )
                return self._unlink_skill_child(
                    parent,
                    ".promoted",
                    expected=marker,
                    expected_identity=marker_identity,
                )
        except OSError:
            logger.warning("Could not remove untrusted completion marker from %s", claim)
            return False

    def _authenticated_completion_marker(
        self,
        claim: Path,
        expected_claim_identity: _TaggedFileIdentity | None = None,
    ) -> bool:
        """Recognize only this claim's regular, single-link completion marker."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            with self._pin_skill_parent(claim) as parent:
                if (
                    expected_claim_identity is not None
                    and parent.native_identity != expected_claim_identity
                ):
                    return False
                marker = self._stat_pinned_child(parent, ".promoted")
                if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                    return False
                if os.name == "nt":
                    fd = platform_compat.open_file_no_reparse(claim / ".promoted")
                else:
                    fd = os.open(".promoted", flags, dir_fd=parent.fd)
                opened = os.fstat(fd)
                if (
                    not self._opened_path_matches(fd, claim / ".promoted", marker)
                    or opened.st_size > 256
                ):
                    return False
                payload = os.read(fd, 257)
                return payload == (claim.name + "\n").encode("utf-8")
        except OSError:
            return False
        finally:
            if fd is not None:
                os.close(fd)

    def _write_completion_marker(
        self,
        claim: Path,
        expected_claim_identity: _TaggedFileIdentity | None = None,
    ) -> bool:
        """Exclusively commit and durably link a claim outcome under its directory."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        created: os.stat_result | None = None
        created_identity: _TaggedFileIdentity | None = None
        wrote = False
        try:
            with self._pin_skill_parent(claim) as parent:
                if (
                    expected_claim_identity is not None
                    and parent.native_identity != expected_claim_identity
                ):
                    return False
                if os.name == "nt":
                    fd = os.open(str(claim / ".promoted"), flags, 0o600)
                else:
                    fd = os.open(".promoted", flags, 0o600, dir_fd=parent.fd)
                created = os.fstat(fd)
                created_identity = self._tag_opened_identity(fd)
                if created_identity is None:
                    raise OSError("completion-marker identity is unavailable")
                remaining = memoryview((claim.name + "\n").encode("utf-8"))
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short completion-marker write")
                    remaining = remaining[written:]
                os.fsync(fd)
                os.close(fd)
                fd = None
                # A file fsync does not make its new directory entry durable.
                # Sync the authenticated descriptor before any journal state may
                # depend on this independent completion record.
                self._sync_pinned_parent(parent)
                wrote = True
        except OSError:
            logger.warning("Could not commit claim completion marker in %s", claim)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    wrote = False
        if wrote and self._authenticated_completion_marker(
            claim,
            expected_claim_identity,
        ):
            return True
        if created is not None:
            try:
                with self._pin_skill_parent(claim) as parent:
                    if (
                        expected_claim_identity is not None
                        and parent.native_identity != expected_claim_identity
                    ):
                        return False
                    removed = self._unlink_skill_child(
                        parent,
                        ".promoted",
                        expected=created,
                        expected_identity=created_identity,
                    )
                    if removed:
                        self._sync_pinned_parent(parent)
            except OSError:
                pass
        return False

    def _cleanup_completed_claim(self, claim: Path, claim_fd: int) -> bool:
        """Delete one completed claim without following a replacement name.

        The held per-claim lock is the durable completion record. The claim
        directory identity is captured before any cleanup and is required by
        recursive removal plus every marker fallback, so a parent/name swap can
        only defer cleanup; it cannot redirect deletion or marker publication.
        """
        try:
            with self._pin_skill_parent(claim) as claim_parent:
                claim_identity = claim_parent.native_identity
        except OSError:
            logger.error("Refusing to clean replaced/linked claim %s", claim)
            return False
        lock_path = self._claim_lock_path(claim.name)
        lock_completed = self._authenticated_claim_lock_state(
            claim_fd, lock_path, claim.name, completed=True
        )
        marker_completed = self._authenticated_completion_marker(
            claim,
            claim_identity,
        )
        if not lock_completed and marker_completed:
            lock_completed = self._commit_claim_lock_state(claim_fd, lock_path, claim.name)
        if not lock_completed:
            if marker_completed:
                logger.warning(
                    "Deferred completed-claim cleanup until lock outcome is durable: %s",
                    claim,
                )
                return True
            logger.error("No authenticated committed outcome for claim %s", claim)
            return False
        if not self._cleanup_publication_artifacts(claim.name):
            logger.warning(
                "Deferred completed-claim cleanup until generation artifacts are removed: %s",
                claim,
            )
            return True
        if not self._remove_private_tree(
            claim,
            what="completed skill claim",
            expected_identity=claim_identity,
        ):
            logger.warning("Deferred completed-claim cleanup for %s", claim)
        if not os.path.lexists(claim):
            return True
        try:
            with self._pin_skill_parent(claim) as current_claim:
                if current_claim.identity != claim_identity:
                    logger.error("Completed claim changed identity during cleanup: %s", claim)
                    return False
        except OSError:
            logger.error("Completed claim became unreadable during cleanup: %s", claim)
            return False
        if self._authenticated_completion_marker(claim, claim_identity):
            return True
        if self._completion_marker_present(
            claim,
            claim_identity,
        ) and not self._remove_untrusted_completion_marker(
            claim,
            claim_identity,
        ):
            return False
        if self._write_completion_marker(claim, claim_identity):
            return True
        if self._authenticated_claim_lock_state(claim_fd, lock_path, claim.name, completed=True):
            logger.warning(
                "Claim marker could not be repaired; authenticated lock outcome retained for %s",
                claim,
            )
            return True
        logger.error("Could not preserve committed outcome for claim %s", claim)
        return False

    @staticmethod
    def _auto_apply_candidate_binding(
        skill_bytes: bytes,
        *,
        target: object,
        base_version: object,
        base_content_hash: object,
    ) -> str:
        """Bind unattended apply to exact staged bytes and live-base identity."""
        fields = json.dumps(
            [target, base_version, base_content_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256()
        digest.update(len(skill_bytes).to_bytes(8, "big"))
        digest.update(skill_bytes)
        digest.update(fields)
        return digest.hexdigest()

    @contextlib.contextmanager
    def _create_pinned_child_exclusive(
        self,
        parent: _PinnedSkillParent,
        name: str,
    ) -> Iterator[_PinnedSkillParent]:
        """Create and pin one new child directory beneath *parent*.

        POSIX creation/open is descriptor-relative. Windows keeps the parent
        no-reparse/non-delete-sharing handle live across the unavoidable by-name
        mkdir and then authenticates the child through its own retained handle.
        A setup failure removes only the empty directory this call created.
        """
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise OSError("unsafe staged skill directory name")
        if not self._pinned_parent_matches(parent):
            raise OSError("pending parent changed before staged directory create")
        try:
            self._stat_pinned_child(parent, name)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "staged skill directory exists", name)
        if platform_compat.IS_POSIX:
            if not _DIR_FD_SUPPORTED:
                raise OSError(errno.ENOTSUP, "descriptor-relative staging is unavailable")
            os.mkdir(name, 0o777, dir_fd=parent.fd)
        else:
            os.mkdir(parent.path / name, 0o777)
        created = self._stat_pinned_child(parent, name)
        created_identity: _TaggedFileIdentity | None = None
        try:
            with self._pin_skill_child_parent(parent, name, create=False) as child:
                created_identity = child.native_identity
                if (
                    not platform_compat.IS_WINDOWS
                    and (created.st_dev, created.st_ino) != child.identity
                ):
                    raise OSError("staged skill directory changed during create")
                yield child
        except BaseException:
            self._unlink_skill_child(
                parent,
                name,
                expected=created,
                expected_identity=created_identity,
                directory=True,
            )
            raise

    def _write_pinned_new_file(
        self,
        parent: _PinnedSkillParent,
        name: str,
        payload: bytes,
        *,
        mode: int = 0o666,
    ) -> os.stat_result:
        """Create, flush, and authenticate one new file beneath *parent*."""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise OSError("unsafe staged skill filename")
        if not self._pinned_parent_matches(parent):
            raise OSError("staged skill parent changed before file create")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        path = parent.path / name
        if platform_compat.IS_POSIX and _DIR_FD_SUPPORTED:
            fd = os.open(name, flags, mode, dir_fd=parent.fd)
        else:
            fd = os.open(path, flags, mode)
        owned: os.stat_result | None = None
        owned_identity: _TaggedFileIdentity | None = None
        try:
            opened = os.fstat(fd)
            opened_identity = self._tag_opened_identity(fd)
            current = self._stat_pinned_child(parent, name)
            if (
                opened_identity is None
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not self._opened_path_matches(fd, path, current)
            ):
                raise OSError("staged skill file changed during create")
            owned = current
            owned_identity = opened_identity
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short staged skill file write")
                remaining = remaining[written:]
            os.fsync(fd)
            after = os.fstat(fd)
            current = self._stat_pinned_child(parent, name)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or after.st_size != len(payload)
                or self._tag_opened_identity(fd) != opened_identity
                or not self._opened_path_matches(fd, path, current)
                or not self._pinned_parent_matches(parent)
            ):
                raise OSError("staged skill file changed during write")
            owned = current
            owned_identity = opened_identity
            return current
        except BaseException:
            if owned is not None:
                self._unlink_skill_child(
                    parent,
                    name,
                    expected=owned,
                    expected_identity=owned_identity,
                )
            raise
        finally:
            os.close(fd)

    def _stage_candidate_under_pending_parent(
        self,
        pending_parent: _PinnedSkillParent,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
        scripts: list[dict] | None,
        source: str,
        kind: str,
        target: str | None,
        base_version: int | None,
        notify: bool,
        base_content_hash: str | None,
    ) -> tuple[str, bytes, list[str]]:
        """Write one complete candidate beneath the retained pending parent."""
        content = _build_auto_skill_content(
            slug=slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )
        content_bytes = content.encode("utf-8")
        candidate_path = pending_parent.path / slug
        candidate_identity: _TaggedFileIdentity | None = None
        script_names: list[str] = []
        try:
            with self._create_pinned_child_exclusive(pending_parent, slug) as candidate:
                candidate_identity = candidate.native_identity
                self._write_pinned_new_file(candidate, "SKILL.md", content_bytes)
                clean_scripts = [entry for entry in (scripts or []) if isinstance(entry, dict)]
                if clean_scripts:
                    with self._create_pinned_child_exclusive(candidate, "scripts") as script_parent:
                        for entry in clean_scripts:
                            filename = str(entry.get("filename", "")).strip()
                            if (
                                not filename
                                or "/" in filename
                                or "\\" in filename
                                or ".." in filename
                            ):
                                continue
                            self._write_pinned_new_file(
                                script_parent,
                                filename,
                                str(entry.get("content", "")).encode("utf-8"),
                            )
                            script_names.append(filename)
                        self._sync_pinned_parent(script_parent)
                metadata: dict[str, object] = {
                    "slug": slug,
                    "name": f"{AUTO_SKILL_NAMESPACE}/{slug}",
                    "source": source,
                    "created_at": provenance.created_at or AutoSkillProvenance.now_iso(),
                    "description": description,
                    "triggers": triggers,
                    "has_scripts": bool(script_names),
                    "scripts": script_names,
                    "kind": kind or "new",
                    "notify_suppressed": not notify,
                }
                if target is not None:
                    metadata["target"] = target
                if base_version is not None:
                    metadata["base_version"] = base_version
                if base_content_hash is not None:
                    metadata["base_content_hash"] = base_content_hash
                self._write_pinned_new_file(
                    candidate,
                    ".meta.json",
                    json.dumps(metadata, indent=2).encode("utf-8"),
                )
                self._sync_pinned_parent(candidate)
                if not self._pinned_parent_matches(pending_parent):
                    raise OSError("pending parent changed during candidate staging")
                self._sync_pinned_parent(pending_parent)
                if not self._pinned_parent_matches(candidate):
                    raise OSError("staged candidate changed before commit")
        except BaseException:
            if candidate_identity is not None and not self._remove_private_tree(
                candidate_path,
                what="partial pending skill candidate",
                expected_identity=candidate_identity,
            ):
                logger.warning(
                    "Could not remove refused staged candidate %s without touching a replacement",
                    candidate_path,
                )
            raise
        return f"{AUTO_SKILL_NAMESPACE}/{slug}", content_bytes, script_names

    @contextmanager
    def _auto_slug_claim_lock(self) -> Iterator[bool]:
        """Hold one exclusive lock across an availability test and its claim."""
        yield from _auto_skills._auto_slug_claim_lock(self)

    def _auto_slug_available(
        self,
        slug: str,
        *,
        claim: Literal["live", "pending-new", "pending-update"] = "live",
    ) -> bool:
        """True when ``slug`` is free for the allocation named by ``claim``."""
        return _auto_skills._auto_slug_available(self, slug, claim=claim)

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
        refusal: ClaimRefusal | None = None,
        notify: bool = True,
        base_content_hash: str | None = None,
        unattended_binding_out: list[str] | None = None,
    ) -> str | None:
        """Write a skill candidate to the pending queue (not live).

        The caller passes already-redacted content. ``None`` means nothing was staged; a
        ``ClaimRefusal`` says whether it was a lock. Contract and rationale:
        ``skill_runtime.auto_skills.stage_skill_candidate``.
        """
        return _auto_skills.stage_skill_candidate(
            self,
            slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
            scripts=scripts,
            source=source,
            kind=kind,
            target=target,
            base_version=base_version,
            refusal=refusal,
            notify=notify,
            base_content_hash=base_content_hash,
            unattended_binding_out=unattended_binding_out,
        )

    def _candidate_metadata_from_bytes(self, raw: bytes | None, *, redact: bool) -> dict:
        """Parse metadata already captured from an authenticated tree snapshot."""
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        if not redact:
            return data
        redacted = self._redact_deep(data)
        return redacted if isinstance(redacted, dict) else {}

    def _capture_claim_snapshot(self, candidate_dir: Path) -> _ClaimSnapshot:
        """Capture immutable candidate authority without reopening its metadata."""
        tree = self._skill_tree_snapshot(candidate_dir)
        return _ClaimSnapshot(
            generation_hash=tree.generation_hash if tree is not None else None,
            metadata_bytes=(tree.files.get(Path(".meta.json")) if tree is not None else None),
            tree=tree,
        )

    def _read_pending_meta(self, slug: str) -> dict:
        """The pending candidate's metadata, for display and routing only.

        Advisory: a writer can still change the public candidate, so nothing that
        authorizes a promotion reads it. Promotion parses the metadata captured in
        the claimed generation instead (``_candidate_metadata_from_bytes``).
        """
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

    @staticmethod
    def _emit_pending_staged_metadata(slug: str, meta: dict) -> None:
        """Emit one staged event from metadata the caller already captured."""
        _emit_pending_staged(
            {
                "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{slug}"),
                "slug": slug,
                "kind": meta.get("kind", "new"),
                "target": meta.get("target"),
                "source": meta.get("source", ""),
                "has_scripts": meta.get("has_scripts") is True,
                "description": meta.get("description", ""),
                "triggers": meta.get("triggers", ""),
            }
        )

    def emit_pending_staged(self, slug: str) -> None:
        """Emit a review notification from one captured pending generation."""
        if not self._is_pending_slug_safe(slug):
            return
        candidate = self._pending_root() / slug
        captured = self._capture_claim_snapshot(candidate)
        if captured.generation_hash is None:
            return
        meta = self._candidate_metadata_from_bytes(captured.metadata_bytes, redact=True)
        self._emit_pending_staged_metadata(slug, meta)

    def _claim_pending_update(self, slug: str) -> tuple[Path, int, str, _ClaimSnapshot] | None:
        """Atomically quarantine a public inode and materialize its trusted claim."""
        if not self._is_pending_slug_safe(slug):
            return None
        if not self._private_state_roots_safe(create=True, require_sensitive=True):
            logger.error(
                "Refusing to claim pending skill %s: private state is unsafe",
                slug,
            )
            return None
        # Product writers recover every abandoned prepared publication before
        # claiming more work, so a later update cannot hide whether the prior
        # atomic live replace committed. Recovery acquires its own pinned
        # hierarchy rather than inheriting this admission verdict.
        self._recover_abandoned_claims(roots_authenticated=True)
        # Restoration and publication require atomic no-replace renames. Prove
        # that primitive before removing the visible pending name.
        if not self._probe_no_replace_rename():
            return None
        claim_name = f"{slug}--{secrets.token_hex(16)}"
        claim = self._claims_root() / claim_name
        quarantine = self._quarantine_root() / claim_name
        lock_path = self._claim_lock_path(claim_name)
        fd: int | None = None
        claim_lock_acquired = False
        try:
            with self._pin_private_state(
                create=True,
                require_sensitive=True,
            ) as private_state:
                created_lock: list[bool] = []
                fd = self._open_skill_lock(
                    private_state.claim_locks,
                    lock_path.name,
                    created_out=created_lock,
                )
                if not platform_compat.try_acquire_lock(fd, exclusive=True):
                    raise BlockingIOError("claim lock is held")
                claim_lock_acquired = True
                if not self._initialize_claim_lock_state(fd, lock_path, claim_name):
                    raise OSError("claim lock initialization failed")
                if created_lock == [True]:
                    # File fsync makes the payload durable; parent fsync makes
                    # this fresh journal NAME durable before public mutation.
                    self._sync_pinned_parent(private_state.claim_locks)
        except (OSError, RuntimeError, ValueError):
            if claim_lock_acquired and fd is not None:
                platform_compat.release_lock(fd)
            if fd is not None:
                os.close(fd)
            self._cleanup_claim_lock(claim_name)
            return None
        assert fd is not None
        consumed_at = datetime.now(tz=timezone.utc).isoformat()
        claim_snapshot: _ClaimSnapshot | None = None
        quarantined = False
        try:
            lock_state: list[_PinnedPrivateState] = []
            with self._file_lock("pending.lock", state_out=lock_state) as acquired:
                if not acquired or not lock_state:
                    raise OSError("pending namespace lock unavailable")
                private_state = lock_state[0]
                pending_parent = private_state.pending
                candidate_info = self._stat_pinned_child(pending_parent, slug)
                if self._pinned_parent_matches(pending_parent):
                    linked_candidate = (
                        stat.S_ISLNK(candidate_info.st_mode)
                        or bool(getattr(candidate_info, "st_reparse_tag", 0))
                        or not stat.S_ISDIR(candidate_info.st_mode)
                    )
                    initial_candidate_identity = self._pinned_child_identity(
                        pending_parent,
                        slug,
                    )
                    if initial_candidate_identity is None:
                        raise OSError("pending candidate identity is unavailable before admission")
                    marker_present = False
                    captured_root_identity: _TaggedFileIdentity | None = initial_candidate_identity
                    captured_tree: _SkillTreeSnapshot | None = None
                    if not linked_candidate:
                        with self._pin_skill_child_parent(
                            pending_parent,
                            slug,
                            create=False,
                        ) as candidate_parent:
                            admitted_info = self._stat_pinned_child(pending_parent, slug)
                            if not platform_compat.IS_WINDOWS and not os.path.samestat(
                                candidate_info, admitted_info
                            ):
                                raise OSError("pending candidate changed before capture")
                            if not platform_compat.IS_WINDOWS and candidate_parent.identity != (
                                admitted_info.st_dev,
                                admitted_info.st_ino,
                            ):
                                raise OSError("pending candidate changed before capture")
                            if platform_compat.IS_WINDOWS and (
                                candidate_parent.native_identity != initial_candidate_identity
                                or not self._opened_path_matches(
                                    candidate_parent.fd,
                                    candidate_parent.path,
                                    admitted_info,
                                )
                            ):
                                raise OSError("pending candidate changed before capture")
                            candidate_info = admitted_info
                            captured_root_identity = self._tag_opened_identity(candidate_parent.fd)
                            if captured_root_identity is None:
                                raise OSError("pending candidate identity is unavailable")
                            try:
                                self._stat_pinned_child(candidate_parent, ".promoted")
                            except FileNotFoundError:
                                pass
                            else:
                                marker_present = True
                            captured_tree = self._skill_tree_snapshot_child(
                                pending_parent,
                                slug,
                            )
                            if (
                                captured_tree is not None
                                and captured_tree.root_identity != captured_root_identity
                            ):
                                raise OSError("pending candidate changed during capture")
                            captured_info = self._stat_pinned_child(pending_parent, slug)
                            if not platform_compat.IS_WINDOWS and not os.path.samestat(
                                candidate_info, captured_info
                            ):
                                raise OSError("pending candidate changed during capture")
                            if platform_compat.IS_WINDOWS and (
                                self._pinned_child_identity(pending_parent, slug)
                                != captured_root_identity
                                or not self._opened_path_matches(
                                    candidate_parent.fd,
                                    candidate_parent.path,
                                    captured_info,
                                )
                            ):
                                raise OSError("pending candidate changed during capture")
                            candidate_info = captured_info
                    claim_snapshot = _ClaimSnapshot(
                        captured_tree.generation_hash if captured_tree is not None else None,
                        (
                            captured_tree.files.get(Path(".meta.json"))
                            if captured_tree is not None
                            else None
                        ),
                        tree=captured_tree,
                        quarantine_identity=captured_root_identity,
                    )
                    snapshot_generation = claim_snapshot.generation_hash
                    snapshot_tree = claim_snapshot.tree
                    has_snapshot = snapshot_generation is not None and snapshot_tree is not None
                    # Linked candidates are never publishable, but dismissal and
                    # refusal still atomically remove their public pending name.
                    if not has_snapshot and not (linked_candidate or marker_present):
                        raise OSError("pending candidate snapshot is unreadable")
                    if has_snapshot and not self._write_claim_snapshot_state(
                        fd,
                        lock_path,
                        claim_name,
                        claim_snapshot,
                    ):
                        raise OSError("claim generation witness failed")
                    if linked_candidate:
                        assert captured_root_identity is not None
                        self._rename_untrusted_link_no_replace(
                            pending_parent,
                            slug,
                            private_state.quarantine,
                            claim_name,
                            expected_identity=captured_root_identity,
                        )
                    else:
                        self._rename_skill_child_no_replace(
                            pending_parent,
                            slug,
                            private_state.quarantine,
                            claim_name,
                            expected_identity=captured_root_identity,
                        )
                    quarantined = True
                    if captured_root_identity is not None:
                        if linked_candidate:
                            quarantine_root_identity = self._pinned_child_identity(
                                private_state.quarantine,
                                claim_name,
                            )
                        else:
                            with self._pin_skill_child_parent(
                                private_state.quarantine,
                                claim_name,
                                create=False,
                            ) as quarantine_parent:
                                quarantine_root_identity = self._tag_opened_identity(
                                    quarantine_parent.fd
                                )
                        if quarantine_root_identity != captured_root_identity:
                            raise OSError("quarantined candidate differs from captured candidate")
                    self._sync_pinned_rename_parents(
                        pending_parent,
                        private_state.quarantine,
                    )
                    if has_snapshot:
                        assert snapshot_generation is not None
                        assert snapshot_tree is not None
                        # The public inode can still have retained writers. It
                        # never enters .private; only these captured bytes do.
                        if not self._pinned_parent_matches(private_state.claims):
                            raise OSError("trusted claims parent changed before materialization")
                        materialization = self._claim_materialization_path(claim_name)
                        if os.path.lexists(materialization):
                            raise OSError("trusted claim materialization stage already exists")
                        self._materialize_skill_tree_snapshot(
                            snapshot_tree,
                            materialization,
                        )
                        self._sync_skill_tree(materialization)
                        self._rename_skill_child_no_replace(
                            private_state.private,
                            materialization.name,
                            private_state.claims,
                            claim_name,
                        )
                        if not self._pinned_parent_matches(private_state.claims):
                            raise OSError("trusted claims parent changed during materialization")
                        materialized = self._stat_pinned_child(
                            private_state.claims,
                            claim_name,
                        )
                        if not stat.S_ISDIR(materialized.st_mode) or is_link_or_junction(claim):
                            raise OSError("trusted claim materialization is unsafe")
                        self._sync_skill_tree(claim)
                        # The claims destination is authority; the source
                        # materialization is disposable and may safely reappear.
                        self._sync_pinned_parent(private_state.claims)
                        trusted_snapshot = self._capture_claim_snapshot(claim)
                        if (
                            trusted_snapshot.generation_hash is None
                            or trusted_snapshot.tree is None
                            or not secrets.compare_digest(
                                snapshot_generation,
                                trusted_snapshot.generation_hash,
                            )
                        ):
                            raise OSError("trusted claim materialization changed")
                        claim_snapshot = _ClaimSnapshot(
                            trusted_snapshot.generation_hash,
                            trusted_snapshot.metadata_bytes,
                            tree=trusted_snapshot.tree,
                            quarantine_identity=claim_snapshot.quarantine_identity,
                        )
        except OSError:
            if quarantined and claim_snapshot is not None:
                self._cleanup_publication_artifacts(claim_name)
                self._restore_claimed_update(claim, fd, slug, claim_snapshot)
            platform_compat.release_lock(fd)
            os.close(fd)
            if (
                not os.path.lexists(claim)
                and not os.path.lexists(quarantine)
                and not os.path.lexists(self._evidence_root() / claim_name)
            ):
                self._cleanup_claim_lock(claim_name)
            return None
        if claim_snapshot is None:
            platform_compat.release_lock(fd)
            os.close(fd)
            self._cleanup_claim_lock(claim_name)
            return None
        if not is_link_or_junction(quarantine) and self._completion_marker_present(quarantine):
            logger.warning("Refusing pending skill %s: reserved completion marker exists", slug)
            try:
                if self._remove_untrusted_completion_marker(quarantine):
                    self._restore_claimed_update(claim, fd, slug, claim_snapshot)
            except OSError:
                logger.error("Could not restore marker-bearing claim %s", quarantine, exc_info=True)
            finally:
                platform_compat.release_lock(fd)
                os.close(fd)
                if (
                    not os.path.lexists(claim)
                    and not os.path.lexists(quarantine)
                    and not os.path.lexists(self._evidence_root() / claim_name)
                ):
                    self._cleanup_claim_lock(claim_name)
            return None
        return claim, fd, consumed_at, claim_snapshot

    def _retain_claim_evidence(self, claim: Path, claim_fd: int) -> bool:
        """Retain private evidence while both public identities remain exact."""
        lock_path = self._claim_lock_path(claim.name)
        if not self._commit_claim_evidence_state(claim_fd, lock_path, claim.name):
            logger.error("Could not commit private evidence state for %s", claim)
            return False
        evidence_state = self._authenticated_claim_evidence_state(
            claim_fd,
            lock_path,
            claim.name,
        )
        if evidence_state is None:
            return False
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as private_state:
                if not self._public_claim_evidence_matches(
                    private_state,
                    evidence_state,
                    claim.name,
                ):
                    return False
                try:
                    source = self._stat_pinned_child(private_state.claims, claim.name)
                except FileNotFoundError:
                    try:
                        retained = self._stat_pinned_child(
                            private_state.evidence,
                            claim.name,
                        )
                    except OSError:
                        return False
                    retained_ok = stat.S_ISDIR(retained.st_mode) and not is_link_or_junction(
                        self._evidence_root() / claim.name
                    )
                else:
                    if not stat.S_ISDIR(source.st_mode) or is_link_or_junction(claim):
                        return False
                    try:
                        self._stat_pinned_child(private_state.evidence, claim.name)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return False
                    else:
                        return False
                    self._rename_skill_child_no_replace(
                        private_state.claims,
                        claim.name,
                        private_state.evidence,
                        claim.name,
                    )
                    self._sync_pinned_rename_parents(
                        private_state.claims,
                        private_state.evidence,
                    )
                    retained = self._stat_pinned_child(private_state.evidence, claim.name)
                    retained_ok = stat.S_ISDIR(retained.st_mode) and not is_link_or_junction(
                        self._evidence_root() / claim.name
                    )
                if not retained_ok:
                    return False
                # Linearize completion only after the private move and a fresh
                # check of BOTH descriptor-contaminated public generations.
                return self._public_claim_evidence_matches(
                    private_state,
                    evidence_state,
                    claim.name,
                )
        except (OSError, RuntimeError, ValueError):
            logger.error("Could not retain trusted claim evidence %s", claim, exc_info=True)
            return False

    def _discard_trusted_claim(self, claim: Path) -> bool:
        """Remove only the fresh private materialization for one restored claim."""
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as private_state:
                try:
                    claim_info = self._stat_pinned_child(private_state.claims, claim.name)
                except FileNotFoundError:
                    return True
                if not stat.S_ISDIR(claim_info.st_mode) or is_link_or_junction(claim):
                    return False
                claim_identity = self._pinned_child_identity(
                    private_state.claims,
                    claim.name,
                )
                if claim_identity is None:
                    return False
                return self._remove_private_tree(
                    claim,
                    what="restored trusted skill claim",
                    expected_identity=claim_identity,
                )
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return False

    def _restore_claimed_update(
        self,
        claim: Path,
        claim_fd: int,
        slug: str,
        claim_snapshot: _ClaimSnapshot | None = None,
    ) -> Path | None:
        """Requeue the exact quarantined public inode without replacement."""
        notify = False
        restored: Path | None = None
        captured = claim_snapshot or _ClaimSnapshot(None, None)
        restored_meta = self._candidate_metadata_from_bytes(
            captured.metadata_bytes,
            redact=False,
        )
        lock_state: list[_PinnedPrivateState] = []
        with self._file_lock("pending.lock", state_out=lock_state) as acquired:
            if not acquired or not lock_state:
                logger.error("Could not restore claimed update %s", claim)
                return None
            try:
                private_state = lock_state[0]
                pending_parent = private_state.pending
                if self._pinned_parent_matches(pending_parent):
                    quarantined = self._stat_pinned_child(
                        private_state.quarantine,
                        claim.name,
                    )
                    quarantine_linked = (
                        stat.S_ISLNK(quarantined.st_mode)
                        or bool(getattr(quarantined, "st_reparse_tag", 0))
                        or is_link_or_junction(private_state.quarantine.path / claim.name)
                    )
                    quarantine_identity = self._pinned_child_identity(
                        private_state.quarantine,
                        claim.name,
                    )
                    if quarantine_identity is None:
                        raise OSError("public quarantine identity is unavailable")
                    if captured.quarantine_identity is not None:
                        if quarantine_identity != captured.quarantine_identity or (
                            not quarantine_linked and not stat.S_ISDIR(quarantined.st_mode)
                        ):
                            raise OSError("public quarantine changed identity")
                    elif not quarantine_linked:
                        raise OSError("public quarantine has no authenticated identity")
                    has_authenticated_snapshot = (
                        self._authenticated_claim_snapshot_state(
                            claim_fd,
                            self._claim_lock_path(claim.name),
                            claim.name,
                        )
                        is not None
                        or self._authenticated_claim_publication(
                            claim_fd,
                            self._claim_lock_path(claim.name),
                            claim.name,
                        )
                        is not None
                    )
                    for number in [None, *range(2, 51)]:
                        if number is None:
                            candidate_slug = slug
                        else:
                            suffix = f"-{number}"
                            candidate_slug = f"{slug[: 64 - len(suffix)].rstrip('-')}{suffix}"
                        notify = (
                            restored_meta.get("notify_suppressed") is True or candidate_slug != slug
                        )
                        receipt_written = False
                        if has_authenticated_snapshot:
                            if quarantine_identity is None:
                                raise OSError("authenticated restore has no native identity")
                            receipt_written = self._write_claim_restore_state(
                                claim_fd,
                                self._claim_lock_path(claim.name),
                                claim.name,
                                restore_slug=candidate_slug,
                                restore_identity=quarantine_identity,
                            )
                            if not receipt_written:
                                raise OSError("claim restore receipt failed")
                        try:
                            if quarantine_linked:
                                self._rename_untrusted_link_no_replace(
                                    private_state.quarantine,
                                    claim.name,
                                    pending_parent,
                                    candidate_slug,
                                    expected_identity=quarantine_identity,
                                )
                            else:
                                assert quarantine_identity is not None
                                self._rename_skill_child_no_replace(
                                    private_state.quarantine,
                                    claim.name,
                                    pending_parent,
                                    candidate_slug,
                                    expected_identity=quarantine_identity,
                                )
                        except OSError as exc:
                            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                                raise
                            if receipt_written and not self._clear_claim_restore_state(
                                claim_fd,
                                self._claim_lock_path(claim.name),
                                claim.name,
                            ):
                                raise OSError("could not clear refused restore receipt") from exc
                            continue
                        self._sync_pinned_rename_parents(
                            private_state.quarantine,
                            pending_parent,
                        )
                        restored = self._pending_root() / candidate_slug
                        restored_info = self._stat_pinned_child(
                            pending_parent,
                            candidate_slug,
                        )
                        if quarantine_linked:
                            if (
                                not (
                                    stat.S_ISLNK(restored_info.st_mode)
                                    or bool(getattr(restored_info, "st_reparse_tag", 0))
                                    or is_link_or_junction(restored)
                                )
                                or self._pinned_child_identity(
                                    pending_parent,
                                    candidate_slug,
                                )
                                != quarantine_identity
                            ):
                                raise OSError("restored pending link changed identity")
                        else:
                            restored_identity = self._pinned_child_identity(
                                pending_parent,
                                candidate_slug,
                            )
                            if (
                                not stat.S_ISDIR(restored_info.st_mode)
                                or restored_identity != quarantine_identity
                            ):
                                raise OSError("restored pending inode changed identity")
                        if not self._discard_trusted_claim(claim):
                            logger.error(
                                "Could not remove restored trusted materialization %s",
                                claim,
                            )
                        break
            except OSError:
                logger.error("Could not restore claimed update %s", claim, exc_info=True)
                return None
            if restored is None:
                logger.error("No pending slot available to restore %s", claim)
                return None
        if notify:
            notification_meta = dict(restored_meta)
            notification_meta["slug"] = restored.name
            notification_meta["name"] = f"{AUTO_SKILL_NAMESPACE}/{restored.name}"
            safe_meta = self._redact_deep(notification_meta)
            self._emit_pending_staged_metadata(
                restored.name,
                safe_meta if isinstance(safe_meta, dict) else {},
            )
        return restored

    @staticmethod
    def _lone_regular_file_hash(path: Path) -> str | None:
        """Hash a lone regular file without following a link or junction."""
        if is_link_or_junction(path):
            return None
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    @staticmethod
    def _skill_tree_entries_hash(entries: list[tuple[str, str, int, bytes]]) -> str:
        """Hash one captured tree manifest with the publication digest format."""
        digest = hashlib.sha256()
        for kind, relative, mode, payload in sorted(entries):
            path_bytes = relative.encode("utf-8")
            digest.update(kind.encode("ascii"))
            digest.update(len(path_bytes).to_bytes(8, "big"))
            digest.update(path_bytes)
            digest.update(mode.to_bytes(4, "big"))
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        return digest.hexdigest()

    def _lone_regular_file_hash_at(
        self,
        parent: _PinnedSkillParent,
        name: str,
    ) -> str | None:
        """Hash one stable lone file relative to an authenticated parent."""
        fd: int | None = None
        try:
            before = self._stat_pinned_child(parent, name)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            if platform_compat.IS_WINDOWS:
                fd = platform_compat.open_file_no_reparse(parent.path / name)
            else:
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(name, flags, dir_fd=parent.fd)
            opened = os.fstat(fd)
            if (
                not self._opened_path_matches(fd, parent.path / name, before)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                return None
            captured = self._stable_file_payload(
                fd,
                opened,
                max_bytes=_SKILL_SNAPSHOT_MAX_FILE_BYTES,
            )
            if captured is None or not self._pinned_parent_matches(parent):
                return None
            return hashlib.sha256(captured[0]).hexdigest()
        except OSError:
            return None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _stable_file_payload(
        fd: int,
        opened: os.stat_result,
        *,
        max_bytes: int,
    ) -> tuple[bytes, os.stat_result] | None:
        """Read one bounded regular opened inode and prove it stayed unchanged."""
        if opened.st_size < 0 or opened.st_size > max_bytes:
            return None
        payload = bytearray(opened.st_size)
        view = memoryview(payload)
        offset = 0
        while offset < opened.st_size:
            chunk = os.read(fd, min(opened.st_size - offset, 1024 * 1024))
            if not chunk:
                return None
            view[offset : offset + len(chunk)] = chunk
            offset += len(chunk)
        if os.read(fd, 1):
            return None
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (
                not platform_compat.IS_WINDOWS
                and (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            )
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(after.st_mode)
        ):
            return None
        return bytes(payload), after

    @staticmethod
    def _skill_tree_snapshot_pinned(
        root: Path,
        *,
        parent: _PinnedSkillParent | None = None,
        name: str | None = None,
    ) -> _SkillTreeSnapshot | None:
        """Capture a tree through one pinned root and descriptor-relative descendants."""
        root_fd: int | None = None
        cache: dict[tuple[str, ...], int] = {}
        try:
            if parent is None:
                root_fd = pinned_fs.open_dir_pinned(root, what="live skill tree", refusal=OSError)
            else:
                if name is None or not SkillsLoader._pinned_parent_matches(parent):
                    return None
                before = SkillsLoader._stat_pinned_child(parent, name)
                if not stat.S_ISDIR(before.st_mode) or is_link_or_junction(root):
                    return None
                root_fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
                if not platform_compat.IS_WINDOWS and not os.path.samestat(
                    before, os.fstat(root_fd)
                ):
                    return None
            if root_fd is None:  # defensive typing; open_dir_pinned returns int or raises
                return None
            root_info = os.fstat(root_fd)
            if not stat.S_ISDIR(root_info.st_mode):
                return None
            device = root_info.st_dev
            tree = pinned_fs.scan_tree_pinned(
                root_fd,
                device=device,
                max_entries=_SKILL_SNAPSHOT_MAX_ENTRIES,
                max_depth=_SKILL_SNAPSHOT_MAX_DEPTH,
            )
            if tree.links:
                return None

            files: dict[Path, bytes] = {}
            file_modes: dict[Path, int] = {}
            dir_modes: dict[Path, int] = {Path("."): stat.S_IMODE(root_info.st_mode)}
            entries: list[tuple[str, str, int, bytes]] = [
                ("d", ".", stat.S_IMODE(root_info.st_mode), b"")
            ]
            total_bytes = 0
            for parts in sorted(tree.dirs, key=lambda item: (len(item), item)):
                fd = pinned_fs.open_verified_chain(
                    root_fd,
                    parts,
                    cache=cache,
                    dirs=tree.dirs,
                    device=device,
                )
                info = os.fstat(fd)
                relative = Path(*parts)
                mode = stat.S_IMODE(info.st_mode)
                dir_modes[relative] = mode
                entries.append(("d", relative.as_posix(), mode, b""))

            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            for parts, expected_inode in sorted(tree.files.items()):
                parent_fd = pinned_fs.open_verified_chain(
                    root_fd,
                    parts[:-1],
                    cache=cache,
                    dirs=tree.dirs,
                    device=device,
                )
                file_before = pinned_fs.stat_at(parent_fd, parts[-1])
                remaining_budget = _SKILL_SNAPSHOT_MAX_TOTAL_BYTES - total_bytes
                read_limit = min(_SKILL_SNAPSHOT_MAX_FILE_BYTES, remaining_budget)
                if (
                    file_before is None
                    or not stat.S_ISREG(file_before.st_mode)
                    or file_before.st_nlink != 1
                    or (file_before.st_dev, file_before.st_ino) != (device, expected_inode)
                    or file_before.st_size < 0
                    or file_before.st_size > read_limit
                ):
                    return None
                fd = os.open(parts[-1], flags, dir_fd=parent_fd)
                try:
                    opened = os.fstat(fd)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (device, expected_inode)
                        or opened.st_size < 0
                        or opened.st_size > read_limit
                    ):
                        return None
                    captured = SkillsLoader._stable_file_payload(
                        fd,
                        opened,
                        max_bytes=read_limit,
                    )
                finally:
                    os.close(fd)
                if captured is None:
                    return None
                payload, after = captured
                total_bytes += len(payload)
                relative = Path(*parts)
                mode = stat.S_IMODE(after.st_mode)
                files[relative] = payload
                file_modes[relative] = mode
                entries.append(("f", relative.as_posix(), mode, payload))
            root_identity = SkillsLoader._tag_opened_identity(root_fd)
            if root_identity is None:
                return None
            snapshot = _SkillTreeSnapshot(
                files=files,
                file_modes=file_modes,
                dir_modes=dir_modes,
                generation_hash=SkillsLoader._skill_tree_entries_hash(entries),
                root_identity=root_identity,
            )
            if parent is not None and not SkillsLoader._pinned_parent_matches(parent):
                return None
            return snapshot
        except (OSError, ValueError):
            return None
        finally:
            pinned_fs.drain_verified_chain(cache)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _snapshot_path_matches(fd: int, expected: str | Path) -> bool:
        """Authenticate an opened snapshot entry against its expected location.

        Windows compares native IDs from two no-reparse handles, so 8.3 and long
        path spellings of one object agree without reopening through
        ``os.path.samefile``. POSIX keeps the descriptor-derived path
        comparison: directory handles there do not prevent renames, so a
        by-name identity reopen would create a new race.
        """
        if platform_compat.IS_WINDOWS:
            return platform_compat.opened_path_identity_matches(fd, expected)
        opened = pinned_fs.fd_real_path(fd)
        if opened is None:
            return False
        return os.path.normcase(os.path.normpath(opened)) == os.path.normcase(
            os.path.normpath(expected)
        )

    @staticmethod
    def _skill_tree_snapshot_by_name(root: Path) -> _SkillTreeSnapshot | None:
        """Fallback: hold directories and authenticate each opened file by location."""
        if is_link_or_junction(root):
            return None
        held_dirs: list[int] = []
        try:
            expected_root = os.path.realpath(root)
            root_fd = platform_compat.pin_directory(root)
            held_dirs.append(root_fd)
            root_info = os.fstat(root_fd)
            real_root = pinned_fs.fd_real_path(root_fd)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or (not platform_compat.IS_WINDOWS and real_root is None)
                or not SkillsLoader._snapshot_path_matches(root_fd, expected_root)
            ):
                return None
            identity_root = expected_root if platform_compat.IS_WINDOWS else real_root
            if identity_root is None:  # narrowed above for POSIX; defensive for typing
                return None

            files: dict[Path, bytes] = {}
            file_modes: dict[Path, int] = {}
            dir_modes: dict[Path, int] = {Path("."): stat.S_IMODE(root_info.st_mode)}
            entries: list[tuple[str, str, int, bytes]] = [
                ("d", ".", stat.S_IMODE(root_info.st_mode), b"")
            ]
            stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
            entry_count = 0
            total_bytes = 0

            def opened_at(fd: int, parts: tuple[str, ...]) -> bool:
                expected = os.path.join(identity_root, *parts)
                return SkillsLoader._snapshot_path_matches(fd, expected)

            while stack:
                current_path, parent_parts = stack.pop()
                with os.scandir(current_path) as listing:
                    for entry in listing:
                        parts = parent_parts + (entry.name,)
                        entry_count += 1
                        if entry_count > _SKILL_SNAPSHOT_MAX_ENTRIES:
                            return None
                        if len(parts) > _SKILL_SNAPSHOT_MAX_DEPTH:
                            return None
                        entry_path = Path(entry.path)
                        if entry.is_symlink() or is_link_or_junction(entry_path):
                            return None
                        before = entry.stat(follow_symlinks=False)
                        relative = Path(*parts)
                        if stat.S_ISDIR(before.st_mode):
                            child_fd = platform_compat.pin_directory(entry_path)
                            held_dirs.append(child_fd)
                            opened = os.fstat(child_fd)
                            if (
                                not stat.S_ISDIR(opened.st_mode)
                                or not SkillsLoader._opened_path_matches(
                                    child_fd,
                                    entry_path,
                                    before,
                                )
                                or not opened_at(child_fd, parts)
                            ):
                                return None
                            mode = stat.S_IMODE(opened.st_mode)
                            dir_modes[relative] = mode
                            entries.append(("d", relative.as_posix(), mode, b""))
                            stack.append((entry_path, parts))
                            continue
                        # ``os.DirEntry.stat()`` sets st_ino, st_dev and
                        # st_nlink to zero on Windows (the directory-enumeration
                        # data carries no link count), so a pre-open
                        # ``before.st_nlink != 1`` would refuse EVERY regular
                        # file there and fail-close the whole fallback snapshot.
                        # The single-link requirement is still enforced below on
                        # the OPENED descriptor, whose ``os.fstat`` reads the
                        # real ``nNumberOfLinks`` on Windows -- that is the
                        # authoritative check the spec names ("the opened
                        # inode's ... checks still run before bytes are
                        # accepted"). POSIX keeps the pre-open gate as an early
                        # reject.
                        if not stat.S_ISREG(before.st_mode) or (
                            not platform_compat.IS_WINDOWS and before.st_nlink != 1
                        ):
                            return None
                        remaining_budget = _SKILL_SNAPSHOT_MAX_TOTAL_BYTES - total_bytes
                        read_limit = min(_SKILL_SNAPSHOT_MAX_FILE_BYTES, remaining_budget)
                        if before.st_size < 0 or before.st_size > read_limit:
                            return None
                        fd = platform_compat.open_file_no_reparse(
                            entry_path,
                            nonblocking=True,
                        )
                        try:
                            opened = os.fstat(fd)
                            if (
                                not stat.S_ISREG(opened.st_mode)
                                or opened.st_nlink != 1
                                or not SkillsLoader._opened_path_matches(
                                    fd,
                                    entry_path,
                                    before,
                                )
                                or not opened_at(fd, parts)
                                or opened.st_size < 0
                                or opened.st_size > read_limit
                            ):
                                return None
                            captured = SkillsLoader._stable_file_payload(
                                fd,
                                opened,
                                max_bytes=read_limit,
                            )
                        finally:
                            os.close(fd)
                        if captured is None:
                            return None
                        payload, after = captured
                        total_bytes += len(payload)
                        mode = stat.S_IMODE(after.st_mode)
                        files[relative] = payload
                        file_modes[relative] = mode
                        entries.append(("f", relative.as_posix(), mode, payload))
            if not SkillsLoader._snapshot_path_matches(root_fd, identity_root):
                return None
            root_identity = SkillsLoader._tag_opened_identity(root_fd)
            if root_identity is None:
                return None
            return _SkillTreeSnapshot(
                files=files,
                file_modes=file_modes,
                dir_modes=dir_modes,
                generation_hash=SkillsLoader._skill_tree_entries_hash(entries),
                root_identity=root_identity,
            )
        except (OSError, ValueError):
            return None
        finally:
            pinned_fs.close_all(held_dirs)

    @staticmethod
    def _skill_tree_snapshot(root: Path) -> _SkillTreeSnapshot | None:
        if not platform_compat.IS_WINDOWS and pinned_fs.supports_pinned_tree_walk():
            return SkillsLoader._skill_tree_snapshot_pinned(root)
        return SkillsLoader._skill_tree_snapshot_by_name(root)

    @staticmethod
    def _skill_tree_hash(root: Path) -> str | None:
        """Hash one exact generation without following a file or ancestor link."""
        snapshot = SkillsLoader._skill_tree_snapshot(root)
        return snapshot.generation_hash if snapshot is not None else None

    def _skill_tree_snapshot_child(
        self,
        parent: _PinnedSkillParent,
        name: str,
    ) -> _SkillTreeSnapshot | None:
        """Capture one authority child without abandoning its retained parent."""
        root = parent.path / name
        if platform_compat.IS_POSIX and not platform_compat.IS_WINDOWS:
            if not pinned_fs.supports_pinned_tree_walk():
                return None
            return self._skill_tree_snapshot_pinned(root, parent=parent, name=name)
        if not self._pinned_parent_matches(parent):
            return None
        snapshot = self._skill_tree_snapshot_by_name(root)
        if not self._pinned_parent_matches(parent):
            return None
        return snapshot

    def _skill_tree_hash_child(
        self,
        parent: _PinnedSkillParent,
        name: str,
    ) -> str | None:
        snapshot = self._skill_tree_snapshot_child(parent, name)
        return snapshot.generation_hash if snapshot is not None else None

    def _pinned_child_exists(self, parent: _PinnedSkillParent, name: str) -> bool:
        try:
            self._stat_pinned_child(parent, name)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _sync_skill_tree(root: Path) -> None:
        """Durably flush one authenticated staged generation before journaling.

        Windows ``FlushFileBuffers`` requires a writable handle, so its files
        are opened ``O_RDWR``.  Every platform still opens with ``O_NOFOLLOW``
        where available and compares the descriptor with the lstat identity;
        fixing Windows durability must not turn the flush into a link-following
        read of an attacker-swapped entry.
        """
        directories: list[Path] = []
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            if is_link_or_junction(current_path):
                raise OSError("linked directory in staged skill tree")
            directories.append(current_path)
            for name in files:
                entry = current_path / name
                if is_link_or_junction(entry):
                    raise OSError("linked file in staged skill tree")
                before = os.lstat(entry)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise OSError("unsafe file in staged skill tree")
                access = os.O_RDWR if platform_compat.IS_WINDOWS else os.O_RDONLY
                flags = access | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(entry), flags)
                try:
                    opened = os.fstat(fd)
                    opened_identity = SkillsLoader._tag_opened_identity(fd)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or opened_identity is None
                        or not SkillsLoader._opened_path_matches(fd, entry, before)
                    ):
                        raise OSError("staged skill file changed during durable open")
                    os.fsync(fd)
                    after = os.fstat(fd)
                    after_identity = SkillsLoader._tag_opened_identity(fd)
                    if (
                        not stat.S_ISREG(after.st_mode)
                        or after.st_nlink != 1
                        or after_identity != opened_identity
                        or (
                            not platform_compat.IS_WINDOWS
                            and (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                        )
                    ):
                        raise OSError("staged skill file changed during flush")
                finally:
                    os.close(fd)
            for name in dirs:
                entry = current_path / name
                if is_link_or_junction(entry):
                    raise OSError("linked directory in staged skill tree")
        for directory in reversed(directories):
            fsync_dir(directory)

    def _publication_paths(self, claim_name: str) -> tuple[Path, Path]:
        private = self._private_root()
        return (
            private / f".publish-{claim_name}",
            self._live_quarantine_root() / claim_name,
        )

    def _claim_materialization_path(self, claim_name: str) -> Path:
        token = hashlib.sha256(claim_name.encode("utf-8")).hexdigest()
        return self._private_root() / f".claim-{token}"

    def _restoration_path(self, claim_name: str) -> Path:
        """Return the private claims staging path for one immutable restoration."""
        token = hashlib.sha256(claim_name.encode("utf-8")).hexdigest()
        return self._claims_root() / f".restore-{token}"

    def _cleanup_publication_artifacts(self, claim_name: str) -> bool:
        """Remove only fresh private stage, materialization, and restoration trees."""
        clean = True
        stage, _public_old_live = self._publication_paths(claim_name)
        paths = [
            stage,
            self._claim_materialization_path(claim_name),
            self._restoration_path(claim_name),
        ]
        for path in paths:
            if is_link_or_junction(path):
                clean = False
                continue
            if os.path.lexists(path) and not self._remove_private_tree(
                path,
                what="skill publication artifact",
            ):
                clean = False
        return clean

    def _publish_prepared_skill_tree(
        self,
        *,
        state: _PinnedPrivateState,
        live_dir: Path,
        stage: Path,
        backup: Path | None,
        backup_identity: _TaggedFileIdentity | None,
        before_hash: str | None,
        after_hash: str,
    ) -> str:
        """Publish a prepared generation beneath retained authority parents.

        ``published`` means the exact authenticated before-generation was moved
        aside and the exact prepared after-generation is live. ``drift`` means a
        different live generation reached the mutation boundary and was restored
        byte-for-byte. ``incomplete`` retains the prepared journal and artifacts
        for recovery because the filesystem state cannot be classified safely.
        """
        if (
            live_dir.parent != state.auto.path
            or stage.parent != state.private.path
            or (backup is not None and backup.parent != state.live_quarantine.path)
            or not self._pinned_parent_matches(state.auto)
            or not self._pinned_parent_matches(state.private)
            or not self._pinned_parent_matches(state.live_quarantine)
        ):
            return "incomplete"
        live_name = live_dir.name
        stage_name = stage.name
        backup_name = backup.name if backup is not None else None
        captured_hash: str | None = None
        moved_live = False
        try:
            if backup_name is not None:
                if before_hash is None or backup_identity is None:
                    return "incomplete"
                self._rename_skill_child_no_replace(
                    state.auto,
                    live_name,
                    state.live_quarantine,
                    backup_name,
                    expected_identity=backup_identity,
                )
                moved_live = True
                self._sync_pinned_rename_parents(state.auto, state.live_quarantine)
                current_backup_identity = self._pinned_child_identity(
                    state.live_quarantine,
                    backup_name,
                )
                if current_backup_identity != backup_identity:
                    return "incomplete"
                captured_hash = self._skill_tree_hash_child(
                    state.live_quarantine,
                    backup_name,
                )
                if captured_hash != before_hash:
                    if self._pinned_child_exists(state.auto, live_name):
                        return "incomplete"
                    self._rename_skill_child_no_replace(
                        state.live_quarantine,
                        backup_name,
                        state.auto,
                        live_name,
                        expected_identity=backup_identity,
                    )
                    self._sync_pinned_rename_parents(state.live_quarantine, state.auto)
                    moved_live = False
                    restored_identity = self._pinned_child_identity(state.auto, live_name)
                    restored_hash = self._skill_tree_hash_child(state.auto, live_name)
                    return (
                        "drift"
                        if (
                            restored_identity == backup_identity
                            and captured_hash is not None
                            and restored_hash == captured_hash
                        )
                        else "incomplete"
                    )
            elif before_hash is not None:
                return "incomplete"

            self._rename_skill_child_no_replace(
                state.private,
                stage_name,
                state.auto,
                live_name,
                compensate_mismatch_to_source=False,
            )
            self._sync_pinned_rename_parents(state.private, state.auto)
            if backup_name is not None:
                current_backup_identity = self._pinned_child_identity(
                    state.live_quarantine,
                    backup_name,
                )
                if current_backup_identity != backup_identity:
                    return "incomplete"
                if self._skill_tree_hash_child(state.live_quarantine, backup_name) != before_hash:
                    # The detached pre-publication inode can still have a retained
                    # writer. Its drift does not authorize moving the current live
                    # name into a disposable stage: that name may already hold a
                    # later by-name generation. An exact after-tree is committed and
                    # the changed public backup remains evidence; every other live
                    # state is ambiguous and all artifacts stay in place.
                    live_hash = self._skill_tree_hash_child(state.auto, live_name)
                    return "published" if live_hash == after_hash else "incomplete"
        except OSError:
            if backup_name is not None and moved_live:
                try:
                    backup_info = self._stat_pinned_child(
                        state.live_quarantine,
                        backup_name,
                    )
                    current_backup_identity = self._pinned_child_identity(
                        state.live_quarantine,
                        backup_name,
                    )
                    if (
                        not self._pinned_child_exists(state.auto, live_name)
                        and stat.S_ISDIR(backup_info.st_mode)
                        and current_backup_identity == backup_identity
                        and not is_link_or_junction(state.live_quarantine.path / backup_name)
                    ):
                        self._rename_skill_child_no_replace(
                            state.live_quarantine,
                            backup_name,
                            state.auto,
                            live_name,
                            expected_identity=backup_identity,
                        )
                        self._sync_pinned_rename_parents(
                            state.live_quarantine,
                            state.auto,
                        )
                except OSError:
                    logger.error(
                        "Could not roll back interrupted skill generation swap for %s",
                        live_dir,
                    )
            return "incomplete"
        return (
            "published"
            if self._skill_tree_hash_child(state.auto, live_name) == after_hash
            else "incomplete"
        )

    def _reconcile_prepared_claim(
        self,
        claim: Path,
        claim_fd: int,
        lock_path: Path,
        *,
        private_state: _PinnedPrivateState | None = None,
    ) -> bool | None:
        """Return True if fully published, False if rolled back, else None."""
        journal = self._authenticated_claim_publication(claim_fd, lock_path, claim.name)
        if journal is None:
            return None
        target_slug = str(journal["target"])
        with self._promotion_lock(target_slug) as acquired:
            if not acquired:
                return None
            if private_state is not None:
                if (
                    not self._pinned_parent_matches(private_state.auto)
                    or not self._pinned_parent_matches(private_state.private)
                    or not self._pinned_parent_matches(private_state.live_quarantine)
                ):
                    return None
                return self._reconcile_prepared_claim_pinned(
                    journal,
                    private_state,
                    claim.name,
                )
            try:
                with self._pin_private_state(
                    create=False,
                    require_sensitive=True,
                ) as state:
                    return self._reconcile_prepared_claim_pinned(
                        journal,
                        state,
                        claim.name,
                    )
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                return None

    def _reconcile_prepared_claim_pinned(
        self,
        journal: dict[str, object],
        state: _PinnedPrivateState,
        claim_name: str,
    ) -> bool | None:
        """Classify one journal while every authority parent remains retained."""
        target_slug = str(journal["target"])
        kind = journal["kind"]
        before_hash = journal.get("before")
        after_hash = str(journal["after"])
        journal_format = journal.get("format", 1)
        live_exists = self._pinned_child_exists(state.auto, target_slug)

        if journal_format == 1:
            if kind == "new":
                return False if not live_exists else None
            if not live_exists:
                return None
            try:
                with self._pin_skill_child_parent(
                    state.auto,
                    target_slug,
                    create=False,
                ) as live_parent:
                    current_hash = self._lone_regular_file_hash_at(live_parent, "SKILL.md")
                    if current_hash != before_hash:
                        return None
                    snapshot_value = journal.get("snapshot")
                    if not isinstance(snapshot_value, int):
                        return None
                    try:
                        with self._pin_skill_child_parent(
                            live_parent,
                            VERSIONS_DIRNAME,
                            create=False,
                        ) as versions_parent:
                            orphan_name = f"v{snapshot_value}-SKILL.md"
                            orphan_exists = self._pinned_child_exists(
                                versions_parent,
                                orphan_name,
                            )
                            orphan_hash = (
                                self._lone_regular_file_hash_at(
                                    versions_parent,
                                    orphan_name,
                                )
                                if orphan_exists
                                else None
                            )
                            if orphan_hash == before_hash:
                                orphan_info = self._stat_pinned_child(
                                    versions_parent,
                                    orphan_name,
                                )
                                orphan_identity = self._pinned_child_identity(
                                    versions_parent,
                                    orphan_name,
                                )
                                if not self._unlink_skill_child(
                                    versions_parent,
                                    orphan_name,
                                    expected=orphan_info,
                                    expected_identity=orphan_identity,
                                ):
                                    return None
                                self._sync_pinned_parent(versions_parent)
                            elif orphan_exists:
                                return None
                    except FileNotFoundError:
                        pass
                    return False
            except (FileNotFoundError, OSError):
                return None

        stage, backup = self._publication_paths(claim_name)
        stage_exists = self._pinned_child_exists(state.private, stage.name)
        backup_exists = self._pinned_child_exists(state.live_quarantine, backup.name)
        expected_backup_identity = self._publication_live_backup_identity(journal)
        current_backup_identity = (
            self._pinned_child_identity(state.live_quarantine, backup.name)
            if backup_exists
            else None
        )
        backup_matches = (
            expected_backup_identity is not None
            and current_backup_identity == expected_backup_identity
        )
        live_hash = self._skill_tree_hash_child(state.auto, target_slug) if live_exists else None
        stage_hash = (
            self._skill_tree_hash_child(state.private, stage.name) if stage_exists else None
        )
        backup_hash = (
            self._skill_tree_hash_child(state.live_quarantine, backup.name)
            if backup_matches
            else None
        )

        if kind == "new":
            if live_hash == after_hash and not backup_exists:
                return True
            if not live_exists and stage_exists and stage_hash == after_hash and not backup_exists:
                return False
            return None
        if not isinstance(before_hash, str) or expected_backup_identity is None:
            return None
        if live_hash == after_hash:
            return True if backup_matches else None

        if live_hash == before_hash:
            if stage_exists and stage_hash != after_hash:
                return None
            if backup_exists:
                return None
            return False

        if (
            backup_matches
            and backup_hash == before_hash
            and stage_exists
            and stage_hash == after_hash
            and not live_exists
        ):
            try:
                self._rename_skill_child_no_replace(
                    state.live_quarantine,
                    backup.name,
                    state.auto,
                    target_slug,
                    expected_identity=expected_backup_identity,
                )
                self._sync_pinned_rename_parents(state.live_quarantine, state.auto)
            except OSError:
                return None
            return (
                False
                if (
                    self._pinned_child_identity(state.auto, target_slug) == expected_backup_identity
                    and self._skill_tree_hash_child(state.auto, target_slug) == before_hash
                )
                else None
            )
        return None

    def _restore_failed_promotion_claim(
        self,
        claim: Path,
        claim_fd: int,
        slug: str,
        claim_snapshot: _ClaimSnapshot,
    ) -> bool:
        """Restore a proven rollback and report whether its lock may be removed."""
        quarantine = self._quarantine_root() / claim.name
        if not (
            os.path.lexists(claim)
            or os.path.lexists(quarantine)
            or is_link_or_junction(claim)
            or is_link_or_junction(quarantine)
        ):
            return True
        lock_path = self._claim_lock_path(claim.name)
        journal = self._authenticated_claim_publication(claim_fd, lock_path, claim.name)
        if journal is None:
            if not self._cleanup_publication_artifacts(claim.name):
                logger.error(
                    "Could not clean failed skill publication artifacts; retaining %s",
                    claim,
                )
                return False
            self._restore_claimed_update(claim, claim_fd, slug, claim_snapshot)
            return (
                self._authenticated_claim_evidence_state(
                    claim_fd,
                    lock_path,
                    claim.name,
                )
                is None
                and not os.path.lexists(claim)
                and not os.path.lexists(quarantine)
            )
        published = self._reconcile_prepared_claim(claim, claim_fd, lock_path)
        if published is False:
            if not self._cleanup_publication_artifacts(claim.name):
                logger.error(
                    "Prepared skill publication rolled back but artifact cleanup failed; "
                    "retaining %s",
                    claim,
                )
                return False
            if self._completion_marker_present(claim):
                if not self._remove_untrusted_completion_marker(claim):
                    return False
            self._restore_claimed_update(claim, claim_fd, slug, claim_snapshot)
            return (
                self._authenticated_claim_evidence_state(
                    claim_fd,
                    lock_path,
                    claim.name,
                )
                is None
                and not os.path.lexists(claim)
                and not os.path.lexists(quarantine)
            )
        if published is None:
            logger.error(
                "Prepared skill claim has ambiguous publication state; retaining %s",
                claim,
            )
            return False
        logger.info(
            "Prepared skill generation is fully live; retaining %s for restart commit",
            claim,
        )
        return False

    def _recover_recorded_claim_restore(
        self,
        claim: Path,
        claim_fd: int,
        restore_state: tuple[str, _TaggedFileIdentity],
    ) -> bool:
        """Restore to the exact journaled pending slot or remain unresolved."""
        restore_slug, restore_identity = restore_state
        lock_state: list[_PinnedPrivateState] = []
        restored = False
        with self._file_lock("pending.lock", state_out=lock_state) as acquired:
            if not acquired or not lock_state:
                return False
            try:
                private_state = lock_state[0]
                pending_parent = private_state.pending
                if not self._pinned_parent_matches(pending_parent):
                    return False
                try:
                    current = self._stat_pinned_child(
                        private_state.quarantine,
                        claim.name,
                    )
                except FileNotFoundError:
                    pending_info = self._stat_pinned_child(
                        pending_parent,
                        restore_slug,
                    )
                    restored = (
                        stat.S_ISDIR(pending_info.st_mode)
                        and self._pinned_child_identity(pending_parent, restore_slug)
                        == restore_identity
                    )
                else:
                    current_identity = self._pinned_child_identity(
                        private_state.quarantine,
                        claim.name,
                    )
                    if not stat.S_ISDIR(current.st_mode) or current_identity != restore_identity:
                        return False
                    try:
                        self._stat_pinned_child(pending_parent, restore_slug)
                    except FileNotFoundError:
                        pass
                    else:
                        return False
                    self._rename_skill_child_no_replace(
                        private_state.quarantine,
                        claim.name,
                        pending_parent,
                        restore_slug,
                        expected_identity=restore_identity,
                    )
                    self._sync_pinned_rename_parents(
                        private_state.quarantine,
                        pending_parent,
                    )
                    pending_info = self._stat_pinned_child(
                        pending_parent,
                        restore_slug,
                    )
                    restored = (
                        stat.S_ISDIR(pending_info.st_mode)
                        and self._pinned_child_identity(pending_parent, restore_slug)
                        == restore_identity
                    )
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                return False
        return restored and self._discard_trusted_claim(claim)

    def _recover_abandoned_claims(self, *, roots_authenticated: bool = False) -> None:
        """Restore or retire claims whose owning process exited mid-transaction."""
        del roots_authenticated  # Each recovery pass acquires its own mutation authority.
        try:
            self._preflight_private_state(require_sensitive=True)
        except (OSError, RuntimeError, ValueError):
            return
        if not os.path.lexists(self._private_root()):
            return
        try:
            private_state = self._pin_private_state(
                create=True,
                require_sensitive=True,
            )
            state = private_state.__enter__()
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return
        root = self._claims_root()
        try:
            claim_names: set[str] = set()
            orphan_probes: list[str] = []
            consumed = 0

            def retain_active_names(
                parent: _PinnedSkillParent,
                *,
                label: str,
                retire_against_evidence: bool,
            ) -> None:
                nonlocal consumed
                if not _DIR_FD_SUPPORTED and not self._pinned_parent_matches(parent):
                    raise OSError(f"{label} parent changed during recovery inspection")
                target: int | Path = parent.fd if _DIR_FD_SUPPORTED else parent.path
                with os.scandir(target) as entries:
                    for entry in entries:
                        claim_name = entry.name
                        if retire_against_evidence and claim_name not in claim_names:
                            try:
                                self._stat_pinned_child(state.evidence, claim_name)
                            except FileNotFoundError:
                                pass
                            else:
                                # Retention moves private evidence last. A retired
                                # public name is history that only grows, so it is
                                # excluded before it counts toward the bound on the
                                # active namespace.
                                continue
                        if consumed >= _ACTIVE_CLAIM_SCAN_LIMIT:
                            raise _ActiveClaimScanOverflow(
                                "active claim namespace exceeded the "
                                f"{_ACTIVE_CLAIM_SCAN_LIMIT}-entry scan limit while reading "
                                f"{label}"
                            )
                        consumed += 1
                        if parent is state.claims and claim_name.startswith(_RENAME_PROBE_PREFIX):
                            # A probe's scratch file is never a claim. Collect a
                            # bounded number for removal once it is provably old.
                            if len(orphan_probes) < _ORPHANED_RENAME_PROBE_CLEANUP_LIMIT:
                                orphan_probes.append(claim_name)
                            continue
                        claim_names.add(claim_name)

            def remove_orphaned_probes() -> None:
                """Unlink rename probes a crashed process left under ``claims/``.

                A probe younger than ``_ORPHANED_RENAME_PROBE_MIN_AGE_S`` may belong
                to a claim in progress in another process and is left alone; the
                identity captured here must still hold at the unlink.
                """
                now = time.time()
                for probe_name in orphan_probes:
                    try:
                        info = self._stat_pinned_child(state.claims, probe_name)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or now - info.st_mtime < _ORPHANED_RENAME_PROBE_MIN_AGE_S
                        ):
                            continue
                        self._unlink_skill_child(
                            state.claims,
                            probe_name,
                            expected=info,
                            expected_identity=self._pinned_child_identity(
                                state.claims,
                                probe_name,
                            ),
                        )
                    except OSError:
                        logger.debug("Could not remove orphaned rename probe", exc_info=True)

            try:
                retain_active_names(
                    state.claims,
                    label="active claims",
                    retire_against_evidence=False,
                )
                retain_active_names(
                    state.quarantine,
                    label="public candidate quarantine",
                    retire_against_evidence=True,
                )
                retain_active_names(
                    state.live_quarantine,
                    label="public live quarantine",
                    retire_against_evidence=True,
                )
            except _ActiveClaimScanOverflow as exc:
                logger.warning("Could not recover abandoned skill claims: %s", exc)
                return
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "Could not recover abandoned skill claims: active claim namespace "
                    "inspection was indeterminate",
                    exc_info=True,
                )
                return
            finally:
                remove_orphaned_probes()
            for claim_name in sorted(claim_names):
                try:
                    claim_info = self._stat_pinned_child(state.claims, claim_name)
                except FileNotFoundError:
                    claim_info = None
                except OSError:
                    continue
                try:
                    quarantine_info = self._stat_pinned_child(
                        state.quarantine,
                        claim_name,
                    )
                except FileNotFoundError:
                    quarantine_info = None
                except OSError:
                    continue
                try:
                    live_quarantine_info = self._stat_pinned_child(
                        state.live_quarantine,
                        claim_name,
                    )
                except FileNotFoundError:
                    live_quarantine_info = None
                except OSError:
                    continue
                if claim_info is None and quarantine_info is None and live_quarantine_info is None:
                    continue
                claim = root / claim_name
                claim_linked = claim_info is None or (
                    stat.S_ISLNK(claim_info.st_mode)
                    or bool(getattr(claim_info, "st_reparse_tag", 0))
                    or not stat.S_ISDIR(claim_info.st_mode)
                )
                quarantine_linked = quarantine_info is not None and (
                    stat.S_ISLNK(quarantine_info.st_mode)
                    or bool(getattr(quarantine_info, "st_reparse_tag", 0))
                )
                if quarantine_info is not None and (
                    not quarantine_linked and not stat.S_ISDIR(quarantine_info.st_mode)
                ):
                    continue
                if "--" not in claim_name:
                    continue
                slug, _token = claim_name.rsplit("--", 1)
                if not self._is_pending_slug_safe(slug):
                    continue
                lock_path = self._claim_lock_path(claim_name)
                try:
                    lock_info = self._stat_pinned_child(
                        state.claim_locks,
                        lock_path.name,
                    )
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                        continue
                    fd = self._open_skill_lock(
                        state.claim_locks,
                        lock_path.name,
                    )
                except OSError:
                    continue
                acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
                cleanup_claim_lock = False
                try:
                    if not acquired:
                        continue
                    if (
                        self._authenticated_claim_evidence_state(
                            fd,
                            lock_path,
                            claim_name,
                        )
                        is not None
                    ):
                        if not self._retain_claim_evidence(claim, fd):
                            logger.error("Could not retain recovered claim evidence %s", claim)
                        continue
                    restore_state = self._authenticated_claim_restore_state(
                        fd,
                        lock_path,
                        claim_name,
                    )
                    if restore_state is not None:
                        restored = self._recover_recorded_claim_restore(
                            claim,
                            fd,
                            restore_state,
                        )
                        if not restored:
                            logger.error("Could not complete recorded claim restore %s", claim)
                        cleanup_claim_lock = (
                            restored
                            and not os.path.lexists(claim)
                            and not os.path.lexists(self._quarantine_root() / claim_name)
                        )
                        continue
                    publication = (
                        None
                        if claim_linked
                        else self._authenticated_claim_publication(fd, lock_path, claim_name)
                    )
                    claim_snapshot = self._authenticated_claim_snapshot_state(
                        fd,
                        lock_path,
                        claim_name,
                    )
                    if claim_snapshot is None and publication is not None:
                        claim_snapshot = self._claim_snapshot_from_fields(publication)
                    if claim_snapshot is None:
                        claim_snapshot = _ClaimSnapshot(None, None)
                    retained_update = (
                        publication is not None
                        and publication.get("format") == 2
                        and publication.get("kind") == "update"
                    )
                    lock_completed = self._authenticated_claim_lock_state(
                        fd, lock_path, claim_name, completed=True
                    )
                    marker_completed = not claim_linked and self._authenticated_completion_marker(
                        claim
                    )
                    if not claim_linked and (lock_completed or marker_completed):
                        retained = (
                            self._retain_published_update_evidence(claim, fd)
                            if retained_update
                            else self._retain_claim_evidence(claim, fd)
                        )
                        if not retained:
                            logger.error(
                                "Could not retain completed claim evidence %s",
                                claim,
                            )
                        continue

                    prepared = publication is not None
                    published = (
                        self._reconcile_prepared_claim(
                            claim,
                            fd,
                            lock_path,
                            private_state=state,
                        )
                        if prepared
                        else False
                    )
                    if published is True:
                        retained = (
                            self._retain_published_update_evidence(claim, fd)
                            if retained_update
                            else self._retain_claim_evidence(claim, fd)
                        )
                        if not retained:
                            logger.error(
                                "Could not retain recovered publication evidence %s",
                                claim,
                            )
                        continue
                    if published is None:
                        logger.error(
                            "Prepared skill claim has ambiguous publication state; retaining %s",
                            claim,
                        )
                        continue
                    if not self._cleanup_publication_artifacts(claim_name):
                        logger.error(
                            "Could not clean rolled-back publication artifacts; retaining %s",
                            claim,
                        )
                        continue
                    if not claim_linked and self._completion_marker_present(claim):
                        if not self._remove_untrusted_completion_marker(claim):
                            continue
                    self._restore_claimed_update(claim, fd, slug, claim_snapshot)
                    cleanup_claim_lock = (
                        self._authenticated_claim_evidence_state(
                            fd,
                            lock_path,
                            claim_name,
                        )
                        is None
                        and not os.path.lexists(claim)
                        and not os.path.lexists(self._quarantine_root() / claim_name)
                    )
                finally:
                    if acquired:
                        platform_compat.release_lock(fd)
                    os.close(fd)
                    if acquired and cleanup_claim_lock:
                        self._cleanup_claim_lock(claim_name)
        finally:
            private_state.__exit__(None, None, None)

    def list_pending_skills(self) -> list[dict]:
        """Return ``{slug, name, description, triggers, has_scripts, created_at, path}``
        for every staged candidate."""
        # A claim whose owning process died mid-transaction is restored or
        # retired before the queue is read, so the list never omits a candidate
        # that only an interrupted promotion is holding.
        self._recover_abandoned_claims()
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
                "name": f"{AUTO_SKILL_NAMESPACE}/{child.name}",
                "description": meta.get("description", ""),
                "triggers": meta.get("triggers", ""),
                "has_scripts": meta.get("has_scripts") is True,
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
    def _candidate_has_unsafe_inode(pdir: Path) -> bool:
        """True unless a tree contains only real dirs and lone regular files.

        Renaming a candidate directory does not sever a hardlink to one of its
        files. Reject every file whose inode has another name so no public alias
        can mutate claimed bytes after review. Traversal and stat errors fail
        closed because an uninspected entry is not safe to promote.
        """
        try:
            if is_link_or_junction(pdir):
                return True
            if not stat.S_ISDIR(os.lstat(pdir).st_mode):
                return True

            def raise_walk_error(error: OSError) -> None:
                raise error

            for root, dirs, files in os.walk(
                pdir,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                for nm in dirs:
                    entry = Path(root) / nm
                    if is_link_or_junction(entry):
                        return True
                    if not stat.S_ISDIR(os.lstat(entry).st_mode):
                        return True
                for nm in files:
                    entry = Path(root) / nm
                    if is_link_or_junction(entry):
                        return True
                    entry_stat = os.lstat(entry)
                    if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
                        return True
        except OSError:
            return True
        return False

    @staticmethod
    def _candidate_has_symlink(pdir: Path) -> bool:
        """True if the candidate dir itself or any entry under it is a link —
        so the read/approve paths never follow an LLM-planted link to a
        sensitive file. (Scripts always require human review before going live;
        this is defense-in-depth, not the primary control.)

        "Link" is :func:`platform_compat.is_link_or_junction`, not
        ``os.path.islink``: a Windows directory junction is a reparse point
        ``islink`` reports as a plain directory, so the fence answered False for
        one AND ``os.walk`` descended through it -- the read path then reached
        whatever it pointed at, which is the exact escape this refuses.
        Directories are tested before files because a topdown walk offers a
        directory in ``dirs`` before descending into it, so answering there is
        what keeps this from ever walking THROUGH a link to reach its verdict.
        """
        if is_link_or_junction(pdir):
            return True
        for root, dirs, files in os.walk(pdir):
            for nm in list(dirs):
                if is_link_or_junction(os.path.join(root, nm)):
                    return True
            for nm in files:
                if is_link_or_junction(os.path.join(root, nm)):
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

    def _collect_scripts_pinned(
        self,
        pinned: PinnedDirectory,
        rel: tuple[str, ...],
        out: list[dict],
        budget: dict[str, int],
    ) -> bool:
        """Collect ``{filename, content}`` under *pinned*, refusing a link anywhere.

        Returns False when the tree is not readable as plain files and directories, or
        when it does not fit the budget, which the caller turns into a refusal of the
        whole candidate. Relative filenames carry the platform separator, because they
        are served through the API.

        Every bound here is the verdict walk's bound, deliberately: the SAME
        ``_PENDING_SCRIPT_MAX_ENTRIES`` entries and the same ``MAX_SCRIPT_BYTES`` per
        file. Those two ARE the bound on what this accumulates -- at most
        ``entries * per-file`` bytes -- so there is deliberately no third, aggregate
        check: with both of the above in force it could never fire, and a guard that
        cannot fire reads as protection while providing none. This runs per request
        against a tree an agent writes directly, so reusing the verdict's numbers is
        also what keeps the two from disagreeing about which candidates are readable.
        *budget* is threaded through the recursion rather than recreated per directory,
        or a planted tree of many small directories would each get a fresh allowance and
        the total would be unbounded again. Names are enumerated LAZILY against the
        entry cap for the same reason the verdict walk does it: an eager
        ``sorted(names())`` spends the allocation before any budget can refuse it.

        Depth is bounded by ``_PENDING_SCRIPT_MAX_DEPTH``, the same cap the verdict
        walk uses, and bounded the SAME WAY: that walk refuses to descend when the
        directory it is standing in is already at the cap, so it enumerates the level
        AT the cap. The comparison here is ``>`` rather than ``>=`` for exactly that
        reason -- one level tighter would refuse a tree the verdict judged fine, and
        the detail read would answer 404 for a candidate that is waiting for review. The tree is written directly by an agent, so a nesting chain deep
        enough to exhaust the interpreter's recursion limit is free to produce, and
        the resulting ``RecursionError`` would surface as a failed request rather
        than a refused candidate. Refusing at the cap also keeps the two traversals
        in step: a tree the verdict declines to judge is not one this still reads.
        """
        if len(rel) > _PENDING_SCRIPT_MAX_DEPTH:
            return False
        listed = pinned.names_bounded(_PENDING_SCRIPT_MAX_ENTRIES)
        if listed is None:
            return False
        for name in sorted(listed):
            if pinned.is_link(name):
                return False
            budget["entries"] += 1
            if budget["entries"] > _PENDING_SCRIPT_MAX_ENTRIES:
                # Directories spend the entry budget too, so a planted tree of many
                # nearly-empty ones cannot buy traversal the files could not.
                return False
            if pinned.is_dir(name):
                sub = pinned.child_if_real_dir(name)
                if sub is None:
                    # The open refusing IS the protection: what is at the name is not
                    # the real directory it screened as. Refuse the candidate.
                    return False
                with sub:
                    if not self._collect_scripts_pinned(sub, (*rel, name), out, budget):
                        return False
                continue
            try:
                text = pinned.read_text(name, max_bytes=MAX_SCRIPT_BYTES)
            except (OSError, UnicodeDecodeError):
                # ONE refusal policy, so nothing here reasons about error classes.
                # An entry that cannot be read as text is not shown to the reviewer,
                # and this API exists for INFORMED approval -- silently omitting it
                # would let a clean-looking detail stand for a candidate that approve
                # then refuses, or worse, promotes unreviewed. A hardlink, a
                # non-regular entry and one over the per-file cap arrive here too,
                # each refused by the read itself.
                return False
            out.append(
                {
                    "filename": os.path.join(*rel, name) if rel else name,
                    "content": text,
                }
            )
        return True

    def _read_candidate_pinned(self, pdir: Path) -> tuple[str, dict, list[dict]] | None:
        """Read a candidate's ``SKILL.md``, ``.meta.json`` and scripts through ONE pin chain.

        The traversal both judges and reads. The screen-then-read shape this replaces
        refused a link that was PRESENT at the check, so a candidate tree the LLM can
        write needed only to have the link absent at the screen and present by the
        ``read_text`` a few statements later, and the contents of a file of its
        choosing were served -- the redaction passes cover credential and exfil-URL
        shapes only, so anything else came back intact. Here every open refuses a link
        at the name itself, so there is no gap between the judgement and the read.

        Returns None when the candidate cannot be read as a plain tree: a link
        anywhere, a non-regular entry, or a directory replaced underneath. "Anywhere"
        is the whole top level, not only the names this reads: the approve path
        refuses an unexpected top-level entry outright, so a detail read that served
        one would be MORE permissive than the approve it exists to inform.

        Two byte caps, one per kind of file. ``SKILL.md`` and ``.meta.json`` are
        documents and read under ``_PENDING_DOCUMENT_MAX_BYTES``, derived from what
        the generator may write; every file under ``scripts/`` is an executable and
        read under ``MAX_SCRIPT_BYTES``, the verdict walk's own per-file cap. Either
        cap refuses the candidate whole: the read is bounded, never partial.
        """
        try:
            pinned = pinned_directory(pdir)
        except OSError:
            return None
        with pinned:
            # Screen the whole top level BEFORE a byte is read, so the refusal this
            # docstring promises is structural rather than a side effect of which
            # names happen to be read. `_collect_scripts_pinned` refuses a link
            # anywhere under `scripts/`; this covers every OTHER top-level name --
            # `.meta.json`, and an unexpected entry the approve path refuses outright,
            # which nothing on this path opens and so nothing else would check. It
            # does not REPLACE the per-name refusals below: it cannot see a swap that
            # happens after it, which is what those catch.
            #
            # Enumeration is BOUNDED by the same entry budget the verdict walk spends,
            # because this runs per request against a tree an agent writes directly: a
            # planted crowd of names would otherwise be materialized in one allocation
            # here, before any later check could refuse it. Over budget refuses the
            # candidate rather than serving a partial view of its directory.
            listed = pinned.names_bounded(_PENDING_SCRIPT_MAX_ENTRIES)
            if listed is None:
                return None
            names = set(listed)
            if any(pinned.is_link(name) for name in names):
                return None
            try:
                # The DOCUMENT cap, not the script cap: a generated procedure may run
                # to AUTO_SKILL_MAX_PROCEDURE_CHARS characters, several times what a
                # bundled script may hold, and a valid candidate must open for review.
                body = pinned.read_text("SKILL.md", max_bytes=_PENDING_DOCUMENT_MAX_BYTES)
            except (OSError, UnicodeDecodeError):
                return None
            meta: dict = {}
            if ".meta.json" in names:
                try:
                    raw = pinned.read_text(".meta.json", max_bytes=_PENDING_DOCUMENT_MAX_BYTES)
                except (OSError, UnicodeDecodeError):
                    # Unreadable THROUGH THE PIN is a fence signal, not bad content:
                    # the name is not the plain file it screened as, which is the same
                    # class as SKILL.md failing above, and over the document cap is
                    # refused the same way. Refuse the candidate rather than serve it
                    # with empty metadata.
                    return None
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    # Malformed JSON is the agent writing nonsense, not a swap. The
                    # reviewer still sees SKILL.md and the scripts.
                    parsed = None
                if isinstance(parsed, dict):
                    # Recursively redact secrets from LLM-produced metadata before it
                    # can surface via the pending detail API: the crystallize skill
                    # writes ``.meta.json`` directly, bypassing the consolidation
                    # redaction path, so a credential in ANY nested value is scrubbed.
                    scrubbed = self._redact_deep(parsed)
                    meta = scrubbed if isinstance(scrubbed, dict) else {}
            scripts: list[dict] = []
            if "scripts" in names:
                if pinned.is_link("scripts"):
                    # NOT redundant with the screen above, and the difference is
                    # timing: that screen refuses a link PRESENT when the traversal
                    # started, this one refuses a link swapped in while the reads
                    # above were running. A tree written by an agent can change
                    # between the two.
                    return None
                if pinned.is_dir("scripts"):
                    sub = pinned.child_if_real_dir("scripts")
                    if sub is None:
                        return None
                    # ONE budget for the whole subtree, spent the way the verdict walk
                    # spends it: entries and aggregate bytes, same constants. Per-call
                    # rather than per-directory, or a planted tree of many small
                    # directories would each get a fresh allowance.
                    budget = {"entries": 0}
                    with sub:
                        if not self._collect_scripts_pinned(sub, (), scripts, budget):
                            return None
            return body, meta, scripts

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
        max_depth = _PENDING_SCRIPT_MAX_DEPTH
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

    def pending_candidate_is_staged(self, slug: str) -> bool:
        """Whether a candidate is still staged at *slug*, for CHOOSING A MESSAGE.

        Contract and rationale:
        ``skill_runtime.auto_skills.pending_candidate_is_staged``.
        """
        return _auto_skills.pending_candidate_is_staged(self, slug)

    def get_pending_skill(self, slug: str) -> dict | None:
        """Return full pending-candidate detail incl. SKILL.md body + script bodies."""
        if not self._is_pending_slug_safe(slug):
            return None
        pdir = self._pending_root() / slug
        if not (pdir / "SKILL.md").exists():
            # The ordinary "no such candidate" answer. Not a security check -- the
            # pinned read below is -- so probing by name here costs nothing.
            return None
        # ONE descriptor-pinned traversal both validates and reads: the body, the
        # metadata and every script come back from opens that refuse a link AT THE
        # NAME, so there is no screen-then-read gap for a candidate to flip a name
        # through and no way to point the detail API at a file outside the candidate.
        read = self._read_candidate_pinned(pdir)
        if read is None:
            logger.warning(
                "Refusing to read pending %s: candidate is not a plain tree of files", slug
            )
            return None
        body, meta, scripts = read
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
            # Display-only newline folding: stored bytes, generation hashes and
            # promotion authority keep the candidate's exact spelling.
            content = str(s.get("content", "")).replace("\r\n", "\n").replace("\r", "\n")
            s["content"] = self._redact_text(content)
        detail = {
            "slug": slug,
            # The directory name is authoritative: a refusal restore can land a
            # candidate under a sibling slot without rewriting its metadata.
            "name": f"{AUTO_SKILL_NAMESPACE}/{slug}",
            "meta": meta,
            "kind": meta.get("kind", "new"),
            "target": meta.get("target"),
            "base_version": meta.get("base_version"),
            "content": self._redact_text(body),
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

        Rejects (a) any link, hardlinked/non-regular file, or unstatable entry
        anywhere in the candidate (promotion + chmod must touch only stable,
        private inodes), and (b) any unexpected top-level entry: only ``SKILL.md``,
        ``.meta.json`` and a ``scripts`` DIRECTORY are allowed. An injected
        auxiliary file (dropped outside the validated set) would ride live
        WITHOUT validation or redaction; a regular file named ``scripts`` would
        skip the directory-only script validation + redaction walk. Returns True
        only when the layout is safe to promote.
        """
        if self._candidate_has_unsafe_inode(src):
            logger.warning("Refusing to approve %s: candidate tree is unsafe", name)
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

    def _validate_and_redact_candidate(
        self,
        src: Path,
        name: str,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> _ValidatedCandidateSnapshot | None:
        """Validate and redact one descriptor-authenticated candidate generation.

        Never mutates the candidate: redaction is applied to the captured
        snapshot, so a refused candidate stays byte-identical to what was staged.
        ``refusal`` receives the reason (and a script report) for a refusal.
        """
        if not self._candidate_layout_ok(src, name):
            _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
            return None
        tree = self._skill_tree_snapshot(src)
        if tree is None:
            logger.warning("Refusing to approve %s: candidate snapshot is unreadable", name)
            _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
            return None
        return self._validate_and_redact_snapshot(tree, name, refusal=refusal)

    def _validate_and_redact_snapshot(
        self,
        tree: _SkillTreeSnapshot,
        name: str,
        *,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> _ValidatedCandidateSnapshot | None:
        """Validate and redact one already-captured immutable candidate tree."""
        allowed_files = {Path("SKILL.md"), Path(".meta.json")}
        if any(
            relative != Path(".") and (not relative.parts or relative.parts[0] != "scripts")
            for relative in tree.dir_modes
        ) or any(
            (len(relative.parts) == 1 and relative not in allowed_files)
            or (len(relative.parts) > 1 and relative.parts[0] != "scripts")
            for relative in tree.files
        ):
            logger.warning("Refusing to approve %s: unexpected candidate entry", name)
            _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
            return None
        source_files = tree.files
        redacted_files: dict[Path, bytes] = {}
        metadata: dict[str, object] = {}
        try:
            for relative, raw in source_files.items():
                if relative == Path(".meta.json"):
                    try:
                        parsed = json.loads(raw)
                    except (TypeError, ValueError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        redacted_meta = self._redact_deep(parsed)
                        if isinstance(redacted_meta, dict):
                            metadata = redacted_meta
                    continue
                redacted_files[relative] = self._redact_text(raw.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Refusing to approve %s: candidate snapshot is unreadable", name)
            _note_refusal(refusal, PendingApprovalRefused("redaction_failed"))
            return None

        skill_path = Path("SKILL.md")
        if skill_path not in redacted_files:
            _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
            return None

        def _scripts(files: dict[Path, bytes]) -> list[dict[str, str]]:
            scripts: list[dict[str, str]] = []
            for relative, payload in sorted(files.items(), key=lambda item: str(item[0])):
                if not relative.parts or relative.parts[0] != "scripts":
                    continue
                scripts.append(
                    {
                        "filename": str(relative.relative_to("scripts")),
                        "content": payload.decode("utf-8"),
                    }
                )
            return scripts

        before_scripts = _scripts(source_files)
        if before_scripts:
            ok, report = validate_scripts(before_scripts)
            if not ok:
                logger.warning("Refusing to approve %s: script validation failed: %s", name, report)
                _note_refusal(
                    refusal,
                    PendingApprovalRefused(
                        "script_validation_failed", report=self._redact_validation_report(report)
                    ),
                )
                return None
        after_scripts = _scripts(redacted_files)
        if after_scripts:
            ok, report = validate_scripts(after_scripts)
            if not ok:
                logger.warning(
                    "Refusing to approve %s: scripts invalid after redaction: %s",
                    name,
                    report,
                )
                _note_refusal(
                    refusal,
                    PendingApprovalRefused(
                        "script_validation_failed", report=self._redact_validation_report(report)
                    ),
                )
                return None
        return _ValidatedCandidateSnapshot(
            source_files=source_files,
            files=redacted_files,
            modes=tree.file_modes,
            metadata=metadata,
            generation_hash=tree.generation_hash,
        )

    @staticmethod
    def _materialize_candidate_snapshot(
        snapshot: _ValidatedCandidateSnapshot, destination: Path
    ) -> None:
        """Write an immutable candidate snapshot into a fresh private directory."""
        destination.mkdir(parents=True, exist_ok=False)
        for relative, payload in sorted(snapshot.files.items(), key=lambda item: str(item[0])):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = snapshot.modes.get(relative, 0o600)
            if relative.parts and relative.parts[0] == "scripts":
                mode |= 0o111
            atomic_write(target, payload, mode=mode, fsync=True)
            platform_compat.chmod_safe(target, mode)

    @staticmethod
    def _materialize_skill_tree_snapshot(snapshot: _SkillTreeSnapshot, destination: Path) -> None:
        """Materialize raw live bytes and modes without reopening the live tree."""
        root_mode = snapshot.dir_modes.get(Path("."), 0o700)
        destination.mkdir(mode=root_mode, parents=False, exist_ok=False)
        platform_compat.chmod_safe(destination, root_mode)
        for relative, mode in sorted(
            snapshot.dir_modes.items(), key=lambda item: (len(item[0].parts), str(item[0]))
        ):
            if relative == Path("."):
                continue
            target = destination / relative
            target.mkdir(mode=mode, parents=False, exist_ok=False)
            platform_compat.chmod_safe(target, mode)
        for relative, payload in sorted(snapshot.files.items(), key=lambda item: str(item[0])):
            target = destination / relative
            mode = snapshot.file_modes.get(relative, 0o600)
            atomic_write(target, payload, mode=mode, fsync=True)
            platform_compat.chmod_safe(target, mode)

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
        """Return the bare slug for an auto-skill *name*, accepting either ``auto/<slug>`` or a bare ``<slug>``. Non-auto namespaces (any name with a slash after stripping the ``auto/`` prefix) fall through and are caught by the ``_is_pending_slug_safe`` guard at the call sites."""
        return _versions._auto_slug_from_name(name)

    def get_auto_skill_version(self, name: str) -> int:
        """Return the ``version`` frontmatter of a live auto-skill (default 1).

        Contract and rationale: ``skill_runtime.versions.get_auto_skill_version``.
        """
        return _versions.get_auto_skill_version(self, name)

    def read_auto_skill_body(self, name: str) -> str | None:
        """Return the full live ``SKILL.md`` text for an auto-skill, or ``None``.

        Contract and rationale: ``skill_runtime.versions.read_auto_skill_body``.
        """
        return _versions.read_auto_skill_body(self, name)

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
        """Rebuild an update candidate's body as the new live SKILL.md."""
        return _versions._rewrite_update_frontmatter(
            candidate_content,
            target_name=target_name,
            created_at=created_at,
            version=version,
            pinned=pinned,
            pointer_only=pointer_only,
        )

    def _versions_root(self, target_slug: str) -> Path:
        return _versions._versions_root(self, target_slug)

    def _prune_versions(self, versions_dir: Path) -> None:
        """Keep only the newest ``MAX_SKILL_VERSIONS`` ``v<N>-SKILL.md`` snapshots in *versions_dir*, deleting the lowest-numbered excess."""
        return _versions._prune_versions(self, versions_dir)

    def preview_pending_update(self, slug: str) -> dict | None:
        """Return an approval preview for a pending UPDATE candidate.

        Produces ``{live_body, proposed_body, diff, from_version, to_version,
        base_version, stale_base}`` where ``proposed_body`` is the EXACT content
        ``approve_pending_update`` would write (same frontmatter rewrite), so the
        reviewer's diff is what approval actually does — not raw candidate text
        whose ``name`` / ``created_at`` / ``version`` lines are rewritten anyway.

        Returns ``None`` when the slug is unsafe, the candidate is missing or is
        not an update, or its target is not a live auto-skill whose metadata
        can be read (a refused rewrite read included). Read-only:
        never mutates the candidate or the live skill.
        """
        if not self._is_pending_slug_safe(slug):
            return None
        src = self._pending_root() / slug
        # One snapshot supplies body and metadata, so a retained writer cannot
        # make the preview describe two different candidate generations.
        candidate_tree = self._skill_tree_snapshot(src)
        if candidate_tree is None:
            return None
        meta = self._candidate_metadata_from_bytes(
            candidate_tree.files.get(Path(".meta.json")),
            redact=True,
        )
        if meta.get("kind") != "update":
            return None
        try:
            cand_body = candidate_tree.files[Path("SKILL.md")].decode("utf-8")
        except (KeyError, UnicodeDecodeError):
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
            current_version = self.get_auto_skill_version(target_name)
            _live_fm = self._cached_frontmatter(live_file, within=None, for_write=True)
        except OSError:
            return None
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

    def _resolve_snapshot_version(
        self,
        versions_dir: Path,
        fm_version: int,
        live_snapshot: _SkillTreeSnapshot | None = None,
    ) -> int:
        """Return the version number to snapshot the CURRENT live body under.

        With ``live_snapshot`` the answer comes from that authenticated
        generation's own ``.versions`` entries, never from a re-read of the
        mutable live directory. Contract and rationale:
        ``skill_runtime.versions._resolve_snapshot_version``.
        """
        return _versions._resolve_snapshot_version(
            self, versions_dir, fm_version, live_snapshot=live_snapshot
        )

    def _promote_pending_update(
        self,
        slug: str,
        *,
        refuse_scripts: bool = False,
        expected_candidate_binding: str | None = None,
        claimed_out: list[bool] | None = None,
        committed_out: list[tuple[str, int]] | None = None,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> tuple[str, int] | None:
        """Claim and promote an update, returning its lock-authoritative version."""
        claimed = self._claim_pending_update(slug)
        if claimed is None:
            return None
        if claimed_out is not None:
            claimed_out[:] = [True]
        claim, claim_fd, consumed_at, claim_snapshot = claimed
        result: tuple[str, int] | None = None
        live_published = False
        try:
            if is_link_or_junction(claim):
                return None
            observed = self._validate_and_redact_candidate(claim, slug, refusal=refusal)
            if (
                observed is None
                or claim_snapshot.generation_hash is None
                or not secrets.compare_digest(
                    observed.generation_hash,
                    claim_snapshot.generation_hash,
                )
            ):
                logger.warning("Refusing promotion of %s: claimed generation changed", slug)
                return None
            if claim_snapshot.tree is None:
                logger.warning("Refusing promotion of %s: claim has no immutable authority", slug)
                return None
            snapshot = self._validate_and_redact_snapshot(
                claim_snapshot.tree, slug, refusal=refusal
            )
            if snapshot is None:
                logger.warning("Refusing promotion of %s: snapshot validation failed", slug)
                return None
            meta = snapshot.metadata
            target = meta.get("target")
            if meta.get("kind") != "update":
                _note_refusal(refusal, PendingApprovalRefused("not_found"))
                return None
            if not isinstance(target, str) or not target:
                _note_refusal(refusal, PendingApprovalRefused("target_missing"))
                return None
            target_slug = self._auto_slug_from_name(target)
            if not self._is_pending_slug_safe(target_slug):
                _note_refusal(refusal, PendingApprovalRefused("target_missing"))
                return None
            with self._promotion_lock(target_slug) as acquired:
                if not acquired:
                    logger.warning("Promotion lock unavailable for %s", target)
                    return None
                try:
                    with self._pin_private_state(
                        create=False,
                        require_sensitive=True,
                    ) as private_state:
                        result = self._approve_claimed_update_locked(
                            claim,
                            claim_fd=claim_fd,
                            slug=slug,
                            meta=meta,
                            snapshot=snapshot,
                            refuse_scripts=refuse_scripts,
                            expected_candidate_binding=expected_candidate_binding,
                            private_state=private_state,
                            refusal=refusal,
                        )
                        if result is None:
                            return None
                        name, _new_version = result
                        live_published = True
                        if committed_out is not None:
                            committed_out[:] = [result]
                        if not self._commit_claim_consumption(claim, claim_fd):
                            result = None
                            return None
                        if not self._pinned_parent_matches(
                            private_state.auto
                        ) or not self._pinned_parent_matches(private_state.private):
                            result = None
                            return None
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    logger.warning("Promotion authority became unavailable for %s", target)
                    return None
            if os.path.lexists(claim):
                logger.warning(
                    "Published update evidence remains active for restart retention: %s",
                    claim,
                )
            _emit_pending_consumed(
                {"slug": slug, "outcome": "approved", "name": name, "consumed_at": consumed_at}
            )
            return result
        finally:
            cleanup_claim_lock = True
            try:
                if result is None and not live_published:
                    cleanup_claim_lock = self._restore_failed_promotion_claim(
                        claim,
                        claim_fd,
                        slug,
                        claim_snapshot,
                    )
            except OSError:
                cleanup_claim_lock = False
                logger.error("Could not restore claimed update %s", claim, exc_info=True)
            finally:
                platform_compat.release_lock(claim_fd)
                os.close(claim_fd)
                if cleanup_claim_lock:
                    self._cleanup_claim_lock(claim.name)

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

    def _approve_claimed_update_locked(
        self,
        src: Path,
        *,
        claim_fd: int,
        slug: str,
        meta: dict[str, object],
        snapshot: _ValidatedCandidateSnapshot,
        refuse_scripts: bool,
        expected_candidate_binding: str | None,
        private_state: _PinnedPrivateState,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> tuple[str, int] | None:
        """Promote only the whole generation authenticated at the claim rename."""
        bound_raw = snapshot.source_files.get(Path("SKILL.md"))
        if expected_candidate_binding is not None:
            if bound_raw is None:
                logger.warning("Refusing unattended promotion of %s: body is unreadable", slug)
                return None
            try:
                actual_binding = self._auto_apply_candidate_binding(
                    bound_raw,
                    target=meta.get("target"),
                    base_version=meta.get("base_version"),
                    base_content_hash=meta.get("base_content_hash"),
                )
            except (OSError, TypeError, ValueError):
                logger.warning("Refusing unattended promotion of %s: binding is unreadable", slug)
                return None
            if not secrets.compare_digest(actual_binding, expected_candidate_binding):
                logger.warning("Refusing unattended promotion of %s: candidate changed", slug)
                return None
        snapshot_has_scripts = any(
            relative.parts and relative.parts[0] == "scripts" for relative in snapshot.files
        )
        if refuse_scripts and (meta.get("has_scripts") is True or snapshot_has_scripts):
            logger.info("Refusing unattended promotion of %s: scripts require review", slug)
            return None
        target = meta.get("target")
        if not isinstance(target, str) or not target:
            return None
        target_slug = self._auto_slug_from_name(target)
        live_dir = private_state.auto.path / target_slug
        live_skill = live_dir / "SKILL.md"
        if not self._pinned_child_exists(private_state.auto, target_slug):
            logger.warning(
                "Refusing to approve update %s: target %r is not a live auto skill", slug, target
            )
            _note_refusal(refusal, PendingApprovalRefused("target_missing"))
            return None
        target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
        self._fm_cache.pop(str(live_skill), None)
        live_snapshot = self._skill_tree_snapshot_child(private_state.auto, target_slug)
        if live_snapshot is None:
            logger.warning(
                "Refusing to approve update %s: live skill directory is unsafe",
                target_name,
            )
            _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
            return None
        try:
            live_prev = live_snapshot.files[Path("SKILL.md")].decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            logger.warning("Refusing to approve update %s: live body is unreadable", target_name)
            return None
        if expected_candidate_binding is not None:
            expected_hash = meta.get("base_content_hash")
            if not isinstance(expected_hash, str) or not expected_hash:
                logger.warning(
                    "Refusing unattended promotion of %s: live-content hash is missing",
                    target_name,
                )
                return None
            actual_hash = canonical_skill_text_hash(live_prev)
            if not secrets.compare_digest(expected_hash, actual_hash):
                logger.warning(
                    "Refusing unattended promotion of %s: live skill changed after staging",
                    target_name,
                )
                return None
        try:
            candidate_body = snapshot.files[Path("SKILL.md")].decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            return None
        before_hash = live_snapshot.generation_hash
        live_frontmatter = self._parse_frontmatter_text(live_prev)
        try:
            parsed_version = int(live_frontmatter.get("version", ""))
        except (TypeError, ValueError):
            parsed_version = 1
        current_version = parsed_version if parsed_version >= 1 else 1
        # Snapshot under a number that is guaranteed free, so an earlier snapshot
        # can never be destroyed by drifted numbering.
        versions_dir = self._versions_root(target_slug)
        snapshot_version = self._resolve_snapshot_version(
            versions_dir, current_version, live_snapshot
        )
        new_version = snapshot_version + 1
        # ``base_version`` records the live version the merge was computed
        # against. If the live skill advanced since staging, this candidate's body
        # was merged from an OLDER base, so writing it would replace whatever the
        # intervening approval added. REFUSE rather than warn.
        raw_base = meta.get("base_version")
        if isinstance(raw_base, int) and raw_base != current_version:
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
            _note_refusal(refusal, PendingApprovalRefused("stale_base"))
            return None
        live_created_at = live_frontmatter.get("created_at", "")
        live_pinned = str(live_frontmatter.get("pinned", "")).strip().lower() in (
            "true",
            "1",
            "yes",
        )
        live_pointer_only = (
            str(live_frontmatter.get("inject_on_trigger", "")).strip().lower() == "false"
        )
        new_live_content = self._rewrite_update_frontmatter(
            candidate_body,
            target_name=target_name,
            created_at=live_created_at,
            version=new_version,
            pinned=live_pinned,
            pointer_only=live_pointer_only,
        )

        stage, backup = self._publication_paths(src.name)
        if (
            stage.parent != private_state.private.path
            or backup.parent != private_state.live_quarantine.path
        ):
            return None
        if self._pinned_child_exists(
            private_state.private,
            stage.name,
        ) or self._pinned_child_exists(private_state.live_quarantine, backup.name):
            logger.error("Refusing to reuse skill publication artifacts for %s", target_name)
            return None
        script_modes = snapshot.modes
        script_items = [
            (relative, payload)
            for relative, payload in snapshot.files.items()
            if relative.parts and relative.parts[0] == "scripts"
        ]
        try:
            self._materialize_skill_tree_snapshot(live_snapshot, stage)
            staged_skill = stage / "SKILL.md"
            with staged_skill.open("wb") as handle:
                handle.write(new_live_content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())

            staged_versions = stage / VERSIONS_DIRNAME
            staged_versions.mkdir(parents=True, exist_ok=True)
            version_snapshot = staged_versions / f"v{snapshot_version}-SKILL.md"
            atomic_write(version_snapshot, live_prev.encode("utf-8"), fsync=True)

            if not refuse_scripts and script_items:
                staged_scripts = stage / "scripts"
                staged_scripts.mkdir(parents=True, exist_ok=True)
                for relative, payload in sorted(script_items, key=lambda item: str(item[0])):
                    script_relative = relative.relative_to("scripts")
                    destination = staged_scripts / script_relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    mode = script_modes.get(relative, 0o600) | 0o111
                    atomic_write(destination, payload, mode=mode, fsync=True)
                    platform_compat.chmod_safe(destination, mode)

            self._prune_versions(staged_versions)
            self._sync_skill_tree(stage)
            after_hash = self._skill_tree_hash_child(private_state.private, stage.name)
            if (
                after_hash is None
                or self._skill_tree_hash_child(private_state.auto, target_slug) != before_hash
            ):
                raise OSError("live or staged skill generation changed during preparation")
            if not self._prepare_claim_publication(
                claim_fd,
                self._claim_lock_path(src.name),
                src.name,
                kind="update",
                target_slug=target_slug,
                before_hash=before_hash,
                after_hash=after_hash,
                claim_snapshot=_ClaimSnapshot(
                    snapshot.generation_hash,
                    snapshot.source_files.get(Path(".meta.json")),
                ),
                live_backup_identity=live_snapshot.root_identity,
                snapshot_version=snapshot_version,
                new_version=new_version,
            ):
                raise OSError("claim journal failed")
            publication = self._publish_prepared_skill_tree(
                state=private_state,
                live_dir=live_dir,
                stage=stage,
                backup=backup,
                backup_identity=live_snapshot.root_identity,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            if publication != "published":
                if publication == "drift":
                    # The captured concurrent generation is live again and the
                    # prepared after-tree never committed. Return the journal to
                    # its active claim state so normal refusal recovery can put
                    # the candidate back in the public review queue.
                    prepared_state = self._authenticated_claim_publication(
                        claim_fd,
                        self._claim_lock_path(src.name),
                        src.name,
                    )
                    prepared_snapshot = (
                        self._claim_snapshot_from_fields(prepared_state)
                        if prepared_state is not None
                        else None
                    )
                    active_state = self._initialize_claim_lock_state(
                        claim_fd,
                        self._claim_lock_path(src.name),
                        src.name,
                    ) and self._write_claim_snapshot_state(
                        claim_fd,
                        self._claim_lock_path(src.name),
                        src.name,
                        _ClaimSnapshot(
                            snapshot.generation_hash,
                            snapshot.source_files.get(Path(".meta.json")),
                            quarantine_identity=(
                                prepared_snapshot.quarantine_identity
                                if prepared_snapshot is not None
                                else None
                            ),
                        ),
                    )
                    if active_state:
                        self._cleanup_publication_artifacts(src.name)
                    else:
                        logger.error(
                            "Retaining %s after live drift because claim rollback was not durable",
                            src,
                        )
                logger.warning(
                    "Refusing to approve update %s: whole-tree publication did not complete",
                    target_name,
                )
                return None
        except OSError:
            # Before the journal exists the stage is disposable. Once prepared,
            # the claim and generation artifacts are recovery state and must not
            # be guessed away here.
            if (
                self._authenticated_claim_publication(
                    claim_fd, self._claim_lock_path(src.name), src.name
                )
                is None
            ):
                self._cleanup_publication_artifacts(src.name)
            logger.warning(
                "Refusing to approve update %s: could not prepare whole skill generation",
                target_name,
            )
            return None
        # (i) Audit the approved update.
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
        self._invalidate_iter_cache()
        logger.info(
            "Approved pending update: %s (v%d -> v%d)", target_name, current_version, new_version
        )
        return target_name, new_version

    def auto_apply_pending_update(
        self,
        slug: str,
        *,
        expected_candidate_binding: str,
        recovery_pending_out: list[tuple[str, int]] | None = None,
    ) -> tuple[str, int] | None:
        """Promote a prose-only update when approval is disabled."""
        claimed: list[bool] = []
        committed: list[tuple[str, int]] = []
        applied = self._promote_pending_update(
            slug,
            refuse_scripts=True,
            expected_candidate_binding=expected_candidate_binding,
            claimed_out=claimed,
            committed_out=committed,
        )
        if applied is None:
            if committed:
                if recovery_pending_out is not None:
                    recovery_pending_out[:] = committed
                name, version = committed[0]
                _emit_update_auto_applied(
                    {
                        "name": name,
                        "slug": slug,
                        "target": name,
                        "new_version": version,
                        "description": "",
                        "recovery_pending": True,
                    }
                )
                return None
            if not claimed:
                # Auto-apply is the only caller that suppresses the initial
                # staged event. Capture the still-public generation once; a
                # claimed refusal already emitted from its immutable metadata.
                self.emit_pending_staged(slug)
            return None
        name, version = applied
        _emit_update_auto_applied(
            {
                "name": name,
                "slug": slug,
                "target": name,
                "new_version": version,
                # Deliberately no description: the only source would be the
                # PUBLIC pending metadata read before the claim, which an
                # attacker-writable sibling can rewrite so the notification
                # describes different bytes than were promoted. The name,
                # target and version above are computed from the claimed
                # snapshot under the target lock.
                "description": "",
            }
        )
        return applied

    def _approve_claimed_skill_locked(
        self,
        src: Path,
        claim_fd: int,
        slug: str,
        claim_snapshot: _ClaimSnapshot,
        private_state: _PinnedPrivateState,
        *,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> str | None:
        """Publish one immutable claimed snapshot while its target lock is held."""
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        dest = private_state.auto.path / slug
        if is_link_or_junction(src):
            return None
        if claim_snapshot.tree is None:
            logger.warning("Refusing to approve %s: claim has no immutable authority", name)
            return None
        snapshot = self._validate_and_redact_snapshot(claim_snapshot.tree, name, refusal=refusal)
        if snapshot is None:
            logger.warning("Refusing to approve %s: snapshot validation failed", name)
            return None
        if snapshot.metadata.get("kind") == "update":
            logger.warning("Refusing to approve %s as new: claimed candidate is an update", name)
            _note_refusal(refusal, PendingApprovalRefused("kind_mismatch"))
            return None
        if self._pinned_child_exists(private_state.auto, slug):
            logger.warning("Cannot approve %s: a live skill already exists", name)
            _note_refusal(refusal, PendingApprovalRefused("live_exists"))
            return None

        stage, _backup = self._publication_paths(src.name)
        if stage.parent != private_state.private.path:
            return None
        if self._pinned_child_exists(private_state.private, stage.name):
            logger.error("Refusing to reuse skill publication stage for %s", name)
            return None
        try:
            self._materialize_candidate_snapshot(snapshot, stage)
            self._sync_skill_tree(stage)
            after_hash = self._skill_tree_hash_child(private_state.private, stage.name)
            if after_hash is None:
                raise OSError("staged skill generation changed")
            if not self._prepare_claim_publication(
                claim_fd,
                self._claim_lock_path(src.name),
                src.name,
                kind="new",
                target_slug=slug,
                before_hash=None,
                after_hash=after_hash,
                claim_snapshot=_ClaimSnapshot(
                    snapshot.generation_hash,
                    snapshot.source_files.get(Path(".meta.json")),
                ),
                live_backup_identity=None,
                snapshot_version=None,
                new_version=1,
            ):
                raise OSError("claim journal failed")
            if (
                self._publish_prepared_skill_tree(
                    state=private_state,
                    live_dir=dest,
                    stage=stage,
                    backup=None,
                    backup_identity=None,
                    before_hash=None,
                    after_hash=after_hash,
                )
                != "published"
            ):
                logger.warning(
                    "Refusing to approve %s: whole-tree publication did not complete", name
                )
                return None
        except (KeyError, OSError):
            if (
                self._authenticated_claim_publication(
                    claim_fd, self._claim_lock_path(src.name), src.name
                )
                is None
            ):
                self._cleanup_publication_artifacts(src.name)
            logger.warning("Refusing to approve %s: could not publish validated snapshot", name)
            return None

        self._invalidate_iter_cache()
        logger.info("Approved pending skill: %s", name)
        return name

    def _refuse_failing_pending_scripts(self, src: Path, name: str) -> None:
        """Raise ``script_validation_failed`` when the candidate's scripts fail.

        One verdict, two surfaces: approve consults the SAME verdict function the
        pre-click badge serves, so a computable failing verdict refuses with the
        badge's own report and the click cannot disagree with the badge. Where no
        verdict is computable (no pinned walk), the by-name scripts are validated
        instead so the refusal still names the reason. Advisory either way: it
        reads the public candidate and can only refuse; the claimed snapshot is
        validated again before anything is published.
        """
        _fold = self._pending_scripts_verdict(src)
        if _fold is None:
            sdir = src / "scripts"
            scripts = self._collect_scripts(sdir) if sdir.is_dir() else []
            _fold = validate_scripts(scripts) if scripts else (True, {})
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

    def approve_pending_update_checked(self, slug: str) -> str:
        """Atomically claim and promote a pending UPDATE candidate over its live target.

        Raises ``PendingApprovalRefused`` with the reason; a refusal leaves both
        the live skill and the candidate as they were. The advisory checks below
        read the PUBLIC candidate, which a writer can still change, so they can
        only refuse earlier with a precise reason and never authorize anything.
        Promotion then captures an immutable generation before the public
        directory is renamed into the claim, and publishes only from that
        capture: the live tree is moved aside under ``auto/.live-quarantine/``
        and a fresh private stage holding the rewritten body, the version
        snapshot and the validated scripts is renamed live in its place. A
        refusal restores the claimed candidate to review without replacing a
        newer public candidate. Returns ``auto/<target>`` on success.
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
        if not (self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md").exists():
            logger.warning(
                "Refusing to approve update %s: target %r is not a live auto skill", slug, target
            )
            raise PendingApprovalRefused("target_missing")
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is not None:
            logger.warning("Refusing to approve update %s: %s", slug, disabled)
            raise PendingApprovalRefused("promotion_disabled")
        target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
        # The live version and frontmatter are rewrite reads, so a live path
        # that does not vet raises rather than answering "no metadata"; refuse
        # with the I/O reason instead of letting the claim run.
        try:
            self.get_auto_skill_version(target_name)
            self._cached_frontmatter(
                self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md",
                within=None,
                for_write=True,
            )
        except OSError:
            logger.warning(
                "Refusing to approve update %s: could not read the live skill's metadata",
                target_name,
            )
            raise PendingApprovalRefused("promotion_failed")
        if not self._candidate_layout_ok(src, target_name):
            raise PendingApprovalRefused("invalid_layout")
        self._refuse_failing_pending_scripts(src, target_name)
        refusal: list[PendingApprovalRefused] = []
        result = self._promote_pending_update(slug, refusal=refusal)
        if result is None:
            raise refusal[0] if refusal else PendingApprovalRefused("promotion_failed")
        return result[0]

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
        """Atomically claim and publish a pending candidate as a live auto-skill.

        Returns the live name; raises ``PendingApprovalRefused`` (with the reason,
        and the redacted findings report for a validation failure) if the
        candidate is missing, a live skill of that name already exists, it holds
        a link or an unexpected entry, script validation fails, or publication
        does not complete. The advisory checks read the PUBLIC candidate and can
        only refuse; publication uses only the generation captured at the claim
        rename, materialized into a fresh private stage that is renamed live
        without replacement. A refusal restores the claimed candidate to review.
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
        if (self._dir / name).exists():
            logger.warning("Cannot approve %s: a live skill already exists", name)
            raise PendingApprovalRefused("live_exists")
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is not None:
            logger.warning("Refusing to approve %s: %s", name, disabled)
            raise PendingApprovalRefused("promotion_disabled")
        if not self._candidate_layout_ok(src, name):
            raise PendingApprovalRefused("invalid_layout")
        self._refuse_failing_pending_scripts(src, name)
        refusal: list[PendingApprovalRefused] = []
        result = self._promote_pending_skill(slug, refusal=refusal)
        if result is None:
            raise refusal[0] if refusal else PendingApprovalRefused("promotion_failed")
        return result

    def _promote_pending_skill(
        self,
        slug: str,
        *,
        refusal: list[PendingApprovalRefused] | None = None,
    ) -> str | None:
        """Claim one NEW candidate and publish its immutable snapshot live."""
        claimed = self._claim_pending_update(slug)
        if claimed is None:
            return None
        src, claim_fd, consumed_at, claim_snapshot = claimed
        result: str | None = None
        live_published = False
        try:
            with self._promotion_lock(slug) as acquired:
                if not acquired:
                    logger.warning("Could not acquire promotion lock for auto/%s", slug)
                    return None
                try:
                    with self._pin_private_state(
                        create=False,
                        require_sensitive=True,
                    ) as private_state:
                        result = self._approve_claimed_skill_locked(
                            src,
                            claim_fd,
                            slug,
                            claim_snapshot,
                            private_state,
                            refusal=refusal,
                        )
                        if result is None:
                            return None
                        live_published = True
                        if not self._commit_claim_consumption(src, claim_fd):
                            result = None
                            return None
                        if not self._pinned_parent_matches(
                            private_state.auto
                        ) or not self._pinned_parent_matches(private_state.private):
                            result = None
                            return None
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    logger.warning("Promotion authority became unavailable for auto/%s", slug)
                    return None
            _emit_pending_consumed(
                {"slug": slug, "outcome": "approved", "name": result, "consumed_at": consumed_at}
            )
            return result
        finally:
            cleanup_claim_lock = True
            try:
                if result is None and not live_published:
                    cleanup_claim_lock = self._restore_failed_promotion_claim(
                        src,
                        claim_fd,
                        slug,
                        claim_snapshot,
                    )
            except OSError:
                cleanup_claim_lock = False
                logger.error("Could not restore claimed skill %s", src, exc_info=True)
            finally:
                platform_compat.release_lock(claim_fd)
                os.close(claim_fd)
                if cleanup_claim_lock:
                    self._cleanup_claim_lock(src.name)

    def dismiss_pending_skill(self, slug: str) -> bool:
        """Atomically claim and durably consume a pending candidate.

        A committed marker makes a failed physical cleanup recoverable by the
        same abandoned-claim pass used after promotion. Until that marker is
        durable, every failure restores the exact claim to review. The public
        candidate inode is retained as evidence, never deleted, because a
        writer holding a descriptor into it may still be writing.

        Lock-free dismissal is allowed only when an unmasked process observes this
        data home has no authority root, or freshly re-proves its no-replace
        refusal. No authority-backed claim can race in either state, so the
        candidate is removed by name as before this protocol. A sandboxed process
        treats both verdicts as indeterminate. With a root possible in every other
        state, dismissal takes the authority lock and fails closed when it cannot.
        """
        if not self._is_pending_slug_safe(slug):
            return False
        if _auto_skill_promotion_ruled_out():
            return _auto_skills.dismiss_pending_skill(self, slug)
        claimed = self._claim_pending_update(slug)
        if claimed is None:
            return False
        claim, claim_fd, consumed_at, claim_snapshot = claimed
        consumed = False
        quarantine = self._quarantine_root() / claim.name
        try:
            if is_link_or_junction(quarantine):
                try:
                    with self._pin_private_state(
                        create=False,
                        require_sensitive=True,
                    ) as private_state:
                        linked = self._stat_pinned_child(
                            private_state.quarantine,
                            claim.name,
                        )
                        if not (
                            stat.S_ISLNK(linked.st_mode)
                            or bool(getattr(linked, "st_reparse_tag", 0))
                            or is_link_or_junction(private_state.quarantine.path / claim.name)
                        ):
                            raise OSError("linked quarantine changed kind before unlink")
                        captured_identity = claim_snapshot.quarantine_identity
                        linked_identity = self._pinned_child_identity(
                            private_state.quarantine,
                            claim.name,
                        )
                        if (
                            captured_identity is None
                            or linked_identity is None
                            or linked_identity != captured_identity
                            or not self._unlink_skill_child(
                                private_state.quarantine,
                                claim.name,
                                expected=linked,
                                expected_identity=linked_identity,
                            )
                        ):
                            raise OSError("linked quarantine changed before unlink")
                except OSError:
                    logger.warning("Could not unlink pending-skill link: %s", slug)
                    return False
                consumed = True
            else:
                if not self._commit_claim_consumption(claim, claim_fd):
                    logger.warning("Could not commit pending-skill dismissal: %s", slug)
                    return False
                consumed = True
            logger.info("Dismissed pending skill: %s", slug)
            _emit_pending_consumed(
                {"slug": slug, "outcome": "dismissed", "consumed_at": consumed_at}
            )
            return True
        finally:
            try:
                if not consumed and (
                    os.path.lexists(claim)
                    or os.path.lexists(quarantine)
                    or is_link_or_junction(claim)
                    or is_link_or_junction(quarantine)
                ):
                    self._restore_claimed_update(claim, claim_fd, slug, claim_snapshot)
            except OSError:
                logger.error("Could not restore failed dismissal claim %s", claim, exc_info=True)
            finally:
                platform_compat.release_lock(claim_fd)
                os.close(claim_fd)
                if (
                    not os.path.lexists(claim)
                    and not os.path.lexists(quarantine)
                    and not os.path.lexists(self._evidence_root() / claim.name)
                ):
                    self._cleanup_claim_lock(claim.name)

    def dismiss_pending_skill_checked(self, slug: str) -> bool:
        """Dismiss like :meth:`dismiss_pending_skill`, naming an authority refusal.

        Returns ``True`` once the candidate is consumed and ``False`` when it is
        absent or its dismissal failed for another reason. Raises
        ``PendingDismissalRefused`` when the candidate is still staged, by-name
        dismissal is not ruled in, and this process cannot take the claim because
        it holds no authority certificate: the case a dashboard would otherwise
        report as "not found" for a candidate the operator can still see. The
        classification runs only after the dismissal returned ``False``, so it
        can change which refusal is reported and never what is removed.
        """
        if self.dismiss_pending_skill(slug):
            return True
        if not self._is_pending_slug_safe(slug) or _auto_skill_promotion_ruled_out():
            return False
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is None or not self.pending_candidate_is_staged(slug):
            return False
        hint = (
            _auto_skill_authority_retire_hint()
            if _auto_skill_sandbox_excludes_every_promoter()
            else None
        )
        raise PendingDismissalRefused("promotion_disabled", detail=disabled, hint=hint)

    def dismiss_all_pending(self) -> int:
        """Delete all pending candidates. Returns count dismissed.

        Contract and rationale: ``skill_runtime.auto_skills.dismiss_all_pending``.
        """
        return _auto_skills.dismiss_all_pending(self)

    def dismiss_pending_slugs(self, slugs: list[str]) -> int:
        """Delete only the specified pending candidates. Returns count dismissed.

        Contract and rationale: ``skill_runtime.auto_skills.dismiss_pending_slugs``.
        """
        return _auto_skills.dismiss_pending_slugs(self, slugs)

    def prune_pending(self, ttl_days: int, *, now: float | None = None) -> int:
        """Remove pending candidates older than ``ttl_days``. Returns count pruned.

        Prunes nothing while auto-skill staging and promotion are off
        (:func:`auto_skill_promotion_disabled_reason`): a candidate queued before
        then cannot be approved on this host, so an automatic TTL removal would
        destroy reviewable work instead of leaving it for a supervised migration.
        An explicit dismissal still removes it. Contract and rationale:
        ``skill_runtime.auto_skills.prune_pending``.
        """
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is not None:
            logger.info("Pending-skill TTL prune skipped: %s", disabled)
            return 0
        return _auto_skills.prune_pending(self, ttl_days, now=now)

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

        Returns at most ``max_triggered`` matcher results sorted by best overlap
        score; the cap does not apply to a list *select* returns, which replaces
        them as given. A cap of zero (the shipped default) is the matcher
        switched off: no skill is scanned or scored and no trigger audit row is
        written; only *select* can still name skills.
        """
        scored: list[tuple[str, float]] = []
        # Skills a negative trigger actively excluded — a permission DENY that
        # must still be audited (see the audit event below).
        negated_skills: list[str] = []
        # The cap is read BEFORE the scan. At zero every skill scored below would
        # be sliced away, so the walk -- a frontmatter read, a repo-scope fence
        # check and a trigger score per visible skill, on every message -- would
        # buy nothing, and a `!` veto it recorded would be a DENY for a grant that
        # could never have happened. Tokenizing the message feeds only that
        # scoring, so it waits for the cap too. `select` still gets its turn: a
        # selection point owns its own zero-cap refusal.
        cap = self._max_triggered_now()
        visible = self._iter_visible(project_dir) if cap > 0 else ()
        text_words: set[str] = words_of(text) if cap > 0 else set()
        for name, skill_file, _within in visible:
            # A reader on the per-message path: one SKILL.md that is not UTF-8
            # costs its own match, never the turn (rationale on the helper).
            meta = self._readable_frontmatter(skill_file, within=_within)
            if meta is None:
                continue
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
        triggered = [name for name, _ in scored[:cap]]

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

        Contract and rationale: ``skill_runtime.delivery.split_triggered``.
        """
        return _delivery.split_triggered(self, names, project_dir)

    def confined_triggered(
        self, names: list[str], project_dir: str | Path | None = None
    ) -> set[str]:
        """Return the subset of *names* that are confined project skills.

        Contract and rationale: ``skill_runtime.delivery.confined_triggered``.
        """
        return _delivery.confined_triggered(self, names, project_dir)

    def trigger_hint(self, names: list[str], project_dir: str | Path | None = None) -> str:
        """Return a pointer block naming *names* and where to read each one.

        Contract and rationale: ``skill_runtime.delivery.trigger_hint``.
        """
        return _delivery.trigger_hint(self, names, project_dir)

    def _resolve_path(self, name: str, project_dir: str | Path | None = None) -> Path | None:
        """Return the ``SKILL.md`` path for an enumerated skill *name*."""
        return _catalog._resolve_path(self, name, project_dir)

    def _resolve_path_and_root(
        self, name: str, project_dir: str | Path | None = None
    ) -> tuple[Path, str | None] | None:
        """The enumerated path for *name* PLUS the root it is confined to."""
        return _catalog._resolve_path_and_root(self, name, project_dir)

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

        Raises ``SkillContextCapacityError`` rather than trimming an operator's
        required body; a project's ``always: true`` body that cannot be delivered
        is skipped with a warning and an in-prompt notice instead.
        Contract and rationale: ``skill_runtime.delivery.get_context``.
        """
        return _delivery.get_context(
            self,
            budget,
            only,
            project_dir,
            project_body_budget,
            discovery_only=discovery_only,
            required_parts_out=required_parts_out,
        )

    def _legacy_context(
        self,
        all_skills: list[dict],
        restricted: bool = False,
        project_dir: str | Path | None = None,
        project_body_budget: int | None = None,
    ) -> str:
        """Explicit unbudgeted reader, not the default startup path."""
        return _delivery._legacy_context(
            self, all_skills, restricted, project_dir, project_body_budget
        )

    def _append_project_skill_bodies(
        self,
        parts: list[str],
        project_skills: list[dict],
        project_dir: str | Path | None,
        budget: int | None,
        pinned: set[str] | None = None,
    ) -> _delivery.SkippedProjectSkills:
        """Append confined bodies within the section budget; return what was skipped.

        Each skipped key is also discarded from *pinned*, when given, in place.
        """
        return _delivery._append_project_skill_bodies(
            self, parts, project_skills, project_dir, budget, pinned
        )

    def _record_use(self, key: str) -> None:
        """Best-effort usage bump for the lazy-load ranking. Never raises."""
        return _read_credit._record_use(self, key)

    def _recency_boost(self, path_str: str, fingerprint: str = "") -> float:
        """Return the file mtime if the skill is newer than the boost window, else 0.0. Lets a freshly-added, never-used skill rank above stale unused ones (cold-start protection) without flooding the top of the list."""
        return _delivery._recency_boost(self, path_str, fingerprint)

    def _rank_key(self, s: dict) -> tuple[float, float]:
        """Sort key for on-demand skills: (usage_hits, effective_recency). Higher sorts first. Falls back to recency-only if the ledger is absent."""
        return _delivery._rank_key(self, s)

    def _is_user_authored(self, s: dict) -> bool:
        """Whether *s* is a skill the user wrote rather than one Kiro Crew shipped.

        Shipped means a packaged built-in (by key, or by the provenance marker
        the builtin sync writes into every copy it installs — which also covers
        a retired built-in still on disk), a skill an app registered (its file
        resolves into a provider root), or an edition-contributed root. Anything
        else — a skill the user created in the skills dir, a ``skills.extra_paths``
        root, a trusted project's ``.kiro/skills``, an agent's ``skill://``
        mapping — is the user's.

        A confined project row answers before any filesystem call: resolving its
        path would reintroduce the link probe the confined walker exists to
        prevent, and a project skill is never shipped anyway.
        """
        if s.get("confine_root"):
            return True
        if str(s["key"]) in _packaged_skill_names():
            return False
        path = Path(str(s["path"]))
        if any(path.is_relative_to(root) for root in self._edition_extra_paths):
            return False
        if os.path.lexists(path.parent / _PROVENANCE_MARKER):
            return False
        return not _within_any(os.path.realpath(path), _trusted_skill_roots())

    def _user_first(self, ranked: list[dict]) -> list[dict]:
        """Reorder *ranked* so the user's own skills lead without owning the head.

        The first ``_INDEX_HEAD_SLOTS`` positions hold up to ``_INDEX_USER_SLOTS``
        user-authored rows — the highest-ranked ones — and whatever positions
        those leave are filled from the rank order of everything else, shipped or
        overflow user rows alike. After the head, user rows precede shipped rows,
        each group in rank order.

        A new install has no usage history, so rank alone would let the ~60
        shipped skills take every slot and a skill the user just wrote is never
        named; reserving the whole head for the user instead lets a large user
        tree evict a shipped skill the user genuinely relies on. The quota fixes
        the first without causing the second. Order within each group is the
        caller's rank order, so the existing key tie-break still decides ties.
        """
        user_ids = {id(s) for s in ranked if self._is_user_authored(s)}
        head = [s for s in ranked if id(s) in user_ids][:_INDEX_USER_SLOTS]
        head_ids = {id(s) for s in head}
        rest = [s for s in ranked if id(s) not in head_ids]
        fill = _INDEX_HEAD_SLOTS - len(head)
        head += rest[:fill]
        tail = rest[fill:]
        return (
            head
            + [s for s in tail if id(s) in user_ids]
            + [s for s in tail if id(s) not in user_ids]
        )

    @staticmethod
    def _short_desc(desc: str, suffix: str = "...") -> str:
        """Collapse whitespace and truncate a description for the summary line."""
        return _delivery._short_desc(desc, suffix)

    def _body_hits(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> dict[str, int]:
        return _search._body_hits(self, skills, terms, live_keys, project_dir)

    def _body_matches(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> tuple[dict[str, set[str]], bool]:
        """Refresh once per query, with bounded work and explicit incomplete recall."""
        return _search._body_matches(self, skills, terms, live_keys, project_dir)

    def _scoped_entries(
        self,
        project_dir: str | Path | None,
        only: list[str] | None,
    ) -> list[_ScopedSkillEntry]:
        return _catalog._scoped_entries(self, project_dir, only)

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
        max_bytes: int = SKILL_READ_CAPACITY,
    ) -> str | None:
        """Read an exact catalog key, never an ambiguous leaf or caller path.

        The whole body when it fits *max_bytes*, else ``None``: the contract the
        required-skill and ``$key`` activation paths rely on, where a body that does
        not fit is a body to refuse. :meth:`read_scoped_skill_page` is the face that
        also says WHY, and that serves a larger body in pages.
        """
        return self._read_exact_key(
            key, only=only, project_dir=project_dir, max_bytes=max_bytes
        ).content

    def read_scoped_skill_page(
        self,
        key: str,
        *,
        only: list[str] | None = None,
        project_dir: str | Path | None = None,
        offset: int | None = None,
        limit: int | None = None,
        capacity: int = SKILL_READ_CAPACITY,
    ) -> SkillBodyPage | SkillReadRefusal:
        """An exact-key read for the search tool: the whole body, or one page of it.

        *offset* and *limit* are LINES (0-based first line, most lines), the unit
        the file and transcript readers already page in. With neither given the
        body is delivered whole when it fits *capacity* and refused with its size
        when it does not; with either given, as many whole lines from *offset* as
        fit *capacity* (at most *limit*) are delivered with the offset of the next
        page. A refusal says which of three things stopped the read instead of
        naming all three, because the caller acts differently on each.

        The capacity bounds one DELIVERY, not the file. A body over it is read
        again under the bound the whole file has anyway -- the shared file safety
        cap for a global body, ``PROJECT_SKILL_BODY_CAP`` for a confined one --
        so that its size can be reported and its pages served. A confined body is
        never read past the project cap: paging never reads a checkout's file
        past what one read may, so a body over that cap is refused naming the
        project bound, whatever capacity the caller asked for, and offers no page.
        """
        read = self._read_exact_key(key, only=only, project_dir=project_dir, max_bytes=capacity)
        bound = capacity
        if read.refusal == SKILL_READ_OVER_CAPACITY:
            bound = PROJECT_SKILL_BODY_CAP if read.confined else hooks_module.MAX_FILE_BYTES
            if bound > capacity:
                read = self._read_exact_key(
                    key, only=only, project_dir=project_dir, max_bytes=bound
                )
        if read.content is None:
            if read.confined and read.refusal == SKILL_READ_OVER_CAPACITY:
                return SkillReadRefusal(read.refusal, PROJECT_SKILL_BODY_CAP, confined=True)
            return SkillReadRefusal(read.refusal, bound, incomplete=read.incomplete)
        return _page_skill_body(read.content, offset=offset, limit=limit, capacity=capacity)

    def _read_exact_key(
        self,
        key: str,
        *,
        only: list[str] | None,
        project_dir: str | Path | None,
        max_bytes: int,
    ) -> _ExactRead:
        """Resolve an exact key through the scope and classify why it was refused.

        A key the scoped enumeration does not hold, and that the building-time
        resolver does not admit, is outside the scope; so is a body whose
        ``repo_scope`` this project does not satisfy, because the catalog never
        listed it here. Once the scope holds the key, every failure to deliver its
        bytes is either the size bound (``size_cap``) or the fenced reader's own
        refusal, and never a missing key.
        """
        entry = next(
            (entry for entry in self._scoped_entries(project_dir, only) if entry[0] == key), None
        )
        confined = entry is not None and entry.project_root is not None
        reasons: list[str] = []
        if entry is None:
            content = self._exact_read_while_building(
                key, only, project_dir, max_bytes, refusal_reasons=reasons
            )
        elif entry.mapping_root is not None:
            content = self._read_global_skill_text(
                entry.path,
                max_bytes,
                canonical_root=entry.mapping_root,
                refusal_reasons=reasons,
            )
        else:
            content = self.load_skill(
                key, project_dir, max_bytes=max_bytes, refusal_reasons=reasons
            )
        if content is None:
            if "size_cap" in reasons:
                refusal = SKILL_READ_OVER_CAPACITY
            elif entry is not None or reasons:
                refusal = SKILL_READ_UNREADABLE
            else:
                # While the first walk is unfinished the building-time resolver
                # withholds the confined project tier by design, so a miss here
                # cannot tell a correct project key from an absent one.
                building = self.catalog_status(project_dir) == "building"
                return _ExactRead(None, SKILL_READ_OUTSIDE_SCOPE, confined, building)
            return _ExactRead(None, refusal, confined)
        meta = self._parse_frontmatter_text(content)
        if meta.get("repo_scope") and not self._repo_scope_satisfied(
            meta["repo_scope"], project_dir
        ):
            return _ExactRead(None, SKILL_READ_OUTSIDE_SCOPE, confined)
        return _ExactRead(content, "", confined)

    def _exact_read_while_building(
        self,
        key: str,
        only: list[str] | None,
        project_dir: str | Path | None,
        max_bytes: int,
        refusal_reasons: list[str] | None = None,
    ) -> str | None:
        """Serve a COMPLETE key during an unfinished first walk, or ``None``."""
        return _search._exact_read_while_building(
            self, key, only, project_dir, max_bytes, refusal_reasons=refusal_reasons
        )

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

        The matches of :meth:`search_skills_report`, for callers that do not report
        whether the answer may be missing matches.
        """
        return self.search_skills_report(
            query, limit, project_dir=project_dir, only=only, offset=offset, browse=browse
        ).matches

    def search_skills_report(
        self,
        query: str,
        limit: int = 20,
        *,
        project_dir: str | Path | None = None,
        only: list[str] | None = None,
        offset: int = 0,
        browse: bool = False,
    ) -> SkillSearchReport:
        """One search's matches plus whether they may be incomplete.

        Contract and rationale: ``skill_runtime.search.SkillSearchReport`` and
        ``skill_runtime.search.search_skills_report``.
        """
        return _search.search_skills_report(
            self, query, limit, project_dir=project_dir, only=only, offset=offset, browse=browse
        )

    def resolve_dollar_skills(
        self,
        text: str,
        project_dir: str | Path | None = None,
        *,
        only: list[str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Resolve ``$skillname`` tokens in *text* to loadable skills.

        Contract and rationale: ``skill_runtime.search.resolve_dollar_skills``.
        """
        return _search.resolve_dollar_skills(self, text, project_dir, only=only)

    @staticmethod
    def has_dollar_candidate(text: str) -> bool:
        """True if *text* contains at least one ``$skill``-shaped token.

        Contract and rationale: ``skill_runtime.search.has_dollar_candidate``.
        """
        return _search.has_dollar_candidate(text)

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
        meta = parse_frontmatter(content, SKILL_LOADER)
        body = SkillsLoader.strip_frontmatter(content.lstrip("\ufeff")).lstrip()[:10]
        if re.match(r"<(?:!doctype|html)[\s>]", body, re.IGNORECASE):
            meta["_html:body"] = "true"
        return meta

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
