"""Native Kiro launch views whose skill directory is supplied by Crew.

The authored resource mapping remains the authority for Crew search/list/read.
Native aliases preserve the other spec fields but carry no skill:// resources:
Kiro 2.21.2 progressively loads bodies, yet enumerates all their metadata before
the first prompt. Bounding only the Crew prompt cannot bound that native cost.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp import session_mcp
from kiro_crew.agent_discovery import SCOPE_PROJECT, _read_agent_spec, list_agents
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write, on_event_loop
from kiro_crew.config.paths import data_home, kiro_agents_dir, kiro_home, project_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.security import _PATH_RESOLVE_TIMEOUT_SECS
from kiro_crew.validation import is_registered_agent_name
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock

logger = logging.getLogger(__name__)

_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"
_SEARCH_TOOL = "@kirocrew-core/skill_search"
_PROJECTION_LOCK_NAME = ".kirocrew-skill-projection.lock"
_PROJECTION_LEASE_DIR_NAME = ".kirocrew-skill-projection-leases"
# A lease is a readable record plus an unread lock target. Windows file locks are
# mandatory, so a lock on the record itself makes every reader's probe fail.
_PROJECTION_LEASE_RECORD_SUFFIX = ".json"
_PROJECTION_LEASE_HOLDER_SUFFIX = ".hold"
# The exact stem _acquire_projection_lease writes (``<pid>-<uuid4 hex>``). A
# record-less ``.hold`` is reclaimed only when its stem matches, because nothing
# else about an empty file proves this module wrote it: an operator's
# ``notes.hold`` in the lease directory is left alone. (A record/holder pair is
# reclaimed only after its record parses as a lease record, which is that proof.)
_PROJECTION_LEASE_STEM_RE = re.compile(r"[0-9]+-[0-9a-f]{32}")
# ONE bound for both ends of the lease. The reader answers "live" above it, so a
# writer allowed to exceed it could publish a record that is unreclaimable by
# construction: a crash would leave it on disk and every later probe would read
# it as held, disabling pruning for good. Refusing the publication instead falls
# back to authored native agents, which is recoverable on the next spawn.
_PROJECTION_LEASE_MAX_ALIASES = 1024
_PROJECTION_LEASE_MAX_BYTES = 65536
# Lease discovery is a startup-path liveness check. Stream records and stop
# after bounded work; an incomplete scan is uncertainty and therefore live.
_PROJECTION_LEASE_SCAN_LIMIT = 4096
# The scan's wall-clock bound, BETWEEN entries: the entry cap above bounds how
# many parse-plus-lock-probe steps run, not how long a slow filesystem takes
# over them, and the scan runs under the publication lock. Expiry is handled
# exactly like the cap (validated residue drains, the pass answers uncertain).
# It shares _PROJECTION_LOCK_TIMEOUT_SECS with the prune walk's own
# _PRUNE_MAX_SECONDS_PER_RUN and the publication writes, so it is sized to match
# that walk's budget rather than to fill the ceiling.
_PROJECTION_LEASE_SCAN_MAX_SECONDS = 0.4
# The exact shape prepare_native_skill_projection derives, so a legacy reclaim
# admits only names this module could have produced. The digest length is pinned
# here rather than recomputed from the writer, because widening the writer must
# not silently widen what the reclaim is willing to delete.
_LEGACY_ALIAS_NAME_RE = re.compile(re.escape(NATIVE_SKILL_ALIAS_PREFIX) + r"[0-9a-f]{24}")
# Candidate work is bounded independently of successful reclamation. Retained
# aliases must not turn a prune under the publication lock into an unbounded scan.
# It is the unit of the ENUMERATION ceiling, the part of the section the time
# budget below does not cover: one call yields at most this many stale candidates
# and walks at most three times this many directory entries (one limit for stale
# candidates, one for ordinary authored entries, one of retained-alias credit)
# before the budgeted classification starts (see _projection_prune_candidates).
_PROJECTION_PRUNE_WORK_LIMIT = 4096
# A CEILING on reclaims per run, never a floor: the time budget below can end a
# call having reclaimed none at all. It is headroom over the aliases one run
# publishes, so a call that does reach them covers the steady-state orphan rate
# as well as some backlog. It bounds no part of the critical section: a candidate
# that is unreclaimable costs a full classification and never increments it,
# which is why the section carries its own budget below.
_PRUNE_MAX_RECLAIMS_PER_RUN = 64
# The boot drain runs the per-spawn prune in a loop, so each batch holds the
# publication lock at most as long as one spawn's prune does. The pause between
# batches lets a waiting spawn take the lock: a blocked acquire polls with
# backoff up to the lock's poll cap, so the pause exceeds that cap (asserted by
# test). One batch reclaiming nothing can be a budget spent on kept or leased
# entries before its random start offset reached a stale one, so the drain
# stops only after this many CONSECUTIVE idle batches, and the batch count
# bounds it even if another writer keeps refilling the directory.
_DRAIN_MAX_BATCHES = 1000
_DRAIN_IDLE_BATCHES = 3
_DRAIN_BATCH_PAUSE_SECS = platform_compat._LOCK_POLL_MAX_SECS * 2
# One warning per this many seconds when an alias unlink is refused by the OS.
# Silence here is what turned a permission problem into a wrong root cause: a
# read-only or foreign-owned agents directory made every reclaim a no-op and
# nothing said so. Per-file logging would flood at backlog scale, so the line
# carries how many refusals it stands for.
_UNLINK_WARNING_INTERVAL_SECS = 300.0
# The refusals that DO mean the directory is not writable by this process. Any
# other errno (ENOENT after a concurrent prune elsewhere took the file, EMFILE,
# EIO, ...) is reported by name without that diagnosis, which would send an
# operator to fix a mount or an owner that is fine.
_UNWRITABLE_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
# The ONE window the re-preparation contract does not cover, and the only thing
# this age excludes. A publisher from a build that predates the lease holds no
# lease, so between its write and kiro-cli reading `--agent` its alias looks
# exactly like backlog -- and that process will NOT re-prepare, because it
# already did, so a deletion there is a failed spawn rather than an eviction.
# This is deliberately NOT a liveness proxy (the reason an age cut-off was
# rejected for the recorded path): it only has to exceed publish-to-spawn, which
# is milliseconds, and the real backlog is hours to days old.
_LEGACY_RECLAIM_MIN_AGE_SECS = 600.0
_PROJECTION_METADATA_DIR_NAME = ".kirocrew-skill-projection-metadata"
# Publication plus pruning is normally sub-second. Two seconds absorbs scheduler
# jitter and short Windows rename retries without inheriting the generic five-minute
# lock ceiling on the native startup path.
_PROJECTION_LOCK_TIMEOUT_SECS = 2.0
# Threads of THIS process queue on an in-process lock before the file lock, so
# the file lock's ceiling above measures cross-process contention only. Without
# it, N concurrent session starts in one gateway each race the file lock on
# their own clock, and in a burst the later ones time out and start without
# their skill view although no other process holds the lock. The in-process
# wait is bounded well under the session/new budget; every caller runs off the
# event loop.
_IN_PROCESS_PROJECTION_WAIT_SECS = 30.0
_IN_PROCESS_PROJECTION_LOCKS: dict[str, threading.Lock] = {}
_IN_PROCESS_PROJECTION_LOCKS_GUARD = threading.Lock()
# Queued starts pay for each other's locked sections one after another, and the
# per-spawn prune walk is most of a section. Pruning only reclaims stale files,
# so a burst gets one walk per this interval per agents directory; the boot
# drain prunes on its own schedule and is unaffected.
_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS = 5.0
_LAST_PER_SPAWN_PRUNE: dict[str, float] = {}
# The share of that ceiling the prune walk must leave to the rest of its locked
# section: the publication writes (two atomic writes per alias plus the settings
# commit) and scheduler jitter. The lease scan's budget is taken out on its own.
_PROJECTION_PUBLICATION_RESERVE_SECS = 1.2


def _prune_budget_within(ceiling: float) -> float:
    """The walk's share of ``ceiling`` once the lease scan and publication reserve are out."""
    return ceiling - _PROJECTION_PUBLICATION_RESERVE_SECS - _PROJECTION_LEASE_SCAN_MAX_SECONDS


# The budget for one call's classification work, and a BETWEEN-candidate one: it
# bounds how many candidates are walked, not how long any single one takes, and
# the directory enumeration that precedes the walk is outside it. Derived from the
# lock ceiling, so retuning the ceiling moves the budget and keeps the reserve.
_PRUNE_MAX_SECONDS_PER_RUN = _prune_budget_within(_PROJECTION_LOCK_TIMEOUT_SECS)

# Generated specs stay in Kiro's shared agents directory, so metadata identifies
# them for direct scanners and scopes cleanup to the owning Kiro Crew data home.
_MANAGED_MARKER = "x-kirocrew-managed"
_MANAGED_MARKER_VALUE = "skill-view"
_MANAGED_CREW_HOME = "x-kirocrew-home"
_MANAGED_AGENT = "x-kirocrew-agent"
_MANAGED_SOURCE = "x-kirocrew-source"
_MANAGED_ALIAS_SHA256 = "x-kirocrew-alias-sha256"
# The digest of the alias's IDENTITY view (:func:`_alias_identity`), recorded next
# to the byte digest. A launcher that re-serializes every spec in the agents
# directory, or stamps a fresh VOLATILE value (:func:`volatile_env_keys`) into an
# MCP server's ``env``, changes the
# bytes but not what the alias is, so ownership survives it where the byte digest
# alone does not. Sidecars written without this key carry only the byte digest
# and are judged by it.
_MANAGED_VIEW_SHA256 = "x-kirocrew-view-sha256"
# Ownership sidecars one prune may reclaim when their alias is already gone.
# Metadata only -- no kiro-cli cost rides on it -- so a modest ceiling that the
# boot drain multiplies by its batch count is enough to retire a backlog.
_SIDECAR_SWEEP_MAX_PER_RUN = 256

# Display-only fields kiro-cli copies out of EVERY agent spec in the directory into
# each ``session/new`` / ``session/load`` reply (its per-agent info: name,
# description, source, welcomeMessage). A projected alias is a second copy of an
# agent the host already lists, so its copy of that text only doubles the reply,
# and past the stdout frame limit the reply is dropped. Kiro Crew renders an
# agent's welcomeMessage from the AUTHORED spec (agent_discovery), never from an
# alias, so the alias leaves it out and keeps a short description.
_ALIAS_DROPPED_DISPLAY_FIELDS = ("welcomeMessage",)
_ALIAS_DESCRIPTION_MAX_CHARS = 512
# Warn when an authored spec's display text alone is this large: kiro-cli sends it
# in every session reply however Kiro Crew projects it, so the fix is at the source.
_DISPLAY_TEXT_WARN_BYTES = 32 * 1024
# ... and when the whole directory's display text approaches the frame limit.
_DISPLAY_TEXT_TOTAL_WARN_BYTES = 4 * 1024 * 1024
_DISPLAY_TEXT_WARNED: set[tuple[str, int]] = set()  # (sha256 of path, size)
_DISPLAY_TEXT_WARNED_MAX = 256
# The ACP readers' stdout frame limit (``acp.runtime._STDOUT_BUFFER_LIMIT``; a test
# pins the two equal). Not imported: this module is imported by the runtime.
_STDOUT_FRAME_LIMIT_BYTES = 10 * 1024 * 1024


def _loggable(value: str) -> str:
    """*value* (an agent name or path read from disk) as one bounded, escaped token."""
    return repr(value[:_LOGGED_NAME_MAX_CHARS])


_LOGGED_NAME_MAX_CHARS = 240


def _display_text_bytes(spec: dict[str, Any]) -> int:
    """UTF-8 bytes of the display-only text kiro-cli ships for *spec* per session reply."""
    total = 0
    for key in ("description", *_ALIAS_DROPPED_DISPLAY_FIELDS):
        value = spec.get(key)
        if isinstance(value, str):
            total += len(value.encode("utf-8", "replace"))
    return total


def _strip_alias_display_text(view: dict[str, Any]) -> None:
    """Keep an alias view's per-session-reply footprint small (see above)."""
    for key in _ALIAS_DROPPED_DISPLAY_FIELDS:
        view.pop(key, None)
    description = view.get("description")
    if isinstance(description, str) and len(description) > _ALIAS_DESCRIPTION_MAX_CHARS:
        view["description"] = description[: _ALIAS_DESCRIPTION_MAX_CHARS - 1] + "\u2026"


def _warn_on_display_text(sizes: dict[str, tuple[int, str]]) -> None:
    """Name the agents whose display text inflates every session reply; once per size.

    *sizes* maps agent name to (display bytes, source path). The source path names
    the package that installed the spec, which is who can trim it.
    """
    large = sorted(
        (
            (size, name, path)
            for name, (size, path) in sizes.items()
            if size >= _DISPLAY_TEXT_WARN_BYTES
        ),
        reverse=True,
    )
    omitted = 0
    for size, name, path in large:
        entry = (hashlib.sha256(path.encode("utf-8", "replace")).hexdigest(), size)
        if entry in _DISPLAY_TEXT_WARNED:
            continue
        if len(_DISPLAY_TEXT_WARNED) >= _DISPLAY_TEXT_WARNED_MAX:
            # The bound holds per insertion: past it, the rest are only counted.
            omitted += 1
            continue
        _DISPLAY_TEXT_WARNED.add(entry)
        logger.warning(
            "skill projection: agent %s (%s) carries %d KB of description/welcomeMessage "
            "text; kiro-cli sends it for every agent in every session/new reply, which "
            "Kiro Crew drops past %d MB. Skill-view aliases leave it out; trim it at the source.",
            _loggable(name),
            _loggable(path),
            size // 1024,
            _STDOUT_FRAME_LIMIT_BYTES // (1024 * 1024),
        )
    if omitted:
        logger.warning(
            "skill projection: %d more agent(s) carry over %d KB of description/"
            "welcomeMessage text each (not named: the warning memory is full)",
            omitted,
            _DISPLAY_TEXT_WARN_BYTES // 1024,
        )
    total = sum(size for size, _path in sizes.values())
    key = ("<total>", total)
    if (
        total >= _DISPLAY_TEXT_TOTAL_WARN_BYTES
        and key not in _DISPLAY_TEXT_WARNED
        and len(_DISPLAY_TEXT_WARNED) < _DISPLAY_TEXT_WARNED_MAX
    ):
        _DISPLAY_TEXT_WARNED.add(key)
        logger.warning(
            "skill projection: the agents directory carries %d KB of description/"
            "welcomeMessage text across %d agent(s), near the %d MB session/new reply "
            "limit; largest: %s",
            total // 1024,
            len(sizes),
            _STDOUT_FRAME_LIMIT_BYTES // (1024 * 1024),
            ", ".join(f"{_loggable(name)} ({size // 1024} KB)" for size, name, _path in large[:3])
            or "-",
        )


# Every view name this process has published, mapped to the agent it projects.
# A projection is replaced when a view's content changes, so a frame or a
# session can still carry a name the CURRENT projection does not hold; this
# is what turns that name back into its agent without a file read. Bounded:
# the oldest names go first, and a name that falls out is still resolved from
# its ownership sidecar, or from the view ledger below.
_VIEW_SOURCES: dict[str, str] = {}
_VIEW_SOURCES_LOCK = threading.Lock()
# One entry bound for this map and the ledger below, so a name the ledger still
# holds is never one the map was sized to forget, or the reverse.
_VIEW_SOURCES_MAX = 2048
# The same map on disk, so a view name stored before a restart still resolves
# after the boot drain removed its alias AND its sidecar. It lives beside the
# sidecars (the directory the prune never walks) under a name no alias can
# take, is rewritten only under the publication lock, and holds at most
# _VIEW_SOURCES_MAX entries (see _record_view_ledger_entries for which go first
# past that). A missing or unreadable ledger resolves nothing.
# Written at every publication AND before every sidecar deletion
# (_ViewLedgerWrites), so a view published by a build that predates the ledger
# keeps resolving once the prune or the orphan-sidecar sweep retires its record.
_VIEW_LEDGER_NAME = "view-sources.json"


class RetiredSkillView(ValueError):
    """A mode name is a skill view whose source agent cannot be recovered."""

    def __init__(self, view: str) -> None:
        self.view = view
        super().__init__(
            f"'{view}' is a generated skill view from an earlier run, and the agent it "
            "was built from is not recorded anywhere. Pick the agent for this chat or "
            "crewmate again, or start a new chat; a gateway restart rebuilds every view."
        )


def _admissible_source_agent(agent_name: object) -> bool:
    """Whether *agent_name* may be recorded or returned as a view's source agent.

    One admission for the in-memory map, the sidecar read and the ledger: the
    registered agent-name grammar, which bounds the length and admits no
    control character, so a recorded name is safe to log and to send. Never
    another view name.
    """
    return is_registered_agent_name(agent_name) and not is_skill_view_name(agent_name)


def _remember_view_sources(aliases: dict[str, str]) -> None:
    with _VIEW_SOURCES_LOCK:
        for agent_name, alias in aliases.items():
            if not _admissible_source_agent(agent_name):
                continue
            _VIEW_SOURCES.pop(alias, None)
            _VIEW_SOURCES[alias] = agent_name
        while len(_VIEW_SOURCES) > _VIEW_SOURCES_MAX:
            _VIEW_SOURCES.pop(next(iter(_VIEW_SOURCES)))


def is_skill_view_name(name: object) -> bool:
    """Whether *name* is a generated skill-view name rather than an agent."""
    return isinstance(name, str) and name.startswith(NATIVE_SKILL_ALIAS_PREFIX)


def remembered_view_source(name: str) -> str | None:
    """The agent a view name projects, from this process's memory only. No I/O."""
    with _VIEW_SOURCES_LOCK:
        return _VIEW_SOURCES.get(name)


def _read_json_regular_file(path: Path) -> Any:
    """Parse *path* when it is a regular, non-link file; ``None`` otherwise."""
    try:
        info = pinned_fs.lstat_by_name(path)
        if info is None or platform_compat.is_link_or_junction(path):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        raw = safe_read_file_bytes(str(path))
    except (OSError, ValueError, FileTooLargeError):
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None


def _read_view_ledger(metadata_dir: Path) -> dict[str, str]:
    """The ledger's admissible entries, oldest first; empty when unreadable."""
    data = _read_json_regular_file(metadata_dir / _VIEW_LEDGER_NAME)
    if not isinstance(data, dict):
        return {}
    return {
        alias: agent_name
        for alias, agent_name in data.items()
        if isinstance(alias, str)
        and _LEGACY_ALIAS_NAME_RE.fullmatch(alias)
        and _admissible_source_agent(agent_name)
    }


def _record_view_ledger(metadata_dir: Path, aliases: dict[str, str]) -> None:
    """Add *aliases* (agent -> alias) to the ledger. The caller holds the publication lock."""
    _record_view_ledger_entries(
        metadata_dir, {alias: agent_name for agent_name, alias in aliases.items()}
    )


_LEDGER_EVICTION_WARNED = False


def _sidecar_answers_for(metadata_dir: Path, alias: str, agent_name: str) -> bool:
    """Whether *alias*'s sidecar still maps it to *agent_name* on its own.

    The same reading :func:`_recorded_view_source` gives it: a regular file
    carrying the managed marker and naming that agent. A sidecar that is
    present but unreadable, foreign or naming another agent does not answer, so
    its ledger entry is not redundant.
    """
    record = _read_json_regular_file(metadata_dir / f"{alias}.json")
    return _managed_marker(record) and record.get(_MANAGED_AGENT) == agent_name


def _record_view_ledger_entries(metadata_dir: Path, views: dict[str, str]) -> dict[str, str]:
    """Add *views* (alias -> agent) to the ledger and return what it now holds.

    The one ledger writer. A view of a run that is publishing names one alias per
    agent; a retired backlog names many aliases of the same agent, which is why
    this is keyed by alias. Inadmissible pairs are skipped and an unchanged
    ledger is not rewritten. The caller holds the publication lock; the write is
    atomic and a failure raises ``OSError`` with the old ledger intact.

    Every pair this call adds becomes the newest entry. Past
    :data:`_VIEW_SOURCES_MAX`, entries go in this order: first those whose
    sidecar still answers for them on its own (:func:`_sidecar_answers_for`;
    oldest first), then the oldest entries
    whose sidecar is gone, whose view names stop resolving (counted and
    logged), and only when this call alone adds more than the bound, its own
    oldest additions, which the caller then sees as not held. The bound
    therefore degrades the oldest retired names rather than refusing new ones,
    so a full ledger never stops a sweep from retiring sidecars.
    """
    global _LEDGER_EVICTION_WARNED
    ledger = _read_view_ledger(metadata_dir)
    updated = dict(ledger)
    adding: set[str] = set()
    for alias, agent_name in views.items():
        if _admissible_source_agent(agent_name) and _LEGACY_ALIAS_NAME_RE.fullmatch(alias):
            updated.pop(alias, None)
            updated[alias] = str(agent_name)
            adding.add(alias)
    if len(updated) > _VIEW_SOURCES_MAX:
        for alias in list(updated):
            if len(updated) <= _VIEW_SOURCES_MAX:
                break
            if alias not in adding and _sidecar_answers_for(metadata_dir, alias, updated[alias]):
                del updated[alias]
        dropped = 0
        for alias in list(updated):
            if len(updated) <= _VIEW_SOURCES_MAX:
                break
            if alias not in adding:
                del updated[alias]
                dropped += 1
        while len(updated) > _VIEW_SOURCES_MAX:
            updated.pop(next(iter(updated)))
        if dropped:
            log = logger.debug if _LEDGER_EVICTION_WARNED else logger.warning
            _LEDGER_EVICTION_WARNED = True
            log(
                "skill projection: view ledger full (%d entries); dropped the %d oldest "
                "retired view name(s), which no longer map back to their agent",
                _VIEW_SOURCES_MAX,
                dropped,
            )
    if list(updated.items()) == list(ledger.items()):
        return updated
    atomic_write(
        metadata_dir / _VIEW_LEDGER_NAME,
        json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
        restrict_to_owner=True,
    )
    return updated


#: How many prune candidates one ledger write covers ahead of their reclaim.
_LEDGER_PREFETCH_WINDOW = 64


class _ViewLedgerWrites:
    """Keep a view's source agent in the ledger before its sidecar is deleted.

    A sidecar is the record :func:`source_agent_name` maps a stored view name
    back through. Deleting it is harmless for a view this build published, whose
    name the ledger already holds, and loses the mapping for one a pre-ledger
    build published (a name an older conversation still stores). So every
    deletion path asks :meth:`retain` first, and keeps the sidecar when the
    ledger write fails. A prune walk names its remaining candidates through
    :meth:`look_ahead`, and the first pair :meth:`retain` has to record brings
    the next :data:`_LEDGER_PREFETCH_WINDOW` of them along in the same write, so
    a drain of thousands of pairs pays one ledger write per window rather than
    one per pair, and a walk that reclaims nothing reads nothing extra. Used only
    under the publication lock, for the life of one prune or sweep.
    """

    def __init__(self, directory: Path, crew_home_id: str) -> None:
        self._directory = directory
        self._crew_home_id = crew_home_id
        self._metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
        self._entries: dict[str, str] | None = None
        self._ahead: list[Path] = []
        self._ahead_from = 0
        # Set by a write the filesystem refused; the rest of this prune or
        # sweep keeps every sidecar instead of retrying it per candidate.
        self._failed = False

    def _held(self) -> dict[str, str]:
        if self._entries is None:
            self._entries = _read_view_ledger(self._metadata_dir)
        return self._entries

    def look_ahead(self, candidates: list[Path], index: int) -> None:
        """The walk is at ``candidates[index]``; a write may cover the ones after it."""
        self._ahead, self._ahead_from = candidates, index

    def retain_many(self, views: dict[str, str]) -> bool:
        """Whether every admissible pair of *views* is in the ledger now."""
        held = self._held()
        wanted = {
            alias: agent_name
            for alias, agent_name in views.items()
            if _admissible_source_agent(agent_name) and _LEGACY_ALIAS_NAME_RE.fullmatch(alias)
        }
        if all(held.get(alias) == agent for alias, agent in wanted.items()):
            return True
        if self._failed:
            return False
        try:
            # The WHOLE batch is written, not only its missing pairs: a pair the
            # ledger already holds becomes newest too, so the bound cannot evict
            # it in this same write while its sidecar is about to go.
            self._entries = _record_view_ledger_entries(self._metadata_dir, wanted)
        except OSError:
            logger.debug("skill projection: view ledger not written", exc_info=True)
            self._failed = True
            return False
        return all(self._entries.get(alias) == agent for alias, agent in wanted.items())

    def retain(self, alias: str, record: object) -> bool:
        """Whether *record*'s source agent for *alias* is in the ledger (or it names none).

        A record with no admissible agent is not one :func:`source_agent_name`
        would answer from, so deleting it loses nothing.
        """
        agent_name = record.get(_MANAGED_AGENT) if isinstance(record, dict) else None
        if not _admissible_source_agent(agent_name):
            return True
        if self._held().get(alias) == agent_name:
            return True
        if self._failed:
            return False
        views = self._upcoming_views()
        views[alias] = str(agent_name)
        return self.retain_many(views)

    def _upcoming_views(self) -> dict[str, str]:
        """Source agents of this home's sidecars for the next window of candidates.

        Recording one is harmless whether or not its pair is reclaimed: the
        record names this home and the agent the view was built from, which is
        what the ledger states. A stem with no readable record is skipped.
        """
        window = self._ahead[self._ahead_from : self._ahead_from + _LEDGER_PREFETCH_WINDOW]
        views: dict[str, str] = {}
        for candidate in window:
            record, _path, _identity = _sidecar_record(self._directory, candidate.stem)
            if record is None or record.get(_MANAGED_CREW_HOME) != self._crew_home_id:
                continue
            agent_name = record.get(_MANAGED_AGENT)
            if _admissible_source_agent(agent_name):
                views[candidate.stem] = str(agent_name)
        return views


def _recorded_view_source(name: str) -> str | None:
    """The agent a view's sidecar or the view ledger names, or ``None``. Blocking.

    Only a name of the exact shape this module mints is looked up, so the name
    cannot steer the read outside the sidecar directory. The alias itself may be
    gone; the sidecar's agent field is read on its own, and it must carry the
    managed marker and name an agent rather than another view. When the boot
    drain has removed the sidecar too, the ledger still answers.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(name):
        return None
    try:
        metadata_dir = kiro_agents_dir() / _PROJECTION_METADATA_DIR_NAME
    except (OSError, ValueError, RuntimeError):
        return None
    metadata = _read_json_regular_file(metadata_dir / f"{name}.json")
    if _managed_marker(metadata):
        agent_name = metadata.get(_MANAGED_AGENT)
        if _admissible_source_agent(agent_name):
            return str(agent_name)
    return _read_view_ledger(metadata_dir).get(name)


def source_agent_name(name: str) -> str:
    """The agent to activate for a mode name that may be a stored view name.

    A mode name is sent to kiro-cli only after it is mapped to the running
    projection, so a stored view name -- written by an earlier version, or by an
    earlier projection of this process -- is never sent as is: it names a file
    the boot drain or a content change may already have removed. It maps back
    to the agent it was built from, whose CURRENT view the caller then uses.
    Blocking (it may read one sidecar); callers on the event loop run it in a
    thread. Raises :class:`RetiredSkillView` when nothing records the source,
    rather than guessing an agent, which could be a broader one.
    """
    if not is_skill_view_name(name):
        return name
    source = remembered_view_source(name) or _recorded_view_source(name)
    if source is None:
        raise RetiredSkillView(name)
    return source


async def resolve_source_agent(name: str) -> str:
    """:func:`source_agent_name` for an event-loop caller; no hop for an agent name."""
    if not is_skill_view_name(name):
        return name
    return await asyncio.to_thread(source_agent_name, name)


# How many earlier aliases one projection keeps translating inbound. A process
# publishes a new alias only when an agent's view changes, so this is far above
# any real run; it bounds a process whose specs are edited without end.
_RECOGNISED_ALIASES_MAX = 1024


@dataclass
class NativeSkillProjection:
    """Translate transport identities while Crew keeps the authored agent name."""

    aliases: dict[str, str]
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    search_agents: set[str] = field(default_factory=set)
    _lease_finalizer: Any = field(default=None, repr=False, compare=False)
    # Aliases an EARLIER projection of this process published, alias -> agent. The
    # host may still hold them (every alias it loaded at spawn, say), so inbound
    # frames keep translating them back even after this projection renamed the
    # agent; only outbound requests use ``aliases``.
    _recognised: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    _recognition_overflow_warned: bool = field(default=False, repr=False, compare=False)

    def recognise(self, earlier: "NativeSkillProjection") -> None:
        """Keep translating the aliases *earlier* knew in inbound frames, up to a bound.

        Called with the spawn projection FIRST, so its aliases -- the ones the host
        is guaranteed to hold -- are the last to be refused once the bound is met.
        Only alias-shaped names mapped to admissible agent names are kept, so the
        count bound bounds the memory too.
        """
        dropped = 0
        for alias, name in (
            *((a, n) for n, a in earlier.aliases.items()),
            *earlier._recognised.items(),
        ):
            if alias in self._recognised:
                continue
            if len(self._recognised) >= _RECOGNISED_ALIASES_MAX:
                dropped += 1
                continue
            # The registered agent-name grammar bounds each retained name and admits
            # no control character; an alias is a fixed-length digest name.
            if not _admissible_source_agent(name) or not _LEGACY_ALIAS_NAME_RE.fullmatch(alias):
                continue
            self._recognised.setdefault(alias, name)
        if dropped and not self._recognition_overflow_warned:
            # Once per projection: the bound is a backstop, and past it the oldest
            # aliases this process published stop translating in inbound frames.
            self._recognition_overflow_warned = True
            logger.warning(
                "skill projection: %d earlier skill-view alias(es) not recognised past the "
                "%d-alias bound; a mode the host still lists only under one of them will "
                "not be offered until the gateway restarts",
                dropped,
                _RECOGNISED_ALIASES_MAX,
            )

    def agent(self, name: str) -> str:
        if is_skill_view_name(name):
            if name in self.aliases.values():
                return name
            source = remembered_view_source(name)
            if source is None:
                raise RetiredSkillView(name)
            name = source
        if name not in self.aliases:
            if name in self.errors:
                raise ValueError(f"Agent {name!r}: {self.errors[name]}")
            raise ValueError(f"Agent {name!r} has no prepared skill discovery view")
        return self.aliases[name]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_mode":
            return {**params, "modeId": self.agent(str(params.get("modeId", "")))}
        if method == "_kiro.dev/commands/execute":
            command = params.get("command", "")
            if isinstance(command, dict):
                name = str(command.get("command", "")).lstrip("/")
                args = command.get("args") or {}
                value = str(args.get("value", "")) if isinstance(args, dict) else ""
            else:
                words = str(command).strip().lstrip("/").split(None, 1)
                name = words[0] if words else ""
                value = words[1] if len(words) > 1 else ""
            if name == "agent" and value.strip() not in {"list", "schema"}:
                raise ValueError(
                    "Use Crew's agent selector to change agents so its skill scope stays in sync."
                )
        return params

    def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        reverse = {**self._recognised, **{alias: name for name, alias in self.aliases.items()}}

        def visit(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {key: visit(item, key) for key, item in value.items()}
            if isinstance(value, list):
                if field == "availableModes":
                    # A mode stays listed when the host advertises ANY name this
                    # process knows the agent by: one of its aliases, or the
                    # authored id of a projected agent (listed while the host has
                    # not loaded the alias, so the start reaches set_mode and its
                    # actionable refusal instead of "not installed"). Each agent is
                    # listed once; unprojected host agents stay hidden.
                    kept: list[Any] = []
                    listed: set[str] = set()
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        mode_id = item.get("id")
                        name = reverse.get(mode_id) if isinstance(mode_id, str) else None
                        if name is None and mode_id in self.aliases:
                            name = mode_id
                        if name is None or name in listed:
                            continue
                        listed.add(name)
                        kept.append(item)
                    value = kept
                return [visit(item) for item in value]
            if field in {"id", "name", "agentName", "modeId", "currentModeId"} and isinstance(
                value, str
            ):
                if value in reverse:
                    return reverse[value]
                if is_skill_view_name(value):
                    # A view an earlier projection of this process published:
                    # report its agent, so no session stores a view name.
                    return remembered_view_source(value) or value
            return value

        return visit(frame)


_ACTIVE_PROJECTIONS: weakref.WeakValueDictionary[int, NativeSkillProjection] = (
    weakref.WeakValueDictionary()
)
_ACTIVE_PROJECTIONS_LOCK = threading.Lock()


def _register_active_projection(projection: NativeSkillProjection) -> None:
    with _ACTIVE_PROJECTIONS_LOCK:
        _ACTIVE_PROJECTIONS[id(projection)] = projection


def _active_aliases() -> set[str]:
    with _ACTIVE_PROJECTIONS_LOCK:
        projections = tuple(_ACTIVE_PROJECTIONS.values())
    return {alias for projection in projections for alias in projection.aliases.values()}


def _in_process_projection_lock(directory: Path) -> threading.Lock:
    """The one in-process lock for *directory*'s alias publication and pruning."""
    key = directory.absolute().as_posix()
    with _IN_PROCESS_PROJECTION_LOCKS_GUARD:
        lock = _IN_PROCESS_PROJECTION_LOCKS.get(key)
        if lock is None:
            lock = _IN_PROCESS_PROJECTION_LOCKS[key] = threading.Lock()
        return lock


def _per_spawn_prune_due(directory: Path) -> bool:
    """Whether this spawn runs the prune walk; call under the alias lock."""
    key = directory.absolute().as_posix()
    now = time.monotonic()
    last = _LAST_PER_SPAWN_PRUNE.get(key)
    if last is not None and 0.0 <= now - last < _PER_SPAWN_PRUNE_MIN_INTERVAL_SECS:
        return False
    _LAST_PER_SPAWN_PRUNE[key] = now
    return True


def _projection_alias_lock(directory: Path) -> ExitStack:
    """Acquire the bounded cross-process lock for alias publication and pruning.

    Threads of this process first queue on :func:`_in_process_projection_lock`,
    so only one of them at a time competes for the file lock.
    """
    stack = ExitStack()
    local = _in_process_projection_lock(directory)
    if not local.acquire(timeout=_IN_PROCESS_PROJECTION_WAIT_SECS):
        raise OSError(
            "skill projection lock still busy inside this process after "
            f"{_IN_PROCESS_PROJECTION_WAIT_SECS:g}s"
        )
    stack.callback(local.release)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / _PROJECTION_LOCK_NAME
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError("skill projection lock is a symlink or junction")
        lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
        opened = os.fstat(lock_fd)
        named = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("skill projection lock changed while it was opened")
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd, exclusive=True, timeout=_PROJECTION_LOCK_TIMEOUT_SECS
            )
        )
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("skill projection lock changed while it was acquired")
    except BaseException:
        # Any failure, not only OSError: the stack holds the in-process lock, and
        # a leaked one makes every later start in this process wait it out.
        stack.close()
        raise
    return stack


def _ensure_projection_metadata_directory(directory: Path) -> Path:
    """Create and verify the hidden directory that owns projection sidecars."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    if platform_compat.is_link_or_junction(metadata_dir):
        raise OSError("skill projection metadata directory is a symlink or junction")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    info = pinned_fs.lstat_by_name(metadata_dir)
    if (
        platform_compat.is_link_or_junction(metadata_dir)
        or info is None
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise OSError("skill projection metadata directory is not a real directory")
    return metadata_dir


def _unlink_projection_lease_if_unchanged(
    path: Path, identity: tuple[int, int], *, what: str = "projection record"
) -> bool:
    """Remove one unlocked lease only while its random name keeps its identity.

    *what* names the kind of file for the refusal report: a lease record, its
    holder sidecar, or an ownership sidecar. A refusal from the filesystem is
    reported through the same rate-limited seam as an alias unlink -- the same
    environment fault silences both, and one that is only half reported is the
    wrong root cause again. An identity change stays a silent ``False``.
    """
    current = pinned_fs.lstat_by_name(path)
    if (
        current is None
        or platform_compat.is_link_or_junction(path)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        return False
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError as exc:
            _warn_unlink_refused(path, exc, what=what)
            return False
        try:
            return pinned_fs.unlink_verified(
                parent_fd,
                path.name,
                identity,
                on_error=lambda exc: _warn_unlink_refused(path, exc, what=what),
            )
        finally:
            os.close(parent_fd)
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        path.unlink()
    except OSError as exc:
        _warn_unlink_refused(path, exc, what=what)
        return False
    return True


def _acquire_projection_lease(directory: Path, aliases: set[str]) -> ExitStack:
    """Publish and hold one process lease covering this projection's aliases.

    The lease is TWO files: a ``.json`` record naming the aliases, which is never
    locked, and a ``.lock`` sidecar that carries the lifetime lock and is never
    read. They are split because Windows file locks are MANDATORY, not advisory:
    :func:`platform_compat.file_lock` takes ``msvcrt.locking`` on byte 0, and a
    read of a locked byte from any other handle -- including another handle in
    this same process -- fails with a lock violation. Holding the lock on the
    record a reader must parse therefore made every liveness probe raise, which
    :func:`_scan_projection_leases` reads as uncertainty and answers "live", so
    nothing was ever reclaimed on Windows while a single lease was held. Locking
    a file nobody reads keeps the OS liveness proof and leaves the record legible.
    """
    stack = ExitStack()
    if not aliases:
        return stack
    try:
        lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
        if platform_compat.is_link_or_junction(lease_dir):
            raise OSError("skill projection lease directory is a symlink or junction")
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_info = pinned_fs.lstat_by_name(lease_dir)
        if (
            platform_compat.is_link_or_junction(lease_dir)
            or lease_info is None
            or not stat.S_ISDIR(lease_info.st_mode)
        ):
            raise OSError("skill projection lease directory is not a real directory")
        stem = f"{os.getpid()}-{uuid.uuid4().hex}"
        lease_path = lease_dir / f"{stem}{_PROJECTION_LEASE_RECORD_SUFFIX}"
        holder_path = lease_dir / f"{stem}{_PROJECTION_LEASE_HOLDER_SUFFIX}"
        record = json.dumps({"aliases": sorted(aliases)}, separators=(",", ":"))
        if (
            len(aliases) > _PROJECTION_LEASE_MAX_ALIASES
            or len(record.encode()) > _PROJECTION_LEASE_MAX_BYTES
        ):
            # Publishing past the reader's own bound would leave a record no
            # reclaim can ever retire. Refuse instead: the caller falls back to
            # authored native agents and the next spawn tries again.
            raise OSError(
                f"skill projection lease would exceed its reader's bound "
                f"({len(aliases)} aliases, {len(record.encode())} bytes)"
            )
        atomic_write(lease_path, record, restrict_to_owner=True)
        created = pinned_fs.lstat_by_name(lease_path)
        if created is None or not stat.S_ISREG(created.st_mode):
            raise OSError("skill projection lease was not published as a regular file")
        identity = (created.st_dev, created.st_ino)
        # Registered before the descriptor contexts so ExitStack releases the
        # lease lock and file handle first (required for unlink on Windows).
        stack.callback(
            _unlink_projection_lease_if_unchanged, lease_path, identity, what="lease record"
        )
        atomic_write(holder_path, "", restrict_to_owner=True)
        holder_created = pinned_fs.lstat_by_name(holder_path)
        if holder_created is None or not stat.S_ISREG(holder_created.st_mode):
            raise OSError("skill projection lease holder was not published as a regular file")
        holder_identity = (holder_created.st_dev, holder_created.st_ino)
        stack.callback(
            _unlink_projection_lease_if_unchanged,
            holder_path,
            holder_identity,
            what="lease holder",
        )
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            platform_compat.is_link_or_junction(lease_path)
            or platform_compat.is_link_or_junction(holder_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != holder_identity
            or (named.st_dev, named.st_ino) != holder_identity
        ):
            raise OSError("skill projection lease changed while it was opened")
        stack.enter_context(platform_compat.file_lock(holder_fd, exclusive=True, wait=False))
    except OSError:
        stack.close()
        raise
    return stack


def _read_lease_record(lease_path: Path) -> list[str] | None:
    """The alias list one lease record names, or ``None`` when it cannot be trusted.

    The ONE parse of the record shape, shared by the liveness probe and the
    census so the two cannot disagree. Bounded by the writer's own limits
    (:data:`_PROJECTION_LEASE_MAX_BYTES`, :data:`_PROJECTION_LEASE_MAX_ALIASES`):
    an oversized, malformed, or non-list record is ``None``, and so is any read
    error. A plain bounded read, not the hardened one: this runs once per lease
    on EVERY spawn and every set_mode, and the hardened reader adds path
    validation and an audit write per call, which is measurable on the
    projected-MCP E2E -- it passes at 288s against a 300s ceiling, so a few
    percent decides it. No lock is ever taken on this file, so the read cannot
    collide with a holder the way the pre-split single-file lease did.
    """
    try:
        record_fd = os.open(lease_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(record_fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(record_fd)
        if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
            return None
        body = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        # RecursionError is a RuntimeError, not a ValueError: a hand-authored
        # record nested past the interpreter limit must read as untrusted, not
        # abort the caller. No writer in this module produces one.
        return None
    listed = body.get("aliases") if isinstance(body, dict) else None
    if (
        not isinstance(listed, list)
        or len(listed) > _PROJECTION_LEASE_MAX_ALIASES
        or any(not isinstance(value, str) for value in listed)
    ):
        return None
    return listed


def _is_projection_lease_stem(stem: str) -> bool:
    """Whether *stem* has the ``<pid>-<uuid4 hex>`` shape this module's writer uses."""
    return _PROJECTION_LEASE_STEM_RE.fullmatch(stem) is not None


def _path_exists(path: Path) -> bool:
    """Whether *path* is present by name; any error other than absence is present.

    Only a clean ``FileNotFoundError`` proves a sidecar is gone, so a reclaim
    count can never claim a lease whose pair could not be observed as removed.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


_LeaseIdentities = tuple[tuple[int, int], tuple[int, int]]


def _probe_projection_lease(lease_path: Path) -> tuple[list[str], _LeaseIdentities | None] | None:
    """Parse one lease record and lock-probe its ``.hold``; ``None`` is uncertainty.

    Returns the aliases the record names plus, when the holder lock could be
    taken, the record and holder identities of that crash/finalizer residue.
    A held lock returns no identities: its process is live. Any link, non-regular
    file, replaced holder, unreadable record or probe failure answers ``None``.
    """
    holder_path = lease_path.with_name(
        lease_path.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)] + _PROJECTION_LEASE_HOLDER_SUFFIX
    )
    stack = ExitStack()
    try:
        if platform_compat.is_link_or_junction(lease_path) or platform_compat.is_link_or_junction(
            holder_path
        ):
            return None
        record_info = pinned_fs.lstat_by_name(lease_path)
        if record_info is None or not stat.S_ISREG(record_info.st_mode):
            return None
        record_identity = (record_info.st_dev, record_info.st_ino)
        # The record is already identity-checked and non-link above; the
        # bounded parse itself is shared with the census (see the helper).
        listed = _read_lease_record(lease_path)
        if listed is None:
            return None
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            platform_compat.is_link_or_junction(holder_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            return None
        holder_identity = (opened.st_dev, opened.st_ino)
        try:
            with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                return listed, (record_identity, holder_identity)
        except (BlockingIOError, OSError):
            return listed, None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        stack.close()


def _probe_orphan_projection_holder(holder_path: Path) -> tuple[int, int] | None:
    """Return the identity of an unlocked ``.hold`` whose record is gone, else ``None``.

    A lease is published record-first and finalized holder-first, so a holder
    with no record is never part of a live lease: it is litter a failed record
    or holder unlink left behind. It names no alias, so a holder that cannot be
    proven unlocked is simply left alone rather than making the pass uncertain.

    The writer publishes every holder EMPTY and never writes to it again (the
    lock occupies no bytes), so a non-empty file carries content this module did
    not put there. The stem shape alone is not provenance for an irreversible
    unlink, so such a file is kept whatever its name.
    """
    stack = ExitStack()
    try:
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            named is None
            or platform_compat.is_link_or_junction(holder_path)
            or not stat.S_ISREG(named.st_mode)
            or named.st_size != 0
        ):
            return None
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or opened.st_size != 0:
            return None
        try:
            with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                return (opened.st_dev, opened.st_ino)
        except (BlockingIOError, OSError):
            return None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        stack.close()


def _scan_projection_leases(directory: Path) -> tuple[set[str], bool]:
    """Scan the lease directory ONCE, returning ``(live_aliases, uncertain)``.

    The single lease-liveness scan, which a prune calls exactly once rather than
    once per candidate: with a candidate cap and a lease-scan cap both in the
    thousands, a per-candidate scan multiplies into a quadratic sweep under the
    held publication lock. One bounded scan parses and lock-probes each lease a
    single time, unions the aliases named by every HELD lease into
    ``live_aliases``, and reports ``uncertain`` when it could not see the whole
    directory with confidence. Callers test membership in the returned
    ``live_aliases`` set for a single alias; there is no per-alias wrapper.

    Uncertainty is fail-closed: the caller authorizes NO candidate deletion when
    it is True, because a candidate whose covering lease the scan could not read
    must be treated as live. Held leases contribute to the union without
    short-circuiting, so the scan runs to the end and can still
    reclaim crash/finalizer residue after closure even when held leases exist.
    Residue is only ever unlinked AFTER the ``scandir`` context closes, never
    mid-iteration. A clean completion, a cap and an unreadable record all drain
    the residue they lock-validated before stopping, because each unlink is
    identity-checked on its own pair; a failed open or iteration of the directory
    itself trusts nothing it observed and discards the queued residue. A cap, an
    unreadable record and any error all answer uncertain, and the first two are
    logged at INFO with the count reclaimed. A spent
    :data:`_PROJECTION_LEASE_SCAN_MAX_SECONDS` deadline, checked between entries,
    is treated exactly like the cap. An unlocked ``.hold`` whose record
    is gone is reclaimed as residue too, so that litter cannot pin the ceiling.
    That orphan reclaim is limited to the writer's ``<pid>-<uuid4 hex>`` stem:
    an empty ``.hold`` carries no other proof that this module wrote it.

    The record is read WITHOUT taking any lock on it -- see
    :func:`_acquire_projection_lease` for why the lock lives on a separate file.
    """
    lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
    lease_info = pinned_fs.lstat_by_name(lease_dir)
    if lease_info is None:
        return set(), False
    if platform_compat.is_link_or_junction(lease_dir) or not stat.S_ISDIR(lease_info.st_mode):
        return set(), True
    live_aliases: set[str] = set()
    deferred_reclaims: list[tuple[Path, tuple[int, int] | None, Path, tuple[int, int]]] = []
    scan_capped = False
    scan_expired = False
    unreadable: str | None = None
    scanned = 0
    deadline = time.monotonic() + _PROJECTION_LEASE_SCAN_MAX_SECONDS
    try:
        with os.scandir(lease_dir) as entries:
            for entry in entries:
                # Bound EVERY directory entry, not just the ``.json`` records:
                # the cap is a ceiling on directory traversal under the held
                # publication lock, so ``.hold`` sidecars and unrelated files
                # consume it before the suffix filter runs. The check runs before
                # the increment: at most the limit is counted, and the next
                # fetched entry triggers the break.
                if scanned >= _PROJECTION_LEASE_SCAN_LIMIT:
                    scan_capped = True
                    break
                # A count alone cannot bound wall-clock time, so the scan also
                # stops between entries once its deadline is spent.
                if time.monotonic() >= deadline:
                    scan_expired = True
                    break
                scanned += 1
                if entry.name.endswith(_PROJECTION_LEASE_HOLDER_SUFFIX):
                    # A holder whose record is gone still consumes the entry
                    # ceiling on every scan, so it is reclaimed as residue;
                    # otherwise enough of that litter would keep every pass
                    # capped and pruning deferred for good.
                    holder_path = lease_dir / entry.name
                    record_path = holder_path.with_name(
                        entry.name[: -len(_PROJECTION_LEASE_HOLDER_SUFFIX)]
                        + _PROJECTION_LEASE_RECORD_SUFFIX
                    )
                    if _is_projection_lease_stem(
                        entry.name[: -len(_PROJECTION_LEASE_HOLDER_SUFFIX)]
                    ) and not _path_exists(record_path):
                        orphan = _probe_orphan_projection_holder(holder_path)
                        if orphan is not None:
                            deferred_reclaims.append((record_path, None, holder_path, orphan))
                    continue
                if not entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX):
                    continue
                lease_path = lease_dir / entry.name
                probed = _probe_projection_lease(lease_path)
                if probed is None:
                    # An unreadable, linked or replaced record makes the pass
                    # uncertain, so no alias is deleted. The scan still
                    # continues within its cap and deadline, and the residue it
                    # validates is drained below: each unlink is identity-checked
                    # on its own pair, so one bad sibling does not invalidate that
                    # proof. Stopping here would let a single persistent bad
                    # record early in directory order block cleanup of every
                    # stale lease after it forever.
                    if unreadable is None:
                        unreadable = entry.name
                    continue
                listed, unlocked = probed
                if unlocked is None:
                    # Held: this lease's process is live, so EVERY alias it
                    # names is live. Union them and keep scanning, so a later
                    # error can still discard the queued cleanup and a clean
                    # completion can still reclaim residue behind this lease.
                    live_aliases.update(listed)
                    continue
                record_identity, holder_identity = unlocked
                holder_path = lease_path.with_name(
                    entry.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)]
                    + _PROJECTION_LEASE_HOLDER_SUFFIX
                )
                deferred_reclaims.append(
                    (lease_path, record_identity, holder_path, holder_identity)
                )
    except OSError:
        # A failed open or iteration trusts nothing this pass observed, so it
        # discards the queued residue and answers uncertain.
        return live_aliases, True

    # Residue is unlinked only after the scandir context has closed. A cap still
    # drains the residue validated before it, and an unreadable record the
    # residue validated anywhere in the walk, then each answers uncertain.
    reclaimed_leases = 0
    for lease_path, pending_record, holder_path, holder_identity in deferred_reclaims:
        if not _unlink_projection_lease_if_unchanged(holder_path, holder_identity) and (
            _path_exists(holder_path)
        ):
            # Keep the record while its holder stays, so the pair is retried
            # whole by the next scan rather than left as a record-less holder.
            continue
        if pending_record is not None:
            _unlink_projection_lease_if_unchanged(lease_path, pending_record)
        # Count a lease reclaimed only once BOTH sidecars are gone: one failed
        # unlink leaves a half-removed pair, which the next scan still sees.
        if not _path_exists(lease_path) and not _path_exists(holder_path):
            logger.debug("skill projection: reclaimed stale lease %s", lease_path.name)
            reclaimed_leases += 1
    if unreadable is not None:
        # Persistent while the record stays, and it blocks every alias prune in
        # this directory, so it is reported where an operator will see it.
        logger.info(
            "skill projection: lease record %s is unreadable; reclaimed %d stale lease(s), "
            "alias pruning deferred until it is removed or becomes readable",
            unreadable,
            reclaimed_leases,
        )
        return live_aliases, True
    if scan_capped:
        # The cap is the signal that a backlog is draining incrementally, and the
        # caller authorizes no alias deletion on it, so say so with the count
        # this bounded prefix did reclaim rather than returning silently.
        logger.info(
            "skill projection: lease scan reached its %d-entry ceiling; reclaimed %d stale "
            "lease(s), alias pruning deferred to a later spawn",
            _PROJECTION_LEASE_SCAN_LIMIT,
            reclaimed_leases,
        )
    if scan_expired:
        logger.info(
            "skill projection: lease scan spent its %.1f-second budget after %d entr(ies); "
            "reclaimed %d stale lease(s), alias pruning deferred to a later spawn",
            _PROJECTION_LEASE_SCAN_MAX_SECONDS,
            scanned,
            reclaimed_leases,
        )
    # A clean scan proves the union is complete; a capped or expired scan drained
    # the residue it validated but could not see past where it stopped, so it
    # answers uncertain.
    return live_aliases, scan_capped or scan_expired


_ABSENT_SETTINGS = object()

#: Pauses before the retries of a refused ``cli.json`` read, in seconds; the
#: first attempt runs before any of them. A healthy file is refused transiently
#: in two ways: a writer renames a new file over the one that was opened (the
#: descriptor check then names the unlinked inode), which clears on an immediate
#: re-check, and a saturated sensitive-path resolver, which clears once a
#: candidate resolution has had its full budget. No other refusal changes with
#: time inside one call, so more pauses would only delay the fail-closed answer.
_SETTINGS_READ_RETRY_PAUSES: tuple[float, ...] = (0.0, _PATH_RESOLVE_TIMEOUT_SECS)

#: The pause primitive of :func:`_settings`, held as a module attribute so a test
#: can replace it without touching the shared ``time`` module.
_settings_read_sleep = time.sleep


def _settings(path: Path, *, may_pause: bool = True) -> dict[str, Any]:
    """Read one Kiro ``cli.json`` through the credential gate; absence reads ``{}``.

    Off the event loop a refused read is retried on
    :data:`_SETTINGS_READ_RETRY_PAUSES`: one immediate re-check, then one pause
    and a final attempt. Every attempt re-runs the whole gate, so a path that
    stays refused still raises. On a thread running an event loop exactly one
    attempt runs, because each attempt is several bounded path resolutions and a
    pause there stalls every session the gateway serves.

    ``may_pause=False`` keeps the immediate re-check but drops the pause, for a
    caller holding a cross-process lock whose contenders give up after the same
    2 s: sleeping there would make a concurrent spawn lose its lock instead.
    """

    def read_once() -> bytes | object | None:
        if not path.exists():
            return _ABSENT_SETTINGS
        return safe_read_file_bytes(str(path))

    raw = read_once()
    if raw is None and not on_event_loop():
        for pause in _SETTINGS_READ_RETRY_PAUSES:
            if pause and not may_pause:
                break
            if pause:
                _settings_read_sleep(pause)
            raw = read_once()
            if raw is not None:
                break
    if raw is None:
        raise ValueError(f"Cannot read Kiro settings at {path}")
    if not isinstance(raw, bytes):
        # The absence sentinel: the file does not exist.
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {path}")
    return data


def _inheritance_preference(
    local: dict[str, Any], global_settings: dict[str, Any]
) -> tuple[bool, Any, bool]:
    """Return ``(inherited, source, overlaid)`` for custom agents in one workspace.

    Once Crew's overlay is in place the native key reads ``true`` whatever the
    user chose, so the recorded preference decides: a ``global`` source follows
    the live global setting and any other source keeps the recorded value.
    Without the overlay, or after the user moves the native key off ``true``,
    the native key decides -- the workspace value when present, else the global
    one -- and only the literal ``true`` opts out.
    """
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
        source = "local" if _INHERIT_SETTING in local else "global"
        native = local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING))
        return native is not True, source, False
    if source == "global":
        return global_settings.get(_INHERIT_SETTING) is not True, source, True
    return inherited, source, True


def inherits_default_resources(work_dir: str | os.PathLike[str] | None) -> bool:
    """Whether a custom agent started in *work_dir* inherits kiro-cli's default resources.

    Those defaults are global and workspace steering plus ``AGENTS.md``. This is
    the read-only twin of the decision :func:`prepare_native_skill_projection`
    makes, for callers that must mirror what the native agent loads. Settings
    that cannot be read keep inheritance, which is how those callers behaved
    before they asked.
    """
    try:
        global_settings = _settings(kiro_home() / "settings" / "cli.json")
        local = (
            _settings(Path(work_dir) / ".kiro" / "settings" / "cli.json")
            if work_dir is not None
            else {}
        )
    # RuntimeError: Path.resolve() reports a symlink loop that way before Python 3.13.
    except (OSError, ValueError, RuntimeError, RecursionError, FileTooLargeError):
        logger.warning(
            "skill projection: Kiro settings unreadable; keeping inherited resources",
            exc_info=True,
        )
        return True
    return _inheritance_preference(local, global_settings)[0]


def _restore_inheritance(path: Path, local: dict[str, Any]) -> None:
    """Undo only our overlay; a changed or removed native setting wins."""
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {path}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    atomic_write(path, json.dumps(local, indent=2))


def _managed_marker(spec: object) -> bool:
    """Return whether a generated spec carries this lifecycle's marker."""
    return isinstance(spec, dict) and spec.get(_MANAGED_MARKER) == _MANAGED_MARKER_VALUE


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _view_digest(view: dict[str, Any]) -> str:
    """The sha256 of *view*'s identity (:func:`_alias_identity`), serialization-free.

    What the ownership sidecar binds besides the exact bytes, so a launcher that
    re-serializes an alias or re-stamps an ``env`` value in it does not strand
    the pair. The identity is the one the alias is NAMED by.
    """
    return hashlib.sha256(_canonical_json(_alias_identity(view)).encode()).hexdigest()


def _parsed_spec(raw: bytes) -> dict[str, Any] | None:
    try:
        spec = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    return spec if isinstance(spec, dict) else None


_UNLINK_WARNING_LOCK = threading.Lock()
_UNLINK_WARNING_LAST = 0.0
_UNLINK_WARNING_SUPPRESSED = 0


def _warn_unlink_refused(path: Path, exc: OSError, *, what: str = "stale alias") -> None:
    """Report a projection-file unlink the OS refused, at most once per interval.

    The prune's every other "no" is a deliberate keep -- kept, active, leased,
    changed under the walk -- and stays at debug. This one is not: the walk
    classified the file as reclaimable and the filesystem would not let it go,
    which is a permission or mount problem an operator has to fix, and at
    backlog scale it is the same answer thousands of times per spawn. One line
    per interval, carrying the count it stands for, is what makes it visible
    without making it the log.

    This is the ONE reporter and the ONE throttle for every such path: aliases
    and the lease records, holder sidecars and ownership sidecars that travel
    with them share the interval and the suppressed count, because they share
    the fault. *what* names the kind of file so the line stays honest about
    which one it saw, and the line names the operation and the errno it got:
    only a permission-class errno is diagnosed as an unwritable directory --
    a file that vanished between the walk and the unlink, or a descriptor
    limit, is a refusal too, but not that one.
    """
    global _UNLINK_WARNING_LAST, _UNLINK_WARNING_SUPPRESSED
    now = time.monotonic()
    with _UNLINK_WARNING_LOCK:
        if _UNLINK_WARNING_LAST and now - _UNLINK_WARNING_LAST < _UNLINK_WARNING_INTERVAL_SECS:
            _UNLINK_WARNING_SUPPRESSED += 1
            return
        suppressed = _UNLINK_WARNING_SUPPRESSED
        _UNLINK_WARNING_SUPPRESSED = 0
        _UNLINK_WARNING_LAST = now
    code = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno is not None else "?"
    reason = exc.strerror or exc.__class__.__name__
    if exc.errno in _UNWRITABLE_ERRNOS:
        logger.warning(
            "skill projection: cannot remove %s %s -- unlink refused, %s (%s); %d similar "
            "refusal(s) since the last report -- the directory %s is not writable by this "
            "process, so no backlog there can drain",
            what,
            path.name,
            code,
            reason,
            suppressed,
            path.parent,
        )
        return
    logger.warning(
        "skill projection: cannot remove %s %s -- unlink refused, %s (%s); %d similar "
        "refusal(s) since the last report, in the directory %s",
        what,
        path.name,
        code,
        reason,
        suppressed,
        path.parent,
    )


def _unlink_alias_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Unlink *path* only while it still names the classified alias inode.

    The caller holds the global projection lock, which excludes every product
    publisher. POSIX additionally pins the parent descriptor. Windows lacks
    unlink-at, so it performs one final no-link identity check before the
    by-name unlink; other platforms without a pinned walk retain the alias.

    A refusal from the filesystem itself is reported (rate-limited); every
    other ``False`` is an identity change and stays silent.
    """
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError as exc:
            _warn_unlink_refused(path, exc)
            return False
        try:
            return pinned_fs.unlink_verified(
                parent_fd,
                path.name,
                identity,
                on_error=lambda exc: _warn_unlink_refused(path, exc),
            )
        finally:
            os.close(parent_fd)

    if platform_compat.IS_WINDOWS:
        current = pinned_fs.lstat_by_name(path)
        if (
            current is None
            or platform_compat.is_link_or_junction(path)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            return False
        try:
            path.unlink()
        except OSError as exc:
            _warn_unlink_refused(path, exc)
            return False
        return True

    # An unknown non-Windows platform without descriptor-relative unlink has
    # neither the POSIX identity pin nor Windows' publication-lock contract.
    return False


def _is_legacy_projected_view(path: Path, alias_raw: bytes) -> bool:
    """Whether *path* is a projected view from a build that wrote no ownership.

    Builds shipped before this lifecycle published aliases with neither a
    metadata sidecar nor an in-spec marker, so :func:`_managed_metadata_for_alias`
    cannot admit them and a reclaim keyed on ownership alone leaves the ENTIRE
    accumulated backlog on disk -- the exact per-turn tool-spec cost this module
    exists to bound. Those aliases are still identifiable without a record: the
    name is Crew's own prefix plus the 24-hex digest :func:`prepare_native_skill_projection`
    derives, and a projected view always renames itself to that alias and carries
    no ``skill://`` resource (both are what the projection strips and rewrites).

    Deleting one cannot prove the pair unregenerable the way a recorded work
    directory can, so the safety argument is the caller's instead: every consumer
    re-prepares first -- the spawn argv, and ``session/set_mode``, which re-runs
    preparation before it sends the alias -- and the `/agent` command is refused
    rather than translated. A removal is therefore a cache eviction for a live
    pre-upgrade session, which republishes the same name WITH a record, and a
    reclaim for every dead work directory.

    Name and shape alone are NOT provenance, and an unlink is not undoable: an
    operator's own agent could in principle carry this name. So one POSITIVE mark
    the projection itself writes is also required -- Crew's managed
    ``kirocrew-core`` server entry, or the absolute steering resource pointing at
    THIS host's kiro home -- both of which the shipped builds that produced the
    backlog already write. An unrecorded view carrying neither is left alone; it
    is a smaller reclaim than the name shape would allow, and the right side to
    err on when the alternative is deleting a file somebody else authored.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(path.stem):
        return False
    try:
        spec = json.loads(alias_raw)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(spec, dict) or spec.get("name") != path.stem:
        return False
    resources = spec.get("resources", [])
    if not isinstance(resources, list):
        return False
    if any(isinstance(r, str) and r.startswith("skill://") for r in resources):
        return False
    servers = spec.get("mcpServers")
    if isinstance(servers, dict) and "kirocrew-core" in servers:
        return True
    try:
        steering = f"file://{kiro_home().as_posix()}/steering/**/*.md"
    except (OSError, ValueError, RuntimeError):
        return False
    return steering in resources


def _managed_metadata_for_alias(
    directory: Path, path: Path, alias_raw: bytes
) -> tuple[dict[str, Any], Path | None, tuple[int, int] | None, bytes | None] | None:
    """Load ownership outside the Kiro agent spec, or one legacy in-spec record."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    directory_info = pinned_fs.lstat_by_name(metadata_dir)
    if directory_info is not None:
        if platform_compat.is_link_or_junction(metadata_dir) or not stat.S_ISDIR(
            directory_info.st_mode
        ):
            return None
        metadata_path = metadata_dir / f"{path.stem}.json"
        metadata_info = pinned_fs.lstat_by_name(metadata_path)
        if metadata_info is not None:
            if platform_compat.is_link_or_junction(metadata_path) or not stat.S_ISREG(
                metadata_info.st_mode
            ):
                return None
            try:
                metadata_raw = safe_read_file_bytes(str(metadata_path))
            except FileTooLargeError:
                return None
            if metadata_raw is None:
                return None
            try:
                metadata = json.loads(metadata_raw)
            except (ValueError, TypeError, RecursionError):
                return None
            if not _managed_marker(metadata):
                return None
            if metadata.get(_MANAGED_ALIAS_SHA256) != hashlib.sha256(alias_raw).hexdigest():
                # The bytes moved. Still ours when the record binds the view's
                # IDENTITY and the file still says that: a launcher that
                # re-serialized it or re-stamped an ``env`` value changed no
                # part of what this module published.
                recorded_view = metadata.get(_MANAGED_VIEW_SHA256)
                alias_spec = _parsed_spec(alias_raw) if isinstance(recorded_view, str) else None
                if alias_spec is None or _view_digest(alias_spec) != recorded_view:
                    return None
            return (
                metadata,
                metadata_path,
                (metadata_info.st_dev, metadata_info.st_ino),
                metadata_raw,
            )

    # No released build wrote lifecycle keys INTO a spec -- kiro-cli denies
    # unknown fields, so the projection never could. An alias without a sidecar
    # is therefore unrecorded, and `_is_legacy_projected_view` decides it from
    # the name shape and the view's own form instead.
    return None


def _sidecar_record(
    directory: Path, stem: str
) -> tuple[dict[str, Any] | None, Path, tuple[int, int] | None]:
    """``(record, path, identity)`` of the ownership sidecar named *stem*, digest unchecked.

    ``record`` is the parsed sidecar when it is a regular, non-link, bounded,
    well-formed file carrying this lifecycle's marker; ``identity`` is set when
    ANY entry exists by that name (so a caller can tell "absent" from
    "present but not provably ours"). Unlike :func:`_managed_metadata_for_alias`
    this does not bind the record to alias bytes: it answers who wrote the
    record, for the two callers that need exactly that -- a pair whose alias
    bytes moved, and a sidecar whose alias is gone.
    """
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    path = metadata_dir / f"{stem}.json"
    directory_info = pinned_fs.lstat_by_name(metadata_dir)
    if directory_info is None:
        return None, path, None
    if platform_compat.is_link_or_junction(metadata_dir) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        return None, path, (directory_info.st_dev, directory_info.st_ino)
    info = pinned_fs.lstat_by_name(path)
    if info is None:
        return None, path, None
    identity = (info.st_dev, info.st_ino)
    if platform_compat.is_link_or_junction(path) or not stat.S_ISREG(info.st_mode):
        return None, path, identity
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None, path, identity
    if (opened.st_dev, opened.st_ino) != identity or len(raw) > _PROJECTION_LEASE_MAX_BYTES:
        return None, path, identity
    record = _parsed_spec(raw)
    if not _managed_marker(record):
        return None, path, identity
    return record, path, identity


def _sweep_orphan_sidecars(
    directory: Path, crew_home_id: str, *, skip: set[str], deadline: float
) -> int:
    """Remove this home's ownership sidecars whose alias is already gone.

    A sidecar outlived its alias whenever something other than the recorded
    pair removal took the alias: an older build's unrecorded path unlinked the
    alias alone, and so does an operator deleting ``kirocrew-skill-view-*.json``
    by hand. Each one is metadata only, but the directory is walked by
    kiro-cli's own directory scans, so the backlog is not free.

    A sidecar is removed only when its stem has the shape this module derives,
    nothing this call keeps or any live lease names it (*skip*), no alias by
    that name exists (any doubt reads as present), and the record carries the
    marker AND names THIS data home -- another home's record is that home's to
    retire. Runs under the publication lock, so no publisher can be mid-pair.
    Bounded by the same entry-walk limit as the alias walk, a random start
    offset, :data:`_SIDECAR_SWEEP_MAX_PER_RUN` and *deadline*. Returns how many
    it removed.
    """
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    info = pinned_fs.lstat_by_name(metadata_dir)
    if (
        info is None
        or platform_compat.is_link_or_junction(metadata_dir)
        or not stat.S_ISDIR(info.st_mode)
    ):
        return 0
    stems: list[str] = []
    walked = 0
    try:
        with os.scandir(metadata_dir) as entries:
            for entry in entries:
                if walked >= _PROJECTION_PRUNE_WORK_LIMIT:
                    break
                walked += 1
                if not entry.name.endswith(".json"):
                    continue
                stem = entry.name[: -len(".json")]
                if _LEGACY_ALIAS_NAME_RE.fullmatch(stem) and stem not in skip:
                    stems.append(stem)
    except OSError:
        logger.debug("skill projection: cannot scan %s for sidecars", metadata_dir, exc_info=True)
        return 0
    offset = _prune_start_offset(len(stems))
    doomed: list[tuple[str, dict[str, Any], Path, tuple[int, int]]] = []
    for stem in stems[offset:] + stems[:offset]:
        if len(doomed) >= _SIDECAR_SWEEP_MAX_PER_RUN or time.monotonic() >= deadline:
            break
        if _path_exists(directory / f"{stem}.json"):
            continue
        record, path, identity = _sidecar_record(directory, stem)
        if record is None or identity is None or record.get(_MANAGED_CREW_HOME) != crew_home_id:
            continue
        doomed.append((stem, record, path, identity))
    # The sidecar is the last record of which agent a view with no alias was
    # built from, and a conversation may still name that view. Its mapping goes
    # into the ledger first, in one write for the whole batch; a batch the
    # ledger cannot take is kept for a later run.
    ledger = _ViewLedgerWrites(directory, crew_home_id)
    views = {
        stem: str(record.get(_MANAGED_AGENT))
        for stem, record, _path, _identity in doomed
        if _admissible_source_agent(record.get(_MANAGED_AGENT))
    }
    if views and not ledger.retain_many(views):
        logger.debug("skill projection: view ledger unwritable; ownership sidecars kept")
        return 0
    removed = 0
    for stem, _record, path, identity in doomed:
        if time.monotonic() >= deadline:
            break  # the rest is recorded already and goes on a later run
        if _path_exists(directory / f"{stem}.json"):
            continue
        if _unlink_projection_lease_if_unchanged(path, identity, what="ownership sidecar"):
            removed += 1
    if removed:
        logger.info("skill projection: removed %d ownership sidecar(s) with no alias", removed)
    return removed


_ALIAS_LOCK_SUFFIX = ".lock"


def _unlink_unheld_lock_file(path: Path, identity: tuple[int, int]) -> bool:
    """Remove the lock file at *path* only while nobody holds it, and only that inode.

    A launcher may still hold (or re-take) an old ``<stem>.lock``; unlinking it then
    would let the next launcher lock a fresh inode while the first still holds the
    old one, and their rewrites would race. So the file is opened without
    following links, its identity checked, and an exclusive lock taken without
    waiting: a held lock is left alone, and the unlink happens while ours is held.

    The open is :func:`platform_compat.open_lock_file_for_sweep`, not a bare
    ``os.open``: on Windows a CRT descriptor omits ``FILE_SHARE_DELETE``, so our
    own verification handle would block the very ``unlink`` below with a sharing
    violation and the sweep would leave genuine residue behind. The helper opens
    with delete-sharing on Windows so the unlink lands while the handle (and its
    lock) is still held, matching POSIX, where an open fd never blocks an unlink.
    """
    try:
        fd = platform_compat.open_lock_file_for_sweep(path)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or opened.st_size != 0:
            return False
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            return False
        return _unlink_projection_lease_if_unchanged(path, identity, what="orphaned alias lock")
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _sweep_orphan_alias_locks(directory: Path, *, deadline: float) -> int:
    """Remove empty ``<alias>.lock`` files left beside aliases that are gone.

    This module never creates one. A launcher that rewrites every spec in the
    agents directory takes a per-file lock named ``<stem>.lock`` next to each
    spec and leaves it behind (one host: 1,661 of them beside 23 aliases). For an
    authored agent that is the launcher's file; for a skill-view alias the name is
    in THIS module's namespace and outlives the alias it guarded, so each prune of
    an alias strands one. Only that residue is removed: the exact derived name
    shape, a regular EMPTY file, no ``<stem>.json`` beside it, and older than the
    legacy reclaim age so a launcher pass that is still running is never raced.
    The same walk limit, random start, per-run ceiling and *deadline* as the
    sidecar sweep bound it. Returns how many it removed.
    """
    names: list[str] = []
    walked = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if walked >= _PROJECTION_PRUNE_WORK_LIMIT:
                    break
                walked += 1
                if entry.name.endswith(_ALIAS_LOCK_SUFFIX) and _LEGACY_ALIAS_NAME_RE.fullmatch(
                    entry.name[: -len(_ALIAS_LOCK_SUFFIX)]
                ):
                    names.append(entry.name)
    except OSError:
        logger.debug("skill projection: cannot scan %s for alias locks", directory, exc_info=True)
        return 0
    offset = _prune_start_offset(len(names))
    removed = 0
    for name in names[offset:] + names[:offset]:
        if removed >= _SIDECAR_SWEEP_MAX_PER_RUN or time.monotonic() >= deadline:
            break
        path = directory / name
        spec = directory / f"{name[: -len(_ALIAS_LOCK_SUFFIX)]}.json"
        info = pinned_fs.lstat_by_name(path)
        if (
            info is None
            or platform_compat.is_link_or_junction(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != 0
            or time.time() - info.st_mtime < _LEGACY_RECLAIM_MIN_AGE_SECS
            or _path_exists(spec)
        ):
            continue
        if _unlink_unheld_lock_file(path, (info.st_dev, info.st_ino)):
            removed += 1
    if removed:
        logger.info("skill projection: removed %d orphaned alias lock file(s)", removed)
    return removed


def _prune_start_offset(count: int) -> int:
    """Where this call begins its bounded walk over *count* candidates.

    A time-budgeted walk from a fixed start examines the same prefix every call,
    so unreclaimable candidates at the front would hide the rest of the list
    behind the budget permanently. Moving the start makes every entry OF THIS LIST reachable
    across calls. The list itself is the bounded window
    :func:`_projection_prune_candidates` yields, so an alias beyond that entry
    ceiling in a stably padded directory is not promised a turn: the rotation
    bounds lock time within the window, not eventual drain of the whole
    directory. It cannot be a cursor in memory: the workload this bound exists
    for spawns a fresh process per cron run, so a process-local cursor restarts at
    zero every time and rotates nothing.
    """
    if count <= 0:
        return 0
    return secrets.randbelow(count)


def _projection_prune_candidates(directory: Path, skip: set[str]) -> Iterator[Path]:
    """Yield bounded stale candidates without letting retained aliases starve them.

    Retained aliases do not consume the stale-work budget. Total directory
    traversal is bounded separately: one work-limit for stale candidates, one
    for ordinary authored entries, and at most one work-limit of retained-alias
    credit. A larger skip set therefore cannot turn membership into an unbounded
    traversal under the publication lock.
    """
    work_limit = _PROJECTION_PRUNE_WORK_LIMIT
    stale_work = 0
    walked = 0
    walk_ceiling = (2 * work_limit) + min(len(skip), work_limit)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if walked >= walk_ceiling or stale_work >= work_limit:
                    # INFO, like the lease-scan ceiling: past this window an alias
                    # is not promised a turn, so the deferral must be visible.
                    logger.info(
                        "skill projection: deferred remaining alias pruning after %d stale "
                        "candidate(s) across %d walked entr(ies)",
                        stale_work,
                        walked,
                    )
                    return
                # Count every entry before filtering. Otherwise authored files or
                # arbitrary padding can bypass the traversal ceiling just as
                # non-record files once bypassed the lease-scan ceiling.
                walked += 1
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                ):
                    continue
                stem = entry.name[: -len(".json")]
                if stem in skip:
                    continue
                stale_work += 1
                yield directory / entry.name
    except OSError:
        logger.debug("skill projection: cannot scan %s to prune aliases", directory, exc_info=True)


def _prune_stale_managed_aliases(
    directory: Path, crew_home_id: str, *, keep: set[str], sweep_sidecars: bool = True
) -> int:
    """Remove aliases owned by this Kiro Crew data home that no projection uses.

    Runs while the publication lock is held, so a deletion cannot land on an alias
    a publisher that takes that lock is writing; a build predating the lease takes
    no part in it, and the minimum age is what covers that one. A time budget keeps
    the held lock down to a slice of the walk rather than all of it. An alias is
    kept when this run publishes it, a projection in this process holds it, or a
    held lease in any process names it. Everything else this data home recorded
    is a cache entry for a projection that has ended: every consumer re-prepares
    before it sends an alias, so removing one costs the next spawn of that agent
    one rewrite and nothing else. Aliases are keyed on the agent's view, so a new
    one appears when an agent's spec changes, not once per run.

    With *sweep_sidecars* (the default) the rest of the same time budget then
    retires this home's ownership sidecars whose alias is gone
    (:func:`_sweep_orphan_sidecars`) and the empty ``<alias>.lock`` residue an
    external rewriter left beside gone aliases (:func:`_sweep_orphan_alias_locks`);
    the boot drain sweeps on its own instead, so it can count them. Returns how
    many aliases it removed.
    """
    # ONE bounded lease scan per prune, under the held publication lock, rather
    # than one full scan per candidate (which multiplied the candidate cap by the
    # lease-scan cap). It also reclaims crash/finalizer lease residue as a side
    # effect. Any uncertainty is fail-closed: authorize no deletion this run and
    # let a later spawn re-scan, because a candidate whose covering lease the scan
    # could not read must be treated as live.
    live_aliases, lease_uncertain = _scan_projection_leases(directory)
    if lease_uncertain:
        return 0
    active = _active_aliases()
    # Kept, active, and live-lease aliases are never reclaimable. The candidate
    # scan skips them WITHOUT charging them to the stale-work budget, so a run's
    # own just-published aliases (which sort early) cannot starve the backlog of
    # examination -- the order-dependent defect this reconciliation closes. The
    # scan still bounds total traversal at the class level plus this bounded skip
    # set, so the skip is not an unbounded bypass.
    skip = keep | active | live_aliases
    # Materialized so the time-budgeted walk below can start at a rotated offset.
    # The entry-walk limit inside _projection_prune_candidates bounds this list,
    # so the enumeration that precedes the budget is bounded too; rotation then
    # spreads a budgeted walk across that bounded window on successive spawns.
    candidates = list(_projection_prune_candidates(directory, skip))
    # The reclaim ceiling is expressed over the same skip accounting: each run
    # publishes len(keep) aliases and leaves that many behind when it ends, so
    # the cap covers that steady-state rate plus a bounded backlog drain. The
    # candidate scan already excludes the skip set, so every yielded candidate is
    # unretained -- it may still prove foreign, malformed or unowned -- and the
    # two ceilings account for distinct work.
    cap = _PRUNE_MAX_RECLAIMS_PER_RUN + len(keep)
    offset = _prune_start_offset(len(candidates))
    candidates = candidates[offset:] + candidates[:offset]
    deadline = time.monotonic() + _PRUNE_MAX_SECONDS_PER_RUN
    reclaimed = 0
    examined = 0
    ledger = _ViewLedgerWrites(directory, crew_home_id)
    for index, path in enumerate(candidates):
        if reclaimed >= cap:
            logger.debug(
                "skill projection: reclaim cap reached (%d); the rest drains on later spawns",
                cap,
            )
            break
        if time.monotonic() >= deadline:
            logger.debug(
                "skill projection: prune budget spent after %d candidate(s); the rest drains on later spawns",
                examined,
            )
            break
        # Counted for EVERY candidate, not only the reclaimed ones: what the budget
        # has to cover is the classification, which an unreclaimable entry pays in
        # full. The candidate scan already excluded kept, active and leased names.
        examined += 1
        ledger.look_ahead(candidates, index)
        if _reclaim_prune_candidate(directory, path, crew_home_id, ledger):
            reclaimed += 1
    if reclaimed > 0:
        logger.info("skill projection: reclaimed %d unused alias(es)", reclaimed)
    if sweep_sidecars:
        _sweep_orphan_sidecars(directory, crew_home_id, skip=skip, deadline=deadline)
        _sweep_orphan_alias_locks(directory, deadline=deadline)
    return reclaimed


def drain_stale_aliases() -> int:
    """Best-effort boot drain of unused aliases this data home owns; never raises.

    The per-spawn prune is capped, so a backlog of thousands takes hundreds of
    spawns to clear, and every spawn in between still lists the backlog in
    kiro-cli's subagent tool. This runs once at gateway boot and repeats that
    same prune, under the publication lock, until three batches in a row
    reclaim nothing or the batch limit is reached, so some unused aliases can
    remain for later spawns. Returns how many aliases it removed.
    """
    try:
        directory = kiro_agents_dir()
        crew_home_id = data_home().absolute().as_posix()
    except (OSError, ValueError, RuntimeError):
        logger.debug("skill projection: cannot resolve directories to drain", exc_info=True)
        return 0
    if pinned_fs.lstat_by_name(directory) is None:
        return 0
    total = 0
    sidecars = 0
    idle_batches = 0
    for batch in range(_DRAIN_MAX_BATCHES):
        if batch:
            time.sleep(_DRAIN_BATCH_PAUSE_SECS)
        swept = 0
        try:
            with _projection_alias_lock(directory):
                reclaimed = _prune_stale_managed_aliases(
                    directory, crew_home_id, keep=set(), sweep_sidecars=False
                )
                # Its own budget, the size of one prune walk's: a batch holds
                # the lock for at most two walks' worth, both bounded.
                sweep_deadline = time.monotonic() + _PRUNE_MAX_SECONDS_PER_RUN
                swept = _sweep_orphan_sidecars(
                    directory, crew_home_id, skip=_active_aliases(), deadline=sweep_deadline
                ) + _sweep_orphan_alias_locks(directory, deadline=sweep_deadline)
        except OSError as exc:
            # A concurrent spawn holding the lock, or a lock-file fault; either
            # way an idle batch, so a fault ends the drain at the idle bound.
            logger.debug("skill projection: drain batch skipped; lock or I/O error: %s", exc)
            reclaimed = 0
        except Exception:
            logger.warning("skill projection: drain failed", exc_info=True)
            break
        total += reclaimed
        sidecars += swept
        idle_batches = 0 if reclaimed or swept else idle_batches + 1
        if idle_batches >= _DRAIN_IDLE_BATCHES:
            break
    else:
        logger.warning(
            "skill projection: drain stopped at its batch bound (%d) after %d alias(es); "
            "the rest drains on later spawns or the next boot",
            _DRAIN_MAX_BATCHES,
            total,
        )
    if total > 0 or sidecars > 0:
        logger.info(
            "skill projection: boot drain removed %d unused alias(es) and %d orphaned "
            "ownership sidecar(s) or alias lock file(s)",
            total,
            sidecars,
        )
    return total


def _reclaim_prune_candidate(
    directory: Path, path: Path, crew_home_id: str, ledger: _ViewLedgerWrites
) -> bool:
    """Classify one stale candidate and remove it when ownership proves it is ours.

    The per-candidate step of :func:`_prune_stale_managed_aliases`, which charges
    each call against its time budget. The sidecar goes with its alias only once
    *ledger* holds the view's source agent (see :class:`_ViewLedgerWrites`);
    when the ledger cannot take it, the alias still goes and the sidecar, which
    then answers for the view on its own, stays for a later sweep. Returns
    whether the alias was removed.
    """
    candidate = pinned_fs.lstat_by_name(path)
    if candidate is None:
        return False
    identity = (candidate.st_dev, candidate.st_ino)
    try:
        raw = safe_read_file_bytes(str(path))
    except FileTooLargeError:
        return False
    if raw is None:
        return False
    managed = _managed_metadata_for_alias(directory, path, raw)
    if managed is None:
        record, sidecar_path, sidecar_identity = _sidecar_record(directory, path.stem)
        if sidecar_identity is not None:
            # A sidecar IS there; it just does not bind these bytes. That is not
            # the pre-lifecycle case below, whose premise is that no record
            # exists: a launcher re-serialized an alias this home
            # published before its record carried the view digest. Every build
            # that writes a sidecar also holds a lease for as long as it uses the
            # alias, so the caller's lease gate already covers the publish-to-
            # spawn window the legacy age exists for, and the pair goes together
            # -- but only when the record names THIS home and the file is still
            # a projected view. Anything else (another home's record, a record
            # that is not provably ours) keeps the alias.
            if (
                record is None
                or record.get(_MANAGED_CREW_HOME) != crew_home_id
                or not _is_legacy_projected_view(path, raw)
            ):
                return False
            current = pinned_fs.lstat_by_name(path)
            if current is None or (current.st_dev, current.st_ino) != identity:
                return False
            try:
                current_raw = safe_read_file_bytes(str(path))
            except FileTooLargeError:
                return False
            if current_raw != raw:
                return False
            if _sidecar_record(directory, path.stem) != (record, sidecar_path, sidecar_identity):
                return False
            recorded = ledger.retain(path.stem, record)
            if _unlink_alias_if_unchanged(path, identity):
                if recorded:
                    _unlink_projection_lease_if_unchanged(
                        sidecar_path, sidecar_identity, what="ownership sidecar"
                    )
                logger.debug("skill projection: pruned re-serialized alias %s", path.name)
                return True
            return False
        # No ownership record at all. A pre-lifecycle build wrote this, so
        # the recorded-pair proof is unavailable and the re-preparation
        # contract carries the removal instead (see _is_legacy_projected_view).
        # Every gate the caller applied still holds: it is not in this run's set,
        # no live projection claims it, and no held lease names it.
        if _is_legacy_projected_view(path, raw):
            if time.time() - candidate.st_mtime < _LEGACY_RECLAIM_MIN_AGE_SECS:
                # Possibly mid-publish by a build that holds no lease. A
                # negative age (clock moved) lands here too, which is the
                # safe side.
                return False
            current = pinned_fs.lstat_by_name(path)
            if current is None or (current.st_dev, current.st_ino) != identity:
                return False
            try:
                current_raw = safe_read_file_bytes(str(path))
            except FileTooLargeError:
                return False
            if current_raw != raw or not _is_legacy_projected_view(path, current_raw):
                return False
            if _managed_metadata_for_alias(directory, path, current_raw) is not None:
                # A concurrent preparation republished it WITH a record
                # between the two reads; that owner decides its lifetime.
                return False
            if _unlink_alias_if_unchanged(path, identity):
                logger.debug("skill projection: pruned unrecorded legacy alias %s", path.name)
                return True
            logger.debug("skill projection: legacy alias changed before removal: %s", path)
        return False
    metadata, metadata_path, metadata_identity, metadata_raw = managed
    if metadata.get(_MANAGED_CREW_HOME) != crew_home_id:
        return False

    # Re-open and revalidate the exact alias and ownership sidecar at
    # deletion time. A sidecar digest binds the ownership record to these
    # projected bytes; any replacement or uncertainty keeps both files.
    current = pinned_fs.lstat_by_name(path)
    if current is None or (current.st_dev, current.st_ino) != identity:
        return False
    try:
        current_raw = safe_read_file_bytes(str(path))
    except FileTooLargeError:
        return False
    if current_raw != raw:
        return False
    current_managed = _managed_metadata_for_alias(directory, path, current_raw)
    if current_managed is None:
        return False
    current_metadata, current_metadata_path, current_metadata_identity, current_metadata_raw = (
        current_managed
    )
    if (
        current_metadata != metadata
        or current_metadata_path != metadata_path
        or current_metadata_identity != metadata_identity
        or current_metadata_raw != metadata_raw
        or current_metadata.get(_MANAGED_CREW_HOME) != crew_home_id
    ):
        return False
    recorded = ledger.retain(path.stem, current_metadata)
    if _unlink_alias_if_unchanged(path, identity):
        if recorded and metadata_path is not None and metadata_identity is not None:
            _unlink_projection_lease_if_unchanged(metadata_path, metadata_identity)
        logger.debug("skill projection: pruned unused managed alias %s", path.name)
        return True
    logger.debug("skill projection: unused alias changed before removal: %s", path)
    return False


# Bounds on what the census RETAINS, not on what it counts: every retained
# collection has a ceiling and the result says when one was hit. The alias
# ceiling is the diagnostic's own memory budget, far above the backlogs that
# motivated the census (28k on one host) and comfortably below what a doctor run
# may hold in memory. The lease walk has no ceiling of its own: it is bounded by
# the reclaim scan's :data:`_PROJECTION_LEASE_SCAN_LIMIT`, charged per directory
# entry exactly as that scan charges it, so ONE constant decides both where the
# census stops and where every prune defers. Past either bound the diagnostic
# cannot say what a later reclaim pass will do and reports that instead of
# guessing.
_CENSUS_MAX_ALIASES = 65536


def census_projected_aliases(directory: Path) -> dict[str, int]:
    """Count the projected aliases in *directory* without touching any of them.

    A read-only census for diagnostics, in this module so the lease-record and
    sidecar shapes it reads are the ones :func:`_prune_stale_managed_aliases`
    reclaims by. Returns plain counts:

    ``total``
        regular ``<prefix>*.json`` files directly in *directory*;
    ``leased``
        of those, how many a lease record names. The record is read with the
        reclaim's own parse and NO lock is probed -- a probe would reclaim
        residue as a side effect, which a census must not do -- so this is
        "published by some projection", not "held right now": a crash-stale
        record counts here until the next spawn's probe reclaims it;
    ``foreign_home`` / ``foreign_leased``
        of the unreferenced and of the lease-named aliases respectively, how
        many an ownership sidecar attributes to a Kiro Crew data home other
        than this process's own -- spelled exactly as the publisher records it,
        so a caller cannot pass a differently normalised id. Two homes share
        one agents directory whenever they share ``~/.kiro``, and this
        gateway's reclaim skips the other home's aliases unconditionally, so
        neither bucket drains here and the leased one is not "held or
        crash-stale" from this gateway's point of view either;
    ``unreadable_leases``
        lease records the reclaim reads as uncertainty. When one is unreadable,
        :func:`_scan_projection_leases` reports uncertainty for the pass and
        nothing is reclaimed, so a diagnostic must not promise a drain;
    ``truncated``
        1 when the alias retention bound (:data:`_CENSUS_MAX_ALIASES`) was hit,
        so the other counts are floors, or when the lease directory holds more
        entries than the reclaim scan's :data:`_PROJECTION_LEASE_SCAN_LIMIT` --
        counted over every entry, not just records -- so every prune would
        defer and no drain is promised.

    Every read failure counts toward the side that claims less: an unreadable
    directory is an empty census, an unreadable sidecar is not foreign.
    """
    counts = {
        "total": 0,
        "leased": 0,
        "foreign_home": 0,
        "foreign_leased": 0,
        "unreadable_leases": 0,
        "truncated": 0,
    }
    crew_home_id = data_home().absolute().as_posix()
    stems: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                    and entry.is_file(follow_symlinks=False)
                ):
                    continue
                if len(stems) >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                stems.add(entry.name[: -len(".json")])
    except OSError:
        return counts
    counts["total"] = len(stems)
    if not stems:
        return counts

    named: set[str] = set()
    lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
    lease_entries = 0
    try:
        with os.scandir(lease_dir) as entries:
            for entry in entries:
                # Charge EVERY entry against the reclaim scan's own ceiling
                # before any suffix filter, exactly as _scan_projection_leases
                # does: a directory that caps that scan (records plus their
                # ``.hold`` sidecars and any padding) makes every prune defer,
                # so the census must report truncated there, not promise a drain.
                if lease_entries >= _PROJECTION_LEASE_SCAN_LIMIT:
                    counts["truncated"] = 1
                    break
                lease_entries += 1
                if not (
                    entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX)
                    and entry.is_file(follow_symlinks=False)
                ):
                    continue
                listed = _read_lease_record(Path(entry.path))
                if listed is None:
                    counts["unreadable_leases"] += 1
                    continue
                # Only stems this census retained: bounded by the alias ceiling.
                named.update(stems.intersection(listed))
    except FileNotFoundError:
        pass
    except OSError:
        counts["unreadable_leases"] += 1
    counts["leased"] = len(named)

    # Plain bounded reads, like the lease records: the hardened reader audits
    # every call, and a backlog is tens of thousands of sidecars.
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    for stem in stems:
        try:
            fd = os.open(metadata_dir / f"{stem}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
                continue
            metadata = json.loads(raw)
        except (OSError, ValueError, TypeError, RecursionError):
            continue
        if (
            _managed_marker(metadata)
            and isinstance(metadata.get(_MANAGED_CREW_HOME), str)
            and metadata[_MANAGED_CREW_HOME] != crew_home_id
        ):
            counts["foreign_leased" if stem in named else "foreign_home"] += 1
    return counts


def census_projection_residue(
    directory: Path, churning: dict[tuple[str, str], dict[str, set[str]]] | None = None
) -> dict[str, int]:
    """Count what the projection leaves around its aliases, touching nothing.

    The companion of :func:`census_projected_aliases` for the files that are
    not aliases, read-only and bounded by the same retention ceiling. Returns:

    ``sidecars`` / ``orphan_sidecars``
        ownership sidecars in the metadata directory, and of those how many
        name an alias that is not on disk;
    ``alias_locks``
        empty ``<alias>.lock`` files beside the aliases -- never written by this
        module; an external rewriter leaves one per spec it locked;
    ``rewritten``
        aliases whose bytes differ from the digest their own sidecar recorded:
        another program is rewriting the agents directory. That is the
        precondition of both the unbounded alias growth and the "not installed"
        failure, so a non-zero count is worth seeing before a user reports it;
    ``truncated``
        1 when a walk hit :data:`_CENSUS_MAX_ALIASES`, making the rest floors.
    """
    counts = {
        "sidecars": 0,
        "orphan_sidecars": 0,
        "alias_locks": 0,
        "rewritten": 0,
        "truncated": 0,
    }
    aliases: set[str] = set()
    lock_names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(aliases) + len(lock_names) >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                if not entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX):
                    continue
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    aliases.add(entry.name[: -len(".json")])
                elif entry.name.endswith(_ALIAS_LOCK_SUFFIX) and entry.is_file(
                    follow_symlinks=False
                ):
                    lock_names.append(entry.name)
    except OSError:
        return counts
    # Residue by the sweeper's own rule (_sweep_orphan_alias_locks): empty, older
    # than the legacy age, and no alias beside it. A live or non-empty lock is not.
    for name in lock_names:
        info = pinned_fs.lstat_by_name(directory / name)
        if (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and info.st_size == 0
            and time.time() - info.st_mtime >= _LEGACY_RECLAIM_MIN_AGE_SECS
            and name[: -len(_ALIAS_LOCK_SUFFIX)] not in aliases
        ):
            counts["alias_locks"] += 1
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    try:
        with os.scandir(metadata_dir) as entries:
            for entry in entries:
                if counts["sidecars"] >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    continue
                stem = entry.name[: -len(".json")]
                if not _LEGACY_ALIAS_NAME_RE.fullmatch(stem):
                    # Not a sidecar: the view ledger (_VIEW_LEDGER_NAME) lives here too.
                    continue
                counts["sidecars"] += 1
                if stem not in aliases:
                    if not counts["truncated"]:
                        counts["orphan_sidecars"] += 1
                    continue
                try:
                    fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
                    finally:
                        os.close(fd)
                    alias_fd = os.open(
                        directory / f"{stem}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        alias_raw = os.read(alias_fd, _CENSUS_ALIAS_MAX_BYTES + 1)
                    finally:
                        os.close(alias_fd)
                except OSError:
                    continue
                record = _parsed_spec(raw) if len(raw) <= _PROJECTION_LEASE_MAX_BYTES else None
                recorded = (
                    record.get(_MANAGED_ALIAS_SHA256)
                    if record is not None and _managed_marker(record)
                    else None
                )
                if len(alias_raw) > _CENSUS_ALIAS_MAX_BYTES:
                    # Not read whole, so its digest proves nothing either way.
                    continue
                if isinstance(recorded, str) and recorded != hashlib.sha256(alias_raw).hexdigest():
                    counts["rewritten"] += 1
                if record is not None and _managed_marker(record) and churning is not None:
                    _note_env_values(
                        churning,
                        (
                            str(record.get(_MANAGED_AGENT))[:_CHURNING_LABEL_MAX_CHARS],
                            str(record.get(_MANAGED_SOURCE))[:_CHURNING_LABEL_MAX_CHARS],
                        ),
                        _parsed_spec(alias_raw),
                    )
    except OSError:
        pass
    return counts


# The largest alias the census hashes whole; a bigger one is left unjudged.
_CENSUS_ALIAS_MAX_BYTES = 8 * 1024 * 1024


def _note_env_values(
    churning: dict[tuple[str, str], dict[str, set[str]]],
    group: tuple[str, str],
    view: dict[str, Any] | None,
) -> None:
    """Record, per agent and source, a digest of each non-volatile MCP env value."""
    servers = view.get("mcpServers") if isinstance(view, dict) else None
    if not isinstance(servers, dict):
        return
    volatile = volatile_env_keys()
    if group not in churning and len(churning) >= _CHURNING_GROUPS_MAX:
        return
    seen = churning.setdefault(group, {})
    for server, entry in servers.items():
        env = entry.get("env") if isinstance(entry, dict) else None
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if str(key) in volatile:
                continue
            # Bounded at insertion: a label is cut to a fixed length, and a group
            # keeps a fixed number of them, so a spec with a huge env map costs a
            # bounded amount. Rendering escapes it; see the doctor line.
            label = f"{server}.{key}"[:_CHURNING_LABEL_MAX_CHARS]
            if label not in seen and len(seen) >= _CHURNING_KEYS_PER_GROUP_MAX:
                continue
            values = seen.setdefault(label, set())
            if len(values) < 2:
                values.add(hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest())


def census_churning_env_keys(directory: Path) -> list[str]:
    """``<server>.<ENV_KEY>`` names whose value differs across one agent's aliases.

    Read-only and bounded like :func:`census_projection_residue`. A key a launcher
    re-stamps on every launch but that is not in :func:`volatile_env_keys` mints a
    new alias per launch; this is how an operator finds it and adds it to
    ``KIROCREW_SKILL_VIEW_VOLATILE_ENV``. A real edit to a value shows up once too.
    """
    churning: dict[tuple[str, str], dict[str, set[str]]] = {}
    census_projection_residue(directory, churning)
    names = {key for keys in churning.values() for key, values in keys.items() if len(values) > 1}
    return sorted(names)[:_CHURNING_ENV_KEYS_MAX]


_CHURNING_ENV_KEYS_MAX = 20
_CHURNING_GROUPS_MAX = 256
_CHURNING_KEYS_PER_GROUP_MAX = 64
_CHURNING_LABEL_MAX_CHARS = 120


def _is_current_publication(
    directory: Path, alias_path: Path, alias_raw: str, crew_home_id: str
) -> bool:
    """Whether *alias_path* already says *alias_raw* and carries this home's sidecar.

    "Says", not "holds byte for byte": the comparison is of the parsed specs, so
    a launcher that re-serialized the file does not force a rewrite of
    every alias, and its sidecar, on every preparation under the shared lock.
    Every VALUE still counts, ``env`` included -- a changed value is a changed
    spec kiro-cli must be given, unlike the alias name and the ownership proof,
    which deliberately ignore ``env`` values (:func:`_alias_identity`).
    """
    info = pinned_fs.lstat_by_name(alias_path)
    if (
        info is None
        or platform_compat.is_link_or_junction(alias_path)
        or not stat.S_ISREG(info.st_mode)
    ):
        return False
    try:
        existing = safe_read_file_bytes(str(alias_path))
    except FileTooLargeError:
        return False
    if existing is None:
        return False
    if existing != alias_raw.encode():
        parsed = _parsed_spec(existing)
        if parsed is None or _canonical_json(parsed) != _canonical_json(json.loads(alias_raw)):
            return False
    managed = _managed_metadata_for_alias(directory, alias_path, existing)
    return managed is not None and managed[0].get(_MANAGED_CREW_HOME) == crew_home_id


def _alias_identity(view: dict[str, Any]) -> dict[str, Any]:
    """The part of *view* an alias is named by: the view minus volatile env values.

    A launcher that re-injects a per-launch nonce into each agent file's server
    env (a fresh id every sandbox start) changes the view on every spawn, and
    naming the alias by it mints a new file per spawn without bound. So the values
    of :func:`volatile_env_keys` are left out; every other env value is in, as a
    digest. A rotated credential therefore names a NEW alias -- one kiro-cli has
    not loaded, so ``set_mode`` never activates a copy still carrying the old
    credential -- and two launch contexts with different credentials never share
    one. Keys stay in too: adding or removing a variable names a new alias.
    """
    servers = view.get("mcpServers")
    if not isinstance(servers, dict):
        return view
    volatile = volatile_env_keys()
    identity = dict(view)
    identity["mcpServers"] = {
        name: (
            {**entry, "env": _env_identity(entry["env"], volatile)}
            if isinstance(entry, dict) and isinstance(entry.get("env"), dict)
            else entry
        )
        for name, entry in servers.items()
    }
    return identity


def _announce_publication(path: Path, data: bytes) -> None:
    """Make a watcher that ignores renames notice the alias just published at *path*.

    ``atomic_write`` publishes by rename, and kiro-cli (every release since its
    agent-config hot reload landed in 2.10.0, 2.26.0 included) does not act on
    one: its watcher reloads only on a create, a DATA modification or a remove of
    a ``*.json`` in the agents directory, so the rename (a name modification) is
    dropped, and so are the temp file's own events (not ``*.json``), an attribute
    change and a close without a write. ``session/set_mode`` then looks only in
    what was loaded. A NEW alias renamed in after the process started therefore
    stays ``Mode ... not found`` indefinitely, and a KNOWN alias renamed over
    keeps serving its old content. Rewriting the same bytes in place is
    a data write, so it triggers a full rescan after a 500 ms quiet window
    (measured: visible ~0.7 s later); and because every byte written equals the
    byte already there and nothing is truncated, a reader racing it can never
    see a torn file. Best effort: the file must still hold exactly *data*, and
    any failure leaves it as published.
    """
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        logger.debug("skill projection: cannot reopen %s to announce it", path.name, exc_info=True)
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
            return
        current = b""
        while len(current) < len(data):
            chunk = os.read(fd, len(data) - len(current))
            if not chunk:
                break
            current += chunk
        if current != data:
            return
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        logger.debug("skill projection: cannot announce %s", path.name, exc_info=True)
    finally:
        try:
            os.close(fd)
        except OSError:
            # A deferred write error (network filesystems report one at close) is
            # no reason to fail a best-effort nudge, or the session start behind it.
            logger.debug("skill projection: close after announcing %s", path.name, exc_info=True)


def announce_alias(alias: str) -> None:
    """Rewrite published alias *alias* in place, byte for byte, so the host rescans.

    The recovery ``session/set_mode`` takes on a ``Mode ... not found`` for a
    fresh alias before it retries: see :func:`_announce_publication`. Only a name
    of the shape this module mints is touched. Blocking; best effort.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(alias):
        return
    path = kiro_agents_dir() / f"{alias}.json"
    try:
        data = safe_read_file_bytes(str(path))
    except (OSError, ValueError, FileTooLargeError):
        return
    if data:
        _announce_publication(path, data)


# MCP env keys whose VALUE a launcher re-stamps on every launch (a per-launch
# nonce, not a credential the agent's grants depend on), so a changed value is no
# change. Every other env value counts: a rotated credential is a different grant.
# Extended, never narrowed, by ``KIROCREW_SKILL_VIEW_VOLATILE_ENV`` (comma-separated
# key names), for a launcher this list does not know yet.
_VOLATILE_ENV_KEYS_DEFAULT = frozenset({"AIM_CREDS_AGENT_INJECTION"})
_VOLATILE_ENV_VAR = "KIROCREW_SKILL_VIEW_VOLATILE_ENV"


def volatile_env_keys() -> frozenset[str]:
    """The MCP env keys whose values identity digests ignore (see above)."""
    extra = os.environ.get(_VOLATILE_ENV_VAR, "")
    return _VOLATILE_ENV_KEYS_DEFAULT | {k.strip() for k in extra.split(",") if k.strip()}


def _env_identity(env: dict[Any, Any], volatile: frozenset[str]) -> dict[str, str | None]:
    """*env* as identity: every key, and a digest of each value but the volatile ones."""
    return {
        str(key): (
            None
            if str(key) in volatile
            else hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
        )
        for key, value in env.items()
    }


def prepare_native_skill_projection(
    work_dir: Path, *, enabled: bool | None = None, per_session_element: bool = True
) -> NativeSkillProjection | None:
    """Prepare native views after spec freshness admission, before spawning.

    Uses the existing workspace CLI settings channel. No home, identity store,
    session store or authored agent file is relocated or rewritten.

    ``per_session_element`` says how ``kirocrew-core`` reaches a search agent's
    sessions with this session's identity. ``True`` is the shared runtime: the
    mount re-declares the server as a per-session ``mcpServers`` element
    (:func:`session_mcp.kiro_control_plane_servers`) that REPLACES the spec's
    declaration, so a declaration the element cannot carry -- a ``disabledTools``
    list, a ``timeout``, a transport that is not stdio -- withholds the element,
    the view would stand on nothing, and the spec's part of that verdict refuses
    the view here. ``False`` is the direct client: one kiro-cli process for one
    session, the identity on the process environment, and the declaration
    mounted natively from the view with every restriction the view copies
    honoured by kiro-cli itself. No element replaces anything there, so a
    restriction on OTHER tools -- ``disabledTools: ["learn_add"]`` -- refuses
    nothing; only ``skill_search`` itself disabled or excluded still does.
    """
    directory = kiro_agents_dir()
    crew_home_id = data_home().absolute().as_posix()
    if enabled is None:
        enabled = os.environ.get("KIROCREW_NATIVE_SKILL_PROJECTION", "1") != "0"
    if not enabled:
        if not (work_dir / ".kiro" / "settings" / "cli.json").exists():
            return None
        try:
            with workspace_cli_settings_lock(work_dir) as locked_settings:
                _restore_inheritance(locked_settings, _settings(locked_settings, may_pause=False))
        except OSError:
            logger.warning(
                "skill projection: workspace settings lock unavailable during rollback",
                exc_info=True,
            )
        return None
    global_settings = _settings(kiro_home() / "settings" / "cli.json")
    # Whether kirocrew-core's per-session element can be mounted is decided
    # from the agent spec HERE and from the settings files BY THE MOUNT at each
    # session start (:func:`session_mcp.kiro_control_plane_servers`, whose
    # verdict the runtime refuses the session on), through one predicate shared
    # with the mount. The split follows what each source is: the spec is this
    # preparation's input and a restriction authored there is static for the
    # view's life, so it refuses the view now with the reason; the settings files
    # are written while a runtime is warm (the dashboard's tool toggle) and are
    # shared by every agent of the process, so a restriction there refuses the
    # search agent's SESSIONS, one at a time, naming the file -- never the spawn,
    # which every other agent's sessions ride on. This module reads no settings
    # file: the mount is their one reader.
    aliases: dict[str, str] = {}
    specs: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    errors: dict[str, str] = {}
    search_agents: set[str] = set()
    display_sizes: dict[str, tuple[int, str]] = {}
    for agent in list_agents(project_dir=str(work_dir)):
        if not agent.filename:
            continue
        source_dir = (
            project_agents_dir(str(work_dir)) if agent.scope == SCOPE_PROJECT else directory
        )
        source = source_dir / agent.filename
        spec = _read_agent_spec(source, operation="native_skill_projection", source="acp")
        if spec is None:
            continue
        display_sizes[agent.name] = (_display_text_bytes(spec), source.absolute().as_posix())
        view = copy.deepcopy(spec)
        _strip_alias_display_text(view)
        resources = view.get("resources", [])
        resources = resources if isinstance(resources, list) else []
        view["resources"] = [
            r for r in resources if not (isinstance(r, str) and r.startswith("skill://"))
        ]
        needs_search = agent.name == "kirocrew" or any(
            isinstance(r, str) and r.startswith("skill://") for r in resources
        )
        if needs_search:
            excluded = view.get("excludedTools", [])
            if isinstance(excluded, list) and any(
                isinstance(t, str)
                and (t == "@kirocrew-core" or fnmatch.fnmatchcase(_SEARCH_TOOL, t))
                for t in excluded
            ):
                errors[agent.name] = (
                    "skill_search is explicitly excluded; bounded skill discovery requires it"
                )
                continue
            # The bounded directory must have a loading path even for a custom spec
            # whose authored resources rely on native skill activation. Expose
            # only the read/search capability; do not grant server-wide tools or
            # change the author's approval policy.
            from kiro_crew.agent import managed_mcp_spec_entry

            servers = view.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                errors[agent.name] = "mcpServers must be an object"
                continue
            original_core = servers.get("kirocrew-core", {})
            if not isinstance(original_core, dict):
                errors[agent.name] = "kirocrew-core must be a server object"
                continue
            disabled = original_core.get("disabled", False)
            disabled_tools = original_core.get("disabledTools", [])
            if not isinstance(disabled, bool):
                errors[agent.name] = "kirocrew-core.disabled must be a boolean"
                continue
            if not isinstance(disabled_tools, list) or any(
                not isinstance(tool, str) for tool in disabled_tools
            ):
                errors[agent.name] = "kirocrew-core.disabledTools must be a list of strings"
                continue
            if disabled or "skill_search" in disabled_tools:
                errors[agent.name] = "skill_search is disabled; bounded skill discovery requires it"
                continue
            # The verdict the mount will reach for the spec's part, reached here
            # first, over the declaration AS AUTHORED. The managed replacement
            # built below carries only the keys a per-session element can
            # express, so judging it would let a ``type`` or a key the element
            # cannot carry vanish unjudged and the control plane mount despite the
            # restriction; the mount's own arms read the declaration itself, and
            # so does this. A restriction authored in the spec refuses the view:
            # the view would drop this agent's skill resources on the promise of
            # an element the mount can never mount for it. The settings files are
            # deliberately NOT read here -- see the note above the loop -- so a
            # spec that is clean gets its view whatever the files say today, and
            # the mount asks the full question over the files at every session
            # start. A broker stub ``session/new`` would mount ahead of the
            # element is not a source either: a stub element carries a
            # kiro-cli-only restriction no better than the native element does,
            # and its overlay is written from the spec and the global file alone,
            # so the sources decide, and only the sources.
            #
            # The question exists only where an element will REPLACE the
            # declaration. The direct client mounts none: kiro-cli loads the
            # view's entry itself -- the managed launch plus the restrictions
            # copied onto it below -- and honours them natively, with the
            # session's identity on the process environment. There a
            # ``disabledTools`` naming other tools, or a ``timeout``, restricts
            # exactly what its author meant and leaves ``skill_search`` standing
            # (the checks above already refused the view if it did not), so
            # withholding the view for it would refuse a supported customization
            # and abort the spawn over a restriction that reaches the session.
            if per_session_element:
                withheld = session_mcp.native_mount_withholding("kirocrew-core", original_core, [])
                if withheld is not None:
                    errors[agent.name] = withheld.explain("skill search")
                    continue
            entry = managed_mcp_spec_entry("kirocrew-core")
            if entry is None:
                errors[agent.name] = "Crew's managed skill search server is unavailable"
                continue
            for key in ("autoApprove", "disabledTools", "timeout"):
                if key in original_core:
                    entry[key] = original_core[key]
            servers["kirocrew-core"] = entry
            tools = view.get("tools", [])
            if tools != "*" and isinstance(tools, list):
                if not any(t in tools for t in ("*", "@kirocrew-core", _SEARCH_TOOL)):
                    view["tools"] = [*tools, _SEARCH_TOOL]
            search_agents.add(agent.name)
        prompt = view.get("prompt")
        if isinstance(prompt, str) and prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            if not path.is_absolute():
                view["prompt"] = "file://" + (source.parent / path).absolute().as_posix()
        specs[agent.name] = view
        sources[agent.name] = source.absolute().as_posix()

    _warn_on_display_text(display_sizes)
    try:
        alias_lock = _projection_alias_lock(directory)
    except OSError:
        logger.warning(
            "skill projection: alias lock unavailable; retaining aliases, settings, and using "
            "authored agents",
            exc_info=True,
        )
        # `local` was read before agent enumeration and lock acquisition. A
        # concurrent projection can write a newer overlay or unrelated setting
        # while this process waits, so writing this stale snapshot would clobber
        # that update. Keep the current file byte-for-byte; a later successful
        # preparation or explicit rollback can update it under normal ownership.
        return None
    with alias_lock:
        try:
            settings_lock = workspace_cli_settings_lock(work_dir)
            with settings_lock as locked_settings:
                # This is the authoritative read for both projected resources and
                # the write below. Every in-product workspace cli.json writer uses
                # the same sidecar lock, so no effort or Tool Search update can land
                # between this read and commit.
                local = _settings(locked_settings, may_pause=False)
                inherited, preference_source, overlaid = _inheritance_preference(
                    local, global_settings
                )
                if not overlaid:
                    local[_PREVIOUS_INHERITANCE] = {
                        "present": _INHERIT_SETTING in local,
                        "value": local.get(_INHERIT_SETTING),
                    }

                if inherited:
                    for view in specs.values():
                        for resource in (
                            f"file://{kiro_home().as_posix()}/steering/**/*.md",
                            "file://.kiro/steering/**/*.md",
                            "file://AGENTS.md",
                        ):
                            if resource not in view["resources"]:
                                view["resources"].append(resource)

                # The alias is named by what the view says, not by where it is
                # used: spawns that derive the same view -- any run folder, any
                # session -- share one file, and the directory holds one view per
                # distinct view content instead of one per agent per run. Views
                # can still differ per workspace: a SCOPE_PROJECT agent's prompt
                # is a file:// path under its project, and workspace-local
                # inheritance shapes the resources, so those get one alias per
                # workspace. The agent name is hashed too, so two agents with
                # identical specs still get distinct aliases, and so is the Crew
                # data home, so two homes sharing one agents directory never
                # contend for (and re-own) the same file. MCP server env VALUES
                # are left out of the name (see _alias_identity): a tool that
                # rewrites a fresh per-write value into every agent file would
                # otherwise mint a new alias on every spawn. The source spec's
                # path is in the name instead, so two agent files that differ
                # only in env values -- the same project agent copied into two
                # workspaces with its own token each -- never share one alias.
                ownership: dict[str, dict[str, Any]] = {}
                for agent_name, view in list(specs.items()):
                    view.pop("name", None)
                    digest = hashlib.sha256(
                        json.dumps(
                            {
                                "agent": agent_name,
                                "home": crew_home_id,
                                "source": sources[agent_name],
                                "view": _alias_identity(view),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()[:24]
                    alias = NATIVE_SKILL_ALIAS_PREFIX + digest
                    specs[agent_name] = {"name": alias, **view}
                    aliases[agent_name] = alias
                    ownership[alias] = {
                        _MANAGED_MARKER: _MANAGED_MARKER_VALUE,
                        _MANAGED_CREW_HOME: crew_home_id,
                        _MANAGED_AGENT: agent_name,
                        _MANAGED_SOURCE: sources[agent_name],
                    }

                metadata_dir = _ensure_projection_metadata_directory(directory) if aliases else None
                lease_stack = _acquire_projection_lease(directory, set(aliases.values()))
                try:
                    for agent_name, alias in aliases.items():
                        alias_raw = json.dumps(specs[agent_name], ensure_ascii=False)
                        metadata = {
                            **ownership[alias],
                            _MANAGED_ALIAS_SHA256: hashlib.sha256(alias_raw.encode()).hexdigest(),
                            _MANAGED_VIEW_SHA256: _view_digest(specs[agent_name]),
                        }
                        alias_path = directory / f"{alias}.json"
                        if _is_current_publication(directory, alias_path, alias_raw, crew_home_id):
                            # Another spawn already published this spec with
                            # this home's record; keep its inode as is.
                            continue
                        atomic_write(alias_path, alias_raw, restrict_to_owner=True)
                        # Renamed into place, which kiro-cli does not reload on.
                        _announce_publication(alias_path, alias_raw.encode())
                        assert metadata_dir is not None
                        atomic_write(
                            metadata_dir / f"{alias}.json",
                            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                            restrict_to_owner=True,
                        )
                    if metadata_dir is not None:
                        try:
                            _record_view_ledger(metadata_dir, aliases)
                        except OSError:
                            # The ledger only serves names stored before a
                            # restart; losing one write never blocks a spawn.
                            logger.debug("skill projection: view ledger not written", exc_info=True)
                    local[_MANAGED_SETTING] = inherited
                    local[_INHERIT_SOURCE] = preference_source
                    local[_INHERIT_SETTING] = True
                    atomic_write(locked_settings, json.dumps(local, indent=2))
                    prepared = NativeSkillProjection(aliases, specs, errors, search_agents)
                    _remember_view_sources(aliases)
                    prepared._lease_finalizer = weakref.finalize(prepared, lease_stack.close)
                except BaseException:
                    lease_stack.close()
                    raise
        except OSError:
            logger.warning(
                "skill projection: workspace settings or lease lock unavailable; retaining "
                "aliases and using authored agents",
                exc_info=True,
            )
            return None
        _register_active_projection(prepared)
        if _per_spawn_prune_due(directory):
            _prune_stale_managed_aliases(directory, crew_home_id, keep=set(aliases.values()))

    return prepared
