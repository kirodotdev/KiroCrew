"""Which agent a skill-view name projects.

The bounded in-process map, the on-disk view ledger and the writes it takes ahead of
every sidecar deletion, the sidecar fallback, and the resolvers that raise
:class:`RetiredSkillView` when nothing records the source.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import threading
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp.skill_projection_parts.side_files import (
    _LEGACY_ALIAS_NAME_RE,
    _metadata_dirs,
    _projection_metadata_dir,
)
from kiro_crew.acp.skill_projection_parts.view_ownership import (
    _MANAGED_AGENT,
    _MANAGED_CREW_HOME,
    _managed_marker,
    _same_crew_home,
    _sidecar_record,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.validation import is_registered_agent_name

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


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
            "mate again, or start a new chat; a gateway restart rebuilds every view."
        )


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
RetiredSkillView.__module__ = "kiro_crew.acp.skill_projection"


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


def _import_legacy_ledger(metadata_dir: Path, legacy: dict[str, str]) -> None:
    """Copy *legacy* ledger entries this home's ledger lacks, as its OLDEST entries.

    The destination already holds what this home recorded or recovered, newest
    last. An import that made the legacy names the newest would evict, past the
    bound, the very mappings a sidecar-less recovery just wrote there. So only
    names the destination does not hold are taken, ahead of its own entries, and
    only as many as the bound leaves room for; the rest stay in the legacy file,
    which is kept while it holds anything. Nothing is written when there is nothing
    to add, so a boot that finds the ledger already imported leaves it untouched.
    """
    held = _read_view_ledger(metadata_dir)
    room = _VIEW_SOURCES_MAX - len(held)
    missing = [(alias, agent) for alias, agent in legacy.items() if alias not in held]
    taken = missing[-room:] if room > 0 else []
    if not taken:
        return
    merged = {**dict(taken), **held}
    atomic_write(
        metadata_dir / _VIEW_LEDGER_NAME,
        json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
        restrict_to_owner=True,
    )


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
        self._metadata_dir = _projection_metadata_dir()
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
            if record is None or not _same_crew_home(
                record.get(_MANAGED_CREW_HOME), self._crew_home_id
            ):
                continue
            agent_name = record.get(_MANAGED_AGENT)
            if _admissible_source_agent(agent_name):
                views[candidate.stem] = str(agent_name)
        return views


def _recorded_view_source(name: str) -> str | None:
    """The agent a view's sidecar or the view ledger names, or ``None``. Blocking.

    Only a name of the exact shape the projection mints is looked up, so the name
    cannot steer the read outside the sidecar directory. The alias itself may be
    gone; the sidecar's agent field is read on its own, and it must carry the
    managed marker and name an agent rather than another view. When the boot
    drain has removed the sidecar too, the ledger still answers.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(name):
        return None
    try:
        metadata_dirs = _metadata_dirs(kiro_agents_dir())
    except (OSError, ValueError, RuntimeError):
        return None
    # This home's own records first, then the legacy ones in the agents
    # directory: a name stored before the side files moved may have its record
    # only there until the boot migration carries it over.
    for metadata_dir in metadata_dirs:
        metadata = _read_json_regular_file(metadata_dir / f"{name}.json")
        if _managed_marker(metadata):
            agent_name = metadata.get(_MANAGED_AGENT)
            if _admissible_source_agent(agent_name):
                return str(agent_name)
    for metadata_dir in metadata_dirs:
        source = _read_view_ledger(metadata_dir).get(name)
        if source is not None:
            return source
    return None


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
